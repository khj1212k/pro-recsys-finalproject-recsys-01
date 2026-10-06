"""E15 원격 런타임 드라이버(scripts/e15_colab_driver.py): 세션 계획, 재개, 예산, 환경 확인, 묶음, URL 비노출.

네트워크도 torch도 없이 돈다. 내려받기는 가짜 opener, 단계 실행은 가짜 runner, torch 확인은 가짜 probe로 바꿔 끼운다.
사전 등록 yaml과 드라이버가 들고 있는 표가 같은지도 여기서 묶는다.
"""
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PREREG = yaml.safe_load((REPO / "evaluation/recsys/ebnerd/preregistration/neural-e15.yaml").read_text())
SECRET = "X-Goog-Signature=TOPSECRET0123456789"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drv = _load("e15_colab_driver")
m4 = drv.m4


def _url(key):
    return f"https://storage.example.invalid/private-bucket/{key}?{SECRET}"


def _tarball(path: Path, files: dict, commit="c" * 40) -> Path:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def probe(version="2.11.0+cu128", cuda=True):
    return lambda python: {"torch": version, "cuda_available": cuda, "gpu": "Tesla T4" if cuda else None, "cuda": "12.8"}


class Runner:
    """단계 명령을 기록하는 가짜 실행기. fail={단계 id: returncode}, die=단계 id(세션 끊김 흉내)."""

    def __init__(self, fail=None, die=None, clock=None, seconds=0.0):
        self.calls, self.cmds, self.fail, self.die, self.clock, self.seconds = [], [], fail or {}, die, clock, seconds

    def __call__(self, cmd, cwd, env, log_path):
        if "--stage" not in cmd:
            sid = "synthetic-data"
        else:
            stage = cmd[cmd.index("--stage") + 1]
            task = cmd[cmd.index("--task") + 1] if "--task" in cmd else None
            sid = "assemble" if stage == "assemble" else f"{task}:{stage}" + (":recheck" if "--recheck-gate" in cmd else "")
        self.calls.append(sid)
        self.cmds.append(cmd)
        self.last_env = env
        if self.clock:
            self.clock.t += self.seconds
        if sid == self.die:
            raise KeyboardInterrupt("세션 끊김")
        return self.fail.get(sid, 0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    keys = [f"ebnerd_small/{f}" for f in m4.DATA_FILES] + [f"derived/ebnerd_small/{f}" for f in m4.EMB_FILES]
    payload = {k: f"bytes-of-{k}".encode() * 40 for k in keys}
    original = hashlib.sha256(b"original articles with text").hexdigest()
    manifest = {"dataset": "ebnerd_small",
                "files": {k: {"sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for k, v in payload.items()},
                "articles": {"derived_sha256": hashlib.sha256(payload["ebnerd_small/articles.parquet"]).hexdigest(),
                             "original_sha256": original}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    registered = "\n".join([v["sha256"] for k, v in manifest["files"].items() if k.endswith(m4.REGISTERED_INPUTS)] + [original])
    tarball = _tarball(tmp_path / "code.tar.gz", {
        drv.REQUIREMENTS: "numpy==2.4.6\ntorch==2.11.0\n", "evaluation/__init__.py": "", drv.PREREG_YAML: registered,
        "scripts/e15_colab_driver.py": "print('child')\n", "scripts/m4_colab_driver.py": ""})
    opened = []

    def opener(url, timeout=None):
        opened.append(url)
        return io.BytesIO(payload[url.split("/private-bucket/")[1].split("?")[0]])

    class Env:
        work, out, tmp = tmp_path / "work", tmp_path / "work" / "out", tmp_path

        def argv(self, session, *extra, urls=True, rate="1.0", work=None):
            a = ["run", "--workdir", str(work or self.work), "--code-tarball", str(tarball), "--session", session,
                 "--skip-install", "--python", "PY"]
            if session != "s0":
                a += ["--manifest", str(tmp_path / "manifest.json")]
                if rate is not None:
                    a += ["--cu-rate", rate]
                if urls:
                    for k in payload:
                        a += ["--url", f"{k}={_url(k)}"]
            return a + list(extra)

        def run(self, argv, runner=None, **hooks):
            hooks.setdefault("torch_probe", probe())
            hooks.setdefault("heartbeat_seconds", 0)
            runner = runner or Runner()
            return drv.main(argv, runner=runner, opener=opener, **hooks), runner

    e = Env()
    e.payload, e.manifest, e.opened, e.tarball = payload, manifest, opened, tarball
    return e


# --- 사전 등록과의 일치 ------------------------------------------------------------------------

def test_driver_tables_equal_the_preregistered_sessions_run_arguments_and_budget():
    assert set(drv.SESSIONS) == set(PREREG["sessions"])
    for name, reg in PREREG["sessions"].items():
        mine = drv.SESSIONS[name]
        assert list(mine["tasks"]) == reg["tasks"] and list(mine["stages"]) == reg["stages"], name
        assert (mine["runtime"], mine["cu_cap"], mine["judged"]) == (reg["runtime"], reg["cu_cap"], reg["judged"]), name
        assert bool(mine.get("synthetic")) == (reg["data"] == "synthetic")
        assert mine.get("seeds") == reg.get("seeds") and bool(mine.get("recheck_gate")) == bool(reg.get("recheck_gate"))
        assert mine.get("resumes_from") == reg.get("resumes_from")
    assert drv.REGISTERED == {k: PREREG["run"][k] for k in drv.REGISTERED}
    assert (drv.CU_CAP_TOTAL, drv.CU_WARN_TOTAL) == (PREREG["budget"]["cu_cap_total"], PREREG["budget"]["cu_warn_total"])
    assert list(drv.ALL_STAGES) == PREREG["run"]["stages"] and list(drv.DESCRIPTIVE_STAGES) == PREREG["run"]["descriptive_stages"]
    assert sum(s["cu_cap"] for s in drv.SESSIONS.values()) <= drv.CU_CAP_TOTAL
    assert drv.SYNTHETIC["gbdt_trials"] == 2 * drv.SYNTHETIC["neural_trials"]        # 드라이 런도 같은 예산 비율
    assert (drv.PREREG_YAML, drv.REQUIREMENTS) == ("evaluation/recsys/ebnerd/preregistration/neural-e15.yaml",
                                                   PREREG["environment"]["requirements"])


def test_gpu_requirements_pin_the_torch_version_the_driver_verifies_and_the_evaluation_pins():
    assert drv.pinned_torch(REPO) == PREREG["environment"]["torch"]
    pins = dict(line.split("==") for line in (REPO / drv.REQUIREMENTS).read_text().split() if "==" in line)
    base = dict(line.split("==") for line in (REPO / "evaluation/requirements.txt").read_text().split() if "==" in line)
    assert {k: pins[k] for k in ("numpy", "pandas", "pyarrow", "lightgbm", "pyyaml")} == \
        {k: base[k] for k in ("numpy", "pandas", "pyarrow", "lightgbm", "pyyaml")}
    assert set(pins) == {"numpy", "pandas", "pyarrow", "lightgbm", "pyyaml", "torch"}


def test_exit_codes_and_environment_names_match_run_neural():
    from evaluation.recsys.ebnerd import run_neural as R

    assert drv.RUN_NEURAL_EXIT[R.EXIT_GATE_FAILED][0] == "reproduction_gate"
    assert drv.RUN_NEURAL_EXIT[R.EXIT_DETERMINISM_FAILED][0] == "determinism_gate"
    assert (R.ENV_CODE_SHA, R.ENV_ARTICLES, R.ENV_PREREG_COMMIT) == ("E15_CODE_SHA", "E15_ARTICLES_ORIGINAL_SHA256",
                                                                     "E15_PREREG_COMMIT")
    assert drv.MODULE == R.__name__


def test_stage_command_is_the_registered_command_and_run_neural_accepts_it():
    from evaluation.recsys.ebnerd import run_neural as R

    step = {"id": "p1:gate", "task": "p1", "stage": "gate", "recheck": None}
    cmd = drv.stage_command("python", Path("/content/e15"), drv.REGISTERED, 2, step)
    assert cmd == ("python -m evaluation.recsys.ebnerd.run_neural --dataset ebnerd_small --root /content/e15/data "
                   "--out-dir /content/e15/out --task p1 --stage gate --seeds 0 1 2 --n-boot 1000 --p2-sample 20000 "
                   "--p2-cold-sample 60000 --p2-select-sample 5000 --neural-trials 12 --gbdt-trials 24 "
                   "--tune-max-epochs 6 --final-max-epochs 20 --patience 2 --threads 2 --device auto --resume").split()
    args = R.build_parser().parse_args(cmd[3:])
    assert {k: getattr(args, k) for k in R.GRADE_KEYS if k in drv.REGISTERED} == {k: v for k, v in drv.REGISTERED.items() if k != "n_boot"}
    recheck = drv.stage_command("python", Path("/w"), drv.REGISTERED, 2, {**step, "recheck": "s3"})
    assert recheck[-2:] == ["--recheck-gate", "s3"] and R.build_parser().parse_args(recheck[3:]).recheck_gate == "s3"
    assemble = drv.stage_command("python", Path("/w"), drv.REGISTERED, 2, {"id": "assemble", "task": None, "stage": "assemble"})
    assert assemble[3:] == ["--stage", "assemble", "--out-dir", "/w/out", "--n-boot", "1000"]


def test_session_plans_put_every_gate_first_and_recheck_the_gate_on_gpu_sessions():
    ids = lambda s: [x["id"] for x in drv.session_plan(s)]  # noqa: E731
    assert ids("s2") == ["p1:gate", "p2:gate", "p1:gbdt_tune", "p2:gbdt_tune", "p1:gbdt_final", "p2:gbdt_final"]
    assert ids("s1") == ["p1:gate", "p2:gate", "p1:determinism", "p2:determinism"]
    assert ids("s3") == ["p1:gate:recheck", "p1:determinism", "p1:neural_tune", "p1:select", "p1:neural_final", "p1:stack",
                         "p1:cold", "p1:narrative"]
    assert ids("s4")[0] == "p2:gate:recheck" and ids("s4")[-2:] == ["p2:narrative", "assemble"]
    assert ids("s0")[-1] == "assemble" and len(ids("s0")) == 2 * 10 + 1
    assert drv.session_run_args("s1")["seeds"] == [0] and drv.session_run_args("s2")["seeds"] == [0, 1, 2]


# --- 실행 --------------------------------------------------------------------------------------

def test_dry_run_session_generates_synthetic_data_and_runs_every_step_without_urls(env):
    rc, r = env.run(env.argv("s0"))
    assert rc == 0 and r.calls[0] == "synthetic-data" and r.calls[1:] == [s["id"] for s in drv.session_plan("s0")]
    assert env.opened == [] and "ebnerd_synth" in r.cmds[1] and r.cmds[1][r.cmds[1].index("--gbdt-trials") + 1] == "2"
    state = json.loads((env.out / "driver_state.json").read_text())
    assert state["judged"] is False and state["environment"]["torch_pin"] == "2.11.0"
    assert r.last_env["CUBLAS_WORKSPACE_CONFIG"] == ":4096:8" and r.last_env["EBNERD_ROOT"] == str(env.work / "data")
    assert r.last_env["E15_CODE_SHA"] == "c" * 40


def test_resume_after_a_dropped_session_skips_finished_steps_and_refuses_other_settings(env):
    with pytest.raises(KeyboardInterrupt):
        env.run(env.argv("s2"), Runner(die="p1:gbdt_tune"))
    state = json.loads((env.out / "driver_state.json").read_text())
    assert {k: v["status"] for k, v in state["stages"].items()} == {"p1:gate": "done", "p2:gate": "done", "p1:gbdt_tune": "running"}
    n_downloads = len(env.opened)
    rc, r = env.run(env.argv("s2"))
    assert rc == 0 and r.calls == ["p1:gbdt_tune", "p2:gbdt_tune", "p1:gbdt_final", "p2:gbdt_final"]
    assert len(env.opened) == n_downloads                                    # 이미 받은 데이터는 다시 받지 않는다
    assert len(json.loads((env.out / "driver_state.json").read_text())["invocations"]) == 2
    rc, r = env.run(env.argv("s1"))                                          # 같은 작업 디렉터리에서 다른 세션
    assert rc == m4.EXIT_CONFIG_MISMATCH and r.calls == []
    assert SECRET not in (env.out / "run.log").read_text() + (env.out / "driver_state.json").read_text()
    assert r.calls == [] and all(SECRET not in " ".join(c) for c in r.cmds)


@pytest.mark.parametrize("code, reason", [(4, "reproduction_gate"), (5, "determinism_gate"), (1, "stage_failed")])
def test_a_failed_step_invalidates_the_run_and_blocks_reruns_and_bundles(env, code, reason):
    rc, r = env.run(env.argv("s2"), Runner(fail={"p2:gate": code}))
    assert rc == m4.EXIT_STAGE_FAILED and r.calls == ["p1:gate", "p2:gate"]          # 튜닝을 시작하지 않았다
    marker = json.loads((env.out / "FAILED").read_text())
    assert (marker["reason"], marker["step"], marker["returncode"], marker["session"]) == (reason, "p2:gate", code, "s2")
    rc, r = env.run(env.argv("s2"))
    assert rc == m4.EXIT_FAILED_MARKER and r.calls == []
    (env.out / "progress.json").write_text("{}")
    assert drv.main(["bundle", "--workdir", str(env.work), "--out", str(env.tmp / "b.tar.gz")]) == m4.EXIT_FAILED_MARKER
    assert not (env.tmp / "b.tar.gz").exists()
    rc, r = env.run(env.argv("s2", "--fresh"))
    assert rc == 0 and len(r.calls) == 6


def test_budget_skips_descriptive_steps_at_the_warning_line_and_everything_at_a_cap(env):
    clock = Clock()
    bundle = _bundle(env)
    extra = ["--resume-bundle", str(bundle["out"]), "--resume-bundle-sha256", bundle["sha256"]]
    # rate 1 CU/h, 단계마다 1시간, 앞 세션까지 18 CU: 6단계 뒤 누적 24 CU로 경고선에 닿아 narrative만 건너뛴다
    rc, r = env.run(env.argv("s3", "--cu-spent-before", "18", *extra), Runner(clock=clock, seconds=3600.0), clock=clock)
    assert rc == 0 and r.calls == [s["id"] for s in drv.session_plan("s3")][:-1]
    state = json.loads((env.out / "driver_state.json").read_text())
    assert state["stages"]["p1:narrative"]["status"] == "skipped_budget"
    compute = json.loads((env.out / "compute.json").read_text())["sessions"]["s3"]
    assert compute["skipped_for_budget"] == ["p1:narrative"] and compute["cu_estimated"] == pytest.approx(7.0)
    assert compute["cu_spent_before"] == 18.0 and compute["budget_rules_active"] is True and compute["environment"]["gpu"] == "Tesla T4"
    # 경고선은 6번째 단계(stack)가 도는 동안 넘었다. 규칙은 단계 시작 전에만 보므로 그 단계는 끝까지 돌았고, 넘은 사실이 남는다
    assert compute["budget_lines_crossed_during_step"] == {"p1:stack": ["total_warn"]}
    assert "step p1:stack 도중 total_warn" in (env.out / "run.log").read_text()
    assert (compute["registered_runtime"], compute["device_args"], compute["allow_cpu_used"]) == ("t4", ["auto"], False)


def test_session_cap_and_total_cap_stop_remaining_steps_but_assemble_still_runs(env, tmp_path):
    clock = Clock()
    bundle = _bundle(env)
    extra = ["--resume-bundle", str(bundle["out"]), "--resume-bundle-sha256", bundle["sha256"]]
    # 단계마다 3시간 x 1 CU/h: 3단계 뒤 9 CU로 세션 상한(9)에 닿는다
    rc, r = env.run(env.argv("s4", *extra), Runner(clock=clock, seconds=3 * 3600.0), clock=clock)
    assert rc == 0 and r.calls == ["p2:gate:recheck", "p2:determinism", "p2:neural_tune", "assemble"]
    skipped = json.loads((env.out / "compute.json").read_text())["sessions"]["s4"]["skipped_for_budget"]
    assert skipped == ["p2:select", "p2:neural_final", "p2:stack", "p2:cold", "p2:narrative"]
    crossed = json.loads((env.out / "compute.json").read_text())["sessions"]["s4"]["budget_lines_crossed_during_step"]
    assert crossed == {"p2:neural_tune": ["session_cap"]}        # 세션 상한을 넘긴 단계가 무엇이었는지(그만큼 초과했다)
    # 전체 상한: 앞 세션까지 29.5 CU면 첫 단계(0.5시간) 뒤 30 CU
    work2 = tmp_path / "work2"
    clock2 = Clock()
    rc, r = env.run(env.argv("s4", "--cu-spent-before", "29.5", *extra, work=work2), Runner(clock=clock2, seconds=1800.0), clock=clock2)
    assert rc == 0 and r.calls == ["p2:gate:recheck", "assemble"]
    crossed = json.loads((work2 / "out" / "compute.json").read_text())["sessions"]["s4"]["budget_lines_crossed_during_step"]
    assert crossed == {"p2:gate:recheck": ["total_cap"]}         # 경고선은 시작 전에 이미 넘어 있었다


def test_real_data_sessions_refuse_to_start_without_a_rate_and_zero_turns_the_rules_off_on_record(env):
    rc, r = env.run(env.argv("s2", rate=None))
    assert rc == m4.EXIT_USAGE and r.calls == [] and env.opened == []
    clock = Clock()
    rc, r = env.run(env.argv("s2", rate="0"), Runner(clock=clock, seconds=10 ** 6), clock=clock)
    assert rc == 0 and len(r.calls) == 6
    assert json.loads((env.out / "compute.json").read_text())["sessions"]["s2"]["budget_rules_active"] is False


def test_wrong_torch_version_or_missing_gpu_stops_before_any_step_without_invalidating(env):
    rc, r = env.run(env.argv("s2"), torch_probe=probe("2.14.0"))
    assert rc == drv.EXIT_ENVIRONMENT and r.calls == [] and not (env.out / "FAILED").exists()
    assert "2.14.0" in (env.out / "run.log").read_text()
    rc, r = env.run(env.argv("s2"), torch_probe=lambda p: {"error": "ModuleNotFoundError"})
    assert rc == drv.EXIT_ENVIRONMENT and r.calls == []
    rc, r = env.run(env.argv("s2"), torch_probe=probe("2.11.0", cuda=False))      # CPU 세션은 GPU가 없어도 된다
    assert rc == 0 and len(r.calls) == 6


def test_gpu_session_needs_cuda_and_the_previous_sessions_bundle(env, tmp_path):
    rc, r = env.run(env.argv("s3"))
    assert rc == m4.EXIT_USAGE and r.calls == []                                   # 묶음 없이 시작할 수 없다
    bundle = _bundle(env)
    extra = ["--resume-bundle", str(bundle["out"]), "--resume-bundle-sha256", bundle["sha256"]]
    rc, r = env.run(env.argv("s3", *extra), torch_probe=probe(cuda=False))
    assert rc == drv.EXIT_ENVIRONMENT and r.calls == []
    rc, r = env.run(env.argv("s3", *extra, "--allow-cpu"), torch_probe=probe(cuda=False))
    assert rc == 0 and r.cmds[0][r.cmds[0].index("--device") + 1] == "cpu" and r.cmds[0][-2:] == ["--recheck-gate", "s3"]
    compute = json.loads((env.out / "compute.json").read_text())["sessions"]["s3"]
    # 등록한 런타임(t4)이 아닌 장치로 돌았다는 것이 계산 기록에 남는다(등급은 run_neural이 신경망 환경 기록으로 내린다)
    assert (compute["registered_runtime"], compute["device_args"], compute["allow_cpu_used"]) == ("t4", ["cpu"], True)


def test_nvidia_driver_version_is_read_when_available_and_absent_otherwise():
    class Done:
        def __init__(self, code, out):
            self.returncode, self.stdout = code, out

    assert drv.nvidia_driver_version(lambda *a, **k: Done(0, "550.54.15\n550.54.15\n")) == "550.54.15"
    assert drv.nvidia_driver_version(lambda *a, **k: Done(9, "")) is None
    assert drv.nvidia_driver_version(lambda *a, **k: Done(0, "\n")) is None

    def missing(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    assert drv.nvidia_driver_version(missing) is None


def _bundle(env, name="prev"):
    """앞 세션이 남긴 체크포인트 디렉터리를 흉내 내 묶는다."""
    out = env.tmp / name / "out"
    (out / "units").mkdir(parents=True)
    (out / "models").mkdir()
    (out / "progress.json").write_text(json.dumps({"units": {"p1__gate": {}}}))
    (out / "units" / "p1__gate.json").write_text("{}")
    (out / "models" / "p1_A_star_s0.txt").write_text("tree")
    (out / "compute.json").write_text(json.dumps({"sessions": {"s2": {"cu_estimated": 3.0}}}))
    for junk in ("driver_state.json", "run.log", "driver.pid"):
        (out / junk).write_text("session-specific")
    dest = env.tmp / f"{name}.tar.gz"
    assert drv.main(["bundle", "--workdir", str(env.tmp / name), "--out", str(dest)]) == 0
    return {"out": dest, "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}


def test_bundle_carries_checkpoints_but_not_the_previous_sessions_driver_state(env):
    bundle = _bundle(env)
    with tarfile.open(bundle["out"]) as tf:
        names = set(tf.getnames())
    assert names == {"progress.json", "units/p1__gate.json", "models/p1_A_star_s0.txt", "compute.json"}
    extra = ["--resume-bundle", str(bundle["out"])]
    rc, r = env.run(env.argv("s3", *extra, "--resume-bundle-sha256", "0" * 64))
    assert rc == m4.EXIT_SHA_MISMATCH and r.calls == [] and not (env.out / "progress.json").exists()
    rc, r = env.run(env.argv("s3", *extra))
    assert rc == m4.EXIT_USAGE and r.calls == []                                   # sha256 없이 묶음을 풀지 않는다
    rc, r = env.run(env.argv("s3", *extra, "--resume-bundle-sha256", bundle["sha256"]))
    assert rc == 0 and (env.out / "models" / "p1_A_star_s0.txt").read_text() == "tree"
    assert json.loads((env.out / "progress.json").read_text()) == {"units": {"p1__gate": {}}}
    state = json.loads((env.out / "driver_state.json").read_text())
    assert state["session"] == "s3" and state["resumed_from_bundle"]["sha256"] == bundle["sha256"]
    sessions = json.loads((env.out / "compute.json").read_text())
    assert set(sessions["sessions"]) == {"s2", "s3"} and sessions["cu_estimated_sum"] == pytest.approx(3.0)   # 앞 세션 기록이 이어진다
    # 같은 세션을 다시 실행하면 묶음을 다시 풀지 않는다(그 사이에 쌓인 체크포인트를 덮어쓰지 않는다)
    (env.out / "models" / "p1_A_star_s0.txt").write_text("newer")
    rc, r = env.run(env.argv("s3", *extra, "--resume-bundle-sha256", bundle["sha256"]))
    assert rc == 0 and r.calls == [] and (env.out / "models" / "p1_A_star_s0.txt").read_text() == "newer"


def test_data_sha_mismatch_invalidates_and_no_message_ever_contains_a_signed_url(env, capsys):
    key = "ebnerd_small/train/history.parquet"
    env.payload[key] = b"tampered"
    rc, r = env.run(env.argv("s2"))
    assert rc == m4.EXIT_SHA_MISMATCH and r.calls == [] and (env.out / "FAILED").exists()
    text = capsys.readouterr()
    logs = (env.out / "run.log").read_text() + (env.out / "FAILED").read_text() + text.out + text.err
    assert SECRET not in logs and "storage.example.invalid" not in logs and key in logs
    with pytest.raises(SystemExit) as e:                                             # --url을 빼먹은 명령의 파서 오류
        drv.main(["run", "--workdir", str(env.work), "--code-tarball", "x", "--session", "s2", _url(key)])
    assert e.value.code == m4.EXIT_USAGE and SECRET not in capsys.readouterr().err


def test_unregistered_inputs_are_refused_before_download(env):
    env.manifest["files"]["derived/ebnerd_small/bge_m3_tsb512.f16.npy"]["sha256"] = "0" * 64
    (env.tmp / "manifest.json").write_text(json.dumps(env.manifest))
    rc, r = env.run(env.argv("s2"))
    assert rc == m4.EXIT_USAGE and env.opened == [] and r.calls == [] and not (env.out / "FAILED").exists()


def test_detach_starts_the_bundled_driver_and_keeps_urls_out_of_the_command_line(env):
    argv = env.argv("s2", "--detach")
    argv[argv.index("PY")] = "/bin/echo"                    # 자식 대신 인자를 그대로 찍는 프로그램
    assert drv.main(argv) == 0
    pid = json.loads((env.out / "driver.pid").read_text())["pid"]
    for _ in range(100):
        if not m4._pid_alive(pid):
            break
        import time
        time.sleep(0.05)
    echoed = (env.out / "driver_stdout.log").read_text()
    assert "scripts/e15_colab_driver.py run" in echoed and "--session s2" in echoed
    assert SECRET not in echoed and "--detach" not in echoed and "--url" not in echoed


def _code_tarball(dest: Path) -> Path:
    """A3.11의 git archive와 같은 경로를 작업 트리에서 모은 tarball(커밋 id 없음 → demo 등급)."""
    roots = ["recsys_core", "evaluation/__init__.py", "evaluation/recsys", "scripts/e15_colab_driver.py",
             "scripts/m4_colab_driver.py", "requirements-colab.txt", "requirements-colab-gpu.txt"]
    with tarfile.open(dest, "w:gz") as tf:
        for root in roots:
            for p in sorted([REPO / root] if (REPO / root).is_file() else (REPO / root).rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    tf.add(p, arcname=p.relative_to(REPO).as_posix())
    return dest


def test_dry_run_session_end_to_end_in_real_subprocesses_then_resume_and_bundle(tmp_path):
    """드라이버 → python -m → 패키지 상대 import → 전 단계 → 리포트, 그리고 같은 명령으로 다시 실행하면 아무것도 다시 돌지 않는다.

    torch가 있는 환경에서만 돈다(합성 데이터, CPU, 1~2분). EB-NeRD는 쓰지 않는다.
    """
    import sys

    # 단계는 새 인터프리터에서 돌므로, 이 프로세스의 sys.modules(다른 테스트가 가짜 torch를 넣어 둘 수 있다)가 아니라
    # 그 인터프리터에 torch가 있는지를 본다.
    local = drv.probe_torch(sys.executable)
    if "torch" not in local:
        pytest.skip("torch가 없다(이 테스트는 torch가 있는 CI job과 로컬에서 돈다)")
    tarball = _code_tarball(tmp_path / "e15_code.tar.gz")
    work = tmp_path / "work"
    argv = ["run", "--workdir", str(work), "--code-tarball", str(tarball), "--session", "s0", "--skip-install",
            "--python", sys.executable]
    # 로컬 torch가 핀과 다른 버전이어도 경로 확인은 하게 한다(원격에서는 실제 probe가 핀을 강제한다)
    hooks = {"torch_probe": lambda p: {**local, "torch": drv.pinned_torch(REPO) + "+local"}, "heartbeat_seconds": 0}
    assert drv.main(argv, **hooks) == 0
    state = json.loads((work / "out" / "driver_state.json").read_text())
    assert [s for s, v in state["stages"].items() if v["status"] != "done"] == [] and len(state["stages"]) == 21
    report = json.loads((work / "out" / "ebnerd_v1_3_neural.json").read_text())
    assert report["meta"]["evidence"]["grade"] == "demo, not evidence" and report["verdict"]["claim"] == "none"
    assert report["meta"]["compute"]["sessions"]["s0"]["budget_rules_active"] is False
    assert all(report["tasks"][t]["units"].values() for t in ("p1", "p2"))
    log = (work / "out" / "run.log").read_text()
    for column in ("title", "subtitle", "body"):                 # 로그에는 집계 수치와 진행 상태만 있다
        assert f"'{column}'" not in log and f'"{column}"' not in log
    before = (work / "out" / "run.log").stat().st_size
    assert drv.main(argv, **hooks) == 0                          # 재실행: 끝난 단계를 전부 건너뛴다
    again = (work / "out" / "run.log").read_bytes()[before:].decode("utf-8")
    assert again.count("이미 끝남, 건너뜀") == 21 and "시작" not in again
    dest = tmp_path / "bundle.tar.gz"
    assert drv.main(["bundle", "--workdir", str(work), "--out", str(dest)]) == 0
    with tarfile.open(dest) as tf:
        names = tf.getnames()
    assert "progress.json" in names and "driver_state.json" not in names and any(n.endswith(".pt") for n in names)
