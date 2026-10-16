# 본문 보존 기한(docs/adr/0023-data-sources-copyright-retention.md)의 상수와 해석 규칙.
#
# 상업 언론사 본문은 수집 시각(news_raw.raw_news_crawled_at) + N일에 만료되고, 만료된 본문은 비운다(NULL).
# 제목·URL·언론사·발행 시각·본문 sha256·본문 길이·임베딩은 남긴다. 재배포 가능한 출처(config/sources.py,
# 지금은 정책브리핑 = 공공누리 제1유형)는 대상이 아니다.
#
# 만료의 정의는 LLM 실험의 동결 반출(evaluation/llm/frozen_export.py, ADR 0036 - 별도 브랜치)과 같다:
#   expires_at = crawled_at + N일, now >= expires_at 이면 만료(같은 순간부터 만료).
# raw_news_crawled_at은 시간대 없는 timestamp이고 값은 UTC다 - 수집기가 datetime.now()로 쓰고 컨테이너의 TZ가
# UTC다(ADR 0026). 그래서 기준 시각도 DB에서 `now() AT TIME ZONE 'UTC'`로 만든다(세션 TimeZone과 무관).
#
# 이 모듈은 가벼워야 한다 - 보존 잡(jobs/tasks/retention.py)이 임포트하므로 trafilatura·lxml 같은 추출기 의존성을
# 끌어오지 않는다(1 GB VM에서 ingest와 겹쳐 도는 프로세스다).
from typing import List, Optional

# ADR 0023: 30일. 실제로 비우는 실행은 이 값만 쓴다 - 늘리는 예외도 줄이는 예외도 없다(반출 도구의
# BODY_RETENTION_DAYS와 같은 값). 바꾸려면 ADR 0023을 먼저 고치고 이 상수를 바꾼다.
# 30일은 평가셋 추출·라벨링·재생성 한 사이클이다. 더 짧게 비우면 진행 중인 사이클의 본문이 되돌릴 수 없게 사라진다
# (덤프에서 본문을 되살리지 않는다 - 런북).
BODY_RETENTION_DAYS = 30
# 30일보다 짧은 기한은 --dry-run(세기만 함)에서만 받는다. 배포 때 실제 규모에서 세는 쿼리의 시간을 미리 재는 용도이고
# (docs/runbook-hosting.md 4.1 6단계), 이보다 짧은 값은 그 용도에도 필요 없다.
DRY_RUN_MIN_DAYS = 7

# 본문 추출기가 다시 시도하는 추출 상태(다시 받으면 결과가 달라질 수 있는 것) - Settings.MAX_EXTRACT_ATTEMPTS회까지.
# 본문 추출기(crawler/content_extractor/extractor.py)의 대상 조회와 보존 잡의 "추출 대기" 조건이 함께 쓴다.
# 두 조건이 같아야 두 잡이 같은 행을 두고 다투지 않는다.
RETRYABLE_EXTRACT_STATUSES = ("fetch_failed", "error")


class RetentionConfigError(ValueError):
    """보존 기한 설정이나 인자가 허용 범위 밖이다."""


def _as_int(days) -> int:
    try:
        return int(days)
    except (TypeError, ValueError):
        raise RetentionConfigError(f"보존 기한은 정수(일)여야 한다: {days!r}") from None


def validate_retention_days(days, *, dry_run: bool = False) -> int:
    """보존 기한(일)을 검증한다. 실제로 비우는 실행은 ADR 0023의 30일만, dry run은 7~30일을 받는다.

    아니면 RetentionConfigError. `--days 7`을 --dry-run 없이 주면 23일치 본문이 사라지므로 막는다."""
    value = _as_int(days)
    if value == BODY_RETENTION_DAYS:
        return value
    if DRY_RUN_MIN_DAYS <= value < BODY_RETENTION_DAYS:
        if dry_run:
            return value
        raise RetentionConfigError(
            f"보존 기한 {value}일은 --dry-run과 함께일 때만 쓸 수 있다 - 실제로 비우는 실행은 ADR 0023의 "
            f"{BODY_RETENTION_DAYS}일만 받는다(더 짧게 비우려면 ADR 0023을 먼저 고친다)"
        )
    raise RetentionConfigError(
        f"보존 기한 {value}일은 허용 범위 밖이다 - 실제 실행은 {BODY_RETENTION_DAYS}일, dry run은 "
        f"{DRY_RUN_MIN_DAYS}~{BODY_RETENTION_DAYS}일(ADR 0023: 30일보다 늘리는 예외는 없다)"
    )


def configured_retention_days(value=None) -> int:
    """설정값(Settings.BODY_RETENTION_DAYS, 환경변수 BODY_RETENTION_DAYS)을 검증해 돌려준다.

    ADR 0023이 바뀌기 전에는 30만 받는다 - dry run이어도 같다. 다르면 RetentionConfigError를 던져 스케줄 실행을
    실패시키고 알림이 가게 한다(잘못된 .env 값 하나로 매일 더 짧은 기한으로 비우지 않게)."""
    if value is None:
        from config.settings import Settings

        value = Settings.BODY_RETENTION_DAYS
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = None
    if days != BODY_RETENTION_DAYS:
        raise RetentionConfigError(
            f"설정 BODY_RETENTION_DAYS={value!r}는 ADR 0023의 {BODY_RETENTION_DAYS}일과 다르다 - 이 값만 받는다. "
            ".env에서 그 줄을 지우거나 30으로 고친다(기한을 바꾸려면 ADR 0023을 먼저 고친다)"
        )
    return days


def exempt_press_names(licenses: Optional[dict] = None) -> List[str]:
    """보존 기한을 적용하지 않는 언론사 이름(재배포 가능 출처), 정렬해서.

    표에 없는 출처는 재배포 불가(fail closed)라 여기에 들어오지 않는다 - 기한이 걸린다. 이 출처의 행이라도
    공공누리 유형이 제1유형이 아니라고 적힌 행은 기한이 걸린다(유형이 NULL인 행은 f1db3221e45f 전에 넣은 행으로,
    수집기가 처음부터 제1유형만 저장했으므로 제1유형으로 본다) - jobs/tasks/retention.py의 SQL."""
    if licenses is None:
        from config.sources import SOURCE_LICENSES as licenses
    return sorted(name for name, lic in licenses.items() if lic.redistributable)
