"""비교 대상 베이스라인 추천기. recommend_engine 코드를 전혀 쓰지 않고(모델과 무관하게
정의되는 규칙 기반 추천이므로), file_loader.ArchiveBundle에서 바로 계산한다. 모든
베이스라인은 동일한 (유저 집합, 후보 풀, top_k)로 recommendations dict를 만들어 team
모델과 완전히 같은 Evaluator/bootstrap 경로로 비교할 수 있게 한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from file_loader import ArchiveBundle


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def history_embedding(
    user_id: int,
    cutoff: datetime,
    logs_df: pd.DataFrame,
    emb_by_id: Dict[int, np.ndarray],
    half_life_days: float = 7.0,
    min_weight: float = 0.01,
) -> Optional[np.ndarray]:
    """team feature_engineer.py/data_loader.py와 동일한 공식(반감기 가중 평균)을
    베이스라인 전용으로 독립 구현한다(recommend_engine 코드는 import하지 않음 -
    이 함수가 재현하는 대상은 '팀이 쓰는 공식'이지 '팀의 코드' 자체가 아니다)."""
    u_logs = logs_df[(logs_df["user_id"] == user_id) & (logs_df["timestamp"] < cutoff)]
    if u_logs.empty:
        return None
    vectors, weights = [], []
    for row in u_logs.itertuples(index=False):
        vec = emb_by_id.get(int(row.news_letter_id))
        if vec is None:
            continue
        days_ago = (cutoff - row.timestamp).days
        w = max(0.5 ** (days_ago / half_life_days), min_weight)
        vectors.append(vec)
        weights.append(w)
    if not vectors:
        return None
    return np.average(np.stack(vectors), axis=0, weights=weights)


def random_baseline(user_ids, candidate_ids, top_k: int, seed: int = 0) -> Dict[int, List[int]]:
    rng = np.random.default_rng(seed)
    out = {}
    cand = list(candidate_ids)
    for uid in user_ids:
        order = rng.permutation(len(cand))
        out[uid] = [cand[i] for i in order[:top_k]]
    return out


def fixed_order_baseline(user_ids, candidate_ids, top_k: int) -> Dict[int, List[int]]:
    """후보 리스트 순서(= newsletters_export.csv 행 순서) 그대로를 모든 유저에게 주는
    고정 리스트. 추천기가 아니라 **진단용 기준선**이다: 동점이 많은(거의 학습되지 않은)
    모델이나 cold 유저 폴백이 사실상 이 순서로 랭킹을 정하므로, 그 결과가 모델/유사도의
    힘인지 이 순서의 우연인지 가르는 데 쓴다(v2 리뷰: 이 순서만으로 MRR 0.638)."""
    rec = list(candidate_ids)[:top_k]
    return {uid: list(rec) for uid in user_ids}


def popularity_order(candidate_ids, train_click_counts: Dict[int, int]) -> List[int]:
    return sorted(candidate_ids, key=lambda nid: (-train_click_counts.get(nid, 0), nid))


def popularity_baseline(
    user_ids, candidate_ids, top_k: int, train_click_counts: Dict[int, int]
) -> Dict[int, List[int]]:
    ranked = popularity_order(candidate_ids, train_click_counts)
    rec = ranked[:top_k]
    return {uid: list(rec) for uid in user_ids}


def recency_baseline(
    user_ids, candidate_ids, top_k: int, created_at_by_id: Dict[int, pd.Timestamp], cutoff: datetime
) -> Dict[int, List[int]]:
    def key(nid):
        ts = created_at_by_id[nid]
        age = (cutoff - ts).total_seconds()
        return (age if age >= 0 else float("inf"), nid)

    ranked = sorted(candidate_ids, key=key)
    rec = ranked[:top_k]
    return {uid: list(rec) for uid in user_ids}


def category_match_baseline(
    user_ids,
    candidate_ids,
    top_k: int,
    user_pref_categories: Dict[int, set],
    category_map: Dict[int, List[int]],
    tie_break_recency: Optional[Dict[int, pd.Timestamp]] = None,
    cutoff: Optional[datetime] = None,
) -> Dict[int, List[int]]:
    out = {}
    for uid in user_ids:
        prefs = user_pref_categories.get(uid, set())

        def key(nid, prefs=prefs):
            cats = set(category_map.get(nid, []))
            match = len(cats & prefs)
            tie = 0.0
            if tie_break_recency is not None and cutoff is not None:
                age = (cutoff - tie_break_recency[nid]).total_seconds()
                tie = age if age >= 0 else float("inf")
            return (-match, tie, nid)

        ranked = sorted(candidate_ids, key=key)
        out[uid] = ranked[:top_k]
    return out


def cosine_history_baseline(
    user_ids,
    candidate_ids,
    top_k: int,
    logs_df: pd.DataFrame,
    emb_by_id: Dict[int, np.ndarray],
    cutoff: datetime,
    half_life_days: float = 7.0,
    min_weight: float = 0.01,
    fallback_order: Optional[Sequence[int]] = None,
) -> Dict[int, List[int]]:
    """fallback_order: 히스토리가 없는 유저에게 줄 순서. None이면(레거시) 후보 리스트
    순서(CSV 행 순서) 그대로인데, 그 순서는 우연히 무작위보다 좋은 고정 리스트라
    cold 유저 수치를 부풀린다(v2 리뷰 MINOR) - compute_all_baselines는 인기도 순서를
    넘긴다."""
    out = {}
    cand_list = list(candidate_ids)
    fallback = list(fallback_order) if fallback_order is not None else cand_list
    cand_matrix = np.stack([emb_by_id[nid] for nid in cand_list])
    cand_norm = np.linalg.norm(cand_matrix, axis=1)
    cand_norm[cand_norm == 0] = 1.0
    for uid in user_ids:
        hist = history_embedding(uid, cutoff, logs_df, emb_by_id, half_life_days, min_weight)
        if hist is None or np.linalg.norm(hist) == 0:
            # 히스토리가 없는 유저: fallback_order(기본 호출부에서는 인기도 순). 몇 명이
            # 폴백됐는지는 run_repro가 cold 유저 수로 따로 보고한다.
            out[uid] = fallback[:top_k]
            continue
        sims = cand_matrix @ hist / (cand_norm * np.linalg.norm(hist))
        order = np.argsort(-sims)[:top_k]
        out[uid] = [cand_list[i] for i in order]
    return out


def onboarding_newsletter_similarity_baseline(
    user_ids,
    candidate_ids,
    top_k: int,
    preferred_newsletters: pd.DataFrame,
    emb_by_id: Dict[int, np.ndarray],
    fallback_order: Optional[Sequence[int]] = None,
) -> Dict[int, List[int]]:
    """온보딩 시 유저가 직접 선택한 뉴스레터(synthetic_user_preferred_newsletters.csv)의
    평균 임베딩과 후보 뉴스레터 임베딩의 코사인 유사도로 랭킹. 선택이 없는 유저는
    fallback_order(None이면 후보 리스트 순서)."""
    cand_list = list(candidate_ids)
    fallback = list(fallback_order) if fallback_order is not None else cand_list
    cand_matrix = np.stack([emb_by_id[nid] for nid in cand_list])
    cand_norm = np.linalg.norm(cand_matrix, axis=1)
    cand_norm[cand_norm == 0] = 1.0

    user_selected = preferred_newsletters.groupby("user_id")["news_letter_id"].apply(list).to_dict()
    out = {}
    for uid in user_ids:
        selected = user_selected.get(uid, [])
        vecs = [emb_by_id[nid] for nid in selected if nid in emb_by_id]
        if not vecs:
            out[uid] = fallback[:top_k]
            continue
        mean_vec = np.mean(np.stack(vecs), axis=0)
        norm = np.linalg.norm(mean_vec)
        if norm == 0:
            out[uid] = fallback[:top_k]
            continue
        sims = cand_matrix @ mean_vec / (cand_norm * norm)
        order = np.argsort(-sims)[:top_k]
        out[uid] = [cand_list[i] for i in order]
    return out


def compute_all_baselines(
    bundle: ArchiveBundle,
    category_map: Dict[int, List[int]],
    candidate_ids: Sequence[int],
    user_ids: Sequence[int],
    train_logs: pd.DataFrame,
    cutoff: datetime,
    top_k: int = 20,
    seed: int = 0,
) -> Dict[str, Dict[int, List[int]]]:
    """cosine_history/onboarding은 히스토리·온보딩 선택이 없는 유저에게 인기도 순서로
    폴백한다(v2.1 - v2까지는 CSV 행 순서였다). `fixed_csv_order`는 그 레거시 폴백
    순서 자체를 진단용 기준선으로 따로 보고한다.

    모든 베이스라인은 `train_logs`(cutoff 이전 로그) + `cutoff`만 본다 - 이미 v1
    시점부터 point-in-time이었다(모델과 달리 미래 정보를 볼 방법이 없었다). v2가
    바꾼 것은 이 함수 자체가 아니라, 호출부(run_repro.py)가 넘기는 `cutoff`를
    모든 arm/시드가 공유하는 고정 answer_start로 통일한 것(MINOR #1 수정)이다."""
    emb_by_id = {
        int(row.news_letter_id): np.asarray(row.embedding, dtype=np.float32)
        for row in bundle.newsletters.itertuples(index=False)
    }
    created_at_by_id = {
        int(row.news_letter_id): row.created_at for row in bundle.newsletters.itertuples(index=False)
    }
    train_click_counts = train_logs["news_letter_id"].value_counts().to_dict()
    user_pref_categories = bundle.preferred_categories.groupby("user_id")["category_id"].apply(set).to_dict()
    pop_order = popularity_order(candidate_ids, train_click_counts)

    return {
        "random": random_baseline(user_ids, candidate_ids, top_k, seed=seed),
        "fixed_csv_order": fixed_order_baseline(user_ids, candidate_ids, top_k),
        "popularity": popularity_baseline(user_ids, candidate_ids, top_k, train_click_counts),
        "recency": recency_baseline(user_ids, candidate_ids, top_k, created_at_by_id, cutoff),
        "category_match": category_match_baseline(
            user_ids, candidate_ids, top_k, user_pref_categories, category_map, created_at_by_id, cutoff
        ),
        "cosine_history": cosine_history_baseline(
            user_ids, candidate_ids, top_k, train_logs, emb_by_id, cutoff, fallback_order=pop_order
        ),
        "onboarding_newsletter_cosine": onboarding_newsletter_similarity_baseline(
            user_ids, candidate_ids, top_k, bundle.preferred_newsletters, emb_by_id, fallback_order=pop_order
        ),
    }
