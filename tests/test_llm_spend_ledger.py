# LLM 지출 원장(core/llm/spend_ledger.py) 단위 테스트 - docs/adr/0035.
#
# 원장은 호출 시도 1회를 "예약 -> 정산" 두 줄로 남기는 추가 전용 파일이다. 여기서는 상한 산술
# (예약 대 실제), 스레드·프로세스 동시성에서 상한을 넘지 않는 것, 예약과 정산 사이에 죽은
# 프로세스의 예약이 지출로 남는 것을 본다. 네트워크·LLM 호출은 없다.
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

AI_WORKSPACE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace")
sys.path.insert(0, AI_WORKSPACE)

from core.llm.spend_ledger import (  # noqa: E402
    CallKey,
    Caps,
    LedgerError,
    Refusal,
    Reservation,
    SpendLedger,
)

DAY = "2026-10-06"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _key(run_id="run-a", day=DAY, model="gemini-3.5-flash-lite", role="generator", purpose="newsletter_content_gen"):
    return CallKey(run_id=run_id, day=day, provider="gemini", model=model, role=role, purpose=purpose)


def _caps(run=10_000, day=10_000, total=10_000):
    return Caps(run_nusd=run, day_nusd=day, total_nusd=total)


def _ledger(tmp_path, **kwargs):
    return SpendLedger(tmp_path / "ledger.jsonl", **kwargs)


def _events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 상한 산술: 예약 대 실제
# ---------------------------------------------------------------------------

def test_reservation_counts_against_every_cap_until_it_is_settled(tmp_path):
    ledger = _ledger(tmp_path)

    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)

    assert isinstance(res, Reservation)
    totals = ledger.totals(run_id="run-a", day=DAY)
    assert totals.committed == {"total": 0, "day": 0, "run": 0}
    assert totals.open == {"total": 4_000, "day": 4_000, "run": 4_000}
    assert totals.open_count == 1


def test_settling_replaces_the_worst_case_reservation_with_the_actual_cost(tmp_path):
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)

    ledger.settle(res, nusd=1_500, outcome="ok", basis="actual")

    totals = ledger.totals(run_id="run-a", day=DAY)
    assert totals.committed == {"total": 1_500, "day": 1_500, "run": 1_500}
    assert totals.open == {"total": 0, "day": 0, "run": 0}


def test_freed_reservation_headroom_can_be_reserved_again(tmp_path):
    """예약 6,000이 실제 1,000으로 정산되면 남은 9,000 안에서 다음 호출이 들어간다."""
    ledger = _ledger(tmp_path)
    caps = _caps(run=10_000)
    first = ledger.reserve(caps=caps, key=_key(), nusd=6_000)
    assert isinstance(ledger.reserve(caps=caps, key=_key(), nusd=6_000), Refusal)  # 6,000 + 6,000 > 10,000

    ledger.settle(first, nusd=1_000, outcome="ok", basis="actual")

    assert isinstance(ledger.reserve(caps=caps, key=_key(), nusd=6_000), Reservation)


def test_request_that_exactly_fills_the_cap_is_allowed_and_one_more_unit_is_refused(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=10_000, day=50_000, total=50_000)
    assert isinstance(ledger.reserve(caps=caps, key=_key(), nusd=10_000), Reservation)

    refusal = ledger.reserve(caps=caps, key=_key(), nusd=1)

    assert isinstance(refusal, Refusal)
    assert (refusal.scope, refusal.cap_nusd, refusal.open_nusd, refusal.requested_nusd) == ("run", 10_000, 10_000, 1)


def test_refusal_reports_committed_and_open_amounts_separately(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=10_000)
    settled = ledger.reserve(caps=caps, key=_key(), nusd=3_000)
    ledger.settle(settled, nusd=2_000, outcome="ok", basis="actual")
    ledger.reserve(caps=caps, key=_key(), nusd=5_000)

    refusal = ledger.reserve(caps=caps, key=_key(), nusd=4_000)

    assert isinstance(refusal, Refusal)
    assert (refusal.committed_nusd, refusal.open_nusd, refusal.used_nusd) == (2_000, 5_000, 7_000)


