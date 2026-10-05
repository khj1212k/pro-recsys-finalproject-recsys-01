"""콜드 regime 사슬 실행기(run_cold): 합성 데이터로 전 구간, 체크포인트 재개, 증거 등급, 조건별 입력.

EB-NeRD 없이 돈다. demo 데이터가 있는 로컬에서는 맨 아래 테스트가 같은 경로를 실데이터 스키마로 한 번 더 돈다.
여기서 나오는 수치는 전부 합성 데이터의 것이라 해석하지 않는다(배선만 본다).
"""
import copy
import json
import shutil

import numpy as np
import pandas as pd
import pytest

from evaluation.recsys.ebnerd import run_cold
from evaluation.recsys.ebnerd.cold_transforms import SHRUNK_COLUMN
from evaluation.recsys.ebnerd.cold_verdicts import DEMO_GRADE, EVIDENCE_GRADE, load_prereg, prereg_sha256
from evaluation.recsys.ebnerd.loaders import ebnerd_root
from evaluation.recsys.ebnerd.neural.cold import POP_RAW_COLUMNS

PREREG = load_prereg()
SMALL = ["--seeds", "0", "--n-boot", "20", "--p2-sample", "120", "--sub-cap", "80", "--threads", "2"]


def _args(root, out, *extra):
    return ["--dataset", "ebnerd_synth", "--root", str(root), "--out-dir", str(out), *SMALL, *extra]


@pytest.fixture(scope="module")
def full_run(synth_root, tmp_path_factory):
    out = tmp_path_factory.mktemp("cold_full")
    assert run_cold.main(_args(synth_root, out, "--stage", "all")) == 0
    return out, json.loads((out / run_cold.REPORT_JSON).read_text())


def _strip_timing(d):
    d = json.loads(json.dumps(d))
    for s in d.get("stages", {}).values():
        s.pop("seconds", None)
    for ms in d.get("models", {}).values():
        for m in ms:
            m.pop("train_seconds", None)
    return d


def test_chain_produces_every_registered_cell_and_a_machine_verdict(full_run):
    out, d = full_run
    traffic = PREREG["conditions"]["traffic"]
    pools = ["full"] + [str(n) for n in PREREG["conditions"]["pool_sizes"]]
    assert set(d["e1"]["cells"]) == {f"{t}|{p}" for t in traffic for p in pools}
    base = d["e1"]["cells"]["orig|full"]
    arms_48 = [a for a, c in PREREG["arms"].items() if c["data"] == "pool_neg" and c["window_h"] == 48]
    assert set(arms_48) <= set(base["methods"])
    assert {"heuristic_cold", "heuristic_prior4", "heuristic_fit_a", "heuristic_fit_b", "popularity_6h"} <= set(base["methods"])
    assert "poolneg_masknan@nan" in d["e1"]["cells"]["pop0|full"]["methods"]      # NaN 입력 변형은 pop0에서만
    assert "poolneg_masknan@nan" not in base["methods"]
    grid = [f"{t}|k{k}" for t in ("orig", "pop0") for k in PREREG["conditions"]["truncate_ks"] + ["all"]]
    assert set(d["e2"]["p2"]) == set(grid)
    assert set(d["e2"]["p1"]) == {f"k{k}" for k in PREREG["conditions"]["truncate_ks"] + ["all"]}
    assert set(d["e8"]) == {"serving", "harness"} and d["e8"]["serving"]["judged_model"] == "poolneg72"
    assert len(d["e4"]["buckets"]) == 5
    v = d["verdicts"]
    assert set(v["verdicts"]) == {"e1", "e2", "e3", "e6", "e7", "e8"}
    assert d["unmeasured"] == {"rules": [], "stages": []}
    assert all(s["units_done"] == s["units_total"] for s in d["stages"].values())


def test_untruncated_e2_reproduces_the_e1_base_cell_exactly(full_run):
    _, d = full_run
    assert d["e2"]["consistency_with_e1"]["max_abs_diff"] == 0.0
    a = d["e2"]["p2"]["orig|kall"]["methods"]["poolneg"]["ndcg@10"]
    b = d["e1"]["cells"]["orig|full"]["methods"]["poolneg"]["ndcg@10"]
    assert a["mean"] == b["mean"] and a["ci95"] == b["ci95"]
    # 절단이 걸리는 요청 비율은 k가 커질수록 줄고, 절단 없음에서는 0이다
    shares = [d["e2"]["p2"][f"orig|k{k}"]["truncated_share"] for k in PREREG["conditions"]["truncate_ks"] + ["all"]]
    assert shares == sorted(shares, reverse=True) and shares[-1] == 0.0 and shares[0] > 0.5


