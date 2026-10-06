#!/bin/sh
# 커밋된 tests/test_scheduler_entrypoint.py를 그대로 반복 실행한다.
#   red   : 엔트리포인트만 origin/main 것으로 바꿔서(잡에 먼저 신호) - 새 단언이 실패하는지
#   green : 이 체크아웃의 엔트리포인트로(supercronic에 먼저 신호) - 한 번도 실패하지 않는지
# 조건: 그대로 / 프로세스 1,000개를 더 띄운 상태 / CPU마다 바쁜 루프를 돌린 상태
set -u
PY=.venv/bin/python
ENTRY=docker/scheduler-entrypoint.sh
OUT="${RUNNER_TEMP:-/tmp}/pytest.out"

loop() {  # $1 = 횟수. 실패 수를 stdout으로, 처음 두 실패의 요약을 stderr로.
  fail=0
  i=0
  while [ "$i" -lt "$1" ]; do
    i=$((i + 1))
    if ! "$PY" -m pytest -q -p no:cacheprovider tests/test_scheduler_entrypoint.py > "$OUT" 2>&1; then
      fail=$((fail + 1))
      [ "$fail" -le 2 ] && grep -E '^E  |^FAILED|^Terminated|passed|failed' "$OUT" | head -6 >&2
    fi
  done
  echo "$fail"
}

run_conditions() {  # $1 = 이름, $2 = 횟수
  plain=$(loop "$2")
  pids=""
  n=0
  while [ "$n" -lt 1000 ]; do sleep 3600 & pids="$pids $!"; n=$((n + 1)); done
  procs=$(loop "$2")
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  wait 2>/dev/null
  pids=""
  n=0
  while [ "$n" -lt "$(nproc)" ]; do "$PY" -c 'while True: pass' & pids="$pids $!"; n=$((n + 1)); done
  load=$(loop "$2")
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  wait 2>/dev/null
  echo "RESULT $1: failed $plain/$2 (plain), $procs/$2 (+1000 procs), $load/$2 (cpu load)"
  total=$((plain + procs + load))
}

echo "blobs: $(git hash-object "$ENTRY") $ENTRY, $(git hash-object tests/test_scheduler_entrypoint.py) tests/test_scheduler_entrypoint.py"
"$PY" -m pytest -q -p no:cacheprovider tests/test_scheduler_entrypoint.py | tail -1

cp "$ENTRY" "${RUNNER_TEMP:-/tmp}/committed-entrypoint.sh"
git show origin/main:"$ENTRY" > "$ENTRY"
run_conditions "red   (committed test, origin/main entrypoint)" 60
cp "${RUNNER_TEMP:-/tmp}/committed-entrypoint.sh" "$ENTRY"
git diff --quiet -- "$ENTRY" || { echo "엔트리포인트를 되돌리지 못함" >&2; exit 2; }
run_conditions "green (committed test, committed entrypoint)" 150
[ "$total" -eq 0 ]
