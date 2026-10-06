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
- 직접 확인한 실측(2026-10-06 13:06 KST, Tier 0 VM에 읽기 전용 세션으로 집계만 조회 — `SET default_transaction_read_only = on`, 본문·제목은 읽지 않음)은 2.1절에 있다.
- 증거 라벨은 설계와 같다: `[코드]` 저장소에서 확인, `[KR-ops]` VM 실측, `[문서]` 공식 문서, `[추정]` 가정치. E0 전의 LLM 수치는 전부 `[추정]`이다.

---

## 0. 요약

1. **C 항목 20건 중 14건이 main에 그대로다.** 수정된 것은 C1(폴백 발행 차단, PR #9)·C5(judge 파싱 실패 PASS, PR #9)·C3(문체 키, PR #15 — 설계가
   고른 쪽이 아닌 다른 구현으로)이고, C6은 절반(한 설정은 삭제, 한 설정은 ADR 0009 사전 등록 때문에 의도적으로 보존), C20은 절반(구분자는
   있고 지시 무시 문장·블록리스트는 없음)이다. 행 번호가 바뀐 곳이 많아 1절에 전부 다시 적었다.
2. **E0은 설계의 "main+M1 뒤"가 아니라 "main 그대로"에서 돈다.** 이번 계획에서 E0은 모든 뒷 실험의 "전(before)" 숫자다. 그래서 정합 패치(M1)는
   E0 **뒤**로 가고, E0 앞에는 동작을 바꾸지 않는 두 가지만 들어간다 — 실험용 저장소 계약(WP1)과 계측·사전 등록(WP2).
3. **데이터 경로는 "동결 반출 + 파일 저장소 + Colab T4 임베딩 + Mac 생성"을 고른다**(2.3절). 1 GB VM에는 읽기 전용 `COPY` 한 번만 닿고, 운영 DB에
   실험 산출물을 쓰지 않으며, 같은 동결 입력으로 뒤의 E4'·E2를 돌릴 수 있다. 대가는 저장소 계약 PR 하나와, 뉴스레터 임베딩 노드를 E0에서
   "기록만"으로 두는 선언된 이탈(사용자 결정 1)이다.
4. **임베딩이 없으면 아무것도 돌지 않는다.** VM의 13,346건 본문에 임베딩이 0개라 `Stage5`는 클러스터 0개를 낸다(`hdbscan_clusterer.py:112`). 임베딩은
   Colab T4에서 ≈0.5~2 CU로 끝난다(2.4절, S0 드라이 런으로 먼저 잰다). Mac·Micro·A1·GitHub Actions는 각각 규칙·메모리·부재·저작권 때문에 안 된다.
5. **LLM 비용은 전 과정 기대 ≈ $3.2(≈₩4,500, ₩1,400/$ 가정), 프로그램 하드캡 ₩6,000.** E0 하나는 기대 $0.62·상한 $1.00이다(3절 산술). 사람 라벨은
   E0 ≈2.5h, 전 과정 ≈12.7h — 설계의 9.9h보다 E0 몫이 늘었다(사용자 결정 3).
6. **첫 세 PR**: WP1 저장소 계약 + 반출/반입 스크립트, WP2 Colab 임베딩 잡 + E0 러너·리포트(사전 등록은 이 브랜치에 이미 있음), WP3 정합 패치
   M1 축소판(C2·C4·C7·C10의 재시도 금지 목록). 4절.

---

## 1. 현재 상태 검증 — C1~C20 (main `b64da14`)

