"""EB-NeRD와 같은 parquet 스키마의 작은 합성 데이터셋을 쓴다 (배선 검증 전용, 수치 해석 금지).

    python -m evaluation.recsys.ebnerd.synthetic --out /tmp/ebnerd_synth --seed 0

용도는 두 가지다. (1) EB-NeRD가 없는 CI에서 콜드 regime 사슬(run_cold) 전 구간을 돌려 보는 테스트,
(2) 실데이터를 올리기 전에 Colab에서 드라이버 경로만 확인하는 드라이 런. 클릭은 "최근 인기 + 선호 카테고리"로
만든 가짜라서 여기서 나온 어떤 지표도 추천 품질의 근거가 아니다. 실데이터의 어떤 값도 쓰지 않는다.

레이아웃(로더가 읽는 열만 채운다):
    <out>/<name>/articles.parquet
    <out>/<name>/{train,validation}/{behaviors,history}.parquet
    <out>/derived/<name>/{article_ids.npy, bge_m3_tsb512.f16.npy, bge_m3_tsb512.meta.json}
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

HOUR = 3600
DAY = 86400
# EB-NeRD small과 같은 모양: 행동 창은 07:00에 시작한다.
TRAIN_START = int(np.datetime64("2023-05-18T07:00:00", "s").astype(np.int64))
EMB_NAME = "bge_m3_tsb512"


@dataclass(frozen=True)
class SyntheticSpec:
    name: str = "ebnerd_synth"
    n_users: int = 120
    n_categories: int = 6
    articles_per_day: int = 40
    train_days: int = 7
    valid_days: int = 3
    history_days: int = 21
    history_events: int = 15
    sessions_per_day: float = 1.2
    max_impressions_per_session: int = 3
    inview: int = 8
    emb_dim: int = 16
    seed: int = 0


def _dt(seconds: np.ndarray) -> np.ndarray:
    return np.asarray(seconds, dtype="int64").astype("datetime64[s]").astype("datetime64[us]")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_synthetic_dataset(out_root: Path, spec: SyntheticSpec = SyntheticSpec()) -> dict:
    """합성 데이터셋과 가짜 임베딩을 쓰고 경로·sha256을 돌려준다. 같은 spec이면 같은 내용이다."""
    rng = np.random.default_rng(spec.seed)
    out_root = Path(out_root)
    ds = out_root / spec.name
    emb_dir = out_root / "derived" / spec.name
    valid_start = TRAIN_START + spec.train_days * DAY
    end = valid_start + spec.valid_days * DAY
    first_pub = TRAIN_START - (spec.history_days + 3) * DAY

    # --- 기사: 고르게 발행, 카테고리와 "품질"(인기 성향) ---
    n_art = int((end - first_pub) / DAY * spec.articles_per_day)
    pub = np.sort(rng.integers(first_pub, end, size=n_art))
    art_id = 9_000_000 + np.arange(n_art, dtype=np.int64)
    category = rng.integers(0, spec.n_categories, size=n_art)
    quality = rng.gamma(2.0, 1.0, size=n_art)
    articles = pd.DataFrame({
        "article_id": art_id.astype(np.int32), "published_time": _dt(pub),
        "category": (100 + category).astype(np.int16), "article_type": "article_default",
        "premium": rng.random(n_art) < 0.1,
    })
    ds.mkdir(parents=True, exist_ok=True)
    articles.to_parquet(ds / "articles.parquet", index=False)

    # --- 가짜 임베딩: 카테고리 중심 + 잡음 (코사인 피처가 상수가 되지 않게) ---
    centroids = rng.standard_normal((spec.n_categories, spec.emb_dim))
    emb = centroids[category] + 0.6 * rng.standard_normal((n_art, spec.emb_dim))
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    emb_dir.mkdir(parents=True, exist_ok=True)
    np.save(emb_dir / "article_ids.npy", art_id)
    np.save(emb_dir / f"{EMB_NAME}.f16.npy", emb.astype(np.float16))
    emb_sha = _sha256(emb_dir / f"{EMB_NAME}.f16.npy")
    (emb_dir / f"{EMB_NAME}.meta.json").write_text(json.dumps({
        "model": "SYNTHETIC (not BGE-M3)", "embeddings_sha256": emb_sha, "max_length_tokens": None,
        "text_template": None, "device": "cpu", "compute_fp16": False, "model_snapshot": None,
    }, indent=2))

    users = 500_000 + np.arange(spec.n_users, dtype=np.int64)
    pref = rng.integers(0, spec.n_categories, size=spec.n_users)

    def pick_articles(t: int, u: int, n: int) -> np.ndarray:
        """t 이전 36h에 발행된 기사 중 품질·선호 카테고리 가중으로 n개(비복원)."""
        lo, hi = np.searchsorted(pub, t - 36 * HOUR), np.searchsorted(pub, t, side="right")
        cand = np.arange(lo, hi)
        w = quality[cand] * np.where(category[cand] == pref[u], 3.0, 1.0)
        return cand[rng.choice(len(cand), size=min(n, len(cand)), replace=False, p=w / w.sum())]

    next_imp, next_sess = 1, 1
    files = {"articles.parquet": _sha256(ds / "articles.parquet")}
    for split, start, days in (("train", TRAIN_START, spec.train_days), ("validation", valid_start, spec.valid_days)):
        rows = []
        for u in range(spec.n_users):
            for d in range(days):
                for _ in range(rng.poisson(spec.sessions_per_day)):
                    t = start + d * DAY + int(rng.integers(0, DAY - HOUR))
                    sess = next_sess
                    next_sess += 1
                    for _ in range(int(rng.integers(1, spec.max_impressions_per_session + 1))):
                        shown = pick_articles(t, u, spec.inview)
                        w = quality[shown] * np.where(category[shown] == pref[u], 4.0, 1.0)
                        clicked = shown[rng.choice(len(shown), p=w / w.sum())]
                        rows.append((next_imp, users[u], sess, t, art_id[shown].tolist(), [int(art_id[clicked])]))
                        next_imp += 1
                        t += int(rng.integers(30, 600))
        b = pd.DataFrame(rows, columns=["impression_id", "user_id", "session_id", "impression_time",
                                        "article_ids_inview", "article_ids_clicked"])
        b = b[b["impression_time"] < start + days * DAY].reset_index(drop=True)
        b["impression_time"] = _dt(b["impression_time"].to_numpy())
        b["age"] = np.where(rng.random(len(b)) < 0.05, 40.0, np.nan)
        b["gender"] = np.where(rng.random(len(b)) < 0.05, 1.0, np.nan)
        (ds / split).mkdir(parents=True, exist_ok=True)
        b.to_parquet(ds / split / "behaviors.parquet", index=False)

        hist_rows = []
        for u in range(spec.n_users):
            n_ev = int(rng.integers(0, spec.history_events * 2 + 1)) if u % 10 else 0  # 열에 하나는 히스토리 없음
            times = np.sort(rng.integers(start - spec.history_days * DAY, start, size=n_ev))
            arts = [int(art_id[pick_articles(int(t), u, 1)[0]]) for t in times]
            hist_rows.append((users[u], list(_dt(times)), arts))
        h = pd.DataFrame(hist_rows, columns=["user_id", "impression_time_fixed", "article_id_fixed"])
        h.to_parquet(ds / split / "history.parquet", index=False)
        for f in ("behaviors.parquet", "history.parquet"):
            files[f"{split}/{f}"] = _sha256(ds / split / f)
    return {"dataset_dir": str(ds), "emb_dir": str(emb_dir), "name": spec.name, "n_articles": n_art,
            "files": files, "embeddings_sha256": emb_sha,
            "note": "SYNTHETIC wiring data - not EB-NeRD, no metric from it is evidence"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default=SyntheticSpec.name)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--users", type=int, default=SyntheticSpec.n_users)
    args = ap.parse_args(argv)
    info = write_synthetic_dataset(Path(args.out), SyntheticSpec(name=args.name, seed=args.seed, n_users=args.users))
    print(json.dumps({k: info[k] for k in ("dataset_dir", "emb_dir", "n_articles", "note")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
