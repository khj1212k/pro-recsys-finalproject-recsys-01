"""후보 생성기 구성(E8): 서빙 방식 라운드로빈·cap·출처 자격, 그리고 사전 등록 구성 = 서빙 기본 구성."""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from evaluation.recsys.ebnerd.candidate_config import (
    HARNESS,
    SERVING,
    CandidateConfig,
    config_from_prereg,
    round_robin_union,
    union_mask,
)

REPO = Path(__file__).resolve().parents[2]
PREREG = yaml.safe_load((REPO / "evaluation/recsys/ebnerd/preregistration/cold-v1.2.yaml").read_text())


def _serving_pipeline():
    backend = str(REPO / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")
    return pytest.importorskip("app.recsys.pipeline")


def test_round_robin_matches_the_serving_implementation_on_random_inputs():
    pipeline = _serving_pipeline()
    rng = np.random.default_rng(0)
    for _ in range(200):
        sources = {}
        for name in ("knn_profile", "knn_short", "recent", "popular", "category"):
            if rng.random() < 0.8:
                sources[name] = rng.choice(60, size=int(rng.integers(0, 25)), replace=False).tolist()
        cap = int(rng.integers(1, 50))
        want = pipeline._round_robin_union(sources, cap)
        merged, contributed = round_robin_union(sources, cap)
        assert merged == want.ids and contributed == want.contributed


def test_registered_serving_config_equals_the_serving_defaults():
    """사전 등록한 서빙 구성이 지금 서빙 코드의 기본값과 같아야 E8의 2단계 수치가 서빙의 것이다."""
    _serving_pipeline()
    from app.recsys.config import RecsysConfig

    rc = RecsysConfig()
    cfg = config_from_prereg("serving", PREREG["e8"]["serving"])
    assert cfg.window_h == rc.freshness_hours and cfg.cap == rc.candidate_cap
    assert dict(cfg.sources) == {"knn_profile": rc.knn_k, "knn_short": rc.knn_k, "recent": rc.recent_n,
                                 "popular": rc.popular_n, "category": rc.category_n}
    # 라운드로빈 순서 = generate_candidates가 출처를 넣는 순서
    assert [s for s, _ in cfg.sources] == ["knn_profile", "knn_short", "recent", "popular", "category"]


def test_module_constants_are_the_registered_configs():
    assert SERVING == config_from_prereg("serving", PREREG["e8"]["serving"])
    assert HARNESS == config_from_prereg("harness", PREREG["e8"]["harness"])
    assert SERVING.model == "poolneg72" and SERVING.cap == 300 and SERVING.window_h == 72


def test_registered_harness_config_is_the_v1_union_of_top_50():
    cfg = config_from_prereg("harness", PREREG["e8"]["harness"])
    assert cfg.cap is None and cfg.window_h == 48 and cfg.model == "poolneg"
    assert dict(cfg.sources) == {"popularity_6h": 50, "popularity_24h": 50, "recency": 50, "cosine_history": 50}


def _feats(n_req=3, per=10, seed=0):
    rng = np.random.default_rng(seed)
    n = n_req * per
    f = pd.DataFrame({
        "hours_since_pub": rng.permutation(n).astype(np.float32),
        "pop_clicks_6h": rng.permutation(n).astype(np.float32),
        "pop_clicks_24h": rng.permutation(n).astype(np.float32),
        "hist_cos": rng.random(n).astype(np.float32),
        "hist_len": np.float32(5),
        "short_cos": rng.random(n).astype(np.float32),
        "short_len": np.float32(2),
        "is_cat_match": (rng.random(n) < 0.4).astype(np.float32),
    })
    return f, np.arange(0, n + 1, per)


def test_union_without_cap_equals_any_source_top_k():
    f, ptr = _feats()
    cfg = CandidateConfig("h", 48, (("popularity_6h", 2), ("recency", 2), ("cosine_history", 2)))
    mask, info = union_mask(cfg, f, ptr)
    for g in range(3):
        sl = slice(ptr[g], ptr[g + 1])
        want = set()
        for col, sign in (("pop_clicks_6h", -1), ("hours_since_pub", 1), ("hist_cos", -1)):
            want |= set(np.argsort(sign * f[col].to_numpy()[sl], kind="stable")[:2].tolist())
        assert set(np.flatnonzero(mask[sl]).tolist()) == want
    assert info["mean_union_size"] == pytest.approx(mask.sum() / 3)


def test_serving_style_union_respects_cap_eligibility_and_inactive_sources():
    f, ptr = _feats()
    f.loc[10:19, "short_len"] = 0.0        # 요청 1: 단기 이벤트 없음 -> knn_short 비활성
    f.loc[20:29, "is_cat_match"] = 0.0     # 요청 2: 카테고리 일치 후보 없음 -> category가 낼 것이 없음
    cfg = CandidateConfig("s", 72, (("knn_profile", 3), ("knn_short", 3), ("recent", 3), ("popular", 3),
                                    ("category", 2)), cap=6)
    mask, info = union_mask(cfg, f, ptr)
    sizes = np.add.reduceat(mask.astype(int), ptr[:-1])
    assert np.all(sizes <= 6) and info["max_union_size"] <= 6 and info["cap"] == 6
    # 요청 1의 합집합은 knn_short 없이 만든 라운드로빈과 같아야 한다
    sl = slice(10, 20)
    order = lambda col, sign: np.argsort(sign * f[col].to_numpy()[sl], kind="stable").tolist()  # noqa: E731
    cat = [i for i in order("hours_since_pub", 1) if f["is_cat_match"].to_numpy()[sl][i] > 0][:2]
    lists = {"knn_profile": order("hist_cos", -1)[:3], "recent": order("hours_since_pub", 1)[:3],
             "popular": order("pop_clicks_6h", -1)[:3]}
    if cat:
        lists["category"] = cat
    want, _ = round_robin_union(lists, 6)
    assert set(np.flatnonzero(mask[sl]).tolist()) == set(want)
    # category 출처는 자격 없는 후보를 내지 않는다: 카테고리가 맞는 후보가 없는 요청 2에서 기여 0
    cfg_cat = CandidateConfig("c", 72, (("category", 5),), cap=5)
    m2, info2 = union_mask(cfg_cat, f, ptr)
    assert not m2[20:30].any()
    assert np.all(f["is_cat_match"].to_numpy()[m2] > 0)


def test_cap_makes_sources_take_turns_instead_of_one_source_filling_the_pool():
    merged, contributed = round_robin_union({"a": [1, 2, 3, 4, 5, 6], "b": [1, 7, 8], "c": [9]}, cap=5)
    # 1회전: a->1, b->7(1은 이미 있음), c->9 / 2회전: a->2, b->8 -> cap
    assert merged == [1, 7, 9, 2, 8] and contributed == {"a": 2, "b": 2, "c": 1}
