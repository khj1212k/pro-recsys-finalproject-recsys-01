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

| 작업 | 개발(Mac) | Tier 0 (E2.1.Micro, 1 GB) | Tier 1 (A1, 2 OCPU/12 GB) |
|---|---|---|---|
| Postgres + pgvector | 꺼 둠 — 16:59 KST 덤프만 보관 | O — **유일한 수집 DB**(임시 단독 수집) | O (확보 시 기록 시스템) |
| `ingest --stages rss,extract` 매시 | 꺼 둠 | O (추출 워커 2개) | O |
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
(`docker/ingest.Dockerfile`, torch 없음), `docker/crontab.micro`(ingest 추출 워커 2개·popularity·daily_report),
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

더 새로운 커밋으로 다시 배포하는 절차는 아직 돌려 보지 않았다(2026-10-06 현재 VM은 09-26 스냅숏 그대로다).
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

대응(ADR 0026 결정 6):
1. Tier 0는 2026-09-26 17:00 KST부터 유일한 수집기다. 회수(삭제일 수도 있음)되면 마지막 호스트 밖 백업 이후를
   잃는다 - 종료 코드가 2나 3이면 먼저 백업을 당겨 온다(6.5). 2026-09-26 22:14 실측으로 이미 3(기준상 유휴)이다.
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

compose 서비스는 `restart: unless-stopped`라 부팅 후 스스로 올라온다. `job_runs`에 공백 시간대가 생기는데,
다음 ingest가 따라잡을 수 있는 공백은 **피드마다 다르다**(6.3).

재부팅에서는 확인됐다: 2026-10-03 03:40 KST에 unattended-upgrades가 커널 업데이트로 예약 재부팅을 했고,
저널 공백 25초 뒤 db·scheduler가 스스로 떠서 04:05 회차가 정시에 시작됐다(ADR 0026 증거 8). 회수로 인한
STOPPED에서 START로 되살리는 경로는 아직 겪어 보지 않았다. 메모리 표본 수집(`memsample.sh` → `~/mem_soak.csv`)은
서비스가 아니라서 재부팅하면 멈춘다 - 소크를 이어 가려면 다시 띄운다.

### 6.2 인스턴스가 사라짐 (TERMINATED)

2~4절을 다시 한다. 2026-09-26 실측: 생성→RUNNING 약 30초, 초기 설정 290초, 이미지 빌드 121초,
빈 DB 첫 ingest 155초 — 합계 약 10분. 지금은 Tier 0가 유일한 수집기이므로, 4절에서 스택을 올릴 때
스케줄러보다 먼저 마지막 백업(6.5)을 되살린다:

```bash
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose up -d db'
scp -F .ops/micro/ssh_config data/backups/<마지막 tier0 덤프> micro:tier0.dump
ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose cp ~/tier0.dump db:/tmp/tier0.dump \
  && sudo docker compose exec -T db pg_restore -U newsletter -d newsletter --clean --if-exists --no-owner \
     --exit-on-error /tmp/tier0.dump && sudo docker compose up -d'
```

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

1. `scripts/oci/micro_bootstrap.sh`를 그대로 실행한다(아키텍처 무관). 12 GB에서도 스왑 2 GB는 모델 로드 피크 흡수용으로 둔다.
2. 코드 스냅숏을 보내고 `.env`는 `COMPOSE_FILE`을 기본값(`docker-compose.yml`만)으로 둔다 - 전체 worker 이미지를
   arm64로 직접 빌드한다. `sudo docker compose up -d db`로 **DB만** 올린다(스케줄러는 아직 끈다).
3. Mac 덤프를 복원한다(Alembic 리비전까지 덤프에 들어 있다 — Tier 0와 같은 `d48994e9d26e`인지 확인).
   배포하는 코드의 head가 그보다 뒤여도(2026-10-06 기준 `8b7f830013b7`) 여기서는 올리지 않는다 - 6단계의
   `up -d`에서 `migrate`가 올린다. `8b7f830013b7`은 `news_raw`·`press`를 바꾸지 않아 5단계 병합에 영향이 없다:

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
     n.raw_news_extracted_at, n.raw_news_extract_attempts, n.raw_news_content_sha256 FROM news_raw n \
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
6. A1 스케줄러를 켠다(`sudo docker compose up -d`). 첫 embed가 병합된 ok 행의 임베딩을 채운다
   (`SELECT count(*) FROM news_raw WHERE raw_news_extract_status='ok' AND embedding_result IS NULL`이 0으로 수렴).
   2026-10-06 기준 Tier 0의 ok 행은 12,823건이라 채울 임베딩이 약 1.3만 건이다. embed 잡은 회차마다 시간
   예산(`--time-budget-s 2400`)만큼만 돌므로 여러 회차에 걸친다 - A1 CPU 처리량은 재 본 적이 없다(ADR 0006).
