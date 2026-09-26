import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ai_workspace"),
)

from evaluation.warmup.budget import (
    BudgetGuard,
    CostLedger,
    LedgerEntry,
    cost_usd,
    generator_visible_articles,
    select_clusters,
    worst_case_cost_usd,
)


def test_cost_uses_per_million_prices_for_each_model():
    # gemini-3.5-flash-lite: $0.30 in / $2.50 out per 1M
    assert cost_usd("gemini-3.5-flash-lite", 1_000_000, 0) == pytest.approx(0.30)
    assert cost_usd("gemini-3.5-flash-lite", 0, 1_000_000) == pytest.approx(2.50)
    # gemini-3.1-flash-lite: $0.25 in / $1.50 out per 1M
    assert cost_usd("gemini-3.1-flash-lite", 2000, 400) == pytest.approx((2000 * 0.25 + 400 * 1.50) / 1e6)


def test_hidden_reasoning_tokens_are_charged_at_output_price():
    visible = cost_usd("gemini-3.5-flash-lite", 100, 10, total_tokens=110)
    with_hidden = cost_usd("gemini-3.5-flash-lite", 100, 10, total_tokens=1110)
    assert with_hidden - visible == pytest.approx(1000 * 2.50 / 1e6)


def test_unknown_model_fails_instead_of_costing_zero():
    with pytest.raises(KeyError):
        cost_usd("some-new-model", 10, 10)


def test_guard_blocks_when_worst_case_of_next_request_would_cross_cap(tmp_path):
    ledger = CostLedger(tmp_path / "ledger.json")
    guard = BudgetGuard(cap_usd=0.01, ledger=ledger)
    # worst case of 8192 output tokens at $2.50/1M is ~$0.0205 > $0.01 cap
    assert not guard.allows("gemini-3.5-flash-lite", prompt_chars=100, max_tokens=8192)
    assert guard.allows("gemini-3.5-flash-lite", prompt_chars=100, max_tokens=1024)

    ledger.add(LedgerEntry(ts=0.0, model="gemini-3.5-flash-lite", purpose="x", kind="observed", cost_usd=0.009))
    assert not guard.allows("gemini-3.5-flash-lite", prompt_chars=100, max_tokens=1024)


def test_ledger_persists_and_reloads_cumulative_spend(tmp_path):
    path = tmp_path / "ledger.json"
    first = CostLedger(path)
    first.add(LedgerEntry(ts=0.0, model="m", purpose="p", kind="observed", cost_usd=0.012))
    first.add(LedgerEntry(ts=1.0, model="m", purpose="p", kind="unobserved_upper_bound", cost_usd=0.003))

    reloaded = CostLedger(path)
    assert reloaded.spent() == pytest.approx(0.015)
    assert [e.kind for e in reloaded.entries] == ["observed", "unobserved_upper_bound"]


def test_worst_case_is_an_upper_bound_of_the_observed_cost_for_the_same_request():
    # 1,000자 프롬프트가 실제로 800 토큰, 출력 300 토큰이었을 때
    observed = cost_usd("gemini-3.1-flash-lite", 800, 300)
    assert worst_case_cost_usd("gemini-3.1-flash-lite", prompt_chars=1000, max_tokens=2048) >= observed


def _clusters(sizes):
    return [{"cluster_idx": i, "size": s, "raw_news_ids": list(range(100 * i, 100 * i + s))}
            for i, s in enumerate(sizes)]


def test_selection_spreads_across_size_buckets_and_skips_fragments():
    clusters = _clusters([2, 1, 3, 3, 4, 3, 5, 6, 7, 12, 15])
    picked = select_clusters(clusters, n=5, seed=20260926)

    sizes = [c["size"] for c in picked]
    assert len(picked) == 5
    assert all(s >= 3 for s in sizes)
    assert sum(1 for s in sizes if s >= 10) == 2
    assert sum(1 for s in sizes if 5 <= s <= 9) == 2
    assert sum(1 for s in sizes if 3 <= s <= 4) == 1


def test_selection_is_deterministic_for_a_seed_and_never_repeats():
    clusters = _clusters([3] * 20 + [5] * 6 + [11] * 2)
    a = select_clusters(clusters, n=5, seed=7)
    b = select_clusters(clusters, n=5, seed=7)
    assert [c["cluster_idx"] for c in a] == [c["cluster_idx"] for c in b]
    assert len({c["cluster_idx"] for c in a}) == 5


def test_selection_returns_fewer_when_not_enough_eligible_clusters():
    assert len(select_clusters(_clusters([2, 3, 4]), n=5, seed=1)) == 2


def test_generator_visible_articles_match_what_the_reconstructor_puts_in_the_prompt():
    from core.reconstruction.generator import NewsReconstructor

    articles = [
        {"id": i, "title": f"t{i}", "press_name": "p", "content": ("가" * (1000 + 150 * i)) + f"끝{i}"}
        for i in range(12)
    ]
    visible = generator_visible_articles(articles)
    prompt_text = NewsReconstructor.__new__(NewsReconstructor)._build_articles_text(
        sorted(articles, key=lambda a: len(a["content"]), reverse=True)[:10]
    )

    assert [a["id"] for a in visible] == list(range(11, 1, -1))
    for a in visible:
        assert len(a["content"]) <= 1500
        assert a["content"] in prompt_text
    # 잘린 뒤쪽 꼬리는 프롬프트에 없다
    assert "끝11" not in prompt_text
