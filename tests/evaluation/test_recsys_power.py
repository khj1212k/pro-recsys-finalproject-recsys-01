"""ADR 0025의 검정력 표가 인용하는 수치를 다시 계산한다(evaluation/recsys/power.py)."""
import pytest

from evaluation.recsys.power import requests_needed, two_proportion_n


def test_ab_test_of_a_ten_percent_relative_lift_needs_about_eighty_thousand_slots_per_arm():
    # 설계 문서의 80,685(다른 반올림)와 0.01% 안에서 같다.
    n = two_proportion_n(0.020, 0.022)
    assert n == 80_682
    assert abs(n - 80_685) / 80_685 < 1e-4
    # 화면 20칸이면 요청 약 4천 건, 설계 효과 1.5면 6천 건 남짓
    assert requests_needed(n, 20) == 4_035
    assert requests_needed(n, 20, design_effect=1.5) == 6_052


def test_exploration_versus_deterministic_slots_at_two_versus_three_percent():
    # 같은 크기의 두 팔로 보면 팔당 3,826칸(설계 문서의 "약 3,830"). 요청당 탐색 2칸이면 약 1,900 요청.
    assert two_proportion_n(0.02, 0.03) == 3_826
    assert requests_needed(3_826, 2) == 1_913
    # 실제로는 결정론 칸이 탐색 칸의 9배(18 대 2)라 탐색 칸은 더 적게 필요하다: 같은 팔 가정은 보수적이다.
    unequal = two_proportion_n(0.02, 0.03, ratio=9)
    assert unequal == 2_246
    assert requests_needed(unequal, 2, design_effect=1.5) == 1_685


def test_the_required_sample_grows_as_the_effect_shrinks_and_rejects_degenerate_input():
    assert two_proportion_n(0.02, 0.021) > two_proportion_n(0.02, 0.022) > two_proportion_n(0.02, 0.03)
    assert two_proportion_n(0.02, 0.03, power=0.9) > two_proportion_n(0.02, 0.03)
    assert two_proportion_n(0.03, 0.02) == two_proportion_n(0.02, 0.03)
    for bad in ((0.02, 0.02), (0.0, 0.1), (0.1, 1.0)):
        with pytest.raises(ValueError):
            two_proportion_n(*bad)
    with pytest.raises(ValueError):
        two_proportion_n(0.02, 0.03, ratio=0)
