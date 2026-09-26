# 추천 모델·추천 과정 v2 — 설계 사양 (2026-09-26)

> 범위: 추천 단위, 후보 생성, 피처(point-in-time·train/serve parity), 모델군, 사용자 모델링, 다양성, 로그와 오프폴리시 평가,
> 오프라인/온라인 평가, 지연·인프라, 이력서 경계. LLM 생성·클러스터링은 인터페이스가 닿는 지점(스토리 정체성, 생성 캡)만 다룬다.
>
> 근거 스냅샷(2026-09-26 17:00 KST, `git fetch origin` 후 `origin/<branch>` tip): `main 9aa235e`, `eval/ebnerd-harness 3df4df6`,
> `feat/realtime-recommendation 5e84358`, `feat/runtime-compose-and-collection b6d4c0c`, `feat/user-simulator-loadtest b3b20e4`,
> `eval/team-baseline-repro-v1 1585ce6`, `docs/fable-review`(종합 아키텍처 리뷰), `docs/decision-map`(ADR 감사).
> 증거 라벨은 `reports/README.md`(fix/cleanup-and-claim-scrub) 규칙을 따른다: **[EB-NeRD]** 덴마크어 공개 벤치마크,
> **[synthetic team data]** 팀 합성 로그, **[SIM]** 시뮬레이터(정확도 무주장), **[LOAD-mac]/[LOAD-arm]** 부하 실측,
> **[KR-eval]** 한국어 사람 라벨, **[KR-online]** 실사용자 로그, **[KR-ops]** 수집 런타임 실측.
>
> 이 문서는 설계 사양이다. 여기서 내린 결정은 §7의 ADR로 옮겨져야 효력을 갖고, 수치는 `reports/**`의 파일에서만 인용한다.
> 팀 시절 추천기(`ai_workspace/recommend_engine`, LightGBM+MMR)는 팀원 성승우의 설계·구현이며, 이 문서에서 "ranker v2",
> `recsys_core`, EB-NeRD 하네스, 요청 시점 서빙, 시뮬레이터는 `team-final` 태그 이후 본인 작업이다.
>
> 2026-09-26 추가(같은 날 사용자 결정 반영): §3.4 D·E(신경망 사용자 모델)를 "기각"에서 "E15로 측정 판정(보류)"로 바꾸고, §4.1에
> E15 사전 등록과 Colab GPU 실행 계획, §5에 M4b를 넣었다. Colab 사용은 사용자가 EB-NeRD 라이선스를 확인한 뒤 **비공개 런타임에
> 실행마다 업로드·실행 뒤 해제**하는 조건으로 허용했다(저장소·공개 데이터셋·영구 사본 금지는 그대로). 노트북은 무거운 계산을 하지 않는다.

---

## 1. 결론 요약

1. **이 도메인의 상한은 모델이 아니라 데이터 구조가 정한다.** 한국어 뉴스레터 0건·사용자 0명·클릭 0건이며, 사용자가 생겨도
   후보 풀은 생성 캡(하루 15~30건) × 신선도 창(72h) ≈ 45~90개로 K=20의 2~4배에 그친다. 이 regime에서 "더 좋은 랭커"가 올릴 수
   있는 nDCG 폭은 구조적으로 작고, 체감 품질은 (a) 같은 사건 재노출·반복 노출 억제, (b) 신선도·다양성 규칙, (c) 첫 클릭까지의
   순위로 결정된다. 따라서 v2의 1차 목표는 **"첫 사용자부터 정확한 propensity로 정책을 비교할 수 있는 서빙"**이고,
   2차 목표가 "공개 클릭 데이터에서 검증된 랭커를 콜드 regime에서도 무너지지 않게 shadow로 올리는 것"이다.
2. **모델군은 확정하되 한 가지는 측정으로 판정한다:** 언어 무관 스칼라 point-in-time 피처 위 LightGBM LambdaRank(요청 단위 쿼리)가
   shadow 주모델, 활성 스코어러는 EB-NeRD로 가중치를 적합한 4항 휴리스틱. ID 임베딩·BGE-M3 미세조정·정책 학습형 밴딧(Thompson/LinUCB)은
   하지 않는다(근거 §3.4). 신경망 사용자 모델(NRMS-lite·SASRec-lite·GBDT 스태킹)은 이유만으로 기각하지 않고 **E15(§4.1)에서 같은
   EB-NeRD 프로토콜·같은 튜닝 예산으로 측정해 판정**한다 — 통과해도 추가 shadow 후보일 뿐 활성이 아니며, §3.4 D·E의 원 기각 사유는
   "GBDT가 이길 이유"라는 가설로 남긴다. 탐색은 ε-균등 슬롯(20 중 2, 콜드 4)로만 한다.
3. **정확성 문제 중 즉시 고쳐야 하는 것**은 (C1) 노출↔클릭 연결키 부재, (C2) 팀 레시피 `train` 잡과 죽은 `LightGBMScorer`의
   두 경로, (C3·C4) 갱신되지 않는 장기 벡터와 세 벌의 프로필 정의, (C5) 서빙 인기도 = 클릭이 아니라 기사 수, (C6) EB-NeRD
   결과의 콜드 regime 전이 미측정, (C8) team_repro 어드버서리얼 수정 미반영, 그리고 두 리뷰가 모두 놓친 (C17) shadow 경로 부재,
   (C21) 풀 크기 regime 미측정, (C19) ADR 번호 충돌이다. 전체 목록은 §2.3.
4. **리뷰 vs 레드팀**: 레드팀의 정정 대부분(A1 보충 실험 기실행, "10배" 서술 오류, 파워 계산, 탐색 슬롯 설계, prior 설계, k* 결정
   과제, KS 검정 무의미, 통합 브랜치 P0, shadow 경로, 증분 프로필 상태)을 채택한다. 레드팀도 두 곳에서 낡았다: ADR 0019와
   Locust 파일은 `b3b20e4`에 이미 커밋돼 있고, 두 리뷰 모두 `docs/decision-map`이 확인한 ADR 번호 충돌(0017·0026)을 반영하지
   않았다. 판정표는 §2.1.
5. **마일스톤 순서**: M0 통합 브랜치·수집 복구(0.5~1일) → M1 team_repro 수정 병합(1일) → M2 로그 v2·탐색·shadow(≈4일) →
   M3 recsys_core 서빙 어댑터·증분 프로필·parity 게이트(3일) → M4 EB-NeRD v1.2 콜드 regime(3일+실행 2h) → M4b E15 신경망 사용자
   모델 비교(3일+Colab T4 ≤30 CU, M5·M6와 병렬) → M5 인기도·휴리스틱 재설계(1.5일) → M6 [SIM]·[LOAD] 증거(1.5일) → M7 ADR·규칙(1일)
   → M8 조건부(스토리 최소판, 인터리빙 A/A). 총 ≈19 작업일, LLM 비용 $0, Colab ≤30 CU(잔액 ≈200 CU), 사람 라벨 시간 ≤2h(E12에서만).
6. **이력서 상한 문장**(그대로 채택): "공개 사람 클릭 데이터에서 검증된 랭커를, 첫 사용자부터 평가 가능한 로그와 탐색 위에
   shadow로 올린 시스템." "한국어 서비스에서 X% 향상"은 이 계획 어디에도 들어가지 않는다.

---

## 2. 현재 상태와 정확성 문제

### 2.1 리뷰 vs 레드팀 판정표 (누가 옳은가)

직접 재확인한 결과로 판정했다. "확인"은 이 세션에서 `git show`/`git ls-tree`/로컬 실행으로 본 것이다.

| # | 쟁점 | 리뷰 | 레드팀 | 판정 |
|---|---|---|---|---|
| J1 | A1 보충 실험(poolneg 피처 사슬·MMR 쌍체) 실행 여부 | 미실행, 사전 등록만 | 실행·커밋됨 | **레드팀.** `fb9f663`이 `reports/recsys/ebnerd_v1_1_poolneg.json`, `282a428`이 부록 A·ADR 0013 A1 결과를 커밋했다(확인). 리뷰는 `2d223a5` 기준의 낡은 서술. E8은 삭제. |
| J2 | "올바른 라벨·후보·인기도가 모델보다 10배 크다" | P1 수치로 주장 | P2에서는 개인화 +0.038 vs 인기도 +0.049 | **레드팀.** 부록 A.1: 단기·세션 +0.0234 + 카테고리 분포 +0.0147 = +0.0381, 인기도 +0.0494(77%). "gain 점유율 <10%"는 LightGBM gain이지 ablation 기여가 아니다. 서빙형 과제에서 개인화는 무시할 크기가 아니며, 인기도가 0인 콜드 regime에서는 개인화·신선도가 사실상 유일한 신호다. |
| J3 | I3(b) P3 일일 배치 릴리스 양자화 | 독립 실험 | 인위적, h*는 규모 종속 | **레드팀(부분).** 같은 실행의 부가 표로만 두고 곡선 형태만 해석한다. 단, "모든 후보가 같은 시각에 태어난다"는 이 제품 고유 조건이므로 표 자체는 유지한다(E4). |
| J4 | I3(e) breadth_24h 대리 피처의 결정 규칙 | CI≤0이면 서빙 인기도 항에서 제거 | 대리 피처의 null을 원 피처 반증으로 쓰는 오류 | **레드팀.** 선택 실험(E5)으로 내리고 규칙은 "양(+)이면 유지 근거 보강, null이면 보류". `raw_news_count`는 어떤 결과든 0.05 동점 깨기로 유지. |
| J5 | k*(콜드스타트 임계)를 P1 nDCG@10으로 결정 | P1 | 서빙은 P2형, P1 popularity_24h 0.5992가 너무 높아 과대 추정 | **레드팀.** P1·P2 곡선을 다 내되 `RECSYS_MIN_PERSONAL_EVENTS`는 P2/poolneg에서 personalization − popularity_6h CI 하한>0인 최소 k로. 온보딩 선택을 이벤트로 셀지 사전 등록(E2). |
| J6 | 덴마크어 vs 한국어(시뮬레이터 로그) 피처 분포 KS ≤0.1 | 언어 전이 계약의 일부 | 합성 클릭의 산물이라 해석 불가 | **레드팀.** 랭크 정규화 손실 ≥ −0.01만 남기고(E3), KS 표는 실제 한국어 노출·클릭 로그가 생긴 뒤 [KR-eval]로 1회. |
| J7 | 인기도 재설계: prior = 전역 CTR × f(raw_news_count), CTR로 대체 | 대체 | 검증 안 된 가정이 prior에 숨고, 검증된 최강 피처(클릭 수)를 버림 | **레드팀.** pop_clicks_6h/24h 원값 유지 + pop_ctr_shrunk_24h(prior=전역 CTR, α 격자) 추가, raw_news_count는 별도 피처(E6, §3.3). |
| J8 | 휴리스틱 가중치를 P1 노출 내 조건부 로지스틱으로 적합 | P1 | P1 분포는 인기도에 몰림, 서빙은 P2형+인기도≈0 | **레드팀.** poolneg 학습 분포와 저트래픽 서브샘플 두 조건에서 4항만 적합, 두 세트 보고, 전환 임계를 ADR 0014에 사전 등록(E7). |
| J9 | I9 온보딩 화면을 breadth·최신성·카테고리 층화로 | 제안 | 이미 그렇게 동작(no-op) | **레드팀.** `onboarding.py`는 `news_letters_category` 최신 배치(= exp(−age/48)+log1p(raw_news_count)/5)를 카테고리로 걸러 6개 반환(확인). 남는 내용(k* 게이트, 콜드 ε 상향, profile_source 로깅)은 §3.2·§3.5에 흡수. |
| J10 | I10 인터리빙 1.5일·medium | 지금 | shadow 경로가 없어 성립 불가 | **레드팀.** shadow 경로(M2) 뒤로 미루고 [SIM] A/A 1회만(E14). 파워 표·전환 규칙은 ADR 0025에 먼저 적는다. |
| J11 | I1 피처 스냅샷: 상위 20개 jsonb, 근거 "+0.1868 재현" | | +0.1868은 P1 효과; P2형에서는 노출 네거티브가 해로움(0.053) | **레드팀.** 노출 로그의 존재 이유는 propensity·위치와 두 네거티브 regime의 한국어 비교 가능성이다. 상위 20개 피처 벡터(bytea float32) + 후보 id 배열(요청 단위) 저장(§3.6). |
| J12 | I4 탐색 슬롯: 위치 고정 2/20, 결정론 슬롯 propensity 1.0, SNIPS | | 위치 무작위, propensity=(n/K)/\|E\|, 캐시 뒤에서 추출, replay 1차 | **레드팀.** 그대로 채택하고 §3.5에서 수식·캐시 위치·hypergeometric 위치 이동을 명시. |
| J13 | I2 공수 2일, long_term을 요청마다 90일 클릭에서 재계산 | | 통합 브랜치 없이는 불가, heavy user 4MB 페치 | **레드팀.** 3일 + 증분 감쇠 상태(§3.3). 코사인은 양의 스칼라에 불변이라 읽기 시점에는 감쇠 곱셈조차 필요 없다. |
| J14 | C14 파워: 팔당 7.7만 노출(≈3,800 요청) | | 80,680 노출(양측)·설계 효과 | **레드팀.** 두 비율 검정(2.0%→2.2%, α=0.05 양측, 80%) 팔당 80,685 노출로 재계산 일치. 요청 단위는 4,000 × 설계 효과(≥1.5) ≈ 6천 요청 이상. |
| J15 | C9 "cosine_history는 P2 최약 베이스라인" | | category_share 0.0246, popularity_48h 0.0205, random 0.0176이 더 약함 | **레드팀.** "popularity_6h(0.1168)·recency(0.1114)보다 훨씬 약한 축(0.0324)"으로 수정. 요지(R3 자명 통과)는 유지. |
| J16 | §4 "DB 포함 실측·부하 수치 없음" | | 코드 존재: `tests/integration/test_realtime_recsys_seeded_db.py::test_sql_path_latency_p50_p95`, `evaluation/serving/request_path_bench.py`, `.github/workflows/recsys-bench.yml` | **레드팀.** 확인. 없는 것은 기록된 결과(`reports/serving/`)뿐. I11은 "돌리고 기록하기". |
| J17 | C13 `data_loader.py:524-525` | | 파일 442줄; 상수는 `data_loader.py:365-368`·`feature_engineer.py:154-155` | **레드팀.** 결론(상수 피처 2개)은 유지. |
| J18 | I1(3) alembic merge 대상 f87f7378672e + 8b7f830013b7 | | d48994e9d26e + 8b7f830013b7 | **레드팀.** 확인: runtime 브랜치 head는 `d48994e9d26e_news_raw_content_sha256_dedupe.py`(revises f87f7378672e). |
| J19 | C3 `refresh_recently_active_users`가 "같은 파일"에 있음 | | 그 메서드는 realtime 브랜치 `user_embedder.py:60`에만 있고 runtime 브랜치에는 없음 | **레드팀.** 두 브랜치 모두에서 "어디서도 호출되지 않음"은 참. 결론 유지. |
| J20 | DB 실측치·"매시 증가 중" | 690/649/0/0/0 | colima 미실행(16:46 KST) | **레드팀.** 17:00 KST 재확인: `colima status` fatal, 5433 응답 없음. 수치 자체는 반박 안 되지만 수집 연속성이 깨져 있어 M0에 복구를 넣는다(도메인 밖이나 모든 [KR-*]의 전제). |
| J21 | ADR 0019 파일 부재, Locust 파일 미커밋 | (리뷰: ADR 0019 파일 없음) | 동일 주장 | **둘 다 낡음.** `feat/user-simulator-loadtest b3b20e4`에 `docs/adr/0019-user-simulator-design-and-claim-scope.md`(제안됨, 지표 타당성 격자 사전 등록) 존재, `4998918`에 `sim/locustfile.py`·`sim/load.py`·`sim/loadtest.py`·`tests/simulator/test_sim_load.py` 커밋(확인). 여전히 없는 ADR은 0015·0017·0026. |
| J22 | 새 ADR 번호 0025(로그·탐색·OPE)·0026(parity)·0017(콜드스타트·전이) | 사용 | 사용 | **둘 다 미반영.** `docs/decision-map` 2절: 0017은 realtime 브랜치 코드 2곳이 "단기 상태 저장소 벤치마크"로, 0026은 `ops/hosting-tiers` 코드 6곳이 "Tier 0 호스팅"으로 이미 참조한다. 코드 번호 우선 원칙에 따라 **parity → 0033, 콜드스타트·전이 → 0031**로 재배정(§7). 0025는 충돌 없음. |
| J23 | 통합 브랜치가 P0 | I1 소항목 | 모든 제안의 선행 조건 | **레드팀.** `recsys_core/`는 ebnerd-harness에만, `backend/app/recsys/`·노출 로그 마이그레이션은 realtime에만, `jobs/`·crontab은 runtime에만, `sim/`은 simulator에만 있다(ls-tree 확인). |
| J24 | shadow 스코어링 경로 | I2에 `score_shadow` 언급 | 코드에 없음, 구현 항목으로 승격 | **레드팀.** `ScoreResult`는 점수 배열 1개, 노출 로그 `score` 1컬럼(확인). §3.4 `ScorerStack`. |
| J25 | 풀 크기 regime(한국어 45~90 vs EB-NeRD P2 235) | 없음 | E1에 풀 축소 조건, 1차 온라인 지표를 첫 클릭 순위로 | **레드팀.** `p2_info.pool_size` mean 235(161~279) 확인. §1 결론 1의 근거이며 E1·E13에 반영. |
| J26 | 후보 생성기 구성 parity(서빙 5출처·cap 300·72h vs 하네스 4출처·상위 50·48h) | 인용만 | 하네스 2단계 표는 서빙 구성의 결과가 아님 | **레드팀.** E8 신설: 서빙 구성을 하네스에 넣어 2단계 표를 다시 낸다. parity 게이트 정의에 후보 생성기 구성 포함. |
| J27 | 단기 벡터 정의(서빙 ≤20클릭 AVG vs recsys_core 24h 전부) | 없음 | parity 깨는 지점 | **레드팀.** `RecsysConfig.short_term_max_clicks=20` 확인. `FeatureConfig.short_max_events=20`을 추가해 하네스 쪽을 맞춘다(비용 상한이 서빙에 필요하므로). |
| J28 | 노출 피로(impression fatigue) 억제 | 없음 | 필요, [SIM]으로 검증 | **레드팀.** 규칙을 로그 전용으로 넣고 E10에서 검증(§3.2). |
| J29 | 첫 [KR-online] 수치 정의 | "인터리빙" | 탐색 슬롯 vs 휴리스틱 슬롯 쌍체 CTR | **레드팀.** E13의 1차 지표로 사전 등록. 20명 규모에서도 4~6주면 수치가 나온다(§4 E13 파워). |
| J30 | 카테고리 캘리브레이션(Steck 2018) | sources에만 | MMR 대안 선택 과제 | **레드팀.** 선택 과제로 두되 스택 추가 금지(§3.5). |
| J31 | `train` 잡 은퇴 후 `batch` 폴백 단계의 의미 | 없음 | 결정 필요 | **레드팀.** 결정: realtime 모드의 폴백 체인에서 `batch` 단계를 제거하고 `popular → recent → empty`로 단순화(§3.2, ADR 0015). |
| J32 | 귀속 각주 | 지킴 | 고정 필요 | **레드팀.** ADR 0013/0015/0025 공통 각주로 고정(§8). |

