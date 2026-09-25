import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.llm_client import extract_json_from_response


def test_extract_json_repairs_escaped_quotes_next_to_raw_newline():
    """감사(audit) 발견 버그: _repair_json_string의 이스케이프 감지가
    ch == '\\\\'(단일 문자를 2글자 문자열과 비교, 항상 False)로 되어 있어
    문자열 안의 '\\' 이스케이프를 전혀 인식하지 못했다. 이 때문에 이미
    올바르게 이스케이프된 따옴표(\\") 뒤에 오는 문자에 따라 휴리스틱이
    문자열을 엉뚱한 위치에서 잘못 종료시킬 수 있었다.

    이 입력은 (1) 실제 개행문자가 포함돼 있어 repair 경로를 타야 하고,
    (2) 이미 올바르게 이스케이프된 `\\"wow,\\"` 바로 뒤에 쉼표가 와서
    버그가 있으면 그 지점에서 문자열이 조기 종료된다.
    """
    raw = '{"content": "line one\nHe said \\"wow,\\" and left."}'

    result = extract_json_from_response(raw)

    assert result == {"content": 'line one\nHe said "wow," and left.'}


def test_extract_json_still_handles_plain_valid_json():
    raw = '{"a": 1, "b": "two"}'
    assert extract_json_from_response(raw) == {"a": 1, "b": "two"}


def test_extract_json_returns_none_for_empty_content():
    assert extract_json_from_response("") is None
    assert extract_json_from_response(None) is None