def test_pool_shrink_cells_use_the_same_requests_and_smaller_pools(full_run):
    out, d = full_run
    with np.load(out / "units/e1_base.npz") as z:
        n = len(z["clusters"])
        assert all(z[k].shape == (3, n) for k in z.files if k != "clusters")
    cells = d["e1"]["cells"]
    assert cells["orig|40"]["n_requests"] == cells["orig|full"]["n_requests"] == 120
    # 풀이 작아지면 무작위 순위의 nDCG는 올라간다(정답은 남고 경쟁 후보만 준다)
    r = [cells[f"orig|{p}"]["methods"]["random"]["ndcg@10"]["mean"] for p in ("full", "60", "40")]
    assert r[0] < r[1] < r[2]


def test_pop0_cells_make_popularity_baseline_carry_no_signal(full_run):
    _, d = full_run
    cells = d["e1"]["cells"]
    assert cells["pop0|full"]["methods"]["recency"]["ndcg@10"] == cells["orig|full"]["methods"]["recency"]["ndcg@10"]
    assert (cells["pop0|full"]["methods"]["heuristic_prior4"]["ndcg@10"]["mean"]
            == cells["pop0|full"]["methods"]["heuristic_cold"]["ndcg@10"]["mean"])   # 인기 항이 0이면 두 휴리스틱이 같다
    assert (cells["orig|full"]["methods"]["heuristic_prior4"]["ndcg@10"]["mean"]
            != cells["orig|full"]["methods"]["heuristic_cold"]["ndcg@10"]["mean"])


def test_report_is_labelled_demo_and_not_judgeable(full_run):
    out, d = full_run
    ev = d["meta"]["evidence"]
    assert ev["grade"] == DEMO_GRADE and any("dataset" in r for r in ev["reasons"])
    assert d["verdicts"]["judgeable"] is False
    md = (out / run_cold.REPORT_MD).read_text()
    assert "demo, not evidence" in md.splitlines()[2] and "이 실행은 판정에 쓰지 않는다" in md
    assert d["meta"]["preregistration"]["sha256"] == prereg_sha256()
    from evaluation.recsys.ebnerd.make_cold_report import render
    assert render(d) == md      # 표는 JSON에서 다시 만들 수 있다


def test_report_records_the_environment_that_computed_the_numbers(full_run, tmp_path):
    """리포트의 환경 기록은 조립한 곳이 아니라 계산한 곳의 것이어야 한다(조립은 다른 기계에서 다시 할 수 있다)."""
    import lightgbm
    import numpy

    out, d = full_run
    envs = d["meta"]["environments"]
    assert len(envs) == 1 and envs[0]["lightgbm"] == lightgbm.__version__ and envs[0]["numpy"] == numpy.__version__
    assert envs[0]["threads"] == 2 and "python" in envs[0] and "assembled_on" in d["meta"]
    # 다른 환경에서 이어 돌리면 둘 다 남는다
    store = run_cold.Store.open_existing(out)
    cfg = store.progress["config"]
    other = {**envs[0], "lightgbm": "0.0.0-other"}
    again = run_cold.Store(tmp_path / "copy", cfg, resume=False, env=envs[0])
    assert again.progress["environments"] == [envs[0]]
    resumed = run_cold.Store(tmp_path / "copy", cfg, resume=True, env=other)
    assert resumed.progress["environments"] == [envs[0], other]
    assert run_cold.Store(tmp_path / "copy", cfg, resume=True, env=other).progress["environments"] == [envs[0], other]


def test_report_has_no_per_user_or_article_content(full_run):
    out, d = full_run
    text = (out / run_cold.REPORT_JSON).read_text()
    assert "user_id" not in text and "article_id" not in text and "title" not in text
    with np.load(out / "units/e1_base.npz") as z:
        clusters = z["clusters"]
    assert clusters.min() == 0 and clusters.max() < 200     # 원 유저 id(500000대)가 아니라 묶음 번호


def test_assemble_alone_rebuilds_the_same_report_without_the_dataset(full_run, tmp_path):
    out, d = full_run
    js, md = tmp_path / "again.json", tmp_path / "again.md"
    rc = run_cold.main(["--dataset", "ebnerd_synth", "--root", str(tmp_path / "no-data-here"), "--out-dir", str(out),
                        *SMALL, "--stage", "assemble", "--out-json", str(js), "--out-md", str(md)])
    assert rc == 0
    assert json.loads(js.read_text()) == d and md.read_text() == (out / run_cold.REPORT_MD).read_text()


