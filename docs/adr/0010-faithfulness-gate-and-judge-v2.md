# ADR 0010: 결정론적 사실성 게이트, 문체 드리프트 게이트, judge v2

## 상태
채택됨 (2026-09-26). 차단 유형·judge 임계값·judge 모드는 **잠정값**이다. ADR 0009 bake-off의 사람
라벨로 보정한다. 실제 기사 본문이 딸린 생성물로 잰 차단율은 아직 **없다**(아래 "증거" 참고).

2026-10-18 Addendum(맨 끝): **차단 유형·judge 임계값·모드는 그대로**이고, 재시도 정책과 기록 방식 일부가 바뀌었다
(C2 저장 연결, C4 문체 드리프트 비교 필드, C10 축소판 재시도 금지 목록, 스키마 재시도 상한). 본문의 "결정"과
"증거"는 그 전의 기록이고, 달라진 곳은 Addendum의 표가 가리킨다.

## 컨텍스트
- main의 단일 클러스터 LangGraph 서브그래프는 클러스터 평가 → 생성 → judge v1 → 임베딩 → 문체 변환 →
  저장 순서였다. 품질 신호는 judge v1 하나였다.
  1. judge v1은 원문 **제목 최대 5개**만 본다. 원문에 없는 수치나 인용을 판정할 수 없다.
  2. **문체 변환 결과를 검사하지 않는다.** 사용자에게 보이는 것은 캐주얼 변환본인데, 변환 LLM이 수치를
     바꿔도 그대로 저장된다.
  3. 생성기와 judge가 같은 계열(Gemini)이고, PASS 기준 `score >= 5`는 보정된 적이 없다(ADR 0009).
  4. 클러스터를 순차 처리한다. 한 클러스터에서 LLM 호출(클러스터 평가, 본문·메타 생성, judge, 문체 변환)이
     줄줄이 이어지는 동안 CPU와 DB는 논다.
- 7월 작업으로 결정론적 사실 추출기(`evaluation/llm/faithfulness.py`)가 있었다. 수치, 날짜, 개체명,
  인용문을 뽑아 원문과 대조하는 `check_against_sources`와, 두 글을 비교하는 `compare_rewrite`를 제공한다.
  다만 평가용으로만 쓰였다.
- 실제 한국어 기사 수집은 이제 막 시작됐다(다른 작업). 결정을 내리는 시점에 **원문 본문과 생성물이 짝지어진
  데이터가 없다.** 팀 아카이브의 뉴스레터 401편에는 원문 기사가 딸려 있지 않다.

## 검토한 대안

### 1. 사실성 검사를 어디서, 무엇으로 할까
1. **judge(LLM)만 쓰기.** 호출마다 비용이 든다. 판정이 비결정적이다. 보정되지 않았고 생성기와 같은
   계열이다. judge가 틀리면 원인을 추적할 수 없다.
2. **NLI 모델(한국어 cross-encoder)로 문장 단위 함의 판정.** 모델 선정과 보정에 쓸 사람 라벨이 없다.
   M2 16GB에서 BGE-M3와 메모리를 나눠 써야 한다. 라벨이 생긴 뒤 다시 본다.
3. **생성 직후, judge보다 먼저 결정론적 검사기를 돌린다(채택).** 비용이 없고 재현 가능하다. "3조 원이
   원문에 없음"처럼 **고칠 수 있는 피드백**을 재생성 프롬프트에 넣을 수 있다. 규칙으로 확실히 잡히는
   오류에는 judge 호출 비용을 쓰지 않는다. 한계는 추출 가능한 사실만 본다는 점이다(아래 "결과와 한계").

### 2. 무엇을 막을까 (차단 유형)
- **수치와 인용문은 막는다.** 수치는 단위를 정규화한 뒤 값이 같은지 본다(1조5000억 = 1.5조, 1% 근사 허용).
  인용문은 원문과 퍼지 일치(partial_ratio ≥ 90)를 요구한다. 둘 다 "같다"의 정의가 분명해 오탐 원인을
  추적할 수 있다.
- **개체명은 참고만 한다**(피드백에 "(참고) 원문에서 찾지 못한 고유명사"로만 넣는다). 판정이 Kiwi 품사
  태깅과 퍼지 일치(85)에 기대는데, 정밀도를 잰 적이 없다. 실제로 이 ADR의 프로브에서 Kiwi가 문맥에
  따라 일반명사를 고유명사로 태깅하는 사례가 나왔다(증거 2). 정밀도는 사람 사실 오류 라벨로 잰 뒤에
  `FAITHFULNESS_BLOCKING_TYPES`로 켠다.
- **대조 대상은 클러스터 기사 전체 본문과 제목이다.** 생성기가 본 1500자 발췌보다 넓다. 발췌 밖의 값이
  우연히 일치해 통과하는 경우는 원문과 모순되지 않는다. 느슨한 쪽의 오류라 게이트 목적에 해가 없다.
- **계산값(합계)은 막힌다.** 게이트는 "원문에서 확인되는가"를 보지 "사실인가"를 보지 않는다. 피드백에
  "원문에 없는 수치를 계산해 만들지 마세요"를 넣는다. 사람 라벨 가이드는 정확한 계산값을 오류로 보지
  않는다(`docs/eval/labeling-guide.md` 2.1). 그래서 이 설계가 게이트 정밀도를 얼마나 깎는지는 bake-off
  라벨에서 드러난다.

