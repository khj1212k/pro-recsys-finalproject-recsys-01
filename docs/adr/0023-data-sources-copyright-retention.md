# ADR 0023: 수집 출처와 라이선스, 저장·노출·공개 범위, 본문 보존 기한

## 상태
채택됨 (2026-09-26). 출처 표·정책브리핑 수집기·노출 규칙은 이 ADR과 함께 들어갔다. 30일 본문 보존 잡(`python -m jobs.run retention`)은 2026-10-17에 구현했다 - 맨 아래 "개정 (2026-10-17)". Tier 0 VM에는 아직 배포하지 않았다(병합 뒤 별도 단계, 첫 만료는 2026-10-26 02:36 UTC). 처음의 설계는 "결과와 한계"의 "본문 보존 잡 설계" 절에 그대로 둔다.

추천 평가·요청 시점 추천·시뮬레이터 브랜치 위에 다시 쌓으면서(2026-10-06) 결정은 바꾸지 않았다. 그때 달라진 Alembic head, 옮기지 않은 커밋, 테스트 수는 맨 아래 "통합 기록"에 있다.

**2026-10-06 개정.** LLM 실험이 DB 밖에 만드는 본문 사본(반출 파일, 제3자 연산의 디스크, 라벨링 입력, 덤프)이 놓일 수 있는 곳과 지우는 규칙을 더했다. 30일은 늘리지 않았다. 내용은 맨 아래 "개정 (2026-10-06)" 절에 있고, 본문의 결정·표는 고치지 않았다. 실행 계획 2.6절 밖에서 더한 위치 세 곳은 그 절 안에 따로 표시했고 별도 승인 대상이다(2026-10-17 리뷰 반영).

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
- 저장 조건: 기사 상태(`ContentsStatus`)가 등록·수정, `KoglType`이 제1유형, 원문 URL 있음, 엠바고 해제, 정제 후 본문이 기존 저품질 필터(`is_drop_article`, 350자 등)를 통과.
  - `ContentsStatus`는 필드 이름만 명세에서 확인했고 값의 뜻은 실제 응답으로 확인하지 못했다. I(등록)·U(수정)만 저장한다. D(삭제)는 `withdrawn`, 그 밖의 값이나 빈 값은 `unknown_status`로 세고 저장하지 않는다(fail closed).
  - `KoglType` 표기 형식이 명세에 없다. 그래서 숫자가 정확히 하나 들어 있을 때만 그 유형으로 인정하고, 비었거나 애매하면 제외한다.
- HTML 본문 정제:
  - 이미지, figure, figcaption, 스크립트를 지운다. 사진·이미지는 공공누리 대상이 아니다.
  - 블록 요소 경계에만 공백을 넣는다. 인라인 태그 경계에 공백을 넣으면 "정부는" 같은 어절이 쪼개진다.
  - 결과는 다른 언론사 본문과 같은 형태(모든 공백을 한 칸으로)다.
- 본문을 API가 주므로 본문 추출(Stage2)을 거치지 않고 `news_raw`에 바로 넣는다. press 행 `정책브리핑`은 시드에 추가했고, 기존 DB에서는 수집 시 get-or-create로 만든다.
- INSERT는 스키마를 보고 만든다. 수집 1회마다 `information_schema.columns`를 한 번 조회한다.
  - 수집 런타임 리비전(`f87f7378672e`·`d48994e9d26e`)이 적용된 스키마: `raw_news_extract_status='ok'`, `raw_news_extracted_at=now()`, `raw_news_content_sha256`을 함께 넣는다. 해시는 본문 추출기와 같은 함수(`content_extractor.extractor.content_sha256`, UTF-8 본문의 sha256 hex, 마이그레이션 `d48994e9d26e`의 SQL 식과 같은 값)로 만든다. 본문 추출기는 상태가 비어 있는 행을 원문 페이지에서 다시 내려받아 본문을 덮어쓰는데, 이렇게 넣은 행은 대상이 아니다. 따라서 API 본문(사진 캡션이 빠진 공공누리 텍스트)이 웹 추출 결과로 바뀌지 않는다.
  - 이 스키마에는 `'ok'` 행끼리 본문 해시가 유일해야 하는 부분 unique 인덱스가 있다. 대상을 `raw_news_url`로 좁히면 본문이 같은 기사 한 건 때문에 배치 전체가 롤백된다. 그래서 대상 없는 `ON CONFLICT DO NOTHING`을 쓴다. URL이나 본문이 이미 있는 행은 `skipped`로 센다.
  - 아직 그 리비전으로 올리지 않은 DB(`e725a62ffef1`까지): 기본 6개 컬럼만 넣고 `ON CONFLICT (raw_news_url) DO NOTHING`. 재실행해도 안전하다.
- 호출 위치
  - compose 런타임: `jobs.run ingest`의 rss 단계가 RSS 다음에 부른다(`jobs/tasks/ingest.py`). 스케줄러의 `--stages rss,extract`에 그대로 포함된다. 인증키는 compose `.env`의 `DATA_GO_KR_SERVICE_KEY`를 워커 환경으로 넘긴다(LLM 키 파일 `AI_ENV_FILE`과 별개).
  - `main.py` 경로: `Stage1_RSSCollection`이 RSS 다음에 부른다.
  - 두 곳 모두 이 출처가 실패해도 RSS 결과는 버리지 않는다. 에러는 stats(`policy_briefing`)에 남기고, ingest 잡은 경고를 하나 남긴다.

### 30일 본문 보존
상업 언론사 본문은 `raw_news_crawled_at` 기준 30일이 지나면 비운다. 정책브리핑(재배포 가능 출처)은 대상이 아니다. 구현은 아래 설계를 따르고, 설계와 달라진 점은 "개정 (2026-10-17)"에 있다.

## 증거
- 공식 페이지 확인(모두 접근일 2026-09-26): 위 컨텍스트의 korea.kr 공지·저작권정책, data.go.kr 데이터셋 페이지(swagger 명세 포함), 공공누리 제1유형 안내.
- 엔드포인트 생존 확인(인증키 없이 호출해 응답 코드만 확인, 2026-09-26):
  - `policyNewsService2/policyNewsList2`: 401 `SERVICE_KEY_IS_NULL`
  - 없는 경로: 400 `NO_OPENAPI_SERVICE_ERROR`
  - korea.kr `/rss/*.xml` 5개 주소: 전부 404
- RSS 채널 저작권 표시: 2026-09-26에 `feedparser`로 8개 피드의 채널 `copyright` 요소를 읽었다(값은 컨텍스트 절).
- 본문 저장량: 컨텍스트 절 표. 로컬 compose DB에서 `default_transaction_read_only=on` 연결의 SELECT로 집계했다. 본문 텍스트는 출력하지 않았다.
- 테스트:
  - `tests/test_policy_briefing_source.py` 34건. 파싱, 성공 코드 표기 3가지, 두 종류의 에러 봉투, XML이 아닌 응답, 날짜 창, 공공누리 유형 정규화, HTML 정제, 선별 통계(삭제·상태 불명 기사 제외 포함), 키 비노출, 5xx 재시도, 키 없을 때 no-op, INSERT, 스키마별 INSERT 컬럼(마이그레이션 전 / 현재 head), Stage1 연결을 확인한다. XML은 명세 필드 이름으로 만든 합성 응답이다.
  - `tests/test_jobs_tasks.py`의 ingest 3건: rss 단계가 정책브리핑 결과를 stats에 남김, 정책브리핑 실패 시 RSS 결과 유지 + 경고 1건, 키가 없을 때 경고 없음.
  - `tests/test_source_licenses.py` 5건: 설정된 모든 RSS 출처에 판정이 있음, 상업 언론사는 재배포 불가, 모르는 출처는 fail closed, 정책브리핑 수집기의 출처는 재배포 가능, 재배포 가능 출처에는 모두 출처 표시 문구가 있음.
  - `tests/integration/test_policy_briefing_insert.py`
    - 1건: 실제 `news_raw`/`press` 스키마에서 본문 저장과 재실행 멱등성을 확인한다. 처음 작성할 때(head `e725a62ffef1`) 이 작업 전용 임시 pgvector 컨테이너에서 `alembic upgrade head` 후 통합 테스트 16건 전부 통과(수집 중인 compose DB는 건드리지 않음).
    - 1건: 현재 head에 추출 상태·해시 컬럼과 부분 unique 인덱스가 있는지 먼저 확인한다. 그다음 상태 `ok`·추출 시각·해시(SQL 식과 일치)가 채워지는지, 본문이 같은 두 번째 URL이 배치를 롤백시키지 않고 건너뛰어지는지 본다.
    - `main` 병합 뒤의 두 건은 로컬에서 돌리지 않았다(로컬 DB를 띄우지 않음). CI integration 잡(`alembic upgrade head` 후 실행)에서 확인한다.

