"""evaluation/llm/evalset.py::load_candidates_from_db를 실제 Postgres 스키마
(alembic upgrade head)에 대고 검증한다. cluster_history.cluster_log(json)의
클러스터 키/메타/결과 파싱과, 뉴스레터 카테고리 최빈값 조인이 대상이다.

TEST_DATABASE_URL/DATABASE_URL로 접속할 수 없으면 스킵한다(CI의 integration 잡에서 실행).
"""
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def conn():
    import psycopg2

    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL/DATABASE_URL 미설정")
    try:
        c = psycopg2.connect(url, connect_timeout=3)
    except psycopg2.OperationalError as e:
        pytest.skip(f"DB 접속 불가: {e}")
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


def test_load_candidates_from_db_reads_cluster_log_and_newsletter_categories(conn):
    from evaluation.llm.evalset import UNCATEGORIZED, load_candidates_from_db

    suffix = uuid.uuid4().hex[:10]
    run_id = int(uuid.uuid4().int % 900_000_000) + 100_000_000
    ids = {}
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"테스트일보-{suffix}",))
            press_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO category (category_name, category_code) VALUES (%s, %s) RETURNING category_id",
                (f"경제-{suffix}", run_id),
            )
            category_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                "news_letter_keywords, raw_news_count, news_letter_created_at) "
                "VALUES ('t', 's', 'c', '[]', 3, NOW()) RETURNING news_letter_id"
            )
            letter_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO news_letter_categories (news_letter_id, category_id) VALUES (%s, %s)",
                (letter_id, category_id),
            )
            article_ids = []
            for k in range(7):
                cur.execute(
                    "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, "
                    "raw_news_created_at, raw_news_crawled_at, news_letter_id) "
                    "VALUES (%s, %s, %s, %s, %s, NOW(), %s) RETURNING raw_news_id",
                    (press_id, f"제목{k}", f"본문{k} 내용", f"https://news.example/{suffix}/{k}",
                     "2026-09-26T09:00:00+09:00", letter_id if k < 3 else None),
                )
                article_ids.append(cur.fetchone()[0])
            ids.update(press=press_id, category=category_id, letter=letter_id, articles=article_ids)

            log = {
                "0": article_ids[:3],
                "1": article_ids[3:7],
                "clustering_stats": {"n_articles": 7},
                "cluster_meta": {"0": {"split_v2": False}, "1": {"split_v2": True}},
                "cluster_outcomes": {"1": {"status": "skipped",
                                           "cluster_eval": {"decision": "FAIL", "confidence": 0.9}}},
            }
            cur.execute(
                "INSERT INTO cluster_history (run_id, cluster_log, created_at) VALUES (%s, %s, NOW()) "
                "RETURNING history_id",
                (run_id, json.dumps(log, ensure_ascii=False)),
            )
            ids["history"] = cur.fetchone()[0]

        candidates, articles = load_candidates_from_db(conn)

        mine = {c.cluster_id: c for c in candidates if c.run_id == run_id}
        assert set(mine) == {0, 1}
        assert mine[0].category == f"경제-{suffix}"
        assert mine[0].split_v2 == "no" and mine[0].hard_case is False
        assert mine[1].category == UNCATEGORIZED
        assert mine[1].split_v2 == "yes" and mine[1].hard_case is True
        assert articles[article_ids[0]].body == "본문0 내용"
        assert articles[article_ids[0]].press_name == f"테스트일보-{suffix}"
    finally:
        with conn.cursor() as cur:
            if "history" in ids:
                cur.execute("DELETE FROM cluster_history WHERE history_id = %s", (ids["history"],))
            if "articles" in ids:
                cur.execute("DELETE FROM news_raw WHERE raw_news_id = ANY(%s)", (ids["articles"],))
            if "letter" in ids:
                cur.execute("DELETE FROM news_letter_categories WHERE news_letter_id = %s", (ids["letter"],))
                cur.execute("DELETE FROM news_letter WHERE news_letter_id = %s", (ids["letter"],))
            if "category" in ids:
                cur.execute("DELETE FROM category WHERE category_id = %s", (ids["category"],))
            if "press" in ids:
                cur.execute("DELETE FROM press WHERE press_id = %s", (ids["press"],))
