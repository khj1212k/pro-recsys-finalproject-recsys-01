"""E15 신경망 arm의 입력(시퀀스, 시퀀스 스칼라, 스칼라 블록, 콜드 증강, 배치) — torch 없이 돈다.

합성 데이터와 손으로 만든 작은 로그로 point-in-time, 패딩·마스크, 절단 정의와의 일치, 네거티브 고정을 본다.
"""
import numpy as np
import pandas as pd
import pytest
import yaml

from evaluation.recsys.ebnerd.models import ALL_GROUPS, V2_FEATURES, feature_matrix
from evaluation.recsys.ebnerd.neural import datasets as D
from evaluation.recsys.ebnerd.neural import sequences as S
from evaluation.recsys.ebnerd.neural.cold import POP_RAW_COLUMNS, pop_mask_raw, truncate_logs
from evaluation.recsys.ebnerd.neural.report import PREREG_PATH
from evaluation.recsys.ebnerd.prepare import impressions_in, p1_task, pool_negative_task, protocol_windows
from recsys_core import EventIndex, Requests, compute_features

PREREG = yaml.safe_load(PREREG_PATH.read_text(encoding="utf-8"))
SPEC = S.ScalarSpec.from_prereg(PREREG["neural"]["scalar_block"])


@pytest.fixture(scope="module")
def fit(synth_bench):
    # 행동 창 전체를 쓴다: 창 첫머리에는 히스토리가 하나도 없는 요청(합성 유저 열에 하나)이 있다
    idx = np.arange(len(synth_bench.imps["train"]))
    task = p1_task(synth_bench, "train", idx)
    ctx = synth_bench.ctx["train"]
    return task, ctx, compute_features(ctx, task.req, groups=ALL_GROUPS)


def _log():
    # 유저 1: 시각 10, 20, 30, 40에 아이템 1, 2, 3, 4 / 유저 2: 이벤트 없음
    return EventIndex([1, 1, 1, 1], [10, 20, 30, 40], [1, 2, 3, 4])


def test_last_n_clicks_excludes_the_request_time_and_right_aligns():
    seq = S.last_n_clicks(_log(), np.array([1, 1, 1, 2]), np.array([30, 31, 5, 100]), 3)
    assert seq.items.tolist() == [[-1, 1, 2], [1, 2, 3], [-1, -1, -1], [-1, -1, -1]]   # 시각 30의 클릭은 cutoff 30에서 빠진다
    assert seq.mask.tolist() == [[False, True, True], [True, True, True], [False] * 3, [False] * 3]
    assert seq.pos[1].tolist() == [0, 1, 2] and seq.pos[0, 0] == -1


def test_sequences_on_a_real_shaped_task_never_contain_a_click_at_or_after_the_request(fit):
    task, ctx, _ = fit
    seq = S.last_n_clicks(ctx.user_log, task.req.user, task.req.profile_cutoff, 20)
    t = ctx.user_log.time[np.where(seq.mask, seq.pos, 0)]
    assert seq.mask.any() and (~seq.mask.any(axis=1)).any()          # 히스토리 있는 요청과 없는 요청이 둘 다 있다
    assert np.all((t < task.req.profile_cutoff[:, None]) | ~seq.mask)
    assert np.all(ctx.user_log.key[np.where(seq.mask, seq.pos, 0)][seq.mask] == np.repeat(task.req.user, seq.mask.sum(1)))
    # 오른쪽 정렬: 유효 칸은 끝에 붙어 있고 시각이 뒤로 갈수록 늦다
    assert np.all(seq.mask[:, 1:] | ~seq.mask[:, :-1])
    assert np.all((np.diff(t, axis=1) >= 0) | ~seq.mask[:, :-1])


def test_empty_log_gives_only_padding():
    seq = S.last_n_clicks(EventIndex([], [], []), np.array([3, 4]), np.array([10, 20]), 4)
    assert not seq.mask.any() and (seq.items == -1).all() and (seq.pos == -1).all()


