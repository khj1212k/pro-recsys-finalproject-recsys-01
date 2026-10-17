"""탐색 칸의 기본값과 ADR 0025가 같은 말을 한다(2026-10-17 "부분 채택").

E9(오프폴리시 추정의 시뮬레이터 검증)가 실패한 뒤에도 기본값은 탐색 켬이었고 ADR 0025의 상태는 "제안됨"이었다.
2026-10-17에 탐색 칸의 용도를 무작위화에만 기대는 두 측정(E13, 위치 편향)으로 좁혀 그 부분을 채택하고 기본값을
뒀다. 기본값이나 칸 수를 바꾸면 ADR의 상태 줄·환경변수 표·ADR 목록도 함께 고쳐야 이 테스트가 통과한다.
"""
import re
from pathlib import Path

from app.recsys.config import RecsysConfig

ADR_DIR = Path(__file__).resolve().parents[2] / "docs" / "adr"
ADR = ADR_DIR / "0025-logging-v2-exploration-and-ope.md"


def _status_line(text: str) -> str:
    lines = text.splitlines()
    return lines[lines.index("## 상태") + 1]


def test_the_default_explores_two_slots_and_four_for_users_without_any_signal():
    for cfg in (RecsysConfig(), RecsysConfig.from_env({})):
        assert (cfg.explore_enabled, cfg.explore_slots, cfg.explore_slots_cold) == (True, 2, 4)
        assert (cfg.explore_slots_for(cold=False), cfg.explore_slots_for(cold=True)) == (2, 4)


def test_the_adr_environment_table_states_the_code_defaults():
    text = ADR.read_text(encoding="utf-8")
    enabled = re.search(r"^\| `RECSYS_EXPLORE_ENABLED` \| (\w+) \|", text, re.M)
    slots = re.search(r"^\| `RECSYS_EXPLORE_SLOTS` / `RECSYS_EXPLORE_SLOTS_COLD` \| (\d+) / (\d+) \|", text, re.M)
    assert enabled and slots, "ADR 0025의 환경변수 표에서 탐색 기본값 줄을 찾지 못했다"
    cfg = RecsysConfig()
    assert enabled.group(1) == str(cfg.explore_enabled).lower()
    assert (int(slots.group(1)), int(slots.group(2))) == (cfg.explore_slots, cfg.explore_slots_cold)


def test_exploration_on_by_default_goes_with_the_adopted_exploration_part_of_adr_0025():
    text = ADR.read_text(encoding="utf-8")
    status = _status_line(text)
    row = next(line for line in (ADR_DIR / "README.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("| [0025]"))
    if RecsysConfig().explore_enabled:
        # 켜 둔 기본값의 근거는 탐색 칸을 E13·위치 편향에 쓰는 부분의 채택이다. 오프폴리시 부분은 E9 실패로 제안됨이다.
        assert status.startswith("**부분 채택 ("), status
        # 상태 줄만 훑는 독자가 "부분 채택"을 E9 통과로 읽지 않게, 같은 줄에 등록한 채택 조건(E9)의 실패를 적는다
        assert "E9" in status and "실패했다" in status, status
        assert "| 부분 채택 (" in row, row
        head = text[: text.index("## 컨텍스트")]
        assert "E13" in head and "위치 편향" in head and "제안됨 유지(E9 실패)" in head
