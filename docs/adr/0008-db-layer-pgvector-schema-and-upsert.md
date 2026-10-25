# ADR 0008: pgvector 어댑터 등록, news_raw 스키마 정합성, RSS 수집기 UPSERT

## 상태
채택됨 (2026-09-25)

**2026-10-06 갱신.** RSS 수집기가 시간대 없는 발행 시각을 UTC가 아니라 KST로 읽게 고쳤다(맨 아래
"2026-10-06 갱신"). 코드만 고쳤다 — Tier 0 VM에는 배포하지 않았고, 이미 저장된 행도 고치지 않았다.
**2026-10-17**: Tier 0 VM에 배포했다(새 코드의 첫 ingest 2026-10-16 20:05 UTC, ADR 0026 증거 9). 배포 뒤 첫
AI타임스 행이 KST로 맞게 저장된 것을 확인했다. 이미 저장된 행은 여전히 고치지 않았다.
**2026-10-17 갱신 2**: 이미 저장된 AI타임스 605행을 Tier 0 DB에서 보정했다 — 원래 값은 보관 표에 남겨 되돌릴 수 있다(맨 아래
"2026-10-17 갱신: 기존 행 보정").
**2026-10-25 갱신**: 국민일보 441행의 발행 시각을 기사 페이지의 `datePublished`로 보정했다 — 원래 값은 보관 표에 남겨 되돌릴 수 있다(맨 아래
"2026-10-25 갱신: 국민일보 발행 시각 보정"). AI타임스와 달리 오프셋이 일정하지 않아 식이 아니라 행마다 페이지 값으로 바꿨고, 수집기는 고치지 않았다.
**2026-10-25 갱신 2**: 같은 이상이 보정 뒤에도 계속 들어와, 본문 추출기가 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤인 행을 기사 페이지의
`datePublished`로 바꾸게 고쳤다(맨 아래 "2026-10-25 갱신 2: 수집기가 미래 발행 시각을 추출 때 바로잡는다"). **코드만 고쳤다 - Tier 0 VM에는 배포하지 않았고**, 이미 추출이 끝난 미래 시각 행은
런북 6.10의 "2차 보정"으로 고친다(실행하지 않았다).

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
     (2026-10-06: 여기 적힌 "tz가 없으면 UTC를 채운다"가 결함이었다 - 형식은 안전했지만
     값이 9시간 틀렸다. 맨 아래 "2026-10-06 갱신".)
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

## 2026-10-06 갱신: 시간대 없는 발행 시각은 KST로 읽는다

### 무엇이 틀렸나
`ai_workspace/crawler/rss_collector.py`는 발행 시각 문자열을 `dateutil`로 파싱한 뒤 시간대가 없으면 UTC를
붙였다. AI타임스 피드는 시간대를 빼고 KST 벽시계 시각을 내보내므로 `news_raw.raw_news_created_at`이 실제보다
9시간 뒤로 저장됐다. Tier 0 DB에서는 AI타임스 320행 중 268행의 발행 시각이 수집 시각보다 뒤였고(최대 9.0시간),
Mac 덤프도 같았다([ADR 0026](0026-hosting-tiers-and-contingency.md) 증거 3·8). 이 컬럼은 수집 지연 지표와
시점 기준(point-in-time) 평가셋의 기사 시각으로 쓰인다 — ADR 0026의 지연 통계는 그래서 AI타임스를 빼고 냈다.
같은 이유로 이 피드에는 수집 cutoff(`hours`)도 9시간 느슨하게 걸렸다.

### 피드가 실제로 내보내는 형식 (2026-10-06 12:16 KST, 설정된 10개 피드를 한 번씩 받아 날짜 태그만 확인)
| 피드 | 발행 시각 표기 | 시간대 |
|---|---|---|
| 동아일보 · 한국경제 | `Tue, 06 Oct 2026 12:14:35 +0900` | 있음 |
| 매일경제 | `Tue, 06 Oct 2026 12:01:23 +09:00` | 있음 |
| 세계일보 | `Tue,6 Oct 2026 11:52:28 +0900` | 있음 |
| 전자신문 (IT · AI · 과학) | `Tue, 6 Oct 2026 12:00:42 +0900` | 있음 |
| 경향신문 | `2026-10-06T12:00:00+09:00` (`dc:date`) | 있음 |
| 국민일보 | `6 Oct  2026 03:11:00 GMT` | 있음 (GMT) |
| AI타임스 | `2026-10-06 09:00:00` | **없음** |

AI타임스의 값이 KST라는 근거는 같은 응답 안에 있다: HTTP `Last-Modified`가 `Tue, 06 Oct 2026 03:03:10 GMT`인데
채널의 `lastBuildDate`는 `2026-10-06 12:03:10`으로 정확히 9시간 앞선다. 가장 새 항목의 `pubDate`(09:00:00)는
받은 시각(03:16 UTC)보다 뒤라 UTC일 수 없다. DB에서 본 "최대 9.0시간 앞섬"과도 맞는다.

### 검토한 대안
1. 저장은 그대로 두고 읽는 쪽(지연 지표, 평가셋)에서 언론사별로 보정한다.
   - 이 컬럼을 읽는 코드마다 "AI타임스는 9시간 뺀다"를 알아야 하고, 하나라도 빠뜨리면 조용히 틀린다. 기각.
2. **시간대가 없을 때만 KST(+09:00 고정)를 붙인다. (채택)**
   - 설정된 피드는 모두 한국 언론사다. 시간대가 붙은 값(+0900, GMT)은 손대지 않는다.
   - `zoneinfo("Asia/Seoul")` 대신 고정 오프셋을 쓴다: 한국은 서머타임이 없어 결과가 같고, 수집 전용
     이미지에 tzdata가 있는지에 기대지 않는다. `crawler/policy_briefing.py`가 이미 같은 상수를 쓴다.
3. 피드마다 시간대를 설정한다(기본 Asia/Seoul).
   - `Settings.RSS_FEEDS`의 `(strategy, url)` 튜플은 수집기·본문 추출기·테스트가 그대로 언패킹한다
     (`config/sources.py`가 라이선스를 따로 둔 것도 같은 이유다). 시간대를 빼는 피드는 하나이고 한국 언론사라
     지금은 모양을 바꿀 이유가 없다. 한국 밖 시간대를 표기 없이 내보내는 피드를 넣게 되면 그때 도입한다 —
     `settings.py`의 `RSS_FEEDS` 위에 이 가정을 주석으로 적어 두었다.

