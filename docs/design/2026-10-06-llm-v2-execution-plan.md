# LLM 생성 v2 — 실행 계획과 현재 상태 검증 (2026-10-06)

- 기준: `origin/main` `b64da14`(2026-10-06). 이 문서는 [LLM 생성 v2 설계 스펙](2026-09-26-llm-generation-v2.md)(이하 **설계**)의 실행 순서를
  지금의 인프라 제약에 맞춰 다시 짜고, 설계가 적은 정합 문제 20건(C1~C20)이 main에 **아직 그대로인지를 코드로 다시 확인**한 기록이다.
  설계 문서가 "나머지 C 항목은 다시 확인하지 않았다"고 적은 부분을 여기서 전수 확인했다.
- 효력: 설계 문서와 같다 — **없다.** 여기서 고른 데이터 경로·순서는 ADR로 옮겨질 때 결정이 된다([설계 문서 색인](README.md)). 단 하나의
  예외는 E0 기준선 측정의 사전 등록이고, 그것은 이 문서가 아니라 [ADR 0009 Addendum A8](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md)에
  있다(설계 문서와 ADR이 다르면 ADR이 옳다).
- 읽은 것: 설계 전문, `ai_workspace/pipeline/stages.py`, `workflow/{graph,nodes,gates,evaluators,helpers,state}.py`,
  `core/{reconstruction/*,tone_converter,faithfulness,llm_metrics}.py`, `core/llm/*`, `core/clustering/hdbscan_clusterer.py`, `db/batch_manager.py`,
  `config/{settings.py,llm_pricing.yaml}`, `jobs/tasks/{generate,cluster,daily_report}.py`, `evaluation/llm/*`, `docker/crontab*`, ADR 0005·0006·0009·0010·0013·0023·0026,
  `docs/eval/labeling-guide.md`, `docs/runbook-hosting.md`, `origin/exp/generation-warmup`(미병합).
- 2026-10-06 덧붙임(WP1): 2.3절이 0035로 예상한 데이터 경로 ADR은 [ADR 0036](../adr/0036-experiment-data-path-frozen-export-file-stand-ins.md)이다(0035는 LLM 지출 상한 ADR이 쓴다). 2.6절의 개정은 [ADR 0023](../adr/0023-data-sources-copyright-retention.md) 맨 아래에 있다. 본문의 번호·수치는 고치지 않았다.
- 직접 확인한 실측(2026-10-06 13:06 KST, Tier 0 VM에 읽기 전용 세션으로 집계만 조회 — `SET default_transaction_read_only = on`, 본문·제목은 읽지 않음)은 2.1절에 있다.
- 증거 라벨은 설계와 같다: `[코드]` 저장소에서 확인, `[KR-ops]` VM 실측, `[문서]` 공식 문서, `[추정]` 가정치. E0 전의 LLM 수치는 전부 `[추정]`이다.

---

## 0. 요약

1. **C 항목 20건 중 15건이 main에 그대로다**(C2, C4, C7~C19). 수정된 것은 C1(폴백 발행 차단, PR #9)·C5(judge 파싱 실패 PASS, PR #9)·C3(문체 키, PR #15 — 설계가
   고른 쪽이 아닌 다른 구현으로)이고, C6은 절반(한 설정은 삭제, 한 설정은 ADR 0009 사전 등록 때문에 의도적으로 보존), C20은 절반(구분자는
   있고 지시 무시 문장·블록리스트는 없음)이다. 행 번호가 바뀐 곳이 많아 1절에 전부 다시 적었다.
2. **E0은 설계의 "main+M1 뒤"가 아니라 "main 그대로"에서 돈다.** E0은 **기술 통계 기준선**이다 — 2월 이후 첫 완주, 실패 분류, 비용·토큰·지연 실측, 뒤 사전 등록의 입력값.
   뒤 실험과의 **짝지은 전/후 비교가 아니다**(뒤 실험은 E0 기사를 뺀 다른 표본에서 돌고, n=15에서 10/15의 Wilson 95% CI는 [0.42, 0.85]다). 효과 주장은 각 실험 안의 두 팔 비교로만 한다.
   그래서 E0 앞에는 생성 경로의 운영 코드를 바꾸지 않는 것만 둔다 — 반출·하네스(WP1), 임베딩 잡·러너(WP2). 정합 패치(M1)는 E0 뒤다.
3. **데이터 경로는 "동결 반출 + 파일 대역 + Colab T4 임베딩 + Mac 생성"을 고른다**(2.3절). 1 GB VM에는 읽기 전용 `COPY` 한 번만 닿고, 운영 DB에
   실험 산출물을 쓰지 않는다. E0에서는 운영 코드를 고치지 않고 러너가 DB 접촉점을 파일 대역으로 바꿔 끼운다. 저장소 계약(운영 코드 리팩터)은 E0 **뒤**로 미룬다 —
   2월 이후 한 번도 완주하지 않은 경로를 기준선 측정 전에 리팩터링하지 않기 위해서다.
4. **임베딩이 없으면 아무것도 돌지 않는다.** VM의 13,346건 본문에 임베딩이 0개라 `Stage5`는 클러스터 0개를 낸다(`hdbscan_clusterer.py:112`). E0에 필요한 창(최대 5일,
   약 3.5k~9.5k건)의 임베딩은 Colab T4에서 ≈0.4~0.7 CU로 끝난다(2.5절, S0 드라이 런으로 먼저 잰다). Mac·Micro·A1·GitHub Actions는 각각 규칙·메모리·부재·저작권 때문에 안 된다.
5. **저작권: ADR 0023에 날짜 있는 개정이 필요하다**(2.6절). 그 ADR은 본문이 있을 수 있는 곳을 DB(30일)와 외부 LLM API 입력으로만 열어 두었는데, 이 계획은 Mac의 반출 파일·라벨링 입력,
   Colab 디스크(제3자 연산), pg_dump 백업을 더한다. 개정은 WP1 PR에서 하고, 개정이 main에 들어가기 전에는 반출하지 않는다.
6. **LLM 비용은 전 과정 기대 ≈ $3.1(₩4,400~5,200), 비관 ≈ $4.8.** 비관이면 프로그램 하드캡(선불 잔액 실차감 ₩6,000)이 E2에서 닿으므로 E2 시작 전에 n을 줄이는 게이트를 둔다(3절).
   E0 하나는 기대 $0.54·유효 실행 상한 $1.00·누적 상한 $1.50이다. 사람 라벨은 E0 기대 ≈4.3h(2.2~5.3h), 전 과정 ≈14.5~19h다(사용자 결정 3).
7. **첫 세 PR**: WP1 읽기 전용 반출·임베딩 반입·파일 대역 + 데이터 경로 ADR + ADR 0023 개정, WP2 Colab 임베딩 잡 + E0 러너·리포트, WP3 정합 패치
   M1 축소판(C2·C4·C7·C10의 재시도 금지 목록). 4절.

---

## 1. 현재 상태 검증 — C1~C20 (main `b64da14`)

