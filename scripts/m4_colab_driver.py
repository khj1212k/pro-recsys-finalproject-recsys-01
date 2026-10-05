#!/usr/bin/env python3
"""EB-NeRD v1.2 콜드 regime 사슬을 원격 CPU 런타임(Colab)에서 돌리는 드라이버 — 표준 라이브러리만 쓰는 단일 파일.

순서: 코드 tarball 풀기 → 서명 URL로 데이터 내려받기 → manifest의 sha256과 대조 → 고정 버전 설치 →
사전 등록한 단계를 하나씩 서브프로세스로 실행(run_cold) → 계산 자원 기록 → 리포트 조립.
무엇을 어떤 인자로 돌리는지는 ADR 0013 "A2 사전 등록" A2.8·A2.9에 실행 전에 고정했다.

    python m4_colab_driver.py run --workdir /content/m4 --code-tarball /content/m4_code.tar.gz \
        --code-sha256 <sha256> --manifest /content/m4_manifest.json \
        --url ebnerd_small/train/behaviors.parquet=<서명 URL> [--url ...] --cu-rate <CU/h> --cu-cap 6
    python m4_colab_driver.py run --workdir /content/m4 --code-tarball ... --synthetic     # 데이터 없이 경로만 확인
    python m4_colab_driver.py run ... --detach        # 분리해서 띄우고 바로 돌아온다(긴 실행용)
    python m4_colab_driver.py status --workdir /content/m4
    python m4_colab_driver.py stop --workdir /content/m4
    python m4_colab_driver.py cleanup --workdir /content/m4
    python m4_colab_driver.py manifest --root <EBNERD_ROOT> --dataset ebnerd_small \
        --articles-meta <메타 전용 parquet> --out m4_manifest.json                        # 로컬에서, 올리기 전에

규칙:
- 서명 URL은 인자(--url) 또는 환경변수(M4_URLS_JSON, {"키": "URL"})로만 받는다. 로그·상태 파일·오류 메시지 어디에도
  URL을 쓰지 않는다. 내려받은 파일은 키(상대 경로)로만 부른다. 인자를 잘못 줘서(--url이나 `키=`를 빼먹음) 파서가 내는
  오류 메시지에서도 URL처럼 보이는 토큰은 지우고, 그때의 종료 코드는 64(사용법 오류)다.
- 데이터 파일의 sha256이 하나라도 다르면 그 파일을 지우고 FAILED 마커를 남긴 채 멈춘다. 내려받기 자체의 실패
  (만료된 URL, 네트워크)와 설치 실패는 마커 없이 멈추고, 같은 명령으로 다시 시도할 수 있다.
- 세션이 끊겨 프로세스가 죽은 경우에는 같은 명령으로 다시 실행하면 끝난 단계·단위를 건너뛰고 이어서 돈다
  (설정 해시가 같을 때만. 다르면 거부). 단계가 0이 아닌 코드로 끝난 경우는 실행이 무효라서 FAILED 마커를 남기고,
  마커가 있는 동안은 --fresh 없이 다시 돌지 않는다(통과할 때까지 조용히 재시도하는 길을 막는다).
- 단계를 시작하기 전에 `rate x 누적 벽시계 시간`으로 CU를 추정한다. 경고선 이상이면 서술용 단계를 건너뛰고, 상한
  이상이면 남은 단계를 돌리지 않는다. 건너뛴 단계는 리포트에 "미측정"으로 남는다. assemble은 항상 돈다.
  이 규칙은 rate가 있어야 적용되므로 --synthetic이 아닌 실행은 --cu-rate 없이 시작하지 않는다. CU로 과금하지 않는
  런타임에서는 --cu-rate 0을 명시하고, 그때는 규칙이 꺼져 있었다는 것이 로그와 compute.json에 남는다.
- argv를 넘길 수 없는 실행기에서는 환경변수 M4_DRIVER_ARGS_JSON(인자 목록의 JSON)으로 같은 인자를 준다.
- --detach는 tarball을 푼 뒤 그 안의 이 스크립트를 새 세션의 자식 프로세스로 띄우고 pid만 남긴 채 돌아온다. URL은
  자식에게 환경변수로 넘긴다(명령줄에 남기지 않는다). 계산 내용은 분리하지 않은 실행과 같다. stop은 그 프로세스
  묶음을 끝낼 뿐 FAILED 마커를 남기지 않으므로 같은 명령으로 이어서 돌 수 있다. 원격 런타임에서 분리한 프로세스가
  얼마나 살아남는지는 드라이 런(--synthetic)에서 먼저 확인한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# 사전 등록(preregistration/cold-v1.2.yaml)의 run 블록과 같은 값이다. 이 파일은 yaml 없이 돌아야 해서 여기 적고,
# 두 곳이 같은지는 테스트(tests/evaluation/test_m4_colab_driver.py)가 지킨다.
STAGES = ("fit", "e1", "e2p2", "e8", "e4", "e2p1", "assemble")
DESCRIPTIVE_STAGES = ("e4", "e2p1")
REGISTERED = {"dataset": "ebnerd_small", "seeds": [0, 1, 2], "n_boot": 1000, "p2_sample": 20000, "sub_cap": 20000}
SYNTHETIC = {"dataset": "ebnerd_synth", "seeds": [0], "n_boot": 50, "p2_sample": 150, "sub_cap": 100}
CU_CAP, CU_WARN = 6.0, 5.0
MODULE = "evaluation.recsys.ebnerd.run_cold"
REQUIREMENTS = "requirements-colab.txt"
DATA_FILES = ("train/behaviors.parquet", "train/history.parquet", "validation/behaviors.parquet",
              "validation/history.parquet", "articles.parquet")
EMB_FILES = ("article_ids.npy", "bge_m3_tsb512.f16.npy", "bge_m3_tsb512.meta.json")
PREREG_YAML = "evaluation/recsys/ebnerd/preregistration/cold-v1.2.yaml"
# 사전 등록에 sha256이 적혀 있는 입력(나머지 셋 — 메타 전용 기사 파일, 기사 id 배열, 임베딩 meta — 은 등록값이 없다).
REGISTERED_INPUTS = ("train/behaviors.parquet", "train/history.parquet", "validation/behaviors.parquet",
                     "validation/history.parquet", "bge_m3_tsb512.f16.npy")

EXIT_OK, EXIT_SHA_MISMATCH, EXIT_CONFIG_MISMATCH, EXIT_FAILED_MARKER, EXIT_STAGE_FAILED = 0, 2, 3, 4, 5
EXIT_DOWNLOAD, EXIT_INSTALL, EXIT_USAGE = 6, 7, 64


class DriverError(RuntimeError):
    """invalidates_run이 참이면 실행 무효(FAILED 마커)다. 내려받기·설치 실패처럼 다시 시도하면 되는 오류는 거짓이다."""

    def __init__(self, message: str, code: int, invalidates_run: bool = False):
        super().__init__(message)
        self.code = code
        self.invalidates_run = invalidates_run


# --- 작은 도구 ---------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def redact(text: str, secrets: list[str]) -> str:
    """문자열에서 서명 URL(과 그 일부)을 지운다. 예외 메시지를 로그에 쓰기 전에 항상 거친다."""
    out = str(text)
    for s in sorted({x for x in secrets if x}, key=len, reverse=True):
        out = out.replace(s, "<signed-url>")
        for part in (s.split("?", 1)[0], s.split("?", 1)[-1]):
            if len(part) >= 8:
                out = out.replace(part, "<signed-url>")
    return out


# URL처럼 보이는 토큰: 스킴이 있거나(://), 쿼리 문자열(?a=b, &a=b)이 붙었거나, 서명 인자 이름이 들어 있다.
_URLISH = re.compile(r"\S*(?:://|[?&][^\s=&]+=|Signature=|Credential=)\S*")


def scrub_urls(text: str) -> str:
    """어떤 값이 URL인지 모르는 자리(인자 파서의 오류 메시지)에서 URL처럼 보이는 토큰을 통째로 지운다."""
    return _URLISH.sub("<signed-url>", str(text))


class Log:
    """표준 출력과 <out>/run.log에 같은 줄을 쓴다. URL은 호출부가 넘기지 않는다."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} driver {message}"
        print(line, flush=True)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


