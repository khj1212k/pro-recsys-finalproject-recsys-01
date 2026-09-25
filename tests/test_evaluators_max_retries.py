import sys
import os
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator
from config.settings import Settings


def test_cluster_evaluator_stops_after_max_json_parse_retries():
    evaluator = ClusterEvaluator.__new__(ClusterEvaluator)
    # LLM이 계속 파싱 불가능한 응답(빈 문자열이 아닌, JSON이 아닌 텍스트)을 반환한다고 가정
    evaluator.client = MagicMock()
    evaluator.client.chat_completion.return_value = "이것은 JSON이 아닙니다"

    result = evaluator.evaluate([{"title": "a"}, {"title": "b"}])

    # 무한(1,000,000회)이 아니라 설정된 상한만큼만 호출되어야 함
    assert evaluator.client.chat_completion.call_count == Settings.MAX_JSON_PARSE_RETRIES
    assert result["decision"] in ("PASS", "FAIL")


def test_newsletter_evaluator_stops_after_max_json_parse_retries():
    evaluator = NewsletterEvaluator.__new__(NewsletterEvaluator)
    evaluator.client = MagicMock()
    evaluator.client.chat_completion.return_value = "이것은 JSON이 아닙니다"

    result = evaluator.evaluate(
        {"title": "t", "content": "c", "sentence": "s"}, [{"title": "a", "press_name": "p"}]
    )

    assert evaluator.client.chat_completion.call_count == Settings.MAX_JSON_PARSE_RETRIES
    assert result["decision"] in ("PASS", "FAIL")
