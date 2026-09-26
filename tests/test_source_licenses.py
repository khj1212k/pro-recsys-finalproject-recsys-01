# 출처별 라이선스 태그 (docs/adr/0023). 새 RSS 피드를 추가하면서 라이선스 판단을 빠뜨리면
# 그 출처는 "unknown"(재배포 불가)으로 떨어지는데, 그 사실을 조용히 넘기지 않도록 한다.
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from config.settings import Settings
from config.sources import (
    POLICY_BRIEFING_PRESS,
    UNKNOWN_SOURCE,
    is_redistributable,
    license_for,
    press_name_for_feed,
)


def test_every_configured_rss_source_has_an_explicit_license_decision():
    presses = {press_name_for_feed(feed) for feed in Settings.RSS_FEEDS}

    undecided = sorted(p for p in presses if license_for(p) is UNKNOWN_SOURCE)

    assert undecided == []


def test_commercial_outlets_are_not_redistributable():
    presses = {press_name_for_feed(feed) for feed in Settings.RSS_FEEDS}

    assert presses  # 설정이 비어 있으면 위 테스트가 공허하게 통과한다
    assert not any(is_redistributable(p) for p in presses)


def test_unknown_sources_fail_closed():
    assert license_for("처음보는언론") is UNKNOWN_SOURCE
    assert is_redistributable("처음보는언론") is False


def test_policy_briefing_is_redistributable_with_attribution():
    lic = license_for(POLICY_BRIEFING_PRESS)

    assert lic.redistributable is True
    assert lic.tag == "kogl-1"
    assert "정책브리핑" in lic.attribution
