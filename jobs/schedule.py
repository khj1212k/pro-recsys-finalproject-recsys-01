"""crontab(supercronic)의 `python -m jobs.run <job>` 줄에서 정시 회차를 계산하고, job_runs의 시작 시각과 맞춘다.

supercronic은 같은 잡의 앞 실행이 끝나지 않았으면 다음 회차를 시작하지 않고, 그 회차는 job_runs에 행을 남기지 않는다
(ADR 0026 증거 8, 런북 6.6). 그래서 "끝난 잡 성공률"로는 건너뛴 회차가 보이지 않는다 - 예정 회차와 시작된 회차를
따로 센다. 지원하는 형식은 이 저장소의 crontab이 쓰는 것뿐이다: 분·시는 `*`, 숫자, `a-b`, `a,b`, `*/n`, `a-b/n`,
일·월·요일은 `*`. 그 밖의 줄은 세지 않고 `unsupported`로 돌려준다. 시각은 컨테이너 TZ(UTC) 기준이다.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

JOB_RE = re.compile(r"(?:^|\s)python3?\s+-m\s+jobs\.run\s+([A-Za-z0-9_]+)")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
# supercronic은 정시에 바로 띄운다. 그보다 늦게 시작한 행(손으로 돌린 실행 등)은 그 회차로 치지 않는다.
SLOT_MATCH_TOLERANCE = timedelta(minutes=2)


@dataclass(frozen=True)
class CronEntry:
    job: str
    minutes: Tuple[int, ...]
    hours: Tuple[int, ...]
    line: str


def _field(spec: str, low: int, high: int) -> Optional[Set[int]]:
    values: Set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                return None
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = high
        else:
            return None
        if not (low <= start <= end <= high):
            return None
        values.update(range(start, end + 1, step))
    return values


def parse_crontab(text: str) -> Tuple[List[CronEntry], List[str]]:
    """(지원하는 jobs.run 줄, 지원하지 않는 jobs.run 줄). 주석·빈 줄·환경변수 줄과 jobs.run이 아닌 줄은 무시한다."""
    entries: List[CronEntry] = []
    unsupported: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ENV_RE.match(line):
            continue
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        match = JOB_RE.search(fields[5])
        if not match:
            continue
        minutes, hours = _field(fields[0], 0, 59), _field(fields[1], 0, 23)
        if minutes is None or hours is None or fields[2:5] != ["*", "*", "*"]:
            unsupported.append(line)
            continue
        entries.append(CronEntry(match.group(1), tuple(sorted(minutes)), tuple(sorted(hours)), line))
    return entries, unsupported


def expected_slots(entries: Iterable[CronEntry], start: datetime, end: datetime,
                   disabled: Sequence[str] = ()) -> Dict[str, List[datetime]]:
    """(start, end] 안의 정시 회차. JOBS_DISABLED에 있는 잡은 뺀다(그 잡은 실행 기록을 남기지 않는다)."""
    out: Dict[str, List[datetime]] = {}
    hour = start.replace(minute=0, second=0, microsecond=0)
    for entry in entries:
        if entry.job in disabled:
            continue
        slots = out.setdefault(entry.job, [])
        h = hour
        while h <= end:
            if h.hour in entry.hours:
                for m in entry.minutes:
                    t = h + timedelta(minutes=m)
                    if start < t <= end:
                        slots.append(t)
            h += timedelta(hours=1)
    return {job: sorted(set(slots)) for job, slots in out.items()}


def match_slots(slots: Sequence[datetime], runs: Sequence[Tuple[datetime, str]],
                tolerance: timedelta = SLOT_MATCH_TOLERANCE) -> Dict[str, object]:
    """runs: (started_at, status). 회차마다 [회차, 회차 + tolerance) 안에 시작한 행이 있으면 시작된 것으로 센다."""
    started, succeeded, missed = 0, 0, []
    for slot in slots:
        hit = [status for at, status in runs if slot <= at < slot + tolerance]
        if hit:
            started += 1
            succeeded += any(s == "succeeded" for s in hit)
        else:
            missed.append(slot)
    return {"scheduled": len(slots), "started": started, "succeeded": succeeded, "missed": missed}
