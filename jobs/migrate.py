"""`python -m jobs.migrate` - compose의 migrate 서비스(1회성)가 실행한다.

1) vector extension 보장 2) alembic upgrade head 3) 참조 데이터(언론사/카테고리/RSS URI)
시드. 세 단계 모두 재실행 안전하다(IF NOT EXISTS / alembic 버전 테이블 / 존재 확인 후 삽입).
job_runs 테이블 자체가 이 단계에서 만들어지므로 jobs.run 경로(실행 기록)를 타지 않는다.
"""
import sys

from jobs import REPO_ROOT, setup_import_paths

setup_import_paths()


def ensure_vector_extension() -> None:
    from db.connection import connect_unpooled

    conn = connect_unpooled(application_name="jobs.migrate")
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        conn.close()


def upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    command.upgrade(cfg, "head")


def seed_reference_data() -> None:
    from db.schema import (
        insert_initial_category_data,
        insert_initial_press_data,
        insert_initial_rss_url_data,
    )

    insert_initial_press_data()
    insert_initial_category_data()
    insert_initial_rss_url_data()


def main() -> int:
    # print를 쓰는 이유: backend/alembic/env.py의 fileConfig()가 기존 로거를 비활성화한다.
    print("[migrate] vector extension 확인", flush=True)
    ensure_vector_extension()
    print("[migrate] alembic upgrade head", flush=True)
    upgrade_head()
    print("[migrate] 참조 데이터 시드", flush=True)
    seed_reference_data()
    print("[migrate] 완료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
