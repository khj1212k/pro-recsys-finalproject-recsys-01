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
  interrupted=1
  sig=$1
  for stat_file in /proc/[0-9]*/stat; do
    # read는 내장 명령이라 fork하지 않는다(프로세스가 많으면 스캔이 길어진다).
    read -r stat < "$stat_file" 2>/dev/null || continue
    # /proc/<pid>/stat: pid (comm) state ppid pgrp ... - comm에 공백이 있을 수 있어 ')' 뒤부터 자른다.
    # shellcheck disable=SC2086
    set -- ${stat##*) }
    [ "${2:-}" = "$SUPERCRONIC_PID" ] && kill "-$sig" "-$3" 2>/dev/null
  done
  kill "-$sig" "$SUPERCRONIC_PID" 2>/dev/null
}

trap 'forward TERM' TERM
trap 'forward INT' INT

# 신호를 받으면 wait가 트랩 때문에 128+신호로 먼저 돌아온다. supercronic이 그 사이 이미 끝나 셸이
# 거둬 갔을 수 있으므로(kill -0으로는 알 수 없다) 트랩이 돌았으면 wait를 다시 불러 실제 종료 상태를 받는다.
while :; do
  interrupted=0
  wait "$SUPERCRONIC_PID"
  status=$?
  [ "$interrupted" = 1 ] || break
done
exit "$status"
