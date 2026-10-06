# train/serve parity 게이트 v1 (ADR 0033)

> 이 리포트의 수치는 **두 계산 경로가 같은 값을 내는지**에 대한 것이다. 추천 품질의 수치가 아니다.
> 게이트가 쓰는 모델은 무작위 데이터로 만든 22열 LightGBM 모델이다(같은 입력에 같은 점수가 나오는지를 보는 도구).

`parity_v1.json`은 CI 실행 **37389722931**(커밋 `30f6553`, GitHub 호스티드 러너, PostgreSQL 16 + pgvector)의
integration 잡이 쓴 파일을 **그대로 옮긴 것**이다. 손으로 고친 값이 없다. 로컬에서는 DB를 띄우지 않는다.

| 항목 | 값 |
|---|---|
| 실행 명령 | `RECSYS_PARITY_REPORT=parity_v1.json python -m pytest -q -s -m integration tests/integration/test_feature_parity.py` (CI의 "Write parity gate report" 스텝) |
| 코드 | `30f655320f9877e9b436be9dd8c7a970b74430dc` (JSON의 `meta.commit`) |
| 실행 | 2026-10-05T23:40:41Z, GitHub Actions 실행 37389722931 (`meta.ci_run_id`) |
| 데이터 | 합성. 테스트가 CI의 빈 테스트 DB에 시드한 뉴스레터 348개·클릭 585건·노출 6,500행(재생 중에 쌓인 것 포함). 실제 사용자·기사 없음 |
| 표본 | 재생한 요청 200건 중 어댑터 피처가 남은 198건, 화면 칸 3,960개, 요청 사용자 6명 |
| 불확실성 | 없음(구간을 내지 않는다). 시드 하나의 재생 한 번이고, 값은 추정이 아니라 두 계산의 차이다. 같은 테스트가 push마다 다시 돈다 |

파일 이름에 실행 번호가 없는 것은 설계 문서가 정한 이름(`reports/recsys/parity_v1.json`)을 따랐기 때문이다.
실행 번호는 위 표와 JSON 안에 있다.

## 어떻게 만들어지는가
1. `tests/integration/test_feature_parity.py`가 테스트 DB에 뉴스레터 348개, 사용자 11명, 과거 클릭·노출을 시드한다.
   과거 클릭은 클릭 API를 거치지 않은 행이라 `rebuild_user_state` 잡의 함수로 장기 프로필 상태에 채운다.
2. 22열 LightGBM 모델을 `scripts/register_model.py`의 함수로 shadow 등록한다.
3. 운영 배선(`build_sql_service`)의 서비스 하나로 `GET /newsletters/today` 200건을 재생하고, 그중 일부를
   `POST /logs/newsletter/click`으로 클릭한다(실제 라우터).
4. DB의 로그만 읽어(`evaluation/recsys/serving_parity.load_from_db`) 게이트 네 항목을 계산한다(`run_gate`).
5. CI의 "Write parity gate report" 스텝이 `RECSYS_PARITY_REPORT=parity_v1.json`으로 그 테스트를 다시 돌려 파일을
   남기고, 아티팩트 `recsys-parity`로 올린다.

갱신하는 법: `gh run download <실행 ID> -n recsys-parity` 로 받은 파일을 이 디렉터리에 그대로 둔다.
서빙 피처의 정의가 바뀌면(`recsys_core.serving.SCHEMA_HASH`) 이 파일은 지난 정의의 증거가 된다 -
`tests/recsys/test_feature_parity.py`가 그 경우에 실패해 새 실행의 파일로 바꾸게 한다.

같은 게이트를 임의의 DB에 대해 돌리는 명령(읽기만 한다):

```bash
python -m evaluation.recsys.serving_parity --database-url postgresql://... \
    --model-name ranker --model-version <shadow로 등록된 버전> --out parity.json
```

## 결과 (실행 37389722931)
재생 200건 중 어댑터 피처가 남은 요청 198건(나머지 2건은 신호 없는 사용자의 인기 목록 응답), 칸 3,960개.