# --- 코드 tarball ------------------------------------------------------------------------------

def extract_tarball(tarball: Path, dest: Path, expected_sha256: Optional[str] = None) -> dict:
    """tarball을 dest에 푼다. dest 밖으로 나가는 경로·링크가 있으면 거부한다. 반환: sha256과 커밋 id(git archive가
    pax 헤더 comment에 남긴 값, 없으면 None)."""
    digest = sha256_file(tarball)
    if expected_sha256 and digest != expected_sha256.lower():
        raise DriverError("코드 tarball의 sha256이 --code-sha256과 다릅니다(아무것도 실행하지 않았습니다)", EXIT_SHA_MISMATCH)
    marker = dest / ".tarball_sha256"
    with tarfile.open(tarball) as tf:
        commit = (tf.pax_headers or {}).get("comment")
        if marker.exists() and marker.read_text().strip() == digest:
            return {"sha256": digest, "commit": commit, "extracted": False}
        root = dest.resolve()
        members = tf.getmembers()
        for m in members:
            target = (dest / m.name).resolve()
            if target != root and root not in target.parents:
                raise DriverError(f"tarball 안에 대상 디렉터리 밖 경로가 있습니다: {m.name!r}", EXIT_USAGE)
            if m.issym() or m.islnk() or m.isdev():
                raise DriverError(f"tarball 안에 링크·장치 파일이 있습니다: {m.name!r}", EXIT_USAGE)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:
            tf.extractall(dest, members=members)
    marker.write_text(digest + "\n")
    return {"sha256": digest, "commit": commit, "extracted": True}