def test_actual_cost_above_the_reservation_is_charged_in_full(tmp_path):
    """프로바이더가 예약보다 많이 보고하면(추정이 틀린 경우) 실제 값을 그대로 잡는다."""
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=1_000)

    ledger.settle(res, nusd=1_700, outcome="ok", basis="actual")

    assert ledger.totals(run_id="run-a", day=DAY).committed["total"] == 1_700


def test_settling_the_same_reservation_twice_does_not_double_count(tmp_path):
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)

    ledger.settle(res, nusd=1_500, outcome="ok", basis="actual")
    ledger.settle(res, nusd=1_500, outcome="ok", basis="actual")

    assert ledger.totals(run_id="run-a", day=DAY).committed["total"] == 1_500


def test_zero_cost_settlement_releases_the_whole_reservation(tmp_path):
    """HTTP 오류처럼 과금되지 않는 시도는 0으로 정산한다."""
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)

    ledger.settle(res, nusd=0, outcome="http_503", basis="not_billed")

    totals = ledger.totals(run_id="run-a", day=DAY)
    assert totals.used("total") == 0 and totals.open_count == 0


# ---------------------------------------------------------------------------
# 범위: 런 / 일 / 전체
# ---------------------------------------------------------------------------

def test_run_cap_only_counts_the_same_run(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=5_000, day=100_000, total=100_000)
    ledger.reserve(caps=caps, key=_key(run_id="run-a"), nusd=5_000)

    assert isinstance(ledger.reserve(caps=caps, key=_key(run_id="run-b"), nusd=5_000), Reservation)
    refusal = ledger.reserve(caps=caps, key=_key(run_id="run-a"), nusd=1)
    assert isinstance(refusal, Refusal) and refusal.scope == "run"


def test_day_cap_spans_runs_and_resets_on_the_next_day(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=100_000, day=5_000, total=100_000)
    first = ledger.reserve(caps=caps, key=_key(run_id="run-a"), nusd=5_000)
    ledger.settle(first, nusd=5_000, outcome="ok", basis="actual")

    refusal = ledger.reserve(caps=caps, key=_key(run_id="run-b"), nusd=1)
    assert isinstance(refusal, Refusal) and refusal.scope == "day"
    assert isinstance(ledger.reserve(caps=caps, key=_key(run_id="run-b", day="2026-10-07"), nusd=5_000), Reservation)


def test_total_cap_spans_days_and_is_reported_before_narrower_scopes(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=5_000, day=5_000, total=5_000)
    first = ledger.reserve(caps=caps, key=_key(day="2026-10-05"), nusd=5_000)
    ledger.settle(first, nusd=5_000, outcome="ok", basis="actual")

    refusal = ledger.reserve(caps=caps, key=_key(run_id="run-b", day=DAY), nusd=1)

    assert isinstance(refusal, Refusal) and refusal.scope == "total"


def test_zero_cap_refuses_everything(tmp_path):
    ledger = _ledger(tmp_path)

    refusal = ledger.reserve(caps=_caps(total=0), key=_key(), nusd=1)

    assert isinstance(refusal, Refusal) and refusal.scope == "total"


def test_refusal_is_written_to_the_ledger_but_adds_no_spend(tmp_path):
    ledger = _ledger(tmp_path)

    ledger.reserve(caps=_caps(run=10), key=_key(), nusd=11)

    events = _events(ledger.path)
    assert [e["ev"] for e in events] == ["refuse"]
    assert events[0]["scope"] == "run" and events[0]["requested_nusd"] == 11
    assert ledger.totals(run_id="run-a", day=DAY).used("total") == 0


