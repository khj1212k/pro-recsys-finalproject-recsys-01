# core/llm/ 도입(ADR 0005) 전에는 ToneConverter.convert()가 validate_conversion()
# 실패 시 최대 5회까지 재생성을 시도한 뒤에야 결정론적 softener로 폴백했다. 리팩터링
# 과정에서 이 루프가 통째로 사라지고 1회 호출 후 검증 실패 시 곧바로 폴백하게
# 됐었다 - 여기서는 그 콘텐츠 검증 재시도를 (Settings로 조절 가능한) 유한 횟수로
# 복원한다. 전송 계층(429/5xx/timeout) 재시도는 여전히 client.complete() 내부의
# 책임이며, 이 재시도는 오직 "응답은 왔는데 validate_conversion()이 거부한 경우"만
# 다룬다.
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

from core.tone_converter import ToneConverter
from core.llm.schemas import ToneResult
from config.settings import Settings


NEWSLETTER = {
    "title": "삼성전자 HBM 증설",
    "summary": "삼성전자가 HBM 생산을 확대한다.",
    "content": "삼성전자가 HBM 생산을 확대하기로 했다.",
    "keywords": ["삼성전자", "HBM"],
}


def test_retries_once_after_validation_failure_then_succeeds():
    # 1차: content가 빈 문자열이라 validate_conversion()이 거부 -> 재시도
    # 2차: 정상 -> 반환
    fake = FakeLLMClient(results=[
        {"parsed": ToneResult(title="", summary="", content="", keywords=[])},
        {"parsed": ToneResult(
            title="📰 삼성전자 HBM 증설",
            summary="삼성전자가 HBM을 늘려요 ✅",
            content="📰 삼성전자가 HBM 생산을 확대해요.",
            keywords=["삼성전자", "HBM"],
        )},
    ])
    converter = ToneConverter(llm_client=fake)

    result = converter.convert(NEWSLETTER)

    assert fake.call_count == 2
    assert result["title"] == "📰 삼성전자 HBM 증설"


def test_falls_back_to_deterministic_softener_after_exhausting_bounded_retries(monkeypatch):
    monkeypatch.setattr(Settings, "MAX_RETRY_TONE_VALIDATION", 2)
    # 최초 1회 + 추가 2회 = 총 3회 모두 검증 실패
    fake = FakeLLMClient(results=[
        {"parsed": ToneResult(title="", summary="", content="", keywords=[])},
        {"parsed": ToneResult(title="", summary="", content="", keywords=[])},
        {"parsed": ToneResult(title="", summary="", content="", keywords=[])},
    ])
    converter = ToneConverter(llm_client=fake)

    result = converter.convert(NEWSLETTER)

    assert fake.call_count == 3
    # 폴백은 결정론적 softener를 거친 원본 기반 결과여야 한다
    assert result["title"].startswith("📰")
    assert "삼성전자" in result["content"]


def test_retry_bound_is_configurable_via_settings(monkeypatch):
    monkeypatch.setattr(Settings, "MAX_RETRY_TONE_VALIDATION", 0)
    # 추가 재시도가 0이면 최초 1회만 호출하고 바로 폴백해야 한다
    fake = FakeLLMClient(results=[
        {"parsed": ToneResult(title="", summary="", content="", keywords=[])},
    ])
    converter = ToneConverter(llm_client=fake)

    result = converter.convert(NEWSLETTER)

    assert fake.call_count == 1
    assert result["title"].startswith("📰")


def test_does_not_retry_when_first_attempt_already_passes_validation():
    fake = FakeLLMClient(results=[
        {"parsed": ToneResult(
            title="📰 삼성전자 HBM 증설",
            summary="삼성전자가 HBM을 늘려요 ✅",
            content="📰 삼성전자가 HBM 생산을 확대해요.",
            keywords=["삼성전자", "HBM"],
        )},
    ])
    converter = ToneConverter(llm_client=fake)

    result = converter.convert(NEWSLETTER)

    assert fake.call_count == 1
    assert result["title"] == "📰 삼성전자 HBM 증설"