7. A1의 첫 ingest가 끝난 뒤 4~5를 한 번 더 한다 — 그 사이 Tier 0만 받은 행이 옮겨지고, 이미 옮긴 행은 0행이다(멱등).
   끝나면 두 VM의 CSV를 지운다.
8. 7일 소크(잡 성공률 ≥95%, OOM 0, 수집 지연 p95 ≤3h) 동안 Tier 0 수집은 끄지 않는다(섀도로 되돌림).
   소크를 통과하면 A1을 기록 시스템으로 선언하고 ADR 0026을 갱신한다. 블록 볼륨 합계(A1 100 GB + Micro 50 GB
   = 150 GB)는 200 GB 안이다.

### 6.5 Tier 0 호스트 밖 백업 (Mac이 당겨 옴)

Tier 0가 유일한 수집기인 동안의 유일한 호스트 밖 사본이다. 자동화는 Mac에서 상주 작업을 돌리지 않기로 한
동안 보류이므로 사람이 돌린다(가벼운 작업: 2026-09-26 22:18 기준 1.1 MB, 몇 초. 2026-10-06에는 DB가 49 MB라
덤프도 그만큼 커진다). **2026-10-06 현재 마지막 백업은 09-26 22:18이다** - 주기를 정하지 않은 채 10일이 지났다
(ADR 0026 결정 6의 재검토 항목).

```bash
( umask 077; ssh -F .ops/micro/ssh_config micro \
    'cd ~/newsletter-recsys && sudo docker compose exec -T db pg_dump -U newsletter -d newsletter -Fc' \
    > data/backups/tier0-micro-$(date +%Y-%m-%dT%H%M).dump )
# 서버 없이 확인: 목차가 읽히고 news_raw 행 수가 VM의 count(*)와 같은지
pg_restore -l data/backups/tier0-micro-<시각>.dump | grep -c "TABLE DATA"
pg_restore -a -t news_raw -f - data/backups/tier0-micro-<시각>.dump | awk '/^COPY/{f=1;next} /^\\\./{f=0} f{n++} END{print n}'
```

`data/`는 gitignore다. 이 확인은 "덤프가 온전히 읽힌다"까지이고, 실제 복원 검증(일회용 DB에 `pg_restore`)은
승격 조건 (b)에 따라 따로 한다 — 같은 형식의 Mac 덤프는 6.4 리허설에서 복원됐다.

**Mac 덤프의 두 번째 사본.** `data/backups/mac-compose-2026-09-26.dump`(899행·임베딩 845개, 5.9 MB)는 기록
시스템의 절반인데 Mac 디스크에 한 부뿐이다. 한 부를 Mac 밖에 더 둔다(2026-10-06 현재 하지 않았다):

```bash
shasum -a 256 data/backups/mac-compose-2026-09-26.dump          # 해시를 .ops/micro/README.txt에 적어 둔다
ssh -F .ops/micro/ssh_config micro 'umask 077; mkdir -p ~/backups'
scp -F .ops/micro/ssh_config data/backups/mac-compose-2026-09-26.dump micro:backups/
ssh -F .ops/micro/ssh_config micro 'chmod 600 ~/backups/*.dump; sha256sum ~/backups/mac-compose-2026-09-26.dump'  # 같은 해시인지
```

Tier 0 자체가 회수될 수 있으므로 이것만으로 충분하지는 않다 - 외장 디스크 등 세 번째 위치가 있으면 거기에도 둔다.
6.4의 리허설처럼 VM에 올린 덤프를 지우는 절차를 돌릴 때는 `~/backups/`는 남긴다.

**덤프에도 본문 30일이 걸린다**([ADR 0023](adr/0023-data-sources-copyright-retention.md) 개정 2026-10-06). 덤프는 DB 전체의 본문을
담고 행 단위로 지울 수 없으므로, **덤프 안에서 가장 오래된 본문의 수집 시각 + 30일**에 파일을 통째로 지운다.

- 만료일을 파일 이름에 적는다. 덤프를 뜨기 직전에 가장 오래된 본문의 수집 시각을 읽는다(건수·시각만 읽는 조회다):

  ```bash
  ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -At -U newsletter -d newsletter \
    -c \"SELECT (min(n.raw_news_crawled_at) + interval '30 days')::date FROM news_raw n JOIN press p USING (press_id) \
    WHERE n.raw_news_content <> '' AND p.press_name <> '정책브리핑'\""
  # 그 날짜를 넣어: data/backups/tier0-micro-<시각>.expires-<YYYY-MM-DD>.dump
  ```
