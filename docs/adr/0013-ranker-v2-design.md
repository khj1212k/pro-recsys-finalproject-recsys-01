# ADR 0013: ranker v2 설계 — EB-NeRD 오프라인 벤치마크 프로토콜과 승격 규칙

## 상태
제안됨 (2026-09-26, **사전 등록**: 아래 "사전 등록" 절은 ebnerd_small 실측 결과를 보기 전에 커밋했다.
결과를 본 뒤의 변경은 맨 아래 "사후 변경 기록"에만 적는다.)

## 컨텍스트
- 팀 추천기(`ai_workspace/recommend_engine`, ADR 0003)는 LightGBM `binary` 목적함수 + 클릭당 무작위
  네거티브 5개 + 팀 피처(신선도, 히스토리 코사인, 온보딩 카테고리 일치, 나이/성별)로 학습했다.
  팀 합성 로그 재평가(ADR 0007, 별도 브랜치 `eval/team-baseline-repro-v1`)에서 보고치 MRR 0.897은
  추론 시점 누출로 부풀었고, point-in-time으로 고치면 popularity 베이스라인에도 못 미쳤다. 합성 로그는
  LLM 페르소나가 카테고리 라벨로 클릭을 만든 순환 데이터라 모델 설계를 판단할 근거가 되지 못한다.
- 따라서 실제 뉴스 클릭 로그인 공개 벤치마크 EB-NeRD(Ekstra Bladet, 덴마크어, RecSys Challenge 2024)로
  "팀 방식 → ranker v2"의 각 설계 선택이 실제로 기여하는지를 단계별로 잰다. 이 프로젝트의 서빙 조건
  ("요청 시점 t에 최근 발행된 후보를 유저 상태·아이템 상태로 재정렬")과 같은 문제 형태다.
- 라이선스: EB-NeRD는 연구/비상업 전용이고 이 Mac 밖으로 반출할 수 없다. 원본·임베딩·중간 산출물은
  gitignore된 `data/benchmarks/ebnerd/`에만 두고, 저장소에는 집계 수치(`reports/recsys/ebnerd_v1.*`)만 커밋한다.
  기사 텍스트는 리포트에 쓰지 않는다.

## 검토한 대안
- **팀 방식 유지(binary + 무작위 네거티브)**: 구현이 이미 있다. 그러나 학습 분포(클릭 vs 7일 풀 무작위 기사)가
  서빙 분포(같은 목록에 함께 노출된 기사들 사이의 순서)와 다르다. ablation 시작점(arm)으로 보존한다.
- **LambdaRank + 노출(impression) 그룹 + 실제 노출 비클릭 네거티브 + trailing 인기도/단기·세션 피처(= ranker v2)**:
  RecSys Challenge 2024 상위 해법 계열(시간 인식 피처 + GBDT). ID 임베딩을 쓰지 않아 아이템 콜드스타트와
  언어 전이(덴마크어 → 한국어)를 같은 스칼라 피처로 다룬다. **이 ADR의 후보.**
- **다국어 인코더 위 학습형 유저 인코더(NRMS/two-tower)**: EB-NeRD 논문 기준 NRMS가 popularity 대비 AUC +1.3%p 수준이고,
  GPU 의존·시드 분산·교차언어 zero-shot 붕괴 위험이 있다. 기각(ADR 0003 기각 사유 유지).
- **학습 없는 선형 휴리스틱(장기/단기/신선도/인기도 가중합)**: 승격 규칙을 통과하지 못할 때의 폴백·초기 활성 모델.

## 사전 등록 (실행 전 고정)

### 데이터와 창
- 데이터: `ebnerd_small` (train/validation behaviors·history, articles 20,738건). 파일 sha256은 리포트 JSON에 기록.
- 임베딩: BGE-M3(`BAAI/bge-m3`, 1024차원, L2 정규화), 입력 = 제목 + 부제 + 본문을 512 토큰에서 절단,
  MPS fp16 계산, float16 저장(`derived/ebnerd_small/bge_m3_tsb512.*`, sha256 = `2f096ef8…ebeb`).
  512 절단 근거: 입력 토큰 중앙값 497, 512 초과 48%. 512 초과 기사 100건 표본에서 2048 토큰 임베딩과의
  코사인 평균 0.961(p05 0.896)로 제목·리드가 벡터를 지배한다. 처리량은 512 토큰 본 실행 2.28건/s,
  2048 토큰 재임베딩 0.74건/s(배치 크기·동시 부하가 달라 대략적 비교) — 전체 20,738건 기준 약 2.5시간 vs 약 8시간.
  BGE-M3는 다국어 모델이지만 덴마크어 성능은 따로 확인하지 않았으므로 아래 "임베딩 점검"으로 잰다.