# --- 데이터 내려받기와 검증 --------------------------------------------------------------------

def fetch(url: str, dest: Path, opener: Callable = urllib.request.urlopen, timeout: float = 120.0) -> tuple[str, int]:
    """url을 dest에 스트리밍으로 받는다. 반환: (sha256, 바이트 수). 오류 메시지에는 URL을 넣지 않는다."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h, n = hashlib.sha256(), 0
    try:
        with opener(url, timeout=timeout) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                n += len(chunk)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise DriverError(f"내려받기 실패: HTTP {e.code}", EXIT_DOWNLOAD) from None
    except Exception as e:  # noqa: BLE001 - 어떤 예외든 URL이 섞인 메시지를 그대로 내보내지 않는다
        tmp.unlink(missing_ok=True)
        raise DriverError(f"내려받기 실패: {type(e).__name__}", EXIT_DOWNLOAD) from None
    os.replace(tmp, dest)
    return h.hexdigest(), n


def ensure_data(manifest: dict, data_dir: Path, urls: dict[str, str], log: Log,
                opener: Callable = urllib.request.urlopen) -> dict:
    """manifest의 모든 파일이 data_dir에 있고 sha256이 맞게 한다. 이미 맞는 파일은 다시 받지 않는다."""
    report = {}
    for key, spec in manifest["files"].items():
        dest = data_dir / key
        want = spec["sha256"].lower()
        if dest.exists() and sha256_file(dest) == want:
            log(f"data {key}: 이미 있음, sha256 일치")
            report[key] = {"bytes": dest.stat().st_size, "downloaded": False}
            continue
        if key not in urls:
            raise DriverError(f"{key}의 URL이 없습니다(--url {key}=...)", EXIT_USAGE)
        t0 = time.time()
        try:
            got, n = fetch(urls[key], dest, opener=opener)
        except DriverError as e:
            raise DriverError(f"{key}: {redact(str(e), list(urls.values()))}", e.code) from None
        if got != want:
            dest.unlink(missing_ok=True)
            raise DriverError(f"{key}: sha256 불일치(받은 파일을 지웠습니다)", EXIT_SHA_MISMATCH, invalidates_run=True)
        log(f"data {key}: {n} bytes, sha256 일치, {time.time() - t0:.1f}s")
        report[key] = {"bytes": n, "downloaded": True, "seconds": round(time.time() - t0, 1)}
    return report


def check_manifest(manifest: dict, repo: Path, allow_unregistered: bool = False) -> None:
    """내려받기 전에 manifest를 본다: 필요한 파일 8개가 다 있는지, 등록된 입력의 sha256이 사전 등록 yaml에 적힌 값인지.

    여러 시간 돌린 뒤에 "입력이 등록값과 달라 demo 등급"으로 끝나는 일을 막는다. yaml 파서 없이 돌아야 하므로
    등록 파일 안에 그 sha256 문자열이 있는지만 본다(정식 대조는 run_cold의 증거 등급 판정이 한다).
    """
    dataset = manifest.get("dataset")
    need = [f"{dataset}/{rel}" for rel in DATA_FILES] + [f"derived/{dataset}/{rel}" for rel in EMB_FILES]
    missing = [k for k in need if k not in manifest.get("files", {})]
    if missing:
        raise DriverError(f"manifest에 필요한 파일이 없습니다: {missing}", EXIT_USAGE)
    if allow_unregistered:
        return
    if dataset != REGISTERED["dataset"]:
        raise DriverError(f"manifest의 dataset이 {dataset!r}입니다(등록값 {REGISTERED['dataset']!r}). 등록하지 않은 입력으로 "
                          "돌리려면 --allow-unregistered-data(결과는 demo 등급).", EXIT_USAGE)
    registered = (repo / PREREG_YAML).read_text(encoding="utf-8") if (repo / PREREG_YAML).exists() else ""
    wrong = [k for k in need if k.split("/", 1)[1].endswith(REGISTERED_INPUTS)
             and manifest["files"][k]["sha256"].lower() not in registered]
    if wrong:
        raise DriverError(f"manifest의 sha256이 사전 등록 값과 다릅니다: {wrong}. 등록하지 않은 입력으로 돌리려면 "
                          "--allow-unregistered-data(결과는 demo 등급).", EXIT_USAGE)


def parse_urls(pairs: list[str], env: dict) -> dict[str, str]:
    urls: dict[str, str] = {}
    raw = env.get("M4_URLS_JSON")
    if raw:
        urls.update(json.loads(raw))
    for p in pairs or []:
        key, sep, url = p.partition("=")
        # `키=`를 빼먹으면 URL의 쿼리 문자열에 있는 첫 "="에서 잘려 URL 앞부분이 키가 된다. 그 값은 쓰지도 찍지도 않는다.
        if not sep or not key or not url or scrub_urls(key) != key:
            raise DriverError("--url은 <manifest 키>=<URL> 형식이어야 합니다(키는 manifest의 상대 경로)", EXIT_USAGE)
        urls[key] = url
    return urls


def build_manifest(root: Path, dataset: str, articles_meta: Path) -> dict:
    """로컬 파일에서 manifest를 만든다. articles.parquet 자리에는 본문을 뺀 메타 전용 파생 파일을 넣는다."""
    files = {}
    for rel in DATA_FILES:
        src = articles_meta if rel == "articles.parquet" else root / dataset / rel
        files[f"{dataset}/{rel}"] = {"sha256": sha256_file(src), "bytes": src.stat().st_size}
    for rel in EMB_FILES:
        src = root / "derived" / dataset / rel
        files[f"derived/{dataset}/{rel}"] = {"sha256": sha256_file(src), "bytes": src.stat().st_size}
    original = root / dataset / "articles.parquet"
    return {"dataset": dataset, "files": files,
            "articles": {"derived_sha256": files[f"{dataset}/articles.parquet"]["sha256"],
                         "original_sha256": sha256_file(original) if original.exists() else None,
                         "note": "articles.parquet은 메타 5개 열만 남긴 파생 파일이다(본문 텍스트 없음)"}}


# --- 단계 실행 ---------------------------------------------------------------------------------

def stage_command(python: str, workdir: Path, run_args: dict, threads: int, stage: str) -> list[str]:
    """사전 등록 A2.8의 단계 명령. 인자 순서까지 등록한 그대로다."""
    cmd = [python, "-m", MODULE, "--dataset", run_args["dataset"], "--root", str(workdir / "data"),
           "--out-dir", str(workdir / "out"), "--seeds", *[str(s) for s in run_args["seeds"]],
           "--n-boot", str(run_args["n_boot"]), "--p2-sample", str(run_args["p2_sample"]),
           "--sub-cap", str(run_args["sub_cap"]), "--threads", str(threads), "--resume", "--stage", stage]
    return cmd


def subprocess_runner(cmd: list[str], cwd: Path, env: dict, log_path: Path) -> int:
    """단계를 서브프로세스로 돌리고 출력(집계 수치와 진행 상태뿐이다)을 run.log에 이어 쓴다."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"--- {' '.join(cmd[1:])}\n")
        f.flush()
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            sys.stdout.write(line)
            f.write(line)
        return proc.wait()


