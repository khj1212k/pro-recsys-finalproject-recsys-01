# ADR 0005: HyperCLOVA X 이후 - OpenAI 호환 어댑터 하나로 여러 LLM 프로바이더 통합

## 상태
채택됨 (2026-09-25)

## 컨텍스트
- 이 fork를 진행하는 사용자 계정에서 더 이상 HyperCLOVA X(Naver CLOVA Studio) API를 쓸 수 없다. 파이프라인의 기본 프로바이더였던 `core/llm_client.py::NaverHyperCLOVAClient`는 그대로는 동작하지 않는다.
- 대체 프로바이더 후보: Google Gemini(사용자의 Google Cloud 크레딧으로 유료 사용 가능), Upstage Solar Pro 3(한국어 특화), 그리고 challenger 한 곳(OpenAI 소형 모델 또는 Claude Haiku 4.5).
- LLM-as-judge 요건상 judge는 generator와 다른 모델 계열(provider)이어야 한다.
- 기존 코드(`core/llm_client.py`, `workflow/evaluators.py`, `core/reconstruction/generator.py`, `core/tone_converter.py`)는 LangGraph 노드가 호출될 때마다 새 클라이언트를 만들고, 프로바이더별로 다른 메서드 시그니처(`chat_completion` vs 없음)를 썼으며, 구조화 출력은 전부 "JSON을 프롬프트로 요구 + `extract_json_from_response`로 복구 + 수동 파싱"에 의존했다.
- 이 PR의 범위는 클라이언트 추상화 계층 자체이며, 최종적으로 어떤 프로바이더/모델 조합을 운영에 쓸지(bake-off)는 다루지 않는다 - 아래 "결과와 한계"에서 후속 ADR로 명시적으로 미룬다.

### 검증한 사실 (WebFetch/브라우저로 공식 문서 확인, 접근일 2026-09-25)
| 프로바이더 | OpenAI 호환 Chat Completions | `response_format: json_schema` 구조화 출력 | base_url | 출처 |
|---|---|---|---|---|
| OpenAI | 네이티브 | 지원 (`gpt-4o-mini`, `gpt-4o-mini-2024-07-18`, `gpt-4o-2024-08-06` 이상부터; Chat Completions/Responses/Assistants/Fine-tuning/Batch API 전체) | (OpenAI SDK 기본값) | https://developers.openai.com/api/docs/guides/structured-outputs (platform.openai.com/docs/guides/structured-outputs가 301로 리다이렉트되는 현재 정규 URL) |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | 지원 - `client.chat.completions.parse(model=..., response_format=PydanticModel)` 예시가 문서에 있음 (베타 명시: "OpenAI 라이브러리 지원은 아직 베타") | `https://generativelanguage.googleapis.com/v1beta/openai/` | https://ai.google.dev/gemini-api/docs/openai (문서 자체 "최종 업데이트: 2026-09-12") |
| Upstage | `https://api.upstage.ai/v1` | 지원 - `response_format={"type": "json_schema", "json_schema": {..., "strict": true}}`, OpenAI Structured Outputs의 부분집합(subset)을 그대로 따른다고 문서에 명시 | `https://api.upstage.ai/v1` | https://console.upstage.ai/docs/capabilities/generate/structured-outputs |

