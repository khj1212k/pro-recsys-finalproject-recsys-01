# 운영 런북 (docker compose + supercronic)

런타임 구성과 스케줄러 선택의 근거는 [ADR 0006](adr/0006-runtime-compose-and-scheduler.md)을 본다.
이 문서는 "지금 돌고 있는 걸 어떻게 확인하고, 어떻게 멈추고, 어떻게 다시 켜는가"만 다룬다.

## 1. 구성 요약

| 서비스 | 이미지 | 상시 기동 | 역할 |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | O (`restart: unless-stopped`) | 앱 DB. 데이터는 `pgdata` 볼륨. 호스트에는 `127.0.0.1:5433`으로만 노출 |
| `migrate` | worker | 1회성 | `python -m jobs.migrate`: vector extension → `alembic upgrade head` → 언론사/카테고리/RSS 시드 |
| `api` | api (torch·Airflow 없음) | O | FastAPI, `127.0.0.1:8000` |
| `scheduler` | worker + supercronic | O | `docker/crontab`의 잡만 `python -m jobs.run <job>`으로 실행 |
| `worker` | worker | X (`tools` 프로필) | 잡 수동 실행용 (`docker compose run --rm worker <job>`) |
| (호스트) `com.newsletter-recsys.embed` | launchd + `.venv-jobs` | O (Mac 전용) | `jobs.run embed`를 호스트 MPS로 매시 실행 (`scripts/mac_embed_agent.sh`) |

Airflow는 쓰지 않는다. 팀 시절 DAG은 `backend/airflow/dags/`에 보관만 한다(실행하지 않음, ADR 0006).

현재 켜져 있는 스케줄(`docker/crontab`, 컨테이너 시각 UTC):

| 잡 | 주기 | 비고 |
|---|---|---|
| `ingest --stages rss,extract` | 매시 05분 | RSS → 본문 추출. 세계일보 피드는 20건 ≈ 2.7시간 분량이라 매시 돌린다 |
| `embed --time-budget-s 2400` | 매시 20분 | 본문 있고 임베딩 없는 기사를 BGE-M3로. **Mac에서는 컨테이너 줄이 꺼져 있고**(`JOBS_DISABLED=embed`) 호스트 launchd 에이전트가 같은 시각에 MPS로 돈다 |
| `popularity` | 매시 35분 | 뉴스레터가 아직 없어서 빈 랭킹이 저장되는 게 정상 |
| `cluster` | 매일 14:50 UTC (23:50 KST) | LLM 호출 없이 클러스터 통계만 `job_runs`에 기록 |
| `daily_report` | 매일 00:10 UTC (09:10 KST) | 최근 24시간 요약 (로그, `SLACK_WEBHOOK_URL`이 있으면 Slack) |
| `generate`, `user_embed`, `train`, `batch_fallback` | **꺼져 있음** | LLM 평가 프로토콜·사실성 게이트, 랭커 v2 결정 전까지 켜지 않는다 |

## 2. 처음 띄우기 (Mac, colima)

```bash
brew install colima docker docker-compose docker-buildx
# ~/.docker/config.json에 "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
# Mac에서는 임베딩이 호스트에서 돌므로 VM 메모리는 4GB면 충분하다(ADR 0006 - 8GB로 두면 16GB 노트북이 스왑에 몰린다).
colima start --arch aarch64 --cpu 4 --memory 4 --disk 40 --vm-type vz --mount-type virtiofs

cp .env.compose.example .env      # POSTGRES_PASSWORD, API_SECRET_KEY 채우기 (openssl rand -hex 24)
# Mac이면 .env에 COMPOSE_FILE=docker-compose.yml:docker/compose.mac.yaml
#   -> 호스트 ~/.cache/huggingface의 BGE-M3를 읽기 전용으로 재사용(2.2GB 재다운로드 안 함)
# 킬 스위치를 호스트 비용 가드와 공유하려면 OPS_DIR=<메인 체크아웃>/.ops

export GIT_SHA=$(git rev-parse HEAD)   # job_runs.git_sha에 남는다
docker compose up -d --build           # db, migrate, api, scheduler
docker compose run --rm worker ingest --stages rss,extract   # 첫 수집을 바로 한 번 돌려보기

# Mac: 호스트 MPS 임베딩 에이전트 (저장소 루트의 .env에서 DB 접속 정보를 읽는다)
scripts/mac_embed_agent.sh setup       # .venv-jobs (docker/requirements-worker.txt 잠금 그대로, torch MPS 포함)
scripts/mac_embed_agent.sh run         # 백로그 한 번 처리 (포그라운드)
scripts/mac_embed_agent.sh install     # launchd 등록: 매시 20분 + 로그인 시, 로그 ~/Library/Logs/newsletter-recsys/embed.log
scripts/mac_embed_agent.sh keep-awake install   # 전원 연결 중 잠자기 방지(caffeinate -s). 해제: keep-awake uninstall
```

