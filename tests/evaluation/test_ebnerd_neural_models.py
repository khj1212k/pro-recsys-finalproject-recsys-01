"""E15 신경망 arm(neural/models.py, neural/train.py): 모양, 마스킹·패딩 불변, 빈 히스토리, 손실, 결정론, 조기 종료.

torch가 없으면 통째로 건너뛴다(기본 CI). torch가 있는 CI job과 로컬에서는 합성 데이터의 작은 모델로 CPU에서 돈다.
"""
import dataclasses
import math

import lightgbm  # noqa: F401 — macOS에서는 torch보다 먼저 불러야 한다(neural/train.py의 주석)
import numpy as np
import pytest

torch = pytest.importorskip("torch")
if getattr(torch, "__file__", None) is None:      # 다른 테스트가 sys.modules에 넣어 둔 가짜 torch
    pytest.skip("진짜 torch가 없다", allow_module_level=True)

from evaluation.recsys.ebnerd.models import ALL_GROUPS, V2_FEATURES, feature_matrix  # noqa: E402
from evaluation.recsys.ebnerd.neural import datasets as D  # noqa: E402
from evaluation.recsys.ebnerd.neural import sequences as S  # noqa: E402
from evaluation.recsys.ebnerd.neural import train as TR  # noqa: E402
from evaluation.recsys.ebnerd.neural.models import NeuralSpec, listwise_loss, next_click_loss  # noqa: E402
from evaluation.recsys.ebnerd.neural.report import load_prereg  # noqa: E402
from evaluation.recsys.ebnerd.prepare import p1_task  # noqa: E402
from recsys_core import compute_features  # noqa: E402

PREREG = load_prereg()
FIXED = PREREG["neural"]["fixed"]
SCALARS = S.ScalarSpec.from_prereg(PREREG["neural"]["scalar_block"])
CPU = torch.device("cpu")
BASE = dict(d=16, heads=2, dropout=0.1, lr=3e-3, weight_decay=1e-4, batch=64, n_hist=6)
SPECS = {"nrms": NeuralSpec(family="nrms", **BASE), "sasrec": NeuralSpec(family="sasrec", layers=2, aux_lambda=0.5, **BASE)}


