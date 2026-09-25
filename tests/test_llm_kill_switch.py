# LLM 킬 스위치(core/llm/kill_switch.py) 단위 테스트.
# 예정된 비용 가드(cron)가 실제 Google Cloud 과금 시작을 감지하면 이 스위치를 켠다 -
# env LLM_KILL_SWITCH 또는 Settings.LLM_KILL_SWITCH_FILE 파일 존재 여부로 판단하고,
# 켜져 있으면 어댑터가 실제 프로바이더에 네트워크 요청을 보내기 전에 즉시
# LLMResult(error="kill_switch", attempts=0)를 반환해야 한다.
import sys
import os
from pathlib import Path

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import pytest

from tests.llm_fakes import FakeOpenAIClient, make_response

from core.llm.adapters import OpenAICompatLLMClient, HyperCLOVALLMClient
from core.llm.kill_switch import check_kill_switch
from core.llm.schemas import ClusterEval
from core.llm_metrics import LLMMetricsCollector
from config.settings import Settings


def _fresh_collector():
    collector = LLMMetricsCollector()
    collector.reset()
    return collector


class _ExplodingLegacyClient:
    """kill switch가 제대로 동작하지 않으면 곧바로 실패해 드러나게 만드는 더미."""

    model = "HCX-003"

    def chat_completion(self, **kwargs):
        raise AssertionError("kill switch가 켜져 있는데 레거시 클라이언트가 호출됐다")


@pytest.fixture(autouse=True)
def _isolated_kill_switch(monkeypatch):
    """실제 저장소에 우연히 .ops/LLM_KILL_SWITCH가 있어도 테스트에 새지 않도록,
    기본적으로는 존재하지 않는 경로를 가리키게 하고 env도 지운다."""
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", "/nonexistent/path/LLM_KILL_SWITCH")
    yield


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES"])
def test_env_truthy_values_block_openai_compat_client_before_any_network_call(monkeypatch, value):
    monkeypatch.setenv("LLM_KILL_SWITCH", value)
    fake = FakeOpenAIClient(responses=[])  # 호출되면 FakeChatCompletions가 AssertionError를 던짐
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error == "kill_switch"
    assert result.attempts == 0
    assert result.parsed is None
    assert result.text is None
    assert fake.chat.completions.call_count == 0


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
def test_env_falsy_values_do_not_block(monkeypatch, value):
    monkeypatch.setenv("LLM_KILL_SWITCH", value)
    parsed = ClusterEval(decision="PASS", confidence=0.9)
    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=parsed)])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error is None
    assert fake.chat.completions.call_count == 1


def test_file_kill_switch_blocks_openai_compat_client_before_any_network_call(tmp_path, monkeypatch):
    kill_file = tmp_path / "LLM_KILL_SWITCH"
    kill_file.write_text("cost guard tripped")
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(kill_file))

    fake = FakeOpenAIClient(responses=[])
    client = OpenAICompatLLMClient(provider="gemini", model="gemini-3.5-flash-lite", client=fake)

    result = client.complete(
        [{"role": "user", "content": "x"}], schema=ClusterEval, purpose="newsletter_content_gen"
    )

    assert result.error == "kill_switch"
    assert result.attempts == 0
    assert fake.chat.completions.call_count == 0


def test_missing_kill_switch_file_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "does-not-exist" / "LLM_KILL_SWITCH")
    )
    parsed = ClusterEval(decision="PASS", confidence=0.9)
    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=parsed)])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error is None


def test_env_kill_switch_blocks_legacy_hyperclova_client_before_any_network_call(monkeypatch):
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    client = HyperCLOVALLMClient(legacy_client=_ExplodingLegacyClient())

    result = client.complete([{"role": "user", "content": "x"}], purpose="cluster_eval")

    assert result.error == "kill_switch"
    assert result.attempts == 0


def test_kill_switch_records_failure_in_metrics_collector(monkeypatch):
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    collector = _fresh_collector()

    fake = FakeOpenAIClient(responses=[])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)
    client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert len(collector.calls) == 1
    record = collector.calls[0]
    assert record.success is False
    assert record.error_type == "kill_switch"
    assert record.provider == "openai"
    assert record.model == "gpt-4o-mini"
    assert record.input_tokens == 0
    assert record.output_tokens == 0


def test_check_kill_switch_logs_one_error_line(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")

    with caplog.at_level(logging.ERROR, logger="core.llm.kill_switch"):
        result = check_kill_switch("openai", "gpt-4o-mini", "cluster_eval")

    assert result is not None
    kill_switch_records = [r for r in caplog.records if r.name == "core.llm.kill_switch"]
    assert len(kill_switch_records) == 1
    assert kill_switch_records[0].levelname == "ERROR"


def test_default_kill_switch_file_path_is_relative_to_repo_root_not_cwd():
    """기본값은 config/settings.py의 위치를 기준으로 계산되어야 하고, pytest를
    어느 디렉터리에서 실행하든(CWD와 무관하게) 항상 이 저장소를 가리켜야 한다."""
    import config.settings as settings_module

    repo_root = Path(__file__).resolve().parents[1]  # tests/ -> 저장소 루트
    expected = str(repo_root / ".ops" / "LLM_KILL_SWITCH")

    assert settings_module.BaseSettings.LLM_KILL_SWITCH_FILE == expected
