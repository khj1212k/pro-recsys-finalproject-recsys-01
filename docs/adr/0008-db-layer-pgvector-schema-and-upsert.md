# ADR 0008: pgvector 어댑터 등록, news_raw 스키마 정합성, RSS 수집기 UPSERT

## 상태
채택됨 (2026-09-25)

## 컨텍스트
- `ai_workspace/db/connection.py`의 커넥션 풀/직접 연결 어디에서도
  `pgvector.psycopg2.register_vector`를 등록하지 않았다. 그 결과 raw-SQL로
  `vector` 컬럼을 읽으면(예: `ai_workspace/core/user_embedder.py`) 값이
  문자열로 오고, `np.array(<str>)`가 문자열 자체를 감싼 쓸모없는 배열을
  만들어 깨진다.
- `recommend_engine`(`ai_workspace/recommend_engine`)은 `ai_workspace`와
  별개로 설치 가능한 서브 프로젝트로, SQLAlchemy로 자체 연결을 맺는다.
  `DataLoader._parse_pgvector`는 값이 항상 문자열로 온다고 가정했다.
- `news_raw.raw_news_url`에 UNIQUE 제약이 없어 재수집 시 같은 기사가 중복
  적재될 수 있었고, `ai_workspace/crawler/rss_collector.py`는 INSERT 전에
  `SELECT ... WHERE raw_news_url IN %s`로 존재 여부를 확인하는 TOCTOU적
  패턴을 썼다(조회와 삽입 사이에 다른 프로세스가 같은 URL을 먼저 넣으면
  UniqueViolation 가능).
- `news_raw.raw_news_created_at`이 VARCHAR였다. `backend/app/models/news.py`의
  `NewsRaw.raw_news_created_at: str` 타입도 이를 그대로 반영하고 있었다.
- `user_newsletter_ctr_log`에 `(user_id, created_at DESC)` 인덱스가 없어
  "유저의 최근 클릭 로그" 조회(`ai_workspace/core/user_embedder.py`의
  `UserEmbedder.batch_update_all_users`, `WHERE user_id IN %s AND
  created_at >= %s`; `ai_workspace/db/user_log_queries.py`의
  `get_user_read_history` 등)가 전체 스캔에 의존했다. `recommend_engine`의
  `DataLoader.load_ctr_logs`는 `user_id`로 필터링하지 않는 시간 범위 전체
  스캔(`WHERE created_at >= NOW() - INTERVAL ...`)이라 이 인덱스의 대상이
  아니다.
- 이 Mac 워크트리에는 Postgres/Docker가 없어 로컬에서 실제 DB로 검증할 수
  없다. 통합 테스트는 GitHub Actions 서비스 컨테이너에서만 실행 가능해야
  하고, 로컬에서는 `TEST_DATABASE_URL`/`DATABASE_URL`이 없으면(또는 접속이
  안 되면) 스스로 스킵해야 한다.

## 검토한 대안

### 1. pgvector 어댑터를 언제 등록할 것인가
1. 커넥션 풀 초기화 시(`_initialize_pool`)에 미리 생성되는 idle 커넥션에만
   등록한다.
   - `ThreadedConnectionPool`이 `minconn`개를 미리 만들지만, 소진 후 direct
     connection 폴백이나 풀이 나중에 새로 만드는 커넥션까지는 커버하지
     못한다.
2. **`get_connection()`이 커넥션을 내줄 때마다(체크아웃 시점) 등록한다.
   (채택)**
   - 풀에서 받은 커넥션이든 direct fallback이든, 호출자가 실제로 커넥션을
     받는 시점에 항상 등록되어 있음을 보장한다. `register_vector`가
     idempotent해서 이미 등록된 커넥션에 다시 불러도 안전하다.
   - 매 체크아웃마다 `pg_extension` 조회 1번이 추가되지만, 이 프로젝트는
     배치 파이프라인 중심이라 커넥션 체크아웃 빈도가 높지 않다.

### 2. vector extension이 없을 때 실패 방식
1. `register_vector`가 내부적으로 이미 `psycopg2.ProgrammingError('vector
   type not found in the database')`를 던지므로 그냥 호출만 한다.
