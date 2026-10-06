"""동결 반출·임베딩 팩·파일 대역 테스트가 함께 쓰는 합성 입력.

pytest는 test_*.py만 수집하므로 이 파일은 테스트로 수집되지 않는다. 여기의 기사 제목·본문은 전부 지어낸
문장이고(실제 기사가 아니다), 임베딩은 난수로 만든 단위 벡터다.
"""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# 본문·제목이 출력이나 로그로 새는지 찾는 표식. 합성 문장에만 들어 있다.
BODY_MARK = "가람시청은"
TITLE_MARK = "가람시"
CODE = {"git_sha": "0" * 40, "dirty": False}
SNAPSHOT_AT = "2026-10-06T07:21:30.123456Z"
EMBEDDING_DIM = 1024


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body(k: int) -> str:
    return (
        f"{BODY_MARK} {k}일 새 도서관을 짓는 계획을 내놓았다. 예산은 {100 + k}억 원이다.\n"
        f"둘째 문단에는 \"따옴표\"와 역슬래시 \\ 와 탭\t이 있고, 줄 구분 문자   와 그림 문자 📚 도 있다."
    )


def server_row(k: int, *, press: str = "가람일보", crawled: str = "2026-10-03T01:00:00.000000Z",
               text: Optional[str] = None, title: Optional[str] = None, db_sha="auto") -> Dict:
    """서버가 행마다 내보내는 json 객체(evaluation/llm/frozen_export.py의 COPY 쿼리와 같은 키)."""
    text = body(k) if text is None else text
    return {
        "kind": "row", "id": 1000 + k, "press": press, "url": f"https://news.example/a/{k}",
        "title": f"{TITLE_MARK} 도서관 계획 {k}" if title is None else title, "body": text,
        "created_at": "2026-10-03T00:30:00.000000Z", "crawled_at": crawled,
        "extracted_at": "2026-10-03T01:05:00.000000Z", "extract_status": "ok",
        "content_sha256": sha(text) if db_sha == "auto" else db_sha,
    }


def header(**over) -> Dict:
    base = {
        "kind": "header", "snapshot_at_utc": SNAPSHOT_AT,
        "alembic_revision": "d48994e9d26e", "server_version": "16.15", "server_encoding": "UTF8",
        "transaction_read_only": "on", "transaction_isolation": "repeatable read",
        "statement_timeout": "2min", "txid_snapshot": "100:100:",
    }
    base.update(over)
    return base


def copy_line(obj: Dict) -> bytes:
    """서버가 COPY ... TO STDOUT(텍스트 형식)으로 내보내는 한 줄. json 텍스트의 역슬래시만 두 번 적힌다."""
    return json.dumps(obj, ensure_ascii=False).replace("\\", "\\\\").encode("utf-8")


def stream(rows: Sequence[Dict], *, head: Optional[Dict] = None, trailer="auto") -> List[bytes]:
    lines = [copy_line(head or header())]
    lines += [copy_line(r) for r in rows]
    if trailer == "auto":
        ids = [r["id"] for r in rows]
        trailer = {"kind": "trailer", "rows": len(rows), "min_id": min(ids) if ids else None,
                   "max_id": max(ids) if ids else None}
    if trailer is not None:
        lines.append(copy_line(trailer))
    return lines


# ---------------------------------------------------------------- 주제가 있는 합성 기사와 임베딩

_TOPICS = (
    ("가람시 도서관", "가람시가 새 도서관 건립 계획을 확정했다. 개관은 내후년 봄으로 잡혔다."),
    ("누리전자 배터리", "누리전자가 차세대 배터리 시제품을 공개했다. 양산 시점은 밝히지 않았다."),
    ("한빛은행 금리", "한빛은행이 예금 금리를 조정한다고 알렸다. 적용일은 다음 달 첫 영업일이다."),
    ("새솔대 연구", "새솔대 연구진이 해조류로 만든 포장재 실험 결과를 발표했다."),
    ("다온항공 노선", "다온항공이 지방 공항을 잇는 새 노선을 열기로 했다."),
    ("미르구단 감독", "미르구단이 새 감독 선임을 발표했다. 계약 기간은 두 해다."),
)
_PRESSES = ("가람일보", "누리신문", "한빛경제")


def topic_articles(*, topics: int = 5, per_topic: int = 8, noise: int = 10, seed: int = 7,
                   start: datetime = datetime(2026, 10, 3, 0, 0), minutes_apart: int = 20,
                   first_id: int = 5001) -> Tuple[List[Dict], np.ndarray]:
    """주제별로 가까운 벡터를 가진 합성 기사 `topics * per_topic + noise`건.

    돌려주는 것: 서버 행 모양의 dict 목록(id 오름차순)과 같은 순서의 단위 벡터(float32, 1,024차원).
    주제 안의 벡터는 중심에서 조금만 벗어나 HDBSCAN이 주제마다 한 무리로 묶는다. 잡음 기사는 흩어져 있다.
    """
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(topics, EMBEDDING_DIM))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    plan = [t for t in range(topics) for _ in range(per_topic)] + [-1] * noise
    order = rng.permutation(len(plan))  # 주제가 id 순으로 몰리지 않게 섞는다
    rows: List[Dict] = []
    vectors = np.zeros((len(plan), EMBEDDING_DIM), dtype=np.float32)
    for position, pick in enumerate(order):
        topic = plan[pick]
        if topic >= 0:
            name, sentence = _TOPICS[topic % len(_TOPICS)]
            vec = centers[topic] + rng.normal(scale=0.01, size=EMBEDDING_DIM)
            title = f"{name} 소식 {position}"
        else:
            name, sentence = "흩어진 소식", "서로 관련이 없는 짧은 소식이다."
            vec = rng.normal(size=EMBEDDING_DIM)
            title = f"{name} {position}"
        vectors[position] = (vec / np.linalg.norm(vec)).astype(np.float32)
        text = f"{sentence} 이 문장은 시험용으로 지어낸 것이다. 일련번호 {position}. " * 6
        crawled = start + timedelta(minutes=minutes_apart * position)
        rows.append({
            "kind": "row", "id": first_id + position, "press": _PRESSES[position % len(_PRESSES)],
            "url": f"https://news.example/t/{first_id + position}", "title": title, "body": text,
            "created_at": None, "crawled_at": crawled.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            "extracted_at": None, "extract_status": "ok", "content_sha256": sha(text),
        })
    return rows, vectors
