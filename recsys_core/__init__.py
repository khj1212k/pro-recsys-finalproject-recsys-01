"""요청 시점 추천과 오프라인 평가가 공유하는 point-in-time 피처 코어.

계산은 numpy만 쓴다. pandas는 DataFrame을 돌려주는 함수(compute_features, source_flags) 안에서만 읽는다.
"""
from .candidates import (
    SERVING_CANDIDATE_SPEC,
    CandidateSpec,
    group_ids,
    rank_within_groups,
    round_robin_union,
    source_flags,
)
from .events import EventIndex, expand_ranges
from .features import (
    DAY,
    HOUR,
    FeatureConfig,
    FeatureContext,
    ItemCatalog,
    ItemWindowCounts,
    Requests,
    UserHistState,
    compute_feature_columns,
    compute_features,
    feature_groups,
    window_category_counts,
    window_vectors,
)
from .sessions import SESSION_GAP_S, request_sessions, sessionize

__all__ = [
    "DAY",
    "HOUR",
    "SERVING_CANDIDATE_SPEC",
    "SESSION_GAP_S",
    "CandidateSpec",
    "EventIndex",
    "FeatureConfig",
    "FeatureContext",
    "ItemCatalog",
    "ItemWindowCounts",
    "Requests",
    "UserHistState",
    "compute_feature_columns",
    "compute_features",
    "expand_ranges",
    "feature_groups",
    "group_ids",
    "rank_within_groups",
    "request_sessions",
    "round_robin_union",
    "sessionize",
    "source_flags",
    "window_category_counts",
    "window_vectors",
]
