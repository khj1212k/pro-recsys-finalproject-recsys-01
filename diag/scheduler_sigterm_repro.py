#!/usr/bin/env python3
"""scheduler-entrypoint SIGTERM 테스트가 143으로 실패하던 경합을 Linux에서 재현하고 분해한다.

진단 전용이다(main에 넣지 않는다). tests/test_scheduler_entrypoint.py와 같은 절차를 반복하되
가짜 supercronic과 엔트리포인트를 판별로 바꿔 가며 종료 코드를 센다.

가짜 supercronic
  old  : b72b63f 시점 - 파이썬 수준 SIGTERM 핸들러를 두고 sys.exit(0)으로 끝난다(실패하던 CI가 돌린 판)
  new  : 69ccfc9 이후 - 같은 핸들러, os._exit(0)으로 끝난다(main에 있는 판)
  held : old와 같은 핸들러와 sys.exit(0)인데, 잡을 기다리지 않고 바로 종료 정리(모듈 해제)에 들어가
         그 안에서 멈춰 있는다 - 그 구간에 신호가 오면 어떻게 되는지를 타이밍과 무관하게 본다

엔트리포인트
  real : docker/scheduler-entrypoint.sh 그대로
  inst : 같은 스크립트에, supercronic에 신호를 보내기 직전 /proc/<pid>/status의 State·SigCgt를
         stderr로 남기는 줄만 끼운 사본(내장 명령만 써서 fork가 없다)
  reordered : supercronic에 신호를 보내는 줄을 /proc 순회 앞으로 옮긴 사본(supercronic 먼저, 잡은 그 다음)

  resched : new와 같되 "종료 신호를 받기 전에 잡이 끝났는지"를 남긴다. 진짜 supercronic이라면 그때 다음 실행을
            띄웠을 상황이다(그 실행은 엔트리포인트의 신호를 받지 못한다).

REPRO_SET=doubles : 가짜 supercronic 판별 비교(첫 라운드)
REPRO_SET=resched : resched 대역으로 지금 순서와 바꾼 순서 비교
REPRO_SET=gap     : 진짜 supercronic에서, 잡 뒤에 뜬 프로세스가 많아 "잡에 신호 -> supercronic에 신호" 사이가 길 때
REPRO_SET=order   : 신호 순서 비교. 가짜 old/new와, REAL_SUPERCRONIC_DIR에 둔 진짜 supercronic(매초 실행 crontab)
"""
import collections
import json
import os
import random
import signal
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(ROOT, "docker", "scheduler-entrypoint.sh")
SIGTERM_BIT = 1 << (signal.SIGTERM - 1)
SMOKE = "--smoke" in sys.argv

# 잡은 테스트와 같다. ready에 "ok" 대신 pid를 적는 것만 다르다(실패한 반복의 뒷정리에 쓴다).
JOB = """
    import os, signal, sys, time
    def on_term(signum, frame):
        open({outcome!r}, "w").write("terminated")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    open({ready!r}, "w").write(f"{{os.getpid()}} {{os.getppid()}}")
    time.sleep(30)
    open({outcome!r}, "w").write("finished without signal")
"""

DOUBLES = {
    "old": """
        import signal, subprocess, sys
        signal.signal(signal.SIGTERM, lambda *a: None)
        child = subprocess.Popen([{job!r}], process_group=0)
        child.wait()
        sys.exit(0)
    """,
    "new": """
        import os, signal, subprocess
        signal.signal(signal.SIGTERM, lambda *a: None)
        child = subprocess.Popen([{job!r}], process_group=0)
        child.wait()
        os._exit(0)
    """,
    "resched": """
        import os, signal, subprocess
        stopping = False
        def on_term(signum, frame):
            global stopping
            stopping = True
        signal.signal(signal.SIGTERM, on_term)
        child = subprocess.Popen([{job!r}], process_group=0)
        child.wait()
        if not stopping:
            open({rescheduled!r}, "w").close()
        os._exit(0)
    """,
    "held": """
        import signal, subprocess, sys, time
        signal.signal(signal.SIGTERM, lambda *a: None)
        child = subprocess.Popen([{job!r}], process_group=0)
        class Hold:
            def __del__(self, _sleep=time.sleep, _open=open):
                _open({window!r}, "w").close()
                _sleep(5)
        keep = Hold()
        sys.exit(0)
    """,
}

