"""train/serve parity 게이트 (ADR 0033, 설계 문서 §3.3 · E11).

질문: 오프라인 하네스에서 검증한 랭커가 요청 시점에 **같은 피처**를 보는가. 서빙이 남긴 로그(요청 로그,
칸 로그의 피처·shadow 점수)를 같은 DB의 이벤트 로그에서 하네스 방식으로 다시 계산한 값과 비교한다.

게이트 네 항목(임계는 THRESHOLDS - 설계 문서의 값):
1. features: 화면에 나간 칸마다, 칸 로그에 남은 서빙 어댑터 피처와 이벤트 로그에서 다시 계산한 피처의
   전 열 max|Δ| < 1e-6. NaN의 위치도 같아야 한다.
2. scores: 같은 칸들(= 같은 후보 집합)에서, 칸 로그에 남은 shadow 점수(서빙 피처로 낸 점수)와 다시 계산한
   피처로 같은 모델이 낸 점수의 순서가 같다(요청마다 Kendall τ = 1, 동점 쌍 제외).
3. end_to_end: 요청마다 두 목록의 상위 20개 겹침의 평균 ≥ 0.9.
   - 서빙 쪽: 요청 로그에 남은 후보 집합 E(서빙의 후보 생성기 + 제외) 위에서 모델 점수 상위 20개.
   - 하네스 쪽: 하네스의 서빙 후보 구성(candidate_config.SERVING: 출처별 상위 k를 라운드로빈으로 합쳐 cap)으로
     고른 후보 위에서 모델 점수 상위 20개. 풀 = 신선도 창 안에서 화면에 내보낼 수 있고 아직 클릭하지 않은 것.
   랭커의 순위만 비교한다. MMR과 탐색 칸은 서빙에만 있는 뒤 단계라 넣지 않는다. 1·2가 같은 후보에서의
   동일성이라면 이 항목은 **후보 생성기가 갈리는 만큼**을 잰다.
4. candidate_config: 서빙이 지금 도는 후보 구성(RecsysConfig.candidate_spec)과 하네스의 서빙 구성이 같은 값.

"겹침 0.9" 하나만으로는 게이트가 되지 않는다: 피처가 어긋나도 상위 목록은 대체로 겹친다. 그래서 1·2가 먼저다.

DB에서 읽는 부분(load_from_db)과 계산(run_gate)을 나눴다. 계산은 numpy 배열만 받으므로 DB 없이 테스트한다.

    python -m evaluation.recsys.serving_parity --database-url postgresql://... \\
        --model-name ranker --model-version v1 --out reports/recsys/parity_v1.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional, Sequence

import numpy as np

from recsys_core import CandidateSpec
from recsys_core.profile import NO_CATEGORY
from recsys_core.serving import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, SCHEMA_HASH

from .service_logs import LogBench, ServiceLogs, epoch_us

THRESHOLDS = {
    "features_max_abs_diff": 1e-6,   # 미만
    "scores_min_kendall_tau": 1.0,   # 이상
    "end_to_end_mean_overlap": 0.9,  # 이상
}
TOP_K = 20
PredictFn = Callable[[np.ndarray], np.ndarray]


@dataclass
class LoggedRequest:
    """서빙이 남긴 한 요청: 요청 로그 한 행 + 그 요청의 칸 로그 행들(위치순)."""
    request_id: str
    user_id: int
    as_of: datetime                  # recommendation_request_log.features_as_of
    candidate_ids: List[int]         # recommendation_request_log.candidate_ids (E)
    slot_ids: List[int]              # 화면에 나간 뉴스레터
    slot_features: np.ndarray        # (칸 수, 22) - 칸 로그 features를 푼 값
    slot_shadow: Optional[np.ndarray] = None  # (칸 수,) - 그 모델 버전의 shadow 점수. 없으면 None
    meta: Dict[str, object] = field(default_factory=dict)


def kendall_tau_without_ties(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """두 점수의 순서 일치도. 어느 한쪽에서라도 동점인 쌍은 세지 않는다. 비교할 쌍이 없으면 None."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    da = np.sign(a[:, None] - a[None, :])
    db = np.sign(b[:, None] - b[None, :])
    upper = np.triu(np.ones((len(a), len(a)), dtype=bool), k=1)
    comparable = upper & (da != 0) & (db != 0)
    n = int(comparable.sum())
    if n == 0:
        return None
    return float((da[comparable] * db[comparable]).sum() / n)


