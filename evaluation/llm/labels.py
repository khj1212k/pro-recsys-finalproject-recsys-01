"""사람 라벨 저장소 - ADR 0009, 라벨 정의는 docs/eval/labeling-guide.md.

data/labels/<run>/ 아래(gitignore):
  clusters.jsonl, outputs.jsonl, relabel.json   <- evaluation/llm/bakeoff.py export-blind 또는 e0_labels가 만든 입력
  retention.json                                <- 라벨 마감(본문 보존 기한, ADR 0023 개정). E0 내보내기가 쓴다
  cluster_labels.jsonl, output_labels.jsonl     <- 이 모듈이 쓰는 라벨 (append-only)

마감(retention.json의 expires_at_utc)이 지나면 LabelStore를 열 때 clusters.jsonl의 기사 본문을 지운다(제목·URL·id와
라벨은 남는다). 라벨링 UI는 마감 뒤에는 과제를 내주지 않는다. E0 자동 라벨러(ADR 0009 A8.13)의 라벨 파일에는 기사 문장 조각이
든 칸이 둘 있다 - 라벨러의 자유 텍스트(auto/*/rationales.jsonl)와 클러스터 라벨의 핵심 사실 문장(key_facts[].text). 둘 다
같은 때에 지운다(핵심 사실은 길이·해시로 바꾼다).

라벨 행: {"target_id", "round": 1|2, "labeled_at": ISO8601, "label": {...}}. 같은 (target, round)를
다시 저장하면 마지막 행이 이긴다(이력은 남는다). 2차(round 2)는 intra-rater 재라벨이다:
relabel.json에 있는 대상만, 1차 라벨 후 min_hours_between_rounds가 지나야 받는다.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

FIELDS = ("title", "sentence", "content")
RETENTION_FILE = "retention.json"


class LabelError(ValueError):
    """라벨이 가이드의 규칙을 어겼다(저장하지 않음)."""


class KeyFact(BaseModel):
    text: str
    source_ids: List[int] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("핵심 사실 문장이 비어 있습니다")
        return v.strip()


class ClusterLabel(BaseModel):
    single_event: bool
    outlier_ids: List[int] = Field(default_factory=list)
    key_facts: List[KeyFact] = Field(default_factory=list, max_length=6)
    notes: str = ""


class FactError(BaseModel):
    field: Literal["title", "sentence", "content"]
    start: int = Field(ge=0)
    end: int
    text: str
    type: Literal["number", "date", "entity", "quote", "other"] = "other"
    note: str = ""


class OutputLabel(BaseModel):
    fact_errors: List[FactError] = Field(default_factory=list)
    key_facts_covered: List[int] = Field(default_factory=list)
    style: int = Field(ge=1, le=5)
    publishable: bool
    tone_drift: bool
    notes: str = ""


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def read_retention(labels_dir: Path) -> Optional[dict]:
    path = Path(labels_dir) / RETENTION_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def has_bodies(labels_dir: Path) -> bool:
    return any(a.get("body") for r in _read_jsonl(Path(labels_dir) / "clusters.jsonl") for a in r.get("articles", []))


def is_expired(labels_dir: Path, now: datetime) -> bool:
    retention = read_retention(labels_dir)
    if not retention:
        return False
    expires = datetime.fromisoformat(retention["expires_at_utc"].replace("Z", "+00:00"))
    return now >= expires


AUTO_DIR = "auto"                              # <labels_dir>/auto/<프로토콜 id>/ - E0 자동 라벨러(ADR 0009 A8.13)
AUTO_FREE_TEXT_FILE = "rationales.jsonl"       # 라벨러가 쓴 근거 텍스트(응답 전체). 패키지 마감에 지운다


def _redact_key_facts(path: Path) -> int:
    """클러스터 라벨 파일의 핵심 사실 문장(`key_facts[].text`)을 길이(`n_chars`)와 해시(`sha256_16`)로 바꾸고 바꾼 사실 수를 돌려준다.

    핵심 사실은 라벨러가 기사를 읽고 쓴 문장이라 기사 문장의 조각이 그대로 들어갈 수 있다(E0에서 137개 중 44개가 기사 제목·본문과
    16자 조각이 같았다). 사실의 개수와 `source_ids`는 남으므로 커버리지 분모와 근거 id는 그대로 읽힌다. 이미 지운 칸(`text`가 비었다)은
    건드리지 않는다 - 여러 번 불러도 같다.
    """
    rows = _read_jsonl(path)
    changed = 0
    for row in rows:
        for fact in (row.get("label") or {}).get("key_facts") or []:
            text = fact.get("text")
            if text:
                fact["n_chars"] = len(text)
                fact["sha256_16"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                fact["text"] = ""
                changed += 1
    if changed:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        os.replace(tmp, path)
    return changed


def purge_auto_free_text(labels_dir: Path, now: datetime) -> int:
    """마감이 지났으면 자동 라벨러가 쓴 문장을 지우고 지운 것의 수를 돌려준다(여러 번 불러도 같다).

    지우는 것: 자유 텍스트 파일(`auto/*/rationales.jsonl`, 파일 수)과 클러스터 라벨 파일(`auto/*/run-*/cluster_labels.jsonl`)의
    핵심 사실 문장(사실 수). 남기는 것: 라벨 값 파일과 호출 기록의 id·값·수치·길이·해시. 출력 라벨의 `fact_errors[].text`는 우리가
    생성한 초안의 부분 문자열이라 초안(기한 없음, ADR 0023)과 같은 대접이다.
    """
    labels_dir = Path(labels_dir)
    if not is_expired(labels_dir, now):
        return 0
    removed = 0
    for path in sorted((labels_dir / AUTO_DIR).glob(f"*/{AUTO_FREE_TEXT_FILE}")):
        path.unlink()
        removed += 1
    for path in sorted((labels_dir / AUTO_DIR).glob("*/run-*/cluster_labels.jsonl")):
        removed += _redact_key_facts(path)
    return removed


def purge_expired_bodies(labels_dir: Path, now: datetime) -> int:
    """마감이 지났으면 clusters.jsonl의 본문과 자동 라벨러가 쓴 문장(자유 텍스트 파일, 핵심 사실 문장)을 지우고 지운 건수(본문을
    비운 기사 + `purge_auto_free_text`가 센 것)를 돌려준다(여러 번 불러도 같다)."""
    labels_dir = Path(labels_dir)
    if not is_expired(labels_dir, now):
        return 0
    removed_files = purge_auto_free_text(labels_dir, now)
    rows = _read_jsonl(labels_dir / "clusters.jsonl")
    blanked = 0
    for row in rows:
        for article in row.get("articles", []):
            if article.get("body"):
                article["body"] = None
                blanked += 1
    if blanked:
        tmp = labels_dir / "clusters.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, labels_dir / "clusters.jsonl")
    return blanked + removed_files


class LabelStore:
    def __init__(self, labels_dir: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 label_files_dir: Optional[Path] = None):
        self.dir = Path(labels_dir)
        # 라벨 파일(*_labels.jsonl)이 입력(clusters.jsonl 등)과 다른 곳에 있을 때 - E0 자동 라벨러는 런마다
        # <labels_dir>/auto/<프로토콜>/run-a|run-b/에 같은 형식으로 쓴다(ADR 0009 A8.13). 없으면 labels_dir.
        self.label_files_dir = Path(label_files_dir) if label_files_dir is not None else self.dir
        self.now = now
        # 마감이 지났으면 여는 김에 본문을 지운다(ADR 0023 개정: 열 때마다 정리)
        self.expired = is_expired(self.dir, self.now())
        if self.expired:
            purge_expired_bodies(self.dir, self.now())
        self.clusters: Dict[str, dict] = {r["item_id"]: r for r in _read_jsonl(self.dir / "clusters.jsonl")}
        self.outputs: List[dict] = _read_jsonl(self.dir / "outputs.jsonl")
        self.outputs_by_id: Dict[str, dict] = {o["output_id"]: o for o in self.outputs}
        relabel_path = self.dir / "relabel.json"
        self.relabel = (json.loads(relabel_path.read_text(encoding="utf-8")) if relabel_path.exists()
                        else {"outputs": [], "clusters": [], "min_hours_between_rounds": 48})

    # -- 읽기 ---------------------------------------------------------------

    def _path(self, kind: str) -> Path:
        if kind not in ("cluster", "output"):
            raise LabelError(f"알 수 없는 라벨 종류: {kind}")
        return self.label_files_dir / f"{kind}_labels.jsonl"

    def latest(self, kind: str, round_: int) -> Dict[str, dict]:
        """target_id -> 마지막으로 저장된 라벨 행."""
        out: Dict[str, dict] = {}
        for row in _read_jsonl(self._path(kind)):
            if row["round"] == round_:
                out[row["target_id"]] = row
        return out

    def first_labeled_at(self, kind: str, target_id: str) -> Optional[datetime]:
        times = [_parse_ts(r["labeled_at"]) for r in _read_jsonl(self._path(kind))
                 if r["target_id"] == target_id and r["round"] == 1]
        return min(times) if times else None

    def cluster_article_ids(self, item_id: str) -> List[int]:
        return [int(a["raw_news_id"]) for a in self.clusters[item_id]["articles"]]

    def cluster_key_facts(self, item_id: str) -> List[dict]:
        """출력 라벨의 커버리지 기준은 1차 클러스터 라벨의 핵심 사실이다."""
        row = self.latest("cluster", 1).get(item_id)
        return row["label"]["key_facts"] if row else []

    # -- 과제 순서 ------------------------------------------------------------

    def targets(self, kind: str, round_: int) -> List[str]:
        if kind == "cluster":
            ids = sorted(self.clusters)
            return ids if round_ == 1 else [i for i in ids if i in set(self.relabel["clusters"])]
        ids = [o["output_id"] for o in self.outputs]  # export-blind가 섞어 둔 순서 그대로
        return ids if round_ == 1 else [i for i in ids if i in set(self.relabel["outputs"])]

    def next_task(self, round_: int) -> Optional[dict]:
        """클러스터 라벨을 먼저 모두 단다 - 출력을 보기 전에 핵심 사실을 고정하기 위해(ADR 0009)."""
        for kind in ("cluster", "output"):
            done = self.latest(kind, round_)
            for tid in self.targets(kind, round_):
                if tid not in done and (round_ == 1 or self._round2_ready(kind, tid)):
                    return {"kind": kind, "target_id": tid}
        return None

    def progress(self, round_: int) -> dict:
        return {kind: {"done": len(self.latest(kind, round_)), "total": len(self.targets(kind, round_))}
                for kind in ("cluster", "output")}

    def _round2_ready(self, kind: str, target_id: str) -> bool:
        first = self.first_labeled_at(kind, target_id)
        wait = timedelta(hours=float(self.relabel.get("min_hours_between_rounds", 48)))
        return first is not None and self.now() - first >= wait

    # -- 쓰기 ---------------------------------------------------------------

    def save(self, kind: str, target_id: str, round_: int, payload: dict) -> dict:
        if round_ not in (1, 2):
            raise LabelError("round는 1 또는 2")
        if round_ == 2:
            if target_id not in self.targets(kind, 2):
                raise LabelError(f"{target_id}는 재라벨 대상이 아닙니다")
            if not self._round2_ready(kind, target_id):
                raise LabelError("1차 라벨이 없거나 재라벨 최소 간격이 지나지 않았습니다")
        try:
            label = self._validate(kind, target_id, payload)
        except ValidationError as e:
            raise LabelError(str(e)) from e
        row = {"target_id": target_id, "round": round_, "labeled_at": self.now().isoformat(),
               "label": label}
        path = self._path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def _validate(self, kind: str, target_id: str, payload: dict) -> dict:
        if kind == "cluster":
            if target_id not in self.clusters:
                raise LabelError(f"없는 클러스터: {target_id}")
            label = ClusterLabel.model_validate(payload)
            ids = set(self.cluster_article_ids(target_id))
            bad = [i for i in label.outlier_ids if i not in ids]
            bad += [i for f in label.key_facts for i in f.source_ids if i not in ids]
            if bad:
                raise LabelError(f"이 클러스터에 없는 기사 id: {sorted(set(bad))}")
            # 규칙은 docs/eval/labeling-guide.md "클러스터 라벨"과 같다
            if label.single_event and label.outlier_ids:
                raise LabelError("단일 사건(Y)은 이상치가 없는 클러스터입니다 - 이상치가 있으면 N으로 표시하세요")
            if label.single_event and not 3 <= len(label.key_facts) <= 6:
                raise LabelError("단일 사건 클러스터는 핵심 사실 3~6개가 필요합니다")
            if label.key_facts and not 3 <= len(label.key_facts) <= 6:
                raise LabelError("핵심 사실은 적지 않거나(주된 사건 없음) 3~6개를 적습니다")
            outliers = set(label.outlier_ids)
            only_outliers = [f.text for f in label.key_facts if set(f.source_ids) <= outliers]
            if only_outliers:
                raise LabelError(f"이상치 기사만 근거로 든 핵심 사실: {only_outliers}")
            return label.model_dump()

        out = self.outputs_by_id.get(target_id)
        if out is None:
            raise LabelError(f"없는 출력: {target_id}")
        label = OutputLabel.model_validate(payload)
        for err in label.fact_errors:
            field_text = out["draft"].get(err.field) or ""
            if not (err.start < err.end <= len(field_text)) or field_text[err.start:err.end] != err.text:
                raise LabelError(f"사실 오류 구간이 {err.field} 원문과 맞지 않습니다: {err.text!r}")
        n_facts = len(self.cluster_key_facts(out["item_id"]))
        if self.latest("cluster", 1).get(out["item_id"]) is None:
            raise LabelError("이 출력의 클러스터 라벨(1차)을 먼저 달아야 합니다")
        bad = [i for i in label.key_facts_covered if not 0 <= i < n_facts]
        if bad:
            raise LabelError(f"없는 핵심 사실 번호: {bad}")
        return label.model_dump()


def load_labels(labels_dir: Path, label_files_dir: Optional[Path] = None) -> dict:
    """분석용: {"cluster": {1: {id: label}, 2: {...}}, "output": {...}} (마지막 저장값)."""
    store = LabelStore(labels_dir, label_files_dir=label_files_dir)
    return {kind: {r: {tid: row["label"] for tid, row in store.latest(kind, r).items()} for r in (1, 2)}
            for kind in ("cluster", "output")}
