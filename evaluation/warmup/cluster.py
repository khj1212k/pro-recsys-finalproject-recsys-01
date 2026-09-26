"""워밍업 2단계: 현재 기본값으로 클러스터링을 한 번 돌리고 evaluation/clustering/metrics.py 지표를 낸다.

    python -m evaluation.warmup.cluster --lookback-hours 72 --out <json> [--npz <path>]

- 클러스터링은 Stage5와 같은 코드 경로(NewsClusterer.cluster_news: DB 로드 → HDBSCAN →
  split_v2)를 쓴다. 파라미터는 Settings 기본값(HDBSCAN_MIN_CLUSTER_SIZE/MIN_SAMPLES).
- 지표는 두 층위로 낸다: (a) HDBSCAN 1차 라벨(DBCV·실루엣·안정성 ARI), (b) split_v2까지 거친
  최종 그룹(파이프라인이 실제로 생성에 넘기는 단위). (b)의 안정성은 행 번호를 첫 열로 붙인 입력으로
  split_v2까지 포함해 다시 돌려 잰다(split_v2가 제목을 쓰기 때문).
- 산출 JSON에는 클러스터 멤버십(raw_news_id만)과 언론사 수만 남긴다(본문·제목 없음).
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from evaluation.clustering.metrics import basic_stats, cosine_silhouette, dbcv, evaluate_run, stability_ari
from evaluation.warmup.common import distribution, environment, sha256_ids, write_json

logger = logging.getLogger(__name__)


def labels_from_groups(ids: Sequence[int], groups: Dict[int, List[int]]) -> np.ndarray:
    """{그룹 번호: [raw_news_id]} → ids 순서에 맞춘 라벨 배열(어느 그룹에도 없으면 -1)."""
    pos = {int(i): k for k, i in enumerate(ids)}
    labels = np.full(len(ids), -1, dtype=np.int64)
    for gidx, members in groups.items():
        for m in members:
            k = pos.get(int(m))
            if k is None:
                raise ValueError(f"그룹 {gidx}의 id {m}가 입력 ids에 없습니다")
            if labels[k] != -1:
                raise ValueError(f"id {m}가 두 그룹({labels[k]}, {gidx})에 속합니다")
            labels[k] = int(gidx)
    return labels


def hdbscan_fn(min_cluster_size: int, min_samples: int) -> Callable[[np.ndarray], np.ndarray]:
    """NewsClusterer.fit_predict와 같은 HDBSCAN 설정(euclidean, eom)."""
    import hdbscan

    def _fit(X: np.ndarray) -> np.ndarray:
        return hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            metric="euclidean",
            cluster_selection_method="eom",
        ).fit_predict(X)

    return _fit


def pipeline_fn(titles: Sequence[str], min_cluster_size: int, min_samples: int) -> Callable[[np.ndarray], np.ndarray]:
    """HDBSCAN + split_v2 전체를 재실행하는 cluster_fn. 입력 첫 열은 원래 행 번호(제목 조회용)."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    def _fit(X_aug: np.ndarray) -> np.ndarray:
        rows = X_aug[:, 0].astype(np.int64)
        X = np.ascontiguousarray(X_aug[:, 1:])
        clusterer = NewsClusterer(min_cluster_size=min_cluster_size, min_samples=min_samples)
        groups = clusterer.cluster_with_split({
            "embeddings": X,
            "titles": [titles[r] for r in rows],
            "ids": np.arange(len(rows)),
        })
        labels = np.full(len(rows), -1, dtype=np.int64)
        for g, (members, _) in enumerate(groups):
            labels[np.asarray(members, dtype=np.int64)] = g
        return labels

    return _fit


