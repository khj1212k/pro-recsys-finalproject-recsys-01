#!/usr/bin/env python3
"""Tier 0 호스트 밖 백업 - 매일 본문 없는 사본과 최근 본문 사본을 OCI Object Storage에 올린다 (ADR 0026 결정 6, ADR 0023).

VM 호스트의 cron(`scripts/oci/tier0-backup.cron` → /etc/cron.d/)이 root로 부른다. 표준 라이브러리만 쓴다(호스트
파이썬 3.12). DB 도구(pg_dump·pg_restore·psql)는 db 컨테이너 안의 것을 `docker compose exec -T db`로 부르고,
업로드는 OCI CLI를 인스턴스 주체(instance principal)로 부른다 - VM에는 API 키도 사전 인증 URL도 두지 않는다.
권한은 정책 `newsletter-tier0-backup`이 준다: 이 버킷에 객체 만들기·목록 보기, compute agent 지표 읽기뿐이다
(내려받기·지우기·덮어쓰기 없음).

한 실행이 같은 스냅숏(pg_export_snapshot)에서 두 사본을 만든다. 둘 다 tar 하나이고 안의 파일은 이미 압축돼 있다.
  nobody  manifest.json + db.dump(pg_dump -Fc, news_raw의 데이터만 뺌) + news_raw.nobody.copy.gz
          (news_raw의 모든 행, 본문 열은 '추출했지만 본문 없음'이던 ''만 남기고 나머지는 NULL, search_vector 없음)
          -> nobody/daily/ (수명 주기 규칙이 35일 뒤 삭제), 그달의 첫 사본은 nobody/monthly/에도 (삭제 규칙 없음)
  full    nobody의 셋 + news_raw.bodies.copy.gz(최근 27일 수집분과 재배포 가능 출처의 본문)
          -> full/ (수명 주기 규칙이 1일 뒤 삭제). 이름에 만료일 = 든 상업 언론사 본문 중 가장 오래된 것의 수집 + 30일
          설정 `full_enabled`가 true일 때만 만든다(기본 꺼짐 - 본문 사본의 위치는 ADR 0023 개정 2026-10-17 2가 승인돼야 한다).
          꺼져 있으면 본문은 VM 작업 디렉터리에도 쓰지 않는다.

full에 27일까지만 담는 이유(ADR 0023): 덤프는 행 단위로 지울 수 없어 가장 오래된 본문이 만료될 때 통째로 지워야
한다. 보존 잡이 매일 돌면 DB 전체 덤프의 가장 오래된 본문은 늘 30일 직전이라 뜨자마자 만료된다. Object Storage의
삭제 규칙은 일 단위이고(객체의 수정 시각을 다음 UTC 자정으로 올린 뒤 N일, 규칙은 하루 한 번 돈다) 1일 규칙이면
객체는 최대 약 2.x일 산다. 그래서 만료까지 3일 이상 남은 본문만 담고(27 = 30 - 3), 실행할 때마다 확인한다.

하위 명령:
  backup         두 사본을 만들어 올리고, 목록의 크기·MD5로 올라간 바이트를 확인한 뒤 job_runs에 'backup'을 남긴다
  idle-check     유휴 회수 기준(scripts/oci/idle_check.py)을 인스턴스 주체로 계산해 job_runs에 'idle_check'를 남긴다
  restore-check  본문 없는 사본을 일회용 DB에 복원해 행 수·Alembic 리비전·제약 수를 manifest와 대조하고 DB를 지운다
                 (job_runs에 'restore_check')
  restore        같은 복원을 빈 DB에 한다(VM을 다시 만들었을 때, 런북 6.2)
  daily          cron용: backup → idle-check → 복원 검증이 28일 넘게 없었으면 방금 올린 본문 없는 사본으로 restore-check

종료 코드: 0 성공, 1 실패, 2 잘못된 인자, 4 다른 실행이 돌고 있어 건너뜀.
DB 도구의 오류 출력은 오류 줄만, 따옴표 안 값을 지우고 싣는다(scrub_db_stderr) - COPY 오류의 CONTEXT·DETAIL 줄에는 행 내용(제목·본문
조각)이 실릴 수 있다. job_runs.error와 cron 로그에는 건수·해시·시각과 오류 종류만 남긴다(ADR 0023).
일하는 디렉터리(--work-dir)는 실행이 끝나면 비운다(성공·실패 모두). 시작할 때도 지난 실행이 남긴 파일을 지운다 -
full 사본에는 본문이 있으므로 VM 디스크에 남기지 않는다(ADR 0023 개정 2026-10-17의 "VM의 백업 작업 디렉터리").
"""
import argparse
import base64
import fcntl
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

FORMAT = "newsletter-tier0-backup/1"
DEFAULT_DB_EXEC = "docker compose exec -T db"
DEFAULT_BUCKET = "newsletter-tier0-backups"
DEFAULT_WORK_DIR = "/var/lib/newsletter-backup"

NOBODY_DAILY_PREFIX = "nobody/daily/"
NOBODY_MONTHLY_PREFIX = "nobody/monthly/"
FULL_PREFIX = "full/"

# ADR 0023의 30일. config/retention.py의 BODY_RETENTION_DAYS와 같은 값이어야 한다(테스트가 대조한다 - 이 스크립트는
# VM에 따로 설치돼 저장소 코드를 임포트하지 않는다).
BODY_RETENTION_DAYS = 30
# full 사본이 Object Storage에 머무는 최대 기간(1일 삭제 규칙 + 다음 UTC 자정 올림 + 하루 한 번 실행)보다 넉넉하게.
FULL_MIN_REMAINING_DAYS = 3
BODY_BACKUP_MAX_AGE_DAYS = BODY_RETENTION_DAYS - FULL_MIN_REMAINING_DAYS  # 27
# 수명 주기 규칙이 도는지 보는 경고 기준(시간). D일 18:1x에 만든 full 객체는 수정 시각이 D+1 00:00으로 올림된 뒤 1일 → D+2 00:00부터
# 대상이고 규칙은 하루 한 번 돈다 → 늦어도 D+3 00:00 전에 지워진다. 목록은 매일 같은 시각의 백업이 보므로 정상이어도 D+2 조회(약 48시간)
# 에는 남아 있을 수 있고, D+3 조회(약 72시간)에는 없어야 한다. 그 사이 값이라 오탐 없이 D+3 조회에서 잡는다 - 72로 두면 D+3 조회가
# 몇 분만 일러도(71.98시간) 놓치고 하루 뒤에야 경고한다. D+3 18:1x는 그 객체의 가장 오래된 상업 본문이 30일에 닿는 무렵이다.
FULL_LIFECYCLE_ALERT_HOURS = 60
# 재배포 가능 출처(config/sources.py). 면제 조건은 jobs/tasks/retention.py의 EXEMPT_SQL과 같은 식이다(테스트가 대조).
EXEMPT_PRESS_NAMES = ("정책브리핑",)
EXEMPT_SQL_TEMPLATE = "(p.press_name = ANY(%(exempt_presses)s) AND COALESCE(n.raw_news_kogl_type, 1) = 1)"