def config_digest(code_sha256: str, manifest: Optional[dict], run_args: dict) -> str:
    files = {k: v["sha256"] for k, v in (manifest or {"files": {}})["files"].items()}
    blob = json.dumps({"code": code_sha256, "files": files, "run": run_args}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def host_info() -> dict:
    ram_gb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system(),
            "cpu_count": os.cpu_count(), "ram_gb": ram_gb}


def default_runtime_label() -> str:
    """--runtime-label을 주지 않았을 때의 표기. Colab VM(/content가 있는 Linux)이 아니면 local로 적어, 다른 곳에서 돈
    기록이 원격 런타임의 것으로 읽히지 않게 한다."""
    if platform.system() == "Linux" and Path("/content").is_dir():
        return "colab-cpu"
    return f"local-{platform.system().lower()}-{platform.machine()}"


def wall_seconds(state: dict) -> float:
    return float(sum(max(0.0, i["last_seen"] - i["started"]) for i in state.get("invocations", [])))



def run_stages(workdir: Path, state: dict, state_path: Path, run_args: dict, *, python: str, threads: int,
               cu_rate: Optional[float], cu_cap: float, cu_warn: float, log: Log,
               runner: Callable = subprocess_runner, clock: Callable[[], float] = time.time,
               extra_env: Optional[dict] = None, stages: tuple = STAGES) -> int:
    """남은 단계를 순서대로 돌린다. 끝난 단계는 건너뛰고, 예산 규칙으로 건너뛴 단계는 state에 적는다."""
    out_dir = workdir / "out"
    env = dict(os.environ)
    env.update({"EBNERD_ROOT": str(workdir / "data"), "PYTHONUNBUFFERED": "1"})
    env.update(extra_env or {})
    env.pop("M4_URLS_JSON", None)   # 서브프로세스에는 URL을 넘기지 않는다
    invocation = state["invocations"][-1]

    def beat():
        invocation["last_seen"] = clock()
        write_json(state_path, state)

    def cu_now() -> Optional[float]:
        return None if cu_rate is None else cu_rate * wall_seconds(state) / 3600.0

    budget_active = cu_rate is not None and cu_rate > 0
    if not budget_active:
        log(f"budget: CU rate가 {'없다' if cu_rate is None else '0이다'} — 예산 규칙이 꺼져 있다"
            f"(상한 {cu_cap}·경고 {cu_warn} CU를 적용하지 않고, 어떤 단계도 예산으로 건너뛰지 않는다)")

    def write_compute():
        cu = cu_now()
        write_json(out_dir / "compute.json", {
            **state.get("host", {}), "runtime": state.get("runtime_label"),
            "wall_seconds_total": round(wall_seconds(state), 1),
            "wall_seconds_by_stage": {s: v["seconds"] for s, v in state["stages"].items() if v.get("status") == "done"},
            "rate_cu_per_hour": cu_rate, "cu_estimated": None if cu is None else round(cu, 3),
            "budget_rules_active": budget_active,
            "cu_cap": cu_cap, "cu_warn": cu_warn, "cu_before": state.get("cu_before"),
            "skipped_for_budget": [s for s, v in state["stages"].items() if v.get("status") == "skipped_budget"],
            "invocations": len(state["invocations"]),
            "note": "cu_estimated = rate x 벽시계 시간. 죽은 세션의 마지막 단계 시간은 빠지므로 하한이다. "
                    "정확한 값은 실행 전후의 잔액 차이로 따로 적는다."})

    ran_any = False
    for stage in stages:
        beat()
        st = state["stages"].setdefault(stage, {})
        # assemble은 이번 호출에서 다른 단계가 하나라도 돌았으면 다시 돈다(예산으로 건너뛴 단계를 나중에 채운 경우에
        # 예전 리포트가 남지 않게).
        if st.get("status") == "done" and not (stage == "assemble" and ran_any):
            log(f"stage {stage}: 이미 끝남, 건너뜀")
            continue
        if stage == "assemble":
            write_compute()
        else:
            cu = cu_now()
            if cu is not None:
                log(f"budget: 누적 {wall_seconds(state) / 3600:.2f} h x {cu_rate} CU/h = {cu:.2f} CU (경고 {cu_warn}, 상한 {cu_cap})")
                if cu >= cu_cap or (cu >= cu_warn and stage in DESCRIPTIVE_STAGES):
                    st.update(status="skipped_budget", cu_at_decision=round(cu, 3))
                    log(f"stage {stage}: 예산 규칙으로 건너뜀(리포트에 미측정으로 남는다)")
                    beat()
                    continue
        t0 = clock()
        st.update(status="running")
        beat()
        log(f"stage {stage}: 시작")
        code = runner(stage_command(python, workdir, run_args, threads, stage), workdir / "repo", env, out_dir / "run.log")
        st["seconds"] = round(clock() - t0, 1)
        if code != 0:
            st.update(status="failed", returncode=code)
            beat()
            write_json(out_dir / "FAILED", {"stage": stage, "returncode": code,
                                            "rule": "단계 실패는 실행 무효다. 원인을 고친 뒤 새 SHA로 사슬 전체를 다시 돌린다."})
            log(f"stage {stage}: 실패(returncode {code}) — FAILED 마커를 남겼다")
            return EXIT_STAGE_FAILED
        st.update(status="done")
        ran_any = True
        beat()
        log(f"stage {stage}: 완료 {st['seconds']}s")
    write_compute()
    return EXIT_OK


