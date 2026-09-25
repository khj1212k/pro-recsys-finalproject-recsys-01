"""Persona archetypes and seeded synthetic-user population generation.

Archetypes are hand-written category mixtures + keyword seeds + behavior
parameters. They are NOT fitted to any log data (there is no real traffic yet);
the team's 100 LLM personas are deliberately not reused because their clicks were
generated from the same category labels the recommender consumes (circular).
"""

import hashlib
from dataclasses import dataclass, field
from functools import cached_property
from typing import Dict, FrozenSet, List, Optional, Tuple

import numpy as np

from sim.catalog import CATEGORY_CODES, PRESSES
from sim.text import nouns_of_all


@dataclass(frozen=True)
class Archetype:
    name: str
    label_ko: str
    category_mix: Dict[int, float]
    core_categories: FrozenSet[int]
    keywords: Tuple[str, ...]
    press_bias: Tuple[str, ...]
    sessions_per_day: float
    eta: float
    novelty: float
    fatigue: float
    drift_to: str


ARCHETYPES: Tuple[Archetype, ...] = (
    Archetype("politics_junkie", "정치 고관여층", {100: .55, 700: .20, 400: .15, 200: .10}, frozenset({100, 700}),
              ("국회", "대통령", "선거", "여당", "야당", "정당", "외교", "북한"),
              ("경향신문", "동아일보", "국민일보", "세계일보"), 3, 1.0, .6, 1.0, "sports_fan"),
    Archetype("investor", "재테크 투자자", {200: .60, 300: .20, 700: .15, 100: .05}, frozenset({200, 300}),
              ("금리", "환율", "주식", "코스피", "반도체", "실적", "투자", "수출"),
              ("매일경제", "한국경제"), 6, 0.8, .7, 1.0, "culture_lover"),
    Archetype("tech_enthusiast", "테크 얼리어답터", {300: .65, 200: .20, 700: .10, 500: .05}, frozenset({300, 200}),
              ("반도체", "AI", "인공지능", "스마트폰", "배터리", "로봇", "우주", "HBM"),
              ("전자신문", "AI타임스"), 3, 0.9, .8, 1.2, "social_watcher"),
    Archetype("sports_fan", "스포츠 팬", {600: .70, 500: .15, 400: .10, 700: .05}, frozenset({600, 500}),
              ("야구", "축구", "KBO", "월드컵", "올림픽", "손흥민", "감독", "우승"),
              ("동아일보", "경향신문"), 3, 1.2, .5, 0.8, "investor"),
    Archetype("homeowner", "부동산·생활경제", {200: .40, 400: .30, 500: .25, 100: .05}, frozenset({200, 400}),
              ("부동산", "아파트", "전세", "금리", "물가", "육아", "교육", "대출"),
              ("매일경제", "한국경제", "국민일보"), 1, 1.0, .5, 1.0, "global_watcher"),
    Archetype("global_watcher", "국제 뉴스 관심층", {700: .55, 100: .20, 200: .15, 300: .10}, frozenset({700, 100}),
              ("미국", "중국", "트럼프", "우크라이나", "전쟁", "관세", "일본", "정상회담"),
              ("동아일보", "세계일보", "한국경제"), 1, 1.0, .6, 1.0, "tech_enthusiast"),
    Archetype("culture_lover", "생활·문화 소비자", {500: .60, 400: .20, 600: .10, 300: .10}, frozenset({500, 400}),
              ("영화", "드라마", "공연", "여행", "음식", "건강", "전시", "음악"),
              ("경향신문", "국민일보"), 1, 1.1, .4, 1.2, "investor"),
    Archetype("social_watcher", "사회 이슈 관심층", {400: .55, 100: .20, 500: .15, 200: .10}, frozenset({400, 100}),
              ("경찰", "법원", "교육", "의료", "복지", "노동", "저출생", "기후"),
              ("경향신문", "국민일보", "세계일보"), 3, 1.0, .5, 1.0, "tech_enthusiast"),
    Archetype("casual_scanner", "가벼운 훑어보기", {400: .22, 500: .20, 100: .15, 200: .13, 600: .12, 700: .10, 300: .08},
              frozenset({400, 500}),
              ("날씨", "건강", "사건", "여행", "물가"),
              PRESSES, 1, 1.6, .4, 1.5, "global_watcher"),
    Archetype("deep_reader", "시사 심층 독자", {200: .30, 700: .30, 300: .20, 100: .20}, frozenset({200, 700}),
              ("반도체", "관세", "금리", "미국", "중국", "수출", "AI", "외교"),
              ("동아일보", "한국경제", "매일경제"), 6, 0.5, .5, 0.7, "sports_fan"),
)
ARCHETYPE_BY_NAME: Dict[str, Archetype] = {a.name: a for a in ARCHETYPES}


