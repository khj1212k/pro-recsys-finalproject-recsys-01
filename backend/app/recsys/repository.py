from datetime import datetime
from typing import Dict, List, Optional, Protocol, Sequence, Set, Tuple

import numpy as np

from app.recsys.types import ClickEvent, Item, NewsletterMeta, ProfileState, WindowCounts


class RecsysRepository(Protocol):
    """요청 시점 추천이 읽는 모든 데이터 접근. SQL 구현은 sql_repository.py,
    단위 테스트는 메모리 fake를 쓴다. 시각 인자는 모두 tz-aware(UTC)다.

    recent_ids / window_meta / items는 화면에 내보낼 수 있는(카테고리 매핑이 있는) 뉴스레터만
    돌려준다. 그래야 추천 ID가 비어 있지 않으면 응답 본문도 비어 있지 않다."""

    def last_click_id(self, user_id: int) -> Optional[int]: ...

    def profile_state(self, user_id: int) -> Tuple[ProfileState, List[int]]:
        """(장기 프로필의 증분 상태, 온보딩에서 고른 선호 카테고리 ID). 상태 행이 없으면 빈 상태다."""
        ...

    def recent_clicks(
        self, user_id: int, since: datetime, until: datetime, limit: int
    ) -> List[ClickEvent]:
        """[since, until) 구간의 클릭(event = 'click', 임베딩이 있는 뉴스레터) 중 가장 늦은 limit개."""
        ...

    def item_window_counts(
        self,
        news_letter_ids: Sequence[int],
        click_starts: Sequence[datetime],
        inview_start: datetime,
        end: datetime,
    ) -> Dict[int, WindowCounts]:
        """아이템별 인기도 창 집계(ADR 0033). 클릭은 창마다 [click_starts[j], end), 노출은 [inview_start, end).
        모든 사용자의 클릭·노출을 센다. 어느 창에도 행이 없는 아이템은 결과에 없다."""
        ...

    def onboarding_vector(self, user_id: int) -> Optional[np.ndarray]: ...

    def category_centroid(self, category_ids: Sequence[int], since: datetime) -> Optional[np.ndarray]: ...

    def knn_ids(self, query: np.ndarray, since: datetime, k: int) -> List[int]: ...

    def recent_ids(self, n: int) -> List[int]: ...

    def window_meta(self, since: datetime) -> List[NewsletterMeta]: ...

    def category_recent_ids(self, category_ids: Sequence[int], since: datetime, n: int) -> List[int]: ...

    def clicked_among(self, user_id: int, news_letter_ids: Sequence[int]) -> Set[int]: ...

    def fatigued_among(
        self, user_id: int, news_letter_ids: Sequence[int], since: datetime, min_impressions: int
    ) -> Set[int]:
        """news_letter_ids 중 since 이후 이 사용자에게 min_impressions번 이상 노출된 것(ADR 0025).
        이미 클릭한 항목을 뺀 목록을 넘기므로 "노출됐지만 클릭되지 않은 것"이 된다."""
        ...

    def displayable_among(self, news_letter_ids: Sequence[int]) -> Set[int]: ...

    def items(self, news_letter_ids: Sequence[int]) -> Dict[int, Item]: ...

    def latest_batch(self, user_id: int) -> Optional[Tuple[datetime, List[int]]]: ...

    def rollback(self) -> None:
        """실패한 조회 뒤에 같은 저장소로 다음 조회를 할 수 있게 트랜잭션을 되돌린다."""
        ...
