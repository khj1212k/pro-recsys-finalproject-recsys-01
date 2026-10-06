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
- 추천 쪽에서 팀이 보고한 MRR 0.897이 추론 시점 누수로 부풀려졌던 전례(추천 오프라인 평가 재검증,
  `eval/team-baseline-repro-v1` 브랜치의 평가 프로토콜 ADR — 이 ADR보다 나중에 병합된다)가
  있다. 같은 실수를 LLM 쪽에서 반복하지 않으려면 **결과를 보기 전에** 지표·임계값·
  동점 처리·표본 크기의 한계를 고정해야 한다. 결과를 보고 나서 기준을 고르면
  (garden of forking paths) 어떤 후보든 이기게 만들 수 있다.
- 이 프로젝트의 라벨러는 사실상 1명(사용자)이다. 라벨 예산이 표본 크기를 결정한다.

### 검증한 사실 (공식 문서 조회, 접근일 2026-09-25)
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
- 후보당 클러스터 n=40, 후보 3개 → 발행 판정 라벨 120개 + 클러스터 라벨 40개 + 재라벨 40개
  (A5 이후 클러스터 라벨 48개, 합계 약 208개). 라벨 1개에 3~5분이면 **10~17시간**이다(A6 정정).
  그 이상은 1인 라벨러에게 비현실적이다.
- **n=40으로 검출 가능한 효과 크기(α=0.05 양측, 검정력 0.8)** — 아래 "증거"의 계산:
  - 짝지은 설계(같은 40개 클러스터를 모든 후보가 생성, McNemar 근사): 불일치 쌍 비율이
    20% / 30% / 40%일 때 최소 검출 차이 **19.3 / 23.6 / 27.2 %p**.
  - 짝짓지 않은 두 비율 비교(보수적 상한): 기준 발행률 0.5 / 0.6 / 0.7에서 증가 방향 29.5 / 27.2 / 23.8 %p,
    감소 방향 29.5 / 30.7 / 30.7 %p — 나쁜 쪽을 기준으로 **약 30%p**(A6 정정: 처음에는 증가 방향만 적었다).
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
  **→ A5에서 변경**: 어려운 사례 8개는 n=40과 별도로 뽑고, 생성하지 않는다(클러스터 라벨·ROC 전용).
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
- **사람 발행 가능률**: 후보별 40개 출력 중 `publishable=Y` 비율(전체 40개 기준, 가중치 없는 표본
  비율 — A5). 2차로
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
  out-of-fold κ가 가장 높은 judge를 고른다. **→ A4**: κ는 judge 자신의 계열이 만든 출력을 빼고 잰다.
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
  - 두 비율 z-검정 근사(후보당 n=40): 증가 방향 p0=0.5 → 0.295, p0=0.6 → 0.272, p0=0.7 → 0.238,
    감소 방향 p0=0.5 → 0.295, p0=0.6 → 0.307, p0=0.7 → 0.307 (A6 정정).
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
  https://ai.google.dev/gemini-api/docs/pricing, 접근일 2026-09-26, 문서 "Last updated
  2026-09-24 UTC"). `llm_pricing.yaml`에 추가했다.
- 생성 후보·게이트·임계값·승자 결정·표본 크기는 바꾸지 않았다. 실행 전 변경이라 새 id 파일을
  만들지 않고 `bakeoff-v1.yaml`의 `amendments`에 같은 내용을 남겼다(git 이력으로 원문 대조 가능).

### A2 (2026-09-26) — 분석 정의 명확화 3건. 결과 열람: 해당 없음(bake-off 미실행)
분석 스크립트(`evaluation/llm/bakeoff_analysis.py`)를 쓰면서 본문이 정하지 않은 경우가 드러나
결과를 보기 전에 정해 둔다. 규칙의 임계값·표본 크기는 바꾸지 않는다.
1. **재라벨 κ가 정의되지 않을 때**(재라벨한 출력의 `publishable`이 1·2차 모두 한 범주뿐): "κ < 0.60"이
   관찰된 것이 아니므로 대체 규칙(게이트 통과 후보 중 최저 비용)을 발동하지 않는다. 신뢰도는
   "측정 불가"로 보고하고 결정은 잠정(provisional)으로 표시한다. 재라벨을 아직 안 했을 때도 같다.