@dataclass(frozen=True)
class Profile:
    archetype: str
    category_pref: Tuple[float, ...]  # aligned with CATEGORY_CODES, sums to 1
    keywords: Tuple[str, ...]
    press_pref: Tuple[float, ...]     # aligned with PRESSES, sums to 1
    sessions_per_day: float
    eta: float
    novelty: float
    fatigue: float

    def pref_for(self, category_code: Optional[int]) -> float:
        if category_code is None or category_code not in CATEGORY_CODES:
            return 1.0 / len(CATEGORY_CODES)
        return self.category_pref[CATEGORY_CODES.index(category_code)]

    def press_for(self, press_names) -> float:
        prefs = [self.press_pref[PRESSES.index(p)] for p in press_names if p in PRESSES]
        return float(np.mean(prefs)) if prefs else 0.0

    @cached_property
    def keyword_nouns(self) -> FrozenSet[str]:
        return nouns_of_all(self.keywords)

    def top_categories(self, min_weight: float = 0.15, max_n: int = 3) -> List[int]:
        order = sorted(range(len(CATEGORY_CODES)), key=lambda i: -self.category_pref[i])
        chosen = [CATEGORY_CODES[i] for i in order[:max_n] if self.category_pref[i] >= min_weight]
        return chosen or [CATEGORY_CODES[order[0]]]


@dataclass
class SimUser:
    index: int
    email: str
    password: str
    nickname: str
    gender: str
    birth_year: int
    join_day: int
    profile: Profile
    drift_day: Optional[int] = None
    drift_profile: Optional[Profile] = None
    is_synthetic: bool = True

    def profile_on(self, day: int) -> Profile:
        if self.drift_profile is not None and self.drift_day is not None and day >= self.drift_day:
            return self.drift_profile
        return self.profile

    @property
    def is_late_joiner(self) -> bool:
        return self.join_day > 0

    @property
    def is_drifter(self) -> bool:
        return self.drift_profile is not None


@dataclass(frozen=True)
class PopulationConfig:
    n_users: int = 300
    seed: int = 0
    n_days: int = 7
    late_join_frac: float = 0.10
    drift_frac: float = 0.10
    drift_day: int = 3
    category_concentration: float = 30.0
    press_concentration: float = 10.0
    off_archetype_keyword_prob: float = 0.3
    run_tag: str = "sim"


def make_profile(arch: Archetype, rng: np.random.Generator, cfg: PopulationConfig) -> Profile:
    mix = np.array([arch.category_mix.get(c, 0.0) for c in CATEGORY_CODES]) + 0.01
    cat = rng.dirichlet(cfg.category_concentration * mix / mix.sum())
    k = int(rng.integers(4, len(arch.keywords) + 1))
    kws = [str(x) for x in rng.choice(arch.keywords, size=k, replace=False)]
    if rng.random() < cfg.off_archetype_keyword_prob:
        others = [a for a in ARCHETYPES if a.name != arch.name]
        other = others[int(rng.integers(len(others)))]
        kws.append(str(rng.choice(other.keywords)))
    bias = np.array([1.0 if p in arch.press_bias else 0.15 for p in PRESSES])
    press = rng.dirichlet(cfg.press_concentration * bias / bias.sum())
    return Profile(
        archetype=arch.name,
        category_pref=tuple(float(x) for x in cat),
        keywords=tuple(dict.fromkeys(kws)),
        press_pref=tuple(float(x) for x in press),
        sessions_per_day=arch.sessions_per_day,
        eta=float(arch.eta * np.exp(rng.normal(0.0, 0.15))),
        novelty=float(rng.beta(arch.novelty * 10, (1 - arch.novelty) * 10)),
        fatigue=float(arch.fatigue * np.exp(rng.normal(0.0, 0.2))),
    )


def generate_population(cfg: PopulationConfig) -> List[SimUser]:
    n = cfg.n_users
    rng = np.random.default_rng([cfg.seed, 1])
    # Stratified then shuffled so every archetype is represented even at small n.
    arch_idx = rng.permutation(np.arange(n) % len(ARCHETYPES))

    n_late = int(round(cfg.late_join_frac * n)) if cfg.n_days > 1 else 0
    late = set(rng.choice(n, size=n_late, replace=False).tolist()) if n_late else set()
    day0 = [i for i in range(n) if i not in late]
    n_drift = min(int(round(cfg.drift_frac * n)), len(day0)) if cfg.drift_day < cfg.n_days else 0
    drifters = set(rng.choice(day0, size=n_drift, replace=False).tolist()) if n_drift else set()

    users: List[SimUser] = []
    for i in range(n):
        urng = np.random.default_rng([cfg.seed, 2, i])
        arch = ARCHETYPES[int(arch_idx[i])]
        profile = make_profile(arch, urng, cfg)
        drift_profile = None
        if i in drifters:
            drift_profile = make_profile(ARCHETYPE_BY_NAME[arch.drift_to], urng, cfg)
        join_day = int(urng.integers(1, cfg.n_days)) if i in late else 0
        digest = hashlib.sha256(f"{cfg.run_tag}:{cfg.seed}:{i}".encode()).hexdigest()[:16]
        users.append(SimUser(
            index=i,
            email=f"{cfg.run_tag}-{cfg.seed}-{i:05d}@sim.invalid",
            password=f"sim-{digest}",
            nickname=f"sim_{arch.name}_{i:05d}",
            gender=str(urng.choice(["Male", "Female", "Not Specified"])),
            birth_year=int(urng.integers(1960, 2006)),
            join_day=join_day,
            profile=profile,
            drift_day=cfg.drift_day if drift_profile is not None else None,
            drift_profile=drift_profile,
        ))
    return users