- 최신 1개만 둔다. 새 덤프가 위의 확인(목차, 행 수)을 통과하면 앞의 덤프를 지운다. 권한은 0600(`umask 077`).
- 두 번째 사본(위의 VM `~/backups/`, 외장 디스크)도 같은 만료일에 지운다. 사본을 만들 때 만료일을 `.ops/micro/README.txt`에 해시와 함께 적는다.
- 지금 있는 두 개: `mac-compose-2026-09-26.dump`는 **2026-10-25**, `tier0-micro-2026-09-26T2218.dump`는 **2026-10-26**이 만료일이다.
  2026-10-06 현재 둘 다 그대로 있고 `mac-compose-…dump`의 권한은 0644다(`chmod 600`). Mac 덤프의 임베딩 845개는 본문 없이 따로
  남길 수 있다(임베딩은 보존 대상이 아니다) - 남길지와 지우는 날은 사용자 결정이다.
- VM의 본문 보존 잡이 아직 없어서(ADR 0023 TODO) VM에서 뜨는 덤프는 언제 떠도 2026-10-26에 만료된다. 보존 잡이 날마다 돌기 시작하면
  전체 덤프는 뜬 지 하루 안에 만료된다 - 그때의 백업 주기와 방식은 정해지지 않았다(ADR 0023 개정의 "남는 위험", ADR 0026 결정 6).
- 덤프를 복원한 DB에는 만료된 본문이 되살아날 수 있다. 복원한 뒤에는 보존 잡을 먼저 돌린다(잡이 생긴 뒤).

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

하루 `runs`가 24보다 적으면 그만큼 건너뛴 것이다. 지금까지 긴 회차는 동아일보 요청 실패와 같이 나타났다
(fetch_failed 343건 중 339건이 동아일보, 원인 미확정). 손볼 수 있는 곳은 추출 요청의 시간 제한·재시도 횟수와
언론사별 격리인데, 아직 바꾸지 않았다 - 바꾸면 ADR 0026을 갱신한다.

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
- **언제**: 매시 :20~:50(정시 ingest 사이). 도구가 분을 확인하고 그 밖이면 거절한다.

### 9.1 창 정하기
E0의 창은 T0 직전에 완결된 KST 날짜들의 06:00 KST를 의사 시각으로 하는 24시간 창이다(ADR 0009 A8.1). 손으로 계산하지 않는다:

```bash
python scripts/export_news_raw.py plan-window --t0-kst "$(date +%Y-%m-%dT%H:%M)" --days 3   # 후보가 모자라면 5 (A8.1의 연장)
# start_utc, end_utc를 아래에 넣는다. day_windows_utc는 러너가 쓰는 창과 같은 함수의 출력이다
```

창은 최대 120시간이다. 그보다 길면 도구가 거절한다.

### 9.2 VM 상태 확인 (읽기만)

```bash
ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -At -U newsletter -d newsletter \
  -c \"SELECT job, status, started_at FROM job_runs WHERE status = 'running'\" \
  -c \"SELECT count(*) FROM news_raw n WHERE n.raw_news_extract_status = 'ok' AND n.raw_news_content <> '' \
       AND n.raw_news_crawled_at >= TIMESTAMP '<start_utc>' AND n.raw_news_crawled_at < TIMESTAMP '<end_utc>'\" \
  -c \"SELECT count(*), count(*) FILTER (WHERE news_letter_id IS NOT NULL) FROM news_raw\""
ssh -F .ops/micro/ssh_config micro 'grep MemAvailable /proc/meminfo'
```

- 돌고 있는 `ingest`가 있으면(1시간을 넘긴 회차, 6.6) 끝날 때까지 기다린다.
- MemAvailable이 400 MiB 아래면 다음 시간대로 미룬다. 근거: 10일 관측의 p5가 389 MiB, 중앙값이 629 MiB다(ADR 0026 증거 8) - 평소보다
  낮은 상태에 새 부하를 얹지 않는다는 뜻이다. 반출이 실제로 쓰는 메모리는 아직 재지 않았다.
- 둘째 줄의 건수가 반출될 행 수다. 2026-10-06 기준 3개 창 2,595행, 5개 창 5,633행(행당 평균 4.2 KB로 약 11 MB / 23 MB 추정).

### 9.3 반출

다른 터미널에서 메모리를 지켜본다(첫 실행의 증거가 된다):

```bash
ssh -F .ops/micro/ssh_config micro 'for i in $(seq 1 40); do date +%T; grep -E "MemAvailable|SwapFree" /proc/meminfo; sleep 3; done'
```

```bash
time python scripts/export_news_raw.py export --start-utc <start_utc> --end-utc <end_utc> \
  --psql-command "ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter'"
```