### 결정
- `crawler/rss_collector.py`: `KST = timezone(timedelta(hours=9))`를 두고, 파싱한 발행 시각에 시간대가 없을
  때만 붙인다. 스키마·마이그레이션은 바꾸지 않는다(`raw_news_created_at`은 이미 `timestamptz`다).
- 이미 저장된 행의 보정은 이 변경에 넣지 않는다. SQL 제안만 [runbook-hosting.md](../runbook-hosting.md) 6.7에
  적었다 — **실행하지 않았고, 범위와 시점은 소유자가 정한다.**

### 증거
- `tests/test_rss_collector_pubdate_timezone.py` (7건, 수집 시각을 2026-10-06 03:16 UTC로 고정하고 위 표의
  실제 형식을 넣는다):
  - 시간대 없는 `2026-10-06 09:00:00`은 00:00 UTC로 저장된다.
  - 시간대가 붙은 5가지 형식(+0900, +09:00, 쉼표 뒤 공백 없는 +0900, ISO +09:00, GMT)은 원래 시각 그대로다.
  - cutoff(100시간)는 KST로 읽은 시각에 걸린다: `2026-10-02 05:00:00`은 버려지고(`too_old`),
    `2026-10-02 09:00:00`은 남는다.
- 고치기 전 코드에서는 첫째와 셋째가 실패했다(저장 값 09:00 UTC, `too_old` 0). 시간대가 있어도 KST로 덮어쓰는
  잘못된 구현은 국민일보(GMT) 케이스가 잡는다(코드를 그렇게 바꿔 실패하는 것을 확인한 뒤 되돌렸다).
- 전체 단위 테스트(CI와 같은 선택, `python -m pytest -q -m "not integration and not benchmark" tests/`):
  1516 passed, 30 skipped, 87 deselected — 새 7건 포함, 실패 없음.
- DB에 닿는 확인은 하지 않았다. integration 테스트는 CI에서만 돈다 — 기존 `test_news_raw_unique_conflict.py`·
  `test_ingest_job_end_to_end.py`는 시간대가 붙은 발행 시각을 쓰므로 이 변경의 영향을 받지 않는다.

### 결과와 한계
- **Tier 0 VM에는 배포하지 않았다.** VM은 09-26 스냅숏 코드로 돌고 있어(ADR 0026 증거 8), 다시 배포하기
  전까지는 AI타임스의 새 행도 계속 9시간 뒤로 저장된다.
  **2026-10-17 배포**(ADR 0026 증거 9): 배포 뒤 첫 AI타임스 기사(피드 `pubDate` `2026-10-17 07:00:00`, 시간대 없음)가
  `2026-10-16 22:00:00+00`으로 저장됐고 수집은 발행 1.11시간 뒤다. 같은 피드에서 옛 코드가 받은 바로 앞 기사들은 수집보다
  7.7~8.3시간 뒤로 남아 있다. "고치기 전 코드로 수집한 행"의 경계(런북 6.7의 2단계: 새 커밋으로 돈 첫 ingest의 시작)는 VM에서
  `2026-10-16 20:05:01.826735` UTC이고, 그 앞에 수집한 AI타임스 행은 605행(그중 발행 > 수집 552행)이다.
- **이미 저장된 행은 그대로다.** `ON CONFLICT (raw_news_url) DO NOTHING`이라 다시 수집해도 기존 행의 시각은
  바뀌지 않는다. 보정하기 전에 `raw_news_created_at`으로 만드는 평가셋·지연 지표는 AI타임스를 빼고 읽는다.
  9시간을 빼서 쓰려면 고치기 전 코드로 수집한 행에만 빼야 한다 — 재배포 뒤에 수집된 행은 이미 맞다.
- **보정 대상은 268행이 아니라 고치기 전 코드로 수집한 AI타임스 행 전부로 본다.** "발행 > 수집"으로 드러나는
  것이 268행일 뿐, 나머지 52행도 같은 경로로 들어왔다 — 발행 뒤 9시간 넘게 지나 수집돼 9시간 밀려도 수집
  시각을 넘지 않았을 뿐이라고 본다(이 피드는 한 번에 50건을 보여 주므로 첫 ingest가 받은 백로그가 그
  정도다). 이것은 추정이고 DB에서 확인하지 않았다 — 런북 6.7의 사전 확인 쿼리로 본다.
- 피드 형식은 2026-10-06 한 시점에 본 것이다. 시간대 없이 UTC로 내보내는 피드가 생기면 이번에는 9시간 이르게
  저장된다. `dateutil`이 모르는 시간대 약어(예: `EST`)도 시간대 없는 값으로 파싱돼 KST로 읽힌다 — 지금 그런
  피드는 없다.
- `raw_news_crawled_at`은 건드리지 않았다. 여전히 시간대 없는 컬럼에 프로세스의 로컬 시각을 쓴다(컨테이너에서는
  UTC — ADR 0026 "증거 6의 시각 표기").

## 2026-10-17 갱신: 기존 행 보정 (Tier 0 DB, 605행)

### 결정
- **범위: 경계 앞에 수집한 AI타임스 행 전부(넓은 안).** 런북 6.7이 소유자 결정으로 남겨 둔 두 안 중 넓은 안이다. 좁은 안(발행 >
  수집으로 드러난 552행만)은 같은 코드 경로로 들어온 나머지 53행을 9시간 밀린 채 남겨 한 언론사 안에서 기준이 갈린다. 그 53행이
  정말 밀려 있었는지는 아래 증거의 사후 확인으로 본다.
- **경계: 고친 코드로 돈 첫 ingest의 시작 `2026-10-16 20:05:01.826735` UTC**(`job_runs`의 `git_sha`별 첫 시작, 런북 6.7 2단계).
- **되돌릴 수 있게.** 원래 값은 보관 표 `news_raw_created_at_fix_kst`(행마다 보정 전·후 값, 경계, 보정 시각)에 남는다. 이 표가
  "이 DB는 보정했다"는 표시이기도 하다 — 지우지 않는다. 되돌리기는 `scripts/oci/fix_aitimes_created_at_kst_revert.sql`.
