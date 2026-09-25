import json
import pandas as pd
from datetime import datetime


def test_latest_batch_only_picks_max_created_at():
    from scripts.evaluate_results import _latest_batch_only

    df = pd.DataFrame([
        {"user_id": 1, "news_letter_ids": "[1,2]", "created_at": datetime(2026, 1, 1)},
        {"user_id": 1, "news_letter_ids": "[3,4]", "created_at": datetime(2026, 1, 2)},  # 최신
        {"user_id": 2, "news_letter_ids": "[5]", "created_at": datetime(2026, 1, 2)},   # 최신
    ])
    result = _latest_batch_only(df)
    assert set(result["created_at"]) == {datetime(2026, 1, 2)}
    assert len(result) == 2


def test_expand_news_letter_ids_handles_json_string_and_list():
    from scripts.evaluate_results import _expand_news_letter_ids

    df = pd.DataFrame([
        {"user_id": 1, "news_letter_ids": "[10, 20]"},   # JSON 문자열
        {"user_id": 2, "news_letter_ids": [30, 40, 50]},  # 이미 파싱된 리스트
    ])
    result = _expand_news_letter_ids(df)
    assert len(result) == 5
    assert set(result[result["user_id"] == 1]["news_letter_id"]) == {10, 20}
    assert set(result[result["user_id"] == 2]["news_letter_id"]) == {30, 40, 50}


def test_load_valid_period_reads_saved_json(tmp_path):
    from scripts.evaluate_results import load_valid_period

    period_path = tmp_path / "valid_period.json"
    period_path.write_text(json.dumps({
        "valid_start": "2026-01-20T00:00:00",
        "valid_end": "2026-01-28T00:00:00",
    }))

    result = load_valid_period(str(tmp_path))
    assert result["valid_start"] == datetime(2026, 1, 20)
    assert result["valid_end"] == datetime(2026, 1, 28)


def test_load_valid_period_returns_none_if_missing(tmp_path):
    from scripts.evaluate_results import load_valid_period
    assert load_valid_period(str(tmp_path)) is None