# news_raw 중 본문 없는 사본에 싣지 않는 열. raw_news_content는 아래 식으로 바꿔 싣고, search_vector는 아예 뺀다
# (본문에서 만든 단어 목록일 수 있다 - 지금은 아무도 채우지 않는다).
NOBODY_CONTENT_EXPR = "CASE WHEN n.raw_news_content = '' THEN '' END"
NOBODY_DROPPED_COLUMNS = ("search_vector",)

ARCHIVE_MEMBERS = {
    "nobody": ("manifest.json", "db.dump", "news_raw.nobody.copy.gz"),
    "full": ("manifest.json", "db.dump", "news_raw.nobody.copy.gz", "news_raw.bodies.copy.gz"),
}

HOST_BACKUP_JOB = "backup"
IDLE_CHECK_JOB = "idle_check"
RESTORE_CHECK_JOB = "restore_check"
RESTORE_CHECK_EVERY_DAYS = 28
SCRATCH_DB_RE = re.compile(r"^restore_check[a-z0-9_]{0,40}$")
SNAPSHOT_RE = re.compile(r"^[0-9A-F]+-[0-9A-F]+-[0-9]+$")
METADATA_URL = "http://169.254.169.254/opc/v2/instance/"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 4


class BackupError(RuntimeError):
    """백업·복원을 멈춰야 하는 상황(명령 실패, 대조 불일치, 규칙 위반)."""


# --------------------------------------------------------------------------- 순수 함수(단위 테스트 대상)

def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def sql_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def exempt_sql(press_names: Sequence[str] = EXEMPT_PRESS_NAMES) -> str:
    array = "ARRAY[" + ", ".join(sql_literal(p) for p in press_names) + "]::text[]"
    return EXEMPT_SQL_TEMPLATE.replace("%(exempt_presses)s", array)


def nobody_copy_columns(columns: Sequence[str]) -> List[str]:
    """복원 쪽 `COPY news_raw (...) FROM STDIN`의 열 목록. search_vector만 빠진다."""
    return [c for c in columns if c not in NOBODY_DROPPED_COLUMNS]


def nobody_select_list(columns: Sequence[str]) -> str:
    out = []
    for c in nobody_copy_columns(columns):
        out.append(f"{NOBODY_CONTENT_EXPR} AS raw_news_content" if c == "raw_news_content" else f"n.{sql_ident(c)}")
    return ", ".join(out)


def nobody_copy_sql(columns: Sequence[str]) -> str:
    if "raw_news_content" not in columns:
        raise BackupError("news_raw에 raw_news_content 열이 없다 - 스키마가 예상과 다르다")
    return (f"COPY (SELECT {nobody_select_list(columns)} FROM public.news_raw n ORDER BY n.raw_news_id) TO STDOUT")


def body_filter_sql(cutoff_utc: str, press_names: Sequence[str] = EXEMPT_PRESS_NAMES) -> str:
    """full 사본에 담는 본문: 본문이 있고, 재배포 가능 출처이거나 cutoff(UTC) 뒤에 수집된 행."""
    return (
        "n.raw_news_content IS NOT NULL AND n.raw_news_content <> '' "
        f"AND ({exempt_sql(press_names)} OR n.raw_news_crawled_at > {sql_literal(cutoff_utc)}::timestamp)"
    )


def bodies_copy_sql(cutoff_utc: str, press_names: Sequence[str] = EXEMPT_PRESS_NAMES) -> str:
    return (
        "COPY (SELECT n.raw_news_id, n.raw_news_content FROM public.news_raw n "
        "JOIN public.press p ON p.press_id = n.press_id "
        f"WHERE {body_filter_sql(cutoff_utc, press_names)} ORDER BY n.raw_news_id) TO STDOUT"
    )


def bodies_stats_sql(cutoff_utc: str, press_names: Sequence[str] = EXEMPT_PRESS_NAMES) -> str:
    return (
        "SELECT json_build_object('rows', count(*), "
        f"'oldest_commercial_crawled_at_utc', min(n.raw_news_crawled_at) FILTER (WHERE NOT {exempt_sql(press_names)}), "
        f"'exempt_rows', count(*) FILTER (WHERE {exempt_sql(press_names)})) "
        "FROM public.news_raw n JOIN public.press p ON p.press_id = n.press_id "
        f"WHERE {body_filter_sql(cutoff_utc, press_names)}"
    )


