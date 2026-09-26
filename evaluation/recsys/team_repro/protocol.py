"""v2 REQUIRED CHANGE #1: 모든 arm/시드/버전이 공유하는 '고정 정답 구간(answer
window)' 경계 하나와, 그 경계로부터 파생되는 cold/warm 유저 분류·이미 본 아이템
제외(seen-item filtering) 유틸리티.

v1의 문제점(어드버서리얼 리뷰 MINOR #1): 베이스라인은 자기가 따로 80% 지점을
다시 계산했고, 모델은 각 실행이 자기 학습 데이터(negative sampling에 따라 행
수가 미세하게 달라짐)의 80% 지점을 또 따로 계산해서, "동일한 정답 구간"이라는
주장이 시드마다 몇 초씩 드리프트하는 근사치에 불과했다. v2는 이 모듈이 계산한
`answer_start` 값 하나를 run_repro.py가 모든 베이스라인/모델 서브프로세스에
그대로 넘겨, 정답 구간이 문자 그대로 동일하도록 만든다.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Set, Tuple

import numpy as np
import pandas as pd


def compute_fixed_answer_start(clicks_df: pd.DataFrame, val_ratio: float = 0.2) -> pd.Timestamp:
    """클릭 로그(is_clicked==1으로 이미 필터링된 DataFrame)를 시간순 정렬해 뒤에서
    val_ratio만큼을 '정답 구간'으로 남기는 단일 경계 시각을 계산한다.

    이 값이 team_split 프로토콜의 모든 arm(베이스라인 포함)·모든 시드·모든 코드
    버전에서 재사용하는 유일한 '정답 구간 시작 시각'이다.
    """
    if len(clicks_df) == 0:
        raise ValueError("클릭 로그가 비어 있어 정답 구간 경계를 계산할 수 없습니다.")
    sorted_ts = clicks_df.sort_values("timestamp")["timestamp"].reset_index(drop=True)
    n = len(sorted_ts)
    idx = min(int(n * (1 - val_ratio)), n - 1)
    return pd.Timestamp(sorted_ts.iloc[idx])


def cold_warm_split(
    user_ids: Iterable[int], clicks_df: pd.DataFrame, answer_start
) -> Tuple[Set[int], Set[int]]:
    """answer_start 이전에 클릭이 하나도 없는 유저 = cold, 하나라도 있으면 warm.

    데이터 구조상(각 페르소나가 ~1시간 세션 안에 195건을 한 번씩 본 뒤 80/20
    시간순으로 자르므로) 상당수 유저가 cold로 떨어진다 - 이는 버그가 아니라
    이 아카이브의 실제 특성이며, v2 리포트는 이를 그대로 노출한다(요구사항 7).
    """
    before = clicks_df[clicks_df["timestamp"] < answer_start]
    warm_from_clicks = set(int(u) for u in before["user_id"].unique().tolist())
    all_ids = {int(u) for u in user_ids}
    warm_ids = all_ids & warm_from_clicks
    cold_ids = all_ids - warm_from_clicks
    return cold_ids, warm_ids


def seen_items_by_user(clicks_df: pd.DataFrame, answer_start) -> Dict[int, Set[int]]:
    """answer_start 이전에 그 유저가 이미 클릭한 news_letter_id 집합.

    seen-item filtering(요구사항 1, 7) 및 정답 구간 대비 이미 클릭한 아이템 비율
    진단에 쓰인다.
    """
    before = clicks_df[clicks_df["timestamp"] < answer_start]
    out: Dict[int, Set[int]] = {}
    for uid, g in before.groupby("user_id"):
        out[int(uid)] = set(int(x) for x in g["news_letter_id"].tolist())
    return out


def shown_items_by_user(logs_df: pd.DataFrame, answer_start) -> Dict[int, Set[int]]:
    """answer_start 이전에 그 유저에게 **노출된**(클릭 여부 무관) news_letter_id 집합.

    이 아카이브는 페르소나마다 195건을 각각 정확히 한 번씩 노출하므로, 경계 이전에
    노출된 아이템은(클릭했든 안 했든) 정답 구간에 다시 나타날 수 없다 - 클릭한 것만
    지우는 seen-item filtering보다 한 단계 더 엄격한 'unshown-only' 필터가 이 집합을
    쓴다(v2 리뷰 MINOR: 정답 1,857건 중 경계 이전 노출분은 0건).
    `logs_df`는 is_clicked 필터를 하지 않은 전체 노출 로그여야 한다."""
    return seen_items_by_user(logs_df, answer_start)


def filter_seen(
    recommendations: Dict[int, List[int]], seen: Dict[int, Set[int]]
) -> Dict[int, List[int]]:
    """추천 리스트에서 유저가 answer_start 이전에 이미 클릭한 아이템을 제거한다
    (뒤를 당겨 채우지 않음 - Evaluator.precision_at_k는 항상 k로 나누므로, 제거된
    자리는 그대로 '누락'으로 집계된다 - 있는 그대로의 손실을 보여주기 위함)."""
    out: Dict[int, List[int]] = {}
    for uid, recs in recommendations.items():
        already = seen.get(uid, set())
        out[uid] = [nid for nid in recs if nid not in already]
    return out


def seen_share_in_topk(
    recommendations: Dict[int, List[int]], seen: Dict[int, Set[int]], k: int = 5
) -> float:
    """모델/베이스라인 top-k 추천 중 이미 클릭한(=정답이 될 수 없는) 아이템의 비율
    (요구사항 7: '이 데이터가 증명할 수 없는 것'/데이터 구조 절 진단용)."""
    total = 0
    already_seen = 0
    for uid, recs in recommendations.items():
        top = recs[:k]
        already = seen.get(uid, set())
        total += len(top)
        already_seen += sum(1 for nid in top if nid in already)
    return (already_seen / total) if total else float("nan")


def restrict_ground_truth_to_pool(
    ground_truth: Dict[int, Set[int]], candidate_ids
) -> Dict[int, Set[int]]:
    """정답을 후보 풀 안으로 제한한다(요구사항 3, small_recent_15 수정 - 후보에
    없는 아이템은 애초에 추천될 수 없으므로 정답으로 셀 수 없다). candidate_ids가
    None이면(= 전체 풀 사용) 그대로 반환한다."""
    if candidate_ids is None:
        return ground_truth
    pool = set(int(c) for c in candidate_ids)
    out = {}
    for uid, items in ground_truth.items():
        restricted = {int(i) for i in items} & pool
        if restricted:
            out[uid] = restricted
    return out


def split_metrics_by_group(
    per_user: Dict[int, Dict[str, float]], group_ids: Set[int]
) -> Dict[str, float]:
    """per_user 지표 중 group_ids에 속한 유저만 평균한다(cold/warm 분리 집계)."""
    subset = {u: m for u, m in per_user.items() if u in group_ids}
    if not subset:
        return {}
    keys = next(iter(subset.values())).keys()
    agg = {k: float(np.mean([m[k] for m in subset.values()])) for k in keys}
    agg["num_users"] = len(subset)
    return agg
