"""BGE-M3 입력 길이 상한(max_length) 후보별 절단 비율과 절단 영향을 잰다 (ADR 0006 사전 등록 규칙).

DB(news_raw)에서 본문 있는 기사를 읽어 수집 잡과 같은 텍스트(`f"{title} {content}"[:8000]`)를 만들고,
- 토큰 길이 분포와 후보 L별 절단 비율,
- 잘리는 기사들의 cos(emb_L, emb_8192) (emb_8192는 배치 1 CPU fp32),
- 보조: 잘린 기사의 top-5 이웃 보존율(이웃 풀 = 전체 기사의 무절단 임베딩)
을 집계 JSON으로 출력한다. 기사 텍스트나 임베딩은 출력하지 않는다. DB에 쓰지 않는다.

    DB_HOST=127.0.0.1 DB_PORT=5433 ... python scripts/measure_embedding_truncation.py --out reports/ops/x.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(REPO_ROOT / "ai_workspace"))

CANDIDATES = (1024, 2048, 4096)
REFERENCE = 8192


def load_texts():
    from db.connection import connect_unpooled

    conn = connect_unpooled(application_name="measure_embedding_truncation")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT raw_news_id, raw_news_title, raw_news_content FROM news_raw
                WHERE raw_news_content IS NOT NULL AND raw_news_content <> ''
                ORDER BY raw_news_id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows], [f"{r[1]} {r[2]}"[:8000] for r in rows]


def encode(model, texts, max_length, batch_size):
    out = model.encode(texts, batch_size=batch_size, max_length=max_length,
                       return_dense=True, return_sparse=False, return_colbert_vecs=False)["dense_vecs"]
    out = np.asarray(out, dtype=np.float32)
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def pct(values, q):
    return float(np.percentile(values, q)) if len(values) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from FlagEmbedding import BGEM3FlagModel

    ids, texts = load_texts()
    t0 = time.monotonic()
    cpu_model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False, device="cpu")
    tokenizer = cpu_model.tokenizer
    lengths = np.array([len(tokenizer(t, truncation=False)["input_ids"]) for t in texts])
    order = np.argsort(lengths)

    report = {
        "n_articles": len(texts),
        "text_rule": 'f"{title} {content}"[:8000]',
        "token_length": {f"p{q}": pct(lengths, q) for q in (50, 90, 95, 99)} | {"max": int(lengths.max())},
        "truncation_rate": {str(L): round(float((lengths > L).mean()), 4) for L in CANDIDATES},
        "truncated_count": {str(L): int((lengths > L).sum()) for L in CANDIDATES},
    }
    report["token_length"] = {k: (round(v, 1) if v is not None else None) for k, v in report["token_length"].items()}

    # 무절단 기준 임베딩: 짧은 기사(≤ 최소 후보)는 어느 L에서도 같은 입력이라 MPS 배치로,
    # 긴 기사는 사전 등록대로 CPU 배치 1로 계산한다.
    short = [i for i in order if lengths[i] <= CANDIDATES[0]]
    long_ = [i for i in order if lengths[i] > CANDIDATES[0]]
    ref = np.zeros((len(texts), 1024), dtype=np.float32)
    started = time.monotonic()
    for i in long_:
        ref[i] = encode(cpu_model, [texts[i]], REFERENCE, 1)[0]
    report["reference_long_cpu_s"] = round(time.monotonic() - started, 1)

    # 모델 두 벌(각 2.2GB fp32)을 동시에 올리지 않는다.
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    if device != "cpu":
        del cpu_model
        fast_model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False, device=device)
    else:
        fast_model = cpu_model
    for s in range(0, len(short), 8):
        chunk = short[s:s + 8]
        ref[chunk] = encode(fast_model, [texts[i] for i in chunk], REFERENCE, len(chunk))

    sims_all = ref @ ref.T
    np.fill_diagonal(sims_all, -np.inf)

    report["impact"] = {}
    for L in CANDIDATES:
        cut = [i for i in long_ if lengths[i] > L]
        if not cut:
            report["impact"][str(L)] = {"n": 0}
            continue
        emb_L = np.stack([encode(fast_model, [texts[i]], L, 1)[0] for i in cut])
        cos = np.einsum("ij,ij->i", emb_L, ref[cut])
        keep = []
        for row, i in enumerate(cut):
            base = set(np.argsort(-sims_all[i])[:5])
            s = ref @ emb_L[row]
            s[i] = -np.inf
            keep.append(len(base & set(np.argsort(-s)[:5])) / 5)
        report["impact"][str(L)] = {
            "n": len(cut),
            "cos_median": round(float(np.median(cos)), 4),
            "cos_p10": round(pct(cos, 10), 4),
            "cos_min": round(float(cos.min()), 4),
            "top5_neighbor_overlap_mean": round(float(np.mean(keep)), 3),
        }

    rule = []
    for L in CANDIDATES:
        imp = report["impact"][str(L)]
        ok_a = report["truncation_rate"][str(L)] <= 0.02
        ok_b = imp["n"] == 0 or imp["cos_median"] >= 0.95
        rule.append({"L": L, "a_trunc_le_2pct": ok_a, "b_cos_median_ge_0.95": ok_b})
    chosen = next((r["L"] for r in rule if r["a_trunc_le_2pct"] and r["b_cos_median_ge_0.95"]), None)
    report["rule"] = rule
    report["decision"] = chosen if chosen is not None else f"{REFERENCE} (+ >4096 토큰은 배치 1)"
    report["reference_device_short"] = device
    report["wall_s"] = round(time.monotonic() - t0, 1)
    report["versions"] = {"torch": torch.__version__}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
