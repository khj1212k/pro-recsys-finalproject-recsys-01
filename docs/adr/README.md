# Architecture Decision Records

이 저장소의 설계 결정 기록(ADR) 목록이다. 각 문서는 상태-컨텍스트-검토한 대안-결정-
증거-결과와 한계 구조를 따른다.

| 번호 | 제목 | 상태 |
|------|------|------|
| [0001](0001-langgraph-for-newsletter-generation.md) | 뉴스레터 생성 워크플로우에 LangGraph 사용 | 채택됨 |
| [0002](0002-hdbscan-for-news-clustering.md) | 뉴스 클러스터링에 HDBSCAN 사용 | 채택됨 |
| [0003](0003-lightgbm-mmr-for-recommendation.md) | 추천에 LightGBM(LambdaRank) + MMR 조합 사용 | 채택됨 (2026-07 갱신, `objective`를 `lambdarank`로 전환) |
| [0007](0007-recsys-offline-evaluation-protocol.md) | 추천 시스템 오프라인 평가 프로토콜 | 채택됨 (2026-09-26 개정: 증거를 실측 수치로, 학습 붕괴 표시·같은 조건 비교·CI 기반 결론 추가) |

> 이 브랜치(`eval/team-baseline-repro-v1`)에는 위 문서들만 있다. 다른 브랜치(예:
> `port/july-self-review`, `main`)에는 이식 경위를 다루는 ADR 0004나 그 이후 번호의
> 문서가 추가로 있을 수 있다 - 병합 시 번호 충돌이 없는지 확인할 것.