### 3. 걸렸을 때 어떻게 할까
1. 문제 문장을 자동으로 지운다: 흐름이 깨질 수 있다. 수정된 글이 다시 검사되지 않는다.
2. **구체적인 피드백과 함께 재생성한다(채택).** judge FAIL과 같은 생성 상한
   (`MAX_RETRY_NEWSLETTER_EVAL`=3)을 공유한다. 상한을 다 쓰면 클러스터를 실패 처리한다
   (`failure_reason="faithfulness"`).
- 모드는 `enforce`(막고 재생성) / `shadow`(기록만) / `off` 세 가지다.

### 4. 문체 변환 드리프트
- 비교 대상은 **형식체 초안**이다. 초안은 이미 사실성 게이트를 통과했다. 변환이 바꾼 사실만 찾으면 된다.
- **집합 기준(multiset 아님)으로 비교한다.** 캐주얼 변환은 문장을 압축하므로 같은 사실을 더 적게 언급하는
  것은 정상이다.
- 차단하는 경우: 수치가 추가되거나 빠짐, 절대 날짜가 추가되거나 빠짐, 고유명사가 **추가**됨.
  고유명사가 빠지는 것은 수식어를 덜어낼 때 흔하므로 참고만 한다. 상대 날짜(작년/지난해)는 같은 뜻의
  다른 표현이 흔해 참고만 한다. **형식체 초안에 글자 그대로(퍼지 85) 있는 단어는 "추가"로 보지 않는다**
  (증거 2의 재태깅 오탐).
- 드리프트가 나면 피드백과 함께 `MAX_RETRY_TONE_DRIFT`(1)회 다시 변환한다. 그래도 남으면
  **형식체 초안을 저장한다.** 대안이었던 "클러스터 실패"는 검증된 초안을 문체 단계 때문에 버린다.
  "캐주얼본의 수치를 되돌려 끼우기"는 문자열 수술이라 택하지 않았다. 사실이 바뀐 친근한 문장보다
  딱딱하지만 정확한 문장이 낫다. 임베딩은 원래도 형식체 초안으로 만든다.

### 5. judge v2
- **본문을 본다.** 생성기와 같은 기사 선택(`select_prompt_articles`)과 같은 발췌 길이
  (`ARTICLE_PROMPT_CHARS`=1500)를 쓴다. judge가 생성기보다 적게 보면, 생성기가 제대로 옮긴 사실을
  근거 없음으로 오판한다.
- 출력은 기준별 점수(faithfulness/coverage/coherence/style, 1~5), 근거 없는 주장 목록(초안의 부분
  문자열 그대로), 피드백이다. **PASS/FAIL은 모델이 아니라 코드가 정한다**
  (`JUDGE_MIN_CRITERION_SCORE`=3, `JUDGE_MAX_UNSUPPORTED_CLAIMS`=0). 이 임계값은 ADR 0009의 2-fold
  선택으로 교체한다.
- 응답을 스키마로 해석하지 못하면 FAIL로 닫는다. v1은 응답 텍스트에 "PASS"라는 글자가 있으면 통과시키기도
  했다.
- v1은 `NewsletterEvaluatorV1`로 남긴다. bake-off에서 "본문을 보여 준 효과"를 재는 기준선이다.

#### judge 게이트 모드: 기본 `shadow` (리뷰 후 변경)
| 대안 | 판단 |
|---|---|
| v1 enforce 유지(main) | 본문을 못 보는 judge라 사실성 게이트로서 의미가 없다 |
| v2 enforce, 잠정 임계값 | ADR 0009는 사람 라벨 대비 OOF κ ≥ 0.40이 확인된 judge만 게이트로 쓰게 정했다. 아직 측정 전이다. 운영 기본 judge(`gemini-3.1-flash-lite`)는 생성기(`gemini-3.5-flash-lite`)와 같은 계열이다. v1(score ≥ 5)보다 엄격해서 **측정 없이 발행량만 줄 위험**이 있다 |
| **v2 shadow (채택)** | 채점한 결과는 모두 `generation_history`에 남아 나중에 보정 데이터가 된다. 채점된 FAIL은 발행을 막지 않는다 |

shadow의 정확한 의미는 다음과 같다. 리뷰에서 처음 구현의 fail-open이 드러나 고쳤다.
- **채점된** FAIL(기준별 점수가 있음)만 기록하고 통과시킨다.
- **점수가 없는 FAIL**(judge 호출 실패, 파싱 실패)은 enforce와 똑같이 재시도한다. 상한에 닿으면 실패로
  처리한다(`failure_reason="judge_unavailable"`). main에서도 judge 오류 시에는 발행하지 않았으므로,
  shadow가 main보다 느슨해지지 않는다.
- **생성기 로컬 폴백 초안**은 게이트 모드와 무관하게 judge 전에 막는다(`failure_reason="generator_fallback"`).
  생성 LLM이 실패하면 기사 제목을 불릿으로 나열한 초안이나 휴리스틱 제목이 나온다. 제목은 원문에 그대로
  있어서 사실성 검사를 통과한다. 처음 구현에서는 이것이 shadow judge를 지나 발행됐다(리뷰어가 재현했다).
- 대가: main(v1 enforce)보다 **문체와 커버리지 쪽 차단이 느슨하다.** `JUDGE_GATE_MODE=enforce`로 즉시
  되돌릴 수 있다.

