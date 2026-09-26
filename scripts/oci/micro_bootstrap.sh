#!/usr/bin/env bash
# Tier 0 호스트(OCI Always Free VM.Standard.E2.1.Micro, 1 GB, Canonical Ubuntu 24.04 x86_64) 초기 설정.
# 결정과 측정 근거: docs/adr/0026-hosting-tiers-and-contingency.md, 절차: docs/runbook-hosting.md
#
#   scp scripts/oci/micro_bootstrap.sh ubuntu@<ip>:
#   ssh ubuntu@<ip> 'sudo bash micro_bootstrap.sh'
#
# 여러 번 실행해도 같은 상태로 수렴한다(idempotent). 하는 일:
#   1) 2 GB 스왑   2) 패키지 갱신 + fail2ban·unattended-upgrades·Docker
#   3) 방화벽: 인바운드는 SSH(22)만 - UFW 대신 OCI 이미지의 iptables 규칙을 유지·보강
#   4) 안 쓰는 상주 서비스 정지   5) sshd 조이기   6) 로그 상한
#
# UFW를 쓰지 않는 이유: OCI 공식 문서가 Ubuntu 이미지에서 UFW로 규칙을 고치면 부팅이 안 될 수 있다고
# 경고한다(iSCSI 부트 볼륨용 필수 규칙이 iptables에 들어 있음). 이미지 기본 IPv4 규칙이 이미
# "22/tcp 신규 연결만 허용, 나머지 REJECT"이므로 그대로 두고, 비어 있는 IPv6 규칙만 같은 정책으로 채운다.
set -euo pipefail

SWAP_FILE=${SWAP_FILE:-/swapfile}
SWAP_SIZE_MB=${SWAP_SIZE_MB:-2048}
SWAPPINESS=${SWAPPINESS:-10}
ADMIN_USER=${ADMIN_USER:-ubuntu}
# 자동 보안 업데이트 후 재부팅이 필요하면 이 시각(UTC)에 재부팅. 18:40 UTC = 03:40 KST,
# 매시 5분 ingest 실행과 겹치지 않는 시각.
REBOOT_TIME_UTC=${REBOOT_TIME_UTC:-18:40}
export DEBIAN_FRONTEND=noninteractive

log() { printf '[bootstrap] %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "root로 실행하세요: sudo bash $0" >&2
  exit 1
fi

# --- 1) 스왑: 1 GB 메모리에서 apt/도커 이미지 풀·추출 피크를 흡수한다 ---------------------------
if ! swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
  if [ ! -f "$SWAP_FILE" ]; then
    log "스왑 파일 생성 ${SWAP_SIZE_MB} MB"
    fallocate -l "${SWAP_SIZE_MB}M" "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
  fi
  swapon "$SWAP_FILE"
fi
grep -q "^${SWAP_FILE} " /etc/fstab || echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
cat > /etc/sysctl.d/60-newsletter-micro.conf <<EOF
# 스왑은 비상용: 상주 프로세스는 RAM에 두고, 순간 피크만 스왑으로 넘긴다.
vm.swappiness=${SWAPPINESS}
vm.vfs_cache_pressure=50
EOF
sysctl -q --system

# --- 2) 패키지 --------------------------------------------------------------------------------
log "패키지 갱신"
apt-get update -q
apt-get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade -y -q
# docker.io/docker-compose-v2는 Ubuntu 아카이브 패키지 - 외부 apt 저장소를 추가하지 않아
# unattended-upgrades의 보안 업데이트 대상에 그대로 들어간다.
apt-get install -y -q --no-install-recommends \
  fail2ban unattended-upgrades iptables-persistent docker.io docker-compose-v2

# --- 3) 방화벽 -------------------------------------------------------------------------------
# IPv4: 이미지 기본 규칙(22/tcp만 허용 + REJECT)이 있는지 확인만 한다. 없으면 멈춘다 - 사람이 봐야 한다.
if ! iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT 2>/dev/null \
   || ! iptables -S INPUT | grep -q -- '-A INPUT -j REJECT'; then
  echo "예상한 OCI 기본 IPv4 규칙(22 허용 + REJECT)이 없습니다. /etc/iptables/rules.v4를 확인하세요." >&2
  exit 1
fi
# IPv6: 서브넷에 IPv6가 없어도 나중에 켜질 때를 대비해 같은 정책을 둔다.
cat > /etc/iptables/rules.v6 <<'EOF'
# newsletter-recsys Tier 0: 인바운드는 SSH(22)만 (scripts/oci/micro_bootstrap.sh가 관리)
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]
-A INPUT -i lo -j ACCEPT
-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A INPUT -p ipv6-icmp -j ACCEPT
-A INPUT -p tcp -m conntrack --ctstate NEW -m tcp --dport 22 -j ACCEPT
COMMIT
EOF
ip6tables-restore < /etc/iptables/rules.v6
# rules.v4는 건드리지 않는다. `netfilter-persistent save`도 하지 않는다 - Docker가 실행 중일 때
# 저장하면 Docker가 만든 체인까지 파일에 박혀 다음 부팅에서 꼬인다.

# --- 4) 안 쓰는 상주 서비스 정지(공격 면 + 메모리) ----------------------------------------------
# rpcbind(0.0.0.0:111, NFS용), ModemManager(모뎀), udisks2(데스크톱 디스크 관리). multipathd/iscsid는
# 부트·블록 볼륨 경로라 건드리지 않고, oracle-cloud-agent(snap)는 CPU 지표 수집에 필요해 유지한다.
for unit in rpcbind.socket rpcbind.service ModemManager.service udisks2.service; do
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
  fi
done

# --- 5) sshd -------------------------------------------------------------------------------
# sshd_config.d는 먼저 읽힌 값이 이긴다: 10-이 이미지의 60-cloudimg-settings.conf보다 앞선다.
cat > /etc/ssh/sshd_config.d/10-newsletter-hardening.conf <<EOF
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
MaxAuthTries 3
AllowUsers ${ADMIN_USER}
EOF
sshd -t
systemctl reload ssh

# fail2ban: Ubuntu 기본값이 sshd jail을 켜 둔다. 반복 시도 차단 강도만 조정.
cat > /etc/fail2ban/jail.d/10-newsletter-sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban

# --- unattended-upgrades: 보안 업데이트 자동 적용 + 필요 시 새벽 재부팅 ----------------------------
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
cat > /etc/apt/apt.conf.d/52newsletter-unattended <<EOF
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "${REBOOT_TIME_UTC}";
EOF
systemctl enable --now unattended-upgrades >/dev/null

# --- 6) 로그·Docker 설정 -------------------------------------------------------------------------
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/60-newsletter.conf <<'EOF'
[Journal]
SystemMaxUse=200M
EOF
systemctl restart systemd-journald

mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"},
  "live-restore": true
}
EOF
systemctl enable docker >/dev/null
systemctl restart docker

log "완료: $(free -m | awk '/^Mem:/{print "mem total " $2 " MiB, available " $7 " MiB"}'), swap $(free -m | awk '/^Swap:/{print $2}') MiB"
