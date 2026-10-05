"""E15의 튜닝 예산·탐색 공간·튠한 LightGBM(neural/tune.py)과 시간 전진 스태킹(neural/stack.py) — torch 없이 돈다."""
import math

import numpy as np
import pandas as pd
import pytest

from evaluation.recsys.ebnerd import models as M
from evaluation.recsys.ebnerd.neural import stack as K
from evaluation.recsys.ebnerd.neural import tune as T
from evaluation.recsys.ebnerd.neural.report import load_prereg
from evaluation.recsys.ebnerd.prepare import impressions_in, p1_task, protocol_windows
from recsys_core import compute_features

PREREG = load_prereg()


# --- 예산과 탐색 공간 --------------------------------------------------------------------------

def test_tuning_budget_gives_each_gbdt_arm_as_many_trials_as_all_neural_families_together():
    b = T.check_budget(PREREG)
    assert b == {"gbdt_per_arm": 24, "neural_per_family": 12, "neural_total": 24, "families": ["nrms", "sasrec"]}
    assert len(T.gbdt_configs(PREREG)) == sum(len(T.neural_configs(PREREG, f)) for f in b["families"])
    assert T.check_budget(PREREG, neural_trials=1, gbdt_trials=2)["neural_total"] == 2      # 작은 demo 실행도 같은 비율
    for n, g in ((12, 12), (12, 25), (1, 1), (0, 0)):
        with pytest.raises(T.BudgetMismatch):
            T.check_budget(PREREG, neural_trials=n, gbdt_trials=g)


def test_gbdt_trial_zero_is_the_team_setting_and_the_rest_come_from_the_registered_space():
    cfgs = T.gbdt_configs(PREREG)
    space = PREREG["gbdt"]["space"]
    assert cfgs[0] == PREREG["gbdt"]["trial0"] and all(T.in_space(c, space) for c in cfgs)
    for k in ("num_leaves", "learning_rate", "feature_fraction"):
        assert cfgs[0][k] == M.TEAM_PARAMS[k]
    assert cfgs == T.gbdt_configs(PREREG) and len({str(sorted(c.items())) for c in cfgs}) > 12
    fixed = PREREG["gbdt"]["fixed"]
    assert (fixed["bagging_fraction"], fixed["bagging_freq"]) == (M.TEAM_PARAMS["bagging_fraction"], M.TEAM_PARAMS["bagging_freq"])
    assert (PREREG["gbdt"]["num_boost_round"], PREREG["gbdt"]["early_stopping"]) == (M.NUM_BOOST_ROUND, M.EARLY_STOPPING)
    assert T.gbdt_configs(PREREG, 4) == cfgs[:4]


def test_neural_configs_are_reproducible_and_only_sasrec_has_depth_and_aux_weight():
    b, c = T.neural_configs(PREREG, "nrms"), T.neural_configs(PREREG, "sasrec")
    assert len(b) == len(c) == 12 and b == T.neural_configs(PREREG, "nrms")
    assert all(T.in_space(x, T.neural_space(PREREG, "nrms")) for x in b)
    assert all(T.in_space(x, T.neural_space(PREREG, "sasrec")) for x in c)
    assert all("layers" not in x and "aux_lambda" not in x for x in b)
    assert {x["aux_lambda"] for x in c} == {0.0, 0.5} and {x["layers"] for x in c} == {1, 2}
    assert all(3e-4 <= x["lr"] <= 3e-3 for x in b + c) and len({x["lr"] for x in b}) == 12
    assert [x["lr"] for x in b] != [x["lr"] for x in c]


def test_selection_breaks_ties_toward_the_lower_trial_and_the_first_family():
    rows = [{"trial": 2, "selection_metric": 0.31}, {"trial": 0, "selection_metric": 0.30},
            {"trial": 1, "selection_metric": 0.31}, {"trial": 3, "selection_metric": float("nan")}]
    assert T.select_best(rows) == 1
    assert T.select_family({"nrms": 0.3, "sasrec": 0.3}, ["nrms", "sasrec"]) == "nrms"
    assert T.select_family({"nrms": 0.3, "sasrec": 0.31}, ["nrms", "sasrec"]) == "sasrec"
    with pytest.raises(ValueError):
        T.select_family({"nrms": 0.3}, ["nrms", "sasrec"])
    with pytest.raises(ValueError):
        T.select_best([{"trial": 0, "selection_metric": float("nan")}])