@pytest.fixture(scope="module")
def data(synth_bench):
    ctx = synth_bench.ctx["train"]
    n = len(synth_bench.imps["train"])
    out = {}
    for name, idx in (("fit", np.arange(0, n * 3 // 4)), ("es", np.arange(n * 3 // 4, n))):
        task = p1_task(synth_bench, "train", idx)
        x = feature_matrix(compute_features(ctx, task.req, groups=ALL_GROUPS), task, V2_FEATURES)
        if name == "fit":
            stats = S.standardization_stats(x, V2_FEATURES, SCALARS)
        seq = S.last_n_clicks(ctx.user_log, task.req.user, task.req.profile_cutoff, BASE["n_hist"])
        aux = S.aux_negative_table(ctx.user_log, ctx.catalog.pub_time, FIXED["aux_negatives"], FIXED["aux_window_h"], 4000)
        out[name] = D.build_inputs(task, S.scalar_block(x, V2_FEATURES, SCALARS, stats, ctx.catalog.n_categories), seq, aux)
    return out, ctx.catalog.emb, ctx.catalog.n_categories


def _fit(spec, data, seed=0, **kw):
    inputs, emb, n_cat = data
    kw.setdefault("fixed_epochs", 1)
    kw.setdefault("max_epochs", 3)
    return TR.fit(spec, inputs["fit"], emb, seed=seed, device=CPU, fixed=FIXED, n_categories=n_cat, **kw)


@pytest.fixture(scope="module")
def trained(data):
    return {f: _fit(s, data) for f, s in SPECS.items()}


def _predict(t, inp, emb, **kw):
    return TR.predict(t, inp, emb, device=CPU, fixed=FIXED, **kw)


@pytest.mark.parametrize("family", ["nrms", "sasrec"])
def test_scores_are_one_finite_value_per_candidate_pair_in_task_order(data, trained, family):
    inputs, emb, _ = data
    s = _predict(trained[family], inputs["es"], emb)
    assert s.shape == (len(inputs["es"].cand_item),) and s.dtype == np.float32 and np.isfinite(s).all()
    assert len(np.unique(np.round(s, 6))) > len(s) // 4


@pytest.mark.parametrize("family", ["nrms", "sasrec"])
def test_candidate_padding_does_not_change_scores(data, trained, family):
    """그룹을 어떤 배치에 누구와 묶어 패딩하든 점수가 같다."""
    inputs, emb, _ = data
    es = inputs["es"]
    wide = _predict(trained[family], es, emb, max_pairs=10 ** 6)        # 전부 한 배치(가장 긴 그룹에 맞춘 패딩)
    narrow = _predict(trained[family], es, emb, max_pairs=1)            # 그룹 하나씩(패딩 없음)
    assert np.allclose(wide, narrow, atol=1e-5)


@pytest.mark.parametrize("family", ["nrms", "sasrec"])
def test_sequence_padding_content_and_length_do_not_change_scores(data, trained, family):
    inputs, emb, _ = data
    es = inputs["fit"]                 # 행동 창 첫머리가 들어 있어 히스토리가 짧은 요청과 긴 요청이 섞여 있다
    base = _predict(trained[family], es, emb)
    junk = dataclasses.replace(es, seq=S.Sequences(np.where(es.seq.mask, es.seq.items, 7).astype(np.int32), es.seq.mask, es.seq.pos))
    assert np.array_equal(_predict(trained[family], junk, emb), base)           # 패딩 칸에 무엇이 들어 있든 같다
    short = es.seq.mask.sum(axis=1) <= 3                                        # 클릭 3개 이하인 요청은 N=3으로 줄여도 같아야 한다
    assert short.any() and (~short).any()
    cut = dataclasses.replace(es, seq=S.Sequences(es.seq.items[:, -3:], es.seq.mask[:, -3:], es.seq.pos[:, -3:]))
    got = _predict(trained[family], cut, emb)
    pair_short = np.repeat(short, es.n_candidates)
    assert np.allclose(got[pair_short], base[pair_short], atol=1e-5)
    assert not np.allclose(got[~pair_short], base[~pair_short], atol=1e-5)       # 잘린 클릭이 있으면 달라진다


@pytest.mark.parametrize("family", ["nrms", "sasrec"])
def test_empty_history_takes_the_zero_user_path_and_scores_depend_only_on_scalars(data, trained, family):
    inputs, emb, _ = data
    es = inputs["es"]
    empty = dataclasses.replace(es, seq=S.truncate_sequences(es.seq, 0))        # k=0 콜드 조건
    assert not empty.seq.mask.any()
    base = _predict(trained[family], empty, emb)
    other_items = dataclasses.replace(empty, cand_item=np.roll(es.cand_item, 17))
    assert np.array_equal(_predict(trained[family], other_items, emb), base)    # u=0이면 후보 벡터가 점수에 닿지 않는다
    changed_scalars = dataclasses.replace(empty, cont=empty.cont + 0.5)
    assert not np.allclose(_predict(trained[family], changed_scalars, emb), base)
    assert not np.allclose(_predict(trained[family], es, emb), base)            # 히스토리가 있으면 다른 점수


def test_without_scalars_an_empty_history_gives_every_candidate_the_same_score(data):
    inputs, emb, _ = data
    b0 = _fit(dataclasses.replace(SPECS["nrms"], use_scalars=False), data)
    empty = dataclasses.replace(inputs["es"], seq=S.truncate_sequences(inputs["es"].seq, 0))
    s = _predict(b0, empty, emb)
    assert np.allclose(s, s[0], atol=1e-6)
    full = _predict(b0, inputs["es"], emb)
    assert full.std() > 1e-4
    assert np.array_equal(_predict(b0, dataclasses.replace(inputs["es"], cont=inputs["es"].cont + 3.0), emb), full)


def test_listwise_loss_is_group_softmax_averaged_over_positives_and_skips_groups_without_one():
    scores = torch.tensor([[2.0, 0.0, 1.0], [0.5, 0.5, 9.0], [1.0, 3.0, 0.0]])
    labels = torch.tensor([[1.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    mask = torch.tensor([[True, True, True], [True, True, False], [True, True, True]])
    lse0 = math.log(math.exp(2) + math.exp(0) + math.exp(1))
    g0 = ((lse0 - 2.0) + (lse0 - 1.0)) / 2            # 양성 둘의 −log p 평균
    g1 = math.log(2 * math.exp(0.5)) - 0.5            # 패딩 칸(9.0)은 softmax에 들어가지 않는다
    assert float(listwise_loss(scores, labels, mask)) == pytest.approx((g0 + g1) / 2, abs=1e-6)   # 양성 없는 그룹 제외
    assert float(listwise_loss(scores, torch.zeros_like(labels), mask)) == 0.0


def test_next_click_loss_uses_only_real_transitions_and_ignores_masked_negatives(data, trained):
    inputs, emb, _ = data
    model = TR.build_model(SPECS["sasrec"], emb.shape[1], inputs["fit"].cont.shape[1], data[2], FIXED)
    model.load_state_dict(trained["sasrec"].state)
    model.eval()
    e = TR.emb_tensor(emb, CPU)
    rows = np.argsort(-inputs["fit"].seq.mask.sum(axis=1))[:4]
    batch = TR._to_torch(D.collate(inputs["fit"], rows, with_aux=True), CPU)
    with torch.no_grad():
        _, h, proj = model(e, batch)
        full = float(next_click_loss(model, e, h, proj, batch["seq_mask"], batch["aux_neg"]))
        none = torch.full_like(batch["aux_neg"], -1)
        assert float(next_click_loss(model, e, h, proj, batch["seq_mask"], none)) == pytest.approx(0.0, abs=1e-6)
        assert float(next_click_loss(model, e, h, proj, torch.zeros_like(batch["seq_mask"]), batch["aux_neg"])) == 0.0
        one_click = batch["seq_mask"].clone()
        one_click[:, :-1] = False                       # 클릭이 하나뿐이면 맞힐 "다음 클릭"이 없다
        assert float(next_click_loss(model, e, h, proj, one_click, batch["aux_neg"])) == 0.0
    assert full > 0 and math.isfinite(full)


@pytest.mark.parametrize("family", ["nrms", "sasrec"])
def test_same_seed_reproduces_weights_and_scores_bit_for_bit_on_cpu(data, trained, family):
    inputs, emb, _ = data
    again = _fit(SPECS[family], data)
    assert again.loss_curve == trained[family].loss_curve
    assert all(torch.equal(again.state[k], v) for k, v in trained[family].state.items())
    assert np.array_equal(_predict(again, inputs["es"], emb), _predict(trained[family], inputs["es"], emb))
    assert _fit(SPECS[family], data, seed=1).loss_curve != trained[family].loss_curve


@pytest.mark.parametrize("family", ["nrms", "sasrec"])
def test_determinism_gate_passes_on_the_real_training_path(data, family):
    inputs, emb, n_cat = data
    rows = np.sort(np.random.default_rng(7).choice(inputs["fit"].n_groups, inputs["fit"].n_groups // 10, replace=False))
    gate = TR.determinism_gate(SPECS[family], inputs["fit"].subset(rows), inputs["es"], emb, device=CPU, fixed=FIXED,
                               n_categories=n_cat)
    assert gate["status"] == "pass" and len(gate["runs"]) == 2 and gate["runs"][0] == gate["runs"][1]
    assert gate["fit_groups"] == len(rows)


def test_early_stopping_returns_the_best_epoch_weights_and_stops_after_patience(data):
    inputs, _, _ = data
    curve = iter([0.10, 0.30, 0.20, 0.20, 0.99])
    t = _fit(SPECS["nrms"], data, fixed_epochs=None, max_epochs=10, patience=2, es_in=inputs["es"],
             es_metric=lambda s: next(curve))
    assert t.best_epoch == 2 and t.es_curve == [0.10, 0.30, 0.20, 0.20] and len(t.loss_curve) == 4
    two = _fit(SPECS["nrms"], data, fixed_epochs=2)
    assert all(torch.equal(t.state[k], v) for k, v in two.state.items())        # 돌려준 가중치는 2 epoch 시점의 것


def test_fixed_epoch_training_never_looks_at_the_early_stop_rows(data):
    def boom(_):
        raise AssertionError("fixed_epochs 학습은 es를 보면 안 된다")

    t = _fit(SPECS["nrms"], data, fixed_epochs=2, es_in=data[0]["es"], es_metric=boom)
    assert t.best_epoch == 2 and t.es_curve == [] and len(t.loss_curve) == 2
    with pytest.raises(ValueError):
        _fit(SPECS["nrms"], data, fixed_epochs=None)


def test_training_lowers_the_listwise_loss(data):
    t = _fit(dataclasses.replace(SPECS["nrms"], dropout=0.0), data, fixed_epochs=3)
    assert t.loss_curve[2] < t.loss_curve[0] and t.n_params > 0


def test_saved_model_reloads_with_its_statistics_and_scores_identically(data, trained, tmp_path):
    inputs, emb, _ = data
    path = tmp_path / "m.pt"
    TR.save_trained(path, trained["sasrec"], {"stats": {"mean": [0.0, 1.5], "missing": ["user_age"]}})
    back, extra = TR.load_trained(path)
    assert back.spec == SPECS["sasrec"] and extra["stats"]["missing"] == ["user_age"] and back.best_epoch == 1
    assert np.array_equal(_predict(back, inputs["es"], emb), _predict(trained["sasrec"], inputs["es"], emb))
    wrong_width = dataclasses.replace(inputs["es"], cont=inputs["es"].cont[:, :-1])
    with pytest.raises(ValueError):
        _predict(back, wrong_width, emb)
