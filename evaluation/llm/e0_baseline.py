"""E0 기준선 측정 러너 (ADR 0009 A8, evaluation/llm/preregistration/e0-baseline-v1.yaml).

main의 뉴스레터 생성 경로를 **코드 그대로** 동결 반출본에 돌린다. 운영 코드는 바꾸지 않고 DB 접촉점만 파일 대역으로
바꿔 끼운다(ADR 0036, evaluation.llm.e0_store). 계측과 예산 가드는 evaluation.llm.e0_instrument에 있다.

    # A8.0 3단계(LLM 호출 없음): code_sha에서 표본·순서를 만들고 실행 전 기록에 옮겨 적을 값을 출력한다
    python -m evaluation.llm.e0_baseline prepare --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
    # A8.0 5단계: 실행 전 기록 PR이 main에 들어간 뒤(그 커밋을 체크아웃해) pre-flight → warmup → eval
    caffeinate -i nice -n 19 env OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
        python -m evaluation.llm.e0_baseline run --export-dir ... --pack-dir ...
    python -m evaluation.llm.e0_baseline run --resume <run 이름> --resume-reason "절전" --export-dir ... --pack-dir ...
    # 합성 반출본 + 페이크 LLM 전송 계층으로 사슬 전체(리포트·블라인드 내보내기까지)를 돌린다. 실제 기사·호출 없음
    python -m evaluation.llm.e0_baseline --dry-run

시작 조건(하나라도 아니면 시작하지 않는다, A8.2·A8.6): 코드 동일성 (a)~(d), `pre_run_record`가 다 채워짐(e), 등록값 = 실제
설정(`e0_registered.config_differences`), 반출본 신원·행 수·T0·만료일, 임베딩 팩 sha256과 fp16/fp32 코사인, 다시 계산한
멤버십 스냅숏·표본 매니페스트·버킷 배분이 기록과 같음, 킬 스위치 꺼짐, API 키 있음, 지출 가드(ADR 0035) 사용 가능.

화면에는 진행 건수와 누적 비용만 낸다. 초안·판정 내용은 `data/experiments/e0/<run>/newsletters.jsonl`에만 있다.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from evaluation.llm import e0_registered as reg
from evaluation.llm import e0_sample, e0_store, frozen_export

KST = timezone(timedelta(hours=9))
RUN_STATE = "run_state.json"
RUN_MANIFEST = "run_manifest.json"
RUN_SUMMARY = "run_summary.json"
PREFLIGHT_FILE = "preflight.json"
HEARTBEAT_S = 15.0
PACKAGES = ("openai", "httpx", "httpx2", "langgraph", "pydantic", "hdbscan", "kiwipiepy", "rapidfuzz")

# pre-flight 입력: 지어낸 기사 3건(A8.5 "합성 입력"). 실제 기사 본문은 E0 표본으로만 유료 티어에 간다.
PREFLIGHT_ARTICLES = [
    {"id": -1, "title": "가람시, 새 도서관 건립 계획 확정", "press_name": "합성일보",
     "content": "가람시는 12일 새 도서관 건립 계획을 확정했다. 사업 예산은 120억 원이다. 개관은 2028년 3월로 잡혔다. "
                "시 관계자는 \"주민 의견을 반영해 열람실을 넓혔다\"고 말했다."},
    {"id": -2, "title": "가람시 도서관, 2028년 개관 목표", "press_name": "합성신문",
     "content": "가람시가 120억 원을 들여 새 도서관을 짓는다. 개관 목표는 2028년 3월이다. 열람실과 어린이 공간을 함께 둔다."},
    {"id": -3, "title": "가람시 도서관 건립에 주민 기대", "press_name": "합성경제",
     "content": "가람시 새 도서관 계획이 12일 확정되자 주민들은 기대를 나타냈다. 예산 120억 원 가운데 일부는 장서 구입에 쓴다."},
]


class Refused(RuntimeError):
    """시작하지 않는다(아무 호출도 하지 않았다)."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("E0 러너가 시작하지 않는다:\n  - " + "\n  - ".join(problems))
        self.problems = list(problems)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any = None) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def e0_root() -> Path:
    return e0_store.default_experiments_root() / "e0"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def draft_sha256(doc: Optional[Dict[str, Any]], fields: Sequence[str] = ("title", "sentence", "content")) -> Optional[str]:
    if not doc:
        return None
    return _sha256_text(json.dumps({f: doc.get(f) or "" for f in fields}, ensure_ascii=False, sort_keys=True))


# ---------------------------------------------------------------- 입력 대조


def export_record_fields(source: e0_store.FileArticleSource) -> Dict[str, Any]:
    export, pack = source.export, source.pack
    snapshot = frozen_export._parse(export.identity["source"]["snapshot_at_utc"]).replace(tzinfo=timezone.utc)
    job = (pack.manifest.get("job") or {})
    cos = ((job.get("fp16_vs_fp32") or {}).get("stored_f16_vs_fp32") or {})
    return {
        "export_t0_kst": snapshot.astimezone(KST).isoformat(),
        "export_rows": int(export.identity["rows"]),
        "export_manifest_sha256": export.identity_sha256,
        "export_body_expires_at": export.identity["retention"]["first_body_expiry_utc"],
        "embeddings_sha256": pack.vectors_sha256,
        "embeddings_fp16_vs_fp32_cos": {"p10": cos.get("p10"), "min": cos.get("min"), "n": cos.get("n")},
    }


def compare_inputs(source: e0_store.FileArticleSource, record: Dict[str, Any]) -> List[str]:
    """반출본·팩이 실행 전 기록과 같은가(A8.6 c)."""
    have = export_record_fields(source)
    problems = []
    for key in ("export_t0_kst", "export_rows", "export_manifest_sha256", "export_body_expires_at", "embeddings_sha256"):
        if have[key] != record.get(key):
            problems.append(f"{key}: 입력 {have[key]!r} ≠ 실행 전 기록 {record.get(key)!r}")
    want = record.get("embeddings_fp16_vs_fp32_cos") or {}
    got = have["embeddings_fp16_vs_fp32_cos"]
    if any(got.get(k) is None or want.get(k) is None or abs(float(got[k]) - float(want[k])) > 1e-6 for k in ("p10", "min")):
        problems.append("embeddings_fp16_vs_fp32_cos: 팩의 잡 기록과 실행 전 기록이 다르다")
    return problems


# ---------------------------------------------------------------- 지출 가드(ADR 0035)와의 결합


def configure_in_client_cap(run_label: str) -> Dict[str, Any]:
    """E0 프로세스의 클라이언트 안 지출 상한을 정한다(A8.10 덧붙임 2026-10-17). 기록할 값을 돌려준다."""
    os.environ["LLM_RUN_ID"] = run_label
    os.environ["LLM_BUDGET_RUN_USD"] = reg.IN_CLIENT_RUN_DAY_CAP_USD
    os.environ["LLM_BUDGET_DAY_USD"] = reg.IN_CLIENT_RUN_DAY_CAP_USD
    from core.llm import budget

    budget.begin_run(run_label)
    return {"LLM_RUN_ID": run_label, "LLM_BUDGET_RUN_USD": reg.IN_CLIENT_RUN_DAY_CAP_USD,
            "LLM_BUDGET_DAY_USD": reg.IN_CLIENT_RUN_DAY_CAP_USD, "LLM_BUDGET_TOTAL_USD": "unchanged"}