에이전트는 스크립트가 있는 체크아웃을 기준으로 돈다. 워크트리에서 설치했다면 브랜치가 머지된 뒤
메인 체크아웃에서 `setup` → `install`을 다시 실행해 경로를 옮긴다(같은 라벨이라 덮어쓴다).

`.env`는 gitignore 대상이다. **`.env`를 지우면 DB 비밀번호를 잃는다** - 그때는 7절의 재설정 절차를 쓴다.

## 3. 상태 확인

```bash
docker compose ps                         # db/api/scheduler가 Up, migrate는 Exited (0)
docker compose logs --tail 100 scheduler  # supercronic이 잡을 언제 시작/종료했는지
```

잡 실행 기록(`job_runs`)이 1차 확인 수단이다. 실행마다 한 행, `stats`에 단계별 수치가 JSON으로 남는다.

```bash
docker compose exec db psql -U newsletter -d newsletter -c "
  SELECT id, job, status, started_at, finished_at - started_at AS took,
         stats->'rss'->>'inserted' AS rss_new, stats->'extract'->>'ok' AS extracted,
         stats->'embed'->>'embedded' AS embedded, stats->'embed'->>'device' AS device,
         stats->'embed'->>'remaining' AS embed_left, left(error, 80) AS error
  FROM job_runs ORDER BY id DESC LIMIT 20;"
```

- `status`: `running` / `succeeded` / `failed` / `skipped`(락 점유·킬 스위치 등 정상 건너뜀) / `abandoned`(프로세스가 죽어 결과를 못 남긴 실행 - 다음 실행이 표시한다)
- `failed`면 `error`에 traceback 끝부분이 있고, `SLACK_WEBHOOK_URL`이 설정돼 있으면 Slack으로도 온다.
  `docker stop`/`kill -TERM`으로 끊긴 실행은 `failed` + `terminated by SIGTERM`으로 남고, 그때까지의 stats가 보존된다.
- embed 행의 `device`가 `mps`면 호스트 에이전트, `cpu`면 컨테이너 실행이다.

호스트 임베딩 에이전트:

```bash
scripts/mac_embed_agent.sh status           # launchd 상태 + 마지막 실행 JSON 요약 3줄
tail -f ~/Library/Logs/newsletter-recsys/embed.log
```

수집 누적 현황(언론사별):

```bash
docker compose exec db psql -U newsletter -d newsletter -c "
  SELECT p.press_name, count(*) AS total,
         count(*) FILTER (WHERE n.raw_news_extract_status = 'ok') AS with_body,
         count(*) FILTER (WHERE n.raw_news_extract_status = 'dropped') AS dropped,
         count(*) FILTER (WHERE n.embedding_result IS NOT NULL) AS embedded,
         min(n.raw_news_crawled_at) AS first_seen, max(n.raw_news_crawled_at) AS last_seen
  FROM news_raw n JOIN press p USING (press_id) GROUP BY 1 ORDER BY 2 DESC;"
```

지금 도는 잡과 잡이 잡고 있는 advisory lock:

```bash
docker compose exec db psql -U newsletter -d newsletter -c "
  SELECT pid, application_name, state, now() - backend_start AS age
  FROM pg_stat_activity WHERE application_name LIKE 'jobs.run:%';"
```

컨테이너 메모리: `docker stats --no-stream`.

## 4. 잡 수동 실행

```bash
docker compose run --rm worker ingest --stages rss,extract     # 수집만 (스케줄러와 같음)
docker compose run --rm worker embed --limit 200 --time-budget-s 600   # 컨테이너 CPU 임베딩 (서버용. Mac 4GB VM에서는 모델 로드 중 OOM)
scripts/mac_embed_agent.sh run --limit 200                     # 호스트 MPS 임베딩
docker compose run --rm worker cluster
docker compose run --rm worker daily_report
docker compose run --rm worker --help                          # 잡 목록
```

스케줄러가 같은 잡을 돌리는 중이면 수동 실행은 `skipped (lock_held)`로 기록되고 종료 코드 0으로 끝난다.

## 5. 멈추기 / 다시 켜기

