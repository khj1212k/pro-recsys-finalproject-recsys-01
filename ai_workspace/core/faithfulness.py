"""Deterministic fact-extraction and faithfulness checks for Korean LLM newsletters.

Pure functions, no DB/LLM calls. Lives in the runtime package (not evaluation/)
because the LangGraph workflow gates on it (docs/adr/0010); evaluation code
imports it from here so runtime never depends on the evaluation tree.
"""

import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

Span = Tuple[int, int]


# ---------------------------------------------------------------------------
# Fact dataclasses
# ---------------------------------------------------------------------------

@dataclass
class NumberFact:
    surface: str
    value: float
    unit: str
    span: Span

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DateFact:
    surface: str
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    relative: bool
    normalized: Optional[str]
    span: Span

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EntityFact:
    surface: str
    tag: str
    span: Span

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QuoteFact:
    surface: str
    span: Span

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Facts:
    numbers: List[NumberFact] = field(default_factory=list)
    dates: List[DateFact] = field(default_factory=list)
    entities: List[EntityFact] = field(default_factory=list)
    quotes: List[QuoteFact] = field(default_factory=list)
    entity_extractor: str = "kiwi"

    def to_dict(self) -> dict:
        return {
            "numbers": [n.to_dict() for n in self.numbers],
            "dates": [d.to_dict() for d in self.dates],
            "entities": [e.to_dict() for e in self.entities],
            "quotes": [q.to_dict() for q in self.quotes],
            "entity_extractor": self.entity_extractor,
        }


