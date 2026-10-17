"""E0 자동 라벨러 - 사람 라벨(A8.8) 대신 LLM이 라벨링 가이드의 정의로 라벨을 단다 (ADR 0009 A8.13).

프로토콜 값은 `evaluation/llm/preregistration/e0-auto-labels-v1.yaml`, 프롬프트 틀과 응답 스키마는
`evaluation/llm/auto_label_prompts/v1/`에 있고 시작할 때 sha256을 대조한다(다르면 호출하지 않는다).

    python -m evaluation.llm.auto_labeler preflight          # 지어낸 문장으로 호출 종류마다 1건(기사 없음)
    python -m evaluation.llm.auto_labeler run                # 런 a 전부 -> 런 b(재라벨 대상 -> 예산 안에서 나머지)
    python -m evaluation.llm.auto_labeler status             # 건수·지출만(텍스트 없음)

쓰는 곳(전부 <메인 체크아웃>/data/labels/<패키지>/auto/<프로토콜 id>/ 아래, gitignore, 0600):
  run-a|run-b/cluster_labels.jsonl, output_labels.jsonl  <- `labels.LabelStore`와 같은 행 형식(+ 출처 칸 "auto")
  units.jsonl      <- (런, 호출 종류, 대상)마다 시작·끝 행. 재개의 기준
  calls.jsonl      <- HTTP 시도마다 수치·id만(상태, 종료 사유, 토큰, 비용, 지연, 모델 버전)
  rationales.jsonl <- 라벨러 응답 전체(근거 텍스트 포함). 패키지 마감에 지운다(ADR 0023 개정)
  manifest.json    <- 프로토콜 id, 모델, pre-flight 결과, 쓴 스키마 방식
라벨러에게 주는 것은 패키지의 기사(기사당 앞 1500자)와 초안, 저장본의 문체 변환본뿐이다. 블라인드 키·게이트·judge 결과는
주지 않는다. 화면에는 진행 건수와 누적 비용만 낸다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_REL = "evaluation/llm/preregistration/e0-auto-labels-v1.yaml"
PROTOCOL_PATH = REPO_ROOT / PROTOCOL_REL
KINDS = ("cluster", "output", "coverage", "tone")
FIELDS = ("title", "sentence", "content")
ERROR_TYPES = ("number", "date", "entity", "quote", "other")
RESPONSE_STATUSES = ("ok", "schema_invalid", "length", "content_filter")
FILTER_FINISH = {"SAFETY", "RECITATION", "LANGUAGE", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
                 "MALFORMED_RESPONSE", "ESCALATION"}


class ProtocolError(RuntimeError):
    """프로토콜 파일·프롬프트·패키지가 등록한 것과 다르다(호출하지 않는다)."""


class InfraStop(RuntimeError):
    """재시도하지 않는 인프라 실패(A8.13 (b)7 ii) - 런을 멈춘다."""

    def __init__(self, code: str, status: Optional[int] = None):
        super().__init__(code)
        self.code = code
        self.status = status


class LeakRefused(RuntimeError):
    """저장소에 쓰려는 텍스트가 기사·초안·라벨러 텍스트와 겹친다(어느 조각인지는 적지 않는다)."""


class TransportTimeout(Exception):
    pass


class TransportConnection(Exception):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- 프로토콜


@dataclass
class Protocol:
    doc: Dict[str, Any]
    prompts: Dict[str, str]
    schemas: Dict[str, Dict[str, Any]]
    formats: Dict[str, Template]

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def labeler(self) -> Dict[str, Any]:
        return self.doc["labeler"]

    @property
    def model(self) -> str:
        return self.labeler["model"]

    @property
    def chars(self) -> int:
        return int(self.labeler["article_body_chars"])

    def max_tokens(self, kind: str) -> int:
        return int(self.labeler["max_output_tokens"][kind])

    def seed(self, run: str) -> int:
        return int(self.labeler["seeds"][run])


def load_protocol(path: Path = PROTOCOL_PATH, *, root: Path = REPO_ROOT) -> Protocol:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    pdir = Path(root) / doc["prompts"]["dir"]
    problems = []
    for name, want in doc["prompts"]["sha256"].items():
        p = pdir / name
        if not p.exists():
            problems.append(f"{name}: 없다")
        elif sha256_file(p) != want:
            problems.append(f"{name}: sha256이 등록값과 다르다")
    if problems:
        raise ProtocolError("프롬프트 틀이 A8.13에 등록한 것과 다르다 - " + "; ".join(problems))
    lab = doc["labeler"]
    if lab["model"] in (lab.get("not_generator"), lab.get("not_judge_v2")):
        raise ProtocolError("라벨러가 generator나 judge v2와 같은 모델이다")
    prompts = {k: (pdir / f"{k}.txt").read_text(encoding="utf-8") for k in KINDS}
    schemas = json.loads((pdir / "schemas.json").read_text(encoding="utf-8"))
    formats = {k: Template(v) for k, v in doc["prompts"]["user_formats"].items()}
    return Protocol(doc=doc, prompts=prompts, schemas=schemas, formats=formats)


# ---------------------------------------------------------------- 패키지 (사람 라벨 내보내기가 만든 입력)


@dataclass
class Package:
    labels_dir: Path
    run_dir: Path
    clusters: Dict[str, Dict[str, Any]]
    outputs: List[Dict[str, Any]]
    relabel: Dict[str, Any]
    retention: Dict[str, Any]
    conversions: Dict[str, Dict[str, Any]]      # output_id -> {"formal": {...}, "converted": {...}}

    @property
    def outputs_by_id(self) -> Dict[str, Dict[str, Any]]:
        return {o["output_id"]: o for o in self.outputs}

    def expires_at(self) -> datetime:
        return datetime.fromisoformat(self.retention["expires_at_utc"].replace("Z", "+00:00"))

    def article_ids(self, item_id: str) -> List[int]:
        return [int(a["raw_news_id"]) for a in self.clusters[item_id]["articles"]]


def main_data_dir() -> Path:
    from evaluation.llm import frozen_export

    return frozen_export.main_checkout_data()


def load_package(labels_dir: Path, run_dir: Path, *, now: Optional[datetime] = None) -> Package:
    """라벨링 패키지와 run 디렉터리의 저장본 변환본을 읽는다. 마감이 지났거나 본문이 없으면 거절한다."""
    from evaluation.llm import labels as label_store

    labels_dir, run_dir = Path(labels_dir), Path(run_dir)
    retention = label_store.read_retention(labels_dir)
    if not retention:
        raise ProtocolError("retention.json이 없다 - 마감을 모르는 패키지에는 라벨을 달지 않는다")
    now = now or _now()
    if label_store.is_expired(labels_dir, now):
        raise ProtocolError("패키지 마감이 지났다(A8.13 (b)7 iv) - 본문을 쓰지 않는다")
    clusters = {r["item_id"]: r for r in _read_jsonl(labels_dir / "clusters.jsonl")}
    outputs = _read_jsonl(labels_dir / "outputs.jsonl")
    if not clusters or not outputs:
        raise ProtocolError("패키지에 clusters.jsonl·outputs.jsonl이 없다")
    if any(not a.get("body") for c in clusters.values() for a in c["articles"]):
        raise ProtocolError("본문이 지워진 기사가 있다")
    relabel = json.loads((labels_dir / "relabel.json").read_text(encoding="utf-8"))
    key_path = run_dir / "blind_key.json"
    key = json.loads(key_path.read_text(encoding="utf-8"))["outputs"] if key_path.exists() else {}
    saved = {t["item_id"]: t for t in _read_jsonl(run_dir / "newsletters.jsonl") if t.get("kind") == "saved_formal"}
    conversions: Dict[str, Dict[str, Any]] = {}
    for o in outputs:
        conv = o.get("converted") or {}
        if any((conv.get(k) or "").strip() for k in ("title", "summary", "content")):
            conversions[o["output_id"]] = {"formal": o["draft"], "converted": conv}
            continue
        k = key.get(o["output_id"])
        s = saved.get(o["item_id"])
        if not k or not s or s.get("formal_sha256") != k.get("sha256") or s.get("tone_fallback") == "formal":
            continue
        sconv = s.get("converted") or {}
        if any((sconv.get(x) or "").strip() for x in ("title", "summary", "content")):
            conversions[o["output_id"]] = {"formal": s.get("formal") or o["draft"], "converted": sconv}
    return Package(labels_dir=labels_dir, run_dir=run_dir, clusters=clusters, outputs=outputs, relabel=relabel,
                   retention=retention, conversions=conversions)


# ---------------------------------------------------------------- 메시지


def _articles_block(proto: Protocol, articles: Sequence[Dict[str, Any]]) -> str:
    return "".join(proto.formats["article"].substitute(
        id=int(a["raw_news_id"]), press=a.get("press_name") or "", title=a.get("title") or "", chars=proto.chars,
        body=(a.get("body") or "")[:proto.chars]) + "\n" for a in articles)


def _draft_block(proto: Protocol, d: Dict[str, Any]) -> str:
    return proto.formats["draft"].substitute(title=d.get("title") or "", sentence=d.get("sentence") or "",
                                             content=d.get("content") or "")


def system_prompt(proto: Protocol, kind: str, schema_mode: str) -> str:
    text = proto.prompts[kind]
    if schema_mode == "json_mode":  # pre-flight가 responseJsonSchema를 거절했을 때만(A8.13 (b)7)
        text = text + "\n응답 JSON 스키마:\n" + json.dumps(proto.schemas[kind], ensure_ascii=False)
    return text


def user_message(proto: Protocol, kind: str, *, articles=None, draft=None, facts=None, converted=None,
                 violations: Optional[Sequence[str]] = None) -> str:
    f = proto.formats
    if kind == "cluster":
        msg = f["cluster_user"].substitute(n=len(articles), articles=_articles_block(proto, articles))
    elif kind == "output":
        msg = f["output_user"].substitute(n=len(articles), articles=_articles_block(proto, articles),
                                          draft=_draft_block(proto, draft))
    elif kind == "coverage":
        lines = "".join(f["fact"].substitute(index=i, text=fact["text"]) for i, fact in enumerate(facts))
        msg = f["coverage_user"].substitute(facts=lines, draft=_draft_block(proto, draft))
    elif kind == "tone":
        conv = f["converted"].substitute(title=converted.get("title") or "", summary=converted.get("summary") or "",
                                         content=converted.get("content") or "")
        msg = f["tone_user"].substitute(draft=_draft_block(proto, draft), converted=conv)
    else:
        raise ValueError(kind)
    if violations:
        msg += f["repair_suffix"].substitute(violations="\n".join(f"- {v}" for v in violations))
    return msg


# ---------------------------------------------------------------- 응답 검증 (가이드의 저장 거부 규칙과 같은 것)


def schema_violations(kind: str, obj: Any) -> List[str]:
    """응답이 schemas.json의 모양인가. 위반 코드만 돌려준다(모델 응답을 담지 않는다)."""
    if not isinstance(obj, dict):
        return ["schema:not_object"]
    v: List[str] = []

    def need(key, typ):
        if key not in obj:
            v.append(f"schema:missing:{key}")
            return False
        if typ is int and (isinstance(obj[key], bool) or not isinstance(obj[key], int)):
            v.append(f"schema:type:{key}")
            return False
        if typ is not int and not isinstance(obj[key], typ):
            v.append(f"schema:type:{key}")
            return False
        return True

    if kind == "cluster":
        need("analysis", str)
        need("single_event", bool)
        if need("outlier_ids", list) and any(isinstance(i, bool) or not isinstance(i, int) for i in obj["outlier_ids"]):
            v.append("schema:type:outlier_ids")
        if need("key_facts", list):
            if len(obj["key_facts"]) > 6:
                v.append("schema:key_facts_over_6")
            for fct in obj["key_facts"]:
                if not isinstance(fct, dict) or not isinstance(fct.get("text"), str) or not fct["text"].strip() \
                        or not isinstance(fct.get("source_ids"), list) or not fct["source_ids"] \
                        or any(isinstance(i, bool) or not isinstance(i, int) for i in fct["source_ids"]):
                    v.append("schema:key_fact_shape")
                    break
    elif kind == "output":
        need("analysis", str)
        if need("fact_errors", list):
            for e in obj["fact_errors"]:
                if not isinstance(e, dict) or e.get("field") not in FIELDS or e.get("type") not in ERROR_TYPES \
                        or not isinstance(e.get("text"), str) or not e["text"].strip() or not isinstance(e.get("reason"), str):
                    v.append("schema:fact_error_shape")
                    break
        if need("style", int) and not 1 <= obj["style"] <= 5:
            v.append("schema:style_range")
        need("headline_mismatch", bool)
        need("publishable", bool)
    elif kind == "coverage":
        if need("facts", list):
            for x in obj["facts"]:
                if not isinstance(x, dict) or isinstance(x.get("index"), bool) or not isinstance(x.get("index"), int) \
                        or not isinstance(x.get("covered"), bool):
                    v.append("schema:fact_shape")
                    break
    elif kind == "tone":
        need("analysis", str)
        need("tone_drift", bool)
    return v


def cluster_violations(obj: Dict[str, Any], article_ids: Sequence[int]) -> List[str]:
    """labels.LabelStore._validate가 클러스터 라벨 저장을 거부하는 사유와 같다."""
    ids = set(int(i) for i in article_ids)
    v = []
    if any(i not in ids for i in obj["outlier_ids"]) or any(i not in ids for f in obj["key_facts"] for i in f["source_ids"]):
        v.append("rule:id_not_in_cluster")
    if obj["single_event"] and obj["outlier_ids"]:
        v.append("rule:single_event_with_outliers")
    n = len(obj["key_facts"])
    if obj["single_event"] and not 3 <= n <= 6:
        v.append("rule:single_event_needs_3_to_6_key_facts")
    if n and not 3 <= n <= 6:
        v.append("rule:key_facts_zero_or_3_to_6")
    outliers = set(obj["outlier_ids"])
    if any(set(f["source_ids"]) <= outliers for f in obj["key_facts"]):
        v.append("rule:key_fact_only_from_outliers")
    return v


def coverage_violations(obj: Dict[str, Any], n_facts: int) -> List[str]:
    got = [x["index"] for x in obj["facts"]]
    v = []
    if any(not 0 <= i < n_facts for i in got):
        v.append("rule:fact_index_out_of_range")
    if len(set(got)) != len(got):
        v.append("rule:fact_index_repeated")
    if set(range(n_facts)) - set(got):
        v.append("rule:fact_index_missing")
    return v


def locate_span(field_text: str, text: str) -> Tuple[Optional[int], Optional[int], int]:
    """(start, end, 등장 횟수). 그대로 못 찾으면 공백을 하나로 모아 다시 찾는다. 못 찾으면 (None, None, 0)."""
    if text and text in field_text:
        return field_text.index(text), field_text.index(text) + len(text), field_text.count(text)
    words = text.split()
    if not words:
        return None, None, 0
    pattern = r"\s+".join(re.escape(w) for w in words)
    hits = list(re.finditer(pattern, field_text))
    if not hits:
        return None, None, 0
    return hits[0].start(), hits[0].end(), len(hits)


def compose_publishable(raw: Dict[str, Any], *, n_errors: int, n_facts: Optional[int],
                        covered: Optional[int]) -> Tuple[bool, Dict[str, Optional[bool]]]:
    """가이드 2.4의 규칙 1~4. 규칙상 Y여도 라벨러가 N이면 N(가이드 2.4 끝 문단).

    규칙 2(핵심 사실의 절반 이상이 빠지면 N)는 핵심 사실이 없는 클러스터면 건너뛴다(n_facts == 0 -> True).
    커버리지를 알 수 없으면(클러스터 라벨이나 coverage 라벨이 없다: n_facts 또는 covered가 None) None으로 두고
    나머지 규칙만으로 정한다 - 그런 행은 `auto.coverage_basis`로 드러나고 리포트가 따로 센다.
    """
    if n_facts == 0:
        coverage_ok: Optional[bool] = True
    elif n_facts is None or covered is None:
        coverage_ok = None
    else:
        coverage_ok = 2 * (n_facts - covered) < n_facts
    rules = {
        "labeler_publishable": bool(raw["publishable"]),
        "no_fact_errors": n_errors == 0,
        "style_ge_3": int(raw["style"]) >= 3,
        "no_headline_mismatch": not raw["headline_mismatch"],
        "coverage_ok": coverage_ok,
    }
    return all(v for v in rules.values() if v is not None), rules


# ---------------------------------------------------------------- 유출 방지


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


_HANGUL = re.compile(r"[가-힣]")


class LeakGuard:
    """기사·초안·라벨러 텍스트의 16자 조각(공백 제거, 한글 4자 이상 든 조각만)과 겹치는 텍스트를 저장소에 쓰지 않는다."""

    WINDOW = 16
    MIN_HANGUL = 4

    def __init__(self) -> None:
        self._grams: set = set()

    def _windows(self, text: str) -> Iterable[str]:
        t = _norm(text)
        w = self.WINDOW
        for i in range(0, max(0, len(t) - w + 1)):
            g = t[i:i + w]
            if len(_HANGUL.findall(g)) >= self.MIN_HANGUL:
                yield g

    def add(self, text: Optional[str]) -> None:
        if text:
            self._grams.update(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest() for g in self._windows(text))

    def add_package(self, pkg: Package) -> "LeakGuard":
        for c in pkg.clusters.values():
            for a in c["articles"]:
                self.add(a.get("title"))
                self.add(a.get("body"))
        for o in pkg.outputs:
            for k in FIELDS:
                self.add((o.get("draft") or {}).get(k))
            for k in ("title", "summary", "content"):
                self.add((o.get("converted") or {}).get(k))
        for conv in pkg.conversions.values():
            for k in FIELDS:
                self.add((conv.get("formal") or {}).get(k))
            for k in ("title", "summary", "content"):
                self.add((conv.get("converted") or {}).get(k))
        return self

    def add_responses(self, rows: Iterable[Dict[str, Any]]) -> "LeakGuard":
        def walk(x):
            if isinstance(x, str):
                self.add(x)
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        for r in rows:
            walk(r.get("response"))
        return self

    def hits(self, text: str) -> int:
        return sum(1 for g in self._windows(text)
                   if hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest() in self._grams)

    def write_text(self, path: Path, text: str) -> None:
        n = self.hits(text)
        if n:
            raise LeakRefused(f"{path}: 기사·초안·라벨러 텍스트와 겹치는 16자 조각 {n}개 - 쓰지 않는다")
        Path(path).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 전송 (네이티브 generateContent + ADR 0035 가드)


def httpx_post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Tuple[int, Optional[Dict[str, Any]]]:
    import httpx

    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as e:
        raise TransportTimeout(type(e).__name__) from None
    except httpx.TransportError as e:
        raise TransportConnection(type(e).__name__) from None
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data


@dataclass
class CallResult:
    status: str                          # ok / schema_invalid / length / content_filter
    parsed: Optional[Dict[str, Any]]
    finish_reason: Optional[str]
    model_version: Optional[str]
    usage: Dict[str, Optional[int]]
    http_attempts: int
    latency_s: float
    cost_usd: float


def _usage(meta: Dict[str, Any]) -> Dict[str, Optional[int]]:
    def g(k):
        v = meta.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"prompt": g("promptTokenCount"), "candidates": g("candidatesTokenCount"), "thoughts": g("thoughtsTokenCount"),
            "cached": g("cachedContentTokenCount"), "total": g("totalTokenCount")}


def _to_llm_usage(u: Dict[str, Optional[int]]):
    from core.llm.client import LLMUsage

    return LLMUsage(input_tokens=u["prompt"] or 0, output_tokens=u["candidates"] or 0, thinking_tokens=u["thoughts"],
                    cached_tokens=u["cached"], total_tokens=u["total"])


class GeminiTransport:
    """시도마다 `core.llm.budget` 가드에 예약하고 결과로 정산한다. 재시도는 429·5xx·타임아웃·연결 오류만."""

    def __init__(self, proto: Protocol, *, api_key: str, post: Callable = httpx_post, sleep: Callable = time.sleep,
                 on_attempt: Optional[Callable[[Dict[str, Any]], None]] = None, schema_mode: Optional[str] = None,
                 thinking_spelling: Optional[str] = None):
        if not api_key:
            raise InfraStop("no_api_key")
        self.proto = proto
        self.api_key = api_key
        self.post = post
        self.sleep = sleep
        self.on_attempt = on_attempt or (lambda row: None)
        self.schema_mode = schema_mode or proto.labeler["schema_mode"]
        self.thinking_spelling = thinking_spelling or proto.labeler.get("thinking_level_spelling", "lower")
        retry = proto.labeler["transport_retries"]
        self.max_attempts = int(retry["max_attempts"])
        self.backoff = [float(x) for x in retry["backoff_s"]]
        self.timeout = float(proto.labeler["request_timeout_s"])
        self._price = None

    def price(self) -> Tuple[float, float]:
        if self._price is None:
            from core.llm.pricing import load_pricing

            entry = load_pricing().price(self.proto.model, on=_now().date())
            self._price = (float(entry.input_per_1m), float(entry.output_per_1m))
        return self._price

    def body(self, kind: str, system: str, user: str, seed: int) -> Dict[str, Any]:
        lab = self.proto.labeler
        level = lab["thinking_level"]
        gen: Dict[str, Any] = {
            "temperature": float(lab["temperature"]), "seed": int(seed), "maxOutputTokens": self.proto.max_tokens(kind),
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": level.upper() if self.thinking_spelling == "upper" else level.lower()},
        }
        if self.schema_mode == "json_schema":
            gen["responseJsonSchema"] = self.proto.schemas[kind]
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}

    def generate(self, kind: str, system: str, user: str, seed: int, *, meta: Dict[str, Any]) -> CallResult:
        from core.llm import budget

        guard = budget.get_budget_guard()
        url = self.proto.labeler["endpoint"].format(model=self.proto.model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        body = self.body(kind, system, user, seed)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        schema = self.proto.schemas[kind] if self.schema_mode == "json_schema" else None
        purpose = f"auto_label_{kind}"
        started = time.time()
        for attempt in range(1, self.max_attempts + 1):
            spend = guard.begin_attempt(provider="gemini", model=self.proto.model, purpose=purpose, messages=messages,
                                        schema=schema, max_tokens=self.proto.max_tokens(kind))
            t0 = time.time()
            row = {**meta, "kind": kind, "attempt": attempt, "seed": seed, "started_at": _now().isoformat()}
            try:
                try:
                    status, data = self.post(url, headers, body, self.timeout)
                except TransportTimeout:
                    spend.settle("timeout")
                    self.on_attempt({**row, "status": None, "transport_error": "timeout", "latency_s": round(time.time() - t0, 3)})
                    if attempt < self.max_attempts:
                        self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
                        continue
                    raise InfraStop("transport_exhausted")
                except TransportConnection:
                    spend.settle("connection")
                    self.on_attempt({**row, "status": None, "transport_error": "connection", "latency_s": round(time.time() - t0, 3)})
                    if attempt < self.max_attempts:
                        self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
                        continue
                    raise InfraStop("transport_exhausted")
                if status != 200:
                    spend.settle(f"http_{status}")
                    self.on_attempt({**row, "status": status, "transport_error": None, "latency_s": round(time.time() - t0, 3),
                                     "error_status": ((data or {}).get("error") or {}).get("status")})
                    if (status == 429 or status >= 500) and attempt < self.max_attempts:
                        self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
                        continue
                    raise InfraStop(f"http_{status}", status)
                result = self._parse(kind, data or {})
                spend.settle(result["status"], _to_llm_usage(result["usage"]))
                p_in, p_out = self.price()
                u = result["usage"]
                billable_out = max((u["candidates"] or 0) + (u["thoughts"] or 0), (u["total"] or 0) - (u["prompt"] or 0))
                cost = ((u["prompt"] or 0) * p_in + billable_out * p_out) / 1e6
                latency = time.time() - t0
                self.on_attempt({**row, "status": 200, "transport_error": None, "latency_s": round(latency, 3),
                                 "outcome": result["status"], "finish_reason": result["finish_reason"],
                                 "model_version": result["model_version"], "usage": u, "cost_usd": round(cost, 8)})
                return CallResult(status=result["status"], parsed=result["parsed"], finish_reason=result["finish_reason"],
                                  model_version=result["model_version"], usage=u, http_attempts=attempt,
                                  latency_s=round(time.time() - started, 3), cost_usd=cost)
            finally:
                spend.ensure_settled()
        raise InfraStop("transport_exhausted")

    @staticmethod
    def _parse(kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
        usage = _usage(data.get("usageMetadata") or {})
        version = data.get("modelVersion")
        cands = data.get("candidates") or []
        if not cands:
            return {"status": "content_filter", "parsed": None, "finish_reason": "PROMPT_BLOCKED", "model_version": version,
                    "usage": usage}
        cand = cands[0]
        finish = cand.get("finishReason")
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text") or "" for p in parts if not p.get("thought"))
        if finish == "MAX_TOKENS":
            return {"status": "length", "parsed": None, "finish_reason": finish, "model_version": version, "usage": usage}
        if finish in FILTER_FINISH:
            return {"status": "content_filter", "parsed": None, "finish_reason": finish, "model_version": version, "usage": usage}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"status": "schema_invalid", "parsed": None, "finish_reason": finish, "model_version": version, "usage": usage}
        if schema_violations(kind, parsed):
            return {"status": "schema_invalid", "parsed": parsed, "finish_reason": finish, "model_version": version,
                    "usage": usage}
        return {"status": "ok", "parsed": parsed, "finish_reason": finish, "model_version": version, "usage": usage}


# ---------------------------------------------------------------- 저장소 (패키지 아래 auto/<프로토콜 id>/)


class Store:
    def __init__(self, pkg: Package, proto: Protocol, *, check_destination: bool = True):
        self.root = pkg.labels_dir / "auto" / proto.id
        if check_destination:
            from evaluation.llm import frozen_export

            frozen_export.ensure_under(self.root, pkg.labels_dir, what="자동 라벨", strictly=True)
        old = os.umask(0o077)
        try:
            for d in (self.root, self.root / "run-a", self.root / "run-b"):
                d.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old)

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise LeakRefused(f"{p}: 자동 라벨 디렉터리 밖에는 쓰지 않는다")
        return p

    def append(self, rel: str, row: Dict[str, Any]) -> None:
        p = self.path(rel)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def write_json(self, rel: str, doc: Dict[str, Any]) -> None:
        p = self.path(rel)
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False, indent=1))
        os.replace(tmp, p)

    def read(self, rel: str) -> List[Dict[str, Any]]:
        return _read_jsonl(self.root / rel)

    def manifest(self) -> Dict[str, Any]:
        p = self.root / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def purge_expired(pkg_labels_dir: Path, proto_id: str, now: Optional[datetime] = None) -> bool:
    """패키지 마감이 지났으면 라벨러의 자유 텍스트(rationales.jsonl)를 지운다. 지웠으면 True."""
    from evaluation.llm import labels as label_store

    p = Path(pkg_labels_dir) / "auto" / proto_id / "rationales.jsonl"
    if p.exists() and label_store.is_expired(Path(pkg_labels_dir), now or _now()):
        p.unlink()
        return True
    return False


# ---------------------------------------------------------------- 라벨러


@dataclass
class Unit:
    run: str
    kind: str
    target: str      # cluster면 item_id, 나머지는 output_id

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.run, self.kind, self.target)


@dataclass
class Labeler:
    proto: Protocol
    pkg: Package
    store: Store
    transport: GeminiTransport
    now: Callable[[], datetime] = _now
    log: Callable[[str], None] = lambda s: None
    resent: int = 0
    stats: Dict[str, int] = field(default_factory=dict)

    # -- 기록 --------------------------------------------------------------

    def done_units(self) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        return {(r["run"], r["kind"], r["target"]): r for r in self.store.read("units.jsonl") if r["event"] == "end"}

    def started_units(self) -> set:
        return {(r["run"], r["kind"], r["target"]) for r in self.store.read("units.jsonl") if r["event"] == "start"}

    def responses(self) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        out = {}
        for r in self.store.read("rationales.jsonl"):
            out[(r["run"], r["kind"], r["target"])] = r
        return out

    def cluster_labels(self, run: str) -> Dict[str, Dict[str, Any]]:
        return {r["target_id"]: r for r in self.store.read(f"run-{run}/cluster_labels.jsonl")}

    def output_labels(self, run: str) -> Dict[str, Dict[str, Any]]:
        return {r["target_id"]: r for r in self.store.read(f"run-{run}/output_labels.jsonl")}

    # -- 호출 하나 ----------------------------------------------------------

    def _messages(self, u: Unit, violations=None) -> Optional[Tuple[str, str]]:
        mode = self.transport.schema_mode
        if u.kind == "cluster":
            arts = self.pkg.clusters[u.target]["articles"]
            return system_prompt(self.proto, "cluster", mode), user_message(self.proto, "cluster", articles=arts, violations=violations)
        out = self.pkg.outputs_by_id[u.target]
        if u.kind == "output":
            arts = self.pkg.clusters[out["item_id"]]["articles"]
            return system_prompt(self.proto, "output", mode), user_message(self.proto, "output", articles=arts, draft=out["draft"],
                                                                           violations=violations)
        if u.kind == "coverage":
            facts = self.coverage_facts(u)
            if not facts:
                return None
            return system_prompt(self.proto, "coverage", mode), user_message(self.proto, "coverage", facts=facts, draft=out["draft"],
                                                                             violations=violations)
        conv = self.pkg.conversions.get(u.target)
        if not conv:
            return None
        return system_prompt(self.proto, "tone", mode), user_message(self.proto, "tone", draft=conv["formal"],
                                                                     converted=conv["converted"], violations=violations)

    def coverage_basis_run(self, u: Unit) -> str:
        return self.proto.doc["runs"]["b"].get("coverage_key_facts_from", "a") if u.run == "b" else u.run

    def coverage_facts(self, u: Unit) -> List[Dict[str, Any]]:
        item = self.pkg.outputs_by_id[u.target]["item_id"]
        row = self.cluster_labels(self.coverage_basis_run(u)).get(item)
        return (row or {}).get("label", {}).get("key_facts") or []

    def _rule_violations(self, u: Unit, parsed: Dict[str, Any]) -> List[str]:
        if u.kind == "cluster":
            return cluster_violations(parsed, self.pkg.article_ids(u.target))
        if u.kind == "coverage":
            return coverage_violations(parsed, len(self.coverage_facts(u)))
        return []

    def run_unit(self, u: Unit) -> Dict[str, Any]:
        if u.kind in ("coverage", "tone"):
            out_unit = self.done_units().get((u.run, "output", u.target))
            if out_unit is not None and out_unit["status"] != "labeled":
                # 출력 라벨이 없으면 그 출력의 coverage·tone은 쓸 데가 없다 - 부르지 않는다
                end = {"event": "end", "run": u.run, "kind": u.kind, "target": u.target, "status": "skipped_output_unlabeled",
                       "at": self.now().isoformat()}
                self.store.append("units.jsonl", end)
                return end
        msgs = self._messages(u)
        if msgs is None:
            end = {"event": "end", "run": u.run, "kind": u.kind, "target": u.target, "status": "not_applicable",
                   "at": self.now().isoformat()}
            self.store.append("units.jsonl", end)
            return end
        if u.key in self.started_units():
            self.resent += 1
        self.store.append("units.jsonl", {"event": "start", "run": u.run, "kind": u.kind, "target": u.target,
                                          "at": self.now().isoformat()})
        seed = self.proto.seed(u.run)
        attempts = 1 + int(self.proto.labeler["rule_repair_attempts"])
        violations: List[str] = []
        status = "invalid"
        parsed = None
        versions = []
        for i in range(attempts):
            system, user = msgs if i == 0 else self._messages(u, violations)
            res = self.transport.generate(u.kind, system, user, seed, meta={"run": u.run, "target": u.target, "response_no": i + 1})
            versions.append(res.model_version)
            if res.status in ("length", "content_filter"):
                status, violations = res.status, [f"finish:{res.finish_reason}"]
                break
            parsed = res.parsed
            violations = schema_violations(u.kind, parsed) if parsed is not None else ["schema:not_json"]
            if not violations:
                violations = self._rule_violations(u, parsed)
            if not violations:
                status = "labeled"
                break
        end = {"event": "end", "run": u.run, "kind": u.kind, "target": u.target, "status": status,
               "responses": i + 1, "violations": violations, "model_versions": versions, "at": self.now().isoformat()}
        if status == "labeled":
            self.store.append("rationales.jsonl", {"run": u.run, "kind": u.kind, "target": u.target, "response": parsed})
            if u.kind == "cluster":
                self._write_cluster_label(u, parsed, versions)
        self.store.append("units.jsonl", end)
        self.stats[status] = self.stats.get(status, 0) + 1
        return end

    # -- 라벨 조립 ----------------------------------------------------------

    def _write_cluster_label(self, u: Unit, parsed: Dict[str, Any], versions) -> None:
        from evaluation.llm.labels import ClusterLabel

        label = ClusterLabel(single_event=parsed["single_event"], outlier_ids=parsed["outlier_ids"],
                             key_facts=[{"text": f["text"], "source_ids": f["source_ids"]} for f in parsed["key_facts"]],
                             notes="").model_dump()
        self.store.append(f"run-{u.run}/cluster_labels.jsonl", {
            "target_id": u.target, "round": 1, "labeled_at": self.now().isoformat(), "label": label,
            "auto": {"protocol": self.proto.id, "run": u.run, "model": self.proto.model, "model_versions": versions,
                     "seed": self.proto.seed(u.run)}})

    def assemble_output(self, run: str, oid: str) -> Optional[Dict[str, Any]]:
        """output·coverage·tone 단위가 다 끝났으면 사람 도구 형식의 출력 라벨 행을 쓴다."""
        from evaluation.llm.labels import FactError, OutputLabel

        done = self.done_units()
        units = {k: done.get((run, k, oid)) for k in ("output", "coverage", "tone")}
        if any(v is None for v in units.values()) or oid in self.output_labels(run):
            return None
        resp = self.responses()
        if units["output"]["status"] != "labeled":
            return None
        raw = resp[(run, "output", oid)]["response"]
        draft = self.pkg.outputs_by_id[oid]["draft"]
        located, unlocated, ambiguous = [], [], 0
        for e in raw["fact_errors"]:
            start, end, count = locate_span(draft.get(e["field"]) or "", e["text"])
            if start is None:
                unlocated.append({"field": e["field"], "type": e["type"], "text": e["text"]})
                continue
            ambiguous += int(count > 1)
            text = (draft.get(e["field"]) or "")[start:end]
            located.append(FactError(field=e["field"], start=start, end=end, text=text, type=e["type"], note="").model_dump())
        basis = self.coverage_basis_run(Unit(run, "coverage", oid))
        facts = self.coverage_facts(Unit(run, "coverage", oid))
        if units["coverage"]["status"] == "labeled":
            cov = resp[(run, "coverage", oid)]["response"]
            covered = sorted({x["index"] for x in cov["facts"] if x["covered"]})
            coverage_basis = basis
        elif units["coverage"]["status"] == "not_applicable":
            covered, coverage_basis = [], ("none" if self.cluster_labels(basis).get(self.pkg.outputs_by_id[oid]["item_id"]) else "missing")
        else:
            covered, coverage_basis = [], "invalid"
        tone_applicable = units["tone"]["status"] == "labeled"
        tone = bool(resp[(run, "tone", oid)]["response"]["tone_drift"]) if tone_applicable else False
        if coverage_basis == basis:
            n_facts, n_cov = len(facts), len(covered)
        elif coverage_basis == "none":
            n_facts, n_cov = 0, 0
        else:  # 클러스터 라벨이 없거나 coverage 라벨이 무효 - 규칙 2를 알 수 없다
            n_facts, n_cov = None, None
        publishable, rules = compose_publishable(raw, n_errors=len(located) + len(unlocated), n_facts=n_facts, covered=n_cov)
        label = OutputLabel(fact_errors=located, key_facts_covered=covered, style=int(raw["style"]), publishable=publishable,
                            tone_drift=tone, notes="").model_dump()
        row = {"target_id": oid, "round": 1, "labeled_at": self.now().isoformat(), "label": label,
               "auto": {"protocol": self.proto.id, "run": run, "model": self.proto.model, "seed": self.proto.seed(run),
                        "model_versions": sorted({v for x in units.values() for v in (x.get("model_versions") or []) if v}),
                        "publishable_raw": bool(raw["publishable"]), "headline_mismatch": bool(raw["headline_mismatch"]),
                        "rules": rules, "unlocated_fact_errors": unlocated, "ambiguous_spans": ambiguous,
                        "coverage_basis": coverage_basis, "coverage_n_facts": len(facts),
                        "coverage_status": units["coverage"]["status"], "tone_drift_applicable": tone_applicable,
                        "tone_status": units["tone"]["status"]}}
        self.store.append(f"run-{run}/output_labels.jsonl", row)
        return row

    # -- 계획 ---------------------------------------------------------------

    def plan_a(self) -> List[List[Unit]]:
        items = sorted(self.pkg.clusters)
        groups = [[Unit("a", "cluster", i)] for i in items]
        groups += [[Unit("a", k, o["output_id"]) for k in ("output", "coverage", "tone")] for o in self.pkg.outputs]
        return groups

    def plan_b(self) -> Tuple[List[List[Unit]], List[List[Unit]]]:
        """(재라벨 대상 묶음, 확장 묶음). 확장은 item_id 순으로 클러스터 -> 그 출력."""
        rel_c = list(self.pkg.relabel.get("clusters") or [])
        rel_o = list(self.pkg.relabel.get("outputs") or [])
        first = [[Unit("b", "cluster", i)] for i in rel_c]
        first += [[Unit("b", k, o) for k in ("output", "coverage", "tone")] for o in rel_o]
        by_item: Dict[str, List[str]] = {}
        for o in self.pkg.outputs:
            by_item.setdefault(o["item_id"], []).append(o["output_id"])
        rest = []
        for item in sorted(self.pkg.clusters):
            g = [] if item in rel_c else [Unit("b", "cluster", item)]
            for oid in by_item.get(item, []):
                if oid not in rel_o:
                    g += [Unit("b", k, oid) for k in ("output", "coverage", "tone")]
            if g:
                rest.append(g)
        return first, rest

    def worst_case_usd(self, group: Sequence[Unit]) -> float:
        """묶음의 호출마다(단위당 한 번) 가드와 같은 식(바이트 기준 입력 + max_tokens)으로 잡은 최악 예약액의 합.

        A8.13 (b)5의 확장 정지 규칙 그대로다. 다시 묻기(규칙 위반 때만 생긴다)는 넣지 않는다 - 처음 구현은 넣어서
        등록 규칙보다 일찍 멈췄다(A8.14).
        """
        from core.llm import budget

        cfg = budget.BudgetConfig.from_env()
        p_in, p_out = self.transport.price()
        total = 0.0
        for u in group:
            msgs = self._messages(u) if u.kind != "coverage" else self._coverage_messages_for_estimate(u)
            if msgs is None:
                continue
            system, user = msgs
            est = budget.estimate_prompt_tokens([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                                self.proto.schemas[u.kind] if self.transport.schema_mode == "json_schema" else None,
                                                bytes_per_token=cfg.bytes_per_token)
            one = (est * p_in + self.proto.max_tokens(u.kind) * p_out) / 1e6
            total += one
        return total

    def _coverage_messages_for_estimate(self, u: Unit) -> Optional[Tuple[str, str]]:
        facts = self.coverage_facts(u)
        if not facts:
            return None
        out = self.pkg.outputs_by_id[u.target]
        return system_prompt(self.proto, "coverage", self.transport.schema_mode), \
            user_message(self.proto, "coverage", facts=facts, draft=out["draft"])

    def _run_group(self, group: Sequence[Unit]) -> None:
        done = self.done_units()
        for u in group:
            if u.key in done:
                continue
            self.run_unit(u)
            done = self.done_units()
        for u in group:
            if u.kind == "output":
                self.assemble_output(u.run, u.target)

    def run(self, *, spent_usd: Callable[[], float]) -> Dict[str, Any]:
        for g in self.plan_a():
            self._run_group(g)
            self.log(f"run a: {len(self.cluster_labels('a'))} clusters, {len(self.output_labels('a'))} outputs, ${spent_usd():.4f}")
        first, rest = self.plan_b()
        for g in first:
            self._run_group(g)
        self.log(f"run b relabel targets: {len(self.cluster_labels('b'))} clusters, {len(self.output_labels('b'))} outputs, ${spent_usd():.4f}")
        soft = float(self.proto.doc["runs"]["b"]["extension_soft_cap_usd"])
        stopped_at = None
        done = self.done_units()
        for g in rest:
            if all(u.key in done for u in g):
                continue
            pending = [u for u in g if u.key not in done]
            if spent_usd() + self.worst_case_usd(pending) > soft:
                stopped_at = g[0].target
                break
            self._run_group(g)
            done = self.done_units()
            self.log(f"run b extension: {len(self.cluster_labels('b'))} clusters, {len(self.output_labels('b'))} outputs, ${spent_usd():.4f}")
        return {"stats": dict(self.stats), "resent_after_crash": self.resent, "extension_stopped_before": stopped_at,
                "extension_groups_total": len(rest)}


# ---------------------------------------------------------------- pre-flight (지어낸 문장, 기사 없음)


SYNTHETIC_ARTICLES = [
    {"raw_news_id": 1, "press_name": "가상일보", "title": "가상시 도서관 개관 시간 연장",
     "body": "가상시는 내달 1일부터 시립 도서관 세 곳의 개관 시간을 밤 10시까지 늘린다고 밝혔다. 예산은 2억 원이다."},
    {"raw_news_id": 2, "press_name": "예시신문", "title": "가상시 도서관 야간 운영 시작",
     "body": "가상시 시립 도서관 세 곳이 다음 달부터 밤 10시까지 문을 연다. 시는 이용자 설문 결과를 반영했다고 설명했다."},
    {"raw_news_id": 3, "press_name": "샘플방송", "title": "가상시 도서관 이용 시간 늘어",
     "body": "가상시가 도서관 야간 개관을 추진한다. 대상은 시립 도서관 세 곳이고 운영 시간은 밤 10시까지다."},
]
SYNTHETIC_DRAFT = {"title": "가상시 도서관 밤 10시까지", "sentence": "가상시 시립 도서관 세 곳이 다음 달부터 야간에도 문을 엽니다.",
                   "content": "가상시가 다음 달 1일부터 시립 도서관 세 곳의 개관 시간을 밤 10시까지 늘린다. 예산은 3억 원이다."}
SYNTHETIC_CONVERTED = {"title": "도서관, 이제 밤 10시까지!", "summary": "가상시 도서관 세 곳이 야간 개관해요.",
                       "content": "가상시가 다음 달 1일부터 시립 도서관 세 곳을 밤 10시까지 연대요."}
SYNTHETIC_FACTS = [{"text": "가상시 시립 도서관 세 곳이 밤 10시까지 연다", "source_ids": [1, 2, 3]},
                   {"text": "다음 달 1일부터 시행한다", "source_ids": [1]},
                   {"text": "예산은 2억 원이다", "source_ids": [1]}]


def preflight(proto: Protocol, transport: GeminiTransport) -> Dict[str, Any]:
    """호출 종류마다 1건. 400이면 등록한 두 가지(스키마 방식, thinkingLevel 철자)만 바꿔 다시 한다(A8.13 (b)7)."""
    calls = {
        "cluster": dict(articles=SYNTHETIC_ARTICLES),
        "output": dict(articles=SYNTHETIC_ARTICLES, draft=SYNTHETIC_DRAFT),
        "coverage": dict(facts=SYNTHETIC_FACTS, draft=SYNTHETIC_DRAFT),
        "tone": dict(draft=SYNTHETIC_DRAFT, converted=SYNTHETIC_CONVERTED),
    }
    changes: List[str] = []
    results = {}
    for kind, kw in calls.items():
        for _ in range(3):
            system = system_prompt(proto, kind, transport.schema_mode)
            user = user_message(proto, kind, **kw)
            try:
                res = transport.generate(kind, system, user, proto.seed("a"), meta={"run": "preflight", "target": kind,
                                                                                   "response_no": 1})
            except InfraStop as e:
                if e.status == 400 and transport.schema_mode == "json_schema" and "schema_mode" not in changes:
                    transport.schema_mode = "json_mode"
                    changes.append("schema_mode")
                    continue
                if e.status == 400 and transport.thinking_spelling != "upper" and "thinking_spelling" not in changes:
                    transport.thinking_spelling = "upper"
                    changes.append("thinking_spelling")
                    continue
                raise
            results[kind] = {"status": res.status, "finish_reason": res.finish_reason, "model_version": res.model_version,
                             "usage": res.usage, "latency_s": res.latency_s,
                             "schema_ok": res.status == "ok"}
            break
    return {"results": results, "schema_mode": transport.schema_mode, "thinking_spelling": transport.thinking_spelling,
            "changes": changes}


# ---------------------------------------------------------------- 지출 가드 환경


def configure_budget(proto: Protocol) -> Dict[str, str]:
    env = {k: str(v) for k, v in proto.doc["budget"]["env"].items()}
    os.environ.update(env)
    from core.llm import budget

    budget.begin_run(env["LLM_RUN_ID"])
    return env


def budget_problems(proto: Protocol) -> List[str]:
    from core.llm import budget

    issue = budget.preflight_problem()
    if issue:
        return [f"지출 가드(ADR 0035)를 쓸 수 없다: {issue}"]
    problems = []
    missing = budget.unpriced_models([proto.model])
    if missing:
        problems.append(f"단가표에 없는 모델: {sorted(missing)}")
    snap = budget.spend_snapshot()
    if "error" in snap:
        return problems + [f"지출 원장 조회 실패: {snap['error']}"]
    need = proto.doc["budget"]["min_headroom_usd"]
    for scope in ("day", "total"):
        headroom = snap["caps_usd"][scope] - snap[f"{scope}_usd"]
        if headroom < float(need[scope]):
            problems.append(f"{scope} 상한 여유 ${headroom:.4f} < ${float(need[scope]):.2f}")
    if snap["caps_usd"]["run"] > float(proto.doc["budget"]["cap_usd"]) + 1e-9:
        problems.append("런 상한이 등록값보다 크다")
    return problems


def spent_usd() -> float:
    from core.llm import budget

    snap = budget.spend_snapshot()
    if "error" in snap:
        raise ProtocolError(f"지출 원장 조회 실패: {snap['error']}")
    return float(snap["run_usd"])


def api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env = REPO_ROOT / "ai_workspace" / ".env"
    if not env.exists():
        env = main_data_dir().parent / "ai_workspace" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ---------------------------------------------------------------- CLI


def _paths(proto: Protocol) -> Tuple[Path, Path]:
    data = main_data_dir()
    return data / proto.doc["package"]["labels_dir"], data / proto.doc["package"]["run_dir"]


def _check_package(proto: Protocol, pkg: Package) -> None:
    want = proto.doc["package"]
    if (len(pkg.clusters), len(pkg.outputs)) != (int(want["clusters"]), int(want["outputs"])):
        raise ProtocolError("패키지의 클러스터·출력 수가 등록값과 다르다")
    if pkg.retention["expires_at_utc"] != want["expires_at_utc"]:
        raise ProtocolError("패키지 마감이 등록값과 다르다")


def _quiet():
    import logging

    logging.disable(logging.CRITICAL)  # 어댑터·가드 로그에는 응답 조각이 섞일 수 있다 - 화면에는 건수·비용만


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E0 자동 라벨러 (ADR 0009 A8.13)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run", "status"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    _quiet()
    proto = load_protocol()
    labels_dir, run_dir = _paths(proto)
    purge_expired(labels_dir, proto.id)
    pkg = load_package(labels_dir, run_dir)
    _check_package(proto, pkg)
    store = Store(pkg, proto)
    if args.command == "status":
        lab = Labeler(proto, pkg, store, transport=None)  # type: ignore[arg-type]
        counts = {r: {"clusters": len(lab.cluster_labels(r)), "outputs": len(lab.output_labels(r))} for r in ("a", "b")}
        units: Dict[str, int] = {}
        for r in lab.done_units().values():
            units[f"{r['run']}:{r['kind']}:{r['status']}"] = units.get(f"{r['run']}:{r['kind']}:{r['status']}", 0) + 1
        configure_budget(proto)
        print(json.dumps({"labels": counts, "units": dict(sorted(units.items())), "spent_run_usd": spent_usd(),
                          "manifest": {k: v for k, v in store.manifest().items() if k != "preflight"}}, ensure_ascii=False, indent=1))
        return 0
    env = configure_budget(proto)
    problems = budget_problems(proto)
    if problems:
        print("시작하지 않는다: " + "; ".join(problems))
        return 2
    manifest = store.manifest()

    def on_attempt(row):
        store.append("calls.jsonl", row)

    transport = GeminiTransport(proto, api_key=api_key(), on_attempt=on_attempt,
                                schema_mode=manifest.get("schema_mode"), thinking_spelling=manifest.get("thinking_spelling"))
    from core.llm.budget import LLMBudgetRefusal, LLMRunStop

    try:
        if args.command == "preflight" or not manifest.get("preflight"):
            pf = preflight(proto, transport)
            manifest.update({"protocol": proto.id, "model": proto.model, "budget_env": env, "preflight": pf,
                             "schema_mode": pf["schema_mode"], "thinking_spelling": pf["thinking_spelling"],
                             "preflight_at": _now().isoformat()})
            store.write_json("manifest.json", manifest)
            print(json.dumps({"preflight": {k: v["status"] for k, v in pf["results"].items()}, "changes": pf["changes"],
                              "spent_run_usd": spent_usd()}, ensure_ascii=False))
            if any(v["status"] != "ok" for v in pf["results"].values()):
                print("pre-flight 실패 - 라벨을 달지 않는다")
                return 3
            if args.command == "preflight":
                return 0
        lab = Labeler(proto, pkg, store, transport, log=print)
        out = lab.run(spent_usd=spent_usd)
        stop = None
    except LLMBudgetRefusal as e:
        out, stop = {}, {"kind": "budget", "code": e.code}
    except LLMRunStop as e:
        out, stop = {}, {"kind": "infra", "code": e.code}
    except InfraStop as e:
        out, stop = {}, {"kind": "infra", "code": e.code}
    manifest = store.manifest()
    runs = manifest.setdefault("runs", [])
    runs.append({"at": _now().isoformat(), "result": out, "stop": stop, "spent_run_usd": spent_usd()})
    store.write_json("manifest.json", manifest)
    print(json.dumps({"stop": stop, "result": out, "spent_run_usd": spent_usd()}, ensure_ascii=False))
    return 0 if stop is None else 4


if __name__ == "__main__":
    raise SystemExit(main())