### 6. 클러스터 병렬 처리
1. asyncio: LLM 클라이언트와 LangGraph 호출 경로를 전부 비동기로 바꿔야 한다. 범위를 넘는다.
2. 프로세스 풀: 프로세스마다 BGE-M3(~2GB)를 올려야 한다. 16GB Mac에서 불가능하다.
3. **스레드 풀(채택).** `NEWSLETTER_WORKERS` 기본 3, 1~4로 제한한다. 작업이 LLM 대기에 묶여 있어
   스레드로 충분하다. 상한 4의 이유는 두 가지다. dev DB 풀이 최대 5이고, 프로바이더 레이트리미터가
   프로바이더 단위로 공유되므로(`*_LLM_MIN_INTERVAL`) 스레드를 늘려도 429 백오프만 는다.
   - BGE-M3는 하나만 올린다(초기화 잠금과 호출 직렬화). Kiwi도 초기화와 호출을 잠근다.
   - `min_target` 보충은 부족분과 워커 수 중 작은 만큼만 한 번에 띄운다. 목표를 넘겨 LLM 비용을 쓰지 않는다.
   - 저장 실패 때도 DB 커넥션을 반납한다(리뷰 후 수정). 워커 4개가 풀 5개를 나눠 쓰므로 누수가 곧 고갈이다.
   - 재시도 설정으로 최장 경로(기본 노드 21회)를 계산해 `recursion_limit`을 준다(리뷰 후 수정).
     langgraph 버전을 고정하지 않았고 라이브러리 기본값이 25와 10007로 버전마다 달라서다.
- **실제 프로바이더로 잰 속도 향상은 없다.** 페이크 테스트는 동시성 상한, 보충 정지, 한 클러스터 실패의
  격리만 확인한다.

### 7. 사실성 모듈의 위치
- `evaluation/llm/faithfulness.py`를 `ai_workspace/core/faithfulness.py`로 옮겼다(`git mv`로 이력 보존).
  런타임 게이트가 import해야 한다. `evaluation/`은 설치 패키지(`ai_workspace/pyproject.toml`)에 들어
  있지 않다. 런타임이 평가 트리에 의존하면 의존 방향이 뒤집힌다. 평가 코드는 `core.faithfulness`를 쓴다.
  런타임 의존성에 `rapidfuzz`와 `kiwipiepy`를 명시했다.
- 대안이었던 "evaluation을 패키지로 설치"는 운영 이미지에 평가 코드와 그 의존성을 싣는다.

## 결정
단일 클러스터 그래프(`ai_workspace/workflow/graph.py`):

```
init → eval_cluster ─(FAIL: 이상치 제거/분할 재평가, 상한 2)
     → generate → check_faithfulness ─(차단: 피드백과 재생성 / 폴백 초안: 재생성)
     → eval_newsletter(judge v2) ─(enforce FAIL 또는 점수 없음: 재생성)   ※ 생성 상한 3 공유
     → embed(형식체) → convert_tone → check_tone_drift ─(드리프트: 재변환 1회, 지속 시 형식체 저장)
     → save
```

| 설정(환경변수) | 기본값 | 의미 |
|---|---|---|
| `FAITHFULNESS_GATE_MODE` | `enforce` | 사실성 게이트: enforce / shadow / off |
| `FAITHFULNESS_BLOCKING_TYPES` | `numbers,quotes` | 막는 유형. `entities`는 정밀도를 잰 뒤에 추가 |
| `TONE_DRIFT_GATE_MODE` | `enforce` | 문체 드리프트 게이트 |
| `TONE_DRIFT_BLOCKING_TYPES` | `numbers,dates,entities_added` | 막는 드리프트 유형 |
| `MAX_RETRY_TONE_DRIFT` | `1` | 드리프트 재변환 횟수. 소진하면 형식체 저장 |
| `JUDGE_GATE_MODE` | `shadow` | judge v2: enforce / shadow(채점된 FAIL만 기록 후 통과) |
| `JUDGE_MIN_CRITERION_SCORE` | `3` | 기준별 최저 점수(잠정, ADR 0009로 보정) |
| `JUDGE_MAX_UNSUPPORTED_CLAIMS` | `0` | 허용하는 근거 없는 주장 수(잠정) |
| `NEWSLETTER_WORKERS` | `3` | Stage5 클러스터 병렬 수(1~4로 제한) |

- 클러스터별 결과(`status`, `failure_reason` ∈ {faithfulness, generator_fallback, judge, judge_unavailable}
  (2026-10-18 Addendum 이후 `infra`, `length`, `content_filter`가 더해졌다),
  사실성 통과 여부, 형식체 폴백 여부)를 `cluster_history`의 `cluster_outcomes`에 남긴다. 게이트가 발행량을
  얼마나 줄이는지를 운영 데이터로 재기 위해서다. 평가셋 추출기도 여기서 "어려운 사례"를 고른다(ADR 0009).

## 증거

### 1. 그래프 수준 테스트 (페이크 LLM, `tests/test_workflow_faithfulness_gates.py`)
| 시나리오 | 확인한 것 |
|---|---|
| 원문에 없는 "3조 원" 생성 | 재생성 프롬프트에 "3조 원"이 들어가고, judge는 사실성 통과본만 1회 채점하고, 두 번째 초안이 저장된다 |
| 환각이 계속됨 | 생성 3회 후 저장 0건, `failure_reason="faithfulness"`, judge 미호출 |
| 사실성 shadow | 보고서만 남고 재생성은 없다 |
| 변환본이 "2조 원"을 "20조 원"으로 바꿈(2회) | 재변환 프롬프트에 드리프트 피드백이 들어가고, 형식체 초안이 저장된다 |
| 재변환에서 고쳐짐 | 캐주얼본 저장 |
| 생성기 장애(503) + judge shadow | 저장 0건, `generator_fallback`, judge 미호출 |
| 메타만 폴백 | 폴백 초안은 버리고 두 번째 시도본을 저장 |
| judge 오류 + shadow | 저장 0건, `judge_unavailable` |
| 채점된 judge FAIL + shadow | 기록 후 저장 |
| 최장 경로(노드 21회) | 계산한 `recursion_limit`으로 끝난다. 한도 21이면 실패, 22면 통과를 확인해 +1을 넣었다 |
| 라우팅 무한 루프 주입 | 재시도 예산 안에서 `GraphRecursionError`로 끊긴다(수정 전에는 수천 스텝을 돌았다) |