- 도구가 psql의 표준입력으로 보내는 것은 스크립트 하나다: `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` →
  `SET LOCAL statement_timeout = '120s'`(락 5초, 유휴 60초) → `COPY ... TO STDOUT` 세 번 → `ROLLBACK`. DB 비밀번호는 쓰지 않는다
  (컨테이너 안의 로컬 접속). VM에는 파일을 만들지 않는다.
- 성공하면 요약 JSON이 나온다: `dir`, `identity_sha256`, `rows`, `window_utc`, `snapshot_at_utc`(= T0), `alembic_revision`, `code_sha`,
  `code_dirty`, `by_press`, `db_hash_check`, `body_expires_at_utc`, `articles_sha256`. **본문·제목·URL은 출력되지 않는다.**
  파일은 `<메인 체크아웃>/data/exports/<T0>/`에 `articles.jsonl`(본문)과 `manifest.json`으로 놓인다(디렉터리 0700, 파일 0600).
- 실패하면 아무것도 쓰지 않는다.

  | 메시지 | 뜻 | 할 일 |
  |---|---|---|
  | `:20~:50 밖이다` | 정시 ingest와 겹칠 수 있다 | 기다렸다가 다시 |
  | `psql 명령이 종료 코드 …` + `statement timeout` | 한 문장이 120초를 넘겼다 | VM 상태(9.2)를 다시 보고, 그래도 넘으면 `--statement-timeout-s`를 올리되 값을 ADR 0036에 적는다 |
  | `꼬리말이 없다` / `행 수가 맞지 않는다` | 출력이 중간에 끊겼다(SSH) | 다시 돌린다. 읽기만 했으므로 VM에 남은 것은 없다 |
  | `읽기 전용 스냅숏 … 확인해 주지 않았다` | 서버가 읽기 전용 트랜잭션임을 돌려주지 않았다 | 돌리지 않는다. psql 명령이 스크립트를 그대로 전달하는지 조사 |
  | `git이 추적할 수 있는 곳이다` / `data/exports 아래에만 둔다` | 쓰는 곳이 허용 위치가 아니다 | 메인 체크아웃에서 `--root` 없이 돌린다 |
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
- `identity_sha256`, `rows`, `snapshot_at_utc`, `body_expires_at_utc`를 실험의 실행 전 기록(E0이면 ADR 0009 A8.10과 yaml의
  `pre_run_record`)에 옮겨 적고, 만료일을 `.ops/RUNNING.md`에도 적는다.

### 9.5 임베딩 팩 대조
임베딩 잡(Colab)은 다음 PR이다. 잡이 돌려준 팩을 `data/exports/<T0>/embeddings/`에 받은 뒤:

```bash
python scripts/import_embeddings.py --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
```

통과하면 행 수·차원·dtype·벡터 파일 sha256이 나온다(실행 전 기록의 `embeddings_sha256`). 어긋나면 사유 코드(`ids`, `content_sha256`,
`dim`, `norm`, `export_identity`, `model`, `file_sha256` …)와 함께 종료 코드 2로 끝난다 - 그 팩은 쓰지 않는다.

### 9.6 정리 — 본문 30일
- 본문 만료는 행마다 `crawled_at` + 30일이다. 요약의 `body_expires_at_utc`가 가장 이른 만료, `last_body_expiry_utc`가 마지막 만료다.
- 반출본을 여는 도구(`verify`, `import_embeddings.py`, 실험 러너)는 열 때마다 만료된 본문을 디스크에서 지운다. 만료된 본문은 어떤 경우에도
  도구 밖으로 나오지 않는다. 해시·길이·제목·URL과 `identity_sha256`은 남는다.
- 아무 도구도 돌리지 않는 날을 위해, 가장 이른 만료일에 사람이 돌린다:

  ```bash
  python scripts/export_news_raw.py purge --all                              # data/exports 아래 전부, 만료된 본문만
  python scripts/export_news_raw.py purge --dir data/exports/<T0> --everything   # 할 일이 끝났으면 만료 전이라도 전부
  ```

  출력의 `rows_with_body`가 남은 본문 수다. `--everything` 뒤에는 0이어야 한다. `purge --all`이 종료 코드 1이면 읽을 수 없는 반출본이
  있다는 뜻이다(출력의 `error`) - 그 디렉터리는 본문이 남아 있을 수 있으니 직접 확인하고 지운다.
- 디렉터리를 통째로 지우지 않고 본문만 지우는 이유: 매니페스트와 해시가 남아야 실험의 실행 전 기록에 적은 sha256을 나중에도 대조할 수 있다.
- 한 실험이 끝나면(라벨 리포트가 커밋되면) 만료 전이라도 그 반출본의 본문을 지운다.
- 지우는 것은 파일의 내용이다. 저장 장치 수준의 복구 불가능성은 보장하지 않는다(ADR 0023 개정 "남는 위험").

