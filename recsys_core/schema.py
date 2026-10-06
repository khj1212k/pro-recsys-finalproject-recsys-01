"""ranker v2 피처 스키마: 이름·순서·dtype·결측 규칙을 한곳에 둔다.

오프라인 하네스(evaluation/recsys/ebnerd/models.py)와 서빙 어댑터(recsys_core/serving.py)가 같은 목록과
같은 조립 함수를 쓴다. 모델은 열 이름이 아니라 열 위치로 예측하므로 순서가 스키마의 일부다.

- 값은 전부 float32다.
- compute_features가 만들지 않는 열(EXTRA_COLUMNS)은 호출부가 넘긴다. 모르는 값은 NaN이다
  (EB-NeRD의 age/gender가 비어 있을 때와 같은 표기).
"""
from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

import numpy as np

TEAM_FEATURES = ("hours_since_pub", "is_fresh_24h", "is_fresh_7d", "hist_cos", "cat_match_count",
                 "is_cat_match", "news_category", "user_age", "user_gender", "user_ncat")
POP_FEATURES = ("pop_clicks_6h", "pop_clicks_24h", "pop_clicks_48h", "pop_inviews_24h", "pop_ctr_24h")
SHORT_FEATURES = ("short_cos", "short_len", "sess_cos", "sess_len", "hours_since_last_event")
V2_EXTRA_FEATURES = ("cat_share", "hist_len")
# ranker_v2 / ranker_v2_poolneg가 학습한 22개 열, 학습할 때의 순서 그대로.
RANKER_V2_FEATURES = TEAM_FEATURES + POP_FEATURES + SHORT_FEATURES + V2_EXTRA_FEATURES

# RANKER_V2_FEATURES를 만드는 데 필요한 compute_features 그룹.
ALL_GROUPS = ("recency", "history", "team_category", "category", "popularity", "short_term")
# compute_features 밖에서 오는 열.
EXTRA_COLUMNS = ("user_age", "user_gender")
FEATURE_DTYPE = np.float32


def assemble(feats: Mapping[str, np.ndarray], extra: Mapping[str, np.ndarray],
             columns: Sequence[str]) -> np.ndarray:
    """피처 열들을 columns 순서의 (행, 열) float32 행렬로 쌓는다. extra에 있는 이름은 extra가 우선이다."""
    cols = [np.asarray(extra[c] if c in extra else feats[c], dtype=FEATURE_DTYPE) for c in columns]
    if cols:
        return np.column_stack(cols)
    n = len(next(iter(feats.values()))) if len(feats) else 0
    return np.zeros((n, 0), FEATURE_DTYPE)


def schema_hash(columns: Sequence[str], definition: Mapping[str, object]) -> str:
    """피처 스키마의 지문: 열 이름·순서와, 값의 정의를 바꾸는 설정(창 길이, 반감기, 단기 상한 등).

    모델을 등록할 때 이 값을 함께 적고 서빙이 요청마다 자기 값과 비교한다. 이름은 같은데 정의가 달라진
    피처로 예전 모델이 점수를 내는 일을 막는 용도다."""
    payload = {"columns": list(columns), "dtype": np.dtype(FEATURE_DTYPE).name,
               "definition": {k: definition[k] for k in sorted(definition)}}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
