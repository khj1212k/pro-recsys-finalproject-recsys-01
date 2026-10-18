# ADR 0005: HyperCLOVA X 이후 - OpenAI 호환 어댑터 하나로 여러 LLM 프로바이더 통합

## 상태
채택됨 (2026-09-25)

## 컨텍스트
- 이 fork를 진행하는 사용자 계정에서 더 이상 HyperCLOVA X(Naver CLOVA Studio) API를 쓸 수 없다. 파이프라인의 기본 프로바이더였던 `core/llm_client.py::NaverHyperCLOVAClient`는 그대로는 동작하지 않는다.
- 대체 프로바이더 후보: Google Gemini(사용자의 Google Cloud 크레딧으로 유료 사용 가능), Upstage Solar Pro 3(한국어 특화), 그리고 challenger 한 곳(OpenAI 소형 모델 또는 Claude Haiku 4.5).
- LLM-as-judge 요건상 judge는 generator와 다른 모델 계열(provider)이어야 한다.
- 기존 코드(`core/llm_client.py`, `workflow/evaluators.py`, `core/reconstruction/generator.py`, `core/tone_converter.py`)는 LangGraph 노드가 호출될 때마다 새 클라이언트를 만들고, 프로바이더별로 다른 메서드 시그니처(`chat_completion` vs 없음)를 썼으며, 구조화 출력은 전부 "JSON을 프롬프트로 요구 + `extract_json_from_response`로 복구 + 수동 파싱"에 의존했다.
- 이 PR의 범위는 클라이언트 추상화 계층 자체이며, 최종적으로 어떤 프로바이더/모델 조합을 운영에 쓸지(bake-off)는 다루지 않는다 - 아래 "결과와 한계"에서 후속 ADR로 명시적으로 미룬다.

### 검증한 사실 (공식 문서 조회, 접근일 2026-09-25)
| 프로바이더 | OpenAI 호환 Chat Completions | `response_format: json_schema` 구조화 출력 | base_url | 출처 |
|---|---|---|---|---|
| OpenAI | 네이티브 | 지원 (`gpt-4o-mini`, `gpt-4o-mini-2024-07-18`, `gpt-4o-2024-08-06` 이상부터; Chat Completions/Responses/Assistants/Fine-tuning/Batch API 전체) | (OpenAI SDK 기본값) | https://developers.openai.com/api/docs/guides/structured-outputs (platform.openai.com/docs/guides/structured-outputs가 301로 리다이렉트되는 현재 정규 URL) |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | 지원 - `client.chat.completions.parse(model=..., response_format=PydanticModel)` 예시가 문서에 있음 (베타 명시: "OpenAI 라이브러리 지원은 아직 베타") | `https://generativelanguage.googleapis.com/v1beta/openai/` | https://ai.google.dev/gemini-api/docs/openai (문서 자체 "최종 업데이트: 2026-09-12") |
| Upstage | `https://api.upstage.ai/v1` | 지원 - `response_format={"type": "json_schema", "json_schema": {..., "strict": true}}`, OpenAI Structured Outputs의 부분집합(subset)을 그대로 따른다고 문서에 명시 | `https://api.upstage.ai/v1` | https://console.upstage.ai/docs/capabilities/generate/structured-outputs |

