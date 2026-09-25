# bake-off 러너(evaluation/llm/bakeoff.py): 호출 기록·게이트 플래그·비용, 재개 가능성,
# 인프라 실패/킬 스위치에서 멈춤, 블라인드 내보내기. 모든 LLM은 페이크다(유료 호출 없음).
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ai_workspace"),
)

from config.settings import Settings
from core.llm.client import LLMClient, LLMResult, LLMUsage
from core.llm.pricing import PriceTable
from core.llm.schemas import (
    ClusterEval, CriterionScores, NewsletterContent, NewsletterEval, NewsletterEvalV2, NewsletterMeta, ToneResult,
)
from evaluation.llm import bakeoff as bo
from evaluation.llm.evalset import ArticleRecord, EvalItem

BODIES = [
    "삼성전자는 평택 공장에 HBM 라인을 증설한다고 밝혔다. 투자 규모는 2조 원이다.",
    "SK하이닉스도 청주 공장 투자를 늘린다. 회사는 투자 규모를 1조5000억 원으로 잡았다.",
    "메모리 업황이 개선되면서 D램 가격이 10% 올랐다.",
]
CLEAN = ("삼성전자와 SK하이닉스가 HBM 투자를 늘리고 있다. 삼성전자는 평택 공장에 2조 원을 투입하고, "
         "SK하이닉스는 1조5000억 원을 투자한다. 메모리 업황 개선으로 D램 가격은 10% 올랐다.")
HALLUCINATED = CLEAN.replace("2조 원을", "3조 원을")

PRICING = PriceTable.from_dict({"currency": "USD", "models": {
    m: {"provider": "p", "prices": [{"input_per_1m": 1.0, "output_per_1m": 10.0,
                                     "source": "https://example.com", "accessed": "2026-09-26"}]}
    for m in ("gen-a", "gen-b", "judge-x", "judge-y")
}})

PREREG = {
    "id": "test-prereg",
    "candidates": [
        {"name": "A", "generator": {"provider": "pa", "model": "gen-a"}, "tone": {"provider": "pa", "model": "gen-a"}},
        {"name": "B", "generator": {"provider": "pb", "model": "gen-b"}, "tone": {"provider": "pb", "model": "gen-b"}},
    ],
    "judges": [
        {"name": "jx-v2", "provider": "px", "model": "judge-x", "version": "v2"},
        {"name": "jy-v1", "provider": "pa", "model": "judge-y", "version": "v1"},
    ],
    "human_reliability": {"relabel_outputs": 2, "relabel_clusters": 1, "min_hours_between_rounds": 48},
}


class ScriptedClient(LLMClient):
    """purpose별로 고정 응답을 돌려주는 페이크. content/tone 문구와 실패를 주입할 수 있다."""

    def __init__(self, provider, model, content=CLEAN, tone_content=None, fail=None, log=None, prompts=None):
        self.provider, self.model = provider, model
        self.content, self.tone_content, self.fail = content, tone_content, fail
        self.log = log if log is not None else []
        self.prompts = prompts if prompts is not None else []

    def complete(self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
        self.log.append((self.model, purpose))
        self.prompts.append((purpose, schema, messages[-1]["content"]))
        if self.fail:
            out = self.fail(self.model, purpose, len(self.log))
            if out is not None:
                return out
        parsed = {
            "newsletter_content_gen": lambda: NewsletterContent(content=self.content),
            "newsletter_meta_gen": lambda: NewsletterMeta(title="HBM 투자 확대", sentence="반도체 투자가 늘어난다",
                                                          keywords=["HBM", "삼성전자", "SK하이닉스", "D램", "투자"],
                                                          categories=["IT/과학"]),
            "tone_convert": lambda: ToneResult(title="📰 HBM 투자 확대", summary="요약이에요",
                                               content=self.tone_content or self.content.replace("다.", "요."),
                                               keywords=["HBM"]),
            "newsletter_eval": lambda: (
                NewsletterEvalV2(scores=CriterionScores(faithfulness=4, coverage=4, coherence=4, style=4))
                if schema is NewsletterEvalV2 else NewsletterEval(decision="PASS", score=7)),
            "cluster_eval": lambda: ClusterEval(decision="PASS", confidence=0.8),
        }[purpose]()
        return LLMResult(text=None, parsed=parsed, usage=LLMUsage(1000, 100), latency_s=0.01, attempts=1,
                         provider=self.provider, model=self.model)


def _items(n=2):
    items = []
    for k in range(n):
        arts = [ArticleRecord(100 * k + i, f"https://news.example/{k}/{i}", "테스트일보", f"제목 {i}", b)
                for i, b in enumerate(BODIES)]
        items.append(EvalItem(item_id=f"r1-c{k}", split="eval", run_id=1, cluster_id=k, category="경제",
                              split_v2="no", hard_case=False, size_bucket="3-4", articles=arts))
    return items


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch):
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", "/nonexistent/LLM_KILL_SWITCH")


def _factory(log, **per_model):
    def make(provider, model):
        return ScriptedClient(provider, model, log=log, **per_model.get(model, {}))
    return make


