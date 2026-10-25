"""scripts/build_e0_demo.py: E0 run의 저장 기록에서 데모 쪽을 만드는 선택 규칙과 표.
입력은 모두 지어낸 것이다 - 기사도 뉴스레터도 실제 문장이 아니다(ADR 0023: 테스트는 지어낸 문장만 쓴다)."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("build_e0_demo", REPO / "scripts" / "build_e0_demo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_e0_demo"] = module
    spec.loader.exec_module(module)
    return module


demo = _load()

RUN = "run-20990101T000000Z"
BODY_SENTINEL = "지어낸-기사-본문-표지-ZZZ"


def _call(seq, item_run_k, cluster_id, purpose, model, cost, latency=1.5, graph_attempt=1):
    return {"call_seq": seq, "run_k": item_run_k, "cluster_id": cluster_id, "purpose": purpose, "model": model,
            "cost_usd": cost, "latency_s": latency, "graph_attempt": graph_attempt}


def _five_calls(start_seq, run_k, cluster_id, cost=0.001):
    spec = [("cluster_eval", "judge-m"), ("newsletter_content_gen", "gen-m"), ("newsletter_meta_gen", "gen-m"),
            ("newsletter_eval", "judge-m"), ("tone_convert", "gen-m")]
    return [_call(start_seq + n, run_k, cluster_id, p, m, cost) for n, (p, m) in enumerate(spec)]


def _formal(title, sentence, content):
    return {"title": title, "sentence": sentence, "content": content}


def make_inputs():
    """완주 2(문체 변환 1, 형식체 폴백 1)·건너뜀 1·warmup 1. 숫자는 서로 맞게 지어 둔다."""
    articles = {
        101: {"id": 101, "press": "가짜일보", "title": "지어낸 제목 [A] | 하나", "url": "https://example.test/a/1?x=(1)",
              "created_at": "2099-01-01T01:00:00.000000Z", "crawled_at": "2099-01-01T02:00:00.000000Z", "body": BODY_SENTINEL},
        102: {"id": 102, "press": "AI타임스", "title": "지어낸 제목 둘", "url": "https://example.test/a/2",
              "created_at": "2026-10-13T13:53:53.000000Z", "crawled_at": "2026-10-13T05:05:56.000000Z", "body": BODY_SENTINEL},
        201: {"id": 201, "press": "가짜일보", "title": "지어낸 제목 셋", "url": "https://example.test/b/1",
              "created_at": "2099-01-02T01:00:00.000000Z", "crawled_at": "2099-01-02T02:00:00.000000Z", "body": None},
        202: {"id": 202, "press": "모의신문", "title": "지어낸 제목 넷", "url": "https://example.test/b/2",
              "created_at": "2099-01-02T03:00:00.000000Z", "crawled_at": "2099-01-02T04:00:00.000000Z", "body": None},
        203: {"id": 203, "press": "모의신문", "title": "지어낸 제목 다섯", "url": "https://example.test/b/3",
              "created_at": "2099-01-02T05:00:00.000000Z", "crawled_at": "2099-01-02T06:00:00.000000Z", "body": None},
        204: {"id": 204, "press": "임시방송", "title": "지어낸 제목 여섯", "url": "https://example.test/b/4",
              "created_at": "2099-01-02T05:30:00.000000Z", "crawled_at": "2099-01-02T06:30:00.000000Z", "body": None},
        301: {"id": 301, "press": "가짜일보", "title": "지어낸 제목 일곱", "url": "https://example.test/c/1",
              "created_at": "2099-01-03T01:00:00.000000Z", "crawled_at": "2099-01-03T02:00:00.000000Z", "body": None},
        302: {"id": 302, "press": "모의신문", "title": "지어낸 제목 여덟", "url": "https://example.test/c/2",
              "created_at": "2099-01-03T01:10:00.000000Z", "crawled_at": "2099-01-03T02:10:00.000000Z", "body": None},
        303: {"id": 303, "press": "임시방송", "title": "지어낸 제목 아홉", "url": "https://example.test/c/3",
              "created_at": "2099-01-03T01:20:00.000000Z", "crawled_at": "2099-01-03T02:20:00.000000Z", "body": None},
    }
    # 항목 A: 문체 변환본이 저장됐다. 변환본 본문에는 글자 그대로의 \n이 들어 있다.
    a_formal = _formal("정식 제목 가", "정식 한줄 가", "첫 문장이다. 둘째 문장이다. 셋째 문장이다.")
    a_conv = {"title": "서비스 제목 가 *별*", "summary": "서비스 한줄 가", "content": "첫 문단이에요.\\n\\n둘째 문단이에요.\\n- 목록 같은 줄"}
    # 항목 B: 문체 드리프트가 남아 형식체 초안이 저장됐다.
    b_formal = _formal("정식 제목 나", "정식 한줄 나", "나의 본문 한 줄이다.")
    sha = demo.draft_sha
    a_sha, b_sha = sha(a_formal, ("title", "sentence", "content")), sha(b_formal, ("title", "sentence", "content"))
    a_conv_sha = sha(a_conv, ("title", "summary", "content"))
    a_claim = "둘째 문장이다."
    a_feedback = ("전반적으로 괜찮다.\n원문에서 근거를 찾을 수 없는 주장(삭제하거나 원문에 있는 표현으로 고칠 것):\n"
                  f'- "{a_claim}"\n- "초안에는 없는 지적 문장"')

    def attempt_row(iid, cid, rk, doc, sha_, cats, kws):
        return {"kind": "draft_attempt", "item_id": iid, "cluster_id": cid, "run_k": rk, "attempt": 1, "sha256": sha_,
                "draft": {**doc, "categories": cats, "keywords": kws}, "fallback": []}

    newsletters = [
        attempt_row("r1-c1", 1, 1, a_formal, a_sha, ["사회"], ["키가", "키나", "키다", "키라", "키마"]),
        {"kind": "tone_attempt", "item_id": "r1-c1", "cluster_id": 1, "run_k": 1, "tone_round": 1, "converted": a_conv, "sha256": a_conv_sha},
        {"kind": "saved", "news_letter_id": 1, "title": a_conv["title"], "sentence": a_conv["summary"], "content": a_conv["content"],
         "keywords": ["키가", "키나", "키다", "키라", "키마"], "categories_requested": ["사회"], "linked_article_ids": [101, 102],
         "raw_news_count": 2, "run_id": 1, "generation_history": {"attempts": [{"evaluation_feedback": a_feedback}]}},
        {"kind": "saved_formal", "item_id": "r1-c1", "cluster_id": 1, "run_k": 1, "news_letter_id": 1, "formal": a_formal,
         "formal_sha256": a_sha, "converted": a_conv, "tone_fallback": None},
        attempt_row("r2-c2", 2, 2, b_formal, b_sha, ["경제", "정치"], ["키1", "키2", "키3", "키4", "키5"]),
        {"kind": "saved", "news_letter_id": 2, "title": b_formal["title"], "sentence": b_formal["sentence"], "content": b_formal["content"],
         "keywords": ["키1", "키2", "키3", "키4", "키5"], "categories_requested": ["경제", "정치"], "linked_article_ids": [201, 202, 203, 204],
         "raw_news_count": 4, "run_id": 1, "generation_history": {"attempts": [{"evaluation_feedback": "문제 없음."}]}},
        {"kind": "saved_formal", "item_id": "r2-c2", "cluster_id": 2, "run_k": 2, "news_letter_id": 2, "formal": b_formal,
         "formal_sha256": b_sha, "converted": None, "tone_fallback": "drift"},
    ]
    clusters = []
    for order, (iid, cid, rk, split) in enumerate([("r9-c9", 9, 9, "warmup"), ("r1-c1", 1, 1, "eval"), ("r2-c2", 2, 2, "eval"),
                                                   ("r1-c3", 3, 1, "eval")]):
        clusters.append({"kind": "start", "item_id": iid, "cluster_id": cid, "run_k": rk, "order": order, "split": split})
    ends = {
        "r9-c9": dict(outcome="completed", newsletter_id=99, calls=5, n_articles_used=3, link_gap=0, gen_attempts=1, wall_s=3.0),
        "r1-c1": dict(outcome="completed", newsletter_id=1, calls=5, n_articles_used=2, link_gap=0, gen_attempts=1, wall_s=8.5),
        "r2-c2": dict(outcome="completed", newsletter_id=2, calls=5, n_articles_used=3, link_gap=1, gen_attempts=1, wall_s=9.25),
        "r1-c3": dict(outcome="skipped", newsletter_id=None, calls=1, n_articles_used=3, link_gap=0, gen_attempts=0, wall_s=1.1),
    }
    for iid, e in ends.items():
        cid = int(iid.split("-c")[1])
        rk = int(iid[1])
        clusters.append({"kind": "end", "item_id": iid, "cluster_id": cid, "run_k": rk, "split": "warmup" if iid == "r9-c9" else "eval",
                         "root_cause": "cluster_eval_fail" if e["outcome"] == "skipped" else "none", **e})

    def nodes_for(iid, cid, rk, sha_, nid, tone_fb, judge, n_claims, located, ent=0):
        base = {"item_id": iid, "cluster_id": cid, "run_k": rk}
        out = [
            {**base, "node": "eval_cluster", "attempt": 1, "decision": "PASS", "confidence": 0.9, "n_outliers": 0, "n_articles": 3,
             "sub_group_sizes": [3]},
            {**base, "node": "generate_newsletter", "attempt": 1, "draft_sha256": sha_},
            {**base, "node": "check_faithfulness", "attempt": 1, "passed": True, "gate_passed": True, "draft_sha256": sha_,
             "blocking": {"numbers": 0, "quotes": 0}, "advisory": {"entities": ent}, "number_total": 2, "entity_extractor": "kiwi"},
            {**base, "node": "eval_newsletter", "attempt": 1, "draft_sha256": sha_, "decision": judge[0], "score": judge[1],
             "criteria": {"faithfulness": 4, "coverage": 5, "coherence": 5, "style": 5}, "n_unsupported_claims": n_claims,
             "claims_located": located, "judge_unavailable": False},
        ]
        if tone_fb is None:
            out += [{**base, "node": "convert_tone", "conversion": "ok", "tone_round": 1, "converted_sha256": a_conv_sha},
                    {**base, "node": "check_tone_drift", "passed": True, "retries": 0, "blocking": {},
                     "advisory": {"entities_dropped": 3, "relative_dates": 0}, "fallback": None}]
        else:
            out += [{**base, "node": "check_tone_drift", "passed": False, "retries": 1, "blocking": {"numbers": ["1"]},
                     "advisory": {}, "fallback": tone_fb}]
        out.append({**base, "node": "save_newsletter", "newsletter_id": nid, "save_error": False, "tone_fallback": tone_fb,
                    "n_linked": 2 if nid == 1 else 4, "n_used": 2 if nid == 1 else 3, "link_gap": 0 if nid == 1 else 1})
        return out

    nodes = (nodes_for("r1-c1", 1, 1, a_sha, 1, None, ("FAIL", 9.38), 2, 1, ent=2)
             + nodes_for("r2-c2", 2, 2, b_sha, 2, "drift", ("PASS", 10.0), 0, 0)
             + [{"item_id": "r1-c3", "cluster_id": 3, "run_k": 1, "node": "eval_cluster", "attempt": 1, "decision": "FAIL",
                 "confidence": 0.9, "n_outliers": 0, "n_articles": 3, "sub_group_sizes": [1, 2]}])
    calls = (_five_calls(1, 1, 1) + _five_calls(6, 2, 2, cost=0.002) + [_call(11, 1, 3, "cluster_eval", "judge-m", 0.0005)]
             + _five_calls(12, 9, 9)
             + [_call(17, None, None, "cluster_eval", "judge-m", 0.0001)])
    calls[-1]["run_k"], calls[-1]["cluster_id"] = None, None
    total_eval = 0.005 + 0.010 + 0.0005
    out_rows = [
        {"output_id": "oAAA", "item_id": "r1-c1", "a_publishable": True, "a_n_fact_errors": 0, "a_style": 5, "a_n_covered": 3, "a_n_facts": 3,
         "a_tone_drift": False, "b_publishable": False, "b_n_fact_errors": 1, "b_style": 4},
        {"output_id": "oBBB", "item_id": "r2-c2", "a_publishable": False, "a_n_fact_errors": 1, "a_style": 4, "a_n_covered": 1, "a_n_facts": 4,
         "a_tone_drift": False, "b_publishable": None, "b_n_fact_errors": None, "b_style": None},
    ]
    baseline = {
        "header": {"run": RUN, "run_at": "2099-01-01T00:00:00Z", "code_sha": "abcdef0123456789", "cost_usd": total_eval + 0.0051,
                   "export_t0": "2099-01-01T05:00:00+09:00", "day_windows": [["2098-12-31T21:00:00", "2099-01-01T21:00:00"]]},
        "clusters": [
            {"item_id": "r1-c1", "size": 2, "size_bucket": "3-4", "cost_usd": 0.005, "calls": 5, "saved_formal_sha256": a_sha, "article_ids": [101, 102]},
            {"item_id": "r2-c2", "size": 4, "size_bucket": "3-4", "cost_usd": 0.010, "calls": 5, "saved_formal_sha256": b_sha, "article_ids": [201, 202, 203, 204]},
            {"item_id": "r1-c3", "size": 3, "size_bucket": "3-4", "cost_usd": 0.0005, "calls": 1, "saved_formal_sha256": None, "article_ids": [301, 302, 303]},
        ],
        "metrics": {"8_judge_shadow": {"pass_rate_default_thresholds": {"k": 1, "n": 2}},
                    "5_tokens_cost": {"cost_per_cluster_usd": {"sum": total_eval}}},
    }
    labels = {
        "header": {"labeler_model": "label-m"},
        "run_a": {"outputs": {"publishable": {"k": 1, "n": 2}, "fact_error_ge1": {"k": 1, "n": 2}},
                  "clusters": {"single_event": {"k": 2, "n": 3}},
                  "a8_8_uses": {"publishable_primary": {"k": 1, "n": 3, "rate": 0.3333}}},
        "rows": {
            "clusters": [
                {"item_id": "r1-c1", "a_single_event": True, "a_n_outliers": 0, "a_n_key_facts": 3},
                {"item_id": "r2-c2", "a_single_event": True, "a_n_outliers": 0, "a_n_key_facts": 4},
                {"item_id": "r1-c3", "a_single_event": False, "a_n_outliers": 1, "a_n_key_facts": 4},
            ],
            "outputs": out_rows,
        },
    }
    return demo.Inputs(
        newsletters=newsletters, clusters=clusters, nodes=nodes, calls=calls, manifest={},
        summary={"n_eval": 3, "n_warmup": 1, "outcomes": {"completed": 2, "skipped": 1}},
        blind_key={"outputs": {"oAAA": {"item_id": "r1-c1", "sha256": a_sha}, "oBBB": {"item_id": "r2-c2", "sha256": b_sha}}},
        articles=articles, baseline=baseline, labels=labels)


@pytest.fixture
def inp():
    return make_inputs()


def test_fixture_is_consistent(inp):
    files, problems = demo.build(inp)
    assert problems == []
    assert sorted(files) == ["README.md", "newsletters/r1-c1.md", "newsletters/r1-c3.md", "newsletters/r2-c2.md"]


def test_selects_saved_service_and_formal_per_eval_cluster(inp):
    items, problems = demo.select_items(inp)
    assert problems == []
    assert [i.item_id for i in items] == ["r1-c1", "r2-c2", "r1-c3"]  # 처리 순서, warmup(r9-c9) 제외
    a, b, c = items
    assert a.service["title"] == "서비스 제목 가 *별*" and a.formal["title"] == "정식 제목 가"
    assert a.formal["categories"] == ["사회"] and a.formal["keywords"][0] == "키가"
    assert a.tone_fallback is None and a.linked_ids == [101, 102]
    assert b.tone_fallback == "drift" and b.service["content"] == b.formal["content"]
    assert (c.outcome, c.service, c.formal) == ("skipped", None, None)
    assert [x.item_id for x in items if x.completed] == ["r1-c1", "r2-c2"]


def test_judge_claims_listed_only_when_located_in_draft(inp):
    a = demo.select_items(inp)[0][0]
    assert a.claims == ["둘째 문장이다.", "초안에는 없는 지적 문장"] and a.claims_located == ["둘째 문장이다."]
    page = demo.build(inp)[0]["newsletters/r1-c1.md"]
    assert "2건 중 초안에서 위치를 찾은 것 1건" in page
    assert "「둘째 문장이다.」" in page and "초안에는 없는 지적 문장" not in page


def test_index_table_has_every_eval_cluster_and_failure_row(inp):
    readme = demo.build(inp)[0]["README.md"]
    rows = [ln for ln in readme.splitlines() if ln.startswith("| `r")]
    assert [r.split("|")[1].strip() for r in rows] == ["`r1-c1`", "`r2-c2`", "`r1-c3`"]
    a, b, c = rows
    assert "FAIL (9.38, 지적 2)" in a and "발행 가능" in a and "통과 · 참고 개체명 2" in a and "2건 · " in a
    assert "PASS (10.00)" in b and "발행 불가 (사실 오류 1)" in b and "차단 · 형식체 저장" in b
    assert "[생성 안 됨](newsletters/r1-c3.md)" in c and "한 사건 아님" in c
    assert "judge v2 PASS 1/2, 자동 라벨러 발행 가능 1/2" in readme
    assert "judge가 FAIL인 1편 중 라벨러가 발행 가능으로 본 것이 1편" in readme
    assert "`run-20990101T000000Z`" in readme


def test_reading_guide_matches_the_gate_and_labeler_rules(inp):
    """'표 읽는 법'의 정의가 코드 규칙과 같다: 드리프트 게이트는 수치·날짜를 더함과 뺌 모두, 고유명사는 더함만 막고,
    발행 가능의 커버리지 조건은 빠진 사실이 절반 '미만'이다(정확히 절반이면 불가)."""
    readme = demo.build(inp)[0]["README.md"]
    assert "수치와 절대 날짜가 더해지거나 빠졌는지, 고유명사가 더해졌는지" in readme
    assert "TONE_DRIFT_BLOCKING_TYPES=numbers,dates,entities_added" in readme
    assert "빠진 핵심 사실이 절반 미만임" in readme and "절반 이상 담김" not in readme

    sys.path.insert(0, str(REPO))
    from evaluation.llm.auto_labeler import compose_publishable  # 같은 규칙을 코드에서 확인한다

    raw = {"publishable": True, "style": 4, "headline_mismatch": False}
    assert compose_publishable(raw, n_errors=0, n_facts=6, covered=4)[0] is True   # 빠진 2/6 < 절반
    assert compose_publishable(raw, n_errors=0, n_facts=6, covered=3)[0] is False  # 정확히 절반이 빠지면 불가


def test_index_states_denominators_and_run_b_flips(inp):
    """합계는 뉴스레터 수(14)가 분모지만 등록 주 지표는 생성 실패를 N으로 세는 분모 15다. 런 b가 뒤집은 칸에는 괄호로 적는다."""
    readme = demo.build(inp)[0]["README.md"]
    assert "자동 라벨러(런 a): 정식 초안" in readme
    assert "등록 주 지표는 만들지 못한 `r1-c3`를 발행 불가로 세어 1/3 = 0.333" in readme
    rows = {r.split("|")[1].strip(): r for r in readme.splitlines() if r.startswith("| `r")}
    assert "발행 가능 (런 b: 발행 불가)" in rows["`r1-c1`"]   # 런 a Y, 런 b N
    assert "(런 b" not in rows["`r2-c2`"] and "(런 b" not in rows["`r1-c3`"]  # 런 b가 없거나 실패 행
    assert "다시 라벨한(런 b) 1편 중 1편(`r1-c1`)은 발행 가능 판정이 달라" in readme
    # 첫 문장의 163건 같은 수는 입력 전체가 아니라 돌린 클러스터에 든 기사 수다
    assert "묶은 클러스터 중 3개(기사 9건)에 생성 파이프라인을 돌린 결과" in readme


def test_run_b_flip_helper_is_false_without_run_b(inp):
    items = demo.select_items(inp)[0]
    assert [demo.run_b_flipped(i) for i in items] == [True, False, False]


def test_service_and_formal_are_both_on_the_page_and_fallback_shows_one(inp):
    pages = demo.build(inp)[0]
    a, b = pages["newsletters/r1-c1.md"], pages["newsletters/r2-c2.md"]
    assert "## 서비스 판 (문체 변환본)" in a and "## 정식 초안 (형식체)" in a
    assert "첫 문장이다. 둘째 문장이다. 셋째 문장이다." in a
    assert "## 서비스 판 (형식체 폴백)" in b and "## 정식 초안" not in b


def test_literal_backslash_n_becomes_paragraphs(inp):
    a = demo.build(inp)[0]["newsletters/r1-c1.md"]
    assert "> 첫 문단이에요.\n>\n> 둘째 문단이에요.\n>\n> \\- 목록 같은 줄" in a
    assert "\\n" not in a


def test_article_list_has_press_title_link_time_and_no_body(inp):
    pages = demo.build(inp)[0]
    a = pages["newsletters/r1-c1.md"]
    assert "[지어낸 제목 \\[A\\] \\| 하나](https://example.test/a/1?x=%281%29)" in a  # 마크다운 기호와 URL의 괄호
    assert "2099-01-01 10:00" in a  # UTC 01:00 -> KST 10:00
    assert all(BODY_SENTINEL not in text for text in pages.values())


def test_aitimes_time_before_collector_fix_is_shifted_back_nine_hours(inp):
    a = demo.build(inp)[0]["newsletters/r1-c1.md"]
    # RSS 시각 2026-10-13 13:53:53Z는 수집(05:05Z)보다 늦다. 9시간을 빼면 04:53Z = 13:53 KST.
    assert "| AI타임스 | [지어낸 제목 둘](https://example.test/a/2) | 2026-10-13 13:53 † |" in a
    assert "† AI타임스의 RSS 시각" in a
    b = demo.build(inp)[0]["newsletters/r2-c2.md"]
    assert "†" not in b


def test_link_gap_note(inp):
    pages = demo.build(inp)[0]
    b = pages["newsletters/r2-c2.md"]
    assert "생성에 쓴 기사는 3건이다. 나머지 1건은 이상치로 빠졌는데도" in b
    assert "어느 기사가 빠졌는지는 실행 기록에 없어서" in b
    assert "생성에 쓴 기사는" not in pages["newsletters/r1-c1.md"]


def test_link_gap_note_comes_before_the_article_table_and_index_shows_used_count(inp):
    pages = demo.build(inp)[0]
    b = pages["newsletters/r2-c2.md"]
    assert b.index("생성에 쓴 기사는 3건이다") < b.index("| 언론사 | 기사 제목(원문 링크) |")
    assert "| 원문 기사 | 4건(생성에 3건), " in b
    rows = {r.split("|")[1].strip(): r for r in pages["README.md"].splitlines() if r.startswith("| `r")}
    assert "| 4건(생성에 3건) · " in rows["`r2-c2`"]
    assert "(생성에" not in rows["`r1-c1`"] and "(생성에" not in rows["`r1-c3`"]


def test_failed_cluster_page(inp):
    c = demo.build(inp)[0]["newsletters/r1-c3.md"]
    assert "하위 묶음 1+2건" in c and "가장 큰 하위 묶음이 2건" in c and "지어낸 제목 일곱" in c


def test_page_cost_and_latency(inp):
    a = demo.build(inp)[0]["newsletters/r1-c1.md"]
    assert "클러스터 벽시계 8.5초, 호출 5회, $0.0050" in a
    assert "| **합계** |  | 7.50초 | **$0.0050** |" in a


@pytest.mark.parametrize("mutate, expect", [
    (lambda i: i.newsletters[3]["formal"].update(content="바뀐 본문이다."), "정식 초안 sha256"),
    (lambda i: i.summary["outcomes"].update(completed=3), "outcome별 개수"),
    (lambda i: i.summary.update(n_warmup=2), "warmup 수"),
    (lambda i: i.calls[0].update(cost_usd=0.5), "호출 비용 합"),
    (lambda i: i.nodes[3].update(draft_sha256="0" * 64), "eval_newsletter의 draft_sha256"),
    (lambda i: i.nodes[3].update(claims_located=2), "초안에서 위치를 찾은 지적 수"),
    (lambda i: i.baseline["metrics"]["8_judge_shadow"]["pass_rate_default_thresholds"].update(k=2), "judge v2 PASS"),
    (lambda i: i.labels["run_a"]["outputs"]["publishable"].update(k=2), "자동 라벨러 publishable"),
    (lambda i: i.labels["run_a"]["a8_8_uses"]["publishable_primary"].update(k=2), "publishable_primary"),
    (lambda i: i.newsletters[2].update(keywords=["다름"]), "키워드"),
    (lambda i: i.articles.pop(102), "반출본에 없는 기사 id"),
    (lambda i: i.newsletters.pop(3), "saved_formal 0개"),
    (lambda i: i.blind_key["outputs"]["oAAA"].update(sha256="1" * 64), "블라인드 키의 출력 sha256"),
])
def test_any_mismatch_with_the_saved_records_blocks_output(inp, mutate, expect):
    broken = copy.deepcopy(inp)
    mutate(broken)
    files, problems = demo.build(broken)
    assert files == {}
    assert any(expect in p for p in problems), problems


def test_service_page_must_equal_converted_text(inp):
    broken = copy.deepcopy(inp)
    broken.newsletters[2]["content"] = "저장본만 달라졌다."
    _, problems = demo.build(broken)
    assert any("서비스 판 대 변환본" in p for p in problems)


def test_main_writes_files_and_removes_stale_pages(tmp_path, inp, capsys):
    run, export, reports, out = tmp_path / "run", tmp_path / "export", tmp_path / "reports", tmp_path / "out"
    for d in (run, export, reports, out / "newsletters"):
        d.mkdir(parents=True)
    _dump(inp, run, export, reports)
    (out / "newsletters" / "r0-old.md").write_text("옛 쪽", encoding="utf-8")
    assert demo.main(["--run-dir", str(run), "--export-dir", str(export), "--reports-dir", str(reports), "--out", str(out)]) == 0
    assert sorted(p.name for p in (out / "newsletters").iterdir()) == ["r1-c1.md", "r1-c3.md", "r2-c2.md"]
    first = (out / "README.md").read_text(encoding="utf-8")
    assert demo.main(["--run-dir", str(run), "--export-dir", str(export), "--reports-dir", str(reports), "--out", str(out)]) == 0
    assert (out / "README.md").read_text(encoding="utf-8") == first  # 같은 입력이면 같은 출력


def test_main_writes_nothing_and_exits_1_on_mismatch(tmp_path, inp, capsys):
    broken = copy.deepcopy(inp)
    broken.summary["n_eval"] = 99
    run, export, reports, out = tmp_path / "run", tmp_path / "export", tmp_path / "reports", tmp_path / "out"
    for d in (run, export, reports):
        d.mkdir()
    _dump(broken, run, export, reports)
    assert demo.main(["--run-dir", str(run), "--export-dir", str(export), "--reports-dir", str(reports), "--out", str(out)]) == 1
    assert not out.exists()
    assert "평가 클러스터 수" in capsys.readouterr().err


def test_main_exits_2_when_input_file_is_missing(tmp_path, capsys):
    assert demo.main(["--run-dir", str(tmp_path / "none"), "--export-dir", str(tmp_path), "--reports-dir", str(tmp_path),
                      "--out", str(tmp_path / "out")]) == 2


def _dump(inp, run, export, reports):
    def jl(path, rows):
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    jl(run / "newsletters.jsonl", inp.newsletters)
    jl(run / "clusters.jsonl", inp.clusters)
    jl(run / "nodes.jsonl", inp.nodes)
    jl(run / "calls.jsonl", inp.calls)
    (run / "run_manifest.json").write_text("{}", encoding="utf-8")
    (run / "run_summary.json").write_text(json.dumps(inp.summary), encoding="utf-8")
    (run / "blind_key.json").write_text(json.dumps(inp.blind_key), encoding="utf-8")
    jl(export / "articles.jsonl", list(inp.articles.values()))
    (reports / "e0_baseline_v1.json").write_text(json.dumps(inp.baseline, ensure_ascii=False), encoding="utf-8")
    (reports / "e0_auto_labels_v1.json").write_text(json.dumps(inp.labels, ensure_ascii=False), encoding="utf-8")


def test_markdown_helpers_escape_symbols_and_urls():
    assert demo.md("a|b *c* [d] <e> $f") == "a\\|b \\*c\\* \\[d\\] \\<e\\> \\$f"
    assert demo.md("줄\n바꿈  공백") == "줄 바꿈 공백"
    assert demo.md_url("https://x.test/a b(c)") == "https://x.test/a%20b%28c%29"
    assert demo.md_block("# 제목 같은 줄\\n1. 번호 같은 줄") == ["\\# 제목 같은 줄", "\\1. 번호 같은 줄"]


def test_redact_replaces_listed_names_and_reports_a_hit(monkeypatch):
    monkeypatch.setattr(demo, "REDACTIONS", {"홍길동": "A씨"})  # 지어낸 이름
    text, hit = demo.redact("제목\n홍길동가 송치됐다. 홍길동를 조사했다.")
    assert "홍길동" not in text and text.count("A씨") == 2 and hit
    assert demo.redact("아무 이름 없음") == ("아무 이름 없음", False)


def test_redaction_list_is_not_kept_in_the_repository():
    assert demo.REDACTIONS == {}  # 목록은 git 제외 입력 폴더의 demo_redactions.json에서 읽는다
