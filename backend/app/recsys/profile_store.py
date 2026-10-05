"""장기 프로필의 증분 상태(user_profile_state)를 읽고 쓰는 SQL (ADR 0033).

상태는 클릭 로그의 캐시다: 클릭 로그에서 언제든 다시 만들 수 있고(rebuild_user), 클릭 API가 클릭 행을
쓰는 트랜잭션 안에서 한 번 갱신한다(apply_click). 계산은 전부 recsys_core.profile이 한다 - 여기는 행을
잠그고, 읽고, 쓰기만 한다.

- 이벤트 = event = 'click'인 클릭 로그 행 중 임베딩이 있는 뉴스레터의 것. 임베딩이 없는 뉴스레터의 클릭은
  더할 벡터가 없으므로 상태에 들어가지 않는다(오프라인 재계산 경로도 같은 규칙으로 뺀다).
- hist_sum은 float64 little-endian 바이트다. pgvector의 vector는 float32라 클릭 수백 건이 쌓이면 갱신마다의
  반올림이 방향 오차로 남는다.
- 같은 사용자의 갱신은 상태 행의 잠금(SELECT ... FOR UPDATE)으로 줄을 선다.
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import text

from recsys_core.profile import NO_CATEGORY, HistState, apply_event, rebuild, same_state
from recsys_core.serving import epoch_seconds

from app.recsys.pgvector_io import vector_from_send
from app.recsys.types import ProfileState

STATE_COLUMNS = "hist_sum, hist_anchor_ts, hist_len, hist_cat_counts"

_LOCK_SQL = text(f"SELECT {STATE_COLUMNS} FROM user_profile_state WHERE user_id = :uid FOR UPDATE")
_INSERT_SQL = text("INSERT INTO user_profile_state (user_id) VALUES (:uid) ON CONFLICT (user_id) DO NOTHING")
_UPDATE_SQL = text(
    "UPDATE user_profile_state SET hist_sum = :hist_sum, hist_anchor_ts = :anchor, hist_len = :hist_len, "
    "hist_cat_counts = CAST(:counts AS json), updated_at = now() WHERE user_id = :uid"
)
# 대표 카테고리 = 매핑된 카테고리 ID 중 가장 작은 것(app.recsys.sql_repository.items와 같은 규칙)
_ITEM_SQL = text(
    "SELECT vector_send(n.news_letter_embedding), "
    "       (SELECT MIN(c.category_id) FROM news_letter_categories c WHERE c.news_letter_id = n.news_letter_id) "
    "FROM news_letter n WHERE n.news_letter_id = :nid"
)
_EVENTS_SQL = text(
    "SELECT l.created_at::timestamptz, vector_send(n.news_letter_embedding), "
    "       (SELECT MIN(c.category_id) FROM news_letter_categories c WHERE c.news_letter_id = n.news_letter_id) "
    "FROM user_newsletter_ctr_log l JOIN news_letter n ON n.news_letter_id = l.news_letter_id "
    "WHERE l.user_id = :uid AND l.event = 'click' AND n.news_letter_embedding IS NOT NULL "
    "ORDER BY l.created_at, l.log_id"
)
_USERS_SQL = text(
    "SELECT user_id FROM user_newsletter_ctr_log WHERE event = 'click' "
    "UNION SELECT user_id FROM user_profile_state ORDER BY user_id"
)


def encode_hist_sum(vec: Optional[np.ndarray]) -> Optional[bytes]:
    return None if vec is None else np.asarray(vec, dtype="<f8").tobytes()


def decode_hist_sum(buf) -> Optional[np.ndarray]:
    return None if buf is None else np.frombuffer(bytes(buf), dtype="<f8").copy()


def encode_cat_counts(counts: Dict[int, int]) -> str:
    return json.dumps({str(int(k)): int(v) for k, v in sorted(counts.items())})


def decode_cat_counts(value: Any) -> Dict[int, int]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return {int(k): int(v) for k, v in value.items()}


def state_from_row(hist_sum, anchor_ts: Optional[datetime], hist_len: Optional[int], cat_counts) -> ProfileState:
    """user_profile_state의 (hist_sum, hist_anchor_ts, hist_len, hist_cat_counts) -> ProfileState."""
    vec = decode_hist_sum(hist_sum)
    if vec is None or anchor_ts is None or not hist_len:
        return ProfileState()
    hist = HistState(
        hist_sum=vec,
        anchor_s=epoch_seconds(anchor_ts),
        hist_len=int(hist_len),
        cat_counts=decode_cat_counts(cat_counts),
    )
    return ProfileState(hist=hist, last_event_at=anchor_ts)


def _locked_state(conn, user_id: int, create: bool) -> ProfileState:
    row = conn.execute(_LOCK_SQL, {"uid": user_id}).first()
    if row is None and create:
        conn.execute(_INSERT_SQL, {"uid": user_id})
        row = conn.execute(_LOCK_SQL, {"uid": user_id}).first()
    return ProfileState() if row is None else state_from_row(*row)


def _write(conn, user_id: int, hist: HistState, last_event_at: Optional[datetime]) -> None:
    conn.execute(
        _UPDATE_SQL,
        {
            "uid": user_id,
            "hist_sum": None if hist.empty else encode_hist_sum(hist.hist_sum),
            "anchor": None if hist.empty else last_event_at,
            "hist_len": 0 if hist.empty else hist.hist_len,
            "counts": None if hist.empty else encode_cat_counts(hist.cat_counts),
        },
    )


def apply_click(conn, user_id: int, news_letter_id: int, clicked_at: datetime) -> bool:
    """클릭 하나를 상태에 반영한다. conn은 클릭 행을 쓴 그 트랜잭션의 커넥션이다.

    임베딩이 없는(또는 없는 ID의) 뉴스레터면 아무것도 하지 않고 False를 돌려준다. PostgreSQL이 아닌
    연결(단위 테스트의 SQLite)에서도 아무것도 하지 않는다 - 이 SQL은 PostgreSQL 전용이다."""
    if conn.dialect.name != "postgresql":
        return False
    item = conn.execute(_ITEM_SQL, {"nid": news_letter_id}).first()
    if item is None or item[0] is None:
        return False
    state = _locked_state(conn, user_id, create=True)
    category = NO_CATEGORY if item[1] is None else int(item[1])
    hist = apply_event(state.hist, epoch_seconds(clicked_at), vector_from_send(item[0]), category)
    last = clicked_at if state.last_event_at is None else max(state.last_event_at, clicked_at)
    _write(conn, user_id, hist, last)
    return True


def recompute(conn, user_id: int) -> Tuple[HistState, Optional[datetime]]:
    """클릭 로그 전체에서 정의식으로 다시 계산한 상태와 마지막 이벤트 시각."""
    rows = conn.execute(_EVENTS_SQL, {"uid": user_id}).fetchall()
    hist = rebuild(
        (epoch_seconds(at), vector_from_send(vec), NO_CATEGORY if cat is None else int(cat))
        for at, vec, cat in rows
    )
    return hist, (max(r[0] for r in rows) if rows else None)


def rebuild_user(conn, user_id: int, write: bool = True) -> str:
    """한 사용자의 상태를 클릭 로그와 맞춘다. 한 트랜잭션 안에서 부른다.

    반환: 'unchanged'(저장된 상태가 로그와 같다) | 'rebuilt'(달라서 다시 썼다) | 'mismatch'(다르지만
    write=False라 쓰지 않았다). 상태 행을 잠근 뒤 로그를 읽으므로 그 사이의 클릭 갱신과 엇갈리지 않는다."""
    stored = _locked_state(conn, user_id, create=write)
    fresh, last = recompute(conn, user_id)
    if same_state(stored.hist, fresh) and (fresh.empty or stored.last_event_at == last):
        return "unchanged"
    if not write:
        return "mismatch"
    _write(conn, user_id, fresh, last)
    return "rebuilt"


def users_to_rebuild(conn) -> List[int]:
    """클릭이 있거나 상태 행이 있는 사용자 전부."""
    return [int(r[0]) for r in conn.execute(_USERS_SQL).fetchall()]