2. **자기선호 DiD의 점수 정의**: 사람 점수 = `publishable`(0/1), judge 점수 = v2 기준 4개의 평균.
   v1 judge는 기준별 점수가 없어 DiD에서 뺀다. 부트스트랩 횟수·seed는 우열 비교와 같은 값(10,000,
   20260925)을 쓴다. 기준 judge는 어느 생성 후보와도 다른 계열인 v2 judge(= Haiku 4.5 v2)다.
3. **무엇을 라벨하나**: `publishable`·`style`·사실 오류 구간·핵심 사실 커버리지는 **형식체 초안**
   기준이고, `tone_drift`만 문체 변환본 기준이다(`docs/eval/labeling-guide.md`). judge v2와 결정론적
   검사기가 형식체 초안을 보므로 보정 대상과 라벨 대상이 같아야 κ가 의미가 있다. 실제 발행되는 캐주얼본의
   사실 보존은 G2(자동)와 `tone_drift`(사람)로 따로 본다.

### A3 (2026-09-26) — ClusterEvaluator ROC에서 판정이 아닌 행 제외. 결과 열람: 해당 없음(bake-off 미실행)
- 사유(리뷰 지적): `ClusterEvaluator`는 호출이 실패하면 FAIL·confidence 0.0을, JSON 파싱에 실패해
  텍스트 휴리스틱을 쓰면 FAIL·0.1을 돌려준다. 점수 `s = 1 − confidence`(FAIL일 때)로는 각각 1.0·0.9가
  되어 "가장 확신한 단일 사건"으로 정렬된다. 5xx·타임아웃·스키마 소진은 인프라 실패(실행 중단)가
  아니라서 `cluster_evals.jsonl`에 그대로 남는다. 합성 확인(`tests/evaluation/test_calibration.py`의
  `TestClusterConfidenceWithFailedCalls` 데이터): 완벽히 분리되는 판정 32개에 이런 행 6개(실제로는
  단일 사건 아님)를 섞으면 AUC가 1.0에서 0.727로 떨어져 사전 등록 기준(AUC ≥ 0.75)을 밑돈다
  (리뷰어의 별도 합성 예시에서는 0.667).
- 변경: 러너가 행마다 `parsed`(응답이 스키마로 파싱됐는지)를 기록하고, 분석은 `parsed=False` 행을
  AUC·기준선 balanced accuracy·OOF 임계값 계산에서 **모두 뺀 뒤 개수(`n_excluded_unparsed`)를 따로
  보고**한다. 근거: 이 행들은 판정이 아니고, 운영에서는 기존 PASS/FAIL 규칙이든 confidence 게이트든
  똑같이 FAIL(스킵)이 되므로 두 규칙을 비교하는 데 정보가 없다. 제외 비율 자체는 evaluator 신뢰성
  지표로 결과에 함께 적는다.
- 임계값(AUC ≥ 0.75, OOF balanced accuracy 향상 ≥ 0.05)과 폴드·seed는 바꾸지 않았다.

### A4 (2026-09-26) — judge 일치도(OOF κ)는 교차 계열 출력으로만 잰다. 결과 열람: 해당 없음(bake-off 미실행)
- 사유(리뷰 지적): `judge_metrics`가 judge마다 세 후보의 출력 전부로 κ를 쟀다. 그러면 judge 자신의
  계열 생성기가 만든 출력도 섞이는데, 운영에서 선정된 judge는 **승자(다른 계열)의 출력만** 채점한다.
  같은 계열 출력에서의 자기선호가 "같은 계열 judge 금지" 정책을 우회해 선정 지표에 새어 들어간다.
- 변경: `require_cross_family: true`이면 judge의 κ·Spearman·주장 정밀도를 `generator_family ≠
  judge_family`인 출력만으로 계산하고, 뺀 개수(`n_excluded_same_family`)를 함께 보고한다. 승자 출력만
  (n=40)으로 재는 대안은 κ의 95% CI 반폭이 약 ±0.29라 선정 지표로 쓰기에 너무 흔들려 택하지 않았다.
  자기선호의 크기 자체는 기존대로 DiD가 모든 출력으로 잰다.
