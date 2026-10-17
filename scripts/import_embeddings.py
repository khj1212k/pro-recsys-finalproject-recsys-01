#!/usr/bin/env python3
"""원격 임베딩 잡이 돌려준 팩을 동결 반출본과 대조한다. 구현과 형식은 evaluation/llm/embedding_pack.py에 있다.

    python scripts/import_embeddings.py --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings

통과하면 행 수·차원·dtype·벡터 파일 sha256을 출력한다(사전 등록 기록에 옮겨 적는 값). 어긋나면 사유 코드와
함께 종료 코드 2로 끝난다.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evaluation.llm.embedding_pack import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
