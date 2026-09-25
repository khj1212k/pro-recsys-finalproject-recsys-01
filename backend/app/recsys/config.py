"""요청 시점 추천(ADR 0015)의 설정값. 모든 값은 환경변수로 덮어쓸 수 있다."""
import os
from dataclasses import dataclass, fields
from typing import Mapping, Optional

MODES = ("realtime", "batch")


@dataclass(frozen=True)
class RecsysConfig:
    # realtime: 요청마다 후보 생성->스코어링->MMR. batch: 기존처럼 배치 결과를 읽되
    # 비어 있으면 인기/최신으로 채운다(폴백 체인만 사용).
    mode: str = "realtime"
    time_budget_ms: int = 300
    top_k: int = 20

    freshness_hours: int = 72
    knn_k: int = 100
    recent_n: int = 100
    popular_n: int = 100
    category_n: int = 50
    candidate_cap: int = 300

    short_term_hours: int = 24
    short_term_max_clicks: int = 20

    batch_max_age_hours: int = 36

    cache_ttl_s: float = 60.0
    cache_max_entries: int = 10_000
    item_cache_ttl_s: float = 3600.0
    item_cache_max_entries: int = 5_000

    workers: int = 8

    model_name: str = "ranker"
    model_reload_s: float = 60.0
    feature_fn: Optional[str] = None

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"RECSYS_MODE must be one of {MODES}, got {self.mode!r}")
        if self.time_budget_ms <= 0:
            raise ValueError("RECSYS_TIME_BUDGET_MS must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "RecsysConfig":
        kwargs = {}
        for f in fields(cls):
            raw = env.get(f"RECSYS_{f.name.upper()}")
            if raw is None or raw == "":
                continue
            default = f.default
            if isinstance(default, bool):
                kwargs[f.name] = raw.lower() in ("1", "true", "yes")
            elif isinstance(default, int):
                kwargs[f.name] = int(raw)
            elif isinstance(default, float):
                kwargs[f.name] = float(raw)
            else:
                kwargs[f.name] = raw
        return cls(**kwargs)
