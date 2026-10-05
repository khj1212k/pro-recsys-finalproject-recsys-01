"""서빙 어댑터와 로그 재계산 경로를 같은 합성 로그 위에서 돌리기 위한 작은 세계.

로그(클릭·노출)가 유일한 사실이다. 서빙 쪽 입력(증분 상태, 최근 클릭, 창 집계)은 "서빙이 그 시각에 읽었을
값"을 로그에서 직접 만들어 준다 - 저장소(SQL)가 지켜야 하는 계약을 그대로 옮긴 참조 구현이다.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from evaluation.recsys.service_logs import ServiceLogs, epoch_us
from recsys_core.profile import NO_CATEGORY, HistState, apply_event
from recsys_core.serving import (
    SHORT_MAX_EVENTS,
    ClickEvent,
    WindowCounts,
    epoch_seconds,
    inview_window_start,
    item_window_end,
    item_window_starts,
    short_window_start,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@dataclass
class WorldItem:
    news_letter_id: int
    embedding: np.ndarray
    created_at: datetime
    category_id: Optional[int]


@dataclass
class WorldState:
    category_ids: List[int]
    hist: HistState
    recent_clicks: List[ClickEvent]
    popularity: Optional[Dict[int, WindowCounts]]


@dataclass
class World:
    items: Dict[int, WorldItem]
    clicks: List[Tuple[int, int, datetime]] = field(default_factory=list)   # (user, item, at)
    inviews: List[Tuple[int, datetime]] = field(default_factory=list)       # (item, at)
    user_categories: Dict[int, List[int]] = field(default_factory=dict)

    # --- 서빙이 now에 읽었을 입력
    def hist(self, user: int, now: datetime) -> HistState:
        state = HistState()
        mine = sorted((c for c in self.clicks if c[0] == user and c[2] < now), key=lambda c: c[2])
        for _, nid, at in mine:
            item = self.items[nid]
            state = apply_event(state, epoch_seconds(at), item.embedding,
                                NO_CATEGORY if item.category_id is None else item.category_id)
        return state

    def recent_clicks(self, user: int, now: datetime) -> List[ClickEvent]:
        since = short_window_start(now)
        mine = sorted((c for c in self.clicks if c[0] == user and since <= c[2] < now), key=lambda c: c[2])
        return [ClickEvent(at, self.items[nid].embedding) for _, nid, at in mine[-SHORT_MAX_EVENTS:]]

    def popularity(self, ids, now: datetime) -> Dict[int, WindowCounts]:
        end = item_window_end(now)
        starts = item_window_starts(now)
        view_start = inview_window_start(now)
        out = {}
        for nid in ids:
            clicks = tuple(sum(1 for _, i, at in self.clicks if i == nid and s <= at < end) for s in starts)
            views = sum(1 for i, at in self.inviews if i == nid and view_start <= at < end)
            if clicks[-1] or views:  # SQL 집계처럼 행이 없는 아이템은 결과에 없다
                out[nid] = WindowCounts(clicks, views)
        return out

    def state(self, user: int, now: datetime, candidate_ids) -> WorldState:
        return WorldState(
            category_ids=list(self.user_categories.get(user, [])),
            hist=self.hist(user, now),
            recent_clicks=self.recent_clicks(user, now),
            popularity=self.popularity(candidate_ids, now),
        )

    # --- 로그 재계산 경로의 입력
    def logs(self) -> ServiceLogs:
        items = list(self.items.values())
        return ServiceLogs(
            item_ids=np.array([it.news_letter_id for it in items]),
            item_emb=np.stack([it.embedding for it in items]),
            item_created_us=np.array([epoch_us(it.created_at) for it in items]),
            item_category=np.array([NO_CATEGORY if it.category_id is None else it.category_id for it in items]),
            click_user=np.array([c[0] for c in self.clicks], dtype=np.int64),
            click_item=np.array([c[1] for c in self.clicks], dtype=np.int64),
            click_us=np.array([epoch_us(c[2]) for c in self.clicks], dtype=np.int64),
            inview_item=np.array([v[0] for v in self.inviews], dtype=np.int64),
            inview_us=np.array([epoch_us(v[1]) for v in self.inviews], dtype=np.int64),
            user_categories=self.user_categories,
        )


def make_world(seed: int = 0, n_items: int = 90, n_users: int = 6, dim: int = 24, days: int = 12) -> World:
    """뉴스레터 n_items개와 사용자 n_users명의 클릭·노출 로그. 일부러 넣은 것:
    정규화되지 않은 임베딩, 카테고리 없는 아이템, 같은 초의 연속 클릭, 30분 안에 이어지는 세션,
    24시간에 20건을 넘는 사용자, 클릭이 전혀 없는 사용자."""
    rng = np.random.default_rng(seed)
    span = days * 86400
    items = {}
    for i in range(n_items):
        nid = 500 + i
        emb = rng.standard_normal(dim).astype(np.float32) * float(rng.uniform(0.3, 4.0))
        created = T0 + timedelta(seconds=int(rng.integers(0, span)), microseconds=int(rng.integers(0, 1_000_000)))
        items[nid] = WorldItem(nid, emb, created, None if i % 11 == 0 else int(rng.integers(1, 6)))
    world = World(items=items)
    ids = np.array(sorted(items))
    for user in range(1, n_users + 1):
        world.user_categories[user] = sorted(set(rng.integers(1, 6, int(rng.integers(0, 4))).tolist()))
        if user == n_users:
            continue  # 클릭 없는 사용자
        n_sessions = int(rng.integers(3, 9)) if user != 1 else 14
        for _ in range(n_sessions):
            at = T0 + timedelta(seconds=int(rng.integers(0, span)), microseconds=int(rng.integers(0, 1_000_000)))
            for _ in range(int(rng.integers(1, 6)) if user != 1 else int(rng.integers(8, 14))):
                world.clicks.append((user, int(rng.choice(ids)), at))
                if rng.random() < 0.15:  # 같은 초(때로는 같은 뉴스레터)의 연속 클릭
                    world.clicks.append((user, world.clicks[-1][1] if rng.random() < 0.5 else int(rng.choice(ids)),
                                         at + timedelta(microseconds=int(rng.integers(1, 900)))))
                at = at + timedelta(seconds=int(rng.integers(5, 1790)), microseconds=int(rng.integers(0, 1_000_000)))
    # 사용자 1은 마지막 하루에 클릭을 몰아서 24시간·20건 상한에 걸리게 한다
    burst = T0 + timedelta(seconds=span - 20 * 3600)
    for k in range(34):
        world.clicks.append((1, int(rng.choice(ids)), burst + timedelta(seconds=k * 1700, microseconds=k * 7)))
    for _ in range(4000):
        at = T0 + timedelta(seconds=int(rng.integers(0, span)), microseconds=int(rng.integers(0, 1_000_000)))
        world.inviews.append((int(rng.choice(ids)), at))
    return world