- 계열 비교는 분석기에서도 러너와 같은 `core.llm.registry._model_family`로 정규화한다(지금은 항등
  함수라 결과는 같다).

### A5 (2026-09-26) — 어려운 사례는 생성하지 않고 n=40과 별도로 뽑는다, 비율은 가중치 없음. 결과 열람: 해당 없음(bake-off 미실행)
- 사유(리뷰 지적): 어려운 사례 8개(= 운영에서 `ClusterEvaluator`가 떨어뜨려 **뉴스레터를 만들지 않는**
  클러스터)가 생성 대상에 들어가 1차 지표(발행률)의 20%를 차지했다. 운영 분포 밖의 항목에 출력 라벨
  예산의 20%와 검정력을 쓰는 셈이고, 이 항목들이 필요한 이유(ROC의 음성 표본)는 클러스터 라벨과
  cluster-eval만으로 충족된다.
- 검토한 대안: (a) 생성은 하되 1차 지표에서 빼고 2차로 보고 — 출력 라벨 24개를 쓰고도 1차 지표의 n이
  32로 준다. (b) n=40 안에서 생성만 건너뜀 — 1차 지표의 n이 32로 준다(짝지은 설계 최소 검출 차이가
  ψ=0.2에서 19.3%p → 21.3%p, scipy 재계산). (c) **채택**: 일반 클러스터 n=40은 그대로 두고 어려운 사례
  round(40 × 0.2) = 8개를 **별도로** 뽑아 생성하지 않는다. 1차 지표의 n과 위의 검출력 계산이 그대로
  유지되고, 추가 비용은 클러스터 라벨 8개(약 30~40분)뿐이다.
- 구현: `evalset.stratified_sample`이 일반 n개 + 어려운 사례 round(n × hard_fraction)개를 뽑고,
  러너의 `generate`는 `hard_case` 항목을 건너뛰며, `export-blind`는 평가셋의 **모든** 항목에 클러스터
  라벨 과제를 만든다. `cluster-eval`은 전 항목(48개)에 돈다.
- ROC 해석 주의: 어려운 사례는 evaluator 자신의 과거 FAIL로 골라 음성 쪽이 농축된 표본이다. AUC는
  유병률에 둔감하지만 balanced accuracy·임계값은 "농축 표본 기준"임을 결과에 함께 적는다.
- **모든 비율은 가중치 없는 표본 비율이다.** 크기 버킷을 균등 배분했으므로(10+ 클러스터가 운영보다
  많다) 운영 발행률의 추정치가 아니라 후보 간 비교용이다. 매니페스트의 `sampling_weight`는 기록만 하고
  결정 규칙에는 쓰지 않는다(재가중 추정은 필요하면 2차 보고로만).

### A6 (2026-09-26) — 본문 수치 정정(규칙 변경 아님). 결과 열람: 해당 없음(bake-off 미실행)
- 짝짓지 않은 두 비율의 최소 검출 차이를 증가 방향으로만 계산해 "보수적 상한"이라고 적었다. 감소
  방향은 p0=0.6·0.7에서 30.7%p로 더 크다(scipy로 재계산, 리뷰어 계산과 일치). 본문을 양방향 값으로 고쳤다.
- 라벨링 시간을 "10시간 안팎"이라고 적었지만 약 200개 × 3~5분은 10~17시간이다(A5 이후 208개 기준
  10.4~17.3시간). 본문을 고쳤다.
- 추천 평가 프로토콜 ADR은 아직 main에 없다(별도 브랜치 `eval/team-baseline-repro-v1`). 본문의 참조를 "예정"으로 고쳤다 —
  그 ADR이 먼저 병합되어야 번호 참조 규칙(참조하는 ADR은 파일로 존재)을 만족한다. (A7에서 번호 참조를 뺐다.)

