"""
본문 추출기
뉴스 URL에서 본문을 추출하여 news_raw 테이블 업데이트
+ 약한 정제(lite) + DROP 마킹

✅ 반영 사항
- 구조/호출/저장 방식 그대로 유지
- 원문 처리(정제/컷 마커) = FULL 버전 로직 반영
- Selenium 완전 제거 (import/driver/분기 모두 삭제)
"""

import re
import trafilatura
from multiprocessing import Pool
from db.connection import get_connection
from config.settings import Settings


# =========================
# 정제 설정
# =========================
DROP_LEN = 350          # clean 길이 이 값 미만이면 DROP
DROP_PHOTO = True       # [포토] 제목이면 DROP
DROP_LIST = True        # 추천기사/에디터픽 리스트성이면 DROP

# =========================
# 정제용 정규식 & 마커 (FULL)
# =========================
_MULTI_SPACE_RE = re.compile(r"\s+")

_END_MARKERS = [
    "댓글을 입력해 주세요",
    "관련기사",
    # "추천기사",  # ✅ 제거 (매일경제 전용으로 처리)
    "많이 본 기사",
    "저작권자",
    "무단전재",
    "재배포 금지",
    "AI학습 및 활용 금지",
    "모바일버전",
    "트렌드뉴스",
    "© dongA.com",
    "All rights reserved",
    "ADVERTISEMENT",
    "관련 뉴스",
    "Copyright ⓒ",
    "Copyright ©",
    "ⓒ 세계일보",
]

# 동아일보: UI 블록
_DONGA_START_MARKERS = ["본문으로 바로가기"]
_DONGA_CUT_AFTER = ["프린트", "글자크기 설정"]

# 동아일보: 트렌드뉴스/좋아요/댓글 꼬리 블록 컷(추가)
_DONGA_TAIL_CUT_MARKERS = [
    "트렌드뉴스",
    "많이 본 댓글 순",
    "좋아요 ",
    "댓글 ",
]

# 동아일보: 저작권 꼬리 패턴(추가)
_DONGA_COPYRIGHT_MARKERS = [
    "© dongA.com",
    "All rights reserved",
    "dongA.com All rights reserved",
]

# 한국경제: 고정 UI 블록
_HK_UI_BLOCK = " - 기사 스크랩 - 공유 - 댓글 - 클린뷰 - 프린트"

# 기자 이메일 제거용 (모든 신문사 공통) - ✅ 개선사항 추가
_REPORTER_EMAIL_REGEX = re.compile(
    r'[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+'
)

# 한국경제: 기자명 + 이메일 이후 컷
# 다양한 패턴 지원:
# 1) "장지민 한경닷컴 객원기자 [email]@hankyung.com"
# 2) "워싱턴=이상은 특파원/최형창/김대훈 기자 [email]@hankyung.com" (복수 기자)
# 복수 기자 패턴: location=/names with / separators
_HK_REPORTER_REGEX = re.compile(
    r"(?:[가-힣]{2,10}=[가-힣/\s]+/)?((?:[가-힣]{2,4}\s+)?)(한경닷컴\s*)?(기자|객원기자|특파원|편집위원)\s+[A-Za-z0-9_.+-]+@hankyung\.com"
)

# 한국경제: 기사 시작 부분의 기자 서명 (이름 + 직함) 패턴
# 예: "정인설 중소기업부장", "홍길동 기자", "김철수 특파원" 등
_HK_REPORTER_START_REGEX = re.compile(
    r'^([가-힣]{2,4})\s*([가-힣]{2,10}(?:부장|기자|특파원|편집위원|논설위원|객원기자|객원|팀장|차장|국장|연구원))\s+'
)

