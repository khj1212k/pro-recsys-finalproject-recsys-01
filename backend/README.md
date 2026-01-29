## Backend
- FastAPI 기반의 백엔드 서버
- API 제공 및 데이터베이스 관리

### 폴더 구조 및 역할

- **venv/**: 백엔드 전용 Python 가상환경 폴더
- **app/**
  - **main.py**: FastAPI 애플리케이션 Entry point
  - **models/**: DB 스키마 정의
  - **api/**: REST API 라우터 End point 정의
  - **crud/**: DB CRUD(Create, Read, Update, Delete) 쿼리 함수
- **alembic/**: DB 마이그레이션
- **scheduler/**: 주기적인 배치 작업

---

### 시작하기

#### 1. 가상환경 설정 및 패키지 설치
```bash
# 가상환경 생성
python -m venv .venv

# 가상환경 활성화
source .venv/bin/activate

# 의존성 패키지 설치
pip install -r requirements.txt
```

#### 2. 환경 변수 설정 (.env)
`backend/.env` 파일을 생성하고 아래 환경 변수 설정 추가
```ini
DATABASE_URL=postgresql://user:password@localhost:5432/dbname
SECRET_KEY=your_secret_key
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
UPSTAGE_API_KEY=your_upstage_api_key
```

#### 3. 서버 실행
```bash
uvicorn app.main:app --reload
```

`http://localhost:8000/docs`에서 API 문서 확인 가능합니다.