| 하고 싶은 것 | 명령 | 데이터 |
|---|---|---|
| 수집만 잠시 멈춤 | `docker compose stop scheduler` | 유지 |
| 수집 재개 | `docker compose start scheduler` | 유지 |
| 호스트 임베딩 멈춤/재개 | `scripts/mac_embed_agent.sh uninstall` / `install` | 유지 (밀린 기사는 재개 후 오래된 것부터) |
| 잠자기 방지 해제 | `scripts/mac_embed_agent.sh keep-awake uninstall` | - |
| 전체 중지(컨테이너 삭제) | `docker compose down` | **유지** (볼륨은 남음) |
| 전체 재기동 | `docker compose up -d` | 유지 |
| VM까지 끄기 | `docker compose stop && colima stop` | 유지. `colima start` 후 `restart: unless-stopped` 서비스가 다시 뜬다 |
| **데이터까지 삭제** | `docker compose down -v` | **pgdata/hf_cache/model_checkpoints 삭제 - 되돌릴 수 없음** |

Mac이 잠자기에 들어가면 VM도 멈추고, 그동안 예정된 실행은 건너뛴다(cron은 밀린 실행을 따라잡지 않음).
RSS의 100시간 cutoff 덕분에 몇 시간 잠들었다 깨도 다음 ingest가 빠진 기사를 대부분 다시 수집한다.
며칠 수집을 계속하려면 전원을 연결하고 `scripts/mac_embed_agent.sh keep-awake install`(caffeinate -s)을 켜 둔다.
노트북 덮개를 닫으면(외부 디스플레이 없는 경우) 이 설정과 무관하게 잠든다.

## 6. LLM 킬 스위치

`scheduler`/`worker`는 `${OPS_DIR}`를 `/ops`에 읽기 전용으로 마운트하고 `LLM_KILL_SWITCH_FILE=/ops/LLM_KILL_SWITCH`를 쓴다.
호스트의 비용 가드가 `<OPS_DIR>/LLM_KILL_SWITCH`를 만들면 컨테이너 안의 LLM 호출도 즉시 막히고,
`generate` 잡은 시작하자마자 `skipped (llm_kill_switch)`로 끝난다. 확인:

```bash
docker compose run --rm worker generate   # 킬 스위치가 켜져 있으면 status=skipped
```

## 7. 비밀번호를 잃어버렸을 때

컨테이너 안 로컬 소켓 접속은 비밀번호가 필요 없다.

```bash
NEW=$(openssl rand -hex 24)
docker compose exec db psql -U newsletter -d newsletter -c "ALTER USER newsletter PASSWORD '$NEW'"
# .env의 POSTGRES_PASSWORD를 $NEW로 바꾼 뒤
docker compose up -d
```

## 8. 백업

```bash
docker compose exec -T db pg_dump -U newsletter -d newsletter -Fc > backup_$(date +%Y%m%d).dump
# 복원: docker compose exec -T db pg_restore -U newsletter -d newsletter --clean < backup_YYYYMMDD.dump
```

## 9. generate/train 켜기 (나중에)

1. `docker/crontab`에서 해당 줄의 주석을 푼다.
2. `.env`에 `AI_ENV_FILE=<LLM 키가 든 파일 경로>`를 넣는다(키는 generate에만 필요).
3. `GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build scheduler`

## 10. 스키마 변경 반영

새 Alembic 리비전을 머지한 뒤: `docker compose build && docker compose up -d` - `migrate`가 먼저
`upgrade head`를 돌리고 나서 `api`/`scheduler`가 뜬다(`service_completed_successfully`).

## 11. 의존성 잠금 갱신

worker 이미지는 `docker/requirements-worker.txt`(잠금 파일)만 설치한다. 최상위 목록은 `.in` 파일이다.

```bash
uv pip compile docker/requirements-worker.in --universal --python-version 3.11 --torch-backend cpu \
  -o docker/requirements-worker.txt
```

API 이미지는 `backend/requirements.txt`만 설치한다. `backend/requirements-airflow.txt`는 보관된 DAG을 직접 띄워 볼 때만 쓴다(어떤 이미지에도 들어가지 않는다).

## 12. 서버(Oracle Always Free A1) 배포 메모

- arm64 이미지는 서버에서 직접 `docker compose build`로 만든다(CI는 amd64만 빌드 검증).
- `COMPOSE_FILE`은 기본값(docker-compose.yml만)으로 두고 `compose.mac.yaml`은 쓰지 않는다 -
  crontab의 embed 줄이 컨테이너 CPU로 돌고, 첫 embed 실행이 `hf_cache` 볼륨에 BGE-M3(약 2.2GB + ONNX 사본)를 내려받는다.
  CPU 처리량이 수집량을 따라가는지는 `job_runs`의 embed `remaining`으로 본다(ADR 0006).
- `DB_BIND`/`API_BIND`는 `127.0.0.1` 그대로 두고, 외부 공개는 리버스 프록시(TLS)로만 한다.
- `SLACK_WEBHOOK_URL`을 설정해 실패 알림을 받는다.
