# AI 개인화 뉴스레터 추천 시스템 — 종합 아키텍처 리뷰 (2026-09-26)

- 기준: `main` `cff6767` + 인플라이트 워크트리 6개(wt-1 runtime/compose, wt-2 EB-NeRD, wt-3 LLM bake-off/gate, wt-4 realtime, wt-5 simulator, wt-69a team repro) + 플랜 `~/.claude/plans/binary-churning-nova.md`
- 입력: 다섯 개의 독립 리뷰(architecture / recsys / llm / ops / narrative). 이 문서는 그 다섯을 대조·조정해 **하나의 우선순위**로 묶은 종합본이다. 리뷰어 간 의견이 갈린 곳은 3.0절에서 누가 옳은지와 이유를 명시했다.
- 이 문서를 쓰기 전에 직접 다시 확인한 사실(2026-09-26 새벽, 읽기 전용):
  - Alembic 다중 head: wt-1 `f87f7378672e`와 wt-4 `8b7f830013b7`가 둘 다 `down_revision='e725a62ffef1'` — 두 브랜치를 그대로 병합하면 `alembic upgrade head`가 실패한다.
  - 코드가 참조하지만 파일이 없는 ADR: wt-1 → 0006(2회)·0013(1회), wt-3 → 0010(13회), wt-4 → 0015(13회), wt-5 → 0019(7회). 실제 존재하는 ADR은 0001–0005, 0007(wt-69a), 0008, 0009(wt-3)뿐.
  - `ai_workspace/core/tone_converter.py:82`는 `newsletter.get("summary")`를 읽지만 초안 dict의 키는 `sentence`(`core/reconstruction/generator.py:62,171`)다. `workflow/nodes.py:366-369`는 초안의 `sentence`가 있으면 그대로 두므로 캐주얼 요약은 저장되지 않는다.
  - wt-4 `backend/app/recsys/lgbm_scorer.py`에는 `resolve_feature_fn`만 있고 피처 함수 구현은 저장소 어디에도 없다. wt-2 `recsys_core/features.py:218`에 `compute_features(ctx, req, ...)`가 있다.
  - wt-4 `recommendation_impression_log` 컬럼: `impression_id, request_id, user_id, news_letter_id, position, score, source, model_version` — propensity·탐색 표시 없음.
  - `news_letter` 테이블(`backend/app/models/news.py:57-68`)에 story_id·model_id·prompt_sha 컬럼 없음. `hdbscan_clusterer.py:86`은 `news_letter_id IS NULL`로 기배정 기사를 영구 제외.
  - 임베딩 토큰 분포(스크래치 `sample_tokens.json`, n=27): p50 ≈ 605, p95 ≈ 1,533, max 1,997 토큰. ops 리뷰의 "p95 ≈ 3,700"은 문자 수에서 외삽한 값이고 실제 토큰화 표본은 2,048을 넘는 문서가 0건이다 → `max_length=2048` 컷은 안전하다.
  - `team_repro_v2.md`의 `N/A ± N/A` 자리표시자는 wt-69a `1585ce6`에서 이미 수정·커밋됐다(narrative 리뷰 E9의 절반은 완료). 남은 것은 main으로의 PR.
  - EB-NeRD small 임베딩은 14,848/20,738(72%, ≈2 art/s)로 진행 중 — 약 1시간 내 완료 예상.
  - wt-1 crontab: `ingest`는 매시(2시간이 아님), `generate/train`은 꺼져 있고 "ADR 0009/0010, 0013 결정 이후"로 주석 처리.
  - wt-3에 `evaluation/llm/bakeoff_analysis.py`·테스트가 untracked 상태.

---

## 1. 한 문단 결론

이 프로젝트의 병목은 코드 품질이 아니라 **증거 생산의 순서**다. 지금 저장소에서 CI가 통과하고 신뢰구간·SHA가 붙은 결과는 팀 보고치 MRR 0.897의 추론 시점 누출을 규명한 `team_repro_v2` 하나뿐이고, 나머지 모든 스토리(EB-NeRD ablation, 사전 등록 LLM 평가, 사실성 게이트, 클러스터링 품질, 요청 시점 서빙)는 도구는 완성됐지만 **숫자가 0개**다. 그 이유는 두 가지 선행 조건이 막혀 있기 때문이다: (1) 뉴스레터 생성 파이프라인이 2026-02 이후 한 번도 완주하지 않았고(HyperCLOVA 종료 → Gemini 402), (2) 한국어 기사 수집이 2026-09-25에야 시작돼 평가셋을 뽑을 5~7일치가 없다. 따라서 앞으로 2~3주의 우선순위는 "새 기능"이 아니라 **크리티컬 패스 해제 → EB-NeRD로 추천 쪽 양(+)의 수치 확보 → 한국어 데이터가 쌓이는 즉시 사전 등록된 LLM 실험 1건 완주 → 평가 가능한 서빙(노출·propensity 로그, 피처 parity) → 문서·이력서**의 순서여야 한다. 구조적 개선 중에서는 세 가지가 이 순서 안에서 감당 가능하고 이력서 가치가 크다: 결정론적 게이트가 문장의 19.7%만 보는 한계를 **클레임 앵커 생성(문장별 출처)**으로 뒤집는 것, 뉴스레터를 하루살이 아이템으로 두는 대신 **스토리 연속성을 먼저 측정하고 최소판(story_id + 생성 생략 규칙)**을 넣는 것, 그리고 노출 로그에 **propensity와 탐색 슬롯**을 넣어 실사용자가 생기는 순간 오프폴리시 평가가 가능한 상태로 만드는 것이다. 배포는 어떤 이력서 스토리의 전제도 아니므로 마지막에 두되, 수집 연속성만은 오늘 확보한다. 나머지(저장소 대수술, HNSW, Airflow, 대형 judge, two-tower, MIND)는 자르거나 미룬다.

---

## 2. 구조적 문제 Top 9 (근거 경로·수치)

우선순위는 "지금 손대지 않으면 다른 모든 것이 막히는가"로 매겼다.

### P1. LLM 파이프라인 미완주 + 한국어 데이터 부재 — 모든 LLM·클러스터링 증거의 선행 조건이 비어 있다
- 2026-02 이후 Stage5(뉴스레터 생성) 완주 기록 없음. ADR 0005 부록의 실호출 증거는 스키마 프로브 몇 건(`gemini-3.5-flash-lite` 1.8s 정상, `3.5-flash` 30s 타임아웃·503, `3.1-flash-lite` 8.1s)뿐.
- 로컬 compose DB(`newsletter`)는 2026-09-25 관찰 시 `news_raw` 381행(수집 1회분). `evaluation/llm/evalset.py`는 `cluster_history`에서 40클러스터를 층화 추출하므로 5~7일치가 필요.
- 파생 결과: ADR 0009 bake-off, judge κ, 검사기 P/R, 게이트 전후, B-cubed F1, 시뮬레이터 실스택 실행 전부 대기. 지금 켜지 않으면 2주 뒤에도 LLM 스토리에 숫자가 없다(narrative·llm·architecture 세 리뷰가 독립적으로 같은 결론).

### P2. "배포되는 추천 모델"이 사실상 없다
- wt-4에서 실제로 점수를 내는 것은 `HeuristicScorer`(장기 0.45·단기 0.35·recency 0.15·popularity 0.05)이고 이 가중치는 4주제 합성 코퍼스(`evaluation/serving/click_shift_sensitivity.py`)로 고른 사전값이다.
- `LightGBMScorer`는 `RECSYS_FEATURE_FN` 주입을 요구하지만 구현이 없다(검증). wt-2 `recsys_core.compute_features`의 입력형(`Requests/FeatureContext`)과 wt-4의 입력형(`UserState, Sequence[Item]`)이 달라 어댑터 없이는 학습 피처와 서빙 피처가 같은 코드를 탈 수 없다 → train/serve skew의 전형적 지점.
- 유일한 학습 데이터였던 팀 합성 로그는 순환적(100 LLM 페르소나 × 195건 전부 노출, 클릭률 47.6%, 세션 6.3h). point-in-time에서 current 모델 P@5 0.340 < popularity 0.484 < onboarding cosine 0.445, `best_iteration=[35,1,1]` — 3시드 중 2개는 학습이 안 됐다(`team_repro_v2.md` 2-1절). 팀 원본 LightGBM은 상수 피처 2개, 랜덤 negative, Cartesian 추론.
- EB-NeRD 하네스(7단계 ablation, P1/P2, 부트스트랩 CI)는 준비됐으나 임베딩 72%에서 결과 0건.

### P3. 사실성 평가의 커버리지와 루프 구조
- 팀 아카이브 뉴스레터 270건 분석: 뉴스레터당 9.7문장, 숫자/절대날짜/인용이 있는 문장은 517/2,620 = **19.7%**. `FAITHFULNESS_BLOCKING_TYPES=numbers,quotes`이므로 결정론적 게이트는 5문장 중 1문장만 판정하고, 나머지(주체·귀속·인과·사건 존재)는 judge에만 의존한다.
- judge v2는 미보정(`JUDGE_MIN_CRITERION_SCORE=3`, ADR 0009가 스스로 인정), 생성기와 같은 벤더(gemini 3.5-flash-lite / 3.1-flash-lite)라 자기선호편향 경고가 항상 뜬다(`core/llm/registry.py:160-169`).
- 실패 시 초안 **전체** 재생성(`MAX_RETRY_NEWSLETTER_EVAL=3`, 피드백 누적) — 비단조적이고 실패 1회당 생성+judge 비용이 그대로 반복된다.
- 문체 변환 단계는 입력 요약이 항상 비고 출력 요약은 버려진다(위 검증). 출력 토큰 단가가 입력의 8배인데 버려지는 출력을 매번 산다.
- 독자가 검증할 수단이 없다: API는 `raw_news_count`만 노출(`backend/app/api/newsletter.py`), 문장 단위 출처는 어디에도 없다.

