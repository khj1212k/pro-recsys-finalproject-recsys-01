# LLM 킬 스위치
# - 예정된 비용 가드(cron)가 실제 Google Cloud 과금이 시작되는 걸 감지하면 이
#   스위치를 켠다(env LLM_KILL_SWITCH 또는 Settings.LLM_KILL_SWITCH_FILE 파일 생성).
# - core/llm/adapters.py의 모든 LLMClient.complete() 구현체는 실제 프로바이더에
#   네트워크 요청을 보내기 전에 반드시 check_kill_switch()를 제일 먼저 호출해야 한다.

import logging
import os
from typing import Optional

from core.llm.client import LLMResult, LLMUsage
from core.llm_metrics import get_metrics_collector
from config.settings import Settings

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes"}


def _kill_switch_reason() -> Optional[str]:
    """킬 스위치가 켜져 있으면 사람이 읽을 수 있는 사유 문자열을, 아니면 None을 반환한다.

    env는 매 호출마다 os.getenv로 직접 읽는다(런타임에 켜지면 다음 호출부터 즉시
    반영되어야 하므로 Settings에 캐싱하지 않음). 파일 경로만 Settings에서 가져온다
    (기본값은 저장소 루트 기준 <repo root>/.ops/LLM_KILL_SWITCH, config/settings.py 참고).
    """
    env_value = os.getenv("LLM_KILL_SWITCH", "")
    if env_value.strip().lower() in _TRUE_VALUES:
        return f"env LLM_KILL_SWITCH={env_value!r}"

    kill_file = Settings.LLM_KILL_SWITCH_FILE
    if kill_file and os.path.exists(kill_file):
        return f"file {kill_file}"

    return None


def check_kill_switch(provider: str, model: str, purpose: str) -> Optional[LLMResult]:
    """킬 스위치가 켜져 있으면 프로바이더 호출 없이 LLMResult를 반환하고,
    꺼져 있으면 None을 반환한다(호출부는 평소대로 네트워크 호출을 진행).

    반환된 LLMResult는 attempts=0(실제 시도가 없었음), error="kill_switch"로
    호출부가 "이 실패는 재시도해도 소용없다"는 걸 구분할 수 있게 한다.
    """
    reason = _kill_switch_reason()
    if reason is None:
        return None

    logger.error(
        "%s/%s: LLM kill switch가 활성화되어 호출을 차단했습니다 (%s, purpose=%s)",
        provider, model, reason, purpose,
    )
    get_metrics_collector().record_call(
        purpose=purpose, input_tokens=0, output_tokens=0,
        latency_seconds=0.0, success=False,
        provider=provider, model=model, error_type="kill_switch",
    )
    return LLMResult(
        text=None, parsed=None, usage=LLMUsage(), latency_s=0.0,
        attempts=0, provider=provider, model=model, error="kill_switch",
    )
