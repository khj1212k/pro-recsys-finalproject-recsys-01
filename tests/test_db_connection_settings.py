import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def test_build_db_config_reads_from_settings_not_raw_env():
    from db.connection import _build_db_config
    from config.settings import Settings

    with patch.object(Settings, "DB_HOST", "custom-host"), \
         patch.object(Settings, "DB_PORT", "6543"), \
         patch.object(Settings, "DB_USER", "custom-user"), \
         patch.object(Settings, "DB_PASSWORD", "custom-pass"), \
         patch.object(Settings, "DB_NAME", "custom-db"):
        config = _build_db_config()

    assert config["host"] == "custom-host"
    assert config["port"] == "6543"
    assert config["user"] == "custom-user"
    assert config["password"] == "custom-pass"
    assert config["dbname"] == "custom-db"


def test_initialize_pool_uses_build_db_config():
    from db.connection import DatabasePool

    # 싱글턴 상태를 이번 테스트 전용으로 리셋
    DatabasePool._instance = None
    DatabasePool._pool = None

    fake_pool_cls = MagicMock()
    with patch("db.connection.pool.ThreadedConnectionPool", fake_pool_cls), \
         patch("db.connection._build_db_config", return_value={"host": "h", "port": "p", "user": "u", "password": "pw", "dbname": "d", "options": "-c client_encoding=UTF8"}) as mock_build:
        db_pool = DatabasePool()

    assert mock_build.called
    _, kwargs = fake_pool_cls.call_args
    assert kwargs["host"] == "h"
    assert kwargs["dbname"] == "d"

    # 다음 테스트에 영향 주지 않도록 정리
    DatabasePool._instance = None
    DatabasePool._pool = None