def test_resume_refuses_a_different_config_and_requires_the_flag(full_run, synth_root):
    out, _ = full_run
    before = (out / "progress.json").read_text()
    assert run_cold.main(_args(synth_root, out, "--stage", "e1")) == run_cold.EXIT_CONFIG_MISMATCH           # --resume 없음
    changed = [a if a != "120" else "121" for a in _args(synth_root, out, "--stage", "e1", "--resume")]
    assert run_cold.main(changed) == run_cold.EXIT_CONFIG_MISMATCH                                           # 표본 크기 다름
    assert (out / "progress.json").read_text() == before


def test_resume_skips_completed_units_without_rewriting_them(full_run, synth_root):
    out, _ = full_run
    files = sorted((out / "models").glob("*.txt")) + sorted((out / "units").glob("*.npz"))
    stamps = {f: f.stat().st_mtime_ns for f in files}
    assert run_cold.main(_args(synth_root, out, "--stage", "fit", "--resume")) == 0
    assert run_cold.main(_args(synth_root, out, "--stage", "e1", "--resume")) == 0
    assert {f: f.stat().st_mtime_ns for f in files} == stamps


def test_interrupted_run_resumes_to_the_same_numbers(full_run, synth_root, tmp_path, monkeypatch):
    """단계 중간에 죽은 뒤 이어서 돌린 결과가 한 번에 돌린 결과와 같아야 한다(모델·지표 배열·판정)."""
    _, want = full_run
    out = tmp_path / "interrupted"
    real_save_model, real_save_arrays = run_cold.Store.save_model, run_cold.Store.save_arrays
    calls = {"model": 0, "arrays": 0}

    def dying_save_model(self, *a, **k):
        calls["model"] += 1
        if calls["model"] == 4:
            raise KeyboardInterrupt("세션 끊김")
        return real_save_model(self, *a, **k)

    def dying_save_arrays(self, unit, *a, **k):
        calls["arrays"] += 1
        if calls["arrays"] == 2:
            raise KeyboardInterrupt("세션 끊김")
        return real_save_arrays(self, unit, *a, **k)

    monkeypatch.setattr(run_cold.Store, "save_model", dying_save_model)
    with pytest.raises(KeyboardInterrupt):
        run_cold.main(_args(synth_root, out, "--stage", "all"))
    done = json.loads((out / "progress.json").read_text())["units"]
    assert len([u for u in done if u.startswith("model:")]) == 3
    monkeypatch.setattr(run_cold.Store, "save_model", real_save_model)
    monkeypatch.setattr(run_cold.Store, "save_arrays", dying_save_arrays)
    with pytest.raises(KeyboardInterrupt):
        run_cold.main(_args(synth_root, out, "--stage", "all", "--resume"))
    assert "e1_base" in json.loads((out / "progress.json").read_text())["units"]
    assert not list((out / "units").glob("*.tmp*"))          # 반쯤 쓴 단위가 완료로 남지 않는다
    monkeypatch.setattr(run_cold.Store, "save_arrays", real_save_arrays)
    assert run_cold.main(_args(synth_root, out, "--stage", "all", "--resume")) == 0
    got = json.loads((out / run_cold.REPORT_JSON).read_text())
    assert _strip_timing(got) == _strip_timing(want)


def test_stored_model_predicts_exactly_like_the_model_that_was_saved(full_run, synth_bench):
    from evaluation.recsys.ebnerd.models import ALL_GROUPS
    from evaluation.recsys.ebnerd.prepare import impressions_in, p2_task, protocol_windows
    from recsys_core import compute_features

    out, _ = full_run
    store = run_cold.Store.open_existing(out)
    m = store.load_model("poolneg", 0)
    W = protocol_windows(synth_bench)
    task = p2_task(synth_bench, "validation", impressions_in(synth_bench.imps["validation"], W["test"])[:30])
    feats = compute_features(synth_bench.ctx["validation"], task.req, groups=ALL_GROUPS)
    again = store.load_model("poolneg", 0)
    assert np.array_equal(m.predict(feats, task), again.predict(feats, task))
    assert m.best_iteration == m.info["best_iteration"] and m.booster.num_trees() >= m.best_iteration
    assert m.features == m.info["features"] and SHRUNK_COLUMN not in m.features
    assert SHRUNK_COLUMN in store.load_model("poolneg_shrunk_a20", 0).features


# --- 재현 게이트를 e1의 첫 단위 직후에 본다 ----------------------------------------------------

def _prereg_with_gate(lo: float, hi: float) -> dict:
    d = copy.deepcopy(PREREG)
    d["reproduction_gate"]["ndcg10_seed_mean_within"] = [lo, hi]
    return d


