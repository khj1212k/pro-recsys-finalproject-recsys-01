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
    --day-end-cmd 'python -m sim.seed --database-url "$LOAD_DB_URL" batches' --out out/sim_stack.json
```

`--day-end-cmd`는 **작은따옴표**로 감싼다. `$LOAD_DB_URL`은 `sim.run`이 띄우는 자식 셸에서 풀리므로
(3.3의 `export` 필요) 비밀번호가 든 URL이 `sim.run`의 인자에도, 인자를 기록하는 결과 JSON에도 남지 않는다.
큰따옴표로 감싸 URL이 인자에 그대로 들어온 경우에도 결과 JSON에는 `scheme://***@host`로 가려서 쓴다.

대상 API의 세대와 모드에 따라 결과가 다르게 읽힌다. 결과에는 대상 커밋과 `RECSYS_MODE`를 함께 적는다.

- **배치 전용 `/newsletters/today`** (요청 시점 추천이 들어오기 전의 API): `X-Rec-Source` 헤더가 없어
  `fallback_rate`는 0이 아니라 측정 불가(`None`)로 나온다. 도중 가입자의 첫 응답은 빈 목록이다
  (`first_view_coverage` 0).
- **요청 시점 추천 API** (`backend/app/recsys`, 브랜치 `feat/realtime-recommendation`): 모든 응답에 헤더가 있어
  폴백률이 나온다. `RECSYS_MODE=batch`에서는 배치 행이 없는 사용자가 인기 목록을 받고(`popular`, 폴백으로
  센다), `RECSYS_MODE=realtime`에서는 요청마다 계산한다.

`tests/simulator/test_sim_backend_contract.py`가 실제 라우터에 드라이버를 붙여(SQLite) 배치 전용 API, 그리고
요청 시점 API의 두 모드를 확인한다. 체크아웃된 backend에 있는 세대의 테스트만 돌고 나머지는 건너뛴다.

## 3. 부하 테스트 (Locust)

### 3.1 시나리오

- **ActiveReader** (목표 RPS만큼): 기존 계정으로 `/newsletters/today`(가중치 9)와 직전 목록에서 클릭
  모델이 고른 1건 클릭(가중치 1). `constant_throughput(1)`이라 사용자 수 ≈ RPS. 직전 목록이 비어 있으면
  클릭할 것이 없으므로 요청을 보내지 않고 "건너뛴 클릭"으로 센다.
- **Newcomer** (1명): 5초마다 새 `@sim.invalid` 계정으로 가입 → 로그인 → 온보딩 → 첫 `/today`
  (`today_first_view`로 따로 집계).
- `python -m sim.loadtest`는 5/20/50 RPS를 각각 별도 헤드리스 Locust로 돌리고 세 표를 만든다:
  엔드포인트별 요청 수·달성 RPS·p50/p95/p99·오류율, `/today`의 `X-Rec-Source` 분포·빈 응답률·폴백률,
  단계별 측정 구간(초)·리더 준비 시간·준비에 실패한 리더 수·건너뛴 클릭 수.
  Locust는 단일 프로세스로 돌린다(준비 게이트와 출처 집계가 프로세스 안에 있다).

**측정 구간.** Locust의 `--reset-stats`는 사용자를 **띄운 시점**에 통계를 지운다. `-u N -r N`이면 그때 리더
N명의 가입·로그인(bcrypt)·온보딩 요청이 아직 진행 중이라, 그 요청들이 집계에 들어가고 첫 `/today`
요청들과 경합해 p95/p99를 부풀린다(50 RPS 단계에서 가장 크다). 그래서 리더는 계정 준비를 마치면
게이트(`sim.load.ReadyGate`)에 알리고 기다린다. 모든 리더가 준비를 마친(또는 준비에 실패한) 순간에 Locust
통계와 출처 집계를 지우고 태스크를 시작한다. Newcomer도 그때까지 기다리므로 구간이 시작될 때 진행 중인
요청이 없다. `-t`는 실행 시작부터 재므로 **측정 구간 = `--duration` − 리더 준비 시간**이고, 단계마다 표에
적힌다. 준비가 `SIM_READY_TIMEOUT_S`(기본 120초)를 넘기면 게이트를 강제로 열고 표에 표시한다.