def top_k(ids: Sequence[int], scores: np.ndarray, k: int = TOP_K) -> List[int]:
    """점수 내림차순 상위 k. 동점은 ID 오름차순(두 쪽이 같은 규칙으로 깨야 겹침이 동점에 흔들리지 않는다)."""
    ids = np.asarray(ids, dtype=np.int64)
    order = np.lexsort((ids, -np.asarray(scores, dtype=np.float64)))
    return ids[order[:k]].tolist()


# ---------------------------------------------------------------------------- 항목별 계산
def feature_parity(bench: LogBench, requests: Sequence[LoggedRequest]) -> dict:
    worst = np.zeros(len(FEATURE_NAMES))
    nan_mismatch = n_slots = 0
    for r in requests:
        offline = bench.request_features(r.user_id, epoch_us(r.as_of), r.slot_ids)
        serving = np.asarray(r.slot_features, dtype=np.float32)
        if serving.shape != offline.shape:
            raise ValueError(f"{r.request_id}: 칸 로그의 피처 {serving.shape} != 다시 계산한 피처 {offline.shape}")
        both = np.isnan(serving) & np.isnan(offline)
        nan_mismatch += int((np.isnan(serving) != np.isnan(offline)).sum())
        diff = np.abs(serving.astype(np.float64) - offline.astype(np.float64))
        diff[both] = 0.0
        diff[np.isnan(diff)] = np.inf  # 한쪽만 NaN
        worst = np.maximum(worst, diff.max(axis=0, initial=0.0))
        n_slots += len(r.slot_ids)
    max_diff = float(worst.max()) if n_slots else None
    return {
        "requests": len(requests),
        "slots": n_slots,
        "columns": len(FEATURE_NAMES),
        "max_abs_diff": max_diff,
        "per_column_max_abs_diff": {n: float(w) for n, w in zip(FEATURE_NAMES, worst)},
        "nan_position_mismatches": nan_mismatch,
        "threshold": THRESHOLDS["features_max_abs_diff"],
        "pass": bool(n_slots and nan_mismatch == 0 and max_diff < THRESHOLDS["features_max_abs_diff"]),
    }


def score_parity(bench: LogBench, requests: Sequence[LoggedRequest], predict: PredictFn) -> dict:
    taus, no_pairs, max_score_diff, scored = [], 0, 0.0, 0
    for r in requests:
        if r.slot_shadow is None:
            continue
        scored += 1
        offline = np.asarray(predict(bench.request_features(r.user_id, epoch_us(r.as_of), r.slot_ids)), dtype=np.float64)
        logged = np.asarray(r.slot_shadow, dtype=np.float64)
        max_score_diff = max(max_score_diff, float(np.max(np.abs(offline - logged))))
        tau = kendall_tau_without_ties(logged, offline)
        if tau is None:
            no_pairs += 1
        else:
            taus.append(tau)
    return {
        "requests_with_shadow_scores": scored,
        "requests_without_shadow_scores": len(requests) - scored,
        "requests_without_comparable_pairs": no_pairs,
        "min_kendall_tau": min(taus) if taus else None,
        "requests_with_tau_below_1": sum(t < 1.0 for t in taus),
        "max_abs_score_diff": max_score_diff if scored else None,
        "threshold": THRESHOLDS["scores_min_kendall_tau"],
        "pass": bool(taus and min(taus) >= THRESHOLDS["scores_min_kendall_tau"]),
    }


