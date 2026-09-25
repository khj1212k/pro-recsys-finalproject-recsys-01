# 결정론적 품질 게이트 (docs/adr/0010)
# - check_newsletter_faithfulness: 생성 초안의 수치/인용/개체명이 클러스터 원문에 있는가
# - check_tone_drift: 문체 변환이 형식체 초안의 수치/날짜/개체명을 바꾸지 않았는가
# 판정 로직(core/faithfulness.py)은 순수 함수이고, 여기서는 "어떤 유형을 막을지"와
# 재생성 프롬프트에 넣을 구체적인 피드백 문장만 정한다.
import logging
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from core.faithfulness import check_against_sources, compare_rewrite

logger = logging.getLogger(__name__)

FAITHFULNESS_FIELDS: Tuple[str, ...] = ("title", "sentence", "content")
# 한줄소개(sentence)는 문체 변환 결과가 저장되지 않으므로(save_newsletter_to_db는 초안의
# sentence를 쓴다) 드리프트 비교에서 뺀다.
TONE_FIELDS: Tuple[str, ...] = ("title", "content")
_FIELD_SEP = "\n\n"

_TYPE_LABEL = {"numbers": "수치", "quotes": "인용문", "entities": "고유명사"}


def parse_types(value) -> Tuple[str, ...]:
    if isinstance(value, str):
        return tuple(t.strip() for t in value.split(",") if t.strip())
    return tuple(value or ())


def _join_fields(doc: Dict, fields: Sequence[str]) -> Tuple[str, List[Tuple[str, int, int]]]:
    parts, offsets, pos = [], [], 0
    for name in fields:
        text = str(doc.get(name) or "")
        offsets.append((name, pos, pos + len(text)))
        parts.append(text)
        pos += len(text) + len(_FIELD_SEP)
    return _FIELD_SEP.join(parts), offsets


def _attribute(item: dict, offsets) -> dict:
    start, end = item["span"]
    for name, lo, hi in offsets:
        if lo <= start and end <= hi:
            return {**item, "field": name, "span": (start - lo, end - lo)}
    return {**item, "field": None}


@dataclass
class FaithfulnessGateResult:
    passed: bool
    blocking: Dict[str, List[dict]]
    advisory: Dict[str, List[dict]]
    entity_extractor: str
    number_total: int
    feedback: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_newsletter_faithfulness(
    draft: Dict,
    articles: Sequence[Dict],
    blocking_types: Iterable[str] = ("numbers", "quotes"),
) -> FaithfulnessGateResult:
    """초안(제목·한줄소개·본문)의 사실을 클러스터 기사 원문(제목+전체 본문)과 대조한다.

    원문은 생성 프롬프트에 들어간 발췌가 아니라 전체 본문을 쓴다 - 발췌 밖에 있는 값은
    생성기가 볼 수 없었으므로 우연히 일치할 가능성만 남고, 그 경우에도 원문과 모순되지는
    않는다(느슨한 쪽의 오류라 게이트 목적에 해가 없다).
    """
    blocking_types = set(parse_types(blocking_types))
    text, offsets = _join_fields(draft, FAITHFULNESS_FIELDS)
    sources = [f"{a.get('title') or ''}\n{a.get('content') or ''}" for a in articles]

    try:
        report = check_against_sources(text, sources)
    except ImportError:
        # kiwipiepy가 없으면 개체명만 판정할 수 없다. 개체명은 기본적으로 참고용이므로
        # 수치/인용 판정은 그대로 하고, 개체명 결과는 버린다(정규식 폴백은 품사 구분이 없어
        # 모든 한글 토큰을 개체로 본다).
        logger.warning("kiwipiepy 없음: 사실성 게이트가 개체명 검사를 건너뜁니다")
        report = check_against_sources(text, sources, allow_regex_fallback=True)
        report.unsupported_entities = []

    found = {
        "numbers": [_attribute(x, offsets) for x in report.unsupported_numbers],
        "quotes": [_attribute(x, offsets) for x in report.unsupported_quotes],
        "entities": [_attribute(x, offsets) for x in report.unsupported_entities],
    }
    blocking = {k: v for k, v in found.items() if k in blocking_types}
    advisory = {k: v for k, v in found.items() if k not in blocking_types}
    passed = not any(blocking.values())
    return FaithfulnessGateResult(
        passed=passed,
        blocking=blocking,
        advisory=advisory,
        entity_extractor=report.entity_extractor,
        number_total=report.number_total,
        feedback="" if passed else _faithfulness_feedback(blocking, advisory),
    )


