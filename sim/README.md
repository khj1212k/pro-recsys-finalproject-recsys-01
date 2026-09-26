# 합성 사용자 시뮬레이터와 부하 테스트 (`sim/`)

> **[SIM] 시스템 반응 지표만, 정확도 무주장** — 설계·주장 범위·사전 등록은
> [ADR 0019](../docs/adr/0019-user-simulator-design-and-claim-scope.md).
> 부하 수치는 **[LOAD]**로 표시하고, 측정한 하드웨어·대상 커밋·시드 조건을 함께 적는다.

실사용자가 없는 상태에서 추천 API가 **어떻게 반응하는지**(신규 사용자에게 무엇을 주는지, 클릭에
반응하는지, 폴백이 얼마나 나가는지, 클릭이 로그 테이블까지 가는지)를 보고, 같은 가상 사용자로
부하를 만든다. 클릭 모델은 손으로 쓴 가정이라 CTR·정확도 비교에는 쓰지 않는다.

## 구성

| 모듈 | 역할 |
|---|---|
| `personas.py` | 아키타입 10종 → 디리클레 잡음을 얹은 사용자(도중 가입 10%, 3일차 drift 10%, `@sim.invalid`) |
| `click_model.py` | 위치 기반 클릭 모델 P = (1/(r+1))^η · σ(w·φ + b), BGE-M3 코사인 미사용, 프리셋 `default`·`category_only` |
| `calibration.py` | 편향 b를 무작위 top-10 CTR 2%에 맞춤, EB-NeRD·팀 합성 데이터 기저율 집계 |
| `catalog.py` | 합성 카탈로그, 팀 뉴스레터 195개 카탈로그(로컬 전용) |
| `driver.py` | API 계약만 쓰는 HTTP 드라이버(가입·로그인·온보딩·`/newsletters/today`·클릭·상세) |
| `fake_app.py` | 프로세스 내 가짜 FastAPI 앱과 장난감 정책 5종(지표 검증용 테스트 더블) |
| `metrics.py` | 행동 지표(콜드 스타트, 클릭 반응성, drift 적응, 폴백·빈 응답, 오류·지연) |
| `run.py`, `experiments.py`, `prereg.py` | 단일 실행, 지표 타당성 격자, ADR 0019 사전 등록 판정 |
| `load.py`, `locustfile.py`, `loadtest.py` | Locust 부하 시나리오와 5/20/50 RPS 단계 실행기 |
| `seed.py` | 부하 테스트용 **일회용** DB 시드(합성 뉴스레터, 밤 배치 대역) |

설치: `pip install -r sim/requirements.txt` (부하 생성 호스트용. API 이미지에는 넣지 않는다.)

## 1. 행동 시뮬레이션 — 가짜 앱 (서버 불필요)

```bash
# 정책 하나, 300명 7일
python -m sim.run --target fake --policy reactive --users 300 --days 7 --seed 0 --out out/sim.json

# 지표 타당성 격자(정책 5종 x 프리셋 2종 x 시드 3개)와 사전 등록 판정
python -m sim.experiments --out-dir out/sim_grid --workers 2
python -m sim.experiments --out-dir out/sim_grid_team --workers 2 \
    --catalog team_archive --team-archive-dir data/team_archive          # 팀 카탈로그(로컬 전용 데이터)
python -m sim.prereg --runs-dir out/sim_grid out/sim_grid_team --out reports/sim/grid_v1 --git-sha "$(git rev-parse --short HEAD)"

# 기저 클릭률 보고(집계만 출력, 기사 필드는 읽지 않음)
python -m sim.calibration --ebnerd-dir data/benchmarks/ebnerd/ebnerd_small \
    --team-ctr-csv data/team_archive/synthetic_dataset/synthetic_ctr_logs.csv
```

- 결과: [reports/sim/grid_v1.md](../reports/sim/grid_v1.md), [reports/sim/base_rates_v1.json](../reports/sim/base_rates_v1.json).
  실행별 JSON(`out/`)은 커밋하지 않는다.
- 개발 Mac에서는 `nice -n 19 env OMP_NUM_THREADS=2`와 `--workers 2`로 돌렸다(격자 G1 30회 약 5분).
- EB-NeRD와 `data/team_archive`는 이 Mac 밖으로 나가지 않는다. 리포트에는 집계 수치만 싣는다.

## 2. 행동 시뮬레이션 — 실제 스택

같은 드라이버가 실행 중인 API에 붙는다. 사용자·클릭 로그를 만들므로 **일회용 DB에서만** 돌린다
(아래 3.2의 일회용 compose 프로젝트). 가상 하루가 끝날 때마다 밤 배치 대역을 돌린다.

```bash
python -m sim.run --target http://127.0.0.1:8100 --users 50 --days 3 \
    --day-end-cmd "python -m sim.seed --database-url $LOAD_DB_URL batches" --out out/sim_stack.json
```

현재 main의 `/newsletters/today`는 `X-Rec-Source` 헤더를 보내지 않으므로 `fallback_rate`는 측정 불가(`None`)로
나온다. 요청 시점 추천 브랜치(`feat/realtime-recommendation`)는 헤더를 보낸다.

## 3. 부하 테스트 (Locust)

### 3.1 시나리오

- **ActiveReader** (목표 RPS만큼): 기존 계정으로 `/newsletters/today`(가중치 9)와 직전 목록에서 클릭
  모델이 고른 1건 클릭(가중치 1). `constant_throughput(1)`이라 사용자 수 ≈ RPS.
- **Newcomer** (1명): 5초마다 새 `@sim.invalid` 계정으로 가입 → 로그인 → 온보딩 → 첫 `/today`
  (`today_first_view`로 따로 집계).