@pytest.mark.parametrize("k", [0, 1, 3, 5])
def test_truncating_a_sequence_equals_reading_it_from_the_truncated_log(fit, k):
    task, ctx, _ = fit
    full = S.last_n_clicks(ctx.user_log, task.req.user, task.req.profile_cutoff, 20)
    ctx_k, req_k = truncate_logs(ctx, task.req, k)
    ref = S.last_n_clicks(ctx_k.user_log, req_k.user, req_k.profile_cutoff, 20)
    got = S.truncate_sequences(full, k)
    assert np.array_equal(got.items, ref.items) and np.array_equal(got.mask, ref.mask)
    assert got.mask.sum(axis=1).max() <= k


def test_truncation_with_per_request_k_and_negative_means_unchanged():
    seq = S.last_n_clicks(_log(), np.array([1, 1, 1]), np.array([100, 100, 100]), 4)
    got = S.truncate_sequences(seq, np.array([-1, 2, 0]))
    assert got.items.tolist() == [[1, 2, 3, 4], [-1, -1, 3, 4], [-1, -1, -1, -1]]


def test_cold_augment_plan_picks_the_registered_fraction_without_replacement():
    cfg = PREREG["cold_augment"]
    plan = S.cold_augment_plan(1000, cfg["fraction"], cfg["ks"], seed=2000)
    assert (plan >= 0).sum() == 100 and set(np.unique(plan[plan >= 0])) <= set(cfg["ks"])
    assert len(set(np.unique(plan[plan >= 0]))) == len(cfg["ks"])
    assert np.array_equal(plan, S.cold_augment_plan(1000, cfg["fraction"], cfg["ks"], seed=2000))
    assert not np.array_equal(plan, S.cold_augment_plan(1000, cfg["fraction"], cfg["ks"], seed=2001))


def test_augmentation_rewrites_only_selected_requests_and_only_user_state_columns(fit):
    task, ctx, feats = fit
    plan = S.cold_augment_plan(task.req.n, 0.3, [0, 1, 3, 5], seed=2000)
    out = S.augment_features(ctx, task.req, feats, plan)
    k_pair = plan[task.req.pair_req]
    untouched = k_pair < 0
    pd.testing.assert_frame_equal(out[untouched].reset_index(drop=True), feats[untouched].reset_index(drop=True))
    for c in list(POP_RAW_COLUMNS) + ["hours_since_pub", "news_category"]:      # 아이템 쪽 열은 절단과 무관하다
        assert np.array_equal(out[c].to_numpy(), feats[c].to_numpy())
    assert np.all(out["hist_len"].to_numpy()[~untouched] <= k_pair[~untouched])
    assert np.all(out["sess_len"].to_numpy()[~untouched] <= k_pair[~untouched])
    assert (out["hist_len"].to_numpy()[~untouched] < feats["hist_len"].to_numpy()[~untouched]).any()
    # 절단 정의(cold.py) 그대로: k별로 따로 계산한 것과 같다
    rows = np.flatnonzero(plan == 3)
    sub, pairs = S.subset_requests(task.req, rows)
    ctx_k, req_k = truncate_logs(ctx, sub, 3)
    ref = compute_features(ctx_k, req_k, groups=ALL_GROUPS)
    pd.testing.assert_frame_equal(out.iloc[pairs].reset_index(drop=True), ref)