def _copy_up_to_the_base_cell(src, dst):
    """끝난 실행의 체크포인트에서 e1의 서브샘플 단위와 리포트를 지운 사본: "e1_base까지 돈 실행"이다."""
    shutil.copytree(src, dst)
    progress = json.loads((dst / "progress.json").read_text())
    for unit in [u for u in progress["units"] if u.startswith("e1_sub")]:
        for f in progress["units"].pop(unit)["files"]:
            (dst / f).unlink()
    (dst / "progress.json").write_text(json.dumps(progress))
    for name in (run_cold.REPORT_JSON, run_cold.REPORT_MD, run_cold.GATE_JSON):
        (dst / name).unlink()
    return dst


def test_early_gate_is_the_number_the_report_judges(full_run):
    """e1 직후에 본 게이트와 리포트의 게이트가 다른 값이면 "먼저 본다"가 규칙을 바꾸는 것이 된다."""
    out, d = full_run
    early = run_cold.gate_from_checkpoint(run_cold.Store.open_existing(out), PREREG, [0])
    assert early == d["verdicts"]["gate"] and early["mean"] is not None
    assert early["mean"] == d["e1"]["cells"]["orig|full"]["methods"]["poolneg"]["ndcg@10"]["mean"]
    record = json.loads((out / run_cold.GATE_JSON).read_text())
    assert {k: record[k] for k in early} == early
    # 합성 데이터 실행은 demo 등급이라 게이트 값과 무관하게 끝까지 돈다(full_run이 종료 코드 0으로 끝났다)
    assert record["enforced"] is False and record["evidence_grade"] == DEMO_GRADE


def test_early_gate_averages_seeds_per_request_like_the_report(tmp_path):
    """손으로 만든 배열: 게이트 값은 요청별로 seed 평균을 낸 뒤의 요청 평균이고, 등록한 칸(orig·전체 풀)의 arm만 본다."""
    row = lambda *ndcg: np.array([ndcg, (0,) * len(ndcg), (0,) * len(ndcg)], dtype=np.float32)   # noqa: E731
    arrays = {"clusters": np.array([0, 0, 1], dtype=np.int32),
              "orig|full|poolneg|0": row(0.2, 0.4, np.nan), "orig|full|poolneg|1": row(0.4, 0.0, 0.3),
              "orig|40|poolneg|0": row(0.9, 0.9, 0.9), "pop0|full|poolneg|0": row(0.0, 0.0, 0.0),
              "orig|full|poolneg_mask0|0": row(0.8, 0.8, 0.8)}
    store = run_cold.Store(tmp_path / "s", {"hand": "made"}, resume=False)
    store.save_arrays("e1_base", arrays, {"n_requests": 3}, 0.0)
    gate = run_cold.gate_from_checkpoint(store, _prereg_with_gate(0.26, 0.27), [0, 1])
    assert gate["mean"] == pytest.approx((0.3 + 0.2 + 0.3) / 3, abs=1e-6) and gate["status"] == "pass"
    report_mean = run_cold._bank(arrays, "orig|full|", ["poolneg"], [0, 1], 10).summary("poolneg")["ndcg@10"]["mean"]
    assert gate["mean"] == report_mean
    assert run_cold.gate_from_checkpoint(store, _prereg_with_gate(0.27, 0.28), [0, 1])["status"] == "fail"
    # 기준 arm의 값이 없으면 실패가 아니라 미측정이다
    empty = run_cold.Store(tmp_path / "e", {"hand": "made"}, resume=False)
    empty.save_arrays("e1_base", {"clusters": np.zeros(0, dtype=np.int32)}, {"n_requests": 0}, 0.0)
    assert run_cold.gate_from_checkpoint(empty, PREREG, [0, 1]) == {
        "arm": "poolneg", "within": PREREG["reproduction_gate"]["ndcg10_seed_mean_within"], "mean": None,
        "status": "unmeasured"}


