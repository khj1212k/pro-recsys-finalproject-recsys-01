# Stage5: 뉴스레터 생성 워크플로우 그래프 정의
# - 클러스터 평가 → 뉴스레터 생성 → 품질 평가 → 임베딩 → 문체 변환 → 저장
#
# 설계 범위: 이 그래프는 '단일 클러스터'의 생성-평가-재시도 서브그래프만 담당한다.
# 여러 클러스터를 순회하는 상위 오케스트레이션(run 전체 종료 처리 포함)은
# LangGraph 밖에서 pipeline/stages.py의 Stage5_NewsletterGeneration이 클러스터마다
# compile_workflow()로 만든 그래프를 app.invoke(state)로 반복 실행하는 방식으로 담당한다.
from langgraph.graph import StateGraph, END

from workflow.state import AgentState
from workflow.nodes import (
    initialize_cluster_processing,
    evaluate_cluster,
    handle_cluster_eval_failure,
    generate_newsletter,
    check_faithfulness,
    evaluate_newsletter,
    embed_newsletter_node,
    convert_tone_node,
    check_tone_drift_node,
    save_newsletter_to_db,
    handle_newsletter_max_retries,
    route_after_cluster_eval,
    route_after_faithfulness,
    route_after_newsletter_eval,
    route_after_tone_drift,
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
    4. 결정론적 사실성 게이트 (check_faithfulness, ADR 0010)
       - 원문에 없는 수치/인용 -> 구체적 피드백과 함께 재생성 (생성 횟수 상한 공유)
       - 최대 재시도 도달 -> 종료 (failure_reason="faithfulness")
    5. 뉴스레터 품질 평가 (eval_newsletter, judge v2)
       - 통과(PASS) -> 임베딩 생성
       - 실패(FAIL) -> 재시도 (재생성)
       - 최대 재시도 도달 -> 종료
    6. 뉴스레터 임베딩 생성 (embed_newsletter): 원본(Formal) 텍스트 기반
    7. 문체 변환 (convert_tone): 딱딱한 문체 -> 부드러운 문체(Casual)
    8. 문체 드리프트 게이트 (check_tone_drift): 수치/날짜/고유명사가 바뀌면 재변환,
       그래도 바뀌면 형식체 초안을 저장
    9. 저장 (save_newsletter): 변환된 텍스트 + 원본 임베딩 저장 -> 종료
    """
    
    # 그래프 생성
    workflow = StateGraph(AgentState)
    
    # 노드 추가 (Nodes)
    workflow.add_node("init_cluster", initialize_cluster_processing)
    workflow.add_node("eval_cluster", evaluate_cluster)
    workflow.add_node("handle_cluster_fail", handle_cluster_eval_failure)
    workflow.add_node("generate_newsletter", generate_newsletter)
    workflow.add_node("check_faithfulness", check_faithfulness)
    workflow.add_node("eval_newsletter", evaluate_newsletter)
    workflow.add_node("embed_newsletter", embed_newsletter_node)  # 수정: 원본 텍스트로 임베딩 먼저 생성
    workflow.add_node("convert_tone", convert_tone_node)  # 수정: 이후 문체 변환
    workflow.add_node("check_tone_drift", check_tone_drift_node)
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
    
    # 뉴스레터 생성 -> 결정론적 사실성 게이트 -> (통과 시) judge 평가
    workflow.add_edge("generate_newsletter", "check_faithfulness")
    workflow.add_conditional_edges(
        "check_faithfulness",
        route_after_faithfulness,
        {
            "pass": "eval_newsletter",
            "retry": "generate_newsletter",
            "max_retries": "handle_max_retries"
        }
    )
    
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
    
    # 문체 변환 -> 드리프트 게이트 -> 저장 (드리프트 시 재변환 또는 형식체 저장)
    workflow.add_edge("convert_tone", "check_tone_drift")
    workflow.add_conditional_edges(
        "check_tone_drift",
        route_after_tone_drift,
        {
            "retry": "convert_tone",
            "save": "save_newsletter"
        }
    )
    
    # 저장 -> 종료
    workflow.add_edge("save_newsletter", END)
    
    # 최대 재시도 도달 시 -> 종료
    workflow.add_edge("handle_max_retries", END)
    
    return workflow


def compile_workflow():
    """Compile and return the workflow"""
    workflow = create_newsletter_workflow()
    return workflow.compile()
