# ADR 0003: 추천에 LightGBM(LambdaRank) + MMR 조합 사용

## 상태
채택됨 (2026-07, `objective`를 `binary`에서 `lambdarank`로 전환하며 갱신됨 — `docs/fix-log-2026-07.md` #5)
랭킹 부분은 [ADR 0013](0013-ranker-v2-design.md)(ranker v2, 2026-09-26)이 대체한다: EB-NeRD ablation에서 `binary` → 유저 그룹
`lambdarank` 전환의 효과는 nDCG@10 +0.001에 그쳤고, 개선은 네거티브 구성과 trailing 인기도에서 나왔다. MMR 부분은 유지.

## 컨텍스트
사용자별로 후보 뉴스레터를 관련도 순으로 정렬해 추천해야 한다. 동시에 README에 명시된 목표 중 하나가 "편향 우려 해결 — 다양한 언론사 조합으로 균형 잡힌 시각 제공"이므로, 단순히 관련도 1등부터 K개를 뽑으면 비슷비슷한 기사만 노출되는 문제(다양성 부족)도 함께 풀어야 한다.

## 검토한 대안 (랭킹 모델)
- **순수 임베딩 코사인 유사도**: 구현이 간단하지만 클릭 로그로부터 학습하지 않아 개인화 신호(카테고리 선호, 최신성, 클릭 이력)를 결합하기 어렵다.
- **딥러닝 랭킹 모델 (Two-Tower, Wide&Deep 등)**: 표현력은 높지만 이 프로젝트 규모의 데이터량/인프라로는 과적합·학습 비용 대비 이득이 낮다.
- **LightGBM (GBDT)**: 정형 피처(최신성, 카테고리 매치, 히스토리 유사도 등)를 다루기 좋고, 적은 데이터로도 빠르게 학습·반복 실험이 가능해 부트캠프 기간 내 반복 개선에 적합. 클릭률 예측/랭킹 태스크에서 산업적으로 검증된 선택.

## 검토한 대안 (다양성 확보)
- **카테고리 강제 쿼터**: 카테고리별로 최소 N개씩 강제 배정 — 구현은 쉽지만 관련도를 무시하는 경직된 방식.
- **MMR (Maximal Marginal Relevance)**: `λ*relevance - (1-λ)*max_similarity_to_selected` 공식으로 관련도와 다양성을 하나의 점수로 절충. λ를 조절해 트레이드오프를 유연하게 제어할 수 있다.

## 결정
1. **랭킹**: `LGBMRanker`(`src/models/lgbm_ranker.py`)를 사용하되, 최초 구현은 `objective='binary'`(클릭 확률 이진 분류)였다. 이는 "Ranker"라는 이름 및 MRR/nDCG 평가지표와 실제 학습 방식이 불일치하는 문제였다(랭킹 손실을 직접 최적화하지 않음). 2026-07 리뷰에서 `objective='lambdarank'` + `set_group()`(유저 단위 쿼리 그룹)으로 전환해 유저 내 아이템 간 상대적 순서를 직접 학습하도록 수정했다. 이 전환의 실제 효과는 `scripts/ablation_objective_comparison.py`로 재현 가능하게 비교할 수 있다.
2. **다양성**: `CategoryBasedMMRReranker`(`src/core/reranker.py`)로 LightGBM 스코어를 재정렬한다. 사용자가 선호 카테고리를 적게 선택했을수록(1~2개) 관련도를 우선(λ=0.8), 많이 선택했을수록(5개+) 다양성을 우선(λ=0.6)하도록 구간별로 λ를 다르게 적용한다.

## 결과
**긍정적:**
- LightGBM은 학습/추론이 빨라 부트캠프 기간 동안 여러 번 반복 실험 가능
- MMR의 min-max 정규화 덕분에 lambdarank의 unbounded score로 전환해도 하위 호환됨 (`reranker.py`가 후보군 내에서 점수를 0~1로 재정규화하므로 원 점수의 스케일에 의존하지 않음)
- 카테고리 개수 기반 적응형 λ는 "카테고리를 좁게 고른 사람일수록 정확한 추천을, 넓게 고른 사람일수록 다양한 추천을 원할 것"이라는 합리적 가정에 기반

**부정적/한계 (리뷰에서 발견):**
- `history_cosine_similarity` 피처가 초기 구현에서 학습 시점 이전/이후 클릭을 구분하지 않고 계산되어 data leakage가 있었다(수정 완료, `docs/fix-log-2026-07.md` #4)
- `negative_sample_ratio`의 negative sampling에 랜덤 시드가 없어 실행마다 학습 데이터 구성이 달라졌다(수정 완료, `docs/fix-log-2026-07.md` #16)
- λ 구간별 값(0.8/0.7/0.6)이 실제 사용자 행동 데이터 분석(EDA)이 아니라 직관적 가정으로 설정되어 있다 — `scripts/generate_daily_stats.py`의 `analyze_user_diversity()` 결과와 연결해 데이터 기반으로 재검증할 필요가 있다.
- `user_age_band`/`user_gender` 피처가 DB에 실제 컬럼이 없어 항상 0으로 고정된 상수 피처다 — 모델 성능에 기여하지 못하는 죽은 피처. (2026-09-26 제거: LightGBM 4.7은 상수 열을 학습 전에 걸러내므로, 같은 파라미터·3시드·열 위치 3가지에서 제거 전후 예측이 비트 단위로 같았다.)
- **lambdarank 조기 종료가 트리 1개 모델을 낼 수 있다(엔진 미수정).** 합성 아카이브 재현([`reports/recsys/team_repro_v2.md`](../../reports/recsys/team_repro_v2.md), [ADR 0007](0007-recsys-offline-evaluation-protocol.md))에서 이 엔진의 학습 경로와 같은 행 순서로 lambdarank를 NDCG 조기 종료하면 5개 시드 중 3개가 1라운드(`best_iteration`=1)에서 멈췄다. 원인은 모델이나 데이터가 아니라 조기 종료 지표다. LightGBM의 NDCG는 동점을 데이터 행 순서로 깨는데, `lgbm_dataset.create_train_dataset`이 positive를 그 negative들보다 먼저 쌓고 `time_split`과 `LGBMRanker._build_groups`가 모두 안정 정렬이라 검증 그룹마다 positive가 첫 행이 된다. 동점이 많은 1라운드의 검증 NDCG@5가 부풀려져(엔진 순서 평균 0.737, 동점 무작위 기대값 0.679) 이후 라운드가 그 값을 넘지 못한다. 검증 행을 섞으면 같은 시드의 `best_iteration`이 [35, 72, 49, 90, 28]이 된다. 평가 하네스는 재현 대상인 엔진을 바꾸지 않고 자기 검증 프레임만 섞었으므로, **`main_lgbm.py`와 `LGBMRanker.train`의 운영 학습 경로에는 이 문제가 그대로 남아 있다.** 수정 방향: 검증 그룹 안의 행을 섞어 넘기거나, 동점에 영향받지 않는 지표(동점 기대 NDCG 등)로 조기 종료한다.
- 같은 재현에서 point-in-time MRR의 lambdarank와 binary 차이는 검출되지 않았다(binary − lambdarank 조기 종료 −0.031 [−0.112, +0.046], binary − lambdarank 100라운드 −0.032 [−0.121, +0.052], 합성 유저 31명·5시드). 이 전환은 모델 이름·평가지표와 학습 목표를 맞춘 정합성 수정이며, 성능 개선의 근거는 아직 없다.
