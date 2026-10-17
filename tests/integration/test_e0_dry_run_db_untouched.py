"""E0 러너는 실제 DB가 손닿는 곳에 있어도 쓰지도 읽지도 않는다 (ADR 0036 C′, ADR 0009 A8.2 이탈 1).

DB 차단 가드는 등록하지 않아 끈 채로 돈다(forbid_database=False, A8.10 덧붙임 2026-10-17). 그래서 "대역이 모든 접촉점을
막는다"를 실제 Postgres에 접속 정보가 맞춰진 상태에서 확인한다: Settings.DB_*가 CI의 테스트 DB를 가리키는 채로 합성 드라이
런(페이크 LLM)을 끝까지 돌리고, 그 동안 db.connection의 풀이 만들어지지 않았는지와 news_raw·news_letter·cluster_history의
행 수가 그대로인지 본다. 기사 문장은 지어낸 것이다.
"""
from datetime import datetime, timedelta, timezone

import pytest

from evaluation.llm import e0_baseline as base

KST = timezone(timedelta(hours=9))


def _counts(pg_conn):
    out = {}
    with pg_conn.cursor() as cur:
        for table in ("news_raw", "news_letter", "cluster_history"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def test_dry_run_with_a_reachable_database_creates_no_pool_and_writes_no_rows(database_url, pg_conn, tmp_path,
                                                                              monkeypatch):
    import core.llm.adapters as adapters
    from db import connection

    monkeypatch.setattr(adapters.OpenAICompatLLMClient, "_wait_before_retry", staticmethod(lambda b, s: True))
    connection.close_pool()
    before = _counts(pg_conn)
    t0 = datetime.now(timezone.utc).astimezone(KST).replace(microsecond=0)
    out = base.dry_run(tmp_path, t0=t0, run_report=False)

    assert out["summary"]["n_processed"] == 15 and out["summary"]["db_clean"] is True
    assert connection._db_pool is None          # 풀이 만들어진 적이 없다 - 접속을 한 번도 시도하지 않았다
    assert _counts(pg_conn) == before