KILL_LINE = '  kill "-$sig" "$SUPERCRONIC_PID" 2>/dev/null\n'
PROBE = """\
  if [ -r "/proc/$SUPERCRONIC_PID/status" ]; then
    while read -r diag_key diag_val; do
      case $diag_key in State:|SigCgt:) echo "DIAG $diag_key $diag_val" >&2 ;; esac
    done < "/proc/$SUPERCRONIC_PID/status"
  else
    echo "DIAG gone" >&2
  fi
"""


# 진짜 supercronic 아래에서 도는 잡: 시작과 SIGTERM 수신을 줄 단위로 남긴다(한 번의 정지에 잡이 몇 번 떴는지 센다).
REAL_JOB = """
    import os, signal, sys, time
    def on_term(signum, frame):
        open({terms!r}, "a").write(str(os.getpid()) + chr(10))
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    open({starts!r}, "a").write(str(os.getpid()) + chr(10))
    time.sleep(30)
"""

SIG_LINE = "  sig=$1\n"


def write_executable(path, body):
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n" + textwrap.dedent(body))
    os.chmod(path, 0o755)


def wait_for(path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.01)
    raise TimeoutError(path)


def instrumented_entrypoint(directory):
    with open(ENTRYPOINT) as f:
        source = f.read()
    assert source.count(KILL_LINE) == 1, "엔트리포인트에서 supercronic에 신호를 보내는 줄을 찾지 못함"
    path = os.path.join(directory, "scheduler-entrypoint.inst.sh")
    with open(path, "w") as f:
        f.write(source.replace(KILL_LINE, PROBE + KILL_LINE))
    return path


def reordered_entrypoint(directory):
    with open(ENTRYPOINT) as f:
        source = f.read()
    assert source.count(KILL_LINE) == 1 and source.count(SIG_LINE) == 1, "엔트리포인트 구조가 예상과 다름"
    path = os.path.join(directory, "scheduler-entrypoint.reordered.sh")
    with open(path, "w") as f:
        f.write(source.replace(KILL_LINE, "").replace(SIG_LINE, SIG_LINE + KILL_LINE))
    return path


def session_members(sid):
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
        if int(fields[3]) == sid:
            members.append((int(name), comm, fields[0]))
    return members


def run_once_real(entrypoint, timeout, rng, late_sleepers=0):
    """진짜 supercronic + 매초 crontab. 잡이 뜬 뒤 0~1초 아무 때나 SIGTERM을 보낸다(초 경계와의 위상을 고르게)."""
    with tempfile.TemporaryDirectory(prefix="sigterm-real-") as tmp:
        starts, terms = os.path.join(tmp, "starts"), os.path.join(tmp, "terms")
        job = os.path.join(tmp, "job.py")
        write_executable(job, REAL_JOB.format(starts=starts, terms=terms))
        crontab = os.path.join(tmp, "crontab")
        with open(crontab, "w") as f:
            f.write(f"* * * * * * * {job}\n")
        env = {**os.environ, "PATH": f"{os.environ['REAL_SUPERCRONIC_DIR']}{os.pathsep}{os.environ.get('PATH', '')}"}
        err_path = os.path.join(tmp, "stderr")
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(["sh", entrypoint, crontab], env=env, stderr=err, start_new_session=True)
        rc, elapsed, leftover, late = "timeout", None, [], []
        try:
            wait_for(starts, 10.0)
            # 잡보다 나중에 뜬 프로세스는 pid가 커서 /proc 순회에서 잡 뒤에 온다: 잡에 신호가 간 뒤
            # supercronic에 신호가 가기까지 이만큼을 더 훑어야 한다.
            late = [subprocess.Popen(["sleep", "3600"]) for _ in range(late_sleepers)]
            time.sleep(rng.random())
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=timeout)
            elapsed = time.monotonic() - started
        except (TimeoutError, subprocess.TimeoutExpired):
            pass
        finally:
            for p in late:
                p.kill()
            for p in late:
                p.wait()
            leftover = [m for m in session_members(proc.pid) if m[2] != "Z"]
            for pid, _, _ in session_members(proc.pid):
                kill_quietly(pid)
            proc.wait()

        def count(path):
            return len(open(path).read().split()) if os.path.exists(path) else 0

        with open(err_path, errors="replace") as f:
            stderr = f.read()
        return {
            "rc": rc,
            "elapsed": elapsed,
            "terminated_line": "Terminated" in stderr.splitlines(),
            "other_stderr": [],
            "outcome": f"jobs started={count(starts)} got SIGTERM={count(terms)}",
            "state": None,
            "sigterm_handler": None,
            "leftover": sorted(comm for _, comm, _ in leftover),
            "log_tail": stderr.splitlines()[-8:],
        }


def kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_once(double, entrypoint, timeout):
    with tempfile.TemporaryDirectory(prefix="sigterm-repro-") as tmp:
        ready, outcome, window, rescheduled = (
            os.path.join(tmp, name) for name in ("ready", "outcome", "window", "rescheduled")
        )
        job = os.path.join(tmp, "job.py")
        write_executable(job, JOB.format(ready=ready, outcome=outcome))
        bin_dir = os.path.join(tmp, "bin")
        os.mkdir(bin_dir)
        write_executable(os.path.join(bin_dir, "supercronic"), DOUBLES[double].format(job=job, window=window, rescheduled=rescheduled))
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        err_path = os.path.join(tmp, "stderr")
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(["sh", entrypoint, "/dev/null"], env=env, stderr=err)
        rc, elapsed = "timeout", None
        try:
            wait_for(ready, timeout)
            if double == "held":
                wait_for(window, timeout)
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=timeout)
            elapsed = time.monotonic() - started
        except (TimeoutError, subprocess.TimeoutExpired):
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if os.path.exists(ready):
                for pid in open(ready).read().split():
                    kill_quietly(int(pid))
        with open(err_path, errors="replace") as f:
            stderr = f.read()
        outcome_text = open(outcome).read() if os.path.exists(outcome) else None
        if os.path.exists(rescheduled):
            outcome_text = f"{outcome_text} + job ended before supercronic got SIGTERM"
    state = handler = None
    for line in stderr.splitlines():
        parts = line.split()
        if parts[:2] == ["DIAG", "State:"]:
            state = parts[2]
        elif parts[:2] == ["DIAG", "SigCgt:"]:
            handler = bool(int(parts[2], 16) & SIGTERM_BIT)
        elif parts[:2] == ["DIAG", "gone"]:
            state = "gone"
    other = [line for line in stderr.splitlines() if not line.startswith("DIAG ") and line != "Terminated"]
    return {
        "rc": rc,
        "elapsed": elapsed,
        "terminated_line": "Terminated" in stderr.splitlines(),
        "other_stderr": other,
        "outcome": outcome_text,
        "state": state,
        "sigterm_handler": handler,
    }


