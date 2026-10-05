"""원격 런타임 드라이버(scripts/m4_colab_driver.py): 검증·재개·예산·URL 비노출, 그리고 사전 등록한 명령과의 일치.

네트워크 없이 돈다. 내려받기는 가짜 opener로, 단계 실행은 가짜 runner로 바꿔 끼운다. 마지막 테스트 하나만 실제
서브프로세스로 합성 데이터 사슬 전체를 돈다(드라이버 → python -m → 패키지 상대 import → 재개 경로 확인).
"""
import importlib.util
import io
import json
import re
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PREREG = yaml.safe_load((REPO / "evaluation/recsys/ebnerd/preregistration/cold-v1.2.yaml").read_text())
SECRET = "X-Goog-Signature=TOPSECRET0123456789"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drv = _load("m4_colab_driver")


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


@pytest.fixture
def env(tmp_path):
    """작업 디렉터리, 코드 tarball, 데이터 2개짜리 manifest, 그리고 그 데이터를 내주는 가짜 opener."""
    import hashlib

    keys = [f"ebnerd_small/{f}" for f in drv.DATA_FILES] + [f"derived/ebnerd_small/{f}" for f in drv.EMB_FILES]
    payload = {k: f"bytes-of-{k}".encode() * 40 for k in keys}
    manifest = {"dataset": "ebnerd_small",
                "files": {k: {"sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for k, v in payload.items()},
                "articles": {"derived_sha256": "d", "original_sha256": "o"}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    # 드라이버는 등록 yaml에 그 sha256 문자열이 있는지만 본다: 가짜 등록 파일에 가짜 입력의 sha를 적는다
    registered = "\n".join(v["sha256"] for k, v in manifest["files"].items() if k.endswith(drv.REGISTERED_INPUTS))
    tarball = _tarball(tmp_path / "code.tar.gz", {"requirements-colab.txt": "numpy\n", "evaluation/__init__.py": "",
                                                  drv.PREREG_YAML: registered})
    opened = []

    def opener(url, timeout=None):
        opened.append(url)
        key = url.split("/private-bucket/")[1].split("?")[0]
        return io.BytesIO(payload[key])

    class Env:
        work = tmp_path / "work"
        out = tmp_path / "work" / "out"

        def argv(self, *extra, urls=True, rate="0.1"):
            a = ["run", "--workdir", str(self.work), "--code-tarball", str(tarball), "--manifest",
                 str(tmp_path / "manifest.json"), "--skip-install", "--python", "PY"]
            if rate is not None:                       # 판정용 실행의 고정 명령(A2.8)에는 --cu-rate가 있다
                a += ["--cu-rate", rate]
            if urls:
                for k in payload:
                    a += ["--url", f"{k}={_url(k)}"]
            return a + list(extra)

    e = Env()
    e.payload, e.manifest, e.opener, e.opened, e.tarball, e.tmp = payload, manifest, opener, opened, tarball, tmp_path
    return e


class Runner:
    """단계 명령을 기록하는 가짜 실행기. fail={단계: returncode}, die=단계(세션 끊김 흉내)."""

    def __init__(self, fail=None, die=None, clock=None, seconds=0.0):
        self.calls, self.fail, self.die, self.clock, self.seconds = [], fail or {}, die, clock, seconds

    def __call__(self, cmd, cwd, env, log_path):
        stage = cmd[cmd.index("--stage") + 1] if "--stage" in cmd else "synthetic"
        self.calls.append(stage)
        self.last_cmd, self.last_env, self.last_cwd = cmd, env, cwd
        if self.clock:
            self.clock.t += self.seconds
        if stage == self.die:
            raise KeyboardInterrupt("세션 끊김")
        return self.fail.get(stage, 0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _all_text(root: Path, capsys) -> str:
    captured = capsys.readouterr()
    files = "".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file() and p.suffix != ".gz")
    return captured.out + captured.err + files


# --- 사전 등록과의 일치 ------------------------------------------------------------------------

def test_driver_constants_equal_the_preregistration():
    run = PREREG["run"]
    assert list(drv.STAGES) == run["stages"] and list(drv.DESCRIPTIVE_STAGES) == run["descriptive_stages"]
    assert drv.REGISTERED == {k: run[k] for k in ("dataset", "seeds", "n_boot", "p2_sample", "sub_cap")}
    assert (drv.CU_CAP, drv.CU_WARN) == (PREREG["budget"]["cu_cap"], PREREG["budget"]["cu_warn"])


def test_stage_command_is_the_registered_command(tmp_path):
    cmd = drv.stage_command("python", tmp_path, drv.REGISTERED, threads=2, stage="e1")
    assert cmd == ["python", "-m", "evaluation.recsys.ebnerd.run_cold", "--dataset", "ebnerd_small",
                   "--root", str(tmp_path / "data"), "--out-dir", str(tmp_path / "out"), "--seeds", "0", "1", "2",
                   "--n-boot", "1000", "--p2-sample", "20000", "--sub-cap", "20000", "--threads", "2", "--resume",
                   "--stage", "e1"]
    # 실행기 쪽 파서가 이 인자를 그대로 받고, 등록한 인자로 해석한다
    from evaluation.recsys.ebnerd.run_cold import build_parser
    ns = build_parser().parse_args(cmd[3:])
    assert (ns.dataset, ns.seeds, ns.n_boot, ns.p2_sample, ns.sub_cap, ns.fake_dim, ns.max_fit, ns.max_test) == (
        "ebnerd_small", [0, 1, 2], 1000, 20000, 20000, None, None, None)
    assert ns.resume and ns.stage == "e1"


def test_colab_requirements_pin_the_same_versions_as_the_local_evaluation_environment():
    def pins(path):
        return dict(l.strip().split("==") for l in path.read_text().splitlines() if "==" in l and not l.startswith("#"))

    colab, local = pins(REPO / "requirements-colab.txt"), pins(REPO / "evaluation/requirements.txt")
    assert set(colab) == {"numpy", "pandas", "pyarrow", "lightgbm", "pyyaml"}
    assert colab == {k: local[k] for k in colab}
    assert colab["lightgbm"] == "4.7.0"


# --- 내려받기·검증·URL 비노출 ------------------------------------------------------------------

def test_downloads_verify_sha256_and_never_leak_the_signed_url(env, capsys):
    runner = Runner()
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_OK
    for key, data in env.payload.items():
        assert (env.work / "data" / key).read_bytes() == data
    assert runner.calls == list(drv.STAGES)
    assert len(env.opened) == 8
    text = _all_text(env.work, capsys)
    assert "TOPSECRET" not in text and "storage.example.invalid" not in text and "private-bucket" not in text
    assert "M4_URLS_JSON" not in runner.last_env                        # 서브프로세스에는 URL을 넘기지 않는다
    assert runner.last_env["EBNERD_ROOT"] == str(env.work / "data") and runner.last_env["M4_CODE_SHA"] == "c" * 40
    assert runner.last_cwd == env.work / "repo" and (env.work / "repo" / "requirements-colab.txt").exists()


def test_sha256_mismatch_deletes_the_file_marks_the_run_failed_and_blocks_reruns(env, capsys):
    bad = dict(env.manifest)
    key = "derived/ebnerd_small/article_ids.npy"      # 등록값이 없는 파일이라 내려받은 뒤에야 불일치가 드러난다
    bad["files"] = {**env.manifest["files"], key: {"sha256": "0" * 64, "bytes": 1}}
    (env.tmp / "manifest.json").write_text(json.dumps(bad))
    runner = Runner()
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_SHA_MISMATCH
    assert not (env.work / "data" / key).exists() and runner.calls == []
    failed = json.loads((env.out / "FAILED").read_text())
    assert key in failed["reason"] and "sha256" in failed["reason"]
    assert "TOPSECRET" not in _all_text(env.work, capsys)
    # 마커가 있는 동안은 다시 돌지 않는다. --fresh로만 처음부터.
    (env.tmp / "manifest.json").write_text(json.dumps(env.manifest))
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_FAILED_MARKER
    assert drv.main(env.argv("--fresh"), runner=runner, opener=env.opener) == drv.EXIT_OK


def test_download_errors_are_reported_without_the_url_and_are_retryable(env, capsys):
    def forbidden(url, timeout=None):
        raise urllib.error.HTTPError(url, 403, f"Forbidden {url}", None, None)

    def broken(url, timeout=None):
        raise OSError(f"could not resolve {url}")

    for opener, needle in ((forbidden, "HTTP 403"), (broken, "OSError")):
        runner = Runner()
        assert drv.main(env.argv(), runner=runner, opener=opener) == drv.EXIT_DOWNLOAD
        text = _all_text(env.work, capsys)
        assert needle in text and "TOPSECRET" not in text and "storage.example.invalid" not in text
        assert not (env.out / "FAILED").exists() and runner.calls == []
        assert not list((env.work / "data").rglob("*.part"))
    assert drv.main(env.argv(), runner=Runner(), opener=env.opener) == drv.EXIT_OK   # URL을 새로 받아 다시 실행


def test_urls_can_come_from_the_environment_and_missing_urls_are_a_usage_error(env, monkeypatch, capsys):
    assert drv.main(env.argv(urls=False), runner=Runner(), opener=env.opener) == drv.EXIT_USAGE
    monkeypatch.setenv("M4_URLS_JSON", json.dumps({k: _url(k) for k in env.payload}))
    assert drv.main(env.argv(urls=False), runner=Runner(), opener=env.opener) == drv.EXIT_OK
    assert "TOPSECRET" not in _all_text(env.work, capsys)


def test_a_misplaced_url_on_the_command_line_is_never_echoed(env, capsys, monkeypatch):
    """--url이나 `키=`를 빼먹은 명령에서도 URL이 나가지 않는다. argparse는 인식하지 못한 인자를 오류 메시지에 그대로
    되찍는데, 원격 실행기는 그 stderr를 기록으로 남긴다."""
    secret_url = _url("ebnerd_small/articles.parquet")
    base = env.argv(urls=False)
    cases = {
        "--url을 빼먹음": base + [secret_url],
        "키=URL을 --url 없이": base + [f"ebnerd_small/articles.parquet={secret_url}"],
        "하위 명령 자리에 URL": [secret_url] + base[1:],
        "다른 인자의 값 자리에 URL": base + ["--cu-rate", secret_url],
        "키= 를 빼먹음": base + ["--url", secret_url],
        "스킴 없는 서명 URL": base + [secret_url.split("://", 1)[1]],
    }
    for name, argv in cases.items():
        runner = Runner()
        try:
            rc = drv.main(argv, runner=runner, opener=env.opener)
        except SystemExit as e:
            rc = e.code
        text = _all_text(env.tmp, capsys)
        assert rc == drv.EXIT_USAGE, name                      # sha256 불일치(2)와 섞이지 않는 사용법 오류 코드
        assert "TOPSECRET" not in text and "storage.example.invalid" not in text and "private-bucket" not in text, name
        assert runner.calls == [] and env.opened == [], name
    # 환경변수로 인자를 받는 경로도 같은 파서를 쓴다
    monkeypatch.setenv("M4_DRIVER_ARGS_JSON", json.dumps(cases["--url을 빼먹음"]))
    with pytest.raises(SystemExit) as e:
        drv.main()
    assert e.value.code == drv.EXIT_USAGE and "TOPSECRET" not in _all_text(env.tmp, capsys)


def test_argument_errors_still_say_what_was_wrong(env, capsys):
    """URL을 지우느라 오류 메시지가 쓸모없어지면 안 된다: URL이 아닌 잘못된 인자는 그대로 보여 준다."""
    with pytest.raises(SystemExit) as e:
        drv.main(env.argv(urls=False) + ["--no-such-flag", "--cu-rate", "abc"])
    err = capsys.readouterr().err
    assert e.value.code == drv.EXIT_USAGE and "usage:" in err and ("abc" in err or "--no-such-flag" in err)


def test_redact_removes_whole_urls_and_their_parts():
    u = _url("a/b.parquet")
    msg = drv.redact(f"failed {u} then {u.split('?')[0]} and {SECRET}", [u])
    assert "TOPSECRET" not in msg and "storage.example" not in msg and msg.count("<signed-url>") == 3


# --- 재개 --------------------------------------------------------------------------------------

def test_dropped_session_resumes_from_the_first_unfinished_stage(env):
    first = Runner(die="e2p2")
    with pytest.raises(KeyboardInterrupt):
        drv.main(env.argv(), runner=first, opener=env.opener)
    assert first.calls == ["fit", "e1", "e2p2"] and not (env.out / "FAILED").exists()
    state = json.loads((env.out / "driver_state.json").read_text())
    assert [s for s, v in state["stages"].items() if v["status"] == "done"] == ["fit", "e1"]
    second = Runner()
    assert drv.main(env.argv(), runner=second, opener=env.opener) == drv.EXIT_OK
    assert second.calls == ["e2p2", "e8", "e4", "e2p1", "assemble"]       # 끝난 단계는 다시 돌리지 않는다
    assert len(env.opened) == 8                                            # 검증된 데이터도 다시 받지 않는다
    state = json.loads((env.out / "driver_state.json").read_text())
    assert len(state["invocations"]) == 2 and all(v["status"] == "done" for v in state["stages"].values())
    third = Runner()
    assert drv.main(env.argv(), runner=third, opener=env.opener) == drv.EXIT_OK and third.calls == []


def test_failed_stage_invalidates_the_run(env):
    runner = Runner(fail={"e1": 137})
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_STAGE_FAILED
    assert runner.calls == ["fit", "e1"]
    assert json.loads((env.out / "FAILED").read_text())["stage"] == "e1"
    again = Runner()
    assert drv.main(env.argv(), runner=again, opener=env.opener) == drv.EXIT_FAILED_MARKER and again.calls == []


def test_reproduction_gate_failure_stops_the_chain_and_is_recorded_as_such(env):
    """run_cold는 e1의 기준 칸 직후에 재현 게이트를 보고, 판정용 실행에서 실패면 전용 종료 코드로 끝난다. 드라이버는 그
    실행을 무효로 남기고 뒤 단계(assemble 포함)를 돌리지 않는다."""
    from evaluation.recsys.ebnerd import run_cold

    assert drv.RUN_COLD_EXIT_GATE == run_cold.EXIT_GATE_FAILED and drv.GATE_JSON == run_cold.GATE_JSON
    runner = Runner(fail={"e1": drv.RUN_COLD_EXIT_GATE})
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_STAGE_FAILED
    assert runner.calls == ["fit", "e1"]
    failed = json.loads((env.out / "FAILED").read_text())
    assert failed["stage"] == "e1" and failed["reason"] == "reproduction_gate" and drv.GATE_JSON in failed["rule"]
    assert "재현 게이트 실패" in (env.out / "run.log").read_text()
    again = Runner()
    assert drv.main(env.argv(), runner=again, opener=env.opener) == drv.EXIT_FAILED_MARKER and again.calls == []
    # 다른 실패 코드는 게이트 실패로 적지 않는다
    other = Runner(fail={"e1": 137})
    assert drv.main(env.argv("--fresh"), runner=other, opener=env.opener) == drv.EXIT_STAGE_FAILED
    assert json.loads((env.out / "FAILED").read_text())["reason"] == "stage_failed"


def test_changed_inputs_refuse_to_resume(env):
    with pytest.raises(KeyboardInterrupt):
        drv.main(env.argv(), runner=Runner(die="e1"), opener=env.opener)
    registered = "\n".join(v["sha256"] for v in env.manifest["files"].values())
    other = _tarball(env.tmp / "other.tar.gz", {"requirements-colab.txt": "numpy\n", "x.py": "changed",
                                                drv.PREREG_YAML: registered})
    argv = env.argv()
    argv[argv.index("--code-tarball") + 1] = str(other)
    runner = Runner()
    assert drv.main(argv, runner=runner, opener=env.opener) == drv.EXIT_CONFIG_MISMATCH and runner.calls == []
    assert not (env.out / "FAILED").exists()


# --- 예산 --------------------------------------------------------------------------------------

def test_budget_skips_descriptive_stages_at_the_warning_line_and_everything_at_the_cap(env):
    clock = Clock()
    runner = Runner(clock=clock, seconds=3600.0)        # 단계 하나에 1시간
    rc = drv.main(env.argv("--cu-rate", "2.0"), runner=runner, opener=env.opener, clock=clock)
    assert rc == drv.EXIT_OK
    # 2 CU/h: fit 뒤 2, e1 뒤 4, e2p2 뒤 6 -> e8부터 상한. assemble은 항상 돈다.
    assert runner.calls == ["fit", "e1", "e2p2", "assemble"]
    compute = json.loads((env.out / "compute.json").read_text())
    assert compute["skipped_for_budget"] == ["e8", "e4", "e2p1"]
    assert compute["cu_estimated"] == pytest.approx(8.0) and compute["rate_cu_per_hour"] == 2.0
    assert compute["budget_rules_active"] is True
    assert "예산 규칙이 꺼져 있다" not in (env.out / "run.log").read_text()
    assert compute["wall_seconds_by_stage"]["fit"] == 3600.0 and compute["cu_cap"] == 6.0


def test_warning_line_drops_only_descriptive_stages(env):
    clock = Clock()
    runner = Runner(clock=clock, seconds=3600.0)
    # 1.3 CU/h: fit 1.3, e1 2.6, e2p2 3.9, e8 5.2 -> 경고선(5) 이상이라 서술용 e4·e2p1만 건너뛴다
    assert drv.main(env.argv("--cu-rate", "1.3"), runner=runner, opener=env.opener, clock=clock) == drv.EXIT_OK
    assert runner.calls == ["fit", "e1", "e2p2", "e8", "assemble"]
    assert json.loads((env.out / "compute.json").read_text())["skipped_for_budget"] == ["e4", "e2p1"]


def test_stages_skipped_for_budget_can_be_filled_later_and_the_report_is_reassembled(env):
    clock = Clock()
    first = Runner(clock=clock, seconds=3600.0)
    assert drv.main(env.argv("--cu-rate", "2.0"), runner=first, opener=env.opener, clock=clock) == drv.EXIT_OK
    assert first.calls == ["fit", "e1", "e2p2", "assemble"]
    # 예산을 늘려 같은 명령으로 다시: 건너뛴 단계만 돌고, 리포트(assemble)는 다시 만든다
    second = Runner(clock=clock, seconds=60.0)
    assert drv.main(env.argv("--cu-rate", "2.0", "--cu-cap", "100", "--cu-warn", "100"), runner=second,
                    opener=env.opener, clock=clock) == drv.EXIT_OK
    assert second.calls == ["e8", "e4", "e2p1", "assemble"]
    assert json.loads((env.out / "compute.json").read_text())["skipped_for_budget"] == []


def test_a_stage_that_dies_mid_way_still_counts_toward_the_budget(env):
    """세션이 단계 도중에 죽으면 그 단계가 돈 시간도 CU 추정에 들어가야 한다. 단계 경계에서만 시각을 적으면 몇 시간짜리
    단계가 통째로 빠져, 이어서 돌릴 때 상한을 넘긴 줄 모르고 다음 단계를 시작한다."""
    import time

    clock = Clock()
    state_path = env.out / "driver_state.json"

    class DiesInE1(Runner):
        def __call__(self, cmd, cwd, env_, log_path):
            if cmd[cmd.index("--stage") + 1] != "e1":
                return super().__call__(cmd, cwd, env_, log_path)
            self.calls.append("e1")
            clock.t += 3 * 3600.0                       # e1이 3시간 돌았을 때
            deadline = time.time() + 5
            while time.time() < deadline:               # (주기 기록이 그 시각을 상태 파일에 적을 틈을 준 뒤)
                if json.loads(state_path.read_text())["invocations"][-1]["last_seen"] >= clock.t:
                    break
                time.sleep(0.01)
            raise KeyboardInterrupt("세션 끊김")        # 세션이 죽는다

    first = DiesInE1(clock=clock, seconds=1800.0)       # fit은 30분
    with pytest.raises(KeyboardInterrupt):
        drv.main(env.argv("--cu-rate", "2.0"), runner=first, opener=env.opener, clock=clock, heartbeat_seconds=0.01)
    assert first.calls == ["fit", "e1"]
    state = json.loads(state_path.read_text())
    assert state["stages"]["e1"]["status"] == "running"
    assert drv.wall_seconds(state) == pytest.approx(3.5 * 3600.0)       # 죽은 단계의 3시간이 들어 있다
    # 이어서 돌리면 2 CU/h x 3.5 h = 7 CU로 상한(6) 이상이라 남은 단계를 시작하지 않는다
    second = Runner(clock=clock)
    assert drv.main(env.argv("--cu-rate", "2.0"), runner=second, opener=env.opener, clock=clock) == drv.EXIT_OK
    assert second.calls == ["assemble"]
    compute = json.loads((env.out / "compute.json").read_text())
    assert compute["skipped_for_budget"] == ["e1", "e2p2", "e8", "e4", "e2p1"]
    assert compute["cu_estimated"] == pytest.approx(7.0) and compute["heartbeat_seconds"] == drv.HEARTBEAT_SECONDS


def test_heartbeat_stops_with_the_stage_and_never_outlives_it():
    import threading
    import time

    beats = []
    before = threading.active_count()
    with drv.Heartbeat(lambda: beats.append(1), 0.01):
        time.sleep(0.2)
    n = len(beats)
    assert n >= 2 and threading.active_count() == before
    time.sleep(0.05)
    assert len(beats) == n                                               # 블록을 나오면 더 적지 않는다
    with pytest.raises(KeyboardInterrupt):                               # 단계가 예외로 끝나도 스레드를 남기지 않는다
        with drv.Heartbeat(lambda: beats.append(1), 0.01):
            raise KeyboardInterrupt
    assert threading.active_count() == before
    with drv.Heartbeat(lambda: beats.append(1), 0):                      # 0 이하면 끈다
        time.sleep(0.03)
    assert len(beats) == n


def test_manifest_is_checked_against_the_registration_before_any_download(env):
    runner = Runner()
    key = "ebnerd_small/train/history.parquet"
    # (1) 필요한 파일이 빠진 manifest
    short = {**env.manifest, "files": {k: v for k, v in env.manifest["files"].items() if k != key}}
    (env.tmp / "manifest.json").write_text(json.dumps(short))
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_USAGE
    # (2) 등록값과 다른 sha256
    wrong = {**env.manifest, "files": {**env.manifest["files"], key: {"sha256": "f" * 64, "bytes": 1}}}
    (env.tmp / "manifest.json").write_text(json.dumps(wrong))
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_USAGE
    # (3) 등록하지 않은 데이터셋 이름
    renamed = {"dataset": "ebnerd_demo", "files": {k.replace("ebnerd_small", "ebnerd_demo"): v
                                                   for k, v in env.manifest["files"].items()}}
    (env.tmp / "manifest.json").write_text(json.dumps(renamed))
    assert drv.main(env.argv(), runner=runner, opener=env.opener) == drv.EXIT_USAGE
    assert env.opened == [] and runner.calls == [] and not (env.out / "FAILED").exists()   # 아무것도 받거나 돌리지 않았다
    # 등록하지 않은 입력은 명시적으로 허용해야만 받는다(그 결과는 run_cold가 demo 등급으로 표기한다)
    payload = dict(env.payload)
    payload[key] = b"other-bytes"
    import hashlib
    other = {**env.manifest, "files": {**env.manifest["files"],
                                       key: {"sha256": hashlib.sha256(payload[key]).hexdigest(), "bytes": 11}}}
    (env.tmp / "manifest.json").write_text(json.dumps(other))

    def opener(url, timeout=None):
        return io.BytesIO(payload[url.split("/private-bucket/")[1].split("?")[0]])

    assert drv.main(env.argv("--allow-unregistered-data"), runner=runner, opener=opener) == drv.EXIT_OK


def test_real_registration_lists_the_inputs_the_driver_checks():
    """드라이버가 문자열로 찾는 sha256들이 실제 사전 등록 yaml에 있어야 한다(v1 리포트의 입력과 같은 값)."""
    text = (REPO / drv.PREREG_YAML).read_text()
    for name, sha in PREREG["data"]["files_sha256"].items():
        assert name.endswith(drv.REGISTERED_INPUTS) and sha in text
    assert PREREG["data"]["embeddings_sha256"] in text and "bge_m3_tsb512.f16.npy".endswith(drv.REGISTERED_INPUTS)
    v1 = json.loads((REPO / "reports/recsys/ebnerd_v1.json").read_text())
    assert PREREG["data"]["files_sha256"] == {k: v for k, v in v1["data"]["files"].items() if k != "articles.parquet"}
    assert PREREG["data"]["embeddings_sha256"] == v1["data"]["catalog"]["embeddings_sha256"]
    assert PREREG["data"]["articles_original_sha256"] == v1["data"]["files"]["articles.parquet"]


def test_a_real_run_without_a_cu_rate_is_refused_before_anything_is_downloaded(env, capsys):
    """등록한 예산 규칙(상한 6 CU, 경고 5 CU)은 rate가 있어야 적용된다. rate 없이 도는 판정용 실행은 그 규칙을 말없이
    건너뛰는 것이므로 시작하지 않는다."""
    for extra in ((), ("--allow-unregistered-data",), ("--fresh",), ("--detach",)):
        runner = Runner()
        assert drv.main(env.argv(*extra, rate=None), runner=runner, opener=env.opener) == drv.EXIT_USAGE, extra
        assert env.opened == [] and runner.calls == [], extra
        assert not (env.out / "FAILED").exists() and not (env.out / "driver_state.json").exists(), extra
        assert not (env.work / "repo").exists() and not (env.out / "driver.pid").exists(), extra
    assert "--cu-rate" in capsys.readouterr().out
    assert drv.main(env.argv("--cu-rate", "-1"), runner=Runner(), opener=env.opener) == drv.EXIT_USAGE
    assert env.opened == []


def test_a_usage_error_does_not_wipe_an_earlier_run_even_with_fresh(env):
    assert drv.main(env.argv(), runner=Runner(), opener=env.opener) == drv.EXIT_OK
    before = (env.out / "driver_state.json").read_text()
    assert drv.main(env.argv("--fresh", rate=None), runner=Runner(), opener=env.opener) == drv.EXIT_USAGE
    assert (env.out / "driver_state.json").read_text() == before and (env.work / "data").exists()


def test_an_explicit_zero_rate_turns_the_budget_rules_off_and_the_record_says_so(env):
    """CU로 과금하지 않는 런타임에서는 --cu-rate 0을 명시한다. 그때는 아무것도 건너뛰지 않고, 규칙이 꺼져 있었다는 것이
    로그와 compute.json(리포트 meta.compute)에 남는다."""
    clock = Clock()
    runner = Runner(clock=clock, seconds=36000.0)       # 단계 하나에 10시간이어도
    assert drv.main(env.argv("--cu-rate", "0"), runner=runner, opener=env.opener, clock=clock) == drv.EXIT_OK
    assert runner.calls == list(drv.STAGES)
    compute = json.loads((env.out / "compute.json").read_text())
    assert compute["budget_rules_active"] is False and compute["rate_cu_per_hour"] == 0.0
    assert compute["cu_estimated"] == 0.0 and compute["skipped_for_budget"] == []
    assert "예산 규칙이 꺼져 있다" in (env.out / "run.log").read_text()


def test_a_synthetic_dry_run_needs_no_rate_and_is_recorded_as_unmetered(env):
    """드라이 런은 CU/h를 재려고 돌리는 것이라 rate를 요구하지 않는다."""
    runner = Runner()
    argv = ["run", "--workdir", str(env.work), "--code-tarball", str(env.tarball), "--synthetic", "--skip-install",
            "--python", "PY"]
    assert drv.main(argv, runner=runner) == drv.EXIT_OK
    assert runner.calls == ["synthetic", *drv.STAGES]
    compute = json.loads((env.out / "compute.json").read_text())
    assert compute["budget_rules_active"] is False and compute["rate_cu_per_hour"] is None
    assert compute["cu_estimated"] is None and compute["skipped_for_budget"] == []
    assert "예산 규칙이 꺼져 있다" in (env.out / "run.log").read_text()


def test_runtime_label_is_not_colab_unless_running_there_or_told_so(env, tmp_path):
    assert drv.main(env.argv(), runner=Runner(), opener=env.opener) == drv.EXIT_OK
    label = json.loads((env.out / "compute.json").read_text())["runtime"]
    if not (sys.platform.startswith("linux") and Path("/content").is_dir()):
        assert label.startswith("local-")
    assert drv.main(env.argv("--fresh", "--runtime-label", "colab-cpu-highmem"), runner=Runner(),
                    opener=env.opener) == drv.EXIT_OK
    assert json.loads((env.out / "compute.json").read_text())["runtime"] == "colab-cpu-highmem"


# --- tarball -----------------------------------------------------------------------------------

def test_tarball_commit_id_is_read_and_wrong_sha_or_escaping_paths_are_rejected(tmp_path):
    good = _tarball(tmp_path / "g.tar.gz", {"a/b.py": "x = 1\n"}, commit="d" * 40)
    info = drv.extract_tarball(good, tmp_path / "repo")
    assert info["commit"] == "d" * 40 and info["extracted"] and (tmp_path / "repo/a/b.py").read_text() == "x = 1\n"
    assert drv.extract_tarball(good, tmp_path / "repo")["extracted"] is False     # 같은 tarball이면 다시 풀지 않는다
    with pytest.raises(drv.DriverError) as e:
        drv.extract_tarball(good, tmp_path / "repo2", expected_sha256="0" * 64)
    assert e.value.code == drv.EXIT_SHA_MISMATCH and not (tmp_path / "repo2").exists()
    evil = _tarball(tmp_path / "e.tar.gz", {"../outside.py": "boom"})
    with pytest.raises(drv.DriverError):
        drv.extract_tarball(evil, tmp_path / "repo3")
    assert not (tmp_path / "outside.py").exists()


# --- 보조 명령 ---------------------------------------------------------------------------------

def test_manifest_command_hashes_local_files_and_uses_the_meta_only_articles_file(synth_root, tmp_path, capsys):
    meta = _load("make_articles_meta").make_articles_meta(synth_root / "ebnerd_synth", tmp_path / "articles.parquet")
    out = tmp_path / "m.json"
    assert drv.main(["manifest", "--root", str(synth_root), "--dataset", "ebnerd_synth", "--articles-meta",
                     meta["out"], "--out", str(out)]) == drv.EXIT_OK
    m = json.loads(out.read_text())
    assert len(m["files"]) == 8
    assert m["files"]["ebnerd_synth/articles.parquet"]["sha256"] == meta["derived_sha256"] == m["articles"]["derived_sha256"]
    assert m["articles"]["original_sha256"] == meta["original_sha256"] == drv.sha256_file(synth_root / "ebnerd_synth/articles.parquet")
    assert m["files"]["ebnerd_synth/train/behaviors.parquet"]["sha256"] == drv.sha256_file(
        synth_root / "ebnerd_synth/train/behaviors.parquet")
    assert all(re.fullmatch(r"[0-9a-f]{64}", v["sha256"]) for v in m["files"].values())


def test_meta_only_articles_file_loads_to_the_same_bench(synth_root, tmp_path):
    """메타 5개 열만 남긴 기사 파일로 바꿔도 로더 결과가 같다(그래서 본문이 든 원본을 올릴 필요가 없다)."""
    import shutil

    import numpy as np
    import pandas as pd

    from evaluation.recsys.ebnerd.loaders import ARTICLE_META_COLUMNS
    from evaluation.recsys.ebnerd.prepare import load_bench

    src = synth_root / "ebnerd_synth"
    full = tmp_path / "full" / "ebnerd_synth"
    shutil.copytree(src, full)
    arts = pd.read_parquet(full / "articles.parquet")
    arts["title"], arts["body"] = "제목 자리", "본문 자리"          # 원본처럼 텍스트 열이 있는 파일
    arts.to_parquet(full / "articles.parquet", index=False)
    slim = tmp_path / "slim" / "ebnerd_synth"
    shutil.copytree(full, slim)
    meta = _load("make_articles_meta").make_articles_meta(full, slim / "articles.parquet")
    assert meta["columns"] == ARTICLE_META_COLUMNS and meta["original_sha256"] != meta["derived_sha256"]
    assert list(pd.read_parquet(slim / "articles.parquet").columns) == ARTICLE_META_COLUMNS
    emb = synth_root / "derived" / "ebnerd_synth"
    a, b = load_bench(full, emb_dir=emb), load_bench(slim, emb_dir=emb)
    assert np.array_equal(a.catalog.ids, b.catalog.ids) and np.array_equal(a.catalog.pub_time, b.catalog.pub_time)
    assert np.array_equal(a.catalog.category, b.catalog.category) and np.array_equal(a.catalog.emb, b.catalog.emb)
    for s in ("train", "validation"):
        assert np.array_equal(a.ctx[s].user_log.item, b.ctx[s].user_log.item)
        assert np.array_equal(a.ctx[s].item_clicks.time, b.ctx[s].item_clicks.time)


def test_status_and_cleanup_report_state_without_data(env, capsys):
    assert drv.main(env.argv(), runner=Runner(), opener=env.opener) == drv.EXIT_OK
    capsys.readouterr()
    assert drv.main(["status", "--workdir", str(env.work)]) == drv.EXIT_OK
    out = capsys.readouterr().out
    summary = json.loads(out.splitlines()[0])
    assert summary["stages"]["assemble"] == "done" and summary["failed"] is False and "TOPSECRET" not in out
    assert drv.main(["cleanup", "--workdir", str(env.work)]) == drv.EXIT_OK
    left = json.loads(capsys.readouterr().out)
    assert not (env.work / "data").exists() and not any(p.startswith("data") for p in left["remaining"])
    assert "out/driver_state.json" in left["remaining"]


def test_arguments_can_be_passed_through_the_environment(env, monkeypatch):
    monkeypatch.setenv("M4_DRIVER_ARGS_JSON", json.dumps(["status", "--workdir", str(env.work)]))
    assert drv.main() == drv.EXIT_OK


# --- 실제 서브프로세스로 합성 데이터 사슬 전체 --------------------------------------------------

def test_synthetic_dry_run_goes_through_real_subprocesses_and_resumes(tmp_path):
    """드라이버 → `python -m evaluation.recsys.ebnerd.run_cold` → 상대 import → 인자 → 체크포인트 재개.
    tarball은 작업 트리의 실행 경로 파일로 만든다(git archive와 같은 경로 구성)."""
    tarball = tmp_path / "code.tar.gz"
    with tarfile.open(tarball, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": "e" * 40}) as tf:
        for rel in ("recsys_core", "evaluation/__init__.py", "evaluation/recsys", "scripts/m4_colab_driver.py",
                    "requirements-colab.txt"):
            tf.add(REPO / rel, arcname=rel, filter=lambda ti: None if "__pycache__" in ti.name else ti)
    work = tmp_path / "work"
    argv = ["run", "--workdir", str(work), "--code-tarball", str(tarball), "--synthetic", "--skip-install",
            "--python", sys.executable, "--threads", "2", "--cu-rate", "0.1"]
    calls = []

    def dying(cmd, cwd, env, log_path):
        calls.append(cmd[cmd.index("--stage") + 1] if "--stage" in cmd else "synthetic")
        if calls[-1] == "e2p2":
            raise KeyboardInterrupt("세션 끊김")
        return drv.subprocess_runner(cmd, cwd, env, log_path)

    with pytest.raises(KeyboardInterrupt):
        drv.main(argv, runner=dying)
    assert calls == ["synthetic", "fit", "e1", "e2p2"]
    assert drv.main(argv) == drv.EXIT_OK
    report = json.loads((work / "out" / "ebnerd_v1_2_cold.json").read_text())
    assert report["meta"]["evidence"]["grade"] == "demo, not evidence"
    assert report["meta"]["code_sha"] == "e" * 40                      # tarball의 커밋 id가 리포트까지 간다
    assert report["meta"]["compute"]["rate_cu_per_hour"] == 0.1 and report["meta"]["compute"]["cu_estimated"] is not None
    assert report["unmeasured"]["stages"] == [] and "orig|full" in report["e1"]["cells"]
    assert "demo, not evidence" in (work / "out" / "ebnerd_v1_2_cold.md").read_text()
    state = json.loads((work / "out" / "driver_state.json").read_text())
    assert len(state["invocations"]) == 2 and all(v["status"] == "done" for v in state["stages"].values())


def test_detached_run_can_be_stopped_and_then_resumed_to_completion(tmp_path, capsys):
    """--detach는 바로 돌아오고, stop은 마커 없이 끝내며, 같은 명령을 다시 주면 이어서 끝난다. URL은 자식의 명령줄에 없다."""
    import time

    tarball = tmp_path / "code.tar.gz"
    with tarfile.open(tarball, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": "f" * 40}) as tf:
        for rel in ("recsys_core", "evaluation/__init__.py", "evaluation/recsys", "scripts/m4_colab_driver.py",
                    "requirements-colab.txt"):
            tf.add(REPO / rel, arcname=rel, filter=lambda ti: None if "__pycache__" in ti.name else ti)
    work = tmp_path / "work"
    argv = ["run", "--workdir", str(work), "--code-tarball", str(tarball), "--synthetic", "--skip-install",
            "--python", sys.executable, "--threads", "2"]
    assert drv._child_argv(argv + ["--detach", "--url", f"a={_url('a')}", f"--url=b={_url('b')}"]) == argv
    try:
        t0 = time.time()
        assert drv.main(argv + ["--detach"]) == drv.EXIT_OK
        assert time.time() - t0 < 10                                  # 사슬이 끝나기를 기다리지 않는다
        pid = json.loads((work / "out" / "driver.pid").read_text())["pid"]
        assert drv._pid_alive(pid)
        assert drv.main(argv + ["--detach"]) == drv.EXIT_USAGE          # 이미 도는 중이면 또 띄우지 않는다
        state_path = work / "out" / "driver_state.json"
        deadline = time.time() + 120
        while time.time() < deadline:
            if state_path.exists() and json.loads(state_path.read_text())["stages"].get("fit", {}).get("status") == "done":
                break
            time.sleep(0.5)
        else:
            pytest.fail("분리 실행이 fit 단계를 끝내지 못했다")
        capsys.readouterr()
        assert drv.main(["status", "--workdir", str(work)]) == drv.EXIT_OK
        assert json.loads(capsys.readouterr().out.splitlines()[0])["detached_running"] is True
        assert drv.main(["stop", "--workdir", str(work)]) == drv.EXIT_OK
        assert not drv._pid_alive(pid) and not (work / "out" / "FAILED").exists()
    finally:
        drv.main(["stop", "--workdir", str(work), "--wait", "5"])
    assert drv.main(argv) == drv.EXIT_OK                                # 이어서 끝까지
    state = json.loads((work / "out" / "driver_state.json").read_text())
    assert all(v["status"] == "done" for v in state["stages"].values()) and len(state["invocations"]) >= 2
    assert (work / "out" / "ebnerd_v1_2_cold.json").exists()
    capsys.readouterr()
    assert drv.main(["stop", "--workdir", str(work)]) == drv.EXIT_OK
    assert json.loads(capsys.readouterr().out)["stopped"] is False