### P4. 아이템 정체성 부재 — 뉴스레터가 하루살이라서 생기는 연쇄 효과
- `news_raw.news_letter_id` 단일 FK + `news_letter_id IS NULL` 필터(`hdbscan_clusterer.py:86`) → 어제 뉴스레터에 배정된 기사는 오늘 클러스터링에서 영구 제외. 같은 사건의 후속 기사가 2건뿐이면 `min_cluster_size=3`에 걸려 영원히 noise.
- `news_letter_created_at = NOW()`가 recency의 유일한 근거 → 같은 배치의 뉴스레터는 나이가 전부 같다(`backend/scheduler/calculate_ranking.py:35`, wt-4 `scoring.py`). 인기도도 클릭이 아니라 `raw_news_count`.
- 결과: (a) 같은 사건이 날마다 새 ID로 재생성돼 후보·history·MMR 중복, (b) 생성 주기를 1회/일보다 늘릴 수 없음(플랜 PR-15가 PR-20의 선행 조건), (c) 하루 15~30건으로 캡이 걸린 생성 예산(P6)의 일부가 중복 사건에 낭비, (d) 시뮬레이터의 reactivity/similar_share가 "어제 클릭한 사건의 오늘 재생성본 클릭"을 반응성으로 오인할 위험.
- 단, architecture 리뷰가 주장한 "team_repro의 popularity 미달이 하루살이 아이템의 증상"은 과장이다 — 3.0절 D1 참고.

### P5. 통합 부채 — 지금 당장 터질 것
- Alembic 다중 head(검증). 병합 전 merge revision 필수.
- 참조되지만 없는 ADR 5건(0006/0010/0013/0015/0019, 검증). "모든 결정을 ADR로"라는 이력서 문장이 검증 시 깨진다.
- 최상위 임포트 루트 7개(`ai_workspace/*`, `recommend_engine/src`, `backend/app`, `recsys_core`, `sim`, `jobs`, `evaluation`) + sys.path 조작 3곳 이상. `ai_workspace/pyproject.toml`이 `config`, `core`, `db`를 최상위 패키지명으로 설치.
- 피처 코드 3벌(팀 `feature_engineer.py` / `recsys_core/features.py` / wt-4 `scoring.py`), 벡터 파싱 4벌, DB 접근 스택 3개(psycopg2 풀 / SQLAlchemy / SQLModel).
- wt-3 `bakeoff_analysis.py` untracked, `docs/adr/README.md` 병합 충돌 이력.

### P6. 비용·자원 상한이 코드로 정해져 있지 않다
- 뉴스레터 1건 ≈ $0.017(llm 리뷰, judge 1회 가정)~$0.024(ops 리뷰, judge 2.5회 가정). 하루 100건이면 월 $50~72 — 월 $10 크레딧의 5~7배. 일일 생성 캡 없음(`Stage5`의 `limit`은 CLI 인자일 뿐).
- 임베딩 경로: `FlagEmbedding.BGEM3FlagModel` fp32, `max_length=8192`, batch 8, 스레드 미고정. colima 4 vCPU에서 모델 로드 174초, 컨테이너 RSS 4.6~5.6GiB, 355건 중 batch 0이 22분 동안 미완(호스트 과다구독 상태라 깨끗한 수치는 아님). 2 OCPU ARM 이론 추정 ≈18~22 s/기사 → 456건/일 ≈ 2.5h. p95 토큰이 1,533인데 8,192로 패딩하니 실효 토큰이 2~3배.
- `generate` 잡도 LangGraph 노드 안에서 BGE-M3를 로드(`workflow/nodes.py:258`) → 3GB 모델 로드를 두 잡이 치른다.
- Oracle A1(2 OCPU/12GB)은 "Out of host capacity" 수 시간째. Always Free는 7일 유휴(p95 CPU<20% 등) 회수 규칙까지 있다.

### P7. 노출 로그로 오프폴리시 평가를 할 수 없다
- wt-4 `recommendation_impression_log`에 position/score/source/model_version은 있지만 **propensity·탐색 여부가 없다**(검증). 실사용자가 생겨도 replay/IPS 계열 추정이 불가능하고, 로그가 "정책 A가 보여준 것"만 담아 정책 B를 평가할 수 없다.
- 프론트엔드는 `POST /logs/newsletter/click` 하나만 보낸다(`frontend/src/lib/api.ts:177`) — 노출·체류 신호 없음.

### P8. 내러티브 리스크(면접관이 5분 안에 보는 것)
- `team-final` 이후 79커밋이 2026-09-25 18:45~09-26 01:43(약 7시간)에 몰려 있고 73건 전부 `Co-Authored-By: Claude ...`. 숨길 수 없고 숨기려 하면 더 나쁘다.
- README는 팀 시절 문서(HyperCLOVA 배지, "MRR/nDCG 자동 산출", 팀 소개표에 이효창이 같은 영역). 비공개 포트폴리오가 0.897을 인용하고 있을 가능성.
- 기사 본문(`news_raw.raw_news_content`)이 무기한 보존되고 purge 정책이 없다. EB-NeRD는 라이선스상 Mac 밖 반출 금지.

### P9. 작은 결함 묶음(각 1시간 미만)
- `tone_converter` summary/sentence 키(검증), 죽은 설정 `MIN_NEWSLETTER_SCORE`·`MIN_CLUSTER_CONFIDENCE`, `user_age_band/user_gender` 상수 피처, `runner.py`의 미사용 `reset_db`, `stages.py:244-251`의 `min_target` 보충 로직(스토리 도입 후 의미가 바뀜), README의 `assets/architecture.png` 참조.

---

## 3. 가장 어려운 결정별 권고와 대안 비교

### 3.0 리뷰어 간 불일치 판정표

