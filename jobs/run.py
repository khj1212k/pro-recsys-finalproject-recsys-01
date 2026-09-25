"""`python -m jobs.run <job> [옵션]` - 스케줄러(supercronic/Airflow)가 호출하는 유일한 진입점.

종료 코드: 0 = 성공 또는 정상 건너뜀(락 점유, 킬 스위치 등), 1 = 실패, 2 = 잘못된 인자.
"""
import argparse
import importlib
import json
import logging
import signal
import sys
from typing import List, Optional

from jobs import setup_import_paths
from jobs.runtime import JobTerminated, resolve_git_sha, run_job

setup_import_paths()

# 잡 이름 -> 모듈. 모듈은 run(ctx)와 선택적으로 add_arguments(parser)를 제공한다.
# 무거운 의존성(torch 등)은 각 모듈의 run() 안에서만 임포트한다 - 인자 파싱을 위해
# 모든 모듈을 임포트하므로.
JOBS = {
    "ingest": "jobs.tasks.ingest",
    "cluster": "jobs.tasks.cluster",
    "generate": "jobs.tasks.generate",
    "popularity": "jobs.tasks.popularity",
    "user_embed": "jobs.tasks.user_embed",
    "train": "jobs.tasks.train",
    "batch_fallback": "jobs.tasks.batch_fallback",
    "daily_report": "jobs.tasks.daily_report",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jobs.run", description=__doc__)
    sub = parser.add_subparsers(dest="job", required=True, metavar="job")
    for name, module_path in JOBS.items():
        module = importlib.import_module(module_path)
        job_parser = sub.add_parser(name, help=(module.__doc__ or "").strip().splitlines()[0])
        if hasattr(module, "add_arguments"):
            module.add_arguments(job_parser)
    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _raise_terminated(signum, frame):
    raise JobTerminated(signum)


def main(argv: Optional[List[str]] = None) -> int:
    _configure_logging()
    # 기본 SIGTERM 처리는 실행 기록 없이 프로세스를 끝낸다(컨테이너 PID 1이면 아예 무시된다) -
    # 예외로 바꿔 run_job이 중단을 job_runs에 남기고 advisory lock을 풀게 한다.
    signal.signal(signal.SIGTERM, _raise_terminated)
    signal.signal(signal.SIGINT, _raise_terminated)
    args = build_parser().parse_args(argv)
    module = importlib.import_module(JOBS[args.job])

    from jobs.store import PostgresJobRunStore

    result = run_job(
        args.job,
        module.run,
        args=args,
        store_factory=lambda: PostgresJobRunStore.connect(args.job),
        git_sha=resolve_git_sha(),
    )
    # 한 줄 JSON 요약 - supercronic/docker 로그에서 grep하기 쉽게
    print(json.dumps(
        {"job": args.job, "run_id": result.run_id, "status": result.status, "stats": result.stats},
        ensure_ascii=False, default=str,
    ))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