### A7 (2026-09-26) — 병합 순서에 맞춘 참조 정리(규칙 변경 아님). 결과 열람: 해당 없음(bake-off 미실행)
- 병합 순서가 런타임 → 이 브랜치 → 추천 평가 재검증(`eval/team-baseline-repro-v1`)으로 정해져, 이 ADR이
  병합되는 시점에 추천 평가 프로토콜 ADR 파일이 아직 없다. 번호 참조 규칙을 지키려고 본문과 A6에서 그 ADR의
  **번호**를 빼고 브랜치 이름으로만 가리키게 고쳤다. 사전 등록한 지표·임계값·표본 크기는 바뀌지 않았다.

### A8 (2026-10-06) — E0 기준선 측정 사전 등록 (bake-off 규칙 변경 아님). 결과 열람: 해당 없음(생성 파이프라인은 2026-02 이후 완주 기록이 없고, `reports/llm/`은 없다)

**무엇을 등록하나.** `main`의 뉴스레터 생성 파이프라인(LangGraph 그래프 `ai_workspace/workflow/graph.py` 전체 — 클러스터 평가·생성·결정론 게이트·judge v2 shadow·
문체 변환·드리프트 게이트·저장)을 **지금 코드 그대로** 실제 수집 기사에 돌려 "전(before)" 숫자를 재는 **기술 통계 측정**이다. 가설·결정 규칙은
없다. 뒤의 모든 LLM 실험(정합 패치 M1, 프롬프트 v2·문체 병합 E4', 아키텍처 A/B E2)은 이 숫자와 비교된다. 설계 문서(`docs/design/2026-09-26-llm-generation-v2.md`
4.3절 E0)는 "main+M1 뒤"에 E0을 두었지만, 여기서는 **M1 앞**에 둔다 — 그래야 M1의 효과를 전/후로 보일 수 있다. 실행 순서·데이터 경로의 근거는
[실행 계획](../design/2026-10-06-llm-v2-execution-plan.md)에 있고, 효력이 있는 것은 이 Addendum과 `evaluation/llm/preregistration/e0-baseline-v1.yaml`이다.
bake-off v1의 규칙(본문·A1~A7)은 바꾸지 않는다. **E0 결과를 열람한 뒤에도 bake-off v1의 게이트 임계값·표본 크기·승자 규칙을 고치지 않는다**; 고치면
"결과 열람 후 변경"으로 표기한다. 반면 아키텍처 A/B 사전 등록(다음 Addendum, 설계 M4)은 아직 실행 전이므로 E0 수치를 검정력 표의 입력(기준 차단율,
뉴스레터당 문장 수, 비용)으로 **쓰는 것이 목적**이다.

**A8.1 표본 — 어떤 클러스터를 어디서 고르나**
- 프레임: Tier 0 VM `news_raw`의 **동결 반출본**(실행 계획 2.3·2.4: 읽기 전용 `COPY`, 반출 시각 T0, `raw_news_extract_status='ok'`·본문 비어 있지 않음). 반출 매니페스트의
  sha256(행 수·기간·행별 sha256의 sha256)과 임베딩 파일(`.npy`) sha256을 **실행 전에 A8.1 기록 항목으로 적는다**(아래 "실행 전 기록").
- 임베딩: 운영 코드 `core.embedder.NewsEmbedder`(BGE-M3, `EMBEDDING_MAX_LENGTH=8192`, 텍스트 규칙 `f"{title} {content}"[:8000]`, 길이순 배치)를 Colab T4(cuda, fp16 — `embedder.py`의 cuda 기본값)로.
  fp16/fp32 대조 100건의 코사인 p10·최솟값을 함께 적는다. 임베딩 코드는 바꾸지 않는다(ADR 0006 사전 등록 규칙).
- 클러스터링: 운영 코드 `NewsClusterer.cluster_news()` 그대로(`HDBSCAN_MIN_CLUSTER_SIZE=3`, `HDBSCAN_MIN_SAMPLES=2`, `split_v2`, `CLUSTER_LOOKBACK_HOURS=24`), 저장소만 파일
  구현. 의사 시각 t_k = T0 직전 완결된 KST 날짜 D₁<D₂<D₃ 각각의 **06:00 KST**(설계 3.2의 일일 생성 시각; UTC로 변환해 `crawled_at`과 비교) 세 번 돌려 run k=1,2,3의 멤버십 스냅숏(ids만)을 남긴다.
  파일 저장소에는 뉴스레터가 없으므로 `news_letter_id IS NULL` 제외는 세 run에 걸쳐 적용되지 않는다(운영과의 차이로 기록).
