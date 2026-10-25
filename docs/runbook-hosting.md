# 호스팅 런북 — 개발(Mac) · Tier 0(OCI E2.1.Micro) · Tier 1(OCI A1)

계층을 이렇게 나눈 이유와 측정 근거는 [ADR 0026](adr/0026-hosting-tiers-and-contingency.md)을 본다.
compose 스택 자체의 상태 확인·멈추기·백업은 [runbook.md](runbook.md)를 따르고,
이 문서는 "어느 호스트에서 무엇을 돌리고, 호스트를 어떻게 만들고, 문제가 생기면 어떻게 넘기는가"만 다룬다.

**비용 원칙: $0.** Always Free 셰이프만 만들고, 유료 전환(PAYG)·유료 셰이프·200 GB를 넘는 볼륨은 만들지 않는다.

**기계별 값은 저장소에 적지 않는다.** IP·OCID·SSH 설정은 메인 체크아웃의 `.ops/micro/`(gitignore)에 둔다.
아래 명령의 `$TENANCY`(루트 컴파트먼트 = 테넌시 OCID), `$AD`, `$SUBNET`, `$IMAGE`, `$INSTANCE`는 거기서 채운다.
SSH는 `ssh -F .ops/micro/ssh_config micro`로 접속한다(전용 known_hosts를 써서 `~/.ssh`를 건드리지 않는다).

## 1. 무엇이 어디서 도는가

2026-09-26 17:00 KST부터(노트북 응답성을 위해 Mac 로컬 서버를 모두 내린 뒤)의 배치다. A1이 확보될 때까지
**Tier 0가 유일한 수집기**이고, 기록 시스템은 "Mac 덤프(16:59 KST, 동결) + Tier 0 DB"의 합집합이다.
2026-10-25부터 Tier 0는 승격 조건(ADR 0026 결정 3)을 충족한 기록 시스템이다 - 범위 한정(RSS 메타데이터와 일부 언론사 본문), 소유자 위임 아래 판단,
증거는 ADR 0026 증거 13. 2026-11-01 07:00 UTC 이후 지금 코드의 7일 창으로 다시 확인하고 하나라도 못 미치면 "임시 단독 수집"으로 되돌린다.

| 작업 | 개발(Mac) | Tier 0 (E2.1.Micro, 1 GB) | Tier 1 (A1, 2 OCPU/12 GB) |
|---|---|---|---|
| Postgres + pgvector | 꺼 둠 — 16:59 KST 덤프만 보관 | O — **유일한 수집 DB**(2026-10-25부터 기록 시스템, 범위 한정 - ADR 0026 증거 13. 그 전에는 "임시 단독 수집") | O (확보 시 기록 시스템) |
| `ingest --stages rss,extract` 매시 | 꺼 둠 | O (추출 워커 2개) | O |
| `rss_retry` 매시 :40 (직전 ingest가 못 읽은 피드만, 4.2) | 꺼 둠 | O | 정하지 않음 (`docker/crontab`에는 없다) |
| `embed` (BGE-M3) | 필요할 때 수동(호스트 MPS) | **X** (모델 로드만 3.6 GB) | O (컨테이너 CPU) |
| `cluster` (HDBSCAN) | 필요할 때 수동 | X (임베딩 없음) | O |
| `popularity`, `daily_report` | 꺼 둠 | O | O |
| `generate` (LLM API) | 수동·평가 시에만 | X | 생성 캡·비용 가드 결정 후 |
| API (FastAPI) | 필요할 때 루프백 | X (기본 꺼짐, `--profile api`) | O (루프백 + 리버스 프록시) |
| EB-NeRD 등 반출 금지 데이터 | O (Mac 전용) | X | X |

Tier 0가 유일한 수집기인 동안의 최대 손실은 "마지막 호스트 밖 백업(6.5) 이후 수집분"이다.

**2026-10-06 04:41 KST 현재**(ADR 0026 증거 8): 배치는 위 표 그대로다. Tier 0 `news_raw` 14,230행, 마지막 호스트
밖 백업은 09-26 22:18(795행) — 그 뒤 13,435행은 VM에만 있다. **다른 작업보다 먼저 6.5를 돌린다.**
VM의 코드는 09-26 스냅숏이다. 정시 ingest의 85%만 성공하고 있다(동아일보 요청 실패로 1시간을 넘기는 회차가
다음 회차를 건너뛰게 한다 — 6.6).

**2026-10-17 04:34 KST 갱신**: VM의 코드를 4.1의 절차로 `13801fc`(본문 보존 잡·발행 시각 KST 수정 포함)로 바꿨다.
직전 호스트 밖 백업은 같은 날 04:27 KST(`news_raw` 30,754행). 실측은 4.1 끝의 "2026-10-17 배포 기록"이다.

**2026-10-17 15:35 KST 갱신**: 호스트 밖 백업이 자동이다 — VM 호스트의 cron이 매일 18:10 UTC(03:10 KST)에 OCI Object Storage로
올리고 28일마다 복원해 본다(6.5, ADR 0026 증거 11). 최대 손실은 하루치 수집분이다. 유휴 지표도 같은 cron이 매일 잰다(5절).
AI타임스 기존 605행의 발행 시각을 보정했다(6.7).

## 2. Tier 0 인스턴스 만들기

### 2.1 사전 확인 — 하나라도 어긋나면 만들지 않는다

```bash
export SUPPRESS_LABEL_WARNING=True
# 홈 리전인가 (Always Free 컴퓨트는 홈 리전에서만)
oci iam region-subscription list --tenancy-id $TENANCY \
  --query 'data[].{r:"region-name",home:"is-home-region"}' --output table
# 계정 플랜: FREE_TIER 이고 is-intent-to-pay=false 여야 한다(PAYG면 멈추고 사람에게)
oci osp-gateway subscription-service subscription list --osp-home-region <home-region> \
  --compartment-id $TENANCY \
  --query 'data.items[].{plan:"plan-type",upg:"upgrade-state",intent:"is-intent-to-pay"}'
# 셰이프가 ALWAYS_FREE 과금 유형인가
oci compute shape list --compartment-id $TENANCY --availability-domain "$AD" --all \
  --query 'data[?shape==`VM.Standard.E2.1.Micro`].{s:shape,billing:"billing-type"}'
# 한도: available ≥ 1, used가 기대값과 같은가 (테넌시당 최대 2대)
oci limits resource-availability get --service-name compute \
  --limit-name vm-standard-e2-1-micro-count --compartment-id $TENANCY --availability-domain "$AD"
# 기존 인스턴스와 부트/블록 볼륨 합계 (Always Free 합계 200 GB; A1 대기분 100 GB 포함해 계산)
oci compute instance list --compartment-id $TENANCY --all \
  --query 'data[].{n:"display-name",s:shape,st:"lifecycle-state"}' --output table
oci bv boot-volume list --compartment-id $TENANCY --availability-domain "$AD" \
  --query 'data[].{n:"display-name",gb:"size-in-gbs"}' --output table
oci bv volume list --compartment-id $TENANCY --query 'data[].{n:"display-name",gb:"size-in-gbs"}' --output table
```

멈춤 조건: `plan-type`이 `FREE_TIER`가 아님, `billing-type`이 `ALWAYS_FREE`가 아님, 볼륨 합이 200 GB를 넘게 됨,
이미 같은 이름의 인스턴스가 있음.

### 2.2 생성

```bash
# x86_64 Ubuntu 24.04 이미지(E2.1.Micro 호환) 중 최신
oci compute image list --compartment-id $TENANCY --operating-system "Canonical Ubuntu" \
  --operating-system-version "24.04" --shape VM.Standard.E2.1.Micro \
  --sort-by TIMECREATED --sort-order DESC --limit 1 --query 'data[0].{id:id,n:"display-name"}'

oci compute instance launch --compartment-id $TENANCY --availability-domain "$AD" \
  --shape VM.Standard.E2.1.Micro --image-id $IMAGE --subnet-id $SUBNET --assign-public-ip true \
  --display-name newsletter-micro --boot-volume-size-in-gbs 50 \
  --ssh-authorized-keys-file ~/.ssh/oci_newsletter_ed25519.pub --query 'data.id' --raw-output
```

`Out of host capacity`면 몇 분 간격으로 몇 번만 다시 시도한다. 몇 시간짜리 재시도 루프는 A1에만 쓴다(6절).
만든 뒤 인스턴스 OCID·공인 IP·`ssh_config`를 `.ops/micro/`에 적는다.

## 3. 호스트 초기 설정

```bash
scp -F .ops/micro/ssh_config scripts/oci/micro_bootstrap.sh micro:
ssh -F .ops/micro/ssh_config micro 'sudo bash micro_bootstrap.sh'   # 첫 실행 약 5분, 재실행 약 30초
ssh -F .ops/micro/ssh_config micro 'sudo systemctl reboot'          # 커널 업데이트가 있으면
```

재실행은 `apt-get full-upgrade`를 다시 돌린다. `docker.io`가 올라가면 dockerd가 다시 시작돼 돌고 있는 잡의
기록이 흐트러질 수 있으므로, 수집 스택이 떠 있는 VM에서는 ingest가 돌지 않을 때만 재실행한다. ingest는 매시
05분에 시작해 길면 1시간을 넘긴다 — 시각으로 짐작하지 말고 4절의 잡 상태 조회에서 `finished_at`이 찼는지 본다.

스크립트가 하는 일: 2 GB 스왑(`vm.swappiness=10`), 전체 패키지 업그레이드, fail2ban(sshd, 5회/10분 → 1시간 차단),
unattended-upgrades(보안 업데이트 자동 적용, 재부팅 필요 시 18:40 UTC = 03:40 KST), Ubuntu 아카이브의
`docker.io`·`docker-compose-v2`, rpcbind·ModemManager·udisks2 정지, sshd 조이기(root·비밀번호 로그인 금지,
`AllowUsers ubuntu`), journald 200 MB 상한, Docker 로그 10 MB×3 상한.

**UFW는 쓰지 않는다.** OCI 공식 문서(Compute Best Practices, "Essential Firewall Rules")가 Ubuntu 이미지에서
UFW로 규칙을 고치면 인스턴스가 부팅되지 않을 수 있다고 경고한다. 이미지의 iptables 규칙이 이미
"22/tcp 신규 연결만 허용, 나머지 REJECT"이므로 그대로 두고, 스크립트는 그 규칙이 있는지 확인만 한 뒤
IPv6(`/etc/iptables/rules.v6`)를 같은 정책으로 채운다. VCN 보안 목록도 인그레스는 22만 연다.

확인:

```bash
ssh -F .ops/micro/ssh_config micro 'swapon --show; sudo iptables -S INPUT; sudo ip6tables -S INPUT; \
  systemctl is-active docker fail2ban unattended-upgrades'
for p in 22 111 5432 8000; do nc -z -G 5 <공인 IP> $p && echo "$p open" || echo "$p closed"; done  # 22만 open
```

Docker가 publish한 포트는 iptables INPUT 규칙을 우회한다. compose의 `DB_BIND`/`API_BIND`는 반드시
`127.0.0.1`로 둔다(기본값).

## 4. 수집 스택 올리기 (Tier 0)

Tier 0는 `docker-compose.yml` 위에 `docker/compose.micro.yaml`을 얹는다: 수집 전용 이미지
(`docker/ingest.Dockerfile`, torch 없음), `docker/crontab.micro`(ingest 추출 워커 2개·rss_retry·popularity·daily_report),
Postgres `shared_buffers=64MB`·`max_connections=30`, api 기본 꺼짐, scheduler `mem_limit 512m`.

VM에는 저장소 자격 증명을 두지 않는다. Mac에서 커밋 스냅숏을 만들어 보낸다:

```bash
git archive --format=tar.gz -o /tmp/tier0.tar.gz <배포할 커밋>
scp -F .ops/micro/ssh_config /tmp/tier0.tar.gz micro:
ssh -F .ops/micro/ssh_config micro 'mkdir -p ~/newsletter-recsys && tar -xzf ~/tier0.tar.gz -C ~/newsletter-recsys'
```

`.env`는 VM에서 만든다(비밀번호는 VM 밖으로 나오지 않는다):

```bash
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && umask 077 && [ -f .env ] || printf "%s\n" \
  "POSTGRES_USER=newsletter" "POSTGRES_PASSWORD=$(openssl rand -hex 24)" "POSTGRES_DB=newsletter" \
  "API_SECRET_KEY=$(openssl rand -hex 32)" "OPS_DIR=./.ops" \
  "COMPOSE_FILE=docker-compose.yml:docker/compose.micro.yaml" "GIT_SHA=<배포할 커밋>" > .env'
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose up -d --build'
```

이미지와 오버레이는 CI가 본다: `docker-build` 잡이 `docker/ingest.Dockerfile`을 linux/amd64로 빌드하고 그 안에서
Tier 0 잡 모듈을 임포트하며, `docker compose -f docker-compose.yml -f docker/compose.micro.yaml config -q`를 돌린다.

더 새로운 커밋으로 다시 배포하는 절차는 4.1이다(2026-10-17에 처음 돌렸다: 09-26 스냅숏 → `13801fc`).
같은 디렉터리(`~/newsletter-recsys` - compose 프로젝트 이름과 `pgdata` 볼륨 이름이 여기서 나오므로 바꾸지 않는다)에
스냅숏을 다시 풀고 `.env`의 `GIT_SHA`를 고친 뒤 `up -d --build`를 하면, `migrate`가 먼저 돌아 Alembic을 head까지
올리고 언론사 시드에 정책브리핑을 더한다. 정책브리핑 수집은 `.env`에 `DATA_GO_KR_SERVICE_KEY`를 넣었을 때만 돈다
([ADR 0023](adr/0023-data-sources-copyright-retention.md)). 하기 전에 6.5 백업을 먼저 당겨 온다.

잡 상태(최근 실행):

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db \
  psql -U newsletter -d newsletter -c \"SELECT id, job, status, started_at, finished_at - started_at AS took \
  FROM job_runs ORDER BY id DESC LIMIT 10\""
ssh -F .ops/micro/ssh_config micro 'free -m; sudo docker stats --no-stream'
```

### 4.1 본문 보존 잡 배포·확인·되돌리기 ([ADR 0023](adr/0023-data-sources-copyright-retention.md))

`python -m jobs.run retention`이 매일 17:50 UTC(02:50 KST)에 수집 후 30일이 지난 상업 언론사 본문을 비운다(본문만 NULL,
해시·길이·제목·URL·임베딩은 남김, 정책브리핑 면제). 만료는 `raw_news_crawled_at + 30일 <= 지금(UTC)`이다.
실제로 비우는 실행은 30일만 쓴다: `.env`의 `BODY_RETENTION_DAYS`가 30이 아니면 잡이 아무것도 하지 않고 실패(알림)하고,
30일보다 짧은 `--days`는 `--dry-run`과 함께일 때만 받는다(없으면 종료 코드 2로 아무것도 하지 않는다).
**VM의 첫 만료는 2026-10-26 02:36 UTC(11:36 KST)** - 그 전에 아래 배포를 끝낸다. 첫 만료분이 실제로 비워지는 것은
10-26 17:50 UTC 실행이다(하루 한 번이라 최대 보존은 30일 + 24시간 남짓, ADR 0023 개정 2026-10-17).

이 배포는 VM의 코드를 09-26 스냅숏에서 병합 커밋으로 올린다. Alembic은 `d48994e9d26e` → `8b7f830013b7` → `c4d2a91e7f30`
→ `a6f1e83b0d57` → `f1db3221e45f`로 오른다. 가운데 셋은 VM에서 비어 있는 추천용 테이블만 만들거나 바꾼다. 마지막이
본문 보존 스키마이고 기존 행의 길이를 2,000행씩 채운다(락·I/O는 ADR 0023 개정의 "마이그레이션의 락과 I/O"). 언론사
시드에 `정책브리핑`이 더해진다(인증키가 없으면 수집하지 않는다).

아래 명령은 Mac의 **메인 체크아웃**에서 돌린다(`data/`가 gitignore인 곳).

```bash
micro() { ssh -F .ops/micro/ssh_config micro "$@"; }   # 함수로 둔다: zsh(macOS 기본 셸)는 변수에 담은 명령을 단어로 나누지 않아 `MICRO="ssh ..."; $MICRO ...`가 "no such file or directory"로 실패한다
VMPSQL="cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter"
```

**배포**

0. 하기 전에 - 하나라도 아니면 하지 않는다.

   - **Tier 0 기사의 임베딩을 사용자가 정했는가.** VM은 임베딩을 하지 않는다(2026-10-16 실측 임베딩 0개). 이 배포
     뒤 첫 실행(2026-10-26 17:50 UTC)부터 VM 기사는 임베딩 없이 본문이 비워지고, 그 뒤로는 영영 임베딩할 수 없다
     (클러스터링·추천 후보에 들어가지 못한다, ADR 0023 개정의 "결과와 한계"). 그 전에 둘 중 하나를 사용자가 고른다:
     (a) 만료 전에 Tier 0 행을 다른 곳에서 임베딩한다 - 반출 + 원격 임베딩, 또는 Mac MPS(Mac에서 무거운 연산을
     돌리지 않는 방침과 함께 정한다), (b) 손실을 받아들인다. 고르지 않았으면 배포하지 않고 사용자에게 묻는다 - 배포하지 않으면 VM의 DB가 ADR 0023을 어기게 되므로
     10-26 02:36 UTC 전에 답을 받는다.
   - 아래 명령의 결과가 기대와 같다.

   ```bash
   git fetch origin && MERGE=$(git rev-parse origin/main) && git log -1 --oneline $MERGE   # 보존 잡 PR의 병합 커밋
   gh run list --repo khj1212k/pro-recsys-finalproject-recsys-01 --branch main --limit 1   # 그 커밋의 CI가 success
   micro "$VMPSQL -At -c \"SELECT job, started_at FROM job_runs WHERE status = 'running'\""   # 비어 있어야 한다(6.6)
   micro 'free -m; df -h / | tail -1'   # MemAvailable ≥ 300 MiB, 디스크 여유 ≥ 2 GB
   micro "grep '^BODY_RETENTION_DAYS=' ~/newsletter-recsys/.env || echo 없음"   # 없음 또는 =30 (다르면 잡이 실패한다)
   ```

1. 스케줄러를 멈춘다(마이그레이션 동안 잡이 쓰지 않게). ingest가 돌고 있으면 끝날 때까지 기다린 뒤에 한다.

   ```bash
   micro 'cd ~/newsletter-recsys && sudo docker compose stop scheduler'
   ```

2. 백업을 당겨 온다(6.5). 만료일을 파일 이름에 적는다 - 이 덤프에는 본문이 들어 있다.

   ```bash
   EXP=$(micro "$VMPSQL -At -c \"SELECT (min(n.raw_news_crawled_at) + interval '30 days')::date FROM news_raw n \
     JOIN press p USING (press_id) WHERE n.raw_news_content <> '' AND p.press_name <> '정책브리핑'\"")
   DUMP=data/backups/tier0-micro-$(date -u +%Y-%m-%dT%H%MZ).expires-$EXP.dump
   ( umask 077; micro 'cd ~/newsletter-recsys && sudo docker compose exec -T db pg_dump -U newsletter -d newsletter -Fc' > $DUMP )
   pg_restore -l $DUMP | grep -c "TABLE DATA"
   pg_restore -a -t news_raw -f - $DUMP | awk '/^COPY/{f=1;next} /^\\\./{f=0} f{n++} END{print n}'   # = 아래 VM 값
   micro "$VMPSQL -At -c 'SELECT count(*) FROM news_raw'"
   ```

3. 코드를 바꾼다. 이전 스냅숏은 `~/newsletter-recsys.prev`로 옆에 두고(되돌리기 R3), `.env`·`.ops`만 옮긴다.
   디렉터리 이름은 그대로라 compose 프로젝트(`name: newsletter-recsys`)와 `pgdata` 볼륨이 이어진다.

   ```bash
   git archive --format=tar.gz -o /tmp/tier0.tar.gz $MERGE
   scp -F .ops/micro/ssh_config /tmp/tier0.tar.gz micro: && rm /tmp/tier0.tar.gz
   micro "set -e; cd ~; rm -rf newsletter-recsys.prev; mv newsletter-recsys newsletter-recsys.prev; mkdir newsletter-recsys; \
     tar -xzf tier0.tar.gz -C newsletter-recsys; rm tier0.tar.gz; cp -a newsletter-recsys.prev/.env newsletter-recsys/.env; \
     if [ -d newsletter-recsys.prev/.ops ]; then cp -a newsletter-recsys.prev/.ops newsletter-recsys/; fi; \
     sed -i 's/^GIT_SHA=.*/GIT_SHA=$MERGE/' newsletter-recsys/.env; grep '^GIT_SHA=' newsletter-recsys/.env"
   ```

4. 수집 전용 이미지를 다시 빌드한다(1 GB 안에서). 스케줄러가 멈춰 있어 빌드가 쓸 메모리는 DB 몫을 뺀 나머지다
   (09-26 첫 빌드 121초. 10-17 재빌드 159초 - 스왑 사용 116 → 197 MiB, 스왑 입출력 최대 2/6 MB/s로 스래싱은 없었다).
   `migrate`·`worker`·`scheduler`는 같은 이미지 태그를 쓴다.

   ```bash
   micro 'free -m && cd ~/newsletter-recsys && time sudo docker compose build migrate && free -m'
   ```

5. 마이그레이션. 스케줄러를 켜기 전에 따로 돌려 결과를 본다.

   ```bash
   micro 'cd ~/newsletter-recsys && time sudo docker compose run --rm migrate'
   # 기대: "Running upgrade ... -> f1db3221e45f", "[migrate] f1db3221e45f: 본문 길이 N행 채움 (...배치...)", "[migrate] 완료"
   micro "$VMPSQL -At -c 'SELECT version_num FROM alembic_version'"   # f1db3221e45f
   # 길이 채움: 본문이 있는데 길이가 없는 행 0. 스케줄러가 멈춰 있는 지금 본다 - 켜진 뒤에는 RSS 수집기가 넣는
   # 추출 전 행(본문 '', 길이 없음)이 생기므로 조건을 본문 <> ''로 둔다.
   micro "$VMPSQL -At -c \"SELECT count(*) FROM news_raw WHERE raw_news_content <> '' AND raw_news_content_length IS NULL\""   # 0
   ```

6. 잡을 손으로 먼저 돌린다 - dry run 두 번(세기만), 그다음 실제 실행. 마지막 줄의 JSON이 `job_runs.stats`와 같다.

   ```bash
   micro 'cd ~/newsletter-recsys && sudo docker compose run --rm worker retention --dry-run'
   # 10-26 02:36 UTC 전이면 scanned 0, purgeable 0, oldest_remaining_body_crawled_at 2026-09-26T02:36:41...
   micro 'cd ~/newsletter-recsys && sudo docker compose run --rm worker retention --dry-run --days 7'
   # 읽기만 한다: 실제 규모에서 세는 쿼리가 얼마나 걸리는지(duration_s)와 7일 기준 대상 수(purgeable)를 본다.
   # 반드시 --dry-run과 함께 쓴다. 코드도 강제한다: 30일보다 짧은 --days에 --dry-run이 없으면 종료 코드 2로
   # 아무것도 하지 않는다(그대로 비우면 23일치 본문이 되돌릴 수 없게 사라진다).
   micro 'cd ~/newsletter-recsys && sudo docker compose run --rm worker retention'
   # 만료 전이면 purged 0. 만료 뒤에 배포했다면 여기서 첫 비움이 일어난다(remaining_expired_bodies 0이어야 한다)
   ```

7. 스케줄러를 다시 켠다. `up`은 `migrate`도 한 번 더 돌린다(아무것도 바꾸지 않는다).

   ```bash
   micro 'cd ~/newsletter-recsys && sudo docker compose up -d && sudo docker compose ps'
   micro 'cd ~/newsletter-recsys && sudo docker compose logs --tail 40 scheduler | grep -i "retention\|crontab"'
   ```

**확인**

```bash
# 배포 직후: 길이 채움(본문이 있는데 길이가 없는 행 - 수집기가 넣는 추출 전 빈 행은 세지 않는다), 손으로 돌린 실행
micro "$VMPSQL -c \"SELECT count(*) FILTER (WHERE raw_news_content <> '' AND raw_news_content_length IS NULL) AS no_length, \
  count(*) FILTER (WHERE raw_news_content_purged_at IS NOT NULL) AS purged FROM news_raw\""          # no_length 0
