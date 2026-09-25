# ADR 0009: LLM 평가 프로토콜과 사전 등록한 모델 선정 규칙 (bake-off v1)

## 상태
채택됨 (2026-09-25) — **사전 등록(pre-registration)**. 이 문서와
`evaluation/llm/preregistration/bakeoff-v1.yaml`은 bake-off 결과가 하나도 없는
시점에 커밋됐다(이 커밋 시점에 실제 한국어 기사 데이터 수집이 막 시작됐고, Gemini
유료 호출은 HTTP 402로 막혀 있어 어떤 후보도 호출된 적이 없다). 이후 규칙을 바꾸려면
본문을 고치지 않고 맨 아래 "Addendum"에 날짜·사유·결과 열람 여부를 남긴다.

## 컨텍스트
- ADR 0005는 프로바이더 추상화만 다루고 **모델 선정은 후속 bake-off로 미뤘다.** 현재
  기본값(`gemini-3.5-flash-lite` 생성 / `gemini-3.5-flash` 평가)은 "일단 동작하는" 값이다.
- 현재 LLM 품질 신호는 전부 한 곳에서 나온다: `workflow/evaluators.py::NewsletterEvaluator`.
  문제는 네 가지다.
  1. judge가 원문 **제목 5개만** 본다. 본문을 안 보는 judge는 환각(원문에 없는 수치/인용)을
     판정할 수 없다.
  2. 생성기와 judge가 같은 계열(Gemini)이다 → 자기선호편향(self-preference bias) 위험.
  3. PASS 기준 `score >= 5`는 한 번도 사람 라벨과 대조해 보정된 적이 없다
     (`Settings.MIN_NEWSLETTER_SCORE=7`은 아무 데서도 쓰이지 않는 죽은 설정이다).
  4. `ClusterEvaluator`의 `confidence`도 쓰이지 않는다(`Settings.MIN_CLUSTER_CONFIDENCE=0.7` 역시 미사용).
- 추천 쪽에서 팀이 보고한 MRR 0.897이 추론 시점 누수로 부풀려졌던 전례(ADR 0007 작업)가
  있다. 같은 실수를 LLM 쪽에서 반복하지 않으려면 **결과를 보기 전에** 지표·임계값·
  동점 처리·표본 크기의 한계를 고정해야 한다. 결과를 보고 나서 기준을 고르면
  (garden of forking paths) 어떤 후보든 이기게 만들 수 있다.
- 이 프로젝트의 라벨러는 사실상 1명(사용자)이다. 라벨 예산이 표본 크기를 결정한다.

### 검증한 사실 (WebFetch, 접근일 2026-09-25)
| 항목 | 확인 내용 | 출처 |
|---|---|---|
| Gemini 가격 (paid, standard) | `gemini-3.5-flash-lite` 입력 $0.30 / 출력 $2.50, `gemini-3.5-flash` $1.50 / $9.00, `gemini-3.8-flash` $0.75 / $3.75 (2026-12-31까지, 2027-01-01부터 $1.50 / $7.50) (1M 토큰당). 문서 "Last updated 2026-09-24 UTC" | https://ai.google.dev/gemini-api/docs/pricing |
| Upstage 가격 | `solar-pro3` 입력 $0.15 / 캐시 $0.015 / 출력 $0.60 (VAT 10% 별도) | https://www.upstage.ai/pricing |
| OpenAI 가격 (standard) | `gpt-4.1-mini` $0.40 / $1.60, `gpt-4o-mini` $0.15 / $0.60, `gpt-5-mini` $0.25 / $2.00 | https://developers.openai.com/api/docs/pricing |
| Anthropic 가격 | Claude Haiku 4.5 $1 / $5 | https://platform.claude.com/docs/en/about-claude/pricing |
| Claude Haiku 4.5 모델 id / 수명 | API id `claude-haiku-4-5-20251001`, alias `claude-haiku-4-5`. **Retirement "Not sooner than October 15, 2026"** | https://platform.claude.com/docs/en/about-claude/models/overview |
| Anthropic OpenAI SDK 호환 계층 | base_url `https://api.anthropic.com/v1/`. `response_format`은 **"Ignored"**(json_object/json_schema 둘 다 무시). `temperature` 0~1, `max_tokens` 지원, `usage.prompt_tokens`/`completion_tokens` 반환. 문서 스스로 "primarily intended to test and compare model capabilities, and is not considered a long-term or production-ready solution" | https://platform.claude.com/docs/en/api/openai-sdk |
| `gpt-5-mini` | reasoning 모델("Reasoning token support"), structured outputs 지원. temperature/`max_tokens` 허용 여부는 문서에서 확인하지 못함 | https://developers.openai.com/api/docs/models/gpt-5-mini |