2. **`pg_extension`을 먼저 조회해 없으면 전용 예외
   (`VectorExtensionMissingError`)로 즉시 실패시킨 뒤 `register_vector`를
   부른다. (채택)**
   - 에러 메시지에 "CREATE EXTENSION vector를 실행하라"는 실행 가능한
     안내를 담을 수 있다.
   - `DatabasePool.get_connection()`에서 `pool.PoolError`만 direct
     connection 폴백을 타게 하고, `VectorExtensionMissingError`는 폴백에
     삼켜지지 않고 그대로 위로 전파되도록 분리했다 - extension 문제를
     "풀이 고갈됐나보다"로 오인해 조용히 넘어가면 안 되기 때문이다.

### 3. register_vector 등록 후 raw-SQL 값의 실제 타입
- 처음에는 "등록하면 psycopg2가 numpy.ndarray를 바로 돌려준다"고 가정하고
  `DataLoader._parse_pgvector`만 ndarray/문자열 두 가지를 처리하도록
  구현했다.
- 로컬에서 `pgvector.psycopg2.register_vector`의 실제 동작을 직접
  확인해보니(`pgvector==0.5.0`), raw cursor는 `numpy.ndarray`가 아니라
  **`pgvector.Vector` 래퍼 객체**를 돌려준다(`Vector.to_numpy()`로 변환
  가능). `np.array(vector_인스턴스)`는 그 값을 감싼 0차원 `dtype=object`
  배열을 만들 뿐이라 - 문자열 케이스보다 더 알아채기 어려운 방식으로 깨진다.
- **결정: `_parse_pgvector`와 `user_embedder.py`의 벡터 파싱 둘 다
  `to_numpy` 속성이 있는 객체(duck-typing)를 별도 케이스로 처리하도록
  고쳤다.** `ai_workspace/core/user_embedder.py`에는 이 로직을
  `_to_vector_array()` 헬퍼로 뽑아 `vec_map` 생성부에서 재사용한다.
  `recommend_engine`은 `ai_workspace`와 독립 설치 가능한 서브 프로젝트라
  (pyproject.toml에 pgvector 의존성이 없다) 공용 모듈을 만들지 않고 같은
  로직을 두 곳에 각각 작게 중복시켰다 - `pgvector.Vector`를 직접
  import하지 않고 `hasattr(value, "to_numpy")`로만 감지해 새 의존성을
  추가하지 않는다.

### 4. `raw_news_created_at` VARCHAR -> timestamptz 캐스팅 방식
1. 정규식으로 형식을 먼저 검증하고 통과한 값만 `::timestamptz`로 캐스팅한다.
   - 형식은 맞지만 값 자체가 무효인 경우(예: `2026-13-99 99:99:99+00:00`,
     월/일/시각이 범위를 벗어남)를 걸러내지 못해 `ALTER TABLE ... USING`
     전체가 에러로 실패할 수 있다.
2. **PL/pgSQL 함수(`news_raw_safe_to_timestamptz`)로 감싸
   `EXCEPTION WHEN OTHERS THEN RETURN NULL`로 처리한다. (채택)**
   - 형식 오류든 값 범위 오류든 행 단위로 안전하게 NULL로 떨어뜨린다.
   - 마이그레이션 안에서만 쓰고 컬럼 변환 후 바로 `DROP FUNCTION`해
     스키마에 남기지 않는다.
   - `ai_workspace/crawler/rss_collector.py`를 감사한 결과 이 컬럼을 채우는
     경로는 이 파일 하나뿐이고, 항상 `date_parser.parse()` 후 tz가 없으면
     UTC를 채운 `datetime` 객체를 psycopg2가 자동 변환한 문자열
     (`"YYYY-MM-DD HH:MM:SS[.ffffff]+HH:MM"`)만 써왔다 - 이 형식은 안전하게
     캐스팅되고, 이 파일 밖에서 들어온(또는 향후 다른 경로로 들어올) 값만
     실패 시 NULL로 떨어진다.
   - NOT NULL 제약은 캐스팅 전에 먼저 풀어야 한다(NULL이 되는 값이 있을 수
     있으므로) - `DROP NOT NULL`을 타입 변경보다 먼저 실행했다.

