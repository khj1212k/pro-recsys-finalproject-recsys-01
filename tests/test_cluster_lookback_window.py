import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def _make_conn_returning_no_rows():
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_load_data_from_db_default_lookback_is_24_hours_and_parameterized():
    """기본 동작 보존: lookback_hours를 지정하지 않으면 24시간이 사용되고,
    SQL에는 문자열로 끼워넣지 않고 %s 파라미터로 바인딩돼야 한다."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    conn, cursor = _make_conn_returning_no_rows()

    with patch("db.connection.get_connection", return_value=conn), \
         patch("db.connection.release_connection"):
        clusterer = NewsClusterer()
        clusterer._load_data_from_db()

    query, params = cursor.execute.call_args[0]
    assert "%s" in query
    assert "INTERVAL '1 hour'" in query
    # SQL 텍스트 안에 시간 숫자가 직접 문자열로 박혀있으면 안 된다 (파라미터화 확인)
    assert "24 hours" not in query
    assert params == (24,)


def test_load_data_from_db_custom_lookback_hours_is_passed_as_param():
    """carry-over 윈도우(예: 48~72시간)를 활성화하면 그 값이 그대로 쿼리 파라미터로 전달돼야 한다."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    conn, cursor = _make_conn_returning_no_rows()

    with patch("db.connection.get_connection", return_value=conn), \
         patch("db.connection.release_connection"):
        clusterer = NewsClusterer()
        clusterer._load_data_from_db(lookback_hours=72)

    _, params = cursor.execute.call_args[0]
    assert params == (72,)


def test_cluster_news_threads_lookback_hours_into_query():
    """cluster_news(lookback_hours=...)를 통해서도 lookback 값이 실제 쿼리까지 도달해야 한다."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    conn, cursor = _make_conn_returning_no_rows()

    with patch("db.connection.get_connection", return_value=conn), \
         patch("db.connection.release_connection"):
        clusterer = NewsClusterer()
        result = clusterer.cluster_news(lookback_hours=48)

    assert result == {}  # 행이 없으므로 빈 클러스터
    _, params = cursor.execute.call_args[0]
    assert params == (48,)


def test_settings_cluster_lookback_hours_default_preserves_24h_behavior():
    """Settings.CLUSTER_LOOKBACK_HOURS 기본값은 24 - 기존 동작 보존."""
    from config.settings import BaseSettings

    assert BaseSettings.CLUSTER_LOOKBACK_HOURS == 24


def test_stage5_passes_settings_lookback_hours_to_clusterer_when_cli_absent():
    from pipeline.stages import Stage5_NewsletterGeneration

    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = {}
    settings = MagicMock()
    settings.HDBSCAN_MIN_CLUSTER_SIZE = 3
    settings.HDBSCAN_MIN_SAMPLES = 2
    settings.MIN_NEWSLETTER_TARGET = 0
    settings.CLUSTER_LOOKBACK_HOURS = 72

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("core.llm_metrics.get_metrics_collector", return_value=MagicMock()):
        stage = Stage5_NewsletterGeneration(settings=settings)
        result = stage.execute()

    assert result == 0  # 클러스터 없음 -> 조기 반환
    _, call_kwargs = fake_clusterer.cluster_news.call_args
    assert call_kwargs["lookback_hours"] == 72
