# reports

평가·운영 실험의 결과를 두는 곳이다. README와 ADR에 새로 적는 수치는 여기 있는 파일에서 가져온다. 이 규칙을 두기 전에 ADR 본문에만 적힌 측정 두 건(ADR 0005 부록의 LLM 호출 프로브, ADR 0004의 메모이즈 벤치마크)은 리포트 파일이 없는 예외로 남아 있다.

## 규칙

- 파일: `reports/<영역>/<이름>_v<번호>.json`(수치 원본)과 같은 이름의 `.md`(해석). 영역은 `recsys`, `llm`, `clustering`, `ops`, `serving`, `sim` 등. CI가 만든 결과 JSON은 실행 번호를 이름에 넣고, 해석을 ADR에 둔 경우에는 표에 그 ADR을 적는다.
- 각 리포트 머리말에 적는 것: 실행 명령, 코드 git SHA, 실행 날짜, 데이터 출처와 기간, 표본 크기, 불확실성(신뢰구간 또는 시드 수).
- 데이터 출처를 섞지 않는다. 공개 벤치마크, 한국어 수집 데이터, 시뮬레이터, 부하 테스트 결과는 서로 다른 주장이므로 표에 출처를 함께 적는다.
- 음성 결과와 실패한 실험도 남긴다.

## 저작권 경계 ([ADR 0023](../docs/adr/0023-data-sources-copyright-retention.md))

- 언론사 기사 본문과 제목 목록을 싣지 않는다. 기사 id, URL, 본문 sha256, 건수·길이 같은 집계만 쓴다. 예시가 꼭 필요하면 한 문장 이내로 인용하고 출처를 적는다.
- 공공누리 제1유형으로 수집한 정책브리핑 기사는 출처를 표시하면 본문을 실을 수 있다.
- 라이선스가 반출을 막는 외부 데이터셋은 집계 수치만 커밋한다.
- `reports/**/raw/`는 `.gitignore`로 추적하지 않는다.

## 목록

| 리포트 | 내용 | 데이터 |
|---|---|---|
| [recsys/parity_v1](recsys/parity_v1.md) | train/serve parity 게이트: 서빙이 칸 로그에 남긴 피처·shadow 점수·후보를 같은 로그에서 오프라인 하네스 방식으로 다시 계산해 비교한 4항목(추천 품질의 수치가 아니다). 원자료 `parity_v1.json`은 CI 실행 37389722931의 아티팩트 그대로. 설계와 한계는 [ADR 0033](../docs/adr/0033-train-serve-feature-parity.md) | 합성(CI 테스트 DB에 시드한 뉴스레터·클릭, 무작위 데이터로 만든 22열 모델) |
| [recsys/constant_feature_ablation_v1](recsys/constant_feature_ablation_v1.md) | 상수 피처 2개 제거 전후 LightGBM 예측 비교 | 합성 |
| [recsys/team_repro_v2](recsys/team_repro_v2.md) | 팀 베이스라인 재현과 분해 실험(v2.2). 추론 시점 누출, 베이스라인 대비, 조기 종료 아티팩트. 원자료는 `team_repro_v2.json`·`team_repro_v2_raw.json`. 프로토콜은 [ADR 0007](../docs/adr/0007-recsys-offline-evaluation-protocol.md) | 팀이 남긴 합성 클릭 아카이브(LLM 페르소나 100명, 저장소 밖) |
| [recsys/team_repro_v1](recsys/team_repro_v1.md) | 위 재현의 첫 판. 방법론 검토에서 신뢰 불가 판정을 받아 v2로 다시 썼다. 조사 기록으로만 남기며 수치를 인용하지 않는다 | 같음 |
| [recsys/ebnerd_v1](recsys/ebnerd_v1.md) | [EB-NeRD] 팀 방식 LightGBM에서 ranker v2까지의 오프라인 평가. 부록의 원자료는 `ebnerd_v1_1_poolneg.json`·`ebnerd_v1_click_time_sensitivity.json`. 프로토콜과 승격 규칙은 [ADR 0013](../docs/adr/0013-ranker-v2-design.md) | 덴마크어 공개 뉴스 클릭 로그 `ebnerd_small`(집계만, 데이터는 저장소 밖) |
| [serving/](serving/README.md) | 요청 시점 추천의 요청 경로 지연. CI 실행 3건의 결과 JSON. 해석은 [ADR 0015](../docs/adr/0015-request-time-recommendation.md)·[0017](../docs/adr/0017-short-term-state-store.md) | 합성, GitHub 호스티드 러너, HTTP 처리 제외 |
| [sim/grid_v1](sim/grid_v1.md) | [SIM] 시뮬레이터 지표 타당성 격자와 사전 등록 판정. 보정 기저율은 `sim/base_rates_v1.json`. 설계와 주장 범위는 [ADR 0019](../docs/adr/0019-user-simulator-design-and-claim-scope.md) | 시뮬레이터(합성 카탈로그, 팀 아카이브 카탈로그). 추천 정확도 수치가 아님 |
| [ops/embedding_truncation_2026-09-26.json](ops/embedding_truncation_2026-09-26.json) | BGE-M3 입력 토큰 길이 분포와 `max_length`별 절단 영향. 해석은 [ADR 0006](../docs/adr/0006-runtime-compose-and-scheduler.md) 6절 | 한국어 수집 기사 456건(집계만) |

새 리포트를 커밋할 때 이 표에 행을 더한다.
