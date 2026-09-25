# LLM 클라이언트 레지스트리
# - role(generator/judge/tone) -> provider/model을 환경변수로 해석
# - (provider, model) 조합당 인스턴스 1개, 프로바이더당 레이트리미터 1개를 공유한다
#   (기존 코드는 LangGraph 노드가 호출될 때마다 클라이언트를 새로 만들었다 - ADR 0005)

import logging
import os
import threading
from typing import Dict, Tuple

from core.llm.adapters import HyperCLOVALLMClient, OpenAICompatLLMClient
from core.llm.client import LLMClient
from core.llm_client import SimpleRateLimiter

logger = logging.getLogger(__name__)

ROLES = {"generator", "judge", "tone"}

_ROLE_ENV_PREFIX = {
    "generator": "GEN",
    "judge": "JUDGE",
    "tone": "TONE",
}

# provider별 기본 모델은 "동작은 하는" 안전한 폴백일 뿐, 최종 모델 선정은
# bake-off ADR(추후)로 미룬다. 운영에서는 GEN_MODEL/JUDGE_MODEL/TONE_MODEL을
# 명시적으로 설정하는 것을 권장한다.
PROVIDER_CONFIG = {
    "openai": {
        "base_url": None,  # openai 라이브러리 기본값 사용
        "api_key_env": "OPENAI_API_KEY",
        "supports_json_schema": True,
        "default_model": "gpt-4o-mini",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "supports_json_schema": True,
        # role별 기본값(ROLE_DEFAULT_MODEL)이 없는 경우의 범용 폴백. role별 기본값이
        # 실제로는 항상 우선 적용되므로(GEN/JUDGE/TONE 전부 gemini가 기본 프로바이더),
        # 이 값은 "GEN_PROVIDER=gemini인데 GEN_MODEL만 없는" 것처럼 role 기본
        # 프로바이더가 아닌 경로로 gemini를 쓸 때만 쓰인다.
        "default_model": "gemini-3.5-flash-lite",
    },
    "upstage": {
        "base_url": "https://api.upstage.ai/v1",
        "api_key_env": "UPSTAGE_API_KEY",
        "supports_json_schema": True,
        "default_model": "solar-pro3",
    },
    "naver": {
        "legacy": True,
        "default_model": "HCX-003",
    },
}

# 역할별 기본 프로바이더. 사용자가 지금 보유한 키는 Gemini(Google Cloud 크레딧)뿐이라
# (docs/adr/0005 "결과와 한계") GEN/JUDGE/TONE 모두 gemini를 기본값으로 둔다.
# LLM-as-judge 요건("judge != generator")은 ROLE_DEFAULT_MODEL로 다른 모델을 써서
# 최소한 충족하되, 같은 벤더라는 점 자체는 get_client()가 WARNING으로 알린다.
ROLE_DEFAULT_PROVIDER = {
    "generator": "gemini",
    "judge": "gemini",
    "tone": "gemini",
}

# role별 기본 모델(role이 위 ROLE_DEFAULT_PROVIDER를 그대로 쓸 때만 적용 - 다른
# 프로바이더로 오버라이드하면 그 프로바이더의 PROVIDER_CONFIG.default_model을 쓴다).
# 모델 id는 공식 문서(https://ai.google.dev/gemini-api/docs/models, 접근일
# 2026-09-25)에서 확인한 현재 유효한 id다 - 상세 근거는 docs/adr/0005 참고.
ROLE_DEFAULT_MODEL = {
    "generator": "gemini-3.5-flash-lite",  # 초안 생성 - "가장 빠르고 비용 효율적인 3.5 모델"
    "judge": "gemini-3.5-flash",  # 평가 - generator보다 한 단계 위 모델(Flash-Lite가 아닌 Flash)
    "tone": "gemini-3.5-flash-lite",  # 문체 변환 - 생성과 비슷한 비용 프로필의 가벼운 재작성 작업
}

_instances: Dict[Tuple[str, str], LLMClient] = {}
_instances_lock = threading.Lock()

_rate_limiters: Dict[str, SimpleRateLimiter] = {}
_rate_limiters_lock = threading.Lock()