def test_seq_scalars_match_a_brute_force_loop(fit):
    task, ctx, _ = fit
    emb = ctx.catalog.emb
    cols = PREREG["gbdt"]["seq_scalars"]["columns"]
    seq = S.last_n_clicks(ctx.user_log, task.req.user, task.req.profile_cutoff, 6)
    got = S.seq_scalars(seq, task.req, emb, cols, pair_budget=64)       # 작은 예산으로 여러 청크를 지나게 한다
    ptr = task.req.cand_ptr
    checked_empty = checked_short = False
    empty = np.flatnonzero(~seq.mask.any(axis=1))[:3]
    short = np.flatnonzero(seq.mask.sum(axis=1) == 2)[:3]
    for r in np.concatenate([np.linspace(0, task.req.n - 1, 40).astype(int), empty, short]):
        items = seq.items[r][seq.mask[r]]
        for p in range(ptr[r], ptr[r + 1]):
            c = task.req.cand_item[p]
            row = got.iloc[p].to_numpy()
            if len(items) == 0:
                assert np.all(row == 0.0)
                checked_empty = True
                continue
            sims = emb[items] @ emb[c]
            top = np.sort(sims)[::-1][:3]
            checked_short |= len(items) < 3
            assert row == pytest.approx([sims.max(), top.mean(), sims[-1], float(c in items)], abs=1e-5)
    assert checked_empty and checked_short


def test_scalar_block_standardizes_on_fit_statistics_and_flags_missing_values(fit):
    task, _, feats = fit
    x = feature_matrix(feats, task, V2_FEATURES)
    stats = S.standardization_stats(x, V2_FEATURES, SPEC)
    block = S.scalar_block(x, V2_FEATURES, SPEC, stats, n_categories=6)
    n_cont = len(SPEC.continuous)
    assert block.cont.shape == (len(x), n_cont + len(stats["missing"])) == (len(x), S.scalar_width(stats))
    assert {"user_age", "user_gender", "hours_since_last_event"} <= set(stats["missing"])
    assert not np.isnan(block.cont).any()
    j = SPEC.continuous.index("pop_clicks_24h")
    assert block.cont[:, j].mean() == pytest.approx(0.0, abs=1e-4) and block.cont[:, j].std() == pytest.approx(1.0, abs=1e-3)
    raw = np.log1p(x[:, V2_FEATURES.index("pop_clicks_24h")].astype(np.float64))
    assert block.cont[:, j] == pytest.approx((raw - raw.mean()) / raw.std(), abs=1e-4)
    b = SPEC.continuous.index("is_fresh_24h")                      # 이진 열은 그대로
    assert np.array_equal(block.cont[:, b], x[:, V2_FEATURES.index("is_fresh_24h")])
    age_flag = n_cont + stats["missing"].index("user_age")         # 결측 지표와 NaN -> 0
    age = x[:, V2_FEATURES.index("user_age")]
    assert np.array_equal(block.cont[:, age_flag], np.isnan(age).astype(np.float32))
    assert np.all(block.cont[np.isnan(age), SPEC.continuous.index("user_age")] == 0.0)
    g = x[:, V2_FEATURES.index("user_gender")]
    assert np.all(block.gender[np.isnan(g)] == 0) and np.all(block.gender[~np.isnan(g)] == 1 + g[~np.isnan(g)])
    assert np.array_equal(block.category, x[:, V2_FEATURES.index("news_category")].astype(np.int64))


def test_scalar_spec_covers_exactly_the_22_ranker_features():
    SPEC.check_covers(V2_FEATURES)
    with pytest.raises(ValueError):
        SPEC.check_covers(V2_FEATURES + ["pop_ctr_shrunk_24h"])


def test_popularity_zero_is_applied_to_raw_values_not_to_the_standardized_space(fit):
    task, _, feats = fit
    stats = S.standardization_stats(feature_matrix(feats, task, V2_FEATURES), V2_FEATURES, SPEC)
    cold = S.scalar_block(feature_matrix(pop_mask_raw(feats), task, V2_FEATURES), V2_FEATURES, SPEC, stats, 6)
    j = SPEC.continuous.index("pop_clicks_6h")
    expected = (0.0 - stats["mean"][j]) / stats["std"][j]           # raw 0에 fit 통계를 적용한 값. 0("평균 인기도")이 아니다
    assert expected < -0.1 and np.allclose(cold.cont[:, j], expected, atol=1e-5)


