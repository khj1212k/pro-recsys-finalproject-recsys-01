# ADR 0019: 합성 사용자 시뮬레이터 설계와 주장 범위

> 이 ADR과 `reports/sim/`의 모든 시뮬레이터 수치는 **[SIM] 시스템 반응 지표만, 정확도 무주장**이다.
> 부하 테스트 수치는 **[LOAD]**로 표시하며 측정한 하드웨어·대상 앱을 함께 적는다.

## 상태
제안됨 (2026-09-26) — 아래 "사전 등록" 절은 지표 타당성 격자를 돌리기 **전에** 커밋했다.
격자 결과가 나오면 "증거" 절을 채우고 상태를 갱신한다.

## 컨텍스트
- 실사용자가 없다. 추천 시스템이 "동작하는지"(신규 사용자에게 무엇을 보여주는지, 클릭에
  반응하는지, 관심이 바뀌면 따라가는지, 실패 시 무엇을 내보내는지, 클릭이 로그 테이블까지
  흘러가는지)를 볼 수단이 필요하다. 부하 테스트에도 가입·온보딩·조회·클릭을 하는 가상
  사용자가 필요하다.
- 팀이 남긴 합성 데이터(`data/team_archive/synthetic_dataset`, gitignore)는 이 용도로 쓸 수 없다.
  - LLM 페르소나 100명(`S{n}_{나이}_{Scanner|Regular|Deep}_{성별}`)이 뉴스레터 195개 전부를
    한 번씩 보고, 클릭 여부를 LLM이 페르소나 정보(선호 카테고리 포함)로 추론했다. 추천
    모델이 쓰는 카테고리 피처와 정답 라벨이 같은 정보에서 나와 **순환적**이다.
  - 클릭률이 비현실적이다: 19,500노출 중 9,281클릭 = **47.6%**, 사용자별 4.6%~97.4%
    (`python -m sim.calibration --team-ctr-csv ...`, 아래 "증거").
- 현재 main의 `/newsletters/today`는 전날 밤 배치(`news_letter_today_batch`)를 읽기만 한다.
  요청 시점 추천은 별도 브랜치(`feat/realtime-recommendation`)에서 설계 중이며, 그쪽 API는
  응답 출처를 `X-Rec-Source` 헤더(realtime·cold_start_*·batch·popular·recent·empty)로 알린다.
  시뮬레이터는 두 설계 모두를 같은 계약으로 몰아야 한다.

## 검토한 대안

### 1. 클릭을 무엇으로 만들 것인가
1. 팀의 LLM 페르소나·클릭 로그 재사용 — 순환성·47.6% 클릭률 때문에 기각.
2. 노출마다 LLM이 "이 사용자라면 클릭할까"를 판단하는 에이전트형 사용자 — 수천~수만
   노출에 호출 비용이 들고(Gemini 선불 잔액이 작다) 재현성이 없으며, LLM이 카테고리·제목을
   읽고 판단하므로 순환성도 그대로 남는다. 기각.
3. 공개 로그(EB-NeRD) 재생 — 덴마크어 기사라 한국어 API를 처음부터 끝까지 몰 수 없고,
   라이선스상 이 Mac 밖으로 못 나간다. **기저 클릭률 대조에만** 사용.
4. BGE-M3 코사인 유사도를 클릭 확률 피처로 쓰는 파라미터 모델 — 추천기가 같은 임베딩으로
   순위를 매기므로 추천기를 자기 정답지로 채점하게 된다. 기각.
5. **손으로 쓴 아키타입 + 위치 기반(position-based) 파라미터 클릭 모델 (채택)** — 시드로 완전
   재현되고, 비용이 0이며, 피처 가중치를 바꾼 민감도 설정을 둘 수 있다. 대신 정확도 주장이
   불가능하다는 점을 이 ADR의 주장 범위로 못박는다.

### 2. 부하 도구
1. k6 — 가상 사용자 로직을 JS로 다시 써야 해 행동 시뮬레이터와 로직이 갈라진다.
2. wrk/hey — 가입→로그인→온보딩→조회→클릭 같은 상태 있는 흐름을 표현하기 어렵다.
3. **Locust (채택)** — 파이썬이라 `SimUser`·`ClickModel`·`ApiClient`를 그대로 재사용한다.
   gevent 기반이라 동기 코드에서 CPU를 오래 쓰면 지연이 부풀려지는 함정이 있어, Kiwi 모델
   로딩을 import 시점에 끝낸다(`sim/locustfile.py`).