def get_provider_rate_limiter(provider: str) -> SimpleRateLimiter:
    """프로바이더 하나당 레이트리미터 하나를 공유한다(같은 프로바이더의 여러 모델도 공유)."""
    with _rate_limiters_lock:
        if provider not in _rate_limiters:
            min_interval = float(
                os.getenv(f"{provider.upper()}_LLM_MIN_INTERVAL", os.getenv("LLM_MIN_INTERVAL", "0.0"))
            )
            _rate_limiters[provider] = SimpleRateLimiter(min_interval)
        return _rate_limiters[provider]


def resolve_role_config(role: str) -> Tuple[str, str]:
    """role -> (provider, model). 환경변수 <PREFIX>_PROVIDER/<PREFIX>_MODEL로 오버라이드한다."""
    role = role.lower()
    if role not in ROLES:
        raise ValueError(f"알 수 없는 LLM 역할: {role!r} (허용: {sorted(ROLES)})")

    prefix = _ROLE_ENV_PREFIX[role]
    default_provider = ROLE_DEFAULT_PROVIDER[role]
    provider = os.getenv(f"{prefix}_PROVIDER", default_provider).lower()

    if provider not in PROVIDER_CONFIG:
        raise ValueError(
            f"{prefix}_PROVIDER={provider!r}는 알 수 없는 프로바이더입니다 "
            f"(허용: {sorted(PROVIDER_CONFIG)})"
        )

    # role이 자기 기본 프로바이더를 그대로 쓰면 role별 기본 모델(ROLE_DEFAULT_MODEL)을
    # 우선한다(judge가 generator와 다른 모델을 쓰게 하려는 것). 다른 프로바이더로
    # 오버라이드했다면 그 프로바이더의 범용 기본 모델로 폴백한다.
    if provider == default_provider and role in ROLE_DEFAULT_MODEL:
        default_model = ROLE_DEFAULT_MODEL[role]
    else:
        default_model = PROVIDER_CONFIG[provider]["default_model"]
    model = os.getenv(f"{prefix}_MODEL", default_model)
    return provider, model


def _model_family(provider: str) -> str:
    """provider를 '모델 계열'로 정규화한다.

    같은 프로바이더(벤더)를 쓰면 모델 크기/세대가 달라도(judge가 generator보다
    작은/큰 모델을 써도) 같은 학습 lineage를 공유하는 self-preference bias 위험이
    있다고 보고, 프로바이더 단위로 계열을 나눈다.
    """
    return provider


def _build_client(provider: str, model: str) -> LLMClient:
    cfg = PROVIDER_CONFIG[provider]

    if cfg.get("legacy"):
        return HyperCLOVALLMClient(model=model)

    api_key = os.getenv(cfg["api_key_env"], "")
    if not api_key:
        raise ValueError(f"{cfg['api_key_env']} 환경변수가 설정되지 않았습니다 ({provider} 프로바이더)")

    return OpenAICompatLLMClient(
        provider=provider,
        model=model,
        base_url=cfg["base_url"],
        api_key=api_key,
        supports_json_schema=cfg["supports_json_schema"],
        rate_limiter=get_provider_rate_limiter(provider),
    )


def get_client(role: str) -> LLMClient:
    """role(generator/judge/tone)에 대한 LLMClient를 반환한다.

    같은 (provider, model) 조합이면 항상 같은 인스턴스를 재사용한다(레이트리미터도
    프로바이더 단위로 공유된다).
    """
    provider, model = resolve_role_config(role)

    if role == "judge":
        gen_provider, gen_model = resolve_role_config("generator")
        if _model_family(provider) == _model_family(gen_provider):
            logger.warning(
                "JUDGE(%s/%s)가 GEN(%s/%s)와 같은 모델 계열(%s)입니다. "
                "모델이 서로 달라도 같은 벤더면 LLM-as-judge에서 자기선호편향"
                "(self-preference bias) 위험이 있으니, 최종 프로바이더/모델 선정 시"
                "(bake-off ADR) 서로 다른 계열을 쓰는 것을 권장합니다.",
                provider, model, gen_provider, gen_model, _model_family(provider),
            )

    key = (provider, model)
    with _instances_lock:
        if key not in _instances:
            _instances[key] = _build_client(provider, model)
        return _instances[key]


def reset_registry() -> None:
    """테스트 전용: 캐시된 클라이언트/레이트리미터를 모두 초기화한다."""
    with _instances_lock:
        _instances.clear()
    with _rate_limiters_lock:
        _rate_limiters.clear()