- **SQL은 파일로, CI에서 먼저.** `scripts/oci/fix_aitimes_created_at_kst.sql` — 한 트랜잭션, 기본은 리허설(ROLLBACK),
  `-v apply=1`일 때만 COMMIT. 보관 표가 이미 있음·대상 0행·보정 뒤에도 수집보다 10분 넘게 앞선 AI타임스 행·보정한 행의 최소
  수집 지연이 8시간 초과(두 번 뺀 흔적) 중 하나면 예외로 멈춘다. `tests/integration/test_fix_aitimes_created_at_kst_sql.py`가
  CI의 PostgreSQL에서 리허설·적용·두 번째 적용 거절·되돌리기·기준 위반 거절·보관 표를 잃은 DB의 두 번째 보정 거절을 본다
  (CI 실행 39009975313, integration 127 passed).

### 증거 (Tier 0 VM, 2026-10-17 UTC)
| 단계 | 결과 |
|---|---|
| 직전 백업 06:29 | 호스트 밖 백업(ADR 0026 증거 11)에 보정 전 값이 남아 있다 |
| 리허설 06:32 | `INSERT 0 605`, `UPDATE 605`, 보정 뒤 최소 수집 지연 1분 8초 · 최대 2일 22시간 22분, 보정 뒤에도 앞선 행 0, `ROLLBACK` |
| 적용 06:33:35 | 같은 수치로 `COMMIT`. 보관 표 605행, 605행 모두 정확히 9시간 당겨짐 |
| 확인(읽기 전용) | AI타임스 621행 중 발행 > 수집 **0행**, 최대 앞섬 −1분 8초. 다른 7개 언론사도 0행(최대 앞섬 −8초 ~ −2분 26초) |
| 보정 전에 앞서지 않던 53행 중 47행 | 2026-09-26(UTC) 첫 ingest의 피드 백로그다. 보정 뒤 수집 지연 12.3시간 ~ 2.9일(70.4시간) — 발행 뒤 9시간 넘게 지나 수집돼 9시간 밀려도 수집 시각을 넘지 않았던 행이라는 위 추정과 맞는다 |
| 나머지 6행 (2026-10-17 07:20 UTC 읽기 전용 조회, 제목 없이) | 백로그가 아니라 정시 회차에 들어왔다: `raw_news_id` 32893(09-29 20:05 UTC 수집, 보정 뒤 지연 13.0시간) · 40380(09-30 19:05, 9.5) · 40388(09-30 19:05, 11.1) · 40790(09-30 20:05, 12.8) · 66241(10-03 21:05, 19.3) · 152851(10-14 20:06, 12.6). 모두 04~06 KST에 수집됐고 피드 `pubDate`가 분 단위(초가 :00)다. 보정 뒤 지연이 AI타임스 p95(3.97시간)보다 크다 |
| AI타임스 수집 지연(첫 ingest 백로그 제외, n=571) | 중앙 0.91시간 · p95 3.97시간 · 최소 0.02시간 |

### 결과와 한계
- **Tier 0 DB에서는 AI타임스를 빼지 않고 발행 시각을 쓸 수 있다.** 지연 통계·평가셋에서 "AI타임스 제외"는 이 시각 이후의 DB
  조회에는 필요 없다.
- **보정 전에 만든 사본은 보정 전 값이다.** 2026-10-17 06:33 UTC 앞의 덤프·반출본(Mac 덤프 2개, `tier0-micro-2026-10-16T1927Z`,
  E0 동결 반출 `20261017T024007Z`, 호스트 밖 백업의 그 시각 앞 객체 — 2026-10 월별 사본 `nobody/monthly/tier0-2026-10.nobody.tar`
  포함)의 AI타임스 행은 여전히 9시간 뒤다. 그런 사본을 복원하면 보관 표가 없으므로 같은 경계로 이 SQL을 다시 돌린다(런북 6.7).
  E0 반출본은 실행 전 기록이 고정한 입력이라 고치지 않는다.
- **6행은 9시간 밀려 있었다고 추정했을 뿐 확인하지 않았다.** 피드 형식과 코드 경로가 나머지 599행과 같아 넓은 안에 넣었지만, 보정 뒤에도
  수집 지연이 9.5~19.3시간으로 이 언론사의 p95(3.97시간)보다 길다. 발행 시각을 원문 기사 페이지와 대조하지 않았다 — 이 6행의
  발행 시각을 지연 통계에 쓸 때는 이 점을 적는다(되돌리기는 보관 표로 행마다 할 수 있다).
- Mac 덤프(`mac-compose-2026-09-26.dump`)의 행은 고치지 않았다. 런북 6.4로 복원할 일이 생기면 복원한 DB에서 경계를 모든 행보다 뒤(예: `2100-01-01`)로 두고 보정한다
  (런북 6.7 끝).


## 2026-10-25 갱신: 국민일보 발행 시각 보정 (Tier 0 DB, 441행)

### 무엇이 틀렸나
국민일보가 URL 형식과 페이지 템플릿을 바꾼 2026-10-19(ADR 0026 증거 12) 뒤, RSS `pubDate`로 저장한 `raw_news_created_at`이 수집 시각보다 **뒤**인 행이 생겼다. 10-19 ~ 10-23
수집분 440행이 수집보다 4.46 ~ 23.95시간 뒤였다(일별 121 · 73 · 110 · 103 · 33행. 개편 전에는 0행이었다). 위 AI타임스(2026-10-06 갱신)와 달리 시간대 해석이 원인이 아니다: 기사 페이지의
발행 시각과 견주면 저장값이 페이지 값보다 -5.29 ~ +23.98시간(중앙 19.38시간) 뒤이고 크기가 일정하지 않다. 저장값의 시각(KST)은 18시 ~ 23시에 몰려 있어 기사 시각처럼 보이지 않는다. **RSS가 왜 그런 값을 주는지는 모른다**(원인 미규명).