def test_random_search_resumes_without_re_evaluating_finished_trials():
    cfgs = T.gbdt_configs(PREREG, 4)
    calls, saved = [], []

    def evaluate(i, cfg):
        calls.append(i)
        return {"selection_metric": 0.1 * i}

    done = {1: {"trial": 1, "config": cfgs[1], "selection_metric": 0.9, "seconds": 3.0}}
    rows = T.random_search(cfgs, evaluate, done=done, on_trial=saved.append)
    assert calls == [0, 2, 3] and [r["trial"] for r in rows] == [0, 1, 2, 3] and [r["trial"] for r in saved] == [0, 2, 3]
    assert rows[1]["selection_metric"] == 0.9 and T.select_best(rows) == 1


# --- 튠한 LightGBM ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def p1_sets(synth_bench):
    W = protocol_windows(synth_bench)
    tr = synth_bench.imps["train"]
    out = {}
    for k in ("fit", "es"):
        task = p1_task(synth_bench, "train", impressions_in(tr, W[k]))
        out[k] = (task, compute_features(synth_bench.ctx["train"], task.req, groups=M.ALL_GROUPS))
    return out, W


def test_tuned_training_path_reproduces_the_fixed_arm_at_trial_zero(p1_sets):
    """trial 0(팀 설정)으로 돌린 튠 경로가 재현 게이트 arm A(models.train)와 같은 모델을 낸다."""
    sets, _ = p1_sets
    spec = next(s for s in M.ABLATION if s.name == "ranker_v2")
    a = M.train(spec, sets["fit"], sets["es"], seed=0, num_threads=2,
                extra_params={"deterministic": True, "force_col_wise": True})
    star0 = T.train_gbdt(spec, sets["fit"], sets["es"], seed=0,
                         params=T.gbdt_params(PREREG, T.gbdt_configs(PREREG)[0], seed=0, num_threads=2))
    assert star0.best_iteration == a.best_iteration and star0.best_score == pytest.approx(a.best_score, abs=1e-12)
    task, feats = sets["es"]
    assert np.array_equal(star0.predict(feats, task), a.predict(feats, task))
    other = T.train_gbdt(spec, sets["fit"], sets["es"], seed=0,
                         params=T.gbdt_params(PREREG, dict(T.gbdt_configs(PREREG)[0], num_leaves=15, lambda_l2=10.0), 0, 2))
    assert not np.array_equal(other.predict(feats, task), a.predict(feats, task))


def test_tuned_params_keep_objective_and_registered_fixed_options():
    p = T.gbdt_params(PREREG, {"num_leaves": 127, "learning_rate": 0.02}, seed=1, num_threads=2)
    assert (p["objective"], p["metric"], p["eval_at"], p["label_gain"]) == ("lambdarank", "ndcg", [10], [0, 1])
    assert p["deterministic"] is True and p["force_col_wise"] is True and p["num_leaves"] == 127 and p["seed"] == 1
    assert p["bagging_fraction"] == 0.8 and p["bagging_freq"] == 5


def test_training_on_a_row_subset_uses_only_those_rows(p1_sets):
    sets, W = p1_sets
    task, feats = sets["fit"]
    edges = K.block_edges(W["fit"], 24)
    keep = K.blocks_mask(K.block_of(task.req.time, edges), task.req.cand_ptr, [2, 3, 4])
    spec = next(s for s in M.ABLATION if s.name == "ranker_v2")
    params = T.gbdt_params(PREREG, T.gbdt_configs(PREREG)[0], seed=0, num_threads=2)
    m = T.train_gbdt(spec, sets["fit"], sets["es"], seed=0, params=params, fit_rows=keep)
    assert m.fit_rows == int(keep.sum()) < len(task.labels)


# --- 시간 전진 스태킹 -------------------------------------------------------------------------

