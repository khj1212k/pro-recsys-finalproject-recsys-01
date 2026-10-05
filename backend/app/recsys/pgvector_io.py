"""pgvector 값을 드라이버 파싱 없이 읽는 도우미 (ADR 0008/0015)."""
from typing import Optional

import numpy as np


def vector_from_send(buf) -> Optional[np.ndarray]:
    """pgvector vector_send() 바이너리: uint16 dim, uint16 unused, float32[dim] (big-endian)."""
    if buf is None:
        return None
    return np.frombuffer(bytes(buf), dtype=">f4", offset=4).astype(np.float32)
