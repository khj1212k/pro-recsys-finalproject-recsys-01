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
                "news_letter_embedding, news_letter_keywords, raw_news_count) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING news_letter_id",
                (f"뉴스레터-{suffix}", "요약", "내용", seed_vector, "[]", 1),
            )
            news_letter_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO \"user\" (user_email, user_password_hash, user_nickname) "
                "VALUES (%s, %s, %s) RETURNING user_id",
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
