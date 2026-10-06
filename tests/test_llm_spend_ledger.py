# LLM 지출 원장(core/llm/spend_ledger.py) 단위 테스트 - docs/adr/0035.
#
# 원장은 호출 시도 1회를 "예약 -> 정산" 두 줄로 남기는 추가 전용 파일이다. 여기서는 상한 산술
# (예약 대 실제), 스레드·프로세스 동시성에서 상한을 넘지 않는 것, 예약과 정산 사이에 죽은
# 프로세스의 예약이 지출로 남는 것, 그리고 원장이 지금까지의 지출을 증명하지 못할 때(삭제, 절단,
# 손상, 쓰기 유실, 바꿔치기) 0부터 다시 세지 않고 거부하는 것을 본다. 네트워크·LLM 호출은 없다.
import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

AI_WORKSPACE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace")
sys.path.insert(0, AI_WORKSPACE)

from core.llm import spend_ledger  # noqa: E402
from core.llm.spend_ledger import (  # noqa: E402
    CallKey,
    Caps,
    LedgerError,
    LedgerIntegrityError,
    Refusal,
    Reservation,
    SpendLedger,
    watermark_path_for,
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
    """tmp_path의 원장. 없으면 사람이 `spend_cli init`을 한 것처럼 먼저 만든다."""
    ledger = SpendLedger(tmp_path / "ledger.jsonl", **kwargs)
    if not os.path.exists(ledger.path):
        ledger.init()
    return ledger


def _events(path):
    """init 헤더를 뺀 원장 줄."""
    events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if e["ev"] != "init"]


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
    """정산 줄을 쓰다 전원이 나간 상황: 깨진 줄은 건너뛰고, 그 예약은 열린 채(최악 비용) 남는다.

    끝의 끊긴 한 줄은 쓰다 죽은 흔적이라 거부 사유가 아니다. 다음 쓰기가 그 줄을 끝내고 torn 줄로 인정한다.
    """
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)
    torn_at = os.path.getsize(ledger.path)
    fragment = b'{"ev":"commit","id":"' + res.id.encode() + b'","nus'  # 개행 없이 끊김
    with open(ledger.path, "ab") as f:
        f.write(fragment)

    fresh = _ledger(tmp_path)
    assert fresh.totals(run_id="run-a", day=DAY).problem is None  # 읽기만 해도 거부 사유가 아니다
    second = fresh.reserve(caps=_caps(), key=_key(), nusd=1_000)
    fresh.settle(second, nusd=500, outcome="ok", basis="actual")

    totals = _ledger(tmp_path).totals(run_id="run-a", day=DAY)
    assert totals.open["total"] == 4_000
    assert totals.committed["total"] == 500
    assert (totals.corrupt_lines, totals.unacknowledged_corrupt_lines, totals.problem) == (1, 0, None)
    torn = [e for e in SpendLedger(ledger.path).read_events() if e["ev"] == "torn"]
    assert [(e["offsets"], e["bytes"]) for e in torn] == [([torn_at], len(fragment))]


def test_commit_whose_reserve_line_was_lost_is_still_counted(tmp_path):
    ledger = _ledger(tmp_path)
    res = ledger.reserve(caps=_caps(), key=_key(), nusd=4_000)
    ledger.settle(res, nusd=1_500, outcome="ok", basis="actual")
    header, _reserve, commit = Path(ledger.path).read_text(encoding="utf-8").splitlines()
    Path(ledger.path).write_text(header + "\n" + commit + "\n", encoding="utf-8")  # 예약 줄만 사라짐

    # 줄어든 원장이라 호출은 거부된다(아래 무결성 테스트). 합계 산술만 본다.
    totals = _ledger(tmp_path).totals(run_id="run-a", day=DAY, strict=False)

    assert totals.committed == {"total": 1_500, "day": 1_500, "run": 1_500}