판정: **그대로** = 설계가 적은 문제가 지금도 있음 / **수정됨(PR #n)** / **달라짐** = 일부 수정 또는 설계 서술과 사실이 다름. 행 번호는 전부 이번에 다시 읽은 값이다.

| ID | 판정 | 지금의 위치 `[코드]` | 확인한 사실 | 비고 |
|---|---|---|---|---|
| C1 폴백 초안 발행 | **수정됨(PR #9)** | `generator.py:88-89`(`_fallback` 표시), `nodes.py:234-242`(`check_faithfulness`가 모드 무관 차단, `failure_reason=generator_fallback`) | 테스트 `tests/test_workflow_faithfulness_gates.py:192`(503+shadow judge → 저장 0), `:210`(메타만 폴백 → 미발행) | 설계 C1의 "bakeoff 선반영"이 그대로 병합됨 |
| C2 제외 기사까지 FK 갱신 | **그대로** | `nodes.py:70`에서 `current_article_ids`=클러스터 원본 id, `:153-157`·`:186-189`는 `current_articles`만 좁힘, `:502` `article_ids = state["current_article_ids"]` → `db/batch_manager.py:220-225` `UPDATE news_raw ... WHERE raw_news_id = ANY(%s)` | 수정·테스트는 `origin/exp/generation-warmup` `eab2b89`(`tests/test_save_links_only_used_articles.py`)에만 있음 | 설계의 `nodes.py:377`은 지금 `:502` |
| C3 `summary`/`sentence` 키 | **수정됨(PR #15, 설계와 다른 구현)** | `tone_converter.py:73-77 _draft_summary`, `:80-84 _with_sentence_alias`(호출 `:96,:145,:175,:208`), `nodes.py:486-496`(변환본 `sentence`/`summary` 우선 저장) | 테스트 `tests/test_tone_converter_summary_key.py` 7건 | 설계는 warmup `99fa60d`를 고르라 했지만 다른 쪽이 병합됐다. 효과는 같다 — 캐주얼 요약이 저장된다 |
| C4 `TONE_FIELDS`에 sentence 없음 | **그대로(C3 수정으로 더 중요해짐)** | `gates.py:22 TONE_FIELDS = ("title", "content")`; `:20-21` 주석 "sentence는 저장되지 않으므로"는 `nodes.py:488-490`과 **모순**(지금은 저장된다) | 저장되는 캐주얼 `sentence`의 수치·날짜 드리프트를 아무 게이트도 보지 않는다 | 주석도 함께 고쳐야 함 |
| C5 judge 파싱 실패 → "PASS" | **수정됨(PR #9)** | `evaluators.py:346-350`(v2는 `_fail`로 닫음), `nodes.py:15`가 v2 `NewsletterEvaluator`를 쓴다 | v1 휴리스틱은 `evaluators.py:237-245`에 기준선용으로만 남음 | **추가 발견**: `ClusterEvaluator`에는 같은 휴리스틱이 남아 있다(`evaluators.py:125-135`, confidence 0.1). ADR 0009 A3가 분석에서 제외하지만 운영 경로는 판정으로 쓴다 |
| C6 죽은 설정 2개 | **달라짐** | `settings.py`에 `MIN_NEWSLETTER_SCORE` 없음(삭제됨). `MIN_CLUSTER_CONFIDENCE`는 `:74-77`에 "(미사용)" 주석과 함께 **의도적으로 보존** — ADR 0023 통합 기록 1항: ADR 0009가 ROC 결과로 게이트 연결을 사전 등록했으므로 지우지 않는다 | 설계 C6 "둘 다 삭제"는 ADR 0009 사전 등록과 충돌한다 → **설계 수정**: ROC 결과 뒤에 처리 | |
| C7 `reasoning_effort` 미설정 | **그대로** | `core/llm/`에 `reasoning`·`thinking`·`thought`·`cached` 문자열 0건; `adapters.py:263-270`는 `model/messages/response_format/temperature/max_tokens`만 전달; usage는 `:100-103`에서 `prompt_tokens`·`completion_tokens`만; `client.py:14-17 LLMUsage` 2필드 | thinking 토큰이 출력 단가로 과금되는지 여부와 양은 **미지수** → E0 지표(`hidden_tokens = total − prompt − completion`, 응답에 `total_tokens`가 오면) | |
| C8 실호출 검증 스키마 1개 | **그대로** | `core/llm/schemas.py` 8모델 중 운영 경로 5개(`ClusterEval`·`NewsletterContent`·`NewsletterMeta`·`NewsletterEvalV2`·`ToneResult`); ADR 0005 부록은 1개만 실호출. pre-flight 서브커맨드 없음. `reports/llm/` 없음(`reports/`에는 ops·recsys·serving·sim만) | `evaluation/llm/bakeoff.py generate --split warmup`은 bake-off용 단일 패스 러너라 그래프 경로가 아님 | E0 안에 합성 입력 pre-flight 5호출로 넣는다(A8) |
| C9 선택 순서·캡 | **그대로** | `stages.py:371 all_ids = sorted(list(clusters.keys()), reverse=True)`, `:372 [:limit]`; 캡 설정 없음(`settings.py`에 `LLM_DAILY_*` 없음); `jobs/tasks/generate.py:15 --limit`만 | R2 근거 재확인: `hdbscan_clusterer.py:117 ORDER BY N.raw_news_id`, `:73-75 setdefault`, `:164 enumerate(groups)` | 설계의 `:216`은 지금 `:371` |
| C10 서킷브레이커·예산 상한 부재 | **그대로** | 402/401/404는 호출 1회에서는 즉시 실패(`adapters.py:166-171`)지만 그래프가 클러스터마다 재생성 3회를 돈다(`graph.py:102-121`, `nodes.py:273-279`); 킬 스위치는 `LLMResult(error="kill_switch")`(`kill_switch.py:66-77`) → 생성기 폴백(`generator.py:155-156`) → 사실성 노드 차단 → **재생성 라우팅** → 클러스터당 생성 3회×2호출 시도. 스키마 실패 재시도 상한 10(`adapters.py:86`, `llm_client.py:20`=`MAX_LLM_CALL_RETRIES` `settings.py:124`). 스레드 공유 브레이커 없음. `jobs/tasks/generate.py:23-26`는 시작 전 1회만 확인 | **최악 경로 재계산**: 클러스터 평가 3 + 생성(본문+메타) 3×2 + judge 3 + 문체 **6**(`MAX_RETRY_TONE_VALIDATION=2`로 `convert()` 안 3회 × 드리프트 재변환 1) = **18호출**, deadline 180s씩이면 **54분**(설계 R13의 45분보다 길다) | BudgetGuard는 warmup `budget.py`에만(미병합) |
| C11 호출 단위 비용·thought·cached 미기록 | **그대로** | `llm_metrics.py:19-33 LLMCallRecord`에 비용·cluster/run id·숨은 토큰 없음; 파일은 `logs/llm_metrics_run{run_id}.json`(`stages.py:416`); `jobs/tasks/generate.py:36-42`가 요약만 `job_runs.stats`에 | `llm_calls` 테이블 없음(`backend/alembic/versions/` 13개 중 해당 없음) | R5 재확인 |
| C12 소스 준비 | **그대로** | `generator.py:16-28`(본문 긴 순 10건), `:97 [:1500]`(문장 중간 절단), `:98-106`(출처/제목/본문만); `evaluators.py:308-316` 동일 | `core/reconstruction/`에 `sources.py` 없음 | E0 입력 통계(언론사 수·near-dup·절단율)가 설계 입력 |
| C13 날짜 미대조 | **그대로** | `gates.py:90-94`는 numbers/quotes/entities만; `faithfulness.py:444-454 check_against_sources`에 날짜 인자 없음(`:270-292`는 추출만) | ADR 0010 "결과와 한계" 자인 그대로 | |
| C14 규칙 검사기 없음·'뉴스' 필러 | **그대로** | `validator.py:40-50`(키워드 5개 미만이면 제목·요약 토큰으로 채움), `generator.py:174-175`(`"뉴스"` 필러); `gates.py`에 `check_style_rules` 없음 | E0에서 저장본에 대해 오프라인 규칙 검사(shadow)를 돈다(A8) | |
| C15 두 번째 서브그룹 폐기 | **그대로** | `nodes.py:141-157` `sub_groups.sort(key=len, reverse=True)` 후 `[0]`만 | E0에서 "두 번째 그룹 ≥3건" 건수를 센다 | |
| C16 프롬프트 규칙 vs 아카이브 | **그대로** | `prompts.py:64-76` 1,000~1,500자·`:84` 4문단, `:110` 제목 15자, `:131-140` sentence 예시가 감정 자극형("벼락거지" 등) — `:61`의 금지어 규칙과 충돌 | | |
| C17 `_locate` 정확 부분 문자열 | **그대로** | `bakeoff_analysis.py:191-196` `.find(claim.strip())` | E0에서 judge `unsupported_claims`의 위치 추적 성공률을 센다 | |
| C18 평가셋 프레임 = 생성 실행 | **그대로** | `evalset.py:363 SELECT run_id, cluster_log FROM cluster_history`; `jobs/tasks/cluster.py:1-5` "cluster_history는 쓰지 않는다"; `cluster_snapshot` 없음 | 이번 계획에서는 동결 저장소의 클러스터링 스냅숏(ids)이 프레임을 대신한다(2.3) | |
| C19 프로비넌스·출처 없음 | **그대로** | `batch_manager.py:186-196` INSERT 컬럼(제목·요약·본문·키워드·`raw_news_count`·시각·`run_id`·`generation_history`); `backend/app/api/newsletter.py:26,70,195 raw_news_count` | | |
| C20 구분자 없는 본문 삽입 | **달라짐(부분)** | `generator.py:98-106`은 `---`·`[기사 n]` 구분자를 **쓴다**; 없는 것은 "기사 안 지시·광고·구독 유도 무시" 시스템 문장과 보일러플레이트 블록리스트 | 설계의 "구분자 없이"는 부정확 → "지시 무시 문장·블록리스트 없음"으로 고쳐 읽는다 | |

**그 밖에 이번 확인에서 드러난 것**

- `docker/crontab`의 `generate`는 주석 처리, `docker/crontab.micro`에는 없다(설계와 같음). VM의 코드는 09-26 스냅숏이고 Alembic `d48994e9d26e`(코드 head `8b7f830013b7`) — `news_raw`·`press`·`cluster_history`·`news_letter`는 그 리비전에 다 있다. `[KR-ops]`
- **Mac 가상환경에 `FlagEmbedding`이 없다**(`hdbscan`·`kiwipiepy`·`langgraph`는 있음). 워크플로우의 `embed_newsletter_node`(`nodes.py:331-338`)는 Mac에서 그대로 돌 수 없고, 실패하면 임베딩 `None`으로 저장을 계속한다(`:385-390`). 2.3절의 "기록만" 이탈과 맞물린다. `[코드]`
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
| 30일 보존 | 30일 넘은 ok 행 0건(첫 삭제 대상 ≈ 10-26). 보존 잡 미구현(ADR 0023) | 같음 |
| Mac 덤프 | `data/backups/mac-compose-2026-09-26.dump` 899행·임베딩 845개(5.9 MB), `tier0-micro-2026-09-26T2218.dump`(795행) | 디스크 확인 |
| Colab | CLI `~/.local/bin/colab`(`new/run/exec/upload/download/ssh/stop/usage`), T4 ≈1.07 CU/h · CPU ≈0.08 CU/h, 잔액 ≈199 CU. **클라이언트 없는 세션은 ~15분 안에 사라졌고 3.4h 세션 하나를 잃고 체크포인트로 복구한 전례**(ADR 0013 A3.9) | 과제 조건·ADR 0013 |
| GCS | 비공개 버킷 + 서명 URL(≤12h) 절차 있음(`.ops/RUNNING.md`). `colab upload/download`가 있으므로 E0 경로에는 **필요 없다** | |
| Gemini | 선불 ₩8,000(자동충전 꺼짐), 10-06 02:20 결제 복구 확인. generator `gemini-3.5-flash-lite`, judge `gemini-3.1-flash-lite`, tone `gemini-3.5-flash-lite`(`registry.py:81-85`). 단가 `config/llm_pricing.yaml`: 3.5-lite $0.30/$2.50, 3.1-lite $0.25/$1.50 (1M 토큰, 접근 2026-09-25/26) | `[코드]` `[문서]` |
| 다른 프로바이더 키 | 없음 → 교차 벤더 judge(solar-pro3)는 키 확보 전까지 불가; shadow judge는 같은 계열(`get_client`가 WARNING) | |
| GitHub Actions | 단위 + Postgres/pgvector 통합 테스트, `workflow_dispatch` 가능, **API 키 없음**, 공개 저장소(아티팩트가 곧 공개) | `.github/workflows/ci.yml` |
| A1 | 10-06 04:43까지 4,215회 시도·성공 0 | ADR 0026 |
| Mac | 상주 서버·DB·docker·임베딩·대형 클러스터링 금지; 짧은 계산은 `nice -n 19` + 스레드 2, ≤10분. LLM API 호출은 가능 | 과제 조건 |

### 2.2 무엇이 어디서 돌아야 하나 — 단계별 요구 자원

| 단계 | 입력 | 자원 | 비고 |
|---|---|---|---|
| 기사 임베딩(BGE-M3, 1,024-d) | 본문 13~18k건 | GPU 또는 큰 CPU. 모델 fp32 로드만 3.6 GB(ADR 0006 증거 5) | Micro(1 GB) 불가, Mac 금지, A1 없음 |
| 클러스터링(HDBSCAN+split_v2) | 하루 창 1~2k 벡터 | CPU 수 초~수 분, 메모리 수십 MB | Mac에서 `nice` 가능. 운영 코드 경로 그대로 써야 함 |
| LangGraph 생성 | 클러스터 기사 본문, LLM API | 네트워크 대기. **단, `embed_newsletter_node`가 BGE-M3를 올린다**(`nodes.py:331-338`) | LLM은 Mac 가능. 임베딩 노드는 2.3의 이탈 |
| DB 접촉점 | `_load_data_from_db`(`hdbscan_clusterer.py:97-143`), `create_new_batch/update_cluster_log`(`batch_manager.py`), `save_news_letter`(`:146-234`), 저장 노드의 임베딩 UPDATE(`nodes.py:527-535`) | Postgres+pgvector, 또는 같은 계약의 다른 저장소 | 접촉점은 **4곳**이라 계약으로 뽑기 쉽다 |
| 라벨링 | 저장본 + 원문 | Mac 로컬 `labeling_app`(`127.0.0.1:8765`) | 밤 시간, ≤1h 세션 |

### 2.3 데이터 경로 — 대안 비교와 선택

| | **A. SSH 터널 → VM 운영 DB 직접** | **D. SSH 터널 → VM 안 일회용 실험 DB**(ADR 0026 증거 7의 `a1_rehearsal` 방식) | **B. Colab 안 임시 Postgres(덤프 복원)에서 전부** | **C. 동결 반출 + 파일 저장소(채택)** |
|---|---|---|---|---|
| VM 메모리(1 GB) | 벡터 13~18k건 × ≈4 KB UPDATE(≈70 MB) + 생성 중 4스레드 조회. 소크 최솟값 MemAvailable 239 MiB(ADR 0026 증거 8) 위에서 미측정 부하 | 같은 Postgres 안에 DB 2개 + 벡터 70 MB. 리허설 때 최소 583 MiB였지만 벡터 쓰기는 안 해 봤음 | **읽기 전용 `COPY` 1회**(55 MB) 또는 `pg_dump`(51 MB). ingest 사이(:20~:50)에 돌리면 됨 | **B와 같음(`COPY` 1회)** |
| 운영 오염 | **있음** — `news_letter` 행·`news_raw.news_letter_id`가 운영 DB에 남아 그 기사가 영구 제외(C2가 그대로라 제외 기사까지) | 없음(DROP DATABASE) | 없음 | 없음 |
| 저작권 노출 | 본문은 VM에만. Colab 임베딩을 위해서는 어쨌든 본문이 Colab으로 간다 | 같음 | 덤프 전체(제목·URL·본문)가 Colab에. 세션 종료 시 소멸 | 본문 55 MB가 Mac `data/`(gitignore, 덤프와 같은 노출 수준)와 Colab(일시)에. 30일 규칙을 반출본에도 적용 |
| 재현성 | 낮음 — DB가 매시 바뀜. 멤버십을 따로 저장해야 함 | 중간 — 실험 DB는 만든 시점 동결, 그러나 VM 회수 시 소멸 | 높음 — 덤프 SHA 고정, 세션마다 복원 | **가장 높음** — 반출 파일 SHA + 임베딩 `.npy` SHA + 의사 시각 `now`로 클러스터링까지 결정론. 같은 입력으로 E0·E4'·E2를 돈다 |
| 비밀 이동 | Gemini 키 Mac에만 | Mac에만 | **Gemini 키를 Colab에 넣어야 함** | Mac에만 |
| 세션 취약성 | SSH 끊김만 | 같음 | Colab 세션 소멸이 생성 중간을 끊음(LLM 비용 낭비, 체크포인트 필요) | Colab은 임베딩 1~2시간만. 생성은 Mac |
| 코드 변경 | 없음(env만) | 없음(env만) | 없음(런타임 부트스트랩 스크립트) | **저장소 계약 1 PR**(2.2의 4접촉점) |
| 노력 | 낮음(터널·벡터 반입 스크립트) | 낮음~중간(DB 생성·DROP 절차, 백업 선행) | 중간~높음(Postgres+pgvector apt, alembic, 키·keeper·체크포인트) | 중간(계약 PR + 반출/반입 스크립트 2개) |
| 치명적 단점 | 운영 오염, VM 부하 미측정, "VM을 바꾸지 않는다" 위반 | VM 부하 미측정, 회수 시 소멸, 사용자 승인 필요 | 비밀 이동 + 세션 취약성 + 가장 큰 설치 비용 | 뉴스레터 임베딩 노드를 Mac에서 못 돌림(FlagEmbedding 없음) → 선언된 이탈 필요 |

**선택: C.** 이유는 세 가지다. (1) 1 GB VM에는 읽기만 닿고 운영 DB를 더럽히지 않는다 — C2가 그대로인 코드로 운영 DB에 생성을 돌리면 제외
기사까지 소진된다. (2) 동결 입력은 그 자체가 뒤의 사전 등록 실험(E4' 두 팔, E2 두 팔)의 **공통 입력**이 된다 — 두 팔이 같은 바이트의 기사를
본다는 것을 SHA로 증명할 수 있다. (3) Gemini 키와 생성은 Mac에 머물고, Colab은 임베딩만 한다(짧고, 결과를 `colab download`로 받는다).
B가 "코드 변경 없음"이라는 점은 매력적이지만, Postgres를 Colab에 세우고 키를 옮기고 세션을 지키는 비용이 저장소 계약 PR보다 크고, 그 투자는
E0에서 끝난다. C의 계약은 페이크 LLM 그래프 테스트가 Postgres 없이 끝까지 돌게 하는 부수 효과도 있다.

이 선택은 **ADR로 옮겨야 효력이 있다.** WP1 PR에 새 ADR(번호는 그 PR 시점의 [ADR 색인](../adr/README.md) 기준 다음 빈 번호 — 설계·아키텍처 리뷰가
예약한 0012·0021·0022·0027~0030·0032·0034를 피하면 0035)을 "실험 데이터 경로: 동결 반출·파일 저장소·Colab 임베딩"으로 쓰고, 증거는 반출 매니페스트
SHA·왕복 테스트·임베딩 fp16/fp32 대조표를 붙인다.

**선언된 이탈 하나 — 뉴스레터 임베딩 노드.** E0은 "현재 파이프라인"을 재는데 `embed_newsletter_node`는 Mac에서 돌 수 없다. 기본안은 **임베더를
"기록만"으로 둔다**: 노드는 그대로 실행되지만 주입된 임베더가 벡터 대신 형식체 텍스트의 SHA를 기록하고 `None`을 돌려 준다(노드는 이미 실패 시
`None`으로 계속한다, `nodes.py:385-390`). 이 벡터를 읽는 하류 노드는 없으므로 E0의 어떤 지표도 바뀌지 않고, 저장본 임베딩은 Colab에서 뒤에 한 번에
계산한다. 뉴스레터당 지연에서 임베딩 시간(ADR 0006: 로드 6.8s + 건당 ≈1.5s)이 빠진다는 점은 리포트에 적는다. 대안은 생성 전체를 Colab CPU 런타임
(≈0.08 CU/h)에서 돌리는 것이고, 그러면 키 이동·keeper가 필요하다(사용자 결정 1).

### 2.4 데이터 흐름과 만들어야 할 것

```
[Tier 0 VM]  읽기 전용 COPY (ingest 사이, :20~:50)                         ← 먼저 runbook 6.5 백업을 당겨 온다
   └→ Mac data/exports/<T0>/articles.jsonl (id, press, title, body, url, created_at, crawled_at, sha256; ≈55 MB, gitignore)
       + manifest.json (행 수, 기간, 각 행 sha256의 sha256; 리포트에 이 값만)
[Mac → Colab T4]  colab new(T4) → colab upload (articles.jsonl, 코드 tarball) → scripts/colab_embed_articles.py
   NewsEmbedder(cuda, fp16, max_length 8192, 텍스트 규칙 f"{title} {content}"[:8000], 길이순 배치) — 운영 코드 그대로
   1,000건마다 part .npy 저장 + colab download (세션 소멸 대비) → 끝나면 colab rm 본문, colab stop
   └→ Mac data/exports/<T0>/emb.f16.npy + ids.npy + fp32 대조 100건(cos p10·min)
[Mac]  FileGenerationStore(articles, embeddings, now=t_k)  → NewsClusterer.cluster_news()  (nice -n 19, 스레드 2)
   └→ snapshots/day_k.json (membership: ids만, params, git sha)        ← C18의 cluster_snapshot 역할, E7 입력
   └→ evalset.stratified_sample(...)  → e0 manifest (ids, sha)
[Mac]  E0 러너: compile_workflow() 클러스터마다 app.invoke(state) — RecordingClient·플래그 임베더·문체 폴백 플래그 주입
   └→ data/experiments/e0/<run>/{calls.jsonl, clusters.jsonl, newsletters.jsonl(텍스트는 여기만)}  → e0_report → reports/llm/e0_baseline_v1.{json,md}
   └→ export-blind → labeling_app (밤) → labels → e0_labels_report
```

만들어야 할 것(코드는 전부 뒤의 PR에서, 이 브랜치는 문서만):

1. **저장소 계약**(WP1): `GenerationStore` — `load_articles(lookback_hours, exclude_clustered, now)`, `create_batch(cluster_log) → run_id`, `update_cluster_log`,
   `save_newsletter(article_ids, newsletter, run_id, history) → id`, `save_embedding(id, vec)`. `PostgresGenerationStore`는 지금 함수들을 그대로 감싼다(SQL 변경 0,
   기존 테스트의 monkeypatch 대상이 그대로 동작해야 함). `FileGenerationStore`는 반출 JSONL + `.npy`를 읽고 JSONL에 쓴다. 주입은 `get_metrics_collector()`와
   같은 모듈 단일 인스턴스 + `set_store()`(실험·테스트용). `_load_data_from_db`의 `NOW()`는 `now` 인자로 바뀐다(동결 시각 재현).
2. **반출·반입**(WP1): `scripts/export_news_raw.py`(ssh + `COPY ... TO STDOUT`, 읽기 전용 세션, 청크, 매니페스트), `scripts/import_embeddings.py`(ids 정렬 검증,
   차원 1,024, NaN 0, L2 노름 ≈1 확인).
3. **Colab 임베딩 잡**(WP2): `scripts/colab_embed_articles.py` — `m4_colab_driver.py`의 tarball sha256·URL 비노출·heartbeat 도구 재사용, part 체크포인트, CU 상한 4,
   S0 드라이 런(300건)으로 CU/h와 fp16/fp32 코사인을 먼저 잰다.
4. **E0 러너·리포트**(WP2): `evaluation/llm/e0_baseline.py`(계측 주입, 표본, 예산 가드, 재개 가능 JSONL), `e0_report.py`(집계만), `labeling_app` 입력 형식으로 export-blind.
   규칙은 [A8](../adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md)과 `evaluation/llm/preregistration/e0-baseline-v1.yaml`에 이미 고정돼 있다.

### 2.5 계산량·비용 추정 `[추정]` — S0에서 실측으로 교체

**임베딩(Colab T4).** 토큰 분포는 `reports/ops/embedding_truncation_2026-09-26.json`(456건): p50 680·p90 1,633·p99 4,324 토큰 → 평균 ≈900 토큰으로 잡는다.
반출 시점 본문 ≈17,000건(10-06 13,346 + 하루 ≈1,000~1,500 × 3일) × 900 = **1.5×10⁷ 토큰**. BGE-M3(XLM-R large 568M) ≈ 2 × 568M = 1.14 GFLOP/토큰 →
1.7×10¹⁶ FLOP. T4 fp16 텐서코어 피크 65 TFLOPS의 25~35%(길이순 배치) = 16~23 TFLOPS → **740~1,060초 ≈ 12~18분**. 패딩·로딩·모델 로드(≈2분)를 넣어
**30~60분 ≈ 0.5~1.1 CU**, 3배 비관치로도 ≤2 CU. 비교: Mac MPS 실측 0.54~0.69건/초(ADR 0006) → 17k건 = 6.8~8.7시간의 Mac GPU 점유(금지);
A1 CPU 미측정·미확보; GitHub Actions는 CPU 0.3건/초 수준이면 ≈16시간 > 6시간 잡 한도이고 본문이 러너를 거친다(불가).
뒤의 실험(E2 등, 30일 창 안의 새 기사)을 위한 증분 임베딩은 하루 ≈1,000~1,900건 → 7일치 ≈10k건 ≈0.5~1 CU/회. **Colab 총 상한 6 CU**(잔액 199의 3%).

**LLM 호출 단가(현행 파이프라인, 클러스터 1건, 깨끗한 경로).** 한국어 문자/토큰 비율은 미지수라 **1토큰/자**로 잡는다(E0 지표). 입력 = 기사 10건 × 1,500자 + 프롬프트 ≈ 17k자.
thinking 토큰은 3.5-flash-lite 기본 on(설계 R8)이라 출력에 더한다.

| 호출 | 모델 | 입력 토큰 × 단가 | 출력(+thinking) 토큰 × 단가 | 비용 |
|---|---|---|---|---|
| cluster_eval | 3.1-lite | 6건 × 560 + 900 ≈ 4,300 × $0.25/M = $0.0011 | 200 × $1.50/M = $0.0003 | **$0.0014** |
| content gen | 3.5-lite | 17,000 × $0.30/M = $0.0051 | 1,500 + 1,000 = 2,500 × $2.50/M = $0.0063 | **$0.0114** |
| meta gen | 3.5-lite | 2,700 × $0.30/M = $0.0008 | 200 + 500 = 700 × $2.50/M = $0.0018 | **$0.0026** |
| judge v2 | 3.1-lite | 18,000 × $0.25/M = $0.0045 | 400 + 300 = 700 × $1.50/M = $0.0011 | **$0.0056** |
| tone | 3.5-lite | 2,700 × $0.30/M = $0.0008 | 1,700 + 500 = 2,200 × $2.50/M = $0.0055 | **$0.0063** |
| **합계(깨끗한 경로)** | | | | **$0.0273** |

기대값(재생성 30%·문체 재시도 20%·클러스터 평가 재시도 20% 가정): 0.0273 + 0.3 × (0.0114+0.0026+0.0056) + 0.2 × 0.0063 + 0.2 × 0.0014 = **$0.035/클러스터**.
최악(18호출, 1절 C10): 3 × 0.0014 + 3 × 0.0196 + 6 × 0.0063 = **$0.101/클러스터**.

**환율 가정**: ₩1,400/$ → ₩8,000 = **$5.71**(₩1,500/$이면 $5.33). 청구는 Google의 월 환율이라 리포트에는 USD와 청구 환율을 함께 적는다.

---

## 3. 마일스톤 재배열 — 의존, 증거, 비용, 라벨

설계의 M0~M8을 세 가지 이유로 바꾼다: (a) E0이 "전" 숫자가 되도록 정합 패치보다 **앞**에 둔다, (b) 임베딩·저장소가 없으면 어떤 실험도 못 돌므로 데이터 경로가 M0다,
(c) 교차 벤더 키가 없으므로 shadow judge는 당분간 같은 계열이고 그 사실을 모든 리포트에 적는다. 수집 복구(설계 M0)는 Tier 0 단독 수집으로 대체됐다(ADR 0026).

| M | 내용 | 선행 | 산출 증거 | LLM 비용 기대/상한 | Colab | 라벨 |
|---|---|---|---|---|---|---|
| **M0 = WP1** | 저장소 계약 + 반출/반입 + 새 ADR(제안됨) | runbook 6.5 백업 1회(사용자) | 계약 테스트(Postgres 구현 = 기존 SQL 그대로, 파일 구현 왕복), 반출 매니페스트 SHA, 기존 924 테스트 통과 | $0 | 0 | 0 |
| **M0b = WP2** | Colab 임베딩 잡 + E0 러너·리포트 + 사전 등록 동결(A8은 이미 있음, 실행 SHA만 A8.1에 추가) | M0 | S0 드라이 런 표(CU/h, fp16 vs fp32 cos p10·min), 임베딩 17k건 `.npy` SHA, 3일 멤버십 스냅숏(ids), E0 표본 매니페스트 | $0 (pre-flight 5호출은 E0에 포함) | S0 0.3 + 본 0.5~2 (상한 4) | 0 |
| **E0** | 기준선 측정(A8): warmup 2 + eval 15 클러스터, main 그대로 | M0b | `reports/llm/e0_baseline_v1.{json,md}`, 첫 `news_letter`(파일 저장소) ≥1건 — **2월 이후 첫 완주**, 실패 분류표, ADR 0010 증거 갱신(실제 차단율) | **$0.62 / $1.00** | 0 | **≈2.5h**(라벨은 결과 열람과 무관하게 30일 안) |
| **M1 = WP3** | 정합 패치 축소판: C2·C4(+주석)·C7(`reasoning_effort` 전달·usage 확장)·C10 중 "재시도 금지 목록"(402/401/404/킬스위치/`content_filter`/`length`는 그래프 재생성 진입 금지, 스키마 실패 ≤2)·ClusterEvaluator 휴리스틱 제거. C6은 ADR 0009 ROC 뒤 | E0 완료(전 숫자 확정 뒤) | 테스트(사용 기사만 FK, sentence 드리프트 차단, 402 뒤 추가 호출 0), reasoning pre-flight 5스키마×{none, minimal} 표 | 10호출 **$0.04 / $0.10** | 0 | 0 |
| **M2** | 예산·선택·관측: `BudgetGuard`(warmup `budget.py` 승격)·`RunCircuitBreaker`·일일 캡·사전순 선택·`llm_calls`·스냅숏(파일 저장소에는 이미 있는 것을 Postgres 구현에 추가), **E6(a)** 장애 주입, **E7** 기술 통계(동결 반출 3~5일 재클러스터링) | M1 | 장애 주입 테스트(중단까지 호출 ≤ 워커×3), E7 표 | $0 | 0.5(E7용 증분 임베딩 포함 가능) | 0 |
| **M3** | 소스 준비·절대 날짜 대조·규칙 검사기(shadow)·프롬프트 v2·두 번째 서브그룹 재큐잉, **E4'**(20클러스터×2팔: 분리 vs 병합) | M1 | `test_prepare_sources`, 날짜 프로브 표, E0 저장본 규칙 위반률 표(사후 재계산), E4' 리포트 | E4' 20 × (0.013 + 0.020) = **$0.66 / $0.90** | 0 | 0 |
| **M4** | 아키텍처 A/B 사전 등록(ADR 0009 **A9** + `arch-ab-v1.yaml` + `power.py`; E0의 차단율·문장 수가 검정력 표의 입력), 교차 벤더 judge 키 확보(사용자), **E1** 10클러스터 | M3 | 결과 파일 없는 SHA, `claim_anchor_feasibility.md` | E1 10 × (16k×0.30 + 2k×2.50)/M = **$0.10 / $0.30** | 0 | 0.3h(실패 인용 분류) |
| **M5** | 클레임 앵커 v1 + 프로비넌스(C19) + 팔 B 워밍업 3클러스터 | M4(E1 통과) | 페이크 그래프 테스트, 마이그레이션 왕복, API 스냅숏 | 3 × 0.015 = **$0.05 / $0.15** | 0 | 0 |
| **M6** | **E2 → E3**: 40×2팔(동결 저장소의 새 창, 30일 안), 라벨 L0~L3, 판정기 보정 | M5, 증분 임베딩 | `arch_ab.{json,md}`, `judge_calibration.md`, ADR 0021·0022 | 80 × 0.0165 × 1.3 = **$1.72 / $2.20** | 0.5~1 | **≈9.4h** |
| **M7** | 스토리 연속성 측정 **E5**(스냅숏 5~7일) → 조건부 최소판 | M2 + 5~7일 | `story_linking_v1.md`, ADR 0012 | $0 | 0 | 0.5h |
| **합계** | | | | **기대 0.62 + 0.04 + 0.66 + 0.10 + 0.05 + 1.72 = $3.19 ≈ ₩4,470(56%) · 상한 합 1.00 + 0.10 + 0.90 + 0.30 + 0.15 + 2.20 = $4.65** → **프로그램 하드캡 ₩6,000($4.29)**: 누적이 이에 닿으면 E2의 n을 40→30으로 줄이고(검정력 표 재게시), 그래도 넘으면 E2를 멈춘다 | **≤6 CU** | **≈12.7h** |

주의할 점.
- M6의 shadow judge가 같은 계열(Gemini)이면 ADR 0009 A4의 교차 계열 κ는 **계산할 수 없다** — 키가 없으면 E3는 "shadow 기록·κ 미산출"로 축소되고 그 사실을 적는다(사용자 결정 4).
- 30일 창(ADR 0023): 반출본의 각 기사도 `crawled_at + 30일`에 본문을 비운다(`scripts/export_news_raw.py --purge`). E0 라벨 마감 = 표본 중 가장 오래된 기사의 수집 시각 + 30일.
- Colab 세션 규칙(ADR 0013 A3.9에서 배운 것): keeper 클라이언트를 붙이고, part 단위로 `colab download`하며, 세션 소멸 시 재개(part 건너뛰기). 끝나면 본문을 지우고 `colab stop`.

---

## 4. 첫 세 작업 패키지 (PR 1개씩)

### WP1 — 생성 경로 저장소 계약 + Tier 0 읽기 전용 반출/임베딩 반입 (코드, 동작 변화 0)

- 범위: `ai_workspace/db/generation_store.py`(계약·Postgres 구현·파일 구현), `core/clustering/hdbscan_clusterer.py`(`_load_data_from_db` → `store.load_articles(..., now)`),
  `pipeline/stages.py`(`create_new_batch`/`update_cluster_log` → store), `workflow/nodes.py`(저장 노드 → store), `scripts/export_news_raw.py`, `scripts/import_embeddings.py`,
  새 ADR(제안됨) + `docs/adr/README.md` 행, `docs/runbook-hosting.md`에 반출 절(읽기 전용·ingest 사이·백업 선행).
- 수용 기준: 기존 테스트 전부 통과(모듈 함수 monkeypatch가 그대로 먹도록 Postgres 구현은 기존 함수에 위임); 파일 구현 왕복 테스트(기사 50건 합성 → 클러스터링 → 저장 → 읽기);
  `now` 주입으로 같은 입력에서 같은 멤버십(결정론 테스트); 반출 스크립트는 본문을 stdout·로그에 찍지 않고 매니페스트만 출력; 통합 테스트(CI)에서 Postgres 구현이 `news_raw` FK 갱신을 기존과 동일하게 수행.
- 크기: 코드 ≈400행 + 테스트 ≈250행. LLM 호출 0, Colab 0.
- 사용자 선행 조치: runbook 6.5 백업 1회(1~2분, 읽기), 반출 실행 승인(읽기 전용 `COPY` 55 MB).

### WP2 — Colab 임베딩 잡 + E0 러너·리포트 (코드, `evaluation/`·`scripts/`만)

- 범위: `scripts/colab_embed_articles.py`(+ `requirements-colab-embed.txt`: FlagEmbedding 1.2.5·torch 핀), `evaluation/llm/e0_baseline.py`(표본 → 러너 → 재개 가능 JSONL; A8·yaml을 읽고
  설정 불일치면 시작하지 않음), `evaluation/llm/e0_report.py`(집계·Wilson CI·실패 분류·규칙 위반 shadow·C2/C4/C15/C17 계수), `labeling_app` 입력(export-blind) 호환,
  `reports/README.md` 행 예약, A8.1(실행 SHA·반출 매니페스트 SHA·임베딩 SHA 기록, 결과 열람 전).
- 계측(동작 불변 보장): `core.reconstruction.generator`·`workflow.evaluators`·`core.tone_converter`의 `get_client`를 `RecordingClient(client_for(resolve_role_config(role)))`로 패치,
  `workflow.nodes.ToneConverter`를 폴백 플래그 서브클래스로, `workflow.nodes.get_shared_embedder`를 기록 전용 임베더로. 프롬프트·온도·max_tokens·재시도는 건드리지 않는다.
- 수용 기준: 페이크 LLM으로 러너 E2E(기록 필드 전부 채워짐, 예산 가드가 상한에서 멈춤, 재개 시 끝난 클러스터 건너뜀); 리포트에 기사·뉴스레터 **텍스트 0바이트**(테스트로 확인);
  S0 드라이 런 300건(≤0.3 CU)에서 CU/h·fp16/fp32 cos p10 ≥ 0.999 기록; 본 임베딩 ≤4 CU.
- 크기: 코드 ≈600행 + 테스트 ≈300행. LLM: pre-flight 5호출(합성 입력) ≈$0.02. E0 본 실행은 PR 병합 **뒤** 별도 승인으로.

### WP3 — 정합 패치 M1 축소판 (코드, E0 "후" 비교의 첫 변화)

- 범위: C2(`nodes.py` 저장 노드가 `current_articles`의 id만 연결; warmup `eab2b89`의 테스트 이식), C4(`gates.py TONE_FIELDS`에 `sentence` + 주석 정정, 드리프트 테스트 +1),
  C7(`adapters.py` `reasoning_effort` 역할별 env 전달 — 기본값은 pre-flight 결과로; `LLMUsage`에 `total_tokens`·`thought_tokens`·`cached_tokens` nullable, `llm_metrics` 기록),
  C10 축소판(`route_after_faithfulness`·`route_after_newsletter_eval`에서 `LLMResult.error ∈ {kill_switch, length, content_filter}` 또는 `http_status ∈ {401,402,403,404}`면 재생성 금지 →
  `failure_reason=infra`/`length`/`content_filter`; 스키마 실패 재시도 상한 2), `ClusterEvaluator` 텍스트 휴리스틱 제거(파싱 실패 = FAIL·`parsed=False`).
- 수용 기준: 장애 주입 테스트(연속 402: 클러스터당 추가 호출 0; 킬 스위치: 생성 1회 시도 뒤 종료), 사용 기사만 FK, sentence 수치 변조 차단, reasoning pre-flight 표(5스키마×{none, minimal}: parsed·지연·토큰·hidden). ADR 0010 Addendum(차단 유형 변경 없음, 재시도 정책 변경 기록), ADR 0005 부록 갱신.
- 크기: 코드 ≈250행 + 테스트 ≈200행. LLM ≈$0.04.

WP1·WP2는 서로 독립이 아니다(WP2가 WP1의 계약을 쓴다) — 순서대로. WP3는 E0 결과 파일이 커밋된 **뒤** 시작한다(전/후 분리).

---

## 5. 사용자가 정할 것 (기본값 제안)

1. **E0의 뉴스레터 임베딩 노드**: 기본 = Mac에서 "기록만" 임베더(벡터는 Colab에서 사후 계산, 지표 영향 없음, 지연에서 임베딩 시간 제외를 리포트에 명시). 대안 = 생성 전체를 Colab CPU에서(키 이동·keeper 필요).
2. **반출 승인**: Tier 0 VM에 읽기 전용 `COPY` 1회(55 MB, ingest 사이). 기본 = 승인, 단 runbook 6.5 백업을 먼저.
3. **E0 라벨 시간**: 기본 = 저장본 전부(≤15) + 클러스터 15 + 48h 재라벨 8 ≈ **2.5h**(3밤 × ≤1h). 축소안 = 출력 10건·클러스터 10 ≈ 1.7h(A8의 n은 그대로, 라벨만 부분집합).
4. **교차 벤더 judge 키**(Upstage `solar-pro3`, 설계 3.5): E2/E3 전까지 필요. 없으면 E3는 κ 미산출로 축소. 기본 = M4 시점에 결정.
5. **프로그램 LLM 하드캡 ₩6,000**과 E0 상한 $1.00: 기본 = 채택. `gcp-budget-guard`는 E0 실행 직전에만 켤지(10-06 결정 "지금은 켜지 않음") 그때 다시 묻는다.
6. **반출본 30일 규칙**: 기본 = 운영과 같은 `crawled_at + 30일`에 본문 비움(스크립트). 연장은 ADR 0023 개정으로만.

---

## 6. 이 문서가 바꾸지 않는 것

- ADR 0009 본문·A1~A7, ADR 0010·0013 A2/A3·0025의 사전 등록 문안. E0은 그 어느 규칙도 바꾸지 않는 **기술 통계**다(A8).
- 설계의 결정·수치·가설. 1절의 판정(C6 "둘 다 삭제"는 ADR 0009와 충돌, C20 "구분자 없음"은 부정확, C10 최악 경로 54분)은 설계를 고치지 않고 여기 적는다 — 설계 README 규칙대로.
- 코드. 이 브랜치에는 문서와 사전 등록 파일(`e0-baseline-v1.yaml`)만 있다.
