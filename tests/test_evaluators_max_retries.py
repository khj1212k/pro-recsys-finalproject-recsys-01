# NOTE: core/llm/ 도입(ADR 0005) 이전에는 ClusterEvaluator/NewsletterEvaluator가
# 직접 재시도 루프를 돌리며 Settings.MAX_JSON_PARSE_RETRIES를 상한으로 썼다.
# 이제 재시도/백오프는 LLMClient.complete()(core/llm/adapters.py) 내부로 옮겨졌고,
# 그 쪽의 유한 재시도 상한 테스트는 tests/test_llm_v2_retry_backoff.py와
# tests/test_llm_v2_json_mode_fallback.py에 있다. 여기서는 평가기가 client.complete()를
# "한 번만" 호출하고, 그 결과(parsed/text/error)를 올바른 decision 딕셔너리로
# 매핑하는지를 검증한다.
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

from workflow.evaluators import ClusterEvaluator, NewsletterEvaluatorV1
from core.llm.schemas import ClusterEval, NewsletterEval


def test_cluster_evaluator_calls_llm_client_exactly_once_and_uses_parsed_result():
    parsed = ClusterEval(decision="PASS", confidence=0.8, summary="s", feedback="", outlier_indices=[], sub_groups=[])
    fake = FakeLLMClient(results=[{"parsed": parsed}])
    evaluator = ClusterEvaluator(llm_client=fake)

    result = evaluator.evaluate([{"title": "a"}, {"title": "b"}])

    assert fake.call_count == 1
    assert result["decision"] == "PASS"
    assert result["confidence"] == 0.8


def test_cluster_evaluator_falls_back_to_fail_when_llm_client_exhausts_retries():
    # LLMClient.complete()가 (내부적으로 유한 재시도 후) parsed=None, text=None, error="..."로
    # 반환하는 상황을 흉내낸다 - evaluator는 여기서 다시 재시도하지 않고 FAIL로 폴백해야 한다.
    fake = FakeLLMClient(results=[{"parsed": None, "text": None, "error": "max retries exhausted"}])
    evaluator = ClusterEvaluator(llm_client=fake)

    result = evaluator.evaluate([{"title": "a"}, {"title": "b"}])

    assert fake.call_count == 1
    assert result["decision"] == "FAIL"
    assert result["feedback"] == "LLM response empty"


def test_cluster_evaluator_never_derives_a_decision_from_raw_text():
    # 레거시 어댑터는 파싱에 실패해도 text를 돌려줄 수 있다. 그 text의 "PASS"/"FAIL" 낱말로
    # 클러스터를 통과시키던 휴리스틱은 없앴다 - 파싱 실패는 FAIL이고 parsed=False다.
    # (docs/adr/0010 Addendum 2026-10-18)
    for text in ("결과: FAIL 입니다", "결과: PASS 입니다", "PASS and FAIL"):
        fake = FakeLLMClient(results=[{"parsed": None, "text": text}])

        result = ClusterEvaluator(llm_client=fake).evaluate([{"title": "a"}, {"title": "b"}])

        assert fake.call_count == 1
        assert result["decision"] == "FAIL"
        assert result["parsed"] is False
        assert result["confidence"] == 0.0
        assert result["feedback"] == "JSON parsing failed"
        assert "llm_failure" not in result  # 형식 위반은 모델 쪽 실패이고 호출이 막힌 것이 아니다


def test_cluster_evaluator_marks_parsed_true_for_a_schema_valid_answer():
    parsed = ClusterEval(decision="PASS", confidence=0.8)
    fake = FakeLLMClient(results=[{"parsed": parsed}])

    result = ClusterEvaluator(llm_client=fake).evaluate([{"title": "a"}, {"title": "b"}])

    assert result["parsed"] is True
    assert "llm_failure" not in result


def test_cluster_evaluator_passes_schema_and_purpose_to_llm_client():
    parsed = ClusterEval(decision="FAIL", confidence=0.1)
    fake = FakeLLMClient(results=[{"parsed": parsed}])
    evaluator = ClusterEvaluator(llm_client=fake)

    evaluator.evaluate([{"title": "a"}, {"title": "b"}])

    call = fake.calls[0]
    assert call["schema"] is ClusterEval
    assert call["purpose"] == "cluster_eval"


def test_newsletter_evaluator_v1_calls_llm_client_exactly_once_and_uses_parsed_result():
    parsed = NewsletterEval(decision="PASS", score=8, feedback="", issues=[])
    fake = FakeLLMClient(results=[{"parsed": parsed}])
    evaluator = NewsletterEvaluatorV1(llm_client=fake)

    result = evaluator.evaluate(
        {"title": "t", "content": "c", "sentence": "s"}, [{"title": "a", "press_name": "p"}]
    )

    assert fake.call_count == 1
    assert result["decision"] == "PASS"
    assert result["score"] == 8


def test_newsletter_evaluator_v1_falls_back_to_fail_when_llm_client_exhausts_retries():
    fake = FakeLLMClient(results=[{"parsed": None, "text": None, "error": "max retries exhausted"}])
    evaluator = NewsletterEvaluatorV1(llm_client=fake)

    result = evaluator.evaluate(
        {"title": "t", "content": "c", "sentence": "s"}, [{"title": "a", "press_name": "p"}]
    )

    assert fake.call_count == 1
    assert result["decision"] == "FAIL"
    assert result["issues"] == ["API call returned empty"]


def test_newsletter_evaluator_v1_passes_schema_and_purpose_to_llm_client():
    parsed = NewsletterEval(decision="FAIL", score=1)
    fake = FakeLLMClient(results=[{"parsed": parsed}])
    evaluator = NewsletterEvaluatorV1(llm_client=fake)

    evaluator.evaluate({"title": "t", "content": "c", "sentence": "s"}, [{"title": "a", "press_name": "p"}])

    call = fake.calls[0]
    assert call["schema"] is NewsletterEval
    assert call["purpose"] == "newsletter_eval"
