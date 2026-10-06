"""서빙 어댑터: 요청 시점의 사용자 상태와 후보 아이템으로 하네스와 같은 피처 행렬을 만든다 (ADR 0033).

    features(state, items, now) -> (len(items), 22) float32      # 열 = schema.RANKER_V2_FEATURES

하는 일은 입력을 recsys_core의 자료구조(ItemCatalog, Requests, FeatureContext)로 옮기는 것뿐이고, 값은
전부 compute_feature_columns가 계산한다 - 하네스(evaluation/recsys/ebnerd)가 부르는 그 함수다. 그래서
피처의 정의가 한 벌이다.

시각 규칙(오프라인에서 같은 값을 다시 계산하려면 이 규칙을 그대로 따른다):
- 요청 시각 now(마이크로초)의 "요청 초" T = floor(now) + 1. 경과 시간은 전부 T 기준 정수 초다.
- 유저 이벤트 = now보다 엄격히 이전의 클릭. 이벤트의 시각은 floor(클릭 시각)이라 항상 T보다 작다.
  방금 한 클릭(같은 초 안이라도 now 이전이면)이 다음 요청의 피처에 들어간다.
- 아이템 쪽 인기도 창은 [T - ITEM_LAG_S - w, T - ITEM_LAG_S)다. 끝을 요청보다 앞에 두는 이유: 응답 뒤에
  쓰이는 노출 로그나 다른 요청의 클릭처럼 "시각은 창 안인데 집계할 때는 아직 커밋되지 않은" 행이 있으면
  서빙이 본 값과 나중에 로그로 다시 센 값이 달라진다. 1초 넘게 지난 행만 세면 그 경합이 없다.

입력(state가 들고 있어야 하는 것 - 덕 타이핑):
- category_ids: 온보딩에서 고른 선호 카테고리 ID들
- hist: recsys_core.profile.HistState - now 이전의 클릭이 전부 반영된 증분 상태(없으면 빈 상태)
- hist_last_event_at: hist에 반영된 클릭 중 가장 늦은 것의 시각(마이크로초, tz-aware). hist가 비어 있지 않으면
  있어야 한다. HistState의 기준 시각은 정수 초라, "요청과 같은 초 안에서 요청 이후에 커밋된 클릭이 상태에 들어
  있는가"는 이 값으로만 가려진다(check_inputs).
- recent_clicks: [ClickEvent] - [T - 24h, now) 구간의 최근 SHORT_MAX_EVENTS개 클릭.
  "최근 N개"는 (초 단위로 내린 시각, 뉴스레터 ID) 순으로 가장 뒤의 N개다(latest_events). 같은 초 안의 순서를
  마이크로초로 가르지 않는 이유: 피처의 시각이 정수 초라서, 이벤트 로그에서 다시 계산하는 쪽은 같은 초의
  클릭들을 시각으로 구별하지 못한다. 상한의 경계가 같은 초의 클릭 여러 건에 걸리면 두 쪽이 서로 다른 클릭을
  남기게 된다(빠르게 이어지는 클릭에서 실제로 short_cos가 0.04 어긋났다).
- popularity: {news_letter_id: WindowCounts} - 후보 아이템의 창 집계. 없는 ID는 0건이다.
  None이면 아직 읽지 않은 것이므로 FeatureInputsMissing을 낸다(0으로 채워 조용히 틀린 값을 내지 않는다).
items의 원소: news_letter_id, embedding, created_at, category_id(대표 카테고리, 없으면 None).

값의 표기:
- 임베딩은 float32로 L2 정규화해서 쓴다(하네스 카탈로그와 같은 연산).
- news_category와 카테고리 인덱스는 카테고리 ID 그대로다. 0은 "카테고리 없음"이다.
- user_age, user_gender는 항상 NaN이다(서비스의 인구통계를 EB-NeRD의 부호 체계로 옮기지 않는다. ADR 0033).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence, Tuple

import numpy as np

from . import schema
from .events import EventIndex
from .features import (
    HOUR,
    FeatureConfig,
    FeatureContext,
    ItemCatalog,
    ItemWindowCounts,
    Requests,
    UserHistState,
    compute_feature_columns,
)
from .profile import NO_CATEGORY, HistState, counts_row, unit_rows
from .sessions import SESSION_GAP_S, request_sessions, sessionize

FEATURE_SCHEMA_VERSION = 2
FEATURE_NAMES: Tuple[str, ...] = schema.RANKER_V2_FEATURES
SHORT_MAX_EVENTS = 20
ITEM_LAG_S = 2
# 하네스 기본값에서 달라지는 것은 단기 상한과 아이템 쪽 지연 둘뿐이다.
SERVING_FEATURE_CONFIG = FeatureConfig(short_max_events=SHORT_MAX_EVENTS, item_lag_s=ITEM_LAG_S)
# 세션의 최근 N개가 단기 창 밖으로 나가지 않아야 서빙이 읽는 "24시간 안의 최근 N개"로 세션 피처가 닫힌다.
assert (SHORT_MAX_EVENTS - 1) * SESSION_GAP_S <= SERVING_FEATURE_CONFIG.short_window_h * HOUR
# 카테고리 ID를 인덱스로 그대로 쓰므로 터무니없이 큰 ID가 들어오면 배열이 그만큼 커진다.
MAX_CATEGORY_ID = 65_535
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SCHEMA_DEFINITION = {
    "schema_version": FEATURE_SCHEMA_VERSION,
    "groups": list(schema.ALL_GROUPS),
    "half_life_days": SERVING_FEATURE_CONFIG.half_life_days,
    "floor_days": SERVING_FEATURE_CONFIG.floor_days,
    "min_weight": SERVING_FEATURE_CONFIG.min_weight,
    "short_window_h": SERVING_FEATURE_CONFIG.short_window_h,
    "short_max_events": SERVING_FEATURE_CONFIG.short_max_events,
    "pop_windows_h": [float(w) for w in SERVING_FEATURE_CONFIG.pop_windows_h],
    "ctr_window_h": SERVING_FEATURE_CONFIG.ctr_window_h,
    "item_lag_s": SERVING_FEATURE_CONFIG.item_lag_s,
    "session_gap_s": SESSION_GAP_S,
    "request_time": "floor(now)+1 s; user events strictly before now",
    "latest_events_order": "(floor second, news_letter_id)",
    "embedding": "float32 l2-normalized",
    "category": "primary category id, 0 = none",
    "extras": {name: "nan" for name in schema.EXTRA_COLUMNS},
}
SCHEMA_HASH = schema.schema_hash(FEATURE_NAMES, SCHEMA_DEFINITION)


class FeatureInputsMissing(ValueError):
    """어댑터가 읽어야 하는 입력이 state에 없다(아직 읽지 않았거나, 요청 시각과 맞지 않는다)."""


@dataclass(frozen=True)
class ClickEvent:
    at: datetime  # tz-aware
    embedding: np.ndarray
    news_letter_id: int = 0  # 같은 초의 클릭 사이의 순서를 정한다(latest_events)


@dataclass(frozen=True)
class WindowCounts:
    """한 아이템의 인기도 창 집계. clicks는 SERVING_FEATURE_CONFIG.pop_windows_h 순서(6h, 24h, 48h)다."""
    clicks: Tuple[int, ...]
    inviews: int


def epoch_seconds(at: datetime) -> int:
    """tz-aware 시각의 epoch 초(내림). 부동소수점을 거치지 않는다."""
    if at.tzinfo is None:
        raise ValueError("시각은 tz-aware여야 합니다")
    return (at - _EPOCH) // timedelta(seconds=1)


def from_epoch_seconds(seconds: int) -> datetime:
    return _EPOCH + timedelta(seconds=int(seconds))


def request_second(now: datetime) -> int:
    """요청 초 T = floor(now) + 1: now 이전의 모든 이벤트(초 단위로 내린 시각)보다 엄격히 크다."""
    return epoch_seconds(now) + 1


def short_window_start(now: datetime) -> datetime:
    """단기 창의 시작(포함). recent_clicks를 읽는 쪽이 이 시각 이상, now 미만으로 거른다."""
    return from_epoch_seconds(request_second(now) - int(SERVING_FEATURE_CONFIG.short_window_h * HOUR))


def item_window_end(now: datetime) -> datetime:
    """인기도 창의 끝(미포함)."""
    return from_epoch_seconds(request_second(now) - SERVING_FEATURE_CONFIG.item_lag_s)


def item_window_starts(now: datetime) -> Tuple[datetime, ...]:
    """클릭 창들의 시작(포함), SERVING_FEATURE_CONFIG.pop_windows_h 순서."""
    end = request_second(now) - SERVING_FEATURE_CONFIG.item_lag_s
    return tuple(from_epoch_seconds(end - int(w * HOUR)) for w in SERVING_FEATURE_CONFIG.pop_windows_h)


def inview_window_start(now: datetime) -> datetime:
    end = request_second(now) - SERVING_FEATURE_CONFIG.item_lag_s
    return from_epoch_seconds(end - int(SERVING_FEATURE_CONFIG.ctr_window_h * HOUR))


def event_order(click: ClickEvent) -> Tuple[int, int]:
    """클릭의 순서 키: (초 단위 시각, 뉴스레터 ID). recsys_core의 이벤트 인덱스가 같은 초의 이벤트를 아이템
    순으로 세우는 것과 같은 순서다(로그 재계산 경로의 카탈로그는 뉴스레터 ID 순이다)."""
    return (epoch_seconds(click.at), int(click.news_letter_id))


def latest_events(clicks: Sequence[ClickEvent], n: int = SHORT_MAX_EVENTS) -> list:
    """event_order로 가장 뒤의 n개(오름차순). 단기 창·세션의 "최근 N개"는 전부 이 함수의 순서다."""
    return sorted(clicks, key=event_order)[-int(n):]


def _category(value: Optional[int]) -> int:
    c = NO_CATEGORY if value is None else int(value)
    if not 0 <= c <= MAX_CATEGORY_ID:
        raise ValueError(f"카테고리 ID {c}가 범위 [0, {MAX_CATEGORY_ID}] 밖입니다")
    return c


def check_inputs(state, now: datetime) -> None:
    """state의 입력이 now 시점의 것으로 서로 맞는지 본다. 어긋나면 FeatureInputsMissing.

    어긋난 입력으로 계산한 피처는 로그로 다시 계산한 값과 다르다. 조용히 다른 값을 남기느니 그 요청의 피처를
    만들지 않는다.

    시각 비교는 전부 마이크로초다. 유저 이벤트의 경계가 "now보다 엄격히 이전"(마이크로초)이므로 초 단위로 비교하면
    요청과 같은 초 안의 어긋남이 빠진다: 요청 시각 뒤에 커밋된 같은 초의 클릭이 장기 상태에 들어 있어도
    기준 초는 요청 초보다 작다.
    """
    if getattr(state, "popularity", None) is None:
        raise FeatureInputsMissing("state.popularity가 없습니다(후보의 인기도 창 집계를 읽지 않았다)")
    hist: HistState = getattr(state, "hist", None) or HistState()
    clicks: Sequence[ClickEvent] = getattr(state, "recent_clicks", None) or ()
    last_at: Optional[datetime] = None
    if not hist.empty:
        last_at = getattr(state, "hist_last_event_at", None)
        if last_at is None:
            raise FeatureInputsMissing("state.hist_last_event_at이 없습니다(장기 상태에 반영된 마지막 클릭의 시각)")
        if epoch_seconds(last_at) != int(hist.anchor_s):
            raise FeatureInputsMissing("state.hist_last_event_at이 장기 상태의 기준 시각과 다른 초입니다")
        if not last_at < now:
            raise FeatureInputsMissing("장기 상태에 요청 시각 이후의 클릭이 반영돼 있습니다")
    for c in clicks:
        if not c.at < now:
            raise FeatureInputsMissing("recent_clicks에 요청 시각 이후의 클릭이 있습니다")
    if clicks and (last_at is None or last_at < max(c.at for c in clicks)):
        raise FeatureInputsMissing("장기 상태가 클릭 로그보다 뒤처져 있습니다(재구축 필요)")


def build_context(state, items: Sequence, now: datetime) -> Tuple[FeatureContext, Requests]:
    """요청 하나를 recsys_core 입력으로 옮긴다. 카탈로그 행 = 후보 n개 + 최근 클릭 m개(단기·세션 벡터용)."""
    check_inputs(state, now)
    cfg = SERVING_FEATURE_CONFIG
    hist: HistState = getattr(state, "hist", None) or HistState()
    clicks = latest_events(getattr(state, "recent_clicks", None) or ())
    popularity: Mapping[int, WindowCounts] = state.popularity
    n, m = len(items), len(clicks)
    t_req = request_second(now)

    item_cats = [_category(getattr(it, "category_id", None)) for it in items]
    user_cats = sorted({_category(c) for c in (getattr(state, "category_ids", None) or ())})
    hist_cats = [_category(c) for c in hist.cat_counts]
    n_categories = max([NO_CATEGORY, *item_cats, *user_cats, *hist_cats]) + 1

    raw = [np.asarray(it.embedding, dtype=np.float32) for it in items]
    raw += [np.asarray(c.embedding, dtype=np.float32) for c in clicks]
    emb = unit_rows(np.stack(raw))
    dim = emb.shape[1]
    if not hist.empty and hist.hist_sum.shape != (dim,):
        raise ValueError(f"장기 상태의 차원 {hist.hist_sum.shape} != 임베딩 차원 ({dim},)")
    catalog = ItemCatalog(
        ids=np.arange(n + m),
        emb=emb,
        pub_time=[epoch_seconds(it.created_at) for it in items] + [0] * m,
        category=item_cats + [NO_CATEGORY] * m,
        n_categories=n_categories,
    )

    click_s = np.asarray([epoch_seconds(c.at) for c in clicks], dtype=np.int64)
    click_rows = n + np.arange(m, dtype=np.int64)
    zeros = np.zeros(m, dtype=np.int64)
    sessions = sessionize(zeros, click_s, SESSION_GAP_S)
    current = request_sessions(zeros, click_s, sessions, [0], [t_req], SESSION_GAP_S)

    static = np.zeros((1, n_categories), dtype=bool)
    static[0, user_cats] = True

    def per_item(values) -> np.ndarray:
        return np.asarray(list(values) + [0] * m, dtype=np.int64)

    counts = [popularity.get(int(it.news_letter_id)) for it in items]
    windows = tuple(cfg.pop_windows_h)
    for wc in counts:
        if wc is not None and len(wc.clicks) != len(windows):
            raise ValueError("WindowCounts.clicks의 길이가 인기도 창의 수와 다릅니다")
    snapshot = ItemWindowCounts(
        clicks={w: per_item(0 if wc is None else wc.clicks[j] for wc in counts) for j, w in enumerate(windows)},
        inviews=per_item(0 if wc is None else wc.inviews for wc in counts),
    )

    ctx = FeatureContext(
        catalog=catalog,
        user_log=EventIndex(zeros, click_s, click_rows),
        session_log=EventIndex(sessions, click_s, click_rows),
        static_categories=(np.zeros(1, dtype=np.int64), static),
        config=cfg,
        user_hist_state=UserHistState(
            users=[0],
            hist_sum=(np.zeros(dim) if hist.empty else hist.hist_sum)[None, :],
            hist_len=[0 if hist.empty else hist.hist_len],
            cat_counts=counts_row({} if hist.empty else hist.cat_counts, n_categories)[None, :],
            last_time=[-1 if hist.empty else int(hist.anchor_s)],
        ),
        item_window_counts=snapshot,
    )
    req = Requests(user=[0], time=[t_req], cand_ptr=[0, n], cand_item=np.arange(n), session=current)
    return ctx, req


def features(state, items: Sequence, now: datetime) -> np.ndarray:
    """후보 items의 피처 행렬 (len(items), len(FEATURE_NAMES)) float32. 행 순서는 items 순서다."""
    n = len(items)
    if n == 0:
        check_inputs(state, now)
        return np.zeros((0, len(FEATURE_NAMES)), dtype=schema.FEATURE_DTYPE)
    ctx, req = build_context(state, items, now)
    cols = compute_feature_columns(ctx, req, groups=schema.ALL_GROUPS)
    unknown = np.full(n, np.nan, dtype=schema.FEATURE_DTYPE)
    return schema.assemble(cols, {name: unknown for name in schema.EXTRA_COLUMNS}, FEATURE_NAMES)


# LightGBMScorer가 모델의 feature_names·스키마 지문과 요청마다 비교한다.
features.feature_names = list(FEATURE_NAMES)
features.schema_version = FEATURE_SCHEMA_VERSION
features.schema_hash = SCHEMA_HASH
