"""두 비율 비교의 표본 수(정규 근사) - ADR 0025의 검정력 표를 다시 계산할 수 있게 둔다.

팔 1에 n, 팔 2에 ratio·n개의 칸(노출)이 있을 때, 양측 유의수준 alpha에서 p1 대 p2의 차이를
power의 확률로 검출하는 n. 귀무가설 아래 분산은 합동 비율로, 대립가설 아래 분산은 각 팔의 비율로 낸다.
칸은 독립이라고 가정한다 - 같은 요청·같은 사용자의 칸은 독립이 아니므로 실제 필요량은 설계 효과만큼 크다.
"""
from math import ceil, sqrt
from statistics import NormalDist


def two_proportion_n(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8, ratio: float = 1.0) -> int:
    if not (0 < p1 < 1 and 0 < p2 < 1) or p1 == p2:
        raise ValueError("p1 and p2 must be different probabilities in (0, 1)")
    if ratio <= 0:
        raise ValueError("ratio must be positive")
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    pooled = (p1 + ratio * p2) / (1 + ratio)
    under_null = z_alpha * sqrt((1 + 1 / ratio) * pooled * (1 - pooled))
    under_alt = z_power * sqrt(p1 * (1 - p1) + p2 * (1 - p2) / ratio)
    return ceil((under_null + under_alt) ** 2 / (p1 - p2) ** 2)


def requests_needed(slots_per_arm: int, slots_per_request: float, design_effect: float = 1.0) -> int:
    """팔당 칸 수를 요청 수로 바꾼다. design_effect는 같은 사용자·요청의 칸이 서로 닮은 만큼의 할증이다."""
    return ceil(slots_per_arm * design_effect / slots_per_request)