- 부가 확인: Upstage 모델 목록(https://console.upstage.ai/docs/models, 접근일 2026-09-25)에는 `Solar Pro 4`가 현재 flagship으로 소개되고 있지만 `Solar Pro 3`("Powerful MoE model with 102B parameters")도 여전히 사용 가능한 모델로 나열되어 있다 - 과제에서 지정한 `solar-pro3`는 여전히 유효한 선택지다.
- 로컬 환경 확인: 설치된 `openai` 파이썬 SDK(3.19.2)는 내부적으로 `httpx2`(pydantic/httpx 팀이 배포하는 httpx의 차세대 메이저 버전, PyPI 공개 패키지)를 쓴다(`openai/_base_client.py`에서 `import httpx2`). 이 SDK 버전에서는 `client.chat.completions.parse(...)`가 `beta` 네임스페이스 없이도 존재한다(둘 다 있음, 로컬에서 `hasattr`로 확인). 어댑터와 테스트 페이크는 이 사실에 맞춰 `openai.APIStatusError`/`RateLimitError`/`InternalServerError`/`APITimeoutError`/`APIConnectionError`와 `httpx2.Request`/`Response`를 사용한다.
- Claude Haiku 4.5는 이번 조사에서 "OpenAI 호환 Chat Completions 엔드포인트 지원 여부"를 검증하지 않았다 - 과제 범위가 "Gemini/Upstage/OpenAI 셋 다 OpenAI 호환"이라는 전제를 확인하는 것이었고, Claude는 그 전제에 포함되지 않았기 때문이다. 이 PR은 challenger로 OpenAI 소형 모델(`gpt-4o-mini`)을 선택했다 - 세 프로바이더 모두 동일한 어댑터로 통합 가능함이 확인됐기 때문에 어댑터 종류를 하나 더 늘리지 않아도 됐다. Claude Haiku 4.5를 나중에 후보에 넣으려면 별도 어댑터(또는 Anthropic의 OpenAI 호환성 확인)가 먼저 필요하다.
- **role 기본 모델을 Gemini 하나로 좁히며 모델 id 재확인** (공식 문서 조회 `https://ai.google.dev/gemini-api/docs/models`, 접근일 2026-09-25, 문서 자체 "Last updated 2026-09-24 UTC"): 사용자가 지금 보유한 키가 Gemini(Google Cloud 크레딧)뿐이라 GEN/JUDGE/TONE 세 role 모두 gemini를 기본 프로바이더로 바꿨다(아래 "결정" 참고). 이 ADR을 처음 채택했을 때 기본값으로 골랐던 `gemini-2.5-flash`는 이제 문서상 "Limited Access - 신규 프로젝트는 3.5 Flash-Lite 또는 3.8 Flash 사용 권장" 상태로 바뀌어 있어, 현재 유효한 id로 갱신했다:
  - `gemini-3.5-flash-lite` - "Our fastest, most cost-effective 3.5 model for high-throughput execution." → GEN/TONE 기본값(초안 생성·문체 변환처럼 비용 민감한 작업).
  - `gemini-3.5-flash` - "Our legacy Flash model, providing baseline speed and foundational performance for routine, high-throughput workloads." → 처음 JUDGE 기본값으로 골랐으나, 아래 "부록: 2026-09-25 실제 호출 확인"의 결과로 `gemini-3.1-flash-lite`로 교체했다.
  - 둘 다 같은 "3.5" 세대의 Gemini 모델이므로, 크기(Flash vs Flash-Lite)는 다르지만 벤더/학습 lineage는 같다 - self-preference bias 관점에서는 여전히 "같은 모델 계열"로 취급해야 한다(아래 "결정"의 경고 로직 참고). 최종 프로바이더/모델 조합(특히 judge를 정말 다른 벤더로 분리할지)은 여전히 후속 bake-off ADR의 몫이다.

## 검토한 대안

### 1. 클라이언트 추상화 방식
1. **LiteLLM.** 프로바이더가 늘어날 때 코드 변경이 거의 없다는 장점이 있지만, 이 프로젝트가 쓸 세 프로바이더가 전부 OpenAI 호환 Chat Completions를 이미 제공하므로 추가 의존성을 들일 이유가 약하다. `response_format` structured output의 프로바이더별 지원 여부를 LiteLLM이 어떻게 매핑하는지도 별도 검증이 필요해 오히려 불확실성이 늘어난다.
2. **LangChain chat models(`ChatOpenAI`, `ChatGoogleGenerativeAI`, ...).** `langchain-openai`가 이미 의존성에 있고 LangGraph와 궁합이 좋다. 하지만 `with_structured_output()`의 폴백 동작(네이티브 미지원 시 도구 호출 방식으로 전환 등)이 프로바이더마다 달라 "네이티브 json_schema 우선, 아니면 JSON 모드+복구"라는 이 PR이 원하는 재시도/폴백 정책을 세밀하게 통제하기 어렵다. 메트릭(`LLMMetricsCollector`) 훅도 별도로 붙여야 한다.
3. **프로바이더별 네이티브 SDK(`google-genai`, Upstage 전용 SDK 등).** 프로바이더 고유 기능에 접근하기 가장 좋지만, 세 SDK의 예외 타입·재시도 정책·usage 필드 이름이 전부 달라 어댑터 3벌을 사실상 새로 짜야 한다. 이 PR의 요구사항(재시도/백오프/메트릭을 한 곳에서 통일)과 맞지 않는다.
4. **OpenAI 호환 어댑터 하나 (채택).** 위 표에서 확인했듯 OpenAI/Gemini/Upstage가 전부 OpenAI Chat Completions 스펙(및 그 구조화 출력 스펙의 부분집합)을 그대로 노출한다. `openai` 파이썬 SDK 하나로 `base_url`만 바꿔 세 프로바이더를 다 호출할 수 있다. 예외 타입(`openai.APIStatusError` 등)도 공통이라 재시도/백오프 로직을 한 벌만 짜면 된다.

### 2. 구조화 출력 전략
1. **항상 JSON 모드 + 수동 pydantic 검증만.** 구현이 단순하지만, 세 프로바이더 모두 네이티브 `json_schema` 구조화 출력을 지원한다는 걸 확인했는데도 안 쓰는 셈이라 스키마 위반(필드 누락, 타입 불일치) 재시도가 불필요하게 늘어난다.
2. **함수 호출(tool call)을 이용한 강제 추출.** OpenAI/Gemini는 지원하지만 Upstage의 함수 호출 스키마 강제 수준을 이번 조사에서 확인하지 않았고, 프롬프트를 도구 정의로 바꿔야 해서 "이 PR에서는 프롬프트 문구를 바꾸지 않는다"는 제약과 충돌한다.
3. **네이티브 `json_schema` 우선, 미지원 시 JSON 모드 + `extract_json_from_response` 복구 + pydantic 검증으로 폴백 (채택).** `PROVIDER_CONFIG`의 `supports_json_schema` 플래그로 프로바이더별 분기하므로, 앞으로 이 스펙을 지원하지 않는 프로바이더(레거시 HyperCLOVA 포함)가 추가돼도 코드 구조를 바꾸지 않고 대응한다. 복구 로직은 이미 있던 `core/llm_client.py::extract_json_from_response`(7월 셀프 리뷰에서 고친 버전, ADR 0004)를 재사용한다.

### 3. 새 패키지 vs 기존 파일 리팩터링
- **`core/llm_client.py`를 그대로 고쳐 쓰지 않고 `core/llm/` 패키지를 새로 만들었다 (채택).**
  - `core/llm_client.py`는 `NaverHyperCLOVAClient`(V1/V3 페이로드 분기, 레거시 재시도 루프)와 `extract_json_from_response`, `SimpleRateLimiter`를 담고 있다. 레거시 프로바이더(`naver`)를 계속 지원해야 하므로 이 파일 자체를 지울 수는 없다. (2026-10-18: `NaverHyperCLOVAClient`와 `naver` 프로바이더를 제거했다 - 아래 부록. 이 파일은 공용 도구 때문에 남는다.)
  - 새 계약(`LLMClient.complete()`, role 기반 레지스트리)을 기존 파일에 얹으면 "레거시 `BaseLLMClient.chat_completion()` 계약"과 "신규 `LLMClient.complete()` 계약"이 한 파일에 섞여 호출부에서 어느 쪽을 써야 하는지 헷갈리기 쉽다.
  - `core/llm/` 패키지(`client.py`=계약, `adapters.py`=구현, `schemas.py`=pydantic 모델, `registry.py`=role→client 해석)로 분리하고, `adapters.py`가 `core/llm_client.py`의 `SimpleRateLimiter`/`extract_json_from_response`/`NaverHyperCLOVAClient`를 import해 재사용한다. 호출부는 `from core.llm import get_client`만 알면 된다.

## 결정
- `ai_workspace/core/llm/` 패키지를 새로 만든다.
  - `client.py`: `LLMClient`(ABC) / `LLMResult` / `LLMUsage`.
  - `adapters.py`: `OpenAICompatLLMClient`(OpenAI/Gemini/Upstage 공용, `base_url`만 다름) + `HyperCLOVALLMClient`(레거시 `NaverHyperCLOVAClient`를 `LLMClient` 계약으로 감싼 어댑터, 기본 프로바이더 아님). (2026-10-18 제거 - 아래 부록)
  - `schemas.py`: `ClusterEval`, `NewsletterContent`, `NewsletterMeta`, `NewsletterEval`, `ToneResult` - 각각 `core/reconstruction/prompts.py`, `workflow/evaluators.py`, `core/tone_converter.py`의 기존 프롬프트가 요구하던 출력 필드를 그대로 옮겼다. 이 PR에서 프롬프트 문구는 바꾸지 않는다.
  - `registry.py`: `get_client(role)`, `role ∈ {generator, judge, tone}`. `<PREFIX>_PROVIDER`/`<PREFIX>_MODEL` 환경변수로 해석하고(`GEN_*`/`JUDGE_*`/`TONE_*`), `(provider, model)` 조합당 인스턴스 1개·프로바이더당 레이트리미터 1개를 전역 캐시로 공유한다. **기본 프로바이더는 세 role 모두 `gemini`다**(사용자가 지금 보유한 키가 Gemini뿐이라 - 위 "role 기본 모델을 Gemini 하나로 좁히며..." 참고). `judge`가 `generator`와 다른 모델을 쓰도록 `ROLE_DEFAULT_MODEL`로 role별 기본 모델을 따로 두었다(`generator`/`tone`=`gemini-3.5-flash-lite`, `judge`=`gemini-3.5-flash`). 다만 모델이 달라도 같은 벤더(계열)를 쓰는 것 자체가 self-preference bias 위험이라, `get_client("judge")`가 `_model_family(provider)`(현재는 provider 문자열 그대로)로 generator와 비교해 같은 계열이면 WARNING을 남긴다 - 기본 설정 그대로 쓰면 이 경고가 항상 뜬다는 뜻이고, 이는 의도된 것이다(경고만 하고 막지는 않음 - 최종 판단은 bake-off ADR로 미룬다).
- 재시도/백오프(429·5xx·timeout, 지수 백오프, `Settings.MAX_LLM_CALL_RETRIES` 상한)와 실패 시에도 `LLMMetricsCollector.record_call()` 기록을 `OpenAICompatLLMClient.complete()` 내부 한 곳으로 모았다. 호출부(`ClusterEvaluator`/`NewsletterEvaluator`/`NewsReconstructor`/`ToneConverter`)는 이제 재시도 루프를 직접 돌리지 않고 `client.complete()`를 한 번만 호출한다.
- `LLMMetricsCollector`에 `provider`/`model` 필드와 `by_model` 집계를 추가해 프로바이더별 성공률/비용을 나중에 비교할 수 있게 했다.
- `workflow/nodes.py::initialize_cluster_processing`이 만드는 article dict에 `press_name`을 추가했다 - `NewsletterEvaluator`의 프롬프트(`source_summary`)가 이 필드를 참조하는데 지금까지 빈 문자열로 채워지고 있었다.
- `ai_workspace/.env.example`을 `GEN_PROVIDER`/`GEN_MODEL`/`JUDGE_PROVIDER`/`JUDGE_MODEL`/`TONE_PROVIDER`/`TONE_MODEL`과 `GEMINI_API_KEY`/`UPSTAGE_API_KEY`/`OPENAI_API_KEY`로 갱신하고, `LLM_PROVIDER`/HyperCLOVA 관련 변수는 "레거시, 기본 프로바이더 아님" 절로 내렸다. 이후 Gemini 단일 키로도 파이프라인 전체가 동작하도록 기본값을 gemini로 좁히면서(`GEMINI_API_KEY`만 있으면 됨) 다시 갱신했고, `LLM_KILL_SWITCH`/`LLM_KILL_SWITCH_FILE` 절도 추가했다.
- **모델 선정은 이 PR의 범위가 아니다.** `PROVIDER_CONFIG`/`ROLE_DEFAULT_MODEL`의 기본값(`gemini-3.5-flash-lite`, `gemini-3.5-flash`, `gpt-4o-mini`, `solar-pro3`)은 "일단 동작하는" 안전한 폴백일 뿐이며, 실제 운영 모델 선정(정확도/비용/지연 bake-off, judge를 정말 다른 벤더로 분리할지 포함)은 별도 ADR로 다룬다.

## 증거
- `.venv/bin/python -m pytest -q -m "not integration and not benchmark"`: 119 passed (기존 82 + 이번 PR 37).
- 이번 PR이 추가/변경한 테스트 37건, 전부 페이크 HTTP 레이어(`tests/llm_fakes.py`: `FakeOpenAIClient`/`FakeChatCompletions`, 실제 `openai`/`httpx2` 예외 타입으로 구성) 또는 `LLMClient` 계약을 직접 구현하는 `FakeLLMClient`를 쓴다 - 실제 네트워크 호출 없음.
  - 구조화 출력 경로: `tests/test_llm_v2_structured_output.py` (2건) - `chat.completions.parse` 사용, `parsed=None`은 검증 실패로 취급되어 재시도됨을 확인.
  - JSON 모드 폴백 경로: `tests/test_llm_v2_json_mode_fallback.py` (4건) - `response_format={"type":"json_object"}`, `extract_json_from_response` 복구, 스키마 없는 호출은 텍스트 그대로 반환, 복구 실패 시 처리.
  - 재시도/백오프 상한: `tests/test_llm_v2_retry_backoff.py` (4건) - 429/5xx/timeout/connection error 재시도 후 성공, `Settings.MAX_LLM_CALL_RETRIES` 소진 시 포기, 재시도 불가 4xx(401)는 즉시 반환.
  - 실패 시 메트릭 기록: `tests/test_llm_v2_metrics_recording.py` (2건) - 성공 호출은 `purpose`/`provider`/`model`/토큰과 함께, 포기하기 전의 실패한 시도들도 `success=False`로 각각 기록됨을 확인.
  - 레지스트리: `tests/test_llm_v2_registry.py` (9건) - role 기본값이 서로 다른 프로바이더로 갈린다는 요건, 환경변수 오버라이드, 알 수 없는 role/프로바이더 에러, `(provider, model)` 동일 인스턴스 공유, 프로바이더당 레이트리미터 공유, judge==generator 프로바이더일 때 경고 로그, API 키 누락 에러, `naver` 레거시 어댑터 생성.
  - 호출부 통합: `tests/test_generator_llm_v2_integration.py`(2건), `tests/test_tone_converter_provider.py`(2건, 기존 테스트를 주입식 DI에 맞게 갱신), `tests/test_evaluators_max_retries.py`(7건, 기존 "직접 재시도 루프" 가정을 "client.complete()를 한 번만 호출하고 결과를 매핑" 가정으로 갱신).
  - `press_name` 회귀: `tests/test_cluster_processing_press_name.py` (2건, ndarray/list 두 경로 모두).
  - **LangGraph 서브그래프 end-to-end**: `tests/test_llm_v2_workflow_subgraph_e2e.py` (1건) - `workflow.graph.compile_workflow()`로 컴파일한 실제 그래프를, judge/generator/tone 세 role 전부를 가짜 클라이언트로 monkeypatch한 채 `init → cluster_eval → generate → newsletter_eval → embed → tone → save` 전 구간 실행. DB 저장(`save_news_letter`)도 페이크로 대체(로컬 macOS에는 Postgres 없음). 클러스터/뉴스레터 평가가 정확히 큐 순서대로 소비되고, 최종 저장된 뉴스레터가 문체 변환 결과를 우선 쓰는지까지 확인.
- 임베딩(`NewsEmbedder`)은 torch가 없는 이 로컬 환경에서 import가 실패하지만 `embed_newsletter_node`가 이를 잡아 `newsletter_embedding=None`으로 넘어가므로 e2e 테스트가 끊기지 않는다 - 실제 임베딩 동작은 torch가 있는 CI에서 검증된다(기존 동작, 이 PR에서 바꾸지 않음).

## 결과와 한계
- **모델 선정은 후속 bake-off ADR로 미룬다.** 이 ADR은 "어떻게 여러 프로바이더를 한 어댑터로 통합하고 구조화 출력을 강제하는가"만 다룬다. `gemini-2.5-flash`/`gpt-4o-mini`/`solar-pro3` 기본값은 잠정치다. 범위 종결 C-10 (2026-10-18): 유보 - bake-off v1은 유보이고 모델 선정은 하지 않는다. 현행 기본값은 선정이 아니다([ADR 0009 A9](0009-llm-eval-protocol-and-preregistered-decision-rule.md), [목록](../design/2026-10-18-scope-closure.md)).
- **Claude Haiku 4.5는 이 PR에 포함되지 않았다.** OpenAI 호환 Chat Completions 지원 여부를 검증하지 않았기 때문이다(위 "검증한 사실" 참고). 후속 bake-off에서 candidate로 넣으려면 호환성 확인이나 별도 어댑터가 선행돼야 한다.
- **Gemini의 OpenAI 호환 레이어는 구글 공식 문서가 "아직 베타"라고 명시한다.** 프로덕션에서 예외 처리 경로(특히 구조화 출력 실패 시 `parsed=None`이 아니라 다른 형태로 실패하는 경우)가 OpenAI 네이티브와 다를 가능성이 있다 - 실제 API 키로 통합 테스트를 돌리기 전까지는 열린 위험으로 남는다(이 PR은 페이크로만 검증했다).
- **Upstage 구조화 출력은 OpenAI 스펙의 부분집합만 지원**한다(`allOf`/`oneOf`/재귀 `$ref`/`patternProperties` 등 미지원, `additionalProperties: false` 필수, 중첩 10단계 제한). 이 PR의 5개 스키마(`schemas.py`)는 전부 평범한 flat/list 구조라 문제 없지만, 앞으로 스키마가 복잡해지면 이 제약을 다시 확인해야 한다.
- **레이트리미터는 여전히 프로세스 내 최소 호출 간격만 제어**한다(`SimpleRateLimiter`, 기존 구현 재사용). 실제 분당 요청 한도(RPM) 기반 제어나 분산 환경에서의 공유는 다루지 않는다.
- 이 PR은 `evaluate_cluster`/`evaluate_newsletter` 노드의 judge 입력(기사 제목+본문 500자 미리보기)을 바꾸지 않았다 - "judge-v2(기사 본문 포함)"는 과제 정의상 별도 PR이다.

### 후속 커밋으로 닫은 갭

이 ADR 채택 시점에는 없었지만, 실제로 이 fork를 돌려보면서 드러난 세 가지 갭을
후속 커밋(같은 `feat/llm-client-v2` 브랜치)에서 닫았다:

1. **비용 통제 수단 부재 → LLM 킬 스위치.** 이 PR이 병합된 시점에는 프로바이더
   호출을 외부에서 즉시 끌 방법이 없었다 - 예정된 cron 비용 가드가 실제 Google
   Cloud 과금 시작을 감지해도 파이프라인을 세울 방법이 로그를 보고 프로세스를
   죽이는 것뿐이었다. `core/llm/kill_switch.py::check_kill_switch()`를 추가해
   `OpenAICompatLLMClient.complete()`/`HyperCLOVALLMClient.complete()`가 실제
   네트워크 요청 전에 env `LLM_KILL_SWITCH`(`"1"`/`"true"`/`"yes"`) 또는
   `Settings.LLM_KILL_SWITCH_FILE`(기본 `<저장소 루트>/.ops/LLM_KILL_SWITCH`)
   파일 존재를 확인하고, 켜져 있으면 프로바이더를 호출하지 않고
   `LLMResult(error="kill_switch", attempts=0)`를 반환한다. LangGraph 쪽은 별도
   특수 케이스 없이 기존 실패 처리 경로(judge는 FAIL, generator는 로컬 휴리스틱
   폴백, tone은 결정론적 softener)를 그대로 타서 배치가 크래시 없이, 뉴스레터를
   저장하지 않고 끝난다.
2. **ToneConverter 콘텐츠 검증 재시도가 리팩터링 중 사라짐.** 이 PR 이전
   구현(`core/llm_client.py` 기반)은 `validate_conversion()` 실패 시 최대 5회
   재생성했는데, `core/llm/` 도입 리팩터링 과정에서 1회 호출 후 바로 결정론적
   softener로 폴백하도록 축소돼 있었다(의도한 변경이 아니라 리팩터링 중 놓친
   회귀였다). `Settings.MAX_RETRY_TONE_VALIDATION`(기본 2)로 조절 가능한 바운드
   재시도를 복원했다 - 최초 1회 + 추가 최대 2회, `validate_conversion()`이 거부한
   경우에만 재시도한다. 429/5xx/timeout 같은 전송 계층 재시도는 여전히
   `OpenAICompatLLMClient.complete()` 내부 책임이다.
3. **`openai.LengthFinishReasonError`/`ContentFilterFinishReasonError`가 뭉뚱그려짐.**
   `chat.completions.parse()`가 던지는 이 두 예외는 범용 `except Exception`에
   걸려 재시도 없이 종료되는 것까지는 맞았지만, `str(e)` 자유형 메시지만 남아
   메트릭에서 "왜 종료됐는지" 구분할 방법이 없었다. 두 예외를 별도 `except` 절로
   분리해 `LLMResult.error`에 안정적인 코드(`"length"`/`"content_filter"`)를 남기고,
   `LLMMetricsCollector`에 `error_type` 필드를 추가해 같은 코드를 기록하게 했다.

## 부록: 2026-09-25 실제 호출 확인

선불 크레딧 충전 후 실제 키로 확인했다(구조화 출력 스키마 1개, `timeout=30s`, SDK 재시도 없음).

| 모델 | 결과 |
|---|---|
| `gemini-3.5-flash-lite` (generator) | 정상, 1.8초, 입력 27 / 출력 54 토큰, 스키마 파싱 성공 |
| `gemini-3.5-flash` (당시 judge 기본값) | 30초 타임아웃. 앞선 실행에서는 HTTP 503이 연속으로 나 재시도 10회 동안 수 분간 멈췄다 |
| `gemini-flash-latest` | HTTP 503 (수요 과부하) |
| `gemini-2.5-flash` | HTTP 404 (이 계정에서 사용 불가) |
| `gemini-3-flash-preview` | 응답은 오지만 출력이 4토큰에서 잘림 |
| `gemini-3.1-flash-lite` | 정상, 8.1초, 스키마 준수 → judge 기본값으로 채택 |

이 과정에서 두 가지 결함이 드러나 함께 고쳤다.
- OpenAI SDK의 기본 타임아웃(600초)과 SDK 자체 재시도(2회)가 이 클라이언트의 재시도와 겹쳤다. SDK는 `timeout=LLM_REQUEST_TIMEOUT_S`(기본 60초), `max_retries=0`으로 만들고 재시도는 이 클라이언트만 담당한다.
- 재시도 횟수만 제한하고 전체 대기 시간은 제한하지 않았다. 호출 1건의 전체 제한 시간 `LLM_CALL_DEADLINE_S`(기본 180초)를 두어, 다음 대기가 이를 넘으면 `call deadline reached` 오류로 종료한다.

같은 계열(gemini) 모델을 generator와 judge에 함께 쓰는 것은 여전히 임시다. 최종 선택은 bake-off ADR(0009)에서 한다.

## 부록: 2026-10-06 킬 스위치 파일의 기본 위치와 지출 가드가 켠 파일

지출 상한(ADR 0035)을 넣으면서 이 ADR의 킬 스위치 동작 두 가지가 바뀌었다. "닫은 갭 1"의 본문은 그때의 기록으로 둔다.

1. **기본 경로는 메인 체크아웃의 `.ops/LLM_KILL_SWITCH`다.** 전에는 코드가 있는 체크아웃의 `.ops/`였고, git
   워크트리마다 다른 파일이었다. 그래서 저장소 밖 감시가 메인 체크아웃에 켠 킬 스위치를 워크트리에서 돌리는 유료
   실험이 보지 못했고, 워크트리에서 켜진 킬 스위치를 메인에서 돌린 `spend_cli summary`는 꺼짐으로 보여 줬다.
   지출 원장과 같은 규칙(`config/settings.py::shared_ops_root`)으로 맞췄다. `LLM_KILL_SWITCH_FILE`을 지정하면 그
   값이 우선이고, **상대 경로는 실행한 디렉터리가 아니라 메인 체크아웃 기준**으로 읽는다(전에는 CWD 기준이라
   저장소 루트에서 도는 잡과 `ai_workspace/`에서 도는 `main.py`가 서로 다른 파일을 봤다). compose는 지금처럼
   절대 경로 `/ops/LLM_KILL_SWITCH`를 준다. 근거: `tests/test_llm_kill_switch.py`의 경로 테스트 3건.
2. **지출 가드가 켠 파일은 "kill_switch" 결과가 아니라 런 중단이다.** 파일 내용이
   `{"engaged_by": "llm_spend_cap", ...}`이면 클라이언트는 `LLMResult(error="kill_switch")`를 돌려주지 않고
   `LLMBudgetExceeded`를 낸다(ADR 0035 결정 6). 위 "닫은 갭 1"이 적은 경로(judge FAIL, 생성기 로컬 초안, 문체
   softener)는 **뉴스레터를 저장하지 않고 끝난다고 적었지만 문체 단계에 이미 들어온 글에는 성립하지 않는다** -
   게이트를 통과한 초안이 규칙 기반 변환본과 함께 저장된다(리뷰에서 `ToneConverter`가 "kill_switch" 결과 세 번에
   폴백 dict를 돌려주는 것을 확인). 일 상한에 닿을 때마다 킬 스위치가 켜지므로 그 경로가 자주 실행될 뻔했다.
   환경변수 `LLM_KILL_SWITCH`와 다른 주체가 만든 파일(내용 없음 포함)은 이 ADR의 동작 그대로이고, 위의 한계도
   그대로 남아 있다.

## 부록: 2026-10-18 reasoning_effort 역할별 전달, usage 기록, 재시도 금지 목록, pre-flight 실측

정합 패치 M1 축소판(실행 계획 WP3)이다. 재시도·기록 정책의 전/후 표와 E0 숫자에 준 영향은 [ADR 0010 Addendum 2026-10-18](0010-faithfulness-gate-and-judge-v2.md)에 있고, 여기서는 이 ADR의 어댑터 계층이 바뀐 곳만 적는다.

1. **`reasoning_effort`를 역할별 env로 전달한다.** `GEN_REASONING_EFFORT` / `JUDGE_REASONING_EFFORT` / `TONE_REASONING_EFFORT`(허용: `none`, `minimal`, `low`, `medium`, `high`; 비우거나 `unset`·`default`면 미설정).
   미설정이면 요청 본문에 이 키가 **없다** — E0까지의 요청과 같다(`tests/test_reasoning_effort_env.py`). 알 수 없는 값은 조용히 무시하지 않고 `ValueError`로 거부한다(비용 가정이 바뀌므로). 전달을 문서로 확인한 프로바이더(`gemini`, OpenAI 호환 계층 문서
   https://ai.google.dev/gemini-api/docs/openai, 갱신 2026-09-02, 접근 2026-10-06)에만 허용하고, 다른 프로바이더에 값을 설정하면 거부한다. 레지스트리의 인스턴스 캐시 키는 effort를 설정했을 때만 `(provider, model, effort)`이고 미설정은 예전과 같은 `(provider, model)`이다
   (generator와 tone은 기본 모델이 같아 effort가 다르면 인스턴스를 나눠야 하고, E0 합성 실행이 `(provider, model)` 키로 페이크를 끼운다).
2. **usage 기록.** `LLMUsage`의 `total_tokens`·`thinking_tokens`·`cached_tokens`(nullable)는 ADR 0035가 이미 넣었고(실행 계획의 `thought_tokens`는 코드의 `thinking_tokens`다), 이번에 `llm_metrics`의 호출 기록과 요약(`usage_reported`)에 같은 값과 요청에 실은 `reasoning_effort`를 싣는다(성공한 시도뿐 아니라 HTTP 400·스키마 실패·`length`·`content_filter`·타임아웃 등 실패한 시도도 같다 -
   `tests/test_reasoning_effort_env.py`; 킬 스위치·지출 가드로 요청이 나가지 않은 호출은 `unset`으로 센다).
   프로바이더가 보고하지 않은 값은 0이 아니라 `None`이고, 요약은 합과 함께 "보고한 호출 수"를 적는다.
3. **재시도 금지 목록.** `LLMResult.no_retry_reason`이 킬 스위치·HTTP 401/402/403/404·`length`·`content_filter`를 `infra`/`length`/`content_filter`로 분류하고, 그래프와 생성기·문체 변환기가 그 사유가 있으면 같은 호출을 되풀이하지 않는다.
   "닫은 갭 1"이 적은 "기존 실패 처리 경로를 그대로 타서 뉴스레터를 저장하지 않고 끝난다"는 서술은 이 부록 이후 클러스터 단위로도 맞다 — 클러스터 평가·생성·judge 호출이 막히면 클러스터는 `skipped`가 아니라 `failure_reason="infra"`로 닫힌다.
   문체 변환 호출이 막히면 검증 재시도만 멈추고 결정론적 폴백 문장으로 저장하며 `failure_reason`은 없다(저장 동작 불변).
4. **스키마 검증 실패 재시도는 2회까지**(`MAX_LLM_SCHEMA_RETRIES`, 기본 2). 전송 계층 재시도(`MAX_LLM_CALL_RETRIES`=10)와 따로 센다.

### reasoning pre-flight (실측, 합성 입력, 2026-10-18 06:09 UTC)

E0 pre-flight와 같은 합성 3기사(실제 기사 아님)·같은 호출 순서(클러스터 평가 → 본문 → 메타 → 뉴스레터 평가 → 문체 변환)를 effort마다 1회씩 돌렸다.
지출 가드(ADR 0035)를 거쳤고 런 상한은 $0.10으로 걸었다. **원장 기준 실제 지출 $0.006154**(요청 10건 중 HTTP 400 3건은 비과금, 과금 7건). n=1이고 효과 크기를 가를 수 없는 표다.
모델은 E0과 같다: generator·tone `gemini-3.5-flash-lite`, judge `gemini-3.1-flash-lite`. 입력이 앞 호출의 출력에 이어지므로 `none` 팔은 본문 생성이 실패한 뒤 로컬 폴백 초안으로 이어져 뉴스레터 평가 입력이 `minimal` 팔과 다르다(짝지은 비교가 아니다).

| 스키마(purpose) | 모델 | effort | parsed | 지연(s) | prompt / completion / total | hidden | 보고된 reasoning 토큰 |
|---|---|---|---|---|---|---|---|
| cluster_eval | 3.1-flash-lite | `none` | 예 | 1.66 | 604 / 131 / 735 | 0 | 없음(null) |
| newsletter_content_gen | 3.5-flash-lite | `none` | **아니오: HTTP 400 INVALID_ARGUMENT** | 0.37 | — | — | — |
| newsletter_meta_gen | 3.5-flash-lite | `none` | **아니오: HTTP 400** | 0.27 | — | — | — |
| newsletter_eval | 3.1-flash-lite | `none` | 예 | 1.42 | 925 / 176 / 1101 | 0 | 없음(null) |
| tone_convert | 3.5-flash-lite | `none` | **아니오: HTTP 400** | 0.34 | — | — | — |
| cluster_eval | 3.1-flash-lite | `minimal` | 예 | 1.23 | 604 / 135 / 739 | 0 | 없음(null) |
| newsletter_content_gen | 3.5-flash-lite | `minimal` | 예 | 3.22 | 1209 / 470 / 1679 | 0 | 없음(null) |
| newsletter_meta_gen | 3.5-flash-lite | `minimal` | 예 | 1.18 | 1362 / 113 / 1475 | 0 | 없음(null) |
| newsletter_eval | 3.1-flash-lite | `minimal` | 예 | 1.85 | 1341 / 391 / 1732 | 0 | 없음(null) |
| tone_convert | 3.5-flash-lite | `minimal` | 예 | 2.37 | 1156 / 584 / 1740 | 0 | 없음(null) |

hidden = `total − prompt − completion`. 참고로 E0 pre-flight(미설정, 2026-10-17)의 지연은 2.22 / 2.45 / 1.00 / 2.48 / 2.09초, hidden은 전부 0이다(`reports/llm/e0_baseline_v1.md` "pre-flight").

읽은 것과 결정:
- **`none`은 `gemini-3.5-flash-lite`(generator·tone)가 받지 않는다**(HTTP 400, 요청 3건 모두). `gemini-3.1-flash-lite`(judge)는 받는다. 설계 문서가 미확인으로 남긴 "3.5-flash-lite가 `none`을 받는가"의 답은 **아니오**다.
- `minimal`은 세 역할 모델이 모두 받았고 다섯 스키마가 모두 파싱됐다.
- OpenAI 호환 계층의 usage에서 **reasoning 토큰(`completion_tokens_details.reasoning_tokens`)과 cached 토큰(`prompt_tokens_details.cached_tokens`)이 오지 않았다**(필드 없음 또는 null — 어느 쪽인지는 구별하지 않았다). `total`이 `prompt + completion`과 같아 숨은 토큰은 0이다. 그래서 thinking 토큰이 `completion_tokens`에 이미 들어 있는지,
  아예 쓰이지 않았는지는 이 표로 **구별할 수 없다**. 비용 추정이 하한인지는 여전히 **측정하지 않음**(ADR 0035의 보수적 청구 규칙이 그대로 필요하다).
- 지연·토큰의 `none`/`minimal`/미설정 차이는 n=1이라 주장하지 않는다.
- **기본값은 미설정으로 둔다.** `none`은 generator·tone에 쓸 수 없고, `minimal`은 비용·지연·품질에서 이점이 측정되지 않았으며, 기본값을 바꾸면 E0 기준선(미설정으로 사전 등록)과 요청이 달라진다.
  `minimal`은 세 역할이 모두 받는 유일한 값이라 후속 비교(생성 비용·지연·규칙 위반률을 같은 입력에서 짝지어)를 하게 되면 그 후보다.
- 잘못된 값은 서버가 HTTP 400으로 돌려주고 400은 재시도 금지 목록에 없다 — 그래서 `GEN_REASONING_EFFORT=none`을 설정하면 요청마다 400을 받고(과금되지 않는다) 클러스터가 `generator_fallback`으로 실패한다. 설정 전에 이 표를 볼 것.
- 이 표를 만든 임시 스크립트는 저장소에 넣지 않았다(범위 동결: 새 실험 하네스 금지). 합성 입력은 `evaluation/llm/e0_baseline.py`의 `PREFLIGHT_ARTICLES`다.

## 부록: 2026-10-18 HyperCLOVA 레거시 제거

위 "컨텍스트"대로 HyperCLOVA X 계정은 더는 쓸 수 없고, 어댑터·레지스트리·설정에는 `naver` 프로바이더가 "레거시, 기본값 아님"으로 남아 있었다. 범위 종결 뒤 남은 코드 정리로 걷어냈다
(`docs/design/2026-09-26-architecture-review.md`가 제거 대상으로 이미 적어 둔 항목이다).

**제거한 것.**
- `core/llm_client.py`: `NaverHyperCLOVAClient`(와 `V3_MODELS`/`V1_MODELS`), 전역 `LLM_PROVIDER` 환경변수(기본값 `naver`)를 읽던 `get_llm_client()`, 그 클라이언트만 쓰던 `requests` 임포트.
- `core/llm/adapters.py`: `HyperCLOVALLMClient`. `core/llm/registry.py`: `PROVIDER_CONFIG["naver"]`와 `legacy` 분기.
  이제 `GEN_PROVIDER`/`JUDGE_PROVIDER`/`TONE_PROVIDER=naver`는 다른 프로바이더로 조용히 넘어가지 않고 "알 수 없는 프로바이더 ... naver(HyperCLOVA)는 2026-10-18에 제거했다"로 멈춘다.
- `config/settings.py`: `LLM_PROVIDER`(기본값 `naver`), `NCP_CLOVASTUDIO_API_KEY`, `NCP_APIGW_API_KEY`, `HYPERCLOVA_MODEL`. `ai_workspace/.env.example`의 HyperCLOVA 절.
  환경에 이 변수들이 남아 있어도 읽는 곳이 없어 무시된다.
- 테스트: HyperCLOVA 클라이언트·어댑터를 직접 건드리던 것만 뺐다 - `tests/test_llm_client_hyperclova_retry_bound.py`(파일째), `tests/test_llm_budget_clients.py`의 HyperCLOVA 절(6건),
  `tests/test_llm_kill_switch.py`·`tests/test_llm_kill_switch_legacy.py`·`tests/test_llm_metrics_persistence.py`의 각 1건, `tests/test_llm_v2_registry.py`의 `naver` 어댑터 생성 1건.
  같은 성질(미등록 단가 거부, 대체 단가 예약, 실패한 시도의 최악 비용 정산, 서킷브레이커, 킬 스위치가 요청 전에 막음, 실패 시도 메트릭 기록)은 `OpenAICompatLLMClient`와 레거시 `OpenAIClient` 경로의 기존 테스트와
  `tests/test_llm_budget_guard.py`·`tests/test_llm_circuit_breaker.py`가 계속 본다. 더한 것: `naver`가 거부되고 안내 문구를 싣는지(`tests/test_llm_v2_registry.py`), 남긴 결과 이름 `body_5xx`가 가드에서 브레이커에 세어지는지(`tests/test_llm_circuit_breaker.py`).

**남긴 것.** `core/llm_client.py`의 `SimpleRateLimiter`, `extract_json_from_response`, 재시도 상수(`MAX_RETRIES` 등), `BaseLLMClient`, 레거시 `OpenAIClient` - 어댑터·레지스트리·문체 변환기와 테스트가 쓴다.
`OpenAIClient`는 호출하는 운영 코드가 없지만 이번에 건드리지 않았다(지출 상한 테스트가 걸려 있다). `core/llm/budget.py`의 결과 이름 `body_5xx`(HTTP 200 본문의 서버 오류 코드)는 HyperCLOVA 클라이언트만 냈고
지금은 내는 클라이언트가 없다 - 어휘만 남기고 주석을 고쳤으며 동작은 그대로다.

**운영·재현성 확인.**
- 운영 경로: 세 역할의 기본 프로바이더는 `gemini`(위 "결정")이고 `naver`는 명시해야만 닿았다. `get_llm_client()`를 부르는 코드는 저장소에 없었다(문체 변환기는 역할 레지스트리를 쓴다 -
  `tests/test_tone_converter_provider.py`). compose·CI·스크립트에 `naver`/`LLM_PROVIDER`/`NCP_*` 값은 없다(`git grep`). 배포된 Tier 0 VM의 `.env`는 이 세션에서 읽지 않았다 -
  거기에 `GEN_PROVIDER=naver` 같은 값이 있으면 다음 재배포 뒤 시작에서 위 오류로 멈추는데, 그 값이 있었다는 기록은 없다.
- E0 재현성: E0의 등록 설정(`e0_registered.SETTINGS` 18개 키, 역할 → `gemini`)에 `LLM_PROVIDER`·`naver`는 없다. `e0_registered.live_settings()`가 읽는 `core.llm_client.MAX_RETRIES`는 그대로 있다.
  파일 대역의 `SOURCE_PINS`(`evaluation/llm/e0_store.py`, 함수 본문 sha256 8개)는 이번에 건드린 함수가 하나도 없어 **갱신하지 않았다** - `tests/evaluation/test_e0_store.py`가 통과한다.
  E0 리포트의 `code_sha`(35d0a1d)는 git 이력에 그대로 있다. E0을 다시 돌리려면 새 `code_sha`가 필요한데(ADR 0009 A8.2 (c)), 그건 #36 이후 이미 그렇다.
- 확인: `nice -n 19`로 LLM 경로·E0 하네스·bake-off·평가셋·gate_probe 관련 테스트 870건 통과(전체 스위트·docker는 돌리지 않았다).
