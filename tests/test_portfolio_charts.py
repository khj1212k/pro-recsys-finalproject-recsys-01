"""scripts/portfolio_charts.py: 커밋된 SVG가 커밋된 리포트 JSON에서 그대로 다시 만들어지는지 확인한다.

차트의 수치는 reports/의 JSON에서만 읽으므로, 리포트를 바꾸고 SVG를 다시 만들지 않으면 여기서 실패한다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _charts():
    spec = importlib.util.spec_from_file_location("portfolio_charts_under_test", REPO / "scripts" / "portfolio_charts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_svgs_match_a_fresh_build():
    charts = _charts()
    for name, body in charts.build_all().items():
        committed = (REPO / "docs" / "assets" / name).read_text(encoding="utf-8")
        assert committed == body, f"{name}: python scripts/portfolio_charts.py 로 다시 만들어 커밋한다"


def test_svgs_are_well_formed_and_carry_the_report_numbers():
    charts = _charts()
    built = charts.build_all()
    for name, body in built.items():
        root = ET.fromstring(body)
        assert root.tag.endswith("svg"), name
    # team_repro_v2.md 0절 1번과 같은 값
    leak = built["leakage_before_after_mrr.svg"]
    for token in ("0.5636", "0.8485", "+0.2849", "[+0.1581, +0.4177]"):
        assert token in leak
    # ebnerd_v1.md 7절·5.3절과 같은 값
    negatives = built["ebnerd_negatives_define_task.svg"]
    for token in ("+0.1868", "−0.1307", "0.6551", "0.0530", "0.2686"):
        assert token in negatives
    # ebnerd_v1_3_neural.md 판정 표와 같은 값
    e15 = built["e15_neural_vs_lightgbm.svg"]
    for token in ("−0.0215", "−0.0531", "seed_rule_failed"):
        assert token in e15


def test_check_mode_reports_a_stale_file(tmp_path):
    charts = _charts()
    assert charts.main(["--check"]) == 0
    assert charts.main(["--check", "--out", str(tmp_path)]) == 1