### 검토한 대안
1. **저장된 날짜를 기사 URL의 날짜 기준으로 맞춘다("정확히 하루 앞").** 같은 날 검토 하나가 이렇게 읽어 수집 지연 p95를 3.61시간(기준 3시간 밖)으로 계산했다. 기사 페이지와 대조해 보니 맞지 않았다 - URL의 날짜(`/article/<YYYYMMDD…>`)는
   발행일이 아니다(페이지의 발행일이 URL 날짜와 같은 행 125 · 하루 뒤 315 · 이틀 뒤 2). 하루 앞당기면 저장값을 틀리게 옮긴다. 하루 앞당긴 값은 441행 모두 페이지의 발행 시각보다 0.02 ~ 29.29시간 앞서고(중앙 4.62시간), 같은 URL 날짜 안에서 기사 번호순으로 놓으면
   저장값은 이웃한 436쌍 중 뒤집힌 쌍이 0이다(페이지 값은 182쌍, 442건 기준). 기사를 등록한 시각처럼 보이나 확인하지 않았다 - 어느 쪽이든 증거 13 (3)의 지연은 발행(`raw_news_created_at`) → 수집이고,
   이 값으로 재면 다른 것(등록 → 수집)을 잰다. **기각.**
2. **음수 행을 지연 통계에서 빼거나 최악으로 친다.** 틀린 값의 원인을 모른 채 읽기를 고르는 일이고, 시점 기준(point-in-time) 평가셋의 기사 시각도 같은 값을 쓴다. 값을 고치는 쪽이 낫다. 보정 전·후 통계를 모두 적는다
   (ADR 0026 증거 13 (3)). **기각.**
3. **기사 페이지의 JSON-LD `datePublished`로 행마다 바꾼다.** 오프셋이 일정하지 않아 식으로는 고칠 수 없다. **채택.**

### 결정
- **범위: 새 URL 형식의 국민일보 행 가운데 수집 10-19 ~ 10-24(UTC)이고, 저장된 발행 시각이 수집보다 뒤이거나 저장된 발행일(KST)이 URL 날짜의 하루 뒤인 행 442건.** 구 URL(`view.asp`)
  행은 대상이 아니다. 대상 밖 국민일보 행(같은 기간 새 URL 1,206행 중 764행)은 페이지와 대조하지 않았다.
- **값: 기사 페이지의 `datePublished`(시간대 포함).** 대상 442건의 페이지를 한 번씩, 2초 간격으로 받아 `"datePublished"` 값만 읽었다(앞 40만 바이트만 읽고 본문은 저장하지 않았다). 먼저 표본 10건을
  대조해 오프셋이 일정하지 않은 것을 본 뒤 전수로 넓혔다. 수집기의 `pubDate` 처리는 바꾸지 않았다(같은 날 뒤의 "갱신 2"에서 추출기를 고쳤다).
- **건너뛰는 행.** 페이지 값이 없거나 시간대가 없다 / 수집보다 10분 넘게 뒤다 / 수집보다 7일 넘게 앞이다. 이번에 건너뛴 것은 1행이다(페이지 값이 수집보다 1.01시간 뒤라 쓰지 않았다. 저장값은 수집보다
  17.75시간 뒤인 채로 남는다).
- **되돌릴 수 있게.** 원래 값은 보관 표 `news_raw_kmib_created_at_fix`(행마다 보정 전·후, 출처, 보정 시각)에 남는다. 이 표가 "이 DB는 보정했다"는 표시이기도 하다 - 되돌리기 스크립트만 지운다. 되돌리기는
  `scripts/oci/fix_kmib_created_at_revert.sql`(보정 뒤 값이 다시 바뀐 행이 있으면 멈춘다).
- **SQL은 생성기로, CI에서 먼저.** 행마다 값이 달라 입력 CSV(id · URL · 보정 전 값 · 수집 시각 · 페이지 값)에서 `scripts/oci/build_kmib_created_at_fix.py`가 만든다. 한 트랜잭션이고 기본은 리허설(ROLLBACK), `-v apply=1`일
  때만 COMMIT이다. 원래 값이 그대로인 행만 바꾸고, 보관 표가 이미 있음 · 바꾼 행 수가 기대와 다름 · 바꾼 뒤에도 발행 시각이 수집보다 10분 넘게 뒤인 대상 행이 남음 중 하나면 예외로 멈춘다.
  `tests/test_build_kmib_created_at_fix.py`(생성기)와 `tests/integration/test_fix_kmib_created_at_sql.py`(CI의 PostgreSQL에서 리허설 · 적용 · 보관 표 검사 · 되돌리기)가 먼저 돈다.

### 증거 (Tier 0 VM, 2026-10-25 UTC)
| 단계 | 결과 |
|---|---|
| 대상 선정 (읽기 전용) | 442행: 수집 10-19 121 · 10-20 73 · 10-21 110 · 10-22 103 · 10-23 33 · 10-24 2. 저장된 발행 시각이 수집보다 뒤인 행 440(4.46 ~ 23.95시간). 나머지 2행(10-24 수집)은 수집보다 앞섰지만 같은 모양(저장 날짜 = URL 날짜 + 1, KST 21시대)이라 대상에 들어갔다 |
| 페이지 대조 | 442건 모두 받음, 오류 0, 모두 `datePublished` 있음 |
| 저장값 − 페이지값 | -5.29 ~ +23.98시간, 중앙 19.38. 24시간 ±0.1시간 안은 59행 |
| 저장값의 시각(KST) | 18시 179 · 19시 98 · 20시 100 · 21시 36 · 22시 8 · 23시 21 |
| 날짜 비교 | 저장값의 날짜는 442행 모두 URL 날짜의 하루 뒤(선정 기준에 들어 있다). 페이지의 발행일은 URL 날짜와 같은 날 125 · 하루 뒤 315 · 이틀 뒤 2 |
| 페이지 발행 → 수집 (441행) | 중앙 0.72시간, 최대 19.30시간. 3시간을 넘는 행 63건은 두 회차에 모여 있다(10-19 05:05 UTC 50행, 10-21 05:05 UTC 13행). 나머지 378행은 3시간 안 |
| 적용 2026-10-25 11:12:47 UTC | 보관 표 441행, 441행 모두 지금 값이 보관 표의 보정 후 값(페이지 값)과 같고, 발행 시각이 수집보다 10분 넘게 뒤인 대상 행은 0(읽기 전용 확인) |
| 보정 전 → 후 (창 10-18 07:00 ~ 10-25 07:00 UTC 수집 10,980행) | 수집 지연 p95 2.095 → 2.443시간(제외 없는 전체 행), 음수 행 440 → 1, 3시간 넘은 행 340 → 401건 (ADR 0026 증거 13 (3)). 401 = 340 − 2 + 63: 위의 "수집보다 앞선 2행"도 고쳐져 3시간 넘은 행에서 빠졌고(3.43 → 1.09시간, 5.35 → 0.07시간), 보정한 441행 가운데 63행이 3시간을 넘는다. p95는 두 행을 보정 전 값으로 두어도 2.443이다 |