# --- 하위 명령 ---------------------------------------------------------------------------------

def _child_argv(raw: list[str]) -> list[str]:
    """분리 실행의 자식에게 넘길 인자: --detach와 --url 쌍을 뺀다(URL은 환경변수로 간다)."""
    out, skip = [], False
    for tok in raw:
        if skip:
            skip = False
        elif tok == "--url":
            skip = True
        elif tok == "--detach" or tok.startswith("--url="):
            continue
        else:
            out.append(tok)
    return out


def _pid_alive(pid: int) -> bool:
    """pid가 살아 있는가. 끝났지만 아직 거두지 않은 자식(좀비)은 여기서 거두고 죽은 것으로 본다 — 분리 실행을 띄운
    프로세스가 계속 살아 있는 경우(테스트, 노트북 커널)에 끝난 실행이 "실행 중"으로 보이지 않게 한다."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass            # 우리 자식이 아니다(띄운 프로세스가 이미 끝난 보통의 경우)
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def check_run_args(args) -> None:
    """아무것도 지우거나 받거나 띄우기 전에 보는 인자 규칙."""
    if args.cu_rate is None and not args.synthetic:
        raise DriverError("--cu-rate가 필요합니다. 등록한 예산 규칙(상한·경고)은 rate가 있어야 적용됩니다: 드라이 런"
                          "(--synthetic)에서 잰 CU/h를 주거나, CU로 과금하지 않는 런타임이면 --cu-rate 0을 명시하세요.",
                          EXIT_USAGE)
    if args.cu_rate is not None and args.cu_rate < 0:
        raise DriverError("--cu-rate는 0 이상이어야 합니다", EXIT_USAGE)


def cmd_detach(args) -> int:
    workdir = Path(args.workdir)
    out_dir = workdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Log(out_dir / "run.log")
    try:
        check_run_args(args)
        prev = read_json(out_dir / "driver.pid")
        if prev and _pid_alive(prev["pid"]):
            raise DriverError(f"이미 실행 중입니다(pid {prev['pid']}). status로 보거나 stop으로 끝내세요.", EXIT_USAGE)
        urls = parse_urls(args.url, os.environ)
        extract_tarball(Path(args.code_tarball), workdir / "repo", args.code_sha256)
        script = workdir / "repo" / "scripts" / "m4_colab_driver.py"
        if not script.exists():
            raise DriverError("tarball 안에 scripts/m4_colab_driver.py가 없습니다", EXIT_USAGE)
        env = dict(os.environ)
        env.pop("M4_DRIVER_ARGS_JSON", None)
        if urls:
            env["M4_URLS_JSON"] = json.dumps(urls)
        with open(out_dir / "driver_stdout.log", "ab") as sink:
            proc = subprocess.Popen([args.python or sys.executable, str(script), *_child_argv(args.raw_argv)],
                                    env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                    start_new_session=True, cwd=str(workdir))
        write_json(out_dir / "driver.pid", {"pid": proc.pid, "started": time.time()})
        log(f"detach: pid {proc.pid}로 분리 실행")
        print(json.dumps({"detached": True, "pid": proc.pid}))
        return EXIT_OK
    except DriverError as e:
        log(f"중단: {redact(str(e), [u.split('=', 1)[-1] for u in args.url])}")
        return e.code


def cmd_stop(args) -> int:
    out_dir = Path(args.workdir) / "out"
    info = read_json(out_dir / "driver.pid")
    if not info or not _pid_alive(info["pid"]):
        print(json.dumps({"stopped": False, "reason": "실행 중인 분리 프로세스가 없습니다"}, ensure_ascii=False))
        return EXIT_OK
    try:
        os.killpg(info["pid"], signal.SIGTERM)   # 분리할 때 새 세션을 만들었으므로 pid가 프로세스 묶음 id다
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.time() + args.wait
    while _pid_alive(info["pid"]) and time.time() < deadline:
        time.sleep(0.2)
    Log(out_dir / "run.log")(f"stop: pid {info['pid']} 종료 요청(FAILED 마커 없음 — 같은 명령으로 이어서 돌 수 있다)")
    print(json.dumps({"stopped": not _pid_alive(info["pid"]), "pid": info["pid"]}))
    return EXIT_OK


def cmd_run(args, *, runner: Callable = subprocess_runner, opener: Callable = urllib.request.urlopen,
            clock: Callable[[], float] = time.time, installer: Optional[Callable] = None) -> int:
    if getattr(args, "detach", False):
        return cmd_detach(args)
    workdir = Path(args.workdir)
    out_dir, repo, data = workdir / "out", workdir / "repo", workdir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Log(out_dir / "run.log")
    state_path = out_dir / "driver_state.json"
    urls: dict[str, str] = {}
    try:
        check_run_args(args)
        urls = parse_urls(args.url, os.environ)
        if args.fresh:
            for p in (out_dir, data):
                shutil.rmtree(p, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            log("--fresh: 이전 산출물과 데이터를 지웠다")
        if (out_dir / "FAILED").exists():
            raise DriverError("FAILED 마커가 있습니다. 이 실행은 무효입니다(원인을 고친 뒤 --fresh로 처음부터).",
                              EXIT_FAILED_MARKER)
        code = extract_tarball(Path(args.code_tarball), repo, args.code_sha256)
        log(f"code: tarball sha256 {code['sha256'][:12]}…, commit {code['commit']}, "
            f"{'새로 풂' if code['extracted'] else '이미 풀려 있음'}")
        run_args = dict(SYNTHETIC if args.synthetic else REGISTERED)
        manifest = None if args.synthetic else read_json(Path(args.manifest))
        if not args.synthetic and not manifest:
            raise DriverError("--manifest가 필요합니다(--synthetic이 아닐 때)", EXIT_USAGE)
        if manifest:
            check_manifest(manifest, repo, allow_unregistered=args.allow_unregistered_data)
        digest = config_digest(code["sha256"], manifest, run_args)
        state = read_json(state_path)
        if state and state.get("config_digest") != digest:
            raise DriverError("이전 실행과 설정(코드 tarball·manifest·실행 인자)이 다릅니다. 이어서 돌릴 수 없습니다 "
                              "(--fresh로 처음부터).", EXIT_CONFIG_MISMATCH)
        if not state:
            state = {"config_digest": digest, "code": {"sha256": code["sha256"], "commit": code["commit"]},
                     "run_args": run_args, "synthetic": bool(args.synthetic), "stages": {}, "invocations": [],
                     "host": host_info(), "runtime_label": args.runtime_label or default_runtime_label(),
                     "cu_before": args.cu_before}
        now = clock()
        state["invocations"].append({"started": now, "last_seen": now})
        write_json(state_path, state)

        python = args.python or sys.executable
        if not state.get("installed") and not args.skip_install:
            log(f"install: {REQUIREMENTS}")
            rc = (installer or _pip_install)(python, repo / REQUIREMENTS, out_dir / "run.log")
            if rc != 0:
                raise DriverError(f"고정 버전 설치 실패(returncode {rc})", EXIT_INSTALL)
            state["installed"] = True
            write_json(state_path, state)
        if args.synthetic:
            if not (data / SYNTHETIC["dataset"] / "articles.parquet").exists():
                clean_env = {k: v for k, v in os.environ.items() if k != "M4_URLS_JSON"}
                rc = runner([python, "-m", "evaluation.recsys.ebnerd.synthetic", "--out", str(data),
                             "--name", SYNTHETIC["dataset"]], repo, clean_env, out_dir / "run.log")
                if rc != 0:
                    raise DriverError(f"합성 데이터 생성 실패(returncode {rc})", EXIT_INSTALL)
            state["data"] = {"synthetic": True}
        else:
            state["data"] = ensure_data(manifest, data, urls, log, opener=opener)
            state["articles"] = manifest.get("articles")
        write_json(state_path, state)
        extra_env = {"M4_CODE_SHA": code["commit"] or f"tarball-{code['sha256'][:16]}"}
        if args.prereg_commit:
            extra_env["M4_PREREG_COMMIT"] = args.prereg_commit
        rc = run_stages(workdir, state, state_path, run_args, python=python, threads=args.threads,
                        cu_rate=args.cu_rate, cu_cap=args.cu_cap, cu_warn=args.cu_warn, log=log, runner=runner,
                        clock=clock, extra_env=extra_env)
        if rc == EXIT_OK:
            log(f"끝. 리포트: {out_dir / 'ebnerd_v1_2_cold.json'} , {out_dir / 'ebnerd_v1_2_cold.md'}")
        return rc
    except DriverError as e:
        message = redact(str(e), list(urls.values()))
        log(f"중단: {message}")
        if e.invalidates_run:
            write_json(out_dir / "FAILED", {"reason": message, "rule": "실행 무효. 원인을 고친 뒤 처음부터 다시 돌린다."})
        return e.code


def _pip_install(python: str, requirements: Path, log_path: Path) -> int:
    with open(log_path, "a", encoding="utf-8") as f:
        return subprocess.call([python, "-m", "pip", "install", "-q", "-r", str(requirements)], stdout=f, stderr=f)


def cmd_status(args) -> int:
    out_dir = Path(args.workdir) / "out"
    state = read_json(out_dir / "driver_state.json")
    if not state:
        print("상태 파일이 없습니다(아직 실행하지 않았습니다).")
        return EXIT_OK
    progress = read_json(out_dir / "progress.json", {"units": {}})
    pid = read_json(out_dir / "driver.pid")
    summary = {"stages": {s: v.get("status") for s, v in state["stages"].items()},
               "units_done": len(progress.get("units", {})), "wall_hours": round(wall_seconds(state) / 3600, 2),
               "failed": (out_dir / "FAILED").exists(), "code_commit": state["code"]["commit"],
               "detached_running": bool(pid and _pid_alive(pid["pid"]))}
    print(json.dumps(summary, ensure_ascii=False))
    log_path = out_dir / "run.log"
    if log_path.exists():
        print("".join(log_path.read_text(encoding="utf-8").splitlines(keepends=True)[-args.tail:]), end="")
    return EXIT_OK


def cmd_cleanup(args) -> int:
    """데이터(와 --all이면 산출물·코드)를 지우고, 지운 뒤 남은 파일 목록을 출력한다."""
    workdir = Path(args.workdir)
    for name in ("data",) + (("out", "repo") if args.all else ()):
        shutil.rmtree(workdir / name, ignore_errors=True)
    left = sorted(str(p.relative_to(workdir)) for p in workdir.rglob("*")) if workdir.exists() else []
    print(json.dumps({"removed": ["data"] + (["out", "repo"] if args.all else []), "remaining": left},
                     ensure_ascii=False))
    return EXIT_OK


def cmd_manifest(args) -> int:
    manifest = build_manifest(Path(args.root), args.dataset, Path(args.articles_meta))
    write_json(Path(args.out), manifest)
    print(json.dumps({"out": args.out, "files": len(manifest["files"]),
                      "bytes": sum(v["bytes"] for v in manifest["files"].values())}))
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """인자 오류를 낼 때 URL을 되찍지 않는 파서. argparse는 인식하지 못한 인자나 잘못된 값을 오류 메시지에 그대로 넣는데,
    `--url`이나 `키=`를 빼먹은 명령에서는 그 값이 서명 URL이다. 하위 명령의 파서도 이 클래스로 만들어진다."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {scrub_urls(message)}\n")


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(description="EB-NeRD v1.2 콜드 regime 사슬 드라이버")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="tarball·데이터 준비 → 단계 실행 → 리포트")
    run.add_argument("--workdir", required=True)
    run.add_argument("--code-tarball", required=True)
    run.add_argument("--code-sha256", default=None)
    run.add_argument("--manifest", default=None)
    run.add_argument("--url", action="append", default=[], metavar="KEY=URL", help="manifest 키별 서명 URL(출력하지 않음)")
    run.add_argument("--synthetic", action="store_true", help="EB-NeRD 없이 합성 데이터로 경로만 확인(demo 등급)")
    run.add_argument("--threads", type=int, default=2)
    run.add_argument("--cu-rate", type=float, default=None,
                     help="런타임의 CU/h(드라이 런에서 잰 값). --synthetic이 아니면 필수이고, 0은 예산 규칙을 끈다는 명시")
    run.add_argument("--cu-cap", type=float, default=CU_CAP)
    run.add_argument("--cu-warn", type=float, default=CU_WARN)
    run.add_argument("--cu-before", type=float, default=None, help="실행 전 잔액(기록용)")
    run.add_argument("--runtime-label", default=None, help="기록용 런타임 이름(생략하면 자동 판별)")
    run.add_argument("--prereg-commit", default=None, help="사전 등록을 담은 커밋 SHA(리포트 머리말에 적힌다)")
    run.add_argument("--python", default=None)
    run.add_argument("--skip-install", action="store_true")
    run.add_argument("--fresh", action="store_true", help="이전 산출물·데이터를 지우고 처음부터")
    run.add_argument("--allow-unregistered-data", action="store_true",
                     help="사전 등록과 다른 입력도 받는다(run_cold가 결과를 demo 등급으로 표기한다)")
    run.add_argument("--detach", action="store_true", help="새 세션의 자식 프로세스로 띄우고 바로 돌아온다")
    sp = sub.add_parser("stop", help="분리 실행을 끝낸다(FAILED 마커 없음)")
    sp.add_argument("--workdir", required=True)
    sp.add_argument("--wait", type=float, default=10.0)
    st = sub.add_parser("status", help="단계 상태와 로그 끝부분")
    st.add_argument("--workdir", required=True)
    st.add_argument("--tail", type=int, default=20)
    cl = sub.add_parser("cleanup", help="데이터 삭제 후 남은 파일 목록 출력")
    cl.add_argument("--workdir", required=True)
    cl.add_argument("--all", action="store_true", help="산출물과 코드도 삭제(리포트를 내려받은 뒤에)")
    mf = sub.add_parser("manifest", help="로컬 파일의 sha256으로 manifest 작성")
    mf.add_argument("--root", required=True)
    mf.add_argument("--dataset", default="ebnerd_small")
    mf.add_argument("--articles-meta", required=True)
    mf.add_argument("--out", required=True)
    return ap


def main(argv=None, **hooks) -> int:
    if argv is None:
        raw = os.environ.get("M4_DRIVER_ARGS_JSON")
        argv = json.loads(raw) if raw else sys.argv[1:]
    args = build_parser().parse_args(argv)
    args.raw_argv = list(argv)
    if args.command == "run":
        return cmd_run(args, **hooks)
    return {"status": cmd_status, "cleanup": cmd_cleanup, "manifest": cmd_manifest, "stop": cmd_stop}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