## 결과와 한계
- **정책브리핑은 아직 한 건도 수집하지 않았다.**
  - 인증키가 필요하다. 사용자가 공공데이터포털에서 데이터셋 15095335를 활용신청(자동승인)하고 `DATA_GO_KR_SERVICE_KEY`에 넣어야 한다.
  - 첫 실행 전까지 다음 다섯 가지는 추정이다: 날짜 파라미터 형식(YYYYMMDD), 성공 `resultCode` 값(숫자가 모두 0이면 성공으로 처리), `KoglType` 표기, `ContentsStatus` 값(I/U/D), 날짜 창 경계(3일을 양끝 포함으로 해석).
  - 틀리면 97/98 에러나 "제1유형 0건"·`unknown_status` 통계로 바로 드러난다.
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
  - 정책브리핑의 나중 수정·삭제를 이미 저장한 행에 반영하지 않는다. 처음 본 상태로 넣고 이후에는 `ON CONFLICT DO NOTHING`으로 건너뛴다. 새로 받는 삭제(D) 기사만 저장하지 않는다. 저장한 행을 삭제 표시에 맞춰 지우는 동작은 넣지 않았다. 처음 작성할 때의 이유는 "본문을 비우면 본문 추출기가 원문 페이지를 다시 내려받는다"였다. `main` 병합 후 추출기는 추출 상태로 대상을 고르므로 이 이유는 없어졌다. 남은 이유는 두 가지다. 삭제·수정된 기사가 API에서 어떻게 보이는지(같은 `NewsItemId`가 뒤의 날짜 창에 D로 다시 나오는지)를 인증키 없이 확인할 수 없다. 그리고 `raw_news_content`가 아직 NOT NULL이다. 이 DB는 로컬에만 있으므로, 위험이 실제로 생기는 지점은 공개 반출이다. 그래서 공개 평가 부분집합을 반출하는 단계는 반출 직전에 API로 각 기사의 `ContentsStatus`·`KoglType`을 다시 조회하고, 삭제됐거나 제1유형이 아니게 된 기사를 뺀다. 반출 스크립트는 아직 없고, 만들 때 이 조건을 넣는다.
  - 제목은 기한 없이 남긴다. 표시할 때는 원문 링크를 함께 둔다.
  - 정책브리핑 기사별 공공누리 유형(`KoglType`)은 수집할 때 거르기만 하고 저장하지 않는다. `news_raw` 행이 제1유형이라는 사실과 이어지는 연결은 press 이름 `정책브리핑`뿐이다. 그래서 수집 당시의 행별 라이선스 상태를 나중에 증명할 수 없다. API에서 어떤 기사의 유형이 바뀌어도 저장된 행은 계속 제1유형으로 취급된다. 보완 계획은 두 가지다. 첫째, 아래 보존 마이그레이션에 `raw_news_kogl_type smallint NULL` 컬럼을 함께 넣는다. 그러면 수집기의 스키마별 INSERT가 같은 방식으로 이 컬럼을 채운다. 둘째, 공개 반출 단계는 행마다 `config.sources`의 출처 표시를 찍고, 반출 직전 API 재조회로 유형을 다시 확인한다.
  - 생성 뉴스레터가 원문 문장을 얼마나 그대로 옮기는지는 측정하지 않았다. 생성 프롬프트와 게이트에 인용 길이 상한이 아직 없다. 문장별 출처를 다는 생성 방식을 정할 때 다룬다(후속 ADR, 번호 미정).
  - 백업을 만들면 백업도 30일 보존을 따라야 한다. 지금은 DB 백업 절차가 없다.
  - 외부 LLM 프로바이더가 입력을 어떻게 쓰는지(보관·학습 여부)는 이 ADR에서 확인하지 않았다.