리뷰의 나머지(현재 상태 진단 §1~§7, C1·C2·C4~C8·C10~C13, I1·I2·I3(a)(c)(d)·I4·I8·I11·I12·I13, 모델군 선택, 정직한 상한 문장,
달성 불가 목록)는 레드팀이 채택했고 이 문서도 채택한다.

### 2.2 현재 상태 (확인된 사실만)

- **추천 단위·아이템 생애** (main): `news_letter`(`backend/app/models/news.py`)가 아이템, `news_letter_created_at = NOW()`,
  `raw_news_count`, `story_id` 없음. `ai_workspace/core/clustering/hdbscan_clusterer.py:86`의 `AND news_letter_id IS NULL`로
  배정된 기사는 영구 제외 → 같은 사건이 매일 새 ID로 재생성. 사용자 신호는 온보딩(`user_preferred_newsletter`,
  `user_preferred_categories`)과 클릭(`POST /logs/newsletter/click {news_letter_id}` → `user_newsletter_ctr_log(log_id, user_id,
  news_letter_id, created_at)`; `frontend/src/lib/api.ts` `sendNewsletterClickLog`가 유일한 호출). 노출·위치·request_id·체류는 없다.
- **팀 배치 추천** (main `ai_workspace/recommend_engine/`, 성승우): `main_lgbm.py --train --inference` = binary/lambdarank +
  클릭당 무작위 네거티브 5개(`_sample_negatives`, 제외 집합 `user_clicked_map`은 로그 전체) + 유저×전체 뉴스레터 Cartesian 추론 →
  `news_letter_today_batch`. 비개인화 인기 랭킹 `backend/scheduler/calculate_ranking.py::compute_scores = exp(−age_h/48) +
  log1p(raw_news_count)/5`. 클릭은 어디에도 들어가지 않는다. 재평가(`reports/recsys/team_repro_v2.md`, [synthetic team data], n=31 유저,
  3 seed): point-in-time MRR 0.5741±0.0582, P@5 0.3398 < popularity 0.4839 / onboarding-cosine 0.4452, 추론 시점 누출 효과
  MRR +0.1823 [+0.0350, +0.3060], `best_iteration=[35,1,1]`. 2차 어드버서리얼 검토: sound-with-caveats(blocker 2, major 4, minor 6,
  이력서 안전 문장 8), 브랜치 미반영.
- **EB-NeRD 하네스 + recsys_core** (`eval/ebnerd-harness 3df4df6`): `recsys_core/{events,features,candidates}.py`(numpy만;
  `EventIndex` 반열린 구간, `Requests` CSR, `compute_features` 그룹 recency/history/team_category/category/popularity/short_term),
  `evaluation/recsys/ebnerd/{prepare,models,run_ebnerd,make_report}.py`(`--chain inview|poolneg`, seed 3, 유저 부트스트랩 1,000회,
  기계 판정 `promotion_verdict`). 결과 [EB-NeRD] `reports/recsys/ebnerd_v1.{json,md}`(코드 `d27a700`) + `ebnerd_v1_1_poolneg.json`(`2d223a5`):
  - P1 nDCG@10 ranker_v2 0.6551 [0.6533, 0.6569] vs popularity_24h 0.5992 vs team_binary 0.3952(AUC 0.4671). ablation: lambdarank
    +0.0010 / 노출 그룹 −0.0027 / 노출 비클릭 네거티브 +0.1868 / trailing 인기도 +0.0571 / 단기·세션 +0.0088 / cat_share·hist_len +0.0089.
  - P2(48h 풀, 평균 235개, 20k 요청) nDCG@10 ranker_v2_poolneg 0.2686 [0.2640, 0.2728] vs popularity_6h 0.1168, recency 0.1114,
    team_binary 0.1814, ranker_v2(노출 네거티브) 0.0530, cosine_history 0.0324. poolneg − popularity_6h +0.1518 [+0.1464, +0.1573].
  - A1: P2 피처 사슬 +trailing 인기도 +0.0494 [+0.0464, +0.0523], +단기·세션 +0.0234 [+0.0213, +0.0258], +cat_share·hist_len
    +0.0147 [+0.0129, +0.0166]; team_binary 0.1814 ≈ poolneg_team_features 0.1812. MMR λ 0.5 vs 1.0: ΔILD +0.0094 [+0.0090, +0.0099],
    ΔnDCG −0.0025 [−0.0057, +0.0008].
  - 후보 합집합@50(pop6h/pop24h/recency/cosine) 평균 121개, 정답 91.6%(상한 92.7%), 2단계 0.2700 ≥ 0.2686. 실시간 재생
    +0.0100 [+0.0094, +0.0106](노출 68.9%), cosine_history만 갱신 −0.0027. BGE-M3 덴마크어 kNN LOO 0.8458(다수 클래스 0.2096).
  - poolneg의 LightGBM gain: pop_clicks_6h 0.540, hours_since_pub 0.288(seed 0; seed 범위 0.457~0.540 / 0.288~0.344).
  - ADR 0013 채택(shadow부터), 승격 규칙 R1–R4 통과(R1·R2는 ranker_v2, R3·R4는 poolneg — 다른 모델).
- **요청 시점 서빙** (`feat/realtime-recommendation 5e84358`): `RecommendationService.recommend` → 300ms 예산·워커 스레드(`workers=8`)·
  전용 풀 → `TTLCache((user_id, last_click_id), 60s)` → `RealtimeRecommender.recommend`: `build_user_state`(long_term=
  `"user".user_embedding`, short_term=24h 최근 20클릭 AVG, 콜드 체인 long_term→onboarding→category centroid(72h)→`_cold_popular`) →
  `generate_candidates`(knn_profile 100 / knn_short 100 / recent 100 / popular 100 / category 50, 라운드로빈 cap 300, 72h) →
  `clicked_among` 제외 → `HeuristicScorer`(0.45·cos_long + 0.35·cos_short + 0.15·exp(−age/48h) + 0.05·min(1, log1p(raw_news_count)/5),
  가중치는 `evaluation/serving/click_shift_sensitivity.py` 합성 점검의 사전값) → `CategoryBasedMMRReranker`(성승우) → top 20.
  `LightGBMScorer`는 `RECSYS_FEATURE_FN` 구현이 저장소에 없어 항상 폴백. 폴백 체인 batch(≤36h)→popular→recent→empty.
  노출 로그 `recommendation_impression_log(impression_id, request_id, user_id, news_letter_id, position, score, source, model_version,
  created_at)`(마이그레이션 `8b7f830013b7`, revises `e725a62ffef1`). 벤치 코드는 있으나 결과 파일 없음(J16).
- **잡·스케줄** (`feat/runtime-compose-and-collection b6d4c0c`): ingest 매시 5분, embed 매시 20분(40분 예산), popularity 매시 35분,
  cluster(통계) 일 1회, daily_report. generate/user_embed/train/batch_fallback 주석 처리. `jobs/tasks/train.py` = `main_lgbm.py --train
  --inference` 자식 프로세스; `jobs/tasks/user_embed.py` = `UserEmbedder().batch_update_all_users()`(NULL 유저만). Alembic head
  `d48994e9d26e`.
- **시뮬레이터** (`feat/user-simulator-loadtest b3b20e4`): ADR 0019(제안됨; 지표 타당성 격자 P1–P5·H1–H8 사전 등록, 결과 미실행),
  `sim/click_model.py` P(click)=(1/(r+1))^η·σ(w·φ+b), φ=[카테고리, Kiwi 명사 Jaccard, 언론사, exp(−age/24h), log1p(raw_news_count),
  반복], BGE-M3 코사인 제외, 무작위 top-10 CTR 2.00%(b=−5.0177)·오라클 11.76% 보정, 정책 5종 `fake_app.py`, Locust 파일 커밋됨.
- **데이터**: 로컬 DB `news_raw` 690(임베딩 649, 09-26 12:00 기준 리뷰 실측), `news_letter`·`"user"`·`user_newsletter_ctr_log` 0.
  17:00 KST 현재 colima 미실행·5433 응답 없음. EB-NeRD small 로컬 전용. 팀 합성 데이터는 화이트박스 진단 전용(ADR 0007).

### 2.3 정확성 문제 — 수정 필요 목록

우선순위 = "고치지 않으면 다른 증거가 막히는가". H=차단, M=결과 왜곡, L=위생.