def test_fit_window_splits_into_the_registered_four_day_blocks(p1_sets):
    _, W = p1_sets
    edges = K.block_edges(W["fit"], PREREG["windows"]["block_hours"])
    assert len(edges) - 1 == PREREG["windows"]["fit_blocks"] and edges[0] == W["fit"][0] and edges[-1] == W["fit"][1]
    t = np.array([edges[0] - 1, edges[0], edges[1] - 1, edges[1], edges[-1] - 1, edges[-1]])
    assert K.block_of(t, edges).tolist() == [0, 1, 1, 2, 4, 0]


def _chain(times, ptr, edges, log):
    def train(rows):
        log.append(rows.copy())
        return {"max_time": int(times[rows].max()), "rows": rows.copy()}

    def score(model, rows):
        # 점수 = 그 모델이 학습에서 본 가장 늦은 시각. 누출이 있으면 점수가 행의 시각 이상이 된다.
        return np.concatenate([np.full(ptr[r + 1] - ptr[r], model["max_time"], dtype=np.float64) for r in rows])

    return K.forward_chain_scores(times, ptr, edges, train, score)


def test_every_fit_row_score_comes_from_a_model_trained_only_on_earlier_blocks(p1_sets):
    sets, W = p1_sets
    task, _ = sets["fit"]
    times, ptr = task.req.time, task.req.cand_ptr
    edges = K.block_edges(W["fit"], 24)
    log = []
    chain = _chain(times, ptr, edges, log)
    block = K.block_of(times, edges)
    pair_block = block[task.req.pair_req]
    assert len(log) == 3 and [set(block[r]) for r in log] == [{1}, {1, 2}, {1, 2, 3}]
    assert np.isnan(chain.scores[pair_block == 1]).all() and not np.isnan(chain.scores[pair_block >= 2]).any()
    # 핵심 단언: 모든 채점된 행에서 (그 행을 채점한 모델의 학습 데이터 최대 시각) < (그 행의 요청 시각)
    assert np.all(chain.scores[chain.scored] < times[task.req.pair_req][chain.scored])
    assert np.all(chain.train_max_time[block >= 2] < edges[block[block >= 2] - 1])
    audit = K.assert_forward_only(chain, times, ptr)
    assert audit["scored_requests"] == int((block >= 2).sum()) and audit["min_seconds_after_training_data"] > 0
    assert np.array_equal(K.stacker_rows(chain, ptr), pair_block >= 2)


def test_leakage_check_rejects_scores_from_a_model_that_saw_the_row_or_its_future(p1_sets):
    sets, W = p1_sets
    task, _ = sets["fit"]
    times, ptr = task.req.time, task.req.cand_ptr
    edges = K.block_edges(W["fit"], 24)
    chain = _chain(times, ptr, edges, [])
    # 유저 단위 폴드처럼 창 전체로 학습한 모델이 채점했다면: 학습 데이터 최대 시각이 행의 시각 이상이 된다
    leaky = K.ForwardChain(chain.scores, chain.scored, np.full(len(times), int(times.max())), chain.block, [])
    with pytest.raises(K.LeakageError):
        K.assert_forward_only(leaky, times, ptr)
    scored_first = K.ForwardChain(np.zeros_like(chain.scores), np.ones_like(chain.scored), chain.train_max_time, chain.block, [])
    with pytest.raises(K.LeakageError):
        K.assert_forward_only(scored_first, times, ptr)


def test_neural_score_enters_the_stacker_as_a_within_request_rank():
    feats = pd.DataFrame({"x": np.zeros(7, dtype=np.float32)})
    ptr = np.array([0, 3, 5, 7])
    out = K.with_neural_rank(feats, np.array([0.2, 9.0, -1.0, 5.0, 5.0, np.nan, np.nan]), ptr, "neural_score_rank")
    r = out["neural_score_rank"].to_numpy()
    assert r[:5].tolist() == [0.5, 1.0, 0.0, 0.5, 0.5] and np.isnan(r[5:]).all() and "neural_score_rank" not in feats
    assert math.isclose(out["x"].sum(), 0.0)
