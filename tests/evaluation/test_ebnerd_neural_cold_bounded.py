"""E15 P2 콜드 단계의 행 조각 경로가 한 번에 만드는 경로와 같은 결과를 내는지 (ADR 0013 A3.12, 2026-10-17 코드 수정).

P2 콜드 표본(60,000 요청 x 풀 약 235 ≈ 1,410만 쌍)을 한 번에 채점하면 12.7 GB RAM에서 죽는다(크기 리허설). 그래서 P2는
LightGBM 입력 행렬·신경망 스칼라 블록·스태커 입력을 쌍 조각으로 만든다. 표본·시드·순서·모델은 그대로이고, 저장되는 요청별
지표 배열과 단위 메타가 한 번에 만드는 경로(P1이 쓰는 경로)와 **비트 단위로 같아야** 한다. 여기서 그것을 합성 데이터와
(있으면) ebnerd_demo에서 확인한다. 수치는 해석하지 않는다(배선과 동일성만 본다).
"""
import json
import shutil

import lightgbm  # noqa: F401 — macOS에서는 torch보다 먼저 불러야 한다(neural/train.py의 주석)
import numpy as np
import pytest

from evaluation.recsys.ebnerd import run_neural as R
from evaluation.recsys.ebnerd.loaders import ebnerd_root
from evaluation.recsys.ebnerd.models import V2_FEATURES, feature_matrix
from evaluation.recsys.ebnerd.neural.cold import cold_conditions, pop_mask_raw
from evaluation.recsys.ebnerd.neural.sequences import keys_sha256

SMALL = ["--seeds", "0", "--n-boot", "30", "--p2-sample", "150", "--p2-cold-sample", "200", "--p2-select-sample", "80",
         "--neural-trials", "1", "--gbdt-trials", "2", "--tune-max-epochs", "1", "--final-max-epochs", "2",
         "--threads", "2", "--device", "cpu", "--resume"]
P2_BEFORE_COLD = ("gate", "gbdt_tune", "gbdt_final", "determinism", "neural_tune", "select", "neural_final", "stack")
COLD_UNITS = {f"p2__n__cold_{c.name}" for c in cold_conditions()}


def _has_torch() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return getattr(torch, "__file__", None) is not None


pytestmark = pytest.mark.skipif(not _has_torch(), reason="torch가 없다(신경망 단계는 torch가 있는 CI job과 로컬에서 돈다)")


def _run_p2_until_cold(argv_base: list, out) -> None:
    for stage in P2_BEFORE_COLD:
        assert R.main([*argv_base, "--out-dir", str(out), "--task", "p2", "--stage", stage]) == 0


def _drop_cold_units(out) -> None:
    prog_path = out / "progress.json"
    prog = json.loads(prog_path.read_text())
    for u in [u for u in prog["units"] if u in COLD_UNITS]:
        for f in prog["units"].pop(u)["files"]:
            (out / f).unlink()
    prog_path.write_text(json.dumps(prog, indent=1, ensure_ascii=False))


def _cold_units(out) -> dict:
    units = json.loads((out / "progress.json").read_text())["units"]
    got = {}
    for u in sorted(u for u in units if u in COLD_UNITS):
        with np.load(out / "units" / f"{u}.npz") as z:
            arrays = {k: z[k] for k in z.files}
        got[u] = (arrays, json.loads((out / "units" / f"{u}.json").read_text()))
    return got


def _assert_bitwise_equal(a: dict, b: dict) -> None:
    assert set(a) == set(b) == COLD_UNITS
    for u in sorted(a):
        (xa, ma), (xb, mb) = a[u], b[u]
        assert ma == mb, u                                  # 조건·요청 수·쌍 수·스칼라 행렬 sha256·시퀀스 비율
        assert set(xa) == set(xb), u
        for k in xa:
            assert xa[k].dtype == xb[k].dtype and xa[k].shape == xb[k].shape, (u, k)
            assert xa[k].tobytes() == xb[k].tobytes(), (u, k)      # NaN의 비트까지 같다


def _cold_with(monkeypatch, argv_base: list, src, dst, *, in_memory: bool, chunk: int = R.COLD_CHUNK_PAIRS,
               max_pairs=None) -> dict:
    """src의 체크포인트를 dst로 복사하고 P2 콜드 단위만 지운 뒤, 고른 경로로 cold 단계를 다시 돌린다."""
    shutil.copytree(src, dst)
    _drop_cold_units(dst)
    with monkeypatch.context() as m:
        if in_memory:
            m.setattr(R, "_cold_unit_bounded", R._cold_unit_in_memory)
        m.setattr(R, "COLD_CHUNK_PAIRS", chunk)
        if max_pairs is not None:          # 채점 배치를 잘게 — 두 경로가 같은 배치 계획을 쓰는지까지 본다
            from evaluation.recsys.ebnerd.neural import datasets as DS
            from evaluation.recsys.ebnerd.neural import train as TR
            m.setattr(TR, "eval_batches", lambda n, _mp=None: DS.eval_batches(n, max_pairs))
        assert R.main([*argv_base, "--out-dir", str(dst), "--task", "p2", "--stage", "cold"]) == 0
    return _cold_units(dst)