def in_client_problems() -> List[str]:
    from core.llm import budget

    problems = []
    issue = budget.preflight_problem()
    if issue:
        problems.append(f"지출 가드(ADR 0035)를 쓸 수 없다: {issue}")
        return problems
    missing = budget.unpriced_models(sorted({spec["model"] for spec in reg.ROLES.values()}))
    if missing:
        problems.append(f"단가표에 없는 모델: {sorted(missing)}")
    snap = budget.spend_snapshot()
    if "error" in snap:
        problems.append(f"지출 원장 조회 실패: {snap['error']}")
    else:
        headroom = snap["caps_usd"]["total"] - snap["total_usd"]
        if headroom < reg.IN_CLIENT_MIN_TOTAL_HEADROOM_USD:
            problems.append(f"지출 원장의 전체 상한 여유 ${headroom:.4f} < ${reg.IN_CLIENT_MIN_TOTAL_HEADROOM_USD:.2f} - "
                            "E0 가드보다 먼저 막힌다")
    exhausted = budget.exhausted_scope()
    if exhausted:
        problems.append(f"지출 상한이 이미 찼다: {exhausted['scope']}")
    return problems


def environment_problems(*, require_api_key: bool) -> List[str]:
    problems = []
    outside = reg.live_outside_settings()
    if outside["LLM_KILL_SWITCH"] != "unset" or outside["kill_switch_file"] != "absent":
        problems.append("킬 스위치가 켜져 있다(환경변수 또는 파일)")
    if require_api_key and not os.getenv("GEMINI_API_KEY", "").strip():
        problems.append("GEMINI_API_KEY가 없다(키 없음은 인프라 실패 - 시작 전에 막는다)")
    return problems


def run_manifest_fields() -> Dict[str, Any]:
    import importlib.metadata as md

    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True)
    if freeze.returncode == 0 and freeze.stdout.strip():
        frozen, method = freeze.stdout, "pip freeze"
    else:  # uv로 만든 가상환경에는 pip이 없다 - 설치된 배포판 목록으로 같은 형식을 만든다
        frozen = "".join(sorted(f"{d.metadata['Name']}=={d.version}\n" for d in md.distributions()
                                if d.metadata and d.metadata["Name"]))
        method = "importlib.metadata.distributions"
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = md.version(name)
        except md.PackageNotFoundError:
            versions[name] = None
    return {"python_version": platform.python_version(), "pip_freeze_sha256": _sha256_text(frozen),
            "pip_freeze_method": method, "packages": versions, "platform": platform.platform()}


# ---------------------------------------------------------------- run 상태


class RunState:
    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / RUN_STATE
        self._lock = threading.Lock()
        self.data: Dict[str, Any] = _read_json(self.path, {}) or {}

    def save(self) -> None:
        with self._lock:
            _write_json(self.path, self.data)

    def update(self, **fields: Any) -> None:
        with self._lock:
            self.data.update(fields)
        self.save()

    def eval_elapsed(self, now: float) -> float:
        with self._lock:
            started = self.data.get("eval_segment_started")
            return float(self.data.get("eval_elapsed_s", 0.0)) + (now - started if started else 0.0)

    def close_segment(self, now: float) -> None:
        with self._lock:
            started = self.data.get("eval_segment_started")
            if started:
                self.data["eval_elapsed_s"] = float(self.data.get("eval_elapsed_s", 0.0)) + now - started
                self.data["eval_segment_started"] = None
        self.save()


@contextmanager
def e0_lock(root: Path) -> Iterator[None]:
    """E0 러너는 한 번에 하나만 돈다(누적 원장 합이 프로세스 사이에서 어긋나지 않게)."""
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(["다른 E0 러너가 이미 돌고 있다(.lock)"]) from None
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------- 노드 기록(숫자·id·sha만)


