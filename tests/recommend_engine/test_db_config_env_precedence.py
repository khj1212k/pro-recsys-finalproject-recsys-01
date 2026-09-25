import os


def test_resolve_db_config_prefers_env_vars_over_yaml(monkeypatch):
    from src.data.data_loader import resolve_db_config

    yaml_conf = {"host": "yaml-host", "port": 5432, "user": "yaml-user", "password": "yaml-pass", "dbname": "yaml-db"}

    monkeypatch.setenv("DB_HOST", "env-host")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_USER", "env-user")
    monkeypatch.setenv("DB_PASSWORD", "env-pass")
    monkeypatch.setenv("DB_NAME", "env-db")

    resolved = resolve_db_config(yaml_conf)

    assert resolved == {
        "host": "env-host", "port": "6543", "user": "env-user",
        "password": "env-pass", "dbname": "env-db",
    }


def test_resolve_db_config_falls_back_to_yaml_when_no_env_vars(monkeypatch):
    from src.data.data_loader import resolve_db_config

    yaml_conf = {"host": "yaml-host", "port": 5432, "user": "yaml-user", "password": "yaml-pass", "dbname": "yaml-db"}

    for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(key, raising=False)

    resolved = resolve_db_config(yaml_conf)

    assert resolved["host"] == "yaml-host"
    assert resolved["port"] == "5432"
    assert resolved["user"] == "yaml-user"
    assert resolved["dbname"] == "yaml-db"


def test_resolve_db_config_falls_back_to_hardcoded_defaults_when_yaml_conf_missing(monkeypatch):
    from src.data.data_loader import resolve_db_config

    for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(key, raising=False)

    resolved = resolve_db_config(None)

    assert resolved["host"] == "localhost"
    assert resolved["port"] == "5432"
    assert resolved["password"] == ""


def test_config_yaml_no_longer_has_plaintext_password():
    import yaml

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "ai_workspace", "recommend_engine", "config", "config.yaml",
    )
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    assert config["database"]["password"] == ""
