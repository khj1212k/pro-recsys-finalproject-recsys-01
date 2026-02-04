# 언론사별 본문 정제용 정규식/마커 모음
# - cleaners.py에서 사용

import re

#  정제 설정 상수
DROP_LEN = 350          # 본문 길이가 이 값 미만이면 DROP
DROP_PHOTO = True       # 제목에 [포토]가 포함된 경우 처리 기준
DROP_LIST = True        # 리스트성 기사(추천기사 등) 처리 기준

#  공통 정규식 
_MULTI_SPACE_RE = re.compile(r"\s+")
_REPORTER_EMAIL_REGEX = re.compile(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+')
_RE_PHOTO_TITLE = re.compile(r"\[\s*포토\s*\]", re.IGNORECASE)

#  공통 마커 
_END_MARKERS = [
    "댓글을 입력해 주세요", "관련기사", "많이 본 기사", "저작권자", "무단전재", 
    "재배포 금지", "AI학습 및 활용 금지", "모바일버전", "트렌드뉴스", 
    "© dongA.com", "All rights reserved", "ADVERTISEMENT", "관련 뉴스", 
    "Copyright ⓒ", "Copyright ©", "ⓒ 세계일보", "GoodNews paper", 
    "ⓒ 국민일보", "클릭! 기사는"
]

_LIST_MARKERS = ["추천기사", "에디터 픽", "에디터픽", "Editor's Pick", "추천 기사", "많이 본 기사", "실시간", "랭킹"]

#  언론사별 특정 패턴/마커 

# 동아일보
_DONGA_START_MARKERS = ["본문으로 바로가기"]
_DONGA_CUT_AFTER = ["프린트", "글자크기 설정"]
_DONGA_TAIL_CUT_MARKERS = ["트렌드뉴스", "많이 본 댓글 순", "좋아요 ", "댓글 "]
_DONGA_COPYRIGHT_MARKERS = ["© dongA.com", "All rights reserved", "dongA.com All rights reserved"]

# 한국경제
_HK_UI_BLOCK = " - 기사 스크랩 - 공유 - 댓글 - 클린뷰 - 프린트"
_HK_REPORTER_REGEX = re.compile(
    r"(?:[가-힣]{2,10}=[가-힣/\s]+/)?((?:[가-힣]{2,4}\s+)?)(한경닷컴\s*)?(기자|객원기자|특파원|편집위원)\s+[A-Za-z0-9_.+-]+@hankyung\.com"
)
_HK_REPORTER_START_REGEX = re.compile(
    r'^([가-힣]{2,4})\s*([가-힣]{2,10}(?:부장|기자|특파원|편집위원|논설위원|객원기자|객원|팀장|차장|국장|연구원))\s+'
)
_HK_REPORTER_END_REGEX = re.compile(
    r'[.!?]\s+([가-힣]{2,4})(?:\s+([가-힣]{0,10}(?:부장|기자|특파원|편집위원|논설위원|객원기자|객원|팀장|차장|국장|연구원)))?\s*$'
)
_HK_UI_PATTERNS = [
    re.compile(r"[가-힣]{2,4}\s*기자\s*구독하기\s*입력\d{4}\.\d{2}\.\d{2}\s*\d{2}:\d{2}\s*수정\d{4}\.\d{2}\.\d{2}\s*\d{2}:\d{2}\s*"),
    re.compile(r"구독하기\s*입력\d{4}\.\d{2}\.\d{2}\s*\d{2}:\d{2}"),
    re.compile(r"글자크기\s*조절\s*"),
    re.compile(r"기사\s*스크랩\s*기사\s*스크랩\s*"),
    re.compile(r"공유\s*공유\s*"),
    re.compile(r"댓글\s*\d*\s*댓글\s*"),
    re.compile(r"클린뷰\s*클린뷰\s*"),
    re.compile(r"프린트\s*프린트\s*"),
    re.compile(r"좋아요\s*싫어요\s*후속기사\s*원해요\s*"),
    re.compile(r"ⓒ\s*한경닷컴.*$", re.MULTILINE),
]

# AI타임스
_AITIMES_HEADER_MARKERS = ["주요서비스 바로가기", "본문 바로가기", "매체정보 바로가기", "기사검색 바로가기"]

# 경향신문
_KHAN_DROP_TITLE_PREFIXES = ["[TV 하이라이트]", "[케이블·위성 하이라이트]"]

# 전자신문
_ETNEWS_UI_PATTERNS = [
    re.compile(r"^(SW|IT|과학|통신|전자|반도체|AI|인공지능|소프트웨어)\s+\1\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\[(포토|인사|단독|속보)\][^\n]*발행일\s*:\s*\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}\s*", re.IGNORECASE),
    re.compile(r"발행일\s*:\s*\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}\s*"),
    re.compile(r"공유하기\s*페이스북\s*X\(트위터\)\s*메일\s*URL\s*복사\s*"),
    re.compile(r"공유하기\s*페이스북\s*트위터\s*메일\s*URL\s*복사\s*"),
    re.compile(r"글자크기\s*설정\s*가\s*작게\s*가\s*보통\s*가\s*크게\s*"),
    re.compile(r"글자크기\s*설정\s*가\s*작게\s*가\s*보[^\s]*\s*"),
]

# 매일경제
_MK_UI_PATTERNS = [
    re.compile(r"^M매경미디어\s*소개\s*", re.MULTILINE),
    re.compile(r"공유\s*글자\s*크기\s*가\s*번역\s*"),
    re.compile(r"글자\s*크기\s*가\s*번역\s*"),
]