micro "$VMPSQL -c \"SELECT id, status, started_at, stats->>'purged' AS purged, stats->>'remaining_expired_bodies' AS remaining, \
  stats->>'oldest_remaining_body_crawled_at' AS oldest, stats->>'duration_s' AS s FROM job_runs WHERE job = 'retention' \
  ORDER BY id DESC LIMIT 5\""
# 다음 정시 ingest(:05)가 끝난 뒤: 성공, 새로 추출한 본문에 길이가 있다
micro "$VMPSQL -c \"SELECT id, status, finished_at - started_at FROM job_runs WHERE job = 'ingest' ORDER BY id DESC LIMIT 2\" \
  -c \"SELECT count(*) FROM news_raw WHERE raw_news_extracted_at > now() - interval '2 hours' AND raw_news_content_length IS NULL\""   # 0
micro 'free -m; sudo docker stats --no-stream'
# 2026-10-26 17:50 UTC 실행 뒤(10-27 02:50 KST 이후): 마지막 실행과 준수 확인(0이어야 한다)
micro "$VMPSQL -c \"SELECT status, stats->>'purged' AS purged, stats->>'purged_bodies' AS bodies, \
  stats->>'remaining_expired_bodies' AS remaining, stats->>'duration_s' AS s FROM job_runs WHERE job = 'retention' \
  ORDER BY id DESC LIMIT 1\" -c \"SELECT count(*) FROM news_raw n JOIN press p USING (press_id) WHERE n.raw_news_content <> '' \
  AND p.press_name <> '정책브리핑' AND n.raw_news_crawled_at <= (now() AT TIME ZONE 'UTC') - interval '30 days 26 hours'\""
```

일일 리포트(00:10 UTC)에 "본문 보존(30일): 기한 지난 본문 N건(하루 넘게 밀린 것 M건) ..." 줄이 생긴다. M이 0이 아니면
경고(Slack)가 간다 - 잡이 돌지 않았거나 실패한 것이다.

**되돌리기** - 비운 본문은 어느 경우에도 되살리지 않는다(덤프에 남은 본문을 다시 넣지 않는다).

- R1 잡만 끈다(코드·스키마 유지). 끈 동안 만료된 본문은 남으므로 ADR 0023을 어긴다 - 고친 뒤 바로 다시 켠다.

  ```bash
  micro "cd ~/newsletter-recsys && printf '\nSCHEDULER_JOBS_DISABLED=retention\n' >> .env && sudo docker compose up -d scheduler"
  # printf는 앞에 줄바꿈을 넣는다 - .env 끝에 줄바꿈이 없어도 마지막 변수 줄에 이어 붙지 않는다
  # 다시 켜기: .env에서 그 줄을 지우고 같은 up -d scheduler
  ```

- R2 마이그레이션이 중간에 실패했다(예: `lock_timeout`). DDL은 다시 돌려도 되게 쓰여 있고 길이 채우기는 빈 행만 채운다.
  막은 연결을 찾아 끝낸 뒤 5단계를 다시 한다. 스케줄러는 그때까지 멈춰 둔다.

  ```bash
  micro "$VMPSQL -c \"SELECT pid, state, now() - xact_start AS age, left(query, 80) FROM pg_stat_activity \
    WHERE datname = 'newsletter' AND pid <> pg_backend_pid()\""
  ```

- R3 새 코드에서 수집이 깨진다 - 이전 스냅숏으로. 09-26 코드는 `d48994e9d26e`까지만 알아서, 그 `migrate`는 더 새 리비전의
  DB에서 "Can't locate revision"으로 멈춘다. 그래서 **새 이미지로 먼저 내린다**. 내리면 비운 행은 ''가 되고(본문은 돌아오지
  않는다) 길이·비운 시각·유형 열과 비어 있는 추천 테이블이 사라진다. 이전 코드에는 보존 잡이 없으므로 짧게만 쓴다.

  ```bash
  micro 'cd ~/newsletter-recsys && sudo docker compose stop scheduler && \
    sudo docker compose run --rm --entrypoint sh migrate -c "cd /app/backend && alembic downgrade d48994e9d26e"'
  micro 'set -e; cd ~; mv newsletter-recsys newsletter-recsys.bad; mv newsletter-recsys.prev newsletter-recsys; \
    cd newsletter-recsys && sudo docker compose up -d --build'
  ```

- R4 DB가 망가졌다 - 2단계 덤프를 복원한다. 덤프는 `d48994e9d26e`이고 덤프 시점의 본문이 다시 들어 있으므로, 스케줄러를
  켜지 않은 채 복원 → `migrate`(head로) → `retention`(만료분을 비움) → `up -d` 순서로 한다. 6.2의 복원 명령은 빈 DB에
  `--clean`으로 덮어쓰고 끝에 `up -d`가 붙어 스케줄러까지 켜므로 여기서는 쓰지 않는다 - 덤프보다 새 리비전의 테이블이
  남은 DB에 덮어쓰면 `migrate`가 "already exists"로 멈춘다. 그래서 DB를 지우고 빈 DB에 복원한다.

  ```bash
  micro 'cd ~/newsletter-recsys && sudo docker compose stop scheduler && sudo docker compose up -d db'
  scp -F .ops/micro/ssh_config $DUMP micro:tier0.dump
  micro 'cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d postgres \
      -c "DROP DATABASE newsletter WITH (FORCE)" -c "CREATE DATABASE newsletter" \
    && sudo docker compose cp ~/tier0.dump db:/tmp/tier0.dump \
    && sudo docker compose exec -T db pg_restore -U newsletter -d newsletter --no-owner --exit-on-error /tmp/tier0.dump \
    && sudo docker compose exec -T db rm /tmp/tier0.dump && rm ~/tier0.dump'
  micro 'cd ~/newsletter-recsys && sudo docker compose run --rm migrate \
    && sudo docker compose run --rm worker retention && sudo docker compose up -d'
  ```

배포를 마치면 2단계 덤프는 이름의 만료일에 지운다(6.5). `~/newsletter-recsys.prev`는 다음 배포 때 지워진다.

**2026-10-17 배포 기록** (09-26 스냅숏 → `13801fc`. 시각은 10-16 UTC, 수치 전체는 [ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 증거 9)

- 0단계의 임베딩은 (b) 손실을 받아들인다로 정했다([ADR 0023](adr/0023-data-sources-copyright-retention.md) 개정의 "결과와 한계").
  나머지 사전 확인은 모두 기대와 같았다(MemAvailable 590 MiB, 디스크 여유 41 GB, `BODY_RETENTION_DAYS` 없음).
- 스케줄러는 19:27:04~19:34:54에 멈췄다(19:05 ingest가 45초에 끝난 뒤). 백업은
  `tier0-micro-2026-10-16T1927Z.expires-2026-10-26.dump`(43 MB, `news_raw` 30,754행 = VM).
- 빌드 159초, 마이그레이션 48초, head는 `f1db3221e45f` 하나, 길이 없는 본문 0. 5단계의 "본문 길이 N행 채움" 줄은 출력을
  남기지 않아 조회로 대신 확인했다(본문 있는 27,120행 모두 길이 있음).
- 6단계는 dry run 두 번만 했다(`retention --dry-run`: 대상 0, `retention --dry-run --days 7`: 본문 18,498개·7.5초).
  만료된 본문이 0이라 실제 실행은 하지 않고 스케줄(매일 17:50 UTC)에 맡겼다 - 처음 비우는 것은 10-26 17:50 UTC 실행이다.
- 배포 뒤 첫 ingest(20:05)는 동아일보 본문 실패 65%로 `failed`(6.6과 같은 양상, 74분), 둘째(22:05)는 `succeeded`(44분).
  새로 추출한 본문에는 모두 길이가 있다. AI타임스 새 행은 KST로 맞게 저장된다(6.7의 경계 `2026-10-16 20:05:01` UTC).
- 앞의 덤프 두 개는 6.5의 "최신 1개만" 대신 이미 정한 만료일(10-25·10-26)에 지운다.

### 4.2 RSS 재시도 잡 `rss_retry` (2026-10-25, [ADR 0026](adr/0026-hosting-tiers-and-contingency.md) "2026-10-25 갱신 2" · 2026-10-25 배포)

`python -m jobs.run rss_retry`가 매시 :40(popularity :35 뒤, `docker/crontab.micro`)에 **직전 ingest 실행이 읽지 못한 RSS 피드만** 다시 읽는다.
동아일보 RSS는 VM에서 정시 회차의 약 75%가 `RemoteDisconnected`로 실패하고, 피드가 50건이라 읽기에 성공한 회차 사이가 6시간을 넘으면 그 사이
기사를 잃는다. 한 번 더 읽는 것이 전부다 - 차단을 피하려는 변경(User-Agent·프록시·다른 주소)이 아니다.

| | |
|---|---|
| 하는 일 | 직전 ingest 실행의 `job_runs.stats.rss.per_feed`에서 `error`가 있던 피드(설정에 아직 있는 것)만 `collect_rss`로 다시 읽어 새 기사를 넣는다. 같은 호스트 건너뛰기 규칙도 그대로다 - 호스트의 첫 피드를 못 읽으면 같은 호스트의 나머지는 요청 없이 건너뛴다. |
| 하지 않는 일 | 본문 추출·임베딩·정책브리핑(다음 정시 ingest가 한다. `rss_summary` 피드인 동아일보는 수집 때 요약이 본문 칸에 들어간다). 실패한 피드가 없으면 요청 없이 바로 끝난다(`succeeded`, `retry_feeds`가 빈 목록). |
| 요청 수 | 피드 하나의 시간당 **읽기가 최대 1회에서 2회**로 는다(ingest :05 + 이 잡 :40). 읽기 1번은 연결 단계에서 막혀 bozo로 돌아오면 HTTP 요청 최대 3번(`MAX_FETCH_RETRIES`, 대기 1초·2초)이다. 관측한 동아일보 실패(`RemoteDisconnected` 157·`TimeoutError` 1)는 예외라 다시 보내지 않으므로 요청 1번이고, 이 잡이 더하는 요청은 하루 약 18번이다. 성공한 피드는 건드리지 않는다. |
| ingest와 겹침 | ingest의 advisory lock을 잡고 읽는다. ingest가 돌고 있으면 `skipped`(`stats.reason = ingest_running`)이고, 이 잡이 도는 동안은 정시 ingest가 시작하지 못한다(건너뛰어지면 `job_runs`에 `skipped`로 남는다). 이 잡의 길이는 RSS 단계의 일부다 - 2026-10-16 ~ 10-25 ingest 211회의 RSS 단계 전체가 중앙값 22초·최대 136초였다. |
| 다른 건너뛰는 경우 | `already_retried`(같은 ingest 실행의 피드는 한 번만 다시 읽는다 - ingest가 건너뛰어져 새 실행이 없을 때 되풀이하지 않는다), `no_ingest_rss_stats`(RSS 통계가 있는 ingest 실행이 아직 없다). |
| 알림 | 다시 읽어도 실패하면 잡은 `succeeded`이고 경고도 더하지 않는다 - 그 회차의 ingest가 이미 피드별 경고를 냈다. 요약 피드가 새 기사 5건 이상을 넣고도 요약을 하나도 못 저장하면 경고한다(ingest와 같은 규칙). 예외로 죽으면 잡 런타임의 실패 알림이 간다. |

**배포.** Alembic 변경이 없다. crontab은 이미지 안에 있으므로 4.1의 절차대로 이미지를 다시 빌드해 스케줄러를 올려야 새 줄이 돈다. 배포 뒤 첫 :40 이후:

```bash
micro "$VMPSQL -c \"SELECT id, status, to_char(started_at, 'MM-DD HH24:MI') AS started, stats->>'reason' AS reason, stats->>'source_run_id' AS source, \
  stats->'retry_feeds' AS retry, stats->'recovered' AS recovered, stats->'still_failed' AS still_failed, stats->'rss'->>'duration_s' AS rss_s \
  FROM job_runs WHERE job = 'rss_retry' ORDER BY id DESC LIMIT 24\""
```

기대: 동아일보가 실패한 회차 뒤에 `retry`가 `["동아일보"]`이고 `recovered` 또는 `still_failed`에 있다. 실패한 피드가 없던 회차는 `retry`가 `[]`다. `reason`이
`ingest_running`인 행이 이어지면 ingest가 :40을 넘기는 회차가 많은 것이다(6.6). `source`는 그 행이 근거한 `ingest`의 `job_runs.id`다.

효과는 아래로 본다 - **재시도가 실제로 구하는 비율**이다. 실패가 서로 독립이면 약 25%다(ADR의 시뮬레이션 가정). 5% 아래로 이어지면 독립 가정이 틀린 것(35분 사이의 실패가 같은 원인)이고
이 잡은 요청만 더한다:

```bash
micro "$VMPSQL -c \"SELECT count(*) AS retried, count(*) FILTER (WHERE stats->'recovered' ? '동아일보') AS recovered \
  FROM job_runs WHERE job = 'rss_retry' AND stats->'retry_feeds' ? '동아일보'\""
```

`daily_report`에는 `job rss_retry: skipped N, succeeded M` 줄과 정시 회차 줄의 `rss_retry 24/24`로 보인다.

**손으로 한 번 / 끄기.** 스케줄러가 떠 있는 상태에서 `micro 'cd ~/newsletter-recsys && sudo docker compose exec -T scheduler python -m jobs.run rss_retry'`
(`SCHEDULER_JOBS_DISABLED`에 들어 있으면 실행하지 않는다). 끄기는 4.1의 R1과 같다 - `.env`에 `SCHEDULER_JOBS_DISABLED=rss_retry`(이미 값이 있으면 쉼표로 이어 붙인다)를 넣고
`sudo docker compose up -d scheduler`. 끈 동안은 동아일보 RSS가 시간당 1회만 읽히던 때로 돌아간다.

## 5. 유휴 회수 점검

공식 기준(Always Free Resources 문서): 7일 동안 CPU p95 < 20%, 네트워크 사용률 < 20%,
메모리 사용률 < 20%(A1만)가 **모두** 참이면 회수될 수 있다. 문서는 집계 해상도·네트워크 분모·회수 방식
(정지인지 삭제인지)을 밝히지 않는다 - 그래서 최악(삭제)을 가정하고 데이터 손실이 없게 운영한다.

```bash
python3 scripts/oci/idle_check.py --instance-id $INSTANCE --compartment-id $TENANCY           # E2.1.Micro
python3 scripts/oci/idle_check.py --instance-id $A1_INSTANCE --compartment-id $TENANCY --memory  # A1
```

종료 코드 0 안전 / 2 기준+5%p 안쪽(위험) / 3 기준상 유휴 / 1 조회 실패. 출력의
`cpu_minutes_at_or_above_threshold_pct`가 5%를 넘어야 CPU p95가 20%를 넘는다(1분 해상도 가정).

2026-10-17부터 Tier 0는 같은 계산을 VM이 매일 스스로 한다(6.5의 cron, `tier0_backup.py idle-check`, 인스턴스 주체에 지표 읽기 권한만).
결과는 `job_runs`의 `idle_check` 행이고 `daily_report`의 "유휴 회수 위험" 줄이 판정과 마지막 백업 경과를 함께 적는다. 위 명령은 Mac에서
따로 볼 때 쓴다.

대응(ADR 0026 결정 6):
1. Tier 0는 2026-09-26 17:00 KST부터 유일한 수집기다. 회수(삭제일 수도 있음)되면 마지막 호스트 밖 백업 이후를
   잃는다 - 2026-10-17부터는 매일 자동 백업(6.5)이 그 범위를 하루치로 묶는다. 백업이 밀렸는데 종료 코드가 2나 3이면 6.5의 수동 실행을 한다. 2026-09-26 22:14 실측으로 이미 3(기준상 유휴)이다.
   2026-10-06 04:43의 7일 창도 3이다(CPU p95 7.89%, 네트워크 p95 0.076%). 그때까지 회수는 없었다 - 한 번의
   관측일 뿐 회수되지 않는다는 보장이 아니다.
2. 재생성은 6.2, 데이터는 마지막 백업에서 되살린다.
3. Tier 1(A1)은 BGE-M3를 상주시키는 임베딩 서비스가 메모리 20% 이상을 쓰면 "모두 참" 조건이 깨진다(실측 후 확정).
4. 인위적 CPU 부하(루프·lookbusy류)는 기본으로 쓰지 않는다. 쓰려면 ADR을 갱신한다.

## 6. 비상 절차

### 6.1 인스턴스가 STOPPED가 됨 (회수·유지보수)

```bash
oci compute instance get --instance-id $INSTANCE --query 'data."lifecycle-state"' --raw-output
oci compute instance action --action START --instance-id $INSTANCE
```

2026-10-25(무료 체험 종료)부터 개발 Mac의 감시 루프(`.ops/micro_watch.sh`, git 제외)가 15분마다 상태만 읽어 `STOPPED`면 위 START를 보낸다 - 맥이 꺼져 있으면 이 절을 손으로 한다(ADR 0026 2026-10-25 갱신). compose 서비스는 `restart: unless-stopped`라 부팅 후 스스로 올라온다. `job_runs`에 공백 시간대가 생기는데,
다음 ingest가 따라잡을 수 있는 공백은 **피드마다 다르다**(6.3).

재부팅에서는 확인됐다: 2026-10-03 03:40 KST에 unattended-upgrades가 커널 업데이트로 예약 재부팅을 했고,
저널 공백 25초 뒤 db·scheduler가 스스로 떠서 04:05 회차가 정시에 시작됐다(ADR 0026 증거 8). 회수로 인한
STOPPED에서 START로 되살리는 경로는 아직 겪어 보지 않았다. 메모리 표본 수집(`memsample.sh` → `~/mem_soak.csv`)은
서비스가 아니라서 재부팅하면 멈춘다 - 소크를 이어 가려면 다시 띄운다.

### 6.2 인스턴스가 사라짐 (TERMINATED)

2~4절을 다시 한다. 2026-09-26 실측: 생성→RUNNING 약 30초, 초기 설정 290초, 이미지 빌드 121초,
빈 DB 첫 ingest 155초 — 합계 약 10분. 지금은 Tier 0가 유일한 수집기이므로, 4절에서 스택을 올릴 때
스케줄러보다 먼저 마지막 백업(6.5, OCI Object Storage)을 되살린다. 순서:

1. 6.5 "설치·갱신"의 1~3을 새 VM에 한다. 동적 그룹의 일치 규칙은 새 VM의 OCID로 바꾼다
   (`oci iam dynamic-group update --dynamic-group-id <id> --matching-rule "instance.id = '<새 OCID>'"`).
2. `db`만 올린다(빈 DB). `migrate`·`scheduler`는 아직 올리지 않는다 — 스키마는 사본이 만든다.
3. 최신 `full/` 객체를 Mac의 디스크에 쓰지 않고 VM으로 흘려 보내 복원한다(본문이 있다 — ADR 0023 개정 2026-10-17 2). `full/`이 없으면
   (만든 지 하루 남짓에 지워진다, `full_enabled`를 켜지 않았으면 처음부터 없다) `nobody/daily/`의 최신을 쓰고 `--with-bodies`를 뺀다 —
   본문 없이 되살아나고 본문이 있던 행은 "비운 행"으로 표시된다. 받은 아카이브는 `drill/`에 둔다(ADR 0023 개정 2의 위치 표, 같은 명령
   끝에서 지운다) - 스크립트가 시작할 때 비우는 `work/`에 두면 복원 전에 지워진다.

```bash
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose up -d db'
NAME=$(oci os object list -ns $NS -bn newsletter-tier0-backups --prefix full/ --fields name,timeCreated \
  --query 'max_by(data, &"time-created").name' --raw-output)
