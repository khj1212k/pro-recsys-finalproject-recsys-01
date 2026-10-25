#!/usr/bin/env python3
"""E0 실행이 저장한 뉴스레터로 `docs/demo/`를 만든다 (ADR 0023 "개정 (2026-10-25)").

    python3 scripts/build_e0_demo.py                      # 메인 체크아웃의 E0 run을 읽어 docs/demo/를 다시 쓴다
    python3 scripts/build_e0_demo.py --run-dir <run 디렉터리> --export-dir <반출 디렉터리> --out docs/demo

표준 라이브러리만 쓴다. LLM·네트워크 호출이 없고, 이미 만들어 둔 파일만 읽는다. 같은 입력이면 같은 출력이다(시각을 찍지 않는다).

## 입력 (모두 git 밖, 메인 체크아웃의 `data/`)
- `data/experiments/e0/<run>/`: `newsletters.jsonl`(시도·저장본·문체 변환), `clusters.jsonl`(클러스터별 시작·끝 기록),
  `nodes.jsonl`(게이트·judge 결과), `calls.jsonl`(호출별 지연·비용), `run_manifest.json`, `run_summary.json`, `blind_key.json`
- `data/exports/<T0>/articles.jsonl`: 기사 메타(언론사·제목·URL·발행 시각). 본문 칸은 이미 비어 있고 이 스크립트는 읽지도 않는다.
- 커밋된 리포트 `reports/llm/e0_baseline_v1.json`, `e0_auto_labels_v1.json`(자동 라벨러의 판정)

## 고르는 규칙
1. 뉴스레터를 고르는 대상은 `clusters.jsonl`에서 `split == "eval"`인 끝 기록(15개)이다. warmup 2개는 평가 틀 밖이라 싣지 않는다.
2. 끝 기록의 `outcome == "completed"`이면 그 `newsletter_id`로 파이프라인이 저장한 판을 찾는다.
   - 서비스 판 = `newsletters.jsonl`의 `kind == "saved"` 행 중 `news_letter_id`가 같은 것. 서비스가 실제로 내보내는 판이다(문체 변환본).
   - 정식 초안 = 같은 `news_letter_id`의 `kind == "saved_formal"` 행의 `formal`. 사실성 게이트와 judge가 검사한 형식체 초안이다.
   - 시도(`draft_attempt`)·변환 시도(`tone_attempt`)가 여럿 있어도 저장된 것만 고른다. 시도 행은 해시 대조와 키워드·범주를 읽는 데만 쓴다.
   - 문체 드리프트가 남아 형식체를 저장한 경우(`tone_fallback`이 있음)에는 서비스 판이 정식 초안과 같으므로 한 판만 싣는다.
3. 끝 기록의 `outcome`이 `completed`가 아니면(이번 실행은 `skipped` 1개) 뉴스레터 없이 실패로 표에 적는다.
4. 고른 것은 저장 기록과 맞춰 본다. 하나라도 어긋나면 아무것도 쓰지 않고 멈춘다.
   - `run_summary.json`의 `outcomes`·`n_eval`·`n_warmup`과 고른 개수
   - `nodes.jsonl`의 저장 노드(`newsletter_id`·`save_error`·`n_linked`·`n_used`·`link_gap`·`tone_fallback`)
   - 정식 초안의 sha256 = 저장 행의 `formal_sha256` = 마지막 `generate_newsletter`·`check_faithfulness`·`eval_newsletter` 노드의
     `draft_sha256` = 리포트의 `saved_formal_sha256` = 블라인드 키의 출력 sha256, 변환본의 sha256 = `convert_tone` 노드의 값
   - 서비스 판 = 변환본(`tone_fallback`이 없을 때), 연결된 기사 id가 모두 반출본에 있음
   - 호출 수와 비용 합 = 리포트의 클러스터별 값
   - judge가 지적한 문장 수와 초안에서 위치를 찾은 수 = `eval_newsletter` 노드의 값
   - 표의 합계(judge PASS, 자동 라벨러 발행 가능, 사실 오류)가 두 리포트의 집계와 같음

## 쓰는 방식
- 원문 기사 본문은 어디에도 쓰지 않는다. 기사 쪽에서 가져오는 것은 언론사·제목·URL·발행 시각뿐이다.
- 변환본의 본문에는 줄바꿈이 글자 그대로의 `\\n`으로 들어 있는 것이 있다. 문단 나눔으로 바꿔서 보여 준다.
- 발행 시각은 반출본의 `created_at`(RSS 시각, UTC)을 KST로 바꾼 것이다. AI타임스는 수집 코드를 고치기(2026-10-16 20:05 UTC) 전에 모은 행의
  RSS 시각이 9시간 늦게 저장돼 있어서 9시간을 뺐고 †를 붙인다(ADR 0008).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RUN = "run-20261017T054231Z"
DEFAULT_EXPORT = "20261017T024007Z"
DEFAULT_OUT = REPO / "docs" / "demo"
REPORTS_DIR = REPO / "reports" / "llm"

KST = timezone(timedelta(hours=9))
AITIMES = "AI타임스"
# ADR 0008: 이 시각 앞에 수집한 AI타임스 행은 RSS 시각(시간대 없는 KST)이 UTC로 읽혀 9시간 늦게 저장됐다.
AITIMES_FIX_UTC = datetime(2026, 10, 16, 20, 5, 1, 826735, tzinfo=timezone.utc)
# E0 실행 코드(code_sha 35d0a1d)의 생성기 상수다(ai_workspace/core/reconstruction/generator.py).
# 공개 쪽에서 가리는 실명. 생성 원본(data/experiments)과 선택 검증은 그대로이고 렌더링한 쪽에만 적용한다.
# 경찰이 신상을 공개한 피의자라도 공개 저장소에 이름을 범죄와 함께 영구히 남기지 않는다. 그래서 가릴 이름 목록도
# 저장소에 두지 않고 입력과 같은 곳(git 제외 data/experiments/e0/demo_redactions.json, [[이름, 대체어], ...])에 둔다.
REDACTIONS: Dict[str, str] = {}
REDACTION_NOTE = "> 이 쪽은 피의자 실명을 'A씨'로 가렸다. 생성 원본은 바꾸지 않았고 다른 내용은 그대로다."


def load_redactions(run_dir: Path) -> Dict[str, str]:
    path = run_dir.parent / "demo_redactions.json"
    return {name: alias for name, alias in read_json(path)} if path.exists() else {}


def redact(text: str) -> Tuple[str, bool]:
    hit = False
    for name, alias in REDACTIONS.items():
        if name in text:
            text, hit = text.replace(name, alias), True
    return text, hit


MAX_PROMPT_ARTICLES = 10
ARTICLE_PROMPT_CHARS = 1500

PURPOSE_KO = {
    "cluster_eval": "클러스터 평가",
    "newsletter_content_gen": "본문 생성",
    "newsletter_meta_gen": "제목·한줄소개·키워드·범주 생성",
    "newsletter_eval": "judge v2",
    "tone_convert": "문체 변환",
}
CRITERIA_KO = (("faithfulness", "사실성"), ("coverage", "커버리지"), ("coherence", "일관성"), ("style", "문체"))


class DemoError(Exception):
    """입력이 저장 기록과 맞지 않는다. 메시지는 어긋난 곳마다 한 줄이다."""


# ---------------------------------------------------------------- 읽기


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DemoError(f"{path.name}:{n} JSON을 읽지 못했다 ({exc.msg})") from None
    return rows


def read_json(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main_checkout() -> Path:
    """워크트리에서 돌려도 메인 체크아웃을 가리킨다(`data/`는 메인 체크아웃에만 둔다 - ADR 0023 개정)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out).parent
    except (OSError, subprocess.CalledProcessError):
        return REPO