- 후보 = 세 run의 클러스터 중 크기 ≥3(`min_size=3`, split_v2 조각 제외). 카테고리는 전부 `미분류`(뉴스레터가 없다), `split_v2`는 `cluster_meta`에서.
- 추출: `evaluation.llm.evalset.stratified_sample(candidates, n=15, seed=20261006, hard_fraction=0.0, warmup=2, min_size=3)` — 크기 버킷(3–4 / 5–9 / 10+) 균등 배분 5·5·5, 버킷 안은
  (`미분류`, split_v2) 비례, 기사 중복 클러스터 제외(`used` 규칙). `hard_fraction=0`인 이유: 운영 FAIL 기록이 없어 어려운 사례를 정의할 수 없다.
  **eval 15 + warmup 2.** warmup 2개는 설정 오류 확인용 스모크로 먼저 돌리고(결과는 기록하되 E0 집계에서 제외), 설정 오류가 있으면 고친 뒤 eval을 시작한다 —
  모델 품질에 속하는 실패(스키마 소진·length·content_filter·폴백)는 설정 오류가 아니다.
- 후보가 15개 미만이면 하루씩 앞으로 늘려(D₀, D₋₁; 최대 5일) 다시 뽑는다. 5일로도 9개 미만이면 **void(프레임 부족)**로 기록하고 반출을 다시 한다.
- 매니페스트(`evaluation/llm/evalsets/e0-baseline-v1.jsonl`)에는 기사 id·URL·언론사·본문/제목 sha256·길이만 커밋한다(ADR 0023). 본문은 `data/`(gitignore).
- E0 클러스터의 기사 id는 뒤의 평가셋(bake-off·A/B)에서 **중복 제외** 대상이다.

**A8.2 파이프라인 구성 — 실행 전에 고정**
- 코드: `origin/main`의 한 커밋 SHA(E0 러너 PR의 병합 커밋). 러너는 `git rev-parse HEAD`가 등록 SHA와 같고 `git status --porcelain`이 비어 있을 때만 시작한다. SHA는 실행 전
  "실행 전 기록"에 적는다(이 Addendum을 쓰는 지금은 러너가 없어 적을 수 없다).
- 설정: 그 SHA의 `ai_workspace/config/settings.py` 기본값 전부. 특히 `FAITHFULNESS_GATE_MODE=enforce`, `FAITHFULNESS_BLOCKING_TYPES=numbers,quotes`, `TONE_DRIFT_GATE_MODE=enforce`,
  `TONE_DRIFT_BLOCKING_TYPES=numbers,dates,entities_added`, `MAX_RETRY_TONE_DRIFT=1`, `JUDGE_GATE_MODE=shadow`, `JUDGE_MIN_CRITERION_SCORE=3`, `JUDGE_MAX_UNSUPPORTED_CLAIMS=0`,
  `MAX_RETRY_CLUSTER_EVAL=2`, `MAX_RETRY_NEWSLETTER_EVAL=3`, `MAX_RETRY_TONE_VALIDATION=2`, `MAX_LLM_CALL_RETRIES=10`, `LLM_REQUEST_TIMEOUT_S=60`, `LLM_CALL_DEADLINE_S=180`,
  `NEWSLETTER_WORKERS=3`. 모델·프로바이더는 레지스트리 기본값(generator/tone `gemini-3.5-flash-lite`, judge `gemini-3.1-flash-lite`), 온도·`max_tokens`는 코드 값
  (클러스터 평가 0.1/2048, 본문 0.2/8192, 메타 0.2/1024, judge 0.1/2048, 문체 0.4/4096). `reasoning_effort`는 **설정하지 않는다**(프로바이더 기본값; 코드에 없다).
  러너는 시작 시 이 값들을 읽어 yaml과 다르면 시작하지 않고, 실제 값을 run manifest에 적는다. 허용되는 env 차이는 DB 대신 파일 저장소를 가리키는 것과 E0 계측뿐이다.
