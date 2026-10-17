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
| `ingest --stages rss,extract --extract-deadline-s 2700` | 매시 05분 | RSS → 본문 추출. 세계일보 피드는 20건 ≈ 2.7시간 분량이라 매시 돌린다. 본문 추출은 ingest 시작 뒤 45분이 지나면 새 기사를 보내지 않고 남은 기사를 다음 회차로 미룬다([ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 결정 9). rss 단계에서 정책브리핑 Open API도 받는다(`.env`에 `DATA_GO_KR_SERVICE_KEY`가 있을 때만, 본문을 API가 줘서 추출 대상이 아님, 실패해도 경고만 - [ADR 0023](adr/0023-data-sources-copyright-retention.md)) |
| `embed --time-budget-s 2400` | 매시 20분 | 본문 있고 임베딩 없는 기사를 BGE-M3로. **Mac에서는 컨테이너 줄이 꺼져 있고**(`JOBS_DISABLED=embed`) 호스트 launchd 에이전트가 같은 시각에 MPS로 돈다 |
| `popularity` | 매시 35분 | 뉴스레터가 아직 없어서 빈 랭킹이 저장되는 게 정상 |
| `cluster` | 매일 14:50 UTC (23:50 KST) | LLM 호출 없이 클러스터 통계만 `job_runs`에 기록 |
| `daily_report` | 매일 00:10 UTC (09:10 KST) | 최근 24시간 요약 (로그, `SLACK_WEBHOOK_URL`이 있으면 Slack) |
| `retention` | 매일 17:50 UTC (02:50 KST) | 수집 후 30일 지난 상업 언론사 본문을 비운다(해시·길이·제목·URL·임베딩 유지, 정책브리핑 면제 - [ADR 0023](adr/0023-data-sources-copyright-retention.md)). `--dry-run`은 세기만 한다. 배포·확인·되돌리기는 [runbook-hosting.md](runbook-hosting.md) 4.1 |
| `generate`, `batch_fallback` | **꺼져 있음** | LLM 평가 프로토콜·사실성 게이트 결정 전까지 켜지 않는다 |
| `rebuild_user_state --check` | **꺼져 있음** | 장기 프로필 상태가 클릭 로그와 맞는지 점검(쓰지 않음). 사용자가 생기면 켠다 (ADR 0033) |
| `train` | **은퇴** | 팀 시절 학습 레시피. 불려도 아무것도 실행하지 않고 `skipped`로 남는다. 모델은 `scripts/register_model.py`로 올린다 (ADR 0033) |

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

### 운영 런타임은 전용 워크트리에서

compose `.env`, `.venv-jobs`, launchd 에이전트가 가리키는 경로는 모두 "compose를 띄운 체크아웃"에 묶인다. 개발용
워크트리(특히 작업이 끝나면 정리되는 일회성 워크트리)에서 띄우면 그 워크트리가 정리될 때 임베딩 에이전트가 조용히
실패한다. 그래서 운영은 개발과 분리된 detached 워크트리 `~/Projects/newsletter-runtime`에서 한다(2026-09-26부터):

```bash
git -C ~/Projects/newsletter-recsys worktree add --detach ~/Projects/newsletter-runtime origin/<배포할 브랜치 또는 main>
cp <이전 런타임>/.env ~/Projects/newsletter-runtime/.env && chmod 600 ~/Projects/newsletter-runtime/.env
cd ~/Projects/newsletter-runtime
scripts/mac_embed_agent.sh setup && scripts/mac_embed_agent.sh install   # 같은 라벨이라 이전 등록을 덮어쓴다
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build              # 프로젝트 이름이 고정이라 같은 볼륨을 이어 쓴다
```

코드 갱신(배포): 잡이 돌고 있지 않은지(3절 advisory lock 쿼리) 확인한 뒤

```bash
cd ~/Projects/newsletter-runtime && git fetch origin && git checkout --detach origin/<브랜치>
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build   # migrate가 먼저 upgrade head
scripts/mac_embed_agent.sh setup                              # docker/requirements-worker.txt가 바뀐 경우만
```

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
  `docker compose stop scheduler`·재배포·`docker stop`으로 끊긴 실행은 `failed` + `terminated by SIGTERM`으로 남고,
  그때까지의 stats가 보존된다. supercronic은 잡에 신호를 넘기지 않아서 `docker/scheduler-entrypoint.sh`가 잡의
  프로세스 그룹에 SIGTERM을 직접 보낸다(유예 60초, `stop_grace_period`). 유예 안에 끝나지 않거나 OOM·SIGKILL로
  죽은 실행은 `running`으로 남았다가 다음 실행이 `abandoned`로 바꾸고 알림을 보낸다.
- `running` 행의 `stats`는 잡이 도는 동안 갱신된다(ingest는 단계마다, embed는 배치마다) - 강제 종료된 실행도 어디까지 했는지 남는다.
- `succeeded`여도 `stats->'warnings'`가 있으면 알림이 간다(예: RSS 피드 하나를 읽지 못했다, 한 언론사의 본문
  다운로드가 연달아 실패해 남은 기사를 다음 실행으로 미뤘다, 추출 시간 예산을 다 썼다). RSS 경고는 피드마다
  `<피드>: RSS 수집 실패 (<원인 종류>) - 이번 회차 신규 기사 0건`이다 - 그 언론사는 추출 대상이 없을 수 있어 추출
  경고가 나지 않으므로 이것이 그 회차의 유일한 신호다. 모든 RSS 피드를 읽지 못하면 ingest는 `failed`로 끝난다.
  피드별 원인 전문은 `stats->'rss'->'per_feed'-><피드>->>'error'`다.
  ingest가 본문 다운로드 실패로 `failed`가 되는 것은 실패가 한 언론사로 설명되지 않을 때다 - 건강하지 않은 언론사가
  두 곳 이상이거나, 가장 많이 실패한 언론사를 빼고도 받은 기사 5건 이상 중 절반 이상이 실패했을 때
  ([ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 결정 9). 이 규칙을 넣기 전 코드(Tier 0 VM의 `13801fc` 포함)는
  "대상 5건 이상 중 50% 이상 실패면 `failed`"다 - 어느 규칙으로 돈 실행인지는 `job_runs.git_sha`로 가른다.
  언론사별 실패 원인 개수는 `stats->'extract'->'fetch_fail_causes'`, 미룬 기사는 `stats->'extract'->'deferred'`다.
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
         count(*) FILTER (WHERE n.raw_news_extract_status = 'duplicate') AS duplicate,
         count(*) FILTER (WHERE n.raw_news_extract_status IN ('fetch_failed', 'error')) AS failed,
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
깨어난 뒤 ingest는 각 피드가 **지금 보여 주는** 항목만 받는다 - RSS의 100시간 cutoff는 피드가 내보내는 창을 넓히지
못한다. 창이 짧은 피드(세계일보는 20건 ≈ 2.7시간)는 그보다 오래 잠들면 그 사이 기사를 영구히 잃는다(ADR 0006 결과와 한계).
며칠 수집을 계속하려면 전원을 연결하고 `scripts/mac_embed_agent.sh keep-awake install`(caffeinate -s)을 켜 둔다.
노트북 덮개를 닫으면(외부 디스플레이 없는 경우) 이 설정과 무관하게 잠든다.

## 6. LLM 킬 스위치

`scheduler`/`worker`는 `${OPS_DIR}`를 `/ops`에 읽기 전용으로 마운트하고 `LLM_KILL_SWITCH_FILE=/ops/LLM_KILL_SWITCH`를 쓴다.
디렉터리째 마운트하는 이유: 비용 가드가 파일을 만들고 지우므로 파일 하나만 바인드 마운트하면 없을 때 Docker가 그 자리에
디렉터리를 만들고, 파일이 교체되면 마운트가 옛 inode를 가리킨다. 대가로 `.ops/`의 다른 파일(예산 상태, OCI 재시도 스크립트
·로그)도 컨테이너 안에서 읽힌다 - 컨테이너는 이 저장소 코드만 돌리므로 받아들이되, `.ops/`에는 비밀값을 두지 않는다.
호스트의 비용 가드가 `<OPS_DIR>/LLM_KILL_SWITCH`를 만들면 컨테이너 안의 LLM 호출도 즉시 막히고,
`generate` 잡은 시작하자마자 `skipped (llm_kill_switch)`로 끝난다. 확인:

```bash
docker compose run --rm worker generate   # 킬 스위치가 켜져 있으면 status=skipped
```

### LLM 지출 상한 (ADR 0035)

킬 스위치를 켜는 주체가 하나 더 있다. LLM 클라이언트는 요청마다 최악 비용을 지출 원장에 예약하고, 런·일·전체 상한 중
하나를 넘으면 요청을 보내지 않고 멈춘다. 일·전체 상한이면 킬 스위치 파일도 만든다(파일 내용이
`{"engaged_by": "llm_spend_cap", ...}`이면 이 가드가 켠 것이다).

- 원장은 `<OPS_DIR>/llm_spend_ledger.jsonl`이다(compose가 `LLM_SPEND_LEDGER_FILE=/ops/llm_spend_ledger.jsonl`로 준다).
  호스트에서 직접 돌리는 실행의 기본 원장도 메인 체크아웃의 `.ops/llm_spend_ledger.jsonl`이라 같은 파일을 쓴다.
  `LLM_SPEND_LEDGER_FILE`을 직접 지정한다면 절대 경로여야 한다(상대 경로는 거부한다).
- **원장은 자동으로 생기지 않는다.** `spend_cli init`으로 한 번 만든다. 원장이 없으면 모든 LLM 호출이 거부되고
  `generate`는 클러스터링 전에 `failed`(종료 코드 3, `stats.reason=llm_budget_unavailable`)로 끝난다. 지워진 원장이
  0부터 다시 세지 않게 하려는 것이다.
- 원장의 **누계 기록**(원장 id·크기·정산 누계·열린 예약)은 원장과 다른 곳에 있다. 호스트는
  `~/.local/state/newsletter-recsys/llm-spend/`(`LLM_SPEND_STATE_DIR`로 바꾼다), 컨테이너는 이름 있는 볼륨
  `llm_spend_state`(`/spend-state`). 원장이 그 기록과 어긋나면(다른 파일, 줄어듦, 누계가 작음) 호출이 거부된다.
- `/ops`는 기본이 읽기 전용이다. 그 상태에서는 원장을 쓸 수 없어 **컨테이너 안의 LLM 호출이 전부 거부된다**.
  generate를 켤 때 `.env`에 `OPS_MOUNT_MODE=rw`를 넣는다 - 그러면 컨테이너가 `.ops/`의 다른 파일도 고칠 수 있게 된다.
- 호스트와 컨테이너는 누계 기록이 따로다. 호스트에서 만든 원장을 컨테이너가 처음 쓸 때는 컨테이너 안에서
  `adopt --yes`를 한 번 해야 한다(이어받을 금액 0, 지금 원장을 기준으로 삼는다). 호스트 사용자가 0600으로 만든 원장을
  컨테이너 사용자(uid 10001)가 열 수 있는지는 호스트에 따라 다르다 - **컨테이너에서 실제로 돌려 본 적은 없다.**
- Mac(colima)에서 호스트와 컨테이너가 같은 원장을 **동시에** 쓰지 않는다. 바인드 마운트를 건너는 파일 잠금은
  보장되지 않는다(Linux 호스트는 같은 커널이라 문제없다).

```bash
# 호스트에서(ai_workspace/에서 실행). 컨테이너에서는 앞에 `docker compose run --rm --entrypoint python worker`를 붙인다
python -m core.llm.spend_cli init --note "첫 유료 실험"                    # 처음 한 번
python -m core.llm.spend_cli summary                                        # 오늘·전체 사용액, 상한, 원장 무결성
python -m core.llm.spend_cli estimate --plan generator:15:17000:8192        # 0원. 계획한 호출의 최악 비용
# 일 상한에 닿아 멈춘 뒤, 콘솔 청구액을 확인하고 그날 창만 비울 때(전체 누계는 그대로):
python -m core.llm.spend_cli reset-day --yes --clear-kill-switch --note "사유"
```

**"LLM 지출을 확인할 수 없어 호출을 거부합니다"가 뜰 때.** 메시지에 사유와 다음 명령이 들어 있다. `summary`의
"무결성" 줄도 같은 내용을 보여 준다. 전부 `--yes` 없이 실행하면 무엇을 할지만 보여 준다.

| 사유 | 무슨 일이 있었나 | 조치 |
|---|---|---|
| `missing` | 원장 파일이 없다(처음이거나 `.ops/`가 지워졌다. `git clean -fdx`는 gitignore된 `.ops/`도 지운다) | `init`. 전에 쓰던 원장이었다면 이어서 `adopt --yes` |
| `uninitialized` | 파일은 있는데 첫 줄이 init 헤더가 아니다(0바이트로 잘림, 다른 파일) | 파일을 다른 이름으로 옮기고 `init` → `adopt --yes` |
| `corrupt` | 해석할 수 없는 줄이 있다(끝의 끊긴 한 줄은 저절로 처리된다) | 줄을 확인하고 `repair --yes`. 누계가 내려갔다고 나오면 `adopt --yes` |
| `replaced` · `shrunk` · `behind` | 원장이 다른 파일로 바뀌었거나, 줄었거나, 정산 누계가 누계 기록보다 작다 | `adopt --yes` - 누계 기록의 사용액(정산 + 그때 열려 있던 예약)을 지금 원장에 이어받는다 |
| `no_watermark` · `bad_watermark` | 누계 기록이 없거나 읽을 수 없다(상태 디렉터리를 지웠다, 다른 기기·컨테이너에서 처음 쓴다) | `adopt --yes` - 이어받을 금액은 알 수 없어 0이다. 실제로 더 썼다면 `LLM_BUDGET_TOTAL_USD`를 그만큼 낮춘다 |

`adopt`는 누계를 올리기만 한다. 원장과 누계 기록을 **둘 다** 지우면 상한이 처음부터 다시 시작한다 - 그 경우의 방어는
AI Studio의 프로젝트 지출 상한과 선불 잔액뿐이다.

상한에 닿은 `generate`는 그때까지 만든 뉴스레터를 남기고 `failed`(종료 코드 3)로 끝나며, `job_runs.stats`의
`reason`·`llm_stop`·`llm_spend`에 사유와 사용액이 남는다. 상한 값은 `ai_workspace/.env.example`의
`LLM_BUDGET_*_USD`를 본다(기본 런 $0.20 / 일 $0.30 / 전체 $3.00).

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

**덤프의 본문도 30일 규칙을 따른다**([ADR 0023](adr/0023-data-sources-copyright-retention.md) 개정 2026-10-17). 덤프는 DB 전체의
본문을 담고 행 단위로 지울 수 없으므로, 덤프 안에서 가장 오래된 상업 언론사 본문의 수집일 + 30일에 파일을 통째로 지운다.
그 날짜를 파일 이름(`<이름>.expires-YYYY-MM-DD.dump`)에 적는다. 보존 잡이 날마다 도는 DB라면 전체 덤프는 뜬 지 하루 안팎에
만료된다. Mac에는 상주 작업을 두지 않으므로(지우는 데몬도 없다) 덤프를 다룰 때마다 사람이 확인한다:

```bash
# 만료일(UTC 날짜)이 오늘이거나 지난 덤프를 보여 준다 - 보이면 지운다(사본이 있는 곳도 같은 날).
# 셸 glob 대신 find를 쓴다: zsh는 맞는 파일이 없으면 glob에서 "no matches found"로 멈춘다(bash·zsh에서 확인).
find data/backups -name '*.expires-*.dump' 2>/dev/null | sort | while read -r f; do d=${f##*.expires-}; d=${d%.dump}; \
  [[ "$d" > "$(date -u +%F)" ]] || echo "만료: $f ($d)"; done
# 만료일이 이름에 없는 예전 덤프: mac-compose-2026-09-26.dump 2026-10-25, tier0-micro-2026-09-26T2218.dump 2026-10-26
```

덤프를 복원한 DB에는 덤프 시점의 본문이 다시 들어 있다. 복원한 뒤에는 다른 잡보다 먼저 `docker compose run --rm worker retention`을 돌린다.

## 9. generate 켜기 (나중에)

1. `docker/crontab`에서 해당 줄의 주석을 푼다.
2. `.env`에 `AI_ENV_FILE=<LLM 키가 든 파일 경로>`를 넣는다(키는 generate에만 필요).
3. `.env`에 `OPS_MOUNT_MODE=rw`를 넣는다 - LLM 지출 원장을 쓸 수 있어야 호출이 허용된다(6절 "LLM 지출 상한").
   원장이 아직 없으면 `docker compose run --rm --entrypoint python worker -m core.llm.spend_cli init`으로 만들고,
   호스트에서 이미 만든 원장이면 같은 방식으로 `adopt --yes`를 한 번 한다(컨테이너의 누계 기록이 따로라서).
   상한(`LLM_BUDGET_RUN_USD`·`LLM_BUDGET_DAY_USD`·`LLM_BUDGET_TOTAL_USD`)을 기본값과 다르게 쓰려면 `AI_ENV_FILE`에 넣는다.
4. `GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build scheduler`

## 9-1. 랭커 모델과 장기 프로필 상태 (ADR 0033)

**장기 프로필 상태 채우기.** 리비전 `a6f1e83b0d57`은 `user_profile_state` 테이블만 만든다. 그 전에 쌓인 클릭이나
클릭 API를 거치지 않고 들어온 클릭(시드·이관)은 잡이 반영한다. 채우기 전에는 클릭 이력이 있는 사용자도 장기
프로필이 없는 사용자로 읽힌다.

```bash
docker compose run --rm scheduler python -m jobs.run rebuild_user_state           # 다시 만들기(몇 번 돌려도 같다)
docker compose run --rm scheduler python -m jobs.run rebuild_user_state --check   # 쓰지 않고 로그와 다른 사용자만 센다
```

배포 뒤 한 번으로 끝나지 않는다. 상태는 클릭 로그의 캐시라 아래 경우에 로그와 어긋나고, 다시 만들어야 맞는다.

| 어긋나는 경우 | 보이는 모습 |
|---|---|
| 클릭 API를 거치지 않은 클릭(시드·이관), 리비전 적용 전의 클릭 | 클릭 이력이 있는데 장기 프로필이 없는 사용자로 읽힌다 |
| 클릭 API의 상태 갱신 실패(클릭 행은 저장된다) | API 로그의 `profile state update failed` |
| **클릭 시점에 임베딩이 없던 뉴스레터의 클릭**(임베딩은 나중에 채워진다), 클릭 뒤에 임베딩이 다시 계산된 뉴스레터 | 그 클릭이 그 사용자의 가장 최근 클릭인 동안에는 요청마다 어댑터 피처가 남지 않는다(`GET /recsys/stats`의 `features.inputs_missing`이 요청 수만큼 는다). 그 뒤에 다른 클릭이 들어오면 조용히 어긋난 채 남는다 |

마지막 줄의 뒤쪽 경우는 서빙이 알아채지 못한다(ADR 0033 한계). 찾는 방법은 `--check`뿐이라, 사용자가 생기면
`docker/crontab`의 `rebuild_user_state --check` 줄을 켜고, 경고가 뜨면(`job_runs.stats`의 `warnings`,
`SLACK_WEBHOOK_URL`이 있으면 Slack) 다시 만든다.
`features.inputs_missing`이 계속 늘 때도 같은 조치다.

**모델 등록.** 학습한 LightGBM text 모델을 `model_registry`에 올린다. 서빙 피처 어댑터와 열 이름·순서가 다른
모델은 이유와 함께 거절된다. 기본 역할은 shadow다(응답은 바뀌지 않고 칸 로그에 점수만 남는다).

```bash
python scripts/register_model.py --model <model.txt> --version <버전> --dry-run     # 검증만(DB 접속 없음)
DATABASE_URL=postgresql://... python scripts/register_model.py --model <model.txt> --version <버전>
```

API는 60초마다 레지스트리를 확인한다(`RECSYS_MODEL_RELOAD_S`). shadow가 실제로 점수를 남기는지는
`GET /recsys/stats`의 `shadow.scored` / `features.computed`와 칸 로그의 `scores_shadow`로 본다.
`RECSYS_FEATURE_FN=off`면 어댑터 피처와 shadow가 전부 꺼진다(휴리스틱만).

**EB-NeRD M4 모델을 shadow로 올리기 (ADR 0037).** 모델 파일은 저장소·이미지에 없다. 개발용 Mac의 M4 체크포인트
(gitignore된 `data/` 아래의 `models/<arm>_s<seed>.txt`와 같은 이름의 `.json`)에서 두 쌍을 서빙 DB에 닿는 곳의 임시 디렉터리로
옮기고, 검사 → 등록 → 임시 파일 삭제 순서로 한다. 등록 순서가 shadow 칸 순서다(마지막이 첫 칸 = 주모델).

```bash
# 1) 옮기기(예: scp). 저장소 작업 트리 안이면 gitignore된 자리여야 한다 - 도구가 확인한다.
scp <체크포인트>/models/poolneg_s0.{txt,json} <체크포인트>/models/poolneg_mask0_s0.{txt,json} <호스트>:/tmp/m4/
# 2) 검사만(DB 접속 없음): 허용 arm·seed, 판정 리포트와 피처 목록·best_iteration·트리 수, 서빙 어댑터 22열
python scripts/register_ebnerd_shadow.py --model /tmp/m4/poolneg_s0.txt --model /tmp/m4/poolneg_mask0_s0.txt --dry-run
# 3) 등록(둘 다 통과해야 하나도 올라간다), 4) 임시 파일 지우기
DATABASE_URL=postgresql://... python scripts/register_ebnerd_shadow.py --model /tmp/m4/poolneg_s0.txt --model /tmp/m4/poolneg_mask0_s0.txt
rm -r /tmp/m4
```

등록한 뒤 ADR 0037의 "결과와 한계"에 날짜·버전·모델 sha256(출력의 `model_sha256`)을 적는다. 내리는 조건(상업적 사용,
`model_registry`의 공개 반출, 라이선스가 모델 사용을 막음)이 생기면 행을 retired로 바꾸고 지운다. 같은 모델로 parity 게이트를 돌리려면
테스트 DB에서 `RECSYS_PARITY_MODEL_FILE=/tmp/m4/poolneg_mask0_s0.txt pytest -m integration tests/integration/test_feature_parity.py`.

**활성 휴리스틱과 콜드스타트 임계 (ADR 0014, 0031).** `RECSYS_HEURISTIC_SET`(기본 `b`; `a`는 ADR 0014 사전 등록 6의 전환이 성립했을
때만, `prior`는 되돌리기), `RECSYS_MIN_PERSONAL_EVENTS`(기본 0). 6시간 클릭 수 조회 실패는
`GET /recsys/stats`의 `popularity.clicks_error`, 임계에 걸린 사용자는 `profile.below_min_events`로 센다.
`RECSYS_SHORT_TERM_HOURS`·`RECSYS_SHORT_TERM_MAX_CLICKS`는 없어졌다(남아 있어도 읽지 않는다).

`prior`는 M5 전의 요청 경로로 돌아간다: heuristic-v1, 콜드 경로와 후보 출처 `popular`의 `compute_scores` 순서, 6시간 클릭 수 조회 없음
(ADR 0014 변경 기록 1). 그래서 지연 때문에 되돌릴 때도 쓸 수 있다. 지연의 운영 임계는 ADR 0014 구현 기록 4다 —
`fallback.timeout` / `requests`가 하루 1%를 넘거나 `recsys-bench`의 16 클라이언트 p95가 290ms를 넘으면 c6의 정의를 바꾸지 않는
최적화를 먼저 적용하고, 급하면 `prior`나 이전 이미지로 되돌린다. `GET /recsys/stats`의 카운터는 워커 프로세스별로 뜬 뒤의 누적이라 두 시각의 차로 본다.

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

## 13. integration 테스트 (운영 DB 금지)

`jobs.run` 잡은 `news_raw` 전체를 대상으로 돈다 - compose의 `db`(실제 수집 데이터, `127.0.0.1:5433`)에서
integration 테스트를 돌리면 가짜 임베딩이 실제 기사에 저장된다. 테스트는 DB 이름에 `test`가 없거나
`news_raw`에 테스트가 만들지 않은 행이 있으면 실패하도록 막혀 있다(`tests/integration/conftest.py`).
로컬에서는 일회용 컨테이너를 쓴다:

```bash
docker run -d --rm --name newsletter-itest-pg -p 127.0.0.1:5434:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=newsletter_test pgvector/pgvector:pg16
export TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5434/newsletter_test DATABASE_URL=$TEST_DATABASE_URL
.venv/bin/python -c "import os,psycopg2; c=psycopg2.connect(os.environ['DATABASE_URL']); c.autocommit=True; c.cursor().execute('CREATE EXTENSION IF NOT EXISTS vector')"
(cd backend && ../.venv/bin/python -m alembic upgrade head)
.venv/bin/python -m pytest -q -m integration tests/
docker stop newsletter-itest-pg   # --rm이라 컨테이너와 데이터가 함께 사라진다
```