| # | 심각도 | 문제 | 근거(파일) | 고치는 곳 |
|---|---|---|---|---|
| C1 | H | 클릭 로그에 노출 연결키 없음(`request_id`·`position`). in-view 라벨·propensity·위치 편향이 전부 (user, item, 시각) 근사 조인. 노출 로그에 propensity/explored/candidate_count/profile_source/cache_hit 없음 | `backend/app/api/log.py`, `models/log.py`, `models/recsys.py`, `frontend/src/lib/api.ts` | §3.6, M2 |
| C2 | H | LightGBM 경로 두 개: `jobs/tasks/train.py`는 EB-NeRD가 반증한 팀 레시피(무작위 네거티브·미래 클릭 제외 집합·Cartesian·신선도 필터 없음)를 그대로 실행하고 `model_registry`와 무관; `LightGBMScorer`는 feature_fn 없어 항상 폴백 | `jobs/tasks/train.py`, `recommend_engine/main_lgbm.py`, `backend/app/recsys/lgbm_scorer.py` | §3.4, M3 |
| C3 | H | 장기 벡터 미갱신: `user_embed` 잡은 NULL 유저만, `refresh_recently_active_users`는 미호출(realtime 브랜치에만 존재) | `jobs/tasks/user_embed.py`, `ai_workspace/core/user_embedder.py` | §3.3, M3 |
| C4 | H | 프로필 정의 3벌(UserEmbedder: exp(−0.05·days)=반감기 13.9일·pref 0.4·90일·L2 / 팀 `compute_history_embedding`: 7일 반감기·일수 내림·min_weight 0.01 / recsys_core: 7일 연속 감쇠·온보딩 미포함). ranker_v2는 recsys_core `hist_cos`로 학습됐는데 서빙은 `user_embedding` 코사인 | `ai_workspace/core/user_embedder.py`, `recommend_engine/src/data/data_loader.py`, `recsys_core/features.py`, `backend/app/recsys/pipeline.py` | §3.3, M3 |
| C5 | H | 서빙 인기도 = `raw_news_count`(클릭 아님). EB-NeRD 최강 피처 pop_clicks_6h의 한국어 대응물이 서빙에 없고, `raw_news_count`는 벤치마크에 대응물이 없다. recency 근거 `created_at=NOW()`는 배치 내 상수 | `backend/app/recsys/scoring.py`, `backend/scheduler/calculate_ranking.py` | §3.3, E5·E6, M5 |
| C6 | H | EB-NeRD 결과의 콜드 regime 전이 미측정: poolneg gain의 83%가 pop_clicks_6h+hours_since_pub인데 초기 서비스에서 pop_*≈0 | `reports/recsys/ebnerd_v1.json` importance_gain | E1, M4 |
| C7 | M | 아이템 정체성 부재 → `clicked_among`이 ID 단위라 어제 읽은 사건의 오늘판 재노출, 인기도 매일 리셋, 시뮬레이터 reactivity 오염 | `hdbscan_clusterer.py:86`, `sql_repository.clicked_among` | §3.1, E12→I7(조건부) |
| C8 | M | team_repro_v2 어드버서리얼 blocker 2·major 4 미반영(퇴화 as-written 행, 이력서 #2·#3 귀속, 단일 트리 seed, padded_400·small_recent_15, ADR 0007 과장 5곳, cold 폴백 CSV 순서) | `evaluation/recsys/team_repro/{pipeline,make_report_v2}.py`, `docs/adr/0007` | M1 |
| C9 | M | 승격 규칙 약점: R3 기준선 cosine_history(0.0324)는 popularity_6h·recency보다 훨씬 약한 축; R1·R2와 R3·R4가 다른 모델; 최소 효과 크기 없음(+0.0010도 "기여"); P2 48h vs 서빙 72h; 단일 split | `docs/adr/0013`, `make_report.promotion_verdict` | M7(ADR 0013 A2, I12) |
| C10 | M | 한국어 로그 학습 시 네거티브 결정 규칙 없음(노출 비클릭 / 후보-미노출 / 풀 무작위) | 없음 | §3.7 |
| C11 | L | MMR min-max 정규화가 콜드 유저의 준상수 점수에서 노이즈 지배; λ 구간(0.8/0.7/0.6)이 온보딩 카테고리 수에 묶인 가정 미검증 | `recommend_engine/src/core/reranker.py:47-52` | §3.5 |
| C12 | L | 노출 로그 유실율 미측정(BackgroundTasks 실패 시 카운터만); 캐시 적중이 로그에 구분 안 됨 | `service.log_impressions` | §3.6 |
| C13 | L | 상수 피처 `user_age_band`/`user_gender`, `config.yaml` `device: cuda`, Cartesian 경고 주석만 | `data_loader.py:365-368`, `feature_engineer.py:154-155` | M3(팀 레시피 은퇴와 함께) |
| C14 | L | 파워 계산 부재; "A/B로 검증"은 초기 규모에서 실현 불가 | 없음 | §4 E13, ADR 0025 |
| C15 | M | 단기 벡터 정의 불일치(서빙 24h 최근 ≤20 AVG vs recsys_core 24h 전부 합) → parity 실패 지점 | `RecsysConfig.short_term_max_clicks`, `recsys_core.features.window_vectors` | §3.3 |
| C16 | M | 후보 생성기 구성 불일치(서빙 5출처 100/100/100/100/50·cap 300·72h vs 하네스 4출처·상위 50·48h) → 인용된 2단계 수치가 서빙 구성의 것이 아님 | `pipeline.generate_candidates`, `run_ebnerd.py` | E8 |
| C17 | H | shadow 스코어링 경로 부재(`ScoreResult` 점수 1개, 로그 `score` 1컬럼) → ADR 0013 결정 2·4가 실행 불가 | `backend/app/recsys/{types,scoring,service}.py` | §3.4, M2 |
| C18 | M | 노출 피로 억제 없음: seen 제외가 클릭만 보므로 N번 무클릭 노출 아이템이 매 요청 재등장(작은 풀에서 심함) | `pipeline.recommend` | §3.2, E10 |
| C19 | L | ADR 번호 충돌: 코드가 0017(단기 저장소 벤치)·0026(Tier 0 호스팅)을 이미 참조; 리뷰·레드팀은 같은 번호를 콜드스타트·parity에 배정 | `docs/decision-map` 2절 | §7 |
| C20 | H(전제) | 수집 연속성 깨짐(colima 미실행, launchd embed 종료 코드 1). 모든 [KR-*] 일정의 전제 | 로컬 실행 | M0(도메인 밖, 확인만) |
| C21 | H | 풀 크기 regime 미측정: 한국어 후보 45~90개 vs EB-NeRD P2 235개. \|pool\|≈2~4K에서는 top-20 집합이 거의 같고 순서만 갈려 nDCG·OPE 감도가 구조적으로 작다 | `p2_info.pool_size` | E1 풀 축소 조건, E13 1차 지표 |
| C22 | L | `train` 은퇴 후 `news_letter_today_batch`의 유일한 기록자는 `batch_fallback`(인기 재배열)이라 폴백 `batch` 단계가 콜드 체인과 같은 목록을 되돌림 | `jobs/tasks/batch_fallback.py`, `service._fallback` | §3.2 |
| C23 | L | `RecsysConfig.workers=8`은 ARM 2~4 vCPU에 과다; 예산 초과 시 스레드가 쌓인다 | `backend/app/recsys/config.py` | §3.8 |

---

## 3. 목표 설계

### 3.1 추천 단위와 아이템 정체성 (결정)

- **추천 단위는 LLM 생성 뉴스레터로 유지한다.** 기사 단위 추천은 제품(뉴스레터)의 가치를 우회하고 ADR 0023의 본문 30일 보존
  경계와 충돌하며, 사용자 클릭도 뉴스레터에만 존재한다. 스토리 단위 추천(하나의 사건을 하나의 아이템으로)은 **정체성 계층**으로만
  들어간다: `news_letter.story_id`(nullable)를 (a) seen/클릭 제외, (b) 후보 dedup(최신판만), (c) 인기도·breadth 누적 키로 쓴다.
  적용은 E12(연속 사건 비율 ≥20%)를 조건으로 한다(리뷰 I7 = 종합 리뷰 D1과 동일).
- **후보 풀 크기 규칙**: 신선도 창은 고정 72h가 아니라 `[48h, 120h]` 안에서 "제외 후 eligible ≥ 3K(=60)"를 만족하는 최소 창으로
  잡는다(`RECSYS_MIN_ELIGIBLE=60`). 풀이 60 미만이면 창을 120h까지 늘리고, 그래도 부족하면 그대로 낸다(`eligible_count`를 로그에 남겨
  분석에서 층화). 근거: C21.
- 생성 캡(하루 15~30건)은 LLM 도메인의 결정이다. 추천 도메인은 캡이 곧 카탈로그 크기라는 사실만 계약으로 받는다.

### 3.2 요청 경로 (구성요소)

```
GET /newsletters/today                                      backend/app/api/newsletter.py
└ RecommendationService.recommend(user_id)                   backend/app/recsys/service.py
   ├ 캐시 (user_id, last_click_id) → 결정론 부분(DeterministicList)만 저장, TTL 60s
   ├ RealtimeRecommender.recommend                           backend/app/recsys/pipeline.py
   │   ├ build_user_state → UserState v2 (§3.3)
   │   ├ generate_candidates: knn_profile / knn_short / recent / popular_clicks / category, 라운드로빈 cap 300, 동적 창(§3.1)
   │   ├ exclusion: clicked(story 단위, I7 후) ∪ fatigued(48h 내 무클릭 노출 ≥3) → eligible E
   │   ├ features = recsys_core.serving.features(state, items, now)      (§3.3, feature_names 고정)
   │   ├ ScorerStack: active + shadows(예산 초과 시 shadow 생략)             (§3.4)
   │   ├ rerank: MMR λ=0.5(잠정) → 상위 K_det = K − n_e                     (§3.5)
   │   └ DeterministicList{ids, scores, det_rank, eligible_ids, feature rows(상위 K_det), extra_scores}
   ├ 탐색(캐시 뒤, 요청마다): n_e 슬롯 위치 균등 무작위, 아이템은 E \ det_list에서 균등 추출, propensity 기록 (§3.5)
   ├ 응답: 20개 + 헤더 X-Request-Id / X-Rec-Source / X-Model-Version
   └ BackgroundTasks: request 행 1 + slot 행 K 기록, 실패 카운터 (§3.6)
```

- **콜드 경로 통합**: 현재 `_cold_popular`(개인 신호 없음 → compute_scores 상위 20)는 별도 분기다. v2에서는 같은 파이프라인을 타되
  `profile=None`이면 후보 = recent ∪ popular_clicks(∪ category), 활성 스코어러는 recency·인기도 항만 쓰고, `n_e=RECSYS_EXPLORE_SLOTS_COLD=4`.
  `profile_source ∈ {long_term, onboarding, category, none}`을 요청 로그에 남긴다. `RECSYS_MIN_PERSONAL_EVENTS=k*`(E2) 미만이면
  hist/short 항의 가중치를 0으로 두고 onboarding 항으로 넘긴다(피처 자체는 계산·로그).
- **폴백 체인**: realtime 모드에서 `batch` 단계 제거 → `popular → recent → empty`(C22). `news_letter_today_batch`는 `RECSYS_MODE=batch`
  전용 레거시로만 남기고 `batch_fallback` 잡도 그 모드에서만 의미가 있음을 ADR 0015에 적는다. `X-Rec-Source` 값 집합에서 `batch`는
  batch 모드에서만 나온다.
- **노출 피로 규칙**(C18): 같은 유저에게 최근 48h 동안 무클릭 노출이 3회 이상인 아이템은 결정론 목록과 탐색 풀에서 모두 제외한다
  (`SqlRecsysRepository.fatigued_among(user_id, ids, since, min_impressions=3)`; 노출 로그 인덱스 `(user_id, created_at DESC)` 존재).
  탐색 풀에서 빼도 `eligible_count`를 요청마다 기록하므로 propensity는 정확하다. 활성화 전에는 로그 전용(`RECSYS_FATIGUE_MODE=log|enforce`),
  E10에서 [SIM] 전후 지표로 검증한 뒤 enforce. 문헌: LinkedIn 노출 감쇠(Lee et al. 2014), Yahoo 반복 노출 피로(Agarwal et al. 2009).

### 3.3 유저 상태·피처 (train/serve parity의 핵심)

**단일 피처 구현 = `recsys_core`.** 서빙은 `recsys_core/serving.py`의 어댑터로만 피처를 만든다. 피처 이름과 순서는
`recsys_core.features.feature_groups()`가 유일한 출처이며 `model_registry.feature_names`와 요청마다 비교한다(이미 `LightGBMScorer`가 함).

| 그룹 | 피처(recsys_core 이름) | 서빙 입력 | EB-NeRD 대응 | 전이 라벨 |
|---|---|---|---|---|
| recency | hours_since_pub, is_fresh_24h, is_fresh_7d | `news_letter_created_at` | 발행 시각 | 검증됨(단, 배치 내 상수 한계) |
| history | hist_cos, hist_len | 증분 상태 `hist_sum`, `hist_len`(아래) | 21일 history + 창 내 클릭 | 검증됨 |
| team_category | news_category, cat_match_count, is_cat_match, user_ncat | 온보딩 카테고리 | 과거 상위 3 카테고리(적응판) | 적응판 |
| category | cat_share | 증분 `hist_cat_counts int[7]` | 히스토리 카테고리 분포 | 검증됨 |
| popularity | pop_clicks_6h/24h/48h, pop_inviews_24h, pop_ctr_24h (+ pop_ctr_shrunk_24h, E6) | `user_newsletter_ctr_log`·`recommendation_impression_log` 창 집계 | 행동 로그 | 검증됨(콜드 regime은 E1) |
| short_term | short_cos, short_len, sess_cos, sess_len, hours_since_last_event | 24h 내 최근 ≤20 클릭(`short_max_events=20`), 세션 = 30분 갭 | 24h 이벤트, EB-NeRD session_id | 세션 정의 차이 → parity 테스트에서 같은 sessionizer 적용 |
| onboarding(신규) | onb_cos | `user_preferred_newsletter` 평균 벡터 | 없음 | **전이 미검증** 라벨 고정 |
| breadth(신규, E5 후) | raw_news_count, press_count | 클러스터 통계 | 없음(E5 대리) | 미검증, 0.05 동점 깨기 |

**장기 프로필의 증분 상태(C3·C4 해소).** 반감기 h=7일 지수 감쇠는 분리 가능하다:
v(t) = Σ_i 2^{−(t−t_i)/h} e_i = 2^{−(t−a)/h} · S(a), S(a) = Σ_i 2^{−(a−t_i)/h} e_i.
`"user"`에 `hist_sum vector(1024)`, `hist_anchor_ts timestamptz`, `hist_len int`, `hist_cat_counts int[]`를 두고, 클릭 이벤트(t_c, e)마다
`hist_sum ← hist_sum · 2^{−(t_c − anchor)/h} + e; anchor ← t_c; hist_len += 1`(float64 누적, 클릭 API 트랜잭션 안에서 1 UPDATE).
요청 시각 t의 hist_cos는 코사인이 양의 스칼라에 불변이므로 `cos(hist_sum, e_item)` — 감쇠 곱셈도 필요 없다. 이는 recsys_core
`_history_cosine_segments`의 분해식과 같은 정의이며, 온보딩은 포함하지 않는다(onb_cos 별도). `UserEmbedder`(pref 0.4·13.9일)와
`user_embed` 잡은 은퇴, `"user".user_embedding`은 한 릴리스 동안 읽지 않다가 제거. 재구축 잡 `rebuild_user_state`(클릭 로그에서
recsys_core 정의로 전량 재계산)가 있어야 서빙 상태가 캐시임이 보장된다. recsys_core에는 `FeatureContext.user_hist_state`(선택 입력)를
추가하고, 있으면 EventIndex 대신 그것을 쓴다. 단위 테스트: 같은 로그에서 두 경로의 hist_cos max|Δ| < 1e-6(float64).

**서빙 어댑터 인터페이스** (`recsys_core/serving.py`):
```python
def features(state: UserState, items: Sequence[Item], now: datetime) -> np.ndarray  # (len(items), n_features)
features.feature_names: list[str]  # = 순서 고정된 feature_groups() 평탄화
```
내부: `ItemCatalog(ids, emb, pub_time, category)` ← items; `Requests(user=[0], time=[t], cand_ptr=[0, n], cand_item=arange(n))`;
`FeatureContext(user_log=EventIndex(24h 클릭 ≤20), session_log=sessionize(30분), item_clicks/item_inviews=창 집계에서 복원한
EventIndex(48h), static_categories=온보딩, user_hist_state=(hist_sum, hist_len, hist_cat_counts), config=FeatureConfig(short_max_events=20))`.
`Item`에 `category_id`를 추가한다(현재 없음). 배선: `RECSYS_FEATURE_FN=recsys_core.serving:features`.

**parity 게이트**(종합 리뷰 D4 + J26): `tests/recsys/test_feature_parity.py`, CI integration job.
(1) 시드 DB에서 `/newsletters/today` 200요청 재생, 서빙 어댑터 피처 vs 오프라인 하네스 경로(같은 로그를 `prepare.py` 방식으로 적재)
전 컬럼 max|Δ| < 1e-6; (2) 같은 후보 집합에서 점수 Kendall τ = 1.0(동점 제외); (3) end-to-end 목록 top-20 겹침 ≥ 0.9(후보 집합
차이 허용 층); (4) **후보 생성기 구성 동일성**(출처·k·창·cap이 `RecsysConfig`와 하네스 `--candidate-config`에서 같은 값). 결과는
`reports/recsys/parity_v1.json`.

### 3.4 모델군 (결정과 기각 근거)

| 대안 | 판정 | 근거 |
|---|---|---|
| A. LightGBM LambdaRank, 쿼리=요청, recsys_core 스칼라 피처(ranker_v2_poolneg 계열) | **shadow 주모델**, E1 통과 시 활성 후보 | [EB-NeRD] P2 0.2686 vs popularity_6h 0.1168; RecSys Challenge 2024 상위권이 시간 인식 피처+GBDT; EB-NeRD 논문 Table 3에서 NRMS 61.03 vs 인기 59.70 AUC. 하이퍼파라미터는 팀 설정 고정(num_leaves 31, lr 0.05, feature_fraction 0.9, bagging 0.8/5, early stop 50, ≤1000 라운드), `lambdarank_truncation_level` 기본 30 > K=20이라 그대로. E15에서는 이 고정 설정이 판정 기준 arm이고, 같은 시행 예산의 튜닝 arm(`*_tuned`)을 서술용·게이트로 함께 잰다(§4.1.4). |
| B. 인기도 마스킹 학습 변형(poolneg_masked, E1) | E1 결정 규칙에 따라 shadow 주모델 교체 | 콜드 regime에서 pop_*=0 입력을 학습 중에 본 모델만이 초기 서비스에서 퇴화하지 않는다는 가설. |
| C. 4항 휴리스틱(cos_long, cos_short, recency, log1p(pop_clicks_6h)) + 0.05 raw_news_count 동점 깨기, 가중치는 E7 적합 | **활성 스코어러**(초기), 랭커 폴백 | 사람 클릭 데이터에 적합된 유일한 활성 모델. 저트래픽 세트 기본, 일 노출 ≥5k에서 poolneg 세트로 전환(ADR 0014 사전 등록). |
| D. NRMS-lite(고정 BGE-M3 벡터 → 학습 투영, MHSA + additive attention 유저 인코더, 후보 내적) / two-tower 계열 | **E15로 측정 판정(보류)** — 판정 비교·게이트 통과 시 추가 shadow 후보, 활성 아님(§4.1.5) | GBDT가 이길 이유(원 기각 사유를 가설로 유지): zero-shot 교차언어 붕괴(NaSE), GPU 의존, 15k 유저 과적합, EB-NeRD 논문 Table 3에서 NRMS 61.03 vs 인기 59.70 AUC. 반대 가설: 후보의 1024차원 벡터를 직접 보는 학습형 유저 인코더는 hist/short/sess 코사인 스칼라보다 정보가 많다. 다국어 인코더 **미세조정**·ID 임베딩은 여전히 기각(15k 유저·언어 전이). |
| E. 세션 시퀀스 모델(SASRec-lite; GRU4Rec은 대표 1개로 대체) | **E15로 측정 판정(보류)** — 콜드 조건(k≤5)에서 악화 없음이 채택 조건 | GBDT가 이길 이유(원 기각 사유를 가설로 유지): 세션당 조회 ≤4, 풀 45~90개, short/sess 코사인 피처가 같은 정보를 담고 [EB-NeRD] +0.0234로 이미 측정됨. E15는 이 가설을 같은 프로토콜에서 검정한다. |
| I. 스태킹: LightGBM LambdaRank + D/E 중 나은 arm의 OOF 점수 피처 | **E15로 측정 판정(보류)** + 서빙 비용 게이트(CPU p95 추가 지연, M6 [LOAD-arm]에서 측정) | RecSys Challenge 2024 1위 해법(":D", Transformer + LightGBM + CatBoost 3단계, 시간 인식 피처)의 구조. 오프라인 이득이 있어도 요청 경로에 신경망 인코딩 1회가 들어가므로 지연 게이트를 따로 둔다. |
| F. 정책 학습형 밴딧(LinUCB/Thompson) | 지연(실트래픽 후) | 지금 필요한 것은 정확한 propensity(ε-균등)이지 정책 최적화가 아니다. 아이템 콜드스타트는 pop 피처+탐색 슬롯이 담당. |
| G. LLM 관심 프로필 → BGE-M3 코사인 | 보류(≤$5, 한국어 클릭 수천 건 후) | 콜드 유저(k≤5)에서만 이득 가설(LettinGo). EB-NeRD 제목 외부 API 반출은 라이선스 확인 필요. |
| H. 카테고리 캘리브레이션(Steck 2018) | 선택(MMR 대안) | λ 튜닝보다 해석이 쉽고 C_KL로 측정 가능. 스택 추가 없이 `reranker` 교체 가능한 인터페이스만 둔다. |

**ScorerStack(C17)**: `backend/app/recsys/scoring.py`
```python
@dataclass
class ScoreResult:
    scores: np.ndarray; model_version: str
    extra_scores: dict[str, np.ndarray] = field(default_factory=dict)   # shadow 모델버전 → 점수
class ScorerStack(Scorer):
    def __init__(self, active: Scorer, shadows: Sequence[Scorer], deadline_fraction: float = 0.5): ...
    # active 먼저; 남은 예산이 deadline_fraction 미만이면 shadow 생략하고 counters["shadow.skipped"] += 1
```
`model_registry`에 `role varchar(16) NOT NULL DEFAULT 'shadow'`(`active` 부분 UNIQUE는 유지, `shadow` 다수 허용)를 추가하고
`LightGBMScorer`는 role별 인스턴스로 만든다. shadow 점수는 slot 로그 `scores_shadow jsonb`로 남긴다. shadow 모델은
EB-NeRD 학습 모델을 `model_registry`에 `feature_names`와 함께 적재하는 스크립트 `scripts/register_model.py`로 올린다.

### 3.5 다양성·탐색·콜드스타트

- **MMR**: 팀 구현(`CategoryBasedMMRReranker`) 유지, λ=0.5 잠정(ADR 0013 결정 5; [EB-NeRD] ΔILD +0.0094, ΔnDCG 비용 미측정).
  C11의 min-max 문제는 콜드 유저에서만 발생하므로 콜드 경로는 MMR 대신 카테고리 라운드로빈(온보딩 카테고리 층화)으로 K_det를 채운다.
  카테고리 캘리브레이션은 `Reranker` Protocol만 두고 구현은 선택 과제.
- **탐색 슬롯(ε-균등)**: K=20, n_e=2(warm)/4(cold). 슬롯 위치 P ⊂ {0..K−1}를 균등 무작위(비복원)로 뽑고, 아이템은 E' = E \ det_list에서
  균등 비복원 추출, 무작위 배정. 결정론 아이템은 det_rank 순으로 나머지 슬롯을 채운다.
  - propensity(item i가 position p): 탐색 아이템 = (n_e/K)·(1/|E'|). 결정론 아이템(det_rank r)은 위치가 p로 밀릴 확률 =
    hypergeometric(p−r개의 탐색 슬롯이 앞 p개 위치에 있을 확률) — 로그에는 `det_rank`, `explore_positions`, `eligible_count`를 남겨
    사후에 정확히 계산한다(컬럼 `propensity`는 탐색 아이템만 채우고 결정론 아이템은 NULL).
  - 추출은 **캐시 뒤에서 요청마다** 수행한다(캐시는 DeterministicList만 저장). 60초 안의 재요청도 탐색 아이템이 독립이다.
  - 사용자 없는 지금 제품 손실은 0이며, 평균 품질 최대 10%(2/20) 저하는 사용자 생긴 뒤 `RECSYS_EXPLORE_SLOTS`로 조정.
- **OPE**(`evaluation/recsys/ope.py`): 1차 = replay(Li et al. 2011)를 **탐색 슬롯 한정**으로(타깃 정책이 같은 (아이템, 위치)를 택한
  로그만 채점, 매칭 확률 ≈ 2·k_target/|E'|). 2차 = SNIPS(전체 슬레이트, 결정론 슬롯은 타깃과 일치할 때만 기여). ESS는 탐색 슬롯
  표본으로 정의. 위치 편향 η는 탐색 슬롯(위치가 균등 무작위)에서 PBM으로 추정한다 — 개입 수확(Agarwal et al. 2019)의 특수한 경우다.
- **콜드스타트 체인**: `long_term(hist_len ≥ k*) → onboarding → category centroid → none`. k*는 E2. 온보딩 화면은 현행 유지(J9).

### 3.6 데이터 모델 (마이그레이션 단위)

M0에서 merge revision(`down_revision = ('d48994e9d26e', '8b7f830013b7')`) 후 아래 revision들을 순서대로.

1. **클릭 로그** `user_newsletter_ctr_log` + `request_id uuid NULL`, `position smallint NULL`, `event varchar(16) NOT NULL DEFAULT 'click'`
   (`click|detail_view`), `dwell_ms int NULL`. API `LogRequest{news_letter_id, request_id?, position?, event?, dwell_ms?}`;
   프런트 `sendNewsletterClickLog(newsLetterId, requestId, position)`은 `/newsletters/today` 응답 헤더 `X-Request-Id`와 목록 인덱스를 되돌린다.
   인덱스 `(request_id)`.
2. **요청 로그** `recommendation_request_log(request_id uuid PK, user_id int, created_at timestamptz, source varchar(32), model_version
   varchar(64), profile_source varchar(16), cache_hit bool, candidate_count smallint, eligible_count smallint, explore_positions smallint[],
   candidate_ids int[] /*≤300*/, feature_schema_version smallint, latency_ms int, shadow_versions text[])`. 후보 id 배열은 풀 네거티브
   표집·replay 판정용이다(약 1.2KB/요청).
3. **슬롯 로그** = 기존 `recommendation_impression_log` + `explored bool NOT NULL DEFAULT false`, `propensity float NULL`, `det_rank smallint NULL`,
   `scores_shadow jsonb NULL`, `features bytea NULL`(float32[n_features], 상위 K_det 결정론 아이템 + 탐색 아이템 전부 = K행). 크기
   ≈ 20×27×4B ≈ 2.2KB/요청 → 월 10만 요청이면 ≈220MB(허용; 보존 90일 후 features만 NULL 처리하는 잡 `retain_slot_features`).
   피처 스냅샷의 용도는 parity CI(max|Δ|<1e-6 단언)와 학습 데이터 재현 검증이며, 학습 피처 자체는 이벤트 로그에서 recsys_core로
   재계산한다(J11).
4. **유저 상태** `"user"` + `hist_sum vector(1024) NULL`, `hist_anchor_ts timestamptz NULL`, `hist_len int NOT NULL DEFAULT 0`,
   `hist_cat_counts int[] NULL`. (`user_embedding`은 유지 후 다음 릴리스에서 제거.)
5. **모델 저장소** `model_registry` + `role varchar(16) NOT NULL DEFAULT 'shadow'`; `is_active`는 `role='active'`와 동기(체크 제약).
6. **(조건부, I7)** `news_letter.story_id int NULL` + 인덱스; `clicked_among`·`fatigued_among`·인기도 창 집계를 story 키로.

로그 유실율: `service.log_impressions` 실패 카운터를 `/recsys/stats`와 `daily_report`에 노출(`impressions.failed / impressions.logged`),
목표 <0.5%. 캐시 적중은 `cache_hit`로 구분.

### 3.7 한국어 로그로 학습할 때의 규칙 (사전 등록, 활성화 임계 포함)

- **시작 조건**: 클릭 ≥2,000건 AND 요청 ≥5,000건 AND 탐색 슬롯 노출 ≥10,000건. 그 전에는 EB-NeRD 학습 모델만 shadow.
- **쿼리** = request_id. **양성** = 그 요청에서 클릭된 슬롯(request_id 조인). **네거티브 두 변형을 항상 함께 학습**: (a) 같은 요청의
  무클릭 슬롯(P1형, in-view), (b) `candidate_ids`에서 무작위 20개(P2형, 풀). 선택은 검증 표본을 보지 않고 **탐색 슬롯 replay
  추정 CTR**(정확한 propensity가 있는 유일한 표본)로 한다. 두 변형이 replay CI로 갈리지 않으면 (b)를 쓴다(ADR 0013 결정 2와 일관).
- **피처** = 이벤트 로그에서 recsys_core로 재계산(point-in-time 자료구조), 슬롯 로그 `features`와 max|Δ|<1e-6 단언.
- **분할** = 시간순(마지막 7일 test, 그 앞 1일 es), 유저 부트스트랩 CI, seed 3. 최소 효과 크기 nDCG@10 0.005(I12).
- **탐색 슬롯 로그의 가중치**: 학습에는 propensity 역가중 없이 넣되(작은 표본), OPE에서만 가중.

### 3.8 지연·인프라 (ARM VM)

- 목표: `/newsletters/today` p95 < 300ms(예산과 동일), 폴백률 < 5% @20 RPS. 요청 경로 비용: exact `<=>` kNN 2회(N≈수천, 72~120h 창 필터),
  창 집계 1회(GROUP BY news_letter_id over 48h 로그), 유저 상태 1행, 아이템 캐시, recsys_core 피처(≤300행 × 27피처, 히스토리 코사인은
  1 dot/후보), LightGBM predict(≤300행 <5ms), shadow N개. 임베딩 계산은 요청 경로에 없다.
- 설정: `RECSYS_WORKERS = min(4, 2·vCPU)`(C23), `statement_timeout` 유지, `RECSYS_SHADOW_MAX=2`.
- HNSW 미채택: N≥5만·필터 선택도 높을 때만 pgvector 0.8 iterative scan 재검토(ADR 0016 한 문단, E11 실측 첨부).
- 측정 환경 라벨: Mac M2 = [LOAD-mac], OCI A1 Flex(2~4 vCPU) 확보 후 = [LOAD-arm]. 두 표를 나란히 둔다.

### 3.9 파일 단위 변경 계획

| 마일스톤 | 파일 | 변경 |
|---|---|---|
| M0 | `backend/alembic/versions/<merge>_merge_runtime_realtime.py` | merge revision(d48994e9d26e + 8b7f830013b7) |
| M0 | 브랜치 `integrate/recsys-v2` | ebnerd-harness → realtime → runtime → simulator 순 병합(decision-map 4.6: 각 브랜치를 `9aa235e` 위로 rebase 후) |
| M1 | `evaluation/recsys/team_repro/{pipeline,make_report_v2,run_repro,baselines,metrics}.py`, `docs/adr/0007`, `reports/recsys/team_repro_v2.*` | C8 6건 |
| M2 | `backend/app/api/log.py`, `backend/app/models/log.py` | request_id/position/event/dwell_ms |
| M2 | `backend/app/models/recsys.py`, `backend/alembic/versions/<v2_logs>.py` | request_log 신설, slot 로그 컬럼, model_registry.role |
| M2 | `backend/app/recsys/types.py` | `ScoreResult.extra_scores`, `DeterministicList`, `Recommendation.explore_positions/propensities/eligible_count` |
| M2 | `backend/app/recsys/scoring.py` | `ScorerStack` |
| M2 | `backend/app/recsys/exploration.py`(신규) | 슬롯 추출·propensity·hypergeometric 유틸 |
| M2 | `backend/app/recsys/service.py` | 캐시 뒤 탐색, 요청/슬롯 로그 행 구성, 유실 카운터, 폴백 체인 단순화 |
| M2 | `backend/app/recsys/pipeline.py` | 콜드 경로 통합, 동적 창, 피로 제외, 결정론 목록 반환 |
| M2 | `backend/app/recsys/sql_repository.py` | `fatigued_among`, `popularity_window_counts`, `user_hist_state`, 요청 로그 writer |
| M2 | `backend/app/recsys/config.py` | `explore_slots`, `explore_slots_cold`, `min_eligible`, `freshness_hours_max`, `fatigue_mode`, `shadow_max`, `min_personal_events` |
| M2 | `frontend/src/lib/api.ts`, `frontend/src/...`(목록 컴포넌트) | 클릭에 request_id·position 전달 |
| M2 | `evaluation/recsys/ope.py`(신규), `sim/experiments.py` | replay/SNIPS/ESS, OPE 검증 시나리오 |
| M2 | `tests/recsys/test_exploration_slots.py`, `tests/integration/test_logs_join.py` | propensity 합=1 검사, 200회 조인 1:1 |
| M3 | `recsys_core/serving.py`(신규), `recsys_core/features.py` | 어댑터, `user_hist_state`, `short_max_events`, sessionizer |
| M3 | `backend/app/recsys/runtime.py`, `lgbm_scorer.py` | `RECSYS_FEATURE_FN` 배선, role별 스코어러 |
| M3 | `backend/app/api/log.py`, `jobs/tasks/rebuild_user_state.py`(신규) | 클릭 시 증분 갱신, 전량 재구축 |
| M3 | `jobs/tasks/train.py` → `JobSkipped('team_recipe_retired')`, `jobs/tasks/user_embed.py` 삭제, `docker/crontab` 주석 갱신 | C2·C3 |
| M3 | `tests/recsys/test_feature_parity.py`, `.github/workflows/ci.yml` | parity 게이트 |
| M3 | `scripts/register_model.py`(신규) | EB-NeRD 모델 → model_registry(role=shadow) |
| M4 | `evaluation/recsys/ebnerd/{models,prepare,run_ebnerd}.py` | `--chain cold`, `--pop-mask p`, `--pop-subsample f`, `--pool-shrink n`, `--hist-truncate k`, `--rank-normalize`, `--p3-quantize`, `--candidate-config serving` |
| M4 | `evaluation/recsys/ebnerd/make_report.py` | `promotion_verdict(min_effect=0.005, p2_baseline='popularity_6h')` 인자화 |
| M4b | `evaluation/recsys/ebnerd/neural/{__init__,sequences,datasets,models,train,tune,stack,cold,report}.py`, `evaluation/recsys/ebnerd/run_neural.py`(신규) | E15 arm B/C/D(/E), OOF 스태킹, 콜드 조건 채점, `neural_verdict`(§4.1.8) |
| M4b | `evaluation/recsys/ebnerd/{models,run_ebnerd}.py` | `SEEN_FILTER_METHODS`에 신경망 arm 추가, `MetricBank`·`_features`를 `run_neural`이 import(동작 변경 없음) |
| M4b | `scripts/e15_colab_run.sh`, `requirements-colab.txt`, `tests/recsys/test_neural_demo.py` | 업로드·sha256 검증·실행·다운로드·삭제·해제·CU 기록 절차 고정; 버전 고정; `ebnerd_demo --fake-dim 16` CPU 테스트 |
| M4b | `reports/recsys/ebnerd_v1_3_neural.{json,md}`, `docs/adr/0013` A3 | E15 결과·기계 판정 |
| M5 | `recsys_core/features.py`, `backend/app/recsys/scoring.py`, `backend/scheduler/calculate_ranking.py` | pop_ctr_shrunk, 휴리스틱 4항+동점 깨기, 배치 인기 랭킹 클릭 항 |
| M5 | `evaluation/recsys/ebnerd/heuristic_fit.py`(신규) | 두 조건 가중치 적합 |
| M6 | `tests/recsys/test_latency_microbench.py`, `sim/locustfile.py`, `reports/serving/latency_v1.md`, `reports/sim/ope_validation.md` | 실측 기록 |
| M8(조건부) | `backend/app/models/news.py`, `core/clustering/story_linker.py`, `sql_repository.py`, `sim/catalog.py` | story_id 최소판 |

---

## 4. 사전 등록 실험 계획

공통: LLM 비용 $0. 실행 전에 명령·판정 규칙을 ADR(0013 A2 / 0013 A3 / 0014 / 0025 / 0031)에 커밋한 SHA를 리포트 머리말에 적는다. 다중 비교 보정
없음 — 판정용 비교 수를 각 실험에 명시하고 나머지는 서술용. 최소 효과 크기 nDCG@10 0.005. 결과가 나온 뒤 사전 등록 절은 고치지 않는다.

| ID | 라벨 | 가설 | 방법 | n·시드 | 성공/결정 기준 | 비용·시간 |
|---|---|---|---|---|---|---|
| E1 | [EB-NeRD] 콜드 regime 전이 | 인기도를 학습 중 마스킹한 모델은 pop=0·저트래픽·작은 풀에서 덜 퇴화한다 | poolneg vs poolneg_masked(학습 시 pop_* 그룹을 p∈{0.5}로 0 처리, 0 vs NaN 두 방식) × 평가 조건 {원본, pop_*=0 강제, 유저 서브샘플 1%/5%/20%로 인기도 재계산(평가 요청도 서브샘플에서), 풀 축소 40/60/90개(정답 포함 무작위 서브풀)} | P2 20k 요청(서브샘플은 가용 전부), seed 3, 부트스트랩 1,000 | 판정 비교 2개: (a) pop=0 조건에서 masked − unmasked CI 하한 > 0.005 → masked를 shadow 주모델로; (b) 1% 서브샘플·풀 60 조건에서 어느 모델도 recency+cosine 휴리스틱을 CI로 못 넘으면 활성은 휴리스틱 유지 | M2 약 1.5~2h |
| E2 | [EB-NeRD] 콜드 유저 절단 k* | 개인화가 인기도를 이기는 최소 히스토리 길이가 존재한다 | 요청 시점 기준 최근 k∈{0,1,3,5,10,all} 이벤트로 절단, P1·P2 모두 곡선; 온보딩 대체물(상위 3 카테고리)은 이벤트로 세지 않음(사전 등록) | E1과 같은 실행 | k* = P2/poolneg에서 personalization − popularity_6h CI 하한 > 0인 최소 k → `RECSYS_MIN_PERSONAL_EVENTS`; P1 곡선은 서술용 | E1에 포함 |
| E3 | [EB-NeRD] 랭크 정규화(언어 전이 계약 1부) | 연속 피처를 요청 내 랭크(0–1)로 바꿔도 손실이 작다 | poolneg-rank 학습·평가, ΔnDCG(rank−raw) | E1과 같은 실행 | ≥ −0.01이면 "계약 충족"; KS 표는 [KR-eval]로 이월 | E1에 포함 |
| E4 | [EB-NeRD] P3 일일 배치 릴리스(부가 표) | 릴리스 후 경과 버킷별로 콘텐츠→인기도 교차가 있다 | 발행 시각을 그날 07:00으로 양자화, 후보 = 당일(+전일) 배치 − seen, 버킷 {0–2h, 2–6h, 6–12h, 12–24h, 24h+}별 cosine_history / popularity_6h / poolneg nDCG@10 | 같은 실행 | 판정 없음(형태만 보고, h*는 정성적) | E1에 포함 |
| E5 | [EB-NeRD, 선택] 사건 보도 폭 대리 피처 | 24h 창 코사인 ≥τ 컴포넌트 크기가 클릭을 예측한다 | breadth_24h(τ∈{0.80,0.85,0.90})를 poolneg에 추가, P1/P2 기여 CI | seed 3 | CI > 0.005이면 raw_news_count 유지 근거 보강; null이면 보류(제거 아님) | 0.5일 + 20분 |
| E6 | [EB-NeRD] 축소 CTR 추가 | 저트래픽에서 shrunk CTR 추가가 이득 | E1의 1%/5% 서브샘플에서 (pop_clicks 원값)+(pop_ctr_shrunk_24h, α∈{5,20,50}, prior=전역 CTR) vs 원값만 | seed 3 | 어느 α든 CI 하한 > 0.005 → 채택(α는 잠정값 표기) | M4 실행에 포함 |
| E7 | [EB-NeRD] 휴리스틱 4항 가중치 적합 | 데이터 적합 가중치가 사전값(0.45/0.35/0.15/0.05)보다 낫다 | 조건부 로지스틱(pairwise) [cos_long, cos_short, recency, log1p(pop_clicks_6h)]을 (a) poolneg 학습 분포, (b) E1 1% 서브샘플에서 적합; 사전값 대비 쌍체 ΔnDCG@10 | seed 3 | 두 세트 보고; 서빙 기본값 = (b); 일 노출 ≥5k에서 (a)로 전환 규칙을 ADR 0014에 사전 등록; 4항 모델이 popularity_6h를 못 넘으면 "동점 깨기용"으로만 표기 | 1일 |
| E8 | [EB-NeRD] 후보 생성기 구성 parity | 서빙 구성(5출처 100/100/100/100/50, cap 300, 72h)의 2단계 손실이 하네스 구성과 다르지 않다 | `--candidate-config serving`으로 2단계 표 재산출(popular_clicks_6h·recency·knn_profile·knn_short·category) | P2 20k | union recall ≥ 0.90, 2단계 nDCG@10 − 전체 풀 CI 하한 > −0.005 → 서빙 구성 유지; 아니면 서빙을 하네스 구성으로 | M4 실행에 포함 |
| E9 | [SIM] OPE 검증 | 탐색 슬롯 replay가 정책 B의 CTR을 맞힌다 | 정책 A(휴리스틱+ε 2/20) 로그로 정책 B(shadow 랭커 / random / reactive) top-20 CTR을 replay(1차)·SNIPS(2차)로 추정 vs B 실측; 위치 편향 η를 탐색 슬롯에서 PBM으로 추정해 클릭 모델 η와 비교 | 300명×7일×3 seed | 상대오차 ≤15%(3 seed 평균), ESS ≥5%(탐색 슬롯 기준); 실패 시 ε=0.2 1회 재실험 후 기록. η 회복은 서술용 | 1일 |
| E10 | [SIM] 노출 피로·콜드 다양성 | 피로 규칙이 반복 노출을 줄이고 반응성 지표를 해치지 않는다 | `RECSYS_FATIGUE_MODE=log` vs `enforce`: 반복 노출 비율, `after_click_jaccard`, `similar_share_lift`; 콜드 유저 ILD·카테고리 엔트로피 전후 | 300명×7일×3 seed | 반복 노출 비율 감소 CI > 0, 반응성 지표 악화 없음 → enforce | 0.5일 |
| E11 | [SIM+LOAD-mac] parity·지연 | 서빙 = 오프라인 피처; p95 예산 안 | 시드 DB 200요청 재생(§3.3 게이트 4항목); `request_path_bench.py` + `test_sql_path_latency_p50_p95` + Locust 20 RPS(5/50은 폴백률만) + DB 200ms 지연 주입 | 200요청; 3라운드 | max\|Δ\|<1e-6, τ=1.0, 겹침 ≥0.9, 구성 동일; p95 < 300ms, 폴백률 <5% @20 RPS, X-Rec-Source 분포 | 0.5일 |
| E12 | [KR-eval, 조건부] 스토리 연속성 | 일 클러스터 중 연속 사건이 ≥20% | 일자별 스냅샷 재생, (cos τ∈{0.80,0.85,0.90}) × (개체 Jaccard j∈{0.2,0.35,0.5}) 격자, 블라인드 40쌍 라벨 | 한국어 5~7일치 | 연속 비율 CI, 연결 정밀도 ≥0.80(Wilson 하한 ≥0.65); ≥20%면 I7 최소판, <10%면 ADR 0012에 "불필요" | 라벨 1~2h, LLM $0 |
| E13 | [KR-online, 사용자 생기면] 첫 온라인 수치 | 휴리스틱 슬롯 CTR > 탐색(균등 무작위) 슬롯 CTR, 위치 층화 | 같은 요청·같은 위치 분포에서 탐색 슬롯 vs 결정론 슬롯의 쌍체 CTR(위치별 층화, 탐색 propensity로 정확), 첫 클릭 순위 중앙값, clicks@5 | 파워: 2%→3%(α 0.05 양측, 80%) 팔당 ≈3,830 노출 = 탐색 슬롯 2/요청이면 ≈1,900 요청; 유저 20명×3요청/일이면 약 5주 | 1차 지표 사전 등록. A/B 전환 조건: 주간 요청 ≥6,000(팔당 ≥8.1만 노출 × 설계 효과 ≥1.5) | — |
| E14 | [SIM] 인터리빙 A/A·감도(shadow 경로 후) | team-draft가 100~300 요청에서 알려진 우열을 가르고 A/A는 0.5를 포함 | (reactive vs random), (A/A), team-draft | ≤300 요청, 1회 | 우열 쌍 CI가 0.5 배제, A/A는 포함 | 0.5일 |
| E15 | [EB-NeRD] 신경망 사용자 모델 vs LightGBM(상세 §4.1) | 같은 프로토콜·같은 정보·같은 튜닝 예산에서 학습형 시퀀스 유저 인코더(NRMS-lite/SASRec-lite) 또는 GBDT 스태킹이 LightGBM LambdaRank를 nDCG@10 +0.005 이상 이기고 콜드 조건에서 무너지지 않는다 | arm A(`ranker_v2` P1 / `ranker_v2_poolneg` P2, 팀 설정 고정) vs B NRMS-lite vs C SASRec-lite vs D 스태킹(OOF 점수 피처) [E DIN-lite·B0 콘텐츠만·`*_tuned` GBDT는 서술용/게이트]. P1 네거티브 = 노출 비클릭, P2 = 48h 풀 무작위 20개(poolneg와 같은 rng) — 두 학습기가 같은 `RankTask`를 받음. 콜드 조건(pop_*=0, 히스토리 k∈{0,1,3,5}) 재평가 | P1 test 244,647 노출 / P2 20k 요청(v1과 같은 표본), seed 3, 부트스트랩 1,000; 튠 12 trial/family(es 구간) | **판정 비교 4개**: 과제별 (B/C 중 es 선택 arm − A), (D − A) 쌍체 CI 하한 > +0.005 AND 게이트(콜드 5조건·튜닝 GBDT 대비 CI 하한 > −0.005, 재현 게이트) → shadow 후보(활성 아님); D는 CPU p95 게이트 추가. 아니면 ADR 0013 A3에 수치로 기각/보류 기록 | 코드 3일 + Colab T4 ≤30 CU(추정 10~14 CU, 스모크 근거 §4.1.7), LLM $0 |

삭제: 리뷰 E8(A1은 실행됨, J1). 축소: E4·E5(J3·J4). 변경: E3(J5), E4→E3(J6), E6·E7(J7·J8). 추가(2026-09-26): E15.

### 4.1 E15 상세 사전 등록 — 신경망 사용자 모델 vs LightGBM (같은 프로토콜, 같은 정보, 같은 예산)

> 상태: **사전 등록(실행 전)**. 이 절은 결과가 나온 뒤 고치지 않는다(사후 변경은 ADR 0013 A3의 "사후 변경 기록"에만). 실행 명령·기계 판정
> 함수(`neural_verdict`)를 담은 커밋 SHA를 리포트 머리말에 적는다. 실행은 M4b(§5), 결과는 `reports/recsys/ebnerd_v1_3_neural.{json,md}`.
> 근거 라벨은 [EB-NeRD]이며 어떤 결과도 한국어 서비스 성능이 아니다(§4.1.6).

**질문.** §3.4 D·E는 이유만으로 기각돼 있었고 2026-09-26 사용자가 "기각이 아니라 측정"을 요구했다(급하지 않음). 공개된 가장 강한 외부 근거는
RecSys Challenge 2024(EB-NeRD) 1위 해법 ":D"가 Transformer + LightGBM + CatBoost 3단계 앙상블에 시간 인식 피처를 쓴 것이고(arXiv 2409.20483,
DOI 10.1145/3687151.3687160), 리더보드 AUC는 우리 split·정답 창과 비교할 수 없다. 그래서 우리 프로토콜에서 직접 잰다.
- Q1. 같은 정보(같은 스칼라 피처 22개 + 후보·히스토리의 고정 BGE-M3 벡터)를 받는 학습형 시퀀스 유저 인코더가 LightGBM LambdaRank를 이기는가?
- Q2. 이기지 못해도 GBDT 위에 정보를 더하는가(스태킹)?
- Q3. 콜드 조건(인기도 0, 히스토리 ≤5)에서 무너지지 않는가? — 이 제품의 초기 regime이 여기다(§1 결론 1).

#### 4.1.1 프로토콜 — ebnerd_v1과 같은 부분(바꾸지 않음)
- **데이터·임베딩**: `ebnerd_small`. 파일 sha256은 `ebnerd_v1.json` `data.files`와 같아야 한다(articles `b1cea52e…3d2`, train/validation behaviors·history 4개).
  임베딩 = `derived/ebnerd_small/bge_m3_tsb512.f16.npy`(sha256 `2f096ef8…ebeb`, 1024차원, L2 정규화, 512 토큰 절단) — **고정**, 미세조정 없음, 신경망 arm도 같은 파일을 쓴다.
- **창**(반열린, EB-NeRD 로컬 시각): fit 2023-05-20T07~05-24T07(129,080 노출) / es 05-24T07~05-25T07(32,559) / test = validation 전체 05-25T07~06-01T07(244,647).
  `prepare.protocol_windows`·`impressions_in`을 그대로 쓴다. P2 = validation 요청 20,000건 표본(`SPLIT_SEED+2`, v1과 같은 인덱스), 풀 = [t−48h, t] 발행 − t 이전 seen, `n_pos_total` 벌점 동일.
- **조기 종료는 es 구간(train 마지막 24h)에서만**: LightGBM 50라운드 인내·≤1,000라운드(v1 그대로), 신경망 epoch 단위 인내 2·≤20 epoch, 지표 = 같은 구성의 es 데이터 nDCG@10.
  test는 모델 선택·튠·조기 종료 어디에도 쓰지 않는다.
- **통계**: seed 0/1/2(모델 초기화·배치 순서·네거티브 표집·동점 처리). 노출별 지표를 seed 평균 → 유저 단위 클러스터 부트스트랩 1,000회 95% CI(`metrics.cluster_bootstrap`, seed 0),
  같은 노출에서의 쌍체 차이 CI(`metrics.paired_bootstrap_diff`, seed 1). seed별 평균·표준편차 보고. 다중 비교 보정 없음 — 판정용 비교 수는 §4.1.5에 명시.
- **seen**: P1은 seen 포함/제외 둘 다 보고(모든 신경망 arm을 `SEEN_FILTER_METHODS`에 추가), P2는 seen 제거 풀. seen 비율(v1: 후보 4.68%, 클릭 2.48%)을 다시 기록.
- **재현 게이트**: 같은 실행에서 LightGBM arm을 `models.train`으로 다시 학습해 P1 `ranker_v2` nDCG@10 seed 평균이 v1 CI [0.6533, 0.6569] 안, P2 `ranker_v2_poolneg`가
  [0.2640, 0.2728] 안이어야 실행이 유효하다(x86·2 vCPU·lightgbm 버전 차이 점검). 벗어나면 결과를 판정에 쓰지 않고 원인을 기록한다.

#### 4.1.2 과제·네거티브 — 두 학습기가 같은 것을 받는다

| 과제 | 쿼리 | 후보 | 양성 | 학습 네거티브 | 평가 후보 | 하네스 함수 |
|---|---|---|---|---|---|---|
| P1 노출 재정렬 | impression | `article_ids_inview`(≤100) | 클릭 | **같은 노출의 비클릭(in-view)** — `ranker_v2`의 `inview` 데이터와 동일 | in-view 전체 | `prepare.p1_task`(fit/es/test) |
| P2 48h 풀 | impression(=요청) | [t−48h, t] 발행 − seen | 클릭 | **48h 풀 무작위 20개**(t 이전 seen·이번 클릭 제외) = `prepare.pool_negative_task(n_neg=20, window_h=48)`, rng `default_rng(1000+seed)` → `ranker_v2_poolneg`와 **같은 네거티브 표본** | 전체 풀(평균 235, 161~279) | `pool_negative_task`(fit/es), `p2_task`(test) |

두 학습기는 같은 `RankTask` 객체를 받는다. LightGBM은 `compute_features(ctx, task.req, groups=ALL_GROUPS)`의 피처 행렬을, 신경망은 `task.req.{user, profile_cutoff, cand_item, cand_ptr}` +
같은 피처 행렬(스칼라 블록)을 받으며, `labels`·`cand_ptr`가 두 경로에서 같은 배열인지 테스트로 단언한다. 다중 양성 노출(노출당 클릭 평균 1.01)은 LightGBM `label_gain=[0,1]`,
신경망은 양성마다 softmax 항을 더해 평균한다. P1↔P2 교차(각 arm을 다른 과제에 적용)는 v1과 같이 서술용으로만 낸다.

#### 4.1.3 Arm
- **A. LightGBM LambdaRank(판정 기준)**: `ranker_v2`(P1), `ranker_v2_poolneg`(P2), `TEAM_PARAMS` 고정, 피처 = `V2_FEATURES` 22개(팀 10 + 인기도 5 + 단기·세션 5 + cat_share·hist_len),
  es early-stop — ADR 0013 그대로. 서술·게이트용 `ranker_v2_tuned`·`ranker_v2_poolneg_tuned`는 §4.1.4.
- **B. NRMS-lite**: 입력 = 요청 시각(`profile_cutoff` t) **이전** 마지막 N=50 클릭(`ctx.user_log` = split history 21일 + 행동 창 클릭; hist_cos와 같은 출처, `EventIndex.bounds(user, 0, t)`의
  끝에서 N개, 오른쪽 정렬·패딩 마스크). 아이템 표현 = 고정 BGE-M3 1024 → `Linear(1024→d) + LayerNorm + Dropout`(투영은 히스토리·후보가 공유; 학습되는 유일한 아이템 파라미터).
  유저 인코더 = multi-head self-attention(h) + additive attention(NRMS의 유저 인코더; 뉴스 인코더는 고정 임베딩으로 대체). 점수 = ⟨u, c⟩/√d + g(x), g = 2층 MLP → 스칼라,
  x = A와 같은 22개 스칼라 피처를 fit 창 평균·표준편차로 표준화(NaN→0 + `hours_since_last_event` 결측 지표 1개). 빈 시퀀스(k=0)는 학습되는 `[EMPTY]` 벡터.
  손실 = 쿼리(노출) 단위 masked softmax 교차 엔트로피(LambdaRank의 쿼리 그룹과 같은 단위), AdamW, grad-norm clip 1.0. 원 NRMS와 다른 점(뉴스 인코더 고정, 스칼라 블록 g, 그룹 전체 softmax)을 리포트에 명시.
  - **B0(서술용)**: g를 뺀 콘텐츠·시퀀스만 — "스칼라 피처가 담는 정보량"과 "이득이 어느 블록에서 오는가"를 분해한다.
- **C. SASRec-lite**: 같은 투영 위에 인과(causal) self-attention L층(pre-LN, FFN 4d) + 학습 위치 임베딩(N), 마지막 위치 출력 = u, 점수·스칼라 블록·손실은 B와 동일.
  SASRec 원본의 next-item 자기회귀 목적함수 대신 B와 같은 요청 단위 listwise 목적을 쓴다(같은 후보·같은 네거티브를 받게 하려는 선택; 이 차이를 명시하고 "SASRec의 인코더만 차용"이라고 쓴다).
- **D. 스태킹(GBDT + 신경망 점수)**: LightGBM LambdaRank(팀 설정 고정, es early-stop) 피처 = `V2_FEATURES` + `neural_score`(B/C 중 es 선택 arm, §4.1.4).
  **누출 방지(OOF)**: fit 창 노출을 유저 단위 K=5 폴드로 나누고(seed 고정), 폴드 밖 4/5로 학습한 신경망이 폴드 안 노출을 채점 → fit 행 점수는 전부 out-of-fold. es·test 행은 5개 폴드
  모델 점수의 평균. 어떤 행도 그 행의 유저를 학습에 본 모델로 채점되지 않음을 테스트로 단언한다. 폴드 모델의 epoch 수는 최종 arm의 best epoch(seed별)로 고정해 es를 두 번 쓰지 않는다.
  fit 행을 전체 fit으로 학습한 모델로 채점하는 방식(누출)은 금지. 신경망 점수를 그대로 쓰는 것과 요청 내 랭크(0–1)로 바꾼 것 중 랭크 버전을 기본으로 한다(E3 언어 전이 계약과 일관).
  - **서빙 비용 게이트**: 요청당 시퀀스 인코딩 1회 + ≤300 후보 내적 + g 300행을 CPU(ARM 2 vCPU, ONNX Runtime 또는 TorchScript)에서 실행했을 때 `/newsletters/today` p95 추가 지연
    **≤ +30 ms**(예산 300 ms의 10%), 모델 파일 ≤ 20 MB. M6 [LOAD-arm]에서 측정하기 전까지 D의 판정은 "오프라인 통과, 서빙 게이트 대기"로만 쓴다.
- **E. DIN-lite(선택, 서술용)**: 후보 조건부 target attention(MLP([h, c, h−c, h⊙c]) → 가중치) 합 → concat(c) → MLP → 점수 + g(x). B/C 코드 경로에서 0.5일 이내로 추가될 때만 넣고 판정에 쓰지 않는다.
- **콜드 조건(E1·E2 정의 그대로, 평가 시 조건)**: (i) `pop_*` 5개를 0으로 강제(E1의 "pop_*=0 강제" 조건; 신경망은 x의 해당 열, LightGBM은 `--pop-mask` 평가 경로), (ii) 유저 로그를 요청 시각
  기준 최근 k∈{0,1,3,5} 이벤트로 절단(E2 `--hist-truncate k`; 신경망은 시퀀스 절단, LightGBM은 hist/short/sess/cat_share 재계산 — 같은 절단 로그를 두 arm에 적용). 모델은 전체 히스토리로
  학습한 채 평가만 바꾼다(서빙 콜드 유저에 대한 퇴화 측정, E2와 동일 논리).

#### 4.1.4 튜닝 예산(공정성) — family당 같은 시행 수, 탐색 공간 공개
- **신경망 family(B, C; E 포함 시 E)마다 무작위 탐색 12 trial**, seed 0, 선택 지표 = es 구간 nDCG@10(같은 구성: P1은 in-view es 노출, P2는 poolneg es 행), 튜닝 시 ≤6 epoch·인내 2.
  최종은 best 설정으로 seed 0/1/2, ≤20 epoch·인내 2. 탐색 공간: d∈{128,256}, heads∈{2,4}, L(C만)∈{1,2}, dropout∈{0.1,0.3}, lr log-U[3e-4, 3e-3], weight_decay∈{0,1e-4,1e-2},
  batch∈{256,512}, N∈{20,50}. 온도 1/√d 고정, label smoothing 없음. trial 표(설정·es 지표·epoch·시간)를 JSON에 전부 남긴다.
- **LightGBM 판정 arm은 팀 설정 고정**. 이유: ADR 0013에 사전 등록되고 실제로 shadow에 올라갈 바로 그 모델이므로 비교 대상을 바꾸지 않는다. 대신 **같은 12 trial 예산의**
  `ranker_v2_tuned`·`ranker_v2_poolneg_tuned`(num_leaves∈{15,31,63,127}, lr log-U[0.02,0.1], min_data_in_leaf∈{20,50,100,200}, feature_fraction∈{0.7,0.9,1.0}, lambda_l2∈{0,1,10},
  lambdarank_truncation_level∈{10,20,30}; es nDCG@10로 선택, seed 0 튠 → 3 seed)를 함께 학습해 두 용도로 쓴다: (1) 서술용 표, (2) **게이트** — 신경망 arm이 튜닝 GBDT보다
  CI 하한 −0.005 아래로 뒤지면 "신경망 우위"가 아니라 "기준선 튠 부족"이므로 채택하지 않는다(Ferrari Dacrema et al. 2019, Rendle et al. 2020의 교훈).
- **B와 C 중 판정 arm 선택은 test를 보지 않고** es 지표(seed 0 튠 best)로 한다. 나머지 하나는 서술용. 선택 결과와 근거 수치를 JSON `selection`에 기록한다.

#### 4.1.5 지표·판정 규칙(실행 전 고정)
- **1차 지표**: 과제별 ΔnDCG@10 = (arm − 대응 LightGBM arm), 같은 노출 쌍체, 유저 클러스터 부트스트랩 1,000회 95% CI. 최소 효과 크기 0.005(§4 공통·I12).
- **판정용 비교 4개**: (P1) `B|C_sel − ranker_v2`, `D − ranker_v2`; (P2) `B|C_sel − ranker_v2_poolneg`, `D − ranker_v2_poolneg`.
- **게이트**(채택을 막을 수만 있고 채택 근거로 쓰지 않음; 판정 arm마다): 콜드 조건 5개(pop_*=0; k=0,1,3,5) 각각 Δ CI 하한 > −0.005; 튜닝 GBDT 대비 Δ CI 하한 > −0.005;
  §4.1.1 재현 게이트 통과; 신경망 seed 표준편차 ≤ 그 arm CI 반폭(seed 불안정이면 "구분 불가"). D는 서빙 비용 게이트(M6) 추가.
- **결정**: 과제별로 판정 비교 CI 하한 > +0.005 AND 게이트 전부 통과 → 그 arm을 **shadow 후보로 채택**(`model_registry` role=shadow에 등록, slot 로그 `scores_shadow`에 점수 병기;
  활성 전환은 ADR 0013 결정 4 + §4.1.6 증거 후). 그 외 — CI가 0을 포함(구분 불가), 하한이 (0, 0.005](효과 미달), 게이트 실패 — 는 ADR 0013 A3에 수치와 함께 "측정 후 기각/보류"로
  기록하고 §3.4 D·E·I를 그 상태로 갱신한다. 판정은 사람이 표를 읽지 않고 `neural_verdict`(JSON → 규칙 표)가 낸다.
- **주장 범위**: 결과가 어느 쪽이든 §8의 "딥러닝 대비 우위" 금지는 E15가 **두 과제 모두** 통과할 때까지 유지하고, 통과해도 "[EB-NeRD] 같은 프로토콜에서 ΔnDCG@10 +x [CI]"라고만 쓴다.
  통과하지 못하면 "같은 프로토콜·같은 예산에서 측정했고 기각했다"가 주장 가능한 전부다(§8).
- **서술용(판정 없음)**: P1 AUC·MRR·nDCG@5, P2 recall@10·MRR·ILD@10·coverage@10·novelty@10, B0, E, `*_tuned` 표, trial 표, best epoch·학습 시간·파라미터 수, 콜드 k 곡선, P1↔P2 교차,
  스태킹의 `neural_score` gain 비율.

#### 4.1.6 언어 전이 caveat — EB-NeRD에서 이겨도 한국어 전이는 성립하지 않는다
- 투영층·유저 인코더는 덴마크어 기사 벡터의 기하 위에서만 학습됐고, 클릭 역학(타블로이드 편집 프런트 페이지, 세션 ≤4 조회)도 이 제품과 다르다. 스칼라 피처는 언어 무관이지만
  신경망 arm의 이득이 어느 블록에서 오는지(B0 vs B)를 서술용으로 분해해 둔다. 활성 후보로 올리기 전에 필요한 증거(사전 등록):
  - **[KR-eval]** (a) 한국어 뉴스레터 ≥500건에서 BGE-M3 카테고리 kNN(k=10) LOO 정확도 ≥0.6(v1 임베딩 점검과 같은 기준); (b) 스칼라 블록에 E3 랭크 정규화 계약(손실 ≥ −0.01) 적용;
    (c) §3.7 시작 조건(클릭 ≥2,000·요청 ≥5,000·탐색 슬롯 노출 ≥10,000) 충족 후 한국어 로그에서 같은 프로토콜로 신경망 − GBDT를 재측정해 **비열등**(CI 하한 > −0.005).
  - **[KR-online]** (d) shadow 점수 병기 ≥4주 뒤 탐색 슬롯 replay CTR 쌍체 비교(E13 방식, 정확한 propensity)에서 신경망 shadow − GBDT shadow CI 하한 > 0;
    (e) 한국어 로그의 유저 히스토리 길이 중앙값 ≥5 클릭 — 그 미만이면 시퀀스 모델은 E15 콜드 게이트 구간(k≤5)에서만 작동하므로 콜드 게이트 통과가 유일한 근거이고 활성은 미룬다.
- 이 증거들이 없으면 E15 결과는 "[EB-NeRD]에서 측정된 오프라인 차이"로만 쓴다. "한국어 서비스에서 신경망이 낫다/못하다"는 어느 쪽으로도 쓰지 않는다.

#### 4.1.7 실행 — Colab GPU(EB-NeRD 라이선스 범위 안), 예산 상한, 스모크 실측
- **허용 범위**(2026-09-26 사용자 확인): 비공개 Colab 런타임에 **실행마다 업로드하고 실행 뒤 VM을 해제**한다. 저장소 커밋·공개 Kaggle 데이터셋·Drive 영구 사본 금지
  (`colab drivemount`·`--keep` 사용 안 함), 리포트에 기사 텍스트 없음(v1과 동일). MacBook은 무거운 계산을 하지 않는다(로컬은 `--fake-dim` 단위 테스트·`make_report`만, `nice -n 19`).
- **런타임**: T4(실측 1.07 CU/h; 표준 2 vCPU·RAM 12.7 GB·GPU 14.6 GB). v1 피크 메모리 10.1 GB(M2, replay·MMR 포함)이므로 E15는 단계별 스크립트(준비→npz 캐시 / 학습 / 평가)로
  나눠 메모리를 돌려주고, 첫 실행에서 `psutil` 피크를 기록해 11 GB를 넘으면 `--high-mem`(CU/h 실측 기록)으로 옮긴다.
- **실행 절차**(실행마다 동일, `scripts/e15_colab_run.sh`로 고정; 세션 생성 뒤 어떤 실패에도 `colab stop`을 부르는 trap):
  1. `colab usage` → `cu_before` 기록. `colab new -s e15-<task>-<yyyymmdd> --gpu T4`.
  2. `colab install -s … lightgbm==4.7.0 pyarrow psutil`(Colab 기본 lightgbm은 4.6.0이라 **반드시 고정**; torch는 Colab 기본값을 쓰고 버전을 기록 — 스모크 시점 2.11.0+cu128).
  3. `colab upload`: 코드 tarball(`recsys_core/`, `evaluation/recsys/`, 하네스 커밋 SHA 파일), `ebnerd_small.zip`(80 MB, sha256 `84fc6fc9…bd4f`),
     `derived/ebnerd_small/{bge_m3_tsb512.f16.npy(42 MB), article_ids.npy, bge_m3_tsb512.meta.json}`.
  4. VM에서 sha256 검증(zip, npy는 meta의 `embeddings_sha256`·`article_ids_sha256`, 풀어낸 parquet 5개는 `ebnerd_v1.json data.files`) — 하나라도 다르면 즉시 중단·해제.
  5. `colab exec -s … -f evaluation/recsys/ebnerd/run_neural.py --timeout 43200`(과제당 세션 1개: `--task p1` / `--task p2`; 진행 로그 stdout, 중간 산출물 `/content/out/`).
  6. `colab download /content/out/ebnerd_v1_3_neural_<task>.json`(+ trial 로그) → 하네스 워크트리 `reports/recsys/`(현재
     `/Users/brownee/Projects/newsletter-recsys/.claude/worktrees/wf_4c8228fa-9b4-2`; 하네스가 main/통합 브랜치에 들어간 뒤에는 그 브랜치의 같은 경로).
  7. `colab rm` 데이터 파일 → `colab stop` → `colab sessions`가 비어 있는지 확인 → `colab usage` → `cu_after`. JSON `meta.compute = {gpu, driver, torch, cuda, cudnn, python, lightgbm,
     numpy, cpu_count, ram_gb, wall_seconds, rate_cu_per_hour, cu_before, cu_after, cu_used}` — `cu_used`는 잔액 차(표시 해상도 0.01·지연 반영)와 `rate × wall`을 둘 다 적는다.
- **환경 고정·결정론**: `torch.use_deterministic_algorithms(True)`, `CUBLAS_WORKSPACE_CONFIG=:4096:8`, `cudnn.benchmark=False`, seed별 generator로 초기화·배치 순서 고정,
  numpy 네거티브 표집은 v1과 같은 `default_rng(1000+seed)`. 같은 GPU 종류에서는 같은 seed가 비트 단위로 재현된다(스모크 4/4 확인). GPU 종류가 바뀌면 비트 단위 재현은 기대하지
  않으므로 한 실행의 seed 3개는 같은 GPU 종류에서 돌리고, 재현 기준은 "seed 평균이 보고된 CI 안"이다. LightGBM은 `num_threads`(Mac 6 vs Colab 2)에 따라 미세하게 달라질 수 있어
  §4.1.1 재현 게이트로 확인한다. `requirements-colab.txt`에 lightgbm·numpy·pandas·pyarrow 버전을 적고 실행마다 실제 버전을 JSON에 남긴다.
- **예산: E15 전체 ≤30 CU 상한**(≈28 T4-시간, 잔액 199.76 CU의 15%). 추정(스모크 실측 × fit 창 배수 5.22 × GPU 밖 오버헤드 1.2 — 시퀀스·스칼라 블록은 실행 전에 int32/f32 배열로
  한 번 준비해 배치는 인덱스 슬라이스 → GPU gather이므로 CPU 오버헤드는 작다): NRMS-lite epoch ≈14 s, SASRec-lite ≈37 s. 튠 12 trial × 2 family × 2 과제 × ≤6 epoch ≈ 2.0 h;
  최종 3 seed × 2 family × 2 과제 × ≤20 epoch ≈ 1.7 h; 스태킹 OOF 5 폴드 × 3 seed × 2 과제 ≈ 2.4 h(NRMS 선택 시)~6.0 h(SASRec); LightGBM(고정 2×3 seed + 튠 12×2 + 스태킹 2×3, 2 vCPU)
  ≈ 2.5 h; 피처·시퀀스 준비 ≈ 0.5 h; 콜드 조건 채점 ≈ 0.5 h → **≈ 9.6~13.2 h ≈ 10~14 CU**, 상한의 절반. 실행 중 `rate × 경과 + 남은 단계 추정`이 24 CU(상한의 80%)를 넘을 것으로 보이면
  그 시점에서 중단·해제하고 부분 결과만 기록한다. 실행마다 CU를 JSON에 남기고 ADR 0013 A3에 합계를 적는다.
- **무엇을 어디서**: Mac = 코드·단위 테스트(`ebnerd_demo --fake-dim 16`, CPU, 수 초)·`make_report`. Colab GPU(T4) = E15 학습·평가. **M4 콜드 사슬 E1–E8도 Colab으로 옮길 수 있다**
  (numpy+LightGBM만 필요 → CPU 런타임; `--high-mem` 필요 여부는 v1 피크 10.1 GB 기준으로 첫 실행에서 결정; CPU 런타임 CU/h는 그때 실측해 기록). 같은 업로드·검증·해제 절차를 쓴다.
  M4·M4b가 Colab에서 도는 동안 Mac은 M5·M6 코드 작업을 한다.

**스모크 실측**(2026-09-26 13:00–13:01 UTC; 스크립트 `docs/design/e15_colab_smoke.py`, 결과 `docs/design/e15_colab_smoke_result.json`). EB-NeRD는 업로드하지 않았고 ebnerd_demo 형태의
무작위 텐서(기사 11,777×1024, 노출 24,724, 후보 30 패딩 / 1+20, 시퀀스 50, d=256, h=4, batch 256)만 썼다 — 손실 값은 무의미하고 시간·환경·결정론만 유효하다.

| 항목 | 실측 |
|---|---|
| VM | Tesla T4 14.6 GB(capability 7.5), 드라이버 580.82.07, 2 vCPU, RAM 12.7 GB, x86_64 |
| 소프트웨어(Colab 기본) | Python 3.13.15, torch 2.11.0+cu128, CUDA 12.8, cuDNN 9.19.0, numpy 2.1.3, pandas 2.2.3, **lightgbm 4.6.0**(로컬 4.7.0과 다름 → 고정 필요), pyarrow 23.0.1 |
| 할당·실행 | `colab new` 4 s → READY, `colab exec` 벽시계 58 s(스크립트 자체 49.4 s), 세션 존속 ≈66 s, `colab stop` 뒤 `colab sessions` "No active sessions" |
| NRMS-lite(592,128 학습 파라미터) | P1형 1 epoch(97 step) 2.21 s, P2형 2.09 s → small fit 창(×5.22) 추정 11.5 / 10.9 s; 20,000×235 채점 1.53 s; 피크 GPU 메모리 0.35 GB |
| SASRec-lite(2층, 1,855,232) | 5.87 / 5.86 s → 30.6 s; 채점 2.45 s; 피크 0.78 GB |
| 결정론 | 같은 seed 두 번 실행의 손실 합이 비트 단위로 동일 — 4/4(두 모델 × 두 과제형) |
| 비용 | 세션 중 `colab usage` 사용률 **1.07 CU/h**; 시간 기준 ≈0.02 CU(+ 첫 시도에서 로컬 스크립트 오류로 빈 세션 40 s ≈0.012 CU) = **≈0.03 CU ≤ 1 CU**; 잔액 표시 199.76 → 199.76(해상도 0.01, 지연 반영 가능) |
| 운영 메모 | colab CLI 0.7.2는 `jupyter_kernel_client` 불일치로 `exec`이 로컬에서 실패(VM은 정상 해제) → 0.7.4로 갱신 후 성공. 스모크는 `new`→`status`→`usage`→`exec`→`usage`→`stop`→`sessions`→`usage` 순서, 실패 시에도 `stop`을 부르는 trap 사용 |

#### 4.1.8 코드 계획 — 재사용 / 신규(`evaluation/recsys/ebnerd/neural/`)
- **재사용(수정 없음)**: `loaders.load_impressions/load_history_events/click_events`; `prepare.load_bench/protocol_windows/impressions_in/p1_task/p2_task/pool_negative_task/seen_mask/RankTask`;
  `recsys_core.events.EventIndex.bounds`(마지막 N 클릭 gather), `recsys_core.features.compute_features`(스칼라 블록 = LightGBM 피처와 같은 행렬), `ItemCatalog.emb`(고정 벡터, L2 정규화 f32);
  `evaluation.recsys.metrics.ranking_metrics/cluster_bootstrap/paired_bootstrap_diff/topk_items/intra_list_diversity/category_entropy/catalog_coverage`;
  `run_ebnerd.MetricBank/SPLIT_SEED/_features/_list_metrics/_novelty`; `models.train/ABLATION/NEGATIVE_VARIANTS/TEAM_PARAMS/V2_FEATURES/feature_matrix/_order_and_groups`;
  `make_report.ci/dci/table`. M4의 `--pop-mask`·`--hist-truncate` 평가 경로(콜드 조건)를 함수로 노출해 두 arm이 같은 것을 쓴다.
- **신규**:
  - `neural/sequences.py`: `last_n_clicks(user_log, users, cutoff, n) -> (items[n_req, n] int32, mask)`(오른쪽 정렬), `truncate_log(k)`, `scalar_block(feats, columns, stats)`, `standardization_stats(fit_feats)`.
  - `neural/datasets.py`: `GroupBatches(task, seqs, scalars, labels, cand_ptr, batch_groups, seed)` — 가변 길이 후보 그룹 패딩(P1 ≤100, P2 학습 1+20, P2 평가 풀은 요청 단위 청크), seed별 결정론 순서.
  - `neural/models.py`: `ItemProjection`, `NRMSLite`, `SASRecLite`, `DINLite`, `ScalarHead`, `listwise_loss` — 스모크 스크립트의 정의를 옮기고 `[EMPTY]`·스칼라 블록·온도를 추가.
  - `neural/train.py`: `fit(spec, fit_batches, es_batches, seed, device, max_epochs, patience) -> TrainedNeural(state_dict, best_epoch, es_curve, seconds, n_params)`, `predict(task) -> scores[len(pairs)]`, `deterministic_setup(seed)`.
  - `neural/tune.py`: `random_search(space, n_trials, seed=0)` + trial 로그(신경망·LightGBM 공통 인터페이스).
  - `neural/stack.py`: `oof_scores(spec, fit_task, k=5, seed) -> (fit_scores, fold_models)`, `score_with_folds(fold_models, task)`, `stacked_spec(base_spec, 'neural_score_rank')`.
  - `neural/cold.py`: 콜드 조건 5개를 두 arm에 같은 방식으로 적용하는 래퍼(M4 함수 호출).
  - `neural/report.py`: `neural_verdict(d) -> {passed_by_task, comparisons(4), gates, selection}` + 표 렌더러(`make_report` 스타일).
  - `run_neural.py`: `--dataset ebnerd_small --task p1|p2 --arms lgbm nrms sasrec din stack --trials 12 --seeds 0 1 2 --n-boot 1000 --stage prepare|train|evaluate|all --out-json …`;
    JSON에 `meta.compute`, `protocol`(v1과 동일 항목), `selection`, `trials`, `p1|p2`, `diffs`, `cold`, `verdict`.
  - `tests/recsys/test_neural_demo.py`(`ebnerd_demo --fake-dim 16`, CPU, 1 epoch): 두 경로의 `labels`·`cand_ptr` 동일성, OOF 비누출(폴드 모델이 자기 학습 유저를 채점하지 않음), CPU 비트 단위 반복 재현,
    패딩 마스크가 점수에 영향 없음, k=0 절단이 `[EMPTY]` 경로를 탐.
  - `scripts/e15_colab_run.sh`, `requirements-colab.txt`(§4.1.7).
- **산출**: `reports/recsys/ebnerd_v1_3_neural.{json,md}`(표는 `neural/report.py`로 생성, 손으로 옮기지 않음), ADR 0013 A3(사전 등록 SHA·결과·판정·CU 합계), §3.4 D·E·I 갱신.

---

## 5. 마일스톤 (의존 순, 각 산출 증거)

| M | 내용 | 의존 | 산출 증거 | 공수 |
|---|---|---|---|---|
| M0 | 통합 브랜치 `integrate/recsys-v2`(4 브랜치 병합, rebase, Alembic merge revision), colima/Postgres 복구·수집 재개 확인(launchd embed 종료 코드 1 원인 포함; 도메인 밖) | — | CI 초록, `alembic heads` 1개, `job_runs` 재개 행, `docs/adr/README.md` 예약 표 | 0.5~1일 |
| M1 | team_repro_v2 어드버서리얼 6건 반영 → main 병합 | — | 재실행 수치가 검토치와 일치(click-only team-final ≈0.99/0.96, random 0.689/0.506; 동점 무작위 seed 43/44 MRR 0.500/0.449), 85개 테스트, §7 안전 문장 8개만 | 1일 |
| M2 | 로그 v2 + 탐색 슬롯 + ScorerStack/shadow + OPE 추정기 + 폴백 단순화 + 피로 규칙(log) | M0 | 통합 테스트(200회 조인 1:1, propensity 합), [SIM] 로그 유실율 <0.5%, ADR 0025(제안됨) | ≈4일 |
| M3 | recsys_core 서빙 어댑터 + 증분 프로필 상태 + parity 게이트 CI + 팀 레시피 은퇴 + 후보 구성 통일 + 모델 등록 스크립트 | M0, M2(로그 컬럼) | `reports/recsys/parity_v1.json`, CI job, shadow 점수가 slot 로그에 남는 통합 테스트, ADR 0033·0015 | 3일 |
| M4 | EB-NeRD v1.2 콜드 regime(E1–E4, E6, E8; E5 선택) 사전 등록 → 1회 실행 | M0(하네스 코드), 사전 등록 커밋 | `reports/recsys/ebnerd_v1_2_cold.{json,md}`, ADR 0013 A2 결과, ADR 0031, k*·shadow 주모델 결정 | 3일 + 실행 ≈2h |
| M4b | E15 신경망 사용자 모델 비교(§4.1): `neural/` 코드 + demo 테스트 → ADR 0013 A3 사전 등록 커밋 → Colab T4 실행(P1·P2 세션 각 1개, 실행마다 업로드·검증·해제·CU 기록) → 리포트·기계 판정 | M0(하네스 코드가 main/통합 브랜치에 있어야 함), M4(콜드 조건 평가 경로 `--pop-mask`·`--hist-truncate` 재사용); M5·M6와 병렬 | `reports/recsys/ebnerd_v1_3_neural.{json,md}`(`meta.compute`에 CU), ADR 0013 A3 결과·판정, §3.4 D·E·I 갱신, shadow 후보 채택 또는 수치 기록된 기각 | 3일 + Colab ≤30 CU(추정 10~14) |
| M5 | 인기도·휴리스틱 재설계(E7, pop_ctr_shrunk, 배치 인기 랭킹 클릭 항) | M4 | ADR 0014(전환 규칙 사전 등록), `HeuristicWeights` 기본값 교체 커밋 | 1.5일 |
| M6 | [SIM]·[LOAD] 증거(E9, E10, E11) | M2, M3 | `reports/sim/ope_validation.md`, `reports/serving/latency_v1.md`([LOAD-mac]), ADR 0016 한 문단, ADR 0019 증거 절 | 1.5일 |
| M7 | 문서·규칙: ADR 0013 A2 판정 갱신(`promotion_verdict` 인자화 후 v1 재판정 표: lambdarank "기여"→"무시 가능", R3 기준선 popularity_6h로도 통과 +0.1518), ADR 0025 파워 표·전환 규칙·E13 사전 등록, 귀속 각주, 0003 상태 갱신 | M4~M6 | ADR diff, 재판정 표 | 1일 |
| M8 | 조건부: E12 → I7 스토리 최소판; E14 인터리빙 A/A; 카테고리 캘리브레이션 선택 | 한국어 5~7일치, M2 | `reports/clustering/story_linking_v1.*`, ADR 0012 | 2일(조건부) |

총 ≈19 작업일(+조건부 2일). M1은 M0과 병렬 가능. M4의 코드 변경은 M2·M3와 병렬 가능하고, M4·M4b의 실행은 Colab에서 돌리므로(§4.1.7) 노트북 일정과 무관하다 —
M4b의 Colab 실행(≈10~14 h)은 M5·M6 코드 작업과 겹쳐 진행한다.

---

## 6. 버릴 것 / 미룰 것

**버림(이 계획에서 하지 않음)**
- BGE-M3 미세조정·ID 임베딩, LinUCB/Thompson 정책 학습(실트래픽 전). (NRMS-lite·SASRec-lite·GBDT 스태킹은 2026-09-26에 "버림"에서 "미룸"으로 옮겼다 — E15 측정 후 판정.)
- 팀 레시피 `train` 잡의 운영 실행(ablation 시작 arm `team_binary`로만 보존, 성승우 원작 표기), `UserEmbedder`·`user_embed` 잡.
- 팀 합성 데이터로 어떤 정확도 지표를 주장하는 것, `generator_split` 결과 인용.
- LLM per-request 재랭커, LLM 판정을 클릭 라벨로 쓰는 경로.
- HNSW 도입(실측 한 문단으로 종료), Redis 단기 저장소(벤치 비교만), MIND 하네스, EB-NeRD large 다운로드.
- 리뷰 E8(이미 실행), I9(no-op), 시뮬레이터 로그 대상 KS 검정, prior에 raw_news_count를 넣는 인기도 식.
- 온라인 A/B를 초기 계획으로 쓰는 것(파워 표가 금지).

**미룸(조건 명시)**
- 스토리 최소판 → E12 ≥20%일 때만. 전체판(버전·UI 배지·클레임 차분) → 그 뒤.
- 인터리빙 실운영 → shadow 경로(M2) + [SIM] A/A 후, 주간 요청 임계 도달 시.
- LLM 관심 프로필 피처 → 한국어 클릭 수천 건·라이선스 확인 후 ≤$5, null 결과도 기록.
- 카테고리 캘리브레이션 → MMR 대안 선택 과제, 스택 추가 금지.
- 한국어 로그 자기 학습 → §3.7 시작 조건 충족 시.
- [LOAD-arm] → OCI A1 확보 후(그 전엔 [LOAD-mac]만).
- Optuna 튜닝 → 합성 목적함수 폐기, EB-NeRD es 구간으로만, M4 이후(E15의 12 trial 무작위 탐색이 첫 사례).
- 신경망 사용자 모델(NRMS-lite/SASRec-lite/스태킹)의 shadow 후보 등록 → E15(§4.1) 판정 비교 CI 하한 > +0.005 AND 게이트 통과 시에만; 활성은 §4.1.6 [KR-eval]/[KR-online] 증거 후.
  통과하지 못하면 수치와 함께 기각 기록(ADR 0013 A3)으로 종료.

---

## 7. 새로 필요한 ADR (번호는 `docs/decision-map` 코드 우선 원칙)

| 번호 | 제목 | 상태(작성 시) | 담는 결정 | 증거 |
|---|---|---|---|---|
| 0003 갱신 | 상태 "일부 대체됨 — 랭킹 부분은 0013" + 재평가 요지 3줄, 성승우 원 설계 명시 | — | decision-map 6절 main 항목 | team_repro_v2, ebnerd_v1 |
| 0007 갱신 | 어드버서리얼 6건 반영(시드 수, 자리표시자, 퇴화 행 인용 삭제, 캐시 문구, best_iteration=1 지속) | 채택됨 | M1 | team_repro_v2 재실행 |
| 0012 | 스토리 연속성과 추천 단위(뉴스레터 유지, story_id 정체성 계층, 적용 3가지, 불필요 시 기록) | 제안됨→E12 후 | §3.1 | story_linking_v1 |
| 0013 A2 | EB-NeRD 보충 v1.2 콜드 regime 사전 등록(E1–E4, E6, E8; 최소 효과 크기 0.005; R3 기준선 popularity_6h; `promotion_verdict` 인자화·v1 재판정) | 채택됨(A2 결과 전 사전 등록) | §4 | ebnerd_v1_2_cold |
| 0013 A3 | E15 신경망 사용자 모델 비교 사전 등록(arm A–E, 과제·네거티브 동일성, 튜닝 예산 12 trial/family, 판정 비교 4개·게이트, 언어 전이 caveat, Colab 실행 절차·≤30 CU 상한·CU 기록) → 결과·기계 판정(`neural_verdict`)·사후 변경 기록 | 채택됨(사전 등록; 결과 전 커밋) → 결과 후 "채택/기각 수치" 추가 | §4.1 | ebnerd_v1_3_neural, e15_colab_smoke_result |
| 0014 | 활성 스코어러: 4항 휴리스틱 가중치 적합(두 조건), 전환 임계 규칙, 인기도 항(pop_clicks 원값 + shrunk CTR), MMR λ 잠정 0.5 재확인 | 제안됨→E7 후 | §3.4 C, §3.5 | heuristic_fit, E6 |
| 0015 | 요청 시점 추천 설계(코드 14곳 참조): 후보 합집합·동적 창, 콜드 체인 k*, 폴백 `popular→recent→empty`, TTL 캐시(결정론 부분만), ScorerStack·shadow, `RECSYS_FEATURE_FN=recsys_core.serving`, 단기 상태 저장소 절(0017 흡수, 코드 주석 2곳 수정), 워커 수 규칙 | 채택됨(측정 대기→M6 후 채택됨) | §3.2·§3.4·§3.8 | latency_v1, parity_v1 |
| 0016 | HNSW 미채택(exact scan p95 실측, 재검토 규모) | 채택됨 | §3.8 | latency_v1 한 표 |
| 0019 갱신 | 증거 절(격자·OPE 검증·피로 규칙), "스토리 중복이 reactivity에 미치는 영향" 유지 | 제안됨→채택됨 | E9·E10 | grid_v1, ope_validation |
| 0025 | 노출·클릭 로그 v2, ε-균등 탐색 슬롯(위치 무작위, propensity 식, 캐시 뒤 추출), OPE(replay 1차·SNIPS 2차·ESS 정의), 노출 피로 규칙, 온라인 평가 계획(E13 1차 지표, 파워 표 80,685/팔·설계 효과, 인터리빙 후순위, A/B 전환 조건), 한국어 자기 학습 규칙(§3.7), 귀속 각주 | 제안됨→E9 후 채택됨 | §3.5·§3.6·§3.7 | ope_validation, logging_v2 |
| 0031 | 콜드스타트·언어 전이 계약(k*, 랭크 정규화 손실 ≤0.01, onb_cos "전이 미검증", KS는 [KR-eval] 이월, 임베딩 sanity) | 제안됨→E2·E3 후 | §3.3·§3.5 | ebnerd_v1_2_cold |
| 0033 | 단일 피처 구현과 parity 게이트(recsys_core 유일, 서빙 어댑터 경계, 증분 프로필 상태 정의, 단기 cap 20, sessionizer, 게이트 4항목, "겹침 0.9 단독" 폐기, 팀 레시피 은퇴) | 채택됨(M3) | §3.3 | parity_v1, CI job |

`docs/fable-review` 7절 번호표는 이 배정(0017→0031, 0026→0033, 0017 흡수)에 맞춰 갱신해야 한다(decision-map 6절과 동일).

---

## 8. 이력서에서 주장할 수 있게 될 것과 그 전제

귀속 규칙(ADR 0013/0015/0025 공통 각주): 팀 시절 LightGBM+MMR 추천기와 `MMRReranker`는 성승우 설계·구현. `team-final` 이후의
재현 하네스·ADR 0007·recsys_core·EB-NeRD 하네스·ranker v2·요청 시점 서빙·시뮬레이터·로그 v2·탐색·OPE는 본인. "추천 모델을 만들었다/
소유했다"는 팀 시절에 대해 쓰지 않는다.

| 문장 골자 | 지금 상태 | 전제(완료 조건) |
|---|---|---|
| 팀 보고 MRR 0.897의 추론 시점 누출을 재현 하네스로 규명(+0.18 [+0.04, +0.31], n=31 합성 유저, 3 seed), point-in-time 프로토콜 ADR 제정, 본인의 v1 결론 철회(P@5 0.34 vs popularity 0.48) [synthetic team data] | 거의 방어 가능 | M1(어드버서리얼 6건 반영, 안전 문장 8개 범위 안) |
| EB-NeRD small(평가 24.5만 노출)에서 사전 등록 규칙·3 seed·유저 부트스트랩 CI로 7단계 ablation; nDCG@10 0.395→0.655; 개선의 대부분이 네거티브 구성(+0.187)과 trailing 인기도(+0.057); 서빙형 풀에서 피처 기여 분해(+0.049/+0.023/+0.015) [EB-NeRD] | **지금 방어 가능** | 문장에 [EB-NeRD] 라벨과 "서비스 성능 아님" 유지 |
| 콜드 regime 전이를 사전 등록 실험으로 측정(인기도 마스킹·저트래픽 서브샘플·풀 축소·히스토리 절단 k*) | 사전 등록 대기 | M4. CI가 결론을 못 내면 "측정 인프라"로 낮춰 말함 |
| 오프라인 평가와 요청 시점 서빙이 같은 point-in-time 피처 코드를 쓴다(CI parity 게이트 max\|Δ\|<1e-6·τ=1.0·후보 구성 동일), 장기 프로필은 증분 감쇠 상태로 O(1) 읽기 | 코드 없음 | M3 parity_v1 초록 |
| 첫 사용자부터 정책 비교가 가능한 서빙: 노출·클릭 연결 로그, ε-균등 탐색 슬롯과 정확한 propensity, replay/SNIPS OPE를 시뮬레이터에서 검증(상대오차 ≤15%) [SIM] | 코드 없음 | M2·M6 ope_validation 통과. 시뮬레이터는 [SIM] 라벨과 "정확도 무주장" 고정 |
| 검증된 랭커를 shadow로 서빙(점수 병기 로그), 활성은 사람 클릭 데이터에 적합한 휴리스틱 | 코드 없음 | M2 shadow 경로 + M5 E7 |
| 요청 시점 추천 p95 < 300ms, 폴백률 <5% @20 RPS [LOAD-mac] | 코드 있음, 수치 없음 | M6 latency_v1 |
| 사용자가 없을 때 무엇을 주장하지 않는가(절대 CTR·서비스 향상률·"배포됨"·"LightGBM 배포"를 쓰지 않는 규칙) | 규칙 문서화 대기 | M7 |
| 신경망 사용자 모델(NRMS-lite/SASRec-lite/스태킹)과 GBDT를 같은 프로토콜·같은 정보·같은 튜닝 예산·사전 등록 판정으로 비교하고 결과가 어느 쪽이든 수치로 기록 [EB-NeRD] | 사전 등록 대기 | M4b. 통과해도 "우위"가 아니라 "ΔnDCG@10 +x [CI], 같은 프로토콜"로만; 미통과면 "측정 인프라와 수치가 있는 기각 근거"로 낮춰 말함 |

**쓰면 안 되는 것(변동 없음)**: 한국어 절대 정확도, 서비스 CTR 향상률, 0.897 재현·설명, "LightGBM 배포/운영"(shadow 전),
딥러닝 대비 우위(E15가 두 과제 모두 통과하기 전까지; 통과해도 "[EB-NeRD] 같은 프로토콜에서 ΔnDCG@10 +x [CI]"라는 조건부 표현만, "신경망을 이겼다/썼다"는 없음),
다중 주(week) 일반화, EB-NeRD 리더보드 비교(RecSys Challenge 2024 AUC 포함), 시뮬레이터 정확도, "MMR로 다양성 크게 개선", "정확도 손실 없이".

---

## 9. 참고 문헌 (접근일 2026-09-26)

**저장소 내부**
- `docs/design/2026-09-26-fable-architecture-review.md` (origin/docs/fable-review) — 종합 리뷰, D1·D4·D5·D6·D13.
- `docs/adr/DECISION-MAP.md` (origin/docs/decision-map) — ADR 인벤토리, 번호 충돌(0017/0023/0026), 브랜치별 수정 항목.
- `docs/adr/0013-ranker-v2-design.md`, `reports/recsys/ebnerd_v1.{md,json}`, `reports/recsys/ebnerd_v1_1_poolneg.json` (origin/eval/ebnerd-harness 3df4df6).
- `docs/adr/0007-recsys-offline-evaluation-protocol.md`, `reports/recsys/team_repro_v2.{md,json}` (origin/eval/team-baseline-repro-v1 1585ce6);
  2차 어드버서리얼 검토 결과(워크플로 저널 `wf_5a473f22-6ed`, #3 challenge:repro-v2 — 저장소 밖).
- `backend/app/recsys/*`, `backend/alembic/versions/8b7f830013b7_*`, `evaluation/serving/*`, `tests/integration/test_realtime_recsys_seeded_db.py` (origin/feat/realtime-recommendation 5e84358).
- `docs/adr/0019-user-simulator-design-and-claim-scope.md`, `sim/*` (origin/feat/user-simulator-loadtest b3b20e4).
- `docker/crontab`, `jobs/tasks/{train,user_embed,batch_fallback}.py`, `docs/adr/0006` (origin/feat/runtime-compose-and-collection b6d4c0c).
- `reports/README.md`, `docs/adr/0023` (origin/fix/cleanup-and-claim-scrub).

**데이터셋·대회**
- EB-NeRD 데이터셋 논문: https://arxiv.org/abs/2410.03432
- RecSys Challenge 2024 1위 해법(팀 ":D" — Transformer + LightGBM + CatBoost 3단계, 시간 인식 피처; 리더보드 AUC는 우리 split과 비교 불가): https://arxiv.org/abs/2409.20483 ,
  https://doi.org/10.1145/3687151.3687160
- FeatureSalad(PoliMi) 공개 코드: https://github.com/recsyspolimi/recsys-challenge-2024-ekstrabladet
- 교차언어 뉴스 추천 NaSE(zero-shot 전이): https://arxiv.org/html/2406.12634v1

**신경망 사용자 모델·비교 방법론(E15)**
- Wu et al. 2019 NRMS(multi-head self-attention 뉴스 추천): https://aclanthology.org/D19-1671/
- Kang & McAuley 2018 SASRec(self-attentive sequential recommendation): https://arxiv.org/abs/1808.09781
- Hidasi et al. 2016 GRU4Rec(세션 기반 RNN; E15에서는 SASRec-lite가 대표): https://arxiv.org/abs/1511.06939
- Zhou et al. 2018 DIN(target attention): https://arxiv.org/abs/1706.06978
- Chen et al. 2024 BGE M3-Embedding(다국어·다기능 임베딩; E15의 고정 아이템 벡터): https://arxiv.org/abs/2402.03216
- Wolpert 1992 Stacked generalization(OOF 스태킹의 근거): https://doi.org/10.1016/S0893-6080(05)80023-1
- Ferrari Dacrema, Cremonesi, Jannach 2019 "Are We Really Making Much Progress?"(신경망 추천 vs 튠된 기준선): https://arxiv.org/abs/1907.06902
- Rendle, Krichene, Zhang, Anderson 2020 "Neural Collaborative Filtering vs. Matrix Factorization Revisited"(기준선 튠의 중요성): https://arxiv.org/abs/2005.09683
- PyTorch 재현성 노트(`use_deterministic_algorithms`, `CUBLAS_WORKSPACE_CONFIG`): https://pytorch.org/docs/stable/notes/randomness.html
- Colab CLI(`colab new/upload/exec/download/stop/usage`): https://github.com/googlecolab/colab-cli ; Colab 컴퓨트 유닛 FAQ: https://research.google.com/colaboratory/faq.html

**오프폴리시 평가·탐색·위치 편향**
- Li et al. 2010 LinUCB 뉴스 추천: https://arxiv.org/abs/1003.0146
- Li et al. 2011 replay(오프라인 밴딧 평가): https://arxiv.org/abs/1003.5956
- Swaminathan et al. 2017 슬레이트 OPE(PI 추정기): http://papers.neurips.cc/paper/6954-off-policy-evaluation-for-slate-recommendation.pdf
- LIPS(슬레이트 OPE 추상화, 2024): https://arxiv.org/html/2402.02171
- Additive control variates in OPE(2026): https://arxiv.org/html/2602.14914
- Agarwal, Zaitsev, Wang, Li, Najork, Joachims 2019 "Estimating Position Bias without Intrusive Interventions"(개입 수확): https://dl.acm.org/doi/10.1145/3289600.3291017 (PDF https://www.cs.cornell.edu/~tj/publications/agarwal_etal_19a.pdf)

**온라인 평가**
- Radlinski, Kurup, Joachims 2008 team-draft interleaving: https://dl.acm.org/doi/10.1145/1458082.1458092 (PDF https://www.cs.cornell.edu/people/tj/publications/radlinski_etal_08b.pdf)
- Chapelle et al. 2012 인터리빙 대규모 검증(A/B 대비 민감도): https://dl.acm.org/doi/10.1145/2094072.2094078

**피처·인기도·피로·다양성**
- Google "Rules of Machine Learning" Rule #29(서빙 시점 피처를 로그해 학습에 쓴다): https://developers.google.com/machine-learning/guides/rules-of-ml
- Lee, Lakshmanan, Tiwari, Shah 2014 "Modeling Impression Discounting in Large-scale Recommender Systems"(KDD): https://dl.acm.org/doi/10.1145/2623330.2623356
- Agarwal, Chen, Elango 2009 "Spatio-temporal models for estimating click-through rate"(Gamma-Poisson 축소, 반복 노출 피로): https://dl.acm.org/doi/10.1145/1526709.1526713
- Steck 2018 Calibrated Recommendations: https://dl.acm.org/doi/10.1145/3240323.3240372
- LightGBM 파라미터 문서(lambdarank, lambdarank_truncation_level, label_gain): https://lightgbm.readthedocs.io/en/latest/Parameters.html

**LLM 사용자 프로필(보류 항목의 근거)**
- LettinGo(2025): https://arxiv.org/html/2506.18309v1
- LLMs for User Interest Exploration(2024): https://arxiv.org/pdf/2405.16363

**인프라**
- pgvector 0.8.0 iterative index scan: https://www.postgresql.org/about/news/pgvector-080-released-2952
- pgvector 필터링 문서: https://docs.pgedge.com/pgvector/v0-8-1/filtering/

**평가 방법론**
- Ji et al. 2023 추천 오프라인 평가의 데이터 누출: https://dl.acm.org/doi/10.1145/3569930
- Gelman & Loken, garden of forking paths(사전 등록 근거): https://sites.stat.columbia.edu/gelman/research/unpublished/p_hacking.pdf
