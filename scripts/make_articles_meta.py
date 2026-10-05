#!/usr/bin/env python3
"""EB-NeRD articles.parquet에서 본문·제목을 뺀 메타 전용 parquet을 만든다(원격 런타임에 올리는 파일).

    python scripts/make_articles_meta.py --dataset-dir data/benchmarks/ebnerd/ebnerd_small --out /tmp/articles.parquet

평가 하네스의 로더는 기사에서 `ARTICLE_META_COLUMNS` 5개 열(id, 발행 시각, 카테고리, 유형, 유료 여부)만 읽는다.
그 5개 열만 남긴 파일로 바꿔도 결과가 같으므로, 기사 텍스트가 든 원본은 로컬 밖으로 내보내지 않는다.
출력은 두 sha256(원본, 파생)과 행 수뿐이다. 기사 내용은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evaluation.recsys.ebnerd.loaders import ARTICLE_META_COLUMNS  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_articles_meta(dataset_dir: Path, out: Path) -> dict:
    src = Path(dataset_dir) / "articles.parquet"
    df = pd.read_parquet(src, columns=ARTICLE_META_COLUMNS)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return {"rows": int(len(df)), "columns": list(df.columns), "original_sha256": _sha256(src),
            "derived_sha256": _sha256(out), "out": str(out)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    print(json.dumps(make_articles_meta(Path(args.dataset_dir), Path(args.out)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