oci os object get -ns $NS -bn newsletter-tier0-backups --name "$NAME" --file - | ssh -F .ops/micro/ssh_config micro \
  'sudo install -d -m 0700 /var/lib/newsletter-backup/drill && sudo tee /var/lib/newsletter-backup/drill/restore.tar > /dev/null'
ssh -F .ops/micro/ssh_config micro 'sudo env PATH=/opt/oci-cli/bin:$PATH python3 /opt/newsletter-backup/tier0_backup.py \
  --config /etc/newsletter-backup.conf restore --archive /var/lib/newsletter-backup/drill/restore.tar --target-db newsletter --with-bodies; \
  sudo rm -rf /var/lib/newsletter-backup/drill'
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose run --rm worker retention && sudo docker compose up -d'
```

`restore`는 대상 DB에 테이블이 하나라도 있으면 거절한다. 사본의 Alembic 리비전이 코드의 head보다 뒤면 `up -d`의 `migrate`가 올린다.
복원한 뒤에는 보존 잡을 다른 잡보다 먼저 돌린다(위 마지막 줄). 사본이 2026-10-17 06:33 UTC 앞의 것이면 AI타임스 보정(6.7)을 다시 한다.

백업 이후 공백은 RSS 창에 남아 있는 만큼만 첫 ingest가 되찾는다(6.3).

### 6.3 수집기가 멈췄을 때 되찾을 수 있는 범위

RSS는 피드마다 최근 N개만 보여 준다. 2026-09-26 Mac 정지(12:53~16:50 KST) 비교에서, 30~50개짜리 피드는
4시간 공백을 다음 ingest가 모두 따라잡았지만 **세계일보(20개, 시간당 11~14건)는 약 1.5시간이 한계**라 32건을
Mac이 영영 놓쳤다(Tier 0에만 있음, ADR 0026 증거 6). 즉 두 수집기를 겹쳐 돌리는 것만이 피드 창보다 긴 공백을 막는다.

한 DB의 공백을 다른 DB로 메울 때는 6.4의 병합 스크립트를 그대로 쓴다(원본·대상이 같은 스키마면 방향과 무관).

### 6.4 A1이 확보됐을 때 (Tier 1 승격 + 데이터 이전)

A1 재시도 루프(`.ops/oci_launch_retry.sh`)는 성공하면 `.ops/oci_instance.json`에 인스턴스 정보를 쓰고 끝난다.
2026-09-26 22:17 KST에 이 절의 데이터 단계(3~5)를 Tier 0의 일회용 DB에서 리허설했다: 복원 3초, 병합 2초,
기대 행 수 1,087 = 결과 1,087, 재실행 0행(ADR 0026 증거 7).

**2026-10-06 추가 — 병합 전에 6.7을 본다.** 수집기의 발행 시각 처리를 고쳤으므로(ADR 0008), 승격 뒤에도 도는
Tier 0는 고친 코드로 재배포돼 있어야 한다(아니면 A1과 Tier 0가 AI타임스 발행 시각을 서로 다르게 적는다).
기존 행을 보정하기로 했다면 Tier 0 DB와 복원한 Mac 덤프를 **각각 병합 전에** 보정한다 — 병합된 DB에서는
보정할 수 없다.

1. `scripts/oci/micro_bootstrap.sh`를 그대로 실행한다(아키텍처 무관). 12 GB에서도 스왑 2 GB는 모델 로드 피크 흡수용으로 둔다.
2. 코드 스냅숏을 보내고 `.env`는 `COMPOSE_FILE`을 기본값(`docker-compose.yml`만)으로 둔다 - 전체 worker 이미지를
   arm64로 직접 빌드한다. `sudo docker compose up -d db`로 **DB만** 올린다(스케줄러는 아직 끈다).
3. Mac 덤프를 복원한다(Alembic 리비전까지 덤프에 들어 있다 — Mac 덤프는 `d48994e9d26e`). 5단계 병합 SQL은
   본문 보존 열(`f1db3221e45f`, ADR 0023)을 쓰므로 **복원한 뒤 병합 전에** `sudo docker compose run --rm migrate`로
   head까지 올린다(동아일보 요약 배포, 6.9 뒤의 Tier 0는 상태 `rss_summary`·`blocked_origin` 행을 갖고 있어 **대상이 `b7e4a1c93d52`
   이상이어야** 한다 - 아니면 병합이 CheckViolation으로 되돌아간다). Tier 0도 4.1의 배포 뒤라면 `f1db3221e45f`다:

   ```bash
   scp -F <a1 ssh_config> data/backups/mac-compose-2026-09-26.dump a1:mac.dump
   ssh a1 'cd ~/newsletter-recsys && sudo docker compose cp ~/mac.dump db:/tmp/mac.dump && \
     sudo docker compose exec -T db pg_restore -U newsletter -d newsletter --no-owner --exit-on-error /tmp/mac.dump'
   # 기대: news_raw 899행, 임베딩 845개
   ```

   `migrate`가 이미 테이블을 만들었다면 `--clean --if-exists`를 붙이거나 DB를 새로 만든 뒤 복원한다.
4. Tier 0 `news_raw`를 CSV로 뽑아(본문 포함 — 저장소·Mac에 두지 말고 VM 사이로만 옮긴 뒤 지운다) A1로 보낸다:

   ```bash
   ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && umask 077 && sudo docker compose exec -T db \
     psql -U newsletter -d newsletter -v ON_ERROR_STOP=1 -c "COPY (SELECT p.press_name, n.raw_news_title, \
     n.raw_news_content, n.raw_news_url, n.raw_news_created_at, n.raw_news_crawled_at, n.raw_news_extract_status, \
     n.raw_news_extracted_at, n.raw_news_extract_attempts, n.raw_news_content_sha256, n.raw_news_content_length, \
     n.raw_news_content_purged_at, n.raw_news_kogl_type FROM news_raw n \
     JOIN press p USING (press_id) ORDER BY n.raw_news_id) TO STDOUT WITH (FORMAT csv, HEADER)" > ~/tier0_news_raw.csv'
   ```

   Tier 0에서 A1로 직접 `scp`할 수 없으면(키가 Mac에만 있음) `ssh micro 'cat ~/tier0_news_raw.csv' | ssh a1 'umask 077; cat > ~/tier0_news_raw.csv'`로
   Mac을 거쳐 파이프한다(Mac 디스크에는 남지 않는다).
5. A1에서 병합한다 — 단일 트랜잭션(`-1`)과 `ON_ERROR_STOP`이 필수다:

   ```bash
   ssh a1 'cd ~/newsletter-recsys && sudo docker compose cp scripts/oci/merge_tier0_news_raw.sql db:/tmp/ && \
     sudo docker compose exec -T db psql -U newsletter -d newsletter -v ON_ERROR_STOP=1 -1 \
       -f /tmp/merge_tier0_news_raw.sql < ~/tier0_news_raw.csv'
   ```

   출력의 `NOTICE: target_before=… tier0_only=… expected=… after=…`에서 `expected = after`인지 본다. 다르면
   스크립트가 스스로 되돌리고 오류로 끝난다. 같은 URL은 Mac 행을 남기고, 새 URL인데 본문 해시가 기존 ok 행과
   같으면 `duplicate`로 들어간다.

   스크립트가 병합 전에 멈추는 두 경우(둘 다 아무것도 넣지 않는다):
   - `press_name N 개가 대상 press 테이블에 없다` - Tier 0에만 있는 언론사가 있다(예: Tier 0를 정책브리핑 수집이
     들어간 코드로 재배포한 뒤). `sudo docker compose run --rm migrate`로 A1의 Alembic을 head까지 올리고 참조
     데이터를 시드한 다음 다시 병합한다.
   - `press_name N 개가 대상 press 테이블에 둘 이상 있다` - 대상에 같은 이름의 언론사가 두 번 있어 어느
     `press_id`에 붙일지 정할 수 없다. 중복 행을 사람이 정리한 뒤 다시 병합한다.

   이 스크립트의 동작(같은 URL 유지, 이름 매핑, `duplicate` 분기, 재실행 0행, 위 두 중단)은
   `tests/integration/test_merge_tier0_news_raw_sql.py`가 CI의 Postgres에서 같은 `psql` 명령으로 확인한다.
6. 스케줄러를 켜기 전에 보존 잡을 한 번 돌린다(`sudo docker compose run --rm worker retention`) - 덤프와 CSV에서 들어온
   본문 가운데 이미 만료된 것을 비운다(ADR 0023). 비운 행은 병합 SQL이 비운 채 옮긴다.
   그다음 A1 스케줄러를 켠다(`sudo docker compose up -d`). 첫 embed가 병합된 ok 행의 임베딩을 채운다
   (`SELECT count(*) FROM news_raw WHERE raw_news_extract_status='ok' AND embedding_result IS NULL`이 0으로 수렴).
   2026-10-06 기준 Tier 0의 ok 행은 12,823건이라 채울 임베딩이 약 1.3만 건이다. embed 잡은 회차마다 시간
   예산(`--time-budget-s 2400`)만큼만 돌므로 여러 회차에 걸친다 - A1 CPU 처리량은 재 본 적이 없다(ADR 0006).
7. A1의 첫 ingest가 끝난 뒤 4~5를 한 번 더 한다 — 그 사이 Tier 0만 받은 행이 옮겨지고, 이미 옮긴 행은 0행이다(멱등).
   끝나면 두 VM의 CSV를 지운다.
8. 7일 소크(잡 성공률 ≥95%, OOM 0, 수집 지연 p95 ≤3h) 동안 Tier 0 수집은 끄지 않는다(섀도로 되돌림).
   소크를 통과하면 A1을 기록 시스템으로 선언하고 ADR 0026을 갱신한다. 블록 볼륨 합계(A1 100 GB + Micro 50 GB
   = 150 GB)는 200 GB 안이다.

### 6.5 Tier 0 호스트 밖 백업 (VM cron → OCI Object Storage, 2026-10-17부터)

결정과 실측은 [ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 결정 6의 2026-10-17 결정과 증거 11, 본문 규칙은
[ADR 0023](adr/0023-data-sources-copyright-retention.md) 개정 2026-10-17 2. 아래 `$NS`는 Object Storage 네임스페이스
(`oci os ns get`), 버킷은 `newsletter-tier0-backups`다.

**구성.** VM 호스트(컨테이너 밖)의 cron이 매일 18:10 UTC에 root로 `scripts/oci/tier0_backup.py daily`를 부른다.

| 단계 | 하는 일 | `job_runs` |
|---|---|---|
| backup | 같은 스냅숏에서 본문 없는 사본(`nobody/daily/`, 35일 뒤 삭제 · 그달 첫 사본은 `nobody/monthly/`에도)과 최근 27일 본문 사본(`full/`, 1일 뒤 삭제 - 설정 `full_enabled`를 켰을 때만)을 만들어 인스턴스 주체로 올리고, 버킷 목록의 크기·MD5로 확인한다 | `backup` |
| idle-check | `idle_check.py`의 7일 계산(5절)을 VM에서 인스턴스 주체로 | `idle_check` |
| restore-check | 마지막 성공이 28일 넘었으면 방금 올린 본문 없는 사본을 일회용 DB `restore_check`에 복원해 대조하고 지운다 | `restore_check` |

VM의 자격은 이 버킷에 객체 만들기·목록 보기와 compute agent 지표 읽기뿐이다(내려받기·지우기·덮어쓰기 없음, 백업용 API 키·사전 인증 URL
없음 - 수집 스택의 `.env`는 따로 있다). 일하는 디렉터리
`/var/lib/newsletter-backup/work`는 실행이 끝나면 비고, 다음 실행도 시작할 때 비운다. 한 번에 약 2~3분, 최대 RSS 약 80 MB(증거 11).

**매일 확인.** `daily_report`(00:10 UTC)의 "호스트 밖 백업"·"유휴 회수 위험" 줄(마지막 성공 경과, 그 뒤 수집량, 실패 수, 복원 검증 경과,
회수 위험 수준). 경고 조건: 백업이 26시간 넘게 없다(리포트는 백업 약 6시간 뒤라 한 번 거르면 다음 리포트에서 약 30시간), 최근 24시간에
백업·복원 검증이 실패했다(1시간 넘게 `running`으로 남은 행 포함 - cron이 띄운 프로세스가 죽은 경우), 복원 검증이 35일 넘게 없다, `full/`
객체가 60시간 넘게 남아 있다(정상 수명은 최대 약 54시간 - 수명 주기 규칙이 돌지 않음. 지우기는 "내리기"의 bulk-delete를 Mac에서). 직접 볼 때:

```bash
ssh -F .ops/micro/ssh_config micro 'sudo tail -n 3 /var/log/newsletter-backup.log | cut -c1-400'
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter -c \
  \"SELECT id, job, status, started_at AT TIME ZONE 'UTC' AS started_utc, stats->>'duration_s' AS s, left(error, 120) AS error \
  FROM job_runs WHERE job IN ('backup','idle_check','restore_check') ORDER BY id DESC LIMIT 9\""
oci os object list -ns $NS -bn newsletter-tier0-backups --fields name,size,timeCreated \
  --query 'data[].{n:name,mb:size,t:"time-created"}' --output table     # Mac(관리자 자격)에서
```

**설치·갱신** (처음 한 번, 그리고 스크립트가 바뀐 커밋이 main에 들어갈 때마다. 2026-10-17 설치는 브랜치 커밋 `19cf228`(리베이스 전 `0efc2f5`) - 병합 뒤 main의 판으로 다시 깐다.
`19cf228`은 `full_enabled` 설정이 생기기 전 판이라 설정과 무관하게 매일 `full/`을 올린다)

1. OCI 쪽(Mac, 관리자 자격 — 한 번만): 버킷(Standard, `--public-access-type NoPublicAccess`, 버전 관리 끔), 동적 그룹
   `newsletter-micro-backup`(`instance.id = '<VM OCID>'`), 정책 `newsletter-tier0-backup`(ADR 0026 증거 11의 3문장), 수명 주기 규칙
   3개(`full/` 1일, `nobody/daily/` 35일, 멀티파트 중단 1일), 할당량 정책 `newsletter-object-storage-cap`(5 GiB). 정책은 Always Free 한도를
   다시 확인한 뒤에만 바꾼다.
2. VM: OCI CLI는 시스템 파이썬을 건드리지 않게 가상환경에 판을 고정해 둔다.

   ```bash
   ssh -F .ops/micro/ssh_config micro 'sudo apt-get install -y python3.12-venv && sudo python3 -m venv /opt/oci-cli \
     && sudo /opt/oci-cli/bin/pip install -q "oci-cli==3.94.0" && /opt/oci-cli/bin/oci --version'
   ```
3. 스크립트·설정·cron (메인 체크아웃에서, `<sha>`는 설치하는 커밋):

   ```bash
   scp -F .ops/micro/ssh_config scripts/oci/tier0_backup.py scripts/oci/idle_check.py micro:/tmp/
   sed "s/__GIT_SHA__/<sha>/" scripts/oci/tier0-backup.cron > /tmp/newsletter-tier0-backup
   scp -F .ops/micro/ssh_config /tmp/newsletter-tier0-backup micro:/tmp/ && rm /tmp/newsletter-tier0-backup
   ssh -F .ops/micro/ssh_config micro 'sudo install -d -m 0755 /opt/newsletter-backup && sudo install -d -m 0700 /var/lib/newsletter-backup \
     && sudo install -m 0644 /tmp/tier0_backup.py /tmp/idle_check.py /opt/newsletter-backup/ \
     && sudo install -m 0644 /tmp/newsletter-tier0-backup /etc/cron.d/newsletter-tier0-backup \
     && rm /tmp/tier0_backup.py /tmp/idle_check.py /tmp/newsletter-tier0-backup'
   ```

   `/etc/newsletter-backup.conf`(0600 root, 기계별 값 — 저장소에 넣지 않는다):
   `{"namespace": "<$NS>", "bucket": "newsletter-tier0-backups", "compose_dir": "/home/ubuntu/newsletter-recsys", "db_user": "newsletter", "db_name": "newsletter", "work_dir": "/var/lib/newsletter-backup"}`
   본문 사본(`full/`)은 여기에 `"full_enabled": true`(JSON 참)를 넣어야 만든다. [ADR 0023](adr/0023-data-sources-copyright-retention.md) 개정
   2026-10-17 2가 main에 병합(승인)된 뒤에만 넣는다 — 그 전에는 넣지 않고 본문 없는 사본만 올린다. `daily_report` 줄에는 "본문 사본 꺼짐"으로 나온다.
4. 확인: `sudo env PATH=/opt/oci-cli/bin:$PATH python3 /opt/newsletter-backup/tier0_backup.py --config /etc/newsletter-backup.conf backup --dry-run`
   (올리지 않고 크기·건수만) → 같은 명령의 `daily`를 한 번 손으로 → 위 "매일 확인"의 두 조회.

로그는 하루 몇 KB라 돌리지 않는다. 스크립트가 cron 로그·표준출력에 싣는 것은 객체 이름·건수·크기·해시·시각뿐이다.

**복원 검증.** 28일마다 `daily`가 스스로 한다(방금 올린 것과 MD5가 같은 로컬 사본으로). 버킷에서 내려받은 사본으로 하는 검증은 이렇게 한다
(2026-10-17에 한 번, 증거 11). 본문 없는 사본만 쓴다 — `full/`은 Mac에 받지 않는다:

```bash
D=$(mktemp -d); NAME=$(oci os object list -ns $NS -bn newsletter-tier0-backups --prefix nobody/daily/ --fields name,timeCreated \
  --query 'max_by(data, &"time-created").name' --raw-output)