@pytest.fixture(scope="module")
def p2_store(synth_root, tmp_path_factory):
    """합성 데이터로 P2를 cold 앞까지 돌린 체크포인트(cold 단위 없음)."""
    out = tmp_path_factory.mktemp("e15_p2_store")
    base = ["--dataset", "ebnerd_synth", "--root", str(synth_root), *SMALL]
    _run_p2_until_cold(base, out)
    assert not COLD_UNITS & set(json.loads((out / "progress.json").read_text())["units"])
    return base, out


def test_p2_cold_rows_path_equals_the_in_memory_path_bitwise(p2_store, tmp_path, monkeypatch):
    base, out = p2_store
    ref = _cold_with(monkeypatch, base, out, tmp_path / "in_memory", in_memory=True)
    for chunk in (97, 4099, R.COLD_CHUNK_PAIRS):           # 요청 경계와 맞지 않는 조각, 중간, 기본값(조각 하나)
        _assert_bitwise_equal(_cold_with(monkeypatch, base, out, tmp_path / f"rows_{chunk}", in_memory=False,
                                         chunk=chunk), ref)


def test_p2_cold_rows_path_equals_the_in_memory_path_with_many_scoring_batches(p2_store, tmp_path, monkeypatch):
    """채점 배치가 여러 개일 때(배치 묶음이 조각마다 다를 때)도 같다."""
    base, out = p2_store
    ref = _cold_with(monkeypatch, base, out, tmp_path / "in_memory", in_memory=True, max_pairs=700)
    n_pairs = ref["p2__n__cold_k0"][1]["n_pairs"]
    assert n_pairs > 5 * 700                             # 배치가 실제로 여러 개다
    for chunk in (1, 2048):
        _assert_bitwise_equal(_cold_with(monkeypatch, base, out, tmp_path / f"rows_{chunk}", in_memory=False,
                                         chunk=chunk, max_pairs=700), ref)


def test_p1_cold_keeps_the_in_memory_path(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "ensure_neural_env", lambda run: {})
    monkeypatch.setattr(R, "selection", lambda run: {"family": "nrms", "table": {"nrms": {"config": {}}}})
    monkeypatch.setattr(R, "_spec", lambda *a, **k: None)
    monkeypatch.setattr(R, "_unit", lambda run, unit, fn: fn())
    monkeypatch.setattr(R, "_cold_unit_in_memory", lambda *a: calls.append("in_memory"))
    monkeypatch.setattr(R, "_cold_unit_bounded", lambda *a: calls.append("rows"))

    class FakeRun:
        prereg = {"cold": {"conditions": [c.name for c in cold_conditions()]}}

    for task, path in (("p1", "in_memory"), ("p2", "rows")):
        calls.clear()
        run = FakeRun()
        run.task = task
        R.stage_cold(run)
        assert calls == [path] * len(cold_conditions())


def test_row_helpers_match_the_whole_matrix_helpers(p2_store, tmp_path):
    """조각 도구 하나하나: 피처 열, 입력 행렬의 sha256, LightGBM 예측, 신경망 점수가 한 번에 만든 것과 같다."""
    import argparse

    from evaluation.recsys.ebnerd.neural import train as TR
    from evaluation.recsys.ebnerd.prepare import load_bench, protocol_windows

    base, out = p2_store
    work = tmp_path / "w"
    shutil.copytree(out, work)
    args = R.build_parser().parse_args([*base, "--out-dir", str(work), "--task", "p2", "--stage", "cold"])
    root = R.Path(args.root)
    bench = load_bench(root / args.dataset, emb_dir=root / "derived" / args.dataset)
    W = protocol_windows(bench)
    idx = {"fit": R.impressions_in(bench.imps["train"], W["fit"]), "es": R.impressions_in(bench.imps["train"], W["es"]),
           "test": R.impressions_in(bench.imps["validation"], W["test"])}
    store = R.NeuralStore.open_existing(work)
    run = R.Run(args=args, prereg=R.load_prereg(), bench=bench, windows=W, idx=idx, store=store, task="p2")
    whole = run.eval_set("cold")
    lean = R._cold_requests(run)
    assert lean.task.req.cand_item.tobytes() == whole.task.req.cand_item.tobytes()
    assert lean.seq.items.tobytes() == whole.seq.items.tobytes() and lean.clusters.tobytes() == whole.clusters.tobytes()
    pair_req = whole.task.req.pair_req
    cols = R._feature_columns(run, whole.ctx, whole.task.req, "p2-cold", whole.seq, whole.task.req)
    assert list(cols) == list(whole.feats.columns)
    for c in cols:
        assert cols[c].dtype == whole.feats[c].to_numpy().dtype and cols[c].tobytes() == whole.feats[c].to_numpy().tobytes(), c
    masked = pop_mask_raw(whole.feats)
    cols0 = {**cols, **{c: np.zeros(len(pair_req), np.float32) for c in R.POP_RAW_COLUMNS if c in cols}}
    for chunk in (1, 333, 10 ** 9):
        assert R._rows_sha256(cols0, whole.task, pair_req, V2_FEATURES, chunk) == \
            keys_sha256(feature_matrix(masked, whole.task, V2_FEATURES))
        for arm in (R.A_STAR, R.A_PLUS):
            model = store.load_model(f"p2_{arm}", 0)
            assert R._predict_rows(model, cols, whole.task, pair_req, chunk).tobytes() == \
                model.predict(whole.feats, whole.task).tobytes()
    sel = R.selection(run)
    env = {"device": TR.resolve_device("cpu")}
    spec = R._spec(run, sel["family"], sel["table"][sel["family"]]["config"])
    trained, stats = R._neural_model(run, env, sel["family"], spec, 0)
    full = R._score_neural(run, env, trained, stats, whole.task, whole.feats, whole.seq)
    for chunk in (1, 500, 10 ** 9):
        assert R._score_neural_rows(run, env, trained, stats, whole.task, cols, pair_req, whole.seq, chunk).tobytes() \
            == full.tobytes()