- 창(전부 [시작, 끝) 반열린 구간, EB-NeRD 로컬 시각):
  - fit(학습): train 행동 창 시작 + 48h ~ validation 시작 − 24h = 2023-05-20T07 ~ 05-24T07.
    앞 48h를 버리는 이유: 48h trailing 인기도 창이 행동 로그 시작 이전으로 잘려 피처 분포가 달라진다.
  - es(early-stop): 2023-05-24T07 ~ 05-25T07 (train 마지막 24h). 각 모델은 자기 학습 데이터와 같은 방식으로
    만든 es 데이터로 early-stop(50라운드 인내, 최대 1000라운드)하고 `best_iteration`을 기록한다.
  - test(평가): validation 행동 창 전체(2023-05-25T07 ~ 06-01T07), **고정 정답 창**. 평가 데이터는 모델 선택·
    하이퍼파라미터·early-stop 어디에도 쓰지 않는다.
- point-in-time: 유저 상태(히스토리·단기·세션·카테고리 분포)는 요청 시각 t(또는 profile_cutoff)보다 **엄격히 이전**
  이벤트만, 아이템 인기도는 t 이전 행동 로그(train+validation 클릭/노출)만 쓴다. 유저 로그는 split별
  history.parquet(행동 창 이전 21일) + 그 split 행동 창의 클릭이다. 모든 조회는 `recsys_core`의 반열린 구간으로 자른다.

### 과제
- **P1 노출 재정렬**: 각 노출의 `article_ids_inview` 안에서 순위. 지표 = 노출별 AUC, MRR, nDCG@5, nDCG@10의 평균.
  seen 필터: 후보 중 t 이전에 유저가 읽은 기사 비율과, seen 후보를 뺀 뒤 재계산한 지표를 함께 보고.
- **P2 전체 풀**: 후보 = t 기준 [t−48h, t]에 발행된 전체 기사 − t 이전에 읽은 기사. 정답 = 그 노출의 클릭.
  48h 밖에서 발행된 클릭 비율(재현율 상한)을 보고. 지표 = 출처(popularity 6h/24h, recency, cosine_history)별·합집합
  Recall@50/100/200, 랭커의 nDCG@10·Recall@10·MRR, coverage@10(요청 풀 대비 목록에 등장한 고유 기사 비율),
  ILD@10(목록 내 임베딩 평균 코사인 거리), 카테고리 엔트로피@10, novelty@10(48h 인기도 self-information).
  validation 노출 20,000건 표본(seed 20260927).
- **P2 모델 선택**: validation을 보지 않고 es 구간 노출 5,000건 표본의 P2 nDCG@10(3 seed 평균)으로 고른다.

### 모델
- 휴리스틱 베이스라인: random, popularity 6h/24h/48h(trailing 클릭 수), recency, cosine_history(시간 감쇠 반감기 7일
  히스토리 평균 벡터 코사인), category_share(유저 과거 카테고리 분포에서 후보 카테고리 비율).
- ablation 사슬(인접 단계는 한 가지만 바꾼다, 하이퍼파라미터는 팀 설정 고정: num_leaves 31, lr 0.05,
  feature_fraction 0.9, bagging 0.8/5):
  1. `team_binary` — binary, 클릭 + 7일 발행 풀 무작위 네거티브 5개, 팀 피처
  2. `lambdarank_user_groups` — + LambdaRank, 쿼리 = 유저
  3. `lambdarank_impression_groups` — 쿼리 = 노출
  4. `inview_negatives` — 네거티브 = 실제 노출 비클릭
  5. `plus_trailing_popularity` — + 6/24/48h 클릭 수, 24h 노출 수·CTR
  6. `plus_short_term_session` — + 24h 단기 벡터·세션 벡터 코사인, 마지막 이벤트 이후 경과
  7. `ranker_v2` — + 카테고리 share, 히스토리 길이
- 네거티브 변형(사슬 밖, P2 서빙 분포에 맞춤): `ranker_v2_poolneg`(48h 풀 무작위 20개), `ranker_v2_mixed`(노출 비클릭 + 풀 20개).
- 학습 데이터: fit 창의 모든 노출(표본 추출 없음), es 창의 모든 노출.

