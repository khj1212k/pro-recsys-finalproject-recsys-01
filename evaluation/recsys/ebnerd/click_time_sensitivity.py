"""클릭 시각 근사(클릭 시각 = 노출 시각)의 민감도 점검 — popularity 베이스라인만, 재학습 없음.

EB-NeRD 행동 로그에는 클릭 시각이 없어 `loaders.click_events`는 클릭을 그 노출의 시각에 둔다. 실제 클릭은
노출 뒤(그 페이지에 머문 read_time 안)에 일어나므로, 요청 시각 t의 trailing 인기도는 t 직전 노출에서 나와
실제로는 t 이후에 일어난 클릭까지 셀 수 있다. 여기서는

1. 클릭이 있는 평가 노출의 read_time 분포(클릭 지연의 대략적 상한)와
2. 인기도 창의 끝을 t − gap으로 당겼을 때(창 길이는 그대로) popularity 6h/24h/48h의 P1·P2 지표 변화

를 잰다. gap을 두면 누출될 수 있는 클릭과 함께 정상적인 최근 클릭도 빠지므로, 변화량은 근사가 지표에 준
영향의 상한 쪽 추정이다. LightGBM 모델은 재학습이 필요해 다루지 않는다. gap=0 결과는 v1과 같아야 한다
(`--compare-json`으로 확인해 JSON에 기록).

    EBNERD_ROOT=... .venv/bin/python -m evaluation.recsys.ebnerd.click_time_sensitivity \
        --dataset ebnerd_small --compare-json reports/recsys/ebnerd_v1.json \
        --out-json reports/recsys/ebnerd_v1_click_time_sensitivity.json
"""
from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from recsys_core import HOUR

from ..metrics import ranking_metrics
from .loaders import ebnerd_root, to_epoch_seconds
from .prepare import impressions_in, load_bench, p1_task, p2_task, protocol_windows
from .run_ebnerd import P1_KS, P1_METRICS, SPLIT_SEED, MetricBank, _git_sha, _json_default

log = logging.getLogger("ebnerd.click_time")
POP_WINDOWS_H = (6, 24, 48)


def popularity_scores(ctx, req, gap_seconds: int, windows_h=POP_WINDOWS_H) -> dict[str, np.ndarray]:
    """popularity_{w}h 베이스라인 점수 = [t − gap − w, t − gap) 구간 클릭 수. gap=0이면
    recsys_core.compute_features의 pop_clicks_{w}h와 같다."""
    t_end = req.time[req.pair_req] - int(gap_seconds)
    return {f"popularity_{int(w)}h": ctx.item_clicks.count(req.cand_item, t_end - int(w * HOUR), t_end)
            .astype(np.float64) for w in windows_h}


def read_time_stats(behaviors: Path, window: tuple[int, int]) -> dict:
    """[window) 안의 클릭이 있는 노출에서 read_time(그 페이지 체류 초) 분포."""
    b = pd.read_parquet(behaviors, columns=["impression_time", "read_time", "article_ids_clicked"])
    t = to_epoch_seconds(b["impression_time"])
    has_click = b["article_ids_clicked"].map(len).to_numpy() > 0
    sel = (t >= window[0]) & (t < window[1]) & has_click
    rt = b["read_time"].to_numpy(np.float64)[sel]
    ok = rt[~np.isnan(rt)]
    qs = (0.5, 0.75, 0.9, 0.95, 0.99)
    return {
        "n_impressions_with_click": int(sel.sum()), "n_missing": int(np.isnan(rt).sum()),
        "quantiles_seconds": {f"p{int(q * 100)}": float(np.quantile(ok, q)) for q in qs},
        "share_over_seconds": {str(s): float((ok > s).mean()) for s in (60, 300, 1800)},
    }


def _run(bank: MetricBank, task, feats_by_gap: dict, seeds, gaps, **metric_kw) -> dict:
    ptr, labels = task.req.cand_ptr, task.labels
    for g in gaps:
        for name, sc in feats_by_gap[g].items():
            for seed in seeds:
                bank.add(f"{name}|gap{g}", ranking_metrics(sc, labels, ptr, seed=seed, **metric_kw))
    names = sorted({n.split("|")[0] for n in bank.runs})
    return {
        "results": {n: bank.summary(n) for n in bank.runs},
        "diffs_vs_gap0": {f"{n}: gap{g} - gap0": bank.diff(f"{n}|gap{g}", f"{n}|gap0")
                          for n in names for g in gaps if g != 0},
    }


