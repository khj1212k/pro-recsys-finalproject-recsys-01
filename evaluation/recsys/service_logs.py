"""서비스 로그 -> recsys_core 입력: EB-NeRD의 prepare.py가 하는 일을 서비스 DB의 로그에 대해 한다.

서빙은 증분 상태와 SQL 집계 스냅숏으로 피처를 만든다(recsys_core/serving.py). 이 모듈은 같은 피처를
**이벤트 로그 전체에서** 하네스 방식으로 다시 계산한다: 유저 클릭 로그·세션 로그·아이템별 클릭/노출 로그를
EventIndex로 만들고 compute_feature_columns를 부른다. 두 경로의 값이 같은지가 parity 게이트다(ADR 0033).
한국어 로그로 학습할 때 피처를 만드는 경로이기도 하다(설계 문서 §3.7).

EB-NeRD 적재(prepare.load_bench)와 다른 점은 둘이다.
- 세션: 데이터에 세션 ID가 없어 클릭 시각으로 나눈다(recsys_core.sessionize, 30분).
- 중복 제거를 하지 않는다: 같은 초에 같은 뉴스레터를 두 번 누른 행은 이벤트 두 건이다(서빙의 증분 상태가
  클릭 행마다 한 번 갱신되는 것과 같다). EB-NeRD에서는 history.parquet과 행동 로그가 겹쳐서 지웠던 것이다.

시각 규칙은 recsys_core/serving.py의 것을 그대로 쓴다: 요청 초 T = floor(now) + 1, 유저 이벤트는 now보다
엄격히 이전의 클릭, 아이템 쪽 창은 FeatureConfig.item_lag_s만큼 앞에서 끝난다. "now 이전"은 마이크로초로
가르므로(요청 직후 같은 초에 들어온 클릭은 그 요청의 것이 아니다) 유저 쪽 로그는 요청마다 따로 만든다.
수백~수천 요청을 다시 계산하는 용도이고, 학습 규모로 쓰려면 벡터화가 필요하다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

import numpy as np

from recsys_core import (
    EventIndex,
    FeatureConfig,
    FeatureContext,
    ItemCatalog,
    Requests,
    compute_feature_columns,
    request_sessions,
    schema,
    sessionize,
)
from recsys_core.profile import NO_CATEGORY, unit_rows
from recsys_core.serving import FEATURE_NAMES, SERVING_FEATURE_CONFIG
from recsys_core.sessions import SESSION_GAP_S

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
US = 1_000_000


def epoch_us(at: datetime) -> int:
    """tz-aware 시각의 epoch 마이크로초(정확한 정수)."""
    if at.tzinfo is None:
        raise ValueError("시각은 tz-aware여야 합니다")
    return (at - _EPOCH) // timedelta(microseconds=1)


@dataclass
class ServiceLogs:
    """서비스 DB에서 읽은 원시 행. 시각은 epoch 마이크로초(int64)다.

    - items: 임베딩이 있는 뉴스레터 전부. category는 대표 카테고리 ID(없으면 0).
    - clicks: user_newsletter_ctr_log의 event = 'click' 행 전부.
    - inviews: recommendation_impression_log 행 전부(화면에 나간 칸 하나가 한 건).
    - user_categories: 온보딩에서 고른 선호 카테고리.
    """
    item_ids: np.ndarray
    item_emb: np.ndarray
    item_created_us: np.ndarray
    item_category: np.ndarray
    click_user: np.ndarray
    click_item: np.ndarray
    click_us: np.ndarray
    inview_item: np.ndarray
    inview_us: np.ndarray
    user_categories: Mapping[int, Sequence[int]] = field(default_factory=dict)


class LogBench:
    """로그 전체를 한 번 적재해 두고 요청마다 하네스 경로의 피처를 계산한다."""

    def __init__(self, logs: ServiceLogs, config: FeatureConfig = SERVING_FEATURE_CONFIG,
                 session_gap_s: int = SESSION_GAP_S):
        self.config = config
        self.session_gap_s = int(session_gap_s)
        # 카탈로그 행은 뉴스레터 ID 순이다. 이벤트 인덱스는 같은 초의 이벤트를 카탈로그 행 순으로 세우므로,
        # "최근 N개"의 경계가 같은 초의 클릭들에 걸릴 때 서빙(recsys_core.serving.latest_events: 초, 뉴스레터 ID 순)과
        # 같은 클릭을 남기려면 이 순서여야 한다.
        by_id = np.argsort(np.asarray(logs.item_ids, dtype=np.int64), kind="stable")
        item_ids = np.asarray(logs.item_ids, dtype=np.int64)[by_id]
        category = np.asarray(logs.item_category, dtype=np.int64)[by_id]
        item_emb = np.asarray(logs.item_emb, dtype=np.float32)[by_id] if len(item_ids) else logs.item_emb
        item_created_us = np.asarray(logs.item_created_us, dtype=np.int64)[by_id]
        if len(item_ids) > 1 and np.any(np.diff(item_ids) == 0):
            raise ValueError("ServiceLogs.item_ids에 같은 뉴스레터가 두 번 있습니다")
        user_cats = {int(u): sorted({int(c) for c in cats}) for u, cats in logs.user_categories.items()}
        top = max([NO_CATEGORY, *category.tolist(), *(c for cats in user_cats.values() for c in cats)])
        self.catalog = ItemCatalog(
            ids=item_ids,
            emb=unit_rows(item_emb) if len(item_ids) else np.zeros((0, 1), dtype=np.float32),
            pub_time=item_created_us // US,
            category=category,
            n_categories=top + 1,
        )
        self.user_categories = user_cats

        def item_index(ids, us) -> EventIndex:
            rows = self.catalog.index_of(ids)
            ok = rows >= 0
            return EventIndex(rows[ok], np.asarray(us, dtype=np.int64)[ok] // US, rows[ok])

        self.item_clicks = item_index(logs.click_item, logs.click_us)
        self.item_inviews = item_index(logs.inview_item, logs.inview_us)

        # 유저 이벤트 = 카탈로그에 있는(= 임베딩이 있는) 뉴스레터의 클릭. 유저·시각 순으로 세워 둔다.
        rows = self.catalog.index_of(logs.click_item)
        ok = rows >= 0
        users = np.asarray(logs.click_user, dtype=np.int64)[ok]
        us = np.asarray(logs.click_us, dtype=np.int64)[ok]
        order = np.lexsort((us, users))
        self._ev_user, self._ev_us, self._ev_row = users[order], us[order], rows[ok][order]

    def user_events(self, user_id: int, now_us: int) -> tuple[np.ndarray, np.ndarray]:
        """now보다 엄격히 이전인 이 유저의 이벤트: (epoch 마이크로초, 카탈로그 행), 시각순."""
        lo = np.searchsorted(self._ev_user, user_id, side="left")
        hi = np.searchsorted(self._ev_user, user_id, side="right")
        cut = lo + np.searchsorted(self._ev_us[lo:hi], now_us, side="left")
        return self._ev_us[lo:cut], self._ev_row[lo:cut]

    def request_columns(self, user_id: int, now_us: int, candidate_ids: Sequence[int]) -> dict[str, np.ndarray]:
        cand = self.catalog.index_of(np.asarray(candidate_ids, dtype=np.int64))
        if np.any(cand < 0):
            missing = [int(i) for i, r in zip(candidate_ids, cand) if r < 0]
            raise KeyError(f"후보 중 로그의 카탈로그에 없는 뉴스레터: {missing[:5]}")
        t_req = int(now_us) // US + 1
        ev_us, ev_row = self.user_events(int(user_id), int(now_us))
        ev_s = ev_us // US
        zeros = np.zeros(len(ev_s), dtype=np.int64)
        sessions = sessionize(zeros, ev_s, self.session_gap_s)
        current = request_sessions(zeros, ev_s, sessions, [0], [t_req], self.session_gap_s)
        static = np.zeros((1, self.catalog.n_categories), dtype=bool)
        static[0, self.user_categories.get(int(user_id), [])] = True
        ctx = FeatureContext(
            catalog=self.catalog,
            user_log=EventIndex(zeros, ev_s, ev_row),
            session_log=EventIndex(sessions, ev_s, ev_row),
            item_clicks=self.item_clicks,
            item_inviews=self.item_inviews,
            static_categories=(np.zeros(1, dtype=np.int64), static),
            config=self.config,
        )
        req = Requests(user=[0], time=[t_req], cand_ptr=[0, len(cand)], cand_item=cand, session=current)
        return compute_feature_columns(ctx, req, groups=schema.ALL_GROUPS)

    def request_features(self, user_id: int, now_us: int, candidate_ids: Sequence[int]) -> np.ndarray:
        """요청 하나의 (후보 수, 22) float32 피처 행렬 - 열은 recsys_core.serving.FEATURE_NAMES."""
        n = len(candidate_ids)
        if n == 0:
            return np.zeros((0, len(FEATURE_NAMES)), dtype=schema.FEATURE_DTYPE)
        cols = self.request_columns(user_id, now_us, candidate_ids)
        unknown = np.full(n, np.nan, dtype=schema.FEATURE_DTYPE)
        return schema.assemble(cols, {name: unknown for name in schema.EXTRA_COLUMNS}, FEATURE_NAMES)