def test_aux_negative_table_is_fixed_by_seed_and_never_returns_the_clicked_item(synth_bench):
    ctx = synth_bench.ctx["train"]
    a = S.aux_negative_table(ctx.user_log, ctx.catalog.pub_time, 4, 48, seed=4000)
    assert a.shape == (len(ctx.user_log), 4) and np.array_equal(a, S.aux_negative_table(ctx.user_log, ctx.catalog.pub_time, 4, 48, 4000))
    assert not np.array_equal(a, S.aux_negative_table(ctx.user_log, ctx.catalog.pub_time, 4, 48, 4001))
    assert not (a == ctx.user_log.item[:, None]).any()
    ok = a >= 0
    age = ctx.user_log.time[:, None] - ctx.catalog.pub_time[np.where(ok, a, 0)]
    assert np.all(((age >= 0) & (age <= 48 * 3600)) | ~ok) and ok.mean() > 0.9


# --- 배치 --------------------------------------------------------------------------------------

def _inputs(task, ctx, feats, n_hist=5):
    x = feature_matrix(feats, task, V2_FEATURES)
    stats = S.standardization_stats(x, V2_FEATURES, SPEC)
    seq = S.last_n_clicks(ctx.user_log, task.req.user, task.req.profile_cutoff, n_hist)
    return D.build_inputs(task, S.scalar_block(x, V2_FEATURES, SPEC, stats, ctx.catalog.n_categories), seq)


def test_neural_inputs_share_the_task_arrays_that_lightgbm_trains_on(fit):
    task, ctx, feats = fit
    inp = _inputs(task, ctx, feats)
    assert inp.cand_ptr is task.req.cand_ptr and inp.cand_item is task.req.cand_item and inp.labels is task.labels
    assert len(inp.cont) == len(feature_matrix(feats, task, V2_FEATURES)) == len(task.labels)


def test_collate_pads_groups_and_maps_every_cell_back_to_its_pair(fit):
    task, ctx, feats = fit
    inp = _inputs(task, ctx, feats)
    groups = np.array([0, 5, 9])
    b = D.collate(inp, groups)
    width = int(inp.n_candidates[groups].max())
    assert b["cand_item"].shape == (3, width) and b["cont"].shape == (3, width, inp.cont.shape[1])
    assert b["seq_items"].shape == (3, 5) and b["seq_mask"].dtype == bool
    assert np.array_equal(b["cand_mask"].sum(axis=1), inp.n_candidates[groups])
    idx = b["pair_index"][b["cand_mask"]]
    assert np.array_equal(b["cand_item"][b["cand_mask"]], inp.cand_item[idx])
    assert np.array_equal(b["labels"][b["cand_mask"]], inp.labels[idx].astype(np.float32))
    assert (b["cand_item"][~b["cand_mask"]] == -1).all() and (b["labels"][~b["cand_mask"]] == 0).all()


def test_eval_batches_cover_every_pair_exactly_once_within_the_pair_budget(fit):
    task, ctx, feats = fit
    inp = _inputs(task, ctx, feats)
    seen = np.zeros(len(inp.labels), dtype=int)
    for g in D.eval_batches(inp.n_candidates, max_pairs=40):
        b = D.collate(inp, g)
        assert b["pair_index"].size <= 40 or len(g) == 1
        seen[b["pair_index"][b["cand_mask"]]] += 1
    assert (seen == 1).all()