### 결과와 한계
- **지연은 이제 계산할 수 있다.** 창 안 국민일보 441행은 실제 발행 뒤 중앙 0.72시간에 수집됐다. 같은 기간 다른 언론사의 수집 지연 중앙(0.43 ~ 1.31시간)과 같은 범위다.
- **페이지 값을 사실로 둔 것은 가정이고, 독립된 확인은 일부에서만 된다.** JSON-LD `datePublished`는 기사 페이지가 스스로 적은 발행 시각이고, 수정 시각이 아니다. 독립된 출처는 개편 전 RSS(구 URL)로 같은 기사를 이미 받은 행이다:
  보정한 441행 중 같은 제목의 구 URL 행이 하나뿐인 16쌍에서 구 RSS의 발행 시각과 페이지 값은 16쌍 모두 10분 안(13쌍은 1분 안, 가장 큰 차이 2분)이었고, 보정 전 저장값은 16쌍 중 0쌍이었다(차이 중앙 +19.6시간).
  한계: 이 쌍은 10-19 개편 직후 다시 들어온 행뿐이라 441행의 3.6%이고, 나머지 425행은 위의 중앙 0.72시간이 다른 언론사의 RSS 지연과 같은 범위라는 간접 근거에 기댄다.
- **원인이 그대로라 이상이 다시 났다.** 보정 뒤인 같은 날 10:05 UTC 회차에서 25행, 11:05 UTC 회차에서 2행이 수집보다 22.8 ~ 23.8시간 뒤의 발행 시각으로 들어왔다(저장값이 다음 날 18~19시 KST대로 앞의 440행과 같은 모양).
  수집기를 고치지 않았으므로 새로 들어오는 국민일보 행에 같은 일이 일어날 수 있다. 이 27행은 보정하지 않았다 - 런북 6.10을 다시 돌리면 된다(생성기는 입력 CSV만 바꾸면 같은 SQL을 만든다).
  [같은 날 뒤의 "갱신 2"에서 수집기(본문 추출기)를 고쳤다. 이 27행은 이미 추출이 끝난 행이라 그 수정이 배포돼도 스스로 고쳐지지 않고 2차 보정 대상이다.]
- **보정 전에 만든 사본은 보정 전 값이다.** 2026-10-25 11:12 UTC 앞의 덤프 · 반출본 · 호스트 밖 백업의 국민일보 행(수집 10-19 ~ 10-24)은 저장값이 수집보다 뒤인 채다. 18:10 UTC 정시 백업부터 보정된 값(과 보관 표)이 들어간다. 그런 사본을
  복원하면 보관 표가 없으므로 6.10으로 다시 보정한다. LLM 실험의 동결 반출(ADR 0036, 2026-10-17 수집분까지)은 국민일보 이상이 시작한 10-19 앞이라 영향이 없다.
- **대조하지 않은 행이 있다.** 대상 밖 국민일보 764행(같은 기간 새 URL 행)과 구 URL 행의 발행 시각은 페이지와 대조하지 않았다. 건너뛴 1행은 음수로 남는다. 다만 대상 밖 행 가운데 같은 제목의 구 URL 행이 하나뿐인 22쌍(모두 10-19 05:05 UTC 회차)에서는
  19쌍의 저장값이 구 RSS 발행 시각과 1시간 안이다(10분 안 17쌍, 1분 안 6쌍). 나머지 3쌍은 저장값이 구 RSS 값보다 3.2 ~ 57.0시간 이르다 - 알려진 이상(저장값이 더 늦음)의 방향이 아니고 같은 기사인지는 보지 않았다.
  쌍이 생기는 것은 개편 직후 행뿐이라 그 뒤 수집분의 숨은 이상은 이 쌍으로는 가려지지 않는다.


## 2026-10-25 갱신 2: 수집기가 미래 발행 시각을 추출 때 바로잡는다 (코드만, Tier 0 미배포)

### 무엇이 남았나
위 보정(11:12 UTC)은 이미 들어온 441행을 고친 것이고 원인은 그대로다. 보정 뒤에도 같은 모양의 행이 계속 들어왔다: 수집 10:05 UTC 25행 · 11:05 UTC 2행 · 12:05 UTC 2행. 읽기 전용으로 센 값(Tier 0, 2026-10-25 12:31 UTC):
저장된 발행 시각이 수집보다 10분 넘게 뒤인 행은 **지금 30행이고 모두 국민일보**다(29행은 10-25 수집분이고 1행은 위에서 건너뛴 10-20 수집분, 수집보다 17.75 ~ 23.78시간 뒤). 다른 언론사는 DB 전체에서 0행이다. 30행은 모두 이미 추출이 끝났다(`ok` 24 · `dropped` 6, 시도 1회) -
기사 페이지를 수집기가 이미 한 번 받았다는 뜻이다. 보관 표(441행)와 겹치는 행은 없다. 이 셈은 기록이고, 그 뒤 새로 들어온 행은 다시 세지 않았다.

수동 보정(런북 6.10)을 계속 돌리면 재확인(2026-11-01 이후, ADR 0026 결정 3) 전까지 사람이 대상을 뽑고 Mac에서 페이지를 받는 절차를 되풀이해야 하고, 보정 사이에는 틀린 값이 지연 통계에 들어간다. 본문 추출기는 어차피 같은 페이지를 받으므로 거기서 고친다.

