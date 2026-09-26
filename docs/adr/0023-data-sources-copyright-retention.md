# ADR 0023: 수집 출처와 라이선스, 저장·노출·공개 범위, 본문 보존 기한

## 상태
채택됨 (2026-09-26). 출처 표·정책브리핑 수집기·노출 규칙은 이 ADR과 함께 들어갔다. 30일 본문 보존 잡은 설계만 확정했고 구현은 아직이다. 수집 스키마(본문 추출 상태·본문 해시 컬럼)가 `main`에 병합된 뒤에 넣는다. 이유와 설계는 "결과와 한계"의 TODO 절에 있다.

이 문서는 공학적 운영 규칙이다. 법률 자문이 아니며, 법이 요구하는 최소한보다 보수적으로 정했다.

## 컨텍스트
- 수집기는 상업 언론사 8곳(동아일보, 경향신문, 매일경제, 한국경제, 국민일보, 세계일보, 전자신문, AI타임스)의 RSS로 URL을 받는다. 본문은 trafilatura로 추출해 `news_raw.raw_news_content`에 저장한다. 본문을 지우는 코드는 없어서 기한 없이 쌓인다.
- 각 RSS 채널의 저작권 표시(2026-09-26 확인, `feedparser`로 채널 `copyright` 요소 읽음):
  - 동아일보 "Copyright donga.com"
  - 경향신문 "Copyright (C)1996 Kyunghyang Shinmun, All right reserved."
  - 매일경제 "Copyright 2026 MK"
  - 한국경제 "Copyright (c) 2005 hankyung.com All rights reserved"
  - 세계일보 "COPYRIGHT (c) SEGYE.com All rights reserved"
  - AI타임스 "Copyright (c) https://www.aitimes.com All rights reserved"
  - 국민일보·전자신문은 표시 없음
  - 여덟 곳 모두 재배포를 허락하는 표시는 없다.
- 저장소는 공개 GitHub fork다. 커밋한 것은 그대로 재배포된다. 반면 한국어 LLM·클러스터링 평가를 재현하려면 원문이 필요하다. 원문이 없으면 평가셋의 생성·판정을 다시 돌릴 수 없다.
- 로컬 수집 DB 실측(2026-09-26 02:45 UTC 무렵, compose DB `newsletter`, 읽기 전용 트랜잭션의 SELECT만 사용):

  | 항목 | 값 |
  |---|---|
  | `news_raw` 행 | 690 |
  | 본문 있는 행 | 649 |
  | 본문 합계 | 2,628,194 bytes (UTF-8) |
  | 본문 평균 | 4,050 bytes / 1,726자 |
  | 테이블 전체(TOAST·인덱스 포함) | 6.3MB |
  | 수집 기간(`raw_news_crawled_at`, 타임존 없는 컬럼 값) | 2026-09-25 16:50 ~ 09-26 02:04 |

  첫 실행이 RSS 100시간치를 한꺼번에 받았기 때문에 이 기간의 일일 유입량은 정상 상태 값이 아니다.