def test_training_batches_reorder_groups_but_never_resample_negatives(synth_bench):
    ctx = synth_bench.ctx["train"]
    idx = impressions_in(synth_bench.imps["train"], protocol_windows(synth_bench)["fit"])
    task = pool_negative_task(synth_bench, "train", idx, np.random.default_rng(1000), n_neg=20, window_h=48)
    inp = _inputs(task, ctx, compute_features(ctx, task.req, groups=ALL_GROUPS))

    def epoch(e):
        order, cands = [], {}
        for g in D.group_batches(inp.n_candidates, 16, seed=0, epoch=e):
            b = D.collate(inp, g)
            order += g.tolist()
            for row, gid in enumerate(g):
                cands[int(gid)] = b["cand_item"][row][b["cand_mask"][row]].tolist()
        return order, cands

    o0, c0 = epoch(0)
    o1, c1 = epoch(1)
    assert o0 != o1 and sorted(o0) == sorted(o1) == list(range(task.req.n))
    assert c0 == c1                                                    # 그룹의 후보(정답 + 네거티브)는 epoch마다 같다
    assert all(c0[g] == task.req.cand_item[task.req.cand_ptr[g]:task.req.cand_ptr[g + 1]].tolist() for g in c0)
    assert epoch(0)[0] == o0                                           # 같은 (seed, epoch)이면 같은 순서


def test_collate_attaches_aux_negatives_by_event_position(fit):
    task, ctx, feats = fit
    inp = _inputs(task, ctx, feats)
    with pytest.raises(ValueError):
        D.collate(inp, [0], with_aux=True)
    inp.aux_neg = S.aux_negative_table(ctx.user_log, ctx.catalog.pub_time, 4, 48, seed=4000)
    r = int(np.argmax(inp.seq.mask.sum(axis=1)))
    b = D.collate(inp, [r], with_aux=True)
    assert b["aux_neg"].shape == (1, 5, 4)
    assert np.array_equal(b["aux_neg"][0][inp.seq.mask[r]], inp.aux_neg[inp.seq.pos[r][inp.seq.mask[r]]])
    assert (b["aux_neg"][0][~inp.seq.mask[r]] == -1).all()


def test_subset_keeps_request_alignment(fit):
    task, ctx, feats = fit
    inp = _inputs(task, ctx, feats)
    rows = np.array([3, 10, 11])
    sub = inp.subset(rows)
    assert sub.n_groups == 3 and np.array_equal(sub.n_candidates, inp.n_candidates[rows])
    assert np.array_equal(sub.seq.items, inp.seq.items[rows])
    a, b = inp.cand_ptr[10], inp.cand_ptr[11]
    assert np.array_equal(sub.cand_item[sub.cand_ptr[1]:sub.cand_ptr[2]], inp.cand_item[a:b])
    assert np.array_equal(sub.cont[sub.cand_ptr[1]:sub.cand_ptr[2]], inp.cont[a:b])


def test_inputs_carry_request_times_so_a_model_can_record_what_it_was_fitted_on(fit):
    """요청 시각이 입력에 붙어 다닌다(부분 집합에서도). 학습 함수가 이 값으로 "무엇으로 학습했는가"를 모델에 적는다."""
    task, ctx, feats = fit
    inp = _inputs(task, ctx, feats)
    assert inp.req_time is task.req.time
    rows = np.array([3, 10, 11])
    assert np.array_equal(inp.subset(rows).req_time, task.req.time[rows])
    x = feature_matrix(feats, task, V2_FEATURES)
    assert S.standardization_stats(x, V2_FEATURES, SPEC)["n_rows"] == len(x)       # 통계를 계산한 행 수도 통계와 함께 남는다
    assert S.standardization_stats(x[:7], V2_FEATURES, SPEC)["n_rows"] == 7


def test_requests_subset_helper_round_trips_candidates():
    req = Requests(user=[1, 2, 3], time=[10, 20, 30], cand_ptr=[0, 2, 3, 6], cand_item=[5, 6, 7, 8, 9, 4], session=[1, 1, 2])
    sub, pairs = S.subset_requests(req, np.array([0, 2]))
    assert sub.cand_item.tolist() == [5, 6, 8, 9, 4] and pairs.tolist() == [0, 1, 3, 4, 5] and sub.session.tolist() == [1, 2]