# ---------------------------------------------------------------------------
# 프로세스 간 지속과 충돌 복구
# ---------------------------------------------------------------------------

def test_a_new_ledger_object_on_the_same_file_sees_earlier_spend(tmp_path):
    first = _ledger(tmp_path)
    res = first.reserve(caps=_caps(), key=_key(), nusd=4_000)
    first.settle(res, nusd=2_500, outcome="ok", basis="actual")

    second = _ledger(tmp_path)

    assert second.totals(run_id="run-a", day=DAY).committed["total"] == 2_500


def test_crash_between_reserve_and_commit_keeps_the_reservation_as_spend(tmp_path):
    """예약만 쓰고 죽은 프로세스: 그 요청이 과금됐는지 알 수 없으므로 최악 비용으로 남는다."""
    crashed = _ledger(tmp_path)
    crashed.reserve(caps=_caps(run=10_000), key=_key(), nusd=6_000)
    del crashed  # 정산 없이 프로세스가 끝난 상황

    survivor = _ledger(tmp_path)
    refusal = survivor.reserve(caps=_caps(run=10_000), key=_key(), nusd=5_000)

    assert isinstance(refusal, Refusal)
    assert refusal.open_nusd == 6_000


def test_stale_reservation_expires_as_spent_and_stays_charged(tmp_path):
    clock = Clock()
    ledger = _ledger(tmp_path, reservation_ttl_s=600, clock=clock)
    stale = ledger.reserve(caps=_caps(), key=_key(), nusd=6_000)

    clock.now += 601
    ledger.reserve(caps=_caps(), key=_key(), nusd=1_000)

    events = _events(ledger.path)
    expired = [e for e in events if e["ev"] == "expire"]
    assert len(expired) == 1
    assert expired[0]["id"] == stale.id and expired[0]["nusd"] == 6_000
    assert expired[0]["outcome"] == "expired_as_spent"
    totals = ledger.totals(run_id="run-a", day=DAY)
    assert totals.committed["total"] == 6_000 and totals.open["total"] == 1_000


def test_reservation_younger_than_the_ttl_is_not_expired(tmp_path):
    clock = Clock()
    ledger = _ledger(tmp_path, reservation_ttl_s=600, clock=clock)
    ledger.reserve(caps=_caps(), key=_key(), nusd=6_000)

    clock.now += 599
    ledger.reserve(caps=_caps(), key=_key(), nusd=1_000)

    assert [e["ev"] for e in _events(ledger.path)] == ["reserve", "reserve"]


def test_late_commit_after_expiry_never_lowers_the_charge(tmp_path):
    """만료로 최악 비용을 잡은 뒤 늦게 도착한 정산은 지출을 낮추지 못하고, 더 크면 올린다."""
    clock = Clock()
    ledger = _ledger(tmp_path, reservation_ttl_s=600, clock=clock)
    low = ledger.reserve(caps=_caps(total=100_000, day=100_000, run=100_000), key=_key(), nusd=6_000)
    high = ledger.reserve(caps=_caps(total=100_000, day=100_000, run=100_000), key=_key(), nusd=6_000)
    clock.now += 601
    ledger.totals(run_id="run-a", day=DAY)  # 읽기만으로는 만료 기록을 쓰지 않는다
    ledger.reserve(caps=_caps(total=100_000, day=100_000, run=100_000), key=_key(), nusd=1)

    ledger.settle(low, nusd=1_000, outcome="ok", basis="actual")
    ledger.settle(high, nusd=9_000, outcome="ok", basis="actual")

    assert ledger.totals(run_id="run-a", day=DAY).committed["total"] == 6_000 + 9_000


