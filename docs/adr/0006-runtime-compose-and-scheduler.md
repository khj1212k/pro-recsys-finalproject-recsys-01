# ADR 0006: 런타임 구성 - docker compose + supercronic, Mac 개발 중 임베딩은 호스트 MPS

## 상태
채택됨 (2026-09-26). 초안(측정 전 사전 등록 규칙 포함)은 커밋 `2c0d193`. 7일 연속 수집 뒤
`job_runs` 성공률로 다시 확인한다(아래 "결과와 한계"). 같은 날 리뷰 반영으로 결정 8~10과 증거 2·6·8을 고쳤다
(사전 등록 규칙 문구는 그대로 두고, 틀린 전제는 증거 6에 정정으로 적었다).

> 범위 종결 C-27 (2026-10-18): 유보 - 7일 확인은 ADR 0026 결정 9를 배포한 뒤 7일의 `job_runs`로 한다. 지금 정시 회차 성공률은 기준 밑이라 "운영 중"이라고 쓰지 않는다([목록](../design/2026-10-18-scope-closure.md)).

## 컨텍스트
- 팀 시절 배치는 Airflow DAG이 `ai_workspace/main.py` 단계를 직접 불렀다. 배포 대상으로 잡은
  Oracle Always Free A1(arm64, 2 OCPU / 12GB)은 아직 확보하지 못했고("Out of host capacity"),
  당분간 모든 것이 Apple M2 16GB 노트북 한 대(colima VM) 위에서 돈다. 이 노트북에서는 다른 작업
  (다른 워크트리의 평가·임베딩, 브라우저 등)이 동시에 돈다 - 아래 성능 수치는 모두 이 조건의 값이다.
- LLM 평가셋(클러스터 40개 층화 추출)을 만들려면 한국어 기사 5~7일치가 연속으로 쌓여야 한다.
  RSS는 피드마다 최근 20~50건만 보여 주므로(세계일보는 20건 ≈ 2.7시간 분량) 수집이 몇 시간만
  끊겨도 기사를 영구히 놓친다.
- 컨테이너(Linux VM) 안에서는 Apple GPU(MPS)를 쓸 수 없다. 2026-09-25 첫 수집의 컨테이너 CPU
  임베딩은 모델 로드 174초, 355건 중 첫 배치(8건)가 22분 안에 끝나지 않아 중단했다(당시 VM 8GB,
  호스트 과다구독 상태라 깨끗한 수치는 아니다).
- 2026-09-26 호스트 MPS로 백로그 432건을 처리하던 중 파이썬 프로세스 메모리가 14GB까지 커지고
  호스트 스왑(약 28GB)이 가득 찼다. 원인은 두 가지였다(증거 4): (1) `NewsEmbedder`가 `max_length=8192`로
  인코딩하고 긴 기사 8건이 한 배치에 모여 eager attention의 (배치 x 헤드 x L x L) 점수 텐서가 수 GB가 됐다
  (transformers 4.38.2의 XLM-R은 SDPA가 아니라 eager attention, FlagEmbedding 1.2.5는 배치를 최장 길이로 패딩).
  (2) 배치를 고친 뒤에도 MPS 할당자가 길이가 다른 큰 텐서 블록을 재사용하지 못하고 캐시에 쌓았다.

## 검토한 대안

### 1. 스케줄러
1. **Airflow standalone(compose 프로필로 격리)** - 팀 시절 DAG을 그대로 살리고 웹 UI·재시도·백필이 있다.
   대신 api-server/scheduler/dag-processor/triggerer가 상시 떠 있고(유휴 1.0~1.1GiB, 59개 프로세스 - 증거 2),
   메타데이터 DB가 따로 필요하다. 프로필로 "기본은 꺼 둔" 채 유지하는 안도 검토했지만, 두 스케줄러가
   같은 잡을 동시에 스케줄링하는 사고 위험이 남고 이미지는 CI에서 빌드도 깨져 있었다.
2. **supercronic + `python -m jobs.run <job>` (채택)** - 컨테이너 하나에 crontab 한 장(유휴 11~45MiB, 익명 메모리 약 4MB - 증거 2).
   실행 기록은 `job_runs` 테이블, 겹침 방지는 Postgres advisory lock, 실패 알림은 `jobs/notify.py`.
   재시도·백필 UI는 없다 - 잡이 재실행 안전(idempotent)하고 다음 주기가 이어받는 구조로 대신한다.
3. **호스트 cron/launchd만** - 가장 가볍지만 서버(Linux)와 Mac의 실행 경로가 갈라진다.
   Mac에서 MPS가 필요한 임베딩 한 가지에만 쓴다(아래 2).

### 2. 임베딩 실행 위치 (Mac 개발 중)
1. 컨테이너 CPU - 서버와 같은 경로. 배치를 고친 뒤 같은 기사에서 MPS보다 약 2~2.3배 느리다(증거 3).
   더 큰 문제는 메모리다: BGE-M3 fp32 로드에만 익명 메모리 3.6GB가 필요해 4GB VM에서는 모델 로드 중
   OOM으로 죽고, VM을 8GB로 두면 그 메모리를 VM이 상시 점유해 16GB 노트북이 스왑에 몰린다(증거 5).
2. **호스트 MPS + launchd (채택)** - 같은 `jobs.run embed`를 호스트 파이썬(`.venv-jobs`, worker 이미지와 같은
   잠금 파일)으로 돌린다. DB·advisory lock·`job_runs`를 공유하므로 컨테이너 잡과 겹쳐 돌지 않는다.
   메모리(최대 6.4GB)는 실행하는 동안(하루 수십 분)만 쓰고 프로세스가 끝나면 돌려준다.
