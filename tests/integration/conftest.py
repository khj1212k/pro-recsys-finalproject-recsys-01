import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AI_WORKSPACE = os.path.join(REPO_ROOT, "ai_workspace")
BACKEND = os.path.join(REPO_ROOT, "backend")
for _path in (AI_WORKSPACE, BACKEND):
    if _path not in sys.path:
        sys.path.insert(0, _path)

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


@pytest.fixture(scope="session")
def database_url():
    """실제 Postgres(+pgvector)가 준비된 환경(CI의 서비스 컨테이너)에서만 값을 반환한다.

    이 워크트리가 돌아가는 macOS에는 Postgres/Docker가 설치되어 있지 않으므로,
    TEST_DATABASE_URL/DATABASE_URL이 없으면(로컬 기본 상태) 스킵한다 -
    `.github/workflows/ci.yml`의 integration-test 잡만 이 값을 채워서 실행한다.

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
    return url


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
