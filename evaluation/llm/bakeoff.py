"""LLM bake-off 러너 - ADR 0009 (사전 등록: evaluation/llm/preregistration/bakeoff-v1.yaml).

단계(각각 재개 가능 - 이미 기록된 키는 건너뛴다):
  generate      후보(생성+메타+문체 모델) × 평가셋 클러스터를 **단일 패스**로 생성하고,
                호출별 토큰/지연/시도 수/스키마 통과, 결정론적 사실성·문체 드리프트 결과,
                비용을 data/bakeoff/<run>/generations.jsonl에 남긴다.
  judge         생성된 형식체 초안을 사전 등록한 judge들(v1/v2)로 평가 -> judgments.jsonl
  cluster-eval  ClusterEvaluator를 평가셋 클러스터에 돌려 decision/confidence 기록
                -> cluster_evals.jsonl (confidence ROC용)
  export-blind  라벨링용 블라인드 파일(불투명 id·무작위 순서)과, UI가 읽지 않는
                blind_key.json을 따로 쓴다.

인프라 실패(킬 스위치, HTTP 401/402/403/404, API 키 없음)는 결과로 기록하지 않고 실행을
멈춘다 - 다시 실행하면 멈춘 곳부터 이어간다. 모델 품질에 속하는 실패(스키마 검증 소진,
length, content_filter, 생성기 로컬 폴백)는 결과로 기록한다.

본문·생성물은 전부 data/(gitignore) 아래에만 쓴다 - 기사 저작권.

사용 예 (실제 호출은 비용이 든다 - 사전 등록 규칙 확인 후 실행):
  python -m evaluation.llm.bakeoff generate --evalset v1 --split warmup --run bakeoff-v1-preflight
  python -m evaluation.llm.bakeoff generate --evalset v1
  python -m evaluation.llm.bakeoff judge --evalset v1
  python -m evaluation.llm.bakeoff cluster-eval --evalset v1 --provider gemini --model gemini-3.1-flash-lite
  python -m evaluation.llm.bakeoff export-blind
"""

import argparse
import json
import logging
import os
import random
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import yaml

from core.llm.client import LLMClient, LLMResult

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREREG = REPO_ROOT / "evaluation" / "llm" / "preregistration" / "bakeoff-v1.yaml"
RUNS_DIR = REPO_ROOT / "data" / "bakeoff"
LABELS_DIR = REPO_ROOT / "data" / "labels"

# 재시도해도 소용없고 모델 품질과 무관한 실패 - 기록하지 않고 멈춘다 (ADR 0009 실행 프로토콜)
INFRA_HTTP_STATUSES = frozenset({401, 402, 403, 404})

# 사전 등록 게이트의 정의에 쓰는 유형 (Settings 값이 환경변수로 바뀌어도 bake-off 정의는 고정)
OPS_FAITHFULNESS_BLOCKING = ("numbers", "quotes")  # 운영 게이트 기본값 -> 비용식의 p
TONE_DRIFT_BLOCKING = ("numbers", "dates", "entities_added")  # G2


class InfraFailure(RuntimeError):
    """결과로 기록하지 않고 실행을 멈춰야 하는 실패."""