3. ONNX/int8 양자화 CPU - 측정하지 않았다. 임베딩 값이 바뀌므로 도입하려면 별도 ADR과 품질 비교가 필요하다.

### 3. 임베딩 입력 길이 상한(max_length) - 측정 전 사전 등록한 규칙 (커밋 `2c0d193`)
측정 대상은 그 시점까지 수집된 본문 있는 기사 전부(`f"{title} {content}"[:8000]`, 수집 잡과 같은 텍스트),
토크나이저는 BAAI/bge-m3(특수 토큰 포함 길이).

- 후보 L ∈ {1024, 2048, 4096}. 기준선은 L=8192(현재 값, 8000자 절단 때문에 사실상 무절단).
- 조건 (a) 절단 비율: 토큰 길이 > L인 기사 비율 ≤ 2%.
- 조건 (b) 절단 영향: 잘리는 기사들에 대해 cos(emb_L, emb_8192)의 중앙값 ≥ 0.95.
  emb_8192는 배치 1, CPU(fp32)로 계산해 MPS 메모리 문제와 섞이지 않게 한다.
- 결정: (a)와 (b)를 모두 만족하는 가장 작은 L을 쓴다. 하나도 없으면 L=8192를 유지하고
  4096 토큰 초과 기사만 배치 1로 처리한다.
- 보조 보고(결정에 쓰지 않음): 잘린 기사의 top-5 이웃(전체 기사 emb_8192 기준) 보존율, 최소 코사인.
- 어떤 L이든 배치는 토큰 길이로 정렬해 묶고, 배치 크기 x (배치 내 최대 길이)^2 상한으로 메모리를 묶는다.

## 결정
1. **compose 서비스**: `db`(pgvector/pgvector:pg16, `pgdata` 볼륨, 루프백 5433), `migrate`(1회성:
   vector extension → `alembic upgrade head` → 참조 데이터 시드), `api`(FastAPI 전용 슬림 이미지 - torch·Airflow 없음),
   `scheduler`(worker 이미지 + supercronic, `restart: unless-stopped`), `worker`(`tools` 프로필, 수동 실행용).
   이미지는 `python:3.11-slim` 기반 멀티아키텍처(CI는 linux/amd64 빌드만, arm64는 로컬 colima에서 빌드).
2. **잡 CLI**: 모든 배치는 `python -m jobs.run <job>` 하나로 부른다(ingest, embed, cluster, generate, popularity,
   user_embed, train, batch_fallback, daily_report). 실행마다 `job_runs` 행(started/finished/status/stats JSONB/error/git_sha),
   잡별 advisory lock, 실패 시 알림·0이 아닌 종료 코드. SIGTERM/SIGINT는 `failed (terminated by SIGTERM)`과
   부분 stats로 기록하고 락을 푼다. `JOBS_DISABLED`에 있는 잡은 DB에 닿지 않고 0으로 끝난다.
3. **스케줄**(`docker/crontab`, UTC): `ingest --stages rss,extract` 매시 05분, `embed --time-budget-s 2400` 매시 20분,
   `popularity` 매시 35분, `cluster`(통계만, LLM 없음) 매일, `daily_report` 매일. `generate`/`user_embed`/`train`/
   `batch_fallback`은 LLM 평가 프로토콜·사실성 게이트와 랭커 v2가 결정될 때까지 켜지 않는다.
   수집과 임베딩을 다른 잡으로 나눈 이유: 몇 시간 걸릴 수 있는 CPU 임베딩이 ingest 락을 잡고 있으면
   그동안의 매시 수집이 `skipped (lock_held)`로 건너뛰어진다.
4. **Airflow**: compose 프로필과 이미지(`docker/airflow.Dockerfile`)를 제거했다. 팀 시절 DAG은 같은 CLI를 부르는
   얇은 형태로 `backend/airflow/dags/`에 "실행하지 않음" README와 함께 보관한다.
5. **Mac 개발 환경**: `docker/compose.mac.yaml`이 스케줄러의 embed 줄을 끄고(`JOBS_DISABLED=embed`) 호스트 BGE-M3
   가중치를 읽기 전용으로 마운트한다. 임베딩은 `scripts/mac_embed_agent.sh`가 설치하는 launchd 에이전트
   (`com.newsletter-recsys.embed`, 매시 20분 + 로그인 시, `caffeinate -i`로 실행 중에만 잠자기 방지)가 MPS로 돌린다.
   수집 연속성을 위해 전원 연결 시 잠자기를 막는 `keep-awake` 에이전트(`caffeinate -s`)를 선택으로 둔다.
   colima VM은 4GB로 둔다(8GB면 16GB 노트북이 스왑에 몰린다 - 증거 5).
6. **max_length는 8192 유지**(사전 등록 규칙의 결과 - 증거 6). 배치는 토큰 길이로 정렬하고
   배치 크기 x min(최대 길이, max_length)^2 ≤ 8 x 1024^2로 묶는다. 이 예산에서는 2048 토큰을 넘는 기사가
   혼자 배치된다 - 규칙이 요구한 "4096 초과는 배치 1"보다 보수적이며, 배치 구성은 임베딩 값을 바꾸지 않는다
   (패딩은 attention mask로 가려진다). 하위 배치마다 장치 캐시(`torch.mps.empty_cache()`)를 비운다.