def test_gate_failure_stops_a_judged_run_right_after_the_base_cell(full_run, synth_root, tmp_path, monkeypatch):
    """판정용(증거 등급) 실행에서 게이트가 실패하면 나머지 예산을 쓰기 전에 멈춘다. 게이트 실패는 등록상 실행 무효다."""
    out = _copy_up_to_the_base_cell(full_run[0], tmp_path / "gate")
    monkeypatch.setattr(run_cold, "evidence_grade", lambda *a, **k: {"grade": EVIDENCE_GRADE, "reasons": []})
    monkeypatch.setattr(run_cold, "load_prereg", lambda: _prereg_with_gate(2.0, 3.0))      # nDCG로는 닿을 수 없는 구간
    assert run_cold.main(_args(synth_root, out, "--stage", "e1", "--resume")) == run_cold.EXIT_GATE_FAILED
    units = json.loads((out / "progress.json").read_text())["units"]
    assert "e1_base" in units and not any(u.startswith("e1_sub") for u in units)   # 서브샘플 칸을 계산하지 않았다
    record = json.loads((out / run_cold.GATE_JSON).read_text())
    assert record["status"] == "fail" and record["enforced"] is True and record["within"] == [2.0, 3.0]
    # 사슬 전체를 한 번에 돌리는 경로에서도 뒤 단계로 넘어가지 않고 리포트를 만들지 않는다
    assert run_cold.main(_args(synth_root, out, "--stage", "all", "--resume")) == run_cold.EXIT_GATE_FAILED
    assert not (out / run_cold.REPORT_JSON).exists()
    assert not any(u.startswith("e1_sub") for u in json.loads((out / "progress.json").read_text())["units"])


def test_gate_pass_lets_a_judged_run_continue(full_run, synth_root, tmp_path, monkeypatch):
    out = _copy_up_to_the_base_cell(full_run[0], tmp_path / "gate")
    mean = full_run[1]["verdicts"]["gate"]["mean"]
    monkeypatch.setattr(run_cold, "evidence_grade", lambda *a, **k: {"grade": EVIDENCE_GRADE, "reasons": []})
    monkeypatch.setattr(run_cold, "load_prereg", lambda: _prereg_with_gate(mean, mean))    # 경계 포함(lo <= mean <= hi)
    assert run_cold.main(_args(synth_root, out, "--stage", "e1", "--resume")) == 0
    units = json.loads((out / "progress.json").read_text())["units"]
    assert all(f"e1_{t}" in units for t in PREREG["conditions"]["subsample_fractions"])
    record = json.loads((out / run_cold.GATE_JSON).read_text())
    assert record["status"] == "pass" and record["enforced"] is True


def test_gate_failure_does_not_stop_a_demo_run(full_run, synth_root, tmp_path, monkeypatch):
    """demo 등급 실행은 판정에 쓰지 않으므로 게이트로 멈추지 않는다(배선 확인이 끝까지 가야 한다)."""
    out = _copy_up_to_the_base_cell(full_run[0], tmp_path / "gate")
    monkeypatch.setattr(run_cold, "load_prereg", lambda: _prereg_with_gate(2.0, 3.0))
    assert run_cold.main(_args(synth_root, out, "--stage", "e1", "--resume")) == 0
    record = json.loads((out / run_cold.GATE_JSON).read_text())
    assert record["status"] == "fail" and record["enforced"] is False


# --- 조건별 입력이 등록한 순서로 만들어지는지 ---------------------------------------------------

class _FakeRun:
    prereg = PREREG

    def arm_cfg(self, arm):
        return PREREG["arms"][arm]


def _raw(synth_bench, n=40):
    from evaluation.recsys.ebnerd.models import ALL_GROUPS
    from evaluation.recsys.ebnerd.prepare import impressions_in, p2_task, protocol_windows
    from recsys_core import compute_features

    W = protocol_windows(synth_bench)
    task = p2_task(synth_bench, "validation", impressions_in(synth_bench.imps["validation"], W["test"])[:n])
    ctx = synth_bench.ctx["validation"]
    return task, ctx, compute_features(ctx, task.req, groups=ALL_GROUPS)


def test_pop0_is_applied_at_the_raw_stage_before_rank_and_also_zeroes_shrunk_ctr(synth_bench):
    task, ctx, raw = _raw(synth_bench)
    pop = list(POP_RAW_COLUMNS)
    plain = run_cold._eval_view(_FakeRun(), "poolneg", raw, task.req, ctx, task.req.cand_ptr, 0.0)
    assert np.all(plain[pop].to_numpy() == 0.0) and plain["hist_cos"].equals(raw["hist_cos"])
    shrunk = run_cold._eval_view(_FakeRun(), "poolneg_shrunk_a20", raw, task.req, ctx, task.req.cand_ptr, 0.0)
    assert np.all(shrunk[pop + [SHRUNK_COLUMN]].to_numpy() == 0.0)
    warm = run_cold._eval_view(_FakeRun(), "poolneg_shrunk_a20", raw, task.req, ctx, task.req.cand_ptr, None)
    assert (warm[SHRUNK_COLUMN] > 0).any()
    rank = run_cold._eval_view(_FakeRun(), "poolneg_rank", raw, task.req, ctx, task.req.cand_ptr, 0.0)
    # raw 0을 먼저 넣고 랭크로 바꾸므로 인기도 열은 전부 동점(0.5)이다. 순서가 반대면 0이 된다.
    assert np.all(rank[pop].to_numpy() == 0.5)
    cols = PREREG["features"]["rank_columns"]
    assert rank[cols].to_numpy().min() >= 0.0 and rank[cols].to_numpy().max() <= 1.0
    untouched = [c for c in raw.columns if c not in cols]
    pd.testing.assert_frame_equal(rank[untouched], raw[untouched])
    nan = run_cold._eval_view(_FakeRun(), "poolneg_masknan", raw, task.req, ctx, task.req.cand_ptr, float("nan"))
    assert nan[pop].isna().all().all()


