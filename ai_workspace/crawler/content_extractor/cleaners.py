# 기사 본문 정제 모듈
# - 언론사별 UI/광고/기자 서명 등 노이즈 제거
# - 중복 콘텐츠 필터링

import re
from .patterns import (
    _MULTI_SPACE_RE, _REPORTER_EMAIL_REGEX, _RE_PHOTO_TITLE, _END_MARKERS, _LIST_MARKERS,
    _DONGA_START_MARKERS, _DONGA_CUT_AFTER, _DONGA_TAIL_CUT_MARKERS, _DONGA_COPYRIGHT_MARKERS,
    _HK_UI_BLOCK, _HK_REPORTER_REGEX, _HK_REPORTER_START_REGEX, _HK_REPORTER_END_REGEX, _HK_UI_PATTERNS,
    _ETNEWS_UI_PATTERNS, _MK_UI_PATTERNS, _AITIMES_HEADER_MARKERS, _KHAN_DROP_TITLE_PREFIXES,
    DROP_LEN, DROP_PHOTO, DROP_LIST
)

#  공통 보조 함수 
def _normalize_spaces(t: str) -> str:
    # 공백/개행 정리
    return _MULTI_SPACE_RE.sub(" ", (t or "")).strip()

def _cut_end(text: str) -> str:
    # 댓글/저작권/모바일버전 이후를 잘라냄
    if not text:
        return ""
    idxs = [text.find(m) for m in _END_MARKERS if text.find(m) != -1]
    if idxs:
        cut = min(idxs)
        if cut >= 200:
            return text[:cut].rstrip()
    return text

def _remove_reporter_emails(t: str) -> str:
    # 기자 이메일 주소 제거
    if not t:
        return ""
    return _REPORTER_EMAIL_REGEX.sub("[이메일]", t)

def _remove_reporter_bylines_common(t: str) -> str:
    # 모든 신문사 공통: 이메일 제거 후 남은 기자 서명 패턴 제거
    if not t or len(t) < 10:
        return t

    # 1. [이메일] 마커 포함 기자 서명 제거
    t = re.sub(
        r'([가-힣]{2,4}\s+)*[가-힣]{2,4}\s+[가-힣]*(?:기자|특파원|편집위원|논설위원)\s*\[이메일\]',
        '', t
    )
    # 2. 문장 끝 위치=이름 기자 패턴 제거
    t = re.sub(
        r'[.!?]\s+[가-힣]{2,10}=[가-힣]{2,4}\s+[가-힣]*(?:기자|특파원|편집위원)\s*$',
        '.', t
    )
    # 3. 문장 끝 복수 기자 서명 제거
    t = re.sub(
        r'[.!?]\s+([가-힣]{2,4}\s+)*[가-힣]{2,4}\s+[가-힣]*(?:기자|특파원|편집위원|논설위원)\s*$',
        '.', t
    )
    return t.strip()

def _remove_duplicate_content(t: str) -> str:
    # 중복 콘텐츠 블록 제거
    if not t or len(t) < 100:
        return t

    # 라인 단위 중복 제거 (3회 이상)
    lines = t.split('\n')
    seen = {}
    result_lines = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            result_lines.append(line)
            continue
        seen[line_stripped] = seen.get(line_stripped, 0) + 1
        if seen[line_stripped] < 3:
            result_lines.append(line)
    
    t = '\n'.join(result_lines)
    
    # 문장 단위 중복 제거 (50자 이상 긴 문장 2회 이상 시)
    sentences = re.split(r'([.!?]\s+)', t)
    full_sentences = []
    for i in range(0, len(sentences) - 1, 2):
        full_sentences.append(sentences[i] + sentences[i+1])
    if len(sentences) % 2 == 1:
        full_sentences.append(sentences[-1])

    seen_long = {}
    result_sents = []
    for sent in full_sentences:
        s_strip = sent.strip()
        if len(s_strip) >= 50:
            seen_long[s_strip] = seen_long.get(s_strip, 0) + 1
            if seen_long[s_strip] >= 2:
                continue
        result_sents.append(sent)
    
    return ''.join(result_sents).strip()

