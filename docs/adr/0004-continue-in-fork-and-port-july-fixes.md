# ADR 0004: 기존 fork에서 계속 개선하고, 7월 셀프 리뷰 수정을 주제별로 다시 이식한다

## 상태
채택됨 (2026-09-25)

## 컨텍스트
- 팀 프로젝트는 2026-02-10(`ef7c176`, 태그 `team-final`)에 끝났다. 서비스는 배포되지 않았고, 추천은 하루 1회 배치로 계산된다.
- 2026-07-02에 포트폴리오 관점으로 셀프 리뷰를 했다. 26건을 수정하고 테스트 57개를 작성했다. 그런데 이 작업은 push하지 않은 worktree에만 있었고, worktree가 가리키던 OneDrive 저장소가 삭제되면서 Git 메타데이터가 유실됐다. 작업 트리 파일만 남아 있다.
- 2026-09-25 AI 파트 감사에서 두 가지가 확인됐다.
  - 7월 수정은 대부분 타당하다.
  - 그러나 4건은 그대로 옮기면 안 된다.
    - #4: 추론 시 O(사용자×뉴스) 재계산 회귀
    - #5: LambdaRank 그룹이 사용자 전체 기간
    - #8: 하위그룹 분할 경로에서 재시도 상한에 도달하지 못함
    - #16: negative 후보가 클릭 시점에 없던 뉴스를 포함
- 기존 로컬 clone에는 프로젝트와 무관한 비추적 파일이 많다.

## 검토한 대안

### 저장소
1. **새 저장소에 필요한 코드만 가져오기.** 초기 위키 계획이었다.
   - 장점: 깨끗하게 시작하고, 소유가 명확하다.
   - 단점: 팀 커밋 이력(설계 맥락)이 끊긴다. 팀원 코드의 출처를 별도로 표기해야 한다. 원 프로젝트와의 연결이 약해진다.
2. **기존 fork(`khj1212k/pro-recsys-finalproject-recsys-01`)에서 계속한다. (채택)**
   - 팀 이력이 보존된다.
   - `team-final` 이후 커밋은 모두 본인 작업이라 기여 경계가 Git 이력으로 증명된다.
3. **원 조직 저장소(`boostcampaitech8/...`)에 PR을 보낸다.**
   - 팀이 해산됐고 공동 유지보수 합의가 없어서 제외했다. `upstream` remote는 push를 막아 둔다.

### 7월 수정을 옮기는 방식
1. **스냅샷 전체를 커밋 하나로 병합한다.**
   - 리뷰할 수 없다.
   - 알려진 결함 4건이 그대로 들어간다.
2. **주제별로 다시 이식하면서 교정한다. (채택)**
   - 로컬 전용 참조 브랜치 `port/fix-snapshot`을 만든다. 병합하지 않는다.
   - 파이프라인 쪽과 추천 엔진 쪽을 나눠 PR로 올리고, 항목별로 커밋한다.
   - 교정 4건과 추가 변경을 함께 반영한다.
     - 추가 변경: torch를 선택 의존성으로 분리, `kiwipiepy` 명시, 모델을 LightGBM 텍스트 형식으로 저장, 실패·재시도 LLM 호출도 비용 지표에 기록, `.env.example`의 비밀번호 제거.

### 커밋 이력에 남은 DB 비밀번호
- **대안:** `git filter-repo`로 이력을 다시 쓴다.
  - fork 관계가 깨진다.
  - 원 저장소에는 값이 그대로 공개되어 있어서 효과가 없다.
- **결정:** 이력은 그대로 둔다.
  - 해당 값은 부트캠프 종료로 해체된 서버의 것이므로 폐기한다.
  - 파일에서는 제거한다.
  - 이후 비밀은 `.env`와 GitHub Actions secrets로만 관리한다.

## 결정
- 기존 fork의 `main`에 주제별 브랜치를 PR로 병합하는 방식으로 개선을 계속한다.
- 7월 수정은 교정과 함께 재이식한다.

