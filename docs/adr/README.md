# Architecture Decision Records

이 저장소의 설계 결정 기록(ADR) 목록이다. 각 문서는 컨텍스트-검토한 대안-결정-증거-결과/한계 구조를 따른다.

ADR로 옮기기 전 단계의 리뷰와 설계 사양은 [`docs/design/`](../design/README.md)에 있다. 그 문서들과 ADR이 다르면 ADR이 옳다.

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
| [0013](0013-ranker-v2-design.md) | ranker v2 설계 — EB-NeRD 오프라인 벤치마크 프로토콜과 승격 규칙 | 채택됨 (사전 등록 후 R1–R4 통과, 서빙은 shadow부터; 0003의 랭킹 부분 대체; 보충 실험 A2 콜드 regime·A3 신경망 비교 E15는 사전 등록만 있고 결과 대기) | 2026-09-26 |
| [0015](0015-request-time-recommendation.md) | 요청 시점 추천 - API 프로세스 안에서 후보 합집합·휴리스틱 스코어·MMR을 요청마다 계산, 시간 예산 300ms와 폴백 체인, 실시간 전용 커넥션 풀, 노출 로그 | 채택됨 (추천 품질·대상 장비·HTTP 수준 지연은 미확인, 휴리스틱 가중치는 합성 사전값; 2026-10-06 개정: 캐시는 결정론 목록만, realtime 폴백에서 배치 행 제거, 로그 v2는 0025; 개정 2: 장기 벡터는 증분 상태, 피처 어댑터·요청 경로 밖 shadow는 0033) | 2026-10-06 |
| [0017](0017-short-term-state-store.md) | 단기 사용자 상태 저장소 - Redis 없이 클릭 로그에서 요청마다 계산(최근 24시간·20클릭 평균) | 채택됨 (지연은 합성 데이터·CI 러너 측정; 2026-10-06 개정: 평균 벡터 대신 최근 20클릭의 시각·벡터를 읽는다, 순서는 (초, 뉴스레터 ID) - 0033) | 2026-10-06 |
| [0019](0019-user-simulator-design-and-claim-scope.md) | 합성 사용자 시뮬레이터 설계와 주장 범위(φ에 임베딩 유사도를 직접 쓰지 않는 클릭 모델 — 팀 카탈로그의 카테고리 라벨은 임베딩 kNN 추정, 2% 기저 CTR 보정, 정확도 무주장) | 채택됨 (격자 v1: 판정 50건 중 위반 2건·보고만 하는 항목 8건, drift 적응 지표 타당성 미확인, 부하 수치 재실행 대기; 2026-10-06 실제 서빙 경로를 붙인 E9·E10 [SIM] 결과 추가 - 정책이 사용자 상태를 바꾸는 한계 확인) | 2026-09-26 |
| [0023](0023-data-sources-copyright-retention.md) | 수집 출처와 라이선스, 저장·노출·공개 범위, 본문 보존 기한(30일) | 채택됨 (본문 보존 잡은 설계만 있고 미구현. 선행 조건인 수집 스키마는 병합됨) | 2026-09-26 |
| [0025](0025-logging-v2-exploration-and-ope.md) | 노출·클릭 로그 v2(요청 로그, 칸 로그, 클릭의 노출 연결키), ε-균등 탐색 슬롯(위치 무작위, 20칸 중 2칸·신호 없는 사용자 4칸)과 닫힌 식 propensity, 오프폴리시 추정기(replay 1차·SNIPS 2차·위치 기반 보조·coverage·ESS), 첫 온라인 지표와 검정력 표 사전 등록 | 제안됨 (식은 열거로 검증. 채택 조건인 시뮬레이터 검증 E9는 2026-10-06에 돌렸고 **실패** - 탐색 2칸과 사전 등록한 재실험 4칸 모두. 노출 피로 규칙의 enforce 조건 E10도 미충족이라 기본값 `log` 유지; 2026-10-06 개정: 칸 로그 피처 버전 2와 요청 경로 밖 shadow는 0033) | 2026-10-06 |
| [0026](0026-hosting-tiers-and-contingency.md) | 호스팅 계층과 비상 계획 - Always Free만으로(개발 Mac · Tier 0 E2.1.Micro 임시 단독 수집 · Tier 1 A1 대기) | 채택됨 (Tier 0는 승격 조건 미통과, Tier 1은 A1 미확보. 2026-10-06 10일 관측으로 갱신 - 백업 방침 재검토 대기) | 2026-09-26 |
| [0031](0031-cold-start-and-language-transfer-contract.md) | 콜드스타트 임계와 언어 전이 계약 — 히스토리 절단으로 정하는 k\*, 요청 내 랭크 정규화 손실 한도, 전이 미검증 라벨 | 제안됨 (뼈대만 있음. 판정 규칙은 ADR 0013 A2 사전 등록에 고정, 수치는 전부 미측정) | 2026-10-06 |
| [0033](0033-train-serve-feature-parity.md) | 단일 피처 구현과 train/serve parity 게이트 — `recsys_core` 서빙 어댑터(하네스와 같은 22열), 클릭마다 갱신하는 장기 프로필 증분 상태, 요청 경로 밖의 shadow·피처 계산, 게이트 4항목(피처 max\|Δ\| < 1e-6, 점수 순서 τ = 1, 두 후보 생성기 위 랭커 상위 20개 겹침 ≥ 0.9, 후보 구성 동일), 팀 학습 레시피 은퇴, 모델 등록 스크립트 | 채택됨 (게이트는 CI의 PostgreSQL 재생 200건에서 통과; 게이트의 모델은 무작위 데이터의 22열 모델 - EB-NeRD 랭커 등록 전, 서빙 정의와 EB-NeRD 학습 정의의 차이는 미측정) | 2026-10-06 |

> 날짜는 각 ADR이 이 fork(`port/july-self-review` 브랜치 계열)에 커밋된 날짜다. 0001–0003의 원 채택 시점(설계 당시)은 2026-07이지만, 이 fork로 문서가 이식된 시점은 2026-09-25다 — 자세한 배경은 [0004](0004-continue-in-fork-and-port-july-fixes.md)를 참고.