| # | 쟁점 | 의견 분포 | 판정과 이유 |
|---|---|---|---|
| D1 | 스토리 엔티티(옵션 B)를 지금 넣을 것인가 | architecture: 지금, 다른 모든 것의 선행 조건 / recsys·narrative·ops: 미룸 / llm: Must로 승격(클레임 차분) | **측정 먼저, 최소판은 R3, 전체판은 이후.** architecture의 진단(중복 재생성·recency 상수·생성 주기 제한)은 옳고 검증됐다. 그러나 "team_repro에서 popularity를 못 이긴 것이 하루살이 아이템의 증상"이라는 인과는 틀렸다 — recsys가 옳다: 그 데이터는 클릭이 카테고리 라벨에서 LLM으로 생성된 순환 데이터이고 모델도 상수 피처·랜덤 negative로 망가져 있으며, 기사 수명이 똑같이 짧은 EB-NeRD(클릭 기사 나이 중앙값 2.9~3.2h)에서 GBDT는 잘 작동한다. 반면 narrative의 "스토리는 4주 뒤"도 절반만 옳다: 생성 캡이 하루 15~30건이면 중복 사건 하나가 곧 예산 낭비이고, 이 점은 llm 리뷰의 "신규 클레임 <30%면 생성 생략" 규칙과 결합해 **예산 레버**가 된다. 따라서 architecture E1(오프라인 재생, LLM 0원, 2일)로 "일 클러스터 중 연속 사건 비율"을 먼저 재고, ≥20%면 nullable `story_id` + 연결 함수 + 생성 생략 규칙만 R3에 넣는다. 버전·UI 배지·클레임 차분은 그 뒤. |
| D2 | 배포를 언제 | ops: 이번 주 Tier 0 / narrative: 어떤 스토리도 요구하지 않음 / architecture: Mac+Cloudflare 데모 먼저, 배포 마지막 | **수집 연속성은 오늘, 공개 데모는 마지막.** narrative가 옳다 — 5개 스토리 중 배포를 전제로 하는 것은 없고 "배포됨"이라는 단어는 소크 증거 전엔 쓸 수 없다. 그러나 ops가 지적한 "잠자는 Mac"은 P1(5~7일 연속 수집)에 대한 실제 위협이다. 절충: (a) 오늘 Mac 네이티브(MPS)로 `ingest`를 launchd/caffeinate로 돌려 수집을 끊지 않는다(0일), (b) A1이 R2 시작 전까지 안 나오면 ops의 Tier 0 **데이터 플레인만**(E2.1.Micro Postgres + GitHub Actions `ingest`) 보험으로 세운다(1~2일), (c) API+Pages 공개 데모는 R4. |
| D3 | LLM 아키텍처 A/B(클레임 앵커 vs E2E+게이트)를 모델 bake-off보다 먼저 할 것인가 | llm: 먼저(Addendum) / narrative·플랜: ADR 0009 그대로 bake-off | **llm이 옳다, 단 게이트를 둔다.** 근거: (1) 문장 단위 미지원율은 뉴스레터당 ~10단위라 팔당 ~400단위 — n=40 홀리스틱이 20%p 미만을 못 가리는 검정력 문제를 정면으로 푼다. (2) 결정론적 게이트의 19.7% 커버리지는 모델을 바꿔도 안 변한다. (3) ADR 0009는 Addendum 절차를 갖고 있고 A1이 이미 기록돼 있어 "결과 미열람" 상태에서의 변경이 정당하다. 게이트: E2E 팔은 어차피 필요하므로 먼저 완주 → llm E1(10클러스터 feasibility, ≈$0.2)에서 앵커링 ≥85%가 안 나오면 ADR 0009 원안으로 복귀. narrative의 S2("결과 전에 규칙을 커밋했다")는 이 변경으로 약해지지 않고 오히려 강해진다. |
| D4 | 학습/서빙 parity 기준 | architecture: 피처 max\|Δ\|<1e-6 + 순위 동일 / recsys·플랜: top-20 겹침 ≥0.9 | **둘 다, 층을 나눠서.** 같은 함수를 타는 피처 벡터는 비트 동일해야 하고(겹침 0.9는 skew를 허용하는 기준), 후보 집합이 다를 수 있는 end-to-end 목록은 겹침으로 잰다. 플랜의 0.9 단독은 부족. |
| D5 | 탐색 메커니즘 | architecture: ε-균등 슬롯 + SNIPS / recsys: Thompson 슬롯 + replay | **ε-균등 먼저.** propensity가 정확히 계산되고(탐색 슬롯 1/\|pool\|) OPE 검증이 단순하다. Thompson은 실트래픽 이후. 검증은 두 리뷰 모두 시뮬레이터 replay/SNIPS 일치로 하자는 데 동의 — [SIM] 라벨. |
| D6 | LightGBM 존속 | recsys: ranker_v2 주력 + 사전 등록 승격 규칙 / architecture: shadow 먼저 / narrative: feature_fn+parity 없이는 "배포"라 쓰지 말 것 | **셋이 양립한다.** EB-NeRD로 학습한 ranker_v2를 shadow(점수만 로그)로 올리고, 사전 등록 승격 규칙(P1 nDCG@10 +0.02 vs popularity_24h, CI 하한>0; P2에서 cosine_history 이김; 정규화 손실 ≤1%p)을 통과할 때만 활성. 그 전까지 활성 스코어러는 휴리스틱(가중치는 recsys E8로 EB-NeRD에 적합). 이력서에는 "EB-NeRD에서 평가, shadow 서빙"까지만. |
| D7 | Airflow | architecture: 삭제 / ops·플랜: 프로필로 격리 유지 | **compose 프로필·Dockerfile 제거, DAG 파일은 `backend/airflow/`에 "archived, not run" README와 함께 보관.** 두 스케줄러 동시 기동 사고 위험을 없애면서 팀 시절 담당(Airflow)의 흔적은 정직하게 남긴다. 유휴 메모리 비교는 한 줄 실측으로 ADR 0006에. 사용자 결정(8절 #7). |
| D8 | 문체 변환 별도 호출 | llm: E4 결과에 따라 폐지 / 다른 리뷰: 언급 없음 | **키 버그는 지금 고치고(5분), 폐지 여부는 llm E4(길이×병합 2×2)로 결정.** 이견 없음. |
| D9 | ADR 번호 | 네 리뷰가 서로 다른 번호를 제안 | **코드가 이미 참조하는 번호(0006/0010/0013/0015/0019)와 플랜 배정(0011 클러스터링 v2, 0012 스토리, 0014 replay/MMR, 0016 HNSW, 0017 콜드스타트, 0018 잡 분리, 0020 배포)을 고정하고 신규는 0021부터.** 7절 참고. recsys의 0014/0016/0017/0018, ops의 0021~0024, narrative의 0012/0014 제안은 이 체계로 재배정했다. |
| D10 | 뉴스레터당 비용 | llm $0.017 / ops $0.024 | 차이는 judge 호출 횟수 가정(1회 vs 2.5회). 둘 다 추정 — **범위로 쓰고 ops E4에서 실측.** 캡 결정(15~20건/일)은 어느 쪽이든 같다. |
| D11 | 임베딩 `max_length` | ops: 2,048(단 p95 3,700이라는 자기모순) | **2,048 채택.** 토큰화 표본(n=27) p95 1,533·max 1,997. 3,700은 문자 수 외삽. |
| D12 | 스토리 연결 규칙 | architecture: cos≥τ & 개체 Jaccard≥j 격자 / llm: cos≥0.83 & Jaccard≥0.15, 0.75~0.83 구간만 LLM 쌍 판정 | 같은 계열. architecture E1의 격자(τ∈{0.80,0.85,0.90}×j∈{0.2,0.35,0.5})가 llm의 점을 포함하므로 **격자로 재고, LLM 쌍 판정은 선택 사항**. |
| D13 | LLM 관심 프로필 피처(recsys D/E5) | recsys: 조건부 채택 / narrative: 증거 없는 스택 추가 금지 | **R3 이후 선택 과제, 예산 ≤$5, null 결과도 기록.** EB-NeRD 제목을 외부 API에 보내는 것은 라이선스 재확인 필요(사용자 결정). |
| D14 | 임베딩 모델 교체(ops E2) | ops만 제안 | **미룸.** 지금은 `max_length`·스레드 고정·길이순 배치만(1시간, 수집 속도가 크리티컬 패스). int8/ONNX·모델 교체는 라벨셋(E7)이 생긴 뒤 B-cubed 손실 한도와 함께. |
| D15 | 저장소 단일 패키지화(옵션 D) | architecture: 동결 ADR로 축소 / 나머지: 암묵 동의 | **동결.** 새 최상위 루트 금지, sys.path 조작은 `jobs.setup_import_paths` 한 곳, 신규 코드의 벡터 파싱·DB 접근은 기존 것 재사용. |

### 3.1 추천 모델링

**문제 정식화(채택):** "요청 시점 t에, 최근 72h에 태어난 50~300개 후보를 유저 상태(장기·단기·세션·온보딩)와 아이템 상태(콘텐츠·시간·인기·클러스터 크기)로 재정렬하는 point-in-time 랭킹 + 얇은 탐색 계층." 유저 ID·아이템 ID 임베딩은 쓰지 않는다 — 순수 아이템 콜드스타트와 덴마크어→한국어 전이를 같은 메커니즘으로 푼다.

| 대안 | 요지 | 판정 |
|---|---|---|
| A. 언어 무관 스칼라 피처 위 LightGBM lambdarank(ranker_v2, in-view negative, EB-NeRD 학습) | hist_cos/short_cos/sess_cos/hist_len/hours_since_pub/pop_clicks_6/24/48h/pop_ctr_24h/cat_share/cat_match/raw_news_count; 요청 내 랭크 정규화로 언어 간 분포 차 흡수 | **주력.** RecSys Challenge 2024 상위 해법 계열(시간 인식 피처+GBDT). EB-NeRD 논문에서 NRMS는 popularity 대비 +1.3 AUC(61.03 vs 59.70)에 불과. CPU·12GB에서 학습·서빙 가능. |
| B. 다국어 인코더 위 two-tower/NRMS | 학습되는 유저 인코더 | **기각.** zero-shot 교차언어에서 학습 NNR이 크게 무너지고 고정 인코더 late fusion이 경쟁적(NaSE). 15k 유저로 과적합·시드 분산. GPU 의존. ADR 0003의 기각 사유를 뒤집을 근거 없음. |
| C. 학습 없는 선형 재랭킹(현 HeuristicScorer, 가중치를 EB-NeRD로 적합) | 4~6성분 로지스틱 회귀 | **폴백·베이스라인·초기 활성 모델.** recsys E8로 사전값(0.45/0.35/0.15/0.05)을 데이터 기반으로 교체. |
| D. LLM 생성 관심 프로필 → 임베딩 코사인 피처 | 히스토리 제목 → LLM 요약 → BGE-M3 | **선택(R3 이후).** k≤5 유저에서만 이득 가설; EB-NeRD 실제 클릭으로만 검증. |
| E. 탐색 계층(ε 슬롯 + propensity) | 20슬롯 중 2슬롯 균등 탐색 | **얇게 채택(R3).** 정확도 이득은 [SIM]으로만 동작 증명. |

**추가 프로토콜(recsys 제안, 채택):**
- **P3 "일일 배치 릴리스" regime**: EB-NeRD 기사 발행 시각을 그날 07:00 일괄 릴리스로 양자화하고 후보 풀을 당일(+전일) 배치로 제한해 릴리스 후 경과 시간 버킷별로 콘텐츠 코사인 vs trailing 인기도의 nDCG 교차점 h*를 구한다. 이 프로젝트만의 조건("모든 후보가 같은 시각에 태어난다")을 공개 데이터 위에서 재현하는 유일한 방법이고, 온라인 가중치 전환 규칙(인기도 카운트 부족 시 콘텐츠 가중)으로 그대로 옮겨진다.
- **콜드 유저 히스토리 절단 곡선**(k∈{0,1,3,5,10,all}): 개인화가 인기도를 CI 배제로 넘는 최소 k*를 콜드스타트 체인 전환 임계(`RECSYS_MIN_PERSONAL_EVENTS`)로 채택.
- **언어 전이 계약**: 요청 내 랭크 정규화 손실 ≤1%p, 덴마크어/한국어(시뮬레이터 히스토리) 피처 분포 KS ≤0.1, BGE-M3 덴마크어 sanity(카테고리 kNN LOO ≥0.6).
- **승격 규칙 사전 등록**(결과 전 ADR 0013에 커밋): ranker_v2 − popularity_24h ≥ +0.02 nDCG@10(유저 부트스트랩 95% CI 하한 >0) AND ranker_v2 − team_binary CI 하한 >0 AND P2에서 cosine_history 이김 AND `best_iteration>5` 전 시드. 실패 시 옵션 C 배포 + ADR에 기록.
- **parity 게이트**: `recsys_core/serving.py` 어댑터(`UserState+Items → Requests+FeatureContext`)로 wt-4 `feature_fn`을 고정하고, 시드 DB에서 `/newsletters/today` 200회 재생 → 전 컬럼 max|Δ|<1e-6, Kendall τ=1.0(동점 제외), 목록 top-20 겹침 ≥0.9. CI integration job에 추가.

### 3.2 LLM 생성·평가

| 대안 | 요지 | 판정 |
|---|---|---|
| A. 현행 유지 + 게이트 확장(개체명 차단, judge 2-fold 보정) | wt-3 설계 그대로 | 기준 팔(baseline arm)로 보존. 19.7% 커버리지 한계와 전체 재생성 루프의 비단조성은 남는다. |
| B. 사후 문장 정렬 + KLUE-NLI | 생성 후 문장별 원문 정렬 | 기각(다중 출처 합성 문장은 한 원문 문장에 정렬되지 않음: CAMS 기준선 recall 38% vs 64%). 단, C의 Tier1 판정기로는 재사용. |
| C. **클레임 앵커 생성**(extract → 결정론적 anchor → select → 인용 부착 write → 문장 단위 verify) | LLM 2호출 + 로컬 검증; 모든 문장이 (언론사, 기사, 문자 오프셋) 앵커를 가짐 | **채택 후보 — 사전 등록 A/B로 결정.** 판정 단위가 짧아 규칙·NLI 정밀도가 오르고 judge 입력이 5~10배 줄어 비용 중립($0.013~0.016/건 추정). 출처 UI·스토리 차분·클레임 임베딩이 같은 원장에서 파생. 위험: 추출 탈맥락화 오류 전파, 나열식 문체, 문헌(CAMS)은 영어·Claude Opus 4 기준. |
| D. 추출-후-추상(출처 미부착) | 중심성 발췌 → 요약 | 기각(C의 부분집합). |
| E. 더 큰 judge(3.5-flash) | 모델 크기 | 기각(단가 5배, 30s 타임아웃·503 실측, 커버리지 문제 무해결). |

**채택 설계(요약; 상세는 llm 리뷰 권고안과 동일):**
1. `extract_claims`(LLM 1회, compact 스키마 `{c:[{i,t,q:[[a,s]],k,e,n}]}`, 클레임 ≤20) → `anchor`(rapidfuzz `partial_ratio≥85`, 실패 클레임 폐기·비율 기록) → `select`(BGE-M3 코사인≥0.85 병합, 지지도=언론사 수, 충돌=같은 개체+다른 숫자, MMR로 12~18개) → `write`(LLM 1회, 문장마다 `[c3,c7]` 인용, 메타·문체를 같은 호출에 병합, 600~900자) → `verify_sentence`(Tier0 규칙: `faithfulness.check_against_sources`를 스팬에 대해 / Tier1 로컬 KLUE-NLI, AUC≥0.75일 때만 / Tier2 교차 벤더 judge는 불확실 구간 ≤30%만) → 미지원 문장만 재작성 1회, 실패 시 리드가 아니면 삭제.
2. 홀리스틱 judge v2는 coverage/coherence/style만 담당하는 shadow로 강등(OOF κ≥0.40 전까지 발행을 막지 않음 — ADR 0009 규칙 유지).
3. 저장: `news_letter_claims`, `news_letter_sentences`. 프론트 "출처 보기"는 언론사·링크·≤1문장 인용만(저작권).
4. **검증 프로토콜(ADR 0009 Addendum A2, 결과 미열람 상태에서 기록):** 같은 모델(gemini-3.5-flash-lite)로 E2E+게이트 vs 클레임 앵커, 평가셋 40클러스터(seed 20260925) 그대로, 블라인드(E2E 팔에도 문장별 상위 3개 원문 문장을 붙여 라벨 노력 비대칭 완화). 1차 지표 = **문장 단위 미지원율(사람 라벨)**, 2차 = 발행률·핵심 사실 커버리지·문체·100건당 비용·p95·클러스터 실패율. 결정 규칙 = Δ미지원율 ≤ −5%p & 클러스터 단위 짝지은 부트스트랩 10,000회 CI가 0 미포함 & 커버리지 하락 ≤10%p & 비용 ≤1.5배 & p95 ≤60초 → 채택; CI가 0 포함 → 비용 낮은 쪽; 위반 시 E2E 유지. 모델 bake-off는 승자 아키텍처 위에서 20건/모델 홀리스틱 + 편향 보정 judge로 축소.
5. **judge 보정(1인 라벨러):** 라벨 단위 = (문장, 스팬) 쌍, 표적 추출(Tier 불일치 전부 + Tier1 0.3~0.7 구간 + 숫자/날짜/인용 문장 전부 + "전부 지지" 무작위 20%), m≈200, LLM 사전 라벨 은닉. 판정기 민감도 q1·특이도 q0로 일일 미지원율을 θ̂=(p̂+q̂0−1)/(q̂0+q̂1−1)로 편향 보정해 CI와 함께 `reports/llm/`에. 48시간 재라벨 κ: 홀리스틱 ≥0.60, 쌍 ≥0.70.
6. 비용: 접두부 공유 프롬프트로 암묵 캐시 적중, Batch API(−50%) + 3시간 마감 동기 폴백, 클러스터 3~4 병렬. 목표 100건당 비용 동기 대비 ≤50%.

### 3.3 운영·배포

| 대안 | 요지 | 판정 |
|---|---|---|
| A. Oracle A1 단일 VM 올인 | 확보 대기 후 compose 통째 | 확보 시점 불명·7일 유휴 회수·2 OCPU fp32 임베딩 2.5h/일 추정. **Tier 1**(확보되면). |
| B. Tier 0: E2.1.Micro(API+DB) + GitHub Actions 배치(공개 저장소 arm64 4vCPU/16GB 무료) + Cloudflare Pages/Tunnel | 요청 경로 초경량, 무거운 것은 이식 가능한 배치 플레인 | **착수안(단, 시점은 D2 판정대로).** `jobs.run` CLI가 이미 컨테이너/CLI 중립이라 코드 변경 최소. 60일 무커밋 시 cron 비활성, 캐시 7일, 러너 IP 비고정(터널) 주의. |
| C. Cloud Run + Neon/Supabase | 서버리스 | 기각(무료 구간 리전 미확인, 콜드스타트 2~5초, 0.5GB DB). |
| D. Hetzner CAX11/21(€6~10.5/월) | 유료 최소 | **Tier 2**(무료가 모두 실패할 때). |
| E. Mac 상시 + Tunnel | | 수개월 상시 불가. 개발·대량 재임베딩·**수집 연속성 임시 확보**에만. |
| F. HF Spaces | | 기각(48h 슬립·비영속 디스크). |

**즉시(코드 기본값으로):** `max_length` 8192→2048, 길이순 정렬 후 배치, `OMP_NUM_THREADS`/`torch.set_num_threads`=vCPU, 뉴스레터 임베딩을 LangGraph 노드에서 `ingest` 경로로 이관(`generate`의 3GB 로드 제거). ONNX int8은 ops E1 매트릭스(코사인 ≥0.98, B-cubed F1 손실 ≤0.02) 후.
**비용 가드(코드로 강제):** `GENERATE_DAILY_CAP`(기본 15~20) + `LLM_DAILY_BUDGET_USD`(기본 0.30) + 월 $9 하드캡, `llm_metrics`×`config/llm_pricing.yaml` 누적, 초과 시 `JobSkipped(llm_budget)`; 클러스터를 크기·신선도 순으로 처리해 캡 안에서 기사 커버리지 최대화; GCP 예산 알림 $5/$8/$10 → 기존 킬 스위치 파일. Gemini 무료 티어는 개발·bake-off에만(입력이 언론사 본문).
**관측성(전부 무료):** Healthchecks.io dead-man 핑을 `jobs/runtime.py` 성공 경로에, UptimeRobot으로 `/healthz`, 공개 `/status` JSON(마지막 ingest, 오늘 기사/뉴스레터 수, 모델 버전, 오늘 LLM 지출, p95) → README 라이브 배지.
**저작권 바이 디자인:** 본문은 DB에만·N일(권장 7~30) 후 NULL(제목·URL·언론사·임베딩·클러스터 id·sha256 보존), 백업 암호화·비공개, 데모는 제목+언론사+원문 링크+뉴스레터만, 평가셋·리포트는 id·URL·SHA만. korea.kr 정책브리핑(공공누리 1유형)을 11번째 소스로 추가해 그 부분집합만 전문·공개 데이터셋 배포(선택).

---

## 4. 혁신 포인트 (1인·수 주 내 실현 가능성)

| # | 포인트 | 왜 드문가 | 실현 가능성(2~3주) | 측정 |
|---|---|---|---|---|
| I1 | **클레임 앵커 생성 + 문장별 출처 원장** — 모든 문장이 (언론사, 기사, 오프셋)으로 이어지고 "출처 보기"로 짧은 인용+링크 노출 | 사실성을 judge 점수가 아니라 구조로 보장; 저작권을 지키며 신뢰를 제품 표면에 올림 | ★★★☆ (R2, 5~6일; llm E1 feasibility가 게이트) | 문장 단위 미지원율 Δ(CI), 앵커링 성공률, 100건당 비용 |
| I2 | **평가 가능한 서빙** — propensity·ε 슬롯·shadow 점수를 노출 로그에, recsys_core 단일 피처 코드 + CI parity 게이트, 시뮬레이터로 SNIPS 추정치와 실측 일치 검증 | "실사용자가 없다"를 "첫 사용자부터 A/B 없이 정책 비교 가능한 로그"로 바꿈; train/serve skew 0을 숫자로 | ★★★★ (R3, 3~4일) | max\|Δ\|, Kendall τ, SNIPS 상대오차 ≤15% [SIM], ESS |
| I3 | **"일일 배치 릴리스" regime 벤치마크(P3)** — EB-NeRD 위에서 모든 후보가 같은 시각에 태어나는 조건을 재현하고 콘텐츠→인기도 전환 곡선 h*를 수치화해 온라인 규칙으로 이식 | 공개 데이터셋에 없는 조건을 공개 데이터로 재현; 언어 전이 계약(랭크 정규화·KS)과 함께 "왜 덴마크어 모델을 써도 되는가"에 측정 가능한 답 | ★★★★ (R1, 2일) | 버킷별 nDCG@10 곡선, h*, ΔnDCG(rank−raw) |
| I4 | **스토리 연속성 측정 → 최소판 story_id + 생성 생략 규칙** — 일 클러스터 중 연속 사건 비율을 먼저 재고, 연결된 스토리에 신규 기사 <2건이면 생성 생략 | 아이템 정체성을 사건 단위로 두는 것은 학계(USTORY/SCStory)에는 있지만 포트폴리오엔 거의 없음; 생성 예산 레버이자 중복 제거 | 측정 ★★★★ (2일, LLM 0원) / 최소판 ★★★ (R3, 2~3일) / 전체판(버전·UI·클레임 차분) ★★ (이후) | 연속 사건 비율(CI), 연결 정밀도 ≥0.8(n=40), 생략률, 재생성 감소 |
| I5 | **편향 보정 일일 리포트 + 증거 자동 동기화** — 판정기 q0/q1로 보정한 미지원율을 매일 `reports/llm/`에; README/PORTFOLIO의 모든 수치를 `reports/*.json`에서 렌더링하고 불일치 시 CI 실패([EB-NeRD]/[KR-eval]/[SIM]/[LOAD] 라벨·SHA 자동 삽입) | "판정기 점수"가 아니라 "사람 기준으로 보정된 오류율"을 보고; 팀 README의 근거 없는 수치 사건(FIX #7)을 구조적으로 재발 방지 | ★★★★★ (R4, 1~1.5일) | CI 게이트 통과, 리포트 자동 생성 |

---

## 5. 다음 2~3주 우선순위 로드맵 (마일스톤 기준; 의존 순, 주차 없음)

의존 그래프: R0 → R1(추천, 데이터 무관) ∥ R2(LLM, 데이터 게이트) → R3 → R4. R1과 R2는 병렬이며, R2는 한국어 데이터 5~7일치가 쌓이는 시점에 시작한다.

### R0. 막힌 것 풀기 (지금 ~ 2일)
| 항목 | 산출 증거 | 성공 기준 |
|---|---|---|
| 수집 연속성: Mac 네이티브(MPS) `python -m jobs.run ingest` 매시(launchd + caffeinate) 또는 colima+스레드 고정; `max_length=2048`·길이순 배치·스레드 고정 커밋 | `job_runs` 일별 행, `reports/ops/ingest_<날짜>.json` | 48h 연속 성공률 ≥95%, 언론사별 with_body/embedded 카운트, 기사당 임베딩 ≤2s(MPS) |
| 생성 워밍업 1회 완주: Gemini 402 해결이 안 되면 Upstage `solar-pro3` 또는 `gpt-4.1-mini` 키로 클러스터 1~3개를 E2E 완주(bake-off 결과가 아니라 "돈다"는 사실) | DB `news_letter` 행 + `generation_history`, `llm_metrics_run*.json` | 뉴스레터 ≥1건 저장, purpose별 토큰·비용 기록, ADR 0009 Addendum에 "워밍업 실행" 기록 |
| EB-NeRD 임베딩 완료(현재 72%) | `derived/ebnerd_small/` 전체 | 20,738/20,738, 8,192 초과 경고 건수 기록 |
| `team_repro_v2` PR → main(N/A는 이미 수정됨, cold 15/warm 85·best_iteration=1 해석 문단 확인) | PR, ADR 0007 증거 절 실제 절 번호 | 병합, ADR 색인에 0007 표시 |
| 통합 준비: alembic merge revision 계획, wt-3 `bakeoff_analysis.py` 커밋, 병합 순서 확정(wt-69a → wt-2 → wt-1 → wt-3 → wt-4(+merge revision) → wt-5) | `docs/adr/0006`·병합 순서 메모 | "참조된 ADR 파일 존재"를 병합 조건으로 PR 템플릿에 명시 |
| P9 소결함: `tone_converter` 키, 죽은 설정, 상수 피처, README 배지 | 커밋 1~2건 | 테스트 통과 |

### R1. 추천 증거 (R0 후 3~4일, 데이터 무관)
| 항목 | 산출 증거 | 성공 기준 |
|---|---|---|
| ADR 0013에 승격 규칙·P1/P2/P3 정의·시드·부트스트랩을 **실행 전** 커밋 | `docs/adr/0013` (proposed) | 결과 파일 없는 상태에서 커밋된 SHA |
| recsys E1: 7단계 ablation(team_binary → … → ranker_v2) 3시드, P1 전체·P2 20k 표본·replay(A/B/C) | `reports/recsys/ebnerd_v1.{json,md}`(집계만, 기사 텍스트·임베딩 없음) | 7행 표 + 유저 부트스트랩 CI, best_iteration>5 전 시드; 승격 규칙 판정 결과 기록(실패해도 기록) |
| recsys E2(P3 일일 배치 릴리스)·E3(히스토리 절단 k*)·E4(랭크 정규화·KS)·E7(MMR λ Pareto)·E8(휴리스틱 가중치 적합) | 같은 리포트의 절 | h* 교차점, k*, ΔnDCG(rank−raw) ≥ −0.01, λ 선택 규칙, 적합 가중치 vs 사전값 |
| parity: `recsys_core/serving.py` 어댑터 + wt-4 `feature_fn` 배선 + 시드 DB 200회 재생 비교 테스트 | `tests/recsys/test_feature_parity.py` (CI integration job) | max\|Δ\|<1e-6, Kendall τ=1.0, top-20 겹침 ≥0.9 |
| wt-2·wt-69a 병합 | main | CI 초록 |

### R2. LLM 증거 (한국어 5~7일치 확보 시점부터 ~7일; 라벨 ≈10~12h 포함)
| 항목 | 산출 증거 | 성공 기준 |
|---|---|---|
| wt-1·wt-3 병합(ADR 0006·0010 작성 후) | main | alembic 단일 head 유지 |
| 평가셋 40+2 추출(seed 20260925), 클러스터 라벨 L0(2.7h, 출력 전) | `evaluation/llm/evalset/*.json`(id·URL·SHA만) | 층별 크기 3–4/5–9/10+, 어려운 사례 ≥5 |
| E2E 기준 팔 단일 패스 40건 | `reports/llm/<날짜>_e2e_baseline.json` | 실패율·비용·p95 기록 |
| llm E1 feasibility(10클러스터, 평가셋과 분리) | `reports/llm/<날짜>_claim_anchor_feasibility.md` | 앵커링 ≥85%, 클러스터당 유효 클레임 ≥8, p95 ≤25s → 통과 시 Addendum A2 기록(결과 미열람 상태); 실패 시 ADR 0009 원안 |
| llm E2 아키텍처 A/B(블라인드) + 라벨 L1(4h)·L2(1.5h)·L3(1.2h) | `reports/llm/<날짜>_arch_ab.{json,md}`, 라벨 파일(본문 없음) | 사전 등록 규칙 적용 결과; κ(홀리스틱 ≥0.60, 쌍 ≥0.70) 미달 시 "신뢰 불가" 표기 |
| llm E3 판정기 보정(Tier0/1/2 ROC, 2-fold, q0/q1, 편향 보정 CI) | 같은 리포트 절 + `docs/adr/0022` | 어느 조합이든 OOF AUC ≥0.85, Tier2 호출 ≤30%, CI 폭 ≤0.10 |
| llm E4 길이·문체 병합·클레임 임베딩 절제(20클러스터) | 리포트 절 | 짧은 팔 미지원율 ≤ 긴 팔, 문체 차 ≤0.3, 병합 드리프트 ≤ +5%p → 문체 호출 폐지 결정 |
| narrative E7 클러스터링 첫 실측(같은 40클러스터 라벨 재사용): min_cluster_size×min_samples×split_v2 | `reports/clustering/<날짜>_v2.md`, `docs/adr/0011` | B-cubed F1 표 + 부트스트랩 ARI, 채택 조합 |
| 축소된 모델 bake-off(승자 아키텍처, 20건/모델 + 편향 보정 judge) | `reports/llm/<날짜>_bakeoff.md` | ADR 0009 게이트·비용 규칙 적용 |
| ops E4 실측 비용 → 캡·예산 기본값 고정 | `docs/adr/0028` | $/뉴스레터 CI, 월 예상 ≤$9 |

### R3. 평가 가능한 서빙 + 스토리 최소판 + 운영 (R1·R2와 부분 병렬, ~5~6일)
| 항목 | 산출 증거 | 성공 기준 |
|---|---|---|
| wt-4(+alembic merge revision, ADR 0015)·wt-5(ADR 0019) 병합 | main | CI 초록, downgrade −1 라운드트립 통과 |
| architecture E1 스토리 연속성 오프라인 재생(LLM 0원): 일자별 스냅샷, (cos, Jaccard) 격자, 블라인드 40쌍 라벨(1~2h) | `reports/clustering/story_linking_v1.{json,md}` | 연속 사건 비율(CI) 보고; 정밀도 ≥0.80(Wilson 하한 ≥0.65) → 비율 ≥20%면 최소판 채택, <10%면 ADR 0012에 "불필요"로 기록 |
| 스토리 최소판(조건부): `news_letter.story_id` nullable + `core/clustering/story_linker.py`(순수 함수) + `generate`의 생성 생략 규칙(신규 기사 <2건) + wt-4 후보 dedup + 시뮬레이터 카탈로그 story_id | 마이그레이션 1건, `docs/adr/0012` | 중복 스토리 비율 전후, 생략률 ≥20% |
| 노출 로그 `propensity float`·`explored bool` + ε=2/20 균등 슬롯 + LightGBM shadow 점수 | 마이그레이션, `docs/adr/0025` | 헤더·컬럼이 로그에 남음(통합 테스트) |
| E3-arch/E6-recsys OPE 검증 [SIM]: 300명×7일×3시드, 정책 A 로그로 정책 B CTR을 SNIPS/replay 추정 vs 실측 | `reports/sim/<날짜>_ope_validation.md` | 상대오차 ≤15%(3시드 평균), ESS ≥5%; 실패 시 ε=0.2 재실험 1회 |
| narrative E5 지연(microbench + Locust 20 RPS, batch vs realtime, 장애 주입) · E6 sim metric-validity grid | `reports/serving/latency_v1.md` [LOAD], `reports/sim/grid.md` [SIM] | p50/p95/p99 표, 폴백률 <5%, 예상 방향 위반 0건 |
| 비용 가드 코드(`GENERATE_DAILY_CAP`, `LLM_DAILY_BUDGET_USD`, 예산 알림→킬 스위치) + dead-man/uptime 핑 + `/status` | 커밋, `docs/adr/0028`·`0020` | 캡 초과 시 `JobSkipped` 테스트 |
| 배포: A1 확보 시 Tier 1, 아니면 Tier 0 데이터 플레인(Micro Postgres + Actions ingest) → 7일 소크 시작 | `reports/ops/soak_<날짜>.md` | 잡 성공률 ≥95%, 수집 지연 p95 ≤3h, OOM 0 |
| 본문 보존 정책(N일 후 NULL, sha256 보존) 마이그레이션 + 보존 잡 | `docs/adr/0027` | 월 증가량 ≤50MB(halfvec 검토 포함) |

### R4. 문서·이력서 (마지막 3~4일)
| 항목 | 산출 증거 | 성공 기준 |
|---|---|---|
| README 재작성: HyperCLOVA 배지 제거, 팀 표는 "팀 시절"로 구획, 라벨 열([EB-NeRD]/[KR-eval]/[SIM]/[LOAD])이 있는 수치 표만, `/status` 배지 | README, PORTFOLIO.md | 모든 수치가 `reports/*.json`에서 렌더링 |
| 증거 자동 동기화 스크립트 + CI 게이트(I5) | `scripts/render_evidence.py`, CI job | 문서 수치 ≠ 리포트면 실패 |
| ADR 0024 AI 보조 개발 정책 + narrative E8 재도출 훈련(10개 ADR을 노트 없이 3분 설명) | `docs/adr/0024`, 이력서 문장 1줄 | 막힌 ADR 0건(막히면 본인 표현으로 다시 씀) |
| 비공개 포트폴리오·이력서에서 0.897 / nDCG@5 0.801 / "<100ms" / "56.4%" / "서비스 운영" 스크럽; 위키 갱신(`newsletter-realtime-recommendation.md`, `boostcamp-newsletter.md`, catalog S-ID) | 커밋, 위키 lint | `scripts/lint_wiki.py` 통과 |
| 공개 데모(선택): Tier 0 API + Cloudflare Pages | URL | 7일 소크 후에만 "운영 중"이라 표기 |

---

## 6. 자르거나 미룰 것

**삭제**
- Two-tower/NRMS류 학습 유저 인코더, BGE-M3 미세조정(EB-NeRD에서 NRMS ≈ popularity+1.3 AUC, zero-shot 교차언어 붕괴, GPU 의존).
- 팀 합성 페르소나(100명)로 어떤 정확도 지표도 주장하는 것 — 화이트박스 진단 전용(ADR 0007). `generator_split` 결과도 인용 금지.
- LLM per-request 재랭커, LLM 판정을 클릭 라벨로 쓰는 모든 경로.
- 더 큰 judge(3.5-flash), Claude Haiku 4.5를 운영 judge로(OpenAI 호환 계층이 `response_format` 무시, 은퇴 가능) — bake-off 기준선에서도 제외.
- judge v1(제목만) 기준선 팔(라벨 40건 소모 대비 이력서 가치 낮음).
- Airflow compose 프로필·`docker/airflow.Dockerfile`(DAG 파일만 archived 보관 — 사용자 결정).
- 레거시 `ai_workspace/core/llm_client.py`(HyperCLOVA 경로 513줄)·`naver` provider — `extract_json_from_response`·`SimpleRateLimiter`만 `core/llm/`로 옮긴 뒤 제거.
- 죽은 설정·상수 피처·`reset_db` 인자·README `assets/architecture.png` 참조·HyperCLOVA 배지.
- MIND 하네스(공식 다운로드 불가, 본문 없음 — EB-NeRD로 충분).
- 개체명을 문서 단위 차단 게이트로 승격하는 작업(문장 단위 스팬 검사로 대체).
- Cloud Run/Neon 서버리스, Fly/Koyeb/Render/HF Spaces, Redis·큐·k8s·멀티리전, Grafana/Prometheus 스택.

**축소**
- PR-18 pgvector HNSW 벤치: wt-4 KNN 쿼리 p95 한 번 실측해 ADR 0016 한 문단("현 규모 exact 우세, 역전 예상 규모")으로 종료.
- PR-21 Locust: 20 RPS 단일 p50/p95/p99 표 하나. 5/50 RPS는 폴백률만.
- MLflow: file store와 `log_mlflow_run` 유지, UI·대시보드 없음.
- 모델 bake-off: 3후보×40건 홀리스틱(라벨 6h) → 승자 아키텍처에서 20건/모델 + 편향 보정 judge(2h).
- 시뮬레이터: 아키타입 10종·drift·calibration은 유지하되 추가 확장 금지; 리포트 머리말에 "시스템 반응 지표만, 정확도 무주장" 고정.
- 뉴스레터 길이 규칙 1,000~1,500자·4문단 → llm E4 전까지 600~900자 잠정.

**지연**
- 스토리 전체판(버전·UI 배지·클레임 집합 차분) → 최소판 측정 후.
- 저장소 단일 패키지화(옵션 D) → 동결 ADR만(0029).
- LLM 관심 프로필 피처(recsys E5) → R3 이후 선택, ≤$5.
- 임베딩 모델 교체·ONNX int8 → 라벨셋(E7) 확보 후 B-cubed 손실 한도와 함께.
- Thompson sampling·LinUCB 정책 학습 → 실트래픽 이후.
- Optuna 튜닝 → 합성 목적함수 폐기, EB-NeRD es 구간으로만, R1 결과 이후.
- EB-NeRD large → small의 CI가 결론을 못 낼 때만.
- Oracle A1 대기 → Tier 0/Mac으로 대체하고 확보되면 같은 compose를 올린다.
- `min_target` 보충 로직 재설계 → 스토리 최소판 이후.

---

## 7. 새로 써야 할 ADR 목록 (번호 확정안)

번호 원칙: 코드가 이미 참조하는 번호와 플랜 배정을 유지하고 신규는 0021부터. 증거가 나오기 전엔 `proposed`, 나온 뒤 `accepted`. 증거 절이 비어 있는 ADR은 상태를 내려 둔다(문서 인플레이션 방지).

| 번호 | 제목 | 근거 코드/리뷰 | 마일스톤 | 증거 |
|---|---|---|---|---|
| 0006 | 런타임 구성: compose + supercronic, API 이미지 torch 제외, Airflow 프로필 제거·DAG 보관 | wt-1 runbook이 참조 | R0/R2 | 두 방식 유휴 메모리 실측 1행, job_runs 7일 성공률 |
| 0009 Addendum A2 | 아키텍처 A/B(E2E+게이트 vs 클레임 앵커)를 모델 bake-off보다 먼저; 1차 지표 = 문장 단위 미지원율; bake-off 축소; Haiku 4.5 제외; 워밍업 실행 기록 | llm 리뷰 D3 | R2(feasibility 통과 직후, 결과 미열람) | 날짜·사유·열람 여부 |
| 0010 | 결정론적 사실성·드리프트 게이트(차단 유형 기본값, 개체명 advisory, 실패 시 형식체 저장) — 문서 단위 검사 커버리지 19.7%를 "결과와 한계"에 명시하고 문장 단위 이행 조건 기록 | wt-3 gates.py 13회 참조 | R2 | narrative E4 전후표 |
| 0011 | 클러스터링 v2(min_cluster_size/min_samples/split_v2/차원 축소; noise 이월 48~72h) | 플랜 PR-14 | R2 | B-cubed F1·DBCV·부트스트랩 ARI 표 |
| 0012 | 스토리 연속성: 측정 결과(연속 사건 비율)와 최소판(story_id·연결 규칙 τ,j·생성 생략)·전체판 조건, 옵션 A/C 기각 사유, `news_raw.news_letter_id` 의미 축소 | architecture·llm | R3 | story_linking_v1 리포트 |
| 0013 | EB-NeRD 오프라인 벤치마크 프로토콜과 ranker v2 승격 규칙(P1/P2/P3, 학습·es·평가 창, 시드·부트스트랩, 라이선스 경계·집계만 커밋, team_binary를 ablation 시작 arm으로 보존·성승우 출처) | wt-1 crontab·DAG 참조, 플랜 PR-16 | R1(실행 전 proposed) | ebnerd_v1 리포트 |
| 0014 | 실시간 반영 재생(A/B/C)·MMR λ Pareto·휴리스틱 가중치 적합(E8) | 플랜 PR-17 | R1 | ebnerd_v1 절 |
| 0015 | 요청 시점 추천 설계(후보 합집합, 콜드스타트 체인 k*, 폴백, X-Rec-Source, TTL 캐시·워커별 캐시 한계, Postgres 단기 상태, 핫 리로드, feature_fn=recsys_core 어댑터, shadow 모드) | wt-4 13회 참조 | R3 | latency_v1 [LOAD], parity 테스트 |
| 0016 | HNSW 미채택(exact scan p95 실측, 역전 예상 규모) | 플랜 PR-18 | R3 | 1문단 |
| 0017 | 콜드스타트·언어 전이 계약(히스토리 절단 k*, 요청 내 랭크 정규화, KS ≤0.1, 임베딩 sanity, 전이 불가 항목) | recsys | R1 | ebnerd_v1 절 |
| 0018 | 잡 분리·스케줄(ingest 매시, generate 캡, popularity/user_embed 매시, train 주 1회, batch_fallback), advisory lock, 생성 주기 결정(architecture E4) | 플랜 PR-20 | R3 | job_runs, E4 지연표 |
| 0019 | 시뮬레이터 경계(BGE-M3 코사인 제외, 2% CTR 보정, 정확도 무주장, [SIM], 스토리 중복이 reactivity에 미치는 영향 명시, OPE 검증 용도) | wt-5 7회 참조 | R3 | sim grid, ope_validation |
| 0020 | 배포 토폴로지 3단계(Tier 0 Micro+Actions / Tier 1 A1 / Tier 2 Hetzner)·승격·강등 기준·관측성 세트(dead-man, uptime, `/status`) | ops | R3 | 소크 리포트, 컨테이너 RSS·로드 시간 실측 |
| 0021 | 클레임 앵커 생성 v3(extract→anchor→select→write→verify, compact 스키마, 폐기·재작성 규칙, `news_letter_claims/sentences`, 출처 UI 인용 길이 제한) | llm | R2(proposed → A/B 결과로 accepted/rejected) | arch_ab 리포트 |
| 0022 | 판정기 보정·보고 프로토콜(라벨 단위 = 문장·스팬 쌍, 표적 추출, 사전 라벨 은닉, 3단 판정기·2-fold 임계값, 편향 보정 추정량·CI, 48h 재라벨 κ, 주 1회 50쌍 감사) | llm | R2 | E3 결과 |
| 0023 | 주장·증거 라벨 정책([EB-NeRD]/[KR-eval]/[SIM]/[LOAD], 절대 수치 인용 금지 범위, README 표 형식, 팀 수치 철회 기록, 증거 자동 동기화) | narrative·architecture | R4 | CI 게이트 |
| 0024 | AI 보조 개발 정책과 저자 책임(도구 사용 방식, 사람이 내린 결정·검증·기각 사례 목록, Co-Authored-By 유지 이유, 커밋 리듬) | narrative | R4 | 재도출 훈련 완료 |
| 0025 | 노출 로그·탐색·OPE 프로토콜(propensity/explored 컬럼, ε=2/20 균등 슬롯, SNIPS/replay 채택, 실사용자 전환 조건) | architecture·recsys | R3 | ope_validation [SIM] |
| 0026 | 단일 피처 구현과 parity 게이트(recsys_core 유일 피처 코드, 서빙 어댑터 경계, max\|Δ\|·τ·겹침 기준, "겹침 0.9 단독" 폐기) | architecture | R1 | parity 테스트 |
| 0027 | 기사 본문 보존·저작권 정책(N일 후 NULL, sha256, 암호화 백업, 노출 범위, EB-NeRD 로컬 전용, KOGL 소스 부분집합) | ops·architecture | R3 | 저장 증가율 실측 |
| 0028 | LLM 비용·지연 정책(일일 캡·예산·월 하드캡, Batch API+폴백, 병렬도, 접두부 공유 캐시, 단가표·실측 $/건) | ops·llm | R2/R3 | ops E4·llm E6 |
| 0029 | 저장소 경계 동결(새 최상위 루트 금지, sys.path 조작 1곳, 벡터 파싱·DB 접근 신규 구현 금지, 단일 패키지화 조건) + 뉴스레터 provenance 컬럼(model_id, prompt_sha, judge_version, gate_result) | architecture | R0/R3 | — |
| 0030 | 임베딩 실행 경로(max_length 2048, 길이순 배치, 스레드 고정, 뉴스레터 임베딩 ingest 이관; int8·모델 교체 조건) | ops | R0(기본값) / 이후(int8) | 토큰 분포 n=27, E1 매트릭스 |
| 0007 갱신 | 증거 절 자리표시자 → 실제 절 번호·수치 | narrative | R0 | — |

---

## 8. 사용자가 내려야 할 결정 (선택지와 추천)

| # | 결정 | 선택지 | 추천 |
|---|---|---|---|
| 1 | 크리티컬 패스 해제 수단 | (a) Gemini 402 해결 대기 (b) Upstage/OpenAI 키로 워밍업 1회(수 달러) | **(b)** 즉시. bake-off 사전 등록과 충돌하지 않음(워밍업으로 Addendum 기록). 교차 벤더 judge 키도 겸용. |
| 2 | 수집 연속성 | (a) Mac 네이티브 MPS + caffeinate/launchd (b) Tier 0 데이터 플레인(Micro Postgres + Actions) 지금 (c) A1 대기 | **(a) 오늘 + (b)는 R2 시작 전까지 A1이 없으면** |
| 3 | ADR 0009 Addendum A2 승인 | (a) 아키텍처 A/B 먼저, 1차 지표 문장 단위 미지원율 (b) 원안 bake-off 먼저 | **(a)**, 단 llm E1 feasibility 통과를 조건으로. 결과를 보기 전인 지금만 가능. |
| 4 | 라벨 예산 | 최소안 ≈10.2h(모델 bake-off 홀리스틱 미룸) / 전체안 ≈12.2h / + 스토리 연결 40쌍 1~2h | **최소안 + 스토리 40쌍** (≈12h). 지인 1명 두 번째 라벨러(n=30, κ) 섭외 여부도 결정. |
| 5 | 스토리 엔티티 | (a) 측정 후 최소판(권장) (b) 지금 전체판 (c) 미룸 | **(a)** — E1 결과가 ≥20%일 때만 최소판. |
| 6 | 탐색 슬롯 | 20개 중 2개 ε-균등 허용 여부 | **허용** (실사용자 없음 → 제품 손실 0, 평가 준비 가치 큼) |
| 7 | Airflow | (a) 프로필+Dockerfile 제거, DAG 보관 (b) 전부 제거 (c) 유지 | **(a)** |
| 8 | LLM 월 지출 상한 | $10 크레딧 안(≈15~20건/일 표준, ≈28건/일 Batch) / 자비 +$10~20 | **$10**, 캡 15~20. 평가 실험(40×2팔 ≈ $2)은 별도 승인. |
| 9 | 본문 보존 기간·노출 범위 | N=7 / 30 / 무기한; 데모에 제목+링크만 | **N=30**(디버깅 여유), 데모는 제목+언론사+링크+뉴스레터만, 인용 ≤1문장 |
| 10 | 출처 UI | 문장별 "출처 보기"(언론사·링크·≤1문장 인용)를 AI 인터페이스로 포함할지 | **포함**(클레임 앵커 채택 시) |
| 11 | 뉴스레터 길이 | 600~900자 잠정 축소 | **동의**(E4에서 검증) |
| 12 | AI 보조 개발 공개 수준 | (a) 이력서 명시 + ADR 0024 (b) 묻기 전엔 말 안 함 | **(a)** — 트레일러가 공개돼 있어 (b)는 사실상 위험 |
| 13 | 이효창 님 분담 문장 | "팀 저장소의 워크플로우·크롤러·클러스터링 코드는 본인 단독 저자, 이효창 님은 병렬 프로토타입·프롬프트 검토" | 본인에게 문장 확인 요청 여부 결정 |
| 14 | 이선진 님께 Notion「페르소나 데이터 생성」요청 | 요청 / 미요청 | **요청**(v2 리포트 "재현 불가" 항목 하나 닫힘) |
| 15 | 포트폴리오 스크럽 시점 | 지금 / EB-NeRD 결과 후 | **지금 지우고 R1 후 채움** |
| 16 | 병합 순서 | wt-69a → wt-2 → wt-1 → wt-3 → wt-4(+alembic merge) → wt-5; "참조된 ADR 파일 존재"를 병합 조건으로 | **동의 여부** |
| 17 | Oracle PAYG 전환 | 카드 등록·소액 과금 위험 vs A1 우선권·유휴 회수 면제(커뮤니티 보고, 공식 문서는 2 OCPU 상한 유지) | 전환 시 예산 알림 $1 필수; **R3까지 A1이 없으면 전환 검토** |
| 18 | korea.kr 정책브리핑(공공누리 1유형) 소스 추가·부분집합 공개 데이터셋 | 추가 / 미추가 | **추가**(재현 가능한 공개 평가셋이 생김; 1일) |
| 19 | 지원 직무 초점 | 추천/개인화 vs LLM 애플리케이션 vs 일반 AI 엔지니어 | S1·S3(추천) 또는 S2·S4(LLM)를 앞세울지 |
| 20 | LLM 관심 프로필 실험(≤$5) | 실행 / 보류; EB-NeRD 제목 외부 API 전송 라이선스 확인 | **보류**(R3 이후) |

---

## 9. 이력서 스토리 5개와 필요한 증거 / 쓰면 안 되는 주장

프레임: **옵션 B(감사-우선) 헤드라인 + 옵션 A(연대기) 구조.** 팀 프로젝트(2026.01–02, 단독 담당 명시)와 단독 고도화(2026.09–)를 `team-final` 태그로 구획한다.

| # | 스토리(이력서 문장 골자) | 지금 상태 | 완전해지는 조건 |
|---|---|---|---|
| S1 | "팀 보고 MRR 0.897의 추론 시점 누출을 재현 하네스로 규명(+0.18~+0.30, 시드×유저 nested bootstrap 95% CI 0 제외)하고 point-in-time 프로토콜(ADR 0007)을 제정. 고친 모델도 이 데이터에선 popularity를 못 이김(P@5 0.34 vs 0.48)을 그대로 적음." | **거의 방어 가능**(v2 N/A 수정·커밋됨) | R0: main PR + cold/warm·best_iteration 해석 문단 |
| S2 | "결과를 보기 전에 규칙을 커밋했다: 사전 등록 아키텍처 A/B(E2E+게이트 vs 클레임 앵커) + 문장 단위 미지원율 1차 지표 + 판정기 보정(OOF κ, 편향 보정 CI) + 결정론적 한국어 사실성 게이트를 LangGraph 노드로." | 도구 완비, 숫자 0 | R2 완료(κ·P/R·Δ미지원율 표). κ<0.60이면 "게이트·비용" 스토리로 축소 |
| S3 | "오프라인 평가와 온라인 서빙이 같은 point-in-time 피처 코드(recsys_core)를 쓴다: EB-NeRD 7단계 ablation(유저 부트스트랩 CI), 일일 배치 릴리스 regime의 콘텐츠→인기도 전환 곡선, 언어 전이 계약, CI parity 게이트(max\|Δ\|<1e-6)." | 하네스 준비, 임베딩 72% | R1 완료. CI가 0을 배제하지 못하면 "측정 인프라"로 낮춰 말함 |
| S4 | "무료 예산으로 LLM을 안전하게 돌리는 운영 규율: OpenAI 호환 어댑터로 3 프로바이더, 실측 사고(503·30s) → 타임아웃 60s·deadline 180s·judge 교체, 킬 스위치, 단가표 출처·접근일, 일일 캡·예산 가드, $/뉴스레터 실측." | **지금 방어 가능**(비용 수치 제외) | R2/R3: ops E4 실측 $/건, 캡 코드 |
| S5 | "사용자가 없을 때 무엇을 주장하고 무엇을 주장하지 않는가: 자기 정답지를 쓰지 않는 시뮬레이터([SIM]), 요청 시점 추천(후보 합집합·콜드스타트·폴백·X-Rec-Source), propensity·탐색 슬롯 로그로 OPE 준비, 스토리 연속성 측정." | 코드·테스트 있음, 수치 없음 | R3 완료(latency [LOAD], ope_validation [SIM], story_linking) |

**쓰면 안 되는 주장(즉시 스크럽):** MRR 0.897 / nDCG@5 0.801 / "추천 응답 <100ms" / "소스 다양성 56.4%" / HyperCLOVA X 배지 / "실사용자·서비스 운영·배포됨"(7일 소크 전) / 시뮬레이터 정확도·CTR / 합성 페르소나 기반 정확도 / "모든 결정을 ADR로"(누락 5건 작성 전엔 "주요 결정을") / "LightGBM 배포"(feature_fn·parity·승격 규칙 통과 전엔 "shadow 서빙") / 한국어 절대 정확도 / "Airflow 운영"(archived) / EB-NeRD 결과를 "서비스 성능"으로.

**이력서 항목 초안**
"AI 개인화 뉴스레터 추천 — 5인 팀(2026.01–02) / 단독 고도화(2026.09–), Claude Code 페어 프로그래밍
• [팀] LangGraph 평가-생성-재시도 워크플로우·RSS 크롤러·HDBSCAN 클러스터링·생성 프롬프트 단독 구현(git 단독 저자).
• [단독] 팀 보고 MRR 0.897의 추론 시점 누출을 재현 하네스로 규명(+0.18~0.30, 95% CI), point-in-time 평가 프로토콜 ADR 제정; 공개 벤치마크 EB-NeRD 7단계 ablation [수치 R1 후].
• [단독] 사전 등록 LLM 실험(아키텍처 A/B·판정기 보정)과 결정론적 한국어 사실성 게이트 [κ·Δ R2 후]; 실측 사고 기반 타임아웃·deadline·킬 스위치·비용 캡.
• [단독] 요청 시점 추천(후보 합집합·콜드스타트·폴백·propensity 로그) + 자기 정답지를 쓰지 않는 시뮬레이터로 동작 검증 [p95·OPE R3 후]."

---

## 10. 면접 예상 질문과 답변 골자

1. **"팀에서 정확히 뭘 했나? README엔 이효창도 같은 영역인데."** — git 기준 `workflow/`(12커밋), `crawler/`(13), `core/clustering`(3), `core/reconstruction`(5) 단독 저자. 이효창 님은 별도 폴더 병렬 프로토타입·프롬프트 검토, 팀 저장소 커밋 없음. 추천 엔진 성승우, 합성 데이터 이선진 — ADR 0004·커밋 본문에 출처. `team-final` 이후는 전부 본인.
2. **"79커밋이 7시간, 전부 Claude co-author인데 당신이 한 건?"** — 설계 결정·검증·기각. 예: AI가 낸 team_repro v1을 방법론 검토에서 unsound 판정(추론 시점 누출·정답 정의 오류·padded_400) → 6개 BLOCKER 명세 → v2. 7월 수정 26건 중 4건(#4 O(유저×뉴스) 회귀, #5 그룹 키, #8 재시도 가드 위치, #16 시간 필터)은 그대로 옮기면 안 된다고 판단해 교정. ADR 0024에 정책·기각 목록. (→ narrative E8 통과 후에만 이렇게 말한다.)
3. **"MRR 0.897이 틀렸다는 건 팀원 탓?"** — 구조 문제. 평가 스크립트가 NOW()-6일을 정답으로 썼는데 로그가 6.3시간뿐이라 학습 구간이 정답에 포함(as-written MRR 1.0). 멘토·팀도 "쉬운 과제"라고 의심했고 수치로 확인한 것. 고친 모델도 popularity를 못 이김.
4. **"합성 데이터로 뭘 증명하나?"** — 절대 성능은 아무것도. 클릭이 LLM 페르소나에서 카테고리 신호로 생성돼 순환적. 같은 라벨 위에서 코드 결함이 지표를 어느 방향으로 얼마나 움직이는지의 상대 효과만.
5. **"왜 덴마크 데이터인가?"** — 노출·클릭·본문·발행시각·세션이 있는 공개 한국어 뉴스 클릭 데이터셋이 없음(MIND는 영어·본문 없음·다운로드 불가). BGE-M3 다국어라 파이프라인 그대로, 아이템 수명 짧은 특성도 같음. 주장 범위는 "공개 사람 클릭 데이터에서의 알고리즘 간 상대 비교"; 전이 계약(랭크 정규화·KS·임베딩 sanity)으로 조건을 측정. 한국어는 시뮬레이터로 동작·지연만, 이후 노출 로그로 실데이터.
6. **"아이템이 하루살이인데 추천이 되나?"** — 순수 아이템 콜드스타트라 아이템 ID 임베딩을 안 쓰고 콘텐츠·시간·인기도 스칼라 피처만. EB-NeRD에서 "일일 배치 릴리스" 조건을 재현해 릴리스 후 첫 h*시간은 콘텐츠, 이후 인기도가 지배함을 수치화하고 온라인 가중치 전환 규칙으로 옮김. 스토리 연속성은 먼저 측정(일 클러스터 중 연속 사건 비율)해 최소판만 넣음.
7. **"LLM wrapper 아닌가?"** — 생성 호출 자체는 그렇다. 만든 것은 봉투: 3 프로바이더 공통 구조화 출력 계약·폴백, LLM이 속일 수 없는 결정론적 게이트, 사람 라벨에 보정되지 않으면 shadow로만 도는 judge 규칙, 실측 사고에서 나온 타임아웃·deadline·킬 스위치·캡, 결과 전에 커밋한 결정 규칙. (클레임 앵커 채택 시) 모든 문장이 원문 오프셋을 갖는 구조.
8. **"결정론적 게이트가 뭘 못 잡나?"** — 문장의 19.7%(숫자·날짜·인용 포함)만 본다. 나머지 80%(귀속·인과·사건 존재)는 judge에 의존 — 그래서 클레임 앵커로 판정 단위를 문장+스팬으로 바꾸고 3단 판정기와 편향 보정으로 갔다.
9. **"judge가 같은 Gemini 계열이면 self-preference 아닌가?"** — 맞고, `get_client('judge')`가 같은 계열이면 경고. 운영 judge는 다른 계열만 후보, DiD로 편향 수치화(ADR 0009). 문헌(Wataoka 2024)상 GPT-4류도 자기 출력에 유의한 편향.
10. **"n=40으로 뭘 결론 내나?"** — 홀리스틱 n=40은 20%p 미만을 못 가림 — 그래서 1차 지표를 문장 단위(팔당 ~400단위)로 바꿨고, 홀리스틱은 "명백히 나쁜 후보 게이트 + 동률이면 비용" 규칙. 라벨러 1인이라 intra-rater κ만, 0.60 미만이면 무효 처리.
11. **"HDBSCAN 파라미터는 어떻게 튜닝?"** — 팀 시절엔 못 했다: CLI/설정값이 `cluster_news()`에 전달되지 않는 배선 버그로 항상 기본값(3,2) → PR #3에서 수정. 이제 40클러스터 라벨로 B-cubed F1을 1차 지표로 격자 비교(ADR 0011).
12. **"실시간 추천 p95는? 배포는?"** — (R3 전) 로컬 microbench만, 부하 수치 없음; 시간 예산 초과 시 배치→인기→최신 폴백, `X-Rec-Source`로 관측. A1 용량 부족이라 로컬/Tier 0에서 `job_runs`로 성공률만. "배포된 서비스"라 쓰지 않음. (R3 후 수치로 대체)
13. **"train/serve skew는?"** — recsys_core 하나가 EB-NeRD 하네스와 `/newsletters/today`를 모두 태우고, CI가 노출 로그 200건 재생으로 피처 max|Δ|<1e-6·순위 동일을 검사. "겹침 0.9"만으로는 skew를 감춘다고 판단해 상향.
14. **"사용자가 생기면 어떻게 평가하나?"** — 첫 요청부터 노출 로그에 position·score·source·model_version·propensity·explored를 남기고 20슬롯 중 2슬롯을 균등 탐색. 시뮬레이터에서 정책 A 로그로 정책 B CTR을 SNIPS로 추정한 값이 실측과 15% 안에 드는지 미리 검증했다(수치 R3 후).
15. **"저작권은?"** — 본문은 DB에만·N일 후 NULL·sha256 보존, 평가셋·리포트는 id·URL·SHA만, 데모는 제목+링크+뉴스레터, 인용 ≤1문장. EB-NeRD는 라이선스상 Mac 밖으로 안 나가고 집계만 커밋.
16. **"비용은?"** — 뉴스레터당 $0.017~0.024 추정(실측으로 대체), 하루 100건이면 월 $50~72라 캡 15~20건·일 예산·월 $9 하드캡·Batch API·접두부 캐시로 $10 안에. 예산 알림이 킬 스위치를 당긴다.
17. **"왜 two-tower가 아니라 LightGBM?"** — EB-NeRD에서 NRMS가 popularity 대비 +1.3 AUC, RecSys Challenge 2024 상위팀은 시간 인식 피처+GBDT, zero-shot 교차언어에서 학습 NNR이 무너진다는 문헌(NaSE). CPU·12GB 제약. 승격 규칙을 사전 등록해 LightGBM이 popularity를 못 이기면 선형 폴백을 배포한다고 미리 적어 뒀다.

---

## 부록 A. 분석 간 사실 불일치와 확인 결과

| 항목 | 리뷰 주장 | 확인 결과 |
|---|---|---|
| team_repro_v2 `N/A ± N/A` | narrative: 미수정·미커밋 | wt-69a `1585ce6`에서 수정·커밋됨. main PR만 남음 |
| 임베딩 p95 토큰 | ops: 3,700 (본문 내 "1,533"과 모순) | 토큰화 표본 p95 1,533·max 1,997(n=27). 3,700은 문자 외삽 |
| EB-NeRD 임베딩 진행 | recsys 44% / narrative 47% | 72%(14,848/20,738), ≈2 art/s |
| 뉴스레터당 비용 | llm $0.017 / ops $0.024 | judge 호출 횟수 가정 차이. 범위로 표기, ops E4 실측 |
| ingest 주기 | architecture: 2시간 | wt-1 crontab: 매시 5분 |
| team_repro 실패 원인 | architecture: 하루살이 아이템 / recsys: 순환 데이터·모델 결함 | recsys가 옳음(3.0 D1) |
| ADR 0013 참조 위치 | narrative: run_ebnerd.py | wt-2 코드에는 없음; wt-1 `airflow/dags`·`crontab`에 있음 |

## 부록 B. 주요 참고 문헌(리뷰들이 인용, 접근일 2026-09-25/26)

- EB-NeRD: https://arxiv.org/abs/2410.03432 ; RecSys Challenge 2024: https://arxiv.org/pdf/2409.20483 ; 벤치마크 코드: https://github.com/ebanalyse/ebnerd-benchmark
- 교차언어 뉴스 추천(NaSE): https://arxiv.org/html/2406.12634v1 ; xMIND: https://arxiv.org/pdf/2403.17876
- 오프폴리시 평가: Li et al. replay https://arxiv.org/abs/1003.5956 ; deterministic ranking IPS https://arxiv.org/pdf/2208.14980 ; Eugene Yan 정리 https://eugeneyan.com/writing/counterfactual-evaluation/
- 추천 오프라인 평가의 누출(Ji et al. 2023): https://dl.acm.org/doi/10.1145/3569930
- 뉴스 스토리 스트림 클러스터링: USTORY https://arxiv.org/pdf/2304.04099 ; SCStory https://arxiv.org/pdf/2312.03725 ; entity-aware https://arxiv.org/abs/2101.11059
- 클레임 앵커 요약(CAMS): https://arxiv.org/abs/2606.23989 ; 다중 문서 환각 유형: https://arxiv.org/abs/2410.13961 ; LLM-judge 편향 보정 보고: https://arxiv.org/html/2511.21140v4 ; 자기선호편향: https://arxiv.org/pdf/2410.21819
- 사전 등록: Gelman & Loken https://sites.stat.columbia.edu/gelman/research/unpublished/p_hacking.pdf ; Preregistering NLP https://arxiv.org/pdf/2103.06944
- 독자 선호(DIS 2026): https://dl.acm.org/doi/10.1145/3800645.3813044
- Gemini 단가: https://ai.google.dev/gemini-api/docs/pricing ; Upstage: https://www.upstage.ai/pricing ; OpenAI: https://developers.openai.com/api/docs/pricing
- Oracle Always Free: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm ; GitHub Actions 러너/한도: https://docs.github.com/en/actions/reference/runners/github-hosted-runners , https://docs.github.com/en/actions/reference/limits ; Hetzner: https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/
- BGE-M3 ONNX int8: https://huggingface.co/gpahal/bge-m3-onnx-int8 ; KLUE NLI: https://huggingface.co/Huffon/klue-roberta-base-nli
- 저작권: 한국저작권위원회 뉴스저작물 체크리스트 https://www.kcopa.or.kr/download.do?uuid=8641e73c-2d58-4b95-8f64-b0b21bfbfaea.pdf ; 공공누리 https://www.kogl.or.kr/info/introduce.do ; korea.kr RSS https://www.korea.kr/etc/rss.do
