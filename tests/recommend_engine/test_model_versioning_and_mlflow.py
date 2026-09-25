import json
import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


def _train_tiny_booster():
    """실제 lgb.Booster를 하나 만든다(순수 pickle mock 대신) - save_model()/Booster(model_file=)가
    실제 LightGBM 네이티브 포맷으로 왕복되는지 검증하기 위함(CHANGE #17)."""
    from src.models.lgbm_ranker import LGBMRanker

    rng = np.random.RandomState(0)
    rows = []
    for uid in range(4):
        for j in range(3):
            rows.append({
                "user_id": uid, "news_id": j, "label": 1 if j == 0 else 0,
                "_timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=uid),
                "f0": rng.rand(), "f1": rng.rand(),
            })
    df = pd.DataFrame(rows)

    ranker = LGBMRanker(params={"objective": "lambdarank", "metric": "ndcg", "verbose": -1})
    ranker.num_boost_round = 5
    ranker.early_stopping_rounds = 3
    ranker.train(df)
    return ranker.model


def test_save_model_version_writes_native_text_format_and_pointer(tmp_path):
    """CHANGE #17: pickle이 아니라 LightGBM 네이티브 텍스트 포맷(.txt, booster.save_model())으로 저장한다."""
    from main_lgbm import save_model_version, load_latest_model_path, load_booster

    booster = _train_tiny_booster()
    model_path = save_model_version(str(tmp_path), booster, version="20260702_120000")

    assert os.path.exists(model_path)
    assert model_path.endswith("lgbm_model_20260702_120000.txt")

    # pickle이 아니라 LightGBM 텍스트 포맷이어야 함 - 사람이 읽을 수 있는 헤더로 확인
    with open(model_path, encoding="utf-8") as f:
        header = f.read(200)
    assert "tree" in header.lower()

    pointer_path = os.path.join(str(tmp_path), "latest_model.json")
    assert os.path.exists(pointer_path)
    with open(pointer_path) as f:
        pointer = json.load(f)
    assert pointer["version"] == "20260702_120000"
    assert pointer["model_file"] == "lgbm_model_20260702_120000.txt"
    assert pointer["format"] == "lightgbm_text"

    # 포인터를 통해 방금 저장한 모델 경로를 다시 찾고, 실제로 로드해서 예측까지 가능해야 함
    assert load_latest_model_path(str(tmp_path)) == model_path
    loaded = load_booster(model_path)
    assert loaded.predict(pd.DataFrame({"f0": [0.1], "f1": [0.2]})) is not None


def test_save_model_version_does_not_overwrite_previous_version(tmp_path):
    from main_lgbm import save_model_version

    booster = _train_tiny_booster()
    path_a = save_model_version(str(tmp_path), booster, version="v1")
    path_b = save_model_version(str(tmp_path), booster, version="v2")

    assert path_a != path_b
    assert os.path.exists(path_a)  # 이전 버전 파일이 삭제/덮어써지지 않아야 함
    assert os.path.exists(path_b)


def test_load_latest_model_path_raises_clear_error_when_never_trained(tmp_path):
    from main_lgbm import load_latest_model_path
    import pytest

    with pytest.raises(FileNotFoundError):
        load_latest_model_path(str(tmp_path))


def test_load_booster_falls_back_to_pickle_for_legacy_pkl_pointer(tmp_path):
    """CHANGE #17의 사소한 하위 호환: .pkl로 끝나는 예전 포인터는 pickle로 폴백해서 읽는다."""
    import pickle
    from main_lgbm import load_booster

    legacy_path = os.path.join(str(tmp_path), "lgbm_model_legacy.pkl")
    fake_model = {"legacy": True}
    with open(legacy_path, 'wb') as f:
        pickle.dump(fake_model, f)

    assert load_booster(legacy_path) == fake_model


def test_log_mlflow_run_returns_false_when_mlflow_not_installed():
    from main_lgbm import log_mlflow_run

    # 이 테스트 venv에는 mlflow가 실제로 설치되어 있지 않으므로 자연스럽게 ImportError 경로를 탄다
    fake_ranker = MagicMock()
    fake_ranker.model = MagicMock(best_score={})
    config = {"lightgbm": {"params": {"objective": "lambdarank"}, "negative_sample_ratio": 5}, "data": {"validation_ratio": 0.2}}

    result = log_mlflow_run(config, fake_ranker)

    assert result is False


def test_log_mlflow_run_logs_params_and_metrics_when_mlflow_available():
    from main_lgbm import log_mlflow_run

    fake_mlflow = MagicMock()
    fake_mlflow.start_run.return_value.__enter__ = MagicMock(return_value=None)
    fake_mlflow.start_run.return_value.__exit__ = MagicMock(return_value=False)

    fake_ranker = MagicMock()
    fake_ranker.model = MagicMock(best_score={"valid_0": {"ndcg@5": 0.87}})

    config = {
        "lightgbm": {"params": {"objective": "lambdarank", "num_leaves": 31}, "negative_sample_ratio": 5},
        "data": {"validation_ratio": 0.2},
    }

    with patch.dict(sys.modules, {"mlflow": fake_mlflow}):
        result = log_mlflow_run(config, fake_ranker)

    assert result is True
    fake_mlflow.log_params.assert_called_once_with(config["lightgbm"]["params"])
    logged_metric_calls = {c.args[0]: c.args[1] for c in fake_mlflow.log_metric.call_args_list}
    assert logged_metric_calls["valid_0_ndcg@5"] == 0.87