def membership(ids: Sequence[int], final_labels: np.ndarray, hdb_labels: np.ndarray,
               press_names: Sequence[str]) -> Dict[str, Any]:
    ids = [int(i) for i in ids]
    clusters = []
    for g in sorted({int(x) for x in final_labels.tolist() if x != -1}):
        idx = np.where(final_labels == g)[0]
        parents = sorted({int(hdb_labels[k]) for k in idx})
        clusters.append({
            "cluster_idx": g,
            "size": int(idx.size),
            "raw_news_ids": [ids[k] for k in idx],
            "n_press": len({press_names[k] for k in idx}),
            "hdbscan_parent_labels": parents,
        })
    noise_ids = [ids[k] for k in np.where(final_labels == -1)[0]]
    return {"clusters": clusters, "noise_ids": noise_ids}


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evaluation.warmup.cluster")
    parser.add_argument("--lookback-hours", type=int, default=72)
    parser.add_argument("--out", required=True)
    parser.add_argument("--npz", default=None, help="임베딩·라벨을 .npz로 저장(저장소 밖 경로 권장)")
    parser.add_argument("--stability-runs", type=int, default=20)
    parser.add_argument("--stability-frac", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=20260926)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from config.settings import Settings
    from core.clustering.hdbscan_clusterer import NewsClusterer
    from db.connection import get_connection, release_connection

    mcs = Settings.HDBSCAN_MIN_CLUSTER_SIZE
    ms = Settings.HDBSCAN_MIN_SAMPLES
    params = {"min_cluster_size": mcs, "min_samples": ms, "lookback_hours": args.lookback_hours,
              "metric": "euclidean (L2-normalized BGE-M3)", "cluster_selection_method": "eom", "split": "split_v2"}

    t0 = time.perf_counter()
    clusterer = NewsClusterer(min_cluster_size=mcs, min_samples=ms, lookback_hours=args.lookback_hours)
    groups = clusterer.cluster_news(min_cluster_size=mcs, min_samples=ms, lookback_hours=args.lookback_hours)
    cluster_s = time.perf_counter() - t0

    data = clusterer.data
    ids = [int(i) for i in data["ids"]]
    X = np.asarray(data["embeddings"], dtype=np.float64)
    titles = list(data["titles"])
    press = list(data["press_names"])
    hdb_labels = np.asarray(clusterer.labels_)
    final_labels = labels_from_groups(ids, groups)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(raw_news_crawled_at), MAX(raw_news_crawled_at) FROM news_raw WHERE raw_news_id = ANY(%s)",
                (ids,),
            )
            crawled_min, crawled_max = cur.fetchone()
            # 기본 lookback(24h)이었으면 몇 건이 들어갔을지 - lookback이 실제로 제약이었는지 확인용
            cur.execute(
                """
                SELECT COUNT(*) FROM news_raw
                WHERE embedding_result IS NOT NULL AND raw_news_content <> '' AND news_letter_id IS NULL
                  AND raw_news_crawled_at >= NOW() - (%s * INTERVAL '1 hour')
                """,
                (Settings.CLUSTER_LOOKBACK_HOURS,),
            )
            n_default_lookback = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM news_raw WHERE news_letter_id IS NOT NULL")
            n_already_assigned = cur.fetchone()[0]
    finally:
        release_connection(conn)

    norms = np.linalg.norm(X, axis=1)
    logger.info("stage-1 HDBSCAN 지표·안정성 계산 중 (runs=%s)", args.stability_runs)
    stage1 = evaluate_run(
        X, hdb_labels, cluster_fn=hdbscan_fn(mcs, ms),
        stability_n_runs=args.stability_runs, stability_frac=args.stability_frac, seed=args.seed,
        params={"min_cluster_size": mcs, "min_samples": ms},
    )
    logger.info("최종(split_v2 포함) 지표·안정성 계산 중")
    final_dbcv, final_dbcv_reason = dbcv(X, final_labels)
    X_aug = np.hstack([np.arange(len(ids), dtype=np.float64)[:, None], X])
    final_stab = stability_ari(X_aug, pipeline_fn(titles, mcs, ms), n_runs=args.stability_runs,
                               frac=args.stability_frac, seed=args.seed)
    final_sizes = [int(np.sum(final_labels == g)) for g in sorted({int(x) for x in final_labels if x != -1})]
    mem = membership(ids, final_labels, hdb_labels, press)

    report = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": environment(),
        "params": params,
        "input": {
            "n_articles": len(ids),
            "ids_sha256": sha256_ids(ids),
            "embeddings_sha256": stage1["input_hash"],
            "crawled_at_min": crawled_min,
            "crawled_at_max": crawled_max,
            "n_eligible_with_default_lookback": int(n_default_lookback),
            "default_lookback_hours": Settings.CLUSTER_LOOKBACK_HOURS,
            "n_already_assigned_to_newsletter": int(n_already_assigned),
            "l2_norm": distribution(norms.tolist()),
        },
        "timing_s": {"load_and_cluster": round(cluster_s, 3)},
        "stage1_hdbscan": stage1,
        "final_after_split_v2": {
            "basic_stats": basic_stats(final_labels),
            "sizes": distribution(final_sizes),
            "n_groups_below_2_articles": int(sum(1 for s in final_sizes if s < 2)),
            "n_groups_below_3_articles": int(sum(1 for s in final_sizes if s < 3)),
            "n_split_parents": int(sum(1 for c in mem["clusters"] if c["size"] < np.sum(hdb_labels == c["hdbscan_parent_labels"][0]))),
            "dbcv": final_dbcv,
            "dbcv_reason": final_dbcv_reason,
            "cosine_silhouette": cosine_silhouette(X, final_labels),
            "stability": final_stab,
            "press_per_cluster": distribution([c["n_press"] for c in mem["clusters"]]),
            "n_single_press_clusters": int(sum(1 for c in mem["clusters"] if c["n_press"] == 1)),
        },
        "membership": mem,
        "seed": args.seed,
    }
    write_json(args.out, report)
    if args.npz:
        np.savez_compressed(args.npz, ids=np.asarray(ids), X=X.astype(np.float32),
                            hdb_labels=hdb_labels, final_labels=final_labels)
    logger.info("wrote %s (clusters=%s, noise_ratio=%.3f)", args.out,
                report["final_after_split_v2"]["basic_stats"]["n_clusters"],
                report["final_after_split_v2"]["basic_stats"]["noise_ratio"])


if __name__ == "__main__":
    main()