@pytest.mark.parametrize("max_pairs,chunk", [(256, 1), (256, 700), (256, 10 ** 9), (65536, 300)])
def test_predict_streamed_equals_predict_bitwise(p2_store, max_pairs, chunk):
    """신경망 채점: 배치 계획(eval_batches)이 같으면 스칼라 블록을 조각으로 만들어도 점수가 비트 단위로 같다."""
    from evaluation.recsys.ebnerd.neural import train as TR
    from evaluation.recsys.ebnerd.neural.datasets import NeuralInputs
    from evaluation.recsys.ebnerd.neural.models import NeuralSpec
    from evaluation.recsys.ebnerd.neural.sequences import ScalarBlock, Sequences

    rng = np.random.default_rng(7)
    n_req, n_hist, n_cont, n_cat, d = 120, 6, 5, 4, 16
    counts = rng.integers(0, 40, size=n_req)
    ptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
    n = int(ptr[-1])
    emb = rng.normal(size=(60, d)).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    items = rng.integers(0, 60, size=(n_req, n_hist)).astype(np.int32)
    mask = rng.random((n_req, n_hist)) < 0.7
    seq = Sequences(np.where(mask, items, -1), mask, np.where(mask, 0, -1).astype(np.int64))
    cont = rng.normal(size=(n, n_cont)).astype(np.float32)
    cat, gender = rng.integers(0, n_cat, size=n).astype(np.int64), rng.integers(0, 3, size=n).astype(np.int64)
    inp = NeuralInputs(cand_ptr=ptr, cand_item=rng.integers(0, 60, size=n).astype(np.int64),
                       labels=(rng.random(n) < 0.1), cont=cont, category=cat, gender=gender, seq=seq,
                       req_time=np.arange(n_req, dtype=np.int64))
    fixed = R.load_prereg()["neural"]["fixed"]
    device = TR.resolve_device("cpu")
    for family, extra in (("nrms", {}), ("sasrec", {"layers": 2, "aux_lambda": 0.0})):
        spec = NeuralSpec.from_config(family, {"d": 32, "heads": 2, "dropout": 0.1, "lr": 1e-3, "weight_decay": 0.0,
                                                "batch": 32, "n_hist": n_hist, **extra})
        trained = TR.fit(spec, inp, emb, seed=0, device=device, fixed=fixed, n_categories=n_cat, max_epochs=1,
                         fixed_epochs=1)
        full = TR.predict(trained, inp, emb, device=device, fixed=fixed, max_pairs=max_pairs)

        def block_rows(pairs):
            return ScalarBlock(cont=cont[pairs], category=cat[pairs], gender=gender[pairs])
        got = TR.predict_streamed(trained, ptr, inp.cand_item, inp.labels, seq, block_rows, emb, device=device,
                                  fixed=fixed, max_pairs=max_pairs, chunk_pairs=chunk)
        assert got.dtype == full.dtype and got.tobytes() == full.tobytes(), family


@pytest.mark.skipif(not (ebnerd_root() / "ebnerd_demo" / "articles.parquet").exists(),
                    reason="EB-NeRD demo 데이터가 없다(로컬 전용, 라이선스상 저장소·CI에 두지 않는다)")
def test_demo_p2_cold_rows_path_equals_the_in_memory_path_bitwise(tmp_path_factory, monkeypatch):
    """실데이터 스키마(ebnerd_demo, 무작위 임베딩 16차원)의 P2에서도 두 경로가 같다."""
    out = tmp_path_factory.mktemp("e15_demo_p2")
    base = ["--dataset", "ebnerd_demo", "--root", str(ebnerd_root()), "--fake-dim", "16", "--max-fit", "1500",
            "--max-test", "1500", *SMALL]
    _run_p2_until_cold(base, out)
    tmp = tmp_path_factory.mktemp("e15_demo_cold")
    ref = _cold_with(monkeypatch, base, out, tmp / "in_memory", in_memory=True, max_pairs=4096)
    for chunk in (1013, R.COLD_CHUNK_PAIRS):
        _assert_bitwise_equal(_cold_with(monkeypatch, base, out, tmp / f"rows_{chunk}", in_memory=False, chunk=chunk,
                                         max_pairs=4096), ref)