**숫자를 읽을 때.**
- Newcomer 흐름은 한 번에 6~8개 요청(가입 1, 로그인 1, 온보딩 조회 1~3, PUT 2, `/today` 1)이라
  **목표 RPS 위에 약 1.2~1.6 RPS가 더 얹힌다**(5 RPS 단계에서는 약 +30%). 단계 이름의 RPS는 리더의 목표치다.
- `constant_throughput`은 **닫힌 루프**다. 사용자는 앞 요청이 끝나야 다음 요청을 보낸다. 대상이 포화되면
  요청이 쌓이는 대신 보내는 속도가 줄어, 지연 백분위수가 열린 도착 과정에서 볼 값보다 낮게 나온다
  (coordinated omission). 단계의 백분위수는 반드시 달성 RPS와 함께 읽고, 달성 RPS가 목표에 못 미친
  단계의 p95/p99는 하한으로만 쓴다.
- 토큰 만료로 401을 받고 재로그인해 다시 보낸 요청은 실패가 아니며, `<엔드포인트>_token_expired`
  이름으로 따로 집계된다.

### 3.2 어디서 돌리는가

- **개발 Mac에서는 돌리지 않는다.** 노트북 응답성을 지키기 위해 로컬 서버·부하를 금지했다(2026-09-26).
  로컬에서는 CI와 같은 6초짜리 스모크만 명시적으로 켤 수 있다(`SIM_LOAD_SMOKE=1`).
- **Tier 0 (E2.1.Micro, 1 GB)에서도 돌리지 않는다.** 지금 유일한 수집기이고 1/8 OCPU·1 GB라
  부하 대상도 부하 생성기도 될 수 없다. (Tier 0·Tier 1은 호스팅 계층 결정에서 쓰는 이름이다. 그 결정
  문서는 브랜치 `ops/hosting-tiers`에 있다.)
- **Tier 1 (A1)을 확보하면 거기서** 일회용 compose 프로젝트를 띄워 측정한다. 부하 생성기는 가능하면
  다른 호스트에 두고, 같은 VM에서 돌릴 때는 그 사실과 코어 배치(`taskset`)를 결과에 적는다.

### 3.3 절차 (A1 VM, compose 스택은 ADR 0006의 `docker-compose.yml`)

```bash
# 0) 일회용 프로젝트용 env 파일: 수집 DB와 다른 프로젝트 이름·볼륨·포트
#    파일 이름은 .env.load.local — .gitignore의 `.env.*.local`에 걸려 커밋되지 않는다.
cp .env.compose.example .env.load.local  # POSTGRES_*·API_SECRET_KEY를 새 값으로 채운다
#    DB_HOST_PORT=5434, API_HOST_PORT=8100, POSTGRES_DB=newsletter_load
#    COMPOSE_PROJECT_NAME=newsletter-load   # -p를 빠뜨려도 수집 프로젝트(newsletter-recsys)의 볼륨을 건드리지 않게
export LOAD_DB_URL="postgresql://<user>:<pw>@127.0.0.1:5434/newsletter_load"   # 셸에만, 파일에 남기지 않는다

# 1) db + migrate + api만 기동 (scheduler는 띄우지 않는다)
docker compose -p newsletter-load --env-file .env.load.local up -d --build api

# 2) 합성 뉴스레터 시드 (news_raw에 행이 있거나 실사용자가 있으면 sim.seed가 거부한다)
python -m sim.seed --database-url "$LOAD_DB_URL" catalog --days 2

# 3) 워밍업: 최대 단계(50 RPS)의 리더 계정을 만든다 — 결과는 버린다
python -m sim.loadtest --host http://127.0.0.1:8100 --rps 50 --duration 30s --out-dir out/load_warmup

# 4) 리더 계정의 오늘 배치 행 쓰기 (밤 추천 잡 대역)
python -m sim.seed --database-url "$LOAD_DB_URL" batches

# 5) 측정: 같은 run-tag(기본 "load")라 3)의 계정을 다시 쓴다
python -m sim.loadtest --host http://127.0.0.1:8100 --rps 5 20 50 --duration 120s --out-dir out/load_v1

# 6) 정리: 일회용 프로젝트의 컨테이너와 볼륨 삭제 (수집 프로젝트 newsletter-recsys는 건드리지 않는다)
docker compose -p newsletter-load --env-file .env.load.local down -v
```