def test_torn_trailing_line_is_ignored_and_does_not_corrupt_later_appends(tmp_path):
    """정산 줄을 쓰다 전원이 나간 상황: 깨진 줄은 건너뛰고, 그 예약은 열린 채(최악 비용) 남는다."""
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)
    with open(ledger.path, "ab") as f:
        f.write(b'{"ev":"commit","id":"' + res.id.encode() + b'","nus')  # 개행 없이 끊김

    fresh = _ledger(tmp_path)
    second = fresh.reserve(caps=_caps(), key=_key(), nusd=1_000)
    fresh.settle(second, nusd=500, outcome="ok", basis="actual")

    totals = _ledger(tmp_path).totals(run_id="run-a", day=DAY)
    assert totals.open["total"] == 4_000
    assert totals.committed["total"] == 500
    assert totals.corrupt_lines == 1


def test_commit_whose_reserve_line_was_lost_is_still_counted(tmp_path):
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)
    ledger.settle(res, nusd=1_500, outcome="ok", basis="actual")
    lines = Path(ledger.path).read_text(encoding="utf-8").splitlines()
    Path(ledger.path).write_text(lines[1] + "\n", encoding="utf-8")  # 예약 줄만 사라짐

    totals = _ledger(tmp_path).totals(run_id="run-a", day=DAY)

    assert totals.committed == {"total": 1_500, "day": 1_500, "run": 1_500}