그 밖의 테스트: judge v2가 본문 발췌와 생성기와 같은 기사 부분집합을 보는지, 임계값 설정, 파싱 실패 시
FAIL(`tests/test_newsletter_judge_v2.py`). 동시성 상한, 보충 정지, 실패 격리, 결과 기록
(`tests/test_stage5_parallel_clusters.py`). 임베더 1회 로드와 추론 직렬화
(`tests/test_workflow_shared_embedder_threads.py`). 저장 실패 시 커넥션 반납
(`tests/test_save_newsletter_releases_connection.py`).

### 2. 게이트 오탐/검출 프로브 (`python -m evaluation.llm.gate_probe`)
**고정 사례 11개**는 모두 설계대로 동작했다. 원문 "1조5000억 원 / 1만2000명 / 20% / 2조 원 / 따옴표 인용"에 대해:
- 통과: 원문 그대로, `1조 5000억 원`(띄어쓰기), `1조5000억원`(단위 붙여쓰기), `1.5조 원`,
  `12,000명`, `20퍼센트`, 따옴표 없는 인용 요약.
- 차단: `3조 원`(값 변경), `1만2600명`(+5%, 근사 허용 1% 밖), `3조5000억 원`(계산한 합계), 지어낸 인용.

**실제 한국어 LLM 뉴스레터 변형**(팀 아카이브 `data/team_archive/newsletters/*.json`, 본문 해시로 중복을
제거한 401편, 수치 551개). 원문 기사가 없어서 **뉴스레터 자신을 원문으로 두고** 결정론적 변형을 가했다.
출력은 집계 숫자뿐이다.

| 측정 | 수정 전 | 수정 후 |
|---|---|---|
| 동치 재표기 수치 오탐(수치 기준) | 14 / 494 (2.8%) | **0 / 494** |
| 동치 재표기 후 차단된 뉴스레터 | 8 / 208 | 1 / 208 (수치가 든 **직접 인용문**을 프로브가 고친 경우. 인용은 원문 그대로여야 하므로 설계대로의 차단) |
| 수치 하나를 ×1.1로 바꿨을 때 검출 | 500 / 500 | **500 / 500** (바꾼 값이 이미 다른 곳에 있던 15건은 제외) |
| 결정론적 문체 폴백(어미 치환 + 📰)의 드리프트 오탐 | 11 / 401 (2.7%) | **0 / 401** |
| 폴백 변환본의 수치 하나 변경 시 드리프트 검출 | 200 / 200 | **200 / 200** |

프로브에서 찾은 오탐 두 가지는 이 ADR과 같은 브랜치에서 고쳤다.
1. `2.5%포인트`를 `%`로 읽었다. 그래서 같은 값을 `2.5%p`나 `2.5퍼센트포인트`로 옮기면 단위가 다르다고
   막혔다. 기사에서 흔한 표기라 단위표에 `%포인트`를 넣었다.
2. Kiwi가 문맥에 따라 같은 단어를 다르게 태깅했다. 예: "📰 " 바로 뒤의 일반명사가 NNP가 된다. 그래서
   초안에 글자 그대로 있는 단어가 "추가된 고유명사"로 잡혔다.

**이 프로브가 재지 않는 것**: 변형은 합성이다(표기 바꾸기와 ×1.1). 실제 LLM 환각의 분포, 예컨대 틀린 주체나
수치 없는 지어낸 주장은 재지 않는다. 그래서 "오탐 0"은 **표기 차이에 대한 강건성**이라는 뜻이다.
실제 초안에서의 정밀도라는 뜻이 아니다.

### 3. 측정하지 않은 것 (정직하게)
- **실제 초안의 차단율**: 원문 본문과 생성물이 짝지어진 데이터가 없다. 팀 아카이브에는 원문이 없고,
  Gemini 유료 호출은 최근까지 402였다. 첫 운영 실행의 `cluster_outcomes`와 bake-off의 G1(미지원 수치율)이
  첫 측정이 된다.
- **게이트 정밀도와 재현율 대 사람 라벨**, **judge v2의 κ**: ADR 0009 bake-off에서 잰다. 범위 종결 C-11 (2026-10-18): 유보 - 사람 라벨이 없고 bake-off는 유보다(ADR 0009 A8.13·A9).
- **Stage5 병렬화의 실제 속도 향상**, **main 대비 발행량 변화**: 재지 않았다.

### 4. 첫 실제 차단율 — E0 기준선 (2026-10-17, 판정 변경 아님)
[ADR 0009 A8](0009-llm-eval-protocol-and-preregistered-decision-rule.md)의 E0이 main 생성 경로를 코드 그대로 실제 수집 기사 15개 클러스터에 돌린
첫 측정이다(`enforce`, 차단 유형 `numbers,quotes`). 원자료는 [`reports/llm/e0_baseline_v1.json`](../../reports/llm/e0_baseline_v1.json). n이 작아
Wilson 95% CI가 넓다.
- 생성에 도달한 14개 클러스터의 첫 초안: 미지원 수치 ≥1 **0/14** [0.000, 0.215], 미지원 인용 ≥1 **0/14** [0.000, 0.215], 참고 개체명 ≥1 3/14.
  차단으로 인한 재생성 0, 폴백 초안 0. 저장본도 수치·인용 0/14.
