# Stage5가 클러스터를 스레드 풀로 병렬 처리하므로(ADR 0010) 워크플로우의 공유 BGE-M3
# 임베더가 두 번 로드되거나(메모리 2배) 한 모델에 동시 추론이 겹치지 않아야 한다.
import os
import sys
import threading
import time
import types

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import workflow.nodes as nodes


class _SlowEmbedder:
    instances = 0
    active = 0
    peak = 0
    lock = threading.Lock()

    def __init__(self, l2_normalize=True):
        time.sleep(0.05)
        with _SlowEmbedder.lock:
            _SlowEmbedder.instances += 1

    def generate_embeddings_batch(self, texts):
        with _SlowEmbedder.lock:
            _SlowEmbedder.active += 1
            _SlowEmbedder.peak = max(_SlowEmbedder.peak, _SlowEmbedder.active)
        time.sleep(0.02)
        with _SlowEmbedder.lock:
            _SlowEmbedder.active -= 1
        return [[0.1, 0.2]], None

    def cleanup(self):
        pass


def test_concurrent_nodes_load_the_embedder_once_and_serialize_inference(monkeypatch):
    # 진짜 core.embedder는 임포트 시점에 torch를 요구하므로 가짜 모듈을 끼운다
    monkeypatch.setitem(sys.modules, "core.embedder", types.SimpleNamespace(NewsEmbedder=_SlowEmbedder))
    monkeypatch.setattr(nodes, "_CACHED_EMBEDDER", None)
    state = {"newsletter_draft": {"title": "t", "content": "c"}}
    results = []

    threads = [threading.Thread(target=lambda: results.append(nodes.embed_newsletter_node(state)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _SlowEmbedder.instances == 1
    assert _SlowEmbedder.peak == 1
    assert all(r["newsletter_embedding"] == [0.1, 0.2] for r in results)
