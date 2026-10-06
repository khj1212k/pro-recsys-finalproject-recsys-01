"""E15 실행기(run_neural): 합성 데이터로 두 과제의 전 단계, 체크포인트 재개, 환경이 바뀐 신경망 단위의 폐기, 게이트, 등급.

EB-NeRD 없이 CPU에서 수십 초에 돈다. torch가 없으면 전 구간 테스트는 건너뛰고 torch가 필요 없는 것만 돈다.
여기서 나오는 수치는 전부 합성 데이터의 것이라 해석하지 않는다(배선만 본다).
"""
import json
import shutil

import lightgbm  # noqa: F401 — macOS에서는 torch보다 먼저 불러야 한다(neural/train.py의 주석)
import numpy as np
import pytest

from evaluation.recsys.ebnerd import run_neural as R
from evaluation.recsys.ebnerd.loaders import ebnerd_root
from evaluation.recsys.ebnerd.neural import stack as K
from evaluation.recsys.ebnerd.neural.report import DEMO_GRADE, EVIDENCE_GRADE, load_prereg, prereg_sha256
from evaluation.recsys.ebnerd.neural.sequences import keys_sha256
from evaluation.recsys.ebnerd.prepare import impressions_in, pool_negative_task, protocol_windows
from evaluation.recsys.ebnerd.run_ebnerd import SPLIT_SEED

PREREG = load_prereg()
SMALL = ["--seeds", "0", "--n-boot", "30", "--p2-sample", "150", "--p2-cold-sample", "200", "--p2-select-sample", "80",
         "--neural-trials", "1", "--gbdt-trials", "2", "--tune-max-epochs", "1", "--final-max-epochs", "2",
         "--threads", "2", "--device", "cpu", "--resume"]


def _has_torch() -> bool:
    """진짜 torch가 설치돼 있는가. 다른 테스트가 sys.modules에 넣어 둔 가짜 모듈(파일이 없다)은 치지 않는다."""
    try:
        import torch
    except ImportError:
        return False
    return getattr(torch, "__file__", None) is not None


torch_only = pytest.mark.skipif(not _has_torch(), reason="torch가 없다(신경망 단계는 torch가 있는 CI job과 로컬에서 돈다)")


def _args(root, out, *extra):
    return ["--dataset", "ebnerd_synth", "--root", str(root), "--out-dir", str(out), *SMALL, *extra]


@pytest.fixture(scope="module")
def full_run(synth_root, tmp_path_factory):
    if not _has_torch():
        pytest.skip("torch가 없다")
    out = tmp_path_factory.mktemp("e15_full")
    for task in ("p1", "p2"):
        assert R.main(_args(synth_root, out, "--task", task, "--stage", "all")) == 0
    return out, json.loads((out / R.REPORT_JSON).read_text())


def _strip_timing(d):
    d = json.loads(json.dumps(d))
    d["meta"].pop("stage_log", None)
    d["meta"].pop("assembled_on", None)
    return d


@torch_only
def test_chain_runs_every_registered_stage_for_both_tasks_and_labels_the_result_demo(full_run):
    out, d = full_run
    assert d["meta"]["evidence"]["grade"] == DEMO_GRADE and d["verdict"]["judged"] is False and d["verdict"]["claim"] == "none"
    assert d["verdict"]["shadow_eligible"] == []
    assert "demo, not evidence" in (out / R.REPORT_MD).read_text()
    stages = {(s["task"], s["stage"]) for s in d["meta"]["stage_log"]}
    # 리포트는 마지막 assemble이 쓰므로 그 단계 자신의 기록은 리포트에 아직 없다
    assert stages | {("p2", "assemble")} == {(t, s) for t in ("p1", "p2") for s in PREREG["run"]["stages"]}
    for t in ("p1", "p2"):
        block = d["tasks"][t]
        assert set(block["arms"]) == {"A", "A_star", "A_plus", "nrms", "sasrec", "D", "sel_no_scalars", "A_star_noaug", "A_star_b234"}
        assert all(block["units"].values()) and d["unmeasured"]["units_not_run"] == []
        sel = d["selection"][t]["family"]
        assert sel in PREREG["neural"]["families"] and d["selection"][t]["uses_test"] is False
        for arm in (sel, "D"):
            e = block["diffs"][f"{arm}-vs-A_star"]
            assert len(e["per_seed"]) == 1 and set(e["holm_lo"]) == {"1", "2", "3", "4"} and e["ci90"][0] >= e["ci95"][0]
        assert set(block["cold"]) == set(PREREG["cold"]["conditions"])
        for c in block["cold"].values():
            assert {f"{sel}-vs-A_star", "D-vs-A_star", "A_plus-vs-A_star"} == set(c["diffs"])
        assert block["cold"]["k0"]["meta"]["sequences_nonempty_share"] == 0.0        # k=0이면 시퀀스가 전부 비어 있다
        assert len({c["meta"]["scalar_matrix_sha256"] for c in block["cold"].values()}) == 5
        assert d["validity"][t]["determinism"]["status"] == "pass"
        assert d["protocol"][t]["budget"]["gbdt_per_arm"] == d["protocol"][t]["budget"]["neural_total"] == 2
        assert len(d["trials"][t]["A_star"]) == len(d["trials"][t]["A_plus"]) == 2
        assert len(d["trials"][t]["nrms"]) + len(d["trials"][t]["sasrec"]) == 2
        assert d["trials"][t]["A_star"][0]["config"] == PREREG["gbdt"]["trial0"]
    # seed가 하나뿐이라 seed 규칙은 계산되지 않는다: 미측정이지 기각이 아니다
    assert {r["comparison"]["status"] for arms in d["verdict"]["by_task"].values() for r in arms.values()} == {"unmeasured"}
    assert set(d["unmeasured"]["not_implemented_descriptive"]) == set(R.NOT_IMPLEMENTED)