가격은 `ai_workspace/config/llm_pricing.yaml`에 출처 URL·접근일과 함께 기록한다(ADR 0010과 같은 브랜치).

## 검토한 대안

### 1. 무엇을 1차 지표로 삼을 것인가
1. **LLM judge 점수.** 싸고 자동이지만, 이번 평가의 목적 자체가 judge를 보정하는 것이다.
   judge로 judge를 고르는 순환이 생긴다.
2. **자동 지표(ROUGE/BERTScore 등).** 참조 요약이 없고, 여러 기사 통합형 뉴스레터에서
   참조 하나와의 겹침은 "발행 가능성"과 거의 무관하다.
3. **결정론적 사실성 검사기(`faithfulness`)만.** 재현 가능하지만 문체·구성·핵심 사실
   누락을 못 본다. 또 검사기 자체의 정밀도/재현율이 아직 측정되지 않았다.
4. **사람의 "발행 가능(Y/N)" 판정 (채택).** 제품 결정과 가장 가깝다. 다만 라벨러가 1명이라
   (a) 블라인드 처리, (b) intra-rater 재라벨로 신뢰도 측정, (c) 사전 정의한 라벨 가이드
   (`docs/eval/labeling-guide.md`)로 보완한다. 자동 지표(검사기·스키마·지연·비용)는
   **게이트**로만 쓴다.

### 2. 표본 크기
- 후보당 클러스터 n=40, 후보 3개 → 발행 판정 라벨 120개 + 클러스터 라벨 40개 + 재라벨 40개.
  라벨 1개에 3~5분이면 10시간 안팎이다. 그 이상은 1인 라벨러에게 비현실적이다.
- **n=40으로 검출 가능한 효과 크기(α=0.05 양측, 검정력 0.8)** — 아래 "증거"의 계산:
  - 짝지은 설계(같은 40개 클러스터를 모든 후보가 생성, McNemar 근사): 불일치 쌍 비율이
    20% / 30% / 40%일 때 최소 검출 차이 **19.3 / 23.6 / 27.2 %p**.
  - 짝짓지 않은 두 비율 비교(보수적 상한): 기준 발행률 0.5 / 0.6 / 0.7에서 **29.5 / 27.3 / 23.8 %p**.
  - 즉 **발행률 차이가 약 20%p 미만이면 이 bake-off는 우열을 가리지 못한다.** 그래서
    아래 규칙은 "통계적으로 구별되지 않으면 동률 → 비용으로 결정"을 명시한다. 이것은
    결과를 보고 붙인 핑계가 아니라 설계 단계에서 예상한 가장 흔한 결말이다.

### 3. 후보 구성
| 후보 | 채택 | 이유 |
|---|---|---|
| Gemini `gemini-3.5-flash-lite` | 생성 후보 | 현재 기본값(incumbent). Gemini 문서가 신규 프로젝트에 권장하는 두 모델 중 저렴한 쪽 |
| Upstage `solar-pro3` | 생성 후보 | 한국어 특화, 가장 저렴. ADR 0005에서 json_schema 호환 확인 |
| OpenAI `gpt-4.1-mini` | 생성 후보 | reasoning이 아닌 소형 모델이라 현재 어댑터의 `temperature`/`max_tokens` 인자를 그대로 받을 가능성이 높다. `gpt-5-mini`는 reasoning 모델이고 이 인자들의 허용 여부를 확인하지 못해 제외(아래 pre-flight 참고) |
| Claude `claude-haiku-4-5` | **judge 후보만** | (a) retirement가 2026-10-15 이후 언제든 가능 → 운영 생성기로 고르면 곧 교체해야 한다. (b) OpenAI 호환 계층은 `response_format`을 무시하므로 스키마 강제가 안 되고, 문서 스스로 "test and compare" 용도라고 한다 — bake-off의 비교 용도로는 맞지만 운영 경로로는 네이티브 어댑터가 필요하다. 세 생성 후보 모두와 다른 계열이라 교차 계열 judge의 기준점으로 가치가 있다 |
| `gemini-3.8-flash` | 제외 | 2027-01-01부터 입력 단가가 $1.50으로 두 배가 된다. 3.5 Flash-Lite 대비 비용 우위가 없고, 후보를 늘리면 라벨 부담이 선형으로 는다 |

- 생성 후보는 **생성·메타·문체 변환을 같은 모델로** 수행한다(운영에서도 한 프로바이더 키로
  돌리는 것이 현실적이다). judge는 후보와 독립적으로 고정한다.