- 운영과 다른 점(선언): (1) 저장소가 Postgres가 아니라 동결 파일 저장소. (2) `embed_newsletter_node`의 임베더는 벡터 대신 형식체 텍스트 sha256을 기록하고 `None`을 돌려 준다
  (Mac 환경에 FlagEmbedding이 없다; 이 벡터를 읽는 하류 노드는 없다). 저장본 벡터는 뒤에 Colab에서 같은 모델로 계산한다. 뉴스레터당 지연에 임베딩 시간이 빠진다.
  (3) 계측: `RecordingClient`(bake-off 러너와 같은 것)를 역할별 `get_client` 자리에, 문체 변환기에 로컬 폴백 플래그 서브클래스를 주입한다. 프롬프트·온도·`max_tokens`·재시도 로직은
  건드리지 않는다. 사용자 결정으로 (2) 대신 생성 전체를 Colab CPU에서 실제 임베더로 돌릴 수 있고, 그 경우 "실행 전 기록"에 적는다(지표 정의는 같다).

**A8.3 지표 — 정의**
모든 비율에 Wilson 95% CI를 붙인다. 분모를 매번 적는다. 가설 검정은 없다.
1. **완주 분포**: 클러스터별 최종 상태 ∈ {completed, skipped(클러스터 평가 FAIL), failed(`failure_reason` ∈ faithfulness / generator_fallback / judge_unavailable / judge), error(예외)}와 건수.
   완주율 = completed / 15.
2. **첫 시도 스키마 통과율**(bake-off G3 정의 재사용): 모델 응답을 한 번이라도 받은 구조화 호출(`parsed` 또는 `schema_failures>0` 또는 error ∈ {length, content_filter}) 중
   `parsed`이고 `schema_failures==0`인 비율. 전체와 purpose별(cluster_eval / newsletter_content_gen / newsletter_meta_gen / newsletter_eval / tone_convert).
3. **재시도**: `complete()`당 `attempts` 분포, `schema_failures` 합, 전송 실패(429 / 5xx / timeout·connection / deadline) 건수(메트릭 수집기의 실패 레코드와 `http_status`에서),
   **429 비율** = 429 응답 / 전체 HTTP 시도. 그래프 수준: 클러스터당 생성 시도 수(`newsletter_retry_count`), 클러스터 평가 재시도 수, 문체 재변환(드리프트) 수, 문체 검증 재시도 수(`convert()` 안 호출 수).
4. **지연**: 호출별 `latency_s` p50/p95(purpose별); 클러스터별 벽시계(init→END) p50/p95; 저장된 뉴스레터별 벽시계 p50/p95(재시도 포함)와 "마지막 성공 시도의 호출 지연 합"(단일 패스 근사, G4 비교용).
5. **토큰·비용**: 호출별 prompt/completion 토큰, 응답에 `total_tokens`가 있으면 `hidden = total − prompt − completion`(thinking 추정), `core.llm.pricing`으로 실행일 단가 비용. 클러스터별·저장본별 합계,
   purpose별 **문자/토큰 비율**(prompt 문자 수 / prompt 토큰). 전체 비용 USD와 청구 환율 기준 ₩.
6. **결정론 사실성 검사기**: 생성 시도마다 `blocking.numbers`·`blocking.quotes` 건수, `advisory.entities` 건수, `number_total`, 필드별(title/sentence/content) 분포. 비율: 첫 초안 중 미지원 수치 ≥1(G1 상당),
   미지원 인용 ≥1, 참고 개체명 ≥1; 최종 초안에서도 같은 비율; 차단으로 인한 재생성 건수.
7. **문체 드리프트**: 변환 시도 중 차단 드리프트(numbers / dates / entities_added) 비율, 형식체 폴백 저장 비율, 로컬 폴백(`_fallback_convert`) 사용 비율. 추가로 저장된 캐주얼 `sentence`를
   `check_tone_drift`에 **오프라인으로** 넣어 차단됐을 건수를 센다(`TONE_FIELDS`가 sentence를 빼는 C4의 크기).