| 항목 | 기준 | 값 | 판정 |
|---|---|---|---|
| 1. 피처: 칸 로그 vs 같은 로그의 하네스 재계산 | 전 열 max\|Δ\| < 1e-6, NaN 위치 동일 | 1.19e-7 (`hist_cos`. 나머지 21개 열은 0), NaN 불일치 0 | 통과 |
| 2. 점수 순서: 칸 로그의 shadow 점수 vs 재계산 피처의 점수 | 요청마다 Kendall τ = 1 (동점 제외) | 198건 모두 1.0, 점수 차 최대 0 | 통과 |
| 3. 랭커 상위 20개 겹침: 서빙의 후보 집합 위 vs 하네스의 후보 구성 위 (JSON 키 `end_to_end`) | 평균 ≥ 0.9 | 평균 0.9987, 최소 0.95, 1.0인 요청 193건 | 통과 |
| 4. 후보 생성기 구성 | 서빙 = 하네스의 서빙 구성 | 같음(창 72h, knn 100·100, recent 100, popular 100, category 50, cap 300) | 통과 |

3번에서 두 쪽의 후보 집합은 같지 않다: 요청당 평균 후보 수 서빙 172.7·하네스 214.5, Jaccard 0.75.

**3번은 서빙이 실제로 내보낸 목록을 비교한 값이 아니다.** 두 목록 모두 오프라인에서 다시 계산한 것이다: 같은
이벤트 로그, 하네스 방식으로 다시 계산한 피처, 같은 모델. 다른 것은 후보 집합 하나다(요청 로그에 남은 서빙의
후보 집합 대 하네스의 서빙 후보 구성으로 고른 후보). 서빙이 내보낸 목록은 활성 스코어러(휴리스틱)와 MMR,
탐색 칸이 만든 것이고 이 모델이 만든 것이 아니어서 비교 대상이 아니다. 1·2번이 통과한 상태에서 3번의 0.9987이
말하는 것은 "후보 생성기가 갈려도 이 모델의 상위 20개는 거의 같았다"까지다.

설계 문서는 이 항목을 "end-to-end 목록 top-20 겹침"이라고 불렀고, 이 실행의 JSON도 키가 `end_to_end`
(임계는 `meta.thresholds.end_to_end_mean_overlap`)다. 서빙된 목록까지 비교한 것처럼 읽혀서 2026-10-06에 코드의
키를 `candidate_generator_top20_overlap`(임계 `candidate_generator_top20_mean_overlap`)로 바꿨다. 계산은 그대로다.
옮겨 둔 JSON은 그 실행의 파일 그대로 두었고, 다음 실행의 아티팩트부터 새 이름과 `compares` 설명이 들어간다.
예전 이름의 파일은 `evaluation.recsys.serving_parity.read_report`가 새 이름으로 옮겨 읽는다.

## 이 실행 앞의 실패 (같은 테스트, 실행 37388934917, 커밋 `9aa0922`)
1번이 실패했다: max|Δ| = 0.0397. 2·3·4번은 통과(τ 1.0, 겹침 0.9987). 최근 20개 클릭을 서빙은 마이크로초 순으로,
재계산은 정수 초 + 아이템 순으로 골라, 같은 초에 클릭이 여러 건일 때 서로 다른 클릭이 남았다. 순서를
(초, 뉴스레터 ID)로 고정해 고쳤다(`a68b44c`). 경위는 ADR 0033 증거 5.

## 읽을 때의 한계
- 게이트는 "서빙의 값 = 같은 서비스 로그를 같은 설정으로 하네스 방식으로 계산한 값"을 본다. EB-NeRD에서 학습한
  모델이 본 정의와 서빙의 정의가 다른 곳(단기 상한 20, 30분 세션, 카테고리 부호 등)은 보지 않는다(ADR 0033 한계).
- 시드 데이터는 합성이다(주제 4개 주변의 임베딩). 요청은 한 프로세스에서 차례로 온다 - 동시 요청에서의 경합은
  이 실행에 없다.
- 3번의 0.9987은 이 시드·이 모델에서의 값이다. 후보 생성기의 "인기" 출처는 서빙(최신성 + 묶인 기사 수)과
  하네스(최근 6시간 클릭 수)가 다른 정의이고, 모델이 그 출처에만 있는 후보를 위로 올리면 겹침은 내려간다.
- 서빙이 실제로 내보낸 목록과 오프라인 순위를 비교한 항목은 이 게이트에 없다. 등록된 모델이 active가 되어
  목록을 만들기 전에는 비교할 "모델의 서빙 목록"이 없다(지금 목록을 만드는 것은 휴리스틱이다).