def harness_candidates(bench: LogBench, user_id: int, now_us: int, config, seed: int = 0) -> tuple[List[int], dict]:
    """하네스의 서빙 후보 구성으로 고른 후보(뉴스레터 ID)와 그 요청의 피처 열.

    풀 = [요청 초 - 창, 요청 초] 안에 만들어지고, 화면에 내보낼 수 있고(카테고리 있음), 이 유저가 아직 클릭하지
    않은 뉴스레터. 그 위에서 candidate_config.union_mask가 출처별 상위 k를 라운드로빈으로 합쳐 cap에서 자른다."""
    import pandas as pd

    from .ebnerd.candidate_config import union_mask

    cat = bench.catalog
    t_req = int(now_us) // 1_000_000 + 1
    _, seen_rows = bench.user_events(int(user_id), int(now_us))
    in_window = (cat.pub_time >= t_req - int(config.window_h * 3600)) & (cat.pub_time <= t_req)
    pool = np.flatnonzero(in_window & (cat.category != NO_CATEGORY) & ~np.isin(np.arange(len(cat)), seen_rows))
    pool_ids = cat.ids[pool].tolist()
    if not pool_ids:
        return [], {}
    cols = bench.request_columns(user_id, now_us, pool_ids)
    mask, _ = union_mask(config, pd.DataFrame(cols), np.array([0, len(pool_ids)]), seed=seed)
    keep = np.flatnonzero(mask)
    return [pool_ids[i] for i in keep], {name: values[keep] for name, values in cols.items()}


def end_to_end_overlap(bench: LogBench, requests: Sequence[LoggedRequest], predict: PredictFn, config,
                       k: int = TOP_K, seed: int = 0) -> dict:
    overlaps, n_serving, n_harness, jaccard = [], [], [], []
    for r in requests:
        if not r.candidate_ids:
            continue
        now_us = epoch_us(r.as_of)
        serving_scores = predict(bench.request_features(r.user_id, now_us, r.candidate_ids))
        serving_top = top_k(r.candidate_ids, serving_scores, k)
        harness_ids, _ = harness_candidates(bench, r.user_id, now_us, config, seed=seed)
        if not harness_ids:
            continue
        harness_top = top_k(harness_ids, predict(bench.request_features(r.user_id, now_us, harness_ids)), k)
        denom = min(k, len(serving_top), len(harness_top))
        overlaps.append(len(set(serving_top) & set(harness_top)) / denom)
        a, b = set(r.candidate_ids), set(harness_ids)
        jaccard.append(len(a & b) / len(a | b))
        n_serving.append(len(a))
        n_harness.append(len(b))
    mean = float(np.mean(overlaps)) if overlaps else None
    return {
        "k": k,
        "requests": len(overlaps),
        "mean_overlap": mean,
        "min_overlap": float(np.min(overlaps)) if overlaps else None,
        "p10_overlap": float(np.percentile(overlaps, 10)) if overlaps else None,
        "requests_with_full_overlap": int(sum(o == 1.0 for o in overlaps)),
        "mean_serving_candidates": float(np.mean(n_serving)) if n_serving else None,
        "mean_harness_candidates": float(np.mean(n_harness)) if n_harness else None,
        "mean_candidate_set_jaccard": float(np.mean(jaccard)) if jaccard else None,
        "threshold": THRESHOLDS["end_to_end_mean_overlap"],
        "pass": bool(overlaps and mean >= THRESHOLDS["end_to_end_mean_overlap"]),
    }


