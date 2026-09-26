"""EB-NeRD 공개 벤치마크 평가 하네스 (P1 노출 재정렬, P2 전체 풀, ablation, MMR, 실시간 재생).

    EBNERD_ROOT=... .venv/bin/python -m evaluation.recsys.ebnerd.run_ebnerd \
        --dataset ebnerd_small --out-json reports/recsys/ebnerd_v1.json

프로토콜 요약 (자세한 근거는 reports/recsys/ebnerd_v1.md, docs/adr/0013):
- 학습: train split 행동 창 중 [시작+48h, 마지막 날) / early-stop: train 마지막 24h /
  평가: validation split 전체(고정 정답 창). 모든 피처는 요청 시각 t 이전 이벤트만.
- 여러 seed(모델/네거티브 샘플링/동점 처리) + 유저 단위 부트스트랩 95% CI.
EB-NeRD는 연구/비상업 라이선스라 이 스크립트의 입력·중간 산출물은 전부 로컬에만 둔다.
리포트(JSON)에는 집계 수치만 쓰고 기사 텍스트는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import platform
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from recsys_core import DAY, compute_features, source_flags

from ..metrics import (
    catalog_coverage,
    category_entropy,
    cluster_bootstrap,
    group_ids,
    intra_list_diversity,
    paired_bootstrap_diff,
    ranking_metrics,
    topk_items,
)
from .embedding_sanity import knn_category_accuracy
from .loaders import ebnerd_root
from .models import ABLATION, ALL_GROUPS, NEGATIVE_VARIANTS, baseline_scores, train
from .prepare import (
    impressions_in,
    load_bench,
    p1_task,
    p2_task,
    pool_negative_task,
    protocol_windows,
    random_negative_task,
    seen_mask,
)

log = logging.getLogger("ebnerd")
P1_KS = (5, 10)
P1_METRICS = ("auc", "mrr", "ndcg@5", "ndcg@10")
P2_SOURCE_KS = (50, 100, 200)
# P2에서는 학습한 모든 모델을 평가한다(P1 ablation이 네거티브 분포 차이와 섞이는지 P2에서 따로 보기 위해).
P2_MODELS = tuple(s.name for s in ABLATION + NEGATIVE_VARIANTS)
SPLIT_SEED = 20260925
SEEN_FILTER_METHODS = ("popularity_24h", "recency", "cosine_history", "team_binary", "ranker_v2",
                       "ranker_v2_poolneg", "ranker_v2_mixed")


def _git_sha() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "recsys_core", "evaluation"]).returncode
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def _iso(t: int) -> str:
    return str(np.datetime64(int(t), "s"))


class MetricBank:
    """방법별 노출 단위 지표를 seed마다 모았다가 seed 평균/CI/쌍체 차이를 계산한다."""

    def __init__(self, clusters: np.ndarray, n_boot: int, metrics=P1_METRICS):
        self.clusters = clusters
        self.n_boot = n_boot
        self.metrics = metrics
        self.runs: dict[str, list[dict[str, np.ndarray]]] = {}

    def add(self, name: str, per_group: dict[str, np.ndarray]):
        self.runs.setdefault(name, []).append({m: per_group[m] for m in self.metrics if m in per_group})

    def seed_mean(self, name: str) -> dict[str, np.ndarray]:
        runs = self.runs[name]
        return {m: np.nanmean(np.stack([r[m] for r in runs]), axis=0) if len(runs) > 1 else runs[0][m]
                for m in runs[0]}

    def summary(self, name: str) -> dict:
        runs = self.runs[name]
        out = {"n_seeds": len(runs)}
        avg = self.seed_mean(name)
        for m in runs[0]:
            per_seed = [float(np.nanmean(r[m])) for r in runs]
            ci = cluster_bootstrap(avg[m], self.clusters, n_boot=self.n_boot, seed=0)
            out[m] = {"mean": float(np.nanmean(avg[m])), "seed_means": per_seed,
                      "seed_std": float(np.std(per_seed, ddof=1)) if len(per_seed) > 1 else 0.0,
                      "ci95": [ci["lo"], ci["hi"]], "n": ci["n"], "n_users": ci["n_clusters"]}
        return out

    def diff(self, a: str, b: str) -> dict:
        ma, mb = self.seed_mean(a), self.seed_mean(b)
        out = {}
        for m in ma:
            if m in mb:
                ci = paired_bootstrap_diff(ma[m], mb[m], self.clusters, n_boot=self.n_boot, seed=1)
                out[m] = {"diff": ci["mean"], "ci95": [ci["lo"], ci["hi"]], "n": ci["n"]}
        return out


def _features(ctx, task, label: str) -> pd.DataFrame:
    t0 = time.time()
    f = compute_features(ctx, task.req, groups=ALL_GROUPS)
    log.info("features %s: %d requests, %d pairs, %.1fs", label, task.req.n, len(f), time.time() - t0)
    return f


def run_p1(bench, W, args, seeds, out) -> dict:
    tr, va = bench.imps["train"], bench.imps["validation"]
    rng = np.random.default_rng(SPLIT_SEED)
    idx = {"fit": impressions_in(tr, W["fit"]), "es": impressions_in(tr, W["es"]),
           "test": impressions_in(va, W["test"])}
    for k, cap in (("fit", args.max_fit), ("es", args.max_fit), ("test", args.max_test)):
        if cap and len(idx[k]) > cap:
            idx[k] = np.sort(rng.choice(idx[k], size=cap, replace=False))
    out["protocol"]["impressions"] = {k: int(len(v)) for k, v in idx.items()}

    tasks = {"fit": p1_task(bench, "train", idx["fit"]), "es": p1_task(bench, "train", idx["es"]),
             "test": p1_task(bench, "validation", idx["test"])}
    feats = {k: _features(bench.ctx["train" if k != "test" else "validation"], t, f"p1-{k}")
             for k, t in tasks.items()}
    test, ft = tasks["test"], feats["test"]
    ptr, labels = test.req.cand_ptr, test.labels
    bank = MetricBank(test.group_user, args.n_boot)

    # --- 이미 본 아이템(seen) 통계 ---
    seen = seen_mask(bench, "validation", test.req)
    g = group_ids(ptr)
    seen_pos_imp = np.bincount(g[labels & seen], minlength=test.req.n) > 0
    out["p1_seen"] = {
        "candidate_seen_fraction": float(seen.mean()),
        "impressions_with_seen_clicked_item": float(seen_pos_imp.mean()),
        "clicked_items_seen_fraction": float((labels & seen).sum() / labels.sum()),
    }

    scores_keep: dict[str, list[np.ndarray]] = {}

    def keep_scores(name: str, sc: np.ndarray):
        if name in SEEN_FILTER_METHODS:
            scores_keep.setdefault(name, []).append(sc)

    for seed in seeds:
        for name, sc in baseline_scores(ft, len(labels), seed).items():
            bank.add(name, ranking_metrics(sc, labels, ptr, ks=P1_KS, seed=seed))
            keep_scores(name, sc)

    models: dict[str, list] = {}
    rn_cache: dict[int, tuple] = {}
    for spec in ABLATION + NEGATIVE_VARIANTS:
        if args.only_models and spec.name not in args.only_models:
            continue
        if spec.data != "random_neg":
            rn_cache.clear()
        for seed in seeds:
            if spec.data == "random_neg":
                if seed not in rn_cache:
                    r = np.random.default_rng(seed)
                    tf = random_negative_task(bench, "train", idx["fit"], r)
                    te = random_negative_task(bench, "train", idx["es"], r)
                    rn_cache[seed] = ((tf, _features(bench.ctx["train"], tf, f"rn-fit-s{seed}")),
                                      (te, _features(bench.ctx["train"], te, f"rn-es-s{seed}")))
                fit, es = rn_cache[seed]
            elif spec.data in ("pool_neg", "mixed_neg"):
                r = np.random.default_rng(1000 + seed)
                mixed = spec.data == "mixed_neg"
                tf = pool_negative_task(bench, "train", idx["fit"], r, include_inview=mixed)
                te = pool_negative_task(bench, "train", idx["es"], r, include_inview=mixed)
                fit = (tf, _features(bench.ctx["train"], tf, f"{spec.data}-fit-s{seed}"))
                es = (te, _features(bench.ctx["train"], te, f"{spec.data}-es-s{seed}"))
            else:
                fit, es = (tasks["fit"], feats["fit"]), (tasks["es"], feats["es"])
            m = train(spec, fit, es, seed=seed, num_threads=args.threads)
            sc = m.predict(ft, test)
            bank.add(spec.name, ranking_metrics(sc, labels, ptr, ks=P1_KS, seed=seed))
            keep_scores(spec.name, sc)
            models.setdefault(spec.name, []).append(m)
            log.info("trained %s seed=%d best_it=%d es=%.4f %.0fs", spec.name, seed, m.best_iteration,
                     m.best_score, m.seconds)

    names = list(bank.runs)
    out["p1"] = {n: bank.summary(n) for n in names}
    out["p1_models"] = {n: [m.summary() for m in ms] for n, ms in models.items()}
    steps = [s.name for s in ABLATION if s.name in bank.runs]
    out["p1_ablation_diffs"] = {f"{b}-vs-{a}": bank.diff(b, a) for a, b in zip(steps, steps[1:])}
    ref = "ranker_v2" if "ranker_v2" in bank.runs else steps[-1] if steps else None
    if ref:
        variant_names = {s.name for s in NEGATIVE_VARIANTS}
        out["p1_vs_baselines"] = {f"{ref}-vs-{n}": bank.diff(ref, n) for n in names
                                  if n not in steps and n not in variant_names}
        for other in ["team_binary"] + [s.name for s in NEGATIVE_VARIANTS]:
            if other in bank.runs:
                out["p1_vs_baselines"][f"{ref}-vs-{other}"] = bank.diff(ref, other)

    # --- seen 필터 버전: 후보에서 이미 본 아이템을 뺀 뒤 재평가 ---
    keep = ~seen
    fptr = np.concatenate([[0], np.cumsum(np.bincount(g[keep], minlength=test.req.n))])
    fbank = MetricBank(test.group_user, args.n_boot)
    for name in [n for n in SEEN_FILTER_METHODS if n in scores_keep]:
        for seed, sc in zip(seeds, scores_keep[name]):
            fbank.add(name, ranking_metrics(sc[keep], labels[keep], fptr, ks=P1_KS, seed=seed))
    out["p1_seen_filtered"] = {n: fbank.summary(n) for n in fbank.runs}
    return {"models": models, "tasks": tasks, "feats": feats, "idx": idx}


def _novelty(feats: pd.DataFrame, task, lists_pos: np.ndarray) -> np.ndarray:
    """요청 시점 48h 인기도 기반 self-information(-log2 p) 평균. p는 풀 안에서 +1 스무딩."""
    pop = feats["pop_clicks_48h"].to_numpy(np.float64) + 1.0
    denom = np.bincount(task.req.pair_req, weights=pop, minlength=task.req.n)
    info = -np.log2(pop / denom[task.req.pair_req])
    valid = lists_pos >= 0
    vals = np.where(valid, info[np.where(valid, lists_pos, 0)], 0.0)
    with np.errstate(invalid="ignore"):
        return np.where(valid.any(axis=1), vals.sum(axis=1) / valid.sum(axis=1), np.nan)


def _list_metrics(bench, task, feats, lists_pos: np.ndarray, n_boot: int) -> dict:
    """top-k 목록(후보 쌍 위치 인덱스) 기반 다양성/커버리지/신규성."""
    items = np.where(lists_pos >= 0, task.req.cand_item[np.where(lists_pos >= 0, lists_pos, 0)], -1)
    cat = bench.catalog
    ild = intra_list_diversity(items, cat.emb)
    ent = category_entropy(items, cat.category, cat.n_categories)
    nov = _novelty(feats, task, lists_pos)
    res = {"coverage@10": catalog_coverage(items, task.req.cand_item)}
    for k, v in (("ild@10", ild), ("category_entropy@10", ent), ("novelty@10", nov)):
        ci = cluster_bootstrap(v, task.group_user, n_boot=n_boot, seed=0)
        res[k] = {"mean": ci["mean"], "ci95": [ci["lo"], ci["hi"]]}
    return res


def _positions_topk(scores, ptr, k, seed) -> np.ndarray:
    return topk_items(scores, np.arange(len(scores)), ptr, k, seed=seed)


def run_p2(bench, W, args, seeds, p1, out):
    idx_all = p1["idx"]["test"]
    rng = np.random.default_rng(SPLIT_SEED + 2)
    idx = np.sort(rng.choice(idx_all, size=min(args.p2_sample, len(idx_all)), replace=False))
    t0 = time.time()
    task = p2_task(bench, "validation", idx, window_h=48, exclude_seen=True)
    feats = _features(bench.ctx["validation"], task, "p2")
    ptr, labels, npos = task.req.cand_ptr, task.labels, task.n_pos_total
    pool = task.req.n_candidates
    in_pool = np.bincount(task.req.pair_req[labels], minlength=task.req.n)
    info = {
        "n_requests": int(task.req.n), "n_users": int(len(np.unique(task.group_user))),
        "pool_size": {"mean": float(pool.mean()), "median": float(np.median(pool)), "min": int(pool.min()),
                      "max": int(pool.max())},
        "clicked_items_total": int(npos.sum()),
        "clicked_published_within_48h_fraction": float(task.extra["pos_in_pool_before_seen_filter"].sum() / npos.sum()),
        "positives_removed_as_seen": task.extra.get("positives_removed_as_seen"),
        "pool_seen_fraction_removed": task.extra.get("seen_fraction_of_pool"),
        "recall_upper_bound_after_filter": float(in_pool.sum() / npos.sum()),
        "seconds_build": round(time.time() - t0, 1),
    }
    out["p2_info"] = info

    # --- 후보 출처별 recall@K (출처 = 요청 시점 신호 하나로 전체 풀 정렬, recsys_core.source_flags) ---
    sources = {"popularity_6h": feats["pop_clicks_6h"].to_numpy(np.float64),
               "popularity_24h": feats["pop_clicks_24h"].to_numpy(np.float64),
               "recency": -feats["hours_since_pub"].to_numpy(np.float64),
               "cosine_history": feats["hist_cos"].to_numpy(np.float64)}
    g = task.req.pair_req
    src_rec: dict[str, dict] = {}
    union_masks: dict[int, np.ndarray] = {}
    for k in P2_SOURCE_KS:
        flags = source_flags(sources, ptr, k=k, seed=0)
        for name in sources:
            hit = labels & (flags[f"src_{name}"].to_numpy() > 0)
            src_rec.setdefault(name, {})[f"recall@{k}"] = float(np.nanmean(np.bincount(g[hit], minlength=task.req.n) / npos))
        in_union = flags["src_count"].to_numpy() > 0
        union_masks[k] = in_union
        rec = np.bincount(g[labels & in_union], minlength=task.req.n) / npos
        src_rec.setdefault("union", {})[f"recall@{k}"] = float(np.nanmean(rec))
        src_rec["union"][f"mean_union_size@{k}"] = float(np.bincount(g[in_union], minlength=task.req.n).mean())
    out["p2_sources"] = src_rec

    # --- 전체 풀 랭킹: 베이스라인 + 팀 방식 + ranker v2 ---
    bank = MetricBank(task.group_user, args.n_boot, metrics=("ndcg@10", "recall@10", "mrr"))
    lists: dict[str, np.ndarray] = {}
    chosen = out.get("p2_selection", {}).get("chosen")
    chosen_scores: list[np.ndarray] = []

    # 풀이 요청당 수백 개라 (방법 x seed) 점수를 전부 들고 있으면 수 GB가 된다: 지표를 바로 계산하고
    # 목록(seed 0)과 선택 모델 점수만 남긴다.
    def _add(name: str, seed: int, sc: np.ndarray):
        bank.add(name, ranking_metrics(sc, labels, ptr, ks=(10,), n_pos_total=npos, seed=seed, with_auc=False))
        if seed == seeds[0]:
            lists[name] = _positions_topk(sc, ptr, 10, seed=seeds[0])
        if name == chosen:
            chosen_scores.append(sc)

    for seed in seeds:
        for name, sc in baseline_scores(feats, len(labels), seed).items():
            _add(name, seed, sc)
    for name in P2_MODELS:
        for m in p1["models"].get(name, []):
            _add(name, m.seed, m.predict(feats, task))
    out["p2"] = {}
    for name in bank.runs:
        out["p2"][name] = bank.summary(name)
        out["p2"][name].update(_list_metrics(bench, task, feats, lists[name], args.n_boot))
    if chosen in bank.runs:
        out["p2_vs"] = {f"{chosen}-vs-{n}": bank.diff(chosen, n) for n in bank.runs if n != chosen}
    steps = [s.name for s in ABLATION if s.name in bank.runs]
    out["p2_ablation_diffs"] = {f"{b}-vs-{a}": bank.diff(b, a) for a, b in zip(steps, steps[1:])}
    if chosen in bank.runs:
        out["p2_two_stage"] = _two_stage(task, chosen_scores, seeds, union_masks, args.n_boot)
        out["p2_mmr"] = run_mmr(bench, task, feats, chosen_scores[0], args, chosen)


def _two_stage(task, score_list, seeds, union_masks, n_boot) -> dict:
    """후보 생성(출처별 상위 k 합집합) -> 랭커 2단계. 합집합 밖 후보는 순위 맨 뒤로 보낸다."""
    ptr, labels, npos = task.req.cand_ptr, task.labels, task.n_pos_total
    res = {}
    for k, mask in union_masks.items():
        bank = MetricBank(task.group_user, n_boot, metrics=("ndcg@10", "recall@10"))
        for seed, sc in zip(seeds, score_list):
            staged = np.where(mask, sc, -np.inf)
            bank.add("m", ranking_metrics(staged, labels, ptr, ks=(10,), n_pos_total=npos, seed=seed, with_auc=False))
        res[f"union@{k}"] = bank.summary("m")
    return res


def select_p2_model(bench, W, args, seeds, p1, out):
    """P2(전체 풀)용 모델을 validation을 보지 않고 고른다: train 마지막 날(early-stop 구간)
    노출 표본으로 P2 과제를 만들어 seed 평균 nDCG@10이 가장 높은 모델을 선택."""
    rng = np.random.default_rng(SPLIT_SEED + 5)
    es_idx = p1["idx"]["es"]
    idx = np.sort(rng.choice(es_idx, size=min(args.p2_select_sample, len(es_idx)), replace=False))
    task = p2_task(bench, "train", idx, window_h=48, exclude_seen=True)
    feats = _features(bench.ctx["train"], task, "p2-select")
    table = {}
    for name in P2_MODELS:
        ms = p1["models"].get(name, [])
        if not ms:
            continue
        vals = [float(np.nanmean(ranking_metrics(m.predict(feats, task), task.labels, task.req.cand_ptr, ks=(10,),
                                                 n_pos_total=task.n_pos_total, seed=m.seed,
                                                 with_auc=False)["ndcg@10"])) for m in ms]
        table[name] = {"ndcg@10_seed_mean": float(np.mean(vals)), "seed_values": vals}
    chosen = max(table, key=lambda n: table[n]["ndcg@10_seed_mean"]) if table else None
    out["p2_selection"] = {"slice": "train 마지막 24h(early-stop 구간) 노출 표본", "n_requests": int(task.req.n),
                           "table": table, "chosen": chosen}
    log.info("p2 selection: %s", {k: round(v["ndcg@10_seed_mean"], 4) for k, v in table.items()})


def run_mmr(bench, task, feats, scores, args, model_name: str) -> dict:
    """ai_workspace/recommend_engine의 MMRReranker(성승우 작성)를 그대로 써서 lambda 스윕."""
    from src.core.reranker import MMRReranker

    ptr, labels, npos = task.req.cand_ptr, task.labels, task.n_pos_total
    cat = bench.catalog
    rng = np.random.default_rng(SPLIT_SEED + 3)
    sub = np.arange(task.req.n)
    if args.mmr_sample and task.req.n > args.mmr_sample:
        sub = np.sort(rng.choice(task.req.n, size=args.mmr_sample, replace=False))
    res = {"n_requests": int(len(sub)), "model": f"{model_name} seed0", "top_k": 10, "pool_multiplier": 4,
           "sweep": {}}
    for lam in args.mmr_lambdas:
        rr = MMRReranker(lambda_param=lam, pool_multiplier=4)
        lists = np.full((len(sub), 10), -1, dtype=np.int64)
        t0 = time.time()
        for row, r in enumerate(sub):
            a, b = ptr[r], ptr[r + 1]
            if b == a:
                continue
            picked = rr.rerank(scores[a:b], cat.emb[task.req.cand_item[a:b]], top_k=10)
            lists[row, :len(picked)] = [a + int(i) for i, _ in picked]
        hit = np.zeros(len(sub))
        dcg = np.zeros(len(sub))
        for j in range(10):
            pos = lists[:, j]
            ok = pos >= 0
            rel = np.zeros(len(sub))
            rel[ok] = labels[pos[ok]]
            dcg += rel / np.log2(j + 2)
            hit += rel
        n_rel = npos[sub]
        idcg = np.array([np.sum(1 / np.log2(np.arange(min(int(n), 10)) + 2)) for n in n_rel])
        ndcg = np.where(n_rel > 0, dcg / np.where(idcg > 0, idcg, 1), np.nan)
        sub_task = _sub_task(task, sub)
        remap = _remap_positions(task, sub)
        metrics = _list_metrics(bench, sub_task, feats.iloc[remap["pairs"]].reset_index(drop=True),
                                np.where(lists >= 0, remap["new_pos"][np.where(lists >= 0, lists, 0)], -1),
                                args.n_boot)
        ci = cluster_bootstrap(ndcg, task.group_user[sub], n_boot=args.n_boot, seed=0)
        metrics["ndcg@10"] = {"mean": ci["mean"], "ci95": [ci["lo"], ci["hi"]]}
        metrics["recall@10"] = float(np.nanmean(hit / np.where(n_rel > 0, n_rel, np.nan)))
        metrics["seconds"] = round(time.time() - t0, 1)
        res["sweep"][f"{lam:.1f}"] = metrics
        log.info("mmr lambda=%.1f ndcg@10=%.4f ild=%.4f", lam, metrics["ndcg@10"]["mean"], metrics["ild@10"]["mean"])
    return res


def _remap_positions(task, sub):
    ptr = task.req.cand_ptr
    counts = ptr[sub + 1] - ptr[sub]
    from recsys_core import expand_ranges
    _, pairs = expand_ranges(ptr[sub], ptr[sub + 1])
    new_pos = np.full(len(task.labels), -1, dtype=np.int64)
    new_pos[pairs] = np.arange(len(pairs))
    return {"pairs": pairs, "new_pos": new_pos, "counts": counts}


def _sub_task(task, sub):
    from recsys_core import Requests
    from .prepare import RankTask
    remap = _remap_positions(task, sub)
    req = task.req
    new_req = Requests(user=req.user[sub], time=req.time[sub],
                       cand_ptr=np.concatenate([[0], np.cumsum(remap["counts"])]),
                       cand_item=req.cand_item[remap["pairs"]], session=req.session[sub])
    return RankTask(req=new_req, labels=task.labels[remap["pairs"]], group_user=task.group_user[sub],
                    imp_index=task.imp_index[sub], n_pos_total=None if task.n_pos_total is None else task.n_pos_total[sub])


def run_replay(bench, W, args, seeds, p1, out):
    """실시간 재생: 같은 날 앞선 이벤트가 1건 이상인 노출에서 유저 프로필 신선도만 바꿔 비교.
    (A) 일 배치: 그날 00:00 이전까지 / (B) 요청 시각 t까지 / (C) 행동 창 이전 history만.
    아이템 쪽 피처(인기도·신선도)는 세 경우 모두 t 기준으로 고정한다."""
    va = bench.imps["validation"]
    ctx = bench.ctx["validation"]
    idx_test = p1["idx"]["test"]
    t = va.time[idx_test]
    day_start = t // DAY * DAY
    window_start = W["test"][0]
    # 첫날(05-25)은 00:00 < 행동 창 시작(07:00)이라 A가 C보다 정보가 적어지므로 제외한다.
    eligible = day_start >= window_start
    same_day = ctx.user_log.count(va.user_id[idx_test], day_start, t) >= 1
    idx = idx_test[eligible & same_day]
    n_eligible = len(idx)
    if args.replay_max and len(idx) > args.replay_max:
        idx = np.sort(np.random.default_rng(SPLIT_SEED + 4).choice(idx, size=args.replay_max, replace=False))
    tt = va.time[idx]
    cut = {"A_daily_batch": tt // DAY * DAY, "B_up_to_t": tt, "C_history_only": np.full(len(idx), window_start)}
    bank = MetricBank(va.user_id[idx], args.n_boot)
    daily_models = _train_daily_batch_ranker(bench, p1, seeds, args) if p1["models"].get("ranker_v2") else []
    for name, c in cut.items():
        task = p1_task(bench, "validation", idx, profile_cutoff=c)
        feats = _features(ctx, task, f"replay-{name}")
        ptr, labels = task.req.cand_ptr, task.labels
        for seed in seeds:
            bank.add(f"cosine_history|{name}",
                     ranking_metrics(feats["hist_cos"].to_numpy(), labels, ptr, ks=P1_KS, seed=seed))
        for m in p1["models"].get("ranker_v2", []):
            bank.add(f"ranker_v2|{name}", ranking_metrics(m.predict(feats, task), labels, ptr, ks=P1_KS, seed=m.seed))
        if name == "A_daily_batch":
            for m in daily_models:
                bank.add("ranker_v2_daily_trained|A_daily_batch",
                         ranking_metrics(m.predict(feats, task), labels, ptr, ks=P1_KS, seed=m.seed))
    out["replay"] = {
        "definition": "validation 둘째 날(00:00 >= 행동 창 시작)부터, 같은 날 t 이전 유저 로그 이벤트 1건 이상인 노출",
        "n_impressions": int(len(idx)), "n_users": int(len(np.unique(va.user_id[idx]))),
        "n_eligible": int(n_eligible), "share_of_test_impressions": float(n_eligible / len(idx_test)),
        "results": {n: bank.summary(n) for n in bank.runs},
        "diffs": {},
    }
    for model in ("cosine_history", "ranker_v2"):
        if f"{model}|B_up_to_t" not in bank.runs:
            continue
        for a, b in (("B_up_to_t", "A_daily_batch"), ("B_up_to_t", "C_history_only"), ("A_daily_batch", "C_history_only")):
            out["replay"]["diffs"][f"{model}: {a} - {b}"] = bank.diff(f"{model}|{a}", f"{model}|{b}")
    if daily_models:
        # 학습·서빙 모두 일 배치 피처인 시스템 vs 학습·서빙 모두 실시간 피처인 시스템(짝 맞춘 비교).
        out["replay"]["diffs"]["ranker_v2 realtime(B-trained,B-served) - daily(A-trained,A-served)"] = bank.diff(
            "ranker_v2|B_up_to_t", "ranker_v2_daily_trained|A_daily_batch")
        out["replay"]["daily_trained_models"] = [m.summary() for m in daily_models]


def _train_daily_batch_ranker(bench, p1, seeds, args) -> list:
    """ranker v2를 일 배치 프로필 피처(유저 상태 = 그날 00:00 이전)로 학습한다. 실시간 피처로 학습한
    모델에 일 배치 피처를 넣으면 학습/서빙 분포가 어긋나므로, 일 배치 시스템의 공정한 대표로 쓴다."""
    spec = next(s for s in ABLATION if s.name == "ranker_v2")
    tr = bench.imps["train"]
    sets = {}
    for k in ("fit", "es"):
        idx = p1["idx"][k]
        task = p1_task(bench, "train", idx, profile_cutoff=tr.time[idx] // DAY * DAY)
        sets[k] = (task, _features(bench.ctx["train"], task, f"daily-{k}"))
    models = []
    for seed in seeds:
        m = train(spec, sets["fit"], sets["es"], seed=seed, num_threads=args.threads)
        log.info("trained ranker_v2_daily seed=%d best_it=%d es=%.4f", seed, m.best_iteration, m.best_score)
        models.append(m)
    return models


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="ebnerd_small")
    ap.add_argument("--root", default=None)
    ap.add_argument("--emb-dir", default=None)
    ap.add_argument("--fake-dim", type=int, default=None, help="개발용 무작위 임베딩(결과 해석 금지)")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--max-fit", type=int, default=None)
    ap.add_argument("--max-test", type=int, default=None)
    ap.add_argument("--p2-sample", type=int, default=20000)
    ap.add_argument("--p2-select-sample", type=int, default=5000)
    ap.add_argument("--mmr-sample", type=int, default=5000)
    ap.add_argument("--mmr-lambdas", type=float, nargs="+", default=[0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    ap.add_argument("--replay-max", type=int, default=None)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--only-models", nargs="*", default=None)
    ap.add_argument("--skip", nargs="*", default=[], choices=["p2", "replay"])
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)

    root = Path(args.root) if args.root else ebnerd_root()
    dataset_dir = root / args.dataset
    emb_dir = Path(args.emb_dir) if args.emb_dir else root / "derived" / args.dataset
    t_start = time.time()
    bench = load_bench(dataset_dir, emb_dir=None if args.fake_dim else emb_dir, fake_dim=args.fake_dim)
    W = protocol_windows(bench)
    log.info("bench loaded %.1fs windows=%s", time.time() - t_start, {k: (_iso(a), _iso(b)) for k, (a, b) in W.items()})
    out = {
        "meta": {
            "git_sha": _git_sha(), "dataset": args.dataset, "seeds": args.seeds, "split_seed": SPLIT_SEED,
            "n_boot": args.n_boot, "python": platform.python_version(),
            "machine": f"{platform.machine()} {platform.system()}", "argv": sys.argv[1:] if argv is None else argv,
        },
        "data": {"catalog": bench.catalog_info, **bench.data_info},
        "protocol": {"windows": {k: [_iso(a), _iso(b)] for k, (a, b) in W.items()},
                     "features": bench.ctx["train"].config.__dict__ | {"pop_windows_h": list(bench.ctx["train"].config.pop_windows_h)},
                     "ablation": [s.__dict__ for s in ABLATION]},
        "timing": {},
    }
    if not args.fake_dim:
        t0 = time.time()
        san = knn_category_accuracy(bench.catalog.emb, bench.catalog.category, k=10)
        san.pop("per_item_correct")
        out["embedding_sanity"] = {"category_knn_loo": san, "seconds": round(time.time() - t0, 1)}
        log.info("embedding sanity: %s", out["embedding_sanity"])
    t0 = time.time()
    p1 = run_p1(bench, W, args, args.seeds, out)
    out["timing"]["p1_seconds"] = round(time.time() - t0, 1)
    if "p2" not in args.skip:
        t0 = time.time()
        select_p2_model(bench, W, args, args.seeds, p1, out)
        run_p2(bench, W, args, args.seeds, p1, out)
        out["timing"]["p2_seconds"] = round(time.time() - t0, 1)
    if "replay" not in args.skip:
        t0 = time.time()
        run_replay(bench, W, args, args.seeds, p1, out)
        out["timing"]["replay_seconds"] = round(time.time() - t0, 1)
    out["timing"]["total_seconds"] = round(time.time() - t_start, 1)
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(out, indent=2, ensure_ascii=False, default=_json_default))
    log.info("wrote %s (%.0fs)", args.out_json, time.time() - t_start)
    return 0


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, tuple):
        return list(o)
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
