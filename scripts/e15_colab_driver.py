#!/usr/bin/env python3
"""E15(신경망 사용자 모델 vs LightGBM)를 원격 런타임(Colab)에서 세션 단위로 돌리는 드라이버 — 표준 라이브러리만 쓴다.

같은 디렉터리의 m4_colab_driver.py(콜드 regime 사슬의 드라이버)를 불러 그 도구를 그대로 쓴다: tarball 확인과 안전한
풀기, 서명 URL 내려받기와 URL 비노출, manifest sha256 대조, 핀 설치, heartbeat, 분리 실행·stop·cleanup. 두 파일을 함께 올린다.
무엇을 어떤 인자로 돌리는지는 ADR 0013 "A3 사전 등록" A3.9·A3.11에 실행 전에 고정했다.

    python e15_colab_driver.py run --workdir /content/e15 --code-tarball /content/e15_code.tar.gz --session s0   # 드라이 런
    python e15_colab_driver.py run --workdir /content/e15 --code-tarball /content/e15_code.tar.gz \
        --code-sha256 <sha256> --session s2 --manifest /content/e15_manifest.json \
        --url ebnerd_small/train/behaviors.parquet=<서명 URL> [--url ...] --cu-rate <CU/h> --cu-spent-before <CU>
    python e15_colab_driver.py run ... --session s3 --resume-bundle /content/e15_bundle.tar.gz \
        --resume-bundle-sha256 <sha256> [--detach]
    python e15_colab_driver.py bundle --workdir /content/e15 --out /content/e15_bundle.tar.gz
    python e15_colab_driver.py status|stop|cleanup --workdir /content/e15
    python e15_colab_driver.py manifest --root <EBNERD_ROOT> --articles-meta <메타 전용 parquet> --out e15_manifest.json

콜드 regime 드라이버와 같은 규칙:
- 서명 URL은 인자(--url) 또는 환경변수(E15_URLS_JSON)로만 받고 로그·상태 파일·오류 메시지 어디에도 쓰지 않는다.
- 데이터 sha256 불일치와 단계 실패(0이 아닌 종료 코드)는 실행 무효다(FAILED 마커, --fresh 없이는 다시 돌지 않는다).
  내려받기·설치 실패와 환경 확인 실패는 무효가 아니고 같은 명령으로 다시 시도한다.
- 세션이 끊기면 같은 명령으로 다시 실행한다. 설정 해시가 같을 때만 끝난 단계를 건너뛴다(단계 안의 단위는 run_neural이 건너뛴다).
- argv를 넘길 수 없는 실행기에서는 환경변수 E15_DRIVER_ARGS_JSON(인자 목록의 JSON)으로 같은 인자를 준다.
E15에서 더한 규칙:
- 세션(s0~s4)이 과제·단계·런타임·세션 CU 상한을 정한다(사전 등록 yaml의 sessions와 같은 표, 테스트가 묶는다).
- 단계를 돌리기 전에 설치된 torch의 버전이 requirements-colab-gpu.txt의 핀과 같은지 본다. T4 세션은 CUDA도 본다.
- 예산은 `앞 세션까지 쓴 CU(--cu-spent-before) + rate x 이 세션의 벽시계 시간`으로 본다. 전체 경고선 이상이면 서술용 단계를
  건너뛰고, 전체 상한이나 세션 상한 이상이면 남은 단계를 돌리지 않는다(리포트에 미측정으로 남는다). assemble은 항상 돈다.
- 뒤 세션은 앞 세션의 체크포인트 묶음(--resume-bundle, sha256 확인)을 받아 이어서 돈다. FAILED 마커가 있는 실행은 묶지 않는다.
- run_neural이 종료 코드 4(재현 게이트)나 5(결정론 게이트)로 끝나면 마커에 그 사유를 적는다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional


def _load_m4():
    path = Path(__file__).resolve().parent / "m4_colab_driver.py"
    if not path.exists():
        raise SystemExit("m4_colab_driver.py가 이 파일과 같은 디렉터리에 있어야 합니다(두 드라이버 파일을 함께 올리세요).")
    spec = importlib.util.spec_from_file_location("m4_colab_driver", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("m4_colab_driver", mod)
    spec.loader.exec_module(mod)
    return mod


m4 = _load_m4()
DriverError, Log, write_json, read_json = m4.DriverError, m4.Log, m4.write_json, m4.read_json

MODULE = "evaluation.recsys.ebnerd.run_neural"
REQUIREMENTS = "requirements-colab-gpu.txt"
PREREG_YAML = "evaluation/recsys/ebnerd/preregistration/neural-e15.yaml"
ALL_STAGES = ("gate", "gbdt_tune", "gbdt_final", "determinism", "neural_tune", "select", "neural_final", "stack", "cold",
              "narrative", "assemble")
NEURAL_STAGES = ("determinism", "neural_tune", "select", "neural_final", "stack", "cold", "narrative")
DESCRIPTIVE_STAGES = ("narrative",)
# 사전 등록 yaml의 sessions·run·budget과 같은 값이다. 이 파일은 yaml 없이 돌아야 해서 여기 적고,
# 두 곳이 같은지는 테스트(tests/evaluation/test_e15_colab_driver.py)가 지킨다.
SESSIONS = {
    "s0": {"runtime": "cpu", "synthetic": True, "tasks": ("p1", "p2"), "stages": ALL_STAGES, "cu_cap": 0.5, "judged": False},
    "s1": {"runtime": "cpu", "tasks": ("p1", "p2"), "stages": ("gate", "determinism"), "seeds": [0], "cu_cap": 1.0,
           "judged": False},
    "s2": {"runtime": "cpu", "tasks": ("p1", "p2"), "stages": ("gate", "gbdt_tune", "gbdt_final"), "cu_cap": 8.0,
           "judged": True},
    "s3": {"runtime": "t4", "tasks": ("p1",), "stages": NEURAL_STAGES, "cu_cap": 9.0, "judged": True, "recheck_gate": True,
           "resumes_from": "s2"},
    "s4": {"runtime": "t4", "tasks": ("p2",), "stages": NEURAL_STAGES + ("assemble",), "cu_cap": 9.0, "judged": True,
           "recheck_gate": True, "resumes_from": "s3"},
}
REGISTERED = {"dataset": "ebnerd_small", "seeds": [0, 1, 2], "n_boot": 1000, "p2_sample": 20000, "p2_cold_sample": 60000,
              "p2_select_sample": 5000, "neural_trials": 12, "gbdt_trials": 24, "tune_max_epochs": 6,
              "final_max_epochs": 20, "patience": 2}
SYNTHETIC = {"dataset": "ebnerd_synth", "seeds": [0], "n_boot": 50, "p2_sample": 150, "p2_cold_sample": 200,
             "p2_select_sample": 80, "neural_trials": 1, "gbdt_trials": 2, "tune_max_epochs": 1, "final_max_epochs": 2,
             "patience": 2}
CU_CAP_TOTAL, CU_WARN_TOTAL = 30.0, 24.0
RUN_NEURAL_EXIT = {4: ("reproduction_gate", "재현 게이트 실패"), 5: ("determinism_gate", "결정론 게이트 실패")}
EXIT_ENVIRONMENT = 8
URLS_ENV, ARGS_ENV = "E15_URLS_JSON", "E15_DRIVER_ARGS_JSON"
# 묶음에 넣지 않는 것: 그 세션의 드라이버 상태와 로그(다음 세션은 자기 것을 새로 쓴다)
BUNDLE_EXCLUDE = ("driver_state.json", "driver.pid", "run.log", "driver_stdout.log", "FAILED")


# --- 계획 --------------------------------------------------------------------------------------

def session_plan(session: str) -> list[dict]:
    """세션이 도는 (과제, 단계) 순서. 여러 과제를 도는 세션은 단계가 바깥이다 — 모든 과제의 재현 게이트가 튜닝보다 먼저 온다.
    T4 세션은 맨 앞에 그 VM에서의 재현 게이트 재확인을 넣는다. assemble은 과제 없이 맨 끝에 한 번."""
    s = SESSIONS[session]
    steps = [{"id": f"{t}:gate:recheck", "task": t, "stage": "gate", "recheck": session} for t in s["tasks"]] \
        if s.get("recheck_gate") else []
    for stage in s["stages"]:
        if stage != "assemble":
            steps += [{"id": f"{t}:{stage}", "task": t, "stage": stage, "recheck": None} for t in s["tasks"]]
    if "assemble" in s["stages"]:
        steps.append({"id": "assemble", "task": None, "stage": "assemble", "recheck": None})
    return steps


def session_run_args(session: str) -> dict:
    s = SESSIONS[session]
    args = dict(SYNTHETIC if s.get("synthetic") else REGISTERED)
    if s.get("seeds"):
        args["seeds"] = list(s["seeds"])
    return args


def stage_command(python: str, workdir: Path, run_args: dict, threads: int, step: dict, device: str = "auto") -> list[str]:
    """사전 등록 A3.11의 단계 명령. 인자 순서까지 등록한 그대로다."""
    if step["stage"] == "assemble":
        return [python, "-m", MODULE, "--stage", "assemble", "--out-dir", str(workdir / "out"), "--n-boot",
                str(run_args["n_boot"])]
    cmd = [python, "-m", MODULE, "--dataset", run_args["dataset"], "--root", str(workdir / "data"),
           "--out-dir", str(workdir / "out"), "--task", step["task"], "--stage", step["stage"],
           "--seeds", *[str(x) for x in run_args["seeds"]], "--n-boot", str(run_args["n_boot"]),
           "--p2-sample", str(run_args["p2_sample"]), "--p2-cold-sample", str(run_args["p2_cold_sample"]),
           "--p2-select-sample", str(run_args["p2_select_sample"]), "--neural-trials", str(run_args["neural_trials"]),
           "--gbdt-trials", str(run_args["gbdt_trials"]), "--tune-max-epochs", str(run_args["tune_max_epochs"]),
           "--final-max-epochs", str(run_args["final_max_epochs"]), "--patience", str(run_args["patience"]),
           "--threads", str(threads), "--device", device, "--resume"]
    return cmd + (["--recheck-gate", step["recheck"]] if step.get("recheck") else [])


# --- 환경 확인 ---------------------------------------------------------------------------------

def pinned_torch(repo: Path) -> str:
    text = (repo / REQUIREMENTS).read_text(encoding="utf-8") if (repo / REQUIREMENTS).exists() else ""
    m = re.search(r"^torch==([0-9][^\s#]*)", text, flags=re.MULTILINE)
    if not m:
        raise DriverError(f"{REQUIREMENTS}에 torch 핀이 없습니다", m4.EXIT_USAGE)
    return m.group(1)


def probe_torch(python: str) -> dict:
    """단계를 돌릴 인터프리터에서 torch 버전과 장치를 읽는다(설치 뒤의 새 프로세스)."""
    code = ("import json, torch; c = torch.cuda.is_available(); print(json.dumps({'torch': torch.__version__, "
            "'cuda_available': c, 'gpu': torch.cuda.get_device_name(0) if c else None, 'cuda': torch.version.cuda}))")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": type(e).__name__}
    if out.returncode != 0:
        return {"error": (out.stderr or "").strip().splitlines()[-1:] or ["torch를 임포트하지 못했습니다"]}
    return json.loads(out.stdout.strip().splitlines()[-1])


def check_environment(session: str, repo: Path, python: str, probe: Callable[[str], dict], allow_cpu: bool) -> dict:
    """torch 버전이 핀과 같은지, T4 세션이면 CUDA가 있는지. 다르면 아무 단계도 돌리지 않는다(무효 처리 아님)."""
    want = pinned_torch(repo)
    info = probe(python)
    if info.get("error"):
        raise DriverError(f"torch 확인 실패: {info['error']}", EXIT_ENVIRONMENT)
    have = str(info["torch"]).split("+", 1)[0]
    if have != want:
        raise DriverError(f"설치된 torch {info['torch']}가 핀 {want}과 다릅니다(런타임을 바꾸거나 새로 사전 등록하세요).",
                          EXIT_ENVIRONMENT)
    if SESSIONS[session]["runtime"] == "t4" and not info.get("cuda_available") and not allow_cpu:
        raise DriverError("이 세션은 GPU 런타임에서 돌아야 하는데 CUDA를 쓸 수 없습니다(--allow-cpu는 결과를 느리게만 "
                          "할 뿐이지만 등록한 런타임이 아니므로 기록에 남습니다).", EXIT_ENVIRONMENT)
    return {**info, "torch_pin": want}


# --- 체크포인트 묶음 ---------------------------------------------------------------------------

def make_bundle(out_dir: Path, dest: Path) -> dict:
    if (out_dir / "FAILED").exists():
        raise DriverError("FAILED 마커가 있는 실행은 묶지 않습니다(무효 실행을 다음 세션의 출발점으로 쓰지 않는다).",
                          m4.EXIT_FAILED_MARKER)
    if not (out_dir / "progress.json").exists():
        raise DriverError("묶을 체크포인트가 없습니다(progress.json 없음)", m4.EXIT_USAGE)
    files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.relative_to(out_dir).parts[0] not in BUNDLE_EXCLUDE
                   and not p.name.endswith((".tmp", ".part")))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        for p in files:
            tf.add(p, arcname=p.relative_to(out_dir).as_posix())
    return {"out": str(dest), "files": len(files), "bytes": dest.stat().st_size, "sha256": m4.sha256_file(dest)}


def restore_bundle(bundle: Path, expected_sha256: Optional[str], workdir: Path, log: Log) -> Optional[dict]:
    """앞 세션의 묶음을 out/에 푼다. 이미 체크포인트가 있으면(같은 세션을 이어서 도는 중) 건드리지 않는다."""
    out_dir = workdir / "out"
    if (out_dir / "progress.json").exists():
        log("bundle: out/에 체크포인트가 이미 있다 — 묶음을 다시 풀지 않는다")
        return None
    if not expected_sha256:
        raise DriverError("--resume-bundle에는 --resume-bundle-sha256이 필요합니다", m4.EXIT_USAGE)
    if not bundle.exists() or m4.sha256_file(bundle) != expected_sha256.lower():
        raise DriverError("체크포인트 묶음이 없거나 sha256이 --resume-bundle-sha256과 다릅니다(아무것도 풀지 않았습니다)",
                          m4.EXIT_SHA_MISMATCH)
    tmp = workdir / "bundle_tmp"
    info = m4.extract_tarball(bundle, tmp)       # 경로 탈출·링크 거부
    n = 0
    for p in sorted(tmp.rglob("*")):
        rel = p.relative_to(tmp)
        if p.is_file() and rel.parts[0] not in BUNDLE_EXCLUDE and rel.name != ".tarball_sha256":
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out_dir / rel)
            n += 1
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"bundle: sha256 {info['sha256'][:12]}… 일치, 파일 {n}개를 out/에 풀었다")
    return {"sha256": info["sha256"], "files": n}


# --- 단계 실행 ---------------------------------------------------------------------------------

def run_steps(workdir: Path, state: dict, state_path: Path, *, python: str, threads: int, device: str,
              cu_rate: Optional[float], cu_spent_before: float, cu_cap_total: float, cu_warn_total: float, log: Log,
              runner: Callable = m4.subprocess_runner, clock: Callable[[], float] = time.time,
              extra_env: Optional[dict] = None, heartbeat_seconds: float = m4.HEARTBEAT_SECONDS) -> int:
    """남은 단계를 순서대로 돌린다. 끝난 단계는 건너뛰고, 예산 규칙으로 건너뛴 단계는 state에 적는다."""
    out_dir, session = workdir / "out", state["session"]
    session_cap = SESSIONS[session]["cu_cap"]
    env = dict(os.environ)
    env.update({"EBNERD_ROOT": str(workdir / "data"), "PYTHONUNBUFFERED": "1", "CUBLAS_WORKSPACE_CONFIG": ":4096:8"})
    env.update(extra_env or {})
    for k in (URLS_ENV, "M4_URLS_JSON", ARGS_ENV):        # 서브프로세스에는 URL을 넘기지 않는다
        env.pop(k, None)
    invocation = state["invocations"][-1]
    budget_active = cu_rate is not None and cu_rate > 0

    def beat():
        invocation["last_seen"] = clock()
        write_json(state_path, state)

    def session_cu() -> Optional[float]:
        return None if cu_rate is None else cu_rate * m4.wall_seconds(state) / 3600.0

    def write_compute():
        cu = session_cu()
        all_sessions = (read_json(out_dir / "compute.json") or {}).get("sessions", {})
        all_sessions[session] = {
            **state.get("host", {}), "runtime": state.get("runtime_label"), "environment": state.get("environment"),
            "wall_seconds_total": round(m4.wall_seconds(state), 1),
            "wall_seconds_by_step": {s: v["seconds"] for s, v in state["stages"].items() if v.get("status") == "done"},
            "rate_cu_per_hour": cu_rate, "cu_estimated": None if cu is None else round(cu, 3),
            "cu_spent_before": cu_spent_before, "cu_before": state.get("cu_before"), "budget_rules_active": budget_active,
            "cu_cap_session": session_cap, "cu_cap_total": cu_cap_total, "cu_warn_total": cu_warn_total,
            "skipped_for_budget": [s for s, v in state["stages"].items() if v.get("status") == "skipped_budget"],
            "invocations": len(state["invocations"]), "code_commit": state["code"]["commit"]}
        known = [v["cu_estimated"] for v in all_sessions.values() if v.get("cu_estimated") is not None]
        write_json(out_dir / "compute.json", {
            "sessions": all_sessions, "cu_estimated_sum": round(sum(known), 3) if known else None,
            "note": "cu_estimated = rate x 벽시계 시간(세션마다). 단계가 도는 동안 60초마다 시각을 적으므로 세션이 단계 도중에 "
                    "죽어도 그때까지의 시간이 들어간다. 정확한 값은 실행 전후의 잔액 차이로 따로 적는다."})

    if not budget_active:
        log(f"budget: CU rate가 {'없다' if cu_rate is None else '0이다'} — 예산 규칙이 꺼져 있다")
    ran_any = False
    for step in session_plan(session):
        beat()
        sid = step["id"]
        st = state["stages"].setdefault(sid, {})
        if st.get("status") == "done" and not (sid == "assemble" and ran_any):
            log(f"step {sid}: 이미 끝남, 건너뜀")
            continue
        if sid == "assemble":
            write_compute()
        elif budget_active:
            mine = session_cu()
            total = cu_spent_before + mine
            log(f"budget: 이 세션 {mine:.2f} CU(상한 {session_cap}), 누적 {total:.2f} CU(경고 {cu_warn_total}, 상한 {cu_cap_total})")
            if mine >= session_cap or total >= cu_cap_total or (total >= cu_warn_total and step["stage"] in DESCRIPTIVE_STAGES):
                st.update(status="skipped_budget", cu_session_at_decision=round(mine, 3), cu_total_at_decision=round(total, 3))
                log(f"step {sid}: 예산 규칙으로 건너뜀(리포트에 미측정으로 남는다)")
                beat()
                continue
        t0 = clock()
        st.update(status="running")
        beat()
        log(f"step {sid}: 시작")
        with m4.Heartbeat(beat, heartbeat_seconds):
            code = runner(stage_command(python, workdir, state["run_args"], threads, step, device), workdir / "repo", env,
                          out_dir / "run.log")
        st["seconds"] = round(clock() - t0, 1)
        if code != 0:
            st.update(status="failed", returncode=code)
            beat()
            reason, what = RUN_NEURAL_EXIT.get(code, ("stage_failed", "실패"))
            write_json(out_dir / "FAILED", {
                "session": session, "step": sid, "returncode": code, "reason": reason,
                "rule": "실행 무효다. 남은 단계를 돌리지 않았다. 원인을 특정해 ADR 0013 A3.12에 '폐기'로 적고, 고친 뒤 새 SHA로 "
                        "E15 전체를 다시 돌린다(통과할 때까지 조용히 다시 돌리지 않는다)."})
            log(f"step {sid}: {what}(returncode {code}) — FAILED 마커를 남겼다")
            write_compute()
            return m4.EXIT_STAGE_FAILED
        st.update(status="done")
        ran_any = True
        beat()
        log(f"step {sid}: 완료 {st['seconds']}s")
    write_compute()
    return m4.EXIT_OK


# --- 하위 명령 ---------------------------------------------------------------------------------

def check_run_args(args) -> None:
    session = SESSIONS[args.session]
    if args.cu_rate is None and not session.get("synthetic"):
        raise DriverError("--cu-rate가 필요합니다. 예산 규칙은 rate가 있어야 적용됩니다: 드라이 런(s0)에서 잰 CU/h를 주거나, "
                          "CU로 과금하지 않는 런타임이면 --cu-rate 0을 명시하세요.", m4.EXIT_USAGE)
    if (args.cu_rate is not None and args.cu_rate < 0) or args.cu_spent_before < 0:
        raise DriverError("--cu-rate와 --cu-spent-before는 0 이상이어야 합니다", m4.EXIT_USAGE)
    if session.get("resumes_from") and not args.resume_bundle and not (Path(args.workdir) / "out" / "progress.json").exists():
        raise DriverError(f"세션 {args.session}은 {session['resumes_from']}의 체크포인트 묶음에서 이어서 돕니다 "
                          "(--resume-bundle, --resume-bundle-sha256).", m4.EXIT_USAGE)


def parse_urls(pairs: list[str]) -> dict[str, str]:
    return m4.parse_urls(pairs, {"M4_URLS_JSON": os.environ.get(URLS_ENV)})


def cmd_detach(args) -> int:
    workdir = Path(args.workdir)
    out_dir = workdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Log(out_dir / "run.log")
    try:
        check_run_args(args)
        prev = read_json(out_dir / "driver.pid")
        if prev and m4._pid_alive(prev["pid"]):
            raise DriverError(f"이미 실행 중입니다(pid {prev['pid']}). status로 보거나 stop으로 끝내세요.", m4.EXIT_USAGE)
        urls = parse_urls(args.url)
        m4.extract_tarball(Path(args.code_tarball), workdir / "repo", args.code_sha256)
        script = workdir / "repo" / "scripts" / "e15_colab_driver.py"
        if not script.exists() or not (script.parent / "m4_colab_driver.py").exists():
            raise DriverError("tarball 안에 scripts/e15_colab_driver.py와 scripts/m4_colab_driver.py가 없습니다", m4.EXIT_USAGE)
        env = dict(os.environ)
        env.pop(ARGS_ENV, None)
        if urls:
            env[URLS_ENV] = json.dumps(urls)        # URL은 자식에게 환경변수로 넘긴다(명령줄에 남기지 않는다)
        with open(out_dir / "driver_stdout.log", "ab") as sink:
            proc = subprocess.Popen([args.python or sys.executable, str(script), *m4._child_argv(args.raw_argv)],
                                    env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                    start_new_session=True, cwd=str(workdir))
        write_json(out_dir / "driver.pid", {"pid": proc.pid, "started": time.time()})
        log(f"detach: pid {proc.pid}로 분리 실행")
        print(json.dumps({"detached": True, "pid": proc.pid}))
        return m4.EXIT_OK
    except DriverError as e:
        log(f"중단: {m4.redact(str(e), [u.split('=', 1)[-1] for u in args.url])}")
        return e.code


def cmd_run(args, *, runner: Callable = m4.subprocess_runner, opener: Callable = urllib.request.urlopen,
            clock: Callable[[], float] = time.time, installer: Optional[Callable] = None,
            torch_probe: Callable[[str], dict] = probe_torch, heartbeat_seconds: float = m4.HEARTBEAT_SECONDS) -> int:
    if getattr(args, "detach", False):
        return cmd_detach(args)
    session = SESSIONS[args.session]
    synthetic = bool(session.get("synthetic"))
    workdir = Path(args.workdir)
    out_dir, repo, data = workdir / "out", workdir / "repo", workdir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Log(out_dir / "run.log")
    state_path = out_dir / "driver_state.json"
    urls: dict[str, str] = {}
    try:
        check_run_args(args)
        urls = parse_urls(args.url)
        if args.fresh:
            for p in (out_dir, data):
                shutil.rmtree(p, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            log("--fresh: 이전 산출물과 데이터를 지웠다")
        if (out_dir / "FAILED").exists():
            raise DriverError("FAILED 마커가 있습니다. 이 실행은 무효입니다(원인을 고친 뒤 --fresh로 처음부터).",
                              m4.EXIT_FAILED_MARKER)
        code = m4.extract_tarball(Path(args.code_tarball), repo, args.code_sha256)
        log(f"code: tarball sha256 {code['sha256'][:12]}…, commit {code['commit']}, session {args.session}")
        run_args = session_run_args(args.session)
        manifest = None if synthetic else read_json(Path(args.manifest)) if args.manifest else None
        if not synthetic and not manifest:
            raise DriverError("--manifest가 필요합니다(s0이 아닐 때)", m4.EXIT_USAGE)
        if manifest:
            m4.check_manifest(manifest, repo, allow_unregistered=args.allow_unregistered_data, prereg_yaml=PREREG_YAML)
        bundle = restore_bundle(Path(args.resume_bundle), args.resume_bundle_sha256, workdir, log) \
            if args.resume_bundle else None
        digest = m4.config_digest(code["sha256"], manifest, {**run_args, "session": args.session})
        state = read_json(state_path)
        if state and state.get("config_digest") != digest:
            raise DriverError("이전 실행과 설정(코드 tarball·manifest·세션·실행 인자)이 다릅니다. 이어서 돌릴 수 없습니다 "
                              "(--fresh로 처음부터).", m4.EXIT_CONFIG_MISMATCH)
        if not state:
            state = {"config_digest": digest, "session": args.session, "judged": session["judged"],
                     "code": {"sha256": code["sha256"], "commit": code["commit"]}, "run_args": run_args,
                     "synthetic": synthetic, "stages": {}, "invocations": [], "host": m4.host_info(),
                     "runtime_label": args.runtime_label or m4.default_runtime_label(), "cu_before": args.cu_before,
                     "resumed_from_bundle": bundle}
        now = clock()
        state["invocations"].append({"started": now, "last_seen": now})
        write_json(state_path, state)

        python = args.python or sys.executable
        if not state.get("installed") and not args.skip_install:
            log(f"install: {REQUIREMENTS}")
            rc = (installer or m4._pip_install)(python, repo / REQUIREMENTS, out_dir / "run.log")
            if rc != 0:
                raise DriverError(f"고정 버전 설치 실패(returncode {rc})", m4.EXIT_INSTALL)
            state["installed"] = True
            write_json(state_path, state)
        state["environment"] = check_environment(args.session, repo, python, torch_probe, args.allow_cpu)
        log(f"environment: torch {state['environment']['torch']} (핀 {state['environment']['torch_pin']}), "
            f"gpu {state['environment'].get('gpu')}")
        if synthetic:
            if not (data / SYNTHETIC["dataset"] / "articles.parquet").exists():
                clean_env = {k: v for k, v in os.environ.items() if k not in (URLS_ENV, "M4_URLS_JSON")}
                rc = runner([python, "-m", "evaluation.recsys.ebnerd.synthetic", "--out", str(data),
                             "--name", SYNTHETIC["dataset"]], repo, clean_env, out_dir / "run.log")
                if rc != 0:
                    raise DriverError(f"합성 데이터 생성 실패(returncode {rc})", m4.EXIT_INSTALL)
            state["data"] = {"synthetic": True}
        else:
            state["data"] = m4.ensure_data(manifest, data, urls, log, opener=opener)
            state["articles"] = manifest.get("articles")
        write_json(state_path, state)
        extra_env = {"E15_CODE_SHA": code["commit"] or f"tarball-{code['sha256'][:16]}"}
        if m4.articles_original(manifest):
            extra_env["E15_ARTICLES_ORIGINAL_SHA256"] = m4.articles_original(manifest)
        if args.prereg_commit:
            extra_env["E15_PREREG_COMMIT"] = args.prereg_commit
        rc = run_steps(workdir, state, state_path, python=python, threads=args.threads,
                       device="cpu" if args.allow_cpu else "auto", cu_rate=args.cu_rate,
                       cu_spent_before=args.cu_spent_before, cu_cap_total=args.cu_cap_total,
                       cu_warn_total=args.cu_warn_total, log=log, runner=runner, clock=clock, extra_env=extra_env,
                       heartbeat_seconds=heartbeat_seconds)
        if rc == m4.EXIT_OK:
            log(f"세션 {args.session} 끝. 다음: bundle로 체크포인트 묶음을 만들어 내려받는다.")
        return rc
    except DriverError as e:
        message = m4.redact(str(e), list(urls.values()))
        log(f"중단: {message}")
        if e.invalidates_run:
            write_json(out_dir / "FAILED", {"session": args.session, "reason": message,
                                            "rule": "실행 무효. 원인을 고친 뒤 처음부터 다시 돌린다."})
        return e.code


def cmd_bundle(args) -> int:
    try:
        info = make_bundle(Path(args.workdir) / "out", Path(args.out))
    except DriverError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return e.code
    print(json.dumps(info, ensure_ascii=False))
    return m4.EXIT_OK


def build_parser():
    ap = m4._Parser(description="EB-NeRD E15 신경망 사용자 모델 비교 드라이버")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="tarball·데이터 준비 → 환경 확인 → 세션의 단계 실행")
    run.add_argument("--workdir", required=True)
    run.add_argument("--code-tarball", required=True)
    run.add_argument("--code-sha256", default=None)
    run.add_argument("--session", required=True, choices=sorted(SESSIONS))
    run.add_argument("--manifest", default=None)
    run.add_argument("--url", action="append", default=[], metavar="KEY=URL", help="manifest 키별 서명 URL(출력하지 않음)")
    run.add_argument("--resume-bundle", default=None, help="앞 세션의 체크포인트 묶음(bundle 하위 명령이 만든 tar.gz)")
    run.add_argument("--resume-bundle-sha256", default=None)
    run.add_argument("--threads", type=int, default=2)
    run.add_argument("--cu-rate", type=float, default=None,
                     help="런타임의 CU/h. s0이 아니면 필수이고, 0은 예산 규칙을 끈다는 명시")
    run.add_argument("--cu-spent-before", type=float, default=0.0, help="앞 세션들이 쓴 CU의 합(전체 상한·경고선 계산용)")
    run.add_argument("--cu-cap-total", type=float, default=CU_CAP_TOTAL)
    run.add_argument("--cu-warn-total", type=float, default=CU_WARN_TOTAL)
    run.add_argument("--cu-before", type=float, default=None, help="실행 전 잔액(기록용)")
    run.add_argument("--runtime-label", default=None, help="기록용 런타임 이름(생략하면 자동 판별)")
    run.add_argument("--prereg-commit", default=None, help="코드에 든 등록 기록(.commit 파일)이 없을 때만 리포트 머리말에 쓰인다")
    run.add_argument("--python", default=None)
    run.add_argument("--skip-install", action="store_true")
    run.add_argument("--fresh", action="store_true", help="이전 산출물·데이터를 지우고 처음부터")
    run.add_argument("--allow-unregistered-data", action="store_true",
                     help="사전 등록과 다른 입력도 받는다(run_neural이 결과를 demo 등급으로 표기한다)")
    run.add_argument("--allow-cpu", action="store_true", help="T4 세션을 GPU 없이 돈다(등록한 런타임이 아님, 기록에 남는다)")
    run.add_argument("--detach", action="store_true", help="새 세션의 자식 프로세스로 띄우고 바로 돌아온다")
    bd = sub.add_parser("bundle", help="체크포인트 묶음(tar.gz)을 만들고 sha256을 출력")
    bd.add_argument("--workdir", required=True)
    bd.add_argument("--out", required=True)
    sp = sub.add_parser("stop", help="분리 실행을 끝낸다(FAILED 마커 없음)")
    sp.add_argument("--workdir", required=True)
    sp.add_argument("--wait", type=float, default=10.0)
    st = sub.add_parser("status", help="단계 상태와 로그 끝부분")
    st.add_argument("--workdir", required=True)
    st.add_argument("--tail", type=int, default=20)
    cl = sub.add_parser("cleanup", help="데이터 삭제 후 남은 파일 목록 출력")
    cl.add_argument("--workdir", required=True)
    cl.add_argument("--all", action="store_true", help="산출물과 코드도 삭제(묶음을 내려받은 뒤에)")
    mf = sub.add_parser("manifest", help="로컬 파일의 sha256으로 manifest 작성")
    mf.add_argument("--root", required=True)
    mf.add_argument("--dataset", default=REGISTERED["dataset"])
    mf.add_argument("--articles-meta", required=True)
    mf.add_argument("--out", required=True)
    return ap


def main(argv=None, **hooks) -> int:
    if argv is None:
        raw = os.environ.get(ARGS_ENV)
        argv = json.loads(raw) if raw else sys.argv[1:]
    args = build_parser().parse_args(argv)
    args.raw_argv = list(argv)
    if args.command == "run":
        return cmd_run(args, **hooks)
    return {"status": m4.cmd_status, "cleanup": m4.cmd_cleanup, "manifest": m4.cmd_manifest, "stop": m4.cmd_stop,
            "bundle": cmd_bundle}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