### 5. `news_raw.raw_news_url` 중복 제거 방식
1. 중복 그룹 중 `raw_news_id`가 가장 큰(가장 최근) 행만 남긴다.
2. **가장 작은(가장 먼저 수집된) 행만 남긴다. (채택)**
   - "최초 수집 시점"이 더 의미 있는 메타데이터라고 판단했다(이후 재수집에서
     같은 URL에 더 최신 `raw_news_id`가 생기는 건 재수집일 뿐 "더 정확한
     기사"를 뜻하지 않는다).
   - **한계**: 이 삭제는 downgrade로 되돌릴 수 없다 - 삭제된 행은
     영구적으로 사라진다. downgrade()는 UNIQUE 제약만 제거할 뿐, 삭제된
     행을 복원하지 않는다.

### 6. RSS 수집기의 중복 처리
1. 기존 방식 유지: INSERT 전에 `SELECT raw_news_url ... WHERE raw_news_url
   IN %s`로 존재하는 URL을 조회해 후보에서 제외한다.
   - 조회와 삽입 사이의 TOCTOU 레이스에 취약하다(동시 수집 실행, 또는 같은
     기사가 여러 RSS 피드에 동시에 걸리는 경우).
2. **`INSERT ... ON CONFLICT (raw_news_url) DO NOTHING ... RETURNING
   raw_news_url`을 `psycopg2.extras.execute_values(..., fetch=True)`로
   실행한다. (채택)**
   - DB가 원자적으로 충돌을 처리하므로 레이스가 없다.
   - `RETURNING`으로 실제 삽입된 URL만 돌려받아 언론사(press)별
     SAVEPOINT 안에서 `inserted`/`skipped` 건수를 정확히 집계한다.
   - `collect_rss()`의 반환 타입을 `int`(총 신규 건수)에서
     `Dict[str, int]`(`{"inserted": ..., "skipped": ...}`)로 바꿨다 - 저장소
     안의 두 호출부(`ai_workspace/pipeline/stages.py`의
     `Stage1_RSSCollection.execute`, `ai_workspace/pipeline/runner.py`의
     `_print_summary`)를 함께 갱신했다. 저장소 밖에 이 반환값에 의존하는
     코드는 없다.

### 7. 로컬에서 "실제 DB가 있는지" 판단하는 방법 (통합 테스트)
1. `TEST_DATABASE_URL` 또는 `DATABASE_URL` 환경변수가 설정돼 있으면 실제
   DB가 있다고 본다.
   - 이 저장소의 `tests/conftest.py`가 `backend` 모듈을 DB 없이 임포트할 수
     있도록 `DATABASE_URL`에 접속 불가능한 더미 값
     (`postgresql://test:test@localhost:5432/testdb`)을 이미 `setdefault`로
     채워두고 있다. 그래서 "값이 설정돼 있는지"만으로는 로컬(더미값)과
     CI(진짜 서비스 컨테이너)를 구분할 수 없어, 로컬에서
     `pytest tests/integration/`을 직접 돌리면 스킵이 아니라
     `OperationalError`로 에러가 난다.
2. **값이 있으면 짧게(`connect_timeout=3`) 실제 접속을 시도해보고,
   `OperationalError`면 스킵으로 처리한다. (채택)**
   - 더미 값이든, 진짜 DB가 일시적으로 죽어있든 동일하게 "DB 없음"으로
     취급해 깔끔하게 스킵한다.
   - `tests/integration/conftest.py`의 `pytest_collection_modifyitems`가
     `tests/integration/` 아래 모든 테스트에 `integration` 마커를 자동으로
     붙인다 - 개별 파일이 데코레이터를 깜빡해 로컬 `-m "not integration"`
     필터나 CI의 `-m integration` 선택 중 하나가 조용히 깨지는 걸 막는다.

## 결정
- `ai_workspace/db/connection.py`: `get_connection()`이 커넥션을 내줄 때마다
  `_register_pgvector_adapter()`를 호출해 `pg_extension`을 확인하고(없으면
  `VectorExtensionMissingError`) `register_vector`를 등록한다. `PoolError`만
  direct connection 폴백을 타고, extension 누락 에러는 그대로 전파된다.
- `recommend_engine`의 `DataLoader._parse_pgvector`와
  `ai_workspace/core/user_embedder.py`의 `_to_vector_array()`는 `None`/
  `numpy.ndarray`/`list`·`tuple`/`to_numpy()`가 있는 래퍼(`pgvector.Vector`
  등, duck-typing)/문자열을 모두 안전하게 처리한다.
- 새 Alembic 리비전(`e725a62ffef1`, `c97fb5709513` 다음):
  (a) `news_raw.raw_news_url` 중복 제거(최소 id만 유지) 후 UNIQUE 제약 추가
  (b) `news_raw.raw_news_created_at`을 `NOT NULL` 해제 + PL/pgSQL 안전
  캐스팅 함수로 `timestamptz`로 변환(파싱 실패 시 NULL, 함수는 사용 후
  `DROP`) - `backend/app/models/news.py`의 필드 타입도
  `Optional[datetime]` + `Column(DateTime(timezone=True))`로 갱신
  (c) `user_newsletter_ctr_log(user_id, created_at DESC)` 인덱스 추가
- `ai_workspace/crawler/rss_collector.py`: `INSERT ... ON CONFLICT
  (raw_news_url) DO NOTHING ... RETURNING`(`execute_values(fetch=True)`)으로
  전환, 언론사별 SAVEPOINT 격리는 유지, 반환값을
  `{"inserted": int, "skipped": int}`로 변경.
- `.github/workflows/ci.yml`에 `integration-test` 잡 추가:
  `pgvector/pgvector:pg16` 서비스 컨테이너 -> `CREATE EXTENSION vector` ->
  `alembic upgrade head` -> `alembic downgrade -1 && upgrade head`(왕복
  검증) -> `pytest -m integration`.
- `tests/integration/`: `conftest.py`가 `integration` 마커 자동 부여 +
  DB 접속 가능 여부에 따른 스킵을 전담하고, 4개 테스트 파일이 pgvector
  왕복(+코사인 거리), UNIQUE/ON CONFLICT, timestamptz 캐스팅, UserEmbedder
  seeded-DB 경로를 각각 검증한다.

## 증거
- 단위 테스트(mock 기반, 이 워크트리에서 실행 가능):
  - `tests/test_db_connection_pgvector.py` (7건): `_register_pgvector_adapter`의
    guard(등록 성공/실패), `get_connection()`이 풀 커넥션과 direct fallback
    커넥션 모두에 등록을 호출하는지, extension 누락 에러가 `PoolError`
    폴백에 삼켜지지 않는지, 기존 `PoolError` 폴백 동작이 유지되는지.
  - `tests/recommend_engine/test_parse_pgvector_ndarray_robust.py` (7건):
    문자열/ndarray/`pgvector.Vector`/`None`/손상 문자열 입력, 그리고
    1024차원 ndarray를 str()로 감싸면 numpy 요약 표기("...")로 값이
    깨진다는 전제 자체를 확인하는 회귀 테스트.
  - `tests/test_user_embedder_vector_parsing.py` (5건): `_to_vector_array`
    단위 테스트 4건 + `batch_update_all_users()`가 `news_letter_embedding`을
    `pgvector.Vector` 객체로 받는 상황을 mock으로 재현해 end-to-end로
    검증하는 1건.
  - `tests/test_rss_collector_upsert.py` (3건): `execute_values` 호출에
    `ON CONFLICT (raw_news_url) DO NOTHING`/`RETURNING raw_news_url`이
    포함되는지, `RETURNING` 건수로 `{"inserted", "skipped"}`가 정확히
    계산되는지, 언론사별 SAVEPOINT/ROLLBACK이 유지되는지, cutoff 이전
    기사는 애초에 INSERT를 시도하지 않는지.
  - 전체: 이 워크트리에서 `.venv/bin/python -m pytest -q -m "not
    integration and not benchmark" tests/` 실행 시 109 passed, 16
    deselected(통합 15 + 벤치마크 1) - 새로 추가한 21건 포함, 기존 테스트
    회귀 없음.
- 통합 테스트(`tests/integration/`, 15건 - 이 macOS 워크트리에는 Postgres가
  없어 로컬에서는 15 skipped로 스스로 건너뛰는 것까지만 확인했고, 실제 DB
  동작 자체는 CI의 `integration-test` 잡에서만 검증된다):
  - `test_pgvector_roundtrip.py`: `get_connection()`으로 float list를
    쓰고 raw-SQL로 다시 읽어 `_to_vector_array()`로 ndarray 변환, 코사인
    거리(`<=>`) 쿼리(자기 자신과의 거리 ≈ 0, 직교 벡터와의 거리 ≈ 1),
    vector extension이 없는 새 DB에 연결하면 `VectorExtensionMissingError`로
    즉시 실패하는지.
  - `test_news_raw_unique_conflict.py`: 평범한 중복 INSERT는
    `UniqueViolation`, `ON CONFLICT DO NOTHING`은 에러 없이 스킵하고 행 1건만
    유지, `collect_rss()`를 같은 기사에 대해 두 번 실행해도(재수집)
    idempotent한지(1차: inserted=1/skipped=0, 2차: inserted=0/skipped=1,
    최종 행 1건).
  - `test_timestamptz_migration_parsing.py`: 마이그레이션과 동일한 정의의
    안전 캐스팅 함수를 rss_collector 실제 출력 형식 4가지 + 손상된 값 4가지
    (빈 문자열, 비-날짜 문자열, 형식은 맞지만 범위를 벗어난 값 포함)에
    파라미터화 테스트, `raw_news_created_at` 컬럼이 실제로
    `timestamp with time zone`이고 timezone-aware datetime이 왕복되는지.
  - `test_user_embedder_seeded_db.py`: news_letter 1건(임베딩은 실제 모델
    대신 손으로 채운 단위 벡터) + user 1건 + user_preferred_newsletter
    연결을 시드하고 `UserEmbedder().batch_update_all_users()`를 실행,
    `user.user_embedding`이 채워지고 정규화됐는지.
  - `.github/workflows/ci.yml`은 `python -c "import yaml; yaml.safe_load(...)"`
    로 파싱 가능함을 확인했다(`jobs`에 `test`, `integration-test` 둘 다
    존재).
- `backend/alembic history`(DB 연결 없이 스크립트 그래프만 순회)로 새
  리비전 `e725a62ffef1`이 `c97fb5709513` 다음에 오고 `head`임을 확인했다.

## 결과와 한계
- `news_raw` 중복 제거는 downgrade로 되돌릴 수 없다(삭제된 행은 영구
  소실) - 프로덕션에 적용하기 전에 이 사실을 인지하고 있어야 한다.
- `raw_news_created_at`의 downgrade는 `VARCHAR`로 되돌리지만 `NOT NULL`은
  다시 걸지 않는다 - 파싱 실패로 NULL이 된 값의 원본 문자열은 복원할 수
  없다.
- `register_vector`가 raw-SQL 값을 `numpy.ndarray`가 아니라
  `pgvector.Vector` 래퍼로 돌려준다는 사실은 pgvector-python 버전에 따라
  달라질 수 있다 - `to_numpy` 속성 유무로 duck-typing해 처리했으므로 향후
  버전이 다른 래퍼를 쓰더라도 같은 인터페이스(`to_numpy()`)를 제공하는 한
  깨지지 않지만, 아예 다른 형태(예: bytes)로 바뀌면 다시 손봐야 한다.
- 통합 테스트 15건은 이 환경(Postgres 없는 macOS 워크트리)에서 "스킵되는
  것" 자체만 검증했고, 실제 DB에서의 동작(트랜잭션 격리, 동시성, 실제
  `<=>` 연산자 인덱스 사용 여부 등)은 CI의 `integration-test` 잡이 실제로
  통과해야 최종 확인된다 - 이 PR을 여는 시점에 GitHub Actions 실행 결과를
  반드시 확인해야 한다.
- `collect_rss()`의 반환 타입 변경(`int` -> `Dict[str, int]`)은 저장소 안의
  호출부(`pipeline/stages.py`, `pipeline/runner.py`)만 갱신했다. 이 함수를
  외부에서 직접 호출하는 코드(예: Airflow DAG)가 있다면 별도 확인이
  필요하다 - 이번 조사에서는 저장소 안에 다른 호출부를 찾지 못했다.

### 운영 규칙: 벡터 쿼리 파라미터 (2026-09-25 CI에서 확인)
- 파이썬 list를 파라미터로 넘기면 psycopg2가 `numeric[]`로 바인딩한다. INSERT/UPDATE는 pgvector의 대입 캐스트로 동작하지만, `<=>`·`<->` 같은 거리 연산자는 `operator does not exist: vector <=> numeric[]`로 실패한다(실제 PostgreSQL integration 잡에서 재현).
- 따라서 후보 검색 등 벡터 연산 쿼리는 파라미터를 `numpy.ndarray`로 넘기거나(`register_vector`가 vector로 변환) SQL에서 `%s::vector`로 명시 캐스트한다.
