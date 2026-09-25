import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AI_WORKSPACE = os.path.join(REPO_ROOT, "ai_workspace")
if AI_WORKSPACE not in sys.path:
    sys.path.insert(0, AI_WORKSPACE)

_HERE = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
    """tests/integration/ 아래 모든 테스트에 integration 마커를 자동으로 붙인다.

    이렇게 하면 로컬 기본 실행(`pytest -m "not integration and not benchmark"`)에서는
    자동으로 제외되고, CI의 integration-test 잡(`pytest -m integration`)에서만
    선택된다 - 테스트 파일마다 데코레이터를 붙이는 걸 잊어서 둘 중 하나가
    깨지는 실수를 막는다.
    """
    for item in items:
        if str(item.path).startswith(_HERE):
            item.add_marker(pytest.mark.integration)


def _resolve_database_url():
    return os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


def _dsn_params(url):
    from psycopg2.extensions import parse_dsn

    return parse_dsn(url)


def require_test_database_name(url):
    """DB 이름에 'test'가 없으면 실패시킨다.

    jobs.run 잡(ingest/embed/cluster)은 news_raw 전체를 대상으로 돈다 - 운영 수집 DB에서
    돌리면 가짜 임베딩이 실제 기사에 저장되고(embedding_result IS NULL만 다시 고르므로 영영
    재임베딩되지 않는다) 대기 중인 기사의 추출 재시도 횟수가 소진된다. 스킵이 아니라 실패인
    이유: 잘못된 설정을 조용히 넘기지 않기 위해서다.
    """
    dbname = _dsn_params(url).get("dbname") or ""
    if "test" not in dbname.lower():
        pytest.fail(
            f"integration 테스트 DB 이름에 'test'가 없습니다 (dbname={dbname!r}). "
            "운영/수집 DB를 가리키고 있을 수 있어 실행하지 않습니다 - "
            "TEST_DATABASE_URL에 전용 테스트 DB(예: .../newsletter_test)를 지정하세요.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def database_url():
    """실제 Postgres(+pgvector) 테스트 DB가 준비된 환경에서만 값을 반환한다.

    TEST_DATABASE_URL/DATABASE_URL이 없으면(로컬 기본 상태) 스킵한다 - CI의
    integration-test 잡은 서비스 컨테이너(newsletter_test)를, 로컬에서는 docs/runbook.md의
    일회용 테스트 컨테이너를 가리킨다. 운영 수집 DB(compose의 db)를 가리키면 안 된다 -
    아래 require_test_database_name이 DB 이름으로 막는다.

    tests/conftest.py는 backend 모듈 임포트가 실제 DB 없이도 되게 하려고
    DATABASE_URL에 접속 불가능한 더미 값("localhost:5432/testdb")을
    setdefault로 채워둔다 - 그래서 "환경변수가 설정돼 있는지"만으로는 실제 DB가
    있는지 판단할 수 없다. 값이 있으면 실제로 짧게 접속을 시도해보고, 접속
    실패(OperationalError)면 - 더미 값이든 진짜 DB가 잠깐 죽어있든 - 에러로
    죽이는 대신 스킵으로 처리한다.
    """
    url = _resolve_database_url()
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL 또는 DATABASE_URL이 설정되어 있지 않아 "
            "DB integration 테스트를 스킵합니다 (실제 Postgres+pgvector 필요 - "
            "CI의 integration-test 잡에서만 값이 채워짐)."
        )

    import psycopg2

    try:
        probe = psycopg2.connect(url, connect_timeout=3)
        probe.close()
    except psycopg2.OperationalError as e:
        pytest.skip(
            "TEST_DATABASE_URL/DATABASE_URL에 접속할 수 없어 DB integration 테스트를 "
            f"스킵합니다 (로컬에는 실제 Postgres가 없음): {e}"
        )
    require_test_database_name(url)

    # 테스트 셋업(pg_conn)은 url로, 코드 경로(db.connection 풀, jobs.run)는 Settings.DB_*로
    # 접속한다. 둘이 다른 DB(예: 환경변수/.env의 운영 DB)를 가리키지 않게 Settings를 url에 맞춘다.
    from config.settings import Settings
    from db import connection

    params = _dsn_params(url)
    patcher = pytest.MonkeyPatch()
    patcher.setattr(Settings, "DB_HOST", params.get("host") or "localhost")
    patcher.setattr(Settings, "DB_PORT", str(params.get("port") or "5432"))
    patcher.setattr(Settings, "DB_USER", params.get("user") or "")
    patcher.setattr(Settings, "DB_PASSWORD", params.get("password") or "")
    patcher.setattr(Settings, "DB_NAME", params["dbname"])
    connection.close_pool()
    try:
        yield url
    finally:
        connection.close_pool()
        patcher.undo()


@pytest.fixture
def pg_conn(database_url):
    """db.connection의 풀/어댑터 등록 로직과 별개인, 테스트 셋업·검증 전용 raw
    psycopg2 연결. autocommit이라 각 테스트가 자신이 만든 행만 직접 정리하면 된다."""
    import psycopg2

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def isolated_news_raw(pg_conn):
    """news_raw 전체를 대상으로 도는 잡을 부르기 전에, 테이블에 이 테스트가 만들지 않은 행이
    없는지 확인한다. 행이 있으면 공유/운영 DB일 수 있으므로 실패시킨다(require_test_database_name과
    함께 이중 안전장치)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_raw")
        (existing,) = cur.fetchone()
    if existing:
        pytest.fail(
            f"news_raw에 이 테스트가 만들지 않은 행이 {existing}건 있습니다 - 전체 테이블을 도는 "
            "잡(ingest/embed/cluster)을 이 DB에서 실행하지 않습니다. 빈 전용 테스트 DB를 쓰세요.",
            pytrace=False,
        )