oci os object get -ns $NS -bn newsletter-tier0-backups --name "$NAME" --file $D/drill.tar
WANT=$(oci os object head -ns $NS -bn newsletter-tier0-backups --name "$NAME" --query '"opc-meta-sha256"' --raw-output)
if [ -n "$WANT" ] && [ "$(shasum -a 256 $D/drill.tar | cut -d' ' -f1)" = "$WANT" ]; then
  echo sha256 일치
  scp -F .ops/micro/ssh_config $D/drill.tar micro:/tmp/ \
  && ssh -F .ops/micro/ssh_config micro 'sudo install -d -m 0700 /var/lib/newsletter-backup/drill && sudo mv /tmp/drill.tar /var/lib/newsletter-backup/drill/ \
    && sudo python3 /opt/newsletter-backup/tier0_backup.py --config /etc/newsletter-backup.conf restore-check \
         --archive /var/lib/newsletter-backup/drill/drill.tar --target-db restore_check_drill; sudo rm -rf /var/lib/newsletter-backup/drill /tmp/drill.tar'
else
  echo "sha256이 객체 메타데이터와 다르다 - 복원하지 않는다"
fi
rm -rf $D
```

받은 아카이브는 `work/`가 아니라 `drill/`에 둔다 - 스크립트는 시작할 때 `work/`를 비운다. `drill/`은 ADR 0023 개정 2026-10-17 2의 위치
표에 있는 자리이고, 같은 명령 줄 끝에서 지운다.

대조 항목: 테이블마다 행 수, Alembic 리비전, 제약·인덱스 수(NOT VALID 0), 본문 0행, `news_raw` 최근 수집 시각. 하나라도 다르면
종료 코드 1이고 `job_runs`의 `restore_check` 행에 이유가 남는다. 일회용 DB는 성공·실패와 무관하게 지운다.

**복구**(VM을 다시 만들었을 때)는 6.2를 본다. 2026-10-17 06:33 UTC 앞의 사본(2026-10 월별 사본 포함)은 AI타임스 발행 시각이 보정 전
값이다 — 복원한 뒤 6.7을 같은 경계로 다시 돌린다.

**내리기**(ADR 0023 개정 2026-10-17 2가 병합되지 않을 때, 또는 백업을 멈출 때): `sudo rm /etc/cron.d/newsletter-tier0-backup`, Mac에서
`oci os object bulk-delete -ns $NS -bn newsletter-tier0-backups --prefix full/ --force`(본문 사본부터). 본문 없는 사본과 버킷은 남겨도 된다.

**비용.** 사용량은 사본 하루 약 46 MB, 버킷 전체 약 0.3 GB(2026-10-17 크기 기준), API 요청 하루 약 7건 — Always Free(체험 중 Standard
10 GB, 그 뒤 세 계층 합 20 GB, 월 50,000건) 안이다. 할당량 5 GiB가 넘지 못하게 막는다(넘으면 올리기가 실패하고 daily_report가 경고한다).
`nobody/monthly/`는 지우지 않으므로 해마다 12개씩 는다 — `news_raw`가 커지는 만큼 함께 커진다(2026-10 3.8 MB). 1 GB에 가까워지면 다시 본다.

#### 옛 절차: Mac이 당겨 오는 전체 덤프 (2026-10-17 전, 수동)

2026-10-17 전에는 이것이 유일한 호스트 밖 사본이었다(09-26 22:18 · 10-17 04:27 두 번). 자동 백업이 실패하는데 급히 사본이 필요할 때만 쓴다.
**전체 덤프에는 본문이 있어** ADR 0023 개정 2026-10-06의 규칙(덤프 안 가장 오래된 상업 본문의 수집 + 30일에 파일을 통째로 지운다, 최신
1개만, 0600, 이름에 만료일)을 따른다. 보존 잡이 매일 도는 DB의 전체 덤프는 뜬 지 하루 안에 만료된다.

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -At -U newsletter -d newsletter \
  -c \"SELECT (min(n.raw_news_crawled_at) + interval '30 days')::date FROM news_raw n JOIN press p USING (press_id) \
  WHERE n.raw_news_content <> '' AND p.press_name <> '정책브리핑'\""          # 만료일
( umask 077; ssh -F .ops/micro/ssh_config micro \
    'cd ~/newsletter-recsys && sudo docker compose exec -T db pg_dump -U newsletter -d newsletter -Fc' \
    > data/backups/tier0-micro-$(date +%Y-%m-%dT%H%M).expires-<만료일>.dump )
pg_restore -l data/backups/tier0-micro-<시각>.dump | grep -c "TABLE DATA"     # 목차가 읽히는지
```

Mac에 남아 있는 옛 덤프 세 개의 만료일: `mac-compose-2026-09-26.dump` **2026-10-25**, `tier0-micro-2026-09-26T2218.dump` **2026-10-26**,
`tier0-micro-2026-10-16T1927Z.expires-2026-10-26.dump` **2026-10-26** — 그날 지운다(확인은 [runbook.md](runbook.md) 8절의 한 줄).
`mac-compose-…dump`(899행·임베딩 845개)는 기록 시스템의 절반인데 Mac에 한 부뿐이다. 본문 없는 부분(행·임베딩)을 남길지는 사용자 결정이고,
남기려면 그 덤프를 일회용 DB에 복원한 뒤 `restore-check`와 같은 방식의 본문 없는 사본으로 바꿔 둔다(아직 하지 않았다).

### 6.6 ingest가 1시간을 넘겨 다음 회차가 건너뛰어질 때

스케줄러(supercronic)는 같은 잡의 앞 실행이 끝나지 않았으면 다음 실행을 시작하지 않고, 건너뛴 회차는
`job_runs`에 행을 남기지 않는다. 그래서 실패 알림 없이 RSS 조회 간격이 2시간으로 벌어진다(2026-09-26 ~ 10-06에
233회 중 27회, ADR 0026 증거 8). 찾는 법:

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db \
  psql -U newsletter -d newsletter -c \"SELECT (started_at AT TIME ZONE 'Asia/Seoul')::date AS kst_day, count(*) AS runs, \
  count(*) FILTER (WHERE finished_at - started_at > interval '1 hour') AS over_1h, \
  count(*) FILTER (WHERE status <> 'succeeded') AS failed FROM job_runs WHERE job = 'ingest' GROUP BY 1 ORDER BY 1\""
```

하루 `runs`가 24보다 적으면 그만큼 건너뛴 것이다.

**원인(2026-10-17 진단, [ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 증거 10).** 동아일보 서버가 VM에서 온
기사 요청을 응답 없이 끊거나(연결·TLS는 되고 약 4초 뒤 빈 응답) 아무것도 보내지 않고 붙잡아 둔다. 같은 시각 Mac에서
보낸 같은 URL·같은 User-Agent 요청은 0.1~0.3초에 200이었다(20/20, VM은 0/20). HTTP 응답이 아예 없으므로
4xx/5xx·리다이렉트 문제가 아니고, 내려받은 페이지에서는 본문이 그대로 추출된다(최근 7일 동아일보 `empty` 0건) — 페이지 개편도 아니다. 긴 회차는 이 실패가 trafilatura 안의 숨은 재시도(호출 1번에 요청 3번 +
30초 대기)와 우리 재시도 3번에 곱해진 결과다 — 09-26 ~ 10-17 ingest 418회에서 추출 시간 ≈ 181초 + 실패 기사 1건당
120.5초(R² 0.96). VM의 IP 하나 때문인지, OCI 대역·일본 출발이라서인지는 가르지 못했다(VM이 한 대뿐).

**코드의 대응(브랜치 `fix/extraction-donga-failures`, ADR 0026 결정 9 — Tier 0에는 아직 배포하지 않았다 - 2026-10-18 `e9864cf`로 배포(범위 종결 C-29)).**

범위 종결 C-29 (2026-10-18): 완료(배포) - 2026-10-18 06:29~06:32 UTC에 4.1 절차로 main `e9864cf`(PR #37·#40)를 배포했고 6.5대로 백업 스크립트도 다시 깔았다. 배포 뒤 확인은 진행 중이다([목록](design/2026-10-18-scope-closure.md)).
- 기사 요청 1번은 연결 10초·읽기 15초·전체 30초 상한, 숨은 재시도 없음. 같은 실행 안에서는 2번까지(대기 2초).
- 같은 언론사 기사가 연달아 3건 사이트 단위 원인(연결·읽기 실패, 5xx, 403·429 등)으로 다운로드에 실패하면 그 언론사의 남은
  기사는 이번 실행에서 받지 않는다(DB에 쓰지 않으므로 시도 횟수를 쓰지 않고 다음 실행에서 다시 대상이 된다). 404·410 같은
  기사 단위 실패는 세지 않는다 — 앞서 실패한 죽은 URL이 회차 맨 앞에 와도 건강한 언론사의 새 기사를 미루지 않는다.
- crontab의 `--extract-deadline-s 2700`: ingest 시작 뒤 45분(:50)이 지나면 새 기사를 보내지 않고 남은 기사는 다음 회차로
  미룬다. 추출 시작이 아니라 ingest 시작부터 재므로 RSS 단계가 늦으면 그만큼 추출 시간이 준다(최소 60초).
- RSS 요청은 소켓 대기 1번(연결·읽기 각각)이 120초(`RSS_SOCKET_TIMEOUT_S`)를 넘으면 끊는다. 요청 전체 상한은 아니다 —
  VM에서 성공한 동아일보 RSS는 30~321초가 걸렸다.
- 한 언론사만의 실패는 경고(원인 개수·미룬 건수 포함)로 알리고 실행은 성공시킨다(규칙은 [runbook.md](runbook.md) 잡 상태 절).
  RSS 단계에서 읽지 못한 피드도 피드마다 경고한다(`동아일보: RSS 수집 실패 (RemoteDisconnected) - 이번 회차 신규 기사 0건`).
  10-16 22:05 ~ 10-17 05:05 UTC에는 동아일보 RSS가 8회 중 7회 실패했고, 그중 추출 대상에 동아일보가 없던 회차는 경고 없이
  끝났다 — 예전에는 RSS 실패가 추출 경고 뒤에 숨어 있었다. 이 경고가 매 회차 나면 동아일보 수집량은 0이다.
  그래서 배포 뒤의 "잡 성공률"은 규칙이 바뀐 만큼 저절로 오른다 — 수집 품질은 아래처럼 언론사별 실패·미룬 건수와 정시
  회차 수로 본다.

배포는 4.1 절차를 따른다. 배포 뒤 회차별 확인:

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db \
  psql -U newsletter -d newsletter -c \"SELECT id, status, to_char(started_at, 'MM-DD HH24:MI') AS started, \
  round(extract(epoch FROM finished_at - started_at)) AS took_s, stats->'rss'->>'failed_feeds' AS rss_failed, \
  stats->'extract'->>'fetch_failed' AS ff, stats->'extract'->>'deferred' AS deferred, \
  stats->'extract'->'press_breaker' AS breaker, stats->'extract'->'fetch_fail_causes' AS causes, \
  stats->'extract'->'fetch_ok_latency' AS ok_latency, stats->'extract'->>'time_budget_exhausted' AS budget_hit \
  FROM job_runs WHERE job = 'ingest' ORDER BY id DESC LIMIT 24\""
```

`breaker`가 매 회차 동아일보로 차 있으면 차단이 이어지는 것이다. 그동안 동아일보는 회차마다 가장 오래된 3~4건만 시험 삼아
받고(그 기사들이 시도 상한 3회를 채우면 다음 기사로 넘어간다) 나머지는 기다린다. 미룬 기사도 수집 후 30일(ADR 0023)이
지나면 대상에서 빠진다. `budget_hit`이 `true`인 회차가 이어지면 대상이 예산보다 많은 것이다 — 어느 언론사가 시간을 쓰는지
`causes`의 `read_timeout`·`deadline`(요청마다 15~30초)을 본다. `rss_failed`가 0보다 크면 `warnings`에 피드별 RSS 경고가 있다
(그 회차에 그 언론사의 신규 기사는 0건).

`ok_latency`는 다운로드에 성공한 요청의 시간 구간(`<5s`·`5-15s`·`>=15s`) 개수다. 어느 언론사의 `>=15s`가 늘면서 같은 언론사의
`read_timeout`·`deadline`도 늘면, 읽기 15초·요청 30초 상한이 느리지만 결국 성공하던 요청을 끊고 있을 수 있다 — 차단(빈 응답
`disconnect`, 0바이트 붙잡기)과 이것으로 가른다. 배포 전 기준값은 없다(예전 코드는 요청 시간을 남기지 않았다). 상한을 바꿀지는
그때 `ok` 건수와 함께 본다.

미시도 잔량(차단기가 미루기만 하고 처리량이 새 기사를 못 따라가면 쌓인다 — ADR 0026 증거 10 (f), 배포 전 동아일보 0건). 미룬
기사는 최종 `fetch_failed`로 세어지지 않으므로 실패 비율과 따로 본다:

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db \
  psql -U newsletter -d newsletter -c \"BEGIN READ ONLY; SELECT p.press_name, count(*) AS never_tried, \
  round(extract(epoch FROM (now() AT TIME ZONE 'UTC') - min(n.raw_news_crawled_at)) / 3600) AS oldest_h \
  FROM news_raw n JOIN press p USING (press_id) WHERE n.raw_news_extract_status IS NULL \
  AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days' GROUP BY 1 ORDER BY 2 DESC; COMMIT;\""
```

`never_tried`가 하루 넘게 늘거나 `oldest_h`가 몇 시간을 넘으면 동아일보가 처리량에 묶인 것이다 — 차단기 기준(`EXTRACT_PRESS_BREAKER_FAILURES`,
코드 기본값 3)을 다시 정할지는 그때 수집량(`ok`)과 함께 본다. 다른 선택지는 시도 순서다: 지금은 대상이 `ORDER BY raw_news_id`라
차단 중에는 회차마다 시험 요청 3~4건이 앞서 실패한 재시도 기사에 가고 새 기사가 밀린다. 언론사 안에서 시도한 적 없는 기사를
먼저 보내려면 `extractor.py`의 대상 조회를 `ORDER BY (N.raw_news_extract_status IS NOT NULL), N.raw_news_id`로 바꾼다(코드
변경·배포 필요, ADR 0026 결정 9 "시도 순서").

차단기 기준을 다시 따져 볼 때(ADR 0026 증거 10 f를 다시 낸다): 회차 번호·시각·상태·시도 횟수만 내려받는다(URL·제목·본문 없음).
`news_raw`에는 기사마다 마지막 시도만 남으므로, 마지막 시도 시각이 ingest 회차의 시작~끝 안에 드는 기사만 나온다:

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T \
  -e PGOPTIONS='-c default_transaction_read_only=on' db psql -U newsletter -d newsletter -A -F '|' -c \"\
  WITH runs AS (SELECT id, started_at, coalesce(finished_at, now()) AS finished_at FROM job_runs \
    WHERE job = 'ingest' AND started_at >= now() - interval '7 days') \
  SELECT r.id, n.raw_news_id, n.raw_news_extracted_at, n.raw_news_extract_status, n.raw_news_extract_attempts \
  FROM news_raw n JOIN press p USING (press_id) \
  JOIN runs r ON n.raw_news_extracted_at BETWEEN r.started_at AND r.finished_at \
  WHERE p.press_name = '동아일보' ORDER BY r.id, n.raw_news_extracted_at, n.raw_news_id\"" > donga_seq.psv
```

회차마다 이 순서로 처음부터 보내고, 다운로드 실패(`fetch_failed`)가 연달아 기준 수가 되면 그 실패까지는 보낸 것으로 치고 그
회차의 나머지를 미룬다(성공이 끼면 0부터). 보낸 쪽과 미룬 쪽의 다운로드 성공 비율, 실패 기사 중 요청이 간 몫, 회차당 보낸
건수를 센다. 이 파일은 저장소에 넣지 않고 다 쓰면 지운다.

원인을 다시 확인할 때(같은 시각 VM과 Mac에서 같은 URL을 받는다. 본문은 저장하지 않고 상태 코드·시간만 본다):

```bash
URL=<news_raw에서 고른 동아일보 fetch_failed 기사 URL>
FMT='%{http_code} connect=%{time_connect} ttfb=%{time_starttransfer} total=%{time_total} bytes=%{size_download}\n'
UA='trafilatura/2.2.0 (+https://github.com/adbar/trafilatura)'
ssh -F .ops/micro/ssh_config micro "curl -sS -o /dev/null -A '$UA' --max-time 45 -w '$FMT' '$URL'" &
curl -sS -o /dev/null -A "$UA" --max-time 45 -w "$FMT" "$URL"; wait
```

VM 쪽이 `Empty reply from server`(약 4초) 또는 0바이트 시간 초과이고 Mac 쪽이 200이면 증거 10과 같은 상태다.

값을 바꾸려면 `ai_workspace/config/settings.py`의 `EXTRACT_*` 기본값이나 crontab을 고쳐 다시 배포한다(compose는 이 환경변수를
컨테이너로 넘기지 않는다). 차단을 피하려고 User-Agent를 바꾸거나 프록시·다른 주소로 우회하지 않는다 — 증거 10에서 브라우저
User-Agent도 똑같이 막혔다. 동아일보를 어디서 얼마나 수집할지(지금처럼 VM에서 받을 수 있는 만큼 / 클라우드가 아닌 곳에서 동아일보만 /
Tier 0 피드에서 빼기)는 정하지 않았다(사용자 결정 대기).

범위 종결 C-31 (2026-10-18): 유보 - 소유자 결정이다. 결정 9를 배포한 뒤의 관측(미시도 잔량, 최종 `fetch_failed` 비율)이 판단 자료다.
범위 종결 C-31 (2026-10-25 갱신): 결정 · 배포 대기 - 위 "정하지 않았다"는 10-18 시점의 기록이다. 소유자가 위임한 상태에서 넷째 안(동아일보 RSS 요약을 본문 칸에 저장)을
택했다([ADR 0026](adr/0026-hosting-tiers-and-contingency.md) "2026-10-25 결정 — 동아일보는 RSS 요약으로 받는다", 6.9).

### 6.7 AI타임스 기존 행의 발행 시각 보정 (Tier 0는 2026-10-17에 했다)

**Tier 0 DB는 2026-10-17 06:33 UTC에 보정했다**(605행, [ADR 0008](adr/0008-db-layer-pgvector-schema-and-upsert.md) "2026-10-17 갱신").
SQL은 파일이다 — `scripts/oci/fix_aitimes_created_at_kst.sql`(보정)과 `scripts/oci/fix_aitimes_created_at_kst_revert.sql`(되돌리기),
CI의 PostgreSQL에서 `tests/integration/test_fix_aitimes_created_at_kst_sql.py`가 먼저 돌린다. 이 절은 그 기록이자, 보정 전 사본을 복원한
DB(6.2·6.4)에서 다시 할 때의 절차다.

배경: 2026-10-06 이전 수집기는 시간대 없는 발행 시각에 UTC를 붙였다. AI타임스 피드는 KST 벽시계 시각을
시간대 없이 내보내므로 `raw_news_created_at`이 9시간 뒤로 저장돼 있다(ADR 0008 "2026-10-06 갱신", ADR 0026
증거 3·8). 수집기를 고쳐도 이미 들어간 행은 바뀌지 않는다(`ON CONFLICT DO NOTHING`).

지켜야 할 것:

- **한 행은 한 번만 보정한다.** 두 번 빼면 18시간 밀린다.
- **보정은 DB마다 따로, 그 DB에 쓰는 수집기를 고친 코드로 바꾼 뒤에 한다.** "고치기 전 코드로 수집한 행"은
  그 DB에서 고친 코드로 돈 첫 ingest보다 먼저 수집된 행이다. 수집기가 옛 코드인 채로 보정하면 그 뒤에
  들어오는 행이 다시 9시간 밀린다.
- **병합된 DB에서는 이 SQL을 돌리지 않는다.** 6.4로 합친 DB에서는 어느 행이 어느 수집기에서 왔는지를 수집
  시각으로 가를 수 없다. 병합에 넣는 원본(Tier 0 DB, 복원한 Mac 덤프)을 각각 먼저 보정하고 나서 합친다.

Tier 0에서의 순서:

1. **고친 코드를 먼저 배포한다**(4절의 재배포 절차, 그 전에 6.5 백업). `.env`의 `GIT_SHA`도 새 커밋으로 고친다 —
   다음 단계가 이 값으로 경계를 찾는다.
2. 경계 시각을 정한다. 고친 코드로 돈 첫 ingest의 시작 시각(UTC)이다:

   ```sql
   SELECT git_sha, count(*) AS runs,
          min(started_at) AT TIME ZONE 'UTC' AS first_start_utc,
          max(started_at) AT TIME ZONE 'UTC' AS last_start_utc
   FROM job_runs
   WHERE job = 'ingest'
   GROUP BY git_sha
   ORDER BY first_start_utc;
   ```

   기대: 옛 스냅숏과 새 커밋, 두 줄. 새 커밋 줄의 `first_start_utc`가 경계다. 한 줄뿐이면 `GIT_SHA`를 고치지
   않고 재배포한 것이므로 이 표로는 가를 수 없다 — 재배포한 시각 뒤에 시작한 첫 ingest를 `job_runs`에서 직접
   찾아 그 `started_at`을 쓴다.
3. 사전 확인(읽기 전용). `raw_news_crawled_at`은 시간대 없는 컬럼에 UTC로 적혀 있어 `AT TIME ZONE 'UTC'`로
   맞춰 비교한다:

   ```sql
   SELECT p.press_name,
          count(*) AS total_rows,
          count(*) FILTER (WHERE n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC')) AS published_after_crawled,
          max(n.raw_news_created_at - (n.raw_news_crawled_at AT TIME ZONE 'UTC')) AS max_lead
   FROM news_raw n
   JOIN press p ON p.press_id = n.press_id
   GROUP BY p.press_name
   ORDER BY max_lead DESC NULLS LAST;
   ```

   기대: AI타임스의 `max_lead`만 9시간 안팎이다(2026-10-06 04:41 KST 조회에서 320행 중 268행, 최대 9.0시간).
   다른 언론사는 0 이하이거나 분 단위다(언론사 시계 오차·예약 발행). 다른 언론사의 `max_lead`가 1시간을
   넘으면 원인이 다르니 여기서 멈춘다.
4. 보정. 기본 실행은 리허설이다(한 트랜잭션, 끝이 `ROLLBACK`). 출력을 보고 기준을 만족하면 `-v apply=1`을 더해 한 번 더 돈다.
   스크립트가 기준을 스스로 확인하고 하나라도 어기면 예외로 멈춘다(psql 종료 코드 3, 아무것도 커밋되지 않음) — 보관 표
   `news_raw_created_at_fix_kst`가 이미 있음(이미 보정한 DB), 대상 0행(경계가 틀림), 보정 뒤에도 수집보다 10분 넘게 앞선 AI타임스 행,
   보정한 행의 최소 수집 지연 8시간 초과(두 번 뺀 흔적). 트랜잭션은 스크립트 안에서 끝나므로 열어 둔 채 기다리는 일이 없다.

   ```bash
   B='2026-10-16 20:05:01.826735'   # 2단계의 경계(UTC)
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
     -v boundary='$B' -U newsletter -d newsletter -f -" < scripts/oci/fix_aitimes_created_at_kst.sql          # 리허설
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
     -v boundary='$B' -v apply=1 -U newsletter -d newsletter -f -" < scripts/oci/fix_aitimes_created_at_kst.sql  # 적용
   ```

   읽을 것: `NOTICE: fix_kst fixed=<행 수> min_lag=<분 단위> max_lag=<며칠 안> ahead_after_fix=0`, 끝 줄 `ROLLBACK`/`COMMIT`.
   2026-10-17 Tier 0: `fixed=605 min_lag=00:01:08 max_lag=2 days 22:21:59 ahead_after_fix=0`(리허설과 적용이 같은 수치).

   되돌리기(보관 표까지 지운다 - 그래야 범위를 바꿔 다시 보정할 수 있다. 보정 뒤 발행 시각이 다른 이유로 바뀐 행이 있으면 멈춘다):

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
     [-v apply=1] -U newsletter -d newsletter -f -" < scripts/oci/fix_aitimes_created_at_kst_revert.sql
   ```

   되돌리지 않는 한 보관 표는 "이 DB는 보정했다"는 표시이므로 지우지 않는다.
5. 확인(읽기 전용): 3단계 조회를 다시 돌려 AI타임스의 `published_after_crawled`가 0인지 본다. 2026-10-17: AI타임스 621행 중 0행,
   최대 앞섬 −1분 8초, 다른 언론사도 0행.

Mac 덤프(`data/backups/mac-compose-2026-09-26.dump`)의 AI타임스 행도 같은 상태다(5.0~8.9시간 앞섬). 덤프 파일은
고치지 않는다. 6.4에서 덤프를 복원하면 **Tier 0 행을 병합하기 전에** 복원한 DB에서 4단계를 실행한다 — 덤프의
행은 모두 고치기 전 코드로 수집했으므로 경계를 모든 행보다 뒤(`-v boundary='2100-01-01'`)로 둔다. 그때
병합에 넣는 Tier 0 쪽도 이미 보정돼 있어야 한다(위 "지켜야 할 것"). 보정 전에 받아 둔 백업을 복원하면 그 행은
다시 보정 전 값이다.

정한 것(2026-10-17, ADR 0008 "2026-10-17 갱신"):

- **범위: 넓은 안** — 경계 앞 AI타임스 행 전부(605행). 좁은 안(발행 > 수집으로 드러난 552행만)은 나머지 53행을 9시간 밀린 채 남겨
  한 언론사 안에서 기준이 갈린다. 사후 확인: 그 53행 중 47행이 09-26 첫 ingest의 피드 백로그이고 보정 뒤 수집 지연이 9.5시간 ~ 2.9일 —
  9시간 밀려도 수집 시각을 넘지 않던 행이라는 추정과 맞는다.
- **시점: 재배포(1단계) 약 11시간 뒤, 호스트 밖 백업 직후.** 보정 전 값은 보관 표와 06:29 UTC의 백업 사본에 남아 있다.


### 6.8 국민일보 본문 추출 붕괴 복구 (2026-10-19 ~, 2026-10-25 적용)

**2026-10-25 적용했다**: `2b74c25` 배포(03:28 UTC) 뒤 첫 ingest(04:05:01 UTC = until)를 확인하고 04:08 UTC에 1,195행(duplicate 707·ok 488)을 되돌렸다. 05:05 UTC ingest가 다시 받아 ok 1,083(중앙값 1,082자)·dropped 71·duplicate 40·fetch_failed 1이 됐다. 아래는 그 절차의 기록이다.

**Tier 0는 2026-10-25 현재 옛 추출기(`e9864cf`)로 돌고 있어 국민일보 본문은 특집 카드 글이다.** 원인·증거는
[ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 결정 9 "2026-10-25 추가"와 증거 12다. 이 절은 고친 추출기를 올리고 영향 행 1,190건(2026-10-25 02:56 UTC 조회)을
다시 받게 하는 절차다. SQL은 파일이다 - `scripts/oci/fix_kmib_body_extraction.sql`(복구)과 `scripts/oci/fix_kmib_body_extraction_revert.sql`(되돌리기),
CI의 PostgreSQL에서 `tests/integration/test_fix_kmib_body_extraction_sql.py`가 먼저 돌린다.

지켜야 할 것:

- **고친 추출기를 먼저 배포한다.** 옛 추출기인 채로 SQL을 돌리면 되돌린 행을 다음 ingest가 같은 특집 글로 다시 채운다. 스크립트가 `job_runs`로 이를 확인한다 - `until` 뒤에 고친 추출기
  (`stats.extract.body_container` 키)로 돈 ingest가 없거나 옛 추출기로 돈 ingest가 있거나 `until`이 지금보다 뒤이면 리허설도 멈춘다.
- **`until`은 고친 추출기의 첫 ingest `started_at`이다.** 그 뒤에 추출된 행은 고친 추출기가 받은 것이라 건드리지 않는다. `boundary`는 붕괴 시작 직전이다 - 마지막 정상 추출(10-19 03:06:10 UTC)과
  첫 이상 추출(05:06:54 UTC) 사이면 된다: `2026-10-19 04:00:00+00`.
- **본문은 보관 표에 복사하지 않는다**(ADR 0023). 되돌리기는 본문이 원래 없던 행(duplicate·dropped·empty)만 원상으로 돌린다. 원래 ok였던 행은 미시도로 남아 원본 페이지에서 다시 받는다.
- 수집한 지 30일이 넘었거나 본문을 이미 비운 행은 건드리지 않는다. 지금은 해당 행이 없다(가장 이른 만료 11-18). **11-18 이후에 하면 일부 행이 대상에서 빠진다.**
- 임베딩이 있거나 뉴스레터에 묶인 대상 행이 있으면 스크립트가 멈춘다(지금은 0건). 그런 행은 이 스크립트가 다루지 않는다.

순서:

1. **백업과 배포.** 6.5로 호스트 밖 백업을 뜨고, 4절의 재배포 절차로 이 브랜치가 병합된 main을 올린다. `.env`의 `GIT_SHA`도 새 커밋으로 고친다.
2. **고친 추출기의 첫 ingest 확인(읽기 전용).**

   ```sql
   -- 새 커밋의 첫 ingest 시작 시각(UTC) = until
   SELECT git_sha, count(*) AS runs, min(started_at) AT TIME ZONE 'UTC' AS first_start_utc
   FROM job_runs WHERE job = 'ingest' GROUP BY git_sha ORDER BY first_start_utc;

   -- 그 실행들의 국민일보 추출 경로: container만 있고 container_missing이 없어야 한다
   SELECT started_at AT TIME ZONE 'UTC' AS start_utc,
          stats->'extract'->'body_container' AS body_container,
          stats->'extract'->'per_press'->'국민일보' AS kmib,
          stats->'extract'->'suspect_press' AS suspect_press
   FROM job_runs WHERE job = 'ingest' AND git_sha = '<새 커밋>' ORDER BY started_at DESC LIMIT 5;

   -- 새 행의 본문 길이: 중앙값이 1,000자 안팎(개편 전 중앙값 1,045)이고 duplicate가 적어야 한다
   SELECT n.raw_news_extract_status, count(*),
          percentile_cont(0.5) WITHIN GROUP (ORDER BY n.raw_news_content_length)::int AS median_chars
   FROM news_raw n JOIN press p ON p.press_id = n.press_id
   WHERE p.press_name = '국민일보' AND n.raw_news_extracted_at >= '<until>'
   GROUP BY 1 ORDER BY 1;
   ```

   기대: `body_container`에 `{"국민일보": {"container": N}}`만 있다. `container_missing`이 있거나 중앙값이 수천 자 이상이면 여기서 멈춘다 - 고친 추출기가 새 페이지에 맞지 않는 것이다.
   주말에는 국민일보 새 행이 적다(하루 20~40건) - 새 행이 5건 이상 쌓인 뒤에 본다.
3. **사전 확인(읽기 전용).** 스크립트와 같은 조건의 영향 행 수다(2026-10-25 02:56 UTC: duplicate 703, ok 487 = 1,190. 원본이 개편 전인 duplicate 1건은 뺀다):

   ```sql
   SELECT n.raw_news_extract_status, count(*), count(DISTINCT n.raw_news_content_sha256) AS distinct_hashes,
          count(*) FILTER (WHERE n.embedding_result IS NOT NULL OR n.news_letter_id IS NOT NULL) AS linked
   FROM news_raw n JOIN press p ON p.press_id = n.press_id
   WHERE p.press_name = '국민일보' AND n.raw_news_extract_status IN ('ok', 'duplicate', 'dropped', 'empty')
     AND n.raw_news_extracted_at >= '2026-10-19 04:00:00+00' AND n.raw_news_extracted_at < '<until>'
     AND n.raw_news_content_purged_at IS NULL AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days'
     AND NOT (n.raw_news_extract_status = 'duplicate' AND EXISTS (SELECT 1 FROM news_raw o WHERE o.raw_news_extract_status = 'ok'
              AND o.raw_news_content_sha256 = n.raw_news_content_sha256 AND o.raw_news_extracted_at < '2026-10-19 04:00:00+00'))
   GROUP BY 1 ORDER BY 1;
   ```

   기대: `linked`가 모두 0, 행 수가 위 숫자에서 크게 벗어나지 않는다(그 뒤로 옛 추출기가 돈 시간만큼 늘었다).
4. **복구.** 기본 실행은 리허설이다(한 트랜잭션, 끝이 `ROLLBACK`). 출력을 보고 기준을 만족하면 `-v apply=1`을 더해 한 번 더 돈다. 스크립트가 기준을 스스로 확인하고 하나라도 어기면 예외로 멈춘다
   (psql 종료 코드 3, 아무것도 커밋되지 않음) - 보관 표 `news_raw_kmib_extract_fix`가 이미 있음(이미 복구한 DB), 대상 0행(`boundary`·`until` 확인), 대상이 `max_rows`(기본 3000)를 넘음,
   임베딩·뉴스레터에 묶인 행이 있음, 고친 추출기를 배포하지 않았거나 `until`이 틀림(위 "지켜야 할 것").

   ```bash
   B='2026-10-19 04:00:00+00'; U='<2단계의 until, UTC>'
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
     -v boundary='$B' -v until='$U' -U newsletter -d newsletter -f -" < scripts/oci/fix_kmib_body_extraction.sql          # 리허설
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
     -v boundary='$B' -v until='$U' -v apply=1 -U newsletter -d newsletter -f -" < scripts/oci/fix_kmib_body_extraction.sql  # 적용
   ```

   읽을 것: `NOTICE: fix_kmib reset=<행 수> ok=<n> duplicate=<n> dropped=<n> empty=<n>`, 요약 표(이전 상태별 행 수·서로 다른 해시 수·수집 시각 범위), 끝 줄 `ROLLBACK`/`COMMIT`.
   리허설과 적용은 같은 수치여야 한다(그 사이 옛 추출기가 새 행을 쓰지 않았다면).
5. **다음 ingest 뒤 확인(읽기 전용).** 되돌린 행은 `ORDER BY raw_news_id`로 오래된 것부터 다시 받는다. 1,190건은 약 15분이라 한두 회차에 끝난다(회차 마감 45분).

   ```sql
   SELECT n.raw_news_extract_status, count(*),
          percentile_cont(0.5) WITHIN GROUP (ORDER BY n.raw_news_content_length)::int AS median_chars
   FROM news_raw n JOIN press p ON p.press_id = n.press_id
   WHERE p.press_name = '국민일보' AND n.raw_news_crawled_at >= '2026-10-19 05:00:00'
   GROUP BY 1 ORDER BY 1;
   ```

   기대: `NULL`(미시도) 행이 0으로 줄고, ok의 중앙값이 1,000자 안팎이고, duplicate는 구 URL·신 URL로 이중 수집된 기사(최대 47건쯤) 정도다. 미시도가 줄지 않으면 6.6의 확인 쿼리로
   회차가 건너뛰어지는지 본다. `daily_report`의 "같은 본문 반복" 줄이 사라지는 것은 24시간 창이 지난 뒤다.

되돌리기(복구 SQL이 만든 보관 표까지 지운다 - 그래야 범위를 바꿔 다시 복구할 수 있다. 이미 다시 받은 행은 덮어쓰지 않는다 - 돌려놓을 행의 조건을 UPDATE가 행의 현재 값으로 판정하므로 ingest가 도는 중에 돌려도 된다):

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
  [-v apply=1] -U newsletter -d newsletter -f -" < scripts/oci/fix_kmib_body_extraction_revert.sql
```

읽을 것: `NOTICE: fix_kmib_revert restored=<n> left_unattempted_ok=<n> already_reextracted=<n> missing_from_news_raw=<n>`. 되돌리기가 필요한 경우는 드물다 - 복구가 미시도로 둔 행은 되돌리지 않아도
다음 ingest가 원본 페이지에서 다시 받는다. 복구 직후 다시 받기 전에 멈추고 싶을 때, 또는 범위를 바꿔 다시 복구하려고 보관 표를 치울 때 쓴다. 되돌리기는 옛 특집 글을 다시 만들지 않는다.

되돌리기는 완전한 왕복이 아니다:

- **돌려놓은 duplicate는 본문을 받지 못한다.** 그 해시의 원본(ok)은 미시도로 남았거나(`left_unattempted_ok`) 다시 받아 새 해시가 됐으므로 짝 없는 duplicate가 되고, 추출기는 duplicate를 다시 받지
  않는다(재시도 대상은 `fetch_failed`·`error`뿐, `config/retention.py`). 되돌린 뒤 본문을 받으려면 범위를 고쳐 복구 SQL을 다시 돌린다 - 보관 표를 지웠으므로 돈다.
- 원래 ok였던 행은 본문을 보관하지 않아 되돌리지 못하고 미시도로 남는다. 보관 표와 함께 그 행의 이전 해시·길이 기록도 사라진다.

복구를 해도 **이중 수집된 같은 기사는 합쳐지지 않는다**(ADR 0026 결정 9 "하지 않은 것"). 국민일보 `raw_news_created_at`의 미래 시각(ADR 0026 증거 12 g)은 이 절이 고치지 않는다 - 6.10이 기사 페이지 값으로 고친다.

### 6.9 동아일보 RSS 요약 전환과 추출 대기 행 닫기 (2026-10-25, ADR 0026 C-31 · 2026-10-25 배포)

동아일보는 VM 출발지의 본문 요청을 거절한다. 이 변경이 배포된 뒤 수집기는 동아일보 RSS 요약을 본문 칸에 넣고 상태를 `rss_summary`로
닫는다(요약이 350자 미만이거나 없으면 `dropped`, 같은 요약이 이미 있으면 `duplicate` - 본문 ''). 동아일보 서버에는 본문 요청을 보내지 않는다.
배포 전 행 중 아직 추출 대기인 것은 한 번 닫는다(되돌릴 수 있다). 결정과 한계는 [ADR 0026](adr/0026-hosting-tiers-and-contingency.md)의
"2026-10-25 결정 - 동아일보"다. **2026-10-25에 배포·적용했다**: `03b174c`(마이그레이션 15초), 05:09 UTC 대기 행 984 닫음(ADR 0026 "2026-10-25 갱신(배포·복구 기록)").

**이 배포에는 Alembic 변경이 있다**: `f1db3221e45f` → `b7e4a1c93d52`(`ck_news_raw_extract_status`에 `rss_summary`·`blocked_origin`을 더하고, 같은 요약 조회용 부분 인덱스
`ix_news_raw_content_sha256_rss_summary`를 만든다. 인덱스를 만드는 동안(힙을 한 번 읽는다, 54 MB에서 1초 안팎으로 추정 - 만들어 보지는 않았다) 쓰기가 기다리고 그 락이 커밋까지 이어지므로
**스케줄러를 멈춘 상태에서 돌린다**(아래 2단계). 제약을 바꾸는 동안은 테이블 락이 잡혀 수 밀리초이고, 그 뒤 검증이 힙을 한 번 읽는다 - 읽기·쓰기를 막지 않는다). **새 이미지로 첫 ingest가 돌기 전에 `migrate`를 한다.** 제약이 그 두 상태를 모르는 채 새 수집기가 돌면 동아일보 INSERT가
`CheckViolation`으로 피드째 실패해(`per_feed`의 `error`) 동아일보가 매시 0건이 되고, close SQL도 같은 이유로 멈춘다. `docker compose up`은 scheduler 앞에
`migrate`를 돌리지만(compose `depends_on`), `run --rm worker ingest`처럼 손으로 띄우는 실행은 그렇지 않으므로 아래 2단계에서 따로 돌려 확인한다.

**배포** (4.1의 절차를 쓴다. 이 변경에 맞춰 달라지는 곳만 적는다)

1. 사전 확인은 4.1의 0단계와 같다(CI success, 도는 잡 없음, 메모리·디스크). 하나 더 - close SQL에 넘길 시도 횟수 상한을 **컨테이너 안의 실효값**으로
   읽어 둔다. VM의 `.env`를 보지 않는다: compose는 `MAX_EXTRACT_ATTEMPTS`를 컨테이너로 넘기지 않아(`AI_ENV_FILE`로 넘기는 경우만 예외) `.env`에
   무엇을 적어도 추출기와 마이그레이션은 설정 기본값을 쓴다(2026-10-25 VM: `.env`에 이 키가 없고 `fetch_failed` 행의 시도 횟수 최댓값이 3).
   VM에도 `worker`가 있다 - `profiles: ["tools"]`라 `docker compose config --services`에는 안 보이고 `--profile tools`를 붙이면 보인다(2026-10-25 VM 확인:
   기본 `db migrate scheduler`, `--profile tools`는 `worker`까지). `docker compose run`은 프로필이 꺼진 서비스도 띄우므로 이 런북의 `run --rm worker ...` 명령은 그대로 된다.
   같은 이미지인 `scheduler`로 읽어도 같다(2026-10-25에 이 명령으로 3을 읽었다):
   `micro 'cd ~/newsletter-recsys && sudo docker compose run --rm --no-deps --entrypoint python scheduler -c "from config.settings import Settings; print(Settings.MAX_EXTRACT_ATTEMPTS)"'` (기본 3).
