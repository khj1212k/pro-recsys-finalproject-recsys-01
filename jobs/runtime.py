"""잡 1회 실행의 공통 뼈대: advisory lock -> job_runs 기록 -> 실행 -> 결과 기록 -> 알림.

store(jobs/store.py::PostgresJobRunStore)는 주입받는다 - 단위 테스트는 인메모리
store로 상태 전이만 검증하고, 실제 락/행 동작은 integration 테스트가 검증한다.
"""
import argparse
import hashlib
import logging
import os
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from jobs import REPO_ROOT
from jobs.notify import send_alert

logger = logging.getLogger("jobs")

_ERROR_TEXT_LIMIT = 8000


class JobSkipped(Exception):
    """잡이 '할 일이 없어서/막혀 있어서' 정상적으로 건너뛸 때 던진다(실패로 치지 않음)."""

    def __init__(self, reason: str, stats: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.stats = stats or {}


@dataclass
class JobContext:
    job: str
    args: argparse.Namespace
    run_id: Optional[int] = None
    # 잡이 진행하면서 채운다. 중간에 예외가 나도 여기까지 채운 값은 job_runs에 남는다.
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobResult:
    status: str
    exit_code: int
    run_id: Optional[int]
    stats: Dict[str, Any]
    error: Optional[str] = None


def advisory_lock_key(job: str) -> int:
    # hashtext()는 Postgres 버전 간 안정성이 보장되지 않아 키를 파이썬에서 고정 계산한다.
    digest = hashlib.blake2b(f"jobs.run:{job}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def resolve_git_sha() -> Optional[str]:
    sha = os.getenv("GIT_SHA", "").strip()
    if sha and sha != "unknown":
        return sha[:40]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()[:40] or None


def to_jsonable(obj: Any) -> Any:
    """stats를 JSONB로 저장할 수 있게 numpy 스칼라/배열, datetime 등을 기본 타입으로 바꾼다."""
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if hasattr(obj, "tolist"):  # numpy ndarray / numpy scalar
        return to_jsonable(obj.tolist())
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def run_job(
    job: str,
    fn: Callable[[JobContext], Optional[Dict[str, Any]]],
    *,
    args: argparse.Namespace,
    store_factory: Callable[[], Any],
    notify: Callable[[str], None] = send_alert,
    git_sha: Optional[str] = None,
) -> JobResult:
    try:
        store = store_factory()
    except Exception as e:
        message = f"🚨 job 실패: job={job}, DB 연결 불가로 실행 기록 없이 중단 - {_short(e)}"
        notify(message)
        return JobResult(status="failed", exit_code=1, run_id=None, stats={}, error=str(e))

    try:
        return _run_with_store(job, fn, args, store, notify, git_sha)
    finally:
        try:
            store.close()
        except Exception:
            logger.exception("job_runs store 종료 실패")


def _run_with_store(job, fn, args, store, notify, git_sha) -> JobResult:
    try:
        acquired = store.try_lock(job)
    except Exception as e:
        notify(f"🚨 job 실패: job={job}, advisory lock 조회 실패 - {_short(e)}")
        return JobResult(status="failed", exit_code=1, run_id=None, stats={}, error=str(e))

    if not acquired:
        # 이전 실행(예: 첫 백로그 임베딩)이 아직 도는 중 - 겹쳐 돌리지 않고 기록만 남긴다.
        stats = {"reason": "lock_held"}
        logger.warning(f"job={job}: 다른 실행이 advisory lock을 잡고 있어 이번 실행은 건너뜁니다")
        try:
            run_id = store.record(job, "skipped", stats, git_sha)
        except Exception:
            logger.exception("skipped 실행 기록 실패")
            run_id = None
        return JobResult(status="skipped", exit_code=0, run_id=run_id, stats=stats)

    ctx = JobContext(job=job, args=args)
    try:
        # 락을 잡았다면 같은 잡의 다른 실행은 없다 - 남아 있는 'running' 행은 프로세스가
        # 죽어(OOM, 컨테이너 재시작) 끝을 기록하지 못한 실행이다.
        abandoned = store.abandon_stale(job)
        ctx.run_id = store.start(job, git_sha)
        if abandoned:
            ctx.stats["abandoned_previous_runs"] = abandoned
        return _execute(job, fn, ctx, store, notify)
    except Exception as e:
        # job_runs 기록 자체가 실패한 경우(DB가 도중에 내려감 등)
        logger.exception(f"job={job}: 실행 기록 실패")
        notify(f"🚨 job 실패: job={job}, run_id={ctx.run_id}, 실행 기록 실패 - {_short(e)}")
        return JobResult(status="failed", exit_code=1, run_id=ctx.run_id, stats=ctx.stats, error=str(e))
    finally:
        try:
            store.unlock(job)
        except Exception:
            logger.exception(f"job={job}: advisory lock 해제 실패 (세션 종료 시 자동 해제됨)")


def _execute(job, fn, ctx: JobContext, store, notify) -> JobResult:
    started = time.monotonic()
    error = None
    try:
        returned = fn(ctx)
        if returned:
            ctx.stats.update(returned)
        status = "succeeded"
    except JobSkipped as skip:
        ctx.stats.update(skip.stats)
        ctx.stats["reason"] = skip.reason
        status = "skipped"
        logger.warning(f"job={job}: 건너뜀 ({skip.reason})")
    except Exception as e:
        status = "failed"
        error = traceback.format_exc()[-_ERROR_TEXT_LIMIT:]
        logger.exception(f"job={job}: 실패")
        failure_summary = _short(f"{type(e).__name__}: {e}")
    ctx.stats["duration_s"] = round(time.monotonic() - started, 3)

    stats = to_jsonable(ctx.stats)
    store.finish(ctx.run_id, status, stats, error)

    if status == "failed":
        notify(f"🚨 job 실패: job={job}, run_id={ctx.run_id}, error={failure_summary}")
        return JobResult(status=status, exit_code=1, run_id=ctx.run_id, stats=stats, error=error)
    return JobResult(status=status, exit_code=0, run_id=ctx.run_id, stats=stats, error=None)