`docker-compose.yml`은 프로젝트 이름을 `newsletter-recsys`로 고정한다. `down -v`는 그 프로젝트의 볼륨
(수집 DB의 `pgdata` 포함)을 지우므로 일회용 스택에는 이름을 두 겹으로 건다: 명령마다 `-p newsletter-load`,
env 파일에 `COMPOSE_PROJECT_NAME=newsletter-load`. env 파일의 값이 compose 파일의 `name:`보다 우선한다
(`docker compose --env-file <파일> config`로 프로젝트·볼륨 이름을 띄우기 전에 확인할 수 있다).
`out/`은 gitignore 대상이다.

결과 보고: `out/load_v1/summary.md`(세 표 모두)를 `reports/serving/load_v1.md`로 옮기고 머리말에 `[LOAD]`, VM
셰이프(OCPU·메모리), 부하 생성기 위치, 대상 커밋, uvicorn 워커 수, 시드 조건을 적는다. 20 RPS는
p50/p95/p99 표로, 5·50 RPS는 오류율과 빈 응답률·폴백률 위주로 읽는다. 준비에 실패한 리더가 있거나
게이트가 시간 초과로 열린 단계는 다시 돌린다.

### 3.4 이 부하 테스트가 재는 것과 못 재는 것

- 재는 것: 합성 시드 DB 위에서 대상 API가 실제로 타는 경로(배치 행 읽기, 가입·로그인의 bcrypt, 클릭 로그
  INSERT)의 단계별 지연 분포와 오류율, 빈 응답률(헤더가 있으면 폴백률).
  - 대상이 **배치 전용 API**면 Newcomer의 첫 `/today`는 배치 행이 없어 빈 목록이다 — `today_first_view`의
    빈 응답률 100%는 콜드 스타트 공백이 그대로 보이는 것이고(ADR 0019 격자의 static_batch와 같은 현상),
    그 지연은 "빈 목록을 돌려주는 비용"이다.
  - 대상이 **요청 시점 API**면 Newcomer의 첫 `/today`는 비지 않는다. `RECSYS_MODE=batch`에서는 인기 목록
    (`popular`, 폴백으로 집계)이, `RECSYS_MODE=realtime`에서는 콜드 스타트 경로가 답한다.
- 못 재는 것:
  - 합성 뉴스레터에는 임베딩이 없다. 요청 시점 API를 `RECSYS_MODE=realtime`으로 띄워도 개인 신호가
    없는 콜드 스타트 경로(`cold_start_popular`)만 탄다(계약 테스트에서 확인). KNN·스코어링 경로의 지연을
    재려면 임베딩이 있는 콘텐츠 덤프(뉴스레터 테이블만, 사용자·로그 제외)를 일회용 DB에 복원한 뒤 돌린다.
  - 밤 배치 대역(`sim.seed batches`)은 실제 추천 잡이 아니다. 배치 생성 시간은 이 측정에 없다.
  - API 컨테이너는 uvicorn 단일 프로세스다(`docker/api.Dockerfile`). 워커 수를 바꾼 결과는 별도 실행으로 적는다.
  - 가짜 앱을 대상으로 한 수치(CI 스모크)는 하네스 자체의 처리 능력일 뿐 API 성능이 아니다.

### 재실행 대기(클라우드)

2026-09-26 기준 실측 부하 수치는 없다. A1 확보 후 3.3의 1)~6)을 그대로 실행해
`reports/serving/load_v1.md`를 만든다. 그 전까지 README·이력서에 부하 수치를 쓰지 않는다.