@dataclass
class Inputs:
    newsletters: List[Dict[str, Any]]
    clusters: List[Dict[str, Any]]
    nodes: List[Dict[str, Any]]
    calls: List[Dict[str, Any]]
    manifest: Dict[str, Any]
    summary: Dict[str, Any]
    blind_key: Dict[str, Any]
    articles: Dict[int, Dict[str, Any]]
    baseline: Dict[str, Any]
    labels: Dict[str, Any]


def load_inputs(run_dir: Path, export_dir: Path, reports_dir: Path) -> Inputs:
    articles = {int(r["id"]): r for r in read_jsonl(export_dir / "articles.jsonl")}
    return Inputs(
        newsletters=read_jsonl(run_dir / "newsletters.jsonl"),
        clusters=read_jsonl(run_dir / "clusters.jsonl"),
        nodes=read_jsonl(run_dir / "nodes.jsonl"),
        calls=read_jsonl(run_dir / "calls.jsonl"),
        manifest=read_json(run_dir / "run_manifest.json"),
        summary=read_json(run_dir / "run_summary.json"),
        blind_key=read_json(run_dir / "blind_key.json"),
        articles=articles,
        baseline=read_json(reports_dir / "e0_baseline_v1.json"),
        labels=read_json(reports_dir / "e0_auto_labels_v1.json"),
    )


# ---------------------------------------------------------------- 선택과 검증


@dataclass
class Item:
    item_id: str
    cluster_id: int
    run_k: int
    order: int
    outcome: str
    root_cause: str
    wall_s: float
    cost_usd: float
    size: int
    bucket: str
    nodes: List[Dict[str, Any]]
    calls: List[Dict[str, Any]]
    end: Dict[str, Any]
    newsletter_id: Optional[int] = None
    service: Optional[Dict[str, Any]] = None  # title, sentence, content, keywords, categories
    formal: Optional[Dict[str, Any]] = None  # title, sentence, content, keywords, categories
    tone_fallback: Optional[str] = None
    linked_ids: List[int] = field(default_factory=list)
    n_used: int = 0
    link_gap: int = 0
    claims: List[str] = field(default_factory=list)
    claims_located: List[str] = field(default_factory=list)
    label_cluster: Optional[Dict[str, Any]] = None
    label_output: Optional[Dict[str, Any]] = None

    def node(self, name: str) -> Optional[Dict[str, Any]]:
        """같은 이름의 노드 중 마지막(저장된 시도의) 기록."""
        hits = [n for n in self.nodes if n.get("node") == name]
        return hits[-1] if hits else None

    def all_nodes(self, name: str) -> List[Dict[str, Any]]:
        return [n for n in self.nodes if n.get("node") == name]

    @property
    def completed(self) -> bool:
        return self.outcome == "completed"


