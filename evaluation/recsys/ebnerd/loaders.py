"""EB-NeRD parquet 로더 (메타데이터/행동 로그만, 기사 본문은 읽지 않는다).

라이선스: EB-NeRD는 연구/비상업 전용이고 자체 IT 환경 밖으로 복사하면 안 된다.
데이터는 저장소 밖(gitignore된 data/)에 두고 EBNERD_ROOT 환경변수나 인자로 경로를 준다.

시각은 전부 epoch 초(int64)로 바꾼다. EB-NeRD 타임스탬프는 타임존 없는 로컬 시각이라
그대로 UTC처럼 취급한다(상대 비교만 하므로 결과에 영향 없음).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
ARTICLE_META_COLUMNS = ["article_id", "published_time", "category", "article_type", "premium"]


def ebnerd_root() -> Path:
    env = os.getenv("EBNERD_ROOT")
    return Path(env) if env else REPO_ROOT / "data" / "benchmarks" / "ebnerd"


def to_epoch_seconds(values) -> np.ndarray:
    arr = np.asarray(values, dtype="datetime64[s]")
    return arr.astype(np.int64)


@dataclass
class Impressions:
    """한 split의 노출 로그를 CSR 배열로 편 것. 인덱스 i는 노출 하나."""
    impression_id: np.ndarray
    user_id: np.ndarray
    session_id: np.ndarray
    time: np.ndarray
    age: np.ndarray
    gender: np.ndarray
    inview_ptr: np.ndarray
    inview_article: np.ndarray
    inview_clicked: np.ndarray
    clicked_ptr: np.ndarray
    clicked_article: np.ndarray

    def __len__(self) -> int:
        return len(self.impression_id)

    def subset(self, idx: np.ndarray) -> "Impressions":
        idx = np.asarray(idx, dtype=np.int64)
        iv_ptr, iv_pos = _subset_csr(self.inview_ptr, idx)
        ck_ptr, ck_pos = _subset_csr(self.clicked_ptr, idx)
        return Impressions(
            impression_id=self.impression_id[idx], user_id=self.user_id[idx],
            session_id=self.session_id[idx], time=self.time[idx], age=self.age[idx],
            gender=self.gender[idx], inview_ptr=iv_ptr, inview_article=self.inview_article[iv_pos],
            inview_clicked=self.inview_clicked[iv_pos], clicked_ptr=ck_ptr,
            clicked_article=self.clicked_article[ck_pos],
        )


def _subset_csr(ptr: np.ndarray, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = ptr[idx], ptr[idx + 1]
    counts = hi - lo
    new_ptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
    starts = new_ptr[:-1]
    pos = np.arange(new_ptr[-1], dtype=np.int64) - np.repeat(starts, counts) + np.repeat(lo, counts)
    return new_ptr, pos


def _flatten(lists: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    lengths = lists.map(len).to_numpy(dtype=np.int64)
    ptr = np.concatenate([[0], np.cumsum(lengths)]).astype(np.int64)
    flat = np.concatenate([np.asarray(x, dtype=np.int64) for x in lists]) if len(lists) else np.zeros(0, np.int64)
    return ptr, flat


def load_articles(dataset_dir: Path, columns: Optional[list[str]] = None) -> pd.DataFrame:
    cols = columns or ARTICLE_META_COLUMNS
    df = pd.read_parquet(Path(dataset_dir) / "articles.parquet", columns=cols)
    if "published_time" in df:
        df["published_ts"] = to_epoch_seconds(df["published_time"])
    return df


def load_impressions(dataset_dir: Path, split: str) -> Impressions:
    cols = ["impression_id", "user_id", "session_id", "impression_time", "article_ids_inview",
            "article_ids_clicked", "age", "gender"]
    b = pd.read_parquet(Path(dataset_dir) / split / "behaviors.parquet", columns=cols)
    b = b.sort_values(["impression_time", "impression_id"], kind="mergesort").reset_index(drop=True)
    iv_ptr, iv = _flatten(b["article_ids_inview"])
    ck_ptr, ck = _flatten(b["article_ids_clicked"])
    # 노출 목록 안에서 클릭 여부 표시: (노출 번호, 아이템) 결합키 membership
    iv_row = np.repeat(np.arange(len(b), dtype=np.int64), np.diff(iv_ptr))
    ck_row = np.repeat(np.arange(len(b), dtype=np.int64), np.diff(ck_ptr))
    shift = np.int64(1) << np.int64(32)
    clicked = np.isin(iv_row * shift + iv, ck_row * shift + ck)
    return Impressions(
        impression_id=b["impression_id"].to_numpy(np.int64),
        user_id=b["user_id"].to_numpy(np.int64),
        session_id=b["session_id"].to_numpy(np.int64),
        time=to_epoch_seconds(b["impression_time"]),
        age=b["age"].to_numpy(np.float64),
        gender=b["gender"].to_numpy(np.float64),
        inview_ptr=iv_ptr, inview_article=iv, inview_clicked=clicked,
        clicked_ptr=ck_ptr, clicked_article=ck,
    )


def load_history_events(dataset_dir: Path, split: str) -> pd.DataFrame:
    """history.parquet(행동 창 이전 21일 읽기 기록)을 (user_id, time, article_id) 이벤트로 편다."""
    h = pd.read_parquet(Path(dataset_dir) / split / "history.parquet",
                        columns=["user_id", "impression_time_fixed", "article_id_fixed"])
    lengths = h["article_id_fixed"].map(len).to_numpy(dtype=np.int64)
    users = np.repeat(h["user_id"].to_numpy(np.int64), lengths)
    times = np.concatenate([np.asarray(x, dtype="datetime64[s]") for x in h["impression_time_fixed"]])
    arts = np.concatenate([np.asarray(x, dtype=np.int64) for x in h["article_id_fixed"]])
    return pd.DataFrame({"user_id": users, "time": times.astype(np.int64), "article_id": arts})


def click_events(imp: Impressions) -> pd.DataFrame:
    """노출 로그의 클릭을 (user_id, session_id, time, article_id) 이벤트로. 클릭 시각 = 노출 시각."""
    rows = np.repeat(np.arange(len(imp), dtype=np.int64), np.diff(imp.clicked_ptr))
    return pd.DataFrame({
        "user_id": imp.user_id[rows], "session_id": imp.session_id[rows],
        "time": imp.time[rows], "article_id": imp.clicked_article,
    })


def inview_events(imp: Impressions) -> pd.DataFrame:
    rows = np.repeat(np.arange(len(imp), dtype=np.int64), np.diff(imp.inview_ptr))
    return pd.DataFrame({"time": imp.time[rows], "article_id": imp.inview_article})