- 문체 드리프트 게이트: numbers·dates·entities_added 차단 각 0/14, 형식체 폴백 저장 0/14, 로컬 폴백 0/14.
- judge v2 shadow: 기본 임계값 PASS 4/14(같은 Gemini 계열 judge).
위 "측정하지 않은 것"의 첫 항목(실제 초안의 차단율)이 이것으로 처음 채워졌다. 게이트의 정밀도·재현율은 여전히 재지 않았다 - E0의 사람
라벨(A8.8)이 끝나면 첫 초안에서 잰다. 이 수치로 바뀌는 결정은 없다.

**Addendum (2026-10-18).** 위 문장의 "E0의 사람 라벨(A8.8)이 끝나면"은 더 이어지지 않는다 - E0의 사람 라벨은 달지 않기로 했다(사용자 결정
2026-10-17, [ADR 0009](0009-llm-eval-protocol-and-preregistered-decision-rule.md) A8.13). 게이트의 정밀도·재현율은 **사람 정답 대비로는 측정하지
못했다.** 있는 것은 자동 라벨러(LLM)의 구간과 검사기 구간이 겹친 비율뿐이고(`reports/llm/e0_auto_labels_v1.md`; 검사기 차단 항목 0건, 라벨러가 단 number·quote
오류 0건이라 분모가 모두 0이다) 그 값은 정밀도·재현율이 아니다. 사람 라벨 대비 값은 bake-off 또는 새 라벨 결정이 있을 때까지 비어 있다 - 위 "측정하지 않은
것"의 "게이트 정밀도와 재현율 대 사람 라벨"은 그대로 비어 있다.

## 결과와 한계
- **결정론적 게이트가 보는 것은 수치와 인용문뿐이다.** 날짜는 원문과 대조하지 않는다. 추출기는 날짜를
  뽑지만 `check_against_sources`는 날짜를 비교하지 않는다. 그래서 "9월 25일"을 "9월 26일"로 쓴 환각은
  통과한다. 개체명은 참고만 한다. 수치 없는 지어낸 주장은 judge만 볼 수 있다. judge 기본값이 shadow라서
  **기본 설정에서는 수치와 인용 외의 환각을 막는 장치가 없다.** 보정 전 judge로 막는 것보다 정직한
  선택이라고 판단했지만, 공백이 있다는 사실은 분명히 적어 둔다. 다음 순서는 (1) 날짜 대조 추가,
  (2) bake-off 라벨로 개체명 정밀도를 잰 뒤 차단 여부 결정, (3) κ ≥ 0.40이면 judge를 enforce로 전환이다.
- 계산한 합계처럼 **사실이지만 원문에 글자로 없는 수치도 막는다.** 재생성 비용과 발행량 손실로 이어진다.
- 클러스터당 생성이 최대 3회로 늘 수 있다. judge 호출과 문체 재변환도 더해진다. bake-off 비용식이 게이트의
  재생성 기대 호출 수를 반영한다(ADR 0009).
- judge 장애는 shadow에서도 발행을 막는다. main과 같은 동작이다. 대가는 가용성이다.
- kiwipiepy가 없으면 개체명 검사만 경고와 함께 건너뛴다. 수치와 인용 검사는 그대로 한다.
- 잠정값(차단 유형, judge 임계값, 모드)은 사람 라벨이 생기면 바뀐다. 바뀔 때는 이 ADR에 Addendum을 남긴다.

## Addendum 2026-10-18: 정합 패치 M1 축소판 (C2·C4·C10 축소판) — 차단 유형 변경 없음, 재시도 정책 변경

E0 기준선 리포트([`reports/llm/e0_baseline_v1.md`](../../reports/llm/e0_baseline_v1.md))가 커밋된 뒤의 첫 LLM 경로 코드 변경이다
(`docs/design/2026-10-06-llm-v2-execution-plan.md` WP3). 사전 등록 파일과 E0 리포트는 고치지 않았다.

**바꾸지 않은 것.** `FAITHFULNESS_BLOCKING_TYPES`(`numbers,quotes`), `TONE_DRIFT_BLOCKING_TYPES`(`numbers,dates,entities_added`),
`JUDGE_GATE_MODE`(`shadow`), judge 임계값, 생성 상한 3, 클러스터 평가 재시도 2, 드리프트 재변환 1, 문체 검증 재시도 2,
전송 계층 재시도(429/5xx/타임아웃, `MAX_LLM_CALL_RETRIES`=10, deadline 180초), 지출 가드의 402 런 중단(ADR 0035), 프롬프트·온도·`max_tokens`.
문체 드리프트 게이트가 비교하는 **텍스트**에 sentence가 더해졌을 뿐(아래 C4) 막는 **유형**은 늘지 않았다.

### 1. 바뀐 것