8. **judge v2 shadow**: 채점된 초안의 기준별 점수(faithfulness/coverage/coherence/style) 분포, `unsupported_claims` 수, 기본 임계값 기준 PASS/FAIL 비율, `judge_unavailable` 건수,
   `unsupported_claims`의 초안 내 정확 부분 문자열 위치 추적 성공률(C17).
9. **클러스터 평가기**: decision·confidence 분포(`parsed=False` 행은 따로 센다, A3), 이상치 수, `sub_groups` 수, 재시도 수, "두 번째 서브그룹 ≥3건이 버려진" 건수(C15).
10. **연결 정합**: 저장본마다 `|current_article_ids| − |current_articles|`(제외됐는데 연결된 기사 수, C2).
11. **규칙 위반(shadow, 오프라인·결정론)**: 저장 형식체 본문 길이(자)·문단 수·첫 어절 금지어·감정 자극어 수·제목 길이·sentence 길이·키워드 수와 `"뉴스"` 필러·카테고리 유효성. 비율만.
12. **입력 통계**: 클러스터별 기사 수·언론사 수, 본문 길이, 1,500자 절단 비율, 같은 언론사 제목 near-dup 쌍(rapidfuzz `ratio ≥ 85`) 수.
13. **실패 분류**(고정 목록): infra(401/402/403/404/kill_switch) · transport(429/5xx/timeout/deadline) · schema(검증 소진) · length · content_filter · generator_fallback · faithfulness_block ·
    judge_unavailable · judge_fail(shadow→통과) · cluster_eval_fail · save_error · exception(기타). 클러스터마다 1차 결과 하나 + 사건 건수.
사후에 더한 지표는 리포트에 "사후(post hoc)"로 표기한다.

**A8.4 호출마다 기록하는 것**(`data/experiments/e0/<run>/calls.jsonl`, 저장소 밖): run, cluster_id, run_k(날짜 창), graph_attempt, purpose, provider, model, prompt_sha12(messages JSON sha256 앞 12자),
prompt_chars, prompt_tokens, completion_tokens, total_tokens(없으면 null), hidden_tokens, cost_usd, latency_s, attempts, schema_failures, parsed, first_try_schema_pass, error, http_status, started_at.
**프롬프트·응답 텍스트는 기록하지 않는다.** 초안·저장본 텍스트는 같은 디렉터리의 `newsletters.jsonl`에만(라벨링 입력), 리포트에는 sha256과 길이만.

**A8.5 비용 상한·중단**
- 상한 **$1.00**(warmup 2 + eval 15 + 합성 입력 pre-flight 5호출 포함). 기대값 ≈$0.62(실행 계획 2.5의 산술: 클러스터당 기대 $0.035, 최악 $0.101).
- 러너는 호출 전에 `누적 비용 + 이 호출의 최악 비용(prompt 문자 × 1.5 토큰/자 × 입력 단가 + max_tokens × 출력 단가)`이 상한을 넘으면 보내지 않고 멈춘다(warmup `budget.py`의 규칙).
  이렇게 멈춘 실행은 **void가 아니라 "예산으로 중단된 부분 실행"**으로, 끝난 클러스터까지 집계해 보고한다.
- 인프라 실패(401/402/403/404, 킬 스위치, 레지스트리 키 없음)는 즉시 멈춘다. 처리(어느 결과든)가 끝난 eval 클러스터가 8개 미만이면 void, 이상이면 부분 실행.
- 벽시계 상한 2시간: 넘으면 새 클러스터를 시작하지 않는다(진행 중은 끝낸다).
- 429는 자동으로 멈추지 않고 비율만 잰다(E0의 측정 대상).
- pre-flight(합성 입력, 운영 경로 5개 스키마 각 1회)는 eval 전에 돌리고 결과 표(parsed·지연·토큰·hidden)를 리포트에 넣는다. 실제 기사 본문은 유료 티어로만 보낸다(설계 3.7).

