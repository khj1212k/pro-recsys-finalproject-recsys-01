"""결정론적 게이트(ADR 0010)의 오탐/미탐 프로브 - LLM·DB 호출 없음.

1) 고정 사례: 같은 값의 다른 표기(띄어쓰기, 1.5조, 만 단위, 퍼센트, 천 단위 쉼표)는 통과하고
   값 변경·계산값·지어낸 인용은 막히는지.
2) 실제 한국어 LLM 뉴스레터(로컬 전용 파일, 예: 팀 아카이브)에 결정론적 변형을 가해 잰다.
   원문 기사 본문이 없는 뉴스레터라 "뉴스레터 자신"을 원문으로 두고,
   - 동치 재표기: 모든 수치를 같은 값의 다른 표기로 바꾼 초안이 막히는 비율(오탐)
   - 값 변경: 수치 하나를 ×1.1로 바꾼 초안이 막히는 비율(검출)
   - 문체: ToneConverter의 결정론적 폴백(어미 치환 + 이모지)이 드리프트로 막히는 비율(오탐)과,
     그 변환본의 수치 하나를 바꿨을 때 막히는 비율(검출)
   을 센다. 출력은 집계 숫자뿐이다(텍스트는 출력하지 않는다 - 저작권/데이터 정책).

  python -m evaluation.llm.gate_probe --newsletters 'data/team_archive/newsletters/*.json'
"""

import argparse
import glob
import hashlib
import json
import sys
from typing import Dict, Iterable, List, Optional

from core.faithfulness import NumberFact, _extract_numbers
from workflow.gates import check_newsletter_faithfulness, check_tone_drift

_SCALES = ((1e12, "조"), (1e8, "억"), (1e4, "만"))
_UNIT_SUFFIX = {"KRW": "원", "%": "%", "%p": "%p", "명": "명", "건": "건", "배": "배", "bp": "bp"}


def _fmt_int(n: int, comma: bool) -> str:
    return f"{n:,}" if comma else str(n)


def render_korean(value: float, *, spaced: bool = False) -> Optional[str]:
    """정수 값을 조/억/만 단위 한국어 표기로. 정수가 아니면 None(소수 표기는 render_decimal)."""
    if value != int(value) or value < 1e4:
        return None
    n, parts = int(value), []
    for scale, word in _SCALES:
        q, n = divmod(n, int(scale))
        if q:
            parts.append(f"{q}{word}")
    if n:
        parts.append(str(n))
    return (" " if spaced else "").join(parts)


def render_decimal(value: float) -> Optional[str]:
    """가장 큰 단위 하나로 소수 표기(1조5000억 -> 1.5조). 소수 둘째 자리 안에서 정확할 때만."""
    for scale, word in _SCALES:
        if value >= scale:
            q = value / scale
            if q != int(q) and round(q, 2) == q:
                return f"{q:g}{word}"
            return None
    return None


def equivalent_surfaces(fact: NumberFact) -> List[str]:
    """같은 값·같은 단위의 다른 표기 후보(원래 표기와 다른 것만). 표기 자체가 바뀌는 후보
    (1.5조, 만 단위, 쉼표, 퍼센트)를 앞에, 단위 앞 띄어쓰기만 다른 후보를 뒤에 둔다."""
    if fact.unit == "USD":
        return []
    if fact.unit in ("%", "%p"):
        word = "퍼센트" if fact.unit == "%" else "퍼센트포인트"
        out = [f"{fact.value:g}{word}", f"{fact.value:g} {_UNIT_SUFFIX[fact.unit]}"]
    else:
        body = [render_decimal(fact.value), render_korean(fact.value), render_korean(fact.value, spaced=True)]
        if fact.value == int(fact.value) and fact.value < 1e4:
            body += [_fmt_int(int(fact.value), True), _fmt_int(int(fact.value), False)]
        body = [b for b in body if b]
        unit = _UNIT_SUFFIX.get(fact.unit, "")
        if not unit:
            out = body
        else:
            first, second = (" ", "") if fact.unit == "KRW" else ("", " ")
            out = [f"{b}{first}{unit}" for b in body] + [f"{b}{second}{unit}" for b in body]
    return [x for x in dict.fromkeys(out) if x != fact.surface]