def parse_utc(value: str) -> datetime:
    """DB가 json으로 낸 시각(시간대 없는 UTC 또는 +00:00)을 aware UTC로."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def full_expiry(oldest_commercial: Optional[datetime], snapshot_at: datetime) -> Optional[datetime]:
    """full 사본의 만료 시각. 만료까지 FULL_MIN_REMAINING_DAYS가 안 남으면 BackupError(올리지 않는다)."""
    if oldest_commercial is None:
        return None
    expires = oldest_commercial + timedelta(days=BODY_RETENTION_DAYS)
    if expires - snapshot_at < timedelta(days=FULL_MIN_REMAINING_DAYS):
        raise BackupError(
            f"full 사본의 가장 오래된 상업 본문이 {expires.isoformat()}에 만료된다 - 스냅숏({snapshot_at.isoformat()})부터 "
            f"{FULL_MIN_REMAINING_DAYS}일이 안 남아 수명 주기 규칙(1일)이 지우기 전에 30일을 넘길 수 있다. 올리지 않는다"
        )
    return expires


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def object_names(snapshot_at: datetime, expires_at: Optional[datetime]) -> Dict[str, str]:
    ts = stamp(snapshot_at)
    expiry = f"expires-{expires_at.date().isoformat()}" if expires_at else "no-commercial-bodies"
    return {
        "nobody": f"{NOBODY_DAILY_PREFIX}tier0-{ts}.nobody.tar",
        "monthly": f"{NOBODY_MONTHLY_PREFIX}tier0-{snapshot_at.strftime('%Y-%m')}.nobody.tar",
        "full": f"{FULL_PREFIX}tier0-{ts}.{expiry}.full.tar",
    }


def file_digests(path: str) -> Dict[str, Any]:
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": sha.hexdigest(), "md5_b64": base64.b64encode(md5.digest()).decode()}


def validate_snapshot_id(snapshot: str) -> str:
    if not SNAPSHOT_RE.match(snapshot or ""):
        raise BackupError(f"스냅숏 id 형식이 아니다: {snapshot!r}")
    return snapshot


def validate_scratch_name(name: str, main_db: str) -> str:
    if not SCRATCH_DB_RE.match(name or "") or name == main_db:
        raise BackupError(f"일회용 DB 이름은 restore_check로 시작하는 소문자·숫자·밑줄이어야 하고 운영 DB와 달라야 한다: {name!r}")
    return name


def restore_check_due(last_success: Optional[datetime], now: datetime, every_days: int = RESTORE_CHECK_EVERY_DAYS) -> bool:
    return last_success is None or now - last_success >= timedelta(days=every_days)


def bucket_summary(objects: Iterable[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """버킷 목록에서 접두어별 개수·크기, 가장 오래된 full 객체의 나이(수명 주기 규칙이 도는지)."""
    groups = {"nobody_daily": NOBODY_DAILY_PREFIX, "nobody_monthly": NOBODY_MONTHLY_PREFIX, "full": FULL_PREFIX}
    out: Dict[str, Any] = {k: {"objects": 0, "bytes": 0} for k in groups}
    out["other"] = {"objects": 0, "bytes": 0}
    total, oldest_full = 0, None
    for o in objects:
        name, size = o.get("name", ""), int(o.get("size") or 0)
        total += size
        key = next((k for k, prefix in groups.items() if name.startswith(prefix)), "other")
        out[key]["objects"] += 1
        out[key]["bytes"] += size
        if key == "full" and o.get("time-created"):
            created = parse_utc(o["time-created"])
            oldest_full = created if oldest_full is None or created < oldest_full else oldest_full
    out["total_bytes"] = total
    out["oldest_full_age_hours"] = round((now - oldest_full).total_seconds() / 3600, 1) if oldest_full else None
    return out


def lifecycle_warnings(summary: Dict[str, Any]) -> List[str]:
    """full 객체가 FULL_LIFECYCLE_ALERT_HOURS 넘게 남아 있으면 수명 주기 규칙이 돌지 않는 것이다(본문 30일을 넘길 수 있다)."""
    age = summary.get("oldest_full_age_hours")
    if age is not None and age > FULL_LIFECYCLE_ALERT_HOURS:
        return [f"full/ 객체가 {age}시간째 남아 있다 - 1일 삭제 규칙이 돌지 않는다(버킷 수명 주기·서비스 정책 확인, 런북 6.5)"]
    return []


def manifest_text(manifest: Dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def write_archive(path: str, kind: str, files: Dict[str, str]) -> None:
    """files: 아카이브 안 이름 -> 로컬 경로. 이름은 ARCHIVE_MEMBERS[kind]와 같아야 한다."""
    if sorted(files) != sorted(ARCHIVE_MEMBERS[kind]):
        raise BackupError(f"{kind} 아카이브 구성이 다르다: {sorted(files)}")
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in ARCHIVE_MEMBERS[kind]:
            info = tar.gettarinfo(files[name], arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o600
            with open(files[name], "rb") as f:
                tar.addfile(info, f)


def extract_archive(path: str, dest: str) -> Dict[str, Any]:
    """아카이브를 풀고 manifest의 sha256으로 각 파일을 확인한다. 예상하지 않은 이름·경로·링크는 거절한다."""
    os.makedirs(dest, mode=0o700, exist_ok=True)
    with tarfile.open(path, "r") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        kind = next((k for k, v in ARCHIVE_MEMBERS.items() if sorted(v) == sorted(names)), None)
        if kind is None:
            raise BackupError(f"아카이브 구성이 형식과 다르다: {names}")
        for m in members:
            if not m.isfile() or "/" in m.name or m.name.startswith("."):
                raise BackupError(f"아카이브에 허용하지 않는 항목: {m.name!r}")
            src = tar.extractfile(m)
            with open(os.path.join(dest, m.name), "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
    with open(os.path.join(dest, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("format") != FORMAT or manifest.get("kind") != kind:
        raise BackupError(f"manifest 형식·종류가 다르다: {manifest.get('format')!r} {manifest.get('kind')!r} (구성은 {kind})")
    for name, meta in manifest["files"].items():
        got = file_digests(os.path.join(dest, name))
        if got["sha256"] != meta["sha256"] or got["bytes"] != meta["bytes"]:
            raise BackupError(f"{name}의 sha256·크기가 manifest와 다르다")
    return manifest


def clear_dir(path: str) -> List[str]:
    """디렉터리 안을 비운다(디렉터리는 남긴다). 지운 이름을 돌려준다(로그용, 내용은 읽지 않는다)."""
    removed = []
    if not os.path.isdir(path):
        return removed
    for name in sorted(os.listdir(path)):
        target = os.path.join(path, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
        removed.append(name)
    return removed


_DB_ERROR_LINE_RE = re.compile(r"\b(?:ERROR|FATAL|PANIC|error|fatal):")
# 셋째 갈래: 닫히지 않은 따옴표(값이 줄을 넘긴 경우) 뒤는 줄 끝까지 버린다.
_QUOTED_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(["\']).*$')
# 이 스키마의 식별자(소문자 테이블·열·제약 이름)만 남긴다. 기사 문장이나 대소문자가 섞인 값은 지운다.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_.$]{0,62}$")
STDERR_MAX_LINES = 3
STDERR_MAX_CHARS = 600


def scrub_db_stderr(raw: Any) -> str:
    """psql·pg_dump·pg_restore의 stderr에서 실을 수 있는 부분만 남긴다. 행 내용이 실릴 수 있는 줄(CONTEXT·DETAIL·HINT·
    QUERY·LINE·'Command was' 등)은 버리고 오류 줄(ERROR:·FATAL:·error:)만, 따옴표 안의 값은 소문자 식별자 모양(테이블·열
    이름)이 아니면 '…'로 바꾼다 - COPY 오류의 CONTEXT에는 문제 행의 내용이, 형식 오류 메시지에는 그 값이 따옴표 안에 들어간다."""
    text = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")

    def quoted(m: "re.Match[str]") -> str:
        if m.group(3) is not None:
            return m.group(3) + "…"
        value = m.group(1) if m.group(1) is not None else m.group(2)
        q = '"' if m.group(1) is not None else "'"
        return f"{q}{value}{q}" if _IDENT_RE.match(value) else f"{q}…{q}"

    kept = [_QUOTED_RE.sub(quoted, line.strip()) for line in text.splitlines() if _DB_ERROR_LINE_RE.search(line)]
    if not kept:
        return "(오류 줄 없음 - stderr 내용은 싣지 않는다)"
    out = " | ".join(kept[:STDERR_MAX_LINES])
    if len(kept) > STDERR_MAX_LINES:
        out += f" | 외 오류 줄 {len(kept) - STDERR_MAX_LINES}개"
    return out[:STDERR_MAX_CHARS]


# --------------------------------------------------------------------------- DB와 업로드 (외부 명령)

@dataclass
class Db:
    """DB 도구 호출. exec_prefix는 `docker compose exec -T db`(VM) 또는 빈 목록(CI - PG* 환경변수로 접속)."""
    exec_prefix: List[str]
    user: Optional[str]
    name: str
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None

    def conn(self, dbname: Optional[str] = None) -> List[str]:
        args = ["-U", self.user] if self.user else []
        return args + ["-d", dbname or self.name]

    def cmd(self, tool: str, *args: str, dbname: Optional[str] = None) -> List[str]:
        return [*self.exec_prefix, tool, *self.conn(dbname), *args]

    def run(self, argv: List[str], *, stdin=None, stdout=subprocess.PIPE, input_text: Optional[str] = None,
            what: str = "") -> subprocess.CompletedProcess:
        proc = subprocess.run(argv, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, cwd=self.cwd, env=self.env,
                              input=input_text.encode() if input_text is not None else None)
        if proc.returncode != 0:
            raise BackupError(f"{what or argv[len(self.exec_prefix)]} 실패(종료 코드 {proc.returncode}): "
                              f"{scrub_db_stderr(proc.stderr)}")
        return proc

    def psql_script(self, sql: str, *, dbname: Optional[str] = None, variables: Optional[Dict[str, str]] = None,
                    what: str = "psql") -> str:
        """stdin으로 SQL을 보내고 -A -t 출력(문자열)을 돌려준다. 변수는 psql -v(:'name' 인용 치환)로."""
        argv = self.cmd("psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", dbname=dbname)
        for key, value in (variables or {}).items():
            argv += ["-v", f"{key}={value}"]
        argv += ["-f", "-"]
        return self.run(argv, input_text=sql, what=what).stdout.decode().strip()

    def psql_json(self, sql: str, *, dbname: Optional[str] = None, what: str = "psql") -> Any:
        out = self.psql_script(sql.rstrip().rstrip(";") + ";\n", dbname=dbname, what=what)
        return json.loads(out) if out else None


class SnapshotSession:
    """REPEATABLE READ 트랜잭션 하나를 열어 두고 pg_export_snapshot()을 낸다. 같은 스냅숏으로 pg_dump·COPY를 돌린다."""

    END = "__tier0_backup_end__"

    def __init__(self, db: Db):
        self.db = db
        # stderr는 cron 로그로 그대로 흘리지 않고 임시 파일에 받아, 실패했을 때 scrub_db_stderr를 거쳐 싣는다.
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            db.cmd("psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, cwd=db.cwd, env=db.env,
        )
        self.snapshot = validate_snapshot_id(self._ask(
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();"))

    def _ask(self, sql: str) -> str:
        self.proc.stdin.write(sql.rstrip() + "\n\\echo " + self.END + "\n")
        self.proc.stdin.flush()
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                self.proc.wait(timeout=60)
                self.stderr.seek(0)
                raise BackupError(f"스냅숏 세션이 끝났다(종료 코드 {self.proc.returncode}): "
                                  f"{scrub_db_stderr(self.stderr.read())}")
            if line.strip() == self.END:
                return "\n".join(lines).strip()
            lines.append(line.rstrip("\n"))

    def json(self, sql: str) -> Any:
        out = self._ask(sql.rstrip().rstrip(";") + ";")
        return json.loads(out) if out else None

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write("COMMIT;\n")
                self.proc.stdin.close()
                self.proc.wait(timeout=60)
            except Exception:
                self.proc.kill()
        self.stderr.close()


def snapshot_copy_argv(db: Db, snapshot: str, copy_sql: str) -> List[str]:
    return db.cmd("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                  "-c", "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
                  "-c", f"SET TRANSACTION SNAPSHOT {sql_literal(validate_snapshot_id(snapshot))}",
                  "-c", copy_sql, "-c", "COMMIT")


def copy_out_gzip(db: Db, argv: List[str], path: str) -> int:
    """COPY ... TO STDOUT의 출력을 gzip으로 쓰고 행 수(텍스트 형식은 한 행이 한 줄)를 돌려준다."""
    rows = 0
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=db.cwd, env=db.env)
    with gzip.open(path, "wb", compresslevel=6) as out:
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            rows += chunk.count(b"\n")
            out.write(chunk)
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise BackupError(f"COPY 실패(종료 코드 {proc.returncode}): {scrub_db_stderr(err)}")
    return rows


def copy_in_gzip(db: Db, argv: List[str], path: str, *, prefix: bytes = b"", suffix: bytes = b"") -> None:
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            cwd=db.cwd, env=db.env)
    try:
        proc.stdin.write(prefix)
        with gzip.open(path, "rb") as src:
            shutil.copyfileobj(src, proc.stdin, 1 << 20)
        proc.stdin.write(suffix)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise BackupError(f"COPY FROM 실패(종료 코드 {proc.returncode}): {scrub_db_stderr(err)}")


@dataclass
class Uploader:
    """OCI CLI로 객체를 올리고 목록을 본다(인스턴스 주체). 권한은 OBJECT_CREATE·OBJECT_INSPECT뿐이다."""
    namespace: str
    bucket: str
    auth: str = "instance_principal"
    oci_bin: str = "oci"

    def _oci(self, *args: str) -> str:
        argv = [self.oci_bin, *args, "--namespace", self.namespace, "--bucket-name", self.bucket]
        if self.auth != "api_key":
            argv += ["--auth", self.auth]
        proc = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "SUPPRESS_LABEL_WARNING": "True"})
        if proc.returncode != 0:
            raise BackupError(f"oci {' '.join(args[:3])} 실패(종료 코드 {proc.returncode}): {proc.stderr.strip()[-800:]}")
        return proc.stdout

    def put(self, name: str, path: str, metadata: Dict[str, str]) -> None:
        self._oci("os", "object", "put", "--name", name, "--file", path, "--no-multipart", "--no-overwrite",
                  "--content-type", "application/x-tar", "--metadata", json.dumps(metadata))

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        args = ["os", "object", "list", "--all", "--fields", "name,size,md5,timeCreated"]
        if prefix:
            args += ["--prefix", prefix]
        out = self._oci(*args)
        return (json.loads(out).get("data") or []) if out.strip() else []


def upload_verified(uploader: Uploader, name: str, path: str, metadata: Dict[str, str]) -> Dict[str, Any]:
    """올린 뒤 목록의 크기와 MD5가 로컬 파일과 같은지 본다(내려받기 권한 없이 올라간 바이트를 확인하는 방법)."""
    digests = file_digests(path)
    uploader.put(name, path, {**metadata, "sha256": digests["sha256"]})
    listed = [o for o in uploader.list(name) if o.get("name") == name]
    if len(listed) != 1:
        raise BackupError(f"올린 객체 {name}이 목록에 없다")
    if int(listed[0].get("size") or -1) != digests["bytes"] or listed[0].get("md5") != digests["md5_b64"]:
        raise BackupError(f"올린 객체 {name}의 크기·MD5가 로컬 파일과 다르다")
    return {"object": name, "bytes": digests["bytes"], "sha256": digests["sha256"]}


# --------------------------------------------------------------------------- job_runs 기록

class JobRecorder:
    """운영 DB의 job_runs에 이 스크립트의 실행을 남긴다(daily_report가 읽는다). jobs.run의 잡이 아니므로 lock은 없다."""

    def __init__(self, db: Db, job: str, git_sha: Optional[str]):
        self.db, self.job, self.git_sha = db, job, git_sha
        self.run_id: Optional[int] = None
        self.started = time.monotonic()

    def start(self) -> None:
        out = self.db.psql_script(
            "INSERT INTO job_runs (job, status, git_sha) VALUES (:'job', 'running', NULLIF(:'sha', '')) RETURNING id;",
            variables={"job": self.job, "sha": self.git_sha or ""}, what="job_runs 기록")
        self.run_id = int(out.splitlines()[-1])

    def record(self, status: str, stats: Dict[str, Any], error: Optional[str] = None) -> None:
        """끝난 실행을 한 행으로 남긴다 - 실행 전에는 job_runs가 없는 DB(빈 DB에 복원하는 restore)용."""
        stats = {**stats, "duration_s": round(time.monotonic() - self.started, 1)}
        self.db.psql_script(
            "INSERT INTO job_runs (job, status, started_at, finished_at, stats, error, git_sha) VALUES "
            "(:'job', :'status', now() - make_interval(secs => :'secs'::float8), now(), :'stats'::jsonb, "
            "NULLIF(:'error', ''), NULLIF(:'sha', ''));",
            variables={"job": self.job, "status": status, "secs": str(stats["duration_s"]),
                       "stats": json.dumps(stats, ensure_ascii=False, default=str), "error": (error or "")[:8000],
                       "sha": self.git_sha or ""}, what="job_runs 기록")

    def finish(self, status: str, stats: Dict[str, Any], error: Optional[str] = None) -> None:
        if self.run_id is None:
            return
        stats = {**stats, "duration_s": round(time.monotonic() - self.started, 1)}
        self.db.psql_script(
            "UPDATE job_runs SET status = :'status', finished_at = now(), stats = :'stats'::jsonb, "
            "error = NULLIF(:'error', '') WHERE id = :'id'::bigint;",
            variables={"status": status, "stats": json.dumps(stats, ensure_ascii=False, default=str),
                       "error": (error or "")[:8000], "id": str(self.run_id)}, what="job_runs 기록")


def last_success(db: Db, job: str) -> Optional[datetime]:
    out = db.psql_json(
        "SELECT to_json(max(finished_at)) FROM job_runs WHERE job = " + sql_literal(job) + " AND status = 'succeeded'",
        what="job_runs 조회")
    return parse_utc(out) if out else None


# --------------------------------------------------------------------------- 백업

@dataclass
class BackupResult:
    stats: Dict[str, Any]
    archives: Dict[str, str] = field(default_factory=dict)  # kind -> 로컬 경로 (keep을 준 것만 남는다)


def build_archives(db: Db, work: str, *, body_max_age_days: int = BODY_BACKUP_MAX_AGE_DAYS,
                   script_git_sha: Optional[str] = None,
                   exempt_presses: Sequence[str] = EXEMPT_PRESS_NAMES, full: bool = True) -> BackupResult:
    """같은 스냅숏에서 두 아카이브를 만든다(올리지는 않는다). 표준출력·manifest에는 건수·크기·해시·시각만 남긴다.
    full=False면 본문 없는 사본만 만든다 - 본문을 읽는 COPY 자체를 돌리지 않는다."""
    if not 1 <= body_max_age_days <= BODY_BACKUP_MAX_AGE_DAYS:
        raise BackupError(f"본문 사본 기간은 1~{BODY_BACKUP_MAX_AGE_DAYS}일이어야 한다: {body_max_age_days}")
    os.makedirs(work, mode=0o700, exist_ok=True)
    session = SnapshotSession(db)
    try:
        meta = session.json(
            "SELECT json_build_object('snapshot_at_utc', now() AT TIME ZONE 'UTC', "
            "'cutoff_utc', (now() AT TIME ZONE 'UTC') - make_interval(days => " + str(int(body_max_age_days)) + "), "
            "'server_version', current_setting('server_version'))")
        tables = session.json(
            "SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'") or []
        if "news_raw" not in tables:
            raise BackupError("public.news_raw가 없다")
        counts = session.json("SELECT json_build_object(" + ", ".join(
            f"{sql_literal(t)}, (SELECT count(*) FROM public.{sql_ident(t)})" for t in tables) + ")")
        alembic = session.json(
            "SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version"
        ) if "alembic_version" in tables else None
        columns = session.json(
            "SELECT json_agg(column_name::text ORDER BY ordinal_position) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'news_raw'")
        schema_objects = session.json(
            "SELECT json_build_object("
            "'constraints', (SELECT count(*) FROM pg_constraint c JOIN pg_namespace s ON s.oid = c.connamespace "
            "WHERE s.nspname = 'public'), "
            "'indexes', (SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'))")
        news = session.json(
            "SELECT json_build_object('rows', count(*), 'bodies_in_db', count(*) FILTER (WHERE raw_news_content <> ''), "
            "'max_crawled_at_utc', max(raw_news_crawled_at)) FROM public.news_raw")
        bodies = session.json(bodies_stats_sql(meta["cutoff_utc"], exempt_presses)) if full else None
        snapshot_at = parse_utc(meta["snapshot_at_utc"])
        oldest = bodies.get("oldest_commercial_crawled_at_utc") if full else None
        expires_at = full_expiry(parse_utc(oldest) if oldest else None, snapshot_at) if full else None

        paths = {name: os.path.join(work, name) for name in ARCHIVE_MEMBERS["full"]}
        dump = db.cmd("pg_dump", "-Fc", "--snapshot", session.snapshot, "--exclude-table-data", "public.news_raw")
        with open(paths["db.dump"], "wb") as out:
            db.run(dump, stdout=out, what="pg_dump")
        nobody_rows = copy_out_gzip(db, snapshot_copy_argv(db, session.snapshot, nobody_copy_sql(columns)),
                                    paths["news_raw.nobody.copy.gz"])
        body_rows = copy_out_gzip(db, snapshot_copy_argv(db, session.snapshot,
                                                         bodies_copy_sql(meta["cutoff_utc"], exempt_presses)),
                                  paths["news_raw.bodies.copy.gz"]) if full else None
    finally:
        session.close()

    if nobody_rows != counts["news_raw"]:
        raise BackupError(f"news_raw 사본 행 수 {nobody_rows}가 스냅숏의 count(*) {counts['news_raw']}와 다르다")
    if full and body_rows != bodies["rows"]:
        raise BackupError(f"본문 사본 행 수 {body_rows}가 스냅숏의 조회 {bodies['rows']}와 다르다")

    base = {
        "format": FORMAT,
        "snapshot_at_utc": snapshot_at.isoformat(),
        "script_git_sha": script_git_sha,
        "server_version": meta["server_version"],
        "alembic_version": alembic,
        "table_rows": counts,
        "schema_objects": schema_objects,
        "news_raw": {**news, "copy_file": "news_raw.nobody.copy.gz",
                     "copy_columns": nobody_copy_columns(columns), "copy_rows": nobody_rows,
                     "content_rule": "'' 그대로, 본문이 있던 행은 NULL"},
    }
    names = object_names(snapshot_at, expires_at)
    if not full:
        del names["full"]
    result = BackupResult(stats={"snapshot_at_utc": snapshot_at.isoformat(), "names": names})
    for kind in ("nobody", "full") if full else ("nobody",):
        manifest = dict(base, kind=kind, files={})
        if kind == "full":
            manifest["bodies"] = {
                "file": "news_raw.bodies.copy.gz", "rows": body_rows, "max_age_days": body_max_age_days,
                "cutoff_utc": meta["cutoff_utc"], "exempt_presses": list(exempt_presses),
                "exempt_rows": bodies["exempt_rows"], "oldest_commercial_crawled_at_utc": oldest,
                "expires_at_utc": expires_at.isoformat() if expires_at else None,
            }
        for member in ARCHIVE_MEMBERS[kind]:
            if member != "manifest.json":
                d = file_digests(paths[member])
                manifest["files"][member] = {"bytes": d["bytes"], "sha256": d["sha256"]}
        mpath = os.path.join(work, f"manifest.{kind}.json")
        with open(mpath, "w", encoding="utf-8") as f:
            f.write(manifest_text(manifest))
        archive = os.path.join(work, os.path.basename(names[kind]))
        write_archive(archive, kind, {**{m: paths[m] for m in ARCHIVE_MEMBERS[kind] if m != "manifest.json"},
                                      "manifest.json": mpath})
        os.unlink(mpath)
        result.archives[kind] = archive
        result.stats[kind] = {"bytes": file_digests(archive)["bytes"], "news_raw_rows": nobody_rows}
    if full:
        result.stats["full"].update({"body_rows": body_rows,
                                     "expires_at_utc": expires_at.isoformat() if expires_at else None})
    else:
        result.stats["full"] = {"disabled": True}
    result.stats["table_rows"] = counts
    result.stats["alembic_version"] = alembic
    for member in ARCHIVE_MEMBERS["full"]:
        if member != "manifest.json" and os.path.exists(paths[member]):
            os.unlink(paths[member])
    return result


def run_backup(db: Db, uploader: Optional[Uploader], work: str, *, body_max_age_days: int = BODY_BACKUP_MAX_AGE_DAYS,
               script_git_sha: Optional[str] = None, keep: Sequence[str] = (), full: bool = True,
               now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> BackupResult:
    """아카이브를 만들고(uploader가 있으면) 올린다. keep에 없는 아카이브는 끝나면 지운다. full=False면 full/을 올리지 않는다."""
    result = build_archives(db, work, body_max_age_days=body_max_age_days, script_git_sha=script_git_sha, full=full)
    try:
        if uploader is not None:
            names = result.stats["names"]
            meta = {"format": FORMAT, "snapshot-at-utc": result.stats["snapshot_at_utc"]}
            result.stats["nobody"].update(upload_verified(uploader, names["nobody"], result.archives["nobody"],
                                                          {**meta, "kind": "nobody"}))
            if not uploader.list(names["monthly"]):
                result.stats["monthly"] = upload_verified(uploader, names["monthly"], result.archives["nobody"],
                                                          {**meta, "kind": "nobody"})
            if "full" in result.archives:
                result.stats["full"].update(upload_verified(uploader, names["full"], result.archives["full"],
                                                            {**meta, "kind": "full"}))
            summary = bucket_summary(uploader.list(), now())
            result.stats["bucket"] = summary
            result.stats["warnings"] = lifecycle_warnings(summary)
    finally:
        for kind, path in list(result.archives.items()):
            if kind not in keep and os.path.exists(path):
                os.unlink(path)
                del result.archives[kind]
    return result


# --------------------------------------------------------------------------- 복원

RESTORE_SETVAL_SQL = """
DO $$
DECLARE s text := pg_get_serial_sequence('public.news_raw', 'raw_news_id'); m bigint;
BEGIN
  IF s IS NOT NULL THEN
    SELECT max(raw_news_id) INTO m FROM public.news_raw;
    IF m IS NOT NULL THEN
      EXECUTE format('SELECT setval(%L, GREATEST(%s, (SELECT last_value FROM %s)))', s, m, s);
    END IF;
  END IF;