### 검토한 대안
1. **수동 보정(런북 6.10)을 되풀이한다.** 사람이 대상을 뽑고, Mac에서 페이지를 받고, 리허설을 거쳐 적용한다. 이상이 계속 들어오는 동안 회차마다 틀린 값이 남고, 안 돌리면 지연 p95를 읽을 수 없다(런북 6.11). 같은 페이지를 수집기가 이미 받는데 따로 또 받는 셈이다. **기각**(2차 보정으로 이미 추출이 끝난 행만 한 번 정리한다).
2. **미래 시각을 수집 시각으로 잘라 넣는다(clamp).** 값을 지어내는 것이다 - 지연이 0이 되어 통계가 실제보다 좋아 보이고 평가셋의 기사 시각도 틀린다. **기각.**
3. **미래 시각 행을 NULL로 두거나 넣지 않는다.** 시각만 틀린 것이지 기사는 유효하다. 넣지 않으면 기사가 사라지고, NULL이면 발행 시각을 쓰는 모든 곳(지연 통계, 시점 기준 평가셋)이 NULL을 따로 다뤄야 한다. **기각.**
4. **모든 행의 발행 시각을 페이지 값으로 바꾼다(미래가 아니어도).** 페이지 값을 사실로 둔 것은 가정이고, 독립된 확인은 개편 전 RSS로 같은 기사를 이미 받은 16쌍뿐이다(위 "결과와 한계"). 미래 값은 기사가 수집 뒤에 발행됐다는 뜻이라 페이지 값을 믿을지 말지와 상관없이 틀렸다고 말할 수 있지만,
   미래가 아닌 행은 그런 근거가 없다. 국민일보의 미래가 아닌 행은 페이지와 대조하지 않았고(위 "대조하지 않은 행"), 다른 언론사의 JSON-LD가 발행 시각을 맞게 적는지도 확인하지 않았다. 정상으로 보이는 값을 가정에 기대어 덮으면 멀쩡한 행을 틀리게 바꿀 수 있다.
   **기각** - 값이 틀렸다고 말할 수 있는 행에만 쓴다.
5. **RSS 수집 단계에서 페이지를 받아 고친다.** RSS 단계는 페이지를 받지 않는다. 기사마다 요청을 더하는 일이고(1 GB VM, 동아일보처럼 요청을 거절하는 출발지가 있다 - ADR 0026 증거 10) 이미 받는 추출 단계가 있다. **기각.**
6. **본문 추출 단계에서, 미래인 행에 한해 페이지 값으로 바꾼다.** **채택.**

### 결정
- **대상: 저장된 `raw_news_created_at`이 `raw_news_crawled_at`(시간대 없는 UTC)보다 10분 넘게 뒤인 행.** 10분은 서버 시계 차이로 몇 분 앞서는 값을 정상으로 두려는 여유다(일회성 보정과 같은 값). 그 밖의 행은 **페이지를 읽지도 않고 건드리지 않는다** -
  정상으로 보이는 피드의 값을 페이지 값으로 덮지 않는다(위 대안 4).
- **값: 기사 페이지의 JSON-LD `datePublished`, 없으면 `<meta property="article:published_time">`. 쓰는 값은 그 기사 자신의 값뿐이다.**
  JSON-LD는 **최상위 기사 노드**의 **문자열** `datePublished`만 받는다. 최상위 기사 노드는 `@type`이 `*Article`/`*Posting`(배열 · IRI · 하위 형식 포함)이고, 덩어리 최상위이거나 최상위 배열의 항목이거나 최상위 객체의 `@graph` 항목인 노드다.
  그런 노드가 여럿이면 문서 순서로 첫 노드가 정한다 - 그 노드에 문자열 값이 없으면(키 없음 · `{"@value": ...}` · 숫자) JSON-LD 값은 없는 것이고, 다음 기사 노드로 넘어가지 않는다. **중첩된 노드(관련 기사 목록 · `ItemList` · `image`의 `ImageObject` 등)와 기사가 아닌 노드(`WebPage` 등)의 값은 절대 쓰지 않는다** -
  그러면 다른 기사의 시각을 이 행에 써 넣는다. 중첩 노드까지 깊이·형식으로 순위를 매겨 후보에 두면, 기사 자신의 노드에 값이 없을 때 관련 기사·`image`의 값이 1위가 되어 맞는 meta가 무시되고, `WebPage` 최상위 옆의 깊이 2 `NewsArticle`이 1위가 되는 경우가 재현된다.
  **깨진 JSON-LD**: 정상으로 읽힌 최상위 기사 노드가 있으면 깨진 덩어리는 보지 않는다. 깨진 덩어리뿐일 때는 `datePublished`가 적힌 덩어리가 정확히 하나이고, 그 안의 서로 다른 값이 하나이고, `"@type"`에 Article/Posting이 보일 때만 그 값을 쓴다(아니면 버린다 -
  구조를 알 수 없는 글자에서 관련 기사의 값을 집지 않으려는 것이다). 너무 깊이 중첩돼 `json.loads`가 `RecursionError`를 내는 덩어리도 깨진 덩어리로 다루며, 그 덩어리 때문에 meta나 뒤 덩어리를 버리지 않는다.
  **meta와의 교차 확인**: JSON-LD 값과 meta 값이 모두 읽히고 10분 넘게 다르면 어느 쪽도 쓰지 않고 `conflict`로 센다(한쪽만 시간대가 없으면 벽시계 시각으로 견준다). 한쪽을 읽을 수 없거나 시간대가 없으면 시간대가 있는 쪽을 쓴다(JSON-LD 우선). 수정 시각(`dateModified`)은 읽지 않는다.
  새 모듈 `ai_workspace/crawler/content_extractor/page_pubdate.py`(표준 라이브러리만, `body_container.py`처럼 추출기와 분리)가 읽고 판정한다. 예외를 던지지 않는다 - 발행 시각을 못 고치는 것이 본문 추출을 막지 않는다.
- **페이지 값을 쓰는 조건(일회성 생성기와 같다): 시간대가 붙어 있고, 수집 시각 + 10분 이하이고, 수집 시각 - 7일 이상.** 어긴 행은 저장값을 그대로 두고 사유를 센다(`no_page_time` · `no_tz` · `conflict` · `after_crawl` · `too_old`). 두 곳의 상수는 `tests/test_content_extractor_page_pubdate.py`가 같은지 확인한다.
- **언론사를 가리지 않는다.** 미래의 발행 시각은 어느 언론사든 틀린 값이다(기사는 수집 앞에 발행된다). 규칙은 미래인 행에만 걸리고, 지금 다른 언론사의 해당 행은 DB 전체에서 0이므로 국민일보 밖의 동작은 지금 달라지지 않는다. 국민일보로 한정하면 다른 언론사에서 같은 이상이 날 때
  코드를 또 고쳐 배포해야 한다. 한계: 동아일보는 RSS 요약 전략(ADR 0026 C-31)이라 추출 단계를 거치지 않아 이 경로로 고치지 못한다(지금 해당 행 0).
