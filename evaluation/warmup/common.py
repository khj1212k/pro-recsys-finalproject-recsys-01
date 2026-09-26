"""워밍업 스크립트 공용 헬퍼: 분포 요약, 해시, JSON 기록, 실행 환경 메타데이터."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]


def distribution(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """개수·평균·분위수(p50/p90/p95)·최소·최대. 값이 없으면 개수 0과 None들."""
    arr = np.asarray(list(values), dtype=np.float64)
    if arr.size == 0:
        return {"n": 0, "mean": None, "min": None, "p50": None, "p90": None, "p95": None, "max": None}
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "min": float(arr.min()),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
    }


def sha256_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_ids(ids: Iterable[int]) -> str:
    """정렬한 id 목록의 해시 - 같은 입력 집합인지 리포트끼리 비교할 때 쓴다."""
    joined = ",".join(str(int(i)) for i in sorted(ids))
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


def git_sha() -> Optional[str]:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return None


def environment() -> Dict[str, Any]:
    load = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    return {
        "git_sha": git_sha(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "loadavg_1_5_15": load,
    }


def write_json(path: str | Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
        f.write("\n")
    return path


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"JSON 직렬화 불가: {type(obj)!r}")


def cosine_rows(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """행별 코사인 유사도. 영벡터 행은 0으로 둔다."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape 불일치: {a.shape} vs {b.shape}")
    na = np.linalg.norm(a, axis=1)
    nb = np.linalg.norm(b, axis=1)
    denom = na * nb
    dots = np.einsum("ij,ij->i", a, b)
    out = np.zeros(a.shape[0], dtype=np.float64)
    nz = denom > 0
    out[nz] = dots[nz] / denom[nz]
    return out


def ids_from(rows: List[Sequence[Any]]) -> List[int]:
    return [int(r[0]) for r in rows]