def draft_sha(doc: Dict[str, Any], fields: Sequence[str]) -> str:
    """`evaluation/llm/e0_baseline.py::draft_sha256`과 같은 식 - 정식 초안은 (title, sentence, content), 변환본은 (title, summary, content)."""
    body = json.dumps({f: doc.get(f) or "" for f in fields}, ensure_ascii=False, sort_keys=True)
    return sha256(body.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


_CLAIM_LINE = re.compile(r'^- "(.*)"\s*$')


def parse_claims(feedback: str) -> List[str]:
    """judge 피드백의 '원문에서 근거를 찾을 수 없는 주장' 목록 - 초안의 부분 문자열 그대로 인용한 줄이다."""
    claims, active = [], False
    for line in (feedback or "").splitlines():
        if "근거를 찾을 수 없는 주장" in line:
            active = True
            continue
        if active:
            m = _CLAIM_LINE.match(line.strip())
            if m:
                claims.append(m.group(1))
    return claims


def _eq(problems: List[str], where: str, got: Any, want: Any, what: str) -> None:
    if got != want:
        problems.append(f"{where}: {what} 불일치 (얻은 값 {got!r}, 기대 {want!r})")


def select_items(inp: Inputs) -> Tuple[List[Item], List[str]]:
    """(평가 클러스터별 항목, 어긋난 곳 목록). 어긋난 곳이 있으면 쓰지 않는다."""
    problems: List[str] = []
    starts = {r["item_id"]: r for r in inp.clusters if r.get("kind") == "start"}
    ends = [r for r in inp.clusters if r.get("kind") == "end"]
    base_by_item = {c["item_id"]: c for c in inp.baseline.get("clusters", [])}
    nodes_by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in inp.nodes:
        if n.get("item_id"):
            nodes_by_item[n["item_id"]].append(n)
    calls_by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in inp.calls:
        if c.get("cluster_id") is not None:
            calls_by_item[f"r{c['run_k']}-c{c['cluster_id']}"].append(c)
    saved = defaultdict(list)
    formal = defaultdict(list)
    attempts = defaultdict(list)
    tone_attempts = defaultdict(list)
    for r in inp.newsletters:
        kind = r.get("kind")
        if kind == "saved":
            saved[r["news_letter_id"]].append(r)
        elif kind == "saved_formal":
            formal[r["news_letter_id"]].append(r)
        elif kind == "draft_attempt":
            attempts[r["item_id"]].append(r)
        elif kind == "tone_attempt":
            tone_attempts[r["item_id"]].append(r)
    label_clusters = {r["item_id"]: r for r in inp.labels.get("rows", {}).get("clusters", [])}
    label_outputs = {r["item_id"]: r for r in inp.labels.get("rows", {}).get("outputs", [])}
    blind = inp.blind_key.get("outputs", {})

    items: List[Item] = []
    n_warmup = 0
    for end in ends:
        iid = end["item_id"]
        if end.get("split") != "eval":
            n_warmup += 1
            continue
        base = base_by_item.get(iid)
        if base is None:
            problems.append(f"{iid}: 리포트(e0_baseline_v1.json)의 clusters에 없다")
            continue
        its = Item(
            item_id=iid, cluster_id=int(end["cluster_id"]), run_k=int(end["run_k"]), order=int(starts[iid]["order"]),
            outcome=end["outcome"], root_cause=end.get("root_cause") or "none", wall_s=float(end["wall_s"]),
            cost_usd=float(base["cost_usd"]), size=int(base["size"]), bucket=base["size_bucket"],
            nodes=nodes_by_item.get(iid, []), calls=sorted(calls_by_item.get(iid, []), key=lambda c: c["call_seq"]),
            end=end,
        )
        # 호출 수·비용 합은 완주 여부와 무관하게 리포트의 클러스터별 값과 같아야 한다.
        _eq(problems, iid, len(its.calls), int(end["calls"]), "호출 수(calls.jsonl 대 clusters.jsonl)")
        _eq(problems, iid, int(base["calls"]), int(end["calls"]), "호출 수(리포트 대 clusters.jsonl)")
        if abs(sum(c["cost_usd"] for c in its.calls) - its.cost_usd) > 1e-6:
            problems.append(f"{iid}: 호출 비용 합 {sum(c['cost_usd'] for c in its.calls):.6f} ≠ 리포트 {its.cost_usd:.6f}")
        its.label_cluster = label_clusters.get(iid)
        if its.label_cluster is None:
            problems.append(f"{iid}: 자동 라벨 리포트에 클러스터 행이 없다")
        if its.completed:
            _select_saved(its, inp, saved, formal, attempts, tone_attempts, base, blind, label_outputs, problems)
        items.append(its)

    items.sort(key=lambda i: i.order)
    summary = inp.summary
    counts = Counter(i.outcome for i in items)
    _eq(problems, "run_summary", len(items), int(summary.get("n_eval", -1)), "평가 클러스터 수")
    _eq(problems, "run_summary", n_warmup, int(summary.get("n_warmup", -1)), "warmup 수")
    _eq(problems, "run_summary", dict(counts), dict(summary.get("outcomes", {})), "outcome별 개수")
    _check_totals(items, inp, problems)
    return items, problems


def _select_saved(its: Item, inp: Inputs, saved, formal, attempts, tone_attempts, base, blind, label_outputs,
                  problems: List[str]) -> None:
    iid = its.item_id
    nid = its.end.get("newsletter_id")
    its.newsletter_id = nid
    s_rows = saved.get(nid, [])
    f_rows = [r for r in formal.get(nid, []) if r.get("item_id") == iid]
    if len(s_rows) != 1 or len(f_rows) != 1:
        problems.append(f"{iid}: news_letter_id {nid}의 saved {len(s_rows)}개·saved_formal {len(f_rows)}개 (각 1개여야 한다)")
        return
    s, f = s_rows[0], f_rows[0]
    fdoc = f["formal"]
    fsha = draft_sha(fdoc, ("title", "sentence", "content"))
    _eq(problems, iid, fsha, f["formal_sha256"], "정식 초안 sha256(본문 재계산 대 saved_formal)")
    _eq(problems, iid, fsha, base.get("saved_formal_sha256"), "정식 초안 sha256(리포트)")
    for name in ("generate_newsletter", "check_faithfulness", "eval_newsletter"):
        n = its.node(name)
        if n is None:
            problems.append(f"{iid}: {name} 노드가 없다")
        else:
            _eq(problems, iid, n.get("draft_sha256"), fsha, f"{name}의 draft_sha256")
    ok_blind = [k for k, v in blind.items() if v.get("item_id") == iid]
    for k in ok_blind:
        _eq(problems, iid, blind[k].get("sha256"), fsha, "블라인드 키의 출력 sha256")
    out_row = label_outputs.get(iid)
    if out_row is None:
        problems.append(f"{iid}: 자동 라벨 리포트에 출력 행이 없다")
    elif out_row["output_id"] not in blind or blind[out_row["output_id"]].get("item_id") != iid:
        problems.append(f"{iid}: 라벨 행의 output_id가 블라인드 키의 항목과 다르다")
    its.label_output = out_row

    save = its.node("save_newsletter")
    if save is None:
        problems.append(f"{iid}: save_newsletter 노드가 없다")
        return
    _eq(problems, iid, save.get("newsletter_id"), nid, "저장 노드의 newsletter_id")
    _eq(problems, iid, bool(save.get("save_error")), False, "저장 노드의 save_error")
    _eq(problems, iid, save.get("tone_fallback"), f.get("tone_fallback"), "저장 노드의 tone_fallback")
    its.linked_ids = [int(i) for i in s["linked_article_ids"]]
    its.n_used = int(its.end["n_articles_used"])
    its.link_gap = int(its.end["link_gap"])
    _eq(problems, iid, save.get("n_linked"), len(its.linked_ids), "연결된 기사 수(저장 노드 대 saved)")
    _eq(problems, iid, save.get("n_used"), its.n_used, "쓴 기사 수(저장 노드 대 clusters.jsonl)")
    _eq(problems, iid, len(its.linked_ids) - its.n_used, its.link_gap, "link_gap")
    _eq(problems, iid, s["raw_news_count"], len(its.linked_ids), "raw_news_count")
    missing = [i for i in its.linked_ids if i not in inp.articles]
    if missing:
        problems.append(f"{iid}: 반출본에 없는 기사 id {missing[:5]}")

    # 정식 초안의 키워드·범주는 같은 해시의 draft_attempt 행에서 읽는다.
    drafts = [a for a in attempts.get(iid, []) if a.get("sha256") == fsha]
    if len(drafts) != 1:
        problems.append(f"{iid}: 정식 초안 해시와 같은 draft_attempt 행이 {len(drafts)}개 (1개여야 한다)")
        draft_doc: Dict[str, Any] = {}
    else:
        draft_doc = drafts[0]["draft"]
        _eq(problems, iid, drafts[0].get("attempt"), int(its.end["gen_attempts"]), "저장된 초안의 시도 번호(gen_attempts)")
    its.formal = {**fdoc, "keywords": draft_doc.get("keywords", []), "categories": draft_doc.get("categories", [])}
    _eq(problems, iid, s["keywords"], its.formal["keywords"], "키워드(저장본 대 초안)")
    _eq(problems, iid, s["categories_requested"], its.formal["categories"], "범주(저장본 대 초안)")

    its.tone_fallback = f.get("tone_fallback")
    if its.tone_fallback is None:
        conv = f.get("converted") or {}
        _eq(problems, iid, (s["title"], s["sentence"], s["content"]),
            (conv.get("title"), conv.get("summary"), conv.get("content")), "서비스 판 대 변환본")
        tn = its.node("convert_tone")
        csha = draft_sha(conv, ("title", "summary", "content"))
        _eq(problems, iid, csha, tn.get("converted_sha256") if tn else None, "변환본 sha256(convert_tone 노드)")
        last_tone = (tone_attempts.get(iid) or [{}])[-1]
        _eq(problems, iid, csha, last_tone.get("sha256"), "변환본 sha256(마지막 tone_attempt)")
    else:
        _eq(problems, iid, (s["title"], s["sentence"], s["content"]),
            (fdoc["title"], fdoc["sentence"], fdoc["content"]), "서비스 판 대 정식 초안(형식체 폴백)")
    its.service = {"title": s["title"], "sentence": s["sentence"], "content": s["content"],
                   "keywords": s["keywords"], "categories": s["categories_requested"]}

    # judge가 지적한 문장: 초안에서 위치를 찾은 것만 싣는다. 개수는 노드 기록과 맞춰 본다.
    judge = its.node("eval_newsletter") or {}
    hist = (s.get("generation_history") or {}).get("attempts") or [{}]
    its.claims = parse_claims(hist[-1].get("evaluation_feedback", ""))
    text = _norm("\n".join([fdoc["title"], fdoc["sentence"], fdoc["content"]]))
    its.claims_located = [c for c in its.claims if _norm(c) and _norm(c) in text]
    _eq(problems, iid, len(its.claims), int(judge.get("n_unsupported_claims", -1)), "judge가 지적한 문장 수")
    _eq(problems, iid, len(its.claims_located), int(judge.get("claims_located", -1)), "초안에서 위치를 찾은 지적 수")


def _check_totals(items: List[Item], inp: Inputs, problems: List[str]) -> None:
    done = [i for i in items if i.completed]
    base = inp.baseline.get("metrics", {})
    judge_pass = sum(1 for i in done if (i.node("eval_newsletter") or {}).get("decision") == "PASS")
    want = base.get("8_judge_shadow", {}).get("pass_rate_default_thresholds", {})
    _eq(problems, "합계", (judge_pass, len(done)), (want.get("k"), want.get("n")), "judge v2 PASS")
    run_a = inp.labels.get("run_a", {}).get("outputs", {})
    rows = [i.label_output for i in done if i.label_output]
    for key, got in (("publishable", sum(1 for r in rows if r["a_publishable"])),
                     ("fact_error_ge1", sum(1 for r in rows if r["a_n_fact_errors"] >= 1))):
        want = run_a.get(key, {})
        _eq(problems, "합계", (got, len(rows)), (want.get("k"), want.get("n")), f"자동 라벨러 {key}")
    # 등록 주 지표는 생성하지 못한 클러스터를 N으로 세므로 분모가 평가 클러스터 수다(표 합계의 분모는 뉴스레터 수).
    primary = ((inp.labels.get("run_a") or {}).get("a8_8_uses") or {}).get("publishable_primary") or {}
    if primary:
        _eq(problems, "합계", (sum(1 for r in rows if r["a_publishable"]), len(items)), (primary.get("k"), primary.get("n")),
            "자동 라벨러 publishable_primary(등록 주 지표)")
    cl = inp.labels.get("run_a", {}).get("clusters", {}).get("single_event", {})
    got = sum(1 for i in items if i.label_cluster and i.label_cluster["a_single_event"])
    _eq(problems, "합계", (got, len(items)), (cl.get("k"), cl.get("n")), "자동 라벨러 single_event")
    cost = sum(c["cost_usd"] for i in items for c in i.calls)
    want_cost = inp.baseline.get("metrics", {}).get("5_tokens_cost", {}).get("cost_per_cluster_usd", {}).get("sum")
    if want_cost is None or abs(cost - float(want_cost)) > 1e-6:
        problems.append(f"합계: 평가 클러스터 비용 합 {cost:.6f} ≠ 리포트 {want_cost}")


# ---------------------------------------------------------------- 문자열 도구

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~$&])")


