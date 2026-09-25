"""사람 라벨 저장소 - ADR 0009, 라벨 정의는 docs/eval/labeling-guide.md.

data/labels/<run>/ 아래(gitignore):
  clusters.jsonl, outputs.jsonl, relabel.json   <- evaluation/llm/bakeoff.py export-blind가 만든 입력
  cluster_labels.jsonl, output_labels.jsonl     <- 이 모듈이 쓰는 라벨 (append-only)

라벨 행: {"target_id", "round": 1|2, "labeled_at": ISO8601, "label": {...}}. 같은 (target, round)를
다시 저장하면 마지막 행이 이긴다(이력은 남는다). 2차(round 2)는 intra-rater 재라벨이다:
relabel.json에 있는 대상만, 1차 라벨 후 min_hours_between_rounds가 지나야 받는다.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

FIELDS = ("title", "sentence", "content")


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


class LabelStore:
    def __init__(self, labels_dir: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.dir = Path(labels_dir)
        self.now = now
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
        return self.dir / f"{kind}_labels.jsonl"

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
            if label.single_event and not 3 <= len(label.key_facts) <= 6:
                raise LabelError("단일 사건 클러스터는 핵심 사실 3~6개가 필요합니다")
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


def load_labels(labels_dir: Path) -> dict:
    """분석용: {"cluster": {1: {id: label}, 2: {...}}, "output": {...}} (마지막 저장값)."""
    store = LabelStore(labels_dir)
    return {kind: {r: {tid: row["label"] for tid, row in store.latest(kind, r).items()} for r in (1, 2)}
            for kind in ("cluster", "output")}
