# Architecture Decision Records

이 저장소의 설계 결정 기록(ADR) 목록이다. 각 문서는 컨텍스트-검토한 대안-결정-증거-결과/한계 구조를 따른다.

| 번호 | 제목 | 상태 | 날짜 |
|------|------|------|------|
| [0001](0001-langgraph-for-newsletter-generation.md) | 뉴스레터 생성 워크플로우에 LangGraph 사용 | 채택됨 | 2026-09-25 |
| [0002](0002-hdbscan-for-news-clustering.md) | 뉴스 클러스터링에 HDBSCAN 사용 | 채택됨 | 2026-09-25 |
| [0003](0003-lightgbm-mmr-for-recommendation.md) | 추천에 LightGBM(LambdaRank) + MMR 조합 사용 | 채택됨 (2026-07 갱신, `objective`를 `lambdarank`로 전환; 랭킹 부분은 0013이 대체) | 2026-09-25 |
| [0004](0004-continue-in-fork-and-port-july-fixes.md) | 기존 fork에서 계속 개선하고, 7월 셀프 리뷰 수정을 주제별로 다시 이식한다 | 채택됨 | 2026-09-25 |
| [0005](0005-llm-provider-abstraction.md) | HyperCLOVA X 이후 - OpenAI 호환 어댑터 하나로 여러 LLM 프로바이더 통합 | 채택됨 (모델 선정은 후속 bake-off ADR로 이관) | 2026-09-25 |
| [0006](0006-runtime-compose-and-scheduler.md) | 런타임 구성 - docker compose + supercronic(Airflow 제거), Mac 개발 중 임베딩은 호스트 MPS, BGE-M3 max_length 8192 유지(사전 등록 규칙), 본문 해시 중복 제거·잡 종료 신호 전달 | 채택됨 (7일 수집 성공률로 재확인 예정) | 2026-09-26 |
| [0007](0007-recsys-offline-evaluation-protocol.md) | 추천 시스템 오프라인 평가 프로토콜 | 채택됨 (2026-10-06 개정: 조기 종료 검증 행을 섞어 지표의 동점 순서 아티팩트 제거, 코드 버전도 같은 조건끼리만 비교, 원인은 측정으로 확인한 것만 서술) | 2026-09-25 |
| [0008](0008-db-layer-pgvector-schema-and-upsert.md) | pgvector 어댑터 등록, news_raw 스키마 정합성(UNIQUE·timestamptz), RSS 수집기 UPSERT | 채택됨 | 2026-09-25 |
| [0009](0009-llm-eval-protocol-and-preregistered-decision-rule.md) | LLM 평가 프로토콜과 사전 등록한 모델 선정 규칙 (bake-off v1) | 채택됨 (사전 등록, 결과 없음) | 2026-09-25 |
| [0010](0010-faithfulness-gate-and-judge-v2.md) | 결정론적 사실성 게이트, 문체 드리프트 게이트, judge v2 | 채택됨 (차단 유형·judge 임계값·모드는 잠정, ADR 0009로 보정) | 2026-09-26 |
| [0013](0013-ranker-v2-design.md) | ranker v2 설계 — EB-NeRD 오프라인 벤치마크 프로토콜과 승격 규칙 | 채택됨 (사전 등록 후 R1–R4 통과, 서빙은 shadow부터; 0003의 랭킹 부분 대체) | 2026-09-26 |
| [0015](0015-request-time-recommendation.md) | 요청 시점 추천 - API 프로세스 안에서 후보 합집합·휴리스틱 스코어·MMR을 요청마다 계산, 시간 예산 300ms와 폴백 체인, 실시간 전용 커넥션 풀, 노출 로그 | 채택됨 (추천 품질·대상 장비·HTTP 수준 지연은 미확인, 휴리스틱 가중치는 합성 사전값) | 2026-10-06 |
| [0017](0017-short-term-state-store.md) | 단기 사용자 상태 저장소 - Redis 없이 클릭 로그에서 요청마다 계산(최근 24시간·20클릭 평균) | 채택됨 (지연은 합성 데이터·CI 러너 측정) | 2026-10-06 |

> 날짜는 각 ADR이 이 fork(`port/july-self-review` 브랜치 계열)에 커밋된 날짜다. 0001–0003의 원 채택 시점(설계 당시)은 2026-07이지만, 이 fork로 문서가 이식된 시점은 2026-09-25다 — 자세한 배경은 [0004](0004-continue-in-fork-and-port-july-fixes.md)를 참고.
