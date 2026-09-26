# 수집 출처별 라이선스 태그 (docs/adr/0023-data-sources-copyright-retention.md)
#
# Settings.RSS_FEEDS의 (strategy, url) 튜플 모양은 수집기·본문 추출기·다른 브랜치 코드가
# 그대로 언패킹하므로 바꾸지 않고, 라이선스는 언론사 이름(press 테이블의 press_name) 기준으로
# 여기에 따로 둔다. 모르는 출처는 재배포할 수 없는 것으로 본다(fail closed).
from dataclasses import dataclass
from typing import Dict

POLICY_BRIEFING_PRESS = "정책브리핑"


@dataclass(frozen=True)
class SourceLicense:
    tag: str
    # 본문을 저장소·공개 평가셋에 실어도 되는가
    redistributable: bool
    # 재배포할 때 붙일 출처 표시 (재배포 불가면 빈 문자열)
    attribution: str
    # 판단 근거와 확인 날짜
    evidence: str
    checked_on: str


ALL_RIGHTS_RESERVED = SourceLicense(
    tag="all-rights-reserved",
    redistributable=False,
    attribution="",
    evidence=(
        "각 RSS 채널의 copyright 요소(예: 'All rights reserved') 또는 표시 없음. "
        "별도 이용허락이 없으므로 저작권법상 보호되는 저작물로 취급한다."
    ),
    checked_on="2026-09-26",
)

KOGL_TYPE_1_POLICY_BRIEFING = SourceLicense(
    tag="kogl-1",
    redistributable=True,
    attribution="출처: 대한민국 정책브리핑(www.korea.kr), 공공누리 제1유형",
    evidence=(
        "https://www.data.go.kr/data/15095335/openapi.do (이용허락범위: 공공저작물 출처표시 제1유형), "
        "https://www.korea.kr/guide/copyRight.do (공공누리 표시 저작물의 텍스트만 자유이용, 사진·이미지 제외). "
        "기사별 KoglType이 1인 항목만 수집한다(crawler/policy_briefing.py)."
    ),
    checked_on="2026-09-26",
)

UNKNOWN_SOURCE = SourceLicense(
    tag="unknown",
    redistributable=False,
    attribution="",
    evidence="라이선스를 확인하지 않은 출처 - 재배포하지 않는다.",
    checked_on="",
)

SOURCE_LICENSES: Dict[str, SourceLicense] = {
    "동아일보": ALL_RIGHTS_RESERVED,
    "경향신문": ALL_RIGHTS_RESERVED,
    "매일경제": ALL_RIGHTS_RESERVED,
    "한국경제": ALL_RIGHTS_RESERVED,
    "국민일보": ALL_RIGHTS_RESERVED,
    "세계일보": ALL_RIGHTS_RESERVED,
    "전자신문": ALL_RIGHTS_RESERVED,
    "AI타임스": ALL_RIGHTS_RESERVED,
    POLICY_BRIEFING_PRESS: KOGL_TYPE_1_POLICY_BRIEFING,
}


def press_name_for_feed(feed_name: str) -> str:
    """Settings.RSS_FEEDS의 키를 press 테이블 이름으로 바꾼다 (전자신문_IT -> 전자신문).
    crawler/rss_collector.py와 같은 규칙."""
    return "전자신문" if feed_name.startswith("전자신문") else feed_name


def license_for(press_name: str) -> SourceLicense:
    return SOURCE_LICENSES.get(press_name, UNKNOWN_SOURCE)


def is_redistributable(press_name: str) -> bool:
    return license_for(press_name).redistributable