def run_arm(name, double, entrypoint, n, sleepers=0, cpu_load=False, late_sleepers=0, budget=420.0):
    if SMOKE:  # 로컬 문법 점검용: 부하를 만들지 않는다
        sleepers, cpu_load = min(sleepers, 2), False
    extras = [subprocess.Popen(["sleep", "3600"]) for _ in range(sleepers)]
    if cpu_load:
        extras += [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(os.cpu_count() or 2)]
    timeout = 2.0 if SMOKE else 10.0
    rng = random.Random(20261006)
    results = []
    began = time.monotonic()
    try:
        for _ in range(n):
            if time.monotonic() - began > budget:
                break
            if double == "real":
                results.append(run_once_real(entrypoint, min(timeout, 6.0), rng, late_sleepers))
            else:
                results.append(run_once(double, entrypoint, timeout))
    finally:
        for p in extras:
            p.kill()
        for p in extras:
            p.wait()
    rcs = collections.Counter(str(r["rc"]) for r in results)
    elapsed = [r["elapsed"] for r in results if r["elapsed"] is not None]
    summary = {
        "arm": name,
        "double": double,
        "entrypoint": os.path.basename(entrypoint),
        "sleepers": sleepers,
        "cpu_load": cpu_load,
        "n": len(results),
        "rc": dict(rcs),
        "rc_143": rcs.get("143", 0),
        "rc_143_with_terminated_line": sum(1 for r in results if r["rc"] == 143 and r["terminated_line"]),
        "terminated_line_without_143": sum(1 for r in results if r["rc"] != 143 and r["terminated_line"]),
        "outcome": dict(collections.Counter(str(r["outcome"]) for r in results)),
        "other_stderr_runs": sum(1 for r in results if r["other_stderr"]),
        "other_stderr_sample": next((r["other_stderr"][:3] for r in results if r["other_stderr"]), None),
        "elapsed_median_ms": round(statistics.median(elapsed) * 1000, 1) if elapsed else None,
        "elapsed_max_ms": round(max(elapsed) * 1000, 1) if elapsed else None,
        "wall_s": round(time.monotonic() - began, 1),
    }
    if double == "real":
        summary["leftover_at_exit_or_timeout"] = dict(collections.Counter(str(r["leftover"]) for r in results))
        summary["log_tail_ok"] = next((r["log_tail"] for r in results if r["rc"] == 0), None)
        summary["log_tail_not_ok"] = next((r["log_tail"] for r in results if r["rc"] != 0), None)
    elif os.path.basename(entrypoint).endswith(".inst.sh"):
        crosstab = collections.Counter(
            f"state={r['state']} sigterm_handler={r['sigterm_handler']} -> rc={r['rc']}" for r in results
        )
        summary["at_kill_time"] = dict(sorted(crosstab.items()))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def sh_capture(script, signal_after=None):
    proc = subprocess.Popen(["sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if signal_after is not None:
        time.sleep(signal_after)
        proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    return f"{out.rstrip()}\n[shell exit={proc.returncode}]"


def shell_semantics():
    print("== sh 의미 확인 ==", flush=True)
    print("-- 백그라운드 자식이 SIGTERM으로 죽었을 때 wait")
    print(sh_capture('sleep 30 &\npid=$!\nkill -TERM "$pid"\nwait "$pid"\necho "wait status=$?"'))
    print("-- wait 중 트랩이 돌고, 그 사이 자식이 7로 끝났을 때 두 번째 wait")
    loop = textwrap.dedent("""
        (sleep 0.3; exit 7) &
        pid=$!
        trap 'interrupted=1; sleep 1' TERM
        n=0
        while :; do
          interrupted=0
          wait "$pid"
          status=$?
          n=$((n+1))
          echo "wait#$n status=$status interrupted=$interrupted"
          [ "$interrupted" = 1 ] || break
        done
        exit "$status"
    """)
    print(sh_capture(loop, signal_after=0.1), flush=True)


def environment():
    def run(cmd):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception as exc:  # 진단 출력일 뿐이라 실패해도 계속 간다
            return f"<{exc}>"

    info = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "sh": run("readlink -f \"$(command -v sh)\""),
        "dash": run("dpkg-query -W -f='${Version}' dash 2>/dev/null"),
        "kernel": run("uname -sr"),
        "cpus": os.cpu_count(),
        "proc_pids": len([d for d in os.listdir("/proc") if d.isdigit()]) if os.path.isdir("/proc") else None,
    }
    print("== 환경 ==")
    print(json.dumps(info, ensure_ascii=False), flush=True)


def main():
    environment()
    shell_semantics()
    scale = 0 if SMOKE else float(os.environ.get("REPRO_SCALE", "1"))

    def n(count):
        return 1 if SMOKE else max(1, int(count * scale))

    which = os.environ.get("REPRO_SET", "order")
    with tempfile.TemporaryDirectory(prefix="sigterm-inst-") as tmp:
        inst = instrumented_entrypoint(tmp)
        reordered = reordered_entrypoint(tmp)
        for path in (inst, reordered):
            subprocess.run(["sh", "-n", path], check=True)
        print(f"== 반복 실행 ({which}) ==", flush=True)
        if which == "gap":
            arms = [
                ("supercronic/real, 1000 procs after job", "real", ENTRYPOINT, n(40), {"late_sleepers": 1000}),
                ("supercronic/reordered, 1000 procs after job", "real", reordered, n(40), {"late_sleepers": 1000}),
            ]
        elif which == "resched":
            arms = [
                (f"resched/{label}{suffix}", "resched", entry, n(count), kw)
                for suffix, count, kw in (
                    ("", 300, {}),
                    (" +300 procs", 200, {"sleepers": 300}),
                    (" +1000 procs", 150, {"sleepers": 1000}),
                    (" cpu load", 200, {"cpu_load": True}),
                )
                for label, entry in (("real", ENTRYPOINT), ("reordered", reordered))
            ]
        elif which == "doubles":
            arms = [
                # 고정 재현: 종료 정리 구간에 들어가 있는 old에 신호가 온다
                ("held/inst", "held", inst, n(30), {}),
                # old: 실패하던 판의 자연 발생률과, 신호를 보내는 순간의 상태
                ("old/real", "old", ENTRYPOINT, n(300), {}),
                ("old/inst", "old", inst, n(300), {}),
                ("old/real +300 procs", "old", ENTRYPOINT, n(200), {"sleepers": 300}),
                ("old/inst +300 procs", "old", inst, n(200), {"sleepers": 300}),
                ("old/real +1000 procs", "old", ENTRYPOINT, n(150), {"sleepers": 1000}),
                ("old/real cpu load", "old", ENTRYPOINT, n(200), {"cpu_load": True}),
                # new: main에 있는 판
                ("new/real", "new", ENTRYPOINT, n(500), {}),
                ("new/inst", "new", inst, n(300), {}),
                ("new/real +300 procs", "new", ENTRYPOINT, n(300), {"sleepers": 300}),
                ("new/inst +300 procs", "new", inst, n(200), {"sleepers": 300}),
                ("new/real +1000 procs", "new", ENTRYPOINT, n(200), {"sleepers": 1000}),
                ("new/real cpu load", "new", ENTRYPOINT, n(300), {"cpu_load": True}),
            ]
        else:
            arms = [
                # 첫 라운드에서 old가 실패하던 조건에서, 신호를 보내는 순간의 supercronic 상태
                ("old/inst +1000 procs", "old", inst, n(150), {"sleepers": 1000}),
                ("old/inst cpu load", "old", inst, n(200), {"cpu_load": True}),
                # 같은 조건에서 신호 순서만 바꾼다(supercronic 먼저)
                ("old/reordered", "old", reordered, n(300), {}),
                ("old/reordered +300 procs", "old", reordered, n(200), {"sleepers": 300}),
                ("old/reordered +1000 procs", "old", reordered, n(150), {"sleepers": 1000}),
                ("old/reordered cpu load", "old", reordered, n(200), {"cpu_load": True}),
                ("new/reordered", "new", reordered, n(300), {}),
                ("new/reordered +1000 procs", "new", reordered, n(150), {"sleepers": 1000}),
                ("new/reordered cpu load", "new", reordered, n(200), {"cpu_load": True}),
            ]
            if os.environ.get("REAL_SUPERCRONIC_DIR"):
                arms = arms + [
                    # 진짜 supercronic: 지금 순서(잡 먼저)와 바꾼 순서. 프로세스가 많을수록 두 신호 사이가 벌어진다.
                    ("supercronic/real", "real", ENTRYPOINT, n(50), {}),
                    ("supercronic/reordered", "real", reordered, n(50), {}),
                    ("supercronic/real +2000 procs", "real", ENTRYPOINT, n(50), {"sleepers": 2000}),
                    ("supercronic/reordered +2000 procs", "real", reordered, n(50), {"sleepers": 2000}),
                ]
                arms = arms[-4:] + arms[:-4]
        summaries = [run_arm(name, double, entry, count, **kw) for name, double, entry, count, kw in arms]

    lines = [
        "| arm | n | rc=0 | rc=143 | 143 중 'Terminated' 줄 | 다른 rc | 신호~종료 중앙값(ms) |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        other = {k: v for k, v in s["rc"].items() if k not in ("0", "143")}
        lines.append(
            f"| {s['arm']} | {s['n']} | {s['rc'].get('0', 0)} | {s['rc_143']} | "
            f"{s['rc_143_with_terminated_line']} | {other or '-'} | {s['elapsed_median_ms']} |"
        )
    table = "\n".join(lines)
    print("== 요약 ==")
    print(table, flush=True)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(table + "\n")


if __name__ == "__main__":
    main()
