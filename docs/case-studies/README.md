# 케이스 스터디

각 편은 2쪽 분량이다. 문제 → 가설 → 측정 설계 → 결과(수치·95% CI) → 결정 → 한계 → 링크 순서로 쓰고, 수치는 커밋된 [`reports/`](../../reports/README.md)에서만 옮겼다. 모든 수치에 데이터 라벨을 붙였다. 라벨과 증거 등급의 정의는 [증거 요약표](../evidence-summary.md)에 있다.

| 편 | 한 줄 | 데이터 | 등급 |
|---|---|---|---|
| [01. 팀 지표 누출 규명](01-team-metric-leakage.md) | 같은 모델의 추론 시점만 바꾸면 팀 최종 코드의 MRR이 0.8485 → 0.5636(+0.2849 [+0.1581, +0.4177]) | [team-synth] | `descriptive` |
| [02. 네거티브 분포가 과제를 정한다](02-negatives-define-the-task.md) | 랭커 개선의 대부분은 학습 네거티브에서 왔고, 사전 등록한 신경망 비교는 졌고, train/serve parity 게이트가 서빙 경로의 어긋남을 잡았다 | [EB-NeRD], [CI-synth] | `preregistered-run`, `descriptive` |
| [03. LLM 생성 첫 실측](03-first-llm-measurement.md) | 2월 이후 처음 끝까지 돈 생성 파이프라인(15클러스터 중 14 생성, $0.155614)과 사람 라벨 없는 자동 라벨의 한계 | [KR-E0] | `descriptive` |

차트는 [`scripts/portfolio_charts.py`](../../scripts/portfolio_charts.py)가 리포트 JSON에서 만든다([`docs/assets/README.md`](../assets/README.md)).

원문 기사와 생성 초안의 텍스트는 [ADR 0023](../adr/0023-data-sources-copyright-retention.md) 때문에 싣지 않았다.