# 한국경제: 문장 끝의 기자 서명 패턴 (이메일 제거 후 남는 부분)
# 예: "양지윤 기자", "김정아 객원기자", "김정아 객원", "장지민" 등
# 문장 끝(마침표 등) 뒤에 나오는 2-4자 한글 이름
_HK_REPORTER_END_REGEX = re.compile(
    r'[.!?]\s+([가-힣]{2,4})(?:\s+([가-힣]{0,10}(?:부장|기자|특파원|편집위원|논설위원|객원기자|객원|팀장|차장|국장|연구원)))?\s*$'
)

# AI타임스: 고정 네비 헤더
_AITIMES_HEADER_MARKERS = ["주요서비스 바로가기", "본문 바로가기", "매체정보 바로가기", "기사검색 바로가기"]

# DROP 판정용
_RE_PHOTO_TITLE = re.compile(r"\[\s*포토\s*\]", re.IGNORECASE)
_LIST_MARKERS = ["추천기사", "에디터 픽", "에디터픽", "Editor's Pick", "추천 기사", "많이 본 기사", "실시간", "랭킹"]

# 경향신문: 편성표/하이라이트 제목 DROP
_KHAN_DROP_TITLE_PREFIXES = [
    "[TV 하이라이트]",
    "[케이블·위성 하이라이트]",
]


def _cut_end(text: str) -> str:
    """댓글/저작권/모바일버전 이후를 잘라냄"""
    if not text:
        return ""
    idxs = [text.find(m) for m in _END_MARKERS if text.find(m) != -1]
    if idxs:
        cut = min(idxs)
        if cut >= 200:
            return text[:cut].rstrip()
    return text


def _normalize_spaces(t: str) -> str:
    """공백/개행 정리"""
    return _MULTI_SPACE_RE.sub(" ", (t or "")).strip()


def _cut_donga_tail(t: str) -> str:
    """
    동아일보에 자주 붙는 '트렌드뉴스/많이 본 댓글/좋아요/댓글' 덩어리,
    그리고 dongA.com 저작권 꼬리를 시작점부터 통째로 컷
    """
    if not t:
        return ""

    # 1) 트렌드뉴스/많이본댓글/좋아요/댓글 블록 시작점에서 끝까지 컷
    idxs = []
    for m in _DONGA_TAIL_CUT_MARKERS:
        i = t.find(m)
        if i != -1:
            idxs.append(i)
    if idxs:
        cut = min(idxs)
        if cut >= 400:
            t = t[:cut].rstrip()

    # 2) © dongA.com / All rights reserved 등 저작권 꼬리도 추가 컷
    idxs = []
    for m in _DONGA_COPYRIGHT_MARKERS:
        i = t.find(m)
        if i != -1:
            idxs.append(i)
    if idxs:
        cut = min(idxs)
        if cut >= 400:
            t = t[:cut].rstrip()

    return t


def _remove_reporter_emails(t: str) -> str:
    """
    ✅ 개선사항: 기자 이메일 주소 제거 (프라이버시 보호)
    보수적으로 이메일만 제거하고 기자명은 유지
    """
    if not t:
        return ""
    return _REPORTER_EMAIL_REGEX.sub("[이메일]", t)


