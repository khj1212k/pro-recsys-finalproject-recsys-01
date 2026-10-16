"""E0 표본 만들기 - 동결 반출본의 날짜 창마다 운영 클러스터링을 돌리고 등록한 규칙으로 15+2개를 뽑는다 (ADR 0009 A8.1).

LLM을 부르지 않는다. 같은 반출본·임베딩 팩에서는 언제 돌려도 같은 결과가 나와야 한다 - 러너는 실행 직전에 이 계산을
다시 해서 실행 전 기록(A8.10)의 스냅숏·매니페스트 sha256과 대조한다.

- 창: 반출본에 적힌 날짜 창 계획(`FrozenExport.day_windows()`)의 **마지막 3개**(T0 직전 완결된 KST 날짜 D₁<D₂<D₃의
  06:00 KST)부터. eval 15 + warmup 2를 다 뽑지 못하면 하루씩 앞으로(최대 5개) 늘려 같은 seed로 처음부터 다시 뽑는다.
  run k는 쓴 창들의 시간 순 번호다(1이 가장 이르다).
- 클러스터링: 운영 `NewsClusterer.cluster_news()`(evaluation.llm.e0_store.cluster_frozen_window - 파일 로더만 대역).
- 후보: `evaluation.llm.evalset.candidates_from_cluster_log`(카테고리 `미분류`, split_v2는 cluster_meta에서).
- 추출: `evaluation.llm.evalset.stratified_sample(n=15, seed=20261006, hard_fraction=0.0, warmup=2, min_size=3)`.
- 순서: warmup 2개가 먼저((run_k, cluster_id) 오름차순), 그다음 eval을 (run_k, cluster_id) 오름차순으로 놓고
  `random.Random(20261007).shuffle`. 매니페스트의 `order`는 처리 순서 전체에서의 위치(0부터)다.
- 매니페스트에는 id·URL·언론사·본문/제목 sha256(반출본과 같은 UTF-8 sha256)·길이·order만 쓴다(ADR 0023).
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evaluation.llm import e0_registered as reg
from evaluation.llm import e0_store, evalset, frozen_export

SAMPLE_FORMAT = "e0-sample-manifest/1"


class FrameTooSmall(RuntimeError):
    """5개 창으로도 eval이 9개 미만이다 - void(프레임 부족), 반출을 다시 한다(A8.1)."""


class ExportTooNarrow(RuntimeError):
    """표본을 다 뽑으려면 창을 늘려야 하는데 반출본에 그 날짜가 없다 - 더 많은 날짜로 다시 반출한다."""


@dataclass
class SamplePlan:
    runs: List[e0_store.ClusterRun]
    windows: List[frozen_export.Window]
    sample: List[evalset.SampledCluster]
    order: List[str]                      # 처리 순서의 item_id
    rows: List[Dict[str, Any]]            # 매니페스트 행(order 순)
    frame: str                            # "full" | "short_n"
    candidates_by_bucket: Dict[str, int] = field(default_factory=dict)
    extracted_after_pseudo_now: Dict[str, int] = field(default_factory=dict)

    @property
    def eval_items(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["split"] == "eval"]

    @property
    def warmup_items(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["split"] == "warmup"]

    def bucket_allocation(self) -> Dict[str, int]:
        alloc = {"3-4": 0, "5-9": 0, "10+": 0}
        for r in self.eval_items:
            alloc[r["size_bucket"]] += 1
        return alloc

    def snapshots(self) -> Dict[str, Dict[str, Any]]:
        return {f"run{k}": run.membership_snapshot() for k, run in enumerate(self.runs, start=1)}

    def snapshot_sha256(self) -> Dict[str, str]:
        return {name: snap["membership_sha256"] for name, snap in self.snapshots().items()}

    def day_windows_utc(self) -> List[List[str]]:
        return [[w.start.strftime("%Y-%m-%dT%H:%M:%S"), w.end.strftime("%Y-%m-%dT%H:%M:%S")] for w in self.windows]

    def manifest_bytes(self) -> bytes:
        return manifest_bytes(self.rows)

    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest_bytes()).hexdigest()

    def record_fields(self) -> Dict[str, Any]:
        """yaml pre_run_record에 옮겨 적는 값(표본 부분)."""
        return {"day_windows_utc": self.day_windows_utc(), "snapshot_sha256": self.snapshot_sha256(),
                "sample_manifest_sha256": self.manifest_sha256(), "bucket_allocation": self.bucket_allocation()}


def manifest_bytes(rows: Sequence[Dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def candidates_for(run: e0_store.ClusterRun, run_k: int) -> List[evalset.ClusterCandidate]:
    log = {str(cid): [int(x) for x in members] for cid, members in run.clusters.items()}
    log["cluster_meta"] = {str(cid): dict(meta) for cid, meta in run.cluster_meta.items()}
    return evalset.candidates_from_cluster_log(run_k, log)


def processing_order(sample: Sequence[evalset.SampledCluster]) -> List[evalset.SampledCluster]:
    key = lambda s: (s.candidate.run_id, s.candidate.cluster_id)  # noqa: E731
    warm = sorted((s for s in sample if s.split == "warmup"), key=key)
    evals = sorted((s for s in sample if s.split == "eval"), key=key)
    random.Random(reg.ORDER_SEED).shuffle(evals)
    return warm + evals if reg.ORDER["warmup_first"] else evals + warm


def _extracted_after(source: e0_store.FileArticleSource, window: frozen_export.Window) -> int:
    """창 안에 있지만 본문 추출이 의사 시각 뒤에 끝난 행 수(A8.2 이탈 7 - 운영에서 t_k에 돌렸다면 빠졌을 행)."""
    count = 0
    for row in source.export.rows:
        crawled = frozen_export._parse(row["crawled_at"])
        if window.start <= crawled < window.end and row.get("extracted_at"):
            if frozen_export._parse(row["extracted_at"]) >= window.end:
                count += 1
    return count


def build_plan(source: e0_store.FileArticleSource) -> SamplePlan:
    """반출본·팩에서 표본을 만든다. 같은 입력이면 같은 결과다."""
    all_windows = source.export.day_windows()
    rule = reg.CLUSTERING["extend_days_if_short"]
    days = reg.CLUSTERING["days"]
    runs_by_end: Dict[datetime, e0_store.ClusterRun] = {}
    while True:
        if days > len(all_windows):
            raise ExportTooNarrow(f"창을 {days}개로 늘려야 하는데 반출본의 날짜 창은 {len(all_windows)}개다 - "
                                  f"export --days {rule['max_days']}로 다시 반출한다")
        windows = all_windows[-days:]
        runs = []
        for window in windows:
            if window.end not in runs_by_end:
                runs_by_end[window.end] = e0_store.cluster_frozen_window(
                    source, window.end, min_cluster_size=reg.CLUSTERING["min_cluster_size"],
                    min_samples=reg.CLUSTERING["min_samples"], lookback_hours=reg.CLUSTERING["lookback_hours"])
            runs.append(runs_by_end[window.end])
        candidates = [c for k, run in enumerate(runs, start=1) for c in candidates_for(run, k)]
        sample = evalset.stratified_sample(
            candidates, n=reg.SAMPLE["n"], seed=reg.SAMPLE["seed"], hard_fraction=reg.SAMPLE["hard_fraction"],
            warmup=reg.SAMPLE["warmup"], min_size=reg.SAMPLE["min_size"])
        n_eval = sum(1 for s in sample if s.split == "eval")
        n_warm = sum(1 for s in sample if s.split == "warmup")
        if n_eval + n_warm >= rule["need_eval_plus_warmup"] and n_eval == reg.SAMPLE["n"]:
            frame = "full"
            break
        if days < rule["max_days"]:
            days += 1
            continue
        if n_eval < rule["void_if_eval_below"]:
            raise FrameTooSmall(f"창 {days}개로 eval {n_eval}개 - {rule['void_if_eval_below']}개 미만이라 void(프레임 부족)")
        frame = "short_n"
        break

    ordered = processing_order(sample)
    rows_by_id = {int(r["id"]): r for r in source.export.rows}
    manifest_rows = []
    for position, s in enumerate(ordered):
        c = s.candidate
        run = runs[c.run_id - 1]
        articles = []
        for aid in c.article_ids:
            row = rows_by_id[int(aid)]
            articles.append({"raw_news_id": int(aid), "url": row["url"], "press_name": row["press"],
                             "body_sha256": row["content_sha256"], "title_sha256": row["title_sha256"],
                             "body_chars": row["body_chars"]})
        manifest_rows.append({
            "format": SAMPLE_FORMAT, "item_id": c.item_id, "split": s.split, "order": position, "run_k": c.run_id,
            "cluster_id": c.cluster_id, "pseudo_now_utc": _iso(run.pseudo_now), "size": len(c.article_ids),
            "size_bucket": evalset.size_bucket(len(c.article_ids)), "category": c.category, "split_v2": c.split_v2,
            "hard_case": c.hard_case, "sampling_weight": round(s.sampling_weight, 6), "articles": articles,
        })
    buckets: Dict[str, int] = {"3-4": 0, "5-9": 0, "10+": 0}
    for c in (c for k, run in enumerate(runs, start=1) for c in candidates_for(run, k)):
        bucket = evalset.size_bucket(len(c.article_ids))
        if bucket:
            buckets[bucket] += 1
    return SamplePlan(runs=runs, windows=list(windows), sample=list(sample),
                      order=[r["item_id"] for r in manifest_rows], rows=manifest_rows, frame=frame,
                      candidates_by_bucket=buckets,
                      extracted_after_pseudo_now={f"run{k}": _extracted_after(source, w)
                                                  for k, w in enumerate(windows, start=1)})


def read_manifest(path: Path) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_manifest(path: Path, plan: SamplePlan) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = plan.manifest_bytes()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha256(data).hexdigest()


def write_snapshots(directory: Path, plan: SamplePlan) -> Dict[str, str]:
    """멤버십 스냅숏(ids만)을 로컬에 남긴다. 본문·제목은 없다."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, snap in plan.snapshots().items():
        (directory / f"{name}.json").write_text(json.dumps(snap, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                                                encoding="utf-8")
        out[name] = snap["membership_sha256"]
    return out


def compare_with_record(plan: SamplePlan, record: Dict[str, Any], manifest_path: Path) -> List[str]:
    """다시 계산한 표본이 실행 전 기록·커밋된 매니페스트와 같은가(A8.6 c). 비어 있으면 같다."""
    problems = []
    if plan.day_windows_utc() != record.get("day_windows_utc"):
        problems.append("day_windows_utc가 실행 전 기록과 다르다")
    if plan.snapshot_sha256() != record.get("snapshot_sha256"):
        problems.append("날짜 창별 멤버십 스냅숏 sha256이 실행 전 기록과 다르다")
    if plan.bucket_allocation() != record.get("bucket_allocation"):
        problems.append("버킷 배분이 실행 전 기록과 다르다")
    committed = Path(manifest_path).read_bytes() if Path(manifest_path).exists() else b""
    if hashlib.sha256(committed).hexdigest() != record.get("sample_manifest_sha256"):
        problems.append("커밋된 표본 매니페스트의 sha256이 실행 전 기록과 다르다")
    if committed != plan.manifest_bytes():
        problems.append("다시 계산한 표본·순서가 커밋된 매니페스트와 다르다")
    return problems