def test_unwritable_ledger_location_raises_instead_of_allowing_the_call(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    ledger = SpendLedger(blocker / "ledger.jsonl")

    with pytest.raises(LedgerError):
        ledger.reserve(caps=_caps(), key=_key(), nusd=1)


def test_ledger_and_lock_files_are_private_to_the_user(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve(caps=_caps(), key=_key(), nusd=1)

    assert os.stat(ledger.path).st_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# 하루 초기화
# ---------------------------------------------------------------------------

def test_reset_day_clears_the_day_window_but_never_the_total(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=100_000, day=5_000, total=8_000)
    first = ledger.reserve(caps=caps, key=_key(), nusd=5_000)
    ledger.settle(first, nusd=5_000, outcome="ok", basis="actual")

    cleared = ledger.reset_day(DAY, note="콘솔 청구액 확인 후")

    assert cleared == 5_000
    totals = ledger.totals(run_id="run-a", day=DAY)
    assert totals.committed["day"] == 0 and totals.committed["total"] == 5_000
    assert isinstance(ledger.reserve(caps=caps, key=_key(run_id="run-b"), nusd=3_000), Reservation)
    refusal = ledger.reserve(caps=caps, key=_key(run_id="run-c"), nusd=1)
    assert isinstance(refusal, Refusal) and refusal.scope == "total"


def test_reset_day_keeps_in_flight_reservations_counted(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)

    ledger.reset_day(DAY, note="")

    assert ledger.totals(run_id="run-a", day=DAY).open["day"] == 4_000


# ---------------------------------------------------------------------------
# 동시성: 스레드와 프로세스
# ---------------------------------------------------------------------------

def test_threads_never_reserve_past_the_cap(tmp_path):
    ledger = _ledger(tmp_path)
    caps = _caps(run=10_000, day=10_000, total=10_000)
    start = threading.Barrier(8)
    granted, refused = [], []

    def worker():
        start.wait()
        for _ in range(10):
            result = ledger.reserve(caps=caps, key=_key(), nusd=700)
            (granted if isinstance(result, Reservation) else refused).append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 14  # floor(10,000 / 700)
    assert len(refused) == 80 - 14
    assert ledger.totals(run_id="run-a", day=DAY).used("total") == 14 * 700


def test_threads_with_separate_ledger_objects_share_the_cap_through_the_file(tmp_path):
    """스레드마다 원장 객체를 따로 만들어도(프로세스 안 뮤텍스를 공유하지 않아도) 파일 잠금이 막는다."""
    caps = _caps(run=10_000, day=10_000, total=10_000)
    start = threading.Barrier(6)
    granted = []

    def worker():
        ledger = _ledger(tmp_path)
        start.wait()
        for _ in range(10):
            result = ledger.reserve(caps=caps, key=_key(), nusd=900)
            if isinstance(result, Reservation):
                granted.append(result)
                ledger.settle(result, nusd=900, outcome="ok", basis="actual")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 11  # floor(10,000 / 900)
    assert _ledger(tmp_path).totals(run_id="run-a", day=DAY).committed["total"] == 11 * 900


_PROCESS_WORKER = """
import json, sys, time
from core.llm.spend_ledger import CallKey, Caps, Reservation, SpendLedger

path, run_id, attempts = sys.argv[1], sys.argv[2], int(sys.argv[3])
ledger = SpendLedger(path)
caps = Caps(run_nusd=10**12, day_nusd=20_000, total_nusd=10**12)
key = CallKey(run_id=run_id, day="2026-10-06", provider="gemini", model="m", role="generator", purpose="p")
granted = 0
print("ready", flush=True)
sys.stdin.readline()  # 두 프로세스가 임포트를 끝낸 뒤 같이 출발한다
for i in range(attempts):
    time.sleep(0.002)  # 잠금을 놓자마자 다시 잡아 상대를 굶기지 않게 - 실제 호출도 사이에 네트워크 대기가 있다
    result = ledger.reserve(caps=caps, key=key, nusd=300)
    if isinstance(result, Reservation):
        granted += 1
        # 절반은 최악 비용 그대로, 절반은 예약을 연 채로 둔다(정산 전에 죽은 호출)
        if i % 2 == 0:
            ledger.settle(result, nusd=300, outcome="ok", basis="actual")
print(json.dumps({"granted": granted}))
"""


def test_two_processes_never_reserve_past_the_shared_day_cap(tmp_path):
    path = tmp_path / "ledger.jsonl"
    env = {**os.environ, "PYTHONPATH": AI_WORKSPACE + os.pathsep + os.environ.get("PYTHONPATH", "")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _PROCESS_WORKER, str(path), f"run-{i}", "60"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for i in range(2)
    ]
    for p in procs:
        assert p.stdout.readline().strip() == "ready", p.stderr.read()
    for p in procs:
        p.stdin.write("go\n")
        p.stdin.flush()
    outputs = [p.communicate(timeout=120) for p in procs]
    for p, (_, err) in zip(procs, outputs):
        assert p.returncode == 0, err

    granted = [json.loads(out.strip().splitlines()[-1])["granted"] for out, _ in outputs]
    # 두 프로세스가 각각 60번(합계 36,000 nUSD어치) 시도했지만 일 상한 20,000 안에서만 허용된다.
    assert sum(granted) == 66  # floor(20,000 / 300)
    assert all(g > 0 for g in granted), "한 프로세스가 전부 가져가면 경합을 시험하지 못한다"
    totals = SpendLedger(path).totals(run_id="run-0", day=DAY)
    assert totals.used("day") == 66 * 300 <= 20_000
    assert totals.corrupt_lines == 0


# ---------------------------------------------------------------------------
# 집계용 읽기
# ---------------------------------------------------------------------------

def test_read_events_yields_every_parsed_line_in_order(tmp_path):
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000, extra={"est_input_tokens": 123})
    ledger.settle(res, nusd=1_500, outcome="ok", basis="actual", extra={"input_tokens": 100})

    events = list(ledger.read_events())

    assert [e["ev"] for e in events] == ["reserve", "commit"]
    assert events[0]["est_input_tokens"] == 123 and events[0]["role"] == "generator"
    assert events[1]["input_tokens"] == 100 and events[1]["reserved_nusd"] == 4_000
    assert events[1]["model"] == "gemini-3.5-flash-lite"  # 정산 줄만으로도 집계할 수 있다


def test_totals_on_a_missing_file_are_zero_and_create_nothing(tmp_path):
    ledger = _ledger(tmp_path)

    totals = ledger.totals(run_id="run-a", day=DAY)

    assert totals.used("total") == 0
    assert not Path(ledger.path).exists()