## 결정

### 페르소나 (`sim/personas.py`)
- 아키타입 10종: 정치 고관여층, 재테크 투자자, 테크 얼리어답터, 스포츠 팬, 부동산·생활경제,
  국제 뉴스 관심층, 생활·문화 소비자, 사회 이슈 관심층, 가벼운 훑어보기, 시사 심층 독자.
  각각 7개 카테고리(100 정치 … 700 세계) 혼합 비율, 키워드 시드(예: 반도체·부동산·야구),
  선호 언론사, 하루 세션 수 λ ∈ {1, 3, 6}, 위치 인내심 η, 신선도 선호, 반복 피로도,
  drift 대상 아키타입을 가진다. **로그에 맞춘 값이 아니라 손으로 쓴 값**이다(맞출 실로그가 없다).
- 사용자는 아키타입에 디리클레 잡음을 얹어 만든다(카테고리 집중도 30, 언론사 10, 키워드는
  시드에서 4개 이상 부분집합 + 30% 확률로 다른 아키타입 키워드 1개, η·피로도 로그정규 잡음).
  모든 아키타입이 소표본에서도 나오게 층화 후 섞는다.
- 10%는 시뮬레이션 도중 가입(콜드 스타트), 첫날부터 있던 사용자 중 10%는 3일차에 핵심
  카테고리가 겹치지 않는 아키타입으로 바뀐다(drift). 10개 drift 쌍 모두 핵심 카테고리가 서로소다.
- 합성 계정 표시: 이메일 `<run_tag>-<seed>-<index>@sim.invalid`(RFC 2606 예약 도메인),
  닉네임 `sim_<archetype>_<index>`, 코드상 `SimUser.is_synthetic=True`. DB `user` 테이블에는
  별도 플래그 컬럼이 없으므로 **`@sim.invalid` 도메인이 DB에서의 합성 표시**다.

### 클릭 모델 (`sim/click_model.py`)
- P(click | 아이템, 순위 r, 사용자 u) = (1/(r+1))^η_u · σ(w_u·φ + b)
- φ = [카테고리 선호, 뉴스레터 제목·요약·키워드의 Kiwi 명사와 페르소나 키워드의 Jaccard,
  언론사 선호, exp(−나이/τ) (τ=24h), log1p(raw_news_count), log1p(노출 수 + 3·과거 클릭 수)].
  w = (4, 8, 1, 1, 0.3, −1), 사용자별로 신선도 가중치 × 2·novelty_u, 반복 가중치 × fatigue_u.
- **BGE-M3 코사인은 φ에 넣지 않는다.** 그래도 키워드·카테고리 겹침은 임베딩 유사도와
  상관이 있으므로, 이 시뮬레이터는 **콘텐츠 기반 추천기를 구조적으로 유리하게 평가한다**.
  이를 드러내는 민감도 설정으로 프리셋 `category_only`(키워드 가중치 0)를 함께 둔다.
- 한 번의 `/today` 응답에서 상위 20개(view_depth)를 순위별 독립 베르누이로 클릭한다.
  세션은 클릭이 있을 때만 이어진다(읽고 돌아와 다시 목록을 봄, 세션당 최대 4회 조회).
  온보딩 선택과 부하 테스트의 클릭 태스크는 위치를 무시한 Gumbel-top-k(Plackett-Luce) 선택이다.
- 모든 난수는 `numpy.random.default_rng([seed, 용도, 사용자, …])`로 분리해, 같은 시드는 같은
  조회·클릭 시퀀스를 재현한다(테스트로 고정).

### 기저 클릭률 보정 (`sim/calibration.py`)
- 편향 b는 **무작위 순위의 top-10 기대 CTR = 2%**가 되도록 이분법으로 푼다. 무작위 정책에
  맞추는 이유: 나중에 어떤 추천기를 붙이든 기준점이 그 추천기와 독립이어야 한다.
