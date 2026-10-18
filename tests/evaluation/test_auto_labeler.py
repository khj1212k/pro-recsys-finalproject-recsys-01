"""E0 자동 라벨러(evaluation/llm/auto_labeler.py, ADR 0009 A8.13).

전부 페이크 전송과 지어낸 기사·초안(실제 기사 아님)으로 돈다. 지출 가드(ADR 0035)는 tests/conftest.py가 준 임시 원장을 쓴다.
"""
import json
import logging
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import auto_labeler as al  # noqa: E402
from evaluation.llm import labels as label_store  # noqa: E402

NOW = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)
LONG = "가" * 1400 + "나머지본문은창밖에있다" * 40


def _articles(base, n, press="가상일보"):
    return [{"raw_news_id": base + i, "press_name": press, "title": f"지어낸 제목 {base + i}",
             "body": (LONG if i == 0 else f"지어낸 본문 {base + i}번 기사는 가상시의 도서관 이야기를 한다."), "url": f"https://example.invalid/{base + i}"}
            for i in range(n)]


DRAFT = {"title": "가상시 도서관 소식", "sentence": "가상시가 도서관 운영 시간을 늘립니다.",
         "content": "가상시는 도서관 세 곳의 운영 시간을 늘린다.  예산은 3억 원이다. 시민 반응은 좋다."}
CONVERTED = {"title": "도서관 소식!", "summary": "운영 시간이 늘어나요.", "content": "가상시 도서관이 더 오래 열어요."}


def make_package(root: Path, *, expires="2026-11-12T00:05:07Z"):
    labels = root / "labels" / "pkg"
    run = root / "run"
    labels.mkdir(parents=True)
    run.mkdir(parents=True)
    clusters = [{"item_id": "r1-c1", "articles": _articles(100, 3)},
                {"item_id": "r1-c2", "articles": _articles(200, 4)},
                {"item_id": "r2-c3", "articles": _articles(300, 3)}]
    outputs = [{"output_id": "o1", "item_id": "r1-c1", "draft": dict(DRAFT), "converted": {"title": "", "summary": "", "content": ""}},
               {"output_id": "o2", "item_id": "r2-c3", "draft": dict(DRAFT, title="가상시 도서관 다른 소식"),
                "converted": {"title": "", "summary": "", "content": ""}}]
    (labels / "clusters.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in clusters), encoding="utf-8")
    (labels / "outputs.jsonl").write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in outputs), encoding="utf-8")
    (labels / "relabel.json").write_text(json.dumps({"outputs": ["o2"], "clusters": ["r2-c3"], "min_hours_between_rounds": 48}))
    (labels / "retention.json").write_text(json.dumps({"expires_at_utc": expires}))
    (run / "blind_key.json").write_text(json.dumps({"outputs": {
        "o1": {"item_id": "r1-c1", "kind": "first_draft", "attempt": 1, "sha256": "a" * 64},
        "o2": {"item_id": "r2-c3", "kind": "first_draft", "attempt": 1, "sha256": "b" * 64}}}))
    saved = [{"kind": "saved_formal", "item_id": "r1-c1", "formal": DRAFT, "formal_sha256": "a" * 64, "converted": CONVERTED,
              "tone_fallback": None},
             {"kind": "saved_formal", "item_id": "r2-c3", "formal": DRAFT, "formal_sha256": "different", "converted": CONVERTED,
              "tone_fallback": None}]
    (run / "newsletters.jsonl").write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in saved), encoding="utf-8")
    return labels, run


@pytest.fixture
def proto():
    return al.load_protocol()


@pytest.fixture
def pkg(tmp_path):
    labels, run = make_package(tmp_path)
    return al.load_package(labels, run, now=NOW)


class FakeGemini:
    """프롬프트 틀로 호출 종류를 알아내 지어낸 JSON을 돌려준다. 받은 요청 본문을 모두 남긴다."""

    def __init__(self, proto, *, overrides=None, statuses=None):
        self.proto = proto
        self.calls = []
        self.overrides = overrides or {}
        self.statuses = list(statuses or [])

    def kind_of(self, body):
        system = body["systemInstruction"]["parts"][0]["text"]
        return next(k for k in al.KINDS if system.startswith(self.proto.prompts[k][:40]))

    def __call__(self, url, headers, body, timeout):
        self.calls.append(body)
        if self.statuses:
            st = self.statuses.pop(0)
            if st == "timeout":
                raise al.TransportTimeout("t")
            if st != 200:
                return st, {"error": {"status": "X"}}
        kind = self.kind_of(body)
        user = body["contents"][0]["parts"][0]["text"]
        if kind in self.overrides:
            resp = self.overrides[kind](user, len([c for c in self.calls if self.kind_of(c) == kind]))
        else:
            resp = default_response(kind, user)
        text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        return 200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                     "usageMetadata": {"promptTokenCount": len(user) // 2, "candidatesTokenCount": 50, "thoughtsTokenCount": 7,
                                       "totalTokenCount": len(user) // 2 + 57},
                     "modelVersion": "fake-3-flash"}


def default_response(kind, user):
    ids = [int(x) for x in re.findall(r"\[기사 id=(\d+)\]", user)]
    if kind == "cluster":
        return {"analysis": "지어낸 근거", "single_event": True, "outlier_ids": [],
                "key_facts": [{"text": f"지어낸 사실 {i}", "source_ids": [ids[0]]} for i in range(3)]}
    if kind == "output":
        return {"analysis": "지어낸 근거", "fact_errors": [{"field": "content", "text": "3억 원", "type": "number", "reason": "다르다"}],
                "style": 4, "headline_mismatch": False, "publishable": False}
    if kind == "coverage":
        n = len(re.findall(r"^\[(\d+)\] ", user, flags=re.M))
        return {"facts": [{"index": i, "covered": i < 2} for i in range(n)]}
    return {"analysis": "지어낸 근거", "tone_drift": False}


