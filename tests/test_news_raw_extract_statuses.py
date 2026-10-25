# news_raw.raw_news_extract_status의 CHECK 제약(ck_news_raw_extract_status)에 들어 있는 값과, 코드·SQL이 쓰는 값이 어긋나지 않는지
# DB 없이 확인한다. 어긋나면 그 상태로 INSERT/UPDATE하는 코드가 운영 DB에서 CheckViolation으로 실패한다 - 동아일보 요약 수집이
# 제약에 없는 'rss_summary'를 써서 매 회차 0건이 될 뻔했다(ADR 0026 2026-10-25). 실제 DB에서의 확인은
# tests/integration/test_extract_status_values_migration.py와 test_news_raw_unique_conflict.py가 한다.
import importlib.util
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(REPO_ROOT, "ai_workspace"), os.path.join(REPO_ROOT, "backend")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.models.news import NEWS_RAW_EXTRACT_STATUSES  # noqa: E402
from crawler.content_extractor.extractor import _RECORDED_STATUSES  # noqa: E402
from config.retention import RETRYABLE_EXTRACT_STATUSES  # noqa: E402
from crawler.rss_collector import RSS_SUMMARY_STATUS  # noqa: E402

VERSIONS = os.path.join(REPO_ROOT, "backend", "alembic", "versions")


def _load_migration(filename_prefix):
    (name,) = [f for f in os.listdir(VERSIONS) if f.startswith(filename_prefix) and f.endswith(".py")]
    spec = importlib.util.spec_from_file_location(f"_mig_{filename_prefix}", os.path.join(VERSIONS, name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_check_constraint_and_model_list_the_same_statuses():
    migration = _load_migration("b7e4a1c93d52")
    assert tuple(migration.EXTRACT_STATUSES) == tuple(NEWS_RAW_EXTRACT_STATUSES)


def test_downgrade_target_is_the_previous_revisions_status_list():
    previous = _load_migration("d48994e9d26e")  # f1db3221e45f는 이 목록을 바꾸지 않았다
    assert tuple(_load_migration("b7e4a1c93d52").OLD_EXTRACT_STATUSES) == tuple(previous.EXTRACT_STATUSES)


def test_every_status_the_code_writes_is_allowed_by_the_constraint():
    allowed = set(NEWS_RAW_EXTRACT_STATUSES)
    assert set(_RECORDED_STATUSES) <= allowed       # 본문 추출기가 쓰는 값
    assert RSS_SUMMARY_STATUS in allowed            # 수집기가 요약 행에 쓰는 값
    assert {"dropped", "duplicate", "blocked_origin"} <= allowed  # 수집기(요약 거르기·중복)와 close SQL이 쓰는 값


def test_close_sql_only_writes_allowed_statuses():
    path = os.path.join(REPO_ROOT, "scripts", "oci", "close_donga_pending.sql")
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    written = set(re.findall(r"raw_news_extract_status\s*=\s*'([a-z_]+)'", sql))
    assert "blocked_origin" in written and written <= set(NEWS_RAW_EXTRACT_STATUSES)


def test_close_sql_pending_statuses_match_the_extractors_retryable_statuses():
    # close SQL의 "추출 대기" 조건에 옮겨 적은 ('fetch_failed', 'error')가 추출기·보존 잡이 쓰는 목록과 같아야 한다 -
    # 어긋나면 추출기가 아직 집는 행을 닫지 못하거나 끝난 행을 닫는다(scripts/oci/close_donga_pending.sql 머리말).
    path = os.path.join(REPO_ROOT, "scripts", "oci", "close_donga_pending.sql")
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    lists = re.findall(r"raw_news_extract_status IN \(([^)]*)\)", sql)
    assert lists, "close SQL에서 재시도 가능한 상태 목록을 찾지 못했다"
    for items in lists:
        assert set(re.findall(r"'([a-z_]+)'", items)) == set(RETRYABLE_EXTRACT_STATUSES)
