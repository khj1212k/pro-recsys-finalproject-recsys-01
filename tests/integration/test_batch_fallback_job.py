"""jobs/tasks/batch_fallback.py를 실제 DB에서 검증: 오늘 배치가 없는 사용자만 채우고,
재실행해도 행이 늘지 않는다."""
import argparse
import uuid

from jobs.runtime import JobContext


def test_batch_fallback_fills_missing_users_once_with_preferred_first_and_seen_removed(database_url, pg_conn):
    from jobs.tasks import batch_fallback

    suffix = uuid.uuid4().hex[:8]
    created = {"newsletters": [], "categories": [], "user": None, "ranking": None}
    try:
        with pg_conn.cursor() as cur:
            for code_offset, name in enumerate(("정치", "경제")):
                cur.execute(
                    "INSERT INTO category (category_name, category_code) VALUES (%s, %s) RETURNING category_id",
                    (f"{name}-{suffix}", 9000 + code_offset + int(suffix[:4], 16) % 1000 * 2),
                )
                created["categories"].append(cur.fetchone()[0])
            politics, economy = created["categories"]

            for i, cat in enumerate((politics, economy, economy, politics)):
                cur.execute(
                    "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                    "news_letter_keywords, raw_news_count, news_letter_created_at) "
                    "VALUES (%s, 's', 'c', '[]', 1, NOW()) RETURNING news_letter_id",
                    (f"nl-{suffix}-{i}",),
                )
                nid = cur.fetchone()[0]
                created["newsletters"].append(nid)
                cur.execute(
                    "INSERT INTO news_letter_categories (news_letter_id, category_id) VALUES (%s, %s)", (nid, cat)
                )
            n0, n1, n2, n3 = created["newsletters"]

            cur.execute(
                "INSERT INTO news_letters_category (news_letter_ids, created_at) VALUES (%s, NOW()) "
                "RETURNING news_letter_batch_id",
                (f"[{n0}, {n1}, {n2}, {n3}]",),
            )
            created["ranking"] = cur.fetchone()[0]

            cur.execute(
                'INSERT INTO "user" (user_email, user_password_hash, user_nickname, user_created_at) '
                "VALUES (%s, 'hash', %s, NOW()) RETURNING user_id",
                (f"fallback-{suffix}@example.com", f"폴백-{suffix}"),
            )
            user_id = created["user"] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_preferred_categories (category_id, user_id) VALUES (%s, %s)", (economy, user_id)
            )
            cur.execute(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) VALUES (%s, %s, NOW())",
                (user_id, n2),
            )

        ctx = JobContext(job="batch_fallback", args=argparse.Namespace())
        stats = batch_fallback.run(ctx)
        again = batch_fallback.run(JobContext(job="batch_fallback", args=argparse.Namespace()))

        assert stats["ranking_batch_id"] == created["ranking"]
        assert stats["users_filled"] >= 1
        with pg_conn.cursor() as cur:
            cur.execute("SELECT news_letter_ids FROM news_letter_today_batch WHERE user_id = %s", (user_id,))
            rows = cur.fetchall()
        assert len(rows) == 1  # 재실행해도 하루 한 번만
        # 선호(경제) 중 클릭 안 한 n1이 먼저, 나머지는 인기도 순서, 클릭한 n2는 제외
        assert rows[0][0] == [n1, n0, n3]
        assert again["users_filled"] == 0
    finally:
        with pg_conn.cursor() as cur:
            if created["user"]:
                cur.execute("DELETE FROM news_letter_today_batch WHERE user_id = %s", (created["user"],))
                cur.execute("DELETE FROM user_newsletter_ctr_log WHERE user_id = %s", (created["user"],))
                cur.execute("DELETE FROM user_preferred_categories WHERE user_id = %s", (created["user"],))
                cur.execute('DELETE FROM "user" WHERE user_id = %s', (created["user"],))
            if created["ranking"]:
                cur.execute("DELETE FROM news_letters_category WHERE news_letter_batch_id = %s", (created["ranking"],))
            if created["newsletters"]:
                cur.execute("DELETE FROM news_letter_categories WHERE news_letter_id = ANY(%s)", (created["newsletters"],))
                cur.execute("DELETE FROM news_letter WHERE news_letter_id = ANY(%s)", (created["newsletters"],))
            if created["categories"]:
                cur.execute("DELETE FROM category WHERE category_id = ANY(%s)", (created["categories"],))
