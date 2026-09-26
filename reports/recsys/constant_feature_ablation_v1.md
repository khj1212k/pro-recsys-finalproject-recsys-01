# 상수 피처 제거 전후 LightGBM 예측 비교 (v1)

- 실행 명령: `cd ai_workspace/recommend_engine && python scripts/constant_feature_ablation.py --out ../../reports/recsys/constant_feature_ablation_v1.json`
  - 로컬 부하 제한: `nice -n 19 env OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2`, LightGBM `num_threads=2`
- 코드: `73ed24f` (스크립트를 추가한 커밋)
- 실행: 2026-09-26 13:05 UTC, 약 8초
- 환경: Python 3.11.14, LightGBM 4.7.0, numpy 2.4.6
- 데이터: **합성**(`numpy.random.RandomState(seed)`). 실제 로그와 기사는 쓰지 않았다.
  - 그룹 수: 학습 400, 검증 100, 시험 100
  - 그룹 구성: 정답 1 + 오답 5. 시험 예측은 600행이다.
  - 피처 8개: 모델 입력 열 계약과 같은 개수이고, 연속값·0/1·개수 형태도 비슷하게 맞췄다.
- 불확실성: 시드 3개(42, 7, 2026) × 상수 열 위치 3가지(앞/중간/뒤) × 학습 방식 2가지 = 18회. 결정적 비교라서 신뢰구간은 없다.
- 수치 원본: [`constant_feature_ablation_v1.json`](constant_feature_ablation_v1.json)

## 질문
커밋 `d1bc2d2`는 피처 `user_age_band`와 `user_gender`를 모델 입력에서 뺐다. 두 피처는 DB에서 읽지 않고 항상 0으로 채워지던 열이다. 이 제거가 같은 데이터·같은 파라미터에서 학습한 모델의 예측을 바꾸는지 확인한다. 바꾸지 않는다면, 제거 전 코드로 잰 결과를 제거 후 코드로 재현해도 이 변경 때문에 수치가 달라지지 않는다.

## 방법
- 파라미터: `config/config.yaml`의 `lightgbm.params`를 그대로 쓰고 `random_state`만 시드로 바꿨다.
  - lambdarank, ndcg@5·10, num_leaves 31, learning_rate 0.05
  - feature_fraction 0.9, bagging 0.8 / 5
- 모델 두 개를 학습해 시험 예측을 원소 단위로 비교했다. 하나는 피처 8개만 쓰고, 다른 하나는 같은 행렬에 0으로 채운 열 2개를 넣었다.
- 학습 방식
  - `early_stopping`: `LGBMRanker.train`과 같다. 최대 1000라운드이고, 검증셋 기준 early stopping 50을 둔다.
  - `fixed_rounds`: early stopping 없이 트리 300개를 만든다. early stopping은 이 데이터에서 트리 1~10개에서 멈춘다. 그러면 열 샘플링과 배깅 난수가 몇 번 쓰이지 않아 비교가 약하다. 그래서 난수가 수백 번 쓰이는 조건도 함께 돌렸다.

## 결과

| 학습 방식 | 시드 | 트리 수(없음/있음) | 위치 앞·중간·뒤의 max\|차이\| | 상수 열 분할 횟수 |
|---|---|---|---|---|
| early_stopping | 42 | 10 / 10 | 0.0 · 0.0 · 0.0 | 0 |
| early_stopping | 7 | 8 / 8 | 0.0 · 0.0 · 0.0 | 0 |
| early_stopping | 2026 | 1 / 1 | 0.0 · 0.0 · 0.0 | 0 |
| fixed_rounds | 42 | 300 / 300 | 0.0 · 0.0 · 0.0 | 0 |
| fixed_rounds | 7 | 300 / 300 | 0.0 · 0.0 · 0.0 | 0 |
| fixed_rounds | 2026 | 300 / 300 | 0.0 · 0.0 · 0.0 | 0 |

18회 모두 예측 배열이 `np.array_equal`로 같았다. 비트 단위로 같다는 뜻이다. early stopping이 고른 트리 수도 같았다.

## 해석과 한계
- LightGBM 4.7.0은 값이 하나뿐인 열을 학습 전에 쓰지 않는 피처로 걸러 낸다.
  - 시드 42 학습 데이터를 `verbose=1`로 학습해 확인했다. 열 8개와 열 10개(상수 2개 포함) 모두 로그가 `number of used features: 8`이었다.
  - 열 위치와 관계없이 예측과 트리 수가 같게 나왔다. 따라서 열 샘플링도 걸러 낸 뒤의 피처 위에서 일어나는 것으로 보인다. 이 부분은 LightGBM 소스를 읽어 확인하지 않았고, 결과에서 추론한 것이다.
- 이 실험이 보여 주는 것은 "상수 열 제거가 예측을 바꾸지 않는다"는 것뿐이다. 모델 품질에 대해서는 아무것도 말하지 않는다. 팀 시절 성능 수치는 따로 철회했다(USER GUIDE, 평가 브랜치의 재현 리포트).
- LightGBM 버전이 바뀌면 이 동작도 달라질 수 있다. 다른 버전에서 쓰려면 다시 돌려야 한다.
- 제거 전에 학습해 저장한 모델 파일은 입력 열 수가 다르다. 따라서 그대로 불러 쓸 수 없고 다시 학습해야 한다.