def test_unwritable_ledger_location_raises_instead_of_allowing_the_call(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    ledger = SpendLedger(blocker / "ledger.jsonl")

    with pytest.raises(LedgerError):
        ledger.reserve(caps=_caps(), key=_key(), nusd=1)


def test_ledger_and_watermark_files_are_private_to_the_user(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.reserve(caps=_caps(), key=_key(), nusd=1)

    assert os.stat(ledger.path).st_mode & 0o077 == 0
    assert os.stat(ledger.watermark_path).st_mode & 0o077 == 0


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
        start.wait(timeout=30)
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
    path = _ledger(tmp_path).path  # 원장은 스레드를 띄우기 전에 한 번 만든다
    start = threading.Barrier(6)
    granted = []

    def worker():
        ledger = SpendLedger(path)
        start.wait(timeout=30)
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
    path = _ledger(tmp_path).path
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

    assert [e["ev"] for e in events] == ["init", "reserve", "commit"]
    assert events[1]["est_input_tokens"] == 123 and events[1]["role"] == "generator"
    assert events[2]["input_tokens"] == 100 and events[2]["reserved_nusd"] == 4_000
    assert events[2]["model"] == "gemini-3.5-flash-lite"  # 정산 줄만으로도 집계할 수 있다


def test_reading_a_missing_ledger_creates_nothing(tmp_path):
    ledger = SpendLedger(tmp_path / "ledger.jsonl")

    with pytest.raises(LedgerIntegrityError):
        ledger.totals(run_id="run-a", day=DAY)
    lenient = ledger.totals(run_id="run-a", day=DAY, strict=False)  # 요약 CLI용: 문제를 설명과 함께 돌려준다

    assert lenient.used("total") == 0 and "spend_cli init" in lenient.problem
    assert not Path(ledger.path).exists() and not Path(ledger.watermark_path).exists()


# ---------------------------------------------------------------------------
# 무결성: 원장이 지금까지의 지출을 증명하지 못하면 0부터 다시 세지 않고 거부한다
# ---------------------------------------------------------------------------

def _spend(ledger, nusd, caps=None, key=None):
    res = ledger.reserve(caps=caps or _caps(), key=key or _key(), nusd=nusd)
    assert isinstance(res, Reservation)
    ledger.settle(res, nusd=nusd, outcome="ok", basis="actual")
    return res


def _problem_code(tmp_path, **kwargs):
    """새 프로세스처럼 원장 객체를 새로 만들어 예약을 시도하고, 거부 사유 코드를 돌려준다."""
    with pytest.raises(LedgerIntegrityError) as exc:
        SpendLedger(tmp_path / "ledger.jsonl", **kwargs).reserve(caps=_caps(), key=_key(), nusd=1)
    return exc.value.code


def test_the_guard_never_creates_a_ledger(tmp_path):
    """원장이 없으면 만들지 않고 거부한다 - 자동 생성은 지워진 원장을 0부터 다시 세는 길이다."""
    ledger = SpendLedger(tmp_path / "ledger.jsonl")

    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.reserve(caps=_caps(), key=_key(), nusd=1)
    with pytest.raises(LedgerIntegrityError):
        ledger.probe()

    assert exc.value.code == "missing" and "spend_cli init" in str(exc.value)
    assert os.listdir(tmp_path) == []


def test_init_writes_the_header_and_the_out_of_tree_watermark(tmp_path):
    ledger = SpendLedger(tmp_path / "ledger.jsonl")

    info = ledger.init(note="첫 유료 실험")

    header = json.loads(Path(ledger.path).read_text(encoding="utf-8").splitlines()[0])
    assert (header["ev"], header["ledger_id"], header["note"]) == ("init", info["ledger_id"], "첫 유료 실험")
    watermark = json.loads(Path(ledger.watermark_path).read_text(encoding="utf-8"))
    assert (watermark["ledger_id"], watermark["committed_total_nusd"]) == (info["ledger_id"], 0)
    assert tmp_path not in Path(ledger.watermark_path).parents  # 원장 디렉터리 밖
    assert ledger.watermark_path == watermark_path_for(ledger.path)
    ledger.probe()


def test_init_never_overwrites_an_existing_file(tmp_path):
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    before = Path(ledger.path).read_bytes()

    with pytest.raises(LedgerError, match="이미 파일이"):
        SpendLedger(ledger.path).init()

    assert Path(ledger.path).read_bytes() == before


def test_deleting_the_ledger_refuses_every_later_call(tmp_path):
    """(a) 상한에 닿은 뒤 원장을 지워도(`git clean -fdx`) 다음 호출이 허용되지 않는다."""
    ledger = _ledger(tmp_path)
    caps = _caps(total=5_000)
    _spend(ledger, 5_000, caps=caps)
    assert isinstance(ledger.reserve(caps=caps, key=_key(), nusd=1), Refusal)

    os.remove(ledger.path)

    with pytest.raises(LedgerIntegrityError) as same_process:
        ledger.reserve(caps=caps, key=_key(), nusd=1)
    assert same_process.value.code == "missing"
    assert _problem_code(tmp_path) == "missing"
    assert not os.path.exists(ledger.path)


def test_a_recreated_ledger_is_refused_until_the_lost_total_is_adopted(tmp_path):
    """(a) 지운 자리에 원장을 새로 만들어도 원장 밖의 누계 기록이 남아 있어 0부터 시작하지 못한다."""
    ledger = _ledger(tmp_path)
    caps = _caps(run=100_000, day=100_000, total=8_000)
    _spend(ledger, 5_000, caps=caps)
    os.remove(ledger.path)

    info = SpendLedger(ledger.path).init()

    assert info["prior"]["committed_total_nusd"] == 5_000
    assert _problem_code(tmp_path) == "replaced"

    result = SpendLedger(ledger.path).adopt(note="git clean으로 지워짐")

    assert (result["was"], result["carried_nusd"], result["problem"]) == ("replaced", 5_000, None)
    fresh = SpendLedger(ledger.path)
    assert fresh.totals(run_id="run-a", day=DAY).committed == {"total": 5_000, "day": 5_000, "run": 0}
    refusal = fresh.reserve(caps=caps, key=_key(run_id="run-b"), nusd=3_001)
    assert isinstance(refusal, Refusal) and (refusal.scope, refusal.committed_nusd) == ("total", 5_000)
    assert isinstance(fresh.reserve(caps=caps, key=_key(run_id="run-b"), nusd=3_000), Reservation)


def test_adopting_keeps_the_day_window_of_the_lost_ledger(tmp_path):
    """이어받는 것은 전체 누계만이 아니다 - 그날 이미 쓴 금액도 새 원장의 일 상한에 잡힌다."""
    ledger = _ledger(tmp_path)
    caps = _caps(run=100_000, day=6_000, total=100_000)
    _spend(ledger, 2_000, caps=caps, key=_key(day="2026-10-05"))
    _spend(ledger, 5_000, caps=caps)
    os.remove(ledger.path)
    SpendLedger(ledger.path).init()

    result = SpendLedger(ledger.path).adopt()

    assert (result["carried_nusd"], result["day"], result["carried_day_nusd"]) == (7_000, DAY, 5_000)
    refusal = SpendLedger(ledger.path).reserve(caps=caps, key=_key(run_id="run-b"), nusd=1_001)
    assert isinstance(refusal, Refusal) and refusal.scope == "day"


def test_adopting_counts_reservations_that_were_open_when_the_ledger_was_lost(tmp_path):
    """정산 전에 원장이 사라진 예약: 과금됐는지 알 수 없으므로 만료된 예약처럼 최악 비용으로 이어받는다."""
    ledger = _ledger(tmp_path)
    caps = _caps(run=100_000, day=100_000, total=8_000)
    _spend(ledger, 2_000, caps=caps)
    assert isinstance(ledger.reserve(caps=caps, key=_key(), nusd=6_000), Reservation)  # 상한을 채운 채 열려 있다
    os.remove(ledger.path)
    SpendLedger(ledger.path).init()

    result = SpendLedger(ledger.path).adopt()

    assert (result["prev_committed_total_nusd"], result["prev_open_nusd"], result["carried_nusd"]) == (2_000, 6_000, 8_000)
    refusal = SpendLedger(ledger.path).reserve(caps=caps, key=_key(run_id="run-b"), nusd=1)
    assert isinstance(refusal, Refusal) and refusal.scope == "total"


def test_adopt_on_a_healthy_ledger_changes_nothing(tmp_path):
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    before = Path(ledger.path).read_bytes()

    result = ledger.adopt()

    assert (result["was"], result["carried_nusd"], result["problem"]) == (None, 0, None)
    assert Path(ledger.path).read_bytes() == before


@pytest.mark.parametrize("keep_lines,code", [(0, "uninitialized"), (1, "shrunk")])
def test_truncated_ledger_is_refused(tmp_path, keep_lines, code):
    """(b) 0바이트로 자르거나 헤더만 남겨도 상한이 처음부터 다시 시작하지 않는다."""
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    lines = Path(ledger.path).read_bytes().splitlines(keepends=True)
    Path(ledger.path).write_bytes(b"".join(lines[:keep_lines]))  # 같은 inode에 덮어쓴다

    with pytest.raises(LedgerIntegrityError) as same_process:
        ledger.reserve(caps=_caps(), key=_key(), nusd=1)

    assert same_process.value.code == code
    assert _problem_code(tmp_path) == code


def test_a_truncated_ledger_gets_its_total_back_through_adopt(tmp_path):
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    header = Path(ledger.path).read_bytes().splitlines(keepends=True)[0]
    Path(ledger.path).write_bytes(header)

    result = SpendLedger(ledger.path).adopt()

    assert (result["was"], result["carried_nusd"], result["problem"]) == ("shrunk", 5_000, None)
    assert SpendLedger(ledger.path).totals(run_id="run-a", day=DAY).committed["total"] == 5_000


def test_ledger_whose_total_fell_behind_the_watermark_is_refused(tmp_path):
    """줄 수와 크기는 그대로인데 정산 금액만 낮아진 원장(손으로 고친 경우)도 누계 비교에 걸린다."""
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    text = Path(ledger.path).read_text(encoding="utf-8")
    Path(ledger.path).write_text(text.replace('"ev":"commit"', '"ev":"refuse"'), encoding="utf-8")

    assert _problem_code(tmp_path) == "behind"


def _flip_first_byte(path, skip_header):
    lines = Path(path).read_bytes().splitlines(keepends=True)
    start = 1 if skip_header else 0
    Path(path).write_bytes(b"".join(lines[:start]) + b"".join(b"#" + line[1:] for line in lines[start:]))


def test_corrupt_lines_refuse_the_call_instead_of_being_skipped(tmp_path):
    """(c) 줄이 깨져 읽히지 않으면 그 줄의 지출을 0으로 치지 않는다 - 호출을 거부한다."""
    ledger = _ledger(tmp_path)
    for _ in range(3):
        _spend(ledger, 1_000)

    _flip_first_byte(ledger.path, skip_header=True)

    with pytest.raises(LedgerIntegrityError) as exc:
        SpendLedger(ledger.path).reserve(caps=_caps(), key=_key(), nusd=1)
    assert exc.value.code == "corrupt" and "6개" in str(exc.value) and "repair --yes" in str(exc.value)
    lenient = SpendLedger(ledger.path).totals(run_id="run-a", day=DAY, strict=False)
    assert (lenient.corrupt_lines, lenient.unacknowledged_corrupt_lines) == (6, 6)


def test_corrupt_header_reads_as_an_uninitialized_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    _spend(ledger, 1_000)

    _flip_first_byte(ledger.path, skip_header=False)

    assert _problem_code(tmp_path) == "uninitialized"


def test_repair_acknowledges_corrupt_lines_and_adopt_restores_the_total(tmp_path):
    """깨진 줄을 인정해도 누계는 내려가지 않는다: 원장 밖의 누계 기록과의 차이를 adopt가 이어받는다."""
    ledger = _ledger(tmp_path)
    for _ in range(3):
        _spend(ledger, 1_000)
    _flip_first_byte(ledger.path, skip_header=True)
    size = os.path.getsize(ledger.path)

    repaired = SpendLedger(ledger.path).repair(note="디스크 오류 뒤 확인")

    assert len(repaired["acknowledged"]) == 6
    assert os.path.getsize(ledger.path) > size  # 줄을 지우지 않고 repair 줄을 붙인다
    assert _problem_code(tmp_path) == "behind"  # 읽히는 누계가 0이 됐다 - 아직 거부

    adopted = SpendLedger(ledger.path).adopt()

    assert (adopted["was"], adopted["carried_nusd"], adopted["problem"]) == ("behind", 3_000, None)
    totals = SpendLedger(ledger.path).totals(run_id="run-a", day=DAY)
    assert totals.committed["total"] == 3_000
    assert (totals.corrupt_lines, totals.unacknowledged_corrupt_lines) == (6, 0)


def test_repair_and_adopt_refuse_a_ledger_without_a_header(tmp_path):
    ledger = _ledger(tmp_path)
    Path(ledger.path).write_bytes(b"")

    with pytest.raises(LedgerIntegrityError):
        SpendLedger(ledger.path).repair()
    with pytest.raises(LedgerIntegrityError):
        SpendLedger(ledger.path).adopt()


def test_a_second_init_line_is_not_a_valid_event(tmp_path):
    """두 원장을 이어 붙인 파일(cat a b > c)을 한 원장으로 읽지 않는다."""
    ledger = _ledger(tmp_path)
    with open(ledger.path, "ab") as f:
        f.write(b'{"ev":"init","v":1,"ledger_id":"other"}\n')

    assert _problem_code(tmp_path) == "corrupt"


def test_write_that_does_not_reach_the_file_is_refused(tmp_path, monkeypatch):
    """(d) 쓰기를 삼키는 경로: 예약 줄이 파일에 남지 않으면 그 예약은 다음 상한 검사에 잡히지 않는다."""
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(spend_ledger, "_write_all", lambda fd, data: None)

    for _ in range(3):
        with pytest.raises(LedgerError, match="남지 않았습니다"):
            ledger.reserve(caps=_caps(), key=_key(), nusd=1)


def test_ledger_path_that_is_not_a_regular_file_is_refused(tmp_path):
    """(d) /dev/null 심볼릭 링크: 열리고 써지지만 아무것도 남지 않는다."""
    path = tmp_path / "ledger.jsonl"
    os.symlink("/dev/null", path)
    ledger = SpendLedger(path)

    for _ in range(3):
        with pytest.raises(LedgerError, match="일반 파일이 아닙니다"):
            ledger.reserve(caps=_caps(run=10), key=_key(), nusd=1)
    with pytest.raises(LedgerError):
        ledger.totals(run_id="run-a", day=DAY)


def test_ledger_removed_between_the_check_and_the_write_is_refused(tmp_path):
    """상한 검사를 통과한 뒤 파일이 사라지면, 쓴 줄은 아무도 보지 않는 파일에 있다 - 허용하지 않는다."""
    ledger = _ledger(tmp_path)
    clock = Clock()

    def removing_clock():
        if os.path.exists(ledger.path):
            os.remove(ledger.path)
        return clock()

    ledger._clock = removing_clock  # reserve는 무결성 검사 뒤, 줄을 쓰기 전에 시각을 읽는다

    with pytest.raises(LedgerIntegrityError) as exc:
        ledger.reserve(caps=_caps(), key=_key(), nusd=1)

    assert exc.value.code == "missing"


def test_missing_watermark_is_refused_until_adopted(tmp_path):
    """원장 밖의 누계 기록이 없으면 원장이 바뀌거나 줄었는지 확인할 수 없다."""
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    os.remove(ledger.watermark_path)

    assert _problem_code(tmp_path) == "no_watermark"
    result = SpendLedger(ledger.path).adopt()

    assert (result["was"], result["carried_nusd"], result["watermark_known"]) == ("no_watermark", 0, False)
    assert SpendLedger(ledger.path).totals(run_id="run-a", day=DAY).committed["total"] == 5_000
    adopt = [e for e in _events(ledger.path) if e["ev"] == "adopt"]
    assert [(e["reason"], e["watermark"], e["carried_nusd"]) for e in adopt] == [("no_watermark", "missing", 0)]


def test_unreadable_watermark_is_refused(tmp_path):
    ledger = _ledger(tmp_path)
    Path(ledger.watermark_path).write_text("{not json", encoding="utf-8")

    assert _problem_code(tmp_path) == "bad_watermark"


def test_watermark_follows_every_settlement_and_never_blocks_a_ledger_that_is_ahead(tmp_path):
    ledger = _ledger(tmp_path)
    _spend(ledger, 2_000)
    older = Path(ledger.watermark_path).read_bytes()
    _spend(ledger, 3_000)
    newest = json.loads(Path(ledger.watermark_path).read_text(encoding="utf-8"))

    assert newest["committed_total_nusd"] == 5_000 and newest["size"] == os.path.getsize(ledger.path)
    assert (newest["day"], newest["day_committed_nusd"], newest["open_nusd"]) == (DAY, 5_000, 0)
    # 줄을 쓴 뒤 누계 기록을 옮기기 전에 죽은 경우: 원장이 앞서 있을 뿐이라 거부하지 않는다
    Path(ledger.watermark_path).write_bytes(older)
    assert isinstance(SpendLedger(ledger.path).reserve(caps=_caps(), key=_key(), nusd=1), Reservation)


def test_settlement_is_still_recorded_on_a_broken_ledger_but_the_watermark_does_not_move(tmp_path):
    """요청이 나가 있는 동안 원장이 망가졌다: 정산 줄은 남기되, 망가진 원장을 새 기준으로 삼지 않는다."""
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    in_flight = ledger.reserve(caps=_caps(), key=_key(), nusd=2_000)
    watermark_before = Path(ledger.watermark_path).read_bytes()
    os.remove(ledger.path)
    SpendLedger(ledger.path).init()  # 다른 id의 새 원장

    ledger.settle(in_flight, nusd=700, outcome="ok", basis="actual")

    assert [e["ev"] for e in _events(ledger.path)] == ["commit"]
    assert Path(ledger.watermark_path).read_bytes() == watermark_before
    assert _problem_code(tmp_path) == "replaced"
    preview = SpendLedger(ledger.path).adopt(dry_run=True)
    assert preview["carried_nusd"] == 7_000 and [e["ev"] for e in _events(ledger.path)] == ["commit"]
    adopted = SpendLedger(ledger.path).adopt()
    # 다른 원장으로 바뀐 경우: 옛 원장의 정산 5,000과 그때 열려 있던 예약 2,000(최악 비용)을 전부 더한다.
    # 새 원장에 적힌 700은 옛 사용액에 없던 줄이라 빼지 않는다 - 그 예약만큼(2,000) 상한 쪽으로 틀린다.
    assert adopted["carried_nusd"] == 7_000
    assert SpendLedger(ledger.path).totals(run_id="run-a", day=DAY).committed["total"] == 7_700


def test_state_directory_inside_the_ledger_directory_is_rejected(tmp_path):
    """누계 기록이 원장과 같은 디렉터리에 있으면 함께 지워진다."""
    with pytest.raises(LedgerError, match="함께 지워지면"):
        SpendLedger(tmp_path / "ledger.jsonl", state_dir=tmp_path / "state")
    with pytest.raises(LedgerError, match="절대 경로"):
        SpendLedger(tmp_path / "ledger.jsonl", state_dir="relative/state")


def test_each_ledger_path_has_its_own_watermark(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = SpendLedger(tmp_path / "a" / "ledger.jsonl"), SpendLedger(tmp_path / "b" / "ledger.jsonl")
    link = tmp_path / "a" / "link.jsonl"
    os.symlink(first.path, link)

    assert first.watermark_path != second.watermark_path
    assert SpendLedger(link).watermark_path == first.watermark_path  # 같은 파일을 가리키는 다른 철자


# ---------------------------------------------------------------------------
# 잠금: 원장 파일 자체에 건다
# ---------------------------------------------------------------------------

def test_there_is_no_separate_lock_file_to_delete(tmp_path):
    """(e) 잠금 파일을 지워 두 보유자가 임계 구역에 함께 들어가는 길이 없다."""
    ledger = _ledger(tmp_path)
    _spend(ledger, 1_000)

    assert sorted(os.listdir(tmp_path)) == ["ledger.jsonl"]


def test_second_holder_waits_on_the_ledger_file_lock(tmp_path):
    ledger = _ledger(tmp_path)
    holder = os.open(ledger.path, os.O_RDWR)  # 다른 프로세스가 임계 구역 안에 있는 상황
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(LedgerError, match="잠금"):
            SpendLedger(ledger.path, lock_timeout_s=0.05).reserve(caps=_caps(), key=_key(), nusd=1)
    finally:
        os.close(holder)

    assert isinstance(SpendLedger(ledger.path, lock_timeout_s=0.05).reserve(caps=_caps(), key=_key(), nusd=1),
                      Reservation)


def test_waiter_does_not_write_into_a_ledger_that_was_swapped_while_it_waited(tmp_path, monkeypatch):
    """(e) 잠금을 기다리는 사이에 파일이 지워지고 새로 만들어졌다: 옛 파일의 잠금을 얻어도 거기에 쓰지 않는다."""
    ledger = _ledger(tmp_path)
    _spend(ledger, 5_000)
    holder = os.open(ledger.path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    old_size = os.fstat(holder).st_size
    outcome = []
    waiting = threading.Event()  # 대기자가 옛 파일을 열고 잠금을 기다리기 시작했다
    real_flock = SpendLedger._flock

    def flock(self, fd, deadline):
        waiting.set()
        return real_flock(self, fd, deadline)

    monkeypatch.setattr(SpendLedger, "_flock", flock)

    def waiter():
        try:
            outcome.append(SpendLedger(ledger.path).reserve(caps=_caps(), key=_key(), nusd=1))
        except LedgerIntegrityError as e:
            outcome.append(e.code)

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        assert waiting.wait(timeout=30)
        os.remove(ledger.path)
        SpendLedger(ledger.path).init()
        assert os.fstat(holder).st_size == old_size
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        thread.join(timeout=30)

    try:
        assert outcome == ["replaced"]
        assert os.fstat(holder).st_size == old_size  # 지워진 옛 파일에는 아무것도 붙지 않았다
        assert _events(ledger.path) == []
    finally:
        os.close(holder)