- 부가 확인: Upstage 모델 목록(https://console.upstage.ai/docs/models, 접근일 2026-09-25)에는 `Solar Pro 4`가 현재 flagship으로 소개되고 있지만 `Solar Pro 3`("Powerful MoE model with 102B parameters")도 여전히 사용 가능한 모델로 나열되어 있다 - 과제에서 지정한 `solar-pro3`는 여전히 유효한 선택지다.
- 로컬 환경 확인: 설치된 `openai` 파이썬 SDK(3.19.2)는 내부적으로 `httpx2`(pydantic/httpx 팀이 배포하는 httpx의 차세대 메이저 버전, PyPI 공개 패키지)를 쓴다(`openai/_base_client.py`에서 `import httpx2`). 이 SDK 버전에서는 `client.chat.completions.parse(...)`가 `beta` 네임스페이스 없이도 존재한다(둘 다 있음, 로컬에서 `hasattr`로 확인). 어댑터와 테스트 페이크는 이 사실에 맞춰 `openai.APIStatusError`/`RateLimitError`/`InternalServerError`/`APITimeoutError`/`APIConnectionError`와 `httpx2.Request`/`Response`를 사용한다.
- Claude Haiku 4.5는 이번 조사에서 "OpenAI 호환 Chat Completions 엔드포인트 지원 여부"를 검증하지 않았다 - 과제 범위가 "Gemini/Upstage/OpenAI 셋 다 OpenAI 호환"이라는 전제를 확인하는 것이었고, Claude는 그 전제에 포함되지 않았기 때문이다. 이 PR은 challenger로 OpenAI 소형 모델(`gpt-4o-mini`)을 선택했다 - 세 프로바이더 모두 동일한 어댑터로 통합 가능함이 확인됐기 때문에 어댑터 종류를 하나 더 늘리지 않아도 됐다. Claude Haiku 4.5를 나중에 후보에 넣으려면 별도 어댑터(또는 Anthropic의 OpenAI 호환성 확인)가 먼저 필요하다.

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
  - `core/llm_client.py`는 `NaverHyperCLOVAClient`(V1/V3 페이로드 분기, 레거시 재시도 루프)와 `extract_json_from_response`, `SimpleRateLimiter`를 담고 있다. 레거시 프로바이더(`naver`)를 계속 지원해야 하므로 이 파일 자체를 지울 수는 없다.
  - 새 계약(`LLMClient.complete()`, role 기반 레지스트리)을 기존 파일에 얹으면 "레거시 `BaseLLMClient.chat_completion()` 계약"과 "신규 `LLMClient.complete()` 계약"이 한 파일에 섞여 호출부에서 어느 쪽을 써야 하는지 헷갈리기 쉽다.
  - `core/llm/` 패키지(`client.py`=계약, `adapters.py`=구현, `schemas.py`=pydantic 모델, `registry.py`=role→client 해석)로 분리하고, `adapters.py`가 `core/llm_client.py`의 `SimpleRateLimiter`/`extract_json_from_response`/`NaverHyperCLOVAClient`를 import해 재사용한다. 호출부는 `from core.llm import get_client`만 알면 된다.

## 결정
- `ai_workspace/core/llm/` 패키지를 새로 만든다.
  - `client.py`: `LLMClient`(ABC) / `LLMResult` / `LLMUsage`.
  - `adapters.py`: `OpenAICompatLLMClient`(OpenAI/Gemini/Upstage 공용, `base_url`만 다름) + `HyperCLOVALLMClient`(레거시 `NaverHyperCLOVAClient`를 `LLMClient` 계약으로 감싼 어댑터, 기본 프로바이더 아님).
  - `schemas.py`: `ClusterEval`, `NewsletterContent`, `NewsletterMeta`, `NewsletterEval`, `ToneResult` - 각각 `core/reconstruction/prompts.py`, `workflow/evaluators.py`, `core/tone_converter.py`의 기존 프롬프트가 요구하던 출력 필드를 그대로 옮겼다. 이 PR에서 프롬프트 문구는 바꾸지 않는다.
  - `registry.py`: `get_client(role)`, `role ∈ {generator, judge, tone}`. `<PREFIX>_PROVIDER`/`<PREFIX>_MODEL` 환경변수로 해석하고(`GEN_*`/`JUDGE_*`/`TONE_*`), `(provider, model)` 조합당 인스턴스 1개·프로바이더당 레이트리미터 1개를 전역 캐시로 공유한다. 기본값은 `generator=gemini`, `judge=openai`, `tone=upstage`로 둬서 judge가 기본적으로 generator와 다른 모델 계열이 되도록 했다.
- 재시도/백오프(429·5xx·timeout, 지수 백오프, `Settings.MAX_LLM_CALL_RETRIES` 상한)와 실패 시에도 `LLMMetricsCollector.record_call()` 기록을 `OpenAICompatLLMClient.complete()` 내부 한 곳으로 모았다. 호출부(`ClusterEvaluator`/`NewsletterEvaluator`/`NewsReconstructor`/`ToneConverter`)는 이제 재시도 루프를 직접 돌리지 않고 `client.complete()`를 한 번만 호출한다.
- `LLMMetricsCollector`에 `provider`/`model` 필드와 `by_model` 집계를 추가해 프로바이더별 성공률/비용을 나중에 비교할 수 있게 했다.
- `workflow/nodes.py::initialize_cluster_processing`이 만드는 article dict에 `press_name`을 추가했다 - `NewsletterEvaluator`의 프롬프트(`source_summary`)가 이 필드를 참조하는데 지금까지 빈 문자열로 채워지고 있었다.
- `ai_workspace/.env.example`을 `GEN_PROVIDER`/`GEN_MODEL`/`JUDGE_PROVIDER`/`JUDGE_MODEL`/`TONE_PROVIDER`/`TONE_MODEL`과 `GEMINI_API_KEY`/`UPSTAGE_API_KEY`/`OPENAI_API_KEY`로 갱신하고, `LLM_PROVIDER`/HyperCLOVA 관련 변수는 "레거시, 기본 프로바이더 아님" 절로 내렸다.
- **모델 선정은 이 PR의 범위가 아니다.** `PROVIDER_CONFIG`의 `default_model`(`gemini-2.5-flash`, `gpt-4o-mini`, `solar-pro3`)은 "일단 동작하는" 안전한 폴백일 뿐이며, 실제 운영 모델 선정(정확도/비용/지연 bake-off)은 별도 ADR로 다룬다.

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
- **모델 선정은 후속 bake-off ADR로 미룬다.** 이 ADR은 "어떻게 여러 프로바이더를 한 어댑터로 통합하고 구조화 출력을 강제하는가"만 다룬다. `gemini-2.5-flash`/`gpt-4o-mini`/`solar-pro3` 기본값은 잠정치다.
- **Claude Haiku 4.5는 이 PR에 포함되지 않았다.** OpenAI 호환 Chat Completions 지원 여부를 검증하지 않았기 때문이다(위 "검증한 사실" 참고). 후속 bake-off에서 candidate로 넣으려면 호환성 확인이나 별도 어댑터가 선행돼야 한다.
- **Gemini의 OpenAI 호환 레이어는 구글 공식 문서가 "아직 베타"라고 명시한다.** 프로덕션에서 예외 처리 경로(특히 구조화 출력 실패 시 `parsed=None`이 아니라 다른 형태로 실패하는 경우)가 OpenAI 네이티브와 다를 가능성이 있다 - 실제 API 키로 통합 테스트를 돌리기 전까지는 열린 위험으로 남는다(이 PR은 페이크로만 검증했다).
- **Upstage 구조화 출력은 OpenAI 스펙의 부분집합만 지원**한다(`allOf`/`oneOf`/재귀 `$ref`/`patternProperties` 등 미지원, `additionalProperties: false` 필수, 중첩 10단계 제한). 이 PR의 5개 스키마(`schemas.py`)는 전부 평범한 flat/list 구조라 문제 없지만, 앞으로 스키마가 복잡해지면 이 제약을 다시 확인해야 한다.
- **레이트리미터는 여전히 프로세스 내 최소 호출 간격만 제어**한다(`SimpleRateLimiter`, 기존 구현 재사용). 실제 분당 요청 한도(RPM) 기반 제어나 분산 환경에서의 공유는 다루지 않는다.
- 이 PR은 `evaluate_cluster`/`evaluate_newsletter` 노드의 judge 입력(기사 제목+본문 500자 미리보기)을 바꾸지 않았다 - "judge-v2(기사 본문 포함)"는 과제 정의상 별도 PR이다.
