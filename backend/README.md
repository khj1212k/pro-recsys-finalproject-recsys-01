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
  - **recsys/**: GET /newsletters/today의 요청 시점 추천(후보·스코어·MMR·폴백, docs/adr/0015)
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

# 요청 시점 추천(app/recsys)이 재사용하는 MMR 모듈(recommend_engine의 src.core, numpy만 사용)
# - 의존성(pandas/scikit-learn/lightgbm)은 받지 않고 패키지 경로만 등록한다
pip install --no-deps -e ../ai_workspace/recommend_engine
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
# 저장소 루트의 recsys_core(오프라인 하네스와 같이 쓰는 피처 코어, ADR 0033)를 임포트할 수 있어야 한다.
# API 이미지는 그 디렉터리를 복사해 넣는다(docker/api.Dockerfile). 저장소에서 직접 띄울 때는 경로를 준다.
PYTHONPATH=.. uvicorn app.main:app --reload
```

`requirements.txt`의 lightgbm은 레지스트리에 등록된 랭커를 채점할 때만 임포트된다. macOS에서 그 임포트가
`libomp`를 찾지 못하면 `brew install libomp`가 필요하다(이미지에는 `libgomp1`이 들어 있다).

`http://localhost:8000/docs`에서 API 문서 확인 가능합니다.