def _compare(out: dict, ref: dict, section: str, metrics) -> dict:
    """gap=0 결과와 기준 JSON(v1)의 같은 베이스라인 평균의 최대 절대 차이."""
    worst = 0.0
    for key, r in out["results"].items():
        name, gap = key.split("|")
        if gap != "gap0" or name not in ref.get(section, {}):
            continue
        for m in metrics:
            worst = max(worst, abs(r[m]["mean"] - ref[section][name][m]["mean"]))
    return {"section": section, "metrics": list(metrics), "max_abs_diff_of_means": worst}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="ebnerd_small")
    ap.add_argument("--root", default=None)
    ap.add_argument("--emb-dir", default=None)
    ap.add_argument("--fake-dim", type=int, default=None)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--gaps", type=int, nargs="+", default=[0, 300, 1800], help="초 단위, 0 포함")
    ap.add_argument("--max-test", type=int, default=None)
    ap.add_argument("--p2-sample", type=int, default=20000)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--compare-json", default=None)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args(argv)
    if 0 not in args.gaps:
        ap.error("--gaps에 0(기준)이 있어야 합니다")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)

    root = Path(args.root) if args.root else ebnerd_root()
    dataset_dir = root / args.dataset
    emb_dir = Path(args.emb_dir) if args.emb_dir else root / "derived" / args.dataset
    t_start = time.time()
    bench = load_bench(dataset_dir, emb_dir=None if args.fake_dim else emb_dir, fake_dim=args.fake_dim)
    W = protocol_windows(bench)
    va = bench.imps["validation"]
    ctx = bench.ctx["validation"]
    idx_test = impressions_in(va, W["test"])
    if args.max_test and len(idx_test) > args.max_test:
        idx_test = np.sort(np.random.default_rng(SPLIT_SEED).choice(idx_test, size=args.max_test, replace=False))

    out = {
        "meta": {"git_sha": _git_sha(), "dataset": args.dataset, "seeds": args.seeds, "gaps_seconds": args.gaps,
                 "n_boot": args.n_boot, "python": platform.python_version(),
                 "machine": f"{platform.machine()} {platform.system()}",
                 "argv": sys.argv[1:] if argv is None else argv,
                 "definition": "popularity_{w}h = [t - gap - w, t - gap) 클릭 수(클릭 시각 = 노출 시각). "
                               "gap=0이 v1과 같은 정의"},
        "read_time": read_time_stats(dataset_dir / "validation" / "behaviors.parquet", W["test"]),
    }

    task = p1_task(bench, "validation", idx_test)
    scores = {g: popularity_scores(ctx, task.req, g) for g in args.gaps}
    out["p1"] = _run(MetricBank(task.group_user, args.n_boot, metrics=P1_METRICS), task, scores, args.seeds,
                     args.gaps, ks=P1_KS)
    out["p1"]["n_impressions"] = int(task.req.n)
    del scores, task
    log.info("p1 done %.0fs", time.time() - t_start)

    rng = np.random.default_rng(SPLIT_SEED + 2)  # run_ebnerd.run_p2와 같은 표본
    idx = np.sort(rng.choice(idx_test, size=min(args.p2_sample, len(idx_test)), replace=False))
    task = p2_task(bench, "validation", idx, window_h=48, exclude_seen=True)
    scores = {g: popularity_scores(ctx, task.req, g) for g in args.gaps}
    out["p2"] = _run(MetricBank(task.group_user, args.n_boot, metrics=("ndcg@10", "recall@10", "mrr")), task,
                     scores, args.seeds, args.gaps, ks=(10,), n_pos_total=task.n_pos_total, with_auc=False)
    out["p2"]["n_requests"] = int(task.req.n)
    log.info("p2 done %.0fs", time.time() - t_start)

    if args.compare_json:
        ref = json.loads(Path(args.compare_json).read_text())
        out["reproduces_reference"] = {
            "reference": Path(args.compare_json).name,
            "p1": _compare(out["p1"], ref, "p1", ("auc", "ndcg@10")),
            "p2": _compare(out["p2"], ref, "p2", ("ndcg@10", "recall@10")),
        }
    out["timing_seconds"] = round(time.time() - t_start, 1)
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=_json_default))
    log.info("wrote %s (%.0fs)", args.out_json, time.time() - t_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
