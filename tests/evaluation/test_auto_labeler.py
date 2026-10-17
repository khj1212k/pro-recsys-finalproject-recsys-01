"""E0 자동 라벨러(evaluation/llm/auto_labeler.py, ADR 0009 A8.13).

전부 페이크 전송과 지어낸 기사·초안(실제 기사 아님)으로 돈다. 지출 가드(ADR 0035)는 tests/conftest.py가 준 임시 원장을 쓴다.
"""
import json
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