7. **korea.kr 정책브리핑(공공누리 1유형) 소스는 추가하지 않는다.** 2026-09-26 리뷰에서 추가하기로 했지만,
   정책브리핑 RSS가 2026-07-01부로 중단됐다(공지 "정책브리핑 RSS 서비스 제공 중단 안내", 사유: "콘텐츠 저작권 등
   권리 보호에 따른 제공방식 변경"). 사이트 스크래핑은 그 사유와 충돌하고, 공공데이터포털 API는 계정·키 발급이
   필요해 사용자 결정으로 넘긴다.
8. **스케줄러 종료 신호는 잡까지 전달한다.** supercronic은 잡마다 새 프로세스 그룹을 만들고 SIGTERM을 받으면 잡에
   신호를 보내지 않고 끝나기만 기다린다 - `docker compose stop`/재배포 때 잡이 유예 뒤 SIGKILL로 죽어 `abandoned`로만
   남았다(tini -g도 supercronic의 그룹까지만 닿는다, 증거 8). `docker/scheduler-entrypoint.sh`가 supercronic의 자식마다
   그 프로세스 그룹에 신호를 보내고, `stop_grace_period`는 60초다.
9. **조용한 실패를 알림으로.** 결과를 못 남긴 실행(OOM/SIGKILL)은 다음 실행이 `abandoned`로 바꾸면서 알린다.
   `job_runs.stats`는 끝에서만이 아니라 도중에도 쓴다(ingest 단계마다, embed 배치마다). ingest는 본문 다운로드 실패
   (`fetch_failed`+`error`)가 대상 5건 이상 중 50% 이상이면 실패, 한 언론사 3건 이상이 전부 실패하면 경고한다.
   추출 중 예외(`error`)도 시도 횟수를 올려 `MAX_EXTRACT_ATTEMPTS`(3)까지만 다시 받는다.
   **2026-10-17:** 이 실패 조건은 [ADR 0026](0026-hosting-tiers-and-contingency.md) 결정 9가 언론사별로 바꿨다 - 한 언론사만의
   실패(Tier 0에서 동아일보가 VM 출발 요청을 거부)는 경고로 알리고, 실패가 한 언론사로 설명되지 않을 때만 실행을 실패시킨다.
10. **본문 해시로 중복 기사를 거른다**(Alembic `d48994e9d26e`). 동아일보 total.xml은 같은 기사를 `/news/list/...`로
    내보냈다가 분류 뒤 `/news/It/...`처럼 섹션 경로로 다시 내보낸다 - URL UNIQUE로는 못 막는다(증거 1).
    정제 본문의 sha256을 `raw_news_content_sha256`에 저장하고 `'ok'` 행 사이 부분 unique 인덱스로 막는다. 사본은
    `'duplicate'`(본문·임베딩 없음)로 남아 임베딩·클러스터에서 빠진다. 언론사별 URL 정규화 대신 해시를 고른 이유:
    규칙을 언론사마다 유지할 필요가 없고, 본문 30일 보존(해시는 남김) 결정과 맞는다. 대가: 사본도 한 번은 받아 온다.

## 증거
측정 환경: MacBook(Apple M2, 16GB, 전원 연결), macOS 26, colima 0.10.3(vz, aarch64, 4 vCPU),
Docker 29.8.1(client)/29.5.2(engine), Compose 5.5.1, buildx 0.37.1, torch 2.14.0, FlagEmbedding 1.2.5,
transformers 4.38.2. 호스트에서 다른 작업이 동시에 돌았다(측정 시점 스왑 사용량을 함께 적는다).

### 1. 첫 수집과 이후 매시 수집 (`job_runs`)
| 실행 | 시각(UTC) | 결과 | 소요 | RSS 신규 | 본문 ok / 버림 | 비고 |
|---|---|---|---|---|---|---|
| #1 | 09-25 16:50 | abandoned | - | 381 | 355 / 26 | RSS 29초, 추출 2분 9초. 컨테이너 CPU 임베딩이 끝나지 않아 중단(다음 실행이 abandoned로 표시) |
| #2 | 09-25 17:31 | succeeded | 308초 | 50 | 49 / 1 | 한국경제 피드 403 → User-Agent 명시로 해결 후 첫 수집 |
| #4 ~ #10 | 매시 05분 | succeeded | 18~28초 | 1 / 6 / 19 / 26 | 전부 ok | 야간(KST 03~06시) |
| #14 | 09-25 22:05 | succeeded | 94초 | 37 | 37 / 0 | 호스트 스왑이 가득 찬 상태(추출 56초) |
| #19 | 09-25 23:05 | succeeded | 37초 | 28 | 25 / 3 | colima 4GB로 재시작 후 |

누적(2026-09-25 23:25 UTC, 수집 시작 후 약 6.5시간, 첫 실행은 피드의 최근 100시간 창을 한 번에 받음):
| 언론사 | 수집 | 본문 ok | 버림 | 임베딩 |
|---|---|---|---|---|
| 전자신문(IT·AI·과학 3피드) | 133 | 118 | 15 | 118 |
| 동아일보 | 80 | 76 | 4 | 76 |
| 매일경제 | 63 | 62 | 1 | 62 |
| 한국경제 | 63 | 60 | 3 | 60 |
| 세계일보 | 61 | 60 | 1 | 60 |
| 경향신문 | 58 | 55 | 3 | 55 |
| AI타임스 | 52 | 52 | 0 | 52 |
| 국민일보 | 38 | 35 | 3 | 35 |
| 합계 | 548 | 518 | 30 | 518 (본문 있는 기사 전부, L2 노름 1 ± 1e-3) |

위 합계에는 URL만 다른 같은 기사 2쌍(동아일보, 본문·임베딩 동일)이 들어 있다 - 결정 10의 마이그레이션이
뒤 사본 2건을 `duplicate`로 바꾼다(2026-09-26 운영 DB 읽기 전용 미리보기: 같은 해시 그룹 2개, 바뀔 행 2건).
첫 실행 #1은 stats가 `{}`로 남아(끝에서만 기록하던 때) 위 #1 수치는 컨테이너 로그에서 옮겼다 - 결정 9 이후로는
강제 종료된 실행도 `job_runs`에 진행 상황이 남는다.

수집 잡(rss,extract)의 컨테이너 메모리는 관측 최대 376MiB(추출 워커 8개, 36개 프로세스, `docker stats` 4회 표본이라 과소추정 가능).

### 2. 스케줄러 유휴 메모리 (`docker stats`, 30초 간격 8회, 2026-09-25 22:12~22:16 UTC)
| 구성 | 메모리 | 프로세스 수 | 유휴 CPU |
|---|---|---|---|
| supercronic 스케줄러 컨테이너 | 12.2MiB (8회 모두 12.15~12.16MiB) | 10 | 0.00~0.30% |
| Airflow 3.1.6 standalone(LocalExecutor, 메타DB는 같은 Postgres 서버, DAG 4파일 일시정지) | 1.03 → 1.13GiB (4분 동안 증가) | 59 | 4~75% (dag-processor 재파싱·하트비트) |
| 참고: api / db | 80MiB / 81~85MiB | 4 / 13~15 | ~0.5% / 2~6% |

Airflow는 공식 이미지(`apache/airflow:3.1.6-python3.11`)로 따로 띄워 쟀다(잡 venv 없음 - 유휴 메모리에는 영향 없음).
`docker stats` 값에는 회수 가능한 페이지 캐시가 들어 있다. 위 12.2MiB는 스케줄러 시작 직후 값이고, 잡이 여러 번 돈 뒤에는
45.2MiB(리뷰 시점, 익명 4.4MB - 나머지는 잡이 남긴 페이지 캐시), 재시작 16분 뒤(ingest 1회 후) 10.9MiB(cgroup `memory.stat`
anon 3.9MiB / file 9.1MiB, 2026-09-26 00:16 UTC)였다. 그래서 범위는 11~45MiB로 적는다. Airflow 수치는 같은 구분을 하지
않았다. 결론(12GB VM 기준 supercronic이 Airflow 대비 약 1GiB를 아낀다)은 범위의 어느 끝을 써도 같다.

### 3. 임베딩 처리량: CPU vs MPS (같은 기사 49건 중 41건 측정, 첫 배치는 워밍업, 평균 1,820자)
`scripts/bench_embedding_throughput.py`, 호스트 `.venv-jobs`(worker 이미지와 같은 잠금 파일), 배치 8, 배치·캐시 수정 후 코드.
| 장치 | 기사/초 | 초/기사 | 비고 |
|---|---|---|---|
| MPS | 0.69 | 1.4 | 모델 로드 6.8초 |
| CPU 4스레드 | 0.30 | 3.3 | |
| CPU 2스레드 | 0.35 | 2.9 | 4스레드보다 빠름 - 호스트의 다른 작업과 경합한 잡음으로 보고 스레드 수 효과는 판단하지 않는다 |

- 컨테이너 안 CPU 측정은 하지 못했다: 4GB VM에서 모델 로드 중 OOM(커널 로그 `Killed process ... anon-rss:3577176kB`).
  위 CPU 수치는 같은 M2 코어의 호스트 값이라 VM 오버헤드는 빠져 있다. 서버(A1, Ampere) 처리량의 근거로 쓰지 않는다.
- 각 조건 1회 측정이라 신뢰구간이 없다. 호스트 스왑 16~17GB 상태.
- 실제 백필(실행 #20, MPS): 294건 543초 = 0.54건/초(1.85초/기사), 실패 배치 0, 프로세스 최대 메모리 6.4GB.
  같은 백로그의 긴 기사 비중이 벤치 표본보다 커서 벤치보다 느리다. 토큰 길이별 단건 MPS 시간: 1,024토큰 1.5초,
  2,050토큰 2.2초, 4,108토큰 8.1초, 4,464토큰 10.2초.
- 참고(수정 전): 첫 수집의 컨테이너 CPU 임베딩(VM 8GB, 호스트 과다구독, 배치 8을 최장 길이로 패딩)은 모델 로드 174초 뒤
  첫 배치가 22분 안에 끝나지 않았다. 위 수치와의 차이는 배치 구성과 스왑 때문이며 CPU 자체의 한계로 보지 않는다.

### 4. 임베딩 메모리 폭주 진단 (호스트 MPS)
| 조건 | 결과 |
|---|---|
| 수정 전: 창 단위 길이 정렬, 배치 8, max_length 8192 | 프로세스 14GB, 스왑 가득(실행 #13, SIGKILL → 다음 실행이 abandoned 처리) |
| 배치를 토큰 예산으로 분할(커밋 `fd06115`) | 실행 #16~18: 최대 7~18GB, 0.25~0.38건/초. 대기 기사 64건을 잡 순서로 재생하니 4,000토큰대 8건(각각 혼자 배치)에서 MPS 드라이버 메모리 2.98GB → 14.43GB, 182초 |
| + 하위 배치마다 `torch.mps.empty_cache()`(커밋 `2a192d4`) | 같은 8건 뒤 드라이버 2.98GB / 프로세스 3.9GB. 전체 백필(#20) 최대 6.4GB, 0.54건/초 |

중단된 실행 #16~18은 SIGTERM으로 끊었고, 셋 다 `failed (terminated by SIGTERM)`과 그때까지의 저장 건수(40/48/56)를 남겼다.

### 5. 호스트 메모리와 colima VM 크기
- VM 8GB, 호스트에서 다른 작업 동시 실행: 스왑 사용 27.6GB/28.7GB, 메모리 여유 13~17%.
  VM 안 페이지 캐시를 비워도(drop_caches, 여유 7.2GB) 호스트 쪽 VM 메모리는 줄지 않았다(balloon 없음).
- VM을 4GB로 재시작(서비스 중단 35초, `restart: unless-stopped`로 자동 복귀) 직후: 메모리 여유 55%, 스왑 14GB로 감소.
  컨테이너 실사용은 db 70MiB + api 105MiB + 스케줄러 12MiB(수집 실행 중 제외).

### 6. max_length 사전 등록 규칙 적용 (`reports/ops/embedding_truncation_2026-09-26.json`, `scripts/measure_embedding_truncation.py`)
- 대상 456건. 토큰 길이 p50 680 / p90 1,633 / p95 2,673 / p99 4,324 / 최대 4,469.
- (a) 절단 비율: L=1024 21.7%(99건), L=2048 8.1%(37건), L=4096 2.6%(12건) → **세 후보 모두 ≤2% 실패**.
- (b) 잘린 기사의 cos(emb_L, emb_8192) 중앙값: 0.971 / 0.963 / 0.994 → 모두 ≥0.95 통과.
- 보조: top-5 이웃 보존율 0.82 / 0.84 / 0.93, 최소 코사인 0.83 / 0.86 / 0.99.
- 규칙에 따라 **L=8192 유지**. 참고로 리뷰 문서가 인용한 "p95 1,533 토큰"은 이 데이터에서 재현되지 않았다(p95 2,673).
- emb_8192(CPU 배치 1, 99건) 계산에 1,345초, 측정 프로세스 최대 메모리 9.4GB.
- **정정 1 - 기준선도 이미 자른다.** 사전 등록 문구의 "8000자 절단 때문에 사실상 무절단"은 틀렸다. 텍스트는 토큰화 전에
  `[:8000]`자로 잘린다: 측정 대상 456건(2026-09-25 21:30 UTC 이전 추출분으로 재구성) 중 **12건(2.6%)이 8000자를 넘어**
  기준선에서도 잘렸다 - L=4096의 토큰 절단 건수(12건)와 같다. 관측 최대 4,469토큰은 기사 길이가 아니라 이 선절단의
  산물이고, 실제 유효 상한은 약 4.5k 토큰이다. 가장 긴 기사는 12,384자. 2026-09-26 00:10 UTC 기준 본문 있는 560건 중 14건(2.5%).
  그래서 "L=8192 유지"의 실제 의미는 "약 4.5k 토큰(8000자) 유지"이고, L=4096과의 차이는 그 12건의 마지막 ~370토큰뿐이다
  (cos 중앙값 0.994가 높은 이유). 규칙 판정은 바뀌지 않는다((a)는 토큰화 결과만 보므로 세 후보 모두 여전히 실패).
  8000자 선절단 자체를 바꿀지는 긴 기사의 메모리 비용과 함께 따로 결정한다(이번에는 바꾸지 않음).
- **정정 2 - 장치 혼입.** 첫 측정은 emb_8192(긴 기사)를 CPU에서, emb_L을 MPS에서 계산해 (b)의 코사인에 장치 차이가
  섞였다(통제되지 않은 교란, 크기는 작다고 보지만 재지 않았다). 결정에는 영향이 없다((a)에서 이미 전부 탈락).
  스크립트는 이제 emb_L도 CPU 배치 1로 계산하고 8000자 선절단 비율을 함께 출력한다(`char_cut`, `candidate_device`).
  측정은 다시 돌리지 않았다(약 35분·최대 9.4GB).

### 7. 스케줄·에이전트 동작 확인
- 스케줄러 재빌드 후 `20 * * * * embed` 줄이 Mac에서 `{"status": "disabled"}`로 끝나고 `job_runs`에 행을 남기지 않음(23:20 UTC 로그).
- launchd 에이전트 설치 직후(RunAtLoad) 실행 #21이 `device` 없이 targets 0으로 성공.
- `tests/integration/test_ingest_job_end_to_end.py`: 수집(rss,extract) 뒤 embed 잡이 따로 임베딩을 채우는 경로를
  실제 Postgres(운영 DB와 분리한 테스트 DB)에서 확인. integration 테스트는 DB 이름에 `test`가 없거나 `news_raw`에
  테스트가 만들지 않은 행이 있으면 실패하도록 막았다(운영 DB에 가짜 임베딩이 저장되는 사고 방지).
- 리뷰에서 `cluster` 잡이 매번 실패할 것이 드러났다: 풀 연결마다 `register_vector`가 걸려 벡터가 `pgvector.Vector`로 오는데
  `parse_embedding`이 문자열·리스트만 처리했다. 고친 이미지로 운영 DB 읽기 전용 실행: 기사 547건 → 클러스터 41개, 노이즈 350건
  (2026-09-25 23:57 UTC). 중복 제거 마이그레이션 적용 뒤 `docker compose run --rm worker cluster`(실행 #29, 00:25 UTC):
  succeeded, 기사 558건 → 클러스터 42개(최대 19건, 중앙값 3건), 노이즈 비율 0.64, 5.8초.
  `tests/integration/test_cluster_job.py`가 같은 경로를 검증한다(수정 전 코드로 TypeError 재현).
- 마이그레이션 `d48994e9d26e` 운영 적용(2026-09-26 00:22 UTC, 직전 `pg_dump` 백업): `duplicate` 2건(raw_news_id 3424, 3425 -
  섹션 경로 URL 쪽), `ok` 558건 전부 해시·임베딩 보유.

### 8. 스케줄러 종료 신호 (colima, worker 이미지, TERM 트랩을 건 셸 잡)
| 구성 | `docker stop -t 8` 결과 |
|---|---|
| supercronic 직접(`init: true`) | 트랩 미발동, 8초 뒤 exit 137 |
| + `TINI_KILL_PROCESS_GROUP=1` | 같음 - 잡은 별도 그룹(supercronic pgrp 7, 잡 셸 pgrp 15) |
| `docker/scheduler-entrypoint.sh` | 트랩 발동, 즉시 exit 0 |
| 같은 엔트리포인트 + 실제 `jobs.run ingest --stages rss,extract`(일회용 테스트 DB, 본문 추출 도중 stop) | 즉시 exit 0, `job_runs`에 `failed` / `terminated by SIGTERM`, RSS 단계 통계(신규 432건)가 중간 기록으로 남음 |

`tests/test_scheduler_entrypoint.py`(Linux 전용, 가짜 supercronic)가 같은 동작을 검증한다 - 엔트리포인트를
`exec supercronic`으로 바꾸면 실패한다.

## 결과와 한계
- **7일 연속 수집 증거는 아직 없다.** 성공 기준(48시간 연속 성공률 ≥95%, 언론사별 with_body/embedded 수)은
  `job_runs`로 확인하고, 확인 전에는 이 구성을 "운영 중"이라고 쓰지 않는다. 범위 종결 C-27 (2026-10-18): 유보.
- **노트북이 잠들면 수집이 멈춘다.** VM도 멈추고 cron은 밀린 실행을 따라잡지 않는다(launchd는 깨어난 뒤 한 번 실행).
  `keep-awake`는 전원 연결 중에만 효과가 있고 덮개를 닫으면 잠든다. RSS 창이 짧은 피드는 그 사이 기사를 잃는다.
- **서버(A1) CPU 임베딩 처리량은 모른다.** 증거 3의 CPU 수치는 M2 호스트 코어 기준이다. 수집량(야간 시간당 1~37건,
  주간은 아직 미관측)을 M2 CPU 속도(약 3초/기사)로 처리하면 하루 1시간 안팎이지만, A1에서 embed 잡의
  `remaining`이 줄지 않으면 배치·스레드 조정이나 모델 경량화(별도 ADR)가 필요하다.
- worker 컨테이너는 BGE-M3 로드에만 약 3.6GB가 필요하다. 서버에서 embed를 컨테이너로 돌리면 스케줄러 `mem_limit`(6g)과
  VM 여유를 이 기준으로 잡아야 한다.
- max_length=8192에서 가장 긴 기사(4,469토큰)는 배치 1이어도 attention 점수 텐서가 층마다 약 1.3GB다.
  transformers를 SDPA 지원 버전으로 올리면 줄일 수 있지만 임베딩 재현성 확인이 필요해 이번에는 하지 않았다.
- `recommend_engine/src/utils/embedder.py`(합성 데이터용 스크립트 전용)는 여전히 자체 인코딩 경로를 쓴다.
- 운영 런타임(compose `.env`, `.venv-jobs`, launchd 에이전트가 가리키는 경로)은 개발 워크트리와 분리된 전용 워크트리
  `~/Projects/newsletter-runtime`(detached HEAD)에 둔다. 개발 워크트리가 정리돼도 수집이 멈추지 않게 하기 위함이다
  (runbook 2절). 코드를 갱신하려면 그 워크트리를 새 커밋으로 옮기고 다시 빌드해야 한다 - 자동 배포는 없다.
- 이 브랜치의 Alembic 리비전은 `f87f7378672e`(down_revision `e725a62ffef1`) → `d48994e9d26e` 순서다. `f87f7378672e`는
  `feat/realtime-recommendation`의 `8b7f830013b7`과 같은 부모를 가진다 - 둘 다 머지되면 merge 리비전이 필요하다.
- 중복 판정은 정제 본문이 글자 하나까지 같을 때만 잡는다. 같은 기사의 수정판(오탈자 수정, 사진 설명 추가)은 다른 기사로
  남는다 - 근접 중복은 임베딩 코사인으로 따로 재야 한다(측정하지 않음).
- 알림은 `SLACK_WEBHOOK_URL`이 있을 때만 사람에게 닿는다. 지금 Mac 환경에는 설정돼 있지 않아 로그와 `job_runs`,
  `daily_report`로만 확인한다.

## 개정 (2026-10-18) - CI 설치 잠금·린트·비밀 검사·레거시 정리 (엔지니어링 위생, 새 ADR 없이 이 문서에 더한다)

런타임 결정(위)은 그대로다. 이 개정은 같은 "의존성 잠금" 관행을 CI까지 넓히고 저장소 위생 도구를 더한 것이다.

### 결정
1. **CI의 파이썬 설치는 `requirements-ci.txt`(잠금 파일) 하나다.** 최상위 목록은 `requirements-ci.in`이고 `make ci-lock`으로
   만든다(`uv pip compile --universal --python-version 3.11`). `.github/workflows/ci.yml`의 단위·통합 잡이 이 파일만 읽는다.
   `.in`이 직접 `==`로 거는 것은 두 묶음뿐이다: 추천 평가 리포트를 만든 버전(`evaluation/requirements.txt`)과 API 이미지 버전
   (`backend/requirements.txt`의 fastapi·python-jose·passlib·bcrypt, PR #26의 sqlalchemy 2.0.46·sqlmodel 0.0.31).
   두 묶음이 겹치는 fastapi는 API 이미지 값이 이긴다(라우터 테스트가 이미지가 싣는 FastAPI에서 돈다).
   `tests/test_ci_requirements_lock.py`가 원본과의 일치를 대조하고, `ci.yml`에 잠금 파일 밖에서 패키지를 설치하는 스텝(이름을 받는 `pip install`, `uv add|sync`, 다른 requirements 파일)이 없는지, pytest를 돌리는 잡마다 잠금 파일 설치가 pytest 앞에 정확히 한 번인지도 본다.
2. **별도 잠금은 합치지 않는다.** `docker/requirements-worker.txt`·`requirements-ingest.txt`(이미지), `backend/requirements.txt`(API
   이미지), `requirements-colab*.txt`(원격 런타임: `ebnerd-neural.yml`이 같은 핀과 파이썬 3.13으로 돈다)는 배포 대상에 맞춘
   파일이다. `recsys-bench.yml`·`sim-ope-validation.yml`은 측정 환경 자체가 기록된 워크플로라 이 개정에서 건드리지 않았다
   (recsys-bench는 ADR 0014 3절이 "워크플로 파일이 같다"를 비교 조건으로 쓰고 `reports/serving/`의 JSON 3개가 이 워크플로의
   출력이다. sim-ope-validation은 판정용 실행이 받은 버전을 적어 둔 것이다).
3. **ruff는 돌지 못하는 코드만 잡는다.** 규칙은 E9·F50·F52·F6·F7·F82(구문 오류, 잘못된 포맷 문자열, 항상 참인 비교·assert,
   중복 사전 키, 정의되지 않은 이름), 버전은 0.16.9 고정, `pyproject.toml`(도구 설정만)과 CI `lint` 잡, `make lint`.
   스타일(E501)·미사용 임포트(F401)는 켜지 않았다. 이 개정 시점의 트리(main `dede670` 위)에서 켠 규칙의 위반은 0건이고, 꺼 둔 규칙의 기존 위반은
   E501 14,738건(기본 줄 길이 88), F401 103건이다(`ruff check --isolated --select ... --statistics`, git이 무시하는 경로는 제외).
4. **gitleaks는 새 커밋만 본다.** `secret-scan` 잡이 PR은 base..HEAD, push는 이전 head..HEAD만 검사한다. 이력의 평문 DB 비밀번호
   유출 전력(`tests/test_no_plaintext_db_password_in_env_example.py`)은 이력을 다시 쓰지 않고 그대로 둔다. 바이너리는 릴리스 체크섬으로
   고정하고, 설정(`.gitleaks.toml`)은 기본 규칙에 자리표시자 값만 허용한다.
5. **Makefile**은 CI와 같은 명령만 둔다: `help`·`venv`·`ci-lock`·`test`·`itest`·`lint`. (2026-10-18 예외: `report-charts`·`report-charts-check` - 커밋된 리포트 JSON에서 `docs/assets/*.svg`를 만들고 대조하는 문서용 타깃이다. CI 명령이 아니고 표준 라이브러리만 쓴다 - PR #50.)

### 검토했으나 하지 않은 것
- **`-c backend/requirements.txt -c docker/requirements-worker.txt`로 거는 방식.** 해 봤고 해석이 실패한다: 두 파일이 uvicorn(0.54.0 대
  0.40.0)·numpy·pydantic·scipy를 서로 다르게 `==` 고정한다. worker 잠금만 걸면 `sqlmodel==0.0.31`과 `0.0.47`이 부딪히고(#26 고정을
  유지하는 한), 그 두 줄을 걸러 낸 사본을 걸어도 worker 잠금의 numpy가 파이썬 3.12 기준으로 갈라져(`numpy==2.4.6`은 3.12 미만,
  `2.5.3`은 3.12 이상) `--universal` 해석이 실패한다.
- **LICENSE 추가.** 저장소 커밋 작성자가 여럿이고(`git shortlog -sn --all`: khj1212k, seonjin, sojeong0202, seungwoo 계정) 공개 포크의 원본
  (boostcampaitech8 조직 저장소)에 라이선스가 없다(`gh repo view --json licenseInfo`가 null). 팀 공동 저작 코드에 라이선스를 붙이는 것은 공동 저작자 합의가 필요한 일이라 이 개정에서 하지 않았다.

### 레거시 정리: 지운 것과 지우지 않은 것 (2026-10-18, `git grep -w`로 참조 확인)
지운 것(코드·테스트·Dockerfile 참조 없음; 옛 ADR 0008과 `docs/fix-log-2026-07.md`의 서술은 당시 상태 기록이라 그대로 둔다):
`ai_workspace/recommend_engine/logs/match_report.txt`(뉴스레터 제목 목록 - ADR 0023의 "제목 목록을 싣지 않는다"를 어긴 로그. 지우고 `logs/`를
무시한다. git 이력에는 남아 있다), `ai_workspace/crawler/embedding_generator.py`, `ai_workspace/db/user_log_queries.py`.
`ai_workspace/.env.example`은 활성이던 `LLM_PROVIDER=naver`·HyperCLOVA 키와 쓰지 않는 Upstage·OpenAI 키를 주석으로 바꿨다.

지우지 않은 것과 이유:

| 대상 | 참조 | 판단 |
|---|---|---|
| `core/llm_client.py`(HyperCLOVA 클라이언트 + 공용 헬퍼) | `core/llm/adapters.py`(HyperCLOVALLMClient)·`core/llm/registry.py`(`SimpleRateLimiter`)·`core/tone_converter.py`(`extract_json_from_response`)·`evaluation/llm/e0_registered.py`가 임포트, `core.llm_client`를 직접 쓰는 테스트 5개 파일과 어댑터·레지스트리 경로를 쓰는 2개 | 살아 있는 헬퍼 둘을 `core/llm/`로 옮기고 레지스트리의 naver 항목·어댑터·테스트를 함께 걷어내는 리팩터링이다. LLM 생성 코드를 건드리는 별도 변경으로 둔다. 2026-10-18 갱신: HyperCLOVA 클라이언트·`HyperCLOVALLMClient`·레지스트리의 naver 항목은 PR #51로 지웠다(ADR 0005 부록). 공용 헬퍼 둘(`SimpleRateLimiter`, `extract_json_from_response`)을 `core/llm/`로 옮기는 일은 남았다(범위 종결 목록 밖 - 열지 않는다). |
| `jobs/tasks/train.py` 스텁 | `jobs/run.py` 등록, ADR 0033·README가 "은퇴, 불려도 아무것도 실행하지 않는다"고 서술, 옛 crontab 명령줄(`--no-inference`) 호환 | 은퇴 표시가 이미 코드와 문서에 있고 일부러 둔 호환 장치다. |
| `jobs/tasks/batch_fallback.py` | `jobs/run.py` 등록, `docker/crontab`(주석)·runbook 표·README·ADR 0006·0015 | `RECSYS_MODE=batch` 경로의 폴백이라 서빙 모드 결정(ADR 0015)을 건드린다. 꺼져 있다. |
| `core/user_embedder.py` | `pipeline/stages.py`가 임포트, 단위·통합 테스트가 참조 | 살아 있다. |
| `backend/airflow/dags/`, `requirements-airflow.txt` | 결정 4가 "실행하지 않음" README와 함께 보관하기로 했고 `tests/test_airflow_dag_fixes.py`가 구문·경로를 검사, README 팀 소개가 Airflow 담당을 적고 있다 | 팀원이 함께 만든 기록이고 이미 은퇴 표시가 붙어 있다. 지우면 기여 기록이 사라지므로 은퇴 표시만 두는 쪽이 맞다고 판단했다(삭제하려면 팀 합의). |
| `docs/design/e15_colab_smoke.py`, `scripts/bench_embedding_throughput.py`, `scripts/measure_embedding_truncation.py`, `recommend_engine/scripts/constant_feature_ablation.py` | 설계 사양·ADR 0006 증거·리포트가 실행 스크립트로 인용 | 증거 재현 경로다. |
| `recsys-bench.yml`의 Redis | 위 결정 2의 이유 | 지연 비교 쌍이 진행 중이지 않을 때 ADR 0014 3절의 조건과 함께 바꾼다. |

### 한계와 측정하지 않은 것
- CI 잠금 파일은 worker 이미지 잠금과 완전히 같지 않다. 2026-10-18에 두 파일의 `이름==버전` 줄을 비교하면 공통 88개 패키지 중
  28개의 버전이 다르다(langgraph·langchain-openai·openai·trafilatura 등. numpy·scipy 2개는 worker 잠금이 파이썬 3.12 이상용 줄을 더
  가져서 센 것이라 3.11에서는 같다). worker 이미지 안에서 LLM 경로를 돌리는 테스트는 여전히 없다.
- 새 잠금 파일로 임시 venv를 만들어 `pytest --collect-only`가 오류 없이 2,606건을 모으고 일부 테스트(라우터·시뮬레이터 계약·잠금 대조)가
  통과하는 것까지만 확인했다. 전체 스위트와 통합 테스트는 로컬에서 돌리지 않았고 CI가 처음 돌린다.
- gitleaks 바이너리와 `.gitleaks.toml`은 로컬에서 돌려 보지 않았다(바이너리를 내려받지 않음). 범위 계산 셸만 가짜 실행 파일로 확인했다.
- mypy·dependabot·로컬 워크트리 정리는 이 개정에 없다.

## 개정 (2026-10-25) - 본문 해시 중복 제거가 추출 이상을 가린 사례 (새 ADR 없이 이 문서에 더한다)

결정 10의 본문 해시 중복 제거는 "정제 본문이 글자 하나까지 같으면 같은 기사"라는 규칙이다. 추출기가 서로 다른 기사에서 같은 글을 뽑으면 이 규칙이 그 사본들을 `duplicate`로 바꾸고
본문을 지워 버린다 - 규칙대로 동작한 것이라 오류도 실패도 아니다. 2026-10-19 국민일보 페이지 템플릿이 바뀐 뒤 본문 추출기가 기사 본문 대신 기사와 상관없는 특집 카드 묶음을 뽑았고,
같은 특집 글을 받은 기사가 해시 하나로 모여 `duplicate`가 됐다(10-19 이후 704건이 서로 다른 해시 21개로 모였고, 10-22에는 수집 255건 중 254건이 duplicate였다). 다운로드는 성공했으므로 결정 9의 실패 조건(대상 절반 이상
실패)에 걸리지 않았고 ingest는 엿새 동안 매시 succeeded였다.

- **바꾼 것(코드):** 한 실행 안에서 한 언론사의 본문을 받은 기사가 5건 이상이고 절반 이상이 `duplicate`면 `job_runs.stats.warnings`와 `extract.suspect_press`에 "같은 본문 반복 - 추출 이상 의심"을
  남기고 `daily_report`가 최근 24시간의 같은 신호를 보여 준다. 잡은 실패시키지 않는다(추출은 정상이고 행은 되돌릴 수 있다). 자세한 기준·근거·시험은
  [ADR 0026](0026-hosting-tiers-and-contingency.md) 결정 9의 "2026-10-25 추가"와 증거 12, 복구는 [런북](../runbook-hosting.md) 6.8.
- **중복 제거 규칙 자체는 바꾸지 않았다.** 자연 중복(개편 전 국민일보 111건, 해시 111개)은 이 규칙이 맞게 거른다. 바뀐 것은 "추출기가 같은 글을 반복해서 내는지"를 따로 보는 눈이다.
- **한계:** 개편 직후에는 특집 글이 기사마다 본문과 섞여 해시가 서로 달라 이 신호가 약 22시간 뒤에야 걸렸다. 정제 본문 길이가 평소의 몇 배인 것을 보는 신호는 넣지 않았다.