### 본문 보존 잡 설계 (2026-09-26 TODO, 2026-10-17 구현 - "개정 (2026-10-17)")
**처음 작성할 때 넣지 않은 이유** (2026-09-26, 이 ADR의 기준이 `9aa235e`였을 때)
- 그때 `main`의 `news_raw`에는 본문 추출 상태·본문 해시 컬럼이 없었다. 두 컬럼은 수집 런타임 PR(#8)의 마이그레이션 `f87f7378672e`, `d48994e9d26e`가 추가한다.
- 그때 `main`의 본문 추출기는 `raw_news_content IS NULL OR = ''`인 행을 다시 내려받았다. 본문을 비우면 30일 지난 기사를 매번 다시 받는 루프가 생긴다.
- `main`에서 따로 해시 컬럼을 추가하면 같은 컬럼을 두 번 만들게 되고, Alembic head가 여러 개가 된다.

**지금 상태**: PR #8이 2026-09-26 `main`에 병합돼 위 세 가지 선행 조건은 풀렸다. 추출기는 추출 상태로 대상을 고른다(`WHERE raw_news_extract_status IS NULL OR 재시도 여지`). Alembic head는 그때 `d48994e9d26e` 하나였고, 요청 시점 추천 브랜치(ADR 0015) 위에 쌓은 지금은 `8b7f830013b7` 하나다. 그래도 이 브랜치에서는 잡을 넣지 않았다. 켜는 순간 본문을 지우기 시작하는 잡이라서, 켜는 시점(로컬 수집 시작 2026-09-25 기준 첫 삭제는 2026-10-25 무렵)과 평가 표본 일정을 사용자가 정한 다음에 넣는다. 다음 작업으로 아래를 넣는다.

1. 마이그레이션(`down_revision` = 넣을 때의 head. 이 브랜치가 쌓인 순서대로 병합되면 `8b7f830013b7`)
   - `raw_news_content`의 NOT NULL을 푼다. 실측: 현재 NOT NULL이다.
   - `raw_news_content_length integer NULL`, `raw_news_content_purged_at timestamptz NULL`을 추가한다.
   - 정책브리핑 행의 라이선스 상태를 남기려고 `raw_news_kogl_type smallint NULL`도 추가한다("남는 위험" 참고).
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
   - 평가셋 추출기(`evaluation/llm/evalset.py`)는 지금 NULL 본문을 빈 문자열로 바꿔 그대로 쓴다. 본문이 NULL인 기사는 표본에서 빼고 그 수를 보고하도록 고쳐야 한다.
   - 요청 시점 추천(`backend/app/recsys`), 시뮬레이터(`sim/`), 배치 잡(`jobs/`)은 `raw_news_content`를 읽지 않는다. 뉴스레터와 임베딩만 쓴다(2026-10-06 `git grep raw_news_content`로 확인. `backend/app`에서는 모델 정의에만 나온다).
4. 통합 테스트: 다음 행을 두고 잡을 두 번 실행한다.
   - 상업 언론 31일 전: 비워지고, 해시·길이가 남는다.
   - 상업 언론 29일 전: 그대로다.
   - 정책브리핑 31일 전: 그대로다.
   - `dropped` 상태의 빈 본문: NULL이 되고 해시는 NULL이다.
   - 두 번째 실행: 0행을 갱신한다.

### 수집 런타임(PR #8) 병합 후 처리한 것
- 정책브리핑 행의 추출 상태·시각·해시는 위 "스키마를 보고 만드는 INSERT"가 채운다. 통합 테스트는 head에 있는 컬럼·인덱스를 그대로 쓴다.
- `jobs.run ingest`의 rss 단계에 정책브리핑 수집을 연결하고, compose 워커에 인증키 환경변수를 넘겼다(위 "호출 위치").

## 통합 기록 (2026-10-06, 결정 변경 없음)
이 브랜치의 커밋을 시뮬레이터 브랜치(`feat/user-simulator-loadtest`) 위에 순서대로 다시 쌓았다. 그 아래에는 ADR 0007·0013·0015·0017·0019 브랜치가 있다.

1. **옮긴 커밋과 옮기지 않은 커밋.** 원래 브랜치는 `9aa235e`에서 갈라져 중간에 `main`(`dad8da2`)을 병합한 이력이었다(병합 커밋 `c561b0b`). 병합 커밋은 옮기지 않고 이 브랜치 고유의 커밋 27개를 순서대로 얹었다. 그중 2개는 얹으면 내용이 비어서 뺐고 25개가 남았다.
   - `847c8bf`(죽은 설정 `MIN_NEWSLETTER_SCORE` 삭제, `MIN_CLUSTER_CONFIDENCE`에 미사용 표시): `main`의 PR #9(`7267029`)가 같은 삭제와 표시를 이미 했다.
   - `6c90d87`(`MIN_CLUSTER_CONFIDENCE` 삭제): ADR 0009가 이 설정을 ClusterEvaluator confidence ROC 결과에 따라 게이트로 연결하는 규칙을 사전 등록했다. 사전 등록된 규칙이 가리키는 설정이라 지우지 않는다. 원래 브랜치의 병합 커밋도 같은 이유로 `main` 쪽을 남겼다.
   - 그래서 `ai_workspace/config/settings.py`는 이 브랜치에서 바뀌지 않는다.
2. **충돌 해소.** `docs/adr/README.md`는 앞선 브랜치의 행(0007, 0013, 0015, 0017, 0019)과 이 ADR의 행을 번호순으로 합쳤다. ADR 0003의 한계 절은 앞선 브랜치가 더한 두 항목(조기 종료 아티팩트, lambdarank 근거의 한계)을 그대로 두고 상수 피처 항목만 이 브랜치의 문장으로 바꿨다. `.gitignore`와 `tests/test_jobs_tasks.py`는 자동으로 합쳐졌다.
3. **Alembic.** 이 브랜치는 리비전을 더하지 않는다. head는 앞선 브랜치의 `8b7f830013b7` 하나다(`backend/`에서 오프라인 `alembic heads`). 그 리비전은 `news_raw`를 바꾸지 않으므로, 정책브리핑 INSERT가 보는 컬럼과 부분 unique 인덱스는 `f87f7378672e`·`d48994e9d26e`가 만든 그대로다.
4. **수집 경로.** 다시 쌓은 뒤에도 `jobs/tasks/ingest.py`의 rss 단계가 RSS 결과를 stats에 넣은 다음 정책브리핑 수집을 예외를 잡는 함수로 부른다. INSERT는 추출 상태 `ok`·추출 시각·본문 해시를 채운다. 앞선 브랜치는 `jobs/tasks/ingest.py`와 본문 추출기(`crawler/content_extractor/`)를 건드리지 않았다.
5. **테스트.** 로컬(macOS, Python 3.11, CI `test` 잡과 같은 설치 순서, `nice -n 19`, 스레드 2)에서 저장소 전체 단위 테스트는 924 passed, 18 skipped다. 건너뛴 것은 EB-NeRD demo 데이터 없음 15, Linux 전용 1, 배치 전용 계약 테스트 1, Locust 스모크 1이다. 앞선 브랜치 끝의 873건에 이 브랜치의 51건이 더해졌다: `test_policy_briefing_source.py` 34, `test_source_licenses.py` 5, `test_tone_converter_summary_key.py` 7, `test_feature_columns_contract.py` 2, `test_jobs_tasks.py`의 ingest 3.
   - 재실행 대기(CI): `pytest -q -m integration tests/`. 수집되는 52건 가운데 이 브랜치의 것은 `tests/integration/test_policy_briefing_insert.py` 2건이고, `8b7f830013b7`까지 올린 스키마에서 돈다. 로컬에서는 DB를 띄우지 않아 돌리지 않았다.
6. **앞선 브랜치의 재현 리포트.** 상수 피처 2개 제거는 팀 베이스라인 재현 하네스가 실행하는 current 엔진 코드를 바꾼다. 그 영향과 표본 대조 결과는 ADR 0007의 증거 "옮긴 뒤의 출처"에 적었다.
7. **없는 ADR 번호 참조.** "남는 위험"의 문장별 출처 항목이 아직 없는 ADR을 번호로 가리키고 있어 번호를 뺐다. 그 번호의 파일이 없고 번호 배정도 정해지지 않았다.

## 개정 (2026-10-06) — 실험용 DB 밖 본문 사본: 허용 위치와 삭제 규칙 (30일은 늘리지 않는다)

**효력.** main에 병합된 때부터다. 2026-10-06 현재 이 개정이 다루는 반출본·라벨링 입력·실험 산출물은 하나도 없다 — 메인 체크아웃의 `data/`에는 `backups/`·`benchmarks/`·`team_archive/`뿐이고 `data/exports/`·`data/labels/`·`data/evalsets/`·`data/experiments/`는 없다(디렉터리 목록으로 확인, 파일 내용은 읽지 않았다). 병합 전에는 반출하지 않는다([ADR 0009](0009-llm-eval-protocol-and-preregistered-decision-rule.md) A8.0의 2단계).

**왜 필요한가.** 위 "저장·노출·공개 범위" 표는 상업 언론사 본문이 있을 수 있는 곳을 DB(수집 후 30일)와 외부 LLM API 입력으로만 열어 두었고, "결과와 한계"에 "백업을 만들면 백업도 30일 보존을 따라야 한다", "예외는 이 ADR에 추가하는 방식으로만 연다"고 적었다. LLM 실험의 데이터 경로([ADR 0036](0036-experiment-data-path-frozen-export-file-stand-ins.md))는 운영 DB에 쓰지 않으려고 실험 창의 본문을 파일로 반출하고, 임베딩을 계산하려고 그 파일을 제3자 연산(Colab)에 잠깐 올린다. 호스트 밖 백업([런북](../runbook-hosting.md) 6.5)은 DB 전체의 본문을 Mac에 둔다. 표에 없는 사본들이다.

**바꾸지 않는 것.**
- **30일.** 사본의 기한은 원본과 같다 — 행마다 `raw_news_crawled_at` + 30일. 사본을 만든 날부터 다시 세지 않는다. 평가 표본을 위한 연장 예외는 여전히 없다.
- **공개 범위.** 저장소·리포트·로그·표준출력에 실을 수 있는 것은 여전히 기사 id, URL, sha256, 건수·길이뿐이다.
- **대상.** 재배포 가능한 출처(정책브리핑, 공공누리 제1유형)의 본문에는 기한이 없다. 표에 없는 출처는 재배포 불가로 본다(fail closed).

### 허용 위치와 위치별 삭제 규칙
"Mac"은 개발 Mac의 **메인 체크아웃**(`data/`는 `.gitignore`의 `/data/`로 추적되지 않는다)을 뜻한다. 워크트리의 `data/`는 허용 위치가 아니다 — 워크트리는 지워지는 디렉터리라 그 안의 사본은 정리 대상에서 빠진 채 잊힌다.

| 위치 | 무엇이 | 목적 | 삭제 시점 | 수단 | 구현 상태 (2026-10-06) |
|---|---|---|---|---|---|
| Mac `data/exports/<T0>/articles.jsonl` | 실험 창의 본문(창은 최대 120시간) | 클러스터링·생성의 입력 | 행마다 `crawled_at` + 30일. 그 반출본으로 할 일(라벨 리포트 커밋)이 끝나면 그 전에라도 통째로 | 매니페스트에 만료일. 반출 시점에 이미 만료된 본문은 내보내지 않는다(창의 시작 + 30일이 지났으면 DB에 닿기 전에, 서버 스냅숏 시각으로 행마다 한 번 더). 본문을 쓰는 함수는 메인 체크아웃의 `data/exports` 밖(이름만 같은 다른 곳 포함)에는 쓰지 않는다. 반출본을 여는 함수는 만료된 본문을 돌려주지 않고, 열 때마다 디스크에서도 지운다(해시·길이·제목·URL은 남긴다). Mac에는 상주 작업을 둘 수 없으므로 주기 잡이 아니라 "열 때마다 정리"이고, 손으로 돌리는 `purge --all`이 있다 | **이 개정과 함께 들어감** — `evaluation/llm/frozen_export.py`, `tests/evaluation/test_frozen_export.py` |
| Colab VM 디스크(제3자 연산) | 위 파일의 사본 | **BGE-M3 임베딩 계산에 한해.** 생성·판정·라벨링을 Colab에서 하지 않는다 | 임베딩 잡이 끝나는 즉시, 같은 세션 안에서 | 잡의 마지막 단계가 본문 파일을 지우고 남은 파일 목록(이름·크기만)을 로그에 남긴 뒤 세션을 끝낸다. Drive 마운트·노트북 출력·잡 로그에 본문을 남기지 않는다. 돌려받는 것(임베딩 팩)에는 본문이 없고 해시만 있다 | **없음.** 임베딩 잡이 아직 없다. 잡을 넣는 PR이 삭제 단계와 그 테스트를 함께 넣는다. 그 전에는 Colab에 본문을 올리지 않는다 |
| Mac `data/labels/<run>/clusters.jsonl`, `data/evalsets/<이름>/articles.jsonl` | 표본 클러스터의 기사 본문(지금의 도구는 본문 전체를 쓴다) | 라벨링 UI의 입력 | 표본 중 가장 오래된 기사의 `crawled_at` + 30일(= 라벨 마감) | 내보내기가 만료일을 함께 쓰고, 라벨링 UI가 만료 뒤에는 열지 않으며, 위와 같은 정리 함수로 지운다 | **없음.** `evaluation/llm/evalset.py`·`bakeoff.py export-blind`는 만료일을 쓰지 않고 `labeling_app`은 확인하지 않는다. E0의 라벨 내보내기를 넣는 PR이 넣는다. 그 전에는 이 위치에 본문을 쓰지 않는다 |
| Mac `data/experiments/<실험>/<run>/newsletters.jsonl` | 우리가 생성한 초안·저장본(기사 본문이 아니다) | 라벨링·재분석 | 기한 없음(위 표의 "우리가 생성한 뉴스레터") | 저장 대역은 메인 체크아웃의 `data/experiments/<실험>/<run>` 밖이거나 git이 추적할 수 있는 경로면 디렉터리를 만들지 않는다. **공개 저장소에는 넣지 않는다** — 초안이 원문 문장을 얼마나 그대로 옮기는지 아직 재지 않았다("남는 위험") | 경로 확인은 **이 개정과 함께 들어감**(`evaluation/llm/e0_store.py`) |
| Mac `data/backups/*.dump` (`pg_dump`, 런북 6.5) | DB 전체의 본문. 덤프는 행 단위로 지울 수 없다 | VM 회수 대비 | **덤프 안에서 가장 오래된 본문의 `crawled_at` + 30일에 파일을 통째로** | 최신 1개만 둔다(새 덤프가 검증되면 앞의 것을 지운다). 파일 이름에 만료일을 적는다. 권한 0600 | **절차만**(런북 6.5). 자동화 없음 |
| 기존 덤프 2개 | 09-25~26 수집분 | — | `mac-compose-2026-09-26.dump`: **2026-10-25**(가장 오래된 행의 수집 09-25 16:50). `tier0-micro-2026-09-26T2218.dump`: **2026-10-26**(가장 오래된 행의 수집 09-26 02:36 UTC) | 사람이 지운다(사용자 결정). 임베딩 845개는 본문 없이 따로 남길 수 있다(위 표: 임베딩은 유지) | 지우지 않았다. `mac-compose-…dump`의 권한이 0644다 — 0600으로 바꾼다 |

위 표의 여섯 줄은 [실행 계획 2.6절](../design/2026-10-06-llm-v2-execution-plan.md)의 표와 같은 위치다(문구만 구현 상태에 맞게 더했다).

**구현 상태 덧붙임 (2026-10-17, E0 러너·임베딩 잡 PR).** 위 표에서 "없음"이던 두 줄의 수단이 들어갔다. 규칙은 바꾸지 않았다.
- Colab VM 디스크: `scripts/colab_embed_articles.py`가 본문을 세션에 직접 올린 반출본으로만 받고(서명 URL은 본문이 없는 모델 스냅숏에만),
  잡이 끝나면 - 완료·예산 중단·실패·입력 거절 어느 경우든 - 같은 프로세스에서 입력 파일을 지우고 남은 파일의 이름·크기를 `out/cleanup.json`과
  로그에 남긴다. 세션은 그 뒤 사람이 끝낸다. 테스트: `tests/evaluation/test_colab_embed_driver.py`.
- 라벨링 입력(`data/labels/<run>/clusters.jsonl`): E0 내보내기(`evaluation/llm/e0_labels.py`)가 `retention.json`에 마감(표본 중 가장 오래된
  기사의 `crawled_at` + 30일)을 적고, `labels.LabelStore`는 열 때 마감이 지났으면 본문을 지우며 라벨링 UI는 마감 뒤에 과제를 내주지 않는다.
  마감 기록 없이 본문이 든 입력은 UI가 열지 않는다. bake-off의 `export-blind`는 아직 마감을 쓰지 않으므로 그 입력도 UI가 열지 않는다.
  `data/evalsets/<이름>/articles.jsonl`은 E0이 쓰지 않는다(E0의 본문은 반출본에서 바로 읽는다). 테스트: `tests/evaluation/test_e0_labels.py`.

### 2.6절 밖에서 더한 위치 — 따로 승인받는다
아래 세 줄은 실행 계획 2.6절의 표에 **없다**. 기존 런북(6.4·6.5)과 bake-off 도구가 이미 쓰는 위치라서 같은 규칙 아래 두려고 적었다.
**사용자 결정 6의 기본값("2.6의 표대로")을 승인하는 것만으로는 이 세 줄이 승인되지 않는다** — PR에서 따로 묻는다.
승인되지 않으면 이 절을 지우고, 그 경우 이 세 위치에는 상업 언론사 본문 사본을 두지 않는다: 런북 6.5의 덤프 두 번째 사본과 6.4의 VM 간 CSV
이전은 쓰지 않고, bake-off 산출물은 위 표의 `data/experiments/` 아래로 옮긴다.

| 위치 | 무엇이 | 어디서 온 관행 | 삭제 시점 | 수단 | 구현 상태 (2026-10-17) |
|---|---|---|---|---|---|
| 덤프의 두 번째 사본: VM `~/backups/`, 외장 디스크 | 위 덤프와 같은 것(DB 전체의 본문) | 런북 6.5 | 원본 덤프와 같은 날 | 만료일을 `.ops/micro/README.txt`에 해시와 함께 적는다 | **절차만.** 만든 적 없다 |
| Tier 0 VM의 VM 간 이전용 CSV | 이전 대상 행의 본문 | 런북 6.4 | 이전이 끝나는 즉시 | 그 절의 정리 단계 | **절차만.** A1이 없어 쓴 적 없다 |
| Mac `data/bakeoff/<run>/` | bake-off 생성 초안·비용 기록(기사 본문이 아니다) | `evaluation/llm/bakeoff.py`(이 개정 전부터 있다) | 기한 없음(위 표의 생성 저장본과 같다) | 공개 저장소에 넣지 않는다 | 경로 확인 없음 |

### 그 밖의 곳에는 두지 않는다
- 저장소가 추적하는 경로, `reports/`, 테스트 픽스처(테스트는 지어낸 문장만 쓴다), CI 아티팩트·러너(GitHub Actions는 공개 저장소의 러너다).
- 워크트리의 `data/`, 시스템 임시 디렉터리, 이름만 `data/exports`인 다른 곳(동기화 폴더 등). 반출 도구는 출력을 메모리로 받고, 본문 파일과 그 임시 파일은
  메인 체크아웃의 `data/exports/<T0>/` 안에만 만든다(그 밖의 경로는 쓰기 전에 거절한다).
- GCS 버킷, Google Drive, 그 밖의 클라우드 저장소. Colab으로는 세션에 직접 올린다.
- Tier 0 VM의 디스크에 실험용 사본을 만들지 않는다(반출은 VM에 파일을 쓰지 않는다). VM에 놓일 수 있는 것은 위 "2.6절 밖" 표가 승인될 때의 덤프 두 번째
  사본과 VM 간 이전용 CSV(그 절의 규칙대로 끝나면 지운다)뿐이다.
- 표준출력·로그·오류 메시지·프로세스 인자. 반출 도구가 내는 것은 건수·해시·시각·언론사 이름뿐이다.
- 외부 LLM API 입력은 위 표 그대로다(생성·판정 처리 목적에 한해).

### "지운다"의 뜻과 남는 위험
- 지운다는 것은 그 파일에서 본문을 없애는 것이다(반출본은 본문을 비우고 다시 쓴다, 덤프는 파일을 지운다). APFS·SSD에서 옛 블록이 복구 불가능해지는 것까지 보장하지 않는다.
- Time Machine이나 동기화 폴더가 `data/`를 담고 있으면 그쪽 사본은 남는다. 반출 전에 `data/`가 그런 대상이 아닌지 확인한다(런북의 반출 절). 2026-10-06 현재 확인하지 않았다.
- "열 때마다 정리"는 아무도 반출본을 열지 않으면 돌지 않는다. 만료일은 매니페스트와 E0 실행 전 기록(ADR 0009 A8.10)에 적히고, 그 날짜에 `purge --all`을 돌리는 것은 사람이다.
- Colab은 제3자 연산이다. 세션 디스크가 삭제 뒤 제공자 쪽에서 어떻게 처리되는지는 확인하지 않았다. 그래서 올리는 범위를 실험 창으로, 목적을 임베딩 계산으로, 시간을 잡 하나로 묶는다.
- **보존 잡이 생기면 전체 덤프는 뜬 지 하루 안에 만료된다.** 보존 잡이 날마다 돌면 DB의 가장 오래된 본문은 늘 30일에 가깝다. 그 DB를 통째로 뜬 덤프는 위 규칙대로면 다음 날 지워야 한다. 백업 주기를 하루보다 길게 잡으면 그 사이의 덤프는 30일을 넘긴 본문을 주기만큼 더 갖고 있게 된다. 주기와 방식(본문 없는 덤프 + 최근 본문만 따로 등)은 여기서 정하지 않았다 — ADR 0026 결정 6의 재검토 항목과 함께 정한다. 덤프를 복원한 DB에서는 보존 잡을 바로 돌린다.

### Tier 0 VM의 보존 잡은 여전히 없다
이 개정은 사본의 규칙이고, 원본(VM의 `news_raw`)을 비우는 잡은 위 TODO 그대로 미구현이다. 2026-10-06 15:53 KST 조회에서 VM의 가장 오래된 행은 2026-09-26 02:36 UTC 수집분이라 **첫 삭제 대상은 2026-10-26 02:36 UTC(11:36 KST)에 생긴다.** 그때까지 잡이 없으면 VM의 DB가 이 ADR을 어긴다. VM에서 뜬 덤프의 만료일도 같은 날이다.

### 증거 (합성 데이터)
- `tests/evaluation/test_frozen_export.py`: 만료 시각 = 수집 시각 + 30일, 만료와 같은 순간부터 만료, 반출 시점에 이미 만료된 본문은 반출 거절(창의 시작으로 DB 전에, 서버 스냅숏으로 행마다), 여는 것만으로 디스크에서 본문이 사라지고 해시·길이·제목·URL은 남음, 파일을 고칠 수 없을 때도 만료된 본문은 반환되지 않음, 정리 멱등, 정리해도 반출본의 `identity_sha256` 불변, 끊긴 정리는 같은 정리를 다시 해 끝내고 손댄 파일은 거절, 재배포 가능 출처는 30일 뒤에도 유지, 메인 체크아웃 `data/exports` 밖(이름만 같은 곳, `..`로 빠져나가는 경로)·git이 추적하는 경로·`.git` 안·강제로 추적시킨 파일이 있는 경로 거절, 매니페스트 없이 본문 파일만 남은 디렉터리를 `purge --all`이 보고, 반출 도구의 표준출력·로그·요약에 본문·제목 없음.
- `tests/evaluation/test_e0_store.py`: 창 안에 본문이 지워졌거나 소스를 연 뒤 만료된 기사가 있으면 로더가 멈춤, 저장 대역이 메인 체크아웃 `data/experiments/<실험>/<run>` 밖이나 git이 추적할 수 있는 디렉터리를 만들지 않음.
- `tests/integration/test_frozen_export_db.py`(CI의 PostgreSQL): 반출 요약에 기사 텍스트 없음, 재배포 가능 출처의 만료 면제, 서버 스냅숏 시각 기준으로 이미 만료된 행의 반출 거절.
- 실제 반출본으로 확인한 것은 없다(반출 전이다).

## 개정 (2026-10-17) — 본문 보존 잡 구현 (결정은 그대로)

**효력.** main에 병합된 때부터다. 30일, 대상(상업 언론사), 면제(재배포 가능 출처), "본문만 비우고 해시·길이·제목·URL·임베딩은 남긴다"는 위 결정 그대로다. 이 개정은 구현과, 처음 설계(위 "본문 보존 잡 설계")에서 달라진 점과 그 이유를 적는다. Tier 0 VM에는 아직 배포하지 않았다 - 배포·확인·되돌리기는 [런북](../runbook-hosting.md) 4.1이다. **VM의 첫 만료는 2026-10-26 02:36 UTC(11:36 KST)다.** 그 전에 배포하지 않으면 VM의 DB가 이 ADR을 어긴다.

### 들어간 것
| 무엇 | 어디 |
|---|---|
| 마이그레이션 `f1db3221e45f`(`down_revision` = `a6f1e83b0d57`, 단일 head): 본문 NULL 허용, `raw_news_content_length`(글자 수), `raw_news_content_purged_at`, `raw_news_kogl_type`(1~4), CHECK `ck_news_raw_purged_body_gone`(비운 행은 본문 NULL·길이 있음)과 `ck_news_raw_kogl_type`, 기존 행 길이 배치 채움 | `backend/alembic/versions/f1db3221e45f_news_raw_body_retention.py`, `backend/app/models/news.py` |
| 잡 `python -m jobs.run retention [--days N] [--dry-run] [--batch-size N] [--statement-timeout-s N]` | `jobs/tasks/retention.py`, 상수·출처 해석은 `ai_workspace/config/retention.py`(기한 상수 `BODY_RETENTION_DAYS` = 30, 추출기와 함께 쓰는 "추출 대기" 상태). 설정 `Settings.BODY_RETENTION_DAYS`는 30만 받는다(아래 "기한 가드") |
| 스케줄: 매일 17:50 UTC(02:50 KST) | `docker/crontab.micro`(Tier 0), `docker/crontab`(전체 스택). Tier 0는 `.env`의 `SCHEDULER_JOBS_DISABLED=retention`으로 다시 빌드하지 않고 끌 수 있다(`docker/compose.micro.yaml`) |
| 본문을 쓰는 곳: 추출기와 정책브리핑 수집기가 본문 길이를(정책브리핑은 공공누리 유형도) 함께 쓴다. 추출기는 비운 행과 보존 기한이 지난 행을 대상으로 고르지 않는다 | `crawler/content_extractor/extractor.py`, `crawler/policy_briefing.py` |
| 본문을 읽는 곳 | 아래 "하류" 표 |
| 일일 리포트: 기한 지난 본문 수, 26시간 넘게 밀린 본문 수(0이 아니면 경고·알림), 비운 수, 가장 오래된 본문 | `jobs/tasks/daily_report.py` |

### 잡이 하는 일
1. 실행을 시작할 때 DB에서 기준 시각을 한 번 만든다: `cutoff = (now() AT TIME ZONE 'UTC') - days`. 그 실행 동안 고정한다.
2. **만료**: `raw_news_crawled_at <= cutoff`, 곧 `crawled_at + days <= 지금`. LLM 실험의 동결 반출 도구(ADR 0036, 브랜치 `feat/llm-v2-experiment-data-path` - 아직 main에 없다)와 같은 정의다(만료 시각과 같은 순간부터 만료, 행마다 수집 시각 + 30일).
3. **대상**: 만료됐고 본문 칸이 NULL이 아니며, 면제가 아니고, "본문이 있거나 추출이 끝난" 행. 면제 = 언론사가 `config/sources.py`에서 재배포 가능(지금은 정책브리핑)이고 `raw_news_kogl_type`이 1이거나 NULL. 표에 없는 언론사는 대상이다(fail closed).
4. 대상 행을 id 순서로 `--batch-size`(기본 1,000, 최대 5,000)개씩 `FOR UPDATE SKIP LOCKED`로 골라 한 문장으로 바꾸고 배치마다 커밋한다: 본문 NULL, 길이 = `char_length`(바꾸기 직전 본문), 비운 시각 = `now()`, 해시가 없으면 같은 식(`encode(sha256(convert_to(본문,'UTF8')),'hex')`)으로 채운다. 트랜잭션마다 `statement_timeout`(기본 60초)과 `lock_timeout` 5초.
5. 끝난 뒤 "면제가 아닌 행 중 기한이 지났는데 본문이 남은 행"을 다시 센다. 0이 아니면(다른 트랜잭션이 잡고 있던 행 등) 경고를 남기고(알림), 다음 실행이 비운다.
6. `job_runs.stats`: `scanned`(기한 지났고 본문 칸이 비지 않은 행), `purgeable`·`purgeable_bodies`·`purgeable_bytes`, `purged`·`purged_bodies`·`purged_chars`·`purged_bytes`, `hash_filled`, `skipped_exempt`, `skipped_awaiting_extraction`, `batches`, `remaining_expired_bodies`, `oldest_remaining_body_crawled_at`·`_age_days`, `cutoff_utc`. `--dry-run`은 1·2·3·5만 하고 쓰지 않는다.
   - `skipped_awaiting_extraction`은 기한이 지난 빈 행 가운데 추출 상태로는 아직 대기인 행이다. 추출기는 기한이 지난 행을 고르지 않으므로 이 행들은 끝내 추출되지 않고, 이 수는 줄지 않고 쌓일 수 있다. 본문이 ''라 비울 것이 없다(위험 없음).
7. **기한 가드.** 실제로 비우는 실행은 30일만 쓴다(2026-10-17 리뷰 반영).
   - `--days`가 없는 스케줄 실행: `Settings.BODY_RETENTION_DAYS`(환경변수)가 30이 아니면 연결을 열기 전에 실패한다(알림). 잘못된 `.env` 값 하나로 매일 더 짧은 기한으로 비우지 않게 한다.
   - `--days` 7~29는 `--dry-run`과 함께일 때만 받는다(배포 때 실제 규모의 조회 시간을 재는 용도). `--dry-run` 없이 주면 `jobs.run`의 인자 조합 검사(`check_args`)가 실행 기록 없이 종료 코드 2로 끝낸다. `run()`과 `purge_expired_bodies()`도 같은 규칙을 다시 확인한다.
   - 이유: 30일은 평가셋 추출·라벨링·재생성 한 사이클이고, 더 짧게 비우면 진행 중인 사이클의 본문이 되돌릴 수 없게 사라진다(덤프에서 되살리지 않는다). 런북 4.1 6단계의 `--dry-run --days 7`에서 `--dry-run` 하나만 빠져도 23일치가 사라질 수 있었다.
   - 본문 추출기와 일일 리포트는 설정이 아니라 ADR 상수(30)를 쓴다. 설정이 틀려도 기한이 지난 본문을 내려받지 않고 수집을 멈추지도 않는다.

### 처음 설계와 달라진 점
| 처음 설계 | 구현 | 이유 |
|---|---|---|
| `down_revision` = `8b7f830013b7` | `a6f1e83b0d57` | 그 사이 `c4d2a91e7f30`·`a6f1e83b0d57`가 main에 들어왔다. head는 하나다 |
| `crawled_at < now() - make_interval(...)` | `crawled_at <= (now() AT TIME ZONE 'UTC') - make_interval(...)` | 시간대 없는 열을 `timestamptz`와 비교하면 세션 TimeZone으로 바뀐다(KST 세션이면 9시간 어긋남). 경계는 반출 도구와 같게 `<=` |
| 본문 칸이 NULL이 아닌 모든 행 | 본문 추출기가 아직 고를 수 있는 빈 행(상태 NULL, 또는 시도가 `MAX_EXTRACT_ATTEMPTS` 미만인 `fetch_failed`/`error`이고 본문 '')은 건너뛴다 | 두 잡이 서로 다른 행만 쓴다 - 추출기는 그 행들만, 보존 잡은 나머지만. 그래서 서로를 기다리지 않는다. 그 행에 나중에 본문이 들어오면 다음 실행이 비운다 |
| 면제 = 재배포 가능 언론사 | + 공공누리 유형이 1 또는 NULL | 유형을 행마다 남기기 시작했으므로(아래) 1이 아니라고 적힌 행은 면제하지 않는다. NULL은 유형을 저장하기 전의 정책브리핑 행이고, 수집기는 처음부터 1만 저장했다 |
| 길이는 비울 때만 쓴다 | 본문을 쓸 때(추출기·정책브리핑) 쓰고, 기존 행은 마이그레이션이 채우고, 비울 때 다시 쓴다 | 해시와 같은 수명이 된다 - 어떤 행이든 본문 길이를 본문 없이 읽을 수 있다 |
| 해시 `COALESCE(기존, 계산)` | 같다. 다만 'ok' 행이고 같은 해시의 다른 'ok' 행이 있으면 비워 둔다(`hash_left_null`, 경고) | 부분 unique 인덱스 위반으로 배치 전체가 실패하면 본문을 못 비운다. 그런 행은 `d48994e9d26e` 이후 생기지 않는다(VM 실측: 해시 없는 'ok' 행 0) |
| 배치 5,000 | 기본 1,000, 최대 5,000, SKIP LOCKED | 1 GB VM에서 한 트랜잭션의 WAL과 잡히는 행 락을 줄인다. 하루치는 약 1,300행이라 2배치다 |
| CHECK 없음 | `ck_news_raw_purged_body_gone` | 어떤 코드도 비운 행에 본문을 다시 쓸 수 없다(DB가 막는다) |
| `--days 30` | 실제 실행은 30만. 7~29는 `--dry-run`과 함께일 때만(측정용). `Settings.BODY_RETENTION_DAYS`는 30만 받는다 | 30보다 늘리는 예외는 없다(위 결정). 30보다 짧게 비우면 진행 중인 라벨링 사이클의 본문이 사라지므로 ADR을 먼저 고친다(위 "기한 가드"). 어기면 인자 오류(종료 코드 2) 또는 실패·알림(설정값) |
| 추출기는 상태로만 대상을 고른다 | + 비운 행 제외, + 수집 후 30일(ADR 상수)이 지난 행 제외. 재시도 상태 목록은 보존 잡과 같은 상수(`RETRYABLE_EXTRACT_STATUSES`) | 기한이 지난 기사의 본문을 새로 내려받아 저장하지 않는다. 재시도 상한을 올려도 비운 행은 다시 받지 않는다 |

### 시간대 (KST와 UTC)
- `raw_news_crawled_at`은 `timestamp without time zone`이고 값은 UTC다: 수집기가 `datetime.now()`로 쓰고 컨테이너의 TZ가 UTC다(VM 실측 `TimeZone = Etc/UTC`). 기준 시각도 `now() AT TIME ZONE 'UTC'`로 만들어 세션 TimeZone과 무관하다.
- 예: 2026-09-26 02:36 UTC(11:36 KST) 수집 → 2026-10-26 02:36 UTC(11:36 KST) 만료.
- 스케줄은 하루 한 번(17:50 UTC = 02:50 KST)이다. 그래서 DB에 본문이 남는 최대 기간은 **30일 + 다음 실행까지(최대 24시간) + 실행 시간**이다. VM의 첫 만료분(09-26 02:36 UTC 수집)은 10-26 17:50 UTC 실행에서 비워진다(30일 15시간). 30일 + 1시간이 필요해지면 crontab 줄을 매시로 바꾼다 - 할 일 없는 실행의 비용은 아래 측정에 있다.
- 세션 TimeZone이 KST인 연결(`PGTZ=Asia/Seoul`)로 잡을 돌려도 경계와 일일 리포트의 수가 같은지 통합 테스트(`test_boundary_is_the_same_when_the_session_time_zone_is_kst`)가 확인한다.
- KST로 시각을 쓰는 프로세스(예: Mac 호스트에서 `main.py`를 직접 돌린 경우)가 넣은 행은 수집 시각이 9시간 늦게 적혀 9시간 늦게 비워진다. 운영 수집기(컨테이너)는 그런 행을 만들지 않는다.

### 하류 - 본문을 읽는 곳
| 어디 | 비운 행(본문 NULL)을 어떻게 다루나 | 테스트 |
|---|---|---|
| 본문 추출기 대상 조회 | 고르지 않는다(비운 행, 기한 지난 행). 다시 내려받지 않는다 | `test_retention_job_db.py::test_extractor_never_selects_purged_or_expired_rows`, `test_content_extractor_wiring.py` |
| 임베딩(`embed`, ingest의 embed 단계) | 대상이 아니다(`본문 IS NOT NULL AND <> ''`). 임베딩되기 전에 비워진 'ok' 행은 `body_purged`로 센다. 비우기 전에 만든 임베딩은 남는다 | `test_embedding_and_clustering_skip_purged_rows`, `test_stage3_embedding_resilience.py` |
| 클러스터링 = 생성 입력(`HDBSCANClusterer._load_data_from_db` → `workflow/nodes.py`) | 읽지 않는다. 생성은 이 데이터만 받는다 | 위 테스트(`data["contents"]`에 빈 본문 없음) |
| 평가셋 추출기(`evaluation/llm/evalset.py`) | 본문 없는 기사(NULL·'')가 든 클러스터를 후보에서 빼고 수를 매니페스트 `meta.excluded_without_body`와 출력에 남긴다(예전에는 NULL을 ''로 바꿔 그대로 썼다) | `tests/evaluation/test_evalset.py`, `test_evalset_db.py` |
| Tier 0 → A1 병합 SQL(런북 6.4) | CSV에 길이·비운 시각·공공누리 유형 열을 더했다. 비운 행은 비운 채 들어가고 본문이 되살아나지 않는다 | `test_merge_tier0_news_raw_sql.py::test_merge_keeps_purged_rows_purged` |
| 일일 리포트 | 보존 상태 한 줄, 26시간 넘게 밀리면 경고 | `test_daily_report_retention_status_counts_expired_and_purged_bodies`, `test_retention_job.py` |
| 요청 시점 추천·시뮬레이터·API | 본문을 읽지 않는다(2026-10-06 확인 그대로, 이번에 `git grep raw_news_content`로 다시 확인) | - |
| 동결 반출(별도 브랜치) | 행 조건이 `본문 IS NOT NULL AND <> ''`라 비운 행은 반출되지 않는다 | 그 브랜치의 테스트 |

### 마이그레이션의 락과 I/O
- DDL은 카탈로그만 바꾼다(NOT NULL 해제, 기본값 없는 열 추가, `NOT VALID` CHECK). ACCESS EXCLUSIVE는 이 리비전까지의 트랜잭션이 커밋될 때까지만 잡히고, `lock_timeout` 10초를 넘기면 기다리지 않고 실패한다(재실행 안전: `IF NOT EXISTS`, 길이 채우기는 비어 있는 행만).
- 커밋 뒤 autocommit 블록에서 CHECK를 `VALIDATE`(SHARE UPDATE EXCLUSIVE - 읽기·쓰기를 막지 않음)하고, 길이를 2,000행씩 배치마다 커밋하며 채운다. 행 락은 배치 동안만이다.
- I/O: 정수 열 하나를 쓰지만 행 전체의 새 버전이 생긴다. 힙에 인라인으로 든 압축 본문은 새 버전으로 복사되고 TOAST로 나간 본문은 포인터만 복사된다. `char_length`가 본문을 한 번씩 푼다. VM(2026-10-16 실측: 30,712행, 힙 28 MB, TOAST 49 MB, 원문 114 MB / 저장 64.5 MB)에서는 "힙 한 벌 다시 쓰기 + 본문 한 번 읽기"다. 죽은 행 버전은 autovacuum이 회수해 다시 쓴다(OS에 돌려주지는 않는다).
- downgrade: 비운 행을 ''로 두고(본문은 되살아나지 않는다) NOT NULL을 되돌리며 길이·비운 시각·유형 열을 지운다. SET NOT NULL은 ACCESS EXCLUSIVE 아래에서 힙을 한 번 읽는다. 다시 올리면 비운 표시와 유형이 비어 있다 - ''가 된 행은 다음 실행이 다시 비우고(본문은 이미 없다), 유형이 1이 아니던 정책브리핑 행은 "유형 미기록 = 면제"로 보인다(통합 테스트가 이 동작을 확인한다).

### 측정 (CI의 PostgreSQL, 합성 30,000행)
CI 실행 [38934969437](https://github.com/khj1212k/pro-recsys-finalproject-recsys-01/actions/runs/38934969437)(커밋 `93c8f32`)의 `retention-measurement` 아티팩트(`tests/integration/test_retention_volume.py`). PostgreSQL 16.15, `shared_buffers` 128 MB, 러너 4 vCPU. **러너는 Tier 0(1/8 OCPU, 1 GB)가 아니다** - 옮겨지는 값은 행 수·WAL·크기·락 동작이고, 시간은 VM에서 이보다 몇 배에서 수십 배 길 수 있다(VM에서는 배포 6단계의 `--dry-run --days 7`로 처음 잰다).

- 데이터: 30,000행(본문 26,453개, 원문 114.9 MB, 저장 61.5 MB, 평균 1,789자), 수집 시각을 40일에 고르게 폈다. 합성 본문의 어휘 크기는 그 DB의 pglz로 표본을 재어 Tier 0의 저장/원문 비(0.565)에 가장 가까운 것(0.535)을 골랐다 - 첫 측정의 무작위 어휘(압축 안 됨, 비 1.0)는 모든 본문을 TOAST로 보내 길이 채우기의 힙 I/O를 실제보다 작게 보였다. 크기: 힙 14.7 MB, TOAST 61.2 MB(VM 실측 힙 28 MB, TOAST 49 MB).
- 동시 쓰기: 측정하는 동안 다른 연결이 10 ms마다 새 행 INSERT(수집기 역할)와 추출 대기 행 UPDATE(추출기 역할)를 번갈아 했다.

| 단계 | 걸린 시간 | WAL | 결과 | 동시 쓰기 지연 (p50 / p95 / 최대) |
|---|---|---|---|---|
| 마이그레이션 upgrade(길이 채우기 포함) | 1.19초 | 24.6 MB | 30,053행 채움(2,000행 배치). 힙 14.7 → 24.6 MB(VACUUM 뒤에도 같음 - 안에서 다시 쓰인다), TOAST 그대로 | INSERT 0.67 / 0.85 / 108 ms, UPDATE 0.52 / 0.83 / 60 ms (최대값은 DDL 커밋까지의 ACCESS EXCLUSIVE) |
| 첫 실행(10일치 7,500행이 한꺼번에 만료) | 0.47초(비우기 0.27초) | 5.0 MB | 7,490행 비움(본문 6,630개, 28.8 MB), 8배치, 추출 대기 10행 건너뜀, 남은 만료 본문 0 | INSERT 0.64 / 0.98 / 1.5 ms, UPDATE 0.50 / 0.79 / 3.4 ms |
| 매일(하루치 650행) | 0.083초 | 0.49 MB | 650행, 1배치 | 최대 1.4 ms |
| 할 일 없는 실행 | 0.034초 | 13.5 KB | 0행 | 최대 1.3 ms |

- 길이 채우기 동안 예전 코드처럼 길이 없이 들어온 동시 행 9개는 길이가 비어 있다(채우기가 이미 지나간 id). 새 코드는 쓸 때 길이를 쓰고, 그런 행도 비울 때 길이가 쓰인다.
- downgrade(비운 행 없음)는 0.12초였다.
- 비운 뒤 VACUUM을 해도 테이블 파일 크기는 줄지 않는다(TOAST 61.2 MB 그대로). 빈 자리는 뒤에 들어오는 본문이 쓴다.
- VM으로 옮기면: 마이그레이션은 힙 한 벌(VM 28 MB)을 다시 쓰고 WAL 수십 MB를 만든다. 디스크(41 GB 여유)와 메모리(`shared_buffers` 64 MB를 지나가며 읽는다)에 부담이 없다. 매일 실행은 하루치 약 1,300개 본문으로 위 "매일"의 두 배 규모다.

### 결과와 한계
- **Tier 0의 기사는 임베딩 없이 본문이 비워진다.** VM은 임베딩을 하지 않는다(2026-10-16 실측 임베딩 0개). 비운 뒤에는 그 기사를 임베딩할 수 없으므로, 수집 후 30일 안에 다른 곳(Mac MPS, A1, 반출 + 원격 임베딩)에서 임베딩하지 않은 기사는 클러스터링·추천 후보에 영영 들어가지 못한다. 첫 손실은 2026-10-26 17:50 UTC 실행에서 생긴다. 해시·제목·URL은 남는다.
- 하루 한 번이라 최대 보존은 30일 + 24시간 남짓이다(위 시간대 절).
- 비운 공간은 테이블 안에서 다시 쓰이고 OS에 돌아가지 않는다. 하루 유입(약 1,300개 본문)과 하루 비움이 비슷해지면 테이블 크기가 멈춘다.
- WAL·스냅숏에 남은 옛 본문: 비운 직후의 WAL 파일과 볼륨 스냅숏에는 옛 행 버전이 남을 수 있다. Postgres의 WAL은 체크포인트 뒤 재사용·삭제된다. 볼륨 백업은 하지 않는다.
- `FOR UPDATE SKIP LOCKED`로 건너뛴 행은 다음 날까지 남는다(경고가 남는다).
- 동결 반출(ADR 0036 브랜치)은 면제를 언론사로만 판정한다. 이 잡은 같은 언론사라도 공공누리 유형이 1이 아니라고 적힌 행은 비운다 - 이 잡이 더 엄격하다. 그 브랜치를 병합할 때 반출의 면제에도 `raw_news_kogl_type`을 반영해 두 정의를 맞춘다.
- 메모리: 보존 잡은 추출기를 임포트하지 않는다(trafilatura·lxml·tqdm 없음). 로컬에서 `jobs.run` 파서를 만들고 `run()`을 연결 직전까지 돌린 프로세스의 최대 RSS는 29.1 MiB였다(추출기를 임포트하던 때 리뷰 측정 46.2 MiB). VM에서는 17:05 ingest가 17:50을 넘기는 일이 잦아 scheduler cgroup(512 MiB) 안에서 둘이 겹칠 수 있다 - 정확성 문제는 없다(서로 다른 행만 쓴다).

### Mac 쪽 덤프
덤프(`data/backups/*.dump`, 런북 6.5)는 DB 전체의 본문을 담고 행 단위로 지울 수 없다. 규칙: 덤프 안에서 가장 오래된 상업 언론사 본문의 수집 시각 + 30일에 파일을 지운다. 보존 잡이 날마다 돌기 시작하면 DB의 가장 오래된 본문은 늘 29~31일 차라 **전체 덤프는 뜬 지 하루 안팎에 만료된다.** Mac에는 상주 작업을 두지 않기로 했으므로(데몬 없음) 만료일을 파일 이름에 적고 사람이 지운다 - 절차는 [런북](../runbook.md) 8절과 [호스팅 런북](../runbook-hosting.md) 6.5. 지금 있는 두 덤프의 만료일: `mac-compose-2026-09-26.dump` 2026-10-25, `tier0-micro-2026-09-26T2218.dump` 2026-10-26. 지우는 것은 사용자가 한다.

### 증거
- 단위 테스트(DB 없음): `tests/test_retention_job.py` 56건(파라미터 포함) - dry run은 쓰지 않음, 배치·커밋·시간 제한, 기준 시각을 DB에서 한 번, 루프 상한, 기한 가드(설정값 30만 - 7·29·31·45·문자열이면 연결 전에 실패하고 jobs.run을 거치면 failed·알림 1회, `--days` 7~29는 `--dry-run` 없이 종료 코드 2·실행 기록 연결 없음, `run()`·`purge_expired_bodies()`를 직접 불러도 거부), 인자 범위(배치 1~5,000), 런북·crontab의 짧은 `--days`가 늘 `--dry-run`과 함께인지, 보존 잡 프로세스가 trafilatura·lxml·tqdm을 올리지 않는지(새 프로세스), 면제 출처 = 재배포 가능 출처뿐, SQL이 쓰는 열은 본문·길이·비운 시각·해시뿐, 추출기 선택 조건과 같은 "추출 대기" 조건, 두 crontab의 줄, 일일 리포트 문구와 경고. 바뀐 기존 테스트: 추출기(길이 인자, 대상 조건), 정책브리핑(스키마별 열 2건 추가), 임베딩 통계, 평가셋 1건 추가.
- 통합 테스트(CI의 PostgreSQL): `tests/integration/test_retention_job_db.py` 12건 - 29일 23시간/30일 1시간 경계(세션 TimeZone이 KST여도 같음), 면제(유형 1·미기록)와 대상(유형 2), 추출 대기 행, dry run → 실제 → 재실행 0행, 비운 행에 본문을 쓰면 CHECK 위반, advisory lock이면 skipped, 잡힌 행은 기다리지 않고 건너뜀(5초 미만)·경고·다음 실행이 비움, 열린 배치가 수집 INSERT·추출 UPDATE·읽기를 막지 않음(1초 lock_timeout, 배치의 락 모드는 RowExclusiveLock까지), 추출기·임베딩·클러스터링·일일 리포트·ingest 현황(본문 있는 'ok'와 비운 행을 따로 셈), downgrade가 본문을 되살리지 않고 upgrade가 길이를 채움, 모델-스키마 일치. 그 밖에 `test_merge_tier0_news_raw_sql.py`·`test_evalset_db.py`·`test_policy_briefing_insert.py`에 비운 행·길이·유형 확인을 더했다.
- 로컬(macOS, Python 3.11, CI 설치 목록, `nice -n 19`)에서는 단위 테스트만 돌렸다(`1855 passed, 30 skipped`, 5분 34초). DB를 띄우지 않았다 - 통합 테스트와 측정은 CI의 결과다(CI 실행 38934969437: 단위 `1827 passed, 58 skipped`, 통합 `96 passed, 2 skipped` + 측정 1건).
- 리뷰 반영(기한 가드·가벼운 상수 모듈·ingest 현황·런북) 뒤 로컬 전체 단위 테스트: `1883 passed, 30 skipped`(2분 53초, 같은 조건). 새 통합 테스트 2건(KST 세션 경계, ingest 현황)은 CI에서 돈다.