def md(text: str) -> str:
    """한 줄 안의 마크다운 기호를 글자 그대로 보이게 한다(표 칸 안에서도 쓴다)."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join((text or "").split()))


def paragraphs(text: str) -> List[str]:
    """줄바꿈(실제 줄바꿈과 글자 그대로의 `\\n`)을 문단 나눔으로 바꾼다."""
    t = (text or "").replace("\\n", "\n").replace("\r\n", "\n")
    return [p.strip() for p in t.split("\n") if p.strip()]


def md_block(text: str) -> List[str]:
    out = []
    for p in paragraphs(text):
        e = _MD_SPECIAL.sub(r"\\\1", p)
        if re.match(r"^(\d+[.)]|[-+#>=])(\s|$)", e):
            e = "\\" + e
        out.append(e)
    return out


def quote(lines: Sequence[str]) -> List[str]:
    """문단 목록을 인용 블록으로. 문단 사이는 빈 인용 줄이다."""
    out: List[str] = []
    for n, p in enumerate(lines):
        if n:
            out.append(">")
        out.append("> " + p)
    return out


def md_url(url: str) -> str:
    return (url or "").replace(" ", "%20").replace("(", "%28").replace(")", "%29") \
        .replace("<", "%3C").replace(">", "%3E").replace("|", "%7C")


def table(header: Sequence[str], rows: Sequence[Sequence[str]], align: Optional[Sequence[str]] = None) -> List[str]:
    align = align or ["---"] * len(header)
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return lines


def parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def pub_time(article: Dict[str, Any]) -> Tuple[datetime, bool]:
    """(KST 발행 시각, AI타임스 보정 여부)."""
    t = parse_utc(article["created_at"])
    fixed = False
    if article.get("press") == AITIMES and parse_utc(article["crawled_at"]) < AITIMES_FIX_UTC:
        t -= timedelta(hours=9)
        fixed = True
    return t.astimezone(KST), fixed


def fmt_time(t: datetime, fixed: bool) -> str:
    return t.strftime("%Y-%m-%d %H:%M") + (" †" if fixed else "")


def press_counts(ids: Sequence[int], articles: Dict[int, Dict[str, Any]]) -> List[Tuple[str, int]]:
    c = Counter(articles[i]["press"] for i in ids)
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))


def press_cell(ids: Sequence[int], articles: Dict[int, Dict[str, Any]], n_used: Optional[int] = None) -> str:
    """'7건 · 매일경제, 경향신문, 세계일보 외 2곳' - 기사 수가 많은 언론사부터.
    저장 단계가 생성에 쓴 것보다 많이 연결했으면(`link_gap`) '41건(생성에 36건)'처럼 쓴 수를 함께 적는다.
    언론사 집계는 연결된 기사 전체다: 어느 기사가 빠졌는지는 실행 기록에 없다."""
    names = [p for p, _ in press_counts(ids, articles)]
    shown = ", ".join(md(n) for n in names[:3]) + (f" 외 {len(names) - 3}곳" if len(names) > 3 else "")
    used = f"(생성에 {n_used}건)" if n_used is not None and n_used != len(ids) else ""
    return f"{len(ids)}건{used} · {shown}"


def usd(x: float) -> str:
    return f"${x:.4f}"


def sec(x: float) -> str:
    return f"{x:.1f}초"


# ---------------------------------------------------------------- 표 칸

def faith_cell(it: Item) -> str:
    n = it.node("check_faithfulness") or {}
    if not n.get("passed"):
        return "차단" if not n.get("gate_passed", True) else "미통과(shadow)"
    ent = (n.get("advisory") or {}).get("entities", 0)
    return "통과" + (f" · 참고 개체명 {ent}" if ent else "")


def tone_cell(it: Item) -> str:
    if it.tone_fallback:
        return "차단 · 형식체 저장"
    n = it.node("check_tone_drift") or {}
    if not n.get("passed"):
        return "미통과"
    return "통과" + (f" · 재변환 {n['retries']}" if n.get("retries") else "")


def judge_cell(it: Item) -> str:
    n = it.node("eval_newsletter") or {}
    if n.get("judge_unavailable"):
        return "채점 못함"
    extra = f", 지적 {n['n_unsupported_claims']}" if n.get("n_unsupported_claims") else ""
    score = f"{n['score']:.2f}" if n.get("score") is not None else "점수 없음"
    return f"{n.get('decision')} ({score}{extra})"


def label_cell(it: Item) -> str:
    r = it.label_output
    if not r:
        return "—"
    if r["a_publishable"]:
        return "발행 가능"
    return "발행 불가" + (f" (사실 오류 {r['a_n_fact_errors']})" if r["a_n_fact_errors"] else "")


def run_b_flipped(it: Item) -> bool:
    """같은 초안을 라벨러가 다시 라벨한 런 b가 있고 발행 가능 판정이 런 a와 다르다."""
    r = it.label_output
    return bool(r) and r.get("b_publishable") is not None and bool(r["b_publishable"]) != bool(r["a_publishable"])


def label_index_cell(it: Item) -> str:
    """색인 표의 칸: 런 a의 판정. 런 b가 뒤집었으면 괄호로 덧붙인다(라벨러의 판정이 흔들린다는 표시)."""
    cell = label_cell(it)
    if run_b_flipped(it):
        cell += f" (런 b: {'발행 가능' if it.label_output['b_publishable'] else '발행 불가'})"
    return cell


# ---------------------------------------------------------------- 쪽 그리기


def models_by_purpose(calls: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in calls:
        out.setdefault(c["purpose"], c["model"])
    return out


def failed_article_ids(it: Item, inp: Inputs) -> List[int]:
    """생성하지 못한 클러스터에 묶였던 기사 id(리포트의 클러스터 멤버십)."""
    return [int(i) for i in next(c for c in inp.baseline["clusters"] if c["item_id"] == it.item_id)["article_ids"]]


def render_item_page(it: Item, inp: Inputs) -> str:
    return _render_done(it, inp) if it.completed else _render_failed(it, inp)


def _article_table(it: Item, ids: Sequence[int], inp: Inputs) -> List[str]:
    rows, any_fixed = [], False
    arts = []
    for i in ids:
        a = inp.articles[i]
        t, fixed = pub_time(a)
        any_fixed |= fixed
        arts.append((t, i, a, fixed))
    for t, i, a, fixed in sorted(arts, key=lambda x: (x[0], x[1])):
        link = f"[{md(a['title'])}]({md_url(a['url'])})" if str(a.get("url", "")).startswith("http") else md(a["title"])
        rows.append([md(a["press"]), link, fmt_time(t, fixed)])
    lines = table(["언론사", "기사 제목(원문 링크)", "발행 시각(KST)"], rows)
    if any_fixed:
        lines += ["", "† AI타임스의 RSS 시각은 수집 코드를 고치기 전에 9시간 늦게 저장돼 있어서 9시간을 뺐다([ADR 0008](../../adr/0008-db-layer-pgvector-schema-and-upsert.md))."]
    return lines


def _cluster_eval_lines(it: Item) -> List[str]:
    out = []
    for n in it.all_nodes("eval_cluster"):
        sizes = n.get("sub_group_sizes") or []
        parts = [f"신뢰도 {n.get('confidence')}", f"이상치 {n.get('n_outliers')}건"]
        if len(sizes) > 1:
            parts.append("하위 묶음 " + "+".join(str(s) for s in sizes) + "건")
        out.append(f"- 시도 {n.get('attempt')}: {n.get('decision')} ({', '.join(parts)}), 기사 {n.get('n_articles')}건")
    return out


def _label_lines(it: Item) -> List[str]:
    out = []
    c = it.label_cluster
    if c:
        out.append(f"- 클러스터(런 a): 한 사건 {'예' if c['a_single_event'] else '아니오'}, 이상치 {c['a_n_outliers']}건, 핵심 사실 {c['a_n_key_facts']}개")
    r = it.label_output
    if r:
        out.append(f"- 정식 초안(런 a): **{label_cell(it)}**, 사실 오류 {r['a_n_fact_errors']}건, 문체 {r['a_style']}/5, "
                   f"핵심 사실 {r['a_n_covered']}/{r['a_n_facts']}개 담김, 서비스 판의 의미 변화 {'있음' if r['a_tone_drift'] else '없음'}")
        if r.get("b_publishable") is not None:
            out.append(f"- 같은 초안을 다시 라벨한 런 b: {'발행 가능' if r['b_publishable'] else '발행 불가'}, 사실 오류 {r['b_n_fact_errors']}건, 문체 {r['b_style']}/5 "
                       "(런 b는 일부 항목만 다시 라벨했다)")
    return out


def _render_done(it: Item, inp: Inputs) -> str:
    svc, frm = it.service, it.formal
    assert svc is not None and frm is not None
    L: List[str] = [f"# {md(svc['title'])}", "", "[← 데모 목록](../README.md)", ""]
    info = [
        ("항목", f"`{it.item_id}` (클러스터 {it.cluster_id}, 날짜 창 {it.run_k}) · 실행 `{inp.baseline['header']['run']}` · [KR-E0]"),
        ("범주", " · ".join(md(c) for c in svc["categories"])),
        ("키워드", " · ".join(md(k) for k in svc["keywords"])),
        ("원문 기사", f"{len(it.linked_ids)}건" + (f"(생성에 {it.n_used}건)" if it.link_gap else "") + ", "
         + ", ".join(f"{md(p)} {n}" for p, n in press_counts(it.linked_ids, inp.articles))),
        ("지연·비용", f"클러스터 벽시계 {sec(it.wall_s)}, 호출 {len(it.calls)}회, {usd(it.cost_usd)}"),
    ]
    L += table(["", ""], info)
    L += ["", "## 서비스 판 (문체 변환본)" if not it.tone_fallback else "## 서비스 판 (형식체 폴백)", ""]
    L += quote([f"**{md(svc['title'])}**", f"*{md(svc['sentence'])}*"] + md_block(svc["content"]))
    if it.tone_fallback:
        L += ["", f"문체 변환이 사실을 바꿔 형식체 초안을 저장했다(`{md(str(it.tone_fallback))}`). 아래 정식 초안과 같은 글이다."]
    else:
        L += ["", "## 정식 초안 (형식체)", "",
              "사실성 게이트와 judge v2가 검사한 초안이다. 서비스 판은 이 초안을 친근한 말투로 바꾼 것이다.", ""]
        L += quote([f"**{md(frm['title'])}**", f"*{md(frm['sentence'])}*"] + md_block(frm["content"]))

    L += ["", f"## 원문 기사 ({len(it.linked_ids)}건)"]
    notes = []
    if it.link_gap:
        notes.append(f"아래 {len(it.linked_ids)}건 중 사건 평가를 통과해 생성에 쓴 기사는 {it.n_used}건이다. 나머지 {it.link_gap}건은 이상치로 빠졌는데도 저장 단계가 연결했다"
                     "(알려진 결함 C2, [ADR 0010](../../adr/0010-faithfulness-gate-and-judge-v2.md) Addendum). "
                     "어느 기사가 빠졌는지는 실행 기록에 없어서 표에서 구분하지 못한다.")
    if it.n_used > MAX_PROMPT_ARTICLES:
        notes.append(f"생성기는 본문이 긴 순 {MAX_PROMPT_ARTICLES}건의 앞 {ARTICLE_PROMPT_CHARS:,}자만 프롬프트에 넣는다.")
    for note in notes:
        L += ["", note]
    L += [""]
    L += _article_table(it, it.linked_ids, inp)

    gen = it.end["gen_attempts"]
    L += ["", "## 검증 기록", "", "### 클러스터 평가", ""] + _cluster_eval_lines(it)
    fn = it.node("check_faithfulness") or {}
    b = fn.get("blocking") or {}
    L += ["", "### 사실성 게이트 (결정론 검사)", "",
          f"- {faith_cell(it)}. 수치 {fn.get('number_total', 0)}개를 대조했고 차단 사유는 수치 {b.get('numbers', 0)}건·인용 {b.get('quotes', 0)}건이다. "
          f"개체명은 참고만 한다(검사기 `{fn.get('entity_extractor')}`).",
          f"- 생성 시도 {gen}회. 저장된 초안은 {fn.get('attempt', 1)}번째다."]
    td = it.node("check_tone_drift") or {}
    adv = td.get("advisory") or {}
    L += ["", "### 문체 드리프트 게이트", "",
          f"- {tone_cell(it)}. 차단 사유 {len(td.get('blocking') or {})}건, 재변환 {td.get('retries', 0)}회. "
          f"참고: 빠진 개체명 {adv.get('entities_dropped', 0)}개, 상대 날짜 {adv.get('relative_dates', 0)}개."]
    jn = it.node("eval_newsletter") or {}
    crit = jn.get("criteria") or {}
    L += ["", "### judge v2 (shadow)", "",
          f"- {judge_cell(it)}. 기준별(1~5): " + ", ".join(f"{ko} {crit.get(k)}" for k, ko in CRITERIA_KO) + ".",
          "- shadow라 FAIL이어도 발행을 막지 않고 기록만 한다. 생성기와 같은 Gemini 계열이다."]
    if it.claims:
        L += [f"- judge가 근거 없다고 지적한 문장 {len(it.claims)}건 중 초안에서 위치를 찾은 것 {len(it.claims_located)}건(judge의 판단이다)."]
        for c in it.claims_located:
            L += [f"  - 「{md(c)}」"]
    L += ["", "### 자동 라벨러", ""] + _label_lines(it)
    L += ["- 사람 라벨이 아니다. 라벨러(LLM)의 판단이고 정확도는 재지 않았다.", "",
          "## 생성 정보", ""]
    rows = []
    for c in it.calls:
        label = PURPOSE_KO.get(c["purpose"], c["purpose"])
        if c["purpose"] == "cluster_eval" and len(it.all_nodes("eval_cluster")) > 1:
            label += f" #{c['graph_attempt']}"
        rows.append([label, f"`{c['model']}`", f"{c['latency_s']:.2f}초", usd(c["cost_usd"])])
    rows.append(["**합계**", "", f"{sum(c['latency_s'] for c in it.calls):.2f}초", f"**{usd(it.cost_usd)}**"])
    L += table(["단계", "모델", "지연", "비용"], rows, ["---", "---", "---:", "---:"])
    run_dt = parse_utc(inp.baseline["header"]["run_at"])
    L += ["", f"실행 {run_dt.strftime('%Y-%m-%d %H:%M')} UTC ({run_dt.astimezone(KST).strftime('%H:%M')} KST). "
          "설정과 호출 규칙은 [E0 기준선 리포트](../../../reports/llm/e0_baseline_v1.md)에 있다.", ""]
    return "\n".join(L)


def _render_failed(it: Item, inp: Inputs) -> str:
    ids = failed_article_ids(it, inp)
    L = [f"# {it.item_id}: 뉴스레터를 만들지 않은 클러스터", "", "[← 데모 목록](../README.md)", ""]
    n = (it.all_nodes("eval_cluster") or [{}])[-1]
    sizes = n.get("sub_group_sizes") or []
    L += [f"클러스터 평가가 {n.get('decision')}로 판정해서 이 클러스터는 생성 단계에 가지 않았다(`root_cause={it.root_cause}`, outcome `{it.outcome}`).", ""]
    L += _cluster_eval_lines(it)
    if sizes and max(sizes) < 3 and not n.get("n_outliers"):
        L += ["", f"가장 큰 하위 묶음이 {max(sizes)}건이어서 3건 이상이어야 하는 재시도 조건을 채우지 못했고 파이프라인은 이 클러스터를 건너뛰었다."]
    L += ["", f"호출 {len(it.calls)}회, 벽시계 {sec(it.wall_s)}, {usd(it.cost_usd)}.", "", "## 자동 라벨러", ""] + _label_lines(it)
    L += ["- 사람 라벨이 아니다. 라벨러(LLM)의 판단이고 정확도는 재지 않았다.", "", f"## 묶인 원문 기사 ({len(ids)}건)", ""]
    L += _article_table(it, ids, inp)
    L += [""]
    return "\n".join(L)


def concurrency(inp: Inputs) -> Optional[int]:
    try:
        return int(inp.manifest["effective_config"]["pipeline.concurrency"]["max_workers"])
    except (KeyError, TypeError, ValueError):
        return None


def render_index(items: List[Item], inp: Inputs) -> str:
    head = inp.baseline["header"]
    done = [i for i in items if i.completed]
    failed = [i for i in items if not i.completed]
    n_done, n_failed = len(done), len(failed)
    models = models_by_purpose(inp.calls)
    gen_model = models.get("newsletter_content_gen", "?")
    judge_model = models.get("newsletter_eval", "?")
    lab = inp.labels["header"]
    run_dt = parse_utc(head["run_at"])
    windows = head["day_windows"]
    judge_pass = sum(1 for i in done if (i.node("eval_newsletter") or {}).get("decision") == "PASS")
    pub = sum(1 for i in done if i.label_output and i.label_output["a_publishable"])
    primary = ((inp.labels.get("run_a") or {}).get("a8_8_uses") or {}).get("publishable_primary") or {}
    b_rows = [i for i in done if i.label_output and i.label_output.get("b_publishable") is not None]
    b_flips = [i for i in b_rows if run_b_flipped(i)]
    both = sum(1 for i in done if (i.node("eval_newsletter") or {}).get("decision") == "FAIL"
               and i.label_output and i.label_output["a_publishable"])
    first_try = sum(1 for i in done if int(i.end["gen_attempts"]) == 1)
    fallback = sum(1 for i in done if i.tone_fallback)
    cost_all = float(head["cost_usd"])
    cost_eval = sum(c["cost_usd"] for i in items for c in i.calls)
    workers = concurrency(inp)
    wall_note = f"(클러스터 {workers}개를 동시에 돌렸다)" if workers else ""

    L: List[str] = [
        f"# 생성 뉴스레터 데모: E0 실행이 만든 {n_done}편 [KR-E0]", "",
        f"실제 수집한 한국어 기사를 사건별로 묶은 클러스터 중 {len(items)}개(기사 {sum(i.size for i in items)}건)에 생성 파이프라인을 돌린 결과를 그대로 모았다. "
        f"손으로 고치거나 추린 것이 없다. 처리 순서대로 {n_done}편과 만들지 못한 {n_failed}건이 모두 들어 있다. "
        "각 뉴스레터 쪽에 서비스 판, 정식 초안, 원문 기사 목록(언론사·제목·원문 링크·발행 시각), 게이트·judge·자동 라벨 결과, 지연·비용이 있다.", "",
        "**E0가 무엇인가.** 2026-02 팀 종료 이후 처음으로 생성 파이프라인(클러스터 평가, 본문·제목 생성, 사실성 게이트, judge v2, 문체 변환, 문체 드리프트 게이트, 저장)을 "
        f"코드를 바꾸지 않고 실제 기사에 끝까지 돌려 본 기술 통계 기준선이다. 동결 반출본에서 클러스터 {len(items)}개를 크기(3-4 / 5-9 / 10+건) 버킷별로 층화 추출해 돌렸다. "
        "가설도 모델 비교도 없고 사람 라벨도 없다. 품질 신호는 자동 라벨러(LLM)의 판단이다.", "",
        "| 항목 | 값 |", "|---|---|",
        f"| 데이터 | [KR-E0] 한국어 수집 기사 동결 반출본({head.get('export_t0', '')[:10]} 반출), 날짜 창 {len(windows)}개(UTC): "
        + ", ".join(f"창 {n + 1} {w[0][5:16].replace('T', ' ')} ~ {w[1][5:16].replace('T', ' ')}" for n, w in enumerate(windows)) + " |",
        f"| 실행 | `{head['run']}`, {run_dt.astimezone(KST).strftime('%Y-%m-%d %H:%M')} KST ({run_dt.strftime('%H:%M')} UTC), 코드 `{head['code_sha'][:7]}` |",
        f"| 생성 모델 | `{gen_model}` (본문, 제목·한줄소개·키워드, 문체 변환) |",
        f"| 클러스터 평가·judge v2 | `{judge_model}` (judge는 shadow: 기록만 하고 발행을 막지 않는다) |",
        f"| 자동 라벨러 | `{lab['labeler_model']}` (같은 날 별도 실행, 사람 라벨 없음) |",
        f"| 비용 | 이 표의 클러스터 {len(items)}개 ${cost_eval:.6f}, 사전 점검·warmup 포함 실행 전체 ${cost_all:.6f} |", "",
        "링크: [E0 기준선 리포트](../../reports/llm/e0_baseline_v1.md) · [자동 라벨 리포트](../../reports/llm/e0_auto_labels_v1.md) · "
        "[ADR 0009 A8(프로토콜)](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md) · "
        "[ADR 0023(공개 결정)](../adr/0023-data-sources-copyright-retention.md) · "
        "[케이스 03](../case-studies/03-first-llm-measurement.md)", "",
        f"## 뉴스레터 {n_done}편과 만들지 못한 {n_failed}건", "",
    ]
    rows = []
    for it in items:
        tail = f"{sec(it.wall_s)} · {usd(it.cost_usd)}"
        if it.completed and it.service:
            rows.append([f"`{it.item_id}`", f"[{md(it.service['title'])}](newsletters/{it.item_id}.md)",
                         " · ".join(md(c) for c in it.service["categories"]), press_cell(it.linked_ids, inp.articles, it.n_used),
                         faith_cell(it), tone_cell(it), judge_cell(it), label_index_cell(it), tail])
        else:
            lc = it.label_cluster
            lab_txt = "한 사건 아님" if lc and not lc["a_single_event"] else "—"
            rows.append([f"`{it.item_id}`", f"[생성 안 됨](newsletters/{it.item_id}.md): 클러스터 평가 FAIL, 건너뜀", "—",
                         press_cell(failed_article_ids(it, inp), inp.articles), "—", "—", "—", f"(클러스터 라벨: {lab_txt})", tail])
    L += table(["항목", "뉴스레터(서비스 판 제목)", "범주", "원문 기사 · 언론사", "사실성 게이트", "문체 드리프트 게이트",
                "judge v2 (점수, 지적 수)", "자동 라벨러(런 a): 정식 초안", "지연 · 비용"], rows)
    primary_note = (f"등록 주 지표는 만들지 못한 {', '.join(f'`{i.item_id}`' for i in failed)}를 발행 불가로 세어 {primary['k']}/{primary['n']} = {primary['rate']:.3f}이고, "
                    "저장소 README와 증거 요약표가 이 값을 쓴다. " if primary and failed else "")
    L += ["", f"합계: judge v2 PASS {judge_pass}/{n_done}, 자동 라벨러 발행 가능 {pub}/{n_done}. {primary_note}"
          f"judge가 FAIL인 {n_done - judge_pass}편 중 라벨러가 발행 가능으로 본 것이 {both}편이다. "
          "둘 다 같은 Gemini 계열의 자동 판정이라 어느 쪽이 맞는지는 이 자료로 정하지 못한다. "
          "언론사별 기사 수와 기사 목록은 각 쪽에 있다.", "",
          "## 표 읽는 법", "",
          "- **서비스 판 / 정식 초안.** 서비스 판은 파이프라인이 최종으로 저장한 글이고 친근한 말투로 바꾼 문체 변환본이다. 정식 초안은 변환 전의 형식체 글로, "
          f"사실성 게이트와 judge가 검사한 것이다. {n_done}편 중 {first_try}편이 첫 시도의 초안을 저장했고 문체 변환이 사실을 바꿔 형식체로 되돌린 것은 {fallback}편이다.",
          "- **사실성 게이트.** 초안의 수치와 따옴표 인용이 원문에 있는지 보는 결정론 검사다. 개체명은 참고만 한다. 통과는 사실 검증이 아니다.",
          "- **문체 드리프트 게이트.** 변환본에서 수치와 절대 날짜가 더해지거나 빠졌는지, 고유명사가 더해졌는지 본다(설정 `TONE_DRIFT_BLOCKING_TYPES=numbers,dates,entities_added`).",
          "- **judge v2.** 사실성·커버리지·일관성·문체를 1~5로 채점하고, 모든 기준이 3 이상이며 근거 없는 주장이 0건일 때 PASS다. 점수는 기준 평균을 0~10으로 옮긴 값이다. "
          "shadow라 FAIL이어도 저장된다.",
          "- **자동 라벨러.** 정식 초안을 원문(기사당 앞 1,500자)과 대조한 LLM의 판정이다. '발행 가능'은 라벨러 자신의 판단, 사실 오류 0건, 문체 3점 이상, 제목·한줄소개 불일치 없음, "
          "빠진 핵심 사실이 절반 미만임을 모두 만족할 때다([ADR 0009](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md) A8.8·A8.13). 사람 라벨이 아니고 정확도는 재지 않았다."
          + (f" 표의 판정은 런 a다. 같은 초안을 같은 라벨러가 다시 라벨한(런 b) {len(b_rows)}편 중 "
             + (f"{len(b_flips)}편({', '.join(f'`{i.item_id}`' for i in b_flips)})은 발행 가능 판정이 달라 괄호로 덧붙였다." if b_flips else "발행 가능 판정이 달라진 것은 없다.")
             if b_rows else ""),
          f"- **지연·비용.** 지연은 한 클러스터를 처리한 벽시계 시간이다{wall_note}. 비용은 호출별 원장의 합이다.", "",
          "## 고르는 규칙", "",
          f"[`scripts/build_e0_demo.py`](../../scripts/build_e0_demo.py)가 E0 run의 저장 기록에서 만든다. 평가 클러스터 {len(items)}개(warmup {inp.summary.get('n_warmup')}개는 평가 틀 밖이라 뺀다)마다 "
          "파이프라인이 저장한 판을 `news_letter_id`로 찾는다. "
          "서비스 판은 `saved` 행, 정식 초안은 같은 id의 `saved_formal` 행이고, 생성 시도·변환 시도 가운데 저장되지 않은 것은 싣지 않는다. "
          "저장 노드·해시·호출 비용·두 리포트의 합계와 하나라도 어긋나면 아무것도 쓰지 않고 멈춘다. 자세한 규칙은 스크립트 머리말에 있다.", "",
          "다시 만들려면 메인 체크아웃에서 `python3 scripts/build_e0_demo.py`를 돌린다. 입력은 git 밖(`data/experiments/e0/`, `data/exports/`)에 있고, 호출이 없는 읽기 전용 작업이다.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- 쓰기


def build(inp: Inputs) -> Tuple[Dict[str, str], List[str]]:
    items, problems = select_items(inp)
    if problems:
        return {}, problems
    files = {"README.md": redact(render_index(items, inp))[0]}
    for it in items:
        page, hit = redact(render_item_page(it, inp))
        if hit:
            title, rest = page.split("\n", 1)
            page = f"{title}\n\n{REDACTION_NOTE}\n{rest}"
        files[f"newsletters/{it.item_id}.md"] = page
    return files, []


def write_files(out: Path, files: Dict[str, str]) -> None:
    (out / "newsletters").mkdir(parents=True, exist_ok=True)
    keep = set(files)
    for old in (out / "newsletters").glob("*.md"):
        if f"newsletters/{old.name}" not in keep:
            old.unlink()
    for rel, text in files.items():
        (out / rel).write_text(text, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    root = main_checkout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", type=Path, default=root / "data" / "experiments" / "e0" / DEFAULT_RUN)
    ap.add_argument("--export-dir", type=Path, default=root / "data" / "exports" / DEFAULT_EXPORT)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    REDACTIONS.update(load_redactions(args.run_dir))
    try:
        files, problems = build(load_inputs(args.run_dir, args.export_dir, args.reports_dir))
    except (OSError, DemoError, KeyError, ValueError) as exc:
        print(f"입력을 읽지 못했다: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"저장 기록과 맞지 않는 곳 {len(problems)}개 - 아무것도 쓰지 않았다:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    write_files(args.out, files)
    print(json.dumps({"out": str(args.out), "files": len(files), "pages": sorted(files)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
