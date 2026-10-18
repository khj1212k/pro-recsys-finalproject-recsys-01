# docs/assets

README와 케이스 스터디가 쓰는 차트(SVG)다. 손으로 그리지 않고, 커밋된 리포트 JSON에서 스크립트가 만든다.

| 파일 | 내용 | 데이터 라벨 | 입력 |
|---|---|---|---|
| [leakage_before_after_mrr.svg](leakage_before_after_mrr.svg) | 같은 모델을 point-in-time과 팀 방식 추론 시점으로 평가한 MRR, 코드 버전 3개, 쌍 차이와 95% CI | [team-synth] | [`reports/recsys/team_repro_v2.json`](../../reports/recsys/team_repro_v2.json) |
| [ebnerd_negatives_define_task.svg](ebnerd_negatives_define_task.svg) | ablation 사슬 6단계의 nDCG@10 변화, 노출 재정렬(P1)과 전체 풀 랭킹(P2) 나란히 | [EB-NeRD] | [`reports/recsys/ebnerd_v1.json`](../../reports/recsys/ebnerd_v1.json) |
| [e15_neural_vs_lightgbm.svg](e15_neural_vs_lightgbm.svg) | NRMS-lite·스태킹 대 튜닝한 LightGBM, 판정 4개의 차이와 95% CI | [EB-NeRD] | [`reports/recsys/ebnerd_v1_3_neural.json`](../../reports/recsys/ebnerd_v1_3_neural.json) |

## 다시 만들기

표준 라이브러리만 쓴다(설치할 것이 없다). 저장소 루트에서:

```bash
python scripts/portfolio_charts.py           # docs/assets/*.svg 를 다시 쓴다
python scripts/portfolio_charts.py --check   # 쓰지 않고, 커밋된 SVG와 다르면 종료 코드 1
make report-charts                           # 위 첫 줄과 같다 (파이썬은 CHART_PY로 고른다, 기본 python3 - .venv가 없어도 돈다)
make report-charts-check                     # 위 둘째 줄과 같다
```

출력은 시각이나 난수 없이 입력 JSON에서만 정해져서, 같은 JSON이면 바이트까지 같다. 막대·점·신뢰구간뿐 아니라 캡션의 표본 크기, 부트스트랩 횟수, trial·seed 수, 결합 하한도 JSON에서 읽는다. `tests/test_portfolio_charts.py`가 커밋된 SVG와 새로 만든 것이 같은지, 차트에 들어간 핵심 수치가 리포트와 같은지 확인한다. 리포트를 새로 만들었으면 스크립트를 한 번 돌려 SVG를 같이 커밋한다.

차트는 흰 카드 배경을 직접 그려서 밝은 테마와 어두운 테마 모두에서 읽힌다. 수치 라벨 옆의 수염은 각 리포트가 정한 95% 신뢰구간이다(E15 차트의 수염은 유저 단위 부트스트랩이라 seed 간 변동을 담지 않는다. 그 값은 같은 차트의 캡션에 따로 적었다). 차트마다 아래쪽에 출처 JSON 경로와 한계를 적었다.