@dataclass
class Report:
    passed: bool
    number_total: int
    number_exact: int
    number_approx: int
    unsupported_numbers: List[dict]
    entity_total: int
    unsupported_entities: List[dict]
    entity_extractor: str
    quote_total: int
    unsupported_quotes: List[dict]
    thresholds: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DriftReport:
    added_numbers: List[dict]
    dropped_numbers: List[dict]
    added_dates: List[dict]
    dropped_dates: List[dict]
    added_entities: List[dict]
    dropped_entities: List[dict]
    has_drift: bool

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Kiwi lazy loader (same pattern as ai_workspace/core/clustering/split_v2.py
# _get_kiwi, reimplemented here so this module has no import-time dependency
# on the clustering package).
# ---------------------------------------------------------------------------

_KIWI = None
# The workflow runs clusters on a thread pool (docs/adr/0010); the first callers
# would otherwise race to build several ~100MB Kiwi instances, and one shared
# instance's analyze() is not documented as re-entrant, so calls are serialized
# too (milliseconds per newsletter, negligible next to LLM latency).
_KIWI_INIT_LOCK = threading.Lock()
_KIWI_CALL_LOCK = threading.Lock()


def _get_kiwi():
    global _KIWI
    if _KIWI is None:
        with _KIWI_INIT_LOCK:
            if _KIWI is None:
                try:
                    from kiwipiepy import Kiwi
                    _KIWI = Kiwi()
                except ImportError:
                    _KIWI = False
    return _KIWI


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------

SCALE_VALUES = {"조": 1e12, "억": 1e8, "만": 1e4, "천": 1e3}
_SCALE_CHARS = "".join(SCALE_VALUES)

_CORE = r"\d+(?:,\d{3})*(?:\.\d+)?"
# one segment: digits followed by a run of one-or-more scale chars that
# multiply together (e.g. 2천만 = 2 * 천 * 만); segments themselves add,
# e.g. 3조2000억 = (3 * 조) + (2000 * 억).
_SEGMENT = rf"{_CORE}\s*[{_SCALE_CHARS}]+\s*"
# a scaled amount: one-or-more segments plus an optional trailing bare digit
# group that carries no scale char at all (the 2000 in 1만2000).
_SCALED = rf"(?:{_SEGMENT})+(?:{_CORE})?"

# unit words that turn a bare number into a recognized numeric claim.
# order matters: longer/more specific alternatives must come first so the
# regex doesn't stop at a shorter prefix (%p before %, 퍼센트포인트 before 퍼센트).
_UNIT_MAP = [
    ("%p", "%p"),
    ("퍼센트포인트", "%p"),
    ("%", "%"),
    ("퍼센트", "%"),
    ("원", "KRW"),
    ("달러", "USD"),
    ("명", "명"),
    ("건", "건"),
    ("배", "배"),
    ("bp", "bp"),
]
_UNIT_ALT = "|".join(re.escape(u) for u, _ in _UNIT_MAP)
_UNIT_TAG = dict(_UNIT_MAP)

# A bare number (no scale word, e.g. "300건") is only extracted when an
# explicit unit follows -- this is deliberate: it is what keeps list
# numbering ("1."), phone numbers and plain years out of the number facts.
# A scaled number (has a 조/억/만/천 char) is extracted even with no trailing
# unit word, since "1,200억" is already an unambiguous quantity claim.
NUMBER_RE = re.compile(
    rf"(?P<scaled>{_SCALED})\s*(?P<unit1>{_UNIT_ALT})?"
    rf"|(?P<bare>{_CORE})\s*(?P<unit2>{_UNIT_ALT})"
    rf"|\$(?P<dollar>{_CORE})\s*(?P<dunit>billion|million)"
)

# "N사" (e.g. 반도체 3사, "3 companies") is deliberately NOT extracted as a
# numeric claim: 사/개사 and similar bound counters attach to almost any
# small integer in Korean business writing, and treating every one as a
# fact to source-check would flood the report with counter-word noise for
# little value. Only the unit words in _UNIT_MAP count as numeric claims.


def _parse_scaled(chunk: str) -> float:
    total = 0.0
    for m in re.finditer(rf"({_CORE})\s*([{_SCALE_CHARS}]+)", chunk):
        num = float(m.group(1).replace(",", ""))
        mult = 1.0
        for c in m.group(2):
            mult *= SCALE_VALUES[c]
        total += num * mult
    tail = re.search(rf"[{_SCALE_CHARS}]\s*({_CORE})\s*$", chunk)
    if tail:
        total += float(tail.group(1).replace(",", ""))
    return total


def _spans_overlap(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _extract_numbers(text: str, exclude_spans: Optional[List[Span]] = None) -> List[NumberFact]:
    exclude_spans = exclude_spans or []
    facts = []
    for m in NUMBER_RE.finditer(text):
        span = m.span()
        if any(_spans_overlap(span, ex) for ex in exclude_spans):
            continue
        gd = m.groupdict()
        if gd["dollar"] is not None:
            mult = 1e9 if gd["dunit"] == "billion" else 1e6
            value = float(gd["dollar"].replace(",", "")) * mult
            unit = "USD"
        elif gd["scaled"] is not None:
            value = _parse_scaled(gd["scaled"])
            unit = _UNIT_TAG.get(gd["unit1"], "") if gd["unit1"] else ""
        else:
            value = float(gd["bare"].replace(",", ""))
            unit = _UNIT_TAG[gd["unit2"]]
        # _SEGMENT's trailing \s* can pull whitespace before a unit word
        # into the match (e.g. "1,200억 " before "수준"); strip it from
        # both surface and span so span always delimits surface exactly.
        raw = m.group(0)
        surface = raw.strip()
        start = span[0] + (len(raw) - len(raw.lstrip()))
        end = span[1] - (len(raw) - len(raw.rstrip()))
        facts.append(NumberFact(surface=surface, value=value, unit=unit, span=(start, end)))
    return facts


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_RELATIVE_WORDS = [
    "오늘", "어제", "내일", "모레", "그제", "그저께",
    "지난주", "이번주", "다음주",
    "지난달", "이번달", "다음달",
    "작년", "올해", "내년", "지난해", "금년",
]
_REL_ALT = "|".join(sorted(_RELATIVE_WORDS, key=len, reverse=True))

# Alternatives are tried in order at each start position, so the more
# specific (longer) date shapes must be listed before the looser ones --
# a full "YYYY년 M월 D일" is attempted before falling back to "YYYY년 M월"
# then bare "YYYY년". Year requires exactly 4 digits so a duration like
# "3년 연속" isn't mistaken for the year 3.
DATE_RE = re.compile(
    r"(?P<fy>\d{4})\s*년\s*(?P<fm>\d{1,2})\s*월\s*(?P<fd>\d{1,2})\s*일"
    r"|(?P<ymy>\d{4})\s*년\s*(?P<ymm>\d{1,2})\s*월"
    r"|(?P<yy>\d{4})\s*년"
    r"|(?P<mdm>\d{1,2})\s*월\s*(?P<mdd>\d{1,2})\s*일"
    rf"|(?P<rel>{_REL_ALT})"
)


def _extract_dates(text: str) -> List[DateFact]:
    dates = []
    for m in DATE_RE.finditer(text):
        gd = m.groupdict()
        span = m.span()
        surface = m.group(0)
        if gd["fy"]:
            y, mo, d = int(gd["fy"]), int(gd["fm"]), int(gd["fd"])
            dates.append(DateFact(surface, y, mo, d, False, f"{y:04d}-{mo:02d}-{d:02d}", span))
        elif gd["ymy"]:
            y, mo = int(gd["ymy"]), int(gd["ymm"])
            dates.append(DateFact(surface, y, mo, None, False, f"{y:04d}-{mo:02d}", span))
        elif gd["yy"]:
            y = int(gd["yy"])
            dates.append(DateFact(surface, y, None, None, False, f"{y:04d}", span))
        elif gd["mdm"]:
            mo, d = int(gd["mdm"]), int(gd["mdd"])
            # year unknown: not a fully resolvable absolute date, so no
            # normalized string -- callers compare (year, month, day) directly.
            dates.append(DateFact(surface, None, mo, d, False, None, span))
        else:
            dates.append(DateFact(surface, None, None, None, True, None, span))
    return dates


# ---------------------------------------------------------------------------
# Entity extraction (Kiwi NNP / SL / SH)
# ---------------------------------------------------------------------------

_ENTITY_TAGS = {"NNP", "SL", "SH"}

# Coarse regex approximation of "entity-like token", reusing the same shape
# as ai_workspace/core/clustering/split_v2.py's _tokenize_title fallback
# (`re.findall(r"[가-힣A-Za-z0-9]{2,}", ...)`). Unlike Kiwi's tagger it
# cannot tell a proper noun (NNP/SL/SH) apart from an ordinary noun, so it
# is only ever used when a caller explicitly opts in -- see
# _extract_entities.
_ENTITY_FALLBACK_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")


def _extract_entities_fallback(text: str) -> List[EntityFact]:
    return [
        EntityFact(surface=m.group(0), tag="REGEX", span=m.span())
        for m in _ENTITY_FALLBACK_RE.finditer(text)
    ]


def _extract_entities(
    text: str, *, allow_regex_fallback: bool = False
) -> Tuple[List[EntityFact], str]:
    """Returns (entities, extractor_name) where extractor_name is "kiwi"
    or "regex_fallback".

    Raises ImportError when kiwipiepy is unavailable and
    `allow_regex_fallback` is False (the default): silently returning an
    empty list would be indistinguishable from "kiwi ran and found no
    entities", hiding a broken/missing dependency from callers. Passing
    `allow_regex_fallback=True` opts into the much weaker regex
    approximation instead of failing.
    """
    kiwi = _get_kiwi()
    if not kiwi:
        if not allow_regex_fallback:
            raise ImportError(
                "kiwipiepy is required for named-entity extraction but is "
                "not installed. Install kiwipiepy, or pass "
                "allow_regex_fallback=True to use a coarser regex-based "
                "approximation (no POS tagging, so ordinary nouns are not "
                "filtered out)."
            )
        return _extract_entities_fallback(text), "regex_fallback"

    with _KIWI_CALL_LOCK:
        tokens = kiwi.analyze(text)[0][0]
    entities = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.tag in _ENTITY_TAGS and len(tok.form.strip()) >= 2:
            start = tok.start
            end = tok.start + tok.len
            j = i + 1
            # merge only zero-gap same-tag tokens: a compound proper noun
            # Kiwi split into pieces, not two adjacent-but-separate names
            # (those keep a space, so their spans won't be zero-gap).
            while j < n and tokens[j].tag in _ENTITY_TAGS and tokens[j].start == end:
                end = tokens[j].start + tokens[j].len
                j += 1
            entities.append(EntityFact(surface=text[start:end], tag=tok.tag, span=(start, end)))
            i = j
        else:
            i += 1
    return entities, "kiwi"


# ---------------------------------------------------------------------------
# Quote extraction
# ---------------------------------------------------------------------------

_QUOTE_PATTERNS = [
    re.compile(r'"([^"]+)"'),
    re.compile(r'“([^”]+)”'),
    re.compile(r"'([^']+)'"),
    re.compile(r"‘([^’]+)’"),
]


def _extract_quotes(text: str) -> List[QuoteFact]:
    matches = []
    for pat in _QUOTE_PATTERNS:
        matches.extend(pat.finditer(text))
    matches.sort(key=lambda m: m.start())

    quotes = []
    taken: List[Span] = []
    for m in matches:
        span = m.span()
        if any(_spans_overlap(span, t) for t in taken):
            continue
        quotes.append(QuoteFact(surface=m.group(1), span=span))
        taken.append(span)
    return quotes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_facts(text: str, *, allow_regex_fallback: bool = False) -> Facts:
    text = text or ""
    dates = _extract_dates(text)
    numbers = _extract_numbers(text, exclude_spans=[d.span for d in dates])
    entities, entity_extractor = _extract_entities(
        text, allow_regex_fallback=allow_regex_fallback
    )
    quotes = _extract_quotes(text)
    return Facts(
        numbers=numbers,
        dates=dates,
        entities=entities,
        quotes=quotes,
        entity_extractor=entity_extractor,
    )


def _units_compatible(u1: str, u2: str) -> bool:
    """A unit-less scaled number (no trailing currency word, e.g. "12억")
    is assumed KRW-denominated -- the default case in Korean financial
    writing -- so it may stand in for an explicit KRW figure, but never
    for USD. Two explicit currencies must match exactly: without a
    conversion rate, a KRW amount and a USD amount are never the same
    claim even when their bare numeric value happens to coincide (e.g.
    unit-less "12억" must not match a "12억 달러" source figure).
    """
    if u1 == u2:
        return True
    return {u1, u2} == {"", "KRW"}


def _number_match_status(a: NumberFact, b: NumberFact, approx_tol: float) -> Optional[str]:
    if not _units_compatible(a.unit, b.unit):
        return None
    if a.value == b.value:
        return "exact"
    if a.value == 0 or b.value == 0:
        return None
    rel_diff = abs(a.value - b.value) / max(abs(a.value), abs(b.value))
    if rel_diff <= 1e-9:
        return "exact"
    if rel_diff <= approx_tol:
        return "approx"
    return None


def check_against_sources(
    generated: str,
    sources: List[str],
    *,
    number_approx_tol: float = 0.01,
    entity_threshold: float = 85.0,
    quote_threshold: float = 90.0,
    max_unsupported_numbers: int = 0,
    max_unsupported_entities: int = 0,
    max_unsupported_quotes: int = 0,
    allow_regex_fallback: bool = False,
) -> Report:
    gen_facts = extract_facts(generated, allow_regex_fallback=allow_regex_fallback)
    source_facts = [
        extract_facts(s, allow_regex_fallback=allow_regex_fallback) for s in sources
    ]
    sources = sources or []

    number_exact = 0
    number_approx = 0
    unsupported_numbers = []
    for nf in gen_facts.numbers:
        status = None
        for sf in source_facts:
            for cand in sf.numbers:
                s = _number_match_status(nf, cand, number_approx_tol)
                if s == "exact":
                    status = "exact"
                    break
                if s == "approx" and status != "exact":
                    status = "approx"
            if status == "exact":
                break
        if status == "exact":
            number_exact += 1
        elif status == "approx":
            number_approx += 1
        else:
            unsupported_numbers.append({**nf.to_dict(), "reason": "unsupported"})

    unsupported_entities = []
    for ef in gen_facts.entities:
        best = max((fuzz.partial_ratio(ef.surface, s) for s in sources), default=0.0)
        if best < entity_threshold:
            unsupported_entities.append({**ef.to_dict(), "best_score": best})

    unsupported_quotes = []
    for qf in gen_facts.quotes:
        best = max((fuzz.partial_ratio(qf.surface, s) for s in sources), default=0.0)
        if best < quote_threshold:
            unsupported_quotes.append({**qf.to_dict(), "best_score": best})

    passed = (
        len(unsupported_numbers) <= max_unsupported_numbers
        and len(unsupported_entities) <= max_unsupported_entities
        and len(unsupported_quotes) <= max_unsupported_quotes
    )

    return Report(
        passed=passed,
        number_total=len(gen_facts.numbers),
        number_exact=number_exact,
        number_approx=number_approx,
        unsupported_numbers=unsupported_numbers,
        entity_total=len(gen_facts.entities),
        unsupported_entities=unsupported_entities,
        entity_extractor=gen_facts.entity_extractor,
        quote_total=len(gen_facts.quotes),
        unsupported_quotes=unsupported_quotes,
        thresholds={
            "number_approx_tol": number_approx_tol,
            "entity_threshold": entity_threshold,
            "quote_threshold": quote_threshold,
            "max_unsupported_numbers": max_unsupported_numbers,
            "max_unsupported_entities": max_unsupported_entities,
            "max_unsupported_quotes": max_unsupported_quotes,
        },
    )


def _diff_facts(original: list, rewritten: list, eq_fn, multiset: bool = True) -> Tuple[list, list]:
    """Multiset diff: greedily pair each original item with an equivalent
    rewritten item; whatever is left over on each side is added/dropped.

    With multiset=False a fact only counts as added/dropped when no
    equivalent exists anywhere on the other side, so mentioning a fact
    fewer times is not drift."""
    if not multiset:
        added = [r for r in rewritten if not any(eq_fn(o, r) for o in original)]
        dropped = [o for o in original if not any(eq_fn(o, r) for r in rewritten)]
        return added, dropped
    remaining = list(rewritten)
    dropped = []
    for o in original:
        idx = next((i for i, r in enumerate(remaining) if eq_fn(o, r)), None)
        if idx is None:
            dropped.append(o)
        else:
            remaining.pop(idx)
    return remaining, dropped


def compare_rewrite(
    original: str,
    rewritten: str,
    *,
    number_approx_tol: float = 0.01,
    entity_match_threshold: float = 90.0,
    allow_regex_fallback: bool = False,
    multiset: bool = True,
) -> DriftReport:
    orig_facts = extract_facts(original, allow_regex_fallback=allow_regex_fallback)
    rewr_facts = extract_facts(rewritten, allow_regex_fallback=allow_regex_fallback)

    def number_eq(a: NumberFact, b: NumberFact) -> bool:
        return _number_match_status(a, b, number_approx_tol) is not None

    def date_eq(a: DateFact, b: DateFact) -> bool:
        if a.relative or b.relative:
            return a.relative and b.relative and a.surface == b.surface
        return (a.year, a.month, a.day) == (b.year, b.month, b.day)

    def entity_eq(a: EntityFact, b: EntityFact) -> bool:
        if a.surface == b.surface:
            return True
        return fuzz.ratio(a.surface, b.surface) >= entity_match_threshold

    added_numbers, dropped_numbers = _diff_facts(orig_facts.numbers, rewr_facts.numbers, number_eq, multiset)
    added_dates, dropped_dates = _diff_facts(orig_facts.dates, rewr_facts.dates, date_eq, multiset)
    added_entities, dropped_entities = _diff_facts(orig_facts.entities, rewr_facts.entities, entity_eq, multiset)

    added_numbers = [n.to_dict() for n in added_numbers]
    dropped_numbers = [n.to_dict() for n in dropped_numbers]
    added_dates = [d.to_dict() for d in added_dates]
    dropped_dates = [d.to_dict() for d in dropped_dates]
    added_entities = [e.to_dict() for e in added_entities]
    dropped_entities = [e.to_dict() for e in dropped_entities]

    has_drift = any([
        added_numbers, dropped_numbers,
        added_dates, dropped_dates,
        added_entities, dropped_entities,
    ])

    return DriftReport(
        added_numbers=added_numbers,
        dropped_numbers=dropped_numbers,
        added_dates=added_dates,
        dropped_dates=dropped_dates,
        added_entities=added_entities,
        dropped_entities=dropped_entities,
        has_drift=has_drift,
    )
