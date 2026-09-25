import sys
import os
import logging
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def test_stage5_uses_create_new_batch_for_run_id():
    """Stage5_NewsletterGeneration은 batch_manager.create_new_batch()가 반환한
    run_id를 각 클러스터 state에 주입해야 한다 (로거 이름 파싱 방식이 아니라)."""
    from pipeline.stages import Stage5_NewsletterGeneration

    captured_states = []

    def fake_invoke(state):
        captured_states.append(dict(state))
        return {"completed_newsletters": [1]}

    fake_app = MagicMock()
    fake_app.invoke.side_effect = fake_invoke

    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = {101: ["a"], 102: ["b"]}
    fake_clusterer.get_clustered_articles.return_value = {"dummy": "data"}

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("core.llm_metrics.get_metrics_collector") as mock_metrics, \
         patch("db.batch_manager.create_new_batch", return_value=42) as mock_create_batch:

        mock_metrics.return_value = MagicMock()

        stage = Stage5_NewsletterGeneration(settings=MagicMock())
        stage.execute(limit=None, min_cluster_size=3, min_samples=2)

    # create_new_batch가 실제로 호출되어야 한다
    assert mock_create_batch.called
    # 모든 클러스터의 state가 create_new_batch가 반환한 run_id(42)를 사용해야 한다
    assert len(captured_states) == 2
    for state in captured_states:
        assert state["run_id"] == 42


def test_root_logger_name_is_never_digit():
    """회귀 방지: 기존 버그의 전제('root'.isdigit()) 자체가 항상 False임을 문서화."""
    assert logging.getLogger().name == "root"
    assert logging.getLogger().name.isdigit() is False