@torch_only
def test_stacking_scores_come_from_forward_chained_block_models(full_run):
    _, d = full_run
    for t in ("p1", "p2"):
        meta = d["tasks"][t]["unit_meta"][f"{t}__n__test_D"]
        audit = meta["forward_chain"]["0"]
        assert audit["blocks"] == PREREG["windows"]["fit_blocks"] and audit["block_models"] == 3
        assert audit["min_seconds_after_training_data"] > 0 and audit["unscored_requests"] > 0
        assert audit["scored_requests"] + audit["unscored_requests"] == d["meta"]["data"]["impressions"]["fit"]
        assert meta["neural_family"] == d["selection"][t]["family"]
        assert meta["neural_score_rank_gain_share"]["0"] is not None


@torch_only
def test_block_models_recorded_that_they_were_fitted_only_on_earlier_blocks(full_run, synth_bench):
    """리포트의 감사 기록은 블록 모델이 학습 입력에서 직접 적은 값이다(계획한 행 목록을 다시 읽은 것이 아니다):
    블록 j 모델의 학습 요청 수 = 블록 < j의 요청 수, 학습 요청의 최대 시각 < 블록 j의 시작."""
    _, d = full_run
    W = protocol_windows(synth_bench)
    times = synth_bench.imps["train"].time[impressions_in(synth_bench.imps["train"], W["fit"])]
    edges = K.block_edges(W["fit"], PREREG["windows"]["block_hours"])
    block = K.block_of(times, edges)
    for t in ("p1", "p2"):
        audit = d["tasks"][t]["unit_meta"][f"{t}__n__test_D"]["forward_chain"]["0"]
        records = audit["block_model_records"]
        assert [r["block"] for r in records] == [2, 3, 4] == list(range(2, PREREG["windows"]["fit_blocks"] + 1))
        for r in records:
            j = r["block"]
            assert r["train_requests"] == int(((block >= 1) & (block < j)).sum()) < len(times)
            assert r["train_max_time"] == int(times[(block >= 1) & (block < j)].max()) < edges[j - 1] <= r["target_min_time"]
            assert r["stats_pairs"] == r["train_pairs"] and r["target_requests"] == int((block == j).sum())
        final = audit["final_model_record"]
        assert final["train_requests"] == len(times) and final["train_max_time"] == int(times.max()) < W["es"][0]
        assert final["train_max_time"] < final["target_min_time"]


@torch_only
def test_a_block_model_fitted_on_the_whole_window_stops_the_stack_stage(full_run, synth_root, tmp_path, monkeypatch):
    """블록 모델이 넘겨받은 행을 무시하고 fit 전체로 학습하면(예: 부분 집합을 빠뜨린 회귀) 스태킹 단계가 멈춘다."""
    out, _ = full_run
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    progress = json.loads((copy / "progress.json").read_text())
    for unit in ("p1__n__test_D", "model:p1_n_D:s0"):
        progress["units"].pop(unit)
    (copy / "progress.json").write_text(json.dumps(progress))
    real = R._fit_neural
    monkeypatch.setattr(R, "_fit_neural", lambda run, env, spec, seed, max_epochs, rows=None, fixed_epochs=None:
                        real(run, env, spec, seed, max_epochs, rows=None, fixed_epochs=fixed_epochs))
    with pytest.raises(K.LeakageError, match="블록 2 모델"):
        R.main(_args(synth_root, copy, "--task", "p1", "--stage", "stack"))


