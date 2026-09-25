import os

# backend/app/database.py는 임포트 시점에 create_engine(DATABASE_URL, ...)을 호출한다.
# SQLAlchemy의 create_engine은 실제 연결을 맺지 않고 URL만 파싱하므로, 이 워크트리에
# 실제 DB가 없어도 문법적으로 유효한 더미 URL만 있으면 backend 모듈 임포트가 가능하다.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")