def test_training_frames_mask_the_same_requests_for_the_zero_and_nan_arms(synth_bench):
    task, ctx, raw = _raw(synth_bench, n=200)

    class R(_FakeRun):
        off = PREREG["seed_offsets"]

    pop = list(POP_RAW_COLUMNS)
    (_, f0), (_, e0), info0 = run_cold._training_frames(R(), "poolneg_mask0", (task, raw), (task, raw), ctx, seed=1)
    (_, fn), (_, en), infon = run_cold._training_frames(R(), "poolneg_masknan", (task, raw), (task, raw), ctx, seed=1)
    z, n = (f0[pop] == 0).all(axis=1).to_numpy(), fn[pop].isna().all(axis=1).to_numpy()
    originally_zero = (raw[pop] == 0).all(axis=1).to_numpy()
    assert np.array_equal(z & ~originally_zero, n & ~originally_zero) and n.any() and not n.all()
    assert info0 == infon and 0.3 < info0["masked_fit_request_share"] < 0.7
    # es는 fit 다음 난수로 가린다: 같은 과제를 넣어도 fit과 다른 요청이 가려진다
    assert not np.array_equal(fn[pop].isna().all(axis=1).to_numpy(), en[pop].isna().all(axis=1).to_numpy())
    (_, plain), _, info = run_cold._training_frames(R(), "poolneg", (task, raw), (task, raw), ctx, seed=1)
    assert plain is raw and info == {}


# --- 증거 등급 ---------------------------------------------------------------------------------

def _registered_config():
    """원격 런타임의 판정용 실행: 등록한 인자, 등록한 입력 4개, 메타 전용 기사 파일(+manifest가 적은 원본 sha)."""
    run = PREREG["run"]
    return {"dataset": run["dataset"], "seeds": run["seeds"], "p2_sample": run["p2_sample"], "sub_cap": run["sub_cap"],
            "fake_dim": None, "max_fit": None, "max_test": None,
            "data_files": {**PREREG["data"]["files_sha256"], "articles.parquet": "d" * 64},
            "articles_original_sha256_manifest": PREREG["data"]["articles_original_sha256"],
            "embeddings_sha256": PREREG["data"]["embeddings_sha256"], "code_sha": "a" * 40,
            "prereg_sha256": prereg_sha256()}


def test_only_the_registered_arguments_and_inputs_earn_the_evidence_grade():
    ok = run_cold.evidence_grade(_registered_config(), PREREG["run"]["n_boot"], PREREG)
    assert ok == {"grade": EVIDENCE_GRADE, "reasons": []}
    deviations = [("dataset", "ebnerd_demo"), ("seeds", [0]), ("p2_sample", 300), ("sub_cap", 100), ("fake_dim", 8),
                  ("max_fit", 1000), ("max_test", 1000), ("embeddings_sha256", "x"), ("code_sha", "unknown"),
                  ("code_sha", "a" * 40 + "-dirty"), ("code_sha", "tarball-0123456789abcdef"),
                  ("prereg_sha256", "0" * 64),
                  # 기사 파일은 파생본이라 등록한 sha가 없다. 원본이 등록값이라는 기록이 없거나 다르면 판정용이 아니다
                  ("articles_original_sha256_manifest", None), ("articles_original_sha256_manifest", "0" * 64)]
    for key, value in deviations:
        cfg = _registered_config()
        cfg[key] = value
        g = run_cold.evidence_grade(cfg, PREREG["run"]["n_boot"], PREREG)
        assert g["grade"] == DEMO_GRADE and len(g["reasons"]) == 1, key
    cfg = _registered_config()
    cfg["data_files"]["train/behaviors.parquet"] = "deadbeef"
    assert run_cold.evidence_grade(cfg, PREREG["run"]["n_boot"], PREREG)["grade"] == DEMO_GRADE
    assert run_cold.evidence_grade(_registered_config(), 200, PREREG)["grade"] == DEMO_GRADE
    # 원본 기사 파일을 그대로 쓴 실행(파생본·manifest 없음)은 파일 sha가 곧 등록값이다
    cfg = _registered_config()
    cfg["articles_original_sha256_manifest"] = None
    cfg["data_files"]["articles.parquet"] = PREREG["data"]["articles_original_sha256"]
    assert run_cold.evidence_grade(cfg, PREREG["run"]["n_boot"], PREREG) == {"grade": EVIDENCE_GRADE, "reasons": []}