END $$;
"""

RESTORE_BODIES_PREFIX = (
    b"BEGIN;\nCREATE TEMP TABLE tier0_restore_bodies (raw_news_id bigint PRIMARY KEY, raw_news_content text NOT NULL) "
    b"ON COMMIT DROP;\nCOPY tier0_restore_bodies FROM STDIN;\n"
)
RESTORE_BODIES_SUFFIX = (
    b"\\.\nUPDATE public.news_raw n SET raw_news_content = b.raw_news_content FROM tier0_restore_bodies b "
    b"WHERE n.raw_news_id = b.raw_news_id AND n.raw_news_content IS NULL AND n.raw_news_content_purged_at IS NULL;\n"
    b"COMMIT;\n"
)
# 본문 없는 사본만 복원한 DB에서, 본문이 있었지만 되살리지 못한 행을 "비운 행"으로 표시한다(제약: 본문 NULL·길이 있음).
# 운영에 쓸 DB(restore)에서만 돈다 - 그래야 보존 잡·추출기·임베딩이 그 행을 본문 있는 행으로 착각하지 않는다.
RESTORE_MARK_PURGED_SQL = (
    "UPDATE public.news_raw SET raw_news_content_purged_at = now() "
    "WHERE raw_news_content IS NULL AND raw_news_content_purged_at IS NULL AND raw_news_content_length IS NOT NULL;"
)


def restore_into(db: Db, dbname: str, manifest: Dict[str, Any], files_dir: str, *, with_bodies: bool,
                 mark_missing_bodies_purged: bool) -> Dict[str, Any]:
    dump = os.path.join(files_dir, "db.dump")
    restore = ["--no-owner", "--no-privileges", "--exit-on-error"]
    with open(dump, "rb") as f:
        db.run(db.cmd("pg_restore", "--section=pre-data", *restore, dbname=dbname), stdin=f, what="pg_restore pre-data")
    with open(dump, "rb") as f:
        db.run(db.cmd("pg_restore", "--section=data", *restore, dbname=dbname), stdin=f, what="pg_restore data")
    columns = ", ".join(sql_ident(c) for c in manifest["news_raw"]["copy_columns"])
    copy_in_gzip(db, db.cmd("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                            "-c", f"COPY public.news_raw ({columns}) FROM STDIN", dbname=dbname),
                 os.path.join(files_dir, "news_raw.nobody.copy.gz"))
    if with_bodies:
        copy_in_gzip(db, db.cmd("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-", dbname=dbname),
                     os.path.join(files_dir, "news_raw.bodies.copy.gz"),
                     prefix=RESTORE_BODIES_PREFIX, suffix=RESTORE_BODIES_SUFFIX)
    with open(dump, "rb") as f:
        db.run(db.cmd("pg_restore", "--section=post-data", *restore, dbname=dbname), stdin=f,
               what="pg_restore post-data")
    db.psql_script(RESTORE_SETVAL_SQL, dbname=dbname, what="시퀀스")
    if mark_missing_bodies_purged:
        db.psql_script(RESTORE_MARK_PURGED_SQL, dbname=dbname, what="본문 없는 행 표시")
    return verify_restored(db, dbname, manifest, with_bodies=with_bodies)


def verify_restored(db: Db, dbname: str, manifest: Dict[str, Any], *, with_bodies: bool) -> Dict[str, Any]:
    tables = list(manifest["table_rows"])
    got = db.psql_json("SELECT json_build_object(" + ", ".join(
        f"{sql_literal(t)}, (SELECT count(*) FROM public.{sql_ident(t)})" for t in tables) + ")",
        dbname=dbname, what="행 수 대조")
    checks: Dict[str, Any] = {"table_rows": got}
    mismatched = {t: (manifest["table_rows"][t], got.get(t)) for t in tables if got.get(t) != manifest["table_rows"][t]}
    if mismatched:
        raise BackupError(f"복원한 행 수가 manifest와 다르다: {mismatched}")
    if manifest.get("alembic_version") is not None:
        alembic = db.psql_json("SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version",
                               dbname=dbname, what="Alembic 리비전")
        if alembic != manifest["alembic_version"]:
            raise BackupError(f"Alembic 리비전 {alembic}이 manifest {manifest['alembic_version']}와 다르다")
        checks["alembic_version"] = alembic
    objects = db.psql_json(
        "SELECT json_build_object("
        "'constraints', (SELECT count(*) FROM pg_constraint c JOIN pg_namespace s ON s.oid = c.connamespace "
        "WHERE s.nspname = 'public'), "
        "'indexes', (SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'), "
        "'not_valid', (SELECT count(*) FROM pg_constraint c JOIN pg_namespace s ON s.oid = c.connamespace "
        "WHERE s.nspname = 'public' AND NOT c.convalidated))", dbname=dbname, what="제약 대조")
    if {k: objects[k] for k in ("constraints", "indexes")} != manifest["schema_objects"]:
        raise BackupError(f"제약·인덱스 수 {objects}가 manifest {manifest['schema_objects']}와 다르다")
    checks["schema_objects"] = objects
    bodies = db.psql_json("SELECT json_build_object('bodies', count(*) FILTER (WHERE raw_news_content <> ''), "
                          "'max_crawled_at_utc', max(raw_news_crawled_at)) FROM public.news_raw",
                          dbname=dbname, what="본문 대조")
    expected_bodies = manifest["bodies"]["rows"] if with_bodies else 0
    if bodies["bodies"] != expected_bodies:
        raise BackupError(f"복원한 DB의 본문 행 {bodies['bodies']}이 기대값 {expected_bodies}와 다르다")
    if bodies["max_crawled_at_utc"] != manifest["news_raw"]["max_crawled_at_utc"]:
        raise BackupError("복원한 news_raw의 최근 수집 시각이 manifest와 다르다")
    checks["news_raw"] = bodies
    return checks


def restore_archive(db: Db, archive: str, work: str, *, target_db: str, scratch: bool, with_bodies: bool = False,
                    keep_target: bool = False) -> Dict[str, Any]:
    """scratch=True: 일회용 DB를 새로 만들어 복원·대조하고 지운다. False: 이미 있는 빈 DB에 복원한다(운영 복구)."""
    files = os.path.join(work, "restore")
    created = False
    try:
        manifest = extract_archive(archive, files)
        if with_bodies and manifest["kind"] != "full":
            raise BackupError("--with-bodies는 full 아카이브에만 쓴다")
        if scratch:
            validate_scratch_name(target_db, db.name)
            db.psql_script(f"DROP DATABASE IF EXISTS {sql_ident(target_db)};", what="일회용 DB 지우기")
            db.psql_script(f"CREATE DATABASE {sql_ident(target_db)};", what="일회용 DB 만들기")
            created = True
        else:
            existing = db.psql_json("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'", dbname=target_db,
                                    what="대상 DB 확인")
            if existing:
                raise BackupError(f"대상 DB {target_db}에 이미 테이블이 {existing}개 있다 - 빈 DB에만 복원한다")
        checks = restore_into(db, target_db, manifest, files, with_bodies=with_bodies,
                              mark_missing_bodies_purged=not scratch)
        return {"archive": os.path.basename(archive), "kind": manifest["kind"],
                "snapshot_at_utc": manifest["snapshot_at_utc"], "target_db": target_db, "checks": checks}
    finally:
        clear_dir(files)
        if os.path.isdir(files):
            os.rmdir(files)
        if scratch and created and not keep_target:
            db.psql_script(f"DROP DATABASE IF EXISTS {sql_ident(target_db)};", what="일회용 DB 지우기")


# --------------------------------------------------------------------------- 유휴 회수 지표

def instance_identity() -> Dict[str, str]:
    req = urllib.request.Request(METADATA_URL, headers={"Authorization": "Bearer Oracle"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    return {"instance_id": data["id"], "compartment_id": data["compartmentId"]}


def run_idle_check(days: float = 7.0, identity: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import idle_check  # noqa: E402 - 같은 디렉터리의 스크립트(VM에는 함께 설치한다)

    ident = identity or instance_identity()
    report = idle_check.evaluate(idle_check.oci_fetcher(ident["instance_id"], ident["compartment_id"], days),
                                 bandwidth_mbps=50.0, include_memory=False)
    report["requested_days"] = days
    report["exit_code"] = idle_check.exit_code(report["verdict"])
    return report


# --------------------------------------------------------------------------- CLI

def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tier0_backup.py", description=__doc__.splitlines()[0])
    p.add_argument("--config", help="JSON: namespace, bucket, compose_dir, db_user, db_name, work_dir, full_enabled "
                                    "(기계별 값, 저장소 밖)")
    p.add_argument("--compose-dir", help="docker compose 프로젝트 디렉터리(VM의 ~ubuntu/newsletter-recsys)")
    p.add_argument("--db-exec", help=f"DB 도구 앞에 붙일 명령(기본 {DEFAULT_DB_EXEC!r}, 빈 문자열이면 로컬 도구)")
    p.add_argument("--db-user")
    p.add_argument("--db-name")
    p.add_argument("--work-dir")
    p.add_argument("--namespace")
    p.add_argument("--bucket")
    p.add_argument("--oci-auth", default=os.getenv("OCI_CLI_AUTH", "instance_principal"))
    p.add_argument("--git-sha", default=os.getenv("TIER0_BACKUP_GIT_SHA"))
    p.add_argument("--no-record", action="store_true", help="job_runs에 남기지 않는다")
    p.add_argument("--full-enabled", choices=("true", "false"),
                   help="본문 사본(full/)을 만들어 올릴지. 기본은 config의 full_enabled, 없으면 false")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--dry-run", action="store_true", help="아카이브를 만들고 크기·건수만 보인 뒤 지운다(올리지 않음)")
    b.add_argument("--body-max-age-days", type=int, default=BODY_BACKUP_MAX_AGE_DAYS)
    sub.add_parser("idle-check").add_argument("--days", type=float, default=7.0)
    for name in ("restore-check", "restore"):
        r = sub.add_parser(name)
        r.add_argument("--archive", required=True, help="본문 없는(nobody) 또는 full 아카이브(.tar)")
        r.add_argument("--target-db", default="restore_check" if name == "restore-check" else None,
                       required=name == "restore")
        r.add_argument("--with-bodies", action="store_true", help="full 아카이브의 본문까지 복원한다")
        if name == "restore-check":
            r.add_argument("--keep-target", action="store_true", help="대조 뒤 일회용 DB를 지우지 않는다")
    d = sub.add_parser("daily")
    d.add_argument("--body-max-age-days", type=int, default=BODY_BACKUP_MAX_AGE_DAYS)
    d.add_argument("--idle-days", type=float, default=7.0)
    return p


def resolve(args: argparse.Namespace) -> argparse.Namespace:
    cfg = load_config(args.config)
    for key, default in (("compose_dir", None), ("db_exec", DEFAULT_DB_EXEC), ("db_user", "newsletter"),
                         ("db_name", "newsletter"), ("work_dir", DEFAULT_WORK_DIR), ("namespace", None),
                         ("bucket", DEFAULT_BUCKET)):
        if getattr(args, key) is None:
            setattr(args, key, cfg.get(key, default))
    # 본문 사본은 명시적으로 켠 기계에서만 만든다(JSON true만 켬으로 본다 - "false" 같은 문자열을 켬으로 읽지 않는다).
    args.full = args.full_enabled == "true" if args.full_enabled else cfg.get("full_enabled") is True
    return args


def make_db(args: argparse.Namespace) -> Db:
    return Db(exec_prefix=shlex.split(args.db_exec or ""), user=args.db_user or None, name=args.db_name,
              cwd=args.compose_dir)


def make_uploader(args: argparse.Namespace) -> Uploader:
    if not args.namespace:
        raise BackupError("--namespace(또는 config의 namespace)가 없다")
    return Uploader(namespace=args.namespace, bucket=args.bucket, auth=args.oci_auth)


def log(event: Dict[str, Any]) -> None:
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event},
                     ensure_ascii=False, default=str), flush=True)


def recorded(db: Db, job: str, args: argparse.Namespace, fn: Callable[[], Dict[str, Any]]) -> int:
    recorder = None if args.no_record else JobRecorder(db, job, args.git_sha)
    if recorder:
        recorder.start()
    try:
        stats = fn()
    except Exception as exc:  # 실패도 기록하고 종료 코드 1
        if recorder:
            recorder.finish("failed", {}, f"{type(exc).__name__}: {exc}")
        log({"job": job, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return EXIT_FAILED
    if recorder:
        recorder.finish("succeeded", stats)
    log({"job": job, "status": "succeeded", "stats": stats})
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = resolve(build_parser().parse_args(argv))
    os.umask(0o077)
    work_root = args.work_dir
    work = os.path.join(work_root, "work")
    os.makedirs(work, mode=0o700, exist_ok=True)
    lock = open(os.path.join(work_root, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log({"command": args.command, "status": "locked"})
        return EXIT_LOCKED
    leftovers = clear_dir(work)  # 지난 실행이 남긴 파일(본문이 있을 수 있다)
    if leftovers:
        log({"command": args.command, "removed_leftovers": leftovers})
    db = make_db(args)
    try:
        if args.command == "backup" and args.dry_run:
            result = run_backup(db, None, work, body_max_age_days=args.body_max_age_days, script_git_sha=args.git_sha,
                                full=args.full)
            log({"command": "backup", "dry_run": True, "stats": result.stats})
            return EXIT_OK
        if args.command == "backup":
            return recorded(db, HOST_BACKUP_JOB, args, lambda: run_backup(
                db, make_uploader(args), work, body_max_age_days=args.body_max_age_days,
                script_git_sha=args.git_sha, full=args.full).stats)
        if args.command == "idle-check":
            os.environ.setdefault("OCI_CLI_AUTH", args.oci_auth)
            return recorded(db, IDLE_CHECK_JOB, args, lambda: run_idle_check(args.days))
        if args.command == "restore-check":
            return recorded(db, RESTORE_CHECK_JOB, args, lambda: restore_archive(
                db, args.archive, work, target_db=args.target_db, scratch=True, with_bodies=args.with_bodies,
                keep_target=args.keep_target))
        if args.command == "restore":
            # 빈 DB에 복원한다 - 복원 전에는 job_runs가 없으므로 끝난 뒤에 대상 DB에 한 행으로 남긴다.
            restore_db = Db(exec_prefix=db.exec_prefix, user=db.user, name=args.target_db, cwd=db.cwd, env=db.env)
            recorder = JobRecorder(restore_db, "restore", args.git_sha)
            try:
                stats = restore_archive(db, args.archive, work, target_db=args.target_db, scratch=False,
                                        with_bodies=args.with_bodies)
            except Exception as exc:
                log({"job": "restore", "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000]})
                return EXIT_FAILED
            if not args.no_record:
                recorder.record("succeeded", stats)
            log({"job": "restore", "status": "succeeded", "stats": stats})
            return EXIT_OK
        if args.command == "daily":
            return run_daily(db, args, work)
    except BackupError as exc:
        log({"command": args.command, "status": "failed", "error": str(exc)[:2000]})
        return EXIT_FAILED
    finally:
        clear_dir(work)
        lock.close()
    return EXIT_USAGE


def run_daily(db: Db, args: argparse.Namespace, work: str) -> int:
    kept: Dict[str, str] = {}

    def backup() -> Dict[str, Any]:
        result = run_backup(db, make_uploader(args), work, body_max_age_days=args.body_max_age_days,
                            script_git_sha=args.git_sha, keep=("nobody",), full=args.full)
        kept.update(result.archives)
        return result.stats

    rc = recorded(db, HOST_BACKUP_JOB, args, backup)
    os.environ.setdefault("OCI_CLI_AUTH", args.oci_auth)
    recorded(db, IDLE_CHECK_JOB, args, lambda: run_idle_check(args.idle_days))  # 실패해도 백업 결과는 그대로
    if rc == EXIT_OK and "nobody" in kept and restore_check_due(last_success(db, RESTORE_CHECK_JOB),
                                                                 datetime.now(timezone.utc)):
        archive = kept.pop("nobody")
        rc = recorded(db, RESTORE_CHECK_JOB, args, lambda: restore_archive(
            db, archive, work, target_db="restore_check", scratch=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