### 4. judge 편향 측정 방식
1. **같은 계열/다른 계열 judge 점수의 단순 평균 비교.** 생성기 품질 차이와 편향이 섞인다.
2. **이중차분(DiD, 채택).** 사람 점수를 기준으로 judge 오차(`judge_z − human_z`)를 구하고,
   judge j의 같은 계열 출력 오차 평균 − 다른 계열 출력 오차 평균을 계산한 뒤, 그 생성기
   계열 어디에도 속하지 않는 기준 judge(Haiku)에서 같은 차이를 빼 준다. 사람이 특정 생성기
   문체를 유독 엄하게/후하게 본 효과가 상쇄된다. CI는 클러스터 단위 부트스트랩.

## 결정 (사전 등록 규칙)
기계가 읽는 원본은 `evaluation/llm/preregistration/bakeoff-v1.yaml`이고, 분석 스크립트
(`evaluation/llm/bakeoff_analysis.py`)는 이 파일의 상수만 읽어 규칙을 적용한다.
아래는 그 내용을 사람이 읽도록 옮긴 것이다.

### 평가셋
- n=40 클러스터, `evaluation/llm/evalset.py`가 DB의 `cluster_history`에서 층화 추출
  (seed 20260925). 층: 크기(3–4 / 5–9 / 10+) × 카테고리 × split_v2 여부.
  이 중 20%(8개)는 `ClusterEvaluator`가 FAIL을 낸 "어려운 사례"로 채운다(없으면 있는 만큼).
  어려운 사례는 ClusterEvaluator ROC의 음성 표본을 확보하려는 목적이다.
- 저장소에는 id/URL/본문 SHA-256만 커밋하고 본문은 `data/`(gitignore)에 둔다(저작권).
- 워밍업(pre-flight) 클러스터 2개는 평가셋과 겹치지 않게 따로 뽑는다.

### 실행 프로토콜
- 후보 × 클러스터당 **1회 생성**(재굴림·체리피킹 없음), 현재 코드의 프롬프트와 temperature
  그대로(`NewsReconstructor`/`ToneConverter`에 레지스트리 클라이언트를 주입).
- 단일 패스: 생성(본문+메타) 1회 → 문체 변환 1회. 운영 게이트(ADR 0010)의 재생성은 돌리지
  않는다 — 재생성이 섞이면 모델 고유의 사실성과 게이트 효과가 구별되지 않는다. 게이트로 인한
  추가 비용은 아래 비용 공식으로 반영한다.
- 인프라 실패(킬 스위치, HTTP 401/402/403/404)는 결과로 기록하지 않고 실행을 멈춘다(재개
  가능). 모델 품질에 속하는 실패(스키마 검증 소진, length, content_filter, 생성기의 로컬
  폴백 사용)는 **결과로 기록**하고 그대로 라벨링에 넘긴다.
- pre-flight: 워밍업 클러스터 2개로 각 후보를 한 번씩 돌려 **설정 오류(인자 거부 등)만** 고친다.
  설정으로 해결되지 않으면 본 실행 전에 후보를 빼고 Addendum에 기록한다.

### 라벨링과 블라인드
- 클러스터 라벨(단일 사건 Y/N, 이상치 id, 핵심 사실 3–6개 + 근거 기사 id)을 **출력을 보기 전에**
  먼저 단다. 핵심 사실 커버리지가 출력에 맞춰 역산되지 않게 하기 위해서다.
- 출력 라벨은 불투명 id + 무작위 순서로만 보인다. 후보 id 매핑(`blind_key.json`)은 라벨링
  UI가 읽지 않는 별도 파일에 둔다.
- 재라벨(intra-rater): 출력 30개 + 클러스터 10개를, 첫 라벨 후 48시간 이상 지나 첫 라벨을
  보지 않은 채 다시 단다.

### 1차 지표
- **사람 발행 가능률**: 후보별 40개 출력 중 `publishable=Y` 비율(전체 40개 기준). 2차로
  사람이 단일 사건으로 판정한 클러스터 부분집합의 발행률, 핵심 사실 커버리지, 문체 1–5를 보고한다.

### 게이트 (하나라도 못 넘으면 탈락)
| 게이트 | 정의 | 기준 |
|---|---|---|
| G1 미지원 수치율 | 형식체 초안(제목+한줄소개+본문) 중 결정론적 검사기가 원문 본문에서 찾지 못한 수치가 1개 이상인 뉴스레터 비율 | **≤ 0.15** |
| G2 문체 드리프트율 | 문체 변환본 중 차단 드리프트(수치 추가·누락, 절대 날짜 추가·누락, 개체명 추가; 집합 기준)가 있는 비율 | **≤ 0.20** |
| G3 첫 시도 스키마 통과율 | 응답을 한 번이라도 받은 구조화 호출(본문·메타·문체) 중 첫 응답이 스키마를 통과한 비율 | **≥ 0.95** |
| G4 p95 지연 | 뉴스레터 1건의 단일 패스(본문+메타+문체) 벽시계 시간 합의 95백분위 | **≤ 60초** |

