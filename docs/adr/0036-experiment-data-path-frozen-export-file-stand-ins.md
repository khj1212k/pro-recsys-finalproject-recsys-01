# ADR 0036: LLM 실험의 데이터 경로 — 동결 반출, 원격 임베딩 팩, 파일 대역 (운영 DB에 쓰지 않는다)

## 상태
제안됨 (2026-10-06). 반출 도구·임베딩 팩 형식·파일 대역과 그 테스트는 이 ADR과 함께 들어갔다.
**실제 반출, 실제 임베딩, E0 실행은 하지 않았다 — 이 ADR에는 실제 기사로 얻은 결과가 하나도 없다.**

채택으로 올리는 조건은 세 가지다.
1. 사용자 승인([실행 계획](../design/2026-10-06-llm-v2-execution-plan.md) 5절의 결정 2 반출, 결정 6 ADR 0023 개정, 결정 9 운영 코드 무변경).
2. [ADR 0023 개정(2026-10-06)](0023-data-sources-copyright-retention.md)이 main에 있다. 그 전에는 반출하지 않는다(ADR 0009 A8.0의 2단계).
3. 첫 실제 반출의 매니페스트(`identity_sha256`, 행 수, 걸린 시간, 반출 중 VM 여유 메모리)와 임베딩 팩 검증 결과를 아래 "증거"에 날짜와 함께 더한다.