2. 스케줄러를 멈추고(4.1의 1단계) 백업을 당긴 뒤(2단계) 코드를 바꾸고 이미지를 다시 빌드하고(3·4단계), **마이그레이션을 돌린다**:

   ```bash
   micro 'cd ~/newsletter-recsys && time sudo docker compose run --rm migrate'
   # 기대: "Running upgrade f1db3221e45f -> b7e4a1c93d52", "[migrate] 완료"
   micro "$VMPSQL -At -c 'SELECT version_num FROM alembic_version'"   # b7e4a1c93d52
   micro "$VMPSQL -At -c \"SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint WHERE conname = 'ck_news_raw_extract_status'\""
   # 기대: 목록에 'rss_summary'와 'blocked_origin'이 있고 convalidated = t
   micro "$VMPSQL -At -c \"SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_news_raw_content_sha256_rss_summary'\""
   # 기대: ... (raw_news_content_sha256) WHERE ((raw_news_extract_status)::text = 'rss_summary'::text)
   ```

   그다음 스케줄러를 켠다(4.1의 7단계).
3. 첫 정시 ingest가 끝난 뒤 동아일보 새 행의 상태를 본다. `raw_news_crawled_at`은 시간대 없는 UTC 값이라 기준도 UTC로 만든다:

   ```bash
   micro "$VMPSQL -c \"SELECT n.raw_news_extract_status, count(*) FROM news_raw n JOIN press p USING (press_id) \
     WHERE p.press_name = '동아일보' AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '2 hours' GROUP BY 1 ORDER BY 2 DESC\""
   micro "$VMPSQL -c \"SELECT id, status, stats->'rss'->'per_feed'->'동아일보' AS donga FROM job_runs WHERE job = 'ingest' ORDER BY id DESC LIMIT 3\""
   ```

   기대: `rss_summary`가 대부분이고 `dropped`·`duplicate`가 조금(상태 NULL은 없다). `donga`의 `summary`에 stored·dropped·duplicate가 있다. 동아일보 RSS 읽기는 VM에서
   75% 실패하므로(ADR 0026 한계) 한 회차의 `error`가 `RemoteDisconnected`이고 신규 0건인 것은 이 배포의 결함이 아니다 - 읽기에 성공한 회차를 본다.
   `error`에 `CheckViolation`이 보이면 2단계의 마이그레이션이 적용되지 않은 것이다.

**추출 대기 행 닫기**

4. 리허설. `max_attempts`는 위 1단계에서 읽은 컨테이너의 `MAX_EXTRACT_ATTEMPTS`와 **같아야 한다**(기본 3). 다르면 "추출 대기"의 뜻이 추출기와 어긋나 닫을
   행을 놓치거나 이미 끝난 행까지 닫는다:

   ```bash
   micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter -v ON_ERROR_STOP=1 -v max_attempts=3 -f -" < scripts/oci/close_donga_pending.sql
   # NOTICE close_donga closed=N, 끝이 ROLLBACK
   ```

   `ingest가 돌고 있다`로 멈추면(job_runs에 `running`인 ingest가 있다) 그 회차가 끝난 뒤 다시 한다. 프로세스가 죽어 남은 `running` 행이면 다음 ingest가 `abandoned`로 바꾼다.
5. 적용: 같은 명령에 `-v apply=1`. 스크립트가 `job_runs`를 잠가 새 실행이 시작하지 못하게 하지만, 정시 ingest(:05)가 길어지는 날이 있으므로 :50~:59에 하면 겹침이 가장 적다.
6. **다음 ingest가 끝난 뒤 대기 행이 0인지 다시 센다.** 닫은 직후 0이어도, 닫기 전 코드(이전 이미지)로 돈 수집이 넣은 행이나 그 사이 들어온 행이 남을 수 있다:

   ```bash
   micro "$VMPSQL -At -c \"SELECT count(*) FROM news_raw n JOIN press p USING (press_id) WHERE p.press_name = '동아일보' \
     AND COALESCE(n.raw_news_content, '') = '' AND n.raw_news_content_purged_at IS NULL AND (n.raw_news_extract_status IS NULL \
     OR (n.raw_news_extract_status IN ('fetch_failed', 'error') AND n.raw_news_extract_attempts < 3))\""   # 3 = 위 max_attempts. 0이어야 한다
   ```

   0이 아니면 새 이미지가 도는지(`docker compose ps`, `.env`의 `GIT_SHA`)를 보고, 이미 닫은 DB라 close SQL을 다시 돌릴 수 없으므로(보관 표가 있으면 멈춘다) 7단계로 되돌린 뒤 다시 닫는다.
7. 되돌리기: `scripts/oci/close_donga_pending_revert.sql`(리허설 뒤 `-v apply=1`). 보관 표 `news_raw_donga_close_pending`가 있어야 하고, 도는 ingest가 있으면 멈춘다.
   되돌리는 것은 상태와 시도 횟수뿐이다 - **본문 보존 잡(30일)이 닫은 뒤에 돈 행은 본문 칸이 이미 NULL이고 `raw_news_content_purged_at`이 찍혀 있어, 되돌려도 그 칸은 그대로이고 상태만 돌아온다**
   (리허설의 NOTICE `purged=N`이 그 수다. 추출기는 비운 행을 다시 고르지 않는다).

**코드를 되돌릴 때** - 이전 이미지는 `b7e4a1c93d52`를 몰라 그 `migrate`가 "Can't locate revision"으로 멈춘다(4.1의 R3와 같다). 새 이미지로 먼저
`alembic downgrade f1db3221e45f`를 한다: 요약 해시 인덱스를 지우고, `blocked_origin`은 `fetch_failed`(시도 횟수를 `MAX_EXTRACT_ATTEMPTS` 이상으로)로, `rss_summary`는 `dropped`(본문 ''·임베딩 NULL, 이미 비운 행은 본문을
그대로)로 바뀌고 요약 텍스트는 돌아오지 않는다.

### 6.10 국민일보 발행 시각 보정 (2026-10-19 ~ 수집분, 2026-10-25 적용)

**Tier 0 DB는 2026-10-25 11:12 UTC에 보정했다**(441행, [ADR 0008](adr/0008-db-layer-pgvector-schema-and-upsert.md) "2026-10-25 갱신"). 국민일보 RSS의 `pubDate`로 저장한 `raw_news_created_at`이 수집 시각보다
최대 24시간 뒤인 행이 있다(10-19 ~ 10-23 수집분 440행. 저장값 − 기사 페이지의 발행 시각이 -5.3 ~ +24.0시간이라 일정하지 않고, 식으로 고칠 수 없다). 이 절은 그 기록이자 같은 이상이 다시 났을 때의 절차다
(10-25 10:05 UTC 수집분 27행에 다시 났다 - ADR 0026 증거 13 (7)). 원인은 모른다. **본문 추출기는 같은 날 뒤에 고쳤다**(ADR 0008 "2026-10-25 갱신 2": 저장된 발행 시각이 수집보다 10분 넘게 뒤인 행을 추출 때 기사 페이지 값으로 바꾼다.
코드만 고쳤고 Tier 0에는 배포하지 않았다). 배포한 뒤 새로 들어오는 행은 스스로 고쳐지고, 이 절의 손 절차는 그 앞에 들어와 이미 추출이 끝난 행(아래 "수집기 수정과 2차 보정")과 수집기가 못 고친 행(페이지에 발행 시각이 없음 등)에 쓴다.

SQL은 생성기가 만든다 - `scripts/oci/build_kmib_created_at_fix.py`(입력 CSV → 보정 SQL)와 되돌리기 `scripts/oci/fix_kmib_created_at_revert.sql`.
CI의 PostgreSQL에서 `tests/integration/test_fix_kmib_created_at_sql.py`가, 생성기는 `tests/test_build_kmib_created_at_fix.py`가 먼저 돈다.

지켜야 할 것:

- **한 행은 한 번만 보정한다.** 생성한 SQL은 원래 값이 그대로인 행만 바꾸고, 보관 표 `news_raw_kmib_created_at_fix`가 이미 있으면 멈춘다. 이미 보정한 DB에서 새 행을 더 보정하려면 아래 "2차 보정"(보관 표를 따로 둬 1차 보정을 되돌리지 않는다)을 쓴다. 5단계의 "다시 보정할 때"는 1차 보관 표를 치우고 처음부터 다시 하는 방법이다.
- **페이지 값이 사실이라는 것은 가정이다.** `datePublished`는 기사 페이지가 스스로 적은 발행 시각이다. 독립된 확인은 개편 전 RSS(구 URL)로 같은 기사를 이미 받은 16쌍(10분 안 16/16, 1분 안 13)뿐이라 대부분의 행은 간접 근거에 기댄다(ADR 0008). 결과 수치가 다른 언론사의 수집 지연(중앙 0.4 ~ 1.3시간)과 같은 범위인지 6단계처럼 본다.
- **페이지는 Mac에서 받는다.** 기사당 한 번, 요청 사이 2초 이상, 응답에서 `datePublished` 값만 읽고 본문은 저장하지 않는다. VM에서 받지 않는다(1 GB 수집 서버의 요청을 늘릴 이유가 없다).
- 수집이 도는 중에 적용해도 된다(대상 id의 행 단위 UPDATE, `lock_timeout` 5초). 그래도 :50~:59가 겹침이 가장 적다.

순서:

1. **대상 뽑기(VM, 읽기 전용).** 새 URL 형식의 국민일보 행 중 저장된 발행 시각이 수집보다 뒤이거나 저장된 발행일(KST)이 URL 날짜의 하루 뒤인 행이다. `<from>`·`<until>`은 UTC 수집 시각이다
   (처음에는 `2026-10-19`와 `2026-10-25`):

   ```sql
   -- kmib_targets.sql
   \set QUIET on
   SET default_transaction_read_only = on;
   \pset format unaligned
   \pset fieldsep ','
   \pset tuples_only on
   SELECT n.raw_news_id, n.raw_news_url,
          to_char(n.raw_news_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
          to_char(n.raw_news_crawled_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
   FROM news_raw n JOIN press p USING (press_id)
   WHERE p.press_name = '국민일보' AND n.raw_news_url LIKE 'https://www.kmib.co.kr/article/%'
     AND n.raw_news_crawled_at >= '<from>' AND n.raw_news_crawled_at < '<until>'
     AND ( n.raw_news_crawled_at < n.raw_news_created_at AT TIME ZONE 'UTC'
           OR (n.raw_news_created_at AT TIME ZONE 'Asia/Seoul')::date
              - to_date(substring(n.raw_news_url FROM '/article/([0-9]{8})'), 'YYYYMMDD') = 1 )
   ORDER BY n.raw_news_id;
   ```

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter -f -" < kmib_targets.sql > kmib_targets.csv
   wc -l kmib_targets.csv   # 2026-10-25: 442
   ```

   **이미 보정한 DB에서는 이 질의가 고친 행도 잡는다** - 고친 페이지 값의 날짜가 URL 날짜 + 1인 행이 많기 때문이다(2026-10-25 보정 직후 `<until>`을 `2026-10-25`로 두면 315행: 고친 314행 + 건너뛴 1행). 새로 들어온 행만 보려면
   `AND NOT EXISTS (SELECT 1 FROM news_raw_kmib_created_at_fix f WHERE f.raw_news_id = n.raw_news_id)`를 붙이고, 다시 보정하려면 5단계대로 되돌린 뒤 이 질의를 돌린다.

2. **페이지 값 받기(Mac, 2초 간격).** 입력 CSV는 머리줄 없이 `raw_news_id, url, 보정 전 값, 수집 시각, 페이지 값, 오류`다. 먼저 10건 안팎을 받아 페이지 값이 대개 수집 시각보다 1시간 안쪽으로 이른지, 일정한 오프셋이 아닌지(저장값 − 페이지값)
   보고 전수를 받는다. 이 일회성 요청은 수집기의 User-Agent·출발지와 상관없다 - 수집기는 바꾸지 않는다(ADR 0026 결정 9 "하지 않은 것"):

   ```python
   # fetch_pub.py <입력 CSV> <출력 CSV>  (스크래치 폴더에 두고 python3 -I로 돌린다)
   import csv, re, sys, time, urllib.request
   src, out = sys.argv[1], sys.argv[2]
   rx = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
   rows = list(csv.reader(open(src)))
   with open(out, "w", newline="") as f:
       w = csv.writer(f)
       for rid, url, created, crawled in rows:
           pub, err = "", ""
           try:
               req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
               html = urllib.request.urlopen(req, timeout=20).read(400_000).decode("utf-8", "replace")
               m = rx.search(html); pub = m.group(1) if m else ""
               if not pub: err = "no datePublished"
           except Exception as e:
               err = type(e).__name__
           w.writerow([rid, url, created, crawled, pub, err]); f.flush()
           time.sleep(2)
   ```

   442건이 약 15분 걸린다(요청 사이 2초). 오류 칸이 비지 않은 행은 생성기가 건너뛴다 - 많으면 멈추고 원인을 본다.
3. **SQL 만들기.**

   ```bash
   python3 -I scripts/oci/build_kmib_created_at_fix.py kmib_pub.csv > fix_kmib_created_at.sql   # stderr: rows=441 skipped={'no_page_time': 0, 'no_tz': 0, 'after_crawl': 1, 'too_old': 0}
   ```

   `skipped`의 `after_crawl`(페이지 값이 수집보다 10분 넘게 뒤)·`too_old`(7일 넘게 앞)·`no_tz`·`no_page_time`이 0이 아니면 그 행은 보정하지 않는다(저장값이 그대로 남는다). 2026-10-25에는 `after_crawl` 1행이었다.
4. **리허설 → 적용.** 기본은 리허설(ROLLBACK)이고 `-v apply=1`일 때만 COMMIT이다. 리허설의 `NOTICE: fix_kmib_created_at fixed=N`이 3단계의 `rows`와 같아야 한다. 스크립트가 기준을 스스로 확인하고 하나라도 어기면
   예외로 멈춘다(psql 종료 코드 3, 아무것도 커밋되지 않음) - 보관 표가 이미 있음, 바꾼 행 수가 기대와 다름(원래 값이 그 사이 바뀐 행이 있음), 바꾼 뒤에도 발행 시각이 수집보다 10분 넘게 뒤인 대상 행이 남음.

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U newsletter -d newsletter -f -" < fix_kmib_created_at.sql             # 리허설
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -v apply=1 -U newsletter -d newsletter -f -" < fix_kmib_created_at.sql   # 적용
   ```

   적용 뒤 읽기 전용으로 확인한다:

   ```sql
   SELECT count(*) FROM news_raw_kmib_created_at_fix;     -- 3단계의 rows와 같다
   SELECT count(*) FROM news_raw n JOIN press p USING (press_id)
   WHERE p.press_name = '국민일보' AND n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes';
   -- 건너뛴 행과 아직 보정하지 않은 새 행만 남는다 (2026-10-25 11:12 UTC 직후 1행)
   ```

5. **되돌리기.** 보관 표의 원래 값으로 돌리고 보관 표까지 지운다. 보관 표가 없으면 멈추고, 보정 뒤 발행 시각이 다시 바뀐 행이 있으면 덮어쓰지 않도록 멈춘다. 기본은 리허설이다.

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 [-v apply=1] -U newsletter -d newsletter -f -" < scripts/oci/fix_kmib_created_at_revert.sql
   # NOTICE: fix_kmib_created_at_revert restore=<n>
   ```

   **다시 보정할 때.** 이미 보정한 DB에 새로 들어온 행을 더 보정하려면 생성 SQL이 보관 표 때문에 멈추므로, 되돌리기로 보관 표를 치운 뒤 1단계부터 다시 한다 - 되돌린 행도 대상에 다시 들어간다(저장값이 원래 값으로 돌아왔다).
   이미 받은 페이지 값은 새 CSV에 그대로 합쳐도 된다(보정 전 값이 되돌린 뒤의 값과 같다). 새 행만 받으면 된다.
   보정 전에 만든 사본(2026-10-25 11:12 UTC 앞의 덤프 · 반출본 · 호스트 밖 백업)을 복원한 DB에도 같은 절차를 쓴다 - 보관 표가 없으므로 되돌리기는 필요 없다.
6. **결과 수치(2026-10-25).** 대상 442행(수집 10-19 121 · 10-20 73 · 10-21 110 · 10-22 103 · 10-23 33 · 10-24 2), 페이지 대조 442건 오류 0, 생성기 `rows=441 skipped={'no_page_time': 0, 'no_tz': 0, 'after_crawl': 1, 'too_old': 0}`, 적용 11:12:47 UTC, 보관 표 441행.
   저장값 − 페이지값 -5.29 ~ +23.98시간(중앙 19.38, 24시간 ±0.1시간 안 59행). 페이지 발행 → 수집은 중앙 0.72시간(3시간 넘는 63행이 두 회차에 모여 있고 나머지 378행은 3시간 안).
   창(10-18 07:00 ~ 10-25 07:00 UTC) 수집 10,980행의 지연 p95: 보정 전 2.095시간(음수 440행 그대로) → 보정 후 2.443시간(ADR 0026 증거 13 (3)). 보정으로 음수 행은 440 → 1이 됐다.
   같은 날 10:05·11:05 UTC 수집분 27행은 이 보정에 들어 있지 않다. 이 441행의 보정 앞뒤 수치는 6.11의 질의(`C1`·`C5`)로 다시 잴 수 있다.

#### 수집기 수정과 2차 보정 (ADR 0008 "2026-10-25 갱신 2", 코드만 - 배포·실행은 아직)

**수집기(본문 추출기)가 하는 일.** `ai_workspace/crawler/content_extractor/page_pubdate.py`. 기사 페이지를 받은 뒤, 행의 저장된 발행 시각(`raw_news_created_at`)이 수집 시각(`raw_news_crawled_at`, 시간대 없는 UTC)보다 **10분 넘게 뒤**일 때만
페이지의 JSON-LD `datePublished`(없으면 `<meta property="article:published_time">`)로 바꾼다. 쓰는 값은 그 기사 자신의 것뿐이다 - JSON-LD는 모든 덩어리를 통틀어 우선순위 1위 노드의 값 하나만 보고(관련 기사 목록 등 다른 노드의 값으로는 넘어가지 않는다), 그 값을 못 쓰면 meta를 본다. 페이지 값에 시간대가 있고, 수집 시각 + 10분 이하이고, 수집 시각 − 7일 이상일 때만 쓴다(위 3단계 생성기와 같은 조건).
미래가 아닌 행은 읽지도 않는다. 언론사를 가리지 않는다. 추출 결과를 쓰는 같은 트랜잭션에서 쓰고, `WHERE`가 "지금도 미래일 때만"이라 먼저 고쳐진 행(예: 이 절의 보정)은 덮지 않는다.
**ingest의 `stats.extract`에 남는 키:** `created_at_from_page`(언론사별로 바꾼 행 수), `created_at_unfixed`(미래였는데 못 바꾼 행의 언론사별 사유 개수 - `no_page_time` · `no_tz` · `after_crawl` · `too_old`, 그리고 `fetch_failed` · `error`. 그 행은 미래인 채 남는다).
`fetch_failed`(페이지를 못 받았다)·`error`(처리 중 예외로 롤백했다)로 끝난 미래 행은 그 시도에서 못 고치고 시도마다 이 두 사유로 센다 - 시도 상한(3회) 안에서 다시 받을 때 고친다. 동아일보는 RSS 요약이라 추출 단계를 거치지 않아 이 경로로는 고치지 못한다.

**배포 뒤 확인(읽기 전용).** 국민일보 새 행이 있는 회차에서 `created_at_from_page`에 국민일보 건수가 찍히는지, `created_at_unfixed`가 무엇으로 차는지 본다:

```sql
SELECT started_at AT TIME ZONE 'UTC' AS start_utc, git_sha,
       stats->'extract'->'created_at_from_page' AS fixed, stats->'extract'->'created_at_unfixed' AS unfixed
