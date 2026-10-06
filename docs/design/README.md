# 설계 문서

ADR보다 앞 단계의 기록이다. 여러 결정을 한꺼번에 놓고 문제 목록, 대안 비교, 우선순위, 실험 계획을 정리했다. **이 문서들 자체에는 효력이 없다.** 여기서 내린 판단은 ADR로 옮겨졌을 때만 결정이고([ADR 색인](../adr/README.md)), 수치의 원본은 [`reports/`](../../reports/README.md)의 파일이다.

- 세 문서는 2026-09-26 시점의 기록이다(추천 v2에는 09-27 추가분이 있다). 본문은 그때의 판단 그대로 두고, 그 뒤 달라진 것은 각 문서 맨 위 "문서 상태 (2026-10-06)"에 적는다. 본문 안에 덧붙인 것은 `> 2026-10-06:`으로 시작하는 줄 네 곳뿐이다(추천 v2 §3.7·§4·§4.1, 아키텍처 리뷰 3.2절 4항 — 사전 등록과 겹치는 곳).
- 사전 등록 실험의 구속력 있는 문안은 ADR에 있다 — ADR 0009, ADR 0013의 "A2 사전 등록"·"A3 사전 등록", ADR 0025. 설계 문서와 ADR이 다르면 ADR이 옳다.
- 2026-10-06에 main으로 옮기면서 파일·브랜치 이름을 main 경로로 고치고, 작업 과정에 관한 서술과 엔지니어링 설계 범위 밖의 절을 정리했다. 수치·결정·가설·실험 규칙은 바꾸지 않았다.

| 문서 | 무엇인가 | 상태 (2026-10-06) | 결정·구현이 있는 곳 |
|---|---|---|---|
| [종합 아키텍처 리뷰](2026-09-26-architecture-review.md) | 구조적 문제 9가지, 가장 어려운 결정 15개의 대안 비교와 판정, R0–R4 로드맵, 자르거나 미룰 것, 새로 쓸 ADR 목록, 주장 범위 | 로드맵의 R0·R1 대부분과 R3 일부가 main에 있다. LLM 쪽(R2)은 평가 도구까지만 있다. ADR 번호 일부는 다른 주제에 쓰였다(문서 안 대응표) | ADR [0006](../adr/0006-runtime-compose-and-scheduler.md), [0007](../adr/0007-recsys-offline-evaluation-protocol.md), [0009](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md), [0010](../adr/0010-faithfulness-gate-and-judge-v2.md), [0013](../adr/0013-ranker-v2-design.md), [0015](../adr/0015-request-time-recommendation.md), [0017](../adr/0017-short-term-state-store.md), [0019](../adr/0019-user-simulator-design-and-claim-scope.md), [0023](../adr/0023-data-sources-copyright-retention.md), [0025](../adr/0025-logging-v2-exploration-and-ope.md), [0026](../adr/0026-hosting-tiers-and-contingency.md), [0031](../adr/0031-cold-start-and-language-transfer-contract.md) · PR #8–#19 |
| [추천 v2 설계 사양](2026-09-26-recommendation-v2.md) | 요청 시점 추천의 로그·탐색 슬롯·shadow 경로, 단일 피처 구현과 parity 게이트, 모델군 선택, 사전 등록 실험 E1–E15, 마일스톤 M0–M8b | M0–M2 병합. M3는 PR #20에서 검토 중. M4·M4b는 사전 등록과 코드까지(결과 대기). M5 이후는 시작 전 | ADR [0013](../adr/0013-ranker-v2-design.md)(A2·A3 사전 등록), [0015](../adr/0015-request-time-recommendation.md), [0017](../adr/0017-short-term-state-store.md), [0025](../adr/0025-logging-v2-exploration-and-ope.md), [0031](../adr/0031-cold-start-and-language-transfer-contract.md), 0033(PR #20) · PR #11, #17, #18, #19 |
| [LLM 생성 v2 설계 스펙](2026-09-26-llm-generation-v2.md) | 생성 경로의 정합 문제 20건, 클러스터 선택·소스 준비·프롬프트 v2, 클레임 앵커 생성, 비용·장애 정책, 실험 계획 E0–E8 | 시작 전. 전제인 평가 도구와 결정론 게이트만 main에 있다 | 전제: ADR [0005](../adr/0005-llm-provider-abstraction.md), [0009](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md), [0010](../adr/0010-faithfulness-gate-and-judge-v2.md), [0023](../adr/0023-data-sources-copyright-retention.md). 이 문서의 결정을 담은 ADR은 아직 없다 |
| [`e15_colab_smoke.py`](e15_colab_smoke.py) · [결과 JSON](e15_colab_smoke_result.json) | 추천 v2 §4.1.7이 인용하는 실행 가능성 스모크. 무작위 텐서만 쓰고 EB-NeRD는 쓰지 않았다 | 일회성 기록(2026-09-26). 실제 실행 절차의 기준은 ADR 0013 A3.9 | — |

## ADR이 이 문서를 가리키는 방식

ADR 0013(A2.7, A3.0)과 ADR 0025는 추천 v2 설계 사양을 "`docs/design-recsys-v2` 브랜치"의 `docs/design/2026-09-26-recommendation-v2.md`로 인용한다. 그 경로가 이제 main에 있는 [이 파일](2026-09-26-recommendation-v2.md)이고, ADR이 인용한 절 번호(§1, §2.3, §3.7, §4, §4.1)는 그대로다. 사전 등록 문안은 등록 뒤에 고치지 않으므로 ADR의 그 문장은 손대지 않았다.