def _faithfulness_feedback(blocking: Dict[str, List[dict]], advisory: Dict[str, List[dict]]) -> str:
    field_label = {"title": "제목", "sentence": "한줄소개", "content": "본문", None: "초안"}
    lines = ["[사실성 검사] 원문 기사에서 확인할 수 없는 내용이 있습니다. "
             "원문에 있는 값으로 고치거나 해당 문장을 삭제하세요. 원문에 없는 수치를 계산해 만들지 마세요."]
    for kind, items in blocking.items():
        for it in items:
            lines.append(f"- {_TYPE_LABEL.get(kind, kind)} \"{it['surface']}\" ({field_label.get(it.get('field'))}): 원문에 없음")
    names = sorted({it["surface"] for it in advisory.get("entities", [])})
    if names:
        lines.append("(참고) 원문에서 찾지 못한 고유명사: " + ", ".join(names))
    return "\n".join(lines)


@dataclass
class ToneDriftResult:
    passed: bool
    blocking: Dict[str, List[dict]]
    advisory: Dict[str, List[dict]]
    feedback: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_tone_drift(
    original: Dict,
    converted: Dict,
    blocking_types: Iterable[str] = ("numbers", "dates", "entities_added"),
) -> ToneDriftResult:
    """형식체 초안 대비 캐주얼 변환본의 사실 드리프트(집합 기준, ADR 0010).

    집합 기준인 이유: 캐주얼 변환은 문장을 압축하므로 같은 사실을 더 적게 언급하는 것은
    정상이다. 원문 전체에서 사라지거나 새로 생긴 사실만 드리프트로 본다.
    개체명은 "추가"만 막는다 - 수식어를 덜어내며 기관명 하나가 빠지는 것은 흔하지만,
    원문에 없던 고유명사가 생기는 것은 환각이다. 상대 날짜(작년/지난해)는 같은 뜻의 다른
    표현이 흔해 참고용으로만 남긴다.
    """
    blocking_types = set(parse_types(blocking_types))
    orig_text, _ = _join_fields(original, TONE_FIELDS)
    conv_text, _ = _join_fields(converted, TONE_FIELDS)
    try:
        drift = compare_rewrite(orig_text, conv_text, multiset=False)
    except ImportError:
        logger.warning("kiwipiepy 없음: 문체 드리프트 게이트가 개체명 검사를 건너뜁니다")
        drift = compare_rewrite(orig_text, conv_text, multiset=False, allow_regex_fallback=True)
        drift.added_entities, drift.dropped_entities = [], []

    def absolute(dates):
        return [d for d in dates if not d["relative"]]

    def relative(dates):
        return [d for d in dates if d["relative"]]

    candidates = {
        "numbers": [{**n, "change": "added"} for n in drift.added_numbers]
        + [{**n, "change": "dropped"} for n in drift.dropped_numbers],
        "dates": [{**d, "change": "added"} for d in absolute(drift.added_dates)]
        + [{**d, "change": "dropped"} for d in absolute(drift.dropped_dates)],
        "entities_added": [{**e, "change": "added"} for e in drift.added_entities],
    }
    blocking = {k: v for k, v in candidates.items() if k in blocking_types and v}
    advisory = {k: v for k, v in candidates.items() if k not in blocking_types and v}
    rel = [{**d, "change": "added"} for d in relative(drift.added_dates)] + [
        {**d, "change": "dropped"} for d in relative(drift.dropped_dates)
    ]
    if rel:
        advisory["relative_dates"] = rel
    if drift.dropped_entities:
        advisory["entities_dropped"] = [{**e, "change": "dropped"} for e in drift.dropped_entities]

    passed = not blocking
    return ToneDriftResult(passed, blocking, advisory, "" if passed else _tone_feedback(blocking))


def _tone_feedback(blocking: Dict[str, List[dict]]) -> str:
    change_label = {"added": "원본에 없는데 추가됨", "dropped": "원본에 있는데 빠지거나 바뀜"}
    kind_label = {"numbers": "수치", "dates": "날짜", "entities_added": "고유명사"}
    lines = ["이전 변환에서 원본의 사실이 바뀌었습니다. 아래 항목을 원본과 똑같이 유지해 다시 변환하세요."]
    for kind, items in blocking.items():
        for it in items:
            lines.append(f"- {kind_label.get(kind, kind)} \"{it['surface']}\": {change_label[it['change']]}")
    return "\n".join(lines)