def candidate_config_equality(serving_spec: CandidateSpec, harness_spec: CandidateSpec) -> dict:
    return {
        "serving": serving_spec.as_dict(),
        "harness": harness_spec.as_dict(),
        "equal": serving_spec == harness_spec,
        "pass": serving_spec == harness_spec,
    }


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def run_gate(logs: ServiceLogs, requests: Sequence[LoggedRequest], predict: PredictFn,
             serving_spec: CandidateSpec, meta: Optional[Mapping[str, object]] = None, seed: int = 0) -> dict:
    """게이트 네 항목을 계산해 리포트(dict)로 돌려준다. 전체 통과 = 네 항목 모두 통과."""
    from .ebnerd.candidate_config import SERVING, spec_of

    bench = LogBench(logs)
    report = {
        "meta": {
            "report": "serving parity gate v1 (ADR 0033)",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_sha": git_sha(),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_schema_hash": SCHEMA_HASH,
            "catalog_items": int(len(bench.catalog)),
            "click_events": int(len(logs.click_user)),
            "impression_rows": int(len(logs.inview_item)),
            "requests": len(requests),
            "users": len({r.user_id for r in requests}),
            "thresholds": dict(THRESHOLDS),
            **dict(meta or {}),
        },
        "features": feature_parity(bench, requests),
        "scores": score_parity(bench, requests, predict),
        "end_to_end": end_to_end_overlap(bench, requests, predict, SERVING, seed=seed),
        "candidate_config": candidate_config_equality(serving_spec, spec_of(SERVING)),
    }
    report["pass"] = all(report[k]["pass"] for k in ("features", "scores", "end_to_end", "candidate_config"))
    return report


def write_report(report: dict, path) -> None:
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------- DB에서 읽기
def _vector(buf) -> np.ndarray:
    return np.frombuffer(bytes(buf), dtype=">f4", offset=4).astype(np.float32)