- 재배포 가능한 한국어 출처가 필요하다. 원래 계획은 korea.kr 정책브리핑 RSS(공공누리 제1유형)였다. 공식 페이지를 다시 확인한 결과(접근일 2026-09-26):
  - korea.kr RSS는 2026-07-01에 전부 중단됐다. 공지의 중단 사유는 "콘텐츠 저작권 등 권리 보호에 따른 제공방식 변경"이다([공지](https://www.korea.kr/etc/noticeView.do?newsId=132038885)). 예전 피드 주소(`/rss/policy.xml`, `/rss/dept_cnc.xml` 등)는 HTTP 404를 돌려주고, RSS 안내 페이지(`/etc/rss.do`)는 메인으로 리다이렉트된다.
  - 같은 정책뉴스는 공공데이터포털 Open API로 계속 제공된다. 데이터셋은 [문화체육관광부_정책브리핑_정책뉴스_API](https://www.data.go.kr/data/15095335/openapi.do)(등록 2021-12-01, 수정 2026-07-21)다.
    - 이용허락범위: "공공저작물 : 출처표시 (제 1유형)"
    - 비용: 무료
    - 개발계정: 1,000회, 자동승인
    - 명세(페이지에 포함된 swagger): 엔드포인트 `apis.data.go.kr/1371000/policyNewsService2/policyNewsList2`, 필수 파라미터 `serviceKey`·`startDate`·`endDate`, 에러 코드 98 "날짜범위 3일 초과", 97 "날짜형식 오류"
    - 기사 필드: `Title`, `ContentsType`(H=HTML, T=텍스트), `DataContents`, `ApproveDate`(MM/DD/YYYY HH24:MI:SS), `EmbargoDate`, `OriginalUrl`, `KoglType`(공공누리유형)
    - 인증키 없이 호출하면 401 `SERVICE_KEY_IS_NULL`이 온다. 없는 경로는 `NO_OPENAPI_SERVICE_ERROR`를 돌려준다. 따라서 엔드포인트는 살아 있다.
  - [정책브리핑 저작권정책](https://www.korea.kr/guide/copyRight.do)의 내용:
    - 자료는 원칙적으로 문화체육관광부 저작물이다. 저작권법 제24조의2(공공저작물의 자유이용)에 따라 자유이용할 수 있다.
    - 자유이용할 수 있는 것은 공공누리 제1유형 표시가 붙은 저작물의 텍스트다.
    - 사진은 정부기관·연합뉴스 등, 이미지는 통로이미지(주)의 저작물이라 제외된다.
    - 출처는 구체적으로 표시해야 한다.
  - [공공누리 제1유형](https://www.kogl.or.kr/info/licenseType1.do)의 조건:
    - 출처 표시
    - 상업적 이용 가능
    - 변경·2차적 저작물 작성 가능
    - 공공기관이 후원하거나 특수한 관계에 있는 것처럼 오인하게 하는 표시 금지
    - 저작인격권 존중

## 검토한 대안

### 1. 재배포 가능한 한국어 출처
1. **korea.kr RSS**: 2026-07-01 중단(위 공지). 쓸 수 없다.
2. **korea.kr 웹페이지 스크레이핑**
   - RSS를 끊은 사유가 권리 보호다.
   - 기사별 공공누리 표시를 HTML에서 판별해야 한다.
   - 사진 캡션·제3자 사진 설명이 본문 추출에 섞인다.
   - 기각.
3. **공공데이터포털 정책뉴스 API (채택)**
   - 운영 기관이 정한 공식 채널이다. 데이터셋 전체의 이용허락이 제1유형이고, 기사마다 `KoglType`이 붙어 기사 단위로 거를 수 있다.
   - 본문을 API가 주므로 스크레이핑이 필요 없다.
   - 단점: 인증키가 필요하다(사용자가 포털에서 발급). 한 번에 3일까지만 조회된다. 날짜 파라미터 형식은 인증키가 없어 아직 실제 응답으로 확인하지 못했다.
4. **부처별 보도자료 RSS**: 부처마다 피드 형식과 공공누리 표기를 따로 확인해야 한다. 한 API로 모이는 정책브리핑보다 비용이 크다. 필요해지면 다시 검토한다.

### 2. 상업 언론사 본문 보존 기한
1. **무기한(현재)**
   - 재현성은 가장 좋다.
   - 대신 저작권 노출과 유출 위험이 계속 쌓이고, 저장량이 선형으로 늘어난다.
2. **7일**
   - 클러스터링 lookback(24~72시간)에는 충분하다.
   - 평가셋 추출(5~7일치 필요), 라벨링, 같은 클러스터로 생성·판정을 다시 돌리는 실험 한 사이클을 담기에는 빠듯하다.
3. **30일 (채택)**: 평가셋 한 번을 뽑고, 라벨링하고, 재생성 실험까지 끝낼 여유가 있다. 그러면서 "본문은 한 달 안에 지운다"는 상한을 둔다.
4. **본문을 저장하지 않음(처리 직후 폐기)**
   - 임베딩, 생성, 재시도, 판정기의 원문 발췌가 같은 본문을 여러 번 읽는다.
   - 매번 다시 내려받아야 해서 언론사 서버 부하와 실패 지점이 늘어난다.
   - 기각.

### 3. 기한이 지난 본문의 처리
1. **행 삭제**
   - `news_letter`와 클릭 로그가 기사를 참조한다.
   - 중복 판정용 해시도 함께 사라진다.
   - 기각.
2. **본문만 NULL, 나머지 유지 (채택)**
   - 해시·길이·제목·URL·발행시각·임베딩은 남긴다.
   - 해시가 남으므로 URL만 바뀐 재수집 기사를 계속 중복으로 판정할 수 있다.
   - 임베딩은 텍스트 표현이 아니라서 남긴다. 다만 한계 절의 역변환 위험을 참고한다.
3. **본문을 암호화해 따로 보관**
   - 키 관리 부담이 생긴다.
   - 보존 기한을 사실상 늘리는 것과 같다.
   - 기각.

## 결정

### 출처 표 (`ai_workspace/config/sources.py`)
| 출처 | 태그 | 재배포 | 근거 (확인일) |
|---|---|---|---|
| 상업 언론사 8곳 | `all-rights-reserved` | 불가 | RSS 채널 저작권 표시 또는 표시 없음, 이용허락 없음 (2026-09-26) |
| 정책브리핑 (`KoglType`=1인 기사만) | `kogl-1` | 가능, 출처 표시 조건 | data.go.kr 데이터셋 이용허락범위, korea.kr 저작권정책 (2026-09-26) |
| 표에 없는 출처 | `unknown` | 불가 (fail closed) | — |

`Settings.RSS_FEEDS`의 `(strategy, url)` 튜플 모양은 바꾸지 않았다. 수집기, 본문 추출기, 진행 중인 다른 브랜치 코드가 이 튜플을 그대로 언패킹한다. 대신 언론사 이름 기준의 별도 표에 태그를 둔다. 설정된 모든 RSS 출처에 명시적 판정이 있는지는 `tests/test_source_licenses.py`가 확인한다.

### 저장·노출·공개 범위
| 항목 | 상업 언론사 | 정책브리핑(공공누리 제1유형) |
|---|---|---|
| DB 본문 | 수집 후 30일까지. 이후 NULL로 비우고 sha256·길이는 남긴다 | 기한 없음 |
| DB 제목·URL·발행시각·임베딩 | 유지 | 유지 |
| 저장소·리포트·로그 | 기사 id, URL, 본문 sha256, 건수·길이 같은 집계만. 설명에 꼭 필요하면 한 문장 이내 인용 + 출처 | 출처를 표시하면 본문 인용 가능 |
| 공개 평가 데이터셋 | 불가. id·URL 목록만 | 가능. 출처 표시 "출처: 대한민국 정책브리핑(www.korea.kr), 공공누리 제1유형"과 원문 URL을 행마다 붙인다. 사진·이미지 제외 |
| 데모·API 응답 | 기사 제목, 언론사, 원문 링크, 우리가 생성한 뉴스레터까지 | 같음 |
| 외부 LLM API 입력 | 생성·판정 처리 목적에 한해 보낸다. 호출 메트릭 로그에는 토큰 수·지연만 남기고 본문은 남기지 않는다(`core/llm_metrics.py`) | 같음 |

### 정책브리핑 수집 (`ai_workspace/crawler/policy_briefing.py`)
- 인증키는 환경변수 `DATA_GO_KR_SERVICE_KEY`로 받는다(포털의 "일반 인증키(Decoding)"). 키가 없으면 수집을 건너뛰고 에러로 취급하지 않는다. 키는 로그와 예외 메시지에 남기지 않는다. 네트워크 예외 메시지에는 키가 든 URL이 들어 있어서 예외 타입 이름만 남기고 `from None`으로 원래 예외를 끊는다.
- 오늘 포함 최근 N일을 3일 단위 창으로 나눠 호출한다. 네트워크 오류와 5xx만 지수 백오프로 재시도한다. API 에러 코드(인증키, 날짜 범위, 날짜 형식)는 다시 호출해도 같은 결과라 바로 `PolicyBriefingAPIError`로 올린다.
- 저장 조건: `KoglType`이 제1유형, 원문 URL 있음, 엠바고 해제, 정제 후 본문이 기존 저품질 필터(`is_drop_article`, 350자 등)를 통과.
  - `KoglType` 표기 형식이 명세에 없다. 그래서 숫자가 정확히 하나 들어 있을 때만 그 유형으로 인정하고, 비었거나 애매하면 제외한다.
- HTML 본문 정제:
  - 이미지, figure, figcaption, 스크립트를 지운다. 사진·이미지는 공공누리 대상이 아니다.
  - 블록 요소 경계에만 공백을 넣는다. 인라인 태그 경계에 공백을 넣으면 "정부는" 같은 어절이 쪼개진다.
  - 결과는 다른 언론사 본문과 같은 형태(모든 공백을 한 칸으로)다.
- 본문을 API가 주므로 본문 추출(Stage2)을 거치지 않고 `news_raw`에 바로 넣는다. press 행 `정책브리핑`은 시드에 추가했고, 기존 DB에서는 수집 시 get-or-create로 만든다.
- INSERT는 스키마를 보고 만든다. 수집 1회마다 `information_schema.columns`를 한 번 조회한다.
  - `main` 스키마: `ON CONFLICT (raw_news_url) DO NOTHING`. 재실행해도 안전하다.
  - 수집 런타임 브랜치(PR #8)의 컬럼이 있으면 `raw_news_extract_status='ok'`, `raw_news_extracted_at=now()`, `raw_news_content_sha256`(UTF-8 본문의 sha256 hex, 마이그레이션 `d48994e9d26e`와 같은 식)을 함께 넣는다. 그 브랜치의 본문 추출기는 상태가 비어 있는 행을 원문 페이지에서 다시 내려받아 본문을 덮어쓰는데, 이렇게 넣은 행은 대상이 아니다. 따라서 API 본문(사진 캡션이 빠진 공공누리 텍스트)이 웹 추출 결과로 바뀌지 않는다.
  - 그 스키마에는 `'ok'` 행끼리 본문 해시가 유일해야 하는 부분 unique 인덱스가 있다. 대상을 `raw_news_url`로 좁히면 본문이 같은 기사 한 건 때문에 배치 전체가 롤백된다. 그래서 대상 없는 `ON CONFLICT DO NOTHING`을 쓴다. URL이나 본문이 이미 있는 행은 `skipped`로 센다.
- `Stage1_RSSCollection`이 RSS 다음에 호출한다. 이 출처가 실패해도 RSS 결과는 버리지 않고 에러만 기록한다.

### 30일 본문 보존
상업 언론사 본문은 `raw_news_crawled_at` 기준 30일이 지나면 비운다. 정책브리핑(재배포 가능 출처)은 대상이 아니다. 구현은 아래 TODO 설계대로 한다.

## 증거
- 공식 페이지 확인(모두 접근일 2026-09-26): 위 컨텍스트의 korea.kr 공지·저작권정책, data.go.kr 데이터셋 페이지(swagger 명세 포함), 공공누리 제1유형 안내.
- 엔드포인트 생존 확인(인증키 없이 호출해 응답 코드만 확인, 2026-09-26):
  - `policyNewsService2/policyNewsList2`: 401 `SERVICE_KEY_IS_NULL`
  - 없는 경로: 400 `NO_OPENAPI_SERVICE_ERROR`
  - korea.kr `/rss/*.xml` 5개 주소: 전부 404
- RSS 채널 저작권 표시: 2026-09-26에 `feedparser`로 8개 피드의 채널 `copyright` 요소를 읽었다(값은 컨텍스트 절).
- 본문 저장량: 컨텍스트 절 표. 로컬 compose DB에서 `default_transaction_read_only=on` 연결의 SELECT로 집계했다. 본문 텍스트는 출력하지 않았다.
- 테스트:
  - `tests/test_policy_briefing_source.py` 29건. 파싱, 성공 코드 표기 3가지, 두 종류의 에러 봉투, 날짜 창, 공공누리 유형 정규화, HTML 정제, 선별 통계, 키 비노출, 5xx 재시도, 키 없을 때 no-op, INSERT, 스키마별 INSERT 컬럼(main / 수집 런타임), Stage1 연결을 확인한다. XML은 명세 필드 이름으로 만든 합성 응답이다.
  - `tests/test_source_licenses.py` 4건.
  - `tests/integration/test_policy_briefing_insert.py`
    - 1건: 실제 `news_raw`/`press` 스키마에서 본문 저장과 재실행 멱등성을 확인한다. 이 작업 전용 임시 pgvector 컨테이너에서 `alembic upgrade head` 후 통합 테스트 16건 전부 통과(수집 중인 compose DB는 건드리지 않음). CI integration 잡에서도 돈다.
    - 1건: 수집 런타임 브랜치의 컬럼 4개와 부분 unique 인덱스를 테스트 동안만 만든다. 그 상태에서 상태 `ok`·추출 시각·해시(SQL 식과 일치)가 채워지는지, 본문이 같은 두 번째 URL이 배치를 롤백시키지 않고 건너뛰어지는지 확인한다. 로컬에서는 돌리지 않았고 CI integration 잡에서 돈다.

## 결과와 한계
- **정책브리핑은 아직 한 건도 수집하지 않았다.**
  - 인증키가 필요하다. 사용자가 공공데이터포털에서 데이터셋 15095335를 활용신청(자동승인)하고 `DATA_GO_KR_SERVICE_KEY`에 넣어야 한다.
  - 첫 실행 전까지 다음 네 가지는 추정이다: 날짜 파라미터 형식(YYYYMMDD), 성공 `resultCode` 값(숫자가 모두 0이면 성공으로 처리), `KoglType` 표기, 날짜 창 경계(3일을 양끝 포함으로 해석).
  - 틀리면 97/98 에러나 "제1유형 0건" 통계로 바로 드러난다.
  - 정책뉴스는 정부 발표 위주다. 상업 언론 기사와 주제·문체 분포가 다르다. 공개 부분집합에서 얻은 수치를 전체 한국어 뉴스 성능으로 일반화하지 않는다. 이 부분집합은 "재현 가능한 공개 평가"용이다.
- **30일 보존의 영향**
  - 수집일로부터 30일이 지난 상업 언론 기사로는 생성·판정을 다시 돌릴 수 없다.
  - 사전 등록 평가는 표본 추출 후 30일 안에 끝내야 한다. 더 오래 재현해야 하는 평가는 정책브리핑 부분집합으로 만든다.
  - 예외(평가 표본 보존 연장)는 두지 않았다. 필요해지면 기한과 대상을 이 ADR에 추가하는 방식으로만 연다.
- **저장량 추정**(가정 포함)
  - 본문 평균 4,050 bytes(실측) × 하루 유입 R건 × 30일 = 30일 보존분.
  - R=300~600(가정, 정상 상태 일일 유입 미측정)이면 36~73MB다. 무기한 보존이면 연 0.44~0.89GB가 된다(본문만, TOAST 압축 전).
  - `job_runs`에 7일치가 쌓이면 R을 실측값으로 바꾼다.
- **남는 위험**
  - 임베딩을 남긴다. 임베딩에서 원문을 일부 복원하는 역변환 공격이 연구돼 있으므로 임베딩도 공개하지 않는다.
  - 제목은 기한 없이 남긴다. 표시할 때는 원문 링크를 함께 둔다.
  - 생성 뉴스레터가 원문 문장을 얼마나 그대로 옮기는지는 측정하지 않았다. 생성 프롬프트와 게이트에 인용 길이 상한이 아직 없다. 문장별 출처를 다는 생성 방식(ADR 0021 예정)에서 다룬다.
  - 백업을 만들면 백업도 30일 보존을 따라야 한다. 지금은 DB 백업 절차가 없다.
  - 외부 LLM 프로바이더가 입력을 어떻게 쓰는지(보관·학습 여부)는 이 ADR에서 확인하지 않았다.

### TODO: 본문 보존 잡 (구현 대기)
**지금 넣지 않은 이유**
- `main`의 `news_raw`에는 본문 추출 상태·본문 해시 컬럼이 없다. 두 컬럼은 수집 런타임 브랜치의 마이그레이션 `f87f7378672e`, `d48994e9d26e`가 추가하고, 수집 중인 로컬 DB에는 이미 적용돼 있다.
- `main`의 본문 추출기는 `raw_news_content IS NULL OR = ''`인 행을 다시 내려받는다. 이 상태에서 본문을 비우면 30일 지난 기사를 매번 다시 받는 루프가 생긴다.
- `main`에서 따로 해시 컬럼을 추가하면 두 가지 문제가 생긴다. 같은 컬럼을 두 번 만들게 되고, Alembic head가 여러 개가 된다.
- 그래서 그 브랜치가 병합된 뒤 다음을 넣는다.

1. 마이그레이션(`down_revision` = 병합 후 head)
   - `raw_news_content`의 NOT NULL을 푼다. 실측: 현재 NOT NULL이다.
   - `raw_news_content_length integer NULL`, `raw_news_content_purged_at timestamptz NULL`을 추가한다.
   - 추출 상태 CHECK 제약은 바꾸지 않는다. 비운 행은 상태 `ok`를 유지하고 `purged_at`으로 구분한다. 부분 unique 인덱스 `uq_news_raw_content_sha256_ok`도 그대로 동작한다.
2. 잡 `jobs/tasks/retention.py` (`python -m jobs.run retention --days 30`, 하루 1회). 5,000행 배치로 반복하며 한 번에 실행하는 문장은 다음과 같다.
   ```sql
   UPDATE news_raw n
   SET raw_news_content_sha256 = COALESCE(
           n.raw_news_content_sha256,
           CASE WHEN n.raw_news_content <> ''
                THEN encode(sha256(convert_to(n.raw_news_content, 'UTF8')), 'hex') END),
       raw_news_content_length = char_length(n.raw_news_content),
       raw_news_content = NULL,
       raw_news_content_purged_at = now()
   FROM press p
   WHERE p.press_id = n.press_id
     AND n.raw_news_content IS NOT NULL
     AND n.raw_news_crawled_at < now() - make_interval(days => %(days)s)
     AND p.press_name <> ALL(%(redistributable_presses)s)   -- config.sources에서 재배포 가능 출처
   ```
   - 해시는 추출기가 저장한 값(`content_sha256(cleaned)`)을 우선 쓴다. 없을 때만 SQL로 계산하는데, 같은 UTF-8 sha256이라 값이 같다.
   - 멱등이다. 두 번째 실행은 `raw_news_content IS NOT NULL` 조건에 걸려 0행을 갱신한다.
   - 갱신 건수와 비운 바이트 수를 `job_runs.stats`에 남긴다.
3. 본문을 읽는 곳의 확인
   - 임베딩·클러스터링 쿼리는 이미 `IS NOT NULL AND <> ''`로 거른다.
   - 평가셋 추출기(`evaluation/llm/evalset.py`, LLM 평가 브랜치)는 본문이 NULL인 기사를 만나면 표본에서 빼고 그 수를 보고해야 한다.
4. 통합 테스트: 다음 행을 두고 잡을 두 번 실행한다.
   - 상업 언론 31일 전: 비워지고, 해시·길이가 남는다.
   - 상업 언론 29일 전: 그대로다.
   - 정책브리핑 31일 전: 그대로다.
   - `dropped` 상태의 빈 본문: NULL이 되고 해시는 NULL이다.
   - 두 번째 실행: 0행을 갱신한다.

### 병합 시 확인할 것
- (코드로 처리함) 정책브리핑 행의 추출 상태·시각·해시는 위 "스키마를 보고 만드는 INSERT"가 채운다. 병합 후에는 CI의 `alembic upgrade head`에 컬럼이 이미 있으므로, 통합 테스트는 컬럼을 새로 만들지 않고 그대로 확인한다.
- (병합하는 쪽이 할 일) 그 브랜치의 `jobs.run ingest`는 `collect_rss`를 직접 부른다. 따라서 `jobs/tasks/ingest.py`의 rss 단계 뒤에 `collect_policy_briefing()`을 따로 연결해야 한다. `Stage1_RSSCollection`처럼 예외를 잡아 `ctx.stats["policy_briefing"]`에 에러만 남기고, RSS 결과는 버리지 않는다. 지금의 연결은 `main.py` 경로(`Stage1_RSSCollection`)에만 적용된다. 이 브랜치는 `main` 기준이라 그 파일이 없어서 여기서 고칠 수 없다.
