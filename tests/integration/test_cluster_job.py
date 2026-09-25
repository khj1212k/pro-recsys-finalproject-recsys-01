"""`python -m jobs.run cluster`를 실제 Postgres에서 검증한다.

db.connection 풀 연결에는 register_vector가 걸려 있어 embedding_result가 pgvector.Vector로
온다 - 이 경로를 실제로 거쳐야 parse_embedding 회귀(0차원 object 배열 -> HDBSCAN TypeError)를
잡을 수 있다.
"""
import uuid

import numpy as np

DIM = 1024


def _unit(rng, center_axis):
    v = np.zeros(DIM, dtype=np.float32)
    v[center_axis] = 1.0
    v += rng.normal(0.0, 0.01, DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def test_cluster_job_reads_vectors_through_pooled_connection_and_records_stats(
    database_url, pg_conn, isolated_news_raw
):
    from jobs.run import main

    rng = np.random.default_rng(0)
    suffix = uuid.uuid4().hex[:8]
    titles = {0: "반도체 수출 증가 전망", 1: "태풍 북상 대비 비상근무"}
    with pg_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM job_runs")
        (last_run_before,) = cur.fetchone()
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"itest-cluster-{suffix}",))
        press_id = cur.fetchone()[0]
    try:
        with pg_conn.cursor() as cur:
            for axis in (0, 1):
                for i in range(6):
                    cur.execute(
                        """
                        INSERT INTO news_raw (press_id, raw_news_url, raw_news_title, raw_news_content,
                                              raw_news_created_at, raw_news_crawled_at, raw_news_extract_status,
                                              embedding_result)
                        VALUES (%s, %s, %s, %s, now(), now(), 'ok', %s::vector)
                        """,
                        (
                            press_id, f"http://itest.example.com/{suffix}/{axis}/{i}",
                            f"{titles[axis]} {i}", "본문 " * 50,
                            "[" + ",".join(f"{x:.6f}" for x in _unit(rng, axis)) + "]",
                        ),
                    )

        assert main(["cluster"]) == 0

        with pg_conn.cursor() as cur:
            cur.execute("SELECT id, status, stats, error FROM job_runs WHERE job = 'cluster' ORDER BY id DESC LIMIT 1")
            _, status, stats, error = cur.fetchone()
        assert status == "succeeded", error
        clustering = stats["clustering"]
        assert clustering["n_articles"] == 12
        assert clustering["n_clusters"] == 2
        assert clustering["clustered_articles"] == 12
        assert clustering["cluster_sizes_top10"] == [6, 6]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM job_runs WHERE job = 'cluster' AND id > %s", (last_run_before,))
