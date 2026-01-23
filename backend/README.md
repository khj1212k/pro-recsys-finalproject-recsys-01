### Backend

FastAPI 기반의 백엔드 서버입니다. API 제공 및 데이터베이스 관리를 담당합니다. 

#### 폴더 구조 및 역할

- **venv/**: 백엔드 전용 Python 가상환경 폴더.
- **main.py**: FastAPI 애플리케이션의 진입점(Entry point). 서버 실행 파일.
- **alembic/**: DB 마이그레이션 도구. 테이블 생성 및 스키마 변경 관리 담당.

- **app/**
  - **models/**: ★ DB 스키마 정의 (User, News, Rank 등 ORM 모델).
  - **api/**: REST API 라우터 및 핸들러 정의.
  - **crud/**: DB CRUD(Create, Read, Update, Delete) 쿼리 함수 모음.
- **scheduler/**: 오래된 데이터 삭제, 정리 등 주기적인 작업을 수행하는 스크립트.