- 오라클(시뮬레이터 자신의 매력도로 정렬)의 top-10 CTR이 6~12% 범위에 드는지 확인한다.
- 근거로 삼은 공개 수치:
  - MIND(MSN 뉴스 노출 로그): 노출 아이템 중 클릭 비율 약 4% — MIND 대회 기술 보고서
    (Sogou, 2020, msnews.github.io/assets/doc/1.pdf)가 "only about 4% positive ratio"로 보고.
  - EB-NeRD small(Ekstra Bladet, 로컬 계산): in-view 기사당 클릭 비율 train 9.06%,
    validation 8.41%. 단, 이 데이터셋은 클릭이 1개 이상 있는 노출만 공개하므로
    **무조건부 CTR의 상한**이다(노출당 in-view 중앙값 8·9개).
  - 두 수치 모두 실서비스 추천기가 고른 목록의 CTR이다. 무작위 목록은 그보다 낮아야 하고
    (2%), 정답을 아는 오라클은 그 범위 근처(6~12%)여야 한다는 것이 목표 설정의 논리다.
  - 팀 합성 데이터의 47.6%는 이 범위의 5~10배다.
- 보정 결과는 프리셋에 고정 값으로 싣고(default b=−5.018, category_only b=−4.624),
  `tests/simulator/test_sim_calibration.py`가 기준 설정(`sim/reference.py`: 300명, 합성
  카탈로그, 2026-01-05 12:00 UTC)에서 다시 풀어 어긋나면 실패한다.

### 드라이버와 대상 (`sim/driver.py`, `sim/fake_app.py`)
- API 계약만 쓰는 블랙박스 HTTP 드라이버: `POST /auth/signup`, `POST /auth/login`,
  `GET /onboarding/news?category=&limit=6`, `PUT /users/me/newsletters`,
  `PUT /users/me/categories`, `GET /newsletters/today`, `POST /logs/newsletter/click`,
  `GET /newsletters/{id}`. 프런트엔드의 온보딩 순서(뉴스레터 → 카테고리)를 따른다.
- 같은 드라이버가 세 대상에 붙는다: (1) 프로세스 내 가짜 FastAPI 앱(장난감 정책 5종,
  가상 시계), (2) 실제 `backend/app` 라우터 + SQLite(계약 테스트
  `tests/simulator/test_sim_backend_contract.py`), (3) 실행 중인 스택의 URL.
- 가짜 앱의 장난감 정책은 **지표가 알려진 설계 차이를 구분하는지** 보려는 테스트 더블이다:
  `static_batch`(현재 설계: 밤 배치, 배치 없으면 빈 목록), `static_batch_fallback`(배치 없으면
  인기 목록), `reactive`(요청마다 온보딩·클릭으로 재정렬), `reactive_explore`(reactive + top-10 중
  3칸을 상위 2개 관심 밖 카테고리로 탐색), `random`. 운영 추천기가 아니다.

### 행동 지표 (`sim/metrics.py`, k=10)
- 콜드 스타트: 도중 가입자의 첫 `/today`가 비어 있지 않은 비율(`first_view_coverage`), 첫
  응답 top-10 중 온보딩에서 고른 카테고리 비율.
- 클릭 반응성: 같은 세션에서 클릭 직후 다음 응답과의 top-10 Jaccard, 클릭한 아이템과 비슷한
  아이템(같은 카테고리 또는 명사 Jaccard ≥ 0.2) 비율의 클릭 전후 차이(`similar_share_lift`).
- drift 적응: drift 후 top-10의 50% 이상이 새 아키타입 핵심 카테고리가 될 때까지의 요청 수
  (중앙값·p90), 적응 비율, 관측 기간 내 미적응(censored) 수.
- 서빙: 빈 응답 비율, `X-Rec-Source` 기준 폴백률(fallback·popular·recent·empty; 헤더가 없는
  현재 main API에서는 측정 불가로 `None`), 엔드포인트별 오류율·p50/p95.
- 참여: top-10 실현 CTR(보정 상태 점검용이지 품질 점수가 아니다), 클릭 ACK 비율(클릭이
  로그 API까지 도달한 비율).

### 부하 테스트 (`sim/load.py`, `sim/locustfile.py`, `sim/loadtest.py`)
- ActiveReader: 기존 계정으로 `/newsletters/today`(가중치 9)와 직전 목록 중 클릭 모델이 고른
  1건 클릭(가중치 1). `constant_throughput(1)`이라 사용자 수 ≈ 목표 RPS.
- Newcomer(1명 고정): 5초마다 새 `@sim.invalid` 계정으로 가입→로그인→온보딩→첫 `/today`
  (`today_first_view`로 따로 집계).
- `python -m sim.loadtest`가 5/20/50 RPS 단계를 각각 헤드리스 Locust로 돌리고(계정 준비 구간은
  `--reset-stats`로 제외) 엔드포인트별 요청 수·달성 RPS·p50/p95/p99·오류율 표를 만든다.
  Locust는 2xx가 아니면 실패로 세므로, 계약상 기대 상태코드(재실행 시 signup 400)는 성공으로
  판정하도록 `ApiClient`가 `catch_response`를 쓴다.

