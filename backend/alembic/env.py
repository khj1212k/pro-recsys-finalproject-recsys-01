# backend/alembic/env.py
from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context
import os, sys
from dotenv import load_dotenv # [필수] .env 읽기용

# ----------------------------------------------------------------
# 1. .env 파일 로드 (환경변수 읽기)
# ----------------------------------------------------------------
load_dotenv() 

# 2. backend 폴더 경로 추가 (app.models를 찾기 위해)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 3. SQLModel 메타데이터 가져오기
from app.models import SQLModel
target_metadata = SQLModel.metadata

# 4. Alembic 설정 객체 가져오기
config = context.config

# ----------------------------------------------------------------
# [핵심] 5. .env에 있는 'DATABASE_URL'로 설정을 덮어치기
# ----------------------------------------------------------------
database_url = os.getenv("DATABASE_URL")

# 만약 .env에 주소가 있다면, alembic.ini에 있는 가짜 주소를 무시하고 이걸 사용함
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# ----------------------------------------------------------------
# 6. 로그 설정 (기존 로직 유지)
# ----------------------------------------------------------------
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ----------------------------------------------------------------
# 7. 마이그레이션 실행 로직 (건드리지 않음)
# ----------------------------------------------------------------
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()