def _remove_duplicate_content(t: str) -> str:
    """
    ✅ 개선사항: 반복되는 콘텐츠 블록 제거 (동아일보 트렌드뉴스 중복, 한국경제 문단 반복 등)
    1) 개별 라인 3회 이상 반복 제거
    2) 긴 문장/문단 블록이 2회 이상 반복되면 첫 번째만 남김
    """
    if not t or len(t) < 100:
        return t

    # 1단계: 개별 라인 중복 제거 (기존 로직)
    lines = t.split('\n')
    seen = {}
    result_lines = []

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            result_lines.append(line)
            continue

        # 라인 빈도 카운트
        if line_stripped in seen:
            seen[line_stripped] += 1
            # 3회 이상 반복되면 스킵
            if seen[line_stripped] >= 3:
                continue
        else:
            seen[line_stripped] = 1

        result_lines.append(line)

    t = '\n'.join(result_lines)

    # 2단계: 문장/문단 블록 중복 제거
    # 긴 문장 기준으로 split (마침표, 느낌표, 물음표 기준)
    sentences = re.split(r'([.!?]\s+)', t)

    # split 결과를 문장+구분자로 재구성
    full_sentences = []
    for i in range(0, len(sentences) - 1, 2):
        if i + 1 < len(sentences):
            full_sentences.append(sentences[i] + sentences[i + 1])
        else:
            full_sentences.append(sentences[i])
    if len(sentences) % 2 == 1:  # 마지막 남은 부분
        full_sentences.append(sentences[-1])

    # 50자 이상의 긴 문장만 중복 체크 (짧은 문장은 우연히 같을 수 있음)
    seen_long_sentences = {}
    result_sentences = []

    for sent in full_sentences:
        sent_stripped = sent.strip()

        if len(sent_stripped) >= 50:
            # 긴 문장은 중복 체크
            if sent_stripped in seen_long_sentences:
                seen_long_sentences[sent_stripped] += 1
                # 2회 이상 등장하면 스킵
                if seen_long_sentences[sent_stripped] >= 2:
                    continue
            else:
                seen_long_sentences[sent_stripped] = 1

        result_sentences.append(sent)

    return ''.join(result_sentences).strip()


def _remove_hankyung_reporter_start(t: str) -> str:
    """
    ✅ 개선사항: 한국경제 기사 시작 부분의 기자 서명(이름+직함) 제거
    예: "정인설 중소기업부장", "홍길동 기자" 등
    """
    if not t or len(t) < 10:
        return t

    m = _HK_REPORTER_START_REGEX.match(t)
    if m:
        # 기자 서명 부분 제거 (공백 포함)
        return t[m.end():].lstrip()

    return t


def _remove_hankyung_reporter_end(t: str) -> str:
    """
    ✅ 개선사항: 한국경제 기사 끝부분의 기자 서명(이름+직함) 제거
    이메일 제거 후 남는 부분 처리
    예: "양지윤 기자", "김정아 객원" 등
    """
    if not t or len(t) < 10:
        return t

    m = _HK_REPORTER_END_REGEX.search(t)
    if m:
        # 기자 서명 부분 제거
        return t[:m.start()].rstrip()

    return t


def _cut_hankyung_reporter_tail(t: str) -> str:
    """
    한국경제 기사에서 '기자명 + 이메일' 이후 반복 본문 / 광고 / 관련기사 컷
    """
    if not t:
        return ""
    m = _HK_REPORTER_REGEX.search(t)
    if m:
        cut = m.start()
        if cut >= 400:
            return t[:cut].rstrip()
    return t


# 매일경제 전용
def cut_mk_editor_pick_and_reco(text: str) -> str:
    """
    매일경제: '에디터 픽 추천기사', '추천기사' 블록만 제거.
    발견 시 해당 마커부터 끝까지 잘라냄.
    """
    if not text:
        return text

    markers = [
        "에디터 픽 추천기사",
        "추천기사",
    ]

    cut_idx = None
    for m in markers:
        idx = text.find(m)
        if idx != -1:
            cut_idx = idx if cut_idx is None else min(cut_idx, idx)

    if cut_idx is not None:
        return text[:cut_idx].rstrip()

    return text