def test_report_separates_the_registered_articles_sha_from_what_the_run_was_told(synth_root, tmp_path, monkeypatch, caplog):
    """리포트의 기사 원본 sha는 등록 상수를 베낀 값과 이 실행이 받은 값(manifest → 드라이버 → 환경변수)을 따로 적는다.
    단계 함수는 빈 것으로 바꿔, 환경변수 → 설정 → progress.json → 리포트로 가는 길만 본다."""
    import logging

    from evaluation.recsys.ebnerd.prepare import sha256_file

    monkeypatch.setitem(run_cold.STAGE_FUNCS, "fit", lambda run: None)
    registered = PREREG["data"]["articles_original_sha256"]
    file_sha = sha256_file(synth_root / "ebnerd_synth" / "articles.parquet")

    def report(out, value):
        if value is None:
            monkeypatch.delenv(run_cold.ARTICLES_ORIGINAL_ENV, raising=False)
        else:
            monkeypatch.setenv(run_cold.ARTICLES_ORIGINAL_ENV, value)
        assert run_cold.main(_args(synth_root, out, "--stage", "fit")) == 0
        monkeypatch.delenv(run_cold.ARTICLES_ORIGINAL_ENV, raising=False)      # 조립은 다른 기계에서 다시 할 수 있다
        assert run_cold.main(_args(synth_root, out, "--stage", "assemble")) == 0
        return json.loads((out / run_cold.REPORT_JSON).read_text())["meta"], (out / run_cold.REPORT_MD).read_text()

    with caplog.at_level(logging.INFO, logger="ebnerd.cold"):
        meta, md = report(tmp_path / "told", "e" * 64)
    assert meta["articles_original_sha256_registered"] == registered
    assert meta["articles_original_sha256_manifest"] == "e" * 64 and meta["articles_file_sha256"] == file_sha
    assert meta["articles_linked_to_registration"] is False and "articles_original_sha256" not in meta
    assert any("기사 파일" in r for r in meta["evidence"]["reasons"])
    assert "e" * 12 in md and registered[:12] in md and file_sha[:12] in md
    # 실행을 시작할 때 등급과 사유를 로그에 찍는다(몇 시간 뒤 조립 때가 아니라)
    assert any("evidence grade" in r.getMessage() and DEMO_GRADE in r.getMessage() for r in caplog.records)

    meta, md = report(tmp_path / "linked", registered)
    assert meta["articles_original_sha256_manifest"] == registered and meta["articles_linked_to_registration"] is True
    assert not any("기사 파일" in r for r in meta["evidence"]["reasons"])

    meta, md = report(tmp_path / "untold", None)
    assert meta["articles_original_sha256_manifest"] is None and meta["articles_linked_to_registration"] is False
    assert "받지 못함" in md


def test_unknown_stage_is_rejected(synth_root, tmp_path):
    with pytest.raises(SystemExit):
        run_cold.main(_args(synth_root, tmp_path / "x", "--stage", "e5"))


# --- demo 데이터(로컬 전용) --------------------------------------------------------------------

DEMO = ebnerd_root() / "ebnerd_demo"


@pytest.mark.skipif(not (DEMO / "articles.parquet").exists(), reason="EB-NeRD demo 데이터 없음 (로컬 전용)")
def test_chain_runs_on_ebnerd_demo_schema_and_stays_demo_grade(tmp_path):
    out = tmp_path / "demo"
    rc = run_cold.main(["--dataset", "ebnerd_demo", "--root", str(ebnerd_root()), "--fake-dim", "8",
                        "--out-dir", str(out), "--seeds", "0", "--n-boot", "20", "--p2-sample", "60", "--sub-cap", "60",
                        "--max-fit", "1200", "--max-test", "1200", "--threads", "2", "--stage", "all"])
    assert rc == 0
    d = json.loads((out / run_cold.REPORT_JSON).read_text())
    assert d["meta"]["evidence"]["grade"] == DEMO_GRADE and d["verdicts"]["judgeable"] is False
    assert d["unmeasured"]["stages"] == [] and "orig|full" in d["e1"]["cells"]


