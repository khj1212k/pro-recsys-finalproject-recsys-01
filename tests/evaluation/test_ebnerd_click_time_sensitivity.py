"""클릭 시각 근사 민감도 점검의 인기도 창(gap) 정의."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from evaluation.recsys.ebnerd.click_time_sensitivity import popularity_scores
from evaluation.recsys.ebnerd.loaders import ebnerd_root
from recsys_core import HOUR, EventIndex, Requests

DEMO = ebnerd_root() / "ebnerd_demo"
T = 100 * HOUR


def _ctx():
    # 아이템 0: t-7h, t-5h, t-10분, t-1분, t(동시각), 아이템 1: t-30h
    times = [T - 7 * HOUR, T - 5 * HOUR, T - 600, T - 60, T, T - 30 * HOUR]
    items = [0, 0, 0, 0, 0, 1]
    return SimpleNamespace(item_clicks=EventIndex(items, times, items))


def _req():
    return Requests(user=[0], time=[T], cand_ptr=[0, 2], cand_item=[0, 1])


def test_gap_zero_is_half_open_trailing_window():
    sc = popularity_scores(_ctx(), _req(), gap_seconds=0)
    # 6h: t-5h, t-10분, t-1분 (t-7h는 창 밖, 동시각 t는 제외)
    assert sc["popularity_6h"].tolist() == [3.0, 0.0]
    assert sc["popularity_48h"].tolist() == [4.0, 1.0]


def test_gap_shifts_window_end_and_keeps_length():
    sc = popularity_scores(_ctx(), _req(), gap_seconds=300)
    # 창 = [t-5분-6h, t-5분): t-10분·t-5h 포함, t-1분 제외, t-7h는 여전히 밖
    assert sc["popularity_6h"].tolist() == [2.0, 0.0]
    sc = popularity_scores(_ctx(), _req(), gap_seconds=2 * HOUR)
    # 창 = [t-8h, t-2h): t-7h·t-5h 포함
    assert sc["popularity_6h"].tolist() == [2.0, 0.0]


@pytest.mark.skipif(not (DEMO / "articles.parquet").exists(), reason="EB-NeRD demo 데이터 없음 (로컬 전용)")
def test_gap_zero_matches_harness_popularity_features_on_demo(tmp_path):
    from evaluation.recsys.ebnerd.click_time_sensitivity import main
    from evaluation.recsys.ebnerd.prepare import impressions_in, load_bench, p1_task, protocol_windows
    from recsys_core import compute_features

    bench = load_bench(DEMO, fake_dim=8)
    W = protocol_windows(bench)
    idx = impressions_in(bench.imps["validation"], W["test"])[:300]
    task = p1_task(bench, "validation", idx)
    feats = compute_features(bench.ctx["validation"], task.req, groups=("popularity",))
    sc = popularity_scores(bench.ctx["validation"], task.req, gap_seconds=0)
    for w in (6, 24, 48):
        assert np.array_equal(sc[f"popularity_{w}h"], feats[f"pop_clicks_{w}h"].to_numpy(np.float64))

    out = tmp_path / "s.json"
    main(["--dataset", "ebnerd_demo", "--root", str(ebnerd_root()), "--fake-dim", "8", "--seeds", "0",
          "--gaps", "0", "300", "--max-test", "400", "--p2-sample", "50", "--n-boot", "20", "--out-json", str(out)])
    d = json.loads(out.read_text())
    assert set(d["p1"]["results"]) == {f"popularity_{w}h|gap{g}" for w in (6, 24, 48) for g in (0, 300)}
    assert set(d["p2"]["diffs_vs_gap0"]) == {f"popularity_{w}h: gap300 - gap0" for w in (6, 24, 48)}
    assert d["read_time"]["n_impressions_with_click"] > 0
