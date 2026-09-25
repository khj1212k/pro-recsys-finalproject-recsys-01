"""Newsletter items as the simulator sees them (API payloads) and offline catalogs.

The driver only ever builds `Item`s from API responses (black box). The offline
catalogs below exist for the in-process fake app and for bias calibration.
"""

import ast
import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from sim.text import nouns_of_all

# /newsletters/today returns the category *code* in its `category_id` field
# (backend/app/api/newsletter.py: category_id=cat.category_code).
CATEGORIES: Dict[int, str] = {
    100: "정치",
    200: "경제",
    300: "IT/과학",
    400: "사회",
    500: "생활/문화",
    600: "스포츠",
    700: "세계",
}
CATEGORY_CODES: Tuple[int, ...] = tuple(CATEGORIES)

# ai_workspace/config/settings.py RSS_FEEDS (전자신문 feeds collapse to one press).
PRESSES: Tuple[str, ...] = (
    "동아일보", "경향신문", "매일경제", "한국경제", "국민일보", "세계일보", "전자신문", "AI타임스",
)


@dataclass(frozen=True)
class Item:
    news_letter_id: int
    title: str
    sentence: str
    keywords: Tuple[str, ...]
    created_at: datetime
    raw_news_count: int = 1
    category_id: Optional[int] = None
    press_names: Tuple[str, ...] = ()

    @cached_property
    def noun_set(self) -> FrozenSet[str]:
        return nouns_of_all((self.title, self.sentence, " ".join(self.keywords)))

    @classmethod
    def from_api(cls, payload: dict, category_id: Optional[int] = None) -> "Item":
        """Build from a /newsletters/today or /onboarding/news element.

        Onboarding payloads carry neither category nor raw_news_count; the caller
        passes the category it asked for.
        """
        return cls(
            news_letter_id=int(payload["news_letter_id"]),
            title=payload.get("news_letter_title") or "",
            sentence=payload.get("news_letter_sentence") or "",
            keywords=tuple(payload.get("news_letter_keywords") or ()),
            created_at=parse_ts(payload.get("news_letter_created_at")),
            raw_news_count=int(payload.get("raw_news_count") or 1),
            category_id=payload.get("category_id", category_id),
            press_names=tuple(payload.get("press_names") or ()),
        )

    def to_api(self, include_press: bool = False) -> dict:
        out = {
            "news_letter_id": self.news_letter_id,
            "news_letter_title": self.title,
            "news_letter_sentence": self.sentence,
            "news_letter_keywords": list(self.keywords),
            "news_letter_created_at": self.created_at.isoformat(),
            "raw_news_count": self.raw_news_count,
            "category_id": self.category_id,
            "category_name": CATEGORIES.get(self.category_id, ""),
        }
        if include_press:
            out["press_names"] = list(self.press_names)
        return out