- G1/G2의 임계값 근거: 운영 게이트는 최대 3회 생성한다. 실패가 독립이라면 p=0.15일 때 3회 모두
  실패해 클러스터를 버릴 확률은 약 0.3%(p³)이고, p=0.3이면 2.7%로 뛴다. 문체 드리프트는 형식체로
  폴백하므로 오류가 아니라 문체 손실이다 — 그래서 G2가 더 느슨하다.

### 승자 결정
1. 게이트를 모두 통과한 후보만 남긴다. 아무도 못 넘으면 **모델을 바꾸지 않는다**(현재 기본값 유지,
   어떤 게이트가 막았는지 기록, 다음 작업은 모델 교체가 아니라 프롬프트 개선).
2. 발행률 1위 A와 나머지 각 후보 B에 대해, 클러스터 단위 짝지은 부트스트랩(10,000회, seed
   20260925)으로 `rate_A − rate_B`의 95% CI를 구한다. CI가 0을 포함하는 후보는 A와 **동률 집합**이다.
3. 동률 집합이 A 하나면 A가 승자. 아니면 동률 집합에서 **뉴스레터 100건당 비용**이 가장 낮은 후보가 승자.
   - 비용 공식(생성 쪽만, judge 제외): `100 × (gen_cost × (1 + p + p²) + tone_cost × (1 + q))`.
     `gen_cost`/`tone_cost`는 측정한 단일 패스 평균 비용(`llm_pricing.yaml` 단가 × 실제 토큰),
     `p`는 운영 게이트 기본 설정(수치·인용 차단)의 차단 실패율, `q`는 G2 드리프트율
     (재생성 최대 2회, 문체 재변환 최대 1회를 반영한 기대 호출 수).

### judge 선정 (운영 게이트에 쓸 judge)
- judge 후보: Haiku 4.5(v2), `gemini-3.5-flash`(v2), `gpt-4.1-mini`(v2), `solar-pro3`(v2),
  그리고 기준선으로 Haiku 4.5(v1: 제목만 보는 기존 프롬프트).
- **승자 생성기와 다른 계열인 judge만** 대상이다. 2-fold(클러스터 단위로 폴드 분할, seed 20260925)로
  한 폴드에서 기준별 임계값을 고르고 다른 폴드에서 사람 `publishable`과의 Cohen's κ를 잰
  out-of-fold κ가 가장 높은 judge를 고른다.
- **OOF κ ≥ 0.40**일 때만 judge를 게이트로 쓴다. 미달이면 judge는 shadow(기록만)로 돌리고,
  게이트는 결정론적 검사기에 맡긴다.
- 보조 보고: Spearman(judge 기준 평균, 사람 문체 점수), judge `unsupported_claims`가 사람이
  표시한 사실 오류 구간과 겹치는 정밀도, v1 대비 v2의 κ 차이.

### 자기선호편향
- 계열(gemini/openai/upstage)마다 DiD 추정치와 95% 부트스트랩 CI를 보고한다. 결과와 무관하게
  "judge ≠ 생성기 계열" 정책은 유지하며, 이 측정은 그 정책의 근거를 수치로 남기는 용도다.

### ClusterEvaluator confidence
- 점수 `s = confidence (PASS일 때) / 1 − confidence (FAIL일 때)`로 사람 `single_event` 라벨에 대한
  ROC AUC를 잰다. **AUC ≥ 0.75이고**, 2-fold로 고른 임계값의 OOF balanced accuracy가 기존
  PASS/FAIL 결정보다 **0.05 이상 높을 때만** `MIN_CLUSTER_CONFIDENCE`를 게이트로 연결한다.
  아니면 연결하지 않는다(죽은 설정은 별도 정리).

### 사람 라벨 신뢰도
- `publishable`의 intra-rater κ < 0.60이면 1차 지표를 "신뢰 불가"로 표시하고, 승자 결정은
  "게이트 통과 후보 중 최저 비용"으로 대체한다. 이 경우에도 그 사실을 결과 ADR에 그대로 적는다.

