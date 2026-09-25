import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import workflow.nodes as nodes_module


def test_unused_should_continue_processing_and_finalize_workflow_removed():
    """FIX_LOG #8: should_continue_processing/finalize_workflow는 정의만 되고
    graph.py의 어떤 노드로도 등록되지 않은 죽은 코드였다(ADR 0001에도 기록).
    삭제되어 더 이상 workflow.nodes에 존재하지 않아야 한다."""
    assert not hasattr(nodes_module, "should_continue_processing")
    assert not hasattr(nodes_module, "finalize_workflow")


def test_graph_module_documents_single_cluster_subgraph_scope():
    import workflow.graph as graph_module

    source = graph_module.__doc__ or ""
    with open(graph_module.__file__, encoding="utf-8") as f:
        content = f.read()

    assert "단일 클러스터" in content
    assert "pipeline/stages.py" in content