def changed_surface(fact: NumberFact) -> Optional[str]:
    """값을 ×1.1(정수로 반올림)로 바꾼 표기. 근사 허용(1%)보다 충분히 크다."""
    if fact.unit == "USD":
        return None
    new = round(fact.value * 1.1) if fact.value >= 10 else fact.value + 1
    if fact.unit in ("%", "%p"):
        return f"{new:g}{_UNIT_SUFFIX[fact.unit]}"
    body = render_korean(new) or _fmt_int(int(new), False)
    unit = _UNIT_SUFFIX.get(fact.unit, "")
    return f"{body} {unit}".strip() if fact.unit == "KRW" else f"{body}{unit}"


def _replace(text: str, fact: NumberFact, surface: str) -> str:
    s, e = fact.span
    return text[:s] + surface + text[e:]


# ---------------------------------------------------------------------------
# 1) 고정 사례
# ---------------------------------------------------------------------------

SOURCE = ("삼성전자는 평택 공장에 HBM 라인을 증설한다. 투자 규모는 1조5000억 원이다. "
          "협력사 직원 1만2000명이 참여하고 D램 가격은 20% 올랐다. SK하이닉스는 2조 원을 투자한다. "
          "회사 측은 \"수요에 맞춰 생산을 늘리겠다\"고 밝혔다.")

FIXED_CASES = [
    # (이름, 초안, 막혀야 하는가)
    ("원문 그대로", "투자 규모는 1조5000억 원이다.", False),
    ("띄어쓰기", "투자 규모는 1조 5000억 원이다.", False),
    ("단위 붙여쓰기", "투자 규모는 1조5000억원이다.", False),
    ("소수 표기", "투자 규모는 1.5조 원이다.", False),
    ("만 단위", "직원 12,000명이 참여한다.", False),
    ("퍼센트 표기", "D램 가격은 20퍼센트 올랐다.", False),
    ("인용 요약(따옴표 없음)", "회사는 수요에 맞춰 생산을 늘리겠다고 밝혔다.", False),
    ("값 변경", "투자 규모는 3조 원이다.", True),
    ("근사 허용 밖(+5%)", "직원 1만2600명이 참여한다.", True),
    ("계산값(합계)", "두 회사 투자 합계는 3조5000억 원이다.", True),
    ("지어낸 인용", "회사는 \"시장을 선도하겠다\"고 말했다.", True),
]


def fixed_cases() -> List[dict]:
    arts = [{"title": "HBM 증설", "content": SOURCE}]
    rows = []
    for name, text, should_block in FIXED_CASES:
        r = check_newsletter_faithfulness({"title": "", "sentence": "", "content": text}, arts)
        rows.append({"case": name, "should_block": should_block, "blocked": not r.passed,
                     "ok": (not r.passed) == should_block})
    return rows


# ---------------------------------------------------------------------------
# 2) 실제 뉴스레터 변형
# ---------------------------------------------------------------------------

def load_newsletters(patterns: Iterable[str]) -> List[Dict[str, str]]:
    """팀 아카이브 JSON(리스트 또는 {"newsletters": [...]})에서 제목/한줄소개/본문만, 본문 해시로 중복 제거."""
    seen, out = set(), []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            data = json.load(open(path, encoding="utf-8"))
            rows = data.get("newsletters", []) if isinstance(data, dict) else data
            for r in rows:
                doc = {"title": r.get("news_letter_title") or r.get("title") or "",
                       "sentence": r.get("news_letter_sentence") or r.get("sentence") or "",
                       "content": r.get("news_letter_content") or r.get("content") or ""}
                h = hashlib.sha256(doc["content"].encode("utf-8")).hexdigest()
                if doc["content"] and h not in seen:
                    seen.add(h)
                    out.append(doc)
    return out


