# judge v2 (ADR 0010): 원문 "본문" 발췌를 보고, 기준별 점수 + 근거 없는 주장 목록을
# 반환하며, PASS/FAIL은 모델이 아니라 코드가 설정 가능한 임계값으로 결정한다.
import sys
import os

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

import workflow.evaluators as evaluators_module
from core.llm.schemas import CriterionScores, NewsletterEvalV2, UnsupportedClaim
from core.reconstruction.generator import ARTICLE_PROMPT_CHARS, MAX_PROMPT_ARTICLES
from workflow.evaluators import NewsletterEvaluator

DRAFT = {"title": "반도체 증설", "sentence": "메모리, 다시 뜰까요?", "content": "삼성전자가 HBM 라인을 늘린다."}


def _eval(f=5, c=5, h=5, s=5, claims=()):
    return NewsletterEvalV2(
        scores=CriterionScores(faithfulness=f, coverage=c, coherence=h, style=s),
        unsupported_claims=[UnsupportedClaim(claim=x, reason="원문에 없음") for x in claims],
        feedback="구체적인 수정 지시",
    )


def _articles(n, body_len=3000):
    return [
        {"id": i, "title": f"제목{i}", "press_name": "테스트일보",
         "content": (f"[{i}번 기사 시작]" + "가" * body_len)[: body_len + i]}
        for i in range(n)
    ]


def _prompt(fake):
    return "\n".join(m["content"] for m in fake.calls[0]["messages"])


def test_judge_sees_truncated_article_bodies_not_only_titles():
    fake = FakeLLMClient(results=[{"parsed": _eval()}])
    arts = _articles(2)
    arts[0]["content"] = "앞부분에 있는 핵심 수치 1조 원. " + "나" * 5000 + " 잘려야 하는 꼬리"

    NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, arts)

    prompt = _prompt(fake)
    assert "앞부분에 있는 핵심 수치 1조 원" in prompt
    assert "잘려야 하는 꼬리" not in prompt


def test_judge_sees_the_same_article_subset_as_the_generator():
    fake = FakeLLMClient(results=[{"parsed": _eval()}])
    arts = _articles(MAX_PROMPT_ARTICLES + 3, body_len=100)

    NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, arts)

    prompt = _prompt(fake)
    # 생성기는 본문이 긴 순서로 MAX_PROMPT_ARTICLES개만 본다 - 가장 짧은 3개(0,1,2번)는 빠진다
    for i in range(3):
        assert f"[{i}번 기사 시작]" not in prompt
    for i in range(3, MAX_PROMPT_ARTICLES + 3):
        assert f"[{i}번 기사 시작]" in prompt


def test_pass_requires_every_criterion_at_threshold_and_no_unsupported_claims(monkeypatch):
    monkeypatch.setattr(evaluators_module.Settings, "JUDGE_MIN_CRITERION_SCORE", 3)
    monkeypatch.setattr(evaluators_module.Settings, "JUDGE_MAX_UNSUPPORTED_CLAIMS", 0)

    def run(parsed):
        fake = FakeLLMClient(results=[{"parsed": parsed}])
        return NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, _articles(2))

    assert run(_eval(3, 3, 3, 3))["decision"] == "PASS"
    assert run(_eval(5, 5, 2, 5))["decision"] == "FAIL"
    assert run(_eval(5, 5, 5, 5, claims=["1조 원"]))["decision"] == "FAIL"


def test_thresholds_are_configurable_for_later_calibration(monkeypatch):
    monkeypatch.setattr(evaluators_module.Settings, "JUDGE_MIN_CRITERION_SCORE", 2)
    monkeypatch.setattr(evaluators_module.Settings, "JUDGE_MAX_UNSUPPORTED_CLAIMS", 1)
    fake = FakeLLMClient(results=[{"parsed": _eval(5, 5, 2, 5, claims=["x"])}])

    result = NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, _articles(2))

    assert result["decision"] == "PASS"
    assert result["thresholds"] == {"min_criterion_score": 2, "max_unsupported_claims": 1}


def test_result_exposes_criteria_claims_and_a_derived_0_to_10_score():
    fake = FakeLLMClient(results=[{"parsed": _eval(5, 3, 4, 4, claims=["지어낸 인용"])}])

    result = NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, _articles(2))

    assert result["judge_version"] == "v2"
    assert result["criteria"] == {"faithfulness": 5, "coverage": 3, "coherence": 4, "style": 4}
    assert result["unsupported_claims"] == [{"claim": "지어낸 인용", "reason": "원문에 없음"}]
    assert result["score"] == pytest.approx(7.5)
    # 재생성 피드백이 근거 없는 주장을 구체적으로 짚어야 한다
    assert "지어낸 인용" in result["feedback"]
    assert "지어낸 인용" in " ".join(result["issues"])


def test_out_of_range_scores_are_clamped_to_1_5():
    parsed = NewsletterEvalV2(scores=CriterionScores(faithfulness=9, coverage=0, coherence=3, style=3))
    assert parsed.scores.faithfulness == 5
    assert parsed.scores.coverage == 1


def test_unparseable_judge_output_fails_closed():
    fake = FakeLLMClient(results=[{"parsed": None, "text": "PASS 입니다"}])

    result = NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, _articles(2))

    assert result["decision"] == "FAIL"


def test_schema_and_purpose():
    fake = FakeLLMClient(results=[{"parsed": _eval()}])
    NewsletterEvaluator(llm_client=fake).evaluate(DRAFT, _articles(2))
    assert fake.calls[0]["schema"] is NewsletterEvalV2
    assert fake.calls[0]["purpose"] == "newsletter_eval"


def test_shadow_mode_records_a_fail_but_does_not_block(monkeypatch):
    import workflow.nodes as nodes_module

    fail = {"decision": "FAIL", "score": 2.0, "feedback": "f", "issues": []}
    state = {"newsletter_eval": fail, "newsletter_retry_count": 1}

    monkeypatch.setattr(nodes_module.Settings, "JUDGE_GATE_MODE", "enforce")
    assert nodes_module.route_after_newsletter_eval(state) == "retry"

    monkeypatch.setattr(nodes_module.Settings, "JUDGE_GATE_MODE", "shadow")
    assert nodes_module.route_after_newsletter_eval(state) == "pass"


def test_article_prompt_chars_is_shared_with_generator():
    from core.reconstruction.generator import NewsReconstructor

    long_body = "다" * (ARTICLE_PROMPT_CHARS + 50)
    text = NewsReconstructor(llm_client=FakeLLMClient())._build_articles_text(
        [{"title": "t", "press_name": "p", "content": long_body}]
    )
    assert "다" * ARTICLE_PROMPT_CHARS in text
    assert "다" * (ARTICLE_PROMPT_CHARS + 1) not in text
