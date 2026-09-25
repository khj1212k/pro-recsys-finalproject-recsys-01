import sys
import os
import types
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def _install_fake_news_embedder(monkeypatch, embedder_instance):
    """core.embedder는 torch/FlagEmbedding을 임포트 시점에 요구하므로(이 워크트리에는
    torch가 설치되어 있지 않음), 진짜 모듈을 임포트하지 않고 sys.modules에 가짜 모듈을
    끼워넣어 `from core.embedder import NewsEmbedder`가 이를 집어가도록 한다."""
    fake_module = types.ModuleType("core.embedder")
    fake_module.NewsEmbedder = MagicMock(return_value=embedder_instance)
    monkeypatch.setitem(sys.modules, "core.embedder", fake_module)
    return fake_module.NewsEmbedder


def _make_embedder_double(generate_side_effect=None, generate_return=None):
    embedder = MagicMock()
    embedder.__enter__ = MagicMock(return_value=embedder)
    embedder.__exit__ = MagicMock(return_value=False)
    if generate_side_effect is not None:
        embedder.generate_embeddings_batch.side_effect = generate_side_effect
    else:
        embedder.generate_embeddings_batch.return_value = generate_return
    return embedder


def _make_cursor(rows):
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    # 첫 execute -> rows(SELECT), 두번째 execute -> COUNT(*) 조회에 대한 fetchone
    cursor.fetchall.return_value = rows
    cursor.fetchone.return_value = (0,)
    return cursor


def test_query_excludes_rows_with_empty_or_null_content(monkeypatch):
    """raw_news_content가 비어있거나 NULL인 행은 임베딩 대상 조회에서 제외돼야 한다."""
    from pipeline.stages import Stage3_NewsEmbedding

    embedder = _make_embedder_double(generate_return=([], 0.0))
    _install_fake_news_embedder(monkeypatch, embedder)

    cursor = _make_cursor(rows=[])  # 대상 없음
    conn = MagicMock()
    conn.cursor.return_value = cursor

    settings = MagicMock()
    settings.EMBEDDING_BATCH_SIZE = 8

    with patch("db.connection.get_connection", return_value=conn), \
         patch("db.connection.release_connection"):
        stage = Stage3_NewsEmbedding(settings=settings)
        count = stage.execute()

    assert count == 0
    select_sql = cursor.execute.call_args_list[0][0][0]
    assert "raw_news_content IS NOT NULL" in select_sql
    assert "raw_news_content != ''" in select_sql
    assert "embedding_result IS NULL" in select_sql


def test_failed_batch_is_logged_and_skipped_run_continues(monkeypatch):
    """한 배치의 임베딩 생성이 실패해도 나머지 배치는 계속 처리돼야 한다
    (이전에는 예외가 전체 루프를 중단시켰음)."""
    from pipeline.stages import Stage3_NewsEmbedding

    # 1번째 배치는 예외, 2번째 배치는 성공
    embedder = _make_embedder_double(
        generate_side_effect=[
            Exception("embedding backend timeout"),
            ([[0.1, 0.2], [0.3, 0.4]], 0.01),
        ]
    )
    _install_fake_news_embedder(monkeypatch, embedder)

    rows = [
        (1, "제목1", "본문1"),
        (2, "제목2", "본문2"),
        (3, "제목3", "본문3"),
        (4, "제목4", "본문4"),
    ]
    cursor = _make_cursor(rows=rows)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    settings = MagicMock()
    settings.EMBEDDING_BATCH_SIZE = 2

    with patch("db.connection.get_connection", return_value=conn), \
         patch("db.connection.release_connection"):
        stage = Stage3_NewsEmbedding(settings=settings)
        count = stage.execute(batch_size=2)

    # 실패한 1번째 배치(2건)는 카운트되지 않고, 성공한 2번째 배치(2건)만 반영
    assert count == 2
    # 실패 배치에서 rollback, 성공 배치에서 commit이 각각 호출돼야 함
    assert conn.rollback.called
    assert conn.commit.called
    # executemany는 성공한 배치에서만 호출된다 (1회)
    assert cursor.executemany.call_count == 1


def test_all_batches_succeed_commits_per_batch(monkeypatch):
    """정상 케이스: 모든 배치가 성공하면 배치마다 커밋되고 전체 건수가 집계된다."""
    from pipeline.stages import Stage3_NewsEmbedding

    embedder = _make_embedder_double(
        generate_side_effect=[
            ([[0.1], [0.2]], 0.01),
            ([[0.3], [0.4]], 0.01),
        ]
    )
    _install_fake_news_embedder(monkeypatch, embedder)

    rows = [
        (1, "제목1", "본문1"),
        (2, "제목2", "본문2"),
        (3, "제목3", "본문3"),
        (4, "제목4", "본문4"),
    ]
    cursor = _make_cursor(rows=rows)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    settings = MagicMock()
    settings.EMBEDDING_BATCH_SIZE = 2

    with patch("db.connection.get_connection", return_value=conn), \
         patch("db.connection.release_connection"):
        stage = Stage3_NewsEmbedding(settings=settings)
        count = stage.execute(batch_size=2)

    assert count == 4
    assert cursor.executemany.call_count == 2
    assert conn.commit.call_count == 2
    assert not conn.rollback.called
