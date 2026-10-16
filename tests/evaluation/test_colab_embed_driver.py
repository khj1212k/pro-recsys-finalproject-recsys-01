"""기사 임베딩 원격 드라이버(scripts/colab_embed_articles.py): 입력 확인, 핀·환경 확인, CU 상한, 입력 삭제, URL 비노출.

네트워크도 torch도 없이 돈다. 잡 실행은 가짜 runner, 설치는 가짜 installer, 환경 확인은 가짜 probe로 바꿔 끼운다.
입력 반출본은 tests/frozen_news_fakes.py의 지어낸 기사로 만든다.
"""
import importlib.util
import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRET = "X-Goog-Signature=TOPSECRET0123456789"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drv = _load("colab_embed_articles")
m4 = drv.m4

from evaluation.llm import embed_job as job  # noqa: E402
from evaluation.llm import frozen_export as fx  # noqa: E402
from tests.frozen_news_fakes import BODY_MARK, CODE, TITLE_MARK, stream, topic_articles, use_tmp_main_checkout  # noqa: E402

WINDOW = fx.Window.from_iso("2026-10-02T21:00:00", "2026-10-05T21:00:00")
PINS = (REPO / drv.REQUIREMENTS).read_text(encoding="utf-8")


def _tarball(path: Path, files: dict, commit="c" * 40) -> Path:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def probe(**over):
    pins = dict(re.findall(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s#;]*)", PINS, flags=re.MULTILINE))
    info = {**pins, "torch": "2.11.0+cu128", "_cuda_available": True, "_gpu": "Tesla T4", "_cuda": "12.8",
            "_python": "3.13.15"}
    info.update(over)
    return lambda python, names: dict(info)


class Clock:
    def __init__(self, step=0.0):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class Runner:
    """잡 명령을 기록하는 가짜 실행기. 성공하면 job.json을 쓴다."""

    def __init__(self, code=0, wait_for_stop=False):
        self.cmds, self.code, self.wait_for_stop = [], code, wait_for_stop

    def __call__(self, cmd, cwd, env, log_path):
        import time

        self.cmds.append(cmd)
        self.env = env
        out = Path(cmd[cmd.index("--out-dir") + 1])
        if self.wait_for_stop:
            for _ in range(500):
                if (out / drv.STOP_FILE).exists():
                    return drv.JOB_EXIT_BUDGET
                time.sleep(0.01)
            return 0
        if self.code == 0:
            (out / drv.JOB_FILE).write_text(json.dumps({"rows": 50, "encode_s": 1.0}))
        return self.code


@pytest.fixture(autouse=True)
def _main_checkout(monkeypatch, tmp_path):
    return use_tmp_main_checkout(monkeypatch, tmp_path)


@pytest.fixture
def export_dir(tmp_path):
    rows, _ = topic_articles()
    dest = tmp_path / "data" / "exports" / "t0"
    fx.write_export(dest, fx.build_export(fx.parse_copy_stream(stream(rows)), WINDOW, code=CODE))
    return dest


@pytest.fixture
def workdir(tmp_path, export_dir):
    """원격 작업 디렉터리: 코드 tarball과, colab upload로 올린 것처럼 input/에 복사한 반출본."""
    wd = tmp_path / "remote"
    (wd / "input").mkdir(parents=True)
    for name in drv.INPUT_FILES:
        (wd / "input" / name).write_bytes((export_dir / name).read_bytes())
    _tarball(wd / "code.tar.gz", {drv.REQUIREMENTS: PINS, "scripts/colab_embed_articles.py": "#",
                                  "scripts/m4_colab_driver.py": "#"})
    return wd


def identity(export_dir):
    return json.loads((export_dir / fx.MANIFEST_FILE).read_text())["identity_sha256"]


def run(wd, *extra, runner=None, clock=None, env_probe=None, heartbeat_seconds=60.0):
    argv = ["run", "--workdir", str(wd), "--code-tarball", str(wd / "code.tar.gz"), "--cu-rate", "1.07",
            "--python", "python3", *extra]
    args = drv.build_parser().parse_args(argv)
    args.raw_argv = argv
    return drv.cmd_run(args, runner=runner or Runner(), clock=clock or Clock(), installer=lambda *a: 0,
                       env_probe=env_probe or probe(), heartbeat_seconds=heartbeat_seconds)


def _texts(wd):
    return "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (wd / "out").rglob("*") if p.is_file())


# ---------------------------------------------------------------- 잡 모듈·핀과 같은 값


def test_driver_constants_match_the_job_module():
    assert (drv.STOP_FILE, drv.PROGRESS_FILE, drv.JOB_FILE, drv.PACK_DIR, drv.PARTS_DIR) == (
        job.STOP_FILE, job.PROGRESS_FILE, job.JOB_FILE, job.PACK_DIR, job.PARTS_DIR)
    assert (drv.JOB_EXIT_BUDGET, drv.JOB_EXIT_INPUT, drv.JOB_EXIT_ENVIRONMENT) == (
        job.EXIT_BUDGET, job.EXIT_INPUT, job.EXIT_ENVIRONMENT)
    sample = {"b": [1, "가"], "a": {"z": None, "y": 1.5}}
    assert drv.canonical_json(sample) == fx.canonical_json(sample)


def test_embedding_pins_are_the_production_worker_lock_values():
    lock = (REPO / "docker" / "requirements-worker.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s#;]*)", PINS, flags=re.MULTILINE))
    assert pins.pop("torch") == "2.11.0"  # 런타임에 미리 깔린 것을 확인만 한다(운영 워커는 2.14.0 CPU - 이탈 5)
    for name, version in pins.items():
        assert re.search(rf"^{re.escape(name)}=={re.escape(version)}\b", lock, flags=re.MULTILINE | re.IGNORECASE), name


# ---------------------------------------------------------------- 입력


def test_run_embeds_and_then_deletes_the_uploaded_bodies(workdir, export_dir):
    runner = Runner()
    code = run(workdir, "--export-identity", identity(export_dir), runner=runner)
    assert code == m4.EXIT_OK
    assert runner.cmds[0][1:4] == ["-m", drv.MODULE, "--out-dir"] and "--input-dir" in runner.cmds[0]
    assert not (workdir / "input").exists()
    cleanup = json.loads((workdir / "out" / "cleanup.json").read_text())
    assert cleanup["reason"] == "done" and cleanup["input_dir_exists_after"] is False
    assert sorted(x["name"] for x in cleanup["removed"]) == sorted(drv.INPUT_FILES)
    assert all(set(x) == {"name", "bytes"} for x in cleanup["remaining"])
    text = _texts(workdir)
    assert BODY_MARK not in text and TITLE_MARK not in text and "소식" not in text
    assert "PYTHONPATH" in runner.env and drv.URLS_ENV not in runner.env


@pytest.mark.parametrize("damage", ["identity", "file", "missing"])
def test_wrong_or_damaged_input_runs_nothing_and_is_deleted(workdir, export_dir, damage):
    expected = identity(export_dir)
    if damage == "identity":
        expected = "0" * 64
    elif damage == "file":
        path = workdir / "input" / "articles.jsonl"
        path.write_bytes(path.read_bytes()[:-10])
    else:
        (workdir / "input" / "manifest.json").unlink()
    runner = Runner()
    assert run(workdir, "--export-identity", expected, runner=runner) == drv.EXIT_INPUT_REFUSED
    assert runner.cmds == [] and not (workdir / "input").exists()
    assert json.loads((workdir / "out" / "cleanup.json").read_text())["reason"] == "refused"


def test_job_failure_leaves_a_failed_marker_and_still_deletes_the_bodies(workdir, export_dir):
    assert run(workdir, "--export-identity", identity(export_dir), runner=Runner(code=5)) == m4.EXIT_STAGE_FAILED
    assert (workdir / "out" / "FAILED").exists() and not (workdir / "input").exists()


def test_signed_urls_are_accepted_only_for_the_model_snapshot_and_never_logged(workdir, export_dir):
    url = f"https://storage.example.invalid/b/articles.jsonl?{SECRET}"
    code = run(workdir, "--export-identity", identity(export_dir), "--url", f"input={url}")
    assert code == m4.EXIT_USAGE
    assert SECRET not in _texts(workdir) and "storage.example.invalid" not in _texts(workdir)


def test_model_snapshot_download_failure_does_not_print_the_url(workdir, export_dir, capsys):
    def opener(url, timeout=None):
        raise OSError(f"connection reset while fetching {url}")

    url = f"https://storage.example.invalid/b/model.tar?{SECRET}"
    argv = ["run", "--workdir", str(workdir), "--code-tarball", str(workdir / "code.tar.gz"), "--cu-rate", "1.07",
            "--export-identity", identity(export_dir), "--url", f"model_snapshot={url}",
            "--model-snapshot-sha256", "a" * 64]
    args = drv.build_parser().parse_args(argv)
    args.raw_argv = argv
    code = drv.cmd_run(args, runner=Runner(), clock=Clock(), installer=lambda *a: 0, env_probe=probe(), opener=opener)
    assert code == m4.EXIT_DOWNLOAD
    assert SECRET not in _texts(workdir) + capsys.readouterr().out
    assert not (workdir / "input").exists()


# ---------------------------------------------------------------- 환경


def test_a_package_that_differs_from_its_pin_stops_before_the_job(workdir, export_dir):
    runner = Runner()
    code = run(workdir, "--export-identity", identity(export_dir), runner=runner,
               env_probe=probe(transformers="5.20.1"))
    assert code == drv.EXIT_ENVIRONMENT and runner.cmds == []
    assert not (workdir / "input").exists()


def test_a_different_torch_or_no_cuda_stops_before_the_job(workdir, export_dir):
    assert run(workdir, "--export-identity", identity(export_dir), env_probe=probe(torch="2.10.0+cu126")) == \
        drv.EXIT_ENVIRONMENT


def test_no_cuda_stops_unless_allowed(tmp_path):
    wd = tmp_path / "w"
    wd.mkdir()
    _tarball(wd / "code.tar.gz", {drv.REQUIREMENTS: PINS, "scripts/colab_embed_articles.py": "#"})
    assert run(wd, "--synthetic", "10", env_probe=probe(_cuda_available=False)) == drv.EXIT_ENVIRONMENT
    assert run(wd, "--synthetic", "10", "--allow-cpu", env_probe=probe(_cuda_available=False)) == m4.EXIT_OK


# ---------------------------------------------------------------- 예산·재개


def test_cu_cap_asks_the_job_to_stop_and_the_bodies_are_deleted(workdir, export_dir):
    # 시계가 부를 때마다 30분씩 간다: 1.07 CU/h면 몇 번 만에 상한 0.3을 넘는다
    code = run(workdir, "--export-identity", identity(export_dir), "--cu-cap", "0.3",
               runner=Runner(wait_for_stop=True), clock=Clock(step=1800.0), heartbeat_seconds=0.01)
    assert code == drv.EXIT_BUDGET
    assert json.loads((workdir / "out" / "compute.json").read_text())["outcome"] == "budget_stopped"
    assert not (workdir / "input").exists()


def test_rerun_with_a_different_input_is_refused(workdir, export_dir, tmp_path):
    assert run(workdir, "--synthetic", "10") == m4.EXIT_OK
    assert run(workdir, "--synthetic", "11") == m4.EXIT_CONFIG_MISMATCH


def test_synthetic_dry_run_needs_no_input_and_touches_none(workdir):
    runner = Runner()
    assert run(workdir, "--synthetic", "300", runner=runner) == m4.EXIT_OK
    assert runner.cmds[0][-2:] == ["--synthetic", "300"]
    assert (workdir / "input" / "articles.jsonl").exists()  # 합성 실행은 올린 파일을 건드리지 않는다


def test_bundle_round_trip_restores_parts_in_a_new_workdir(tmp_path):
    out = tmp_path / "a" / "out"
    job.run_job(job.synthetic_rows(70), out, input_info={"synthetic": "t"}, part_rows=64, write_pack=False,
                fp32_check=False, log=lambda m: None,
                embedder_factory=lambda use_fp16=None: _FakeEmbedder())
    info = drv.make_bundle(out, tmp_path / "parts.tar.gz")
    new = tmp_path / "b"
    restored = drv.restore_bundle(Path(info["out"]), info["sha256"], new, m4.Log(None))
    assert restored["files"] == info["files"] == 5  # progress.json + 파트 2개 x (ids, emb)
    assert sorted(p.name for p in (new / "out" / "parts").iterdir()) == sorted(
        p.name for p in (out / "parts").iterdir())
    with pytest.raises(m4.DriverError):
        drv.restore_bundle(Path(info["out"]), "0" * 64, tmp_path / "c", m4.Log(None))


class _FakeEmbedder:
    device = "cuda"

    def generate_embeddings_batch(self, texts, batch_size=20):
        import numpy as np

        return [np.eye(1024, dtype=np.float32)[len(t) % 1024].tolist() for t in texts], 0.0


def test_manual_cleanup_lists_names_and_sizes_only(workdir, capsys):
    argv = ["cleanup", "--workdir", str(workdir)]
    assert drv.main(argv) == m4.EXIT_OK
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert sorted(x["name"] for x in printed["removed"]) == sorted(drv.INPUT_FILES)
    assert not (workdir / "input").exists()


# ---------------------------------------------------------------- 커널 런처


def test_launcher_checks_the_tarball_and_runs_the_driver_from_it(tmp_path):
    launcher = _load("colab_embed_launch")
    wd = tmp_path / "wd"
    wd.mkdir()
    tar = _tarball(wd / "code.tar.gz", {"scripts/colab_embed_articles.py": "# driver", "scripts/m4_colab_driver.py": "#"})
    calls = []

    def run(cmd, env, cwd):
        calls.append((cmd, env, cwd))
        return type("R", (), {"returncode": 0})()

    args = json.dumps(["run", "--workdir", str(wd)])
    env = {"E0EMB_WORKDIR": str(wd), "E0EMB_CODE_SHA256": "0" * 64, "E0EMB_DRIVER_ARGS_JSON": args}
    assert launcher.launch(env, run=run) == 2 and calls == []
    env["E0EMB_CODE_SHA256"] = m4.sha256_file(tar)
    assert launcher.launch(env, run=run) == 0
    cmd, passed, cwd = calls[0]
    assert cmd[1] == str(wd / "drv" / "scripts" / "colab_embed_articles.py") and passed["E0EMB_DRIVER_ARGS_JSON"] == args
    assert (wd / "drv" / "scripts" / "m4_colab_driver.py").read_text() == "#"
    assert launcher.launch({"E0EMB_WORKDIR": str(wd)}, run=run) == 64