def clean_text_lite(raw: str, press_name: str = "") -> str:
    """
    ✅ 임베딩용 1차 약한 정제 (FULL 버전 반영)
    - 언론사별 고정 UI 블록 제거
    - 공통 END 컷 (저작권/댓글/관련기사 등)
    - (동아일보) 트렌드뉴스/좋아요/댓글 꼬리 블록 컷 추가
    - (한국경제) 기자명+이메일 이후 꼬리 컷
    - (매일경제) 에디터픽/추천기사 블록 컷
    - 공백 정리만
    """
    if not raw:
        return ""

    t = raw.strip()
    pn = (press_name or "").strip()

    if pn == "동아일보":
        for m in _DONGA_START_MARKERS:
            idx = t.find(m)
            if idx != -1 and idx < 2000:
                t = t[idx + len(m):].lstrip()
                break

        last = -1
        for m in _DONGA_CUT_AFTER:
            j = t.find(m)
            if j != -1:
                last = max(last, j + len(m))
        if last != -1 and last < 1500:
            t = t[last:].lstrip()

    elif pn == "한국경제":
        k = t.find(_HK_UI_BLOCK)
        if k != -1 and k < 2000:
            t = t[k + len(_HK_UI_BLOCK):].lstrip()
        else:
            m = re.search(r"\s-\s기사\s스크랩\s-\s공유\s-\s댓글\s-\s클린뷰\s-\s프린트\s", t)
            if m and m.start() < 2000:
                t = t[m.end():].lstrip()
        # ✅ 개선사항: UI 블록 제거 후 기자 서명 제거
        t = _remove_hankyung_reporter_start(t)

    elif pn == "AI타임스":
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

    elif pn == "매일경제":
        t = cut_mk_editor_pick_and_reco(t)

    t = _cut_end(t)

    if pn == "동아일보":
        t = _cut_donga_tail(t)

    if pn == "한국경제":
        t = _cut_hankyung_reporter_tail(t)

    # ✅ 개선사항: 기자 이메일 제거 (모든 신문사 공통)
    t = _remove_reporter_emails(t)

    # ✅ 개선사항: 한국경제 이메일 제거 후 남은 기자 서명 제거
    if pn == "한국경제":
        t = _remove_hankyung_reporter_end(t)

    # ✅ 개선사항: 중복 콘텐츠 제거
    t = _remove_duplicate_content(t)

    return _normalize_spaces(t)


def is_drop_article(cleaned: str, title: str, press_name: str = "") -> tuple:
    """
    DROP 판정 (✅ 개선: 보수적으로 완화)
    Returns: (is_drop: bool, reasons: list[str])
    """
    reasons = []
    t = cleaned or ""
    ttl = title or ""
    pn = (press_name or "").strip()

    # 0) 경향신문 편성표/하이라이트 제목 DROP
    if pn == "경향신문":
        if any(ttl.strip().startswith(p) for p in _KHAN_DROP_TITLE_PREFIXES):
            reasons.append("khan_schedule_title")

    # 1) 포토 기사 (✅ 개선: 텍스트 길이도 함께 고려)
    # 제목에 [포토]가 있어도 본문이 충분히 있으면 유지
    if DROP_PHOTO and _RE_PHOTO_TITLE.search(ttl):
        if len(t.strip()) < 200:  # 200자 미만일 때만 드롭
            reasons.append("photo_title_short")

    # 2) 너무 짧음
    if len(t.strip()) < DROP_LEN:
        reasons.append(f"too_short<{DROP_LEN}")

    # 3) 추천기사/에디터픽 등 리스트성 본문 (✅ 개선: 조건 유지)
    # 리스트 마커가 있고 텍스트가 짧을 때만 드롭
    if DROP_LIST:
        hit = any(m in t for m in _LIST_MARKERS)
        if hit and len(t.strip()) < max(DROP_LEN * 2, 700):
            reasons.append("list_like_markers")

    return (len(reasons) > 0), reasons