def _counts(items: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    return {k: len(v or []) for k, v in (items or {}).items()}


def _spans(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for group in ("blocking", "advisory"):
        for kind, items in (report.get(group) or {}).items():
            for item in items or []:
                span = item.get("span") or (None, None)
                out.append({"group": group, "type": kind, "field": item.get("field"),
                            "start": span[0], "end": span[1]})
    return out


def node_record(node: str, update: Dict[str, Any], current: Dict[str, Any], ctx) -> Dict[str, Any]:
    """`app.stream`의 노드 갱신 하나를 숫자·id·sha로 줄인다(A8.4). 텍스트는 넣지 않는다."""
    rec: Dict[str, Any] = {"node": node, "item_id": ctx.item_id, "cluster_id": ctx.cluster_id, "run_k": ctx.run_k}
    if node == "init_cluster":
        rec.update(n_articles=len(current.get("current_articles") or []),
                   n_article_ids=len(current.get("current_article_ids") or []))
    elif node == "eval_cluster":
        ce = update.get("cluster_eval") or {}
        last = ctx.last_call.get("cluster_eval")
        called = bool(last and last.get("graph_attempt") == ctx.cluster_eval_attempt and ctx.cluster_eval_attempt)
        rec.update(decision=ce.get("decision"), confidence=ce.get("confidence"),
                   n_outliers=len(ce.get("outlier_indices") or []), n_sub_groups=len(ce.get("sub_groups") or []),
                   sub_group_sizes=[len(g) for g in ce.get("sub_groups") or []],
                   llm_called=called, call_parsed=(last or {}).get("parsed") if called else None,
                   attempt=ctx.cluster_eval_attempt, n_articles=len(current.get("current_articles") or []))
    elif node == "handle_cluster_fail":
        ce = current.get("cluster_eval") or {}
        skipped = ctx.cluster_id in (current.get("skipped_clusters") or [])
        groups = ce.get("sub_groups") or []
        n_articles = len(current.get("current_articles") or [])
        dropped = 0
        if not skipped and len(groups) >= 2:
            sizes = [len([i for i in g if isinstance(i, int)]) for g in groups]
            dropped = sum(1 for s in sizes[1:] if s >= 3)
        rec.update(skipped=skipped, cluster_retry_count=current.get("cluster_retry_count"), n_articles=n_articles,
                   dropped_subgroups_ge3=dropped)
    elif node == "generate_newsletter":
        draft = update.get("newsletter_draft") or {}
        rec.update(attempt=update.get("newsletter_retry_count"), draft_sha256=draft_sha256(draft),
                   draft_is_none=not draft, fallback=list(draft.get("_fallback") or []),
                   content_chars=len(draft.get("content") or ""), title_chars=len(draft.get("title") or ""),
                   sentence_chars=len(draft.get("sentence") or ""),
                   n_keywords=len(draft.get("keywords") or []), n_categories=len(draft.get("categories") or []),
                   n_articles=len(current.get("current_articles") or []))
    elif node == "check_faithfulness":
        report = update.get("faithfulness_report") or {}
        rec.update(attempt=current.get("newsletter_retry_count"), gate_passed=report.get("gate_passed"),
                   passed=report.get("passed"), reason=report.get("reason"),
                   blocking=_counts(report.get("blocking")), advisory=_counts(report.get("advisory")),
                   number_total=report.get("number_total"), spans=_spans(report),
                   entity_extractor=report.get("entity_extractor"),
                   draft_sha256=draft_sha256(current.get("newsletter_draft")))
    elif node == "eval_newsletter":
        ev = update.get("newsletter_eval") or {}
        claims = ev.get("unsupported_claims") or []
        draft = current.get("newsletter_draft") or {}
        fields_text = [str(draft.get(f) or "") for f in ("title", "sentence", "content")]
        located = sum(1 for c in claims if any(str(c.get("claim") or "").strip() and
                                               t.find(str(c.get("claim")).strip()) >= 0 for t in fields_text))
        rec.update(attempt=current.get("newsletter_retry_count"), decision=ev.get("decision"),
                   criteria=ev.get("criteria"), judge_unavailable=not ev.get("criteria"),
                   n_unsupported_claims=len(claims), claims_located=located, score=ev.get("score"),
                   draft_sha256=draft_sha256(draft))
    elif node == "embed_newsletter":
        rec.update(embedding_present=update.get("newsletter_embedding") is not None)
    elif node == "convert_tone":
        converted = update.get("converted_newsletter")
        feedback = str(update.get("conversion_feedback") or "")
        rec.update(tone_round=ctx.tone_round, local_fallbacks_so_far=ctx.tone_local_fallbacks,
                   converted_sha256=draft_sha256(converted, ("title", "summary", "content")) if converted else None,
                   skipped=not update, conversion=("ok" if feedback.startswith("Conversion successful") else
                                                   "failed_original" if feedback.startswith("Conversion failed") else
                                                   "error_original" if feedback.startswith("Conversion error") else None))
    elif node == "check_tone_drift":
        report = update.get("tone_drift_report") or {}
        rec.update(tone_round=ctx.tone_round, gate_passed=report.get("gate_passed"), passed=report.get("passed"),
                   blocking=_counts(report.get("blocking")), advisory=_counts(report.get("advisory")),
                   retries=report.get("retries"), fallback=report.get("fallback"))
    elif node == "save_newsletter":
        completed = update.get("completed_newsletters") or []
        rec.update(newsletter_id=completed[-1] if completed else None,
                   save_error=bool(update.get("failed_clusters")),
                   link_gap=len(current.get("current_article_ids") or []) - len(current.get("current_articles") or []),
                   n_linked=len(current.get("current_article_ids") or []),
                   n_used=len(current.get("current_articles") or []),
                   tone_fallback=current.get("tone_fallback"))
    elif node == "handle_max_retries":
        rec.update(failure_reason=update.get("failure_reason"))
    return rec


def newsletter_texts(node: str, update: Dict[str, Any], current: Dict[str, Any], ctx) -> Optional[Tuple[str, Dict]]:
    """newsletters.jsonl(저장소 밖)에 남길 생성 텍스트. 라벨링 입력이고 리포트에는 sha256·길이만 간다."""
    base = {"item_id": ctx.item_id, "cluster_id": ctx.cluster_id, "run_k": ctx.run_k}
    if node == "generate_newsletter":
        draft = update.get("newsletter_draft") or {}
        return "draft_attempt", {**base, "attempt": update.get("newsletter_retry_count"),
                                 "draft": {k: draft.get(k) for k in ("title", "sentence", "content", "keywords",
                                                                    "categories")} if draft else None,
                                 "fallback": list(draft.get("_fallback") or []), "sha256": draft_sha256(draft)}
    if node == "convert_tone" and update.get("converted_newsletter"):
        conv = update["converted_newsletter"]
        return "tone_attempt", {**base, "tone_round": ctx.tone_round,
                                "converted": {k: conv.get(k) for k in ("title", "summary", "content")},
                                "sha256": draft_sha256(conv, ("title", "summary", "content"))}
    if node == "save_newsletter" and update.get("completed_newsletters"):
        formal = current.get("newsletter_draft") or {}
        conv = current.get("converted_newsletter") or None
        return "saved_formal", {**base, "news_letter_id": update["completed_newsletters"][-1],
                                "formal": {k: formal.get(k) for k in ("title", "sentence", "content")},
                                "converted": {k: conv.get(k) for k in ("title", "summary", "content")} if conv else None,
                                "tone_fallback": current.get("tone_fallback"), "formal_sha256": draft_sha256(formal)}
    return None


# ---------------------------------------------------------------- 러너


@dataclass
class RunConfig:
    export_dir: Path
    pack_dir: Path
    prereg: Dict[str, Any]
    manifest_path: Path
    repo: Path = reg.REPO_ROOT
    resume: Optional[str] = None
    resume_reason: str = ""
    dry_run: bool = False
    check_code_identity: bool = True
    require_api_key: bool = True
    now: Optional[datetime] = None
    command: str = ""
    progress: Callable[[str], None] = lambda message: print(message, flush=True)
    clock: Callable[[], float] = time.time
    wall_clock_cap_s: float = reg.BUDGET["wall_clock_cap_hours"] * 3600.0
    cap_run_usd: float = reg.BUDGET["cap_usd_valid_run"]
    cap_cumulative_usd: float = reg.BUDGET["cap_usd_e0_cumulative"]


def start_problems(cfg: RunConfig, source: Optional[e0_store.FileArticleSource],
                   plan: Optional[e0_sample.SamplePlan]) -> List[str]:
    record = (cfg.prereg or {}).get("pre_run_record") or {}
    problems = [f"(e) pre_run_record {p}" for p in reg.check_pre_run_record(record)]
    problems += [f"설정: {p}" for p in reg.config_differences(cfg.prereg)]
    if cfg.check_code_identity:
        # 실행 전 기록·매니페스트는 HEAD에 커밋된 것만 쓴다(A8.0 4단계, A8.6 c) - 다른 경로의 채운 사본으로는 시작하지 않는다
        problems += reg.committed_input_problems(cfg.repo, cfg.prereg, cfg.manifest_path)
    if cfg.check_code_identity and record.get("code_sha"):
        problems += reg.check_code_identity(cfg.repo, record["code_sha"])
        amend = record.get("adr_0023_amendment_commit")
        if amend and reg._git(cfg.repo, "merge-base", "--is-ancestor", str(amend), "origin/main").returncode != 0:
            problems.append("adr_0023_amendment_commit이 origin/main에 없다(A8.0 2단계)")
    if source is not None:
        problems += compare_inputs(source, record)
    if plan is not None:
        problems += e0_sample.compare_with_record(plan, record, cfg.manifest_path)
    decisions = record.get("user_decisions") or {}
    if decisions.get("newsletter_embedder") == "real_colab_cpu":
        try:
            import FlagEmbedding  # noqa: F401
        except ImportError:
            problems.append("newsletter_embedder=real_colab_cpu인데 이 환경에 FlagEmbedding이 없다")
    problems += environment_problems(require_api_key=cfg.require_api_key)
    return problems


def _new_run_name(root: Path, now: datetime) -> Tuple[str, int]:
    existing = sorted(p for p in root.glob("run-*") if p.is_dir())
    return f"run-{now.strftime('%Y%m%dT%H%M%SZ')}", len(existing) + 1


class Runner:
    def __init__(self, cfg: RunConfig, source: e0_store.FileArticleSource, plan: e0_sample.SamplePlan, run_dir: Path,
                 state: RunState, store: e0_store.FileGenerationStore, recorder, app):
        from evaluation.llm import e0_instrument as ins

        self.ins = ins
        self.cfg, self.source, self.plan, self.dir = cfg, source, plan, run_dir
        self.state, self.store, self.recorder, self.app = state, store, recorder, app
        self.items = {r["item_id"]: r for r in plan.rows}
        self._progress_lock = threading.Lock()
        self.done: Dict[str, int] = {}

    # -- 한 클러스터
    def _stream(self, state: Dict[str, Any], ctx) -> Dict[str, Any]:
        current: Dict[str, Any] = dict(state)
        for mode, chunk in self.app.stream(state, stream_mode=["updates", "values"]):
            if mode == "values":
                current = dict(chunk)
                continue
            for node, update in (chunk or {}).items():
                update = update or {}
                current.update(update)
                self.recorder.node(node_record(node, update, current, ctx))
                text = newsletter_texts(node, update, current, ctx)
                if text is not None:
                    self.store.append_newsletter_record(text[0], text[1])
        return current

    def process(self, phase: str) -> Callable[[str, int], Dict[str, Any]]:
        from evaluation.llm import e0_report
        from pipeline.stages import summarize_cluster_outcome

        def run_one(item_id: str, position: int) -> Dict[str, Any]:
            item = self.items[item_id]
            if self.recorder.stop is not None:
                return {"status": "not_started", "failure_reason": f"stop:{self.recorder.stop['reason']}"}
            if phase == "eval" and self.state.eval_elapsed(self.cfg.clock()) >= self.cfg.wall_clock_cap_s:
                if not self.state.data.get("wall_clock_reached"):
                    self.state.update(wall_clock_reached=_iso(_utc_now()))
                return {"status": "not_started", "failure_reason": "wall_clock"}
            ctx = self.recorder.start_cluster(item, phase=phase)
            run = self.plan.runs[item["run_k"] - 1]
            state = run.state_for(item["cluster_id"], run_id=self.state.data["run_seq"])
            t0 = self.cfg.clock()
            final: Optional[Dict[str, Any]] = None
            try:
                final = self.ins.run_in_cluster(ctx, lambda: self._stream(state, ctx))
            except self.ins.LLMRunStop as stop:
                kind = ctx.stopped or self.ins.stop_kind(stop)
                outcome = "aborted_budget" if kind == "budget" else "aborted_infra"
                self._end(ctx, phase, outcome=outcome, summary=None, wall=self.cfg.clock() - t0, stop_code=stop.code)
                raise
            except Exception as e:  # noqa: BLE001 - 그래프 밖으로 나온 예외. run_clusters_bounded가 error로 적는다
                self._end(ctx, phase, outcome="error", summary=None, wall=self.cfg.clock() - t0,
                          exception=type(e).__name__)
                raise
            summary = summarize_cluster_outcome(final, item["cluster_id"])
            if ctx.stopped == "budget":
                outcome = "aborted_budget"
            elif ctx.stopped == "infra":
                outcome = "aborted_infra"
            elif summary["status"] == "completed":
                outcome = "completed"
            elif summary["status"] == "skipped":
                outcome = "skipped"
            elif summary["status"] == "failed":
                outcome = f"failed:{summary.get('failure_reason') or 'save_error'}"
            else:
                outcome = "error"
            self._end(ctx, phase, outcome=outcome, summary=summary, wall=self.cfg.clock() - t0, final=final)
            return summary

        return run_one

    def _end(self, ctx, phase: str, *, outcome: str, summary: Optional[Dict[str, Any]], wall: float,
             final: Optional[Dict[str, Any]] = None, stop_code: Optional[str] = None,
             exception: Optional[str] = None) -> None:
        from evaluation.llm import e0_report

        row = {"outcome": outcome, "phase": phase, "wall_s": round(wall, 3), "stop_code": stop_code,
               "exception": exception, "status": (summary or {}).get("status"),
               "failure_reason": (summary or {}).get("failure_reason"),
               "newsletter_id": (summary or {}).get("newsletter_id"),
               "events": list(ctx.events), "tone_local_fallbacks": ctx.tone_local_fallbacks,
               "tone_rounds": ctx.tone_round, "calls": ctx.call_seq,
               "gen_attempts": ctx.gen_attempt, "cluster_eval_attempts": ctx.cluster_eval_attempt,
               "tone_validation_calls": ctx.tone_validation_calls,
               "link_gap": (len(final.get("current_article_ids") or []) - len(final.get("current_articles") or [])
                            if final else None),
               "n_articles_used": len(final.get("current_articles") or []) if final else None}
        row["root_cause"] = e0_report.root_cause(row, ctx.calls)
        self.recorder.end_cluster(ctx, row)
        with self._progress_lock:
            self.done[phase] = self.done.get(phase, 0) + 1
            spent = self.recorder.spent()
        self.cfg.progress(f"[e0] {phase} {self.done[phase]} 처리 · 누적 ${spent['run_usd']:.4f} "
                          f"(E0 누적 ${spent['cumulative_usd']:.4f})")

    # -- 단계
    def preflight(self) -> Dict[str, Any]:
        from core.llm.schemas import ToneResult
        from core.reconstruction.generator import NewsReconstructor, select_prompt_articles
        from core.reconstruction.validator import normalize_meta
        from core.tone_converter import ToneConverter
        from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator

        ctx = self.ins.ClusterContext(item_id="preflight", cluster_id=None, run_k=None, split="preflight")

        def calls():
            arts = [dict(a) for a in PREFLIGHT_ARTICLES]
            ClusterEvaluator().evaluate(arts)
            rec = NewsReconstructor()
            content = rec._generate_content(rec._build_articles_text(select_prompt_articles(arts)))
            meta = rec._generate_meta(content) or {}
            draft = normalize_meta({"title": meta.get("title"), "sentence": meta.get("sentence"), "content": content,
                                    "keywords": meta.get("keywords") or [], "categories": meta.get("categories") or []})
            NewsletterEvaluator().evaluate(draft, arts)
            tone = ToneConverter()
            tone.llm_client.complete([{"role": "user", "content": tone.create_prompt(draft)}], schema=ToneResult,
                                     temperature=reg.CALL_PARAMS["tone_convert"]["temperature"],
                                     max_tokens=reg.CALL_PARAMS["tone_convert"]["max_tokens"], purpose="tone_convert")

        error = None
        try:
            self.ins.run_in_cluster(ctx, calls)
        except self.ins.LLMRunStop as stop:
            error = stop.code
        http = [r for r in self.recorder.read(self.ins.HTTP_FILE) if r.get("cluster_id") is None]
        table = []
        for call in ctx.calls:
            attempts = [h for h in http if h.get("call_seq") == call["call_seq"]]
            table.append({"purpose": call["purpose"], "parsed": call["parsed"], "error": call["error"],
                          "schema_failures": call["schema_failures"], "latency_s": call["latency_s"],
                          "http_attempts": call["http_attempts"], "cost_usd": call["cost_usd"],
                          "prompt_tokens": sum(h.get("prompt_tokens") or 0 for h in attempts),
                          "completion_tokens": sum(h.get("completion_tokens") or 0 for h in attempts),
                          "hidden_tokens": sum(h.get("hidden_tokens") or 0 for h in attempts),
                          "temperature": call["temperature"], "max_tokens": call["max_tokens"]})
        mismatched = [c["purpose"] for c in ctx.calls
                      if reg.CALL_PARAMS.get(c["purpose"]) != {"temperature": c["temperature"],
                                                               "max_tokens": c["max_tokens"]}]
        result = {"calls": table, "stop": error, "call_params_mismatch": mismatched,
                  "infra_failure": self.recorder.stop is not None and self.recorder.stop["reason"] == "infra"}
        _write_json(self.dir / PREFLIGHT_FILE, result)
        return result

    def run_phase(self, phase: str, items: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        from pipeline.stages import run_clusters_bounded

        if not items:
            return {}
        conc = reg.CONCURRENCY
        return run_clusters_bounded(self.process(phase), [(r["item_id"], r["order"]) for r in items],
                                    list(conc["fill"]), max_workers=conc["max_workers"], min_target=conc["min_target"])

    def audit(self) -> List[str]:
        """warmup 뒤 계측 점검(A8.5 warmup에서 고칠 수 있는 닫힌 목록의 "계측 버그"를 찾는다)."""
        ins = self.ins
        problems = list(self.recorder.instrumentation_errors)
        http = self.recorder.read(ins.HTTP_FILE)
        calls = self.recorder.read(ins.CALLS_FILE)
        ledger_totals = ins.Ledger.totals(self.dir / ins.LEDGER_FILE)
        if ledger_totals["open_ids"]:
            problems.append(f"정산되지 않은 예약 {len(ledger_totals['open_ids'])}건")
        if abs(ledger_totals["settled_usd"] - sum(float(h["cost_usd"] or 0) for h in http)) > 1e-6:
            problems.append("원장 정산 합 ≠ http_attempts 비용 합")
        if abs(sum(float(c["cost_usd"] or 0) for c in calls) - sum(float(h["cost_usd"] or 0) for h in http
                                                                  if h.get("call_seq") is not None)) > 1e-6:
            problems.append("calls 비용 합 ≠ http_attempts 비용 합")
        keys = {(c["cluster_id"], c["run_k"], c["call_seq"]) for c in calls}
        orphan = [h for h in http if (h["cluster_id"], h["run_k"], h["call_seq"]) not in keys]
        if orphan:
            problems.append(f"complete() 기록과 짝이 없는 HTTP 시도 {len(orphan)}건(귀속 오류)")
        rows = self.recorder.read(ins.CLUSTERS_FILE)
        started = {r["item_id"] for r in rows if r["kind"] == "start"}
        ended = {r["item_id"] for r in rows if r["kind"] == "end"}
        if started - ended:
            problems.append(f"종료 행이 없는 클러스터 {sorted(started - ended)}")
        if self.store.violations:
            problems.append(f"대역 밖 DB 접근 {len(self.store.violations)}건")
        return problems

    def close_interrupted(self) -> List[str]:
        """이전 프로세스가 시작 행만 남긴 클러스터를 interrupted로 닫는다. 다시 돌리지 않는다(A8.5 재개)."""
        rows = self.recorder.read(self.ins.CLUSTERS_FILE)
        ended = {r["item_id"] for r in rows if r["kind"] == "end"}
        closed = []
        for r in rows:
            if r["kind"] == "start" and r["item_id"] not in ended:
                ctx = self.ins.ClusterContext(item_id=r["item_id"], cluster_id=r["cluster_id"], run_k=r["run_k"],
                                              split=r["split"])
                self.recorder.end_cluster(ctx, {"outcome": "interrupted", "root_cause": "interrupted",
                                                "phase": r.get("phase"), "wall_s": None})
                ended.add(r["item_id"])
                closed.append(r["item_id"])
        return closed

    def finalize(self, reason: str, *, void: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ins = self.ins
        rows = self.recorder.read(ins.CLUSTERS_FILE)
        ended = {r["item_id"] for r in rows if r["kind"] == "end"}
        stop = self.recorder.stop or self.state.data.get("stop")
        why = (f"stop:{stop['reason']}" if stop else "wall_clock" if self.state.data.get("wall_clock_reached")
               else reason)
        for item in self.plan.rows:
            if item["item_id"] not in ended:
                ctx = ins.ClusterContext(item_id=item["item_id"], cluster_id=item["cluster_id"], run_k=item["run_k"],
                                         split=item["split"])
                self.recorder.end_cluster(ctx, {"outcome": "not_started", "root_cause": None, "reason": why,
                                                "phase": item["split"]})
        rows = self.recorder.read(ins.CLUSTERS_FILE)
        ends = {r["item_id"]: r for r in rows if r["kind"] == "end"}
        eval_ends = [ends[r["item_id"]] for r in self.plan.eval_items]
        natural = [e for e in eval_ends if e["outcome"].split(":")[0] in reg.NATURAL_OUTCOMES]
        # pre-flight의 인프라 실패는 실행 시작 전이라 void가 아니다(A8.5). 그 뒤의 인프라 중단은 n_p < 8이면 void.
        if void is None and stop and stop.get("reason") == "infra" and reason != "preflight_infra_failure" and \
                len(natural) < reg.BUDGET["void_if_processed_eval_clusters_below"]:
            void = {"reason": "infra_below_8", "n_processed": len(natural)}
        try:
            self.store.assert_clean()
            db_clean = True
        except e0_store.UnexpectedDatabaseAccess:
            db_clean = False
        from core.llm import budget

        summary = {
            "run": self.dir.name, "finished_at": _iso(_utc_now()), "reason": reason, "not_started_reason": why,
            "stop": stop, "void": void,
            "n_eval": len(self.plan.eval_items), "n_warmup": len(self.plan.warmup_items), "n_processed": len(natural),
            "outcomes": {o: sum(1 for e in eval_ends if e["outcome"] == o) for o in sorted({e["outcome"] for e in eval_ends})},
            "ledger": ins.Ledger.totals(self.dir / ins.LEDGER_FILE) | {"open_ids": None},
            "cumulative_usd": ins.cumulative_usd(self.dir.parent),
            "in_client_spend": budget.spend_snapshot(),
            "refusals": len(self.recorder.refusals), "db_clean": db_clean,
            "instrumentation_errors": list(self.recorder.instrumentation_errors),
            "wall_clock_reached": self.state.data.get("wall_clock_reached"),
            "eval_elapsed_s": round(self.state.eval_elapsed(self.cfg.clock()), 1),
            "resumes": self.state.data.get("resumes", []),
        }
        _write_json(self.dir / RUN_SUMMARY, summary)
        self.state.update(finished=True, void=void, stop=stop)
        return summary


def open_inputs(cfg: RunConfig) -> Tuple[e0_store.FileArticleSource, e0_sample.SamplePlan]:
    source = e0_store.FileArticleSource.open(cfg.export_dir, cfg.pack_dir, now=cfg.now)
    return source, e0_sample.build_plan(source)


@contextmanager
def quiet_logs() -> Iterator[None]:
    """실행 중에는 운영 코드의 로그를 화면에 내지 않는다(A8.4: 진행 건수와 누적 비용만). 어댑터의 스키마 검증 경고에는
    모델 응답 조각이 섞인다. 필요한 사실(상태 코드·재시도·오류 유형)은 계측 기록에 있다."""
    import logging

    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def run_e0(cfg: RunConfig) -> Dict[str, Any]:
    """시작 검사 → (재개면 정리) → pre-flight → warmup → 점검 → eval → 마무리. 돌려주는 것: run 요약."""
    with quiet_logs():
        return _run_e0(cfg)


def _run_e0(cfg: RunConfig) -> Dict[str, Any]:
    from evaluation.llm import e0_instrument as ins

    source, plan = open_inputs(cfg)
    problems = start_problems(cfg, source, plan)
    if problems:
        raise Refused(problems)
    root = e0_root()
    with e0_lock(root):
        now = cfg.now or _utc_now()
        name, seq = (cfg.resume, None) if cfg.resume else _new_run_name(root, now)
        # 지출 가드(ADR 0035)를 E0 run에 맞춰 두고 쓸 수 있는지 본다 - 막히면 run 디렉터리를 만들기 전에 멈춘다
        run_label = f"{reg.PREREG_ID}:{name}"
        in_client = configure_in_client_cap(run_label)
        cap_problems = in_client_problems()
        if cap_problems:
            raise Refused(cap_problems)
        if cfg.resume:
            run_dir = root / cfg.resume
            if not (run_dir / RUN_STATE).exists():
                raise Refused([f"재개할 run이 없다: {cfg.resume}"])
            state = RunState(run_dir)
            if state.data.get("finished"):
                raise Refused([f"{cfg.resume}은 이미 끝난 run이다(유효 실행은 1회 - A8.6)"])
            record = cfg.prereg["pre_run_record"]
            if state.data.get("code_sha") != record["code_sha"] or \
                    state.data.get("sample_manifest_sha256") != record["sample_manifest_sha256"]:
                raise Refused(["재개하려는 run의 code_sha·표본이 지금 실행 전 기록과 다르다"])
            state.data.setdefault("resumes", []).append({"at": _iso(now), "reason": cfg.resume_reason or "unspecified"})
            if state.data.get("eval_segment_started"):
                # 죽은 프로세스의 eval 구간은 마지막 heartbeat까지만 센다(멈춰 있던 시간을 벽시계 상한에 넣지 않는다)
                state.data["eval_elapsed_s"] = float(state.data.get("eval_elapsed_checkpoint_s",
                                                                    state.data.get("eval_elapsed_s", 0.0)))
                state.data["eval_segment_started"] = None
            state.save()
        else:
            run_dir = root / name
            store = e0_store.FileGenerationStore(run_dir)  # 경로 확인(메인 체크아웃 data/experiments, git 제외)
            state = RunState(run_dir)
            record = cfg.prereg["pre_run_record"]
            state.data.update({"format": "e0-run-state/1", "run": name, "run_seq": seq, "created_at": _iso(now),
                               "code_sha": record["code_sha"], "sample_manifest_sha256": record["sample_manifest_sha256"],
                               "phases": {}, "eval_elapsed_s": 0.0, "eval_segment_started": None, "stop": None,
                               "finished": False, "resumes": [], "dry_run": cfg.dry_run})
            state.save()
        store = e0_store.FileGenerationStore(run_dir)
        head = reg._git(cfg.repo, "rev-parse", "HEAD").stdout.strip() or None
        if not (run_dir / RUN_MANIFEST).exists():
            _write_json(run_dir / RUN_MANIFEST, {
                "prereg": reg.PREREG_ID, "command": cfg.command, "code_sha": cfg.prereg["pre_run_record"]["code_sha"],
                "head_sha": head, "started_at": _iso(now), "dry_run": cfg.dry_run,
                "effective_config": reg.effective_config(), "in_client_cap": in_client,
                "forbid_database": reg.FORBID_DATABASE,
                "user_decisions": cfg.prereg["pre_run_record"]["user_decisions"],
                "sample": {"order": plan.order, "frame": plan.frame, "bucket_allocation": plan.bucket_allocation(),
                           "extracted_after_pseudo_now": plan.extracted_after_pseudo_now},
                **run_manifest_fields()})

        def on_stop(reason, detail):
            state.update(stop={"reason": reason, **{k: v for k, v in detail.items() if k != "detail"},
                               "detail": detail.get("detail"), "at": _iso(_utc_now())})

        prices = ins.Prices(on=lambda: _utc_now().date())
        recorder = ins.Recorder(run_dir, run_name=run_dir.name, e0_root=run_dir.parent, prices=prices, clock=cfg.clock,
                                on_stop=on_stop, cap_run_usd=cfg.cap_run_usd,
                                cap_cumulative_usd=cfg.cap_cumulative_usd)
        if state.data.get("stop"):
            recorder.stop = dict(state.data["stop"])
        from workflow.graph import compile_workflow

        app = compile_workflow()
        embedder = "production" if cfg.prereg["pre_run_record"]["user_decisions"]["newsletter_embedder"] == \
            "real_colab_cpu" else "record_only"
        runner = Runner(cfg, source, plan, run_dir, state, store, recorder, app)
        heartbeat_stop = threading.Event()

        def heartbeat():
            while not heartbeat_stop.wait(HEARTBEAT_S):
                started = state.data.get("eval_segment_started")
                if started:
                    state.update(eval_elapsed_checkpoint_s=round(state.eval_elapsed(cfg.clock()), 1),
                                 heartbeat_at=_iso(_utc_now()))

        beat = threading.Thread(target=heartbeat, name="e0-heartbeat", daemon=True)
        beat.start()
        try:
            with e0_store.installed(store, forbid_database=reg.FORBID_DATABASE, embedder=embedder), \
                    ins.Instrumentation(recorder):
                closed = runner.close_interrupted()
                if closed:
                    recorder.event("resume_closed_interrupted", items=closed)
                phases = state.data.setdefault("phases", {})
                if state.data.get("stop"):
                    return runner.finalize("stopped_before_resume")
                if phases.get("preflight") != "done":
                    pre = runner.preflight()
                    phases["preflight"] = "done"
                    state.save()
                    if pre["infra_failure"]:
                        return runner.finalize("preflight_infra_failure")
                    if pre["call_params_mismatch"]:
                        recorder.set_stop("config", {"detail": f"call_params_mismatch:{pre['call_params_mismatch']}"})
                        return runner.finalize("preflight_call_params_mismatch",
                                               void={"reason": "settings_mismatch",
                                                     "detail": pre["call_params_mismatch"]})
                if phases.get("warmup") != "done":
                    started = {r["item_id"] for r in recorder.read(ins.CLUSTERS_FILE) if r["kind"] == "start"}
                    runner.run_phase("warmup", [r for r in plan.warmup_items if r["item_id"] not in started])
                    problems = runner.audit()
                    state.update(audit_after_warmup=problems)
                    phases["warmup"] = "done"
                    state.save()
                    if recorder.stop is not None:
                        return runner.finalize("stopped_in_warmup")
                    if problems:
                        recorder.set_stop("warmup_audit", {"detail": "; ".join(problems[:5])})
                        return runner.finalize("warmup_audit_failed")
                started = {r["item_id"] for r in recorder.read(ins.CLUSTERS_FILE) if r["kind"] == "start"}
                todo = [r for r in plan.eval_items if r["item_id"] not in started]
                phases["eval"] = "started"
                state.update(eval_segment_started=cfg.clock())
                try:
                    runner.run_phase("eval", todo)
                finally:
                    state.close_segment(cfg.clock())
                phases["eval"] = "done"
                state.save()
                return runner.finalize("completed_order")
        finally:
            heartbeat_stop.set()
            beat.join(timeout=1)


# ---------------------------------------------------------------- prepare


def prepare(export_dir: Path, pack_dir: Path, *, manifest_out: Path, now: Optional[datetime] = None,
            repo: Path = reg.REPO_ROOT, snapshot_dir: Optional[Path] = None) -> Dict[str, Any]:
    """A8.0 3단계: 표본·순서를 만들고 매니페스트를 쓴 뒤 실행 전 기록에 옮겨 적을 값을 돌려준다. LLM 호출 없음."""
    source = e0_store.FileArticleSource.open(export_dir, pack_dir, now=now)
    plan = e0_sample.build_plan(source)
    sha = e0_sample.write_manifest(manifest_out, plan)
    e0_sample.write_snapshots(snapshot_dir or Path(export_dir) / "e0_prepare" / "snapshots", plan)
    head = reg._git(repo, "rev-parse", "HEAD").stdout.strip() or None
    return {
        **export_record_fields(source), **plan.record_fields(), "sample_manifest_sha256": sha,
        "code_sha_candidate": head, "ai_workspace_diff_since_b64da14": reg.ai_workspace_diff_stat(repo, head) if head else None,
        "frame": plan.frame, "n_eval": len(plan.eval_items), "n_warmup": len(plan.warmup_items),
        "candidates_by_bucket": plan.candidates_by_bucket, "extracted_after_pseudo_now": plan.extracted_after_pseudo_now,
        "processing_order": plan.order, "label_deadline_utc": label_deadline(source, plan),
    }


def label_deadline(source: e0_store.FileArticleSource, plan: e0_sample.SamplePlan) -> Optional[str]:
    """표본 중 가장 오래된 기사의 crawled_at + 30일(A8.8, ADR 0023)."""
    ids = {a["raw_news_id"] for r in plan.rows for a in r["articles"]}
    crawled = sorted(r["crawled_at"] for r in source.export.rows if int(r["id"]) in ids)
    if not crawled:
        return None
    return (frozen_export._parse(crawled[0]) + timedelta(days=frozen_export.BODY_RETENTION_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 드라이 런


@contextmanager
def dry_run_sandbox(root: Path) -> Iterator[Path]:
    """합성 입력과 산출물을 root 아래에만 둔다(메인 체크아웃의 data/를 건드리지 않는다). 지출 원장·킬 스위치도 임시."""
    from config.settings import Settings
    from core.llm import budget, registry
    from core.llm.spend_ledger import SpendLedger

    data = Path(root) / "data"
    data.mkdir(parents=True, exist_ok=True)
    saved_env = {k: os.environ.get(k) for k in ("LLM_SPEND_LEDGER_FILE", "LLM_SPEND_STATE_DIR", "LLM_KILL_SWITCH_FILE",
                                                "LLM_RUN_ID", "LLM_BUDGET_RUN_USD", "LLM_BUDGET_DAY_USD",
                                                "LLM_BUDGET_TOTAL_USD", "LLM_KILL_SWITCH")}
    saved_kill = Settings.LLM_KILL_SWITCH_FILE
    saved_data = frozen_export.main_checkout_data
    ledger = Path(root) / "ops" / "llm_spend_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    os.environ["LLM_SPEND_LEDGER_FILE"] = str(ledger)
    os.environ["LLM_SPEND_STATE_DIR"] = str(Path(root) / "spend-state")
    os.environ["LLM_KILL_SWITCH_FILE"] = str(Path(root) / "ops" / "LLM_KILL_SWITCH")
    os.environ.pop("LLM_KILL_SWITCH", None)
    os.environ["LLM_BUDGET_TOTAL_USD"] = "3.00"
    Settings.LLM_KILL_SWITCH_FILE = os.environ["LLM_KILL_SWITCH_FILE"]
    if not ledger.exists():
        SpendLedger(str(ledger), state_dir=os.environ["LLM_SPEND_STATE_DIR"]).init(note="e0 dry run")
    frozen_export.main_checkout_data = lambda: data
    budget.reset_run_state()
    registry.reset_registry()
    try:
        yield data
    finally:
        frozen_export.main_checkout_data = saved_data
        Settings.LLM_KILL_SWITCH_FILE = saved_kill
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        budget.reset_run_state()
        registry.reset_registry()


@dataclass
class DryInputs:
    root: Path
    data: Path
    export_dir: Path
    pack_dir: Path
    manifest: Path
    prereg: Dict[str, Any]
    prepared: Dict[str, Any]
    t0: datetime


def dry_inputs(root: Path, data: Path, t0: datetime,
               mutate_prereg: Optional[Callable[[Dict[str, Any]], None]] = None) -> DryInputs:
    """합성 반출본·팩·표본 매니페스트와, 그 값으로 채운 실행 전 기록(메모리 안의 yaml 사본)을 만든다."""
    import copy

    from evaluation.llm import e0_synthetic

    export_dir, pack_dir = e0_synthetic.write_synthetic_export(data / "exports", t0)
    manifest = Path(root) / "evalsets" / "e0-baseline-v1.jsonl"
    prepared = prepare(export_dir, pack_dir, manifest_out=manifest, now=t0, snapshot_dir=Path(root) / "snapshots")
    head = reg._git(reg.REPO_ROOT, "rev-parse", "HEAD").stdout.strip() or "0" * 40
    prereg = copy.deepcopy(reg.load_prereg())
    prereg["pre_run_record"].update({
        "code_sha": head, "ai_workspace_diff_since_b64da14": prepared["ai_workspace_diff_since_b64da14"] or "dry-run",
        "adr_0023_amendment_commit": head, "colab_cu_used": 0.0,
        **{k: prepared[k] for k in ("export_t0_kst", "export_rows", "export_manifest_sha256",
                                    "export_body_expires_at", "embeddings_sha256", "embeddings_fp16_vs_fp32_cos",
                                    "day_windows_utc", "snapshot_sha256", "sample_manifest_sha256",
                                    "bucket_allocation")},
        "user_decisions": {"newsletter_embedder": "record_only_mac", "label_subset": "full"},
    })
    if mutate_prereg:
        mutate_prereg(prereg)
    return DryInputs(Path(root), data, export_dir, pack_dir, manifest, prereg, prepared, t0)


def dry_config(inputs: DryInputs, transport: Any, **overrides: Any) -> RunConfig:
    """페이크 전송 계층을 쓰는 운영 어댑터를 레지스트리에 넣고, 드라이 런용 RunConfig를 만든다."""
    from evaluation.llm import e0_synthetic

    roles = sorted({(spec["provider"], spec["model"]) for spec in reg.ROLES.values()})
    e0_synthetic.install_fake_clients(transport, roles)
    params = dict(export_dir=inputs.export_dir, pack_dir=inputs.pack_dir, prereg=inputs.prereg,
                  manifest_path=inputs.manifest, dry_run=True, check_code_identity=False, require_api_key=False,
                  now=inputs.t0, command="python -m evaluation.llm.e0_baseline --dry-run")
    params.update(overrides)
    return RunConfig(**params)


def dry_run(root: Path, *, faults: Optional[Any] = None, fake_transport: Optional[Any] = None,
            t0: Optional[datetime] = None, progress: Callable[[str], None] = lambda m: None,
            mutate_prereg: Optional[Callable[[Dict[str, Any]], None]] = None,
            run_report: bool = True, **overrides: Any) -> Dict[str, Any]:
    """합성 반출본 + 페이크 LLM 전송 계층으로 사슬 전체를 돈다. 돌려주는 것: run 요약·리포트·내보내기 경로."""
    from evaluation.llm import e0_labels, e0_report, e0_synthetic

    t0 = t0 or _utc_now().astimezone(KST).replace(microsecond=0)
    with dry_run_sandbox(root) as data:
        inputs = dry_inputs(root, data, t0, mutate_prereg)
        transport = fake_transport or e0_synthetic.FakeLLMTransport(
            faults or e0_synthetic.FaultPlan(dict(e0_synthetic.DRY_RUN_FAULTS)))
        cfg = dry_config(inputs, transport, progress=progress, **overrides)
        summary = run_e0(cfg)
        run_dir = e0_root() / summary["run"]
        out: Dict[str, Any] = {"summary": summary, "run_dir": str(run_dir), "prepared": inputs.prepared,
                               "fake_requests": transport.requests, "inputs": inputs}
        if run_report:
            report = e0_report.build_report(run_dir, prereg=inputs.prereg, manifest_path=inputs.manifest,
                                            export_dir=inputs.export_dir, now=t0)
            paths = e0_report.write_report(report, Path(root) / "reports" / "llm",
                                           texts=e0_report.leak_corpus(run_dir, inputs.export_dir, now=t0))
            out["report"] = {k: str(v) for k, v in paths.items()}
            out["report_json"] = report
            if not summary.get("void"):
                out["labels"] = e0_labels.export_blind(run_dir, export_dir=inputs.export_dir,
                                                       manifest_path=inputs.manifest, prereg=inputs.prereg,
                                                       labels_root=data / "labels", seed=20261017, now=t0)
        return out


# ---------------------------------------------------------------- CLI


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E0 기준선 측정 러너 (ADR 0009 A8)")
    parser.add_argument("--dry-run", action="store_true", help="합성 반출본 + 페이크 LLM 전송 계층으로 사슬 전체를 돈다")
    parser.add_argument("--dry-run-root", default=None, help="드라이 런 산출물 위치(기본: 임시 디렉터리, 끝나면 지움)")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("prepare", help="표본·순서·스냅숏을 만들고 실행 전 기록 값을 출력(LLM 호출 없음)")
    r = sub.add_parser("run", help="pre-flight → warmup → eval")
    for sp in (p, r):
        sp.add_argument("--export-dir", required=True)
        sp.add_argument("--pack-dir", required=True)
    # run에는 사전 등록·매니페스트 경로를 바꾸는 옵션이 없다: 실행은 HEAD에 커밋된 두 파일만 읽는다(A8.0 4단계).
    p.add_argument("--manifest", default=str(reg.REPO_ROOT / reg.SAMPLE_MANIFEST_REL),
                   help="표본 매니페스트를 쓸 곳(기본: 등록한 경로 - 실행 전 기록 PR에 이 파일을 커밋한다)")
    r.add_argument("--resume", default=None, help="이어서 돌 run 이름(data/experiments/e0/<run>)")
    r.add_argument("--resume-reason", default="")
    args = parser.parse_args(argv)

    if args.dry_run:
        keep = args.dry_run_root is not None
        root = Path(args.dry_run_root) if keep else Path(tempfile.mkdtemp(prefix="e0-dry-run-"))
        try:
            out = dry_run(root, progress=lambda m: print(m, flush=True))
            print(json.dumps({"summary": {k: out["summary"][k] for k in ("run", "reason", "n_eval", "n_warmup",
                                                                          "n_processed", "outcomes", "void",
                                                                          "refusals", "db_clean")},
                              "ledger_settled_usd": out["summary"]["ledger"]["settled_usd"],
                              "fake_requests": out["fake_requests"], "report": out.get("report"),
                              "labels": {k: v for k, v in (out.get("labels") or {}).items() if k != "dir"}},
                             ensure_ascii=False, indent=1))
        finally:
            if not keep:
                shutil.rmtree(root, ignore_errors=True)
        return 0
    if args.command == "prepare":
        head = reg.head_problems(reg.REPO_ROOT)   # A8.2 (a)(b) - 직전에 fetch
        if head:
            print("prepare는 main에 있는 커밋의 깨끗한 체크아웃(code_sha)에서 돈다(A8.0 3단계): " + "; ".join(head),
                  file=sys.stderr)
            return 2
        try:
            out = prepare(Path(args.export_dir), Path(args.pack_dir), manifest_out=Path(args.manifest))
        except e0_sample.FrameTooSmall as e:
            # A8.1: 5개 창으로도 eval이 9개 미만 - void(프레임 부족)로 A8.10에 적고 반출을 다시 한다
            print(json.dumps({"frame": "void_frame_too_small", "detail": str(e)}, ensure_ascii=False))
            return 3
        except e0_sample.ExportTooNarrow as e:
            print(json.dumps({"frame": "export_too_narrow", "detail": str(e)}, ensure_ascii=False))
            return 4
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if args.command == "run":
        cfg = RunConfig(export_dir=Path(args.export_dir), pack_dir=Path(args.pack_dir), prereg=reg.load_prereg(),
                        manifest_path=reg.REPO_ROOT / reg.SAMPLE_MANIFEST_REL, resume=args.resume,
                        resume_reason=args.resume_reason,
                        command=" ".join(["python -m evaluation.llm.e0_baseline", *(argv or sys.argv[1:])]))
        try:
            summary = run_e0(cfg)
        except Refused as e:
            print(str(e), file=sys.stderr)
            return 2
        print(json.dumps({k: summary[k] for k in ("run", "reason", "n_eval", "n_processed", "outcomes", "void",
                                                  "stop")}, ensure_ascii=False, indent=1, default=str))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