| 항목 | 전 | 후 | 근거 테스트 |
|---|---|---|---|
| C2 저장 연결 | 저장 노드가 `current_article_ids`(클러스터 원본 전체)를 `news_letter_id`로 연결. 클러스터 평가가 뺀 이상치·버린 서브그룹까지 연결돼 다음 클러스터링의 `news_letter_id IS NULL` 필터에서 영구 제외되고 `raw_news_count`도 부풀었다 | 클러스터 평가를 거쳐 남은 기사(`current_articles`)의 id만 연결(중복 제거, 순서 유지). 생성기가 프롬프트에 실제로 넣은 목록은 아니다 — 아래 4절. `current_articles`가 비어 있는 입력만 원래 목록으로 폴백 | `tests/test_save_links_only_used_articles.py` — 고치기 전 코드로 이상치·서브그룹 두 시나리오가 `[10, 11, 12, 13]`으로 실패함을 확인(`exp/generation-warmup` eab2b89의 시나리오를 현재 judge v2 그래프로 이식) |
| C4 문체 드리프트 비교 필드 | `TONE_FIELDS = ("title", "content")`, 주석은 "sentence는 저장되지 않는다"(틀림: 저장 노드는 변환본의 요약을 sentence로 저장한다) | `("title", "sentence", "content")`, 주석 정정. 변환 출력의 `summary`만 있는 입력도 `sentence or summary`로 읽어 저장 노드와 같은 규칙을 쓴다 | `tests/test_tone_drift_covers_stored_fields.py` — 저장 필드 ⊆ `TONE_FIELDS` 드리프트 테스트, sentence만 2조 원 → 20조 원으로 바꾼 변환의 차단(단위·그래프 2건), 대조군(옛 필드로는 통과) |
| C10 축소판: 재시도 금지 목록 | 킬 스위치(env/외부 파일)·HTTP 401/402/403/404·`length`·`content_filter`로 호출이 막혀도 그래프가 같은 클러스터를 다시 생성했다 | `LLMResult.no_retry_reason`(`kill_switch`→`infra`, HTTP 401/402/403/404→`infra`, `length`, `content_filter`)이 있으면 재생성하지 않고 그 사유로 닫는다(클러스터 평가·본문/메타 생성·judge 호출. 문체 변환 호출이 막히면 검증 재시도만 멈추고 결정론적 폴백으로 저장되며 `failure_reason`은 없다). 아래 표 | `tests/test_llm_failure_injection.py` (장애 주입 33건) |
| 스키마 검증 실패 재시도 | 한 `complete()`가 `MAX_LLM_CALL_RETRIES`(10)까지 같은 요청을 다시 보내 응답마다 과금 | 재시도 2회(응답 최대 3개), `MAX_LLM_SCHEMA_RETRIES`(기본 2). 소진 오류는 `max retries (schema 2) exhausted: …`(E0 분류기가 읽는 `max retries` 접두를 유지). 전송 계층 재시도는 따로 센다 | `tests/test_llm_schema_retry_cap.py` |
| `ClusterEvaluator` 텍스트 휴리스틱 | 스키마로 해석 안 된 응답에 text가 있으면 낱말 "PASS"/"FAIL"로 판정(confidence 0.1) | 파싱 실패 = `FAIL`, `confidence=0.0`, `parsed=False`. 파싱된 판정은 `parsed=True`. 기본 레지스트리 어댑터는 parsed가 없으면 text도 없어 죽은 분기였고, text만 오는 레거시 어댑터에서만 살아 있었다 | `tests/test_evaluators_max_retries.py` |

재시도 금지 목록의 범위는 **`LLMResult.error ∈ {kill_switch, length, content_filter}` 또는 `http_status ∈ {401, 402, 403, 404}`** 로 고정한다.
HTTP 400(잘못된 인자)·422, 429·5xx·타임아웃의 재시도 소진, 스키마 재시도 소진은 목록에 없고 기존 정책(다음 생성에서 달라질 수 있는 실패)을 그대로 따른다.

### 2. 재시도·기록 정책의 전/후 (페이크 전송 계층의 요청 수로 센 값)

| 상황 | 전 | 후 |
|---|---|---|
| 클러스터 평가 호출이 처음부터 막힘 (401/403/404, 킬 스위치, `length`, `content_filter`) | FAIL로 읽혀 이상치·서브그룹이 없으면 `skipped`. 인프라 장애가 "군집이 나쁨"으로 기록 | `failed_clusters` + `failure_reason` = `infra`/`length`/`content_filter`, `skipped` 아님. 클러스터당 평가 요청 1회, 생성·문체 변환 요청 0 |
| 처음부터 402 | 지출 가드가 한 번에 런을 멈춘다(ADR 0035) | **동작 불변.** 실행 계획 WP3의 수용 기준 "처음부터 402: 클러스터당 호출 1회 뒤 `infra`로 기록"은 ADR 0035의 런 중단으로 대체됐다 — 운영 어댑터에서 402는 *결과*가 아니라 가드의 `LLMCircuitOpen`이라 첫 클러스터는 `stopped`(`llm_circuit_open`), 나머지는 `not_started`이고 요청은 런 전체에서 1회다. 이를 고정한 테스트는 고치기 전 코드(origin/main `dede670`)에서도 통과한다: 이번 변경을 증명하는 것이 아니라 가드 동작이 그대로임을 묶어 두는 회귀 잠금이다. 402를 *결과*로 돌려주는 클라이언트는 가드를 우회한 테스트 대역뿐이다(OpenAI 호환 어댑터는 가드가 먼저 예외를 내고 HyperCLOVA 어댑터는 `http_status`를 채우지 않는다). `no_retry_reason`의 402 분기는 그 대역에서만 실행되며 운영 경로가 없고, 그 대역은 401/403/404와 같이 `infra`로 닫는다 |
| 처리 도중 402 | 가드가 런을 멈춘다(동작 불변) | 동작 불변(위와 같이 고치기 전 코드에서도 통과하는 회귀 잠금). 메타 생성 요청이 402인 경우 요청은 judge 1 + 생성 2에서 끝나고 다음 클러스터 요청은 0임을 고정. 결과로 오는 402(가드 우회 테스트 대역)는 본문 생성 1회 뒤 종료(메타 호출 생략, judge·문체 변환 0) |
| 킬 스위치가 처리 도중 켜짐 | 폴백 초안 → 사실성 게이트 차단 → 생성 상한(3)까지 재생성(회당 본문+메타 2호출 시도) | 생성 1회 시도(전송 요청 0, 킬 스위치는 요청 전에 막는다) 뒤 `infra`로 종료 |
| 본문 생성이 `length` | 같은 `max_tokens`로 최대 3번 다시 생성 | 1번 시도 뒤 `failure_reason="length"`로 종료 |
| judge 호출이 막힘 (shadow·enforce 모두) | 점수 없는 FAIL → 생성 상한까지 재생성(생성 3회 + judge 3회) | 생성 1회 + judge 1회 뒤 `infra`로 종료 |
| 문체 변환 호출이 막힘 | 검증 재시도 포함 최대 3호출 뒤 결정론적 폴백 | 1호출 뒤 같은 결정론적 폴백(저장 동작 불변) |
| 전송 재시도 소진·스키마 재시도 소진 | 생성 상한까지 재생성 | 불변(회귀 테스트로 고정) |

