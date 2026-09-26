# Architecture Decision Records

이 저장소의 설계 결정 기록(ADR) 목록이다. 각 문서는 컨텍스트-검토한 대안-결정-증거-결과/한계 구조를 따른다.

| 번호 | 제목 | 상태 | 날짜 |
|------|------|------|------|
| [0001](0001-langgraph-for-newsletter-generation.md) | 뉴스레터 생성 워크플로우에 LangGraph 사용 | 채택됨 | 2026-09-25 |
| [0002](0002-hdbscan-for-news-clustering.md) | 뉴스 클러스터링에 HDBSCAN 사용 | 채택됨 | 2026-09-25 |
| [0003](0003-lightgbm-mmr-for-recommendation.md) | 추천에 LightGBM(LambdaRank) + MMR 조합 사용 | 채택됨 (2026-07 갱신, `objective`를 `lambdarank`로 전환) | 2026-09-25 |
| [0004](0004-continue-in-fork-and-port-july-fixes.md) | 기존 fork에서 계속 개선하고, 7월 셀프 리뷰 수정을 주제별로 다시 이식한다 | 채택됨 | 2026-09-25 |
| [0005](0005-llm-provider-abstraction.md) | HyperCLOVA X 이후 - OpenAI 호환 어댑터 하나로 여러 LLM 프로바이더 통합 | 채택됨 (모델 선정은 후속 bake-off ADR로 이관) | 2026-09-25 |
| [0008](0008-db-layer-pgvector-schema-and-upsert.md) | pgvector 어댑터 등록, news_raw 스키마 정합성(UNIQUE·timestamptz), RSS 수집기 UPSERT | 채택됨 | 2026-09-25 |
| [0019](0019-user-simulator-design-and-claim-scope.md) | 합성 사용자 시뮬레이터 설계와 주장 범위(자기 정답지 없는 클릭 모델, 2% 기저 CTR 보정, 정확도 무주장) | 채택됨 (격자 v1: 위반 2/58, drift 적응 지표 타당성 미확인, 부하 수치 재실행 대기) | 2026-09-26 |

> 날짜는 각 ADR이 이 fork(`port/july-self-review` 브랜치 계열)에 커밋된 날짜다. 0001–0003의 원 채택 시점(설계 당시)은 2026-07이지만, 이 fork로 문서가 이식된 시점은 2026-09-25다 — 자세한 배경은 [0004](0004-continue-in-fork-and-port-july-fixes.md)를 참고.