@torch_only
def test_both_tasks_use_the_v1_samples_and_negatives(full_run, synth_bench):
    """P2 판정 표본과 풀 네거티브가 v1(run_ebnerd)의 것과 같은 난수에서 나온다."""
    _, d = full_run
    W = protocol_windows(synth_bench)
    tr, va = synth_bench.imps["train"], synth_bench.imps["validation"]
    test_idx = impressions_in(va, W["test"])
    p2 = np.sort(np.random.default_rng(SPLIT_SEED + 2).choice(test_idx, size=150, replace=False))
    assert d["protocol"]["p2"]["gate"]["sample_sha256"] == keys_sha256(p2)
    assert d["protocol"]["p1"]["gate"]["sample_sha256"] == keys_sha256(test_idx)
    rng = np.random.default_rng(1000 + 0)
    fit = pool_negative_task(synth_bench, "train", impressions_in(tr, W["fit"]), rng, n_neg=20, window_h=48)
    es = pool_negative_task(synth_bench, "train", impressions_in(tr, W["es"]), rng, n_neg=20, window_h=48)
    h = d["protocol"]["p2"]["gate"]["negative_hash"]
    assert h["fit|0"] == keys_sha256(fit.req.user, fit.req.time, fit.req.cand_item)
    assert h["es|0"] == keys_sha256(es.req.user, es.req.time, es.req.cand_item)
    assert d["tasks"]["p2"]["cold"]["pop0"]["meta"]["n_requests"] == 200
    # A*가 trial 0(팀 설정)을 골랐고 증강을 빼면 P2에서는 A와 같은 모델이다(같은 데이터·같은 파라미터)
    if d["tasks"]["p2"]["unit_meta"]["p2__test_gbdt"]["best"]["A_star"]["trial"] == 0:
        assert d["tasks"]["p2"]["diffs"]["A_star_noaug-vs-A"]["diff"] == 0.0


@torch_only
def test_rerun_skips_finished_units_and_assemble_alone_rebuilds_the_same_report(full_run, synth_root, tmp_path):
    out, d = full_run
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    before = {p.name: p.stat().st_mtime_ns for p in (copy / "models").iterdir()}
    units = json.loads((copy / "progress.json").read_text())["units"]
    assert R.main(_args(synth_root, copy, "--task", "p1", "--stage", "all")) == 0
    assert {p.name: p.stat().st_mtime_ns for p in (copy / "models").iterdir()} == before          # 다시 학습하지 않았다
    assert json.loads((copy / "progress.json").read_text())["units"] == units
    assert _strip_timing(json.loads((copy / R.REPORT_JSON).read_text())) == _strip_timing(d)
    (copy / R.REPORT_JSON).unlink()
    assert R.main(["--stage", "assemble", "--out-dir", str(copy), "--n-boot", "30"]) == 0          # 데이터 없이
    assert _strip_timing(json.loads((copy / R.REPORT_JSON).read_text())) == _strip_timing(d)


@torch_only
def test_changed_torch_or_device_discards_that_tasks_neural_units_only(full_run, synth_root, tmp_path):
    out, _ = full_run
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    (copy / "p1__neural_env.json").write_text(json.dumps({"torch": "0.0.0", "device": "cuda", "gpu": "Other GPU"}))
    assert R.main(_args(synth_root, copy, "--task", "p1", "--stage", "determinism")) == 0
    units = set(json.loads((copy / "progress.json").read_text())["units"])
    assert {u for u in units if R.is_neural_derived("p1", u)} == {"p1__n__determinism"}          # 방금 다시 돈 것만 남았다
    assert {"p1__gate", "p1__test_gbdt", "model:p1_A_star:s0", "p1__tune_A_star_t00", "p1__narr_A_star_noaug"} <= units
    assert {"p2__n__selection", "p2__n__test_D", "model:p2_n_D:s0", "p2__n__cold_k0"} <= units    # 다른 과제는 그대로
    assert R.main(["--stage", "assemble", "--out-dir", str(copy), "--n-boot", "30"]) == 0
    d = json.loads((copy / R.REPORT_JSON).read_text())
    assert d["selection"].get("p1") is None and {"p1:sel", "p1:D"} <= set(d["verdict"]["unmeasured"]) | {
        f"p1:{a}" for a, r in d["verdict"]["by_task"]["p1"].items() if r["comparison"]["status"] == "unmeasured"}
    assert d["tasks"]["p1"]["units"]["p1__n__test_D"] is False and d["tasks"]["p2"]["units"]["p2__n__test_D"] is True