FROM job_runs WHERE job = 'ingest' ORDER BY started_at DESC LIMIT 12;

-- 배포 뒤 수집된 행에 미래 값이 남았는가 (<deploy>는 새 커밋의 첫 ingest 시작 시각, UTC). 아직 추출 대기인 행과 못 고친 행만 남아야 한다
SELECT p.press_name, coalesce(n.raw_news_extract_status, 'NULL') AS status, count(*)
FROM news_raw n JOIN press p USING (press_id)
WHERE n.raw_news_crawled_at >= '<deploy>'
  AND n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes'
GROUP BY 1, 2 ORDER BY 1, 2;
```

**지금 미래 시각인 행(Tier 0, 읽기 전용, 2026-10-25 12:31 UTC).** 저장된 발행 시각이 수집보다 10분 넘게 뒤인 행은 **30행이고 모두 국민일보**다(수집 10-25 29행 = 10시 25 · 11시 2 · 12시 2, 수집 10-20 1행 = 1차에서 페이지 값이 수집보다 뒤라 건너뛴 행).
수집보다 17.75 ~ 23.78시간 뒤이고, 추출 상태는 `ok` 24 · `dropped` 6(모두 시도 1회 - 수집기가 이미 지나갔다). 다른 언론사는 DB 전체에서 0행이다. 1차 보관 표(441행)와 겹치는 행은 없다. 이 수는 배포 전까지 늘어난다 - 배포한 뒤 다시 센다:

```sql
-- future_counts.sql (읽기 전용)
SELECT p.press_name, coalesce(n.raw_news_extract_status, 'NULL') AS status, count(*),
       min(n.raw_news_crawled_at)::date AS first_crawl, max(n.raw_news_crawled_at)::date AS last_crawl
