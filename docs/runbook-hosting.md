# 호스팅 런북 — 개발(Mac) · Tier 0(OCI E2.1.Micro) · Tier 1(OCI A1)

계층을 이렇게 나눈 이유와 측정 근거는 [ADR 0026](adr/0026-hosting-tiers-and-contingency.md)을 본다.
compose 스택 자체의 상태 확인·멈추기·백업은 `docs/runbook.md`(런타임 compose 브랜치)를 따르고,
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
3. Mac 덤프를 복원한다(Alembic 리비전까지 덤프에 들어 있다 — Tier 0와 같은 `d48994e9d26e`인지 확인):

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
6. A1 스케줄러를 켠다(`sudo docker compose up -d`). 첫 embed가 병합된 ok 행의 임베딩을 채운다
   (`SELECT count(*) FROM news_raw WHERE raw_news_extract_status='ok' AND embedding_result IS NULL`이 0으로 수렴).
7. A1의 첫 ingest가 끝난 뒤 4~5를 한 번 더 한다 — 그 사이 Tier 0만 받은 행이 옮겨지고, 이미 옮긴 행은 0행이다(멱등).
   끝나면 두 VM의 CSV를 지운다.
8. 7일 소크(잡 성공률 ≥95%, OOM 0, 수집 지연 p95 ≤3h) 동안 Tier 0 수집은 끄지 않는다(섀도로 되돌림).
   소크를 통과하면 A1을 기록 시스템으로 선언하고 ADR 0026을 갱신한다. 블록 볼륨 합계(A1 100 GB + Micro 50 GB
   = 150 GB)는 200 GB 안이다.

### 6.5 Tier 0 호스트 밖 백업 (Mac이 당겨 옴)

Tier 0가 유일한 수집기인 동안의 유일한 호스트 밖 사본이다. 자동화는 Mac에서 상주 작업을 돌리지 않기로 한
동안 보류이므로 사람이 돌린다(가벼운 작업: 2026-09-26 22:18 기준 1.1 MB, 몇 초).

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