@torch_only
def test_gate_recheck_in_another_session_is_recorded_without_touching_the_first_result(full_run, synth_root, tmp_path):
    out, _ = full_run
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    first = (copy / "units" / "p1__gate.npz").read_bytes()
    assert R.main(_args(synth_root, copy, "--task", "p1", "--stage", "gate", "--recheck-gate", "s3")) == 0
    rec = json.loads((copy / "p1__gate_recheck_s3_result.json").read_text())
    assert rec["recheck"] == "s3" and rec["max_abs_diff_vs_first_session"] == 0.0 and rec["enforced"] is False
    assert (copy / "units" / "p1__gate.npz").read_bytes() == first
    assert R.main(["--stage", "assemble", "--out-dir", str(copy), "--n-boot", "30"]) == 0
    assert [r["recheck"] for r in json.loads((copy / R.REPORT_JSON).read_text())["validity"]["p1"]["rechecks"]] == ["s3"]


# --- torch 없이 도는 것 ------------------------------------------------------------------------

def test_unequal_tuning_budget_is_refused_before_anything_runs(tmp_path):
    out = tmp_path / "o"
    argv = ["--dataset", "ebnerd_synth", "--root", str(tmp_path / "missing"), "--out-dir", str(out), "--task", "p1",
            "--stage", "gate", "--neural-trials", "1", "--gbdt-trials", "3"]
    assert R.main(argv) == R.EXIT_USAGE and not out.exists()
    assert R.main(["--out-dir", str(out), "--stage", "gate"]) == R.EXIT_USAGE          # 과제 없이 계산 단계를 돌릴 수 없다
    assert R.main(["--out-dir", str(out), "--stage", "nope", "--task", "p1"]) == R.EXIT_USAGE


def test_gbdt_stages_run_without_torch_and_refuse_a_changed_configuration(synth_root, tmp_path):
    out = tmp_path / "o"
    for stage in ("gate", "gbdt_tune", "gbdt_final"):
        assert R.main(_args(synth_root, out, "--task", "p2", "--stage", stage)) == 0
    units = json.loads((out / "progress.json").read_text())["units"]
    assert {"p2__gate", "p2__tune_A_star_t01", "p2__tune_A_plus_t01", "p2__test_gbdt", "model:p2_A_plus:s0"} <= set(units)
    gate = json.loads((out / "p2__gate_result.json").read_text())
    assert gate["status"] == "fail" and gate["enforced"] is False        # 합성 데이터는 v1 구간 밖이지만 demo라 멈추지 않는다
    changed = [a if a != "150" else "140" for a in _args(synth_root, out, "--task", "p2", "--stage", "gate")]
    assert R.main(changed) == R.EXIT_CONFIG_MISMATCH


def test_reproduction_gate_failure_stops_an_evidence_grade_run(synth_root, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "evidence_grade", lambda *a, **k: {"grade": EVIDENCE_GRADE, "reasons": []})
    out = tmp_path / "o"
    assert R.main(_args(synth_root, out, "--task", "p1", "--stage", "gate")) == R.EXIT_GATE_FAILED
    rec = json.loads((out / "p1__gate_result.json").read_text())
    assert rec["enforced"] is True and rec["status"] == "fail" and set(rec["checks"]) == {"judged", "seen_incl"}


