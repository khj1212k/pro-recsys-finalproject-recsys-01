"""요청 시점 추천(ADR 0015, 탐색·로그는 ADR 0025)의 설정값. 모든 값은 환경변수로 덮어쓸 수 있다."""
import os
from dataclasses import dataclass, fields
from typing import Mapping, Optional

from recsys_core import SERVING_CANDIDATE_SPEC, CandidateSpec

MODES = ("realtime", "batch")
FATIGUE_MODES = ("off", "log", "enforce")

# 후보 생성기의 출처·순서·기본값은 recsys_core.SERVING_CANDIDATE_SPEC 한곳에 있다(ADR 0033). 아래는 출처마다
# 그 k를 덮어쓰는 설정 필드다(두 KNN 출처는 RECSYS_KNN_K 하나를 같이 쓴다).
CANDIDATE_SOURCE_FIELDS = {
    "knn_profile": "knn_k",
    "knn_short": "knn_k",
    "recent": "recent_n",
    "popular": "popular_n",
    "category": "category_n",
}
_DEFAULT_K = dict(SERVING_CANDIDATE_SPEC.sources)
assert set(_DEFAULT_K) == set(CANDIDATE_SOURCE_FIELDS), "후보 출처 목록이 설정 필드와 다릅니다"
assert _DEFAULT_K["knn_profile"] == _DEFAULT_K["knn_short"], "두 KNN 출처는 같은 k를 쓴다"


@dataclass(frozen=True)
class RecsysConfig:
    # realtime: 요청마다 후보 생성->스코어링->MMR. batch: 기존처럼 배치 결과를 읽되
    # 비어 있으면 인기/최신으로 채운다(폴백 체인만 사용).
    mode: str = "realtime"
    time_budget_ms: int = 300
    top_k: int = 20

    freshness_hours: int = int(SERVING_CANDIDATE_SPEC.window_h)
    knn_k: int = _DEFAULT_K["knn_profile"]
    recent_n: int = _DEFAULT_K["recent"]
    popular_n: int = _DEFAULT_K["popular"]
    category_n: int = _DEFAULT_K["category"]
    candidate_cap: int = SERVING_CANDIDATE_SPEC.cap

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

    def candidate_spec(self) -> CandidateSpec:
        """지금 설정으로 도는 후보 생성기 구성. 출처와 라운드로빈 순서는 SERVING_CANDIDATE_SPEC의 것이고
        값만 환경변수로 덮어쓴다. parity 게이트가 하네스의 서빙 구성과 비교하는 대상이다."""
        return CandidateSpec(
            window_h=float(self.freshness_hours),
            sources=tuple(
                (name, int(getattr(self, CANDIDATE_SOURCE_FIELDS[name])))
                for name, _ in SERVING_CANDIDATE_SPEC.sources
            ),
            cap=int(self.candidate_cap),
        )

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