def _rows(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def test_generation_records_calls_costs_and_gate_flags_per_candidate(tmp_path):
    log = []
    factory = _factory(log, **{"gen-b": {"content": HALLUCINATED, "tone_content": CLEAN.replace("10%", "20%")}})

    stats = bo.run_generation(_items(1), PREREG, tmp_path, client_factory=factory, pricing=PRICING,
                              on=date(2026, 9, 26), log=lambda *_: None)

    assert stats == {"done": 2, "skipped": 0, "stopped": None}
    rows = {r["candidate"]: r for r in _rows(tmp_path / "generations.jsonl")}
    a, b = rows["A"], rows["B"]
    assert a["key"] == "A|r1-c0"
    assert [c["purpose"] for c in a["calls"]["generation"]] == ["newsletter_content_gen", "newsletter_meta_gen"]
    assert all(c["first_try_schema_pass"] and c["responded"] for c in a["calls"]["generation"] + a["calls"]["tone"])
    # 호출 3회(본문·메타·문체) × (1000 입력 × $1 + 100 출력 × $10) / 1M
    assert a["cost_usd"]["generation"] + a["cost_usd"]["tone"] == pytest.approx(3 * 0.002)
    assert a["flags"] == {"generation_failed": False, "unsupported_number": False,
                          "ops_gate_blocked": False, "tone_drift": False}
    assert b["flags"]["unsupported_number"] and b["flags"]["ops_gate_blocked"]
    assert [n["surface"] for n in b["faithfulness"]["blocking"]["numbers"]] == ["3조 원"]
    assert b["flags"]["tone_drift"]
    assert a["fallback"] == {"content": False, "meta": False, "tone": False}
    assert a["timing"]["total_s"] >= a["timing"]["generation_s"]


def test_schema_failure_is_recorded_as_a_result_not_an_infra_stop(tmp_path):
    def fail(model, purpose, n):
        if purpose == "newsletter_content_gen":
            return LLMResult(text=None, parsed=None, usage=LLMUsage(), latency_s=0.1, attempts=3,
                             provider="pa", model=model, error="max retries (3) exhausted: invalid", schema_failures=3)
    stats = bo.run_generation(_items(1), {**PREREG, "candidates": PREREG["candidates"][:1]}, tmp_path,
                              client_factory=_factory([], **{"gen-a": {"fail": fail}}), pricing=PRICING,
                              log=lambda *_: None)

    assert stats["stopped"] is None
    (row,) = _rows(tmp_path / "generations.jsonl")
    content_call = row["calls"]["generation"][0]
    assert content_call["responded"] and not content_call["first_try_schema_pass"]
    assert row["fallback"]["content"] is True  # 생성기가 제목 나열 폴백을 썼다


def test_runner_resumes_after_a_402_without_duplicating_or_redoing_finished_work(tmp_path):
    items = _items(2)
    log1 = []

    def pay_wall(model, purpose, n):
        # 세 번째 작업(두 번째 클러스터의 A)부터 402
        if n > 6:
            return LLMResult(text=None, parsed=None, usage=LLMUsage(), latency_s=0.0, attempts=1,
                             provider="pa", model=model, error="Payment Required", http_status=402)

    first = bo.run_generation(items, PREREG, tmp_path, pricing=PRICING, log=lambda *_: None,
                              client_factory=_factory(log1, **{m: {"fail": pay_wall} for m in ("gen-a", "gen-b")}))

    assert first["done"] == 2 and "402" in first["stopped"]
    assert [r["key"] for r in _rows(tmp_path / "generations.jsonl")] == ["A|r1-c0", "B|r1-c0"]

    log2 = []
    second = bo.run_generation(items, PREREG, tmp_path, client_factory=_factory(log2), pricing=PRICING,
                               log=lambda *_: None)

    assert second == {"done": 2, "skipped": 2, "stopped": None}
    assert len(log2) == 6  # 남은 두 작업 × 3호출만
    keys = [r["key"] for r in _rows(tmp_path / "generations.jsonl")]
    assert sorted(keys) == ["A|r1-c0", "A|r1-c1", "B|r1-c0", "B|r1-c1"]


def test_a_torn_last_line_from_a_crash_is_redone(tmp_path):
    items = _items(1)
    bo.run_generation(items, {**PREREG, "candidates": PREREG["candidates"][:1]}, tmp_path,
                      client_factory=_factory([]), pricing=PRICING, log=lambda *_: None)
    path = tmp_path / "generations.jsonl"
    path.write_text(path.read_text(encoding="utf-8")[:-40], encoding="utf-8")  # 마지막 줄을 잘라 낸다

    stats = bo.run_generation(items, {**PREREG, "candidates": PREREG["candidates"][:1]}, tmp_path,
                              client_factory=_factory([]), pricing=PRICING, log=lambda *_: None)

    assert stats["done"] == 1
    assert [r["key"] for r in bo.JsonlStore(path).rows()] == ["A|r1-c0"]


def test_kill_switch_stops_before_any_client_is_built(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")

    def factory(provider, model):
        raise AssertionError("킬 스위치가 켜져 있는데 클라이언트를 만들었다")

    stats = bo.run_generation(_items(1), PREREG, tmp_path, client_factory=factory, pricing=PRICING,
                              log=lambda *_: None)

    assert stats["done"] == 0 and "kill switch" in stats["stopped"]
    assert not (tmp_path / "generations.jsonl").exists()


def test_kill_switch_engaged_mid_run_stops_without_recording_a_fake_fallback(tmp_path):
    def killed(model, purpose, n):
        if model == "gen-b":
            return LLMResult(text=None, parsed=None, usage=LLMUsage(), latency_s=0.0, attempts=0,
                             provider="pb", model=model, error="kill_switch")

    stats = bo.run_generation(_items(1), PREREG, tmp_path, pricing=PRICING, log=lambda *_: None,
                              client_factory=_factory([], **{"gen-b": {"fail": killed}}))

    assert "kill switch" in stats["stopped"]
    assert [r["candidate"] for r in _rows(tmp_path / "generations.jsonl")] == ["A"]


def test_unpriced_model_fails_before_spending_anything(tmp_path):
    prereg = {**PREREG, "candidates": [{"name": "Z", "generator": {"provider": "p", "model": "unknown"},
                                        "tone": {"provider": "p", "model": "unknown"}}]}
    with pytest.raises(KeyError):
        bo.run_generation(_items(1), prereg, tmp_path, client_factory=_factory([]), pricing=PRICING,
                          log=lambda *_: None)


def test_judging_covers_every_generation_and_judge_and_records_families(tmp_path):
    items = _items(1)
    bo.run_generation(items, PREREG, tmp_path, client_factory=_factory([]), pricing=PRICING, log=lambda *_: None)
    prompts = []
    factory = _factory([], **{m: {"prompts": prompts} for m in ("judge-x", "judge-y")})

    stats = bo.run_judging(items, PREREG, tmp_path, client_factory=factory, pricing=PRICING,
                           log=lambda *_: None)

    assert stats["done"] == 4
    rows = {r["key"]: r for r in _rows(tmp_path / "judgments.jsonl")}
    v2 = rows["A|r1-c0|jx-v2"]
    assert v2["result"]["criteria"] == {"faithfulness": 4, "coverage": 4, "coherence": 4, "style": 4}
    assert (v2["generator_family"], v2["judge_family"]) == ("pa", "px")
    assert rows["B|r1-c0|jy-v1"]["result"]["score"] == 7
    v2_prompts = [p for _, schema, p in prompts if schema is NewsletterEvalV2]
    v1_prompts = [p for _, schema, p in prompts if schema is NewsletterEval]
    assert len(v2_prompts) == len(v1_prompts) == 2
    assert all(BODIES[2] in p for p in v2_prompts)  # v2는 원문 본문을 본다
    assert not any(BODIES[2] in p for p in v1_prompts)  # v1 기준선은 제목만
    assert bo.run_judging(items, PREREG, tmp_path, client_factory=_factory([]), pricing=PRICING,
                          log=lambda *_: None)["skipped"] == 4


def test_cluster_eval_records_decision_and_confidence(tmp_path):
    stats = bo.run_cluster_eval(_items(2), tmp_path, {"provider": "px", "model": "judge-x"},
                                client_factory=_factory([]), pricing=PRICING, log=lambda *_: None)
    assert stats["done"] == 2
    row = _rows(tmp_path / "cluster_evals.jsonl")[0]
    assert (row["result"]["decision"], row["result"]["confidence"]) == ("PASS", 0.8)
    assert row["article_ids"] == [0, 1, 2]


def test_blind_export_hides_candidates_and_keeps_the_key_separately(tmp_path):
    items = _items(2)
    run_dir, labels_dir = tmp_path / "run", tmp_path / "labels"
    bo.run_generation(items, PREREG, run_dir, client_factory=_factory([]), pricing=PRICING, log=lambda *_: None)

    out = bo.export_blind(items, run_dir, labels_dir, PREREG, seed=5)

    assert out == {"outputs": 4, "clusters": 2}
    outputs = _rows(labels_dir / "outputs.jsonl")
    text = (labels_dir / "outputs.jsonl").read_text(encoding="utf-8")
    assert "gen-a" not in text and '"A"' not in text and "candidate" not in text
    assert "faithfulness" not in text  # 자동 지표도 보여 주지 않는다
    key = json.loads((run_dir / "blind_key.json").read_text(encoding="utf-8"))["outputs"]
    assert sorted((v["candidate"], v["item_id"]) for v in key.values()) == [
        ("A", "r1-c0"), ("A", "r1-c1"), ("B", "r1-c0"), ("B", "r1-c1")]
    assert {o["output_id"] for o in outputs} == set(key)
    relabel = json.loads((labels_dir / "relabel.json").read_text(encoding="utf-8"))
    assert len(relabel["outputs"]) == 2 and len(relabel["clusters"]) == 1
    with pytest.raises(FileExistsError):
        bo.export_blind(items, run_dir, labels_dir, PREREG, seed=5)