- `python -m sim.loadtest`는 5/20/50 RPS를 각각 별도 헤드리스 Locust로 돌리고(`--reset-stats`로 계정
  준비 구간 제외) 두 표를 만든다: 엔드포인트별 요청 수·달성 RPS·p50/p95/p99·오류율, 그리고 `/today`의
  `X-Rec-Source` 분포·빈 응답률·폴백률. Locust는 단일 프로세스로 돌린다(출처 집계가 프로세스 안에 있다).

### 3.2 어디서 돌리는가

- **개발 Mac에서는 돌리지 않는다.** 노트북 응답성을 지키기 위해 로컬 서버·부하를 금지했다(2026-09-26).
  로컬에서는 CI와 같은 6초짜리 스모크만 명시적으로 켤 수 있다(`SIM_LOAD_SMOKE=1`).
- **Tier 0 (E2.1.Micro, 1 GB)에서도 돌리지 않는다.** 유일한 수집기이고(ADR 0026) 1/8 OCPU·1 GB라
  부하 대상도 부하 생성기도 될 수 없다.
- **Tier 1 (A1)을 확보하면 거기서** 일회용 compose 프로젝트를 띄워 측정한다. 부하 생성기는 가능하면
  다른 호스트에 두고, 같은 VM에서 돌릴 때는 그 사실과 코어 배치(`taskset`)를 결과에 적는다.

### 3.3 절차 (A1 VM, compose 스택은 ADR 0006의 `docker-compose.yml`)

```bash
# 0) 일회용 프로젝트용 env 파일: 수집 DB와 다른 프로젝트 이름·볼륨·포트
cp .env.compose.example .env.load        # POSTGRES_*·API_SECRET_KEY를 새 값으로 채운다
#    DB_HOST_PORT=5434, API_HOST_PORT=8100, POSTGRES_DB=newsletter_load
export LOAD_DB_URL="postgresql://<user>:<pw>@127.0.0.1:5434/newsletter_load"   # 셸에만, 파일에 남기지 않는다

# 1) db + migrate + api만 기동 (scheduler는 띄우지 않는다)
docker compose -p newsletter-load --env-file .env.load up -d --build api

# 2) 합성 뉴스레터 시드 (news_raw에 행이 있거나 실사용자가 있으면 sim.seed가 거부한다)
python -m sim.seed --database-url "$LOAD_DB_URL" catalog --days 2

# 3) 워밍업: 최대 단계(50 RPS)의 리더 계정을 만든다 — 결과는 버린다
python -m sim.loadtest --host http://127.0.0.1:8100 --rps 50 --duration 30s --out-dir out/load_warmup

# 4) 리더 계정의 오늘 배치 행 쓰기 (밤 추천 잡 대역)
python -m sim.seed --database-url "$LOAD_DB_URL" batches

# 5) 측정: 같은 run-tag(기본 "load")라 3)의 계정을 다시 쓴다
python -m sim.loadtest --host http://127.0.0.1:8100 --rps 5 20 50 --duration 120s --out-dir out/load_v1

# 6) 정리: 일회용 프로젝트의 컨테이너와 볼륨 삭제 (수집 프로젝트 newsletter-recsys는 건드리지 않는다)
docker compose -p newsletter-load --env-file .env.load down -v
```

결과 보고: `out/load_v1/summary.md`를 `reports/serving/load_v1.md`로 옮기고 머리말에 `[LOAD]`, VM
셰이프(OCPU·메모리), 부하 생성기 위치, 대상 커밋, uvicorn 워커 수, 시드 조건을 적는다. 20 RPS는
p50/p95/p99 표로, 5·50 RPS는 오류율과 빈 응답률·폴백률 위주로 읽는다.

### 3.4 이 부하 테스트가 재는 것과 못 재는 것

- 재는 것: 합성 시드 DB 위에서 현재 API 경로(배치 행 읽기, 가입·로그인의 bcrypt, 클릭 로그 INSERT)의
  단계별 지연 분포와 오류율, 빈 응답률(헤더가 있으면 폴백률). 현재 main에서는 Newcomer의 첫 `/today`가
  배치 행이 없어 빈 목록이다 — `today_first_view`의 빈 응답률 100%는 콜드 스타트 공백이 그대로
  보이는 것이고(ADR 0019 격자의 static_batch와 같은 현상), 그 지연은 "빈 목록을 돌려주는 비용"이다.
- 못 재는 것:
  - 합성 뉴스레터에는 임베딩이 없다. 요청 시점 추천 브랜치에 붙이면 KNN 후보가 비어 다른 후보·폴백
    경로만 탄다. 그 경로의 지연을 재려면 임베딩이 있는 콘텐츠 덤프(뉴스레터 테이블만, 사용자·로그 제외)를
    일회용 DB에 복원한 뒤 돌린다.
  - 밤 배치 대역(`sim.seed batches`)은 실제 추천 잡이 아니다. 배치 생성 시간은 이 측정에 없다.
  - API 컨테이너는 uvicorn 단일 프로세스다(`docker/api.Dockerfile`). 워커 수를 바꾼 결과는 별도 실행으로 적는다.
  - 가짜 앱을 대상으로 한 수치(CI 스모크)는 하네스 자체의 처리 능력일 뿐 API 성능이 아니다.

### 재실행 대기(클라우드)

2026-09-26 기준 실측 부하 수치는 없다. A1 확보 후 3.3의 1)~6)을 그대로 실행해
`reports/serving/load_v1.md`를 만든다. 그 전까지 README·이력서에 부하 수치를 쓰지 않는다.