### 통계
- seed 0, 1, 2 (LightGBM seed, 네거티브 표집, 동점 깨기). 노출별 지표를 seed 평균한 뒤 **유저 단위 클러스터
  부트스트랩**(1,000회)으로 평균의 95% CI, 같은 노출에서의 쌍체 차이(a − b)의 95% CI를 낸다. seed별 평균과 표준편차도 보고.
- 다중 비교 보정은 하지 않는다. 판정에 쓰는 비교는 아래 승격 규칙의 비교뿐이고 나머지는 서술용이다.

### 판정 규칙
- **승격 규칙**(ranker v2를 shadow에서 활성 스코어러 후보로 올리는 조건, 전부 만족):
  - (R1) P1 nDCG@10: `ranker_v2 − popularity_24h` ≥ +0.02 이고 쌍체 95% CI 하한 > 0
  - (R2) P1 nDCG@10: `ranker_v2 − team_binary` 쌍체 95% CI 하한 > 0
  - (R3) P2 nDCG@10: P2 선택 모델 − `cosine_history` 쌍체 95% CI 하한 > 0
  - (R4) `ranker_v2`와 P2 선택 모델의 모든 seed에서 `best_iteration` > 5
  - 하나라도 실패하면 휴리스틱(대안 4)을 활성으로 두고 ranker v2는 shadow로만 두며, 실패 사실을 이 ADR에 기록한다.
- **ablation 해석**: 인접 단계 P1 ΔnDCG@10의 CI가 0 초과면 "기여", 0 미만이면 "해로움", 0을 포함하면 "구분 불가".
  이 ADR에서는 결과를 보고 피처를 빼거나 사슬을 바꾸지 않는다(사후 가지치기는 새 사전 등록 실험으로).
- **실시간 재생**: validation 둘째 날부터, 같은 날 t 이전 유저 이벤트가 1건 이상인 노출에서
  (A) 그날 00:00까지의 프로필, (B) t까지의 프로필, (C) 행동 창 이전 history만으로 유저 피처를 만든다(아이템 피처는 t 고정).
  1차 수치 = `ranker_v2` 실시간(B로 학습·서빙) − 일 배치(A로 학습·서빙) ΔnDCG@10. CI 하한 > 0일 때만 "요청 시점
  프로필 갱신의 가치가 측정됨"이라고 쓴다. n과 전체 노출 대비 비율을 함께 보고.
- **MMR**: `ai_workspace/recommend_engine`의 `MMRReranker`(성승우 작성)를 수정 없이 사용, P2 선택 모델(seed 0) 점수,
  pool_multiplier 4, top 10, λ ∈ {0.5, 0.6, …, 1.0}, P2 요청 3,000건 표본. nDCG@10·ILD@10·coverage@10의 Pareto를
  서술하고, 권장 λ = nDCG@10이 λ=1.0 대비 상대 2% 이내로 유지되는 가장 작은 λ(평가 표본에서 고른 값이므로 잠정값).
- **임베딩 점검**: 카테고리 kNN(k=10) leave-one-out 정확도가 다수 클래스 비율보다 높고 0.6 이상이면 BGE-M3가
  덴마크어 기사 의미를 담는다고 본다. 미달이면 cosine 계열 결과를 "임베딩 한계"와 함께 해석한다.
- 판정은 사람이 표를 읽어 내리지 않고 `evaluation/recsys/ebnerd/make_report.py`의 `promotion_verdict`,
  `recommended_mmr_lambda`가 JSON에서 기계적으로 계산한다(비교 항목이 없으면 실패).
- 범위 밖(후속): 일일 배치 릴리스 regime(P3), 콜드 유저 히스토리 절단 곡선, 언어 전이 계약(요청 내 랭크 정규화).

### 실행 명령(고정)
```
EBNERD_ROOT=<repo>/data/benchmarks/ebnerd .venv/bin/python -m evaluation.recsys.ebnerd.run_ebnerd \
  --dataset ebnerd_small --seeds 0 1 2 --n-boot 1000 --p2-sample 20000 --p2-select-sample 5000 \
  --mmr-sample 3000 --threads 6 --out-json reports/recsys/ebnerd_v1.json
```
(`--max-fit`/`--max-test`/`--replay-max` 없음 = fit·es·test·재생 대상 전부 사용. 실행 전 가짜 임베딩(`--fake-dim`)으로만
시간·메모리를 쟀다: fit/test 40,000건 1 seed 348초, 피크 3.3GB.)

## 결정
(결과를 본 뒤 기록)

## 증거
(결과를 본 뒤 기록 — `reports/recsys/ebnerd_v1.{md,json}`)

## 결과와 한계
(결과를 본 뒤 기록)

## 사후 변경 기록
(없음)
