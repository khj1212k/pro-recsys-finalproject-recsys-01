"""요청 시점 추천(ADR 0015, 탐색·로그는 ADR 0025)의 설정값. 모든 값은 환경변수로 덮어쓸 수 있다."""
import os
from dataclasses import dataclass, fields
from typing import Mapping, Optional

MODES = ("realtime", "batch")
FATIGUE_MODES = ("off", "log", "enforce")


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

    # --- 탐색 슬롯 (ADR 0025). 화면 top_k칸 중 explore_slots칸을 무작위 위치에 무작위 후보로 채운다.
    # 개인 신호가 전혀 없는 사용자(profile_source = none)는 explore_slots_cold칸. explore_enabled=false나
    # 칸 수 0이면 결정론 목록을 그대로 낸다. propensity가 맞으려면 top_k가 표시 단계의 한도(20) 이하여야
    # 한다 - 계획한 화면이 잘려 나가면 그 요청의 propensity는 기록하지 않는다.
    explore_enabled: bool = True
    explore_slots: int = 2
    explore_slots_cold: int = 4

    # --- 노출 피로 규칙 (ADR 0025). 최근 fatigue_hours 동안 fatigue_min_impressions번 이상 노출되고
    # 클릭되지 않은 아이템. off: 조회하지 않음, log: 걸린 수만 요청 로그에 남김, enforce: 후보에서 뺌.
    fatigue_mode: str = "log"
    fatigue_hours: int = 48
    fatigue_min_impressions: int = 3

    # --- shadow 스코어러 (ADR 0025). 레지스트리의 role='shadow' 모델 중 최신 shadow_max개가 같은 후보에
    # 점수만 매긴다. 활성 점수를 낸 뒤 남은 예산 비율이 shadow_deadline_fraction 미만이면 건너뛴다.
    shadow_max: int = 2
    shadow_deadline_fraction: float = 0.5

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"RECSYS_MODE must be one of {MODES}, got {self.mode!r}")
        if self.time_budget_ms <= 0:
            raise ValueError("RECSYS_TIME_BUDGET_MS must be positive")
        if self.fatigue_mode not in FATIGUE_MODES:
            raise ValueError(
                f"RECSYS_FATIGUE_MODE must be one of {FATIGUE_MODES}, got {self.fatigue_mode!r}"
            )
        if self.explore_slots < 0 or self.explore_slots_cold < 0:
            raise ValueError("RECSYS_EXPLORE_SLOTS and RECSYS_EXPLORE_SLOTS_COLD must not be negative")
        if self.shadow_max < 0:
            raise ValueError("RECSYS_SHADOW_MAX must not be negative")

    def explore_slots_for(self, cold: bool) -> int:
        if not self.explore_enabled:
            return 0
        return self.explore_slots_cold if cold else self.explore_slots

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