- **쓰는 곳: 추출 결과를 쓰는 같은 트랜잭션.** `ok`·`dropped`·`empty`·`duplicate` 모두(페이지를 받았으므로) 발행 시각도 함께 바꾼다. `fetch_failed`(페이지를 못 받았다)·`error`(처리 중 예외로 롤백했다, 페이지를 받은 뒤일 수도 있다)는 그 시도에서 못 고치고, 시도 상한(3회) 안에서 다시 받을 때 고친다. 미래인 행이면 이 두 경우도 `created_at_unfixed`에 센다(아래 통계). 발행 시각을 쓰는 `UPDATE`는
  `WHERE`가 지금 행의 값이 아직 미래일 때만 바꾼다 - 대상을 고른 뒤 다른 곳(런북 6.10의 보정)이 먼저 고친 값을 덮지 않고, 0행이면 바꾼 행으로 세지 않는다. 바꾸기 전 값은 같은 문장이 행을 잠그는 부질의(`FOR UPDATE`)에서 `RETURNING`으로 받는다(대상을 고를 때 받은 낡은 값이 아니라 지금 행의 값). `ok`가 본문 해시 unique 인덱스에 막혀 롤백하고 `duplicate`로 다시 쓸 때는 발행 시각도 다시 쓴다.
- **대상 조회: SELECT 열에 발행 시각·수집 시각을 더했다.** 고르는 조건(`WHERE`)은 그대로라 보존 잡(`jobs/tasks/retention.py`)의 "추출 대기" 조건과 같다 - `tests/test_retention_job.py`의 SQL 조각 검사가 그대로 통과한다.
- **통계(`job_runs.stats.extract`): `created_at_from_page`**(언론사별 바꾼 행 수) · **`created_at_unfixed`**(미래였는데 못 바꾼 행의 언론사별 사유 개수. 그 행은 미래인 채 남는다. 사유: 페이지를 읽었다 - `no_page_time` · `no_tz` · `conflict` · `after_crawl` · `too_old`, 읽기 전에 끝났다 - `fetch_failed` · `error`. 뒤 둘은 시도마다 세므로 같은 행이 시도 상한까지 여러 회차에 잡힐 수 있다). 경고(`ctx.warn`)로 연결하지는 않았다 - 배포 뒤 몇 회차를 보고 정한다.
- **원래 RSS 값을 남긴다(되돌릴 수 있게): `created_at_fixed_rows` · `created_at_fixed_rows_omitted`.** 1차(`news_raw_kmib_created_at_fix`)와 2차(`news_raw_created_at_fix_2`) 수동 보정은 보관 표와 되돌리기가 있지만, 수집기는 같은 ingest 안에서 RSS 값을 넣고 몇 분 뒤 고치므로
  원래 값이 DB 어디에도 남지 않고 매일 18:10 UTC 백업에도 없다. 그래서 바꾼 행마다 `[raw_news_id, 바꾸기 전 값, 바꾼 뒤 값]`(UTC ISO, 끝이 Z)을 `job_runs.stats.extract.created_at_fixed_rows`에 행 번호순으로 남긴다(`job_runs`는 매일 백업된다). 한 회차에 최대 200개이고,
  넘친 행은 건수만 `created_at_fixed_rows_omitted`로 세고 값은 컨테이너 로그에 남긴다(`created_at_from_page`의 합 = 목록 + 생략). stats는 추출이 끝나야 저장되므로, 도중에 프로세스가 죽는 경우를 위해 바꾼 행마다 로그 한 줄도 남긴다(로그는 오래 보관되지 않으므로 보조 수단이다). **되돌리는 법**은 런북 6.10 "수집기가 바꾼 행 되돌리기": 회차의 목록으로 `UPDATE ... SET raw_news_created_at = <전> WHERE raw_news_id = <id> AND raw_news_created_at = <후>`를 만든다 -
  지금 값이 수집기가 쓴 값 그대로일 때만 되돌리므로 그 뒤 다른 곳이 바꾼 행은 덮지 않는다.
- **2차 보정(이미 추출이 끝난 행).** 위 30행처럼 수집기가 이미 지나간 행은 이 수정이 닿지 않는다. `scripts/oci/build_kmib_created_at_fix.py`가 `--table`(보관 표 이름) · `--all-presses`/`--press`(언론사) · `--revert`(보관 표 이름을 바꾼 되돌리기 SQL)를 받게 했다 -
  1차 보관 표(`news_raw_kmib_created_at_fix`)와 따로 `news_raw_created_at_fix_2`를 써서 1차 보정을 되돌리지 않고 더 고친다. 절차는 런북 6.10 "2차 보정". **실행하지 않았다**(수정을 배포한 뒤에 한다).
  대상 질의는 수집기가 다시 고르는 행을 뺀다 - 추출기의 대상 조회와 같은 조건으로: 추출 상태 `NULL`, 또는 `fetch_failed`·`error`이면서 시도 3회 미만이고 본문을 비운 적이 없고(`raw_news_content_purged_at IS NULL`) 수집 뒤 30일 안인 행. 시도 횟수만 보면 수집 30일이 지났거나
  본문을 비운 `fetch_failed`·`error` 행이 수집기(다시 고르지 않는다)에도 2차 보정(뺀다)에도 걸리지 않는다. 질의와 추출기의 대상 조회가 한 행도 겹치거나 빠지지 않는지는 integration 테스트가 런북의 SQL을 그대로 꺼내 확인한다.