번호: 실행 계획 2.3절은 이 ADR을 0035로 예상했다. 그 번호는 LLM 지출 상한 ADR이 쓰고 있어(PR #25) 0036으로 한다.
설계 문서와 이 ADR이 다르면 이 ADR이 옳다.

## 컨텍스트
- **무엇을 돌리려는가.** [ADR 0009 A8](0009-llm-eval-protocol-and-preregistered-decision-rule.md)이 사전 등록한 E0은 main의 뉴스레터 생성 경로를
  **코드 그대로** 실제 수집 기사에 돌려 재는 기준선이다. 그 경로는 기사 본문과 BGE-M3 임베딩을 DB에서 읽고, 결과를 DB에 쓴다.
- **기사가 있는 곳.** Tier 0 VM(E2.1.Micro, 1 GB) 한 곳이다([ADR 0026](0026-hosting-tiers-and-contingency.md)). 2026-10-06 15:53 KST에 읽기 전용
  트랜잭션(`REPEATABLE READ READ ONLY`, `statement_timeout 15s`)으로 **건수·스키마·실행 계획만** 조회했다. 본문과 제목은 읽지 않았다.

  | 항목 | 값 `[KR-ops]` |
  |---|---|
  | `news_raw` | 15,086행. `ok`이고 본문이 비어 있지 않은 행 13,542. 임베딩 있는 행 **0**. `news_letter_id`가 채워진 행 0 |
  | `ok` 행 중 저장된 본문 해시가 없는 행 | 0 |
  | 수집 시각 범위(`raw_news_crawled_at`, 시간대 없는 UTC) | 2026-09-26 02:36 ~ 2026-10-06 06:05 |
  | 스키마 | Alembic `d48994e9d26e`(코드 head는 `8b7f830013b7`). `raw_news_crawled_at`은 `timestamp without time zone`, `raw_news_created_at`·`raw_news_extracted_at`은 `timestamp with time zone`, `raw_news_content`는 NOT NULL |
  | 서버 | PostgreSQL 16.15, `TimeZone=Etc/UTC`, `work_mem=4MB`, `shared_buffers=64MB`, `max_connections=30` |
  | 크기 | DB 52 MB. `news_raw` 힙 14 MB + TOAST 24 MB + 인덱스 4.5 MB |
  | 인덱스 | 기본 키, URL unique, `ok` 행의 본문 해시 부분 unique. **수집 시각 인덱스는 없다** → 창 조건은 `news_raw` 순차 스캔이다(`EXPLAIN`: `Seq Scan on news_raw`, 5일 창 추정 5,217행, 비용 2,155) |
  | 06:00 KST 경계의 24시간 창별 `ok` 행 수 | 09-30 1,782 · 10-01 1,740 · 10-02 1,298 · 10-03 462 · 10-04 1,040 · 10-05 1,093 (창 = 그 날짜 06:00 KST부터 24시간) |
  | 호스트 메모리(같은 시각 `free -m`) | MemAvailable 585 MiB, 스왑 사용 85 MiB / 2 GB. 10일 관측의 최솟값은 239 MiB(ADR 0026 증거 8) |

  E0에 필요한 창은 3~5개다(A8.1). 위 숫자로 최근 3개 창은 2,595행, 5개 창은 5,633행이다. 행당 평균 4.16 KB(실행 계획 2.1: 13,346행 55.5 MB)를
  곱하면 약 11 MB / 23 MB다 `[추정]`.
- **임베딩이 없다.** 임베딩이 0개라 운영 클러스터링 쿼리(`embedding_result IS NOT NULL`)는 0행을 돌려준다. BGE-M3는 모델 로드만 3.6 GB라(ADR 0006)
  1 GB VM에서 돌 수 없고, 개발 Mac에서는 서버·DB·임베딩을 돌리지 않는다(ADR 0026).
- **운영 코드가 DB에 닿는 곳** `[코드]`: 기사·임베딩 SELECT(`core/clustering/hdbscan_clusterer.py`의 `_load_data_from_db`), 배치 기록
  (`db/batch_manager.py`의 `create_new_batch`·`update_cluster_log`), 뉴스레터 저장과 `news_raw.news_letter_id` 갱신(`save_news_letter`),
  저장 노드의 뉴스레터 임베딩 UPDATE(`workflow/nodes.py`). 생성 노드는 뉴스레터 임베딩을 위해 BGE-M3를 올린다(`get_shared_embedder`).
- **지켜야 하는 것.**
  - VM을 바꾸지 않는다. 메모리 여유가 작고, 유일한 수집기다.
  - 운영 DB에 실험 산출물을 쓰지 않는다. 저장 노드는 제외된 기사까지 `news_letter_id`를 채운다(설계의 C2, main에 그대로 있다). 그렇게 채워진
    기사는 다음 클러스터링에서 영구히 빠진다.
  - DB 비밀번호는 VM 밖으로 나오지 않는다([런북](../runbook-hosting.md) 4절). LLM 키는 Mac에만 있다.
  - 저장소는 공개다. 상업 언론사 본문은 저장소·리포트·로그에 넣지 않고 수집 후 30일 안에 지운다(ADR 0023).
  - E0은 "고치기 전 코드"를 재야 한다. 생성 경로는 2026-02 이후 완주 기록이 없다.

## 검토한 대안

### 1. 실험이 어디서 읽고 어디에 쓰나
| | A. SSH 터널로 VM 운영 DB에 직접 | D. VM 안 일회용 실험 DB | B. Colab 안 임시 Postgres에서 전부 | **C. 동결 반출 + 파일 (채택)** |
|---|---|---|---|---|
| VM에 가는 부하 | 벡터 2.6k~5.6k건 × 약 4 KB의 UPDATE(11~23 MB)와 생성 중 조회. 여유 메모리 최솟값 239 MiB 위에서 재 본 적 없는 부하 | 같은 Postgres에 DB 하나 더 + 같은 벡터 쓰기. 리허설(ADR 0026 증거 7) 때 여유 최소 583 MiB였지만 벡터 쓰기는 없었다 | 읽기 전용 덤프 1회(DB 52 MB) | **읽기 전용 `COPY` 1회**(창만, 11~23 MB `[추정]`) |
| 운영 DB 오염 | **있다** — `news_letter` 행과 `news_raw.news_letter_id`가 남는다 | 없다(DROP DATABASE). 다만 VM에 쓰기를 한다 | 없다 | 없다 |
| 본문이 놓이는 곳 | VM. 임베딩을 위해 어차피 Colab에도 | 같음 | 덤프 전체(전 기간 본문)가 Colab에 | 창의 본문만 Mac `data/exports`와 Colab(임베딩 동안) |
| 재현성 | 낮다 — DB가 매시 바뀐다 | 중간 — VM이 회수되면 사라진다 | 높다 | 가장 높다 — 반출본 신원과 임베딩 파일의 sha256, 의사 시각으로 클러스터링까지 결정론. 본문이 남아 있는 30일 안에서만 |
| 비밀 | LLM 키는 Mac | Mac | **LLM 키를 Colab에 넣어야 한다** | Mac. DB 비밀번호는 VM을 떠나지 않는다 |
| 운영 코드 변경 | 없음(환경변수) | 없음 | 없음 | 없음(대안 2) |
| 치명적 단점 | 오염, 재지 않은 부하, "VM을 바꾸지 않는다" 위반 | VM에 쓰기, 회수 시 소멸 | 키 이동, 세션 소멸이 유료 호출을 끊음, 설치 비용 | 운영이 쓰지 않는 파일 대역으로 돈다(한계 1) |

### 2. 파일을 운영 코드에 어떻게 끼우나
| | C. 저장소 계약으로 리팩터링한 뒤 실험 | **C′. 운영 코드는 그대로 두고 러너가 접촉점을 바꿔 끼운다 (E0에 채택)** |
|---|---|---|
| E0 전 `ai_workspace/` 변경 | 클러스터러·Stage5·저장 노드를 저장소 객체 주입으로 고침 | **없음** |
| E0이 재는 것 | "main + 리팩터". 완주 기록이 없는 경로를 기준선 전에 고친다 | "main 그대로" |
| 깨지기 쉬움 | 낮다(타입이 있는 계약) | 이름이 바뀌면 조용히 빗나갈 수 있다 → 대안 4의 계약 검사로 막는다 |
| 뒤 실험 | 그대로 재사용 | 같은 대역을 다시 쓸 수 있지만 접점이 늘면 계약이 낫다 |

### 3. 반출의 형식과 경로
- **`pg_dump` 전체**: 창을 고를 수 없다. 전 기간 본문이 Mac에 놓인다. 기각(백업은 런북 6.5가 따로 다룬다).
- **CSV `COPY`**(런북 6.4가 쓰는 형식): 파이썬 `csv` 모듈로 읽으면 NULL(따옴표 없는 빈 칸)과 빈 문자열(`""`)이 구분되지 않고, 본문 안의 개행 때문에
  한 행이 여러 줄에 걸쳐 끊긴 출력을 가려내기 어렵다. 기각.
- **행마다 json 한 줄(`json_build_object(...)::text`)을 `COPY ... TO STDOUT`으로 (채택)**: NULL이 보존되고, json 텍스트에는 날 개행이 없어 한 줄이
  한 행이다. COPY 텍스트 형식이 덧붙이는 것은 역슬래시 두 번 적기뿐이라 되돌리기가 단순하다. `SELECT`가 아니라 `COPY`인 이유: VM 컨테이너 안의
  psql은 기본 설정에서 `SELECT` 결과를 전부 메모리에 모은 뒤 출력하지만 `COPY`는 흘려보낸다.
- **DB에 닿는 길**: (a) SSH 터널 + DSN — DB 비밀번호가 Mac에 있어야 한다. (b) `ssh ... docker compose exec -T db psql`에 스크립트를
  표준입력으로 — 컨테이너 안의 로컬 접속이라 비밀번호가 필요 없다. Tier 0에는 (b)를 쓴다. 도구는 둘 다 받는다(CI와 뒤의 A1은 (a)).

### 4. 대역이 조용히 빗나가지 않게 하는 방법
- **아무것도 하지 않음**: `monkeypatch`류 패치는 대상 이름이 옮겨지면 아무 일도 하지 않은 채 통과한다. 기각.
- **이름과 인자만 확인**: 함수 본문이 바뀐 것(쿼리 조건 추가, 저장 열 추가)을 놓친다. 부족.
- **인자 구성 + 흉내 내는 운영 함수의 본문 sha256 고정 (채택)**: 주석 한 줄만 바뀌어도 멈춘다. E0의 주장이 "생성 경로가 기준 커밋과 같다"이므로
  그 민감함은 의도한 비용이다. 멈추면 사람이 대역을 다시 대조하고 고정값을 갱신한다.

### 5. 임베딩을 어떻게 들여오나
- **VM의 pgvector에 UPDATE**: 대안 1의 A·D와 같은 이유로 기각.
- **파일 팩 (채택)**: `ids.npy` + `emb.f16.npy`(또는 f32) + 잡이 읽은 본문의 해시 + 매니페스트. 받는 쪽이 반출본과 대조한다.
- **float16 저장**: 5,633건 기준 11.5 MB(f32는 23 MB). 저장 정밀도만의 영향은 작다(증거 4). 모델을 fp16으로 **돌린** 영향은 별개이고 아직 재지 않았다.

## 결정
1. **LLM 실험은 운영 DB에 쓰지 않는다.** 입력은 Tier 0 `news_raw`의 동결 반출본과 원격에서 계산한 임베딩 팩이고, 출력은
   `data/experiments/<실험>/<run>/`의 파일이다. VM에는 실험마다 읽기 전용 `COPY` 한 번만 닿는다.
2. **반출** — `scripts/export_news_raw.py`(구현 `evaluation/llm/frozen_export.py`).
   - 트랜잭션 하나: `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` → `SET LOCAL statement_timeout`(기본 120초)·
     `lock_timeout`(5초)·`idle_in_transaction_session_timeout`(60초) → `COPY` 세 번(머리말 1줄, 행, 꼬리말 1줄) → `ROLLBACK`.
     세 `COPY`는 같은 스냅숏을 본다.
   - 서버가 머리말에서 `transaction_read_only=on`, `repeatable read`, 0이 아닌 `statement_timeout`을 돌려주지 않으면 거절한다.
     꼬리말의 행 수와 받은 행 수가 다르면(끊긴 출력) 거절한다. 기대하지 않은 줄이 하나라도 있으면 거절한다.
   - 행 조건: `raw_news_extract_status = 'ok'`, 본문 비어 있지 않음, `start <= raw_news_crawled_at < end`(시간대 없는 UTC).
     창은 최대 120시간이다 — 실험에 필요한 창만 내보낸다. 임베딩 열은 읽지 않는다.
   - Tier 0에서는 매시 :20~:50에만 돌린다(정시 ingest와 겹치지 않게; 도구가 확인하고 `--any-minute`로만 끈다).
   - 받는 쪽은 본문의 sha256을 다시 계산해 DB에 저장된 해시와 비교하고, 다르거나 없는 행의 수를 매니페스트에 적는다.
   - 쓰는 곳: `<메인 체크아웃>/data/exports/<T0>/`(T0 = 서버의 트랜잭션 시작 시각, UTC). git이 추적할 수 있는 경로면 거절하고,
     CLI는 `data/exports` 아래만 받는다. 워크트리가 아니라 메인 체크아웃인 이유: 워크트리는 지워지는 디렉터리라 본문 사본이 정리 대상에서 빠진다.
     디렉터리 0700, 파일 0600. 이미 반출본이 있는 디렉터리는 덮어쓰지 않는다.
   - 본문·제목·URL은 표준출력·로그·오류 메시지에 내지 않는다.
3. **매니페스트** — `manifest.json`. `identity`에 창, 조건, 행 수, **행별 신원의 sha256**(`rows_sha256`: id·언론사·URL·제목 해시·본문 해시·길이·시각·
   만료 시각·만료 면제 여부를 id 순으로 이은 것), 언론사별 건수, 원본의 Alembic 리비전·서버 버전·스냅숏 시각·트랜잭션 속성, 코드 SHA, 본문 만료일(가장 이른 `crawled_at` + 30일)이
   들어간다. `identity_sha256`은 반출 시점에 고정되고 **본문을 지워도 바뀌지 않는다** — ADR 0009 A8.10의 "매니페스트 sha256"에 적는 값이다.
   파일의 sha256은 따로 적고 정리할 때마다 갱신한다(반출 시점의 값도 남긴다).
4. **임베딩 팩** — `evaluation/llm/embedding_pack.py`, `scripts/import_embeddings.py`. 형식은 대안 5. 읽는 쪽은 다음이 모두 맞아야 받는다:
   같은 반출본(`export_identity_sha256`), 같은 id와 순서, 잡이 읽은 본문의 해시 = 반출본의 해시, 1,024차원, NaN·Inf 없음, 단위 길이
   (허용 편차 f16 1e-3, f32 1e-4), 운영과 같은 모델·파라미터(`BAAI/bge-m3`, `max_length` 8192, 텍스트 규칙 `f"{title} {content}"[:8000]`, L2 정규화),
   매니페스트의 파일 sha256. 원격에서 온 파일이라 pickle은 읽지 않는다. 임베딩 잡 자체는 다음 PR이다.
5. **E0은 C′로 돈다** — `evaluation/llm/e0_store.py`. `ai_workspace/`는 바꾸지 않는다.
   - 바꿔 끼우는 이름은 ADR 0009 A8.2 이탈 1·6에 등록된 것이다: `NewsClusterer._load_data_from_db`, `workflow.nodes`의 `get_connection`·
     `release_connection`·`save_news_letter`·`get_shared_embedder`. `db.batch_manager`의 `create_new_batch`·`update_cluster_log` 대역도 있지만
     Stage5를 통째로 돌릴 때만 끼운다(E0은 Stage5를 우회한다, A8.2 이탈 3).
   - 로더 대역은 운영 쿼리의 조건(임베딩 있음, 본문 있음, 수집 시각 하한, id 순)을 그대로 따르고 같은 dict를 낸다. 다른 점은 창의 상한을
     의사 시각으로 닫는 것과, `exclude_clustered`를 적용하지 않는 것이다(A8.2 이탈 7).
   - 저장 대역은 운영 INSERT에 들어갈 값을 운영의 정제 함수로 만들어 JSONL에 남긴다. `news_raw.news_letter_id`는 갱신하지 않고 요청된 id만 기록한다.
   - 뉴스레터 임베더는 텍스트의 sha256만 남기고 `None`을 돌려준다.
   - **끼우기 전에 계약 검사**(대안 4)를 돌리고, 다르면 끼우지 않는다.
   - **대역이 끼워진 동안 실제 DB 연결을 막는다**(`db.connection`의 풀과 접속 설정). 터널이 열려 있거나 DB 환경변수가 잡혀 있어도 운영 DB에
     닿지 않고, 막힌 시도는 기록된다. 운영 저장 노드는 저장 실패를 로그만 남기고 삼키므로, 러너는 끝에서 `assert_clean()`으로 확인해야 한다.
     이 가드는 A8.2의 이탈 목록에 없는 하네스 장치다(바꿔 끼우는 이름이 둘 늘어난다). E0에서 켜고 돌리려면 실행 전에 A8.x 개정으로 등록하고,
     등록하지 않으면 끄고 돈다 — 러너 PR에서 정한다(ADR 0009 A8.10 덧붙임).
6. **저장소 계약(대안 2의 C)은 E0 뒤에 한다.** 그때의 수용 기준은 같은 동결 입력에서 E0이 남긴 창별 멤버십 스냅숏의 sha256을 다시 내는 것이다.
   그래서 `membership_sha256`에는 코드 SHA를 넣지 않는다.
7. **본문 사본의 위치와 30일**은 [ADR 0023 개정(2026-10-06)](0023-data-sources-copyright-retention.md)을 따른다. 이 ADR은 30일을 늘리지 않는다.
   도구가 맡는 부분: 행마다 만료 시각을 적고, 반출본을 여는 함수는 만료된 본문을 돌려주지 않으며 열 때마다 디스크에서도 지운다.
   Mac에는 상주 작업을 둘 수 없어서 주기 잡이 아니라 "열 때마다 정리"다.

## 증거
전부 합성 데이터(지어낸 문장, 난수 벡터)와 VM의 건수·스키마 조회다. 실제 기사로 돌린 것은 없다.

1. **VM 사실**: 컨텍스트의 표(2026-10-06 15:53 KST).
2. **운영 코드 무변경**: `git diff --stat 1a42b46 HEAD -- ai_workspace`가 비어 있다. `1a42b46`의 `ai_workspace/`는 실행 계획·A8의 기준
   `b64da14`와 같다(`git diff --stat b64da14 1a42b46 -- ai_workspace`도 비어 있다).
3. **실제 Postgres에서** — CI integration 잡(PostgreSQL 16 + pgvector, Alembic head, psql 16.15). `tests/integration/test_frozen_export_db.py`
   14건이 실행 `37429092893`(커밋 `d656b25`), `37429343261`(`ddbf9e0`, 연결 경로의 출력을 메모리로 받게 고친 뒤),
   `37430532197`(`3f8dcfa`, 만료 시각을 행의 신원에 넣은 뒤 - 이 PR의 마지막 코드 커밋)에서 통과했다.
   integration 잡 전체는 98 passed · 1 skipped(이 테스트를 넣기 전에는 84 passed · 1 skipped). 로컬에서는 DB를 띄우지 않아 돌리지 않았다.

   | 확인 | 결과 |
   |---|---|
   | 반출 트랜잭션 안의 `UPDATE`·`INSERT`·`DELETE`·`nextval`·`CREATE TABLE` | 5개 모두 `ReadOnlySqlTransaction`으로 거절, 테이블 지문(행 수 + 전체 행 md5) 불변 |
   | 반출 도중 다른 세션이 커밋한 창 안의 행 | 섞이지 않음(받은 5행 = 꼬리말 5행) |
   | `statement_timeout` 1초에서 `pg_sleep(5)` | `QueryCanceled`, 그 뒤 연결 사용 가능 |
   | 창의 경계 | 하한과 같은 시각 포함, 상한과 같은 시각 제외. `dropped`·`fetch_failed`·미처리 행 제외 |
   | 따옴표·역슬래시·탭·CRLF·`\N`·`\.`·U+2028·그림 문자가 든 본문 | 글자 그대로. 파이썬 sha256 = DB의 `encode(sha256(convert_to(...)), 'hex')` |
   | 시각 | `+09:00`으로 넣은 값이 UTC로, NULL은 null로 |
   | psql 경로 | 연결 경로와 `rows_sha256`·본문이 같음. 스크립트에 `UPDATE`를 끼우면 종료 코드 3, 테이블 불변 |
   | 임베딩 팩 | DB의 벡터로 만든 팩은 통과. id 누락·다른 본문 해시·512차원·길이 1.5·다른 반출본·다른 모델은 각자의 사유 코드로 거절 |
   | 파일 대역 로더 대 운영 `_load_data_from_db` | 같은 DB 행에서 같은 dict(id, 제목, 언론사, 본문, float32 임베딩 완전 일치). 운영 쿼리가 빼는 행(임베딩 없음, 24시간 밖, `dropped`)이 있는 상태 |
4. **DB 없이**(단위 테스트 85건: 반출 40, 팩 17, 대역 28):
   - 끊긴 출력·기대하지 않은 줄·읽기 전용을 확인해 주지 않는 머리말 거절. git이 추적하는 경로, `.gitignore`를 무시하고 추적시킨 파일이 있는 경로,
     `.git` 안 거절. 만료된 본문은 파일을 고칠 수 없을 때도 메모리로 나오지 않음. 정리 멱등, 정리 뒤 `identity_sha256` 불변, 본문 파일만 바뀐 채
     끊긴 정리의 복구. 파일에서 만료 시각을 뒤로 고치거나 면제로 바꾸면 읽기와 정리가 모두 거절(만료 시각은 행의 신원에 들어 있다).
   - 저장 대역 = 운영 `save_news_letter`의 INSERT 인자(None, bytes, NUL, 반쪽 대리 문자, numpy 값). 운영 저장 노드·임베딩 노드를 대역 위에서 그대로 호출.
   - 합성 기사 50건(5주제 × 8 + 흩어진 10) → 운영 `NewsClusterer` → 운영 그래프(페이크 LLM) → 저장 대역 → 읽기. 다섯 주제가 각각 8건 한 무리로
     묶였고, HDBSCAN이 한 주제에 붙인 흩어진 2건은 운영 `split_v2`가 떼어 냈다. 같은 입력·같은 의사 시각이면 `membership_sha256`이 같다.
   - 고치지 않은 `Stage5.execute`가 대역 위에서 끝까지 돈다(cluster_history 대역 포함).
   - 운영 함수의 인자가 바뀌거나, 본문이 바뀌거나, 이름이 없어지면 계약 검사가 그 이름을 대며 실패한다.
   - float32 → float16 저장만의 영향: 합성 단위 벡터 500개에서 코사인 최솟값 > 0.999999, 합성 50건의 멤버십 동일.
5. **전체 단위 테스트**(macOS, Python 3.11, CI와 같은 설치 순서, `nice -n 19`, 스레드 2): 이 브랜치를 만든 시점의 main `1a42b46`은
   1,560 passed · 30 skipped, 이 브랜치의 `3f8dcfa`는 1,645 passed · 30 skipped(늘어난 85건이 4번의 테스트다). CI `test` 잡(Linux)은
   main `1a42b46`에서 1,532 passed · 58 skipped(실행 `37425618246`), 이 브랜치 `3f8dcfa`에서 1,617 passed · 58 skipped(실행 `37430532197`)로
   같은 85건이 늘었다. 로컬과 CI의 건너뛴 수가 다른 이유는 이 PR에서 따지지 않았다(main에서도 같은 차이다).

**아직 없는 증거**(채택 조건 3): 실제 반출의 행 수·걸린 시간·반출 중 VM 여유 메모리, 실제 매니페스트의 `identity_sha256`, 실제 임베딩의
fp16 대 fp32 코사인(p10, 최솟값), Colab 연산 단위.

## 결과와 한계
1. **E0은 운영이 쓰지 않는 저장 경로로 돈다.** DB 풀, 운영 SQL의 실행, `news_raw.news_letter_id` 갱신, 카테고리 매핑, pgvector 쓰기는 E0에서
   실행되지 않는다. 로더가 운영 쿼리와 같은 결과를 낸다는 것은 CI의 합성 행에서 확인한 것이고, 저장 쪽은 "운영 INSERT의 인자와 같은 값"까지다.
   E0의 수치는 생성·게이트·판정에 관한 것이지 저장 경로에 관한 것이 아니다.
2. **계약 검사는 일부러 민감하다.** 고정한 운영 함수 8개(`_load_data_from_db`, `cluster_news`, `save_news_letter`, `create_new_batch`,
   `update_cluster_log`, `save_newsletter_to_db`, `embed_newsletter_node`, `initialize_cluster_processing`) 중 하나라도 바뀌면 — 주석만 바뀌어도 —
   대역이 끼워지지 않고 `tests/evaluation/test_e0_store.py`가 실패한다. 그 함수를 고치는 PR(E0 뒤의 정합 패치가 저장 노드를 고친다)은 대역을
   다시 대조하고 `SOURCE_PINS`를 갱신해야 한다. 고정값이 가리키는 것은 "함수 본문"이지 그 함수가 부르는 다른 함수가 아니다.
3. **창의 상한은 운영에 없는 조건이다.** 운영에서는 `NOW()`가 자연 상한이다. 동결본에는 의사 시각 뒤에 본문 추출이 끝난 기사도 `ok`로 들어 있다 —
   반출에 `extracted_at`이 있으므로 그런 행의 수를 E0 리포트가 센다(A8.2 이탈 7).
4. **`raw_news_crawled_at`을 UTC로 읽는다.** 컬럼에 시간대가 없다. 서버의 `TimeZone`은 `Etc/UTC`이고 수집기는 UTC로 쓴다(ADR 0026).
   2026-10-06 조회에서도 서버 시각 06:53 UTC에 가장 최근 수집 시각이 06:05였다(정시 ingest와 맞는다). 이 가정이 틀리면 창이 9시간 어긋난다.
5. **"지운다"는 파일에서 본문을 없앤다는 뜻이다.** APFS·SSD에서 옛 블록이 복구 불가능하게 사라지는 것까지 보장하지 않는다. Time Machine이나
   동기화 폴더가 `data/`를 담고 있으면 그쪽 사본은 이 도구가 지우지 못한다(런북의 반출 절에서 확인한다).
6. **반출은 한 번에 메모리로 받는다.** 창이 120시간으로 묶여 있어 수십 MB다. 더 큰 반출이 필요해지면 이 ADR을 고친다.
7. **VM에서 재지 않은 것.** 순차 스캔 + TOAST 읽기 11~23 MB가 1 GB VM에서 얼마나 걸리고 메모리를 얼마나 쓰는지는 첫 실제 반출에서 잰다.
   `statement_timeout` 120초가 모자라면 반출이 실패하고(아무것도 쓰지 않는다) 그 사실을 적은 뒤 값을 올린다.
8. **30일.** 반출본으로 재현할 수 있는 것은 가장 이른 기사의 수집일 + 30일까지다. 그 뒤에는 로더가 그 창을 거절한다. 멤버십 스냅숏(id만)과
   임베딩 팩은 남지만 본문이 없으면 생성을 다시 돌릴 수 없다.
9. **VM의 본문 보존 잡은 여전히 없다**(ADR 0023 TODO). 이 ADR의 도구는 Mac의 반출본만 정리한다.
