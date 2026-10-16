"""news_raw의 동결 반출 - 읽기 전용 트랜잭션 하나로 시간 창의 기사를 파일로 뽑는다 (ADR 0036, ADR 0023 개정).

실험(E0 등)은 운영 DB에 쓰지 않고 이 반출본에서 돈다. 여기서 하는 일은 세 가지다.

1. **반출**: `BEGIN ... REPEATABLE READ READ ONLY` 한 트랜잭션 안에서 `COPY (SELECT ...) TO STDOUT` 세 번
   (머리말·행·꼬리말)으로 창 안의 `ok` 행을 읽고 `ROLLBACK`한다. 문장마다 시간 제한이 걸려 있다. DB에는
   두 가지 길로 닿는다: psycopg2 연결(SSH 터널, CI) 또는 표준입력으로 스크립트를 받는 psql 명령
   (`ssh ... docker compose exec -T db psql ...` - DB 비밀번호가 VM 밖으로 나오지 않는다).
2. **매니페스트**: 행 수, 창, 행별 해시의 해시, 원본 DB의 Alembic 리비전, 코드 SHA, 파일 sha256, 본문 만료일.
   `identity_sha256`은 반출 시점에 고정되고 본문을 지워도 바뀌지 않는다(사전 등록 기록에 적는 값).
3. **30일**: 행마다 `crawled_at + 30일`이 본문 만료 시각이다. 반출본을 여는 함수(`load_export`)는 만료된
   본문을 돌려주지 않고(fail closed), 열 때마다 디스크에서도 지운다. 해시·길이·제목·URL은 남는다.

본문이 든 파일은 메인 체크아웃의 `data/exports` 아래에만 쓰고(`ensure_export_location`), 그곳이 git이 추적할 수
있는 곳이면 쓰지 않는다(`ensure_private_destination`). 이 모듈은 본문·제목·URL을 표준출력이나 로그에 내지 않는다 -
요약에는 건수·해시·시각만 있다.

    python scripts/export_news_raw.py plan-window --days 3                      # 미리 보기
    python scripts/export_news_raw.py export --days 3 --psql-command "ssh ... psql ..."   # E0: 창을 직접 계산
    python scripts/export_news_raw.py export --start-utc ... --end-utc ... --psql-command "..."  # 그 밖의 반출
    python scripts/export_news_raw.py verify --dir data/exports/<T0>
    python scripts/export_news_raw.py purge  --dir data/exports/<T0> [--everything]
    python scripts/export_news_raw.py purge  --all          # data/exports 아래 전부
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

FORMAT = "frozen-news-export/1"
ARTICLES_FILE = "articles.jsonl"
MANIFEST_FILE = "manifest.json"
BODY_RETENTION_DAYS = 30          # ADR 0023. 이 값을 늘리는 예외는 없다.
MAX_WINDOW_HOURS = 5 * 24         # 실험에 필요한 창만 내보낸다(ADR 0009 A8.1: 날짜 창 최대 5개).
INGEST_GAP_MINUTES = (20, 50)     # 정시 ingest 사이. docs/runbook-hosting.md의 반출 절.
KST = timezone(timedelta(hours=9))

_TS = "%Y-%m-%dT%H:%M:%S.%f"
ROW_FILTER = (
    "n.raw_news_extract_status = 'ok' AND n.raw_news_content IS NOT NULL AND n.raw_news_content <> ''"
)
# 행의 신원에 들어가는 열. 본문 자체는 없다 - 본문을 지운 뒤에도 같은 값이 나와야 한다.
# 만료 시각과 만료 면제 여부도 신원이다: 파일에서 그 값을 고쳐 정리를 피하면 신원이 달라져 읽히지 않는다.
_IDENTITY_KEYS = ("id", "press", "url", "title_sha256", "content_sha256", "body_chars",
                  "created_at", "crawled_at", "extracted_at", "extract_status",
                  "redistributable", "body_expires_at")
# 파일의 한 행이 가진 열 전부. 신원 밖의 열은 셋뿐이고 각각 대조된다: 제목은 title_sha256과, 본문은
# content_sha256·body_chars와, body_purged_at은 본문이 비었는지와. 이 밖의 열이 있거나 빠지면 읽지 않는다.
_ROW_KEYS = frozenset(_IDENTITY_KEYS) | {"title", "body", "body_purged_at"}


class ExportError(RuntimeError):
    """반출을 끝내지 못했다. 메시지에 기사 텍스트는 넣지 않는다."""


class WindowRefused(ExportError):
    """창이 잘못됐거나 허용 범위를 넘는다."""


class DestinationRefused(ExportError):
    """본문이 든 파일을 쓰면 안 되는 곳이다."""


class ExportIntegrityError(ExportError):
    """디스크의 반출본이 매니페스트와 맞지 않는다."""


# ---------------------------------------------------------------- 창


@dataclass(frozen=True)
class Window:
    """`start <= raw_news_crawled_at < end`. 둘 다 시간대 없는 UTC다(그 컬럼이 시간대 없는 UTC라서)."""

    start: datetime
    end: datetime

    def __post_init__(self):
        for value in (self.start, self.end):
            if not isinstance(value, datetime) or value.tzinfo is not None:
                raise WindowRefused("창의 양 끝은 시간대 없는 UTC datetime이어야 한다")
        if self.start >= self.end:
            raise WindowRefused("창의 시작이 끝보다 앞이어야 한다")
        if self.end - self.start > timedelta(hours=MAX_WINDOW_HOURS):
            raise WindowRefused(
                f"창이 {MAX_WINDOW_HOURS}시간을 넘는다 - 실험에 필요한 창만 내보낸다(ADR 0023 개정, ADR 0009 A8.1)"
            )

    @classmethod
    def from_iso(cls, start: str, end: str) -> "Window":
        try:
            return cls(datetime.fromisoformat(start), datetime.fromisoformat(end))
        except ValueError as e:
            raise WindowRefused(f"창의 시각을 읽을 수 없다: {e}") from None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def registered_day_windows(t0: datetime, *, days: int = 3, hour_kst: int = 6, lookback_hours: int = 24) -> List[Window]:
    """T0 직전에 완결된 KST 날짜 D₁<…<D_n 각각의 `hour_kst`시를 의사 시각 t_k로 하는 창 [t_k − 24h, t_k).

    ADR 0009 A8.1의 문장 그대로다: "의사 시각 t_k = T0 직전 완결된 KST 날짜 D₁<D₂<D₃ 각각의 06:00 KST".
    완결된 마지막 날짜는 T0의 KST 날짜 하루 전이므로 마지막 t_k는 언제나 T0보다 앞이다. 창끼리 겹치지 않는다.
    """
    if t0.tzinfo is None:
        raise WindowRefused("T0에는 시간대가 있어야 한다(예: 2026-10-08T14:00+09:00)")
    if not 1 <= days <= MAX_WINDOW_HOURS // lookback_hours:
        raise WindowRefused(f"날짜 창은 1~{MAX_WINDOW_HOURS // lookback_hours}개다")
    last_day = t0.astimezone(KST).date() - timedelta(days=1)
    windows = []
    for back in range(days - 1, -1, -1):
        day = last_day - timedelta(days=back)
        pseudo_now = datetime(day.year, day.month, day.day, hour_kst, tzinfo=KST)
        end = pseudo_now.astimezone(timezone.utc).replace(tzinfo=None)
        windows.append(Window(end - timedelta(hours=lookback_hours), end))
    return windows


def union_window(windows: Sequence[Window]) -> Window:
    """이어 붙은 창들을 하나의 반출 창으로."""
    ordered = sorted(windows, key=lambda w: w.start)
    for a, b in zip(ordered, ordered[1:]):
        if a.end != b.start:
            raise WindowRefused("창들이 이어져 있지 않다 - 하나씩 따로 반출한다")
    return Window(ordered[0].start, ordered[-1].end)


def plan_windows(t0: datetime, days: int) -> Tuple[Window, Dict[str, Any]]:
    """E0의 날짜 창(ADR 0009 A8.1)을 T0에서 계산하고, 반출 창(합집합)과 매니페스트에 적을 계획을 돌려준다.

    계획은 반출본의 신원에 들어간다. 반출은 서버 스냅숏 시각으로 같은 계산을 다시 해 계획과 다르면(T0와 스냅숏의
    KST 날짜가 다르면) 쓰기 전에 거절한다(`build_export`). 그래서 러너가 반출본의 T0로 다시 계산한 창은 언제나
    반출 창 안에 있다(`FrozenExport.day_windows`).
    """
    windows = registered_day_windows(t0, days=days)
    plan = {
        "rule": "ADR 0009 A8.1 - 의사 시각은 T0 직전에 완결된 KST 날짜들의 06:00 KST, 창은 그 앞 24시간",
        "t0_kst": t0.astimezone(KST).isoformat(),
        "days": days,
        "hour_kst": 6,
        "lookback_hours": 24,
        "day_windows_utc": [[_fmt(w.start), _fmt(w.end)] for w in windows],
    }
    return union_window(windows), plan


def _plan_check(plan: Dict[str, Any], window: Window, snapshot: datetime) -> None:
    """서버 스냅숏 시각(T0)으로 다시 계산한 날짜 창이 계획과 같고, 그 합집합이 반출 창과 같아야 한다."""
    again = registered_day_windows(snapshot.replace(tzinfo=timezone.utc), days=int(plan["days"]),
                                   hour_kst=int(plan["hour_kst"]), lookback_hours=int(plan["lookback_hours"]))
    union = union_window(again)
    if [[_fmt(w.start), _fmt(w.end)] for w in again] != plan["day_windows_utc"] or \
            (union.start, union.end) != (window.start, window.end):
        snapshot_kst = snapshot.replace(tzinfo=timezone.utc).astimezone(KST)
        raise WindowRefused(
            f"서버 스냅숏 시각(KST {snapshot_kst:%Y-%m-%d %H:%M})으로 다시 계산한 날짜 창이 계획(T0 {plan['t0_kst']})과 "
            "다르다 - 계획한 T0와 반출 시각의 KST 날짜가 다르다. 그 날짜로 다시 계획해 반출한다(아무것도 쓰지 않았다)"
        )


def _fmt(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.strftime(_TS) + "Z"


def _parse(value: str) -> datetime:
    """파일의 `...Z` 시각을 시간대 없는 UTC로."""
    return datetime.strptime(value, _TS + "Z")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc(now: Optional[datetime]) -> datetime:
    now = _utcnow() if now is None else now
    if now.tzinfo is None:
        raise ExportError("now에는 시간대가 있어야 한다")
    return now.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- SQL


def _sql_ts(value: datetime) -> str:
    # 값은 Window가 검증한 datetime에서만 온다 - 사용자 문자열을 SQL에 끼우지 않는다.
    return "TIMESTAMP '" + value.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"


def _json_ts(expr: str, *, with_zone: bool) -> str:
    source = f"{expr} AT TIME ZONE 'UTC'" if with_zone else expr
    return f"to_char({source}, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"


def preamble_statements(statement_timeout_s: int = 120, lock_timeout_s: int = 5) -> List[str]:
    if not 1 <= int(statement_timeout_s) <= 600 or not 1 <= int(lock_timeout_s) <= 60:
        raise ExportError("시간 제한은 문장 1~600초, 락 1~60초 사이로 준다")
    return [
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        f"SET LOCAL statement_timeout = '{int(statement_timeout_s)}s'",
        f"SET LOCAL lock_timeout = '{int(lock_timeout_s)}s'",
        "SET LOCAL idle_in_transaction_session_timeout = '60s'",
        "SET LOCAL client_encoding = 'UTF8'",
        "SET LOCAL application_name = 'frozen_export_news_raw'",
    ]


def copy_statements(window: Window) -> List[str]:
    """머리말 1줄, 행마다 1줄, 꼬리말 1줄을 내는 COPY 세 개. 세 문장이 같은 스냅숏을 본다."""
    where = (
        f"{ROW_FILTER} AND n.raw_news_crawled_at >= {_sql_ts(window.start)} "
        f"AND n.raw_news_crawled_at < {_sql_ts(window.end)}"
    )
    source = f"FROM news_raw n JOIN press p ON p.press_id = n.press_id WHERE {where}"
    header = (
        "COPY (SELECT json_build_object('kind', 'header', "
        f"'snapshot_at_utc', {_json_ts('now()', with_zone=True)}, "
        "'alembic_revision', (SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version), "
        "'server_version', current_setting('server_version'), "
        "'server_encoding', current_setting('server_encoding'), "
        "'transaction_read_only', current_setting('transaction_read_only'), "
        "'transaction_isolation', current_setting('transaction_isolation'), "
        "'statement_timeout', current_setting('statement_timeout'), "
        "'txid_snapshot', pg_current_snapshot()::text)::text) TO STDOUT"
    )
    rows = (
        "COPY (SELECT json_build_object('kind', 'row', 'id', n.raw_news_id, 'press', p.press_name, "
        "'url', n.raw_news_url, 'title', n.raw_news_title, 'body', n.raw_news_content, "
        f"'created_at', {_json_ts('n.raw_news_created_at', with_zone=True)}, "
        f"'crawled_at', {_json_ts('n.raw_news_crawled_at', with_zone=False)}, "
        f"'extracted_at', {_json_ts('n.raw_news_extracted_at', with_zone=True)}, "
        "'extract_status', n.raw_news_extract_status, "
        f"'content_sha256', n.raw_news_content_sha256)::text {source}) TO STDOUT"
    )
    trailer = (
        "COPY (SELECT json_build_object('kind', 'trailer', 'rows', count(*), "
        f"'min_id', min(n.raw_news_id), 'max_id', max(n.raw_news_id))::text {source}) TO STDOUT"
    )
    return [header, rows, trailer]


def build_psql_script(window: Window, *, statement_timeout_s: int = 120, lock_timeout_s: int = 5) -> str:
    """psql이 표준입력으로 받는 스크립트. 연결 경로(psycopg2)도 같은 문장을 같은 순서로 실행한다."""
    lines = ["\\set ON_ERROR_STOP on", "\\set QUIET on"]
    lines += [s + ";" for s in preamble_statements(statement_timeout_s, lock_timeout_s)]
    lines += [s + ";" for s in copy_statements(window)]
    lines.append("ROLLBACK;")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- COPY 출력 읽기

_COPY_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_COPY_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def _copy_unescape(text: str) -> str:
    """COPY 텍스트 형식의 역슬래시 표기를 되돌린다. json 텍스트에는 날 제어 문자가 없어서 실제로는 `\\\\`뿐이다."""
    if "\\" not in text:
        return text
    return _COPY_ESCAPE_RE.sub(lambda m: _COPY_ESCAPES.get(m.group(1), m.group(1)), text)


@dataclass
class RawExport:
    header: Dict[str, Any]
    rows: List[Dict[str, Any]]
    trailer: Dict[str, Any]


def parse_copy_stream(lines: Iterable[bytes]) -> RawExport:
    """머리말·행·꼬리말 줄을 읽는다. 기대하지 않은 줄이 하나라도 있으면 거절한다(내용은 메시지에 싣지 않는다)."""
    header: Optional[Dict[str, Any]] = None
    trailer: Optional[Dict[str, Any]] = None
    rows: List[Dict[str, Any]] = []
    for number, raw in enumerate(lines, start=1):
        raw = raw.rstrip(b"\n")
        if not raw:
            continue
        try:
            obj = json.loads(_copy_unescape(raw.decode("utf-8")))
            kind = obj["kind"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise ExportError(f"출력의 {number}번째 줄을 읽을 수 없다(내용은 싣지 않는다)") from None
        if trailer is not None:
            raise ExportError(f"꼬리말 뒤에 {number}번째 줄이 더 있다")
        if kind == "header" and header is None and not rows:
            header = obj
        elif kind == "row" and header is not None:
            rows.append(obj)
        elif kind == "trailer" and header is not None:
            trailer = obj
        else:
            raise ExportError(f"출력의 {number}번째 줄이 순서에 맞지 않는다(종류 {kind!r})")
    if header is None:
        raise ExportError("머리말이 없다 - 쿼리가 실행되지 않았다")
    if trailer is None:
        raise ExportError("꼬리말이 없다 - 출력이 중간에 끊겼다")
    if trailer.get("rows") != len(rows):
        raise ExportError(f"행 수가 맞지 않는다: 서버 {trailer.get('rows')}행, 받은 것 {len(rows)}행")
    read_only = header.get("transaction_read_only") == "on"
    snapshot = header.get("transaction_isolation") == "repeatable read"
    bounded = str(header.get("statement_timeout", "0")) not in ("0", "")
    if not (read_only and snapshot and bounded):
        raise ExportError(
            "서버가 읽기 전용 스냅숏 트랜잭션과 문장 시간 제한을 확인해 주지 않았다 "
            f"(read_only={header.get('transaction_read_only')!r}, isolation={header.get('transaction_isolation')!r}, "
            f"statement_timeout={header.get('statement_timeout')!r})"
        )
    return RawExport(header, rows, trailer)


# ---------------------------------------------------------------- DB에 닿는 두 길


@contextmanager
def read_only_snapshot(conn, *, statement_timeout_s: int = 120, lock_timeout_s: int = 5):
    """psycopg2 연결에서 반출 트랜잭션을 열고 커서를 준다. 어떻게 끝나든 ROLLBACK한다."""
    previous = conn.autocommit
    conn.autocommit = True  # BEGIN/ROLLBACK을 직접 보낸다 - psql 스크립트와 같은 문장이다
    cur = conn.cursor()
    try:
        for statement in preamble_statements(statement_timeout_s, lock_timeout_s):
            cur.execute(statement)
        yield cur
    finally:
        try:
            cur.execute("ROLLBACK")
        finally:
            cur.close()
            conn.autocommit = previous


def fetch_with_connection(conn, window: Window, *, statement_timeout_s: int = 120, lock_timeout_s: int = 5) -> RawExport:
    # 메모리에 받는다. 임시 파일을 쓰면 본문 사본이 허용 위치(data/exports) 밖의 디스크에 잠깐 생긴다.
    buffer = io.BytesIO()
    with read_only_snapshot(conn, statement_timeout_s=statement_timeout_s, lock_timeout_s=lock_timeout_s) as cur:
        for statement in copy_statements(window):
            cur.copy_expert(statement, buffer)
    return parse_copy_stream(buffer.getvalue().split(b"\n"))


def _stderr_tail(data: bytes, lines: int = 5, width: int = 300) -> str:
    text = data.decode("utf-8", errors="replace").strip().splitlines()
    return " | ".join(line[:width] for line in text[-lines:])


def fetch_with_psql(command: Sequence[str], window: Window, *, statement_timeout_s: int = 120,
                    lock_timeout_s: int = 5, process_timeout_s: int = 900) -> RawExport:
    """스크립트를 표준입력으로 받는 psql 명령(보통 ssh로 감싼 `docker compose exec -T db psql ...`)을 돌린다."""
    script = build_psql_script(window, statement_timeout_s=statement_timeout_s, lock_timeout_s=lock_timeout_s)
    try:
        done = subprocess.run(list(command), input=script.encode("utf-8"), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=process_timeout_s)
    except subprocess.TimeoutExpired:
        raise ExportError(f"psql 명령이 {process_timeout_s}초 안에 끝나지 않았다") from None
    except OSError as e:
        raise ExportError(f"psql 명령을 실행할 수 없다: {type(e).__name__}") from None
    if done.returncode != 0:
        raise ExportError(f"psql 명령이 종료 코드 {done.returncode}로 끝났다: {_stderr_tail(done.stderr)}")
    return parse_copy_stream(done.stdout.split(b"\n"))


# ---------------------------------------------------------------- 행과 매니페스트


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _license_lookup() -> Callable[[str], bool]:
    """config.sources.is_redistributable. 읽지 못하면 아무것도 재배포 가능하지 않다고 본다(fail closed)."""
    path = REPO_ROOT / "ai_workspace" / "config" / "sources.py"
    try:
        spec = importlib.util.spec_from_file_location("_frozen_export_sources", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclass가 모듈을 sys.modules에서 찾는다
        spec.loader.exec_module(module)
        return module.is_redistributable
    except Exception:  # noqa: BLE001
        logger.warning("출처 라이선스 표를 읽지 못했다 - 모든 행에 30일 규칙을 건다")
        return lambda press: False


def rows_sha256(rows: Sequence[Dict[str, Any]]) -> str:
    """행별 신원(id·해시·시각 등, 본문 제외)을 id 순으로 이어 붙인 것의 sha256. 본문을 지워도 같다."""
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r["id"]):
        digest.update(canonical_json({k: row[k] for k in _IDENTITY_KEYS}).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass
class ExportBundle:
    rows: List[Dict[str, Any]]
    manifest: Dict[str, Any]


def build_export(raw: RawExport, window: Window, *, code: Dict[str, Any],
                 is_redistributable: Optional[Callable[[str], bool]] = None,
                 plan: Optional[Dict[str, Any]] = None) -> ExportBundle:
    """서버가 준 행을 파일의 행으로 바꾸고(본문 해시는 다시 계산한다) 매니페스트를 만든다.

    `plan`(`plan_windows`가 만든 E0 날짜 창 계획)을 주면 서버 스냅숏 시각으로 다시 계산해 대조하고 신원에 넣는다.
    """
    try:
        snapshot = _parse(raw.header["snapshot_at_utc"])
    except (KeyError, TypeError, ValueError):
        raise ExportError("머리말에 서버 스냅숏 시각이 없다") from None
    if plan is not None:
        _plan_check(plan, window, snapshot)
    is_redistributable = is_redistributable or _license_lookup()
    rows: List[Dict[str, Any]] = []
    seen = set()
    hash_mismatch: List[int] = []
    hash_missing: List[int] = []
    for src in raw.rows:
        row_id = int(src["id"])
        if row_id in seen:
            raise ExportError(f"id {row_id}가 중복돼 있다")
        seen.add(row_id)
        body = src.get("body")
        if not isinstance(body, str) or not body:
            raise ExportError(f"id {row_id}의 본문이 비어 있다 - 창의 조건과 맞지 않는다")
        if src.get("extract_status") != "ok":
            raise ExportError(f"id {row_id}의 추출 상태가 ok가 아니다")
        crawled = _parse(src["crawled_at"])
        if not window.start <= crawled < window.end:
            raise ExportError(f"id {row_id}의 수집 시각이 창 밖이다")
        digest = _sha256_text(body)
        stored = src.get("content_sha256")
        if stored is None:
            hash_missing.append(row_id)
        elif stored != digest:
            hash_mismatch.append(row_id)
        press = str(src["press"])
        exempt = bool(is_redistributable(press))
        rows.append({
            "id": row_id,
            "press": press,
            "url": str(src["url"]),
            "title": str(src["title"]),
            "title_sha256": _sha256_text(str(src["title"])),
            "body": body,
            "body_chars": len(body),
            "content_sha256": digest,
            "created_at": src.get("created_at"),
            "crawled_at": src["crawled_at"],
            "extracted_at": src.get("extracted_at"),
            "extract_status": "ok",
            "redistributable": exempt,
            "body_expires_at": None if exempt else _fmt(crawled + timedelta(days=BODY_RETENTION_DAYS)),
            "body_purged_at": None,
        })
    rows.sort(key=lambda r: r["id"])

    by_press: Dict[str, int] = {}
    for row in rows:
        by_press[row["press"]] = by_press.get(row["press"], 0) + 1
    expiries = sorted(r["body_expires_at"] for r in rows if r["body_expires_at"])
    crawled_at = sorted(r["crawled_at"] for r in rows)
    header = raw.header
    identity = {
        "format": FORMAT,
        "table": "news_raw",
        "filter": ROW_FILTER,
        "window_utc": {"start": _fmt(window.start), "end": _fmt(window.end),
                       "bound": "start <= raw_news_crawled_at < end"},
        "plan": dict(plan) if plan is not None else None,
        "rows": len(rows),
        "rows_sha256": rows_sha256(rows),
        "id_range": [rows[0]["id"], rows[-1]["id"]] if rows else None,
        "crawled_at_range_utc": [crawled_at[0], crawled_at[-1]] if rows else None,
        "crawled_at_note": "timestamp without time zone, UTC로 읽는다(ADR 0026)",
        "by_press": dict(sorted(by_press.items())),
        "source": {
            "snapshot_at_utc": header.get("snapshot_at_utc"),
            "alembic_revision": header.get("alembic_revision"),
            "server_version": header.get("server_version"),
            "transaction_read_only": header.get("transaction_read_only"),
            "transaction_isolation": header.get("transaction_isolation"),
            "statement_timeout": header.get("statement_timeout"),
            "txid_snapshot": header.get("txid_snapshot"),
        },
        "code": {"git_sha": code.get("git_sha"), "dirty": bool(code.get("dirty"))},
        "retention": {
            "adr": "0023",
            "days": BODY_RETENTION_DAYS,
            "rule": "body_expires_at = crawled_at + days (재배포 가능 출처는 대상 아님)",
            "first_body_expiry_utc": expiries[0] if expiries else None,
            "last_body_expiry_utc": expiries[-1] if expiries else None,
            "exempt_rows": sum(1 for r in rows if r["redistributable"]),
        },
        "db_hash_check": {"mismatch": len(hash_mismatch), "missing": len(hash_missing),
                          "ids": sorted(hash_mismatch + hash_missing)[:50]},
    }
    manifest = {
        "format": FORMAT,
        "identity": identity,
        "identity_sha256": _sha256_text(canonical_json(identity)),
        "files": {},
        "purges": [],
    }
    return ExportBundle(rows, manifest)


# ---------------------------------------------------------------- 쓰는 곳


def _git(cwd, *args: str) -> "subprocess.CompletedProcess[str]":
    # 메시지로 "작업 트리 밖"을 가려내므로 git의 출력 언어를 고정한다.
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)


def ensure_private_destination(path: Path) -> Path:
    """본문이 든 파일을 써도 되는 곳이면 절대 경로를 돌려준다. git이 추적하거나 추적할 수 있으면 거절한다.

    - git 작업 트리 안이면 `.gitignore`가 그 경로를 무시해야 하고, 그 아래에 이미 추적 중인 파일이 없어야 한다.
    - `.git` 디렉터리 안은 받지 않는다.
    - git 작업 트리 밖(임시 디렉터리 등)은 git이 추적할 수 없으므로 받는다.
    """
    path = Path(path).expanduser().resolve()
    if ".git" in path.parts:
        raise DestinationRefused(f"{path}: .git 안에는 쓰지 않는다")
    probe = path
    while not probe.exists():
        probe = probe.parent
    if not probe.is_dir():
        probe = probe.parent
    try:
        top = _git(probe, "rev-parse", "--show-toplevel")
    except OSError:
        raise DestinationRefused("git을 실행할 수 없어 쓰는 곳이 추적 대상인지 확인하지 못했다") from None
    if top.returncode != 0:
        if "not a git repository" in top.stderr.lower():
            return path  # 작업 트리 밖
        raise DestinationRefused(f"{path}: git 작업 트리인지 확인하지 못했다 - 본문을 쓰지 않는다")
    root = top.stdout.strip()
    if _git(root, "check-ignore", "-q", "--", str(path)).returncode != 0:
        raise DestinationRefused(f"{path}: git이 추적할 수 있는 곳이다(.gitignore에 없다) - 본문을 쓰지 않는다")
    if _git(root, "ls-files", "--", str(path)).stdout.strip():
        raise DestinationRefused(f"{path}: 그 아래에 git이 추적하는 파일이 있다 - 본문을 쓰지 않는다")
    return path


def main_checkout_data() -> Path:
    """`<메인 체크아웃>/data`. 워크트리에서 돌려도 메인 체크아웃을 가리킨다.

    워크트리는 지워지는 디렉터리라 거기에 본문 사본을 두면 정리 대상에서 빠진 채 잊힌다. 테스트는 이 함수를
    바꿔 끼워 임시 디렉터리를 메인 체크아웃처럼 쓴다.
    """
    try:
        common = _git(REPO_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
        git_dir = Path(common.stdout.strip())
        if common.returncode == 0 and git_dir.name == ".git":
            return git_dir.parent / "data"
    except OSError:
        pass
    return REPO_ROOT / "data"


def default_exports_root() -> Path:
    """`<메인 체크아웃>/data/exports` - 본문이 든 반출본이 놓일 수 있는 유일한 곳(ADR 0023 개정)."""
    return main_checkout_data() / "exports"


def ensure_under(path: Path, root: Path, *, what: str, strictly: bool = False) -> Path:
    """`path`가 `root`이거나 그 아래면(`strictly`면 그 아래만) 절대 경로를 돌려준다. 이름만 같은 다른 곳
    (`~/Dropbox/x/data/exports`, 시스템 임시 디렉터리)은 받지 않는다. 그다음 git이 추적할 수 없는 곳인지도 본다."""
    root = Path(root).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    inside = root in resolved.parents or (resolved == root and not strictly)
    if not inside:
        raise DestinationRefused(f"{resolved}: {what}은 {root} 아래에만 둔다(ADR 0023 개정의 허용 위치)")
    return ensure_private_destination(resolved)


def ensure_export_location(path: Path) -> Path:
    return ensure_under(path, default_exports_root(), what="본문이 든 반출본")


def _write_private(path: Path, data: bytes) -> None:
    """같은 디렉터리의 임시 파일(0600)에 쓰고 fsync한 뒤 이름을 바꾼다."""
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _articles_bytes(rows: Sequence[Dict[str, Any]]) -> bytes:
    return "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")


def _file_entry(data: bytes, rows: Sequence[Dict[str, Any]], *, at_export: Optional[str] = None) -> Dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    pending = sorted(r["body_expires_at"] for r in rows if r["body"] is not None and r["body_expires_at"])
    return {
        "sha256": digest,
        "sha256_at_export": at_export or digest,
        "bytes": len(data),
        "rows": len(rows),
        "rows_with_body": sum(1 for r in rows if r["body"] is not None),
        "next_body_expiry_utc": pending[0] if pending else None,
    }


def _manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_export(dest: Path, bundle: ExportBundle) -> Path:
    """반출본을 쓴다. 매니페스트가 먼저다 - 본문 파일이 있는 디렉터리에는 언제나 매니페스트가 있어
    정리(`purge --all`)가 그 디렉터리를 찾는다. 본문 파일을 쓰다 끊기면 반출본은 열리지 않는다(다시 반출한다)."""
    dest = ensure_export_location(Path(dest))
    if (dest / MANIFEST_FILE).exists() or (dest / ARTICLES_FILE).exists():
        raise DestinationRefused(f"{dest}: 이미 반출본이 있다 - 동결된 반출본을 덮어쓰지 않는다")
    old_umask = os.umask(0o077)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        data = _articles_bytes(bundle.rows)
        manifest = dict(bundle.manifest)
        manifest["files"] = {ARTICLES_FILE: _file_entry(data, bundle.rows)}
        _write_private(dest / MANIFEST_FILE, _manifest_bytes(manifest))
        _write_private(dest / ARTICLES_FILE, data)
    finally:
        os.umask(old_umask)
    return dest


# ---------------------------------------------------------------- 읽기, 만료, 정리


@dataclass
class FrozenExport:
    dir: Path
    manifest: Dict[str, Any]
    rows: List[Dict[str, Any]]
    # 만료됐지만 디스크에는 아직 본문이 남아 있는 행 수(purge_expired=False로 열었을 때만 0이 아니다).
    expired_unpurged: int = 0

    @property
    def identity(self) -> Dict[str, Any]:
        return self.manifest["identity"]

    @property
    def identity_sha256(self) -> str:
        return self.manifest["identity_sha256"]

    @property
    def ids(self) -> List[int]:
        return [r["id"] for r in self.rows]

    def day_windows(self) -> List[Window]:
        """E0의 날짜 창(ADR 0009 A8.1). 반출 때 적은 계획을 반출본의 T0(서버 스냅숏 시각)로 다시 계산해 대조한다."""
        plan = self.identity.get("plan")
        if not plan:
            raise ExportError("날짜 창 계획 없이(--start-utc/--end-utc로) 만든 반출본이다 - E0에는 --days로 반출한 것을 쓴다")
        snapshot = _parse(self.identity["source"]["snapshot_at_utc"])
        window = self.identity["window_utc"]
        _plan_check(plan, Window(_parse(window["start"]), _parse(window["end"])), snapshot)
        return [Window(_parse(start), _parse(end)) for start, end in plan["day_windows_utc"]]


def _read_manifest(directory: Path) -> Dict[str, Any]:
    try:
        manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ExportIntegrityError(f"{directory}: 매니페스트를 읽을 수 없다({type(e).__name__})") from None
    if manifest.get("format") != FORMAT:
        raise ExportIntegrityError(f"{directory}: 모르는 형식 {manifest.get('format')!r}")
    if _sha256_text(canonical_json(manifest["identity"])) != manifest.get("identity_sha256"):
        raise ExportIntegrityError(f"{directory}: 매니페스트의 identity가 identity_sha256과 다르다")
    return manifest


def _read_rows(directory: Path, manifest: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """파일의 행을 읽고 매니페스트의 신원과 맞는지 확인한다. 돌려주는 것: 행과 파일의 sha256.

    행마다 확인한다: 열 구성이 정해진 그대로다, 제목이 title_sha256과 같다, 남아 있는 본문이 content_sha256·
    body_chars와 같다, 본문이 비었으면 정리 시각이 있고 본문이 있으면 없다. 그다음 신원(rows_sha256)을 대조한다.
    그래서 identity_sha256이 같으면 남아 있는 제목·본문·URL·시각은 반출 때와 같은 바이트다.
    """
    try:
        data = (directory / ARTICLES_FILE).read_bytes()
    except OSError as e:
        raise ExportIntegrityError(f"{directory}: 본문 파일을 읽을 수 없다({type(e).__name__})") from None
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(data.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            raise ExportIntegrityError(f"{directory}: 본문 파일 {number}번째 줄을 읽을 수 없다") from None
    identity = manifest["identity"]
    if len(rows) != identity["rows"]:
        raise ExportIntegrityError(f"{directory}: 행 수 {len(rows)} ≠ 매니페스트 {identity['rows']}")
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise ExportIntegrityError(f"{directory}: {number}번째 행의 열 구성이 반출 형식과 다르다")
        title = row["title"]
        if not isinstance(title, str) or _sha256_text(title) != row["title_sha256"]:
            raise ExportIntegrityError(f"{directory}: id {row['id']}의 제목이 기록된 해시와 다르다")
        body = row["body"]
        if (body is None) != (row["body_purged_at"] is not None):
            raise ExportIntegrityError(f"{directory}: id {row['id']}의 본문과 정리 기록(body_purged_at)이 맞지 않는다")
        if body is None:
            continue
        if not isinstance(body, str) or _sha256_text(body) != row["content_sha256"] or len(body) != row["body_chars"]:
            raise ExportIntegrityError(f"{directory}: id {row['id']}의 본문이 기록된 해시와 다르다")
    try:
        digest = rows_sha256(rows)
    except (TypeError, ValueError):
        raise ExportIntegrityError(f"{directory}: 행의 값을 신원으로 읽을 수 없다") from None
    if digest != identity["rows_sha256"]:
        raise ExportIntegrityError(f"{directory}: 행의 신원(rows_sha256)이 매니페스트와 다르다")
    return rows, hashlib.sha256(data).hexdigest()


def _is_expired(row: Dict[str, Any], now: datetime) -> bool:
    return row["body_expires_at"] is not None and _parse(row["body_expires_at"]) <= now


def _rewrite(directory: Path, manifest: Dict[str, Any], rows: List[Dict[str, Any]], *, now: datetime,
             blanked: int, reason: str) -> Dict[str, Any]:
    """정리 결과를 쓴다. **매니페스트가 먼저, 본문 파일이 나중이다.**

    매니페스트의 정리 기록에는 시각·사유·전후 sha256이 있다. 둘 사이에서 끊기면 디스크의 본문 파일은 정리 전
    그대로(`sha256_before`)이고, 다음에 열 때 `_open`이 같은 시각·사유로 같은 정리를 다시 해 `sha256_after`와
    같은지 확인하고 끝낸다. 반대 순서(본문 파일이 매니페스트보다 앞선 상태)는 생기지 않으므로 그런 파일은 거절한다.
    """
    previous = manifest["files"][ARTICLES_FILE]
    data = _articles_bytes(rows)
    entry = _file_entry(data, rows, at_export=previous.get("sha256_at_export") or previous["sha256"])
    manifest = dict(manifest)
    manifest["files"] = {**manifest["files"], ARTICLES_FILE: entry}
    manifest["purges"] = list(manifest.get("purges", [])) + [{
        "at_utc": _fmt(now), "reason": reason, "rows_blanked": blanked,
        "sha256_before": previous["sha256"], "sha256_after": entry["sha256"],
    }]
    old_umask = os.umask(0o077)
    try:
        _write_private(directory / MANIFEST_FILE, _manifest_bytes(manifest))
        _write_private(directory / ARTICLES_FILE, data)
    finally:
        os.umask(old_umask)
    return manifest


def _blank(rows: List[Dict[str, Any]], now: datetime, *, everything: bool) -> int:
    blanked = 0
    for row in rows:
        if row["body"] is not None and (everything or _is_expired(row, now)):
            row["body"] = None
            row["body_purged_at"] = _fmt(now)
            blanked += 1
    return blanked


def _open(directory: Path, *, repair: bool = True) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
    """매니페스트와 행을 읽어 검증한다. 돌려주는 것: 매니페스트, 행, 디스크에 아직 남은 "지워졌어야 할" 본문 수.

    디스크의 본문 파일은 매니페스트에 적힌 sha256과 같아야 한다. 다르면 받아들이는 경우는 하나뿐이다: 마지막
    정리가 매니페스트를 쓴 뒤 본문 파일을 쓰기 전에 끊긴 경우(`_rewrite`). 그때 파일은 마지막 정리의
    `sha256_before`와 같고, 같은 시각·사유로 정리를 다시 하면 `sha256_after`가 나와야 한다. 그러면 그 정리를
    끝낸다(`repair=False`면 메모리에서만 비운다). 그 밖의 불일치는 손댄 파일로 보고 거절한다.
    """
    if repair:
        for name in (ARTICLES_FILE, MANIFEST_FILE):  # 끊긴 쓰기가 남긴 임시 파일(진짜 파일이 기준이다)
            (directory / (name + ".tmp")).unlink(missing_ok=True)
    manifest = _read_manifest(directory)
    rows, on_disk = _read_rows(directory, manifest)
    entry = manifest["files"].get(ARTICLES_FILE) or {}
    if on_disk == entry.get("sha256"):
        return manifest, rows, 0
    purges = manifest.get("purges") or []
    last = purges[-1] if purges else {}
    if not last or on_disk != last.get("sha256_before") or last.get("sha256_after") != entry.get("sha256"):
        raise ExportIntegrityError(f"{directory}: 본문 파일이 매니페스트와 다르다(끊긴 정리로 설명되지 않는다)")
    pending = _blank(rows, _parse(last["at_utc"]), everything=last.get("reason") == "everything")
    data = _articles_bytes(rows)
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ExportIntegrityError(f"{directory}: 끊긴 정리를 다시 해도 매니페스트의 본문 파일과 같아지지 않는다")
    if not repair:
        return manifest, rows, pending
    old_umask = os.umask(0o077)
    try:
        _write_private(directory / ARTICLES_FILE, data)
    finally:
        os.umask(old_umask)
    logger.info("끊겼던 정리를 마쳤다: %s (본문 %d건)", directory, pending)
    return manifest, rows, 0


def load_export(directory: Path, *, now: Optional[datetime] = None, purge_expired: bool = True) -> FrozenExport:
    """반출본을 검증해 읽는다. 만료된 본문은 돌려주지 않고, 기본으로 디스크에서도 지운다.

    `purge_expired=False`는 파일을 고칠 수 없는 경우를 위한 것이다. 그때도 만료된 본문은 메모리로 나오지
    않고(`body=None`), 디스크에 남은 그런 본문의 수가 `expired_unpurged`에 적힌다.
    """
    directory = Path(directory)
    now_utc = _naive_utc(now)
    manifest, rows, pending = _open(directory, repair=purge_expired)
    if purge_expired:
        blanked = _blank(rows, now_utc, everything=False)
        if blanked:
            manifest = _rewrite(directory, manifest, rows, now=now_utc, blanked=blanked, reason="expired")
        return FrozenExport(directory, manifest, rows)
    expired = 0
    for row in rows:
        if row["body"] is not None and _is_expired(row, now_utc):
            row["body"] = None  # 디스크는 그대로지만 호출자에게는 주지 않는다
            expired += 1
    return FrozenExport(directory, manifest, rows, expired_unpurged=expired + pending)


def purge_export(directory: Path, *, now: Optional[datetime] = None, everything: bool = False) -> Dict[str, Any]:
    """만료된 본문(또는 `everything`이면 전부)을 지우고 해시·길이·제목·URL은 남긴다. 여러 번 돌려도 같다."""
    directory = Path(directory)
    now_utc = _naive_utc(now)
    manifest, rows, _ = _open(directory)
    blanked = _blank(rows, now_utc, everything=everything)
    if blanked:
        manifest = _rewrite(directory, manifest, rows, now=now_utc, blanked=blanked,
                            reason="everything" if everything else "expired")
    entry = manifest["files"][ARTICLES_FILE]
    return {
        "dir": str(directory),
        "identity_sha256": manifest["identity_sha256"],
        "rows": entry["rows"],
        "rows_blanked": blanked,
        "rows_with_body": sum(1 for r in rows if r["body"] is not None),
        "next_body_expiry_utc": entry.get("next_body_expiry_utc"),
        "articles_sha256": entry["sha256"],
    }


# ---------------------------------------------------------------- 실행


def current_code() -> Dict[str, Any]:
    def git(*args: str) -> str:
        return _git(REPO_ROOT, *args).stdout.strip()

    return {"git_sha": git("rev-parse", "HEAD") or None, "dirty": bool(git("status", "--porcelain"))}


def summarize(directory: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """화면과 사전 등록 기록에 옮겨 적는 값. 기사 텍스트는 없다."""
    identity = manifest["identity"]
    entry = manifest["files"][ARTICLES_FILE]
    return {
        "dir": str(directory),
        "identity_sha256": manifest["identity_sha256"],
        "rows": identity["rows"],
        "window_utc": [identity["window_utc"]["start"], identity["window_utc"]["end"]],
        "snapshot_at_utc": identity["source"]["snapshot_at_utc"],
        "alembic_revision": identity["source"]["alembic_revision"],
        "code_sha": identity["code"]["git_sha"],
        "code_dirty": identity["code"]["dirty"],
        "by_press": identity["by_press"],
        "db_hash_check": {k: identity["db_hash_check"][k] for k in ("mismatch", "missing")},
        "body_expires_at_utc": identity["retention"]["first_body_expiry_utc"],
        "last_body_expiry_utc": identity["retention"]["last_body_expiry_utc"],
        "articles_sha256": entry["sha256"],
        "articles_bytes": entry["bytes"],
        "rows_with_body": entry["rows_with_body"],
        "purges": len(manifest.get("purges", [])),
        "plan": ({k: identity["plan"][k] for k in ("t0_kst", "days", "day_windows_utc")}
                 if identity.get("plan") else None),
    }


def export_news_raw(window: Window, *, root: Optional[Path] = None, conn=None,
                    psql_command: Optional[Sequence[str]] = None, code: Optional[Dict[str, Any]] = None,
                    statement_timeout_s: int = 120, lock_timeout_s: int = 5,
                    plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """창 하나를 반출해 `<root>/<T0>/`에 쓰고 요약을 돌려준다. T0는 서버의 트랜잭션 시작 시각(UTC)이다.

    E0의 반출이면 `plan_windows(T0, days)`의 (창, 계획)을 준다 - 서버 T0의 KST 날짜가 계획과 다르면 쓰지 않는다.
    """
    if (conn is None) == (psql_command is None):
        raise ExportError("DB에 닿는 길을 하나만 준다: conn 또는 psql_command")
    root = ensure_export_location(Path(root) if root is not None else default_exports_root())
    if conn is not None:
        raw = fetch_with_connection(conn, window, statement_timeout_s=statement_timeout_s, lock_timeout_s=lock_timeout_s)
    else:
        raw = fetch_with_psql(psql_command, window, statement_timeout_s=statement_timeout_s,
                              lock_timeout_s=lock_timeout_s)
    bundle = build_export(raw, window, code=code if code is not None else current_code(), plan=plan)
    snapshot = _parse(bundle.manifest["identity"]["source"]["snapshot_at_utc"])
    dest = write_export(root / snapshot.strftime("%Y%m%dT%H%M%SZ"), bundle)
    manifest = _read_manifest(dest)
    logger.info("반출 완료: %d행, identity_sha256=%s", manifest["identity"]["rows"], manifest["identity_sha256"])
    return summarize(dest, manifest)


def _minute_now() -> int:
    return datetime.now().minute


def _cli_root(value: Optional[str]) -> Path:
    """`--root`는 메인 체크아웃의 data/exports이거나 그 아래여야 한다(이름만 같은 다른 곳은 받지 않는다)."""
    return ensure_export_location(Path(value) if value else default_exports_root())


def _emit(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _t0(value: Optional[str]) -> datetime:
    """`--t0-kst` 값(시간대가 없으면 KST로 읽는다). 주지 않으면 지금."""
    if not value:
        return _utcnow().astimezone(KST)
    try:
        t0 = datetime.fromisoformat(value)
    except ValueError:
        raise WindowRefused(f"T0를 읽을 수 없다: {value!r}") from None
    return t0.replace(tzinfo=KST) if t0.tzinfo is None else t0


def _cmd_plan_window(args) -> int:
    t0 = _t0(args.t0_kst)
    union, plan = plan_windows(t0, args.days)
    _emit({
        "t0_kst": plan["t0_kst"],
        "start_utc": union.start.isoformat(),
        "end_utc": union.end.isoformat(),
        "hours": union.hours,
        "day_windows_utc": [[_parse(a).isoformat(), _parse(b).isoformat()] for a, b in plan["day_windows_utc"]],
        "pseudo_now_kst": [_parse(b).replace(tzinfo=timezone.utc).astimezone(KST).isoformat()
                           for _, b in plan["day_windows_utc"]],
        "note": "미리 보기다. export --days가 같은 계산을 다시 하고 서버 스냅숏의 KST 날짜와 대조한다",
    })
    return 0


def _export_window(args) -> Tuple[Window, Optional[Dict[str, Any]]]:
    if args.days is not None:
        if args.start_utc or args.end_utc:
            raise WindowRefused("--days와 --start-utc/--end-utc 중 하나만 준다")
        return plan_windows(_t0(args.t0_kst), args.days)
    if args.t0_kst:
        raise WindowRefused("--t0-kst는 --days와 함께 준다")
    if not (args.start_utc and args.end_utc):
        raise WindowRefused("--days(E0 날짜 창) 또는 --start-utc와 --end-utc를 준다")
    return Window.from_iso(args.start_utc, args.end_utc), None


def _cmd_export(args) -> int:
    window, plan = _export_window(args)
    root = _cli_root(args.root)
    if not args.any_minute and not INGEST_GAP_MINUTES[0] <= _minute_now() < INGEST_GAP_MINUTES[1]:
        raise ExportError(
            f"지금은 매시 :{INGEST_GAP_MINUTES[0]}~:{INGEST_GAP_MINUTES[1]} 밖이다 - 정시 ingest와 겹치지 않게 그 사이에 "
            "돌린다(다른 DB라면 --any-minute)"
        )
    if bool(args.dsn_env) == bool(args.psql_command):
        raise ExportError("--dsn-env와 --psql-command 중 하나만 준다")
    conn = None
    try:
        if args.dsn_env:
            dsn = os.environ.get(args.dsn_env)
            if not dsn:
                raise ExportError(f"환경변수 {args.dsn_env}가 비어 있다")
            import psycopg2

            conn = psycopg2.connect(dsn, connect_timeout=10)
        command = shlex.split(args.psql_command) if args.psql_command else None
        _emit(export_news_raw(window, root=root, conn=conn, psql_command=command,
                              statement_timeout_s=args.statement_timeout_s, plan=plan))
    finally:
        if conn is not None:
            conn.close()
    return 0


def _cmd_verify(args) -> int:
    loaded = load_export(Path(args.dir))
    _emit(summarize(loaded.dir, loaded.manifest))
    return 0


def purge_all(root: Path, *, now: Optional[datetime] = None, everything: bool = False) -> List[Dict[str, Any]]:
    """`root` 바로 아래의 모든 반출본을 정리한다. 읽을 수 없는 반출본과, 매니페스트 없이 본문 파일만 남은
    디렉터리는 건너뛰지 않고 그 사실을 적는다(본문이 남아 있을 수 있으니 사람이 확인한다)."""
    results: List[Dict[str, Any]] = []
    root = Path(root)
    children = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for child in children:
        if (child / MANIFEST_FILE).exists():
            try:
                results.append(purge_export(child, now=now, everything=everything))
            except ExportError as e:
                results.append({"dir": str(child), "error": str(e)})
        elif any((child / name).exists() for name in (ARTICLES_FILE, ARTICLES_FILE + ".tmp")):
            results.append({"dir": str(child), "error": "매니페스트 없이 본문 파일이 남아 있다 - 직접 확인하고 지운다"})
    return results


def _cmd_purge(args) -> int:
    if bool(args.dir) == bool(args.all):
        raise ExportError("--dir와 --all 중 하나만 준다")
    if args.dir:
        _emit(purge_export(Path(args.dir), everything=args.everything))
        return 0
    results = purge_all(_cli_root(args.root), everything=args.everything)
    _emit({"exports": results})
    return 1 if any("error" in r for r in results) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="news_raw 동결 반출(읽기 전용)·검증·30일 정리")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan-window", help="T0에서 등록된 날짜 창과 반출 창(UTC)을 계산한다")
    plan.add_argument("--t0-kst", help="반출 예정 시각(KST), 예: 2026-10-08T14:00. 기본은 지금")
    plan.add_argument("--days", type=int, default=3)
    plan.set_defaults(func=_cmd_plan_window)

    export = sub.add_parser("export", help="창 하나를 읽기 전용 트랜잭션으로 반출한다")
    export.add_argument("--days", type=int,
                        help="E0 날짜 창 수(ADR 0009 A8.1: 3, 모자라면 5). 창을 직접 계산하고 계획을 매니페스트에 적는다")
    export.add_argument("--t0-kst", help="--days와 함께. 계획 T0(KST), 기본은 지금. 서버 스냅숏의 KST 날짜와 다르면 거절한다")
    export.add_argument("--start-utc", help="--days 대신 창을 직접 줄 때(E0이 아닌 반출)")
    export.add_argument("--end-utc")
    export.add_argument("--root", help="기본: <메인 체크아웃>/data/exports")
    export.add_argument("--dsn-env", help="DSN이 든 환경변수 이름(값을 명령줄에 적지 않는다)")
    export.add_argument("--psql-command", help="스크립트를 표준입력으로 받는 psql 명령 한 줄")
    export.add_argument("--statement-timeout-s", type=int, default=120)
    export.add_argument("--any-minute", action="store_true", help="매시 :20~:50 확인을 건너뛴다(Tier 0가 아닌 DB)")
    export.set_defaults(func=_cmd_export)

    verify = sub.add_parser("verify", help="반출본을 검증하고(만료 본문은 지운다) 요약을 낸다")
    verify.add_argument("--dir", required=True)
    verify.set_defaults(func=_cmd_verify)

    purge = sub.add_parser("purge", help="만료된 본문을 지운다. 해시·길이·제목·URL은 남는다")
    purge.add_argument("--dir")
    purge.add_argument("--all", action="store_true", help="data/exports 아래의 모든 반출본")
    purge.add_argument("--root", help="--all일 때의 뿌리. 기본: <메인 체크아웃>/data/exports")
    purge.add_argument("--everything", action="store_true", help="만료 전이라도 본문을 전부 지운다")
    purge.set_defaults(func=_cmd_purge)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ExportError as e:
        print(f"거절: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