### 검증
`tests/test_content_extractor_page_pubdate.py`(읽기와 판정: 최상위 기사 노드만 - 중첩 관련 기사 · `ItemList` · `image` · `WebPage` · 값 객체는 읽지 않음, `@graph` · 최상위 배열 · `@type`의 IRI·배열 표기, 깨진 JSON을 정규식으로 읽는 조건과 버리는 조건, `RecursionError`, JSON-LD와 meta의 충돌 경계 10분,
경계 +10분 · −7일, 바꾼 행 기록의 UTC 표기), `tests/test_content_extractor_created_at.py`(미래 행만 바뀜 · 같은 트랜잭션 · `duplicate` 폴백 · 이미 고쳐진 행 · `fetch_failed`·`error`인 미래 행의 분류 · 통계 키 · 바꾼 행 목록의 순서·상한 200·넘침 건수 · 대상 조회 ·
런북의 2차 보정 대상 질의·되돌리기 질의가 코드와 같은 조건·키를 쓰는지),
`tests/test_build_kmib_created_at_fix.py`(생성기 새 인자, 인자 없이 부른 출력이 예전과 글자 하나까지 같음), PostgreSQL 통합 테스트 `tests/integration/test_extract_created_at_from_page_db.py`(실제 `UPDATE ... RETURNING`과 경계 - 대상을 고른 뒤 값이 낡았어도 수집 + 10분인 행은 SQL 가드가 0행으로 막음, 세션 TimeZone이 UTC·Asia/Seoul·America/Los_Angeles여도 같음,
목록의 전 값은 지금 행의 값, 런북의 되돌리기 질의로 행을 복원하고 그 뒤 다시 바뀐 행은 덮지 않음, 런북의 2차 보정 대상 질의와 추출기의 대상 조회가 한 행도 겹치거나 빠지지 않음) · `tests/integration/test_fix_kmib_created_at_sql.py`(2차 보정과 되돌리기가 1차 보관 표를 건드리지 않음).
페이지는 모두 구조만 흉내 낸 지어낸 HTML이다(ADR 0023). 통합 테스트는 CI의 PostgreSQL에서 돈다 - 이 작업에서는 로컬에서 돌리지 않았다(로컬 DB 서버를 띄우지 않는다). 새 동작마다 고친 코드를 되돌려(변이) 새 테스트가 실패하는 것을 확인했다(통합 테스트 제외).

### 결과와 한계
- **미래로 드러나는 오류만 고친다.** RSS 값이 틀렸는데 수집 시각보다 앞서는 행은 이 규칙으로 가려내지 못하고 그대로 둔다. 위 441행에도 그런 행이 2행 있었다: 저장값이 수집보다 앞섰지만 저장 날짜가 URL 날짜의 하루 뒤인 앞의 440행과 같은 모양이라 1차 보정의 선정 기준에 걸려 들어갔고(수집 지연 3.43 · 5.35시간으로 읽히던 것이
  페이지 값으로는 1.09 · 0.07시간), 이 규칙은 그 모양을 보지 않고 미래만 보므로 그런 행은 놓친다. 그런 행이 얼마나 있는지, 지연 통계를 어느 쪽으로 틀리게 하는지는 모른다. 페이지 값이 RSS 값보다 맞다는 근거가 미래인 행(441행, 독립 확인 16쌍)에서만 있어서 모든 행을 페이지와 대조하는 쪽(대안 4)은 택하지 않았다.
- **페이지 값을 사실로 둔 것은 가정이다**(위 "결과와 한계"와 같다). 이 수정은 그 가정을 미래 행에 한해 수집기에 넣은 것이다.
- **읽기가 엄격해서 못 고치는 페이지가 있을 수 있다.** 기사 노드가 최상위가 아니거나(`WebPage` 아래 `mainEntity`) `@type`이 기사가 아니거나 `datePublished`가 문자열이 아니거나(`{"@value": ...}`) 깨진 덩어리가 모호하면 JSON-LD 값은 없는 것이고, meta도 없으면 `no_page_time`이다.
  다른 기사의 시각을 이 행에 쓰는 것보다 못 고치고 세는 쪽을 택했다. 국민일보의 지금 페이지에서 이 규칙이 값을 내는지는 이 작업에서 페이지를 다시 받아 확인하지 않았다(페이지를 저장소에 넣지 않는다) - 배포 뒤 `created_at_unfixed`의 `no_page_time` · `conflict`가 신호다.
- **1차 보정의 독립 확인 16쌍과의 관계.** 1차 보정은 페이지의 첫 `"datePublished"` 값을 정규식으로 읽었고 그 값이 16쌍 모두 개편 전 RSS와 10분 안이었다. 새 규칙은 페이지에 서로 다른 `datePublished`가 하나뿐이면 같은 값을 읽는다(최상위 기사 노드의 값이든 깨진 덩어리의 유일한 값이든).
  값이 여럿인 페이지에서는 첫 값 대신 최상위 기사 노드의 값을 읽거나, 모호하면 읽지 않는다(`no_page_time`). 그래서 그 16쌍이 근거로 남는 것은 새 규칙이 같은 값을 읽는 페이지까지이고, 16쌍의 페이지를 새 규칙으로 다시 돌려 보지는 못했다(페이지를 보관하지 않았다).
- **못 고친 행은 미래로 남는다**(`created_at_unfixed` - 페이지를 읽고도 못 쓴 행뿐 아니라 `fetch_failed`·`error`로 끝난 미래 행도 센다). 지연 p95 판정에서는 위 보정과 같이 대조하지 못한 행을 최악(99시간)으로 읽는다. 국민일보 페이지가 JSON-LD를 빼면 `no_page_time`이 늘어 통계로 드러나지만, 아직 경고는 없다.
- **원인은 여전히 모른다.** 이 수정은 증상을 막을 뿐이다. RSS가 왜 그런 값을 주는지는 가르지 않았고, 이상이 멈추거나 다른 언론사로 번지는지는 배포 뒤 `created_at_from_page`로 본다.
- **미배포.** Tier 0은 이 수정이 없는 코드로 돈다. 배포 전까지 새 행은 미래 값으로 들어오고 위 30행은 그대로다. 배포한 뒤: ① 첫 ingest의 `stats.extract.created_at_from_page`에 국민일보 건수가 찍히는지(국민일보 새 행이 있는 회차에서), `created_at_unfixed`가 비었거나 사유가 보이는지 ② 읽기 전용으로 새 행 중
  미래 값이 남았는지(런북 6.10 6단계 질의) ③ 이미 추출이 끝난 행을 2차 보정으로 정리한다(배포 전에 하면 그 사이 새 미래 행이 생기므로 배포 뒤 한 번). 배포 기록은 그때 이 절 위의 상태 줄에 더한다.
