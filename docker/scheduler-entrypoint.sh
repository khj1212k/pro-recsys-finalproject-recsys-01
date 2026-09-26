#!/bin/sh
# scheduler 서비스 진입점: supercronic을 띄우고, 컨테이너 종료 신호(SIGTERM/SIGINT)를 실행 중인 잡에도 전달한다.
#
# supercronic은 잡마다 새 프로세스 그룹을 만들고(Setpgid), SIGTERM을 받으면 잡에 신호를 보내지 않고
# 끝나기만 기다린다 - 그래서 `docker compose stop scheduler`나 재배포 때 잡은 유예 시간이 지나 SIGKILL로
# 죽고 job_runs에는 'abandoned'로만 남았다(tini -g도 supercronic의 그룹까지만 닿는다).
# 여기서 supercronic의 자식(잡 셸)마다 그 프로세스 그룹 전체에 신호를 보내면, jobs.run이 JobTerminated로
# 받아 'failed (terminated by SIGTERM)'과 그때까지의 stats를 기록하고 advisory lock을 푼다.
set -u

supercronic "$@" &
SUPERCRONIC_PID=$!

forward() {
  sig=$1
  for stat_file in /proc/[0-9]*/stat; do
    stat=$(cat "$stat_file" 2>/dev/null) || continue
    # /proc/<pid>/stat: pid (comm) state ppid pgrp ... - comm에 공백이 있을 수 있어 ')' 뒤부터 자른다.
    # shellcheck disable=SC2086
    set -- ${stat##*) }
    [ "${2:-}" = "$SUPERCRONIC_PID" ] && kill "-$sig" "-$3" 2>/dev/null
  done
  kill "-$sig" "$SUPERCRONIC_PID" 2>/dev/null
}

trap 'forward TERM' TERM
trap 'forward INT' INT

# 신호를 받으면 wait가 트랩 처리 후 바로 돌아오므로, supercronic이 잡을 모두 기다리고 끝날 때까지 다시 기다린다.
wait "$SUPERCRONIC_PID"
status=$?
while kill -0 "$SUPERCRONIC_PID" 2>/dev/null; do
  wait "$SUPERCRONIC_PID"
  status=$?
done
exit "$status"
