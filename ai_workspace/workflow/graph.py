"""
LangGraph Graph Construction (LangGraph 그래프 구축)

뉴스레터 생성을 위한 워크플로우 그래프(Node와 Edge)를 정의합니다.
"""
from langgraph.graph import StateGraph, END

from workflow.state import AgentState
from workflow.nodes import (
    initialize_cluster_processing,
    evaluate_cluster,
    handle_cluster_eval_failure,
    generate_newsletter,
    evaluate_newsletter,
    embed_newsletter_node,
    convert_tone_node,
    save_newsletter_to_db,
    handle_newsletter_max_retries,
    route_after_cluster_eval,
    route_after_newsletter_eval,
    route_after_cluster_fail
)


def create_newsletter_workflow() -> StateGraph:
    """
    뉴스레터 생성을 위한 LangGraph 워크플로우(단일 클러스터 처리용)를 생성합니다.
    
    실행 흐름 (Flow):
    1. 클러스터 처리 초기화 (init_cluster)
    2. 클러스터 군집도/품질 평가 (eval_cluster)
       - 통과(PASS) -> 뉴스레터 생성
       - 실패(FAIL) -> 종료
    3. 뉴스레터 초안 생성 (generate_newsletter)
    4. 뉴스레터 품질 평가 (eval_newsletter)
       - 통과(PASS) -> 임베딩 생성
       - 실패(FAIL) -> 재시도 (재생성)
       - 최대 재시도 도달 -> 종료
    5. 뉴스레터 임베딩 생성 (embed_newsletter): 원본(Formal) 텍스트 기반
    6. 문체 변환 (convert_tone): 딱딱한 문체 -> 부드러운 문체(Casual)
    7. 저장 (save_newsletter): 변환된 텍스트 + 원본 임베딩 저장 -> 종료
    """
    
    # 그래프 생성
    workflow = StateGraph(AgentState)
    
    # 노드 추가 (Nodes)
    workflow.add_node("init_cluster", initialize_cluster_processing)
    workflow.add_node("eval_cluster", evaluate_cluster)
    workflow.add_node("handle_cluster_fail", handle_cluster_eval_failure)
    workflow.add_node("generate_newsletter", generate_newsletter)
    workflow.add_node("eval_newsletter", evaluate_newsletter)
    workflow.add_node("embed_newsletter", embed_newsletter_node)  # 수정: 원본 텍스트로 임베딩 먼저 생성
    workflow.add_node("convert_tone", convert_tone_node)  # 수정: 이후 문체 변환
    workflow.add_node("save_newsletter", save_newsletter_to_db)
    workflow.add_node("handle_max_retries", handle_newsletter_max_retries)
    
    # 진입점 설정 (Entry Point)
    workflow.set_entry_point("init_cluster")
    
    # 엣지 추가 (Edges)
    
    # 초기화 -> 평가 (직진)
    workflow.add_edge("init_cluster", "eval_cluster")
    
    # 클러스터 평가 후 분기: 통과 시 생성, 실패 시 처리 노드로
    workflow.add_conditional_edges(
        "eval_cluster",
        route_after_cluster_eval,
        {
            "pass": "generate_newsletter",
            "fail": "handle_cluster_fail"
        }
    )
    
    # 클러스터 실패 처리 후: 재시도 할지 종료할지 결정
    workflow.add_conditional_edges(
        "handle_cluster_fail",
        route_after_cluster_fail,
        {
            "retry": "eval_cluster",
            "end": END
        }
    )
    
    # 뉴스레터 생성 -> 평가
    workflow.add_edge("generate_newsletter", "eval_newsletter")
    
    # 뉴스레터 평가 후 분기: 통과 시 임베딩, 실패 시 재시도
    workflow.add_conditional_edges(
        "eval_newsletter",
        route_after_newsletter_eval,
        {
            "pass": "embed_newsletter",  # 중요: 임베딩 먼저 생성
            "retry": "generate_newsletter",
            "max_retries": "handle_max_retries"
        }
    )
    
    # 임베딩 -> 문체 변환 (순서 중요)
    workflow.add_edge("embed_newsletter", "convert_tone")
    
    # 문체 변환 -> 저장
    workflow.add_edge("convert_tone", "save_newsletter")
    
    # 저장 -> 종료
    workflow.add_edge("save_newsletter", END)
    
    # 최대 재시도 도달 시 -> 종료
    workflow.add_edge("handle_max_retries", END)
    
    return workflow


def compile_workflow():
    """Compile and return the workflow"""
    workflow = create_newsletter_workflow()
    return workflow.compile()
