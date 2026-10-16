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
from typing import List, Optional

# ADR 0023: 30일. 늘리는 예외는 없다(반출 도구의 BODY_RETENTION_DAYS와 같은 값).
BODY_RETENTION_MAX_DAYS = 30
# 이보다 짧게는 잘못 쳐도 돌지 않는다. 클러스터링 lookback(24~72시간)과 재시도, ADR 0023이 검토한 가장 짧은
# 대안(7일)을 기준으로 정했다. 더 짧게 하려면 ADR 0023을 먼저 고친다.
BODY_RETENTION_MIN_DAYS = 7


class RetentionConfigError(ValueError):
    """보존 기한 설정이 허용 범위 밖이다."""


def validate_retention_days(days) -> int:
    """일 수를 정수로 검증한다. 범위 밖이면 RetentionConfigError."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise RetentionConfigError(f"보존 기한은 정수(일)여야 한다: {days!r}") from None
    if not BODY_RETENTION_MIN_DAYS <= value <= BODY_RETENTION_MAX_DAYS:
        raise RetentionConfigError(
            f"보존 기한 {value}일은 허용 범위({BODY_RETENTION_MIN_DAYS}~{BODY_RETENTION_MAX_DAYS}일) 밖이다 "
            "(ADR 0023: 30일보다 늘리는 예외는 없고, 7일보다 짧게 하려면 ADR을 먼저 고친다)"
        )
    return value


def exempt_press_names(licenses: Optional[dict] = None) -> List[str]:
    """보존 기한을 적용하지 않는 언론사 이름(재배포 가능 출처), 정렬해서.

    표에 없는 출처는 재배포 불가(fail closed)라 여기에 들어오지 않는다 - 기한이 걸린다. 이 출처의 행이라도
    공공누리 유형이 제1유형이 아니라고 적힌 행은 기한이 걸린다(유형이 NULL인 행은 f1db3221e45f 전에 넣은 행으로,
    수집기가 처음부터 제1유형만 저장했으므로 제1유형으로 본다) - jobs/tasks/retention.py의 SQL."""
    if licenses is None:
        from config.sources import SOURCE_LICENSES as licenses
    return sorted(name for name, lic in licenses.items() if lic.redistributable)