def load_prereg(path: Optional[Path] = None) -> dict:
    with open(path or DEFAULT_PREREG, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# 호출 기록
# ---------------------------------------------------------------------------

@dataclass
class CallRecord:
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    attempts: int
    schema_failures: int
    parsed: bool
    responded: bool
    first_try_schema_pass: bool
    error: Optional[str]
    http_status: Optional[int]


_RESPONSE_ERRORS = {"length", "content_filter"}


def call_record(purpose: str, r: LLMResult) -> CallRecord:
    parsed = r.parsed is not None
    return CallRecord(
        purpose=purpose,
        provider=r.provider,
        model=r.model,
        input_tokens=r.usage.input_tokens,
        output_tokens=r.usage.output_tokens,
        latency_s=r.latency_s,
        attempts=r.attempts,
        schema_failures=r.schema_failures,
        parsed=parsed,
        # G3의 분모: 모델 응답을 한 번이라도 받은 호출 (전송 실패만 있었던 호출은 제외)
        responded=parsed or r.schema_failures > 0 or r.error in _RESPONSE_ERRORS,
        first_try_schema_pass=parsed and r.schema_failures == 0,
        error=r.error,
        http_status=r.http_status,
    )


class RecordingClient(LLMClient):
    """다른 LLMClient를 감싸 호출마다 CallRecord를 남기고, 인프라 실패는 예외로 바꾼다.

    NewsReconstructor/ToneConverter/평가기는 실패한 LLMResult를 받으면 로컬 폴백으로
    넘어가 버린다. 402 같은 결제 문제까지 "모델이 폴백을 썼다"로 기록되면 결과가 오염되므로
    호출 지점에서 바로 끊는다.
    """

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.calls: List[CallRecord] = []

    def complete(self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
        r = self.inner.complete(messages, schema=schema, purpose=purpose,
                                temperature=temperature, max_tokens=max_tokens)
        if r.error == "kill_switch":
            raise InfraFailure(f"{self.provider}/{self.model}: LLM kill switch 활성화")
        if r.http_status in INFRA_HTTP_STATUSES:
            raise InfraFailure(f"{self.provider}/{self.model}: HTTP {r.http_status} ({r.error})")
        self.calls.append(call_record(purpose, r))
        return r


ClientFactory = Callable[[str, str], LLMClient]


def default_client_factory(provider: str, model: str) -> LLMClient:
    from core.llm.registry import client_for

    return client_for(provider, model)


def _make_client(factory: ClientFactory, spec: dict) -> RecordingClient:
    try:
        return RecordingClient(factory(spec["provider"], spec["model"]))
    except ValueError as e:  # API 키 없음 등 설정 문제
        raise InfraFailure(str(e)) from e


# ---------------------------------------------------------------------------
# JSONL 저장소 (append-only, 재개 가능)
# ---------------------------------------------------------------------------

class JsonlStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def rows(self) -> List[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # 쓰는 도중 죽은 마지막 줄 - 그 항목은 다시 돌린다
                    logger.warning("%s: 손상된 줄 1개를 건너뜁니다", self.path)
        return out

    def keys(self) -> set:
        return {r["key"] for r in self.rows() if "key" in r}

    def append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_newline = self.path.exists() and self.path.stat().st_size > 0 and not self._ends_with_newline()
        with open(self.path, "a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _ends_with_newline(self) -> bool:
        with open(self.path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"


# ---------------------------------------------------------------------------
# 생성
# ---------------------------------------------------------------------------

def articles_for(item) -> List[dict]:
    """평가셋 항목 -> 워크플로우가 쓰는 기사 dict(state["current_articles"]와 같은 모양)."""
    return [
        {"id": a.raw_news_id, "title": a.title, "content": a.body, "press_name": a.press_name}
        for a in item.articles
    ]


def _cost(pricing, calls: Iterable[CallRecord], on: date) -> float:
    return sum(pricing.cost(c.model, c.input_tokens, c.output_tokens, on=on) for c in calls)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_one(candidate: dict, item, client_factory: ClientFactory, pricing, on: date) -> dict:
    from core.reconstruction.generator import NewsReconstructor
    from core.tone_converter import ToneConverter
    from workflow.gates import check_newsletter_faithfulness, check_tone_drift

    class _FlaggingToneConverter(ToneConverter):
        fallback_used = False

        def _fallback_convert(self, original, last):
            self.fallback_used = True
            return super()._fallback_convert(original, last)

    articles = articles_for(item)
    gen_client = _make_client(client_factory, candidate["generator"])
    tone_client = _make_client(client_factory, candidate["tone"])

    t0 = time.monotonic()
    draft = NewsReconstructor(llm_client=gen_client).reconstruct(articles)
    gen_s = time.monotonic() - t0

    converted, tone_s, tone_fallback = None, 0.0, None
    if draft:
        converter = _FlaggingToneConverter(llm_client=tone_client)
        t1 = time.monotonic()
        converted = converter.convert(draft)
        tone_s = time.monotonic() - t1
        tone_fallback = converter.fallback_used

    def parsed_for(purpose):
        calls = [c for c in gen_client.calls if c.purpose == purpose]
        return bool(calls) and calls[-1].parsed

    faith = drift = None
    flags = {"generation_failed": draft is None}
    if draft:
        f_all = check_newsletter_faithfulness(draft, articles, blocking_types=("numbers", "quotes", "entities"))
        f_ops = check_newsletter_faithfulness(draft, articles, blocking_types=OPS_FAITHFULNESS_BLOCKING)
        faith = f_all.to_dict()
        d = check_tone_drift(draft, converted, blocking_types=TONE_DRIFT_BLOCKING) if converted else None
        drift = d.to_dict() if d else None
        flags.update({
            "unsupported_number": bool(f_all.blocking.get("numbers")),  # G1
            "ops_gate_blocked": not f_ops.passed,  # 비용식의 p
            "tone_drift": bool(d and not d.passed),  # G2
        })

    return {
        "candidate": candidate["name"],
        "item_id": item.item_id,
        "generator": candidate["generator"],
        "tone": candidate["tone"],
        "draft": draft,
        "converted": converted,
        "fallback": {
            "content": not parsed_for("newsletter_content_gen"),
            "meta": draft is not None and not parsed_for("newsletter_meta_gen"),
            "tone": tone_fallback,
        },
        "calls": {"generation": [asdict(c) for c in gen_client.calls],
                  "tone": [asdict(c) for c in tone_client.calls]},
        "timing": {"generation_s": gen_s, "tone_s": tone_s, "total_s": gen_s + tone_s},
        "faithfulness": faith,
        "tone_drift": drift,
        "flags": flags,
        "cost_usd": {"generation": _cost(pricing, gen_client.calls, on),
                     "tone": _cost(pricing, tone_client.calls, on)},
        "created_at": _now(),
    }


def _check_priced(pricing, specs: Iterable[dict]) -> None:
    missing = sorted({s["model"] for s in specs if s["model"] not in pricing.models()})
    if missing:
        raise KeyError(f"단가표(config/llm_pricing.yaml)에 없는 모델: {missing} - 실행 전에 추가하세요")


def _ensure_kill_switch_off() -> None:
    from core.llm.kill_switch import LLMKillSwitchEngaged, ensure_kill_switch_off

    try:
        ensure_kill_switch_off()
    except LLMKillSwitchEngaged as e:
        raise InfraFailure(f"LLM kill switch 활성화: {e}") from e


def _select(entries: Sequence[dict], names: Optional[Sequence[str]]) -> List[dict]:
    if not names:
        return list(entries)
    unknown = set(names) - {e["name"] for e in entries}
    if unknown:
        raise ValueError(f"사전 등록에 없는 이름: {sorted(unknown)}")
    return [e for e in entries if e["name"] in names]


def _run_loop(tasks, store: JsonlStore, log) -> dict:
    """tasks: (key, fn) 목록. 이미 있는 키는 건너뛰고, 인프라 실패에서 멈춘다."""
    done = store.keys()
    stats = {"done": 0, "skipped": 0, "stopped": None}
    for key, fn in tasks:
        if key in done:
            stats["skipped"] += 1
            continue
        try:
            row = fn()
        except InfraFailure as e:
            stats["stopped"] = str(e)
            log(f"중단: {e} - 다시 실행하면 {key}부터 이어갑니다")
            break
        store.append({"key": key, **row})
        stats["done"] += 1
        log(f"기록: {key}")
    return stats


def run_generation(items, prereg: dict, run_dir: Path, *, client_factory: ClientFactory = default_client_factory,
                   pricing=None, candidates: Optional[Sequence[str]] = None, on: Optional[date] = None,
                   log=print) -> dict:
    from core.llm.pricing import load_pricing

    pricing = pricing or load_pricing()
    cands = _select(prereg["candidates"], candidates)
    _check_priced(pricing, [c["generator"] for c in cands] + [c["tone"] for c in cands])
    on = on or date.today()
    store = JsonlStore(Path(run_dir) / "generations.jsonl")
    try:
        _ensure_kill_switch_off()
    except InfraFailure as e:
        log(f"중단: {e}")
        return {"done": 0, "skipped": 0, "stopped": str(e)}

    # 후보를 바깥 루프가 아니라 안쪽에 둔다 - 중간에 멈춰도 후보들이 같은 클러스터 집합을
    # 가진 채로 멈춰 짝지은 비교가 가능한 상태로 남는다.
    tasks = [
        (f"{c['name']}|{item.item_id}", (lambda c=c, item=item: generate_one(c, item, client_factory, pricing, on)))
        for item in items
        for c in cands
    ]
    return _run_loop(tasks, store, log)


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------

def judge_one(judge: dict, gen_row: dict, articles: List[dict], client_factory: ClientFactory,
              pricing, on: date) -> dict:
    from core.llm.registry import _model_family
    from workflow.evaluators import NewsletterEvaluator, NewsletterEvaluatorV1

    client = _make_client(client_factory, judge)
    cls = NewsletterEvaluator if judge.get("version", "v2") == "v2" else NewsletterEvaluatorV1
    t0 = time.monotonic()
    result = cls(llm_client=client).evaluate(gen_row["draft"], articles)
    return {
        "candidate": gen_row["candidate"],
        "item_id": gen_row["item_id"],
        "judge": judge["name"],
        "judge_version": judge.get("version", "v2"),
        "judge_provider": judge["provider"],
        "judge_model": judge["model"],
        "judge_family": _model_family(judge["provider"]),
        "generator_family": _model_family(gen_row["generator"]["provider"]),
        "result": result,
        "calls": [asdict(c) for c in client.calls],
        "wall_s": time.monotonic() - t0,
        "cost_usd": _cost(pricing, client.calls, on),
        "created_at": _now(),
    }


def run_judging(items, prereg: dict, run_dir: Path, *, client_factory: ClientFactory = default_client_factory,
                pricing=None, judges: Optional[Sequence[str]] = None, on: Optional[date] = None, log=print) -> dict:
    from core.llm.pricing import load_pricing

    pricing = pricing or load_pricing()
    js = _select(prereg["judges"], judges)
    _check_priced(pricing, js)
    on = on or date.today()
    try:
        _ensure_kill_switch_off()
    except InfraFailure as e:
        log(f"중단: {e}")
        return {"done": 0, "skipped": 0, "stopped": str(e)}

    by_item = {i.item_id: articles_for(i) for i in items}
    gens = [g for g in JsonlStore(Path(run_dir) / "generations.jsonl").rows()
            if g.get("draft") and g["item_id"] in by_item]
    tasks = [
        (f"{g['candidate']}|{g['item_id']}|{j['name']}",
         (lambda g=g, j=j: judge_one(j, g, by_item[g["item_id"]], client_factory, pricing, on)))
        for g in gens
        for j in js
    ]
    return _run_loop(tasks, JsonlStore(Path(run_dir) / "judgments.jsonl"), log)


# ---------------------------------------------------------------------------
# ClusterEvaluator
# ---------------------------------------------------------------------------

def run_cluster_eval(items, run_dir: Path, evaluator_spec: dict, *,
                     client_factory: ClientFactory = default_client_factory, pricing=None,
                     on: Optional[date] = None, log=print) -> dict:
    from core.llm.pricing import load_pricing
    from workflow.evaluators import ClusterEvaluator

    pricing = pricing or load_pricing()
    _check_priced(pricing, [evaluator_spec])
    on = on or date.today()
    try:
        _ensure_kill_switch_off()
    except InfraFailure as e:
        log(f"중단: {e}")
        return {"done": 0, "skipped": 0, "stopped": str(e)}

    def one(item):
        client = _make_client(client_factory, evaluator_spec)
        result = ClusterEvaluator(llm_client=client).evaluate(articles_for(item))
        return {
            "item_id": item.item_id,
            "evaluator": evaluator_spec,
            # 워크플로우와 같은 순서의 기사 id - outlier_indices를 id로 되돌릴 때 쓴다
            "article_ids": [a.raw_news_id for a in item.articles],
            "result": result,
            "calls": [asdict(c) for c in client.calls],
            "cost_usd": _cost(pricing, client.calls, on),
            "created_at": _now(),
        }

    tag = f"{evaluator_spec['provider']}/{evaluator_spec['model']}"
    tasks = [(f"{item.item_id}|{tag}", (lambda item=item: one(item))) for item in items]
    return _run_loop(tasks, JsonlStore(Path(run_dir) / "cluster_evals.jsonl"), log)


# ---------------------------------------------------------------------------
# 블라인드 내보내기
# ---------------------------------------------------------------------------

def export_blind(items, run_dir: Path, labels_dir: Path, prereg: dict, seed: Optional[int] = None) -> dict:
    """라벨링 UI가 읽는 파일(labels_dir)과 후보 매핑(run_dir/blind_key.json)을 분리해 쓴다.

    - clusters.jsonl: 클러스터 라벨용 원문(클러스터 라벨은 출력보다 먼저 단다)
    - outputs.jsonl: 불투명 id + 무작위 순서의 출력(형식체 초안 + 문체 변환본). 후보 이름,
      모델, 자동 지표(사실성·드리프트)는 넣지 않는다 - 라벨러가 검사기 결과에 끌려가지 않게.
    - relabel.json: 재라벨(intra-rater) 대상. 사전 등록한 개수만큼 무작위.
    생성 실패(draft 없음) 행은 내보내지 않는다 - 분석에서 publishable=N으로 처리한다.
    """
    run_dir, labels_dir = Path(run_dir), Path(labels_dir)
    key_path = run_dir / "blind_key.json"
    if key_path.exists():
        raise FileExistsError(f"{key_path}가 이미 있습니다 - 블라인드 id를 다시 만들면 기존 라벨과 어긋납니다")
    seed = secrets.randbits(63) if seed is None else seed
    rng = random.Random(seed)

    gens = [g for g in JsonlStore(run_dir / "generations.jsonl").rows() if g.get("draft")]
    rng.shuffle(gens)
    used, key, outputs = set(), {}, []
    for g in gens:
        oid = f"o{rng.getrandbits(40):010x}"
        while oid in used:
            oid = f"o{rng.getrandbits(40):010x}"
        used.add(oid)
        key[oid] = {"candidate": g["candidate"], "item_id": g["item_id"]}
        d, c = g["draft"], g.get("converted") or {}
        outputs.append({
            "output_id": oid,
            "item_id": g["item_id"],
            "draft": {k: d.get(k, "") for k in ("title", "sentence", "content")},
            "converted": {k: c.get(k, "") for k in ("title", "summary", "content")},
        })

    item_ids = sorted({g["item_id"] for g in gens})
    by_id = {i.item_id: i for i in items}
    clusters = [
        {"item_id": iid, "articles": [
            {"raw_news_id": a.raw_news_id, "press_name": a.press_name, "title": a.title, "body": a.body, "url": a.url}
            for a in by_id[iid].articles
        ]}
        for iid in item_ids if iid in by_id
    ]

    hr = prereg.get("human_reliability", {})
    relabel = {
        "outputs": sorted(rng.sample(sorted(key), min(int(hr.get("relabel_outputs", 30)), len(key)))),
        "clusters": sorted(rng.sample(item_ids, min(int(hr.get("relabel_clusters", 10)), len(item_ids)))),
        "min_hours_between_rounds": float(hr.get("min_hours_between_rounds", 48)),
    }

    labels_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("outputs.jsonl", outputs), ("clusters.jsonl", clusters)):
        with open(labels_dir / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (labels_dir / "relabel.json").write_text(json.dumps(relabel, ensure_ascii=False, indent=2), encoding="utf-8")
    run_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps({"seed": seed, "outputs": key}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"outputs": len(outputs), "clusters": len(clusters)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _git_sha() -> Optional[str]:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def _check_run_meta(run_dir: Path, prereg: dict, evalset: str, split: str) -> None:
    """같은 run 디렉터리에 다른 사전 등록/평가셋 결과가 섞이지 않게 막는다."""
    meta_path = run_dir / "run_meta.json"
    meta = {"prereg_id": prereg.get("id"), "evalset": evalset, "split": split}
    if meta_path.exists():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        diff = {k: (old.get(k), v) for k, v in meta.items() if old.get(k) != v}
        if diff:
            raise SystemExit(f"{run_dir}는 다른 설정으로 시작된 실행입니다: {diff}")
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({**meta, "started_at": _now(), "git_sha": _git_sha()},
                                    ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    from evaluation.llm.evalset import default_paths, load_evalset

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, needs_evalset=True):
        if needs_evalset:
            p.add_argument("--evalset", required=True, help="evaluation/llm/evalset.py로 만든 평가셋 이름")
            p.add_argument("--split", default="eval", choices=["eval", "warmup"])
        p.add_argument("--run", help="실행 이름 (기본: 사전 등록 id)")

    p_gen = sub.add_parser("generate")
    common(p_gen)
    p_gen.add_argument("--candidates", nargs="*")
    p_judge = sub.add_parser("judge")
    common(p_judge)
    p_judge.add_argument("--judges", nargs="*")
    p_ce = sub.add_parser("cluster-eval")
    common(p_ce)
    p_ce.add_argument("--provider", required=True)
    p_ce.add_argument("--model", required=True)
    p_exp = sub.add_parser("export-blind")
    common(p_exp)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    prereg = load_prereg(args.prereg)
    run = args.run or prereg["id"]
    run_dir = RUNS_DIR / run
    manifest, bodies = default_paths(args.evalset)
    items = load_evalset(manifest, bodies, split=args.split)
    _check_run_meta(run_dir, prereg, args.evalset, args.split)

    if args.cmd == "generate":
        stats = run_generation(items, prereg, run_dir, candidates=args.candidates)
    elif args.cmd == "judge":
        stats = run_judging(items, prereg, run_dir, judges=args.judges)
    elif args.cmd == "cluster-eval":
        stats = run_cluster_eval(items, run_dir, {"provider": args.provider, "model": args.model})
    else:
        stats = export_blind(items, run_dir, LABELS_DIR / run, prereg)
    print(json.dumps(stats, ensure_ascii=False))
    return 1 if isinstance(stats, dict) and stats.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