`failure_reason`은 {`faithfulness`, `generator_fallback`, `judge`, `judge_unavailable`}에 {`infra`, `length`, `content_filter`}가 더해졌다.
생성 폴백 초안이 이 사유 때문이면 `check_faithfulness`의 보고서에 `llm_failure`가 실리고(`reason`은 `generator_fallback` 그대로),
라우터가 재생성 대신 `handle_max_retries`로 보낸다. 폴백 초안은 여전히 발행되지 않는다.

### 3. 고치기 전 동작이 E0 숫자에 준 영향

E0은 고치기 전 코드로 쟀다(`reports/llm/e0_baseline_v1.{json,md}`, `code_sha` 35d0a1d). 입력은 Tier 0이 수집한 실제 한국어 기사 15개 클러스터이고 사람 라벨은 없다 — 아래 E0 값은 모두 `[KR-ops]`이며
품질 판정이 아니다. 이 절은 그 JSON의 값과 이번 변경이 닿는 경로를 대조한 서술이다.
**변경 후 코드로 E0을 다시 돌려 확인하지 않았다.** E0 리포트의 숫자는 고치지 않는다.

- **C2**: 저장본 14개 중 **1개**(1/14 = 0.071, Wilson 95% [0.013, 0.315])가 평가에서 제외된 기사를 연결했고, 그 저장본에서 제외됐는데 연결된 기사는 최대 5개였다(리포트 10절).
  E0의 저장은 파일 대역이라 DB 연결 오염은 일어나지 않았다(선언된 이탈 1·7). 따라서 **E0의 완주율(14/15)·비용($0.155614)·지연·사실성·judge 지표는 C2의 영향이 없다.**
  운영 DB였다면 그 1건의 5개 기사가 다음 클러스터링에서 영구 제외되고 `raw_news_count`가 부풀었을 것이다. 이번 변경으로 그 연결이 사라진다.
- **C4**: 저장된 캐주얼 sentence를 형식체 sentence와 단독 비교하는 오프라인 게이트에서 **0/14**(Wilson 95% [0.000, 0.215], 리포트 7절)가 막혔을 것으로 나왔다. 그래서 E0의 문체 드리프트 숫자
  (수치·날짜·개체명 추가 차단 각 0/14, 형식체 폴백 저장 0/14)는 sentence를 넣어도 달라지지 않았을 것으로 읽는다. 다만 새 게이트(title+sentence+content 합집합 비교)로 같은 14개를 다시 돌린 값은 **측정하지 않았다**
  (저장본 텍스트는 저장소 밖 로컬 파일이고, 이 PR은 텍스트를 읽지 않는다).
- **C10과 재시도 정책**: E0에서 바뀐 정책이 건드리는 경로가 한 번도 실행되지 않았다 — 이 run이 보낸 HTTP 시도 87건이 모두 200(429 0, 5xx 0, 401~404 0), 킬 스위치 없음(시작 검사가 요구한다), 클러스터에 속한 72개 호출이 전부 첫 시도에 스키마를 통과했으므로 `length`·`content_filter`도 없었고(스키마 실패 합 0), 폴백 초안 0(리포트 2·3·6절).
  그래서 호출 수·비용·완주율·재시도 분포에 영향이 없다. E0의 `skipped` 1건(r1-c52)은 모델이 스키마를 지켜 답한 FAIL 판정(`cluster_eval_fail`, 판정 `parsed=False` 0건)이라 새 코드에서도 `skipped`다.
  `ClusterEvaluator` 텍스트 휴리스틱 경로도 0번 실행됐다.
- **C7**: E0은 `reasoning_effort`를 설정하지 않았고(사전 등록 `evaluation/llm/preregistration/e0-baseline-v1.yaml`의 `pipeline.reasoning_effort: unset`, ADR 0009 A8) 리포트의 `reasoning_tokens_reported`도 숨은 토큰(`total − prompt − completion`)도 0이다(리포트 5절). 기본값은 이번에도 미설정이라 E0의 요청은 바뀌지 않는다
  (미설정 요청에 `reasoning_effort` 키가 없음을 테스트로 고정). 상세는 ADR 0005 부록 2026-10-18.

### 4. 한계와 측정하지 않은 것