## 증거
- 결과 증거는 **없다** — 사전 등록 문서이기 때문이다. 이 커밋의 부모 커밋까지 저장소에는
  bake-off 러너도, 결과 파일도 없다(`git log --stat`으로 확인 가능).
- 검출 가능 효과 크기 계산(설계 증거, scipy 정규 근사):
  - McNemar(Connor 1987 근사) `n = (z_{α/2}·√ψ + z_β·√(ψ − d²))² / d²`에서 n=40을 만족하는 최소 d:
    ψ=0.2 → 0.193, ψ=0.3 → 0.236, ψ=0.4 → 0.272.
  - 두 비율 z-검정 근사: p0=0.5 → 0.295, p0=0.6 → 0.273, p0=0.7 → 0.238.
  - κ의 정밀도: 관찰 일치 0.8·우연 일치 0.5 가정 시 SE ≈ √(p_o(1−p_o) / (n(1−p_e)²)).
    n=120(judge 대 사람)이면 95% CI 반폭 ≈ ±0.14, n=30(재라벨)이면 ≈ ±0.29. 재라벨 30개는
    "대략적인 신뢰도 확인" 수준이라는 뜻이다.
- 예상 비용(가격표 × 대략적 토큰 추정, 뉴스레터당 입력 ~16k/출력 ~3.2k, judge 호출당 입력 ~12k/출력 ~0.8k):
  생성 3후보 × 40 ≈ $1.2, judge 5종 × 120 ≈ $7, 합계 **$10 미만**. 실제 비용은 러너가 호출별로 기록한다.

## 결과와 한계
- **n=40은 작다.** 20%p 미만 차이는 구별하지 못하고, 그런 경우 비용이 결정한다. 이것은
  "모델 A가 B보다 낫다"는 주장을 만들어 내기 위한 평가가 아니라, 명백히 나쁜 후보를 거르고 나머지는
  싸게 고르는 평가다.
- **라벨러 1명.** inter-rater 신뢰도는 측정할 수 없고 intra-rater만 잰다. 사용자의 판단 기준이
  곧 "발행 가능"의 정의다.
- **단일 패스 평가.** 운영에서는 게이트가 재생성하므로 실제 발행물의 사실성은 여기서 잰 것보다 높을
  것이다. bake-off는 모델 고유의 차이를, 비용 공식은 게이트의 추가 비용을 근사한다(실패의 독립 가정).
- **Haiku 4.5는 곧 은퇴할 수 있다.** judge로 뽑혀도 운영 judge로는 은퇴 일정과 네이티브 어댑터
  필요성을 다시 검토해야 한다.
- 결정론적 검사기의 정밀도/재현율은 이 bake-off의 사람 사실 오류 라벨로 처음 측정된다. 그 전까지
  G1은 모든 후보에 같은 방식으로 적용된다는 점에서만 공정하다.
- 실제 실행에는 사용자 조치가 필요하다: Gemini 선불 크레딧(현재 402), `UPSTAGE_API_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.

## Addendum
규칙을 바꿀 때 날짜, 사유, 그 시점에 결과를 열람했는지 여부를 여기에 추가한다.

### A1 (2026-09-26) — judge 후보 1개 추가. 결과 열람: 해당 없음(bake-off 미실행, 결과 파일 없음)
- 사유: 이 ADR을 쓴 뒤 main에 병합된 PR #6(ADR 0005 부록)이 실제 호출 결과로 judge 기본값을
  `gemini-3.5-flash` → `gemini-3.1-flash-lite`로 바꿨다(3.5-flash는 30초 타임아웃과 연속 503).
  "컨텍스트"의 "현재 기본값" 서술은 그 전 상태다.
- 변경: judge 후보에 `gemini-3.1-flash-lite-v2`를 추가한다. 운영 기본 judge라 기준선으로
  빠질 수 없다. `gemini-3.5-flash-v2`는 남겨 두고, pre-flight에서 타임아웃/503이 설정으로
  풀리지 않으면 기존 pre-flight 규칙대로 본 실행 전에 빼고 여기에 기록한다.
- 가격: `gemini-3.1-flash-lite` 입력 $0.25 / 출력 $1.50 (1M 토큰, paid standard, 텍스트 입력.
  https://ai.google.dev/gemini-api/docs/pricing, WebFetch 접근일 2026-09-26, 문서 "Last updated
  2026-09-24 UTC"). `llm_pricing.yaml`에 추가했다.
- 생성 후보·게이트·임계값·승자 결정·표본 크기는 바꾸지 않았다. 실행 전 변경이라 새 id 파일을
  만들지 않고 `bakeoff-v1.yaml`의 `amendments`에 같은 내용을 남겼다(git 이력으로 원문 대조 가능).
