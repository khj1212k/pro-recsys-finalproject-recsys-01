# ADR 0006: 런타임 구성 - docker compose + supercronic, Mac 개발 중 임베딩은 호스트 MPS

## 상태
채택됨 (2026-09-26). 초안(측정 전 사전 등록 규칙 포함)은 커밋 `2c0d193`. 7일 연속 수집 뒤
`job_runs` 성공률로 다시 확인한다(아래 "결과와 한계").

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
2. **supercronic + `python -m jobs.run <job>` (채택)** - 컨테이너 하나에 crontab 한 장(유휴 12MiB).
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

수집 잡(rss,extract)의 컨테이너 메모리는 관측 최대 376MiB(추출 워커 8개, 36개 프로세스, `docker stats` 4회 표본이라 과소추정 가능).

### 2. 스케줄러 유휴 메모리 (`docker stats`, 30초 간격 8회, 2026-09-25 22:12~22:16 UTC)
| 구성 | 메모리 | 프로세스 수 | 유휴 CPU |
|---|---|---|---|
| supercronic 스케줄러 컨테이너 | 12.2MiB (8회 모두 12.15~12.16MiB) | 10 | 0.00~0.30% |
| Airflow 3.1.6 standalone(LocalExecutor, 메타DB는 같은 Postgres 서버, DAG 4파일 일시정지) | 1.03 → 1.13GiB (4분 동안 증가) | 59 | 4~75% (dag-processor 재파싱·하트비트) |
| 참고: api / db | 80MiB / 81~85MiB | 4 / 13~15 | ~0.5% / 2~6% |

Airflow는 공식 이미지(`apache/airflow:3.1.6-python3.11`)로 따로 띄워 쟀다(잡 venv 없음 - 유휴 메모리에는 영향 없음).
12GB VM 기준으로 supercronic은 Airflow 대비 약 1.1GiB를 아낀다.

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

### 7. 스케줄·에이전트 동작 확인
- 스케줄러 재빌드 후 `20 * * * * embed` 줄이 Mac에서 `{"status": "disabled"}`로 끝나고 `job_runs`에 행을 남기지 않음(23:20 UTC 로그).
- launchd 에이전트 설치 직후(RunAtLoad) 실행 #21이 `device` 없이 targets 0으로 성공.
- `tests/integration/test_ingest_job_end_to_end.py`: 수집(rss,extract) 뒤 embed 잡이 따로 임베딩을 채우는 경로를
  실제 Postgres(운영 DB와 분리한 `itest_wt1`)에서 확인, integration 26 passed.

## 결과와 한계
- **7일 연속 수집 증거는 아직 없다.** 성공 기준(48시간 연속 성공률 ≥95%, 언론사별 with_body/embedded 수)은
  `job_runs`로 확인하고, 확인 전에는 이 구성을 "운영 중"이라고 쓰지 않는다.
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
- launchd 에이전트는 설치한 체크아웃 경로를 가리킨다. 워크트리에서 설치했다면 머지 후 메인 체크아웃에서 다시 설치해야 한다.
- 이 브랜치의 Alembic 리비전 `f87f7378672e`(down_revision `e725a62ffef1`)는 `feat/realtime-recommendation`의
  `8b7f830013b7`과 같은 부모를 가진다 - 둘 다 머지되면 merge 리비전이 필요하다.
