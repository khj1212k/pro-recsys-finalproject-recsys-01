import json

from sim.experiments import summarize


def write_report(path, policy, preset, coverage, ctr):
    metrics = {section: {} for section in ("cold_start", "reactivity", "drift", "serving", "errors", "engagement")}
    metrics["cold_start"]["first_view_coverage"] = coverage
    metrics["engagement"]["ctr_top_k"] = ctr
    path.write_text(json.dumps({"args": {"policy": policy, "preset": preset}, "metrics": metrics}), encoding="utf-8")
    return str(path)


def test_summarize_groups_seeds_by_policy_and_preset(tmp_path):
    paths = [
        write_report(tmp_path / "a.json", "reactive", "default", 1.0, 0.03),
        write_report(tmp_path / "b.json", "reactive", "default", 1.0, 0.05),
        write_report(tmp_path / "c.json", "static_batch", "default", 0.0, None),
    ]
    s = summarize(paths)
    assert sorted(s) == ["reactive/default", "static_batch/default"]
    assert s["reactive/default"]["n_seeds"] == 2
    assert s["reactive/default"]["engagement.ctr_top_k"] == {"mean": 0.04, "min": 0.03, "max": 0.05}
    assert s["static_batch/default"]["engagement.ctr_top_k"] is None  # all seeds missing -> no summary
    assert s["static_batch/default"]["drift.adapted_rate"] is None