def _numbers(text: str) -> List[NumberFact]:
    return _extract_numbers(text)


def probe_newsletters(docs: List[Dict[str, str]]) -> dict:
    from core.tone_converter import ToneConverter

    softener = ToneConverter.__new__(ToneConverter)  # 결정론적 폴백만 쓴다 - LLM 클라이언트 불필요
    c = {"newsletters": len(docs), "numbers": 0,
         "equiv_docs": 0, "equiv_numbers": 0, "equiv_flagged": 0, "equiv_docs_blocked": 0,
         "changed_trials": 0, "changed_blocked": 0, "changed_collisions": 0,
         "tone_docs": 0, "tone_blocked": 0, "tone_changed_trials": 0, "tone_changed_blocked": 0}
    for doc in docs:
        src = [{"title": doc["title"], "content": f"{doc['sentence']}\n{doc['content']}"}]
        facts = _numbers(doc["content"])
        c["numbers"] += len(facts)

        # 동치 재표기: 뒤에서부터 바꿔야 앞쪽 span이 유지된다
        text, n_changed = doc["content"], 0
        for f in sorted(facts, key=lambda f: f.span[0], reverse=True):
            alts = equivalent_surfaces(f)
            if alts:
                text = _replace(text, f, alts[0])
                n_changed += 1
        if n_changed:
            r = check_newsletter_faithfulness({**doc, "content": text}, src)
            c["equiv_docs"] += 1
            c["equiv_numbers"] += n_changed
            c["equiv_flagged"] += len(r.blocking.get("numbers", []))
            c["equiv_docs_blocked"] += int(not r.passed)

        # 값 변경: 수치마다 하나씩
        for f in facts:
            new = changed_surface(f)
            if not new:
                continue
            probe = {"title": "", "sentence": "", "content": new}
            if check_newsletter_faithfulness(probe, src).passed:
                c["changed_collisions"] += 1  # 바꾼 값이 뉴스레터 다른 곳에 이미 있음 - 검출 대상 아님
                continue
            c["changed_trials"] += 1
            r = check_newsletter_faithfulness({**doc, "content": _replace(doc["content"], f, new)}, src)
            c["changed_blocked"] += int(not r.passed)

        # 문체: 어미 치환 폴백은 사실을 바꾸지 않으므로 막히면 오탐
        formal = {"title": doc["title"], "content": doc["content"]}
        casual = softener._fallback_convert(formal, None)
        c["tone_docs"] += 1
        c["tone_blocked"] += int(not check_tone_drift(formal, casual).passed)
        cf = _numbers(casual["content"])
        if cf:
            new = changed_surface(cf[0])
            if new and not check_newsletter_faithfulness({"title": "", "sentence": "", "content": new},
                                                         [{"title": "", "content": doc["content"]}]).passed:
                c["tone_changed_trials"] += 1
                drifted = {**casual, "content": _replace(casual["content"], cf[0], new)}
                c["tone_changed_blocked"] += int(not check_tone_drift(formal, drifted).passed)

    def rate(a, b):
        return round(c[a] / c[b], 4) if c[b] else None

    c["equiv_false_positive_rate_per_number"] = rate("equiv_flagged", "equiv_numbers")
    c["equiv_false_block_rate_per_doc"] = rate("equiv_docs_blocked", "equiv_docs")
    c["changed_detection_rate"] = rate("changed_blocked", "changed_trials")
    c["tone_false_block_rate"] = rate("tone_blocked", "tone_docs")
    c["tone_changed_detection_rate"] = rate("tone_changed_blocked", "tone_changed_trials")
    return c


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--newsletters", nargs="*", default=[], help="로컬 뉴스레터 JSON glob (gitignore된 data/ 아래)")
    args = parser.parse_args(argv)
    fixed = fixed_cases()
    report = {"fixed_cases": fixed, "fixed_all_ok": all(r["ok"] for r in fixed)}
    if args.newsletters:
        report["newsletters"] = probe_newsletters(load_newsletters(args.newsletters))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["fixed_all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
