# ADR 0010 증거용 프로브(evaluation/llm/gate_probe.py). 프로브의 변형이 정말 "같은 값"/"다른 값"인지
# 먼저 확인해야 거기서 나온 오탐·검출률을 믿을 수 있다.
import pytest

from core.faithfulness import _extract_numbers
from evaluation.llm import gate_probe as gp

SAMPLES = ["1조5000억 원", "2조 원", "1,200억", "12000명", "1,200명", "20%", "3.5%p", "300건", "1만2000명"]


@pytest.mark.parametrize("surface", SAMPLES)
def test_equivalent_surfaces_parse_to_the_same_value_and_unit(surface):
    (fact,) = _extract_numbers(surface)
    alts = gp.equivalent_surfaces(fact)
    assert alts, f"{surface}: 대체 표기가 없다"
    for alt in alts:
        (other,) = _extract_numbers(alt)
        assert (other.value, other.unit or "KRW") == (fact.value, fact.unit or "KRW"), alt


@pytest.mark.parametrize("surface", SAMPLES)
def test_changed_surface_moves_the_value_beyond_the_approx_tolerance(surface):
    (fact,) = _extract_numbers(surface)
    (other,) = _extract_numbers(gp.changed_surface(fact))
    assert abs(other.value - fact.value) / fact.value > 0.01


def test_fixed_probe_cases_behave_as_designed():
    rows = gp.fixed_cases()
    assert [r["case"] for r in rows if not r["ok"]] == []


def test_real_text_probe_counts_on_a_tiny_corpus():
    docs = [{"title": "HBM 투자", "sentence": "반도체 투자가 늘어요",
             "content": "삼성전자는 1조5000억 원을 투자했습니다. 직원 1만2000명이 참여합니다. 가격은 20% 올랐습니다."}]
    c = gp.probe_newsletters(docs)
    assert c["numbers"] == 3
    assert c["equiv_flagged"] == 0 and c["equiv_numbers"] == 3
    assert c["changed_detection_rate"] == 1.0
    assert c["tone_blocked"] == 0 and c["tone_changed_detection_rate"] == 1.0
