#!/usr/bin/env bash
# Airflow 메타데이터 DB를 앱 DB와 같은 Postgres 서버의 별도 데이터베이스로 만들고
# (없을 때만) standalone(api-server + scheduler + dag-processor + triggerer)을 띄운다.
set -euo pipefail

python - <<'PY'
import os
import psycopg2

name = os.environ["AIRFLOW_METADATA_DB"]
conn = psycopg2.connect(
    host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"], dbname=os.environ["DB_NAME"],
)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    if cur.fetchone() is None:
        cur.execute(f'CREATE DATABASE "{name}"')
conn.close()
PY

exec /entrypoint airflow standalone