## 사전 등록: 지표 타당성 격자

목적: 위 지표가 **행동이 알려진** 장난감 정책들을 기대한 방향으로 구분하는지 확인한다. 이
격자는 운영 추천기에 대해 아무것도 말하지 않는다.

### 설계
- G1(주): 합성 카탈로그, 정책 5종 × 클릭 모델 프리셋 2종(default, category_only) × 시드 {0, 1, 2},
  사용자 300명, 7일, drift 3일차, 도중 가입 10%, drift 10%, k=10.
  `python -m sim.experiments --out-dir <dir> --workers 4`
- G2(강건성): 팀이 생성한 실제 뉴스레터 195개(로컬, gitignore) 카탈로그, 같은 정책·프리셋·시드,
  편향은 실행마다 이 카탈로그에서 다시 보정(`--calibrate`). 카테고리 라벨 일부는 kNN 추정값.
  `python -m sim.experiments --catalog team_archive --team-archive-dir data/team_archive ...`
- 결과: `reports/sim/grid_v1.{json,md}` (요약만; 실행별 JSON과 기사 텍스트는 커밋하지 않음).

### P: 구성상 반드시 성립해야 하는 것 (모든 시드·두 프리셋에서)
위반은 드라이버·지표·가짜 앱의 **버그**로 보고, 원인과 수정을 기록한 뒤 다시 돌린다.
- P1 `first_view_coverage`: static_batch = 0, 나머지 4개 정책 = 1.
- P2 서빙: static_batch는 `empty_rate` > 0이고 `fallback_rate` = `empty_rate`;
  static_batch_fallback은 `fallback_rate` > 0, `empty_rate` = 0;
  reactive·reactive_explore·random은 `empty_rate` = 0, `fallback_rate` = 0.
- P3 모든 실행에서 `error_rate` = 0, `click_ack_rate` = 1.
- P4 static_batch: `after_click_jaccard_mean` = 1, `similar_share_lift` = 0.
- P5 reactive: `after_click_jaccard_mean` < 1, `similar_share_lift` > 0.

### H: 지표 민감도에 대한 방향 가설 (프리셋별 3시드 평균으로 판정, 시드별 최소·최대 병기)
위반은 **지표의 약점**으로 기록하고, 사후에 정의를 바꿔 통과시키지 않는다. 가설이 깨진
지표는 이 ADR에 "타당성 미확인"으로 표시하고 운영 시스템에 대한 근거로 쓰지 않는다.
- H1 첫 응답의 온보딩 카테고리 비율: reactive > static_batch_fallback, reactive > random.
- H2 클릭 직후 Jaccard: random < reactive_explore < reactive.
- H3 클릭 후 유사 아이템 비율(`similar_share_after_click`): reactive > reactive_explore.
- H4 `similar_share_lift`: reactive > random.
- H5 drift: reactive의 `adapted_rate` ≥ static_batch, `requests_to_adapt_median` ≤ static_batch.
- H6 `ctr_top_k`: reactive > random (3시드 각각에서도).
- H7 random의 `ctr_top_k`가 [1%, 3%] 안 — 정적 보정(2%)이 반복 노출·시간 흐름이 있는 동적
  시뮬레이션에서도 크게 어긋나지 않는지.
- H8 구조적 편향의 크기: `ctr_top_k(reactive) / ctr_top_k(random)`이 default > category_only.
  (키워드 피처가 콘텐츠 기반 정책을 얼마나 더 유리하게 만드는지)

### X: 방향을 등록하지 않는 탐색 항목 (있는 그대로 보고)
- X1 random의 `adapted_rate`·`requests_to_adapt_median` — 무작위 top-10이 2/7 카테고리에서 5개
  이상을 우연히 채울 확률(이항 근사 약 12%/요청) 때문에 drift 지표의 우연 수준이 얼마인지.
- X2 static_batch 대비 reactive의 `ctr_top_k`.
- X3 G2에서 P·H가 G1과 같게 나오는지.

### 등록 시점의 사정 (투명성)
- 이 절을 쓰기 전 이번 세션에서 reactive/default/시드 0 한 번을 **실행 시간 측정용**으로 돌렸고
  (약 14초), 그 지표는 열어보지 않았다. 이전 세션에서 격자 실행기를 만들며 탐색적으로 돌렸는지는
  남은 산출물이 없어 확인할 수 없다.