def parse_ts(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Catalog:
    items: List[Item]
    source: str = "synthetic"
    by_id: Dict[int, Item] = field(init=False)

    def __post_init__(self):
        self.by_id = {it.news_letter_id: it for it in self.items}

    def candidates(self, now: datetime, max_age: timedelta = timedelta(days=3)) -> List[Item]:
        return [it for it in self.items if now - max_age <= it.created_at <= now]


# Topic nouns per category for the *synthetic* catalog. Persona keyword seeds are
# drawn from the same pools, so on this catalog keyword overlap is partly true by
# construction - the ADR limits behavior claims accordingly and the team-archive
# catalog (real generated newsletters) is the non-constructed alternative.
TOPIC_POOLS: Dict[int, Tuple[str, ...]] = {
    100: ("국회", "대통령", "선거", "여당", "야당", "정당", "외교", "국방", "개헌", "총리", "법안", "지방선거", "북한", "안보"),
    200: ("금리", "환율", "주식", "코스피", "부동산", "아파트", "물가", "수출", "반도체", "실적", "투자", "은행", "관세", "전세"),
    300: ("반도체", "AI", "인공지능", "스마트폰", "배터리", "로봇", "우주", "양자", "데이터", "클라우드", "보안", "통신", "HBM", "플랫폼"),
    400: ("경찰", "법원", "사건", "교육", "의료", "복지", "노동", "저출생", "재판", "사고", "학교", "병원", "검찰", "기후"),
    500: ("영화", "드라마", "공연", "여행", "음식", "건강", "전시", "책", "음악", "축제", "패션", "날씨", "육아", "요리"),
    600: ("야구", "축구", "KBO", "월드컵", "올림픽", "농구", "배구", "골프", "손흥민", "감독", "선수", "경기", "리그", "우승"),
    700: ("미국", "중국", "일본", "트럼프", "우크라이나", "러시아", "전쟁", "유럽", "중동", "관세", "정상회담", "유엔", "이스라엘", "대선"),
}
_TITLE_TAILS = ("관련 소식", "동향 정리", "이슈 브리핑", "쟁점 분석", "현황 점검")


def synthetic_catalog(
    n_days: int,
    items_per_day: int = 40,
    start: datetime = datetime(2026, 1, 5, tzinfo=timezone.utc),
    seed: int = 0,
    category_weights: Optional[Sequence[float]] = None,
    first_id: int = 1,
    warmup_days: int = 2,
) -> Catalog:
    """Items published uniformly over [start - warmup_days, start + n_days).

    `warmup_days` gives the first simulated day a candidate pool of recent items.
    """
    rng = np.random.default_rng([seed, 7_919])
    codes = np.array(CATEGORY_CODES)
    weights = np.asarray(category_weights if category_weights is not None else np.ones(len(codes)), dtype=float)
    weights = weights / weights.sum()
    items: List[Item] = []
    nid = first_id
    for day in range(-warmup_days, n_days):
        for _ in range(items_per_day):
            code = int(rng.choice(codes, p=weights))
            pool = TOPIC_POOLS[code]
            n_kw = int(rng.integers(3, 6))
            kws = list(rng.choice(pool, size=n_kw, replace=False))
            if rng.random() < 0.2:
                other = int(rng.choice([c for c in codes if c != code]))
                kws[-1] = str(rng.choice(TOPIC_POOLS[other]))
            kws = tuple(dict.fromkeys(str(k) for k in kws))
            title = f"{kws[0]} {kws[1]} {rng.choice(_TITLE_TAILS)}"
            sentence = f"{kws[0]}와 {kws[-1]}을 둘러싼 {CATEGORIES[code]} 분야의 흐름을 정리했다."
            n_press = int(rng.integers(1, 4))
            presses = tuple(str(p) for p in rng.choice(PRESSES, size=n_press, replace=False))
            created = start + timedelta(days=day, seconds=float(rng.uniform(0, 86_400)))
            items.append(Item(
                news_letter_id=nid,
                title=title,
                sentence=sentence,
                keywords=kws,
                created_at=created,
                raw_news_count=int(1 + rng.negative_binomial(2, 0.3)),
                category_id=code,
                press_names=presses,
            ))
            nid += 1
    items.sort(key=lambda it: it.created_at)
    return Catalog(items=items, source="synthetic")


def team_archive_catalog(
    export_csv: Path,
    categories_csv: Path,
    n_days: int,
    start: datetime = datetime(2026, 1, 5, tzinfo=timezone.utc),
    warmup_days: int = 2,
) -> Catalog:
    """The team's 195 generated newsletters (local, gitignored data), re-timed.

    Original created_at order is kept but spread over the simulated window so the
    candidate pool refreshes daily. Press is not stored per newsletter in the
    export, so `press_names` stays empty (same as the production API payload).
    Category labels come from data/team_archive/derived (partly kNN-inferred).
    """
    cats: Dict[int, int] = {}
    with open(categories_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cats[int(row["news_letter_id"])] = _code_for_name(row["category_name"])
    rows = []
    csv.field_size_limit(10_000_000)
    with open(export_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    rows.sort(key=lambda r: (r["news_letter_created_at"], int(r["news_letter_id"])))
    span = timedelta(days=n_days + warmup_days)
    base = start - timedelta(days=warmup_days)
    items = []
    for i, row in enumerate(rows):
        nid = int(row["news_letter_id"])
        kws = row.get("news_letter_keywords") or "[]"
        try:
            keywords = tuple(str(k) for k in ast.literal_eval(kws))
        except (ValueError, SyntaxError):
            keywords = ()
        items.append(Item(
            news_letter_id=nid,
            title=row.get("news_letter_title") or "",
            sentence=row.get("news_letter_sentence") or "",
            keywords=keywords,
            created_at=base + span * (i + 0.5) / len(rows),
            raw_news_count=int(float(row.get("raw_news_count") or 1)),
            category_id=cats.get(nid),
        ))
    return Catalog(items=items, source="team_archive")


def _code_for_name(name: str) -> Optional[int]:
    for code, n in CATEGORIES.items():
        if n == name:
            return code
    return None


def item_ids(items: Iterable[Item]) -> List[int]:
    return [it.news_letter_id for it in items]
