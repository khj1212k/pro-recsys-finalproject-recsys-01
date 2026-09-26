#!/bin/zsh
# Mac(Apple Silicon) 호스트에서 BGE-M3 임베딩을 MPS로 돌리는 launchd 에이전트 (ADR 0006).
#
# 컨테이너(colima) 안에서는 MPS를 쓸 수 없어서, Mac 개발 환경에서는 스케줄러의 embed 줄을
# 끄고(docker/compose.mac.yaml의 JOBS_DISABLED=embed) 이 에이전트가 같은 잡
# (`python -m jobs.run embed`)을 호스트에서 매시 돌린다. job_runs 기록과 advisory lock은
# 컨테이너 잡과 같은 DB를 쓰므로 동시에 떠도 겹쳐 돌지 않는다.
#
#   scripts/mac_embed_agent.sh setup       # .venv-jobs 생성(docker/requirements-worker.txt 잠금 그대로)
#   scripts/mac_embed_agent.sh run         # 한 번 실행 (포그라운드)
#   scripts/mac_embed_agent.sh install     # launchd 등록: 매시 20분 + 로그인 시
#   scripts/mac_embed_agent.sh status      # 등록 상태와 마지막 로그
#   scripts/mac_embed_agent.sh uninstall   # 등록 해제
#   scripts/mac_embed_agent.sh keep-awake install|uninstall   # 전원 연결 중 시스템 잠자기 방지(caffeinate -s)
#
# DB 접속 정보는 저장소 루트의 compose .env(POSTGRES_*, DB_HOST_PORT)에서 읽는다. 값은 출력하지 않는다.
# launchd 등록은 이 스크립트가 있는 체크아웃 경로를 가리킨다 - 운영은 일회성 워크트리가 아니라 전용 런타임
# 워크트리(~/Projects/newsletter-runtime)에서 setup/install 한다(docs/runbook.md 2절).
set -euo pipefail

REPO=${0:A:h:h}
LABEL=com.newsletter-recsys.embed
AWAKE_LABEL=com.newsletter-recsys.keep-awake
AGENTS_DIR=$HOME/Library/LaunchAgents
LOG_DIR=$HOME/Library/Logs/newsletter-recsys
PY=${EMBED_PYTHON:-$REPO/.venv-jobs/bin/python}
TIME_BUDGET_S=${EMBED_TIME_BUDGET_S:-2400}
UV=${UV:-$(command -v uv || echo /opt/homebrew/bin/uv)}

die() { print -u2 -- "error: $*"; exit 1; }

cmd_setup() {
  [[ -x $UV ]] || die "uv가 필요합니다 (brew install uv)"
  [[ -x $PY ]] || $UV venv --python 3.11 "${PY:h:h}" -q
  $UV pip install -q --python "$PY" -r "$REPO/docker/requirements-worker.txt"
  "$PY" -c 'import torch; assert torch.backends.mps.is_available(), "MPS 사용 불가"; print("torch", torch.__version__, "mps ok")'
}

cmd_run() {
  [[ -x $PY ]] || die "$PY 없음 - 먼저 '$0 setup'"
  [[ -f $REPO/.env ]] || die "$REPO/.env(compose 변수) 없음 - docs/runbook.md 2절"
  set -a; source "$REPO/.env"; set +a
  export DB_HOST=127.0.0.1 DB_PORT=${DB_HOST_PORT:-5433} DB_USER=$POSTGRES_USER \
         DB_PASSWORD=$POSTGRES_PASSWORD DB_NAME=$POSTGRES_DB DB_POOL_MIN=1 DB_POOL_MAX=2
  # 모델은 ~/.cache/huggingface에 이미 있다 - 매 실행 허브 조회를 하지 않는다.
  export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false TQDM_DISABLE=1 PYTHONUNBUFFERED=1
  export GIT_SHA=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)
  cd "$REPO"
  # caffeinate -i: 이 실행이 도는 동안만 유휴 잠자기를 막는다(끝나면 해제).
  exec /usr/bin/caffeinate -i "$PY" -m jobs.run embed --time-budget-s "$TIME_BUDGET_S" "$@"
}

write_plist() {  # $1=label $2=plist body(ProgramArguments 이후 키들)
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"
  cat > "$AGENTS_DIR/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
$2
</dict>
</plist>
EOF
  plutil -lint "$AGENTS_DIR/$1.plist" >/dev/null
}

bootout() { launchctl bootout "gui/$UID/$1" 2>/dev/null || true; }

cmd_install() {
  [[ -x $PY ]] || die "$PY 없음 - 먼저 '$0 setup'"
  bootout $LABEL
  # 잠든 동안 놓친 StartCalendarInterval 실행은 깨어난 뒤 한 번으로 합쳐져 실행된다(launchd 동작).
  write_plist $LABEL "  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>$REPO/scripts/mac_embed_agent.sh</string><string>run</string></array>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>20</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/embed.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/embed.log</string>"
  launchctl bootstrap "gui/$UID" "$AGENTS_DIR/$LABEL.plist"
  print "installed $LABEL (repo: $REPO, log: $LOG_DIR/embed.log)"
}

cmd_uninstall() {
  bootout $LABEL
  rm -f "$AGENTS_DIR/$LABEL.plist"
  print "uninstalled $LABEL"
}

cmd_status() {
  local top=$'^\t(state|runs|last exit code|pid) ='
  print "$LABEL:"; launchctl print "gui/$UID/$LABEL" 2>/dev/null | grep -E "$top" || print "  not loaded"
  print "$AWAKE_LABEL:"; launchctl print "gui/$UID/$AWAKE_LABEL" 2>/dev/null | grep -E "$top" || print "  not loaded"
  [[ -f $LOG_DIR/embed.log ]] && grep '^{"job"' "$LOG_DIR/embed.log" | tail -3 | cut -c1-300 || true
}

cmd_keep_awake() {
  case ${1:-} in
    install)
      bootout $AWAKE_LABEL
      # -s: 전원 어댑터가 연결돼 있을 때만 시스템 잠자기를 막는다(배터리에서는 효과 없음).
      write_plist $AWAKE_LABEL "  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-s</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>"
      launchctl bootstrap "gui/$UID" "$AGENTS_DIR/$AWAKE_LABEL.plist"
      print "installed $AWAKE_LABEL" ;;
    uninstall)
      bootout $AWAKE_LABEL
      rm -f "$AGENTS_DIR/$AWAKE_LABEL.plist"
      print "uninstalled $AWAKE_LABEL" ;;
    *) die "usage: $0 keep-awake install|uninstall" ;;
  esac
}

case ${1:-} in
  setup) cmd_setup ;;
  run) shift; cmd_run "$@" ;;
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  keep-awake) shift; cmd_keep_awake "$@" ;;
  *) die "usage: $0 setup|run|install|status|uninstall|keep-awake" ;;
esac