#  언론사별 전용 정제 함수 
def _clean_donga(t: str) -> str:
    # 시작 마커 제거
    for m in _DONGA_START_MARKERS:
        idx = t.find(m)
        if idx != -1 and idx < 2000:
            t = t[idx + len(m):].lstrip()
            break
    # 상단 UI 제거
    last = -1
    for m in _DONGA_CUT_AFTER:
        j = t.find(m)
        if j != -1:
            last = max(last, j + len(m))
    if last != -1 and last < 1500:
        t = t[last:].lstrip()
    
    # 꼬리 제거
    idxs = [t.find(m) for m in _DONGA_TAIL_CUT_MARKERS + _DONGA_COPYRIGHT_MARKERS if t.find(m) != -1]
    if idxs:
        cut = min(idxs)
        if cut >= 400:
            t = t[:cut].rstrip()
    return t

def _clean_hankyung(t: str) -> str:
    # UI 블록 제거
    k = t.find(_HK_UI_BLOCK)
    if k != -1 and k < 2000:
        t = t[k + len(_HK_UI_BLOCK):].lstrip()
    else:
        m = re.search(r"\s-\s기사\s스크랩\s-\s공유\s-\s댓글\s-\s클린뷰\s-\s프린트\s", t)
        if m and m.start() < 2000:
            t = t[m.end():].lstrip()
    
    # 시작 기자 서명 제거
    m_start = _HK_REPORTER_START_REGEX.match(t)
    if m_start:
        t = t[m_start.end():].lstrip()
    
    # UI 패턴들 제거
    for pattern in _HK_UI_PATTERNS:
        t = pattern.sub("", t)
    
    # 기자 이메일 이후 꼬리 컷
    m_tail = _HK_REPORTER_REGEX.search(t)
    if m_tail and m_tail.start() >= 400:
        t = t[:m_tail.start()].rstrip()
        
    return t

def _clean_etnews(t: str) -> str:
    for pattern in _ETNEWS_UI_PATTERNS:
        t = pattern.sub("", t)
    return t.strip()

def _clean_mk(t: str) -> str:
    # 에디터픽/추천기사 컷
    markers = ["에디터 픽 추천기사", "추천기사"]
    idxs = [t.find(m) for m in markers if t.find(m) != -1]
    if idxs:
        t = t[:min(idxs)].rstrip()
    # UI 패턴 제거
    for pattern in _MK_UI_PATTERNS:
        t = pattern.sub("", t)
    return t.strip()

def _clean_aitimes(t: str) -> str:
    last = -1
    for m in _AITIMES_HEADER_MARKERS:
        j = t.find(m)
        if j != -1:
            last = max(last, j + len(m))
    if last != -1 and last < 2500:
        t = t[last:].lstrip()
    j = t.find("발행일:")
    if j != -1 and j < 2500:
        t = t[j:].lstrip()
    return t

#  메인 
def clean_text_lite(raw: str, press_name: str = "") -> str:
    if not raw: return ""
    t = raw.strip()
    pn = (press_name or "").strip()

    # 1. 언론사별 전용 정제
    if pn == "동아일보": t = _clean_donga(t)
    elif pn == "한국경제": t = _clean_hankyung(t)
    elif pn == "전자신문": t = _clean_etnews(t)
    elif pn == "매일경제": t = _clean_mk(t)
    elif pn == "AI타임스": t = _clean_aitimes(t)

    # 2. 공통 후처리
    t = _cut_end(t)
    t = _remove_reporter_emails(t)
    t = _remove_reporter_bylines_common(t)
    
    if pn == "한국경제":
        # 한국경제 특유의 끝 서명 추가 제거
        m_end = _HK_REPORTER_END_REGEX.search(t)
        if m_end: t = t[:m_end.start()].rstrip()

    t = _remove_duplicate_content(t)
    return _normalize_spaces(t)

def is_drop_article(cleaned: str, title: str, press_name: str = "") -> tuple:
    reasons = []
    t = (cleaned or "").strip()
    ttl = (title or "").strip()
    pn = (press_name or "").strip()

    if pn == "경향신문" and any(ttl.startswith(p) for p in _KHAN_DROP_TITLE_PREFIXES):
        reasons.append("khan_schedule_title")
    if DROP_PHOTO and _RE_PHOTO_TITLE.search(ttl) and len(t) < 200:
        reasons.append("photo_title_short")
    if len(t) < DROP_LEN:
        reasons.append(f"too_short<{DROP_LEN}")
    if DROP_LIST:
        if any(m in t for m in _LIST_MARKERS) and len(t) < max(DROP_LEN * 2, 700):
            reasons.append("list_like_markers")

    return (len(reasons) > 0), reasons