FROM news_raw n JOIN press p USING (press_id)
WHERE n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes'
GROUP BY 1, 2 ORDER BY 1, 2;
```

**2차 보정.** 수집기 수정이 닿지 않는 행 - 이미 추출이 끝난 행 - 을 고친다. **수정을 배포한 뒤 한 번** 한다(배포 전에 하면 그 사이 새 미래 행이 또 생긴다). 1차와 달리 언론사를 가리지 않고, 보관 표는 1차(`news_raw_kmib_created_at_fix`)와 따로
`news_raw_created_at_fix_2`를 써서 1차 보정은 그대로 둔 채 더 고치고 따로 되돌린다. 위 1~6단계와 같은 모양이고 다른 곳만 적는다:

1. **대상 뽑기(VM, 읽기 전용).** 지금 미래이면서 **수집기가 다시 고르지 않는** 행이다: 추출 상태가 `NULL`(아직 대기)인 행과 다시 받을 여지가 남은 `fetch_failed`·`error`(시도 3회 미만, `Settings.MAX_EXTRACT_ATTEMPTS`)는 빼는데,
   배포한 수집기가 그 행을 받을 때 스스로 고치고 그 사이 값이 바뀌면 생성 SQL이 "원래 값이 그대로였다"며 통째로 멈추기 때문이다.

   ```sql
   -- future_targets.sql
   \set QUIET on
   SET default_transaction_read_only = on;
   \pset format unaligned
   \pset fieldsep ','
   \pset tuples_only on
   SELECT n.raw_news_id, n.raw_news_url,
          to_char(n.raw_news_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
          to_char(n.raw_news_crawled_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
   FROM news_raw n
   WHERE n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes'
     AND n.raw_news_extract_status IS NOT NULL
     AND NOT (n.raw_news_extract_status IN ('fetch_failed', 'error') AND n.raw_news_extract_attempts < 3)
   ORDER BY n.raw_news_id;
   ```

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter -f -" < future_targets.sql > future_targets.csv
   wc -l future_targets.csv   # 2026-10-25 12:31 UTC에는 30 (배포 전 기준 - 배포 뒤 다시 뽑는다)
   ```

2. **페이지 값 받기(Mac, 2초 간격).** 수집기와 **같은 판정**을 쓰려고 `page_pubdate.py`를 경로로 읽는다(표준 라이브러리만 쓴다). 입력은 1단계의 네 칸 CSV(`future_targets.csv`, 머리줄 없음)이고, 출력은 생성기가 받는 여섯 칸 CSV(`future_pub.csv`)다. 이 스크립트는 페이지 값 칸에 판정이 `fixed`일 때만 UTC 시각을 쓰며
   아니면 비우고 오류 칸에 사유(`no_page_time` · `no_tz` · `after_crawl` · `too_old`)를 쓴다. 본문은 저장하지 않는다. 요청은 기사당 한 번이다.

   ```python
   # fetch_pub_2.py <입력 CSV> <출력 CSV> <저장소 루트>  (스크래치 폴더에 두고 python3 -I로 돌린다)
   #   python3 -I fetch_pub_2.py future_targets.csv future_pub.csv <저장소 루트>
   import collections, csv, importlib.util, sys, time, urllib.request
   from datetime import datetime
   src, out, repo = sys.argv[1:4]
   spec = importlib.util.spec_from_file_location("page_pubdate", f"{repo}/ai_workspace/crawler/content_extractor/page_pubdate.py")
   pp = importlib.util.module_from_spec(spec); sys.modules["page_pubdate"] = pp; spec.loader.exec_module(pp)
   iso = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
   outcomes = collections.Counter()
   with open(out, "w", newline="") as f:
       w = csv.writer(f)
       for rid, url, created, crawled in csv.reader(open(src)):
           pub, err = "", ""
           try:
               req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
               html = urllib.request.urlopen(req, timeout=20).read(2_000_000).decode("utf-8", "replace")
               d = pp.decide_created_at(iso(created), iso(crawled), html)
               pub, err = (d.value.isoformat(), "") if d.value else ("", d.outcome or "not_future")
           except Exception as e:
               err = type(e).__name__
           outcomes[err or "fixed"] += 1
           w.writerow([rid, url, created, crawled, pub, err]); f.flush()
           time.sleep(2)
   print(dict(outcomes), file=sys.stderr)
   ```

   이 일회성 요청은 수집기의 출발지·User-Agent와 상관없다 - 수집기는 바꾸지 않는다(ADR 0026 결정 9 "하지 않은 것"). 동아일보처럼 Mac에서도 거절하는 곳이 있으면 오류 칸이 차고 그 행은 건너뛴다. `not_future`가 나오면
   대상을 뽑은 뒤 값이 바뀐 행이다(수집기가 고쳤다) - 생성기에 넣지 말고 1단계부터 다시 뽑는다.
3. **SQL 만들기.** `--all-presses`와 둘째 보관 표 이름을 준다:

   ```bash
   python3 -I scripts/oci/build_kmib_created_at_fix.py future_pub.csv --all-presses --table news_raw_created_at_fix_2 > fix_created_at_2.sql   # stderr: rows=N skipped={...}
   ```

   `--press 국민일보`처럼 한 언론사로 좁힐 수도 있다(`--all-presses`와 함께는 못 쓴다). 보관 표 이름은 소문자·숫자·밑줄만 받는다. `skipped`가 0이 아닌 행은 보정하지 않는다(위 3단계와 같다). 2단계 스크립트가 페이지 값을 비운 행은 생성기에서 모두 `no_page_time`으로 세어지므로 사유별 개수는 2단계 스크립트가 stderr에 찍은 값을 본다.
4. **리허설 → 적용.** 위 4단계와 같은 명령에 파일만 바꾼다:

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U newsletter -d newsletter -f -" < fix_created_at_2.sql             # 리허설: NOTICE fixed=N이 rows와 같아야 한다
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -v apply=1 -U newsletter -d newsletter -f -" < fix_created_at_2.sql   # 적용
   ```

   적용 뒤 읽기 전용으로: `SELECT count(*) FROM news_raw_created_at_fix_2;`가 `rows`와 같고, 위 `future_counts.sql`이 건너뛴 행과 아직 추출 대기인 행만 남기는지 본다.
5. **되돌리기.** 2차 보관 표 이름을 바꾼 되돌리기 SQL을 생성기가 만든다(보관 표가 없으면 멈추고, 보정 뒤 값이 다시 바뀐 행이 있으면 덮지 않도록 멈춘다. 기본은 리허설). **1차 보관 표와 1차 보정은 건드리지 않는다**:

   ```bash
   python3 -I scripts/oci/build_kmib_created_at_fix.py --revert --table news_raw_created_at_fix_2 > revert_created_at_2.sql
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 [-v apply=1] -U newsletter -d newsletter -f -" < revert_created_at_2.sql
   # NOTICE: fix_kmib_created_at_revert restore=<n>
   ```

2차 보정 전의 값은 `news_raw_created_at_fix_2`에 있다. 6.11의 `C5`(보정 전 읽기)는 1차 보관 표만 읽으므로 2차 행의 보정 전 값은 반영하지 않는다(질의는 이번에 바꾸지 않았다). 배포와 2차 보정을 마친 뒤 6.11의 `C4` 줄에는 생성기가 건너뛴 행만 남는다.
CI에서는 `tests/test_content_extractor_page_pubdate.py` · `tests/test_content_extractor_created_at.py`(수집기), `tests/test_build_kmib_created_at_fix.py`(생성기 인자), PostgreSQL의 `tests/integration/test_extract_created_at_from_page_db.py` ·
`tests/integration/test_fix_kmib_created_at_sql.py`(2차 보정과 되돌리기)가 먼저 돈다. **이 절의 2차 보정은 실행하지 않았다.**

### 6.11 승격 기준 7일 창 재측정 (2026-10-25 판정, 2026-11-01 이후 재확인)

[ADR 0026](adr/0026-hosting-tiers-and-contingency.md) 결정 3의 기준(7일 창에서 잡 성공률·OOM·수집 지연 p95, 매일 백업과 복원 검증)을 같은 질의로 다시 재는 절차다. 2026-10-25 판정(증거 13)은 일회용 질의로 했고,
같은 값을 다시 내는 질의를 `scripts/oci/soak_window_check.sql`로 저장소에 넣었다. **읽기 전용**이다(`default_transaction_read_only = on`, `statement_timeout` 180초). 본문 칸은 읽지 않고, 기사 제목은 재수집 행을
고를 때 같은지만 비교한다(출력하지 않는다). 되돌림은 자동이 아니다 - 이 질의의 출력을 사람이 기준과 맞대어 읽는다.

순서:

1. **국민일보 발행 시각 이상부터 정리한다.** 보정(6.10) 뒤에도 국민일보 새 행의 발행 시각이 수집보다 뒤인 이상이 다시 난다(2026-10-25 10:05 UTC 이후). 정리하지 않으면 재확인 창의 지연 p95를 읽을 수 없다
   (10-25 07:00 ~ 12:00 UTC 조각에서는 미보정 27행이 음수로 잡혔다). 이 질의의 `C4` 줄이 비어 있도록 6.10을 먼저 돌린다 - 수집기 수정(6.10 "수집기 수정과 2차 보정")을 배포한 뒤에는 새 행이 스스로 고쳐지므로 배포 전에 들어온 행만 2차 보정으로 한 번 정리하면 된다.
2. **새 스키마의 복원 검증을 창 안에서 한 번 한다**(6.5의 "복원 검증", 버킷에서 받은 사본). 28일 주기 자동 검증(11-22 이후)은 재확인에 늦다. 결과는 `E2` 줄에 나온다 - 리비전이 지금 head(`b7e4a1c93d52`)여야 한다.
3. **질의를 돌린다.** 창은 파일 앞의 `\set w0`·`\set w1` 두 줄이다(처음 값은 2026-10-25 판정 창 10-18 07:00 ~ 10-25 07:00 UTC). 재확인은 `w0`을 `2026-10-25 07:00+00`, `w1`을 `2026-11-01 07:00+00`으로 바꾼 사본을 쓴다 -
   **정시**여야 한다(정시 슬롯을 시 단위로 센다).

   ```bash
   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U newsletter -d newsletter -f -" < scripts/oci/soak_window_check.sql
   ```

4. **따로 읽는 것**(이 질의로 못 잰다): 커널 저널의 OOM(`journalctl -k`에서 `out of memory`·`oom-kill`·`killed process`와 `page allocation failure`), `docker inspect`의 `OOMKilled`·재시작 횟수,
   VM의 백업 스크립트 sha256, 버킷 `full/`의 수명 주기 삭제. 2026-10-25 판정의 값은 첫 조회의 것이고 보정 PR에서 다시 재지 못했다(증거 13 (1)·(6)).

출력 줄(앞 이름이 줄 이름이다):

| 줄 | 뜻 | 기준 |
|---|---|---|
| `A1` | 정시 ingest 슬롯 수 · 시작한 슬롯 · 성공한 슬롯(시작되지 않은 회차까지 센다) | 성공 ≥ 95% |
| `A2` | 연속 48슬롯 창 수 · 48/48인 창 · 최솟값 | ADR 0006 기준(48시간 연속 ≥ 95%) |
| `A3` | 정시 popularity 슬롯 | 참고 |
| `A4` | 잡별 실행 · 성공 · 끝나지 않은 것(잡 이름이 빈 마지막 줄이 합계) | 성공 ≥ 95% |
| `A5` · `A6` · `A7` | ingest 소요 · 코드(`git_sha`)별 · 손으로 한 실행(백업·복원·`rss_retry`) | 코드가 바뀐 곳과 손으로 한 실행을 같이 적는다 |
| `B1` ~ `B4` | 경고가 있던 회차 · 동아일보 RSS 읽기 · 성공 사이 공백 · 옛 건전성 규칙 반사실 | 성공률이 가린 것을 같이 적는다(결정 9) |
| `C1` | 수집 지연(시간): 가(제외 없음 - **판정값**) · 나(새 URL로 다시 들어온 재수집 행 제외) · 다(국민일보 제외) · 음수를 99시간으로 | 가의 p95 ≤ 3시간 |
| `C2` · `C3` | 언론사별 · 수집일별 p95 | 3시간을 넘는 곳을 숨기지 않고 적는다 |
| `C4` | 국민일보 발행 시각 이상(발행 > 수집 + 10분) 시각별 행 수, 보관 표가 있으면 보정한 행은 뺀다 | 재확인 전에 비어 있어야 한다(생성기가 건너뛴 행만 남는다. 2026-10-25 판정 창에는 그 1행이 나온다) |
| `C5` | 보정 전 읽기(보관 표의 원래 값, 보관 표가 있을 때만) | 참고 |
| `D1` | 언론사별 추출 상태 | 범위를 정하는 근거 |
| `E1` ~ `E3` | 정시 백업 · 최근 복원 검증 5건 · 유휴 점검·일일 리포트·보존 잡 | 백업 7/7, 복원 검증이 창 안에 있고 리비전이 head |

2026-10-25 판정 창에서 이 질의의 출력은 증거 13의 수치와 같다: `A1` 168 · 168 · 168, `A2` 121 · 121 · 48, `A4` 합계 368 · 368 · 0, `B1` 167 · 168 · 292, `B2` 168 · 130 · 38, `B4` 168 · 0 · 164 · 8 · 149,
`C1` 가 10,980행 p95 2.443, `C5` 가′ 2.095. 단 하나는 일회용 질의와 다르다: `C1`의 나(재수집)는 구 URL 행 196개를 잘못 넣은 235행 정의를 새 URL 39행으로 고쳤다(p95 2.287 → 2.232, ADR 0026 증거 13 (3)).

## 7. Tier 0 내리기

```bash
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose down'   # 볼륨(데이터)은 남는다
# 인스턴스 삭제는 되돌릴 수 없다 - 사람이 결정한 경우에만
oci compute instance terminate --instance-id $INSTANCE --preserve-boot-volume false
```

## 8. 수집 전용 이미지 의존성 잠금

`docker/requirements-ingest.txt`는 worker 잠금 파일을 제약으로 걸어 같은 버전을 쓴다:

```bash
uv pip compile docker/requirements-ingest.in --universal --python-version 3.11 \
  -c docker/requirements-worker.txt -o docker/requirements-ingest.txt
```

## 9. LLM 실험용 동결 반출 (읽기 전용) — 누가, 언제, 확인, 정리

실험(E0 등)은 운영 DB에 쓰지 않고, Tier 0 `news_raw`에서 실험 창만 읽기 전용으로 뽑은 파일에서 돈다. 결정과 근거는
[ADR 0036](adr/0036-experiment-data-path-frozen-export-file-stand-ins.md), 본문 사본의 위치와 30일 규칙은
[ADR 0023](adr/0023-data-sources-copyright-retention.md) 개정(2026-10-06)이다.

**2026-10-06 현재 이 절차로 기사를 반출한 적은 없다.** 도구는 CI의 Postgres(합성 행)에서 검증됐고, VM에서는 빈 창(행 0건)으로 9.3의
경로만 확인했다(2.46초, 머리말이 읽기 전용 스냅숏을 확인 — ADR 0036 증거 6). 행을 읽을 때 걸리는 시간과 반출 중 VM 여유 메모리는 첫 실행에서
재서 ADR 0036의 증거에 적는다.

### 9.0 하기 전에 — 하나라도 아니면 돌리지 않는다
- ADR 0023 개정(2026-10-06)이 main에 있고, 사용자가 이 반출을 승인했다.
- **누가**: 사람이 Mac의 터미널에서 직접 돌린다. 예약 작업이나 상주 프로세스로 돌리지 않는다. 한 번의 반출은 한 번의 `ssh` 명령이다.
- **어디서**: 메인 체크아웃(워크트리가 아니다). HEAD가 main에 있는 커밋이고 `git status --porcelain`이 비어 있다 - 매니페스트에 코드 SHA와
  작업 트리가 깨끗했는지가 적힌다. E0의 반출이면 HEAD의 코드 경로가 `code_sha`(러너 PR의 병합 커밋)와 같아야 한다(ADR 0009 A8.2).
- **`data/`가 백업·동기화 대상이 아니다**: `tmutil isexcluded data`(Time Machine을 쓴다면), iCloud·Dropbox 폴더 안이 아닌지. 대상이라면
  그쪽 사본은 도구가 지우지 못한다 - 제외한 뒤에 반출한다.
- **언제**: 매시 :40~:55. `docker/crontab.micro`의 ingest(:05, 소요 중앙 15분·p95 73분 - ADR 0026 증거 8)와 popularity(:35, 4~6초)
  뒤다. rss_retry(:40, 4.2 - 실패한 피드가 있을 때만 보통 수십 초)가 :40에 돌 수 있으므로 :45 이후가 더 한가하다 - 반출은 읽기 전용 스냅숏이라
  겹쳐도 틀리지는 않지만 메모리를 같이 쓴다. 도구가 분을 확인하고 그 밖이면 거절한다. ingest가 1시간을 넘기는 회차가 있어 분만으로는 겹침을
  피할 수 없으므로, 9.2의 첫 조회(돌고 있는 잡)를 **반출 직전에 한 번 더** 한다.

### 9.1 창 정하기
E0의 창은 T0 직전에 완결된 KST 날짜들의 06:00 KST를 의사 시각으로 하는 24시간 창이다(ADR 0009 A8.1). 손으로 계산하지 않고,
반출 명령에 창을 넣지도 않는다 - 9.3의 `export --days 3`이 T0(기본: 지금)로 창을 계산하고 그 계획(T0, 날짜 창)을 매니페스트에 적는다.
서버 스냅숏(= 반출본의 T0)의 KST 날짜가 계획한 T0의 날짜와 다르면(KST 자정 전후) 쓰기 전에 거절한다 - 그때는 다시 돌린다.
9.2의 건수 조회에 넣을 창은 미리 보기로 본다:

```bash
python scripts/export_news_raw.py plan-window --days 3   # 후보가 모자라면 5 (A8.1의 연장). start_utc, end_utc를 9.2에 넣는다
```

창은 최대 120시간이다. 그보다 길면 도구가 거절한다. E0이 아닌 반출(다른 창)은 `--start-utc`·`--end-utc`로 주고, 그 반출본에는 날짜 창
계획이 없어 E0 러너가 받지 않는다.

### 9.2 VM 상태 확인 (읽기만)

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -At -U newsletter -d newsletter \
  -c \"SELECT job, status, started_at FROM job_runs WHERE status = 'running'\" \
  -c \"SELECT count(*) FROM news_raw n WHERE n.raw_news_extract_status = 'ok' AND n.raw_news_content <> '' \
       AND n.raw_news_crawled_at >= TIMESTAMP '<start_utc>' AND n.raw_news_crawled_at < TIMESTAMP '<end_utc>'\" \
  -c \"SELECT count(*), count(*) FILTER (WHERE news_letter_id IS NOT NULL) FROM news_raw\""
ssh -F .ops/micro/ssh_config micro 'grep MemAvailable /proc/meminfo'
```

- 돌고 있는 `ingest`·`popularity`·`rss_retry`가 있으면(1시간을 넘긴 회차, 6.6) 끝날 때까지 기다린다. 이 첫 조회는 9.3의 반출 직전에 한 번 더 한다.
- MemAvailable이 400 MiB 아래면 다음 시간대로 미룬다. 근거: 10일 관측의 p5가 389 MiB, 중앙값이 629 MiB다(ADR 0026 증거 8) - 평소보다
  낮은 상태에 새 부하를 얹지 않는다는 뜻이다. 반출이 실제로 쓰는 메모리는 아직 재지 않았다.
- 둘째 줄의 건수가 반출될 행 수다. 2026-10-06 기준 3개 창 2,595행, 5개 창 5,633행(행당 평균 4.2 KB로 약 11 MB / 23 MB 추정).

### 9.3 반출

다른 터미널에서 메모리를 지켜본다(첫 실행의 증거가 된다):

```bash
ssh -F .ops/micro/ssh_config micro 'for i in $(seq 1 40); do date +%T; grep -E "MemAvailable|SwapFree" /proc/meminfo; sleep 3; done'
```

```bash
time python scripts/export_news_raw.py export --days 3 \
  --psql-command "ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter'"
```

- 도구가 psql의 표준입력으로 보내는 것은 스크립트 하나다: `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` →
  `SET LOCAL statement_timeout = '120s'`(락 5초, 유휴 60초) → `COPY ... TO STDOUT` 세 번 → `ROLLBACK`. DB 비밀번호는 쓰지 않는다
  (컨테이너 안의 로컬 접속). VM에는 파일을 만들지 않는다.
- 도구가 DB에 닿기 전에 거절하는 것: 창의 시작 + 30일이 이미 지난 창(그 본문은 만료됐다), 메인 체크아웃 `data/exports` 밖의 `--root`.
- 받은 뒤 쓰기 전에 거절하는 것: 서버 스냅숏의 KST 날짜가 계획과 다름, 스냅숏 시각에 이미 30일이 지난 상업 언론사 본문, 창 안에 이미 뉴스레터에
  연결된 행(`news_letter_id`가 있는 행 - 운영 로더는 그 행을 빼므로 파일 대역과 결과가 달라진다).
- 성공하면 요약 JSON이 나온다: `dir`, `identity_sha256`, `rows`, `window_utc`, `snapshot_at_utc`(= T0), `plan`(T0·날짜 창), `alembic_revision`,
  `code_sha`, `code_dirty`, `by_press`, `db_hash_check`, `body_expires_at_utc`, `articles_sha256`. **본문·제목·URL은 출력되지 않는다.**
  파일은 `<메인 체크아웃>/data/exports/<T0>/`에 `manifest.json`(먼저 쓴다)과 `articles.jsonl`(본문)로 놓인다(디렉터리 0700, 파일 0600).
- 실패하면 아무것도 쓰지 않는다.

  | 메시지 | 뜻 | 할 일 |
  |---|---|---|
  | `:40~:55 밖이다` | ingest(:05)·popularity(:35)와 겹칠 수 있다 | 기다렸다가 9.2의 첫 조회부터 다시 |
  | `KST 날짜가 다르다` | 계획한 T0와 서버 스냅숏의 KST 날짜가 다르다(자정 전후) | 다시 돌린다(`--t0-kst`를 줬다면 오늘 날짜로) |
  | `보존 기한 … 지났다` / `지난 행이 N건이다` | 창의 본문이 이미 30일을 넘겼다 | 그 창은 반출하지 않는다. 더 최근 창으로 다시 계획한다 |
  | `뉴스레터에 연결된 행 … N건이다` | 창 안에 `news_letter_id`가 채워진 행이 있다(Tier 0에서는 생성이 돌지 않으므로 0이어야 한다) | 반출하지 않는다. 누가 VM DB에 썼는지 조사한다 |
  | `psql 명령이 종료 코드 …` + `statement timeout` | 한 문장이 120초를 넘겼다 | VM 상태(9.2)를 다시 보고, 그래도 넘으면 `--statement-timeout-s`를 올리되 값을 ADR 0036에 적는다 |
  | `꼬리말이 없다` / `행 수가 맞지 않는다` | 출력이 중간에 끊겼다(SSH) | 다시 돌린다. 읽기만 했으므로 VM에 남은 것은 없다 |
  | `읽기 전용 스냅숏 … 확인해 주지 않았다` | 서버가 읽기 전용 트랜잭션임을 돌려주지 않았다 | 돌리지 않는다. psql 명령이 스크립트를 그대로 전달하는지 조사 |
  | `git이 추적할 수 있는 곳이다` / `… 아래에만 둔다` | 쓰는 곳이 허용 위치(메인 체크아웃의 `data/exports`)가 아니다 | 메인 체크아웃에서 `--root` 없이 돌린다 |
  | `이미 반출본이 있다` | 같은 T0 디렉터리가 있다 | 덮어쓰지 않는다. 새 반출은 새 T0다 |

### 9.4 확인

```bash
python scripts/export_news_raw.py verify --dir data/exports/<T0>      # 파일을 다시 읽어 해시를 대조한다. identity_sha256이 9.3과 같아야 한다
ls -ld data/exports/<T0> && ls -l data/exports/<T0>                   # drwx------ / -rw-------
git status --porcelain                                                # 비어 있어야 한다
git check-ignore -v data/exports/<T0>/articles.jsonl                  # .gitignore의 /data/ 규칙이 나와야 한다
```

- `rows`가 9.2의 건수와 같은가. 다르면 9.2와 반출 사이에 추출이 `ok`로 끝난 행이 창 안에 생긴 것이다(늘어난 쪽만 정상) - 차이를 적는다.
- `db_hash_check`의 `mismatch`·`missing`이 0인가. 0이 아니어도 반출은 유효하지만(해시는 받은 본문에서 다시 계산한다) DB의 해시 열이 본문과
  다르다는 뜻이므로 건수를 적고 따로 조사한다.
- `alembic_revision`이 VM의 리비전(`d48994e9d26e`)인가, `code_dirty`가 false인가.
- VM이 그대로인가: 9.2의 셋째 조회를 다시 돌린다. 수집이 계속되므로 전체 행 수는 늘 수 있다. `news_letter_id`가 채워진 행은 0이어야 한다.
  창 안의 연결 행은 도구가 반출과 같은 스냅숏에서 세어 매니페스트(`identity.linked_rows`)에 0으로 적었다 - 이 조회는 VM 전체를 보는 운영 확인이다.
- `identity_sha256`, `rows`, `snapshot_at_utc`, `body_expires_at_utc`를 실험의 실행 전 기록(E0이면 ADR 0009 A8.10과 yaml의
  `pre_run_record`)에 옮겨 적고, 만료일을 `.ops/RUNNING.md`에도 적는다.

### 9.5 임베딩 팩 대조
임베딩 잡(Colab T4)은 10.2절이다. 잡이 돌려준 팩을 `data/exports/<T0>/embeddings/`에 받은 뒤:

```bash
python scripts/import_embeddings.py --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
```

팩은 `ids.npy`, `emb.f16.npy`, 잡이 읽은 본문·제목의 sha256(`content_sha256.txt`, `title_sha256.txt`)과 매니페스트다. 반출본 옆
(`data/exports` 아래)에 있어야 받는다. 통과하면 행 수·차원·dtype·벡터 파일 sha256이 나온다(실행 전 기록의 `embeddings_sha256`). 어긋나면
사유 코드(`ids`, `content_sha256`, `title_sha256`, `dim`, `norm`, `export_identity`, `model`, `file_sha256` …)와 함께 종료 코드 2로 끝난다
- 그 팩은 쓰지 않는다.

### 9.6 정리 — 본문 30일
- 본문 만료는 행마다 `crawled_at` + 30일이다. 요약의 `body_expires_at_utc`가 가장 이른 만료, `last_body_expiry_utc`가 마지막 만료다.
- 반출본을 여는 도구(`verify`, `import_embeddings.py`, 실험 러너)는 열 때마다 만료된 본문을 디스크에서 지운다. 만료된 본문은 어떤 경우에도
  도구 밖으로 나오지 않는다. 해시·길이·제목·URL과 `identity_sha256`은 남는다.
- 아무 도구도 돌리지 않는 날을 위해, 가장 이른 만료일에 사람이 돌린다:

  ```bash
  python scripts/export_news_raw.py purge --all                              # data/exports 아래 전부, 만료된 본문만
  python scripts/export_news_raw.py purge --dir data/exports/<T0> --everything   # 할 일이 끝났으면 만료 전이라도 전부
  ```

  출력의 `rows_with_body`가 남은 본문 수다. `--everything` 뒤에는 0이어야 한다. `purge --all`이 종료 코드 1이면 읽을 수 없는 반출본이나
  매니페스트 없이 본문 파일만 남은 디렉터리가 있다는 뜻이다(출력의 `error`) - 그 디렉터리는 본문이 남아 있을 수 있으니 직접 확인하고 지운다.
- 정리는 매니페스트를 먼저, 본문 파일을 나중에 쓴다. 그 사이에서 끊긴 정리는 다음에 열 때 도구가 같은 정리를 다시 해 끝낸다. 그 밖의 이유로
  본문 파일이 매니페스트와 다르면(`본문 파일이 매니페스트와 다르다`, `제목이 기록된 해시와 다르다` 등) 누군가 손댄 파일이다 - 도구는 열지도
  고쳐 쓰지도 않으므로 직접 확인하고 지운다.
- 디렉터리를 통째로 지우지 않고 본문만 지우는 이유: 매니페스트와 해시가 남아야 실험의 실행 전 기록에 적은 sha256을 나중에도 대조할 수 있다.
- 한 실험이 끝나면(라벨 리포트가 커밋되면) 만료 전이라도 그 반출본의 본문을 지운다.
- 지우는 것은 파일의 내용이다. 저장 장치 수준의 복구 불가능성은 보장하지 않는다(ADR 0023 개정 "남는 위험").

## 10. E0 기준선 측정 — 원격 임베딩, 실행 전 기록, 실행, 리포트, 라벨 ([ADR 0009 A8](adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md))

**2026-10-17 현재 E0은 돌리지 않았다.** 도구는 합성 입력과 페이크 LLM 전송 계층으로만 확인했다
(`python -m evaluation.llm.e0_baseline --dry-run`, 실제 기사·실제 호출 없음). 순서는 A8.0 그대로이고, 한 단계라도 건너뛰면 러너가 시작하지 않는다.
명령은 전부 **메인 체크아웃**에서 `PYTHONPATH=ai_workspace`로 돌린다(워크트리의 편집 설치가 섞이지 않게). `prepare`·`run`(재개 포함)·`e0_report`·`e0_labels`에는 **`PYTHONHASHSEED=0`**도 붙인다 - 운영 `split_v2`의 동점 순서가 해시 시드를 따라 표본을 다시 계산하지 못하면 러너가 시작하지 않는다(ADR 0009 A8.11). Mac 규칙대로 계산이 있는 명령에는
`nice -n 19 env OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2`를 붙인다.

### 10.1 code_sha와 반출
1. E0 러너 PR의 main 병합 커밋이 `code_sha`다. ADR 0023 개정(2026-10-06)도 main에 있어야 한다(그 병합 커밋 = `adr_0023_amendment_commit`).
2. 메인 체크아웃: `git fetch origin && git checkout --detach <code_sha>`, `git status --porcelain`이 비어 있다.
3. 9절대로 반출하되 **날짜 창 5개**로: `python scripts/export_news_raw.py export --days 5 --psql-command "..."`. 러너는 마지막 3개 창부터 쓰고
   표본이 모자라면 하루씩 늘린다(A8.1). 출력의 `identity_sha256`이 `--export-identity`와 `export_manifest_sha256`이다.

### 10.2 임베딩 (Colab T4 세션 하나, 상한 4 CU)
```bash
git archive --format=tar.gz -o /tmp/e0emb_code.tar.gz <code_sha> ai_workspace/config ai_workspace/core ai_workspace/pipeline \
  evaluation/__init__.py evaluation/llm/__init__.py evaluation/llm/embed_job.py evaluation/llm/embedding_pack.py \
  evaluation/llm/frozen_export.py scripts/colab_embed_articles.py scripts/m4_colab_driver.py requirements-colab-embed.txt
shasum -a 256 /tmp/e0emb_code.tar.gz                    # = <code_tar_sha>
colab usage                                             # 실행 전 잔액·사용률(다른 세션의 사용률도 적는다)
colab new -s e0-embed --gpu T4
colab upload -s e0-embed /tmp/e0emb_code.tar.gz /content/e0emb/code.tar.gz
colab upload -s e0-embed data/exports/<T0>/manifest.json /content/e0emb/input/manifest.json
colab upload -s e0-embed data/exports/<T0>/articles.jsonl /content/e0emb/input/articles.jsonl
colab exec -s e0-embed --timeout 3600 --env E0EMB_CODE_SHA256=<code_tar_sha> \
  --env 'E0EMB_DRIVER_ARGS_JSON=["run","--workdir","/content/e0emb","--code-tarball","/content/e0emb/code.tar.gz","--code-sha256","<code_tar_sha>","--export-identity","<identity_sha256>","--cu-rate","1.07","--cu-cap","4"]' \
  -f scripts/colab_embed_launch.py
```
- 본문은 세션에 직접 올린다. GCS·Drive에 올리지 않는다(ADR 0023 개정). 출력에 `cleanup: 입력 파일 2개 삭제(done), 입력 디렉터리 남음=False`가
  있어야 한다 - 실패·예산 중단이어도 입력은 지워진다.
- 팩과 기록을 받는다:
  ```bash
  mkdir -p data/exports/<T0>/embeddings data/exports/<T0>/embed_job
  for f in ids.npy emb.f16.npy content_sha256.txt title_sha256.txt embeddings_manifest.json; do
    colab download -s e0-embed /content/e0emb/out/pack/$f data/exports/<T0>/embeddings/$f; done
  for f in job.json compute.json cleanup.json run.log; do
    colab download -s e0-embed /content/e0emb/out/$f data/exports/<T0>/embed_job/$f; done
  colab stop -s e0-embed && colab usage                 # 실행 뒤 잔액 → colab_cu_used(다른 세션 몫을 뺀 값과 compute.json의 추정을 함께 적는다)
  python scripts/import_embeddings.py --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
  ```
- 세션이 잡 도중에 사라지면: 새 세션에서 같은 업로드를 하고 같은 명령에 `--resume-bundle`(미리 `bundle` 하위 명령으로 받아 둔 파트 묶음)과
  `--resume-bundle-sha256`을 더한다. 15분 안팎의 잡이라 보통은 처음부터 다시 돌리는 편이 낫다.

### 10.3 표본과 실행 전 기록 (LLM 호출 없음)
```bash
nice -n 19 env OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 PYTHONPATH=ai_workspace \
  python -m evaluation.llm.e0_baseline prepare --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
```
`evaluation/llm/evalsets/e0-baseline-v1.jsonl`(id·해시·길이·order만)을 쓰고 실행 전 기록에 옮길 값을 출력한다: `export_*`, `embeddings_sha256`,
`embeddings_fp16_vs_fp32_cos`, `day_windows_utc`, `snapshot_sha256`, `sample_manifest_sha256`, `bucket_allocation`, `code_sha_candidate`,
`ai_workspace_diff_since_b64da14`, 그리고 `frame`(`full` 또는 9~14개로 가는 `short_n`)과 라벨 마감(`label_deadline_utc`). 종료 코드 3
(`void_frame_too_small`, 5개 창으로도 eval 9개 미만)이면 void(프레임 부족)를 A8.10에 적고 10.1부터 다시, 4(`export_too_narrow`)면 `--days 5`로
다시 반출한다. 이 값과 `colab_cu_used`,
`adr_0023_amendment_commit`, 사용자 결정 두 개(`newsletter_embedder`, `label_subset`)를 yaml `pre_run_record`와 A8.10에 적어 **문서 전용 PR**
(yaml·매니페스트·ADR만)로 main에 넣는다. 그 PR이 병합되기 전에는 생성 호출을 하지 않는다. 시각 값(`export_t0_kst`, `export_body_expires_at`,
`day_windows_utc`의 각 끝점)은 출력 그대로 **따옴표로 감싼 문자열**로 적는다 - 따옴표가 없으면 yaml이 날짜로 읽어 러너가 시작하지 않는다
(`yaml.safe_dump`로 옮기면 알아서 감싼다).

### 10.4 실행 (pre-flight → warmup 2 → eval 15, 상한 $1.00, 누적 $1.50, 벽시계 2시간)
1. 실행 직전에 선불 잔액(AI Studio 결제 화면, ₩)과 읽은 시각을 적는다. `PYTHONPATH=ai_workspace python -m core.llm.spend_cli summary`로 지출
   원장(ADR 0035)이 멀쩡한지 본다. 2026-10-17 현재 메인 체크아웃에는 원장이 없다 - 첫 유료 실행 전에 사람이 `spend_cli init`으로 만든다(가드는
   원장을 만들지 않는다). 러너가 다시 확인한다 - 원장이 없거나 전체 상한 여유가 $2.00 미만이면 시작하지 않는다.
2. 메인 체크아웃을 실행 전 기록 PR이 병합된 main으로 맞춘다(`git checkout --detach origin/main`, 깨끗한 트리). `GEMINI_API_KEY`가 환경에 있다.
3. ```bash
   caffeinate -i nice -n 19 env OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 PYTHONPATH=ai_workspace \
     python -m evaluation.llm.e0_baseline run --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
   ```
   화면에는 `[e0] eval 3 처리 · 누적 $0.0123` 같은 줄만 나온다. 시작 검사가 하나라도 실패하면 사유 목록과 종료 코드 2로 끝나고 아무것도
   만들지 않는다. 기록은 `data/experiments/e0/<run>/`(저장소 밖)이다. `run`에는 사전 등록·매니페스트 경로를 바꾸는 옵션이 없다 - 러너는
   등록한 경로의 두 파일을 읽고, 그 내용이 `git show HEAD:<경로>`와 같지 않으면(커밋하지 않은 사본·고친 값) 시작하지 않는다(A8.2 e).
4. 프로세스가 죽었을 때만(절전·크래시·네트워크·운영자 중지) 같은 명령에 `--resume <run> --resume-reason "<사유>"`를 붙여 다시 돌린다. 돌던
   클러스터는 `interrupted`로 닫히고 다시 돌지 않는다. 예산·인프라로 멈춘 run은 이어 돌지 않는다(부분 실행이거나 void다 - A8.5·A8.6).

### 10.5 리포트와 라벨
1. 마지막 호출 24시간 뒤 잔액을 다시 읽는다. 그 사이 다른 Gemini 호출이 있었는지 적는다.
2. ```bash
   PYTHONPATH=ai_workspace python -m evaluation.llm.e0_report --run data/experiments/e0/<run> --export-dir data/exports/<T0> \
     --balance-before <₩> --balance-before-at <시각> --balance-after <₩> --balance-after-at <시각> --billing-fx 1400 --other-gemini-calls no
   ```
   `reports/llm/e0_baseline_v1.{json,md}`를 쓴다(텍스트 조각이 있으면 쓰지 않는다). 커밋하면서 `reports/README.md`의 예약 행을 링크로 바꾸고,
   ADR 0010 증거에 첫 실제 차단율을 날짜와 함께 덧붙인다(A8.7).
3. ```bash
   PYTHONPATH=ai_workspace python -m evaluation.llm.e0_labels export-blind --run data/experiments/e0/<run> --export-dir data/exports/<T0>
   PYTHONPATH=ai_workspace python -m evaluation.llm.labeling_app --run e0-baseline-v1-<run>    # 127.0.0.1:8765, 밤 세션 ≤1h
   ```
   클러스터 라벨을 먼저 모두 단다. 재라벨은 48시간 뒤 `?round=2`. 라벨 마감(retention.json) 뒤에는 UI가 열리지 않고 본문이 지워진다.
4. 라벨이 끝나면 2의 명령에 `--labels-dir data/labels/e0-baseline-v1-<run> --label-hours <실측>`을 더해 리포트를 다시 만든다.
5. 라벨 리포트를 커밋하면 반출본의 본문을 만료 전이라도 지운다: `python scripts/export_news_raw.py purge --dir data/exports/<T0> --everything`.

