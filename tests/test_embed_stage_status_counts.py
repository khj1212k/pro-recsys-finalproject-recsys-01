# embed 단계가 "본문이 없어 임베딩하지 않은" 기사를 세는 조건이 새 추출 상태를 알고 있는지 확인한다(ADR 0026 2026-10-25):
# - 'blocked_origin'(출발지 거절로 본문 없이 닫은 행)은 영구 제외라 no_content에 센다 - 안 그러면 어느 칸에도 안 잡힌다
# - 보존 기한으로 본문을 비운 'rss_summary' 행은 'ok' 행처럼 body_purged에 센다(임베딩 전에 비워지면 영영 못 한다)
# 실제 DB에서 같은 조건을 도는 확인은 tests/integration/test_embedding_stage_status_counts_db.py.
import os
import re
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def _count_sql():
    from pipeline.stages import embed_pending_articles

    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = (0, 0, 0)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    settings = MagicMock()
    settings.EMBEDDING_BATCH_SIZE = 2
    settings.MAX_EXTRACT_ATTEMPTS = 3
    with patch("db.connection.get_connection", return_value=conn), patch("db.connection.release_connection"):
        embed_pending_articles(settings)
    return cursor.execute.call_args_list[1][0][0]


def _filters(sql):
    return re.findall(r"COUNT\(\*\) FILTER \(\s*WHERE(.*?)\)\s*(?:,|FROM)", sql, flags=re.S)


def test_blocked_origin_is_counted_as_no_content_not_awaiting():
    awaiting, no_content, body_purged = _filters(_count_sql())
    assert "'blocked_origin'" in no_content
    assert "'blocked_origin'" not in awaiting and "'blocked_origin'" not in body_purged


def test_purged_rss_summary_rows_are_counted_as_body_purged():
    awaiting, no_content, body_purged = _filters(_count_sql())
    assert "'rss_summary'" in body_purged and "'ok'" in body_purged
    assert "'rss_summary'" not in awaiting and "'rss_summary'" not in no_content