def test_gate_values_require_every_seed_and_every_interval():
    def arrays(judged, seen, seeds=(0, 1, 2)):
        a = {"clusters": np.zeros(4, dtype=np.int32)}
        for s in seeds:
            a[f"judged|A|{s}"] = np.full((4, 4), judged, dtype=np.float32)
            a[f"seen_incl|A|{s}"] = np.full((4, 4), seen, dtype=np.float32)
        return a

    assert R.gate_values(arrays(0.663, 0.655), "p1", [0, 1, 2], PREREG)["status"] == "pass"
    assert R.gate_values(arrays(0.663, 0.650), "p1", [0, 1, 2], PREREG)["status"] == "fail"     # seen 포함판이 구간 밖
    assert R.gate_values(arrays(0.670, 0.655), "p1", [0, 1, 2], PREREG)["status"] == "fail"
    assert R.gate_values(arrays(0.663, 0.655, seeds=(0, 1)), "p1", [0, 1, 2], PREREG)["status"] == "unmeasured"
    assert R.gate_values(arrays(0.268, 0.0), "p2", [0, 1, 2], PREREG)["status"] == "pass"
    assert R.gate_values(arrays(0.2639, 0.0), "p2", [0, 1, 2], PREREG)["status"] == "fail"
    assert R.gate_values({}, "p2", [0, 1, 2], PREREG)["status"] == "unmeasured"


def test_evidence_grade_requires_every_registered_argument_and_input():
    reg = PREREG["run"]
    config = {k: reg[k] for k in R.GRADE_KEYS}
    config.update(data_files={**PREREG["data"]["files_sha256"], "articles.parquet": PREREG["data"]["articles_original_sha256"]},
                  embeddings_sha256=PREREG["data"]["embeddings_sha256"], code_sha="a" * 40, prereg_sha256=prereg_sha256())
    assert R.evidence_grade(config, reg["n_boot"], PREREG, 4) == {"grade": EVIDENCE_GRADE, "reasons": []}
    for change in ({"seeds": [0]}, {"gbdt_trials": 12}, {"final_max_epochs": 5}, {"p2_cold_sample": 20000},
                   {"fake_dim": 16}, {"code_sha": "a" * 40 + "-dirty"}, {"code_sha": "unknown"},
                   {"embeddings_sha256": "0" * 64}, {"prereg_sha256": "0" * 64}):
        g = R.evidence_grade({**config, **change}, reg["n_boot"], PREREG, 4)
        assert g["grade"] == DEMO_GRADE and len(g["reasons"]) == 1
    assert R.evidence_grade(config, 200, PREREG, 4)["grade"] == DEMO_GRADE
    assert R.evidence_grade(config, reg["n_boot"], PREREG, 3)["grade"] == DEMO_GRADE              # fit 블록이 4개가 아니다
    derived = {**config, "data_files": {**config["data_files"], "articles.parquet": "1" * 64}}     # 메타 전용 파생본
    assert R.evidence_grade(derived, reg["n_boot"], PREREG, 4)["grade"] == DEMO_GRADE
    linked = {**derived, "articles_original_sha256_manifest": PREREG["data"]["articles_original_sha256"]}
    assert R.evidence_grade(linked, reg["n_boot"], PREREG, 4)["grade"] == EVIDENCE_GRADE


def test_cli_defaults_are_the_registered_run_arguments():
    args = R.build_parser().parse_args(["--out-dir", "x", "--stage", "gate", "--task", "p1"])
    assert {k: getattr(args, k) for k in R.GRADE_KEYS} == {k: PREREG["run"][k] for k in R.GRADE_KEYS}
    assert args.n_boot == PREREG["run"]["n_boot"] and set(R.STAGE_FUNCS) | {"assemble"} == set(PREREG["run"]["stages"])


@pytest.mark.skipif(not (ebnerd_root() / "ebnerd_demo" / "articles.parquet").exists() or not _has_torch(),
                    reason="EB-NeRD demo 데이터가 없다(로컬 전용, 라이선스상 저장소·CI에 두지 않는다)")
def test_demo_dataset_runs_one_task_end_to_end_and_is_labelled_not_evidence(tmp_path):
    """실데이터 스키마(ebnerd_demo)와 무작위 임베딩으로 P1 사슬을 짧게 돈다. 결과는 demo, not evidence."""
    out = tmp_path / "demo"
    argv = ["--dataset", "ebnerd_demo", "--root", str(ebnerd_root()), "--fake-dim", "16", "--out-dir", str(out),
            "--max-fit", "1500", "--max-test", "1500", *SMALL, "--task", "p1", "--stage", "all"]
    assert R.main(argv) == 0
    d = json.loads((out / R.REPORT_JSON).read_text())
    assert d["meta"]["evidence"]["grade"] == DEMO_GRADE and "demo, not evidence" in (out / R.REPORT_MD).read_text()
    assert d["tasks"]["p1"]["units"]["p1__n__test_D"] and d["verdict"]["claim"] == "none"