def make_labeler(proto, pkg, fake, **kw):
    store = al.Store(pkg, proto)
    transport = al.GeminiTransport(proto, api_key="test-key", post=fake, sleep=lambda s: None,
                                   on_attempt=lambda row: store.append("calls.jsonl", row))
    return al.Labeler(proto, pkg, store, transport, now=lambda: NOW, **kw)


def _ledger_rows():
    return [json.loads(l) for l in Path(os.environ["LLM_SPEND_LEDGER_FILE"]).read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------- 프로토콜


def test_protocol_is_the_registered_one_and_the_labeler_is_neither_generator_nor_judge(proto):
    lab = proto.labeler
    assert proto.model == "gemini-3-flash-preview"
    assert proto.model not in (lab["not_generator"], lab["not_judge_v2"])
    assert lab["not_generator"] == "gemini-3.5-flash-lite" and lab["not_judge_v2"] == "gemini-3.1-flash-lite"
    assert proto.seed("a") != proto.seed("b")
    assert float(proto.doc["budget"]["cap_usd"]) == 0.50 == float(proto.doc["budget"]["env"]["LLM_BUDGET_RUN_USD"])
    assert proto.doc["runs"]["b"]["coverage_key_facts_from"] == "a"
    assert proto.chars == 1500
    from core.reconstruction.generator import ARTICLE_PROMPT_CHARS

    assert proto.chars == ARTICLE_PROMPT_CHARS


def test_changed_prompt_template_is_refused(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "evaluation/llm/auto_label_prompts", root / "evaluation/llm/auto_label_prompts")
    (root / "evaluation/llm/auto_label_prompts/v1/output.txt").write_text("바뀐 프롬프트", encoding="utf-8")
    with pytest.raises(al.ProtocolError, match="output.txt"):
        al.load_protocol(al.PROTOCOL_PATH, root=root)


def test_pricing_table_has_the_labeler_model():
    from core.llm.pricing import load_pricing

    entry = load_pricing().price("gemini-3-flash-preview", on=NOW.date())
    assert (float(entry.input_per_1m), float(entry.output_per_1m)) == (0.50, 3.00)


# ---------------------------------------------------------------- 패키지와 메시지


def test_package_pairs_saved_conversions_only_when_the_saved_formal_is_the_first_draft(pkg):
    assert set(pkg.conversions) == {"o1"}      # o2의 저장 형식체 sha256은 첫 초안과 다르다
    assert pkg.conversions["o1"]["converted"] == CONVERTED


def test_expired_package_is_refused(tmp_path):
    labels, run = make_package(tmp_path, expires="2026-10-01T00:00:00Z")
    with pytest.raises(al.ProtocolError, match="마감"):
        al.load_package(labels, run, now=NOW)


def test_messages_carry_only_what_each_call_may_see(proto, pkg):
    arts = pkg.clusters["r1-c1"]["articles"]
    cluster = al.user_message(proto, "cluster", articles=arts)
    assert "가" * 1400 in cluster and "나머지본문은창밖에있다" * 20 not in cluster   # 기사당 앞 1500자
    assert DRAFT["content"] not in cluster
    output = al.user_message(proto, "output", articles=arts, draft=DRAFT)
    assert DRAFT["content"] in output and CONVERTED["content"] not in output and "지어낸 사실" not in output
    coverage = al.user_message(proto, "coverage", facts=[{"text": "지어낸 사실 0"}], draft=DRAFT)
    assert "[0] 지어낸 사실 0" in coverage and "지어낸 본문" not in coverage and "가" * 100 not in coverage
    tone = al.user_message(proto, "tone", draft=DRAFT, converted=CONVERTED)
    assert CONVERTED["content"] in tone and "지어낸 본문" not in tone


def test_request_body_uses_the_registered_generation_settings(proto):
    t = al.GeminiTransport(proto, api_key="k", post=None)
    body = t.body("output", "s", "u", 20261018)
    gen = body["generationConfig"]
    assert gen["seed"] == 20261018 and gen["temperature"] == 1.0 and gen["maxOutputTokens"] == 4096
    assert gen["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert gen["responseMimeType"] == "application/json" and gen["responseJsonSchema"] == proto.schemas["output"]
    assert t.body("coverage", "s", "u", 1)["generationConfig"]["maxOutputTokens"] == 1024
    t.schema_mode, t.thinking_spelling = "json_mode", "upper"
    gen = t.body("tone", "s", "u", 1)["generationConfig"]
    assert "responseJsonSchema" not in gen and gen["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    assert "응답 JSON 스키마" in al.system_prompt(proto, "tone", "json_mode")


# ---------------------------------------------------------------- 검증과 결정론 처리


def test_cluster_rules_mirror_the_label_store():
    ok = {"analysis": "", "single_event": True, "outlier_ids": [], "key_facts": [{"text": "a", "source_ids": [1]}] * 3}
    assert al.cluster_violations(ok, [1, 2, 3]) == []
    assert "rule:single_event_with_outliers" in al.cluster_violations(dict(ok, outlier_ids=[2]), [1, 2, 3])
    assert "rule:id_not_in_cluster" in al.cluster_violations(dict(ok, outlier_ids=[9], single_event=False), [1, 2, 3])
    two = dict(ok, single_event=False, key_facts=ok["key_facts"][:2])
    assert "rule:key_facts_zero_or_3_to_6" in al.cluster_violations(two, [1, 2, 3])
    only_out = dict(ok, single_event=False, outlier_ids=[1])
    assert "rule:key_fact_only_from_outliers" in al.cluster_violations(only_out, [1, 2, 3])
    assert al.cluster_violations(dict(ok, single_event=False, key_facts=[]), [1]) == []    # 주된 사건 없음


def test_schema_violations_are_codes_without_response_text():
    bad = {"analysis": "비밀 문장", "fact_errors": [{"field": "body", "text": "x", "type": "number", "reason": ""}],
           "style": 7, "headline_mismatch": False}
    v = al.schema_violations("output", bad)
    assert v == ["schema:fact_error_shape", "schema:style_range", "schema:missing:publishable"]
    assert all("비밀" not in x for x in v)
    assert al.schema_violations("cluster", "not a dict") == ["schema:not_object"]


def test_span_location_exact_whitespace_tolerant_or_unlocated():
    text = "예산은 3억 원이다.  시민   반응은 좋다. 예산은 3억 원이다."
    assert al.locate_span(text, "3억 원") == (4, 8, 2)
    s, e, n = al.locate_span(text, "시민 반응은")
    assert text[s:e] == "시민   반응은" and n == 1
    assert al.locate_span(text, "없는 구절") == (None, None, 0)


def test_publishable_follows_guide_rules_and_never_upgrades_the_labelers_no():
    raw = {"publishable": True, "style": 4, "headline_mismatch": False}
    assert al.compose_publishable(raw, n_errors=0, n_facts=4, covered=3)[0] is True
    assert al.compose_publishable(raw, n_errors=1, n_facts=4, covered=4)[0] is False         # 규칙 1
    assert al.compose_publishable(raw, n_errors=0, n_facts=4, covered=2)[0] is False         # 규칙 2: 절반이 빠짐
    assert al.compose_publishable(dict(raw, style=2), n_errors=0, n_facts=4, covered=4)[0] is False
    assert al.compose_publishable(dict(raw, headline_mismatch=True), n_errors=0, n_facts=4, covered=4)[0] is False
    assert al.compose_publishable(dict(raw, publishable=False), n_errors=0, n_facts=4, covered=4)[0] is False
    assert al.compose_publishable(raw, n_errors=0, n_facts=0, covered=0)[1]["coverage_ok"] is True
    ok, rules = al.compose_publishable(raw, n_errors=0, n_facts=None, covered=None)
    assert ok is True and rules["coverage_ok"] is None


# ---------------------------------------------------------------- 전송과 지출 가드


def test_every_http_attempt_is_reserved_and_settled_and_only_transient_errors_retry(proto, pkg):
    fake = FakeGemini(proto, statuses=[503, "timeout", 200])
    lab = make_labeler(proto, pkg, fake)
    system = al.system_prompt(proto, "tone", "json_schema")
    user = al.user_message(proto, "tone", draft=DRAFT, converted=CONVERTED)
    res = lab.transport.generate("tone", system, user, 1, meta={"run": "a", "target": "o1"})
    assert res.status == "ok" and res.parsed == {"analysis": "지어낸 근거", "tone_drift": False}
    assert len(fake.calls) == 3 and res.http_attempts == 3 and res.model_version == "fake-3-flash"
    rows = _ledger_rows()
    reserves = [r for r in rows if r.get("ev") == "reserve"]
    commits = [r for r in rows if r.get("ev") == "commit"]
    assert len(reserves) == 3 and [r["outcome"] for r in commits] == ["http_503", "timeout", "ok"]
    assert all(r["purpose"] == "auto_label_tone" and r["model"] == "gemini-3-flash-preview" for r in reserves)
    assert commits[0]["nusd"] == 0 and commits[1]["nusd"] == reserves[1]["nusd"]    # 오류 응답 0, 타임아웃은 예약액
    u = res.usage
    assert commits[2]["input_tokens"] == u["prompt"] and commits[2]["thinking_tokens"] == 7
    assert res.cost_usd == pytest.approx((u["prompt"] * 0.50 + 57 * 3.00) / 1e6)
    calls = lab.store.read("calls.jsonl")
    assert [c.get("status") for c in calls] == [503, None, 200]
    assert [c.get("transport_error") for c in calls] == [None, "timeout", None]


def test_non_transient_http_error_stops_the_run(proto, pkg):
    fake = FakeGemini(proto, statuses=[404])
    lab = make_labeler(proto, pkg, fake)
    with pytest.raises(al.InfraStop) as e:
        lab.transport.generate("tone", al.system_prompt(proto, "tone", "json_schema"), "u", 1, meta={})
    assert e.value.code == "http_404" and len(fake.calls) == 1


def test_run_cap_refuses_before_the_request_goes_out(proto, pkg, monkeypatch):
    from core.llm import budget

    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0.000001")
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    with pytest.raises(budget.LLMBudgetExceeded):
        lab.transport.generate("cluster", "s", "u" * 1000, 1, meta={})
    assert fake.calls == []


def test_calls_log_and_ledger_hold_no_prompt_or_response_text(proto, pkg):
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab.run(spent_usd=lambda: 0.0)
    blob = (lab.store.root / "calls.jsonl").read_text(encoding="utf-8") + Path(os.environ["LLM_SPEND_LEDGER_FILE"]).read_text()
    for s in ("지어낸 본문", "지어낸 사실", "가상시", "지어낸 근거", "3억 원"):
        assert s not in blob


# ---------------------------------------------------------------- 런 전체


def test_run_writes_label_store_rows_that_the_human_tool_would_accept(proto, pkg):
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    out = lab.run(spent_usd=lambda: 0.0)
    run_a = lab.store.root / "run-a"
    store = label_store.LabelStore(pkg.labels_dir, now=lambda: NOW, label_files_dir=run_a)
    clusters, outputs = store.latest("cluster", 1), store.latest("output", 1)
    assert set(clusters) == {"r1-c1", "r1-c2", "r2-c3"} and set(outputs) == {"o1", "o2"}
    for tid, row in clusters.items():
        store._validate("cluster", tid, row["label"])
    for tid, row in outputs.items():
        store._validate("output", tid, row["label"])
    o1 = outputs["o1"]
    assert o1["label"]["fact_errors"][0]["text"] == "3억 원" and o1["label"]["key_facts_covered"] == [0, 1]
    assert o1["label"]["publishable"] is False and o1["auto"]["rules"]["no_fact_errors"] is False
    assert o1["auto"]["tone_drift_applicable"] is True and outputs["o2"]["auto"]["tone_drift_applicable"] is False
    assert outputs["o2"]["auto"]["tone_status"] == "not_applicable"
    loaded = label_store.load_labels(pkg.labels_dir, label_files_dir=run_a)
    assert set(loaded["output"][1]) == {"o1", "o2"}
    assert out["extension_stopped_before"] is None
    assert set(lab.cluster_labels("b")) == {"r1-c1", "r1-c2", "r2-c3"} and set(lab.output_labels("b")) == {"o1", "o2"}
    for p in lab.store.root.rglob("*.jsonl"):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_run_b_starts_with_the_relabel_targets_and_asks_coverage_with_run_a_facts(proto, pkg):
    order = []
    run_a_fact = "런 a의 지어낸 사실"

    def cluster(user, n):
        ids = [int(x) for x in re.findall(r"\[기사 id=(\d+)\]", user)]
        order.append(("cluster", ids[0]))
        text = run_a_fact if n <= 3 else "런 b의 지어낸 사실"
        return {"analysis": "", "single_event": True, "outlier_ids": [],
                "key_facts": [{"text": f"{text} {i}", "source_ids": [ids[0]]} for i in range(3)]}

    seen_cov = []

    def coverage(user, n):
        seen_cov.append(user)
        return default_response("coverage", user)

    fake = FakeGemini(proto, overrides={"cluster": cluster, "coverage": coverage})
    lab = make_labeler(proto, pkg, fake)
    lab.run(spent_usd=lambda: 0.0)
    b_clusters = [o for o in order[3:]]
    assert b_clusters[0] == ("cluster", 300)      # relabel.json의 클러스터(r2-c3)가 먼저
    seeds = [c["generationConfig"]["seed"] for c in fake.calls]
    assert seeds[:9] and set(seeds[:3]) == {proto.seed("a")} and proto.seed("b") in seeds
    assert all(run_a_fact in u and "런 b의" not in u for u in seen_cov)


def test_rule_violation_is_asked_again_once_with_codes_only_then_counted_invalid(proto, pkg):
    def bad_cluster(user, n):
        return {"analysis": "지어낸 근거", "single_event": True, "outlier_ids": [999],
                "key_facts": [{"text": "응답에만 있는 문장", "source_ids": [999]}]}

    fake = FakeGemini(proto, overrides={"cluster": bad_cluster})
    lab = make_labeler(proto, pkg, fake)
    end = lab.run_unit(al.Unit("a", "cluster", "r1-c1"))
    assert end["status"] == "invalid" and end["responses"] == 2
    assert "rule:id_not_in_cluster" in end["violations"]
    second_user = fake.calls[1]["contents"][0]["parts"][0]["text"]
    assert "rule:id_not_in_cluster" in second_user and "응답에만 있는 문장" not in second_user
    assert fake.calls[0]["generationConfig"]["seed"] == fake.calls[1]["generationConfig"]["seed"]
    assert lab.cluster_labels("a") == {}


def test_coverage_and_tone_are_skipped_when_the_output_label_failed(proto, pkg):
    fake = FakeGemini(proto, overrides={"output": lambda user, n: "not json"})
    lab = make_labeler(proto, pkg, fake)
    lab._run_group([al.Unit("a", "cluster", "r1-c1")])
    lab._run_group([al.Unit("a", k, "o1") for k in ("output", "coverage", "tone")])
    done = lab.done_units()
    assert done[("a", "output", "o1")]["status"] == "invalid"
    assert done[("a", "coverage", "o1")]["status"] == "skipped_output_unlabeled"
    assert lab.output_labels("a") == {}
    kinds = [fake.kind_of(c) for c in fake.calls]
    assert kinds.count("coverage") == 0 and kinds.count("tone") == 0


def test_resume_continues_without_re_asking_finished_units(proto, pkg):
    class Crash(Exception):
        pass

    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    original = lab.transport.post
    count = {"n": 0}

    def flaky(url, headers, body, timeout):
        count["n"] += 1
        if count["n"] == 5:
            raise Crash()
        return original(url, headers, body, timeout)

    lab.transport.post = flaky
    with pytest.raises(Crash):
        lab.run(spent_usd=lambda: 0.0)
    before = len(fake.calls)
    lab2 = make_labeler(proto, pkg, fake)
    out = lab2.run(spent_usd=lambda: 0.0)
    assert out["resent_after_crash"] == 1
    keys = [(r["run"], r["kind"], r["target"]) for r in lab2.store.read("units.jsonl") if r["event"] == "end"]
    assert len(keys) == len(set(keys))                       # 끝난 단위는 한 번만
    assert len(fake.calls) > before


def test_extension_stops_before_an_item_that_would_cross_the_soft_cap(proto, pkg):
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    spent = {"v": 0.0}
    for g in lab.plan_a():
        lab._run_group(g)
    first, rest = lab.plan_b()
    for g in first:
        lab._run_group(g)
    soft = float(proto.doc["runs"]["b"]["extension_soft_cap_usd"])
    spent["v"] = soft - lab.worst_case_usd(rest[0]) + 1e-6
    out = lab.run(spent_usd=lambda: spent["v"])
    assert out["extension_stopped_before"] == rest[0][0].target
    assert set(lab.cluster_labels("b")) == {"r2-c3"}


# ---------------------------------------------------------------- 유출 방지와 보존


def test_leak_guard_refuses_report_text_that_overlaps_article_draft_or_labeler_text(proto, pkg, tmp_path):
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab.run(spent_usd=lambda: 0.0)
    guard = al.LeakGuard().add_package(pkg).add_responses(lab.store.read("rationales.jsonl"))
    safe = tmp_path / "ok.md"
    guard.write_text(safe, "# 자동 라벨 리포트\n- 발행 가능: 1/2 = 0.500 [0.095, 0.905]\n- r1-c1, o1\n")
    assert safe.exists()
    leaked = pkg.clusters["r1-c2"]["articles"][1]["body"]
    with pytest.raises(al.LeakRefused) as e:
        guard.write_text(tmp_path / "bad.md", f"요약: {leaked}")
    assert leaked not in str(e.value) and not (tmp_path / "bad.md").exists()
    with pytest.raises(al.LeakRefused):
        guard.write_text(tmp_path / "bad2.md", "가상시가 도서관 운영 시간을 늘립니다.")     # 초안 한줄소개
    with pytest.raises(al.LeakRefused):
        lab.store.path("../../escape.jsonl")


def test_free_text_is_deleted_at_the_package_deadline(proto, pkg):
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab._run_group([al.Unit("a", "cluster", "r1-c1")])
    assert (lab.store.root / "rationales.jsonl").exists()
    assert al.purge_expired(pkg.labels_dir, proto.id, now=NOW) is False
    assert al.purge_expired(pkg.labels_dir, proto.id, now=datetime(2026, 11, 13, tzinfo=timezone.utc)) is True
    assert not (lab.store.root / "rationales.jsonl").exists()
    assert (lab.store.root / "run-a" / "cluster_labels.jsonl").exists()


def test_preflight_switches_only_the_two_registered_settings_on_400(proto, pkg):
    class Picky(FakeGemini):
        def __call__(self, url, headers, body, timeout):
            gen = body["generationConfig"]
            if "responseJsonSchema" in gen:
                self.calls.append(body)
                return 400, {"error": {"status": "INVALID_ARGUMENT"}}
            return super().__call__(url, headers, body, timeout)

    fake = Picky(proto)
    lab = make_labeler(proto, pkg, fake)
    pf = al.preflight(proto, lab.transport)
    assert pf["changes"] == ["schema_mode"] and pf["schema_mode"] == "json_mode"
    assert {k: v["status"] for k, v in pf["results"].items()} == {k: "ok" for k in al.KINDS}
    users = [c["contents"][0]["parts"][0]["text"] for c in fake.calls]
    assert all("지어낸 본문" not in u for u in users)        # 패키지 기사가 아니라 지어낸 문장


def test_label_metrics_reads_auto_label_files(proto, pkg, tmp_path):
    from evaluation.llm import e0_labels

    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab.run(spent_usd=lambda: 0.0)
    items = [{"item_id": i, "split": "eval", "order": n, "articles": []} for n, i in enumerate(sorted(pkg.clusters))]
    ends = {"r1-c1": {"outcome": "completed"}, "r1-c2": {"outcome": "skipped"}, "r2-c3": {"outcome": "completed"}}
    (pkg.run_dir / "nodes.jsonl").write_text("")
    m = e0_labels.label_metrics(pkg.labels_dir, pkg.run_dir, items, ends, {}, label_files_dir=lab.store.root / "run-a")
    assert m["status"] == "complete" and m["labeled"] == {"outputs": 2, "outputs_total": 2, "clusters": 3, "clusters_total": 3}
    assert m["publishable_primary"]["n"] == 3 and m["outcome_by_single_event"]["skipped"] == {"single_event_Y": 1, "single_event_N": 0}
    assert e0_labels.label_metrics(pkg.labels_dir, pkg.run_dir, items, ends, {})["status"] == "pending"


def test_extension_worst_case_is_one_guard_reservation_per_planned_call(proto, pkg):
    """A8.13 (b)5: 다음 항목 호출들의 최악 예약액 합 - 다시 묻기는 넣지 않는다(A8.14)."""
    from core.llm import budget

    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab._run_group([al.Unit("a", "cluster", "r1-c1")])
    group = [al.Unit("b", "cluster", "r1-c1")] + [al.Unit("b", k, "o1") for k in ("output", "coverage", "tone")]
    cfg = budget.BudgetConfig.from_env()
    want = 0.0
    for u in group:
        system, user = lab._messages(u)
        est = budget.estimate_prompt_tokens([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                            proto.schemas[u.kind], bytes_per_token=cfg.bytes_per_token)
        want += (est * 0.50 + proto.max_tokens(u.kind) * 3.00) / 1e6
    assert lab.worst_case_usd(group) == pytest.approx(want)


# ---------------------------------------------------------------- 리포트 (evaluation/llm/e0_auto_label_report.py)


def test_kappa_and_agreement_definitions():
    from evaluation.llm import e0_auto_label_report as rep

    assert rep.kappa([(True, True), (False, False)]) == 1.0
    assert rep.kappa([(True, True), (True, True)]) is None                     # 한 범주만 - 정의 안 됨
    assert rep.kappa([(True, False), (False, True)]) == -1.0
    pairs = [(True, True), (True, False), (False, False), (False, False)]
    assert rep.kappa(pairs) == 0.5
    ag = rep.agreement(pairs, ["x", "y", "z", "w"])
    assert (ag["k"], ag["n"], ag["disagreement_ids"]) == (3, 4, ["y"])


def test_report_counts_ids_and_labels_only_and_passes_the_leak_guard(proto, pkg, tmp_path):
    from evaluation.llm import e0_auto_label_report as rep

    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab.run(spent_usd=lambda: 0.0)
    items = [{"item_id": i, "split": "eval", "order": n, "articles": []} for n, i in enumerate(sorted(pkg.clusters))]
    ends = {"r1-c1": {"outcome": "completed"}, "r1-c2": {"outcome": "skipped"}, "r2-c3": {"outcome": "completed"}}
    nodes = [{"node": "eval_newsletter", "attempt": 1, "item_id": "r1-c1", "criteria": {"style": 5}, "decision": "FAIL",
              "n_unsupported_claims": 2},
             {"node": "eval_cluster", "item_id": "r1-c2", "call_parsed": True, "decision": "FAIL"}]
    (pkg.run_dir / "nodes.jsonl").write_text("".join(json.dumps(n) + "\n" for n in nodes))
    report, guard = rep.build(proto, pkg, items=items, ends=ends, nodes=nodes, now=NOW)
    a = report["run_a"]
    assert a["clusters"]["single_event"]["k"] == 3 and a["outputs"]["fact_error_ge1"] == {"k": 2, "n": 2, "rate": 1.0,
                                                                                            "wilson95": [0.3424, 1.0]}
    assert a["outputs"]["key_fact_coverage_micro"]["k"] == 4 and a["outputs"]["tone_drift"]["n"] == 1
    assert report["run_a_vs_run_b"]["publishable"]["n"] == 2
    jt = report["labeler_vs_judge_v2"]["judge_decision_vs_publishable"]["table"]
    assert jt == {"judge_pass_pub_Y": 0, "judge_pass_pub_N": 0, "judge_fail_pub_Y": 0, "judge_fail_pub_N": 1}
    assert report["labeler_vs_cluster_evaluator"]["first_cluster_eval_vs_single_event"]["table"]["fail_single_Y"] == 1
    assert "checker_vs_auto_labeler_first_draft" in a["a8_8_uses"] and "relabel" not in a["a8_8_uses"]
    md = rep.render_md(report)
    out = tmp_path / "r.md"
    guard.write_text(out, md)
    guard.write_text(tmp_path / "r.json", json.dumps(report, ensure_ascii=False))
    for text in ("지어낸 본문", "지어낸 사실", "지어낸 근거", DRAFT["sentence"]):
        assert text not in md and text not in json.dumps(report, ensure_ascii=False)
    with pytest.raises(al.LeakRefused):
        guard.write_text(tmp_path / "bad.md", md + "\n" + pkg.clusters["r2-c3"]["articles"][2]["body"])


# ---------------------------------------------------------------- main() - 종료 코드, 멈춤 기록, pre-flight 게이트 (리뷰 반영)


class Cli:
    """`auto_labeler.main`을 페이크 전송과 임시 패키지로 돌린다(실제 네트워크·메인 체크아웃 data/ 없음)."""

    def __init__(self, tmp_path, monkeypatch, proto):
        self.proto = proto
        self.labels, self.run = make_package(tmp_path, expires="2099-01-01T00:00:00Z")
        monkeypatch.setattr(al, "_paths", lambda p: (self.labels, self.run))
        monkeypatch.setattr(al, "_check_package", lambda p, k: None)
        monkeypatch.setattr(al, "api_key", lambda: "test-key")
        monkeypatch.setenv("LLM_RUN_ID", "placeholder")      # configure_budget가 바꾸는 값을 테스트가 끝나면 되돌리려고
        self.monkeypatch = monkeypatch
        self.fake = None

    def use(self, fake):
        self.fake = fake
        post = fake

        class Injected(al.GeminiTransport):
            def __init__(self, *a, **k):
                k.setdefault("post", post)
                k.setdefault("sleep", lambda s: None)
                super().__init__(*a, **k)

        self.monkeypatch.setattr(al, "GeminiTransport", Injected)
        return fake

    def store_root(self):
        return self.labels / "auto" / self.proto.id

    def manifest(self):
        return json.loads((self.store_root() / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def cli(tmp_path, monkeypatch, proto):
    yield Cli(tmp_path, monkeypatch, proto)
    logging.disable(logging.NOTSET)       # main()이 _quiet()로 끈 로깅을 되돌린다


def test_main_runs_to_the_end_and_records_the_invocation(cli, proto, capsys):
    fake = cli.use(FakeGemini(proto))
    assert al.main(["run"]) == 0
    m = cli.manifest()
    assert m["preflight_ok"] is True and m["runs"][-1]["stop"] is None
    assert set(m["preflight"]["results"]) == set(al.KINDS)
    rows = [json.loads(l) for l in (cli.store_root() / "run-a" / "output_labels.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["target_id"] for r in rows} == {"o1", "o2"}
    pre = [c["contents"][0]["parts"][0]["text"] for c in fake.calls[:4]]
    assert all("가상시" in u and "지어낸 본문" not in u for u in pre)       # 앞의 4건은 pre-flight(지어낸 문장)
    n = len(fake.calls)
    assert al.main(["run"]) == 0                                            # 끝난 런을 다시 돌려도 pre-flight·라벨링 호출이 없다
    assert len(fake.calls) == n
    capsys.readouterr()


def test_failed_preflight_is_not_skipped_by_the_next_run(cli, proto):
    """A8.13 (b)7 (iii): 실패한 pre-flight 뒤에는 라벨링을 시작하지 않는다 - 다음 `run`이 기록만 보고 건너뛰면 안 된다."""
    fake = cli.use(FakeGemini(proto, overrides={"tone": lambda user, n: "not json"}))
    assert al.main(["preflight"]) == 3
    m = cli.manifest()
    assert m["preflight_ok"] is False and m["preflight"]["results"]["tone"]["status"] == "schema_invalid"
    before = len(fake.calls)
    assert al.main(["run"]) == 3                                  # pre-flight를 다시 하고, 또 실패해서 시작하지 않는다
    after = fake.calls[before:]
    assert len(after) == 4 and all("지어낸 본문" not in c["contents"][0]["parts"][0]["text"] for c in after)
    assert not (cli.store_root() / "run-a" / "cluster_labels.jsonl").exists()
    assert "runs" not in cli.manifest()


def test_run_repeats_a_failed_preflight_and_starts_once_it_passes(cli, proto):
    state = {"broken": True}

    def tone(user, n):
        return "not json" if state["broken"] else default_response("tone", user)

    cli.use(FakeGemini(proto, overrides={"tone": tone}))
    assert al.main(["preflight"]) == 3
    state["broken"] = False
    assert al.main(["run"]) == 0
    m = cli.manifest()
    assert m["preflight_ok"] is True and m["runs"][-1]["stop"] is None


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_transient_http_errors_stop_without_retry(proto, pkg, status):
    fake = FakeGemini(proto, statuses=[status])
    lab = make_labeler(proto, pkg, fake)
    with pytest.raises(al.InfraStop) as e:
        lab.transport.generate("tone", al.system_prompt(proto, "tone", "json_schema"), "u", 1, meta={})
    assert e.value.code == f"http_{status}" and e.value.status == status and len(fake.calls) == 1


def test_payment_required_stops_through_the_circuit_breaker_and_the_attempt_is_logged(cli, proto):
    from core.llm import budget

    fake = cli.use(FakeGemini(proto, statuses=[200, 200, 200, 200, 402]))
    assert al.main(["run"]) == 4
    last = cli.manifest()["runs"][-1]
    assert last["stop"] == {"kind": "infra", "code": budget.LLMCircuitOpen.code}
    calls = [json.loads(l) for l in (cli.store_root() / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c["status"] for c in calls if c["run"] != "preflight"] == [402]       # 런을 멈춘 시도도 기록에 있다
    assert len(fake.calls) == 5


@pytest.mark.parametrize("status", [429, 503])
def test_transient_http_errors_retry_to_the_registered_limit_then_stop(proto, pkg, status):
    fake = FakeGemini(proto, statuses=[status] * 6)
    sleeps = []
    lab = make_labeler(proto, pkg, fake)
    lab.transport.sleep = sleeps.append
    with pytest.raises(al.InfraStop) as e:
        lab.transport.generate("tone", al.system_prompt(proto, "tone", "json_schema"), "u", 1, meta={})
    assert e.value.code == f"http_{status}" and len(fake.calls) == 4 and sleeps == [2.0, 4.0, 8.0]


def test_main_records_an_infra_stop_and_returns_4(cli, proto):
    cli.use(FakeGemini(proto, statuses=[200, 200, 200, 200, 404]))     # pre-flight 4건 뒤 첫 라벨링 호출이 404
    assert al.main(["run"]) == 4
    last = cli.manifest()["runs"][-1]
    assert last["stop"] == {"kind": "infra", "code": "http_404"} and last["result"] == {}
    calls = [json.loads(l) for l in (cli.store_root() / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c.get("status") for c in calls if c["run"] != "preflight"] == [404]


def test_main_records_retry_exhaustion_as_an_infra_stop(cli, proto):
    cli.use(FakeGemini(proto, statuses=[200, 200, 200, 200, 429, 429, 429, 429]))
    assert al.main(["run"]) == 4
    assert cli.manifest()["runs"][-1]["stop"] == {"kind": "infra", "code": "http_429"}


def test_main_does_not_start_when_the_operators_day_cap_leaves_less_than_the_registered_headroom(cli, proto, monkeypatch, capsys):
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "0.30")
    fake = cli.use(FakeGemini(proto))
    assert al.main(["run"]) == 2
    out = capsys.readouterr().out
    assert fake.calls == [] and "day 상한 여유" in out
    assert "더 낮은 상한을 따른다" in out and "LLM_BUDGET_DAY_USD=0.30" in out


def test_configure_budget_never_raises_a_cap_the_operator_set_lower(proto, monkeypatch):
    monkeypatch.setenv("LLM_RUN_ID", "placeholder")
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0.10")
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "5")
    env = al.configure_budget(proto)
    assert env["LLM_BUDGET_RUN_USD"] == "0.10"            # 더 낮은 값을 따른다
    assert env["LLM_BUDGET_DAY_USD"] == "1.00"            # 더 높은 값은 등록값으로 내린다
    assert os.environ["LLM_BUDGET_RUN_USD"] == "0.10" and os.environ["LLM_BUDGET_DAY_USD"] == "1.00"
    assert env["LLM_RUN_ID"] == "e0-auto-labels-v1"
    monkeypatch.delenv("LLM_BUDGET_RUN_USD")
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "not a number")
    env = al.configure_budget(proto)
    assert env["LLM_BUDGET_RUN_USD"] == "0.50" and env["LLM_BUDGET_DAY_USD"] == "1.00"    # 없거나 읽을 수 없으면 등록값


def test_preflight_switches_thinking_spelling_after_the_schema_mode_and_then_gives_up(proto, pkg):
    class LowerCaseRefused(FakeGemini):
        def __call__(self, url, headers, body, timeout):
            if body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "minimal":
                self.calls.append(body)
                return 400, {"error": {"status": "INVALID_ARGUMENT"}}
            return super().__call__(url, headers, body, timeout)

    lab = make_labeler(proto, pkg, LowerCaseRefused(proto))
    pf = al.preflight(proto, lab.transport)
    assert pf["changes"] == ["schema_mode", "thinking_spelling"]
    assert pf["schema_mode"] == "json_mode" and pf["thinking_spelling"] == "upper"
    assert {k: v["status"] for k, v in pf["results"].items()} == {k: "ok" for k in al.KINDS}

    class AlwaysRefused(FakeGemini):
        def __call__(self, url, headers, body, timeout):
            self.calls.append(body)
            return 400, {"error": {"status": "INVALID_ARGUMENT"}}

    refused = AlwaysRefused(proto)
    lab = make_labeler(proto, pkg, refused)
    with pytest.raises(al.InfraStop) as e:
        al.preflight(proto, lab.transport)
    assert e.value.status == 400 and lab.transport.schema_mode == "json_mode" and lab.transport.thinking_spelling == "upper"
    assert len(refused.calls) == 3          # 처음 + 바꾼 것 둘, 그 뒤에는 더 바꾸지 않는다


# ---------------------------------------------------------------- 재개와 기록의 빈틈 (리뷰 반영)


def test_resume_assembles_a_complete_group_whose_output_label_was_never_written(proto, pkg):
    """묶음의 마지막 단위를 끝낸 뒤 조립 전에 죽었다면, 재개가 그 묶음을 건너뛰어 출력 라벨 행이 영영 없으면 안 된다."""
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    for g in lab.plan_a():
        lab._run_group(g)
    first, rest = lab.plan_b()
    for g in first:
        lab._run_group(g)
    group = rest[0]
    assert any(u.kind == "output" for u in group)
    for u in group:
        lab.run_unit(u)                                  # 단위만 끝내고 조립은 하지 않는다(여기서 프로세스가 죽었다고 본다)
    out_unit = next(u for u in group if u.kind == "output")
    assert out_unit.target not in lab.output_labels("b")
    lab2 = make_labeler(proto, pkg, fake)
    lab2.run(spent_usd=lambda: 0.0)
    assert out_unit.target in lab2.output_labels("b")
    keys = [(r["run"], r["kind"], r["target"]) for r in lab2.store.read("units.jsonl") if r["event"] == "end"]
    assert len(keys) == len(set(keys))                   # 끝난 단위를 다시 묻지 않았다
    rows = lab2.store.read("run-b/output_labels.jsonl")
    assert len({r["target_id"] for r in rows}) == len(rows)


def test_the_attempt_that_opens_the_circuit_breaker_is_still_logged(proto, pkg, monkeypatch):
    from core.llm import budget

    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_THRESHOLD", "3")
    fake = FakeGemini(proto, statuses=[503, 503, 503])
    lab = make_labeler(proto, pkg, fake)
    with pytest.raises(budget.LLMCircuitOpen):
        lab.transport.generate("tone", al.system_prompt(proto, "tone", "json_schema"), "u", 1, meta={"run": "a", "target": "o1"})
    assert len(fake.calls) == 3
    calls = lab.store.read("calls.jsonl")
    assert [c["status"] for c in calls] == [503, 503, 503] and [c["attempt"] for c in calls] == [1, 2, 3]
    commits = [r for r in _ledger_rows() if r.get("ev") == "commit"]
    assert len(commits) == len(calls) == len(fake.calls)         # 원장·호출 기록·전송 수가 어긋나지 않는다


def test_resent_after_crash_counts_only_calls_that_may_have_gone_out(proto, pkg, monkeypatch):
    from core.llm import budget

    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0.000001")
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    with pytest.raises(budget.LLMBudgetExceeded):
        lab.run(spent_usd=lambda: 0.0)
    assert fake.calls == []                                       # 가드가 요청 전에 거절했다
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "1000000")
    budget.reset_run_state()                                      # 새 프로세스로 재개한다(중단 래치는 프로세스 안에만 있다)
    lab2 = make_labeler(proto, pkg, fake)
    out = lab2.run(spent_usd=lambda: 0.0)
    assert out["resent_after_crash"] == 0                        # 보낸 적 없는 호출을 '다시 보낸 호출'로 세지 않는다


# ---------------------------------------------------------------- 마감과 자유 텍스트 (리뷰 반영)


def test_opening_the_label_tools_after_the_deadline_also_deletes_the_labelers_free_text(proto, pkg):
    """ADR 0023 개정의 '열 때마다 정리' - 라벨 도구·load_labels·label_metrics가 패키지를 열 때도 rationales.jsonl이 지워져야 한다."""
    fake = FakeGemini(proto)
    lab = make_labeler(proto, pkg, fake)
    lab.run(spent_usd=lambda: 0.0)
    rationales = lab.store.root / "rationales.jsonl"
    assert rationales.exists()
    before = datetime(2026, 11, 11, tzinfo=timezone.utc)
    after = datetime(2026, 11, 13, tzinfo=timezone.utc)
    label_store.LabelStore(pkg.labels_dir, now=lambda: before, label_files_dir=lab.store.root / "run-a")
    assert rationales.exists()                                    # 마감 전에는 그대로
    store = label_store.LabelStore(pkg.labels_dir, now=lambda: after, label_files_dir=lab.store.root / "run-a")
    assert store.expired and not rationales.exists()
    assert (lab.store.root / "run-a" / "output_labels.jsonl").exists() and (lab.store.root / "run-b" / "cluster_labels.jsonl").exists()
    assert set(store.latest("output", 1)) == {"o1", "o2"}          # 라벨 값은 남는다
    assert label_store.purge_expired_bodies(pkg.labels_dir, after) == 0      # 여러 번 불러도 같다


def test_a_quote_the_labeler_could_not_place_in_the_draft_is_not_kept_in_the_label_file(proto, pkg):
    quoted = "초안에 없는 라벨러의 인용 문장"

    def output(user, n):
        return {"analysis": "지어낸 근거", "fact_errors": [{"field": "content", "text": quoted, "type": "quote", "reason": "다르다"}],
                "style": 4, "headline_mismatch": False, "publishable": True}

    fake = FakeGemini(proto, overrides={"output": output})
    lab = make_labeler(proto, pkg, fake)
    lab._run_group([al.Unit("a", "cluster", "r1-c1")])
    lab._run_group([al.Unit("a", k, "o1") for k in ("output", "coverage", "tone")])
    row = lab.output_labels("a")["o1"]
    un = row["auto"]["unlocated_fact_errors"]
    assert len(un) == 1 and un[0]["field"] == "content" and un[0]["type"] == "quote" and un[0]["n_chars"] == len(quoted)
    assert quoted not in json.dumps(row, ensure_ascii=False)
    assert quoted in (lab.store.root / "rationales.jsonl").read_text(encoding="utf-8")      # 근거 파일에만 있고 마감에 지운다
    assert row["label"]["publishable"] is False and row["auto"]["rules"]["no_fact_errors"] is False    # 오류 수에는 센다
