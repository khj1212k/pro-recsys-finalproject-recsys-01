import json

import pytest

from sim.run import main, redact_credentials


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


def test_report_does_not_store_credentials_from_url_arguments(tmp_path):
    # --day-end-cmd is recorded with the other arguments (it is only executed for http targets)
    cmd = "python -m sim.seed --database-url postgresql://loaduser:s3cretpw@127.0.0.1:5434/newsletter_load batches"
    rep = run_report(tmp_path, "--policy", "static_batch", "--day-end-cmd", cmd)

    assert "s3cretpw" not in (tmp_path / "report.json").read_text(encoding="utf-8")
    assert rep["args"]["day_end_cmd"] == (
        "python -m sim.seed --database-url postgresql://***@127.0.0.1:5434/newsletter_load batches")
    assert rep["args"]["policy"] == "static_batch" and rep["args"]["seed"] == 3  # other arguments untouched


def test_redaction_only_touches_url_credentials():
    assert redact_credentials("http://u:p@host:8100/x?a=b") == "http://***@host:8100/x?a=b"
    assert redact_credentials("http://127.0.0.1:8100") == "http://127.0.0.1:8100"
    assert redact_credentials('python -m sim.seed --database-url "$LOAD_DB_URL" batches') == (
        'python -m sim.seed --database-url "$LOAD_DB_URL" batches')
    assert redact_credentials(7) == 7 and redact_credentials(None) is None
