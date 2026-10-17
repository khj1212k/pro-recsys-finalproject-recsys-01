#!/usr/bin/env python3
"""동결 반출본의 기사 임베딩을 원격 GPU 런타임(Colab T4)에서 돌리는 드라이버 — 표준 라이브러리만 쓴다 (ADR 0036, ADR 0023 개정).

같은 디렉터리의 m4_colab_driver.py의 도구를 그대로 쓴다: tarball sha256 확인과 안전한 풀기, 서명 URL 내려받기와 URL 비노출,
heartbeat, 분리 실행·stop. 임베딩 자체는 코드 tarball 안의 `evaluation.llm.embed_job`이 운영 임베더
(`core.embedder.NewsEmbedder`)로 한다. 이 파일은 그 잡을 서브프로세스로 띄우고 지킨다.

    # 드라이 런(합성 텍스트, 기사 본문 없음): 처리량·결정론·핀을 잰다
    python colab_embed_articles.py run --workdir /content/e0emb --code-tarball /content/code.tar.gz \
        --code-sha256 <sha256> --synthetic 300 --cu-rate 1.07 --cu-cap 0.3
    # 본 실행: 반출본(manifest.json + articles.jsonl)을 `colab upload`로 <workdir>/input에 직접 올린 뒤
    python colab_embed_articles.py run --workdir /content/e0emb --code-tarball /content/code.tar.gz \
        --code-sha256 <sha256> --export-identity <identity_sha256> --cu-rate <CU/h> --cu-cap 4 [--detach]
    python colab_embed_articles.py status|stop|cleanup --workdir /content/e0emb
    python colab_embed_articles.py bundle --workdir /content/e0emb --out /content/e0emb_parts.tar.gz   # 세션 유실 대비
    python colab_embed_articles.py run ... --resume-bundle /content/e0emb_parts.tar.gz --resume-bundle-sha256 <sha256>

규칙:
- **기사 본문은 세션에 직접 올린 파일로만 받는다.** GCS·Drive를 거치지 않는다(ADR 0023 개정 "그 밖의 곳에는 두지 않는다").
  서명 URL(`--url`)은 본문이 없는 것 하나 — 고정 리비전의 모델 스냅숏 tarball(`model_snapshot`) — 에만 받고, URL은 로그·상태
  파일·오류 메시지 어디에도 쓰지 않는다(m4 드라이버와 같은 규칙).
- 입력 반출본의 `identity_sha256`이 `--export-identity`와 다르거나 본문 파일의 sha256이 매니페스트와 다르면 아무것도
  돌리지 않는다. 잡은 동결 반출 도구로 행마다 다시 대조하고, 만료된 본문이 하나라도 있으면 멈춘다.
- **잡이 끝나면(완료·예산 중단·실패·입력 거절 모두) 같은 프로세스 안에서 입력 본문 파일을 지우고**, 남은 파일 목록(이름·
  크기만)을 run.log와 out/cleanup.json에 남긴다. 세션은 그 뒤 사람이 `colab stop`으로 끝낸다. 프로세스가 죽어 이 단계가
  돌지 못하면(세션 유실) VM 자체가 사라진다 — 살아 있는 세션에 남았으면 `cleanup`으로 지운다.
- 단계를 시작하기 전과 잡이 도는 동안(heartbeat마다) `앞 세션들의 CU + rate x 벽시계 시간`을 본다. 상한 이상이면
  STOP_BUDGET 파일을 만들어 잡이 다음 파트를 시작하지 않게 한다(이미 쓴 파트는 남고, 이어서 돌 수 있다).
- 설치 뒤 torch가 핀(requirements-colab-embed.txt, 미리 깔린 것을 확인만 한다)과 같은지, CUDA를 쓸 수 있는지, 고정한 임베딩
  패키지 버전이 핀과 같은지 본다. 다르면 아무것도 돌리지 않는다(무효 처리 아님).
- 세션이 끊기면 같은 명령으로 다시 실행한다(입력·코드·인자가 같을 때만 이어서 돈다). 새 세션이면 bundle로 내려받은 파트
  묶음을 --resume-bundle로 준다.
- argv를 넘길 수 없는 실행기에서는 환경변수 E0EMB_DRIVER_ARGS_JSON(인자 목록의 JSON)으로 같은 인자를 준다.
"""
from __future__ import annotations