- 호출 수 절감은 **페이크 전송 계층의 요청 수**로 확인했다. 실제 장애에서 줄어드는 비용·시간은 측정하지 않았다(비과금 HTTP 오류가 많아 금액 효과는 작을 수 있다).
- `skipped`와 `failed`의 정의가 바뀌었다. 이전 `cluster_history`의 `skipped`에는 인프라 장애가 섞여 있을 수 있고, 과거 행은 고치지 않았다.
- **평가셋의 어려운 사례(`hard_case`).** 추출기(`evaluation/llm/evalset.py`)는 `status`가 아니라 `cluster_eval.decision == "FAIL"`만 봤으므로, `skipped`를 `failed`로 바꾸는 것만으로는 평가 호출이 막힌 클러스터가 어려운 사례에서 빠지지 않았다
  (킬 스위치를 켠 그래프의 요약을 추출기에 넣으면 `hard_case=[True]`). 이제 `summarize_cluster_outcome`이 `cluster_eval.llm_failure`(막힘)와 `parsed=False`(응답이 해석되지 않음)를 해당할 때만 요약에 남기고,
  추출기는 둘 중 하나라도 있는 FAIL을 판정이 아닌 것으로 보고 어려운 사례에서 뺀다(`tests/test_llm_failure_injection.py`, `tests/evaluation/test_evalset.py`; 정상 FAIL 판정은 그대로 어려운 사례). 두 키가 없는 이전 행은 예전대로 읽힌다.
  ADR 0009 A3가 ROC에서 파싱되지 않은 행을 빼는 것과 같은 기준이지만, A5의 "ClusterEvaluator FAIL" 정의를 판정이 있는 FAIL로 좁힌 것이기도 하다. 이 PR은 사전 등록 파일을 고치지 않는다 — 아래 bake-off 항목과 함께 amendment 후보다.
- **bake-off G2와 `q`도 sentence를 포함해 계산된다.** bake-off(`evaluation/llm/bakeoff.py`)의 G2 플래그(문체 드리프트율 ≤ 0.20)와 비용식의 `q`(ADR 0009)는 같은 `check_tone_drift`로 만들어지므로, 비교 필드에 sentence가 더해진 C4가 그대로 반영된다
  (초안 sentence와 변환본 summary/sentence의 수치·날짜·개체명 차이도 G2 차단). ADR 0009의 G2 정의는 비교 필드를 적지 않았고 사전 등록 당시 구현은 title+content였다. bake-off는 실행한 적이 없고(ADR 0009: bake-off 결과 없음, 커밋된 결과 파일 없음)
  결과를 본 적이 없으므로 오염된 결과는 없다. **`evaluation/llm/preregistration/bakeoff-v1.yaml`의 amendments에는 아직 기록하지 않았다** — bake-off 실행 전에 `results_viewed: false`로 기록해야 하고(파일 머리말 규칙),
  사전 등록 당시 정의를 유지하려면 `bakeoff.py`가 비교 필드를 명시적으로 넘기게 해야 한다(이번에는 하지 않았다). 다른 호출부(`e0_report.offline_sentence_drift`, `gate_probe.py`)는 값이 달라지지 않는다 —
  앞은 sentence를 title 자리에 넣어 비교하고, 뒤는 sentence가 없는 형식체 입력에 집합 비교를 쓴다.
- **C2의 연결 범위.** 생성기는 클러스터의 기사가 10개를 넘으면 본문이 긴 순 10개만 프롬프트에 넣는다(`MAX_PROMPT_ARTICLES`). 저장 노드는 `current_articles` 전체를 연결하므로 10개를 넘는 클러스터에서는 프롬프트에 없던 기사도
  `news_letter_id`가 채워지고 `raw_news_count`에 센다(지어낸 12기사 클러스터로 재현: 프롬프트의 기사 블록 10개, 저장 호출의 `article_ids` 12개). 실행 계획 WP3의 사양(`current_articles`의 id만 연결)과 코드는 같아서 바꾸지 않았다.
  바꾸려면 생성기가 쓴 기사 목록을 state로 돌려줘야 한다. E0 [KR-ops] 입력의 클러스터당 기사는 중앙값 6, 최댓값 41(15개 클러스터, `reports/llm/e0_baseline_v1.md` 12절)이라 10개를 넘는 입력이 있었다.
  저장본 중 몇 개가 그렇게 연결됐는지는 측정하지 않음.
- 전송 재시도 소진(429/5xx)과 HTTP 400은 여전히 막힌 실패로 보지 않는다. 스레드 사이 공유 서킷브레이커와 일일 캡(실행 계획 M2)은 없다: 401/403/404 결과는 지출 가드에서 중립이라 남은 클러스터마다 평가 요청이 1회씩 나간다(402만 런을 멈춘다).
- E0 하네스 쪽 두 가지: (1) 파일 대역의 계약 검사가 `save_newsletter_to_db` 본문 sha256을 고정하고 있어 새 값으로 갱신했다(`evaluation/llm/e0_store.py`; 바뀐 것은 저장에 넘기는 id 목록뿐이고 대역은 받은 id를 그대로 남기므로 대역이 하는 일은 같다).
  (2) `e0_report.py`의 근본 원인 분류는 새 `failure_reason` 값을 모른다(`exception`으로 떨어진다). E0 하네스를 다시 쓸 계획이 없어 고치지 않았다.
- 사람 라벨이 없으므로 "고친 뒤 품질이 낫다/같다"는 말하지 않는다. 이 변경은 정합(연결·비교 필드)과 장애 시 낭비 호출의 제거이고, 생성 품질을 건드리지 않는다.