class ContentExtractor:
    """기사 본문 추출기 (news_raw 테이블 사용)"""

    @staticmethod
    def get_strategy_for_press(press_name: str) -> str:
        """
        언론사명으로 크롤링 전략(strategy) 조회
        전자신문_* 통합 처리

        ✅ 구조 유지용: Settings.RSS_FEEDS를 그대로 탐색하지만,
        ✅ Selenium은 이제 사용하지 않으므로 결과가 selenium이어도 direct로 처리됨.
        """
        for source, (strategy, _) in Settings.RSS_FEEDS.items():
            if press_name == '전자신문':
                if source.startswith('전자신문'):
                    return strategy
            elif source == press_name:
                return strategy
        return 'direct'

    @staticmethod
    def process_single_article(article_data):
        """
        개별 기사 처리 (병렬 처리용)

        Args:
            article_data: (raw_news_id, url, press_name)

        Returns:
            str: 처리 결과 메시지
        """
        raw_news_id, url, press_name = article_data

        conn = get_connection()
        cur = conn.cursor()

        try:
            # ✅ 구조 유지: strategy 조회는 그대로 하되, 실제 크롤링은 항상 direct만 사용
            _ = ContentExtractor.get_strategy_for_press(press_name)

            text = None
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False
                )

            # [정제 + DROP 판정 + 저장]
            if text and len(text.strip()) > 0:
                cleaned = clean_text_lite(text, press_name=press_name)

                # 제목 조회 (DROP 판정용)
                cur.execute("SELECT raw_news_title FROM news_raw WHERE raw_news_id = %s", (raw_news_id,))
                title_row = cur.fetchone()
                title = title_row[0] if title_row else ""

                # DROP 판정
                is_drop, reasons = is_drop_article(cleaned, title, press_name=press_name)

                # 저장 (정제된 텍스트 + DROP 마킹)  ✅ 기존 방식 그대로
                if is_drop:
                    cur.execute("""
                        UPDATE news_raw
                        SET raw_news_content = %s, news_letter_id = -1
                        WHERE raw_news_id = %s
                    """, (cleaned, raw_news_id))
                    conn.commit()
                    return f"🚫 {press_name} DROP ({','.join(reasons)})"
                else:
                    cur.execute("""
                        UPDATE news_raw
                        SET raw_news_content = %s
                        WHERE raw_news_id = %s
                    """, (cleaned, raw_news_id))
                    conn.commit()
                    return f"✅ {press_name} (raw={len(text)} → clean={len(cleaned)})"

            else:
                # 빈 내용 → DROP 마킹  ✅ 기존 방식 그대로
                cur.execute("""
                    UPDATE news_raw
                    SET raw_news_content = '', news_letter_id = -1
                    WHERE raw_news_id = %s
                """, (raw_news_id,))
                conn.commit()
                return f"🚫 {press_name} DROP (empty)"

        except Exception as e:
            conn.rollback()
            return f"❌ {press_name} 에러: {str(e)[:50]}"

        finally:
            conn.close()

    def extract_parallel(self, num_workers=None):
        """병렬 본문 추출 (news_raw 테이블 사용)"""
        if num_workers is None:
            num_workers = Settings.PARALLEL_WORKERS

        # 1. 대상 조회 (본문이 없는 기사들)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT N.raw_news_id, N.raw_news_url, P.press_name
            FROM news_raw N
            JOIN press P ON N.press_id = P.press_id
            WHERE N.raw_news_content IS NULL OR N.raw_news_content = ''
        """)
        articles = cur.fetchall()
        conn.close()

        if not articles:
            print("💤 수집할 기사가 없습니다.")
            return 0

        print(f"🚀 병렬 수집 시작! (대상: {len(articles)}건, 워커: {num_workers}명)")

        # 2. 병렬 처리
        from tqdm import tqdm

        success_count = 0
        drop_count = 0
        error_count = 0

        with Pool(processes=num_workers) as pool:
            for result in tqdm(pool.imap_unordered(
                self.process_single_article, articles
            ), total=len(articles), desc="🚀 본문 추출 중"):
                if "✅" in result:
                    success_count += 1
                elif "🚫" in result:
                    drop_count += 1
                else:
                    error_count += 1

        print(f"🏁 전체 작업 종료. 성공: {success_count}, DROP: {drop_count}, 에러: {error_count}")
        return success_count


if __name__ == "__main__":
    extractor = ContentExtractor()
    extractor.extract_parallel()