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
        "default_model": "gemini-2.5-flash",
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

# 역할별 기본 프로바이더. judge 기본값(openai)은 generator 기본값(gemini)과
# 다른 모델 계열이어야 한다는 요건(LLM-as-judge)을 기본 설정만으로 만족시킨다.
ROLE_DEFAULT_PROVIDER = {
    "generator": "gemini",
    "judge": "openai",
    "tone": "upstage",
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
    provider = os.getenv(f"{prefix}_PROVIDER", ROLE_DEFAULT_PROVIDER[role]).lower()

    if provider not in PROVIDER_CONFIG:
        raise ValueError(
            f"{prefix}_PROVIDER={provider!r}는 알 수 없는 프로바이더입니다 "
            f"(허용: {sorted(PROVIDER_CONFIG)})"
        )

    default_model = PROVIDER_CONFIG[provider]["default_model"]
    model = os.getenv(f"{prefix}_MODEL", default_model)
    return provider, model


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
        gen_provider, _ = resolve_role_config("generator")
        if provider == gen_provider:
            logger.warning(
                "JUDGE_PROVIDER(%s)가 GEN_PROVIDER(%s)와 같은 모델 계열입니다. "
                "LLM-as-judge는 생성기와 다른 모델 계열을 쓰는 것을 권장합니다.",
                provider, gen_provider,
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
