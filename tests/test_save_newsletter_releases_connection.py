# Stage5가 클러스터를 최대 4개 스레드로 병렬 처리하므로(dev 풀 최대 5), 저장 실패 때
# 커넥션을 반납하지 않으면 몇 번의 실패만으로 다른 워커가 풀 커넥션을 못 받는다.
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import workflow.nodes as nodes_module


def _state():
    return {
        "newsletter_draft": {"title": "t", "sentence": "s", "content": "c"},
        "converted_newsletter": None,
        "newsletter_embedding": None,
        "current_article_ids": [1, 2, 3],
        "current_cluster_id": 7,
        "run_id": 1,
        "generation_history": {"attempts": []},
    }


def _wire(monkeypatch, save):
    conn, released = object(), []
    monkeypatch.setattr(nodes_module, "get_connection", lambda: conn)
    monkeypatch.setattr(nodes_module, "release_connection", lambda c: released.append(c))
    monkeypatch.setattr(nodes_module, "save_news_letter", save)
    return conn, released


def test_connection_is_released_when_save_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("insert failed")

    conn, released = _wire(monkeypatch, boom)

    result = nodes_module.save_newsletter_to_db(_state())

    assert released == [conn]
    assert result["failed_clusters"] == [7]


def test_connection_is_released_exactly_once_on_success(monkeypatch):
    conn, released = _wire(monkeypatch, lambda *_a, **_k: 42)

    result = nodes_module.save_newsletter_to_db(_state())

    assert released == [conn]
    assert result["completed_newsletters"] == [42]
