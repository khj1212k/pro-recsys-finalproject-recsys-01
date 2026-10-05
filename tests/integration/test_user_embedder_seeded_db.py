"""ai_workspace/core/user_embedder.py의 UserEmbedder.batch_update_all_users()가
raw-SQL(db.connection.get_connection())로 news_letter_embedding을 읽고
user.user_embedding을 계산해 쓰는 전체 경로를, 작게 시드한 실제 DB에서 검증한다.
BGE-M3 등 실제 임베딩 모델은 호출하지 않고 news_letter.news_letter_embedding을
직접 숫자 벡터로 시드해 대신한다("mock the embedding model").
"""
import uuid

import numpy as np
import pytest


def test_user_embedder_batch_update_all_users_on_seeded_db(database_url, pg_conn):
    from core.user_embedder import UserEmbedder, _to_vector_array

    dim = 1024
    seed_vector = [0.0] * dim
    seed_vector[0] = 1.0  # 단위 벡터 [1, 0, 0, ...] - 실제 임베딩 모델 없이 손으로 채운 값

    suffix = uuid.uuid4().hex[:8]
    news_letter_id = None
    user_id = None
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                "news_letter_embedding, news_letter_keywords, raw_news_count, news_letter_created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, NOW()) RETURNING news_letter_id",
                (f"뉴스레터-{suffix}", "요약", "내용", seed_vector, "[]", 1),
            )
            news_letter_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO \"user\" (user_email, user_password_hash, user_nickname, user_created_at) "
                "VALUES (%s, %s, %s, NOW()) RETURNING user_id",
                (f"user-{suffix}@example.com", "hash", f"유저-{suffix}"),
            )
            user_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO user_preferred_newsletter (news_letter_id, user_id) VALUES (%s, %s)",
                (news_letter_id, user_id),
            )

        stats = UserEmbedder().batch_update_all_users()

        assert stats["success"] >= 1
        assert stats["failed"] == 0

        with pg_conn.cursor() as cur:
            cur.execute('SELECT user_embedding FROM "user" WHERE user_id = %s', (user_id,))
            (stored,) = cur.fetchone()

        arr = _to_vector_array(stored)
        assert arr is not None
        assert arr.shape == (dim,)
        # 선호 뉴스레터가 하나뿐이므로, 정규화된 결과는 원본 벡터와 방향이 같아야 한다
        np.testing.assert_allclose(arr, seed_vector, atol=1e-5)
        assert float(np.linalg.norm(arr)) == pytest.approx(1.0, abs=1e-4)
    finally:
        with pg_conn.cursor() as cur:
            if user_id:
                cur.execute("DELETE FROM user_preferred_newsletter WHERE user_id = %s", (user_id,))
                cur.execute('DELETE FROM "user" WHERE user_id = %s', (user_id,))
            if news_letter_id:
                cur.execute("DELETE FROM news_letter WHERE news_letter_id = %s", (news_letter_id,))


def test_refresh_recently_active_users_updates_only_recently_active_users(database_url, pg_conn):
    """이미 장기 벡터가 있는 사용자라도 since 이후 클릭이 있으면 다시 계산되고, 최근
    활동이 없는 사용자의 벡터는 그대로 남아야 한다."""
    from datetime import datetime, timedelta, timezone

    from core.user_embedder import UserEmbedder, _to_vector_array

    dim = 1024
    e0 = [0.0] * dim
    e0[0] = 1.0
    e1 = [0.0] * dim
    e1[1] = 1.0
    suffix = uuid.uuid4().hex[:8]
    nl_id, active_uid, idle_uid = None, None, None
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                "news_letter_embedding, news_letter_keywords, raw_news_count, news_letter_created_at) "
                "VALUES (%s, '요약', '내용', %s, '[]', 1, NOW()) RETURNING news_letter_id",
                (f"refresh-{suffix}", e1),
            )
            nl_id = cur.fetchone()[0]
            uids = []
            for tag in ("active", "idle"):
                cur.execute(
                    'INSERT INTO "user" (user_email, user_password_hash, user_nickname, user_created_at, '
                    "user_embedding) VALUES (%s, 'h', 'n', NOW(), %s) RETURNING user_id",
                    (f"{tag}-{suffix}@example.com", e0),
                )
                uids.append(cur.fetchone()[0])
            active_uid, idle_uid = uids
            cur.execute(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) VALUES (%s, %s, %s)",
                (active_uid, nl_id, datetime.now(timezone.utc) - timedelta(minutes=10)),
            )
            cur.execute(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) VALUES (%s, %s, %s)",
                (idle_uid, nl_id, datetime.now(timezone.utc) - timedelta(days=3)),
            )

        stats = UserEmbedder().refresh_recently_active_users(
            datetime.now(timezone.utc) - timedelta(hours=1)
        )

        assert stats["success"] >= 1 and stats["failed"] == 0
        with pg_conn.cursor() as cur:
            cur.execute('SELECT user_id, user_embedding FROM "user" WHERE user_id = ANY(%s)', ([active_uid, idle_uid],))
            vecs = {uid: _to_vector_array(v) for uid, v in cur.fetchall()}
        # 유일한 클릭이 e1 방향이므로 다시 계산된 벡터는 e1, 활동 없는 사용자는 e0 그대로
        np.testing.assert_allclose(vecs[active_uid], e1, atol=1e-5)
        np.testing.assert_allclose(vecs[idle_uid], e0, atol=1e-5)
    finally:
        with pg_conn.cursor() as cur:
            for uid in (active_uid, idle_uid):
                if uid:
                    cur.execute("DELETE FROM user_newsletter_ctr_log WHERE user_id = %s", (uid,))
                    cur.execute('DELETE FROM "user" WHERE user_id = %s', (uid,))
            if nl_id:
                cur.execute("DELETE FROM news_letter WHERE news_letter_id = %s", (nl_id,))