판정: **그대로** = 설계가 적은 문제가 지금도 있음(15건) / **수정됨(PR #n)**(3건) / **달라짐** = 일부 수정 또는 설계 서술과 사실이 다름(2건). 행 번호는 전부 이번에 다시 읽은 값이다.

| ID | 판정 | 지금의 위치 `[코드]` | 확인한 사실 | 비고 |
|---|---|---|---|---|
| C1 폴백 초안 발행 | **수정됨(PR #9)** | `generator.py:88-89`(`_fallback` 표시), `nodes.py:234-242`(`check_faithfulness`가 모드 무관 차단, `failure_reason=generator_fallback`) | 테스트 `tests/test_workflow_faithfulness_gates.py:192`(503+shadow judge → 저장 0), `:210`(메타만 폴백 → 미발행) | 설계 C1의 "bakeoff 선반영"이 그대로 병합됨 |
| C2 제외 기사까지 FK 갱신 | **그대로** | `nodes.py:70`에서 `current_article_ids`=클러스터 원본 id, `:153-157`·`:186-189`는 `current_articles`만 좁힘, `:502` `article_ids = state["current_article_ids"]` → `db/batch_manager.py:224-229` `UPDATE news_raw ... WHERE raw_news_id = ANY(%s)` | 수정·테스트는 `origin/exp/generation-warmup` `eab2b89`(`tests/test_save_links_only_used_articles.py`)에만 있음 | 설계의 `nodes.py:377`은 지금 `:502` |
| C3 `summary`/`sentence` 키 | **수정됨(PR #15, 설계와 다른 구현)** | `tone_converter.py:73-77 _draft_summary`, `:80-84 _with_sentence_alias`(호출 `:145,:175,:208`), `nodes.py:486-496`(변환본 `sentence`/`summary` 우선 저장) | 테스트 `tests/test_tone_converter_summary_key.py` 7건 | 설계는 warmup `99fa60d`를 고르라 했지만 다른 쪽이 병합됐다. 효과는 같다 — 캐주얼 요약이 저장된다 |
| C4 `TONE_FIELDS`에 sentence 없음 | **그대로(C3 수정으로 더 중요해짐)** | `gates.py:22 TONE_FIELDS = ("title", "content")`; `:20-21` 주석 "sentence는 저장되지 않으므로"는 `nodes.py:488-490`과 **모순**(지금은 저장된다) | 저장되는 캐주얼 `sentence`의 수치·날짜 드리프트를 아무 게이트도 보지 않는다 | 주석도 함께 고쳐야 함 |
| C5 judge 파싱 실패 → "PASS" | **수정됨(PR #9)** | `evaluators.py:346-350`(v2는 `_fail`로 닫음), `nodes.py:15`가 v2 `NewsletterEvaluator`를 쓴다 | v1 휴리스틱은 `evaluators.py:237-245`에 기준선용으로만 남음 | **추가 발견**: `ClusterEvaluator`에 같은 모양의 텍스트 휴리스틱이 남아 있다(`evaluators.py:125-135`, confidence 0.1). 다만 기본 레지스트리(OpenAI 호환 어댑터)에서는 **도달하지 않는 분기**다 — 스키마 호출에서 `parsed`가 없으면 어댑터가 예외로 처리하고 실패 반환은 전부 `text=None`이다(`adapters.py:263-297`). `naver` 레거시 어댑터에서만 탄다 |
| C6 죽은 설정 2개 | **달라짐** | `settings.py`에 `MIN_NEWSLETTER_SCORE` 없음(삭제됨). `MIN_CLUSTER_CONFIDENCE`는 `:74-77`에 "(미사용)" 주석과 함께 **의도적으로 보존** — ADR 0023 통합 기록 1항: ADR 0009가 ROC 결과로 게이트 연결을 사전 등록했으므로 지우지 않는다 | 설계 C6 "둘 다 삭제"는 ADR 0009 사전 등록과 충돌한다 → **설계 수정**: ROC 결과 뒤에 처리 | |
| C7 `reasoning_effort` 미설정 | **그대로** | `core/llm/`에 `reasoning`·`thinking`·`thought`·`cached` 문자열 0건; `adapters.py:263-270`는 `model/messages/response_format/temperature/max_tokens`만 전달; usage는 `:100-103`에서 `prompt_tokens`·`completion_tokens`만; `client.py:14-17 LLMUsage` 2필드 | thinking 토큰이 출력 단가로 과금되는지 여부와 양은 **미지수**. `LLMUsage`로는 볼 수 없으므로 E0은 HTTP 응답의 usage를 직접 기록한다(A8.4: `hidden = total − prompt − completion`) | |
| C8 실호출 검증 스키마 1개 | **그대로** | `core/llm/schemas.py` 8모델 중 운영 경로 5개(`ClusterEval`·`NewsletterContent`·`NewsletterMeta`·`NewsletterEvalV2`·`ToneResult`); ADR 0005 부록은 1개만 실호출. pre-flight 서브커맨드 없음. `reports/llm/` 없음(`reports/`에는 ops·recsys·serving·sim만) | `evaluation/llm/bakeoff.py generate --split warmup`은 bake-off용 단일 패스 러너라 그래프 경로가 아님 | E0 실행의 첫 단계로 합성 입력 pre-flight 5호출을 넣는다(A8.5) |
| C9 선택 순서·캡 | **그대로** | `stages.py:371 all_ids = sorted(list(clusters.keys()), reverse=True)`, `:372 [:limit]`; 캡 설정 없음(`settings.py`에 `LLM_DAILY_*` 없음); `jobs/tasks/generate.py:14 --limit`만 | R2 근거 재확인: `hdbscan_clusterer.py:117 ORDER BY N.raw_news_id`, `:73-75 setdefault`, `:164 enumerate(groups)` | 설계의 `:216`은 지금 `:371` |
| C10 서킷브레이커·예산 상한 부재 | **그대로** | 401/402/403/404는 호출 1회에서는 즉시 실패(`adapters.py:166-171`), 킬 스위치는 `LLMResult(error="kill_switch")`(`kill_switch.py:62-78`). 실패가 **클러스터 처리 도중**에 시작되면 생성기 폴백(`generator.py:155-156`) → 사실성 노드 차단(`nodes.py:234-242`) → 재생성 라우팅(`:273-279`)으로 그 클러스터가 생성 3회×2호출을 헛돈다. 실패가 **처음부터** 있으면 클러스터 평가 호출이 응답 없이 FAIL(`evaluators.py:137-144`) → 이상치 없음 → skipped(`nodes.py:159-164`) — 남은 클러스터마다 헛호출 1회씩이고 **인프라 실패가 `skipped`로 기록된다**. 스키마 실패 재시도 상한 10(`adapters.py:86`, `llm_client.py:20`=`MAX_LLM_CALL_RETRIES` `settings.py:124`). 스레드 공유 브레이커 없음. `jobs/tasks/generate.py:22-24`는 시작 전 1회만 확인 | **모델 품질 실패의 최대 호출 경로**: 클러스터 평가 3 + 생성(본문+메타) 3×2 + judge 3 + 문체 **6**(`MAX_RETRY_TONE_VALIDATION=2`로 `convert()` 안 3회 × 드리프트 재변환 1) = **18호출**. 호출당 deadline 180s면 최소 54분, 마지막 시도가 deadline 직전에 시작해 요청 타임아웃 60s까지 가면 호출당 240s라 **최대 72분**(설계 R13의 45분보다 길다) | BudgetGuard는 warmup `budget.py`에만(미병합) |
| C11 호출 단위 비용·thought·cached 미기록 | **그대로** | `llm_metrics.py:19-33 LLMCallRecord`에 비용·cluster/run id·숨은 토큰·상태 코드 없음; 파일은 `logs/llm_metrics_run{run_id}.json`(`stages.py:416`); `jobs/tasks/generate.py:34-42`가 요약만 `job_runs.stats`에 | `llm_calls` 테이블 없음(`backend/alembic/versions/` 13개 중 해당 없음) | R5 재확인 |
| C12 소스 준비 | **그대로** | `generator.py:16-28`(본문 긴 순 10건), `:97 [:1500]`(문장 중간 절단), `:98-106`(출처/제목/본문만); `evaluators.py:308-316` 동일 | `core/reconstruction/`에 `sources.py` 없음 | E0 입력 통계(언론사 수·near-dup·절단율)가 설계 입력 |
| C13 날짜 미대조 | **그대로** | `gates.py:90-94`는 numbers/quotes/entities만; `faithfulness.py:444-455 check_against_sources`에 날짜 인자 없음(`:270-292`는 추출만) | ADR 0010 "결과와 한계" 자인 그대로 | |
| C14 규칙 검사기 없음·키워드 채움 | **그대로** | `validator.py:40-53`(키워드 5개 미만이면 제목·요약 토큰으로 채움 — 남아 있는 채움 경로); `gates.py`에 `check_style_rules` 없음. `generator.py:174-175`의 `"뉴스"` 리터럴은 `fallback_meta()` 안에만 있고 그 초안은 `_fallback` 표시로 발행되지 않는다(C1 수정) → **발행 경로에서는 도달 불가** | E0에서 첫 초안·저장본에 오프라인 규칙 검사(shadow)를 돌리고, validator 채움은 메타 응답의 키워드 수로 센다(A8.3 #11) | 설계의 "'뉴스' 필러"는 지금은 폴백 전용 |
| C15 두 번째 서브그룹 폐기 | **그대로** | `nodes.py:141-157` `sub_groups.sort(key=len, reverse=True)` 후 `[0]`만 | E0에서 "두 번째 그룹 ≥3건" 건수를 센다 | |
| C16 프롬프트 규칙 vs 아카이브 | **그대로** | `prompts.py:64-76` 1,000~1,500자·`:84` 4문단, `:110` 제목 15자, `:131-140` sentence 예시가 감정 자극형("벼락거지" 등) — `:61`의 금지어 규칙과 충돌 | | |
| C17 `_locate` 정확 부분 문자열 | **그대로** | `bakeoff_analysis.py:191-196` `.find(claim.strip())` | E0에서 judge `unsupported_claims`의 위치 추적 성공률을 센다 | |
| C18 평가셋 프레임 = 생성 실행 | **그대로** | `evalset.py:363 SELECT run_id, cluster_log FROM cluster_history`; `jobs/tasks/cluster.py:1-5` "cluster_history는 쓰지 않는다"; `cluster_snapshot` 없음 | 이번 계획에서는 동결 반출본의 클러스터링 스냅숏(ids)이 프레임을 대신한다(2.3) | |
| C19 프로비넌스·출처 없음 | **그대로** | `batch_manager.py:188-203` INSERT 컬럼(제목·요약·본문·키워드·`raw_news_count`·시각·`run_id`·`generation_history`); `backend/app/api/newsletter.py:26,70,195 raw_news_count` | | |
| C20 구분자 없는 본문 삽입 | **달라짐(부분)** | `generator.py:98-106`은 `---`·`[기사 n]` 구분자를 **쓴다**; 없는 것은 "기사 안 지시·광고·구독 유도 무시" 시스템 문장과 보일러플레이트 블록리스트 | 설계의 "구분자 없이"는 부정확 → "지시 무시 문장·블록리스트 없음"으로 고쳐 읽는다 | |

**그 밖에 이번 확인에서 드러난 것**

- `docker/crontab`의 `generate`는 주석 처리, `docker/crontab.micro`에는 없다(설계와 같음). VM의 코드는 09-26 스냅숏이고 Alembic `d48994e9d26e`(코드 head `8b7f830013b7`) — `news_raw`·`press`·`cluster_history`·`news_letter`는 그 리비전에 다 있다. `[KR-ops]`
- **Mac 가상환경에 `FlagEmbedding`이 없다**(`hdbscan`·`kiwipiepy`·`langgraph`는 있음). 워크플로우의 `embed_newsletter_node`(`nodes.py:349-390`)는 Mac에서 그대로 돌 수 없고, 실패하면 임베딩 `None`으로 저장을 계속한다(`:385-390`). 2.3절의 "기록만" 이탈과 맞물린다. `[코드]`
- `generation_history`는 시도별로 `draft_title`과 사실성 차단 항목만 남기고(`workflow/helpers.py:56-63`, `nodes.py:252-258`) 초안 본문·`number_total`·중간 클러스터 평가 결과는 최종 state에 없다. E0이 첫 초안과 시도별 수치를 보려면 노드별 갱신을 따로 잡아야 한다(A8.2 이탈 4: `app.stream`). `[코드]`
- 정책브리핑(`kogl-1`) 행은 VM에 0건 — VM 코드가 수집기 이전 스냅숏이고 인증키도 없다. 공개 가능한 본문은 아직 하나도 없다. `[KR-ops]`
- `raw_news_crawled_at`은 시간대 없는 UTC 값이다(ADR 0026 결과와 한계). 아래의 날짜 창은 전부 UTC로 정의하고 KST로 환산해 적는다.

---

## 2. 인프라 사실과 실험 데이터 경로

### 2.1 사실 (2026-10-06)

| 항목 | 값 | 출처 |
|---|---|---|
| Tier 0 VM `news_raw` | **14,837행**, 본문 `ok` **13,346**, 임베딩 **0**, `news_letter_id` 설정 0, `news_letter` 0행, `cluster_history` 0행, DB 51 MB | `[KR-ops]` 13:06 KST 읽기 전용 집계 |
| 본문 길이(ok) | p10/p50/p90/p99 = 602 / 1,178 / 3,633 / 9,165자, 평균 1,747자, 합계 55.5 MB(UTF-8); >1,500자 4,417건(33.1%, 절단 대상), <800자 2,984건(22.4%) | 같음. 설계 2.2의 09-26 수치(1,210자·34.9%·27%)와 같은 모양 |
| 일일 유입(ok) | 약 700~1,900건/일(09-27~10-05; 날짜 버킷은 UTC 기준이라 ±9시간 어긋남) | 같음 |
| 언론사별(ok) | 한국경제 2,557 · 매일경제 2,494 · 세계일보 2,169 · 경향신문 1,898 · 국민일보 1,673 · 동아일보 1,560 · 전자신문 676 · AI타임스 319 | 같음 |
| 30일 보존 | VM에 30일 넘은 ok 행 0건. **첫 삭제 대상은 ≈ 10-26인데 보존 잡은 미구현이다**(ADR 0023 TODO) — 그날부터 VM이 ADR 0023을 어긴다 | 같음 |
| Mac 덤프(본문 포함) | `data/backups/mac-compose-2026-09-26.dump` 899행·임베딩 845개(5.9 MB, 가장 오래된 행 09-25 수집 → **10-25 만료**), `tier0-micro-2026-09-26T2218.dump`(795행, **≈10-26 만료**) | 디스크 확인, ADR 0023 컨텍스트 |
| Colab | CLI `~/.local/bin/colab`(`new/run/exec/upload/download/ssh/stop/usage`), T4 ≈1.07 CU/h · CPU ≈0.08 CU/h, 잔액 ≈199 CU. **클라이언트 없는 세션은 ~15분 안에 사라졌고 3.4h 세션 하나를 잃고 체크포인트로 복구한 전례**(ADR 0013 A3.9) | 과제 조건·ADR 0013 |
| GCS | 비공개 버킷 + 서명 URL(≤12h) 절차가 있다. `colab upload/download`가 있으므로 E0 경로에는 **필요 없다** | 과제 조건 |
| Gemini | 선불 ₩8,000(자동충전 꺼짐), 10-06 02:20 결제 복구 확인. generator `gemini-3.5-flash-lite`, judge `gemini-3.1-flash-lite`, tone `gemini-3.5-flash-lite`(`registry.py:81-85`). 단가 `config/llm_pricing.yaml`: 3.5-lite $0.30/$2.50, 3.1-lite $0.25/$1.50 (1M 토큰, 접근 2026-09-25/26) | `[코드]` `[문서]` |
| 다른 프로바이더 키 | 없음 → 교차 벤더 judge(solar-pro3)는 키 확보 전까지 불가; shadow judge는 같은 계열(`get_client`가 WARNING) | |
| GitHub Actions | 단위 + Postgres/pgvector 통합 테스트, `workflow_dispatch` 가능, **API 키 없음**, 공개 저장소(아티팩트가 곧 공개) | `.github/workflows/ci.yml` |
| A1 | 10-06 04:43까지 4,215회 시도·성공 0 | ADR 0026 |
| Mac | 상주 서버·DB·docker·임베딩·대형 클러스터링 금지; 짧은 계산은 `nice -n 19` + 스레드 2, ≤10분. LLM API 호출은 가능 | 과제 조건 |

### 2.2 무엇이 어디서 돌아야 하나 — 단계별 요구 자원

| 단계 | 입력 | 자원 | 비고 |
|---|---|---|---|
| 기사 임베딩(BGE-M3, 1,024-d) | E0 창의 본문 3.5k~9.5k건 | GPU 또는 큰 CPU. 모델 fp32 로드만 3.6 GB(ADR 0006 증거 5) | Micro(1 GB) 불가, Mac 금지, A1 없음 |
| 클러스터링(HDBSCAN+split_v2) | 하루 창 1~2k 벡터 | CPU 수 초~수 분, 메모리 수십 MB | Mac에서 `nice` 가능. 운영 코드 경로 그대로 써야 함 |
| LangGraph 생성 | 클러스터 기사 본문, LLM API | 네트워크 대기. **단, `embed_newsletter_node`가 BGE-M3를 올린다**(`nodes.py:331-338`) | LLM은 Mac 가능. 임베딩 노드는 2.3의 이탈 |
| DB 접촉점 | `_load_data_from_db`(`hdbscan_clusterer.py:97-143`), `create_new_batch`/`update_cluster_log`(`batch_manager.py:47,126`), `save_news_letter`(`:151`), 저장 노드의 임베딩 UPDATE(`nodes.py:526-539`) | Postgres+pgvector, 또는 같은 모양을 돌려 주는 대역 | 접촉점은 **4곳**. Stage5를 우회하고 임베더가 `None`을 주면 E0에서 닿는 것은 앞의 1곳과 저장 1곳이다 |
| 라벨링 | 초안 + 원문 | Mac 로컬 `labeling_app`(`127.0.0.1:8765`) | 밤 시간, ≤1h 세션 |

### 2.3 데이터 경로 — 대안 비교와 선택

| | **A. SSH 터널 → VM 운영 DB 직접** | **D. SSH 터널 → VM 안 일회용 실험 DB**(ADR 0026 증거 7의 `a1_rehearsal` 방식) | **B. Colab 안 임시 Postgres(덤프 복원)에서 전부** | **C. 동결 반출 + 파일(채택)** |
|---|---|---|---|---|
| VM 메모리(1 GB) | 벡터 3.5k~9.5k건 × ≈4 KB UPDATE(≈15~40 MB) + 생성 중 4스레드 조회. 소크 최솟값 MemAvailable 239 MiB(ADR 0026 증거 8) 위에서 미측정 부하 | 같은 Postgres 안에 DB 2개 + 벡터 15~40 MB. 리허설 때 최소 583 MiB였지만 벡터 쓰기는 안 해 봤음 | **읽기 전용 `COPY` 1회** 또는 `pg_dump`(51 MB). ingest 사이(:20~:50)에 돌리면 됨 | **읽기 전용 `COPY` 1회**(E0 창만, 15~40 MB) |
| 운영 오염 | **있음** — `news_letter` 행·`news_raw.news_letter_id`가 운영 DB에 남아 그 기사가 영구 제외(C2가 그대로라 제외 기사까지) | 없음(DROP DATABASE) | 없음 | 없음 |
| 저작권 노출 | 본문은 VM에만. Colab 임베딩을 위해서는 어쨌든 본문이 Colab으로 간다 | 같음 | 덤프 전체(제목·URL·본문)가 Colab에. 세션 종료 시 소멸 | E0 창의 본문이 Mac `data/`(gitignore)와 Colab(임베딩 동안)에. **ADR 0023 개정이 필요하다**(2.6) |
| 재현성 | 낮음 — DB가 매시 바뀜. 멤버십을 따로 저장해야 함 | 중간 — 실험 DB는 만든 시점 동결, 그러나 VM 회수 시 소멸 | 높음 — 덤프 SHA 고정, 세션마다 복원 | 가장 높음 — 반출 파일 SHA + 임베딩 `.npy` SHA + 의사 시각으로 클러스터링까지 결정론. **단 본문이 남아 있는 30일 안에서만** |
| 비밀 이동 | Gemini 키 Mac에만 | Mac에만 | **Gemini 키를 Colab에 넣어야 함** | Mac에만 |
| 세션 취약성 | SSH 끊김만 | 같음 | Colab 세션 소멸이 생성 중간을 끊음(LLM 비용 낭비, 체크포인트 필요) | Colab은 임베딩 20~40분만. 생성은 Mac |
| 코드 변경 | 없음(env만) | 없음(env만) | 없음(런타임 부트스트랩 스크립트) | 아래 표(C와 C′) |
| 노력 | 낮음(터널·벡터 반입 스크립트) | 낮음~중간(DB 생성·DROP 절차, 백업 선행) | 중간~높음(Postgres+pgvector apt, alembic, 키·keeper·체크포인트) | 중간 |
| 치명적 단점 | 운영 오염, VM 부하 미측정, "VM을 바꾸지 않는다" 위반 | VM 부하 미측정, 회수 시 소멸, 사용자 승인 필요 | 비밀 이동 + 세션 취약성 + 가장 큰 설치 비용 | 뉴스레터 임베딩 노드를 Mac에서 못 돌림(FlagEmbedding 없음) → 선언된 이탈 필요 |

**선택: C.** 이유는 세 가지다. (1) 1 GB VM에는 읽기만 닿고 운영 DB를 더럽히지 않는다 — C2가 그대로인 코드로 운영 DB에 생성을 돌리면 제외
기사까지 소진된다. (2) 동결 입력은 SHA로 고정되므로 한 실험의 두 팔이 같은 바이트의 기사를 본다는 것을 증명할 수 있다. (3) Gemini 키와 생성은 Mac에 머물고,
Colab은 임베딩만 한다(짧고, 결과를 `colab download`로 받는다). B가 "코드 변경 없음"이라는 점은 매력적이지만, Postgres를 Colab에 세우고 키를 옮기고 세션을
지키는 비용이 크고 그 투자는 E0에서 끝난다.

**C 안에서 파일을 끼우는 방식 — 계약 먼저(C) 대 하네스 패치(C′)**

| | **C. 저장소 계약 PR을 E0 앞에** | **C′. 운영 코드 무변경, 러너가 접촉점을 바꿔 끼움(E0에 채택)** |
|---|---|---|
| E0 전 운영 코드 변경 | `hdbscan_clusterer`·`stages`·`nodes`를 `GenerationStore` 주입으로 리팩터, `_load_data_from_db`의 `NOW()`를 인자로 | 없음. `evaluation/`의 파일 대역만 추가 |
| E0이 재는 것 | "main + 리팩터" — 2월 이후 완주 기록이 없는 경로를 기준선 **전에** 고친다. 동작 불변을 확인할 완주 테스트가 없다 | "main 그대로" — 생성 경로 파일이 `b64da14`와 같다(A8.10에 diff 요약을 적는다) |
| 바꿔 끼우는 지점 | 계약 구현체 1개 | `NewsClusterer._load_data_from_db` 1곳과 `workflow.nodes`의 `get_connection`·`release_connection`·`save_news_letter` — 기존 그래프 테스트(`tests/test_llm_v2_workflow_subgraph_e2e.py:104-106`)가 이미 바꿔 끼우는 이름이다 |
| 깨지기 쉬움 | 낮음(타입이 있는 계약) | 중간 — 이름이 바뀌면 조용히 빗나갈 수 있다. 러너가 시작 시 패치 대상의 존재와 시그니처를 확인하고, 페이크 LLM 완주 테스트를 둔다 |
| 크리티컬 패스 | 운영 코드 PR 1개가 E0 앞에 더 들어간다 | E0까지 `evaluation/`·`scripts/` PR만 |
| 뒤 실험 재사용 | 두 팔 실험(E4′, E2)과 Postgres 없는 그래프 테스트에 그대로 쓴다 | 같은 대역을 다시 쓸 수 있지만 운영 경로와의 접점이 늘수록 계약이 낫다 |

**판단: E0은 C′로 돌리고, 저장소 계약은 E0 뒤(M2)에 넣는다.** 기준선의 가치는 "고치기 전 코드"라는 데 있고, C′에서는 그 주장이 diff로 확인된다. 계약의 이점(두 팔 재사용, Postgres 없는
그래프 테스트, 스냅숏 저장)은 E0 뒤에도 그대로이고, 그때는 E0이 남긴 산출물이 리팩터의 회귀 테스트가 된다 — 같은 동결 반출에서 계약의 파일 구현이 E0의 날짜 창별 멤버십 스냅숏
sha256을 다시 내야 한다(본문 30일 안에). 어느 쪽이든 E0은 운영에서 쓰지 않는 파일 대역으로 돈다는 한계는 같다(A8.2 이탈 1).

이 선택은 **ADR로 옮겨야 효력이 있다.** WP1 PR에 새 ADR(번호는 그 PR 시점의 [ADR 색인](../adr/README.md) 기준 다음 빈 번호 — 설계·아키텍처 리뷰가
예약한 0012·0021·0022·0027~0030·0032·0034를 피하면 0035)을 "실험 데이터 경로: 동결 반출·파일 대역·Colab 임베딩"으로 쓰고, 증거는 반출 매니페스트
SHA·왕복 테스트·임베딩 fp16/fp32 대조표를 붙인다. 같은 PR에서 ADR 0023을 개정한다(2.6).

**선언된 이탈 하나 — 뉴스레터 임베딩 노드.** E0은 "현재 파이프라인"을 재는데 `embed_newsletter_node`는 Mac에서 돌 수 없다. 기본안은 **임베더를
"기록만"으로 둔다**: 노드는 그대로 실행되지만 주입된 임베더가 벡터 대신 형식체 텍스트의 SHA를 기록하고 `None`을 돌려 준다(노드는 이미 실패 시
`None`으로 계속한다, `nodes.py:385-390`). 이 벡터를 읽는 하류 노드는 없으므로 E0의 어떤 지표도 바뀌지 않고, 저장본 임베딩은 Colab에서 뒤에 한 번에
계산한다. 뉴스레터당 지연에서 임베딩 시간(ADR 0006: 로드 6.8s + 건당 ≈1.5s)이 빠진다는 점은 리포트에 적는다. 대안은 생성 전체를 Colab CPU 런타임
(≈0.08 CU/h)에서 돌리는 것이고, 그러면 키 이동·keeper가 필요하다(사용자 결정 1). 나머지 이탈 7개는 A8.2에 있다.

### 2.4 데이터 흐름과 만들어야 할 것

```
[Tier 0 VM]  읽기 전용 COPY 1회 (ingest 사이, :20~:50) — E0에 필요한 창만: T0 직전 5개 KST 날짜의 06:00 기준 24h 창
   └→ Mac data/exports/<T0>/articles.jsonl (id, press, title, body, url, created_at, crawled_at, sha256; 3.5k~9.5k행, 15~40 MB, gitignore)
       + manifest.json (행 수, 기간, 각 행 sha256의 sha256, 본문 만료일 = 가장 이른 crawled_at + 30일; 리포트에는 이 값들만)
[Mac → Colab T4]  colab new(T4) → colab upload (articles.jsonl, 코드 tarball) → scripts/colab_embed_articles.py
   NewsEmbedder(cuda, fp16, max_length 8192, 텍스트 규칙 f"{title} {content}"[:8000], 길이순 배치) — 운영 코드 그대로
   1,000건마다 part .npy 저장 + colab download (세션 소멸 대비) → 마지막 단계에서 본문 파일 삭제를 확인(파일명·크기만 로그) → colab stop
   └→ Mac data/exports/<T0>/emb.f16.npy + ids.npy + fp32 대조 100건(cos p10·min)
[Mac]  파일 대역 로더(articles, embeddings, 창 = t_k−24h ≤ crawled_at < t_k) → NewsClusterer.cluster_news()  (nice -n 19, 스레드 2)
   └→ snapshots/day_k.json (membership: ids만, params, git sha)        ← C18의 cluster_snapshot 역할, E7 입력
   └→ evalset.stratified_sample(...) + 처리 순서  → e0 manifest (ids, sha, order)
[실행 전 기록 PR]  docs + yaml pre_run_record + 표본 매니페스트만 (A8.10)
[Mac]  E0 러너: compile_workflow() → run_clusters_bounded(max_workers=3) → 클러스터마다 app.stream(state)
       계측 = complete() 래퍼 + httpx 요청/응답 훅(HTTP 시도마다 usage·상태 코드·비용 원장) + 기록 전용 임베더 + 문체 폴백 플래그
   └→ data/experiments/e0/<run>/{http_attempts,calls,nodes,clusters,ledger}.jsonl + newsletters.jsonl(초안 텍스트는 여기만)
   └→ e0_report → reports/llm/e0_baseline_v1.{json,md}
   └→ E0 전용 블라인드 내보내기 → labeling_app (밤) → labels → e0_labels_report
```

러너는 최대 2시간 도는 로컬 프로세스이고 대부분 네트워크 대기다. 실행 명령에 Mac 규칙을 그대로 붙인다:
`caffeinate -i nice -n 19 env OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 python -m evaluation.llm.e0_baseline run ...`(절전으로 끊기면 A8.5의 재개 규칙이 적용되므로 `caffeinate`로 막는다).

만들어야 할 것(코드는 전부 뒤의 PR에서, 이 브랜치는 문서만):

1. **반출·반입·파일 대역**(WP1): `scripts/export_news_raw.py`(ssh + `COPY ... TO STDOUT`, 읽기 전용 세션, 창 한정, 매니페스트에 만료일), `scripts/import_embeddings.py`(ids 정렬 검증,
   차원 1,024, NaN 0, L2 노름 ≈1 확인), `evaluation/llm/e0_store.py`(반출 JSONL + `.npy`를 `_load_data_from_db`와 같은 dict 모양으로 돌려 주는 로더, JSONL에 쓰는 저장 대역,
   **만료된 본문을 읽지 않는 fail-closed 검사와 열 때마다 만료 행을 지우는 정리**).
2. **Colab 임베딩 잡**(WP2): `scripts/colab_embed_articles.py` — `m4_colab_driver.py`의 tarball sha256·URL 비노출·heartbeat 도구 재사용, part 체크포인트, CU 상한 4,
   S0 드라이 런(300건)으로 CU/h와 fp16/fp32 코사인을 먼저 잰다. 잡의 마지막 단계가 Colab 디스크의 본문 삭제를 확인한다.
3. **E0 러너·리포트**(WP2): `evaluation/llm/e0_baseline.py`(시작 검사, 계측, 예산 가드, 중단·재개), `e0_report.py`(집계만), E0 전용 블라인드 내보내기(첫 초안 + 저장본, yaml의 `human_labels.relabel` 키를 읽음).
   규칙은 [A8](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md)과 `evaluation/llm/preregistration/e0-baseline-v1.yaml`에 이미 고정돼 있다.
4. **저장소 계약**(E0 뒤, M2): `GenerationStore` — `load_articles(lookback_hours, exclude_clustered, now)`, `create_batch`, `update_cluster_log`, `save_newsletter`, `save_embedding`.
   Postgres 구현은 지금 함수들을 그대로 감싸고(SQL 변경 0), 파일 구현은 E0의 스냅숏 sha256을 재현해야 한다.

### 2.5 계산량·비용 추정 `[추정]` — S0·E0에서 실측으로 교체

**임베딩(Colab T4).** 토큰 분포는 `reports/ops/embedding_truncation_2026-09-26.json`(456건): p50 680·p90 1,633·p99 4,324 토큰 → 평균 ≈900 토큰으로 잡는다.
E0 창은 최대 5일 × 일 700~1,900건 = 3.5k~9.5k건. 상한 9.5k × 900 = **8.6×10⁶ 토큰**. BGE-M3(XLM-R large 568M) ≈ 2 × 568M = 1.14 GFLOP/토큰 →
9.7×10¹⁵ FLOP. T4 fp16 텐서코어 피크 65 TFLOPS의 25~35%(길이순 배치) = 16~23 TFLOPS → **420~610초 ≈ 7~10분**. 패딩·로딩·모델 로드(≈2분)를 넣어
**20~40분 ≈ 0.4~0.7 CU**, 3배 비관치로도 ≤2 CU. 이 산술은 토큰당 FLOP에 임베딩 행렬 파라미터를 넣어 과대이고 T4 실효 처리량은 낙관적이라 둘이 어느 정도 상쇄된다 — S0(300건)의 실측 CU/h로 바꾼다.
비교: Mac MPS 실측 0.54~0.69건/초(ADR 0006) → 9.5k건 = 3.8~4.9시간의 Mac GPU 점유(금지); A1 CPU 미측정·미확보; GitHub Actions는 본문이 공개 저장소의 러너를 거친다(불가).
뒤의 실험(E4′·E2, 30일 창 안의 새 기사)마다 그 실험의 창만 새로 반출·임베딩한다 — 회당 ≈0.5~1 CU. **Colab 총 상한 6 CU**(잔액 199의 3%).

**LLM 호출 단가(현행 파이프라인, 클러스터 1건, 깨끗한 경로).** 한국어 문자/토큰 비율은 미지수라 **1토큰/자**로 잡는다(E0 지표). thinking 토큰은 3.5-flash-lite 기본 on(설계 R8)이라 출력에
더하되 양은 모른다(본문 1,000 / 메타 500 / judge 300 / 문체 500으로 가정 — 근거 없는 자리값이라 아래에 4배 민감도를 둔다). 입력은 클러스터 크기 버킷별로 잡는다:
기사당 프롬프트에 들어가는 본문은 `min(길이, 1,500)`이고 2.1의 분포(22.4%가 800자 미만, 33.1%가 1,500자 초과)에서 평균 ≈1,150자, 기사 수는 버킷별 3.5 / 7 / 10건(10+ 버킷은 긴 순 10건이라 ≈1,500자씩).

| 호출 | 모델 · 단가(입력/출력, 1M) | 입력 토큰 (3–4 / 5–9 / 10+) | 출력(+thinking) | 비용 (3–4 / 5–9 / 10+) |
|---|---|---|---|---|
| cluster_eval | 3.1-lite · $0.25/$1.50 | 2.9k / 4.8k / 9.3k (기사당 560 + 900; 10+는 15건 가정) | 200 → $0.0003 | $0.0010 / $0.0015 / $0.0026 |
| content gen | 3.5-lite · $0.30/$2.50 | 6k / 10k / 17k (기사 본문 + 프롬프트 ≈2k) | 1,500 + 1,000 = 2,500 → $0.0063 | $0.0081 / $0.0093 / $0.0114 |
| meta gen | 3.5-lite | 2.7k → $0.0008 | 200 + 500 = 700 → $0.0018 | $0.0026 |
| judge v2 | 3.1-lite | 7.2k / 11.4k / 18k (생성기와 같은 기사 + 초안 + 프롬프트) | 400 + 300 = 700 → $0.0011 | $0.0029 / $0.0039 / $0.0056 |
| tone | 3.5-lite | 2.7k → $0.0008 | 1,700 + 500 = 2,200 → $0.0055 | $0.0063 |
| **합계(깨끗한 경로)** | | | | **$0.0209 / $0.0236 / $0.0285**, 5·5·5 평균 **$0.0243** |

**기대값.** 사실성 게이트가 초안을 막을 확률을 b라 하면(막힌 시도는 judge에 가지 않는다) 생성 시도 수의 기대는 1 + b + b²(상한 3), judge·문체는 1 − b³의 확률로 한 번 돈다. 클러스터 평가와 문체에 재시도 20%를 더하면
`E[비용] = 1.2 × 0.0017 + (1 + b + b²) × (0.0096 + 0.0026) + (1 − b³) × (0.0041 + 1.2 × 0.0063)`(버킷 평균값). b는 한 번도 재 보지 않은 값이다(E0의 지표 6).

| E0 합계(17클러스터 + pre-flight $0.02) | thinking 가정 그대로 | thinking 4배 |
|---|---|---|
| b = 0 | 17 × 0.0259 + 0.02 = **$0.46** | 17 × 0.0429 + 0.02 = $0.75 |
| b = 0.3 (기대값으로 씀) | 17 × 0.0303 + 0.02 = **$0.54** | 17 × 0.0515 + 0.02 = $0.90 |
| b = 0.5 | 17 × 0.0336 + 0.02 = **$0.59** | 17 × 0.0582 + 0.02 = **$1.01 → 상한 $1.00에 닿아 부분 실행** |

**상한 쪽 두 숫자(둘 다 비용의 상한이 아니다 — 그래서 가드는 HTTP 시도마다 건다).**
- 최대 호출 수 경로(18호출, 깨끗한 단가, 10+ 버킷): 3 × 0.0026 + 3 × (0.0114 + 0.0026 + 0.0056) + 6 × 0.0063 = **$0.10/클러스터**.
- 같은 경로를 `max_tokens` 기준 최악 단가(프롬프트 문자 × 1.5 토큰/자, 출력 = `max_tokens`)로: 3 × 0.0066 + 3 × (0.0281 + 0.0038) + 3 × 0.0098 + 6 × 0.0115 = **$0.21/클러스터**. 이것도 complete()당
  HTTP 시도 1회 기준이다 — complete() 하나는 스키마 검증 실패마다 다시 보내 최대 10회까지 과금될 수 있다(`MAX_LLM_CALL_RETRIES`, 실제로는 deadline 180s가 먼저 끊는다).

**v2 호출 단가(M3 이후 실험용).** 설계 3.5의 값: 생성 v2는 본문 600~900자 + 메타를 한 호출로(문체 병합), `reasoning_effort=minimal` 가정으로 thinking 300. 입력은 위 버킷 평균 11k.

| 호출 | 모델 | 입력 토큰 × 단가 | 출력(+thinking) 토큰 × 단가 | 비용 |
|---|---|---|---|---|
| 생성 v2(본문+메타) | 3.5-lite | 11,000 × $0.30/M = $0.0033 | 1,200 + 300 = 1,500 × $2.50/M = $0.0038 | **$0.0071** |
| 표적 재작성(≤1회) | 3.5-lite | 3,000 → $0.0009 | 600 → $0.0015 | $0.0024 |
| extract(팔 B) | 3.5-lite | 11,000 → $0.0033 | 2,000 + 300 = 2,300 → $0.0058 | **$0.0091** |
| write(팔 B) | 3.5-lite | 3,500 → $0.0011 | 1,500 → $0.0038 | **$0.0048** |
| judge 문서 단위(shadow) | 3.1-lite | 12,000 × $0.25/M = $0.0030 | 700 × $1.50/M = $0.0011 | $0.0041 |
| judge 쌍 단위(팔 B, shadow) | 3.1-lite | 3,000 → $0.0008 | 500 → $0.0008 | $0.0015 |
| 문체(분리 팔, E4′만) | 3.5-lite | 1,500 → $0.0005 | 1,200 + 300 = 1,500 → $0.0038 | $0.0042 |
| cluster_eval | 3.1-lite | 5,700 → $0.0014 | 200 → $0.0003 | $0.0017 |

- E4′ 병합 팔 = 0.0017 + 0.0071 + 0.0041 = **$0.0129**, 분리 팔 = 0.0017 + 0.0071 + 1.2 × 0.0042 + 0.0041 = **$0.0179**.
- E2 팔 A(e2e_v2) = 0.0017 + 0.0071 + 0.3 × 0.0024 + 0.0041 = **$0.0136**, 팔 B(claims_v1) = 0.0017 + 0.0091 + 0.0048 + 0.3 × 0.0024 + 0.0015 = **$0.0178**.
- judge를 같은 계열 Gemini로 둘 때의 값이다. 교차 벤더(solar-pro3, $0.15/$0.60)면 judge 줄이 설계의 ≈$0.002/$0.001로 내려간다.

**환율·VAT 가정**: ₩8,000 = **$5.71**(₩1,400/$) 또는 $5.33(₩1,500/$). 선불 잔액에서 VAT 10%가 함께 차감되는지는 **확인하지 못했다** — 충전할 때 따로 냈다면 영향이 없고, 차감될 때 붙는다면
같은 호출이 10% 더 든다. 그래서 아래 합계는 두 경우(₩1,400·VAT 없음 / ₩1,500·VAT 10%)를 나란히 적고, E0의 잔액 대조(A8.7)에서 어느 쪽인지 확인한다.

### 2.6 저작권 — ADR 0023 개정이 필요하다

ADR 0023의 저장·노출 표는 상업 언론사 본문이 있을 수 있는 곳을 **DB(수집 후 30일)**와 **외부 LLM API 입력(생성·판정 목적)**으로만 열어 두었고, "백업을 만들면 백업도 30일 보존을
따라야 한다", "예외는 이 ADR에 추가하는 방식으로만 연다"고 적었다. 이 계획은 거기에 없는 사본을 만든다. 따라서 **ADR 0023에 날짜 있는 개정을 추가해야 하고, WP1 PR에서 데이터 경로
ADR과 함께 한다. 개정이 main에 들어가기 전에는 반출하지 않는다**(A8.0의 2단계). 개정에 들어갈 내용:

| 위치 | 무엇이 | 목적 | 삭제 시점 | 수단(사람 기억에 맡기지 않는다) |
|---|---|---|---|---|
| Mac `data/exports/<T0>/articles.jsonl` | 실험 창의 본문(E0: 최대 5일, 3.5k~9.5k건) | 클러스터링·생성 입력 | 행마다 `crawled_at` + 30일. 라벨 리포트를 커밋하면 그 전에라도 통째로 | 매니페스트에 만료일. 로더가 만료된 행의 본문을 읽지 않고(fail closed), 반출본을 여는 모든 도구가 시작할 때 만료 행의 본문을 지운다. Mac에는 상주 작업을 둘 수 없으므로 주기 잡이 아니라 "열 때마다 정리"다 |
| Colab VM 디스크(제3자 연산) | 같은 파일의 사본 | **BGE-M3 임베딩 처리 목적에 한해** | 임베딩 잡이 끝나는 즉시, 같은 세션 안에서 | 잡의 마지막 단계가 삭제하고 남은 파일 목록(이름·크기만)을 로그에 남긴 뒤 `colab stop`. Drive 마운트·노트북 출력·로그에 본문 금지 |
| Mac `data/labels/<run>/clusters.jsonl`, `data/evalsets/*/articles.jsonl` | 표본 클러스터의 기사 본문(기존 도구가 본문 전체를 쓴다) | 라벨링 UI 입력 | 표본 중 가장 오래된 기사의 `crawled_at` + 30일(= 라벨 마감) | 내보내기가 만료일을 함께 쓰고 `labeling_app`이 만료 뒤에는 열지 않는다. 같은 정리 함수 |
| Mac `data/experiments/<exp>/<run>/newsletters.jsonl` | 우리가 생성한 초안(기사 본문 아님) | 라벨링·재분석 | 기한 없음(ADR 0023 표의 "우리가 생성한 뉴스레터") | 공개 저장소에는 넣지 않는다 — 초안이 원문 문장을 얼마나 옮기는지 아직 재지 않았다(ADR 0023 "남는 위험") |
| Mac `data/backups/*.dump`(pg_dump, runbook 6.5) | DB 전체 본문. 부분 삭제 불가 | VM 회수 대비 | **덤프 안 가장 오래된 행의 `crawled_at` + 30일에 통째로** | 최신 1개만 두는 회전, 파일 이름에 만료일. VM의 보존 잡이 돌기 전에 뜬 덤프는 전부 ≈10-26에 만료된다 |
| 기존 덤프 2개(2.1) | 09-25~26 수집분 | — | `mac-compose-2026-09-26.dump` **10-25**, `tier0-micro-2026-09-26T2218.dump` **≈10-26** | 사용자 결정 8. 임베딩 845개는 본문 없이 따로 남길 수 있다(ADR 0023은 임베딩 보존을 허용) |

맞물린 사실 두 가지. (1) **VM의 보존 잡이 없다.** 첫 삭제 대상은 ≈10-26이고 그날부터 VM 자체가 ADR 0023을 어긴다. 이 계획의 WP는 아니지만 pg_dump 회전 규칙이 그 잡에 기대므로
(잡이 없으면 새 덤프도 뜨는 순간 만료 상태다) 기한을 사용자 결정 8에 넣는다. (2) runbook 6.5의 pg_dump는 반출의 **기술적 선행 조건이 아니다** — 읽기 전용 `COPY`는 VM 데이터를 바꾸지 않는다.
runbook이 "다른 작업보다 먼저"라고 적은 운영 우선순위(10-06 현재 ≈14,000행이 VM에만 있다)이고, 본문 전체를 Mac에 두는 일이므로 위 표의 회전 규칙을 따른다.

E0에 필요한 것은 날짜 창 3~5개뿐이므로 **반출도 그 창으로 한정한다**(전량 14.8k행을 내보내지 않는다). 뒤 실험은 그 실험의 창만 따로 반출한다.

---

## 3. 마일스톤 재배열 — 의존, 증거, 비용, 라벨

설계의 M0~M8을 세 가지 이유로 바꾼다: (a) 정합 패치가 고치려는 문제의 크기를 고치기 전 코드에서 세어 두려고 E0을 정합 패치보다 **앞**에 둔다, (b) 임베딩과 파일 입력이 없으면 어떤 실험도 못 돌므로 데이터 경로가 M0다,
(c) 교차 벤더 키가 없으므로 shadow judge는 당분간 같은 계열이고 그 사실을 모든 리포트에 적는다. 수집 복구(설계 M0)는 Tier 0 단독 수집으로 대체됐다(ADR 0026).

| M | 내용 | 선행 | 산출 증거 | LLM 비용 기대 / 상한 | Colab | 라벨 |
|---|---|---|---|---|---|---|
| **M0 = WP1** | 읽기 전용 반출 + 임베딩 반입 + 파일 대역(운영 코드 무변경) + 데이터 경로 ADR(제안됨) + **ADR 0023 개정** | — | 반출 매니페스트 SHA(실제 반출은 ADR 0023 개정 병합 뒤), 대역 왕복 테스트, 만료 fail-closed 테스트, 현재 main의 단위 테스트 전부 통과(실행 시점 수치 기록) | $0 | 0 | 0 |
| **M0b = WP2** | Colab 임베딩 잡 + E0 러너·리포트 | M0 | S0 드라이 런 표(CU/h, fp16 vs fp32 cos p10·min), 페이크 LLM 완주 테스트(계측·가드·중단·재개) | $0 | S0 0.3 | 0 |
| **실행 전 기록** | 반출 → 본 임베딩 → 스냅숏 → 표본·순서 → **문서 전용 PR**(yaml `pre_run_record`, 표본 매니페스트, A8.10) | WP2 병합(= `code_sha`), ADR 0023 개정 | 결과 파일 없는 커밋. 임베딩 `.npy` SHA, 날짜 창별 멤버십 스냅숏 SHA, 표본 매니페스트 SHA | $0 | 본 0.4~0.7 (상한 4) | 0 |
| **E0** | 기준선 측정(A8): pre-flight 5호출 + warmup 2 + eval 15, main 그대로 | 실행 전 기록 | `reports/llm/e0_baseline_v1.{json,md}`, 첫 뉴스레터(파일) ≥1건 — **2월 이후 첫 완주**, 실패 분류표, 잔액 대조, ADR 0010 증거 갱신(실제 차단율) | **$0.54 / 유효 실행 $1.00 · 누적 $1.50** | 0 | **≈4.3h(2.2~5.3h)** — 결과 열람과 무관하게 30일 안 |
| **M1 = WP3** | 정합 패치 축소판: C2·C4(+주석)·C7(`reasoning_effort` 전달·usage 확장)·C10 중 "재시도 금지 목록"(인프라 실패·킬 스위치·`content_filter`·`length`는 그래프 재생성 진입 금지이고 `skipped`가 아닌 인프라 사유로 기록, 스키마 실패 ≤2). C6은 ADR 0009 ROC 뒤 | E0 리포트 커밋 | 테스트(사용 기사만 FK, sentence 드리프트 차단, 402 뒤 추가 호출 0·기록은 infra), reasoning pre-flight 5스키마×{none, minimal} 표 | 10호출 **$0.04 / $0.10** | 0 | 0 |
| **M2** | **저장소 계약**(E0 스냅숏 SHA 재현이 수용 기준), 예산·선택·관측: `BudgetGuard`(warmup `budget.py` 승격)·`RunCircuitBreaker`·일일 캡·사전순 선택·`llm_calls`·스냅숏, **E6(a)** 장애 주입, **E7** 기술 통계(E0 반출본 재클러스터링, 30일 안) | M1 | 계약 테스트(Postgres 구현 = 기존 SQL 그대로, 파일 구현 = E0 스냅숏 재현), 장애 주입 테스트(중단까지 호출 ≤ 워커×3), E7 표 | $0 | 0 | 0 |
| **M3** | 소스 준비·절대 날짜 대조·규칙 검사기(shadow)·프롬프트 v2·두 번째 서브그룹 재큐잉, **E4′**(20클러스터×2팔: 분리 vs 병합, 새 창 반출) | M1 | `test_prepare_sources`, 날짜 프로브 표, E0 초안의 규칙 위반률 표(사후 재계산), E4′ 리포트 | 20 × (0.0129 + 0.0179) = **$0.62 / $0.90** | 0.5 | 0 |
| **M4** | 아키텍처 A/B 사전 등록(ADR 0009 **A9** + `arch-ab-v1.yaml` + `power.py`; E0의 차단율·문장 수·단가가 검정력 표와 상한의 입력), 교차 벤더 judge 키 확보(사용자), **E1** 10클러스터 | M3 | 결과 파일 없는 SHA, `claim_anchor_feasibility.md` | E1 10 × (16k × 0.30 + 2k × 2.50)/M = **$0.10 / $0.30** | 0 | 0.3h(실패 인용 분류) |
| **M5** | 클레임 앵커 v1 + 프로비넌스(C19) + 팔 B 워밍업 3클러스터 | M4(E1 통과) | 페이크 그래프 테스트, 마이그레이션 왕복, API 스냅숏, 팔별 실측 단가 | 3 × 0.0178 = **$0.05 / $0.15** | 0 | 0 |
| **M6 — E2** | 40×2팔(새 창 반출, 30일 안), 라벨 L0~L3 | M5, 증분 임베딩, 아래 게이트 | `arch_ab.{json,md}`, ADR 0021 | 40 × (0.0136 + 0.0178) × 1.3 = **$1.63 / $2.50**(상한은 설계의 80건 × ≤$0.03) | 0.5~1 | **≈9.4h**(4분/건 가정 — 아래) |
| **M6 — E3** | 판정기 보정(E2의 200쌍) | E2 | `judge_calibration.md`, ADR 0022 | 200쌍 × (600 × $0.25/M + 150 × $1.50/M) × 판정 2회 ≈ **$0.15 / $0.50**(상한은 설계값) | 0 | E2 라벨에 포함 |
| **M7** | 스토리 연속성 측정 **E5**(스냅숏 5~7일) → 조건부 최소판 | M2 + 5~7일 | `story_linking_v1.md`, ADR 0012 | $0 | 0 | 0.5h |
| **합계** | | | | 아래 | **≤6 CU** | **≈14.5~19h** |

**LLM 비용 합계와 하드캡.**

| | USD | ₩1,400/$ · VAT 없음 | ₩1,500/$ · VAT 10% |
|---|---|---|---|
| 기대: 0.54 + 0.04 + 0.62 + 0.10 + 0.05 + 1.63 + 0.15 | **$3.13** | ₩4,382 (잔액의 55%) | ₩5,165 (65%) |
| 비관: E0이 상한(1.00), E2·E3이 설계값(2.50 + 0.50), 나머지는 기대값 | **$4.81** | ₩6,734 (84%) | ₩7,937 (99%) |
| 상한의 합: 1.50 + 0.10 + 0.90 + 0.30 + 0.15 + 2.50 + 0.50 | $5.95 | ₩8,330 | ₩9,818 — 두 환산 모두 잔액(₩8,000)을 넘는다. 상한을 전부 채우는 일은 하드캡이 막는다 |

- **프로그램 하드캡 = 선불 잔액의 실차감 ₩6,000**(₩2,000을 남긴다). 원장이 아니라 결제 화면의 잔액으로 판정한다 — 환율과 VAT가 어떻게 붙든 실제로 나간 돈이 기준이다. 달러로는 $4.29(₩1,400·VAT 없음)~$3.64(₩1,500·VAT 10%).
- 기대값은 두 환산 모두에서 하드캡 안이다. **비관값은 어느 환산에서도 하드캡을 넘는다.** 그래서 E2를 중간에 멈추지 않도록 **시작 전 게이트**를 둔다: `누적 실차감 + (M5에서 잰 팔별 단가 × 80 × 1.3) + E3 추정`이
  ₩6,000을 넘으면 E2의 n을 40 → 30으로 줄이고(검정력 표 재게시, A9에 등록), 그래도 넘으면 E2를 시작하지 않는다. E2의 상한은 A9에서 `min(설계 $2.50, 하드캡까지 남은 금액)`으로 등록한다.
- E2 전까지의 기대 누적은 0.54 + 0.04 + 0.62 + 0.10 + 0.05 = $1.35(₩1,890~2,228)다.

**라벨 시간.** E0은 건당 분을 클러스터 크기 버킷별로 잡았다(3–4건 3분, 5–9건 5분, 10건 이상 10분 — A8.8): 기대 ≈4.3h, 범위 2.2~5.3h. E2의 9.4h는 설계의 4분/건 가정 그대로이고, 버킷 평균 6분이면 ≈14h다.
전 과정은 4.3 + 0.3 + 9.4 + 0.5 = **≈14.5h**(E2를 4분/건으로 둘 때)에서 4.3 + 0.3 + 14.1 + 0.5 = **≈19h** 사이다. **E0의 라벨 시간 실측으로 E2의 9.4h를 다시 산정하고**, 그 값이 A9의 라벨 예산이 된다.

주의할 점.
- M6의 shadow judge가 같은 계열(Gemini)이면 ADR 0009 A4의 교차 계열 κ는 **계산할 수 없다** — 키가 없으면 E3는 "shadow 기록·κ 미산출"로 축소되고 그 사실을 적는다(사용자 결정 4).
- 30일 창(ADR 0023): 반출본의 각 기사는 `crawled_at + 30일`에 본문이 지워지고 그 전에도 만료된 행은 읽히지 않는다(2.6). E0 라벨 마감 = 표본 중 가장 오래된 기사의 수집 시각 + 30일.
- Colab 세션 규칙(ADR 0013 A3.9에서 배운 것): keeper 클라이언트를 붙이고, part 단위로 `colab download`하며, 세션 소멸 시 재개(part 건너뛰기). 끝나면 본문 삭제를 확인하고 `colab stop`.

---

## 4. 첫 세 작업 패키지 (PR 1개씩)

### WP1 — Tier 0 읽기 전용 반출·임베딩 반입·파일 대역 + 데이터 경로 ADR + ADR 0023 개정 (운영 코드 변경 0)

- 범위: `scripts/export_news_raw.py`(창 한정, 읽기 전용 세션, 매니페스트에 만료일), `scripts/import_embeddings.py`, `evaluation/llm/e0_store.py`(파일 로더·저장 대역·만료 fail-closed·열 때마다 정리),
  새 ADR(제안됨) + `docs/adr/README.md` 행, **ADR 0023 개정**(2.6의 표: 허용 위치·삭제 시점·수단, Colab은 임베딩 목적에 한해, pg_dump 회전), `docs/runbook-hosting.md`에 반출 절(읽기 전용·ingest 사이)과 6.5의 회전 규칙.
  `ai_workspace/`는 건드리지 않는다.
- 수용 기준: 현재 main의 단위 테스트 전부 통과(실행 시점 수치 기록); 대역 왕복 테스트(합성 기사 50건 → `NewsClusterer`에 대역 로더를 끼워 클러스터링 → 저장 대역 → 읽기);
  의사 시각 창으로 같은 입력에서 같은 멤버십(결정론 테스트); 만료된 행의 본문을 로더가 돌려주지 않고 정리 함수가 지움(테스트); 반출 스크립트는 본문을 stdout·로그에 찍지 않고 매니페스트만 출력;
  대역 로더가 돌려주는 dict의 키·dtype이 `_load_data_from_db`와 같음(테스트).
- 크기: 코드 ≈300행 + 테스트 ≈200행. LLM 호출 0, Colab 0.
- 사용자 선행 조치: ADR 0023 개정 승인(결정 6), 반출 실행 승인(결정 2). 실제 반출은 이 PR이 병합된 뒤에 한다.

### WP2 — Colab 임베딩 잡 + E0 러너·리포트 (코드, `evaluation/`·`scripts/`만)

- 범위: `scripts/colab_embed_articles.py`(+ `requirements-colab-embed.txt`: FlagEmbedding 1.2.5·torch 핀), `evaluation/llm/e0_baseline.py`(A8·yaml을 읽고 A8.2의 시작 검사 5개와 설정 유효값 검사를
  통과해야 시작), `evaluation/llm/e0_report.py`(집계·Wilson CI·실패 분류·규칙 위반 shadow·C2/C4/C15/C17 계수·잔액 대조 행), E0 전용 블라인드 내보내기(첫 초안 + 저장본, yaml의 `human_labels.relabel` 키),
  `reports/README.md` 행 예약. **실행 전 기록(A8.10)은 이 PR에 넣지 않는다** — 이 PR의 병합 커밋이 `code_sha`가 되고, 그 SHA로 만든 반출·임베딩·표본의 해시는 PR 안에서 알 수 없기 때문이다.
  실행 전 기록은 병합 뒤 문서 전용 PR로 따로 낸다.
- 계측(동작 불변 보장, A8.4): `core.reconstruction.generator`·`workflow.evaluators`·`core.tone_converter`의 `get_client`를 complete() 래퍼로 감싸고, 어댑터가 만드는 OpenAI 클라이언트에 httpx 요청/응답
  훅을 단다(`origin/exp/generation-warmup`의 `evaluation/warmup/generate.py`가 쓴 방식; 거기서는 귀속 문맥이 전역 하나였는데 워커가 3개이므로 **스레드 로컬**로 바꾼다). `workflow.nodes.ToneConverter`를
  폴백 플래그 서브클래스로, `workflow.nodes.get_shared_embedder`를 기록 전용 임베더로. 프롬프트·온도·`max_tokens`·재시도·타임아웃은 건드리지 않는다.
- 수용 기준(페이크 LLM·페이크 HTTP로): 완주 테스트에서 두 기록 표의 필드가 전부 채워짐; `app.stream`과 `app.invoke`의 호출 순서가 같음; 워커 3개에서 귀속이 섞이지 않음;
  스키마 검증 실패·length 응답의 usage가 원장에 잡힘(토큰 0으로 사라지지 않음); 응답 없는 시도가 최악 비용으로 잡힘; 가드가 상한 직전 HTTP 시도에서 멈추고 그 뒤 추가 요청 0건,
  중단부터 종료까지가 진행 중 요청의 타임아웃 안; 402 주입 시 클러스터가 `skipped`가 아니라 `aborted_infra`로 기록; 시작 행만 있는 클러스터는 재개 때 `interrupted`로 닫히고 다시 돌지 않음;
  리포트에 기사·뉴스레터 **텍스트 0바이트**; 시작 검사 5개 각각의 거부 테스트. S0 드라이 런 300건(≤0.3 CU)에서 CU/h와 fp16/fp32 cos p10 ≥ 0.999 기록.
- 크기: 코드 ≈700행 + 테스트 ≈400행. LLM 호출 0(pre-flight 5호출은 PR이 아니라 E0 실행의 첫 단계이고 E0 상한 안이다 — A8.5). E0 실행은 실행 전 기록 PR 뒤 별도 승인으로.

### WP3 — 정합 패치 M1 축소판 (코드, E0 뒤의 첫 변화)

- 범위: C2(`nodes.py` 저장 노드가 `current_articles`의 id만 연결; warmup `eab2b89`의 테스트 이식), C4(`gates.py TONE_FIELDS`에 `sentence` + 주석 정정, 드리프트 테스트 +1),
  C7(`adapters.py` `reasoning_effort` 역할별 env 전달 — 기본값은 pre-flight 결과로; `LLMUsage`에 `total_tokens`·`thought_tokens`·`cached_tokens` nullable, `llm_metrics` 기록),
  C10 축소판(`LLMResult.error ∈ {kill_switch, length, content_filter}` 또는 `http_status ∈ {401,402,403,404}`면 `route_after_faithfulness`·`route_after_newsletter_eval`에서 재생성 금지,
  클러스터 평가 호출이 그런 실패면 `skipped`가 아니라 `failure_reason=infra`/`length`/`content_filter`로 기록; 스키마 실패 재시도 상한 2), `ClusterEvaluator` 텍스트 휴리스틱 제거
  (기본 레지스트리에서는 죽은 분기, 레거시 어댑터에서 파싱 실패 = FAIL·`parsed=False`).
- 수용 기준: 장애 주입 테스트(처음부터 402: 클러스터당 호출 1회 뒤 **infra로 기록되고 skipped 목록에 들지 않음**; 처리 도중 402: 그 클러스터의 추가 호출 0; 킬 스위치: 생성 1회 시도 뒤 종료),
  사용 기사만 FK, sentence 수치 변조 차단, reasoning pre-flight 표(5스키마×{none, minimal}: parsed·지연·토큰·hidden). ADR 0010 Addendum(차단 유형 변경 없음, 재시도 정책 변경 기록), ADR 0005 부록 갱신.
- 크기: 코드 ≈250행 + 테스트 ≈200행. LLM ≈$0.04.

순서: WP1 → WP2 → 실행 전 기록 PR → E0 → WP3. WP3는 E0 리포트가 커밋된 **뒤** 시작한다(고치기 전 코드의 숫자가 먼저 고정되도록).

---

## 5. 사용자가 정할 것 (기본값 제안)

1. **E0의 뉴스레터 임베딩 노드**: 기본 = Mac에서 "기록만" 임베더(벡터는 Colab에서 사후 계산, 지표 영향 없음, 지연에서 임베딩 시간 제외를 리포트에 명시). 대안 = 생성 전체를 Colab CPU에서(키 이동·keeper 필요).
2. **반출 승인**: Tier 0 VM에 읽기 전용 `COPY` 1회(E0 창 최대 5일, 15~40 MB, ingest 사이). 기본 = 승인. ADR 0023 개정(결정 6)이 main에 들어간 뒤에 실행한다.
3. **E0 라벨 시간**: 기본 = 클러스터 15 + 출력 ≈20(첫 초안 ≤15 + 첫 초안과 다른 저장본) + 48h 재라벨 8 → **기대 ≈4.3h, 범위 2.2~5.3h**(≤1h 세션으로 5~6밤, 마감 = 표본 최고령 기사 수집일 + 30일).
   축소안 = 클러스터 15는 유지하고 출력은 처리 순서 앞 10개 클러스터만, 재라벨 5 → 기대 ≈3.3h(1.7~4.0h). 상한으로 쓸 시간을 정해 주면 A8.10에 적는다. 전 과정 라벨 ≈14.5~19h도 함께 본다(E0 실측 뒤 재산정).
4. **교차 벤더 judge 키**(Upstage `solar-pro3`, 설계 3.5): E2/E3 전까지 필요. 없으면 E3는 κ 미산출로 축소. 기본 = M4 시점에 결정.
5. **LLM 지출**: 기본 = E0 유효 실행 상한 $1.00·누적 상한 $1.50, 프로그램 하드캡은 선불 잔액 실차감 ₩6,000, E2 시작 전 게이트(3절). 비관 가정(차단율 50%·thinking 4배)에서는 E0이 $1.00에 닿아
   부분 실행이 될 수 있다 — 상한을 $1.30으로 올릴지는 지금 정할 수 있다(올리면 A8.x 개정으로 적는다). `gcp-budget-guard`는 E0 실행 직전에만 켤지(10-06 결정 "지금은 켜지 않음") 그때 다시 묻는다.
6. **ADR 0023 개정 승인**: 실험용 DB 밖 본문 사본의 허용 위치(Mac `data/exports`·`data/labels`·`data/evalsets`·`data/backups`, Colab 디스크는 임베딩 동안만)와 위치별 삭제 규칙(2.6). 기본 = 2.6의 표대로.
   30일을 늘리는 예외는 넣지 않는다.
7. **Colab 예산**: 기본 = S0 드라이 런 300건(≤0.3 CU) 뒤 본 실행 상한 4 CU, 전 과정 ≤6 CU(잔액 ≈199의 3%).
8. **본문이 든 파일의 기한**: (a) 기존 덤프 2개 — `mac-compose-2026-09-26.dump`는 **10-25**, `tier0-micro-2026-09-26T2218.dump`는 **≈10-26**까지 삭제(임베딩 845개만 본문 없이 따로 남길지 포함).
   (b) runbook 6.5의 새 pg_dump를 지금 뜰지 — 뜨면 그 덤프도 ≈10-26에 만료된다. (c) **VM 보존 잡**(ADR 0023 TODO: 마이그레이션 + `jobs.run retention`)을 ≈10-26 전에 VM에 넣을지. 기본 = (a) 기한대로 삭제,
   (b) 뜬다(VM에만 있는 ≈14,000행의 유일한 사본이 된다), (c) 별도 작업으로 10-26 전에.
9. **E0을 운영 코드 무변경(C′)으로 돌리고 저장소 계약은 E0 뒤로**(2.3): 기본 = 그렇게. 대안 = 계약 PR을 E0 앞에(그러면 E0은 "main + 리팩터"를 재고 A8.10에 그 diff가 적힌다).

---

## 6. 이 문서가 바꾸지 않는 것

- ADR 0009 본문·A1~A7, ADR 0010·0013 A2/A3·0025의 사전 등록 문안. E0은 그 어느 규칙도 바꾸지 않는 **기술 통계**다(A8).
- ADR 0023 본문. 2.6은 개정이 **필요하다는 판단과 들어갈 내용**이고, 개정 자체는 WP1 PR에서 한다.
- 설계의 결정·수치·가설. 1절의 판정(C6 "둘 다 삭제"는 ADR 0009와 충돌, C20 "구분자 없음"은 부정확, C14의 "'뉴스' 필러"는 지금은 폴백 전용, C10 최대 경로 54~72분)은 설계를 고치지 않고 여기 적는다 — 설계 README 규칙대로.
- 코드. 이 브랜치에는 문서와 사전 등록 파일(`e0-baseline-v1.yaml`)만 있다.
