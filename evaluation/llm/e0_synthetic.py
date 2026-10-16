"""E0 드라이 런 재료 - 합성 반출본·합성 임베딩 팩·페이크 LLM 전송 계층 (실제 기사·실제 호출 없음).

`python -m evaluation.llm.e0_baseline --dry-run`이 쓴다. 러너의 사슬 전체(표본 → 실행 전 기록 확인 → pre-flight → warmup → eval
→ 리포트 → 블라인드 내보내기)를 운영 코드 그대로 돌리되, 입력은 여기서 지어낸 기사이고 LLM 응답은 HTTP 전송 계층에서 만든다.
그래서 운영 어댑터(OpenAICompatLLMClient)·openai SDK·httpx 훅·지출 가드(ADR 0035)·E0 가드가 모두 실제로 돈다.

합성 기사의 낱말은 지어낸 지명·기관명이다(가람시, 누리구 등). 실제 기사와 무관하다.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from evaluation.llm import embedding_pack, frozen_export

PRESSES = ("가람일보", "누리신문", "한빛경제", "새솔뉴스")
_PLACES = ("가람시", "누리구", "한빛군", "새솔시", "다온구", "미르시", "아라군", "도담시", "라온구", "온새미시",
           "바다시", "하늘군", "솔빛구", "들녘시")
_THINGS = ("도서관", "버스 노선", "공원", "체육관", "문화센터", "주차장", "보건소", "학교 급식", "하천 정비", "도로 확장",
           "청년 주택", "전통시장", "자전거 길", "어린이집")
TOPIC_SIZES = (3, 3, 4, 4, 5, 6, 7, 8, 10, 11, 12, 13)   # 하루 창마다의 주제별 기사 수(버킷마다 4개씩)
NOISE_PER_DAY = 8


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _topic_facts(day: int, topic: int) -> Tuple[str, str, List[str]]:
    place = _PLACES[(day * 5 + topic) % len(_PLACES)]
    thing = _THINGS[(day * 3 + topic) % len(_THINGS)]
    budget = 10 + (day * 13 + topic * 7) % 90
    when = 1 + (day + topic) % 27
    quote = f"\"{thing} 사업을 차례로 진행하겠다\""
    facts = [
        f"{place}는 {when}일 {thing} 계획을 발표했다.",
        f"사업 예산은 {budget}억 원이다.",
        f"{place} 관계자는 {quote}고 말했다.",
        f"공사는 내년 {1 + topic % 12}월에 시작한다.",
    ]
    return place, thing, facts


def synthetic_rows(t0: datetime, *, days: int = 5, seed: int = 20261017) -> Tuple[List[Dict[str, Any]], np.ndarray, Dict]:
    """반출 COPY 행 모양의 합성 기사와 같은 순서의 단위 벡터, 그리고 E0 날짜 창 계획.

    날짜 창마다 TOPIC_SIZES 크기의 주제 12개(주제 안 벡터는 서로 가깝다)와 흩어진 기사 NOISE_PER_DAY개를 둔다.
    """
    union, plan = frozen_export.plan_windows(t0, days)
    windows = frozen_export.registered_day_windows(t0, days=days)
    rng = np.random.default_rng(seed)
    rows: List[Dict[str, Any]] = []
    vectors: List[np.ndarray] = []
    next_id = 7_000_001
    for d, window in enumerate(windows):
        span = (window.end - window.start).total_seconds()
        slots: List[Tuple[int, int]] = []
        for topic, size in enumerate(TOPIC_SIZES):
            slots += [(topic, k) for k in range(size)]
        slots += [(-1, k) for k in range(NOISE_PER_DAY)]
        order = rng.permutation(len(slots))
        centers = rng.normal(size=(len(TOPIC_SIZES), 1024))
        for position, pick in enumerate(order):
            topic, k = slots[pick]
            crawled = window.start + timedelta(seconds=span * (position + 0.5) / len(slots))
            if topic >= 0:
                place, thing, facts = _topic_facts(d, topic)
                title = f"{place} {thing} 소식 {k + 1}"
                filler = f"이 기사는 시험용으로 지어낸 {k + 1}번째 글이다. "
                body = " ".join(facts[: 2 + k % 3]) + " " + filler + " ".join(facts) + " " + filler * (2 + k % 4)
                vec = centers[topic] + rng.normal(scale=0.02, size=1024)
            else:
                title = f"흩어진 소식 {d}-{k}"
                body = f"서로 관련이 없는 짧은 소식이다. 일련번호 {d}-{k}. " * 6
                vec = rng.normal(size=1024)
            rows.append({
                "kind": "row", "id": next_id, "press": PRESSES[position % len(PRESSES)],
                "url": f"https://news.example/synthetic/{next_id}", "title": title, "body": body.strip(),
                "created_at": None, "crawled_at": crawled.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                "extracted_at": (crawled + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                "extract_status": "ok", "content_sha256": _sha(body.strip()),
            })
            vectors.append(vec / np.linalg.norm(vec))
            next_id += 1
    return rows, np.asarray(vectors, dtype=np.float32), {"union": union, "plan": plan}


def _copy_line(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False).replace("\\", "\\\\").encode("utf-8")


def write_synthetic_export(root: Path, t0: datetime, *, days: int = 5) -> Tuple[Path, Path]:
    """`<root>/<T0>/`에 합성 반출본과 `embeddings/` 팩을 쓴다. root는 (임시) 메인 체크아웃의 data/exports여야 한다."""
    rows, vectors, planned = synthetic_rows(t0, days=days)
    snapshot = t0.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    header = {"kind": "header", "snapshot_at_utc": snapshot, "alembic_revision": "synthetic",
              "server_version": "synthetic", "server_encoding": "UTF8", "transaction_read_only": "on",
              "transaction_isolation": "repeatable read", "statement_timeout": "2min", "txid_snapshot": "0:0:"}
    ids = [r["id"] for r in rows]
    trailer = {"kind": "trailer", "rows": len(rows), "linked_rows": 0, "min_id": min(ids), "max_id": max(ids)}
    lines = [_copy_line(header)] + [_copy_line(r) for r in rows] + [_copy_line(trailer)]
    bundle = frozen_export.build_export(frozen_export.parse_copy_stream(lines), planned["union"],
                                        code={"git_sha": "synthetic", "dirty": False},
                                        is_redistributable=lambda press: False, plan=planned["plan"])
    stamp = datetime.strptime(snapshot, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y%m%dT%H%M%SZ")
    dest = frozen_export.write_export(Path(root) / stamp, bundle)
    export = frozen_export.load_export(dest, now=t0)
    order = np.argsort(ids)
    embedding_pack.write_embedding_pack(
        dest / "embeddings", ids=export.ids, vectors=vectors[order],
        content_sha256=[r["content_sha256"] for r in export.rows], title_sha256=[r["title_sha256"] for r in export.rows],
        export_identity_sha256=export.identity_sha256, model=dict(embedding_pack.EXPECTED_MODEL), dtype="float16",
        job={"synthetic": True, "note": "드라이 런용 합성 벡터 - 모델 출력이 아니다",
             "fp16_vs_fp32": {"stored_f16_vs_fp32": {"n": 100, "p10": 1.0, "min": 1.0}}})
    return dest, dest / "embeddings"


# ---------------------------------------------------------------- 페이크 LLM 전송 계층


_BODY_BLOCK = re.compile(r"본문(?:\(발췌\))?:\n(.*?)(?:\n---|\n\n\[기사|\Z)", re.DOTALL)
_TONE_FIELDS = re.compile(r"제목: (.*?)\n요약: (.*?)\n본문: (.*?)\n키워드:", re.DOTALL)


def _first_sentences(text: str, k: int = 4) -> List[str]:
    out: List[str] = []
    for block in _BODY_BLOCK.findall(text):
        sentence = block.strip().split(". ")[0].strip()
        if sentence and not sentence.endswith("."):
            sentence += "."
        if sentence and sentence not in out:
            out.append(sentence)
        if len(out) >= k:
            break
    return out


class FaultPlan:
    """프롬프트 sha256으로 정하는 결정론적 장애. 스레드 순서와 무관하게 같은 요청은 같은 운명이다.

    kinds: 프롬프트 해시 % modulus == residue인 요청의 **첫 시도**에 낼 장애 {"429", "500", "schema", "timeout", "length"},
    always: 모든 요청에 낼 상태 코드(예: 402, 401) - 장애 주입 테스트용.
    """

    def __init__(self, kinds: Optional[Dict[str, Tuple[int, int]]] = None, always: Optional[int] = None,
                 after_requests: int = 0):
        self.kinds = kinds or {}
        self.always = always
        self.after_requests = after_requests
        self._seen: Dict[str, int] = {}
        self._count = 0
        self._lock = threading.Lock()

    def decide(self, prompt_sha: str) -> Optional[str]:
        with self._lock:
            self._count += 1
            n = self._seen.get(prompt_sha, 0) + 1
            self._seen[prompt_sha] = n
            count = self._count
        if self.always is not None and count > self.after_requests:
            return str(self.always)
        if n > 1:
            return None
        value = int(prompt_sha[:8], 16)
        for kind, (modulus, residue) in sorted(self.kinds.items()):
            if value % modulus == residue:
                return kind
        return None


DRY_RUN_FAULTS = {"429": (11, 3), "schema": (13, 5), "500": (17, 7)}


class FakeLLMTransport:
    """OpenAI 호환 chat.completions 응답을 만드는 httpx2 전송 계층 처리기(스키마 이름을 보고 답한다)."""

    def __init__(self, faults: Optional[FaultPlan] = None, *, cluster_eval: str = "PASS"):
        self.faults = faults or FaultPlan()
        self.cluster_eval = cluster_eval
        self.requests = 0
        self._lock = threading.Lock()

    def _content(self, schema_name: str, prompt: str) -> Dict[str, Any]:
        if schema_name == "ClusterEval":
            return {"decision": self.cluster_eval, "confidence": 0.9, "summary": "합성 묶음", "feedback": "",
                    "outlier_indices": [], "sub_groups": []}
        if schema_name == "NewsletterContent":
            sentences = _first_sentences(prompt) or ["합성 기사들의 공통 내용을 정리했다."]
            paragraphs = [sentences[i % len(sentences)] for i in range(4)]
            return {"content": "\n\n".join(paragraphs)}
        if schema_name == "NewsletterMeta":
            body = prompt.split("작성된 본문(content):", 1)[-1].split("━", 1)[0]
            words = [w for w in re.findall(r"[가-힣]{2,}", body) if w not in ("다음", "본문", "제목")]
            title = " ".join(words[:2])[:15] or "합성 소식"
            return {"title": title, "sentence": f"{title} 소식을 정리했다", "keywords": list(dict.fromkeys(words))[:5],
                    "categories": ["사회"]}
        if schema_name == "NewsletterEvalV2":
            return {"scores": {"faithfulness": 4, "coverage": 4, "coherence": 4, "style": 4}, "unsupported_claims": [],
                    "feedback": ""}
        if schema_name == "ToneResult":
            match = _TONE_FIELDS.search(prompt)
            title, summary, content = (match.group(1), match.group(2), match.group(3)) if match else ("", "", "")
            return {"title": title.strip(), "summary": summary.strip(), "content": content.strip(), "keywords": []}
        return {}

    def __call__(self, request):
        import httpx2

        with self._lock:
            self.requests += 1
        body = json.loads(request.content or b"{}")
        messages = body.get("messages") or []
        prompt = "\n".join(m.get("content") or "" for m in messages if isinstance(m, dict))
        prompt_sha = _sha(json.dumps(messages, ensure_ascii=False, sort_keys=True))
        fault = self.faults.decide(prompt_sha)
        if fault == "timeout":
            raise httpx2.ReadTimeout("synthetic timeout", request=request)
        if fault and fault.isdigit():
            return httpx2.Response(int(fault), json={"error": {"code": int(fault), "message": "synthetic"}})
        schema_name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name", "")
        content = self._content(schema_name, prompt)
        text = json.dumps(content, ensure_ascii=False)
        finish = "stop"
        if fault == "schema":
            text = "{not json"
        if fault == "length":
            finish = "length"
        prompt_tokens = max(1, int(len(prompt) * 0.9))
        completion = max(1, int(len(text) * 0.8))
        return httpx2.Response(200, json={
            "id": f"syn-{prompt_sha[:12]}", "object": "chat.completion", "created": 0, "model": body.get("model"),
            "system_fingerprint": "synthetic",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                      "total_tokens": prompt_tokens + completion + 40,
                      "completion_tokens_details": {"reasoning_tokens": 40}},
        })


def install_fake_clients(transport: Callable, roles: Sequence[Tuple[str, str]]) -> List[Any]:
    """레지스트리에 페이크 전송 계층을 쓰는 운영 어댑터를 넣는다(실제 키·네트워크 없음). 돌려준 클라이언트를 계측이 감싼다."""
    import httpx2
    import openai

    from config.settings import Settings
    from core.llm import registry
    from core.llm.adapters import OpenAICompatLLMClient

    out = []
    for provider, model in roles:
        cfg = registry.PROVIDER_CONFIG[provider]
        sdk = openai.OpenAI(api_key="synthetic-dry-run", base_url=cfg["base_url"], timeout=Settings.LLM_REQUEST_TIMEOUT_S,
                            max_retries=0, http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(transport)))
        client = OpenAICompatLLMClient(provider=provider, model=model, supports_json_schema=cfg["supports_json_schema"],
                                       rate_limiter=registry.get_provider_rate_limiter(provider), client=sdk)
        with registry._instances_lock:
            registry._instances[(provider, model)] = client
        out.append(client)
    return out