# --- v1과 같은 표본·네거티브를 쓰는지(재현 게이트의 전제) --------------------------------------

def test_p2_sample_and_pool_negatives_are_drawn_exactly_like_v1(synth_bench):
    """재현 게이트는 poolneg가 v1 ranker_v2_poolneg와 같은 표본·네거티브에서 나왔다는 전제 위에 선다."""
    import argparse

    from evaluation.recsys.ebnerd.models import NEGATIVE_VARIANTS, V2_FEATURES
    from evaluation.recsys.ebnerd.prepare import impressions_in, pool_negative_task, protocol_windows
    from evaluation.recsys.ebnerd.run_ebnerd import SPLIT_SEED

    assert PREREG["statistics"]["split_seed"] == SPLIT_SEED
    W = protocol_windows(synth_bench)
    idx = {"fit": impressions_in(synth_bench.imps["train"], W["fit"]),
           "es": impressions_in(synth_bench.imps["train"], W["es"]),
           "test": impressions_in(synth_bench.imps["validation"], W["test"])}
    run = run_cold.Run(args=argparse.Namespace(p2_sample=100, sub_cap=50, threads=2), prereg=PREREG, bench=synth_bench,
                       windows=W, idx=idx, store=None, seeds=[0])
    # v1 run_p2: default_rng(SPLIT_SEED + 2).choice(test, size, replace=False) 후 정렬
    want = np.sort(np.random.default_rng(SPLIT_SEED + 2).choice(idx["test"], size=100, replace=False))
    assert np.array_equal(run.p2_idx(), want)
    # v1 run_p1: default_rng(1000 + seed)에서 fit -> es 순
    assert PREREG["seed_offsets"]["pool_negatives"] == 1000
    r = np.random.default_rng(1000 + 0)
    tf = pool_negative_task(synth_bench, "train", idx["fit"], r)
    te = pool_negative_task(synth_bench, "train", idx["es"], r)
    r2 = np.random.default_rng(PREREG["seed_offsets"]["pool_negatives"] + 0)
    tf2 = pool_negative_task(synth_bench, "train", idx["fit"], r2, window_h=48)
    te2 = pool_negative_task(synth_bench, "train", idx["es"], r2, window_h=48)
    assert np.array_equal(tf.req.cand_item, tf2.req.cand_item) and np.array_equal(te.req.cand_item, te2.req.cand_item)
    # arm 정의: 기준 arm은 v1 ranker_v2_poolneg와 같은 피처·목적함수·쿼리 단위
    spec, v1 = run.arm_spec("poolneg"), NEGATIVE_VARIANTS[0]
    assert (spec.features, spec.objective, spec.group, spec.data) == (v1.features, v1.objective, v1.group, v1.data)
    assert run.arm_spec("poolneg_shrunk_a5").features == V2_FEATURES + [SHRUNK_COLUMN]
    assert run.arm_spec("ranker_v2").data == "inview" and run.arm_spec("ranker_v2").features == V2_FEATURES


def test_subsample_definitions_are_shared_between_fit_and_evaluation(synth_bench):
    import argparse

    from evaluation.recsys.ebnerd.cold_transforms import behaviour_users, subsample_users
    from evaluation.recsys.ebnerd.prepare import impressions_in, protocol_windows
    from evaluation.recsys.ebnerd.run_ebnerd import SPLIT_SEED

    W = protocol_windows(synth_bench)
    idx = {"test": impressions_in(synth_bench.imps["validation"], W["test"])}
    run = run_cold.Run(args=argparse.Namespace(p2_sample=100, sub_cap=30, threads=2), prereg=PREREG, bench=synth_bench,
                       windows=W, idx=idx, store=None, seeds=[0])
    s1, s5, s20 = (run.subsample(t) for t in ("sub1", "sub5", "sub20"))
    assert set(s1["users"]) <= set(s5["users"]) <= set(s20["users"])
    assert np.array_equal(s20["users"], subsample_users(behaviour_users(synth_bench), 0.20, SPLIT_SEED + 11))
    va = synth_bench.imps["validation"]
    assert len(s20["idx"]) == 30 and np.isin(va.user_id[s20["idx"]], s20["users"]).all()   # 상한 적용
    assert run.subsample("sub20") is s20                                                  # 한 번 만든 것을 다시 쓴다
    ctx = run.sub_ctx("validation", "sub20")
    assert ctx.item_clicks is s20["clicks"] and ctx.user_log is synth_bench.ctx["validation"].user_log
