import json

import pytest

from sim.run import main


def run_report(tmp_path, *extra):
    out = tmp_path / "report.json"
    assert main(["--target", "fake", "--users", "20", "--days", "3", "--seed", "3", "--out", str(out), *extra]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_fake_run_writes_report_with_all_metric_sections(tmp_path):
    rep = run_report(tmp_path, "--policy", "static_batch")
    m = rep["metrics"]
    assert set(m) >= {"cold_start", "reactivity", "drift", "serving", "errors", "engagement"}
    assert m["n_users"] == 20
    assert m["errors"]["error_rate"] == 0.0
    assert rep["click_model"]["name"] == "default"
    assert "calibration" not in rep


def test_calibrate_flag_refits_bias_on_the_run_catalog(tmp_path):
    rep = run_report(tmp_path, "--preset", "category_only", "--calibrate")
    cal = rep["calibration"]
    assert cal["random_ctr"] == pytest.approx(cal["target_random_ctr"], rel=1e-3)
    assert rep["click_model"]["bias"] == cal["bias"]
    assert rep["click_model"]["weights"]["keyword"] == 0.0