## 증거
- `port/fix-snapshot`에서 7월 테스트 57개가 통과한다(Python 3.11). 최신 scipy와 충돌하는 torch 스텁 1줄만 고쳤다.
- 이식 후 테스트 수, 교정 항목별 테스트, #4 memoize 전후 실행 시간(이 워크트리에서 재측정):
  - **전체 테스트**: `port/july-self-review` 브랜치에서 `pytest -q` 89개 전부 통과. 문서·설정 파일의 문자열만 검사하던 메타 테스트는 기능을 검증하지 않고 테스트 수만 부풀려 제외했다. 실행 시간 벤치마크(#4)는 `benchmark` 마커로 분리해 CI에서는 제외한다.
  - **#4 (추론 시 O(사용자×뉴스) 재계산 회귀 → 메모이즈)**: `tests/recommend_engine/test_history_embedding_leakage.py` 6건.
    - 호출 횟수 검증(`test_compute_history_embedding_is_memoized_per_user_and_cutoff`): `_compute_history_embedding_uncached`를 spy로 감싸, 유저 5명 × 뉴스 20건을 반복 호출해도 실제 계산은 unique (user_id, cutoff) 조합 수(5회)만 발생함을 확인.
    - 값 동치성 검증(`test_compute_history_embedding_matches_naive_unmemoized_values`): 메모이즈 전 구현(iterrows 기반 naive 참조 구현)과 메모이즈 후 구현이 유저 15명 × cutoff 3개 조합에서 동일한 결과를 냄을 확인(atol=1e-5).
    - 벤치마크(`test_history_embedding_memoized_inference_is_faster_than_naive_benchmark`, 300 유저 × 200 뉴스 합성 데이터): 이 워크트리(로컬 macOS, Python 3.11)에서 3회 재측정한 실측값은 naive(수정 전) 약 14.48~15.12초 → 메모이즈(수정 후) 약 0.044~0.053초, 약 285~331배 단축. (7월 원본 셀프 리뷰 노트에는 107.49s → 0.149s, 약 722배로 기록되어 있었으나, 이는 다른 하드웨어에서 측정된 값이라 그대로 인용하지 않고 이 브랜치·이 환경에서 재측정한 값으로 대체했다. 절대 시간은 하드웨어에 따라 달라지지만, "메모이즈가 naive보다 항상 빨라야 한다"는 테스트 자체의 assertion과 호출 횟수 검증은 하드웨어 무관하게 통과한다.)
  - **#5 (LambdaRank 그룹이 사용자 전체 기간 → 그룹 키 명시)**: `tests/recommend_engine/test_lgbm_ranker_lambdarank.py` 4건 + `tests/recommend_engine/test_lambdarank_group_split.py` 7건, 총 11건. `group_key`가 `user_id`(팀 원본, 유저당 전체 기간을 한 그룹으로 취급)와 `user_timestamp`(교정 후 기본값, 유저×클릭 타임스탬프 단위로 그룹 분리) 두 모드로 명시적으로 분기되는지, 그리고 train/valid 분할이 한 그룹을 중간에서 자르지 않는지(`_find_group_safe_split_index`, `time_ordered_group_safe_split`)를 검증.
  - **#8 (하위그룹 분할 경로에서 재시도 상한 미도달 → 가드 위치 수정)**: `tests/test_cluster_retry_count.py` 4건(단위, `cluster_retry_count` 증가 로직) + `tests/test_cluster_eval_retry_graph.py` 2건(그래프 레벨: `workflow.graph.compile_workflow`로 컴파일한 LangGraph 앱 전체를 fake evaluator로 구동해 sub-group/outlier 두 경로 모두 `MAX_RETRY_CLUSTER_EVAL` 상한에서 실제로 멈추는지 end-to-end로 검증), 총 6건.
  - **#16 (negative 후보가 클릭 시점에 없던 뉴스를 포함 → point-in-time 필터링)**: `tests/recommend_engine/test_negative_sampling_seed.py` 7건 — 시드 고정 결정성 4건(같은 시드면 같은 negative 샘플, 시드가 다르면 다른 샘플, 전역 `random`이 아닌 로컬 RNG 사용) + `eligible_news_ids_as_of()`가 각 클릭 시점 이전에 존재한 뉴스만 negative 후보 풀로 사용하고 이 인덱스가 메모이즈되어 1회만 구축되는지, 실제 학습 데이터셋 생성 시 미래 뉴스가 negative로 샘플링되지 않는지 3건.
- **포팅 여부 (FIX_LOG 26건 기준, `docs/fix-log-2026-07.md`)**:
  - 25건은 코드 변경으로 포팅됨 (#1–#25, 위 4건은 교정 포함). 세부 매핑은 각 커밋 메시지의 `FIX_LOG #n` 표기와 `docs/fix-log-2026-07.md`를 참고.
  - 1건(#26, `NewsEmbedder`/`BGEEmbedder` 중복 조사)은 "통합하지 않는다"는 조사 결론만 포팅했다 — 코드 변경 없음(원문 그대로 의도된 결과), 결론은 `ai_workspace/recommend_engine/README.md`의 "알려진 한계"에 문서화되어 있다.
  - **`PORTFOLIO.md`(#22의 일부)는 포팅에서 제외(drop)했다.** ADR 3건(`docs/adr/0001`–`0003`)은 포팅했지만, git log 기반 기여 통계와 회고를 담은 `PORTFOLIO.md`는 이 fork의 커밋 이력 자체가 기여 증거를 대신하므로 별도 문서로 유지하지 않기로 했다.

## 결과와 한계
- 팀 기여(`team-final`까지)와 개인 작업(이후)이 이력으로 분리된다.
- 추천 엔진 원 설계는 팀원 성승우, 합성 데이터는 이선진의 작업이다. 커밋 본문과 ADR에 출처를 적는다.
- 7월 수정의 테스트는 모두 mock 기반이다. 실제 DB 동작은 이후 PostgreSQL integration 테스트에서 검증해야 한다.