import hashlib
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def _load_m4():
    path = Path(__file__).resolve().parent / "m4_colab_driver.py"
    if not path.exists():
        raise SystemExit("m4_colab_driver.py가 이 파일과 같은 디렉터리에 있어야 합니다(코드 tarball의 scripts/를 통째로 쓰세요).")
    spec = importlib.util.spec_from_file_location("m4_colab_driver", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("m4_colab_driver", mod)
    spec.loader.exec_module(mod)
    return mod


m4 = _load_m4()
DriverError, Log, write_json, read_json = m4.DriverError, m4.Log, m4.write_json, m4.read_json

MODULE = "evaluation.llm.embed_job"
REQUIREMENTS = "requirements-colab-embed.txt"
# 이 파일은 의존성 없이 돌아야 해서 잡 모듈의 값을 여기 적고, 같은지는 테스트(tests/evaluation/test_colab_embed_driver.py)가 본다.
STOP_FILE, PROGRESS_FILE, JOB_FILE, PACK_DIR, PARTS_DIR = "STOP_BUDGET", "progress.json", "job.json", "pack", "parts"
JOB_EXIT_BUDGET, JOB_EXIT_INPUT, JOB_EXIT_ENVIRONMENT = 10, 2, 8
CU_CAP_DEFAULT = 4.0          # 실행 계획 5절 결정 7: 본 실행 상한 4 CU(드라이 런은 --cu-cap 0.3)
SYNTHETIC_MAX = 2000
URL_KEYS = ("model_snapshot",)  # 서명 URL로 받아도 되는 것(본문이 없는 것)뿐
EXIT_INPUT_REFUSED, EXIT_ENVIRONMENT, EXIT_BUDGET = 9, 8, 10
URLS_ENV, ARGS_ENV = "E0EMB_URLS_JSON", "E0EMB_DRIVER_ARGS_JSON"
INPUT_FILES = ("manifest.json", "articles.jsonl")


# --- 입력 확인(표준 라이브러리만) ----------------------------------------------------------------

def canonical_json(obj) -> str:
    """evaluation.llm.frozen_export.canonical_json과 같은 직렬화(테스트가 묶는다)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def check_input(input_dir: Path, export_identity: str) -> dict:
    """올린 반출본이 맞는 것인지 본다: 매니페스트의 신원 해시, 기대한 신원, 본문 파일의 sha256. 행 단위 대조는 잡이 한다."""
    manifest_path, articles_path = (input_dir / name for name in INPUT_FILES)
    if not manifest_path.is_file() or not articles_path.is_file():
        raise DriverError(f"{input_dir}에 {'와 '.join(INPUT_FILES)}이 없습니다(colab upload로 직접 올립니다)", EXIT_INPUT_REFUSED)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        identity = manifest["identity"]
        recorded = manifest["identity_sha256"]
        entry = manifest["files"]["articles.jsonl"]
    except (OSError, ValueError, KeyError, TypeError):
        raise DriverError("반출 매니페스트를 읽을 수 없습니다", EXIT_INPUT_REFUSED) from None
    if hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest() != recorded:
        raise DriverError("반출 매니페스트의 identity가 identity_sha256과 다릅니다", EXIT_INPUT_REFUSED)
    if recorded != (export_identity or "").lower():
        raise DriverError("올린 반출본의 identity_sha256이 --export-identity와 다릅니다(다른 반출본)", EXIT_INPUT_REFUSED)
    if m4.sha256_file(articles_path) != entry.get("sha256"):
        raise DriverError("본문 파일의 sha256이 매니페스트와 다릅니다(손상되었거나 다른 파일)", EXIT_INPUT_REFUSED)
    return {"export_identity_sha256": recorded, "rows": identity.get("rows"),
            "articles_bytes": articles_path.stat().st_size,
            "first_body_expiry_utc": (identity.get("retention") or {}).get("first_body_expiry_utc")}


def listing(root: Path, *, skip: tuple = ("repo",)) -> list:
    """파일 이름·크기만(내용은 보지 않는다)."""
    out = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in skip:
            continue
        if p.is_file():
            out.append({"name": rel.as_posix(), "bytes": p.stat().st_size})
    return out


def delete_inputs(workdir: Path, input_dir: Path, log: Log, out_dir: Path, *, reason: str) -> dict:
    """입력 본문 파일을 지우고(디렉터리째) 남은 파일 목록을 남긴다. ADR 0023 개정: 잡이 끝나는 즉시, 같은 세션 안에서."""
    removed = []
    if input_dir.exists():
        for p in sorted(input_dir.rglob("*")):
            if p.is_file():
                removed.append({"name": p.relative_to(input_dir).as_posix(), "bytes": p.stat().st_size})
        shutil.rmtree(input_dir, ignore_errors=True)
    left = listing(workdir)
    record = {"at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason,
              "input_dir_exists_after": input_dir.exists(), "removed": removed, "remaining": left}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "cleanup.json", record)
    log(f"cleanup: 입력 파일 {len(removed)}개 삭제({reason}), 입력 디렉터리 남음={input_dir.exists()}, "
        f"남은 파일 {len(left)}개: " + ", ".join(f"{x['name']}({x['bytes']})" for x in left[:40]))
    return record


# --- 환경 확인 ---------------------------------------------------------------------------------

def read_pins(repo: Path) -> dict:
    text = (repo / REQUIREMENTS).read_text(encoding="utf-8") if (repo / REQUIREMENTS).exists() else ""
    pins = dict(re.findall(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s#;]*)", text, flags=re.MULTILINE))
    if "torch" not in pins or "flagembedding" not in {k.lower() for k in pins}:
        raise DriverError(f"{REQUIREMENTS}에 torch·FlagEmbedding 핀이 없습니다", m4.EXIT_USAGE)
    return pins


def probe_environment(python: str, names: list) -> dict:
    code = ("import json, importlib.metadata as md\n"
            "out = {}\n"
            f"for n in {names!r}:\n"
            "    try: out[n] = md.version(n)\n"
            "    except md.PackageNotFoundError: out[n] = None\n"
            "try:\n"
            "    import torch\n"
            "    c = torch.cuda.is_available()\n"
            "    out['_cuda_available'] = c\n"
            "    out['_gpu'] = torch.cuda.get_device_name(0) if c else None\n"
            "    out['_cuda'] = torch.version.cuda\n"
            "except Exception as e:\n"
            "    out['_torch_error'] = type(e).__name__\n"
            "import sys; out['_python'] = sys.version.split()[0]\n"
            "print(json.dumps(out))\n")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"_error": type(e).__name__}
    if out.returncode != 0:
        return {"_error": "probe_failed"}
    return json.loads(out.stdout.strip().splitlines()[-1])


def check_environment(repo: Path, python: str, probe: Callable[[str, list], dict], allow_cpu: bool) -> dict:
    """핀과 다른 패키지가 하나라도 있거나(로컬 표지 +cu128 등은 무시), CUDA를 쓸 수 없으면 멈춘다."""
    pins = read_pins(repo)
    info = probe(python, sorted(pins))
    if info.get("_error") or info.get("_torch_error"):
        raise DriverError(f"환경 확인 실패: {info.get('_error') or info.get('_torch_error')}", EXIT_ENVIRONMENT)
    wrong = {n: {"pin": v, "installed": info.get(n)} for n, v in pins.items()
             if str(info.get(n) or "").split("+", 1)[0] != v}
    if wrong:
        raise DriverError(f"핀과 다른 패키지: {json.dumps(wrong)}", EXIT_ENVIRONMENT)
    if not info.get("_cuda_available") and not allow_cpu:
        raise DriverError("CUDA를 쓸 수 없습니다(T4 런타임에서 돌립니다. --allow-cpu는 기록에 남는다)", EXIT_ENVIRONMENT)
    return {"pins": pins, "installed": {n: info.get(n) for n in pins}, "cuda_available": info.get("_cuda_available"),
            "gpu": info.get("_gpu"), "cuda": info.get("_cuda"), "python": info.get("_python")}


def runtime_label(environment: dict) -> str:
    """기록용 런타임 이름. Colab VM(/content가 있는 Linux)이 아니면 local로 적는다."""
    base = m4.default_runtime_label()
    if not base.startswith("colab"):
        return base
    gpu = environment.get("gpu")
    return f"colab-{gpu.split()[-1].lower()}" if gpu else "colab-cpu"


# --- 모델 스냅숏(선택, 서명 URL) ----------------------------------------------------------------

def ensure_model_snapshot(urls: dict, expected_sha256: Optional[str], workdir: Path, log: Log,
                          opener: Callable = urllib.request.urlopen) -> Optional[dict]:
    """고정 리비전의 Hugging Face 캐시 묶음(tar)을 서명 URL로 받아 HF_HUB_CACHE로 푼다. URL은 찍지 않는다."""
    if "model_snapshot" not in urls:
        return None
    if not expected_sha256:
        raise DriverError("--url model_snapshot=...에는 --model-snapshot-sha256이 필요합니다", m4.EXIT_USAGE)
    dest = workdir / "model_snapshot.tar"
    if not dest.exists() or m4.sha256_file(dest) != expected_sha256.lower():
        try:
            got, n = m4.fetch(urls["model_snapshot"], dest, opener=opener)
        except DriverError as e:
            raise DriverError(m4.redact(str(e), list(urls.values())), e.code) from None
        if got != expected_sha256.lower():
            dest.unlink(missing_ok=True)
            raise DriverError("모델 스냅숏의 sha256이 다릅니다(받은 파일을 지웠습니다)", m4.EXIT_SHA_MISMATCH)
        log(f"model_snapshot: {n} bytes, sha256 일치")
    info = m4.extract_tarball(dest, workdir / "hf_cache")
    return {"sha256": info["sha256"], "hf_cache": str(workdir / "hf_cache")}


# --- 파트 묶음 ---------------------------------------------------------------------------------

def make_bundle(out_dir: Path, dest: Path) -> dict:
    if (out_dir / "FAILED").exists():
        raise DriverError("FAILED 마커가 있는 실행은 묶지 않습니다", m4.EXIT_FAILED_MARKER)
    if not (out_dir / PROGRESS_FILE).exists():
        raise DriverError("묶을 파트가 없습니다(progress.json 없음)", m4.EXIT_USAGE)
    files = [out_dir / PROGRESS_FILE] + sorted(p for p in (out_dir / PARTS_DIR).glob("part_*.npy"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        for p in files:
            tf.add(p, arcname=p.relative_to(out_dir).as_posix())
    return {"out": str(dest), "files": len(files), "bytes": dest.stat().st_size, "sha256": m4.sha256_file(dest)}


def restore_bundle(bundle: Path, expected_sha256: Optional[str], workdir: Path, log: Log) -> Optional[dict]:
    out_dir = workdir / "out"
    if (out_dir / PROGRESS_FILE).exists():
        log("bundle: out/에 진행 기록이 이미 있다 — 묶음을 다시 풀지 않는다")
        return None
    if not expected_sha256 or not bundle.exists() or m4.sha256_file(bundle) != expected_sha256.lower():
        raise DriverError("파트 묶음이 없거나 sha256이 --resume-bundle-sha256과 다릅니다(아무것도 풀지 않았습니다)",
                          m4.EXIT_SHA_MISMATCH)
    tmp = workdir / "bundle_tmp"
    info = m4.extract_tarball(bundle, tmp)     # 경로 탈출·링크 거부
    n = 0
    for p in sorted(tmp.rglob("*")):
        rel = p.relative_to(tmp)
        if p.is_file() and rel.name != ".tarball_sha256" and (rel.parts[0] == PARTS_DIR or rel.name == PROGRESS_FILE):
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out_dir / rel)
            n += 1
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"bundle: sha256 {info['sha256'][:12]}… 일치, 파일 {n}개를 out/에 풀었다")
    return {"sha256": info["sha256"], "files": n}


# --- 잡 실행 -----------------------------------------------------------------------------------

def job_command(python: str, input_dir: Optional[Path], out_dir: Path, synthetic: Optional[int]) -> list:
    cmd = [python, "-m", MODULE, "--out-dir", str(out_dir)]
    return cmd + (["--synthetic", str(synthetic)] if synthetic else ["--input-dir", str(input_dir)])


def run_job(workdir: Path, state: dict, state_path: Path, *, python: str, input_dir: Optional[Path],
            synthetic: Optional[int], cu_rate: Optional[float], cu_cap: float, cu_spent_before: float, log: Log,
            runner: Callable = m4.subprocess_runner, clock: Callable[[], float] = time.time,
            extra_env: Optional[dict] = None, heartbeat_seconds: float = m4.HEARTBEAT_SECONDS) -> int:
    out_dir, repo = workdir / "out", workdir / "repo"
    env = dict(os.environ)
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONPATH": os.pathsep.join([str(repo / "ai_workspace"), str(repo)]),
                "TOKENIZERS_PARALLELISM": "false", "HF_HUB_DISABLE_PROGRESS_BARS": "1"})
    env.update(extra_env or {})
    for k in (URLS_ENV, ARGS_ENV, "M4_URLS_JSON"):     # 잡에는 URL을 넘기지 않는다
        env.pop(k, None)
    invocation = state["invocations"][-1]
    budget_active = cu_rate is not None and cu_rate > 0

    def cu_now() -> float:
        return cu_spent_before + (cu_rate or 0.0) * m4.wall_seconds(state) / 3600.0

    def beat():
        invocation["last_seen"] = clock()
        write_json(state_path, state)
        if budget_active and cu_now() >= cu_cap and not (out_dir / STOP_FILE).exists():
            write_json(out_dir / STOP_FILE, {"cu": round(cu_now(), 3), "cap": cu_cap})
            log(f"budget: {cu_now():.3f} CU ≥ 상한 {cu_cap} — 잡에 멈춤을 요청했다(다음 파트를 시작하지 않는다)")

    def write_compute(outcome: str):
        write_json(out_dir / "compute.json", {
            **state.get("host", {}), "runtime": state.get("runtime_label"), "environment": state.get("environment"),
            "wall_seconds_total": round(m4.wall_seconds(state), 1), "rate_cu_per_hour": cu_rate,
            "cu_spent_before": cu_spent_before, "cu_estimated_total": round(cu_now(), 4) if budget_active else None,
            "cu_cap": cu_cap, "budget_rules_active": budget_active, "outcome": outcome,
            "invocations": len(state["invocations"]), "code_commit": state["code"]["commit"],
            "note": "cu_estimated = 앞 세션들의 CU + rate x 벽시계 시간(heartbeat 간격 오차). 정확한 값은 실행 전후 잔액 차이로 따로 적는다."})

    (out_dir / STOP_FILE).unlink(missing_ok=True)   # 이어서 도는 실행은 새로 판단한다
    if not budget_active:
        log(f"budget: CU rate가 {'없다' if cu_rate is None else '0이다'} — 예산 규칙이 꺼져 있다")
    elif cu_now() >= cu_cap:
        log(f"budget: 시작 전 {cu_now():.3f} CU ≥ 상한 {cu_cap} — 잡을 시작하지 않는다")
        write_compute("budget_stopped")
        return EXIT_BUDGET
    beat()
    t0 = clock()
    with m4.Heartbeat(beat, heartbeat_seconds):
        code = runner(job_command(python, input_dir, out_dir, synthetic), repo, env, out_dir / "run.log")
    state["job_seconds"] = round(state.get("job_seconds", 0.0) + clock() - t0, 1)
    beat()
    if code == 0:
        write_compute("done")
        return m4.EXIT_OK
    if code == JOB_EXIT_BUDGET:
        write_compute("budget_stopped")
        log("job: CU 상한으로 멈췄다(쓴 파트는 남아 있다. 상한을 올려 이어 돌리려면 사람이 결정한다)")
        return EXIT_BUDGET
    write_compute("failed")
    reason = {JOB_EXIT_INPUT: "input_refused", JOB_EXIT_ENVIRONMENT: "environment"}.get(code, "job_failed")
    if code != JOB_EXIT_ENVIRONMENT:
        write_json(out_dir / "FAILED", {"returncode": code, "reason": reason,
                                        "rule": "잡 실패. 원인을 고친 뒤 --fresh로 처음부터 다시 돌린다."})
    log(f"job: 실패(returncode {code}, {reason})")
    return m4.EXIT_STAGE_FAILED if code != JOB_EXIT_ENVIRONMENT else EXIT_ENVIRONMENT


# --- 하위 명령 ---------------------------------------------------------------------------------

def parse_urls(pairs: list) -> dict:
    urls = m4.parse_urls(pairs, {"M4_URLS_JSON": os.environ.get(URLS_ENV)})
    bad = sorted(k for k in urls if k not in URL_KEYS)
    if bad:
        raise DriverError(f"서명 URL로 받을 수 있는 것은 {list(URL_KEYS)}뿐입니다(본문은 세션에 직접 올립니다): {bad}",
                          m4.EXIT_USAGE)
    return urls


def check_run_args(args) -> None:
    if args.synthetic is not None:
        if not 1 <= args.synthetic <= SYNTHETIC_MAX:
            raise DriverError(f"--synthetic은 1~{SYNTHETIC_MAX}건입니다", m4.EXIT_USAGE)
    elif not args.export_identity:
        raise DriverError("--export-identity(로컬 반출 매니페스트의 identity_sha256)가 필요합니다", m4.EXIT_USAGE)
    if args.cu_rate is None and args.synthetic is None:
        raise DriverError("--cu-rate가 필요합니다(드라이 런에서 잰 CU/h. CU로 과금하지 않는 런타임이면 0을 명시)", m4.EXIT_USAGE)
    if (args.cu_rate is not None and args.cu_rate < 0) or args.cu_spent_before < 0 or args.cu_cap <= 0:
        raise DriverError("--cu-rate·--cu-spent-before는 0 이상, --cu-cap은 0보다 커야 합니다", m4.EXIT_USAGE)


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
        script = workdir / "repo" / "scripts" / "colab_embed_articles.py"
        if not script.exists() or not (script.parent / "m4_colab_driver.py").exists():
            raise DriverError("tarball 안에 scripts/colab_embed_articles.py와 scripts/m4_colab_driver.py가 없습니다", m4.EXIT_USAGE)
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
            env_probe: Callable[[str, list], dict] = probe_environment,
            heartbeat_seconds: float = m4.HEARTBEAT_SECONDS) -> int:
    if getattr(args, "detach", False):
        return cmd_detach(args)
    workdir = Path(args.workdir)
    out_dir, repo = workdir / "out", workdir / "repo"
    input_dir = Path(args.input_dir) if args.input_dir else workdir / "input"
    synthetic = args.synthetic
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Log(out_dir / "run.log")
    state_path = out_dir / "driver_state.json"
    urls: dict = {}
    outcome = "refused"
    try:
        check_run_args(args)
        urls = parse_urls(args.url)
        if args.fresh:
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            log("--fresh: 이전 산출물을 지웠다(입력은 그대로)")
        if (out_dir / "FAILED").exists():
            raise DriverError("FAILED 마커가 있습니다(원인을 고친 뒤 --fresh로 처음부터).", m4.EXIT_FAILED_MARKER)
        code = m4.extract_tarball(Path(args.code_tarball), repo, args.code_sha256)
        log(f"code: tarball sha256 {code['sha256'][:12]}…, commit {code['commit']}")
        input_info = {"synthetic": synthetic} if synthetic else check_input(input_dir, args.export_identity)
        log(f"input: {json.dumps(input_info)}")
        run_args = {"synthetic": synthetic, "cu_cap": args.cu_cap}
        digest = m4.config_digest(code["sha256"], None, {**run_args, "input": input_info.get("export_identity_sha256")})
        state = read_json(state_path)
        if state and state.get("config_digest") != digest:
            raise DriverError("이전 실행과 설정(코드·입력·인자)이 다릅니다. 이어서 돌릴 수 없습니다(--fresh로 처음부터).",
                              m4.EXIT_CONFIG_MISMATCH)
        if not state:
            state = {"config_digest": digest, "code": {"sha256": code["sha256"], "commit": code["commit"]},
                     "run_args": run_args, "input": input_info, "invocations": [], "host": m4.host_info(),
                     "cu_before": args.cu_before}
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
        state["environment"] = check_environment(repo, python, env_probe, args.allow_cpu)
        state["runtime_label"] = args.runtime_label or runtime_label(state["environment"])
        log(f"environment: {json.dumps({k: state['environment'][k] for k in ('gpu', 'cuda', 'python')})}, "
            f"핀 {len(state['environment']['pins'])}개 일치")
        write_json(state_path, state)
        if args.resume_bundle:
            state["resumed_from_bundle"] = restore_bundle(Path(args.resume_bundle), args.resume_bundle_sha256, workdir, log)
        extra_env = {}
        snapshot = ensure_model_snapshot(urls, args.model_snapshot_sha256, workdir, log, opener=opener)
        if snapshot:
            extra_env["HF_HUB_CACHE"] = snapshot["hf_cache"]
            state["model_snapshot"] = {"sha256": snapshot["sha256"]}
        rc = run_job(workdir, state, state_path, python=python, input_dir=input_dir, synthetic=synthetic,
                     cu_rate=args.cu_rate, cu_cap=args.cu_cap, cu_spent_before=args.cu_spent_before, log=log,
                     runner=runner, clock=clock, extra_env=extra_env, heartbeat_seconds=heartbeat_seconds)
        outcome = {m4.EXIT_OK: "done", EXIT_BUDGET: "budget_stopped"}.get(rc, "failed")
        if rc == m4.EXIT_OK:
            job = read_json(out_dir / JOB_FILE, {})
            log("끝: " + json.dumps({k: job.get(k) for k in ("rows", "encode_s", "articles_per_s", "tokens_per_s",
                                                             "model_revision")}))
        return rc
    except DriverError as e:
        log(f"중단: {m4.redact(str(e), list(urls.values()))}")
        if e.invalidates_run:
            write_json(out_dir / "FAILED", {"reason": m4.redact(str(e), list(urls.values())),
                                            "rule": "실행 무효. 원인을 고친 뒤 처음부터 다시 돌린다."})
        return e.code
    finally:
        if not synthetic:
            delete_inputs(workdir, input_dir, log, out_dir, reason=outcome)


def cmd_status(args) -> int:
    out_dir = Path(args.workdir) / "out"
    state = read_json(out_dir / "driver_state.json")
    if not state:
        print("상태 파일이 없습니다(아직 실행하지 않았습니다).")
        return m4.EXIT_OK
    progress = read_json(out_dir / PROGRESS_FILE, {"parts": {}})
    pid = read_json(out_dir / "driver.pid")
    summary = {"parts_done": len(progress.get("parts", {})), "wall_hours": round(m4.wall_seconds(state) / 3600, 3),
               "failed": (out_dir / "FAILED").exists(), "budget_stop": (out_dir / STOP_FILE).exists(),
               "pack": (out_dir / PACK_DIR / "embeddings_manifest.json").exists(),
               "input_present": any((Path(args.workdir) / "input" / n).exists() for n in INPUT_FILES),
               "code_commit": state["code"]["commit"], "detached_running": bool(pid and m4._pid_alive(pid["pid"]))}
    print(json.dumps(summary, ensure_ascii=False))
    log_path = out_dir / "run.log"
    if log_path.exists():
        print("".join(log_path.read_text(encoding="utf-8").splitlines(keepends=True)[-args.tail:]), end="")
    return m4.EXIT_OK


def cmd_cleanup(args) -> int:
    """입력 본문을 지운다(--all이면 산출물·코드도). 남은 파일 목록을 출력한다."""
    workdir = Path(args.workdir)
    record = delete_inputs(workdir, Path(args.input_dir) if args.input_dir else workdir / "input",
                           Log(workdir / "out" / "run.log"), workdir / "out", reason="manual_cleanup")
    if args.all:
        for name in ("out", "repo", "hf_cache", "model_snapshot.tar", "bundle_tmp"):
            target = workdir / name
            shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
        record["remaining"] = listing(workdir, skip=())
    print(json.dumps({"removed": record["removed"], "remaining": record["remaining"]}, ensure_ascii=False))
    return m4.EXIT_OK


def cmd_bundle(args) -> int:
    try:
        info = make_bundle(Path(args.workdir) / "out", Path(args.out))
    except DriverError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return e.code
    print(json.dumps(info, ensure_ascii=False))
    return m4.EXIT_OK


def build_parser():
    ap = m4._Parser(description="동결 반출본의 기사 임베딩 드라이버(운영 임베딩 경로, Colab T4)")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="tarball 풀기 → 입력 확인 → 핀 설치·확인 → 잡 → 입력 삭제")
    run.add_argument("--workdir", required=True)
    run.add_argument("--code-tarball", required=True)
    run.add_argument("--code-sha256", default=None)
    run.add_argument("--input-dir", default=None, help="올린 반출본 디렉터리(기본 <workdir>/input)")
    run.add_argument("--export-identity", default=None, help="로컬 반출 매니페스트의 identity_sha256")
    run.add_argument("--synthetic", type=int, default=None, help="합성 텍스트 n건(드라이 런, 기사 본문 없음)")
    run.add_argument("--url", action="append", default=[], metavar="KEY=URL", help="model_snapshot=<서명 URL>(출력하지 않음)")
    run.add_argument("--model-snapshot-sha256", default=None)
    run.add_argument("--resume-bundle", default=None)
    run.add_argument("--resume-bundle-sha256", default=None)
    run.add_argument("--cu-rate", type=float, default=None, help="런타임의 CU/h(드라이 런에서 잰 값)")
    run.add_argument("--cu-cap", type=float, default=CU_CAP_DEFAULT)
    run.add_argument("--cu-spent-before", type=float, default=0.0, help="앞 세션들이 이 잡에 쓴 CU")
    run.add_argument("--cu-before", type=float, default=None, help="실행 전 잔액(기록용)")
    run.add_argument("--runtime-label", default=None)
    run.add_argument("--python", default=None)
    run.add_argument("--skip-install", action="store_true")
    run.add_argument("--fresh", action="store_true", help="이전 산출물을 지우고 처음부터(입력은 그대로)")
    run.add_argument("--allow-cpu", action="store_true", help="CUDA 없이 돈다(등록한 런타임이 아님, 기록에 남는다)")
    run.add_argument("--detach", action="store_true", help="새 세션의 자식 프로세스로 띄우고 바로 돌아온다")
    bd = sub.add_parser("bundle", help="파트 묶음(tar.gz)을 만들고 sha256을 출력")
    bd.add_argument("--workdir", required=True)
    bd.add_argument("--out", required=True)
    sp = sub.add_parser("stop", help="분리 실행을 끝낸다(FAILED 마커 없음)")
    sp.add_argument("--workdir", required=True)
    sp.add_argument("--wait", type=float, default=10.0)
    st = sub.add_parser("status", help="파트 진행과 로그 끝부분")
    st.add_argument("--workdir", required=True)
    st.add_argument("--tail", type=int, default=20)
    cl = sub.add_parser("cleanup", help="입력 본문 삭제 후 남은 파일 목록 출력")
    cl.add_argument("--workdir", required=True)
    cl.add_argument("--input-dir", default=None)
    cl.add_argument("--all", action="store_true", help="산출물·코드·모델 캐시도 삭제(팩을 내려받은 뒤에)")
    return ap


def main(argv=None, **hooks) -> int:
    if argv is None:
        raw = os.environ.get(ARGS_ENV)
        argv = json.loads(raw) if raw else sys.argv[1:]
    args = build_parser().parse_args(argv)
    args.raw_argv = list(argv)
    if args.command == "run":
        return cmd_run(args, **hooks)
    return {"status": cmd_status, "cleanup": cmd_cleanup, "stop": m4.cmd_stop, "bundle": cmd_bundle}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
