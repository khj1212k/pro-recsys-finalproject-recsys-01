# 워크플로우 State 정의
# - 클러스터/뉴스레터 처리 상태를 담는 구조체
from typing import TypedDict, List, Dict, Optional, Any


class ClusterEvaluation(TypedDict):
    """LLM이 수행한 클러스터 평가 결과"""
    decision: str  # "PASS" 또는 "FAIL"
    confidence: float
    summary: str
    feedback: str
    outlier_indices: List[int]


class NewsletterEvaluation(TypedDict):
    """LLM이 수행한 뉴스레터 평가 결과"""
    decision: str  # "PASS" or "FAIL"
    score: int  # 0-10
    feedback: str
    issues: List[str]


class AgentState(TypedDict):
    """LangGraph 워크플로우의 메인 상태값"""
    
    run_id: Optional[int]  
    
    all_cluster_groups: Dict[int, List[int]]  # {클러스터ID: [기사ID목록]}
    all_cluster_ids: List[int]  # 처리할 클러스터 ID 목록
    current_cluster_index: int  # 전체 ID 목록 중 현재 처리 인덱스

    # DB에서 로드한 데이터
    data: Dict[str, Any]  # {ids, titles, embeddings, press_names, contents} 정보 포함
    
    # 결과 추적용
    completed_newsletters: List[int]  # 저장 완료된 뉴스레터 ID 목록
    failed_clusters: List[int]  # 최대 재시도 횟수 초과로 실패한 클러스터 ID
    skipped_clusters: List[int]  # 평가 실패 등으로 건너뛴 클러스터 ID

    # ========== 클러스터별 상태 (반복마다 초기화됨) ==========
    current_cluster_id: int
    current_article_ids: List[int]
    current_articles: List[Dict]  # [{raw_news_id, title, press_name, content}] 목록
    
    # 클러스터 평가 관련
    cluster_eval: Optional[ClusterEvaluation]
    cluster_retry_count: int
    
    # 뉴스레터 생성 관련
    newsletter_draft: Optional[Dict]  # {title, sentence, content, keywords, categories} 포함됨
    newsletter_eval: Optional[NewsletterEvaluation]
    newsletter_retry_count: int
    newsletter_feedback: Optional[str]  # 재생성을 위해 누적된 피드백
    generation_history: Optional[Dict]  # 생성 시도 및 평가 이력
    
    # 문체 변환 관련 (새로 추가됨)
    original_newsletter: Optional[Dict]  # 임베딩용 원본 뉴스레터 (딱딱한 문체)
    newsletter_embedding: Optional[List[float]]  # 원본 텍스트 기반 임베딩
    converted_newsletter: Optional[Dict]  # DB 저장용 변환 뉴스레터 (부드러운 문체)
    conversion_feedback: Optional[str]  # 문체 변환에 대한 피드백

    # 결정론적 게이트 (docs/adr/0010)
    faithfulness_report: Optional[Dict]  # 최신 초안의 사실성 게이트 결과
    tone_drift_report: Optional[Dict]  # 최신 문체 변환본의 드리프트 게이트 결과
    tone_retry_count: int  # 드리프트로 인한 문체 재변환 횟수
    tone_feedback: Optional[str]  # 재변환 프롬프트에 넣을 드리프트 피드백
    tone_fallback: Optional[str]  # "formal"이면 캐주얼본 대신 형식체 초안을 저장
    failure_reason: Optional[str]  # 최대 재시도 도달 사유: "faithfulness" / "judge"
    
    # 워크플로우 제어용
    should_continue: bool  # 다음 클러스터로 진행할지 여부
    error_message: Optional[str]