**A8.6 void 실행**: (a) 코드 SHA 불일치 또는 더티 트리, (b) A8.2 설정 중 하나라도 다름, (c) 표본 매니페스트·반출본·임베딩 sha256이 "실행 전 기록"과 다름, (d) 처리 끝난 eval 클러스터 < 8(인프라 실패),
(e) 러너가 아닌 수단으로 호출을 섞음. void는 비용·사유와 함께 A8.x에 적고, 같은 표본으로 다시 돈다(프레임이 바뀌면 새 표본·새 기록). **숫자를 좋게 하려는 재실행은 없다** — E0은 유효 실행 1회다.
라벨은 void와 무관하게 유효 실행의 저장본에만 단다.

**A8.7 보고**: `reports/llm/e0_baseline_v1.json`(집계 + 클러스터별 행: id·sha·건수·비율만)과 `e0_baseline_v1.md`. 머리말: 실행 명령, 코드 SHA, 실행 일시, 반출 T0·날짜 창·임베딩 sha256,
n(eval/warmup/처리 완료), seed, 비용(USD, 청구 환율·₩), Colab CU, 라벨 시간 실측, "결과 열람 여부"(이 Addendum 등록 시점: 미열람), 선언된 이탈(A8.2). 기사·뉴스레터 **텍스트는 싣지 않는다**(ADR 0023,
`reports/README.md`). 부분 실행·void도 그대로 보고하고 `reports/README.md`에 행을 더한다. ADR 0010 "증거"에는 첫 실제 차단율을 날짜와 함께 덧붙인다(판정 변경 아님).

**A8.8 사람 라벨 부분집합**(`docs/eval/labeling-guide.md`의 정의 그대로, 가이드는 고치지 않는다)
- 대상: E0 유효 실행의 **저장본 전부(≤15)**와 그 클러스터(≤15). 저장본이 15개 미만이면 있는 만큼. 라벨러 1인, 밤 세션 ≤1h.
- 순서: 클러스터 라벨(`single_event`, `outlier_ids`, `key_facts` 3~6 + 근거 id)을 **출력을 보기 전에** 전부 → 출력 라벨(`fact_errors` 구간·유형, `key_facts_covered`, `style` 1~5, `publishable`,
  `tone_drift` — 저장된 캐주얼본 대 형식체 초안). 출력은 불투명 id·무작위 순서, 게이트·judge 결과는 숨긴다(bake-off `export-blind` 형식).
- 재라벨(intra-rater): 1차 뒤 **48시간 이상**, 출력 5건 + 클러스터 3개(`relabel.json`에 러너가 seed로 고른 것). κ는 보고만(n이 작아 판정에 쓰지 않는다).
- 시간: 클러스터 15 × 4분 = 1.0h, 출력 15 × 4분 = 1.0h, 재라벨 8 × 3.5분 ≈ 0.5h → **≈2.5h**(3밤). 마감 = 표본 중 가장 오래된 기사의 `crawled_at` + 30일(ADR 0023).
- 쓰임(기술 통계): 사람 발행 가능률(CI), 사실 오류 유형 분포, **결정론 검사기의 정밀도·재현율**(수치·인용 구간 대 사람 `fact_errors` — ADR 0010이 "측정하지 않은 것"으로 적은 첫 측정),
  judge v2 shadow와 사람 `publishable`의 κ(CI와 함께, n≤15라 선정에 쓰지 않음), 핵심 사실 커버리지. 이 라벨로 발동되는 결정 규칙은 없다.

**A8.9 바꾸지 않는 것**: 표본 추출 규칙·seed·n, 설정, 지표 정의, 상한·중단·void 규칙, 라벨 대상·시간. 바꾸려면 실행 전이면 A8.x로, 실행 뒤면 "결과 열람 후 변경"으로 적는다.
**실행 전 기록(A8.1 추가 예정, 결과 열람 전)**: 코드 SHA, 반출 T0·행 수·매니페스트 sha256, 임베딩 sha256·fp16/fp32 코사인, 세 run의 멤버십 스냅숏 sha256, 표본 매니페스트 sha256, 사용자 결정(임베더 이탈 방식, 라벨 축소 여부).