- P1·P4·P5와 비슷한 단언 일부는 이미 소규모(40명, 4일) 단위 테스트
  (`tests/simulator/test_sim_driver_fake_app.py`)에 있다. P는 새 발견이 아니라 규모를 키웠을 때도
  깨지지 않는지 보는 회귀 점검이다.

## 증거
(격자 실행 후 채움)

### 기저 클릭률 (2026-09-26 로컬 실행, `python -m sim.calibration --ebnerd-dir ... --team-ctr-csv ...`)
| 출처 | 값 | 비고 |
|---|---|---|
| 시뮬레이터 default, 무작위 top-10 | 2.00% (b = −5.0177) | 보정 목표 |
| 시뮬레이터 default, 오라클 top-10 | 11.76% | 6~12% 범위 안 |
| 시뮬레이터 category_only, 무작위 / 오라클 | 2.00% / 8.04% (b = −4.6237) | |
| MIND (MSN) | 약 4% | 대회 기술 보고서 인용 |
| EB-NeRD small train / validation | 9.06% / 8.41% | 클릭 있는 노출만 공개 → 상한 |
| 팀 합성 데이터 | 47.6% (사용자별 4.6~97.4%) | LLM 추론 클릭 |

## 결과와 한계

### 이 시뮬레이터로 보일 수 있는 것
- API 계약 수준의 **데이터 흐름**: 가입·온보딩·조회·클릭이 오류 없이 돌고, 클릭이 로그
  테이블까지 도달하는지(계약 테스트에서 클릭 ACK 수 = 로그 행 수).
- 설계 차이에서 오는 **정성적 행동**: 신규 사용자가 빈 목록을 받는지, 클릭 후 목록이 바뀌는지,
  관심이 바뀐 사용자를 며칠·몇 요청 만에 따라가는지, 폴백이 얼마나 자주 나가는지.
  단, 이 판단은 지표 타당성 격자에서 해당 지표가 검증된 경우에만 쓴다.
- 부하 [LOAD]: 주어진 하드웨어·대상에서 목표 RPS별 지연 분포와 오류율.
- (예정) 오프폴리시 평가 파이프라인의 검증: 한 정책의 노출·propensity 로그로 다른 정책의 CTR을
  추정한 값이 시뮬레이터 안에서의 실측과 맞는지. 정답을 아는 환경이 필요할 때 쓰는 용도다.

### 보일 수 없는 것
- **정확도·CTR 주장 불가.** 클릭 모델은 손으로 쓴 가정이며 실사용자에 맞춘 적이 없다.
  "시뮬레이터에서 CTR x%"나 "정책 A가 B보다 y% 낫다"는 운영 성능 주장으로 쓰지 않는다.
- **콘텐츠 기반 편향.** 키워드·카테고리 겹침이 클릭 확률을 올리므로, 같은 정보를 쓰는
  추천기가 유리하다. 합성 카탈로그에서는 페르소나 키워드와 뉴스레터 주제어를 같은 풀에서
  뽑아 겹침이 부분적으로 구성상 참이다(`sim/catalog.py` TOPIC_POOLS). `category_only` 프리셋과
  팀 뉴스레터 카탈로그(G2)는 이 편향의 크기를 가늠하는 도구이지 제거 수단이 아니다.
- **협업 신호 부재.** 사용자 간 취향 상관은 아키타입 공유로만 생긴다. 협업 필터링의 이점은
  과소평가될 수 있다.
- **스토리 중복.** 실제 파이프라인은 같은 사건을 날마다 새 뉴스레터 ID로 다시 만든다.
  합성 카탈로그에는 이런 중복이 없다. 실스택에 붙이면 `similar_share`가 "어제 클릭한 사건의 오늘
  재생성본"을 반응성으로 셀 수 있으므로, 스토리 식별자가 생기기 전까지 실스택의 반응성 수치는
  이 위험을 적어 두고 해석한다.
- **시간 모델 단순화.** 세션 시각은 하루 안에서 균등, 조회 간격 90초 고정, 요일·시간대 효과 없음.
- 부하 수치는 측정한 대상에만 해당한다. 가짜 앱 대상 수치는 하네스 자체의 처리 능력이고,
  실제 API 수치도 이 Mac·단일 uvicorn 워커·빈 캐시 조건의 값이다.