def load_from_db(conn, model_version: Optional[str] = None, user_ids: Optional[Sequence[int]] = None,
                 since: Optional[datetime] = None) -> tuple[ServiceLogs, List[LoggedRequest]]:
    """psycopg2 연결에서 이벤트 로그 전체와, 어댑터 피처(스키마 2)가 남은 요청들을 읽는다.

    model_version("lgbm:<이름>@<버전>")을 주면 칸 로그의 그 버전 shadow 점수를 함께 읽는다. user_ids / since로
    비교할 요청을 좁힐 수 있다(이벤트 로그는 좁히지 않는다 - 인기도는 모든 사용자의 것이다)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.news_letter_id, vector_send(n.news_letter_embedding), n.news_letter_created_at::timestamptz, "
            "       COALESCE((SELECT MIN(c.category_id) FROM news_letter_categories c "
            "                  WHERE c.news_letter_id = n.news_letter_id), 0) "
            "FROM news_letter n WHERE n.news_letter_embedding IS NOT NULL ORDER BY n.news_letter_id"
        )
        items = cur.fetchall()
        cur.execute(
            "SELECT user_id, news_letter_id, created_at::timestamptz FROM user_newsletter_ctr_log "
            "WHERE event = 'click'"
        )
        clicks = cur.fetchall()
        cur.execute("SELECT news_letter_id, created_at FROM recommendation_impression_log")
        inviews = cur.fetchall()
        cur.execute("SELECT user_id, category_id FROM user_preferred_categories")
        user_categories: Dict[int, List[int]] = {}
        for uid, cid in cur.fetchall():
            user_categories.setdefault(int(uid), []).append(int(cid))

        where, params = ["r.feature_schema_version = %s", "r.features_as_of IS NOT NULL"], [FEATURE_SCHEMA_VERSION]
        if user_ids is not None:
            where.append("r.user_id = ANY(%s)")
            params.append(list(user_ids))
        if since is not None:
            where.append("r.created_at >= %s")
            params.append(since)
        cur.execute(
            "SELECT r.request_id::text, r.user_id, r.features_as_of, r.candidate_ids, r.source, r.cache_hit "
            "FROM recommendation_request_log r WHERE " + " AND ".join(where) + " ORDER BY r.created_at, r.request_id",
            params,
        )
        request_rows = cur.fetchall()
        cur.execute(
            "SELECT request_id::text, news_letter_id, features, scores_shadow FROM recommendation_impression_log "
            "WHERE request_id = ANY(%s::uuid[]) ORDER BY request_id, position",
            ([r[0] for r in request_rows],),
        )
        slots: Dict[str, list] = {}
        for request_id, nid, features, scores_shadow in cur.fetchall():
            slots.setdefault(request_id, []).append((nid, features, scores_shadow))

    dim = len(_vector(items[0][1])) if items else 1
    logs = ServiceLogs(
        item_ids=np.array([r[0] for r in items], dtype=np.int64),
        item_emb=np.stack([_vector(r[1]) for r in items]) if items else np.zeros((0, dim), dtype=np.float32),
        item_created_us=np.array([epoch_us(r[2]) for r in items], dtype=np.int64),
        item_category=np.array([r[3] for r in items], dtype=np.int64),
        click_user=np.array([r[0] for r in clicks], dtype=np.int64),
        click_item=np.array([r[1] for r in clicks], dtype=np.int64),
        click_us=np.array([epoch_us(r[2]) for r in clicks], dtype=np.int64),
        inview_item=np.array([r[0] for r in inviews], dtype=np.int64),
        inview_us=np.array([epoch_us(r[1]) for r in inviews], dtype=np.int64),
        user_categories=user_categories,
    )
    requests = []
    for request_id, user_id, as_of, candidate_ids, source, cache_hit in request_rows:
        rows = slots.get(request_id, [])
        if not rows or any(f is None for _, f, _ in rows):
            continue
        shadow = None
        if model_version is not None and all(s and model_version in s and s[model_version] is not None
                                             for _, _, s in rows):
            shadow = np.array([s[model_version] for _, _, s in rows], dtype=np.float64)
        requests.append(LoggedRequest(
            request_id=request_id, user_id=int(user_id), as_of=as_of,
            candidate_ids=[int(i) for i in (candidate_ids or [])],
            slot_ids=[int(nid) for nid, _, _ in rows],
            slot_features=np.stack([np.frombuffer(bytes(f), dtype="<f4") for _, f, _ in rows]),
            slot_shadow=shadow, meta={"source": source, "cache_hit": bool(cache_hit)},
        ))
    return logs, requests


def load_model(conn, name: str, version: str) -> tuple[PredictFn, str]:
    """model_registry의 LightGBM 모델과, 칸 로그에 남는 그 모델의 버전 이름."""
    import lightgbm as lgb

    with conn.cursor() as cur:
        cur.execute(
            "SELECT model_text FROM model_registry WHERE model_name = %s AND model_version = %s "
            "AND model_format = 'lightgbm_text'", (name, version),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"model_registry에 {name}@{version}이 없습니다")
    booster = lgb.Booster(model_str=row[0])
    return booster.predict, f"lgbm:{name}@{version}"


def serving_spec_from_env() -> CandidateSpec:
    """이 프로세스의 환경변수(RECSYS_*)로 서빙이 돌 때의 후보 구성. API와 같은 환경에서 실행해야 뜻이 있다."""
    import sys
    from pathlib import Path

    backend = str(Path(__file__).resolve().parents[2] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.recsys.config import RecsysConfig

    return RecsysConfig.from_env().candidate_spec()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--model-name", default="ranker")
    ap.add_argument("--model-version", required=True, help="model_registry의 버전(칸 로그에 shadow 점수를 남긴 모델)")
    ap.add_argument("--since", help="이 시각(ISO 8601) 이후의 요청만 비교")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    import psycopg2

    conn = psycopg2.connect(args.database_url)
    try:
        predict, version = load_model(conn, args.model_name, args.model_version)
        since = datetime.fromisoformat(args.since) if args.since else None
        logs, requests = load_from_db(conn, model_version=version, since=since)
    finally:
        conn.close()
    report = run_gate(logs, requests, predict, serving_spec_from_env(),
                      meta={"source": "cli", "model_version": version})
    write_report(report, args.out)
    print(json.dumps({k: report[k]["pass"] for k in ("features", "scores", "end_to_end", "candidate_config")}
                     | {"pass": report["pass"], "requests": len(requests)}))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
