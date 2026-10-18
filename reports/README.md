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
| [recsys/parity_v1](recsys/parity_v1.md) | train/serve parity 게이트: 서빙이 칸 로그에 남긴 피처·shadow 점수·후보를 같은 로그에서 오프라인 하네스 방식으로 다시 계산해 비교한 4항목(추천 품질의 수치가 아니다). 원자료 `parity_v1.json`은 CI 실행 37389722931의 아티팩트 그대로, M5 재실행(ADR 0014, 클릭 순서 `popular`) `parity_v1_m5.json`은 CI 실행 39007956452의 아티팩트 그대로. 설계와 한계는 [ADR 0033](../docs/adr/0033-train-serve-feature-parity.md) | 합성(CI 테스트 DB에 시드한 뉴스레터·클릭, 무작위 데이터로 만든 22열 모델) |
| [recsys/constant_feature_ablation_v1](recsys/constant_feature_ablation_v1.md) | 상수 피처 2개 제거 전후 LightGBM 예측 비교 | 합성 |
| [recsys/team_repro_v2](recsys/team_repro_v2.md) | 팀 베이스라인 재현과 분해 실험(v2.2). 추론 시점 누출, 베이스라인 대비, 조기 종료 아티팩트. 원자료는 `team_repro_v2.json`·`team_repro_v2_raw.json`. 프로토콜은 [ADR 0007](../docs/adr/0007-recsys-offline-evaluation-protocol.md) | 팀이 남긴 합성 클릭 아카이브(LLM 페르소나 100명, 저장소 밖) |
| [recsys/team_repro_v1](recsys/team_repro_v1.md) | 위 재현의 첫 판. 방법론 검토에서 신뢰 불가 판정을 받아 v2로 다시 썼다. 조사 기록으로만 남기며 수치를 인용하지 않는다 | 같음 |
| [recsys/ebnerd_v1](recsys/ebnerd_v1.md) | [EB-NeRD] 팀 방식 LightGBM에서 ranker v2까지의 오프라인 평가. 부록의 원자료는 `ebnerd_v1_1_poolneg.json`·`ebnerd_v1_click_time_sensitivity.json`. 프로토콜과 승격 규칙은 [ADR 0013](../docs/adr/0013-ranker-v2-design.md) | 덴마크어 공개 뉴스 클릭 로그 `ebnerd_small`(집계만, 데이터는 저장소 밖) |
| [recsys/ebnerd_v1_2_cold](recsys/ebnerd_v1_2_cold.md) | [EB-NeRD] v1.2 콜드 regime(E1–E4, E6–E8): 인기도 0·저트래픽·작은 풀·히스토리 절단에서의 랭커와 휴리스틱, 랭크 정규화, 축소 CTR, 4항 가중치 적합, 서빙 후보 구성. 사전 등록 판정을 코드가 계산했다(재현 게이트 통과, **E3 불충족** 포함). JSON·md는 Colab 실행의 산출물 그대로이고, 실행 명령·코드 SHA·날짜·시도와 세션 유실·CU는 `ebnerd_v1_2_cold_run.json`. 규칙과 해석은 ADR 0013 "A2 사전 등록"·"A2 결과", [ADR 0031](../docs/adr/0031-cold-start-and-language-transfer-contract.md) | `ebnerd_small`(집계만, 데이터는 저장소 밖). 2026-10-06 Colab CPU 런타임 |
| [recsys/ebnerd_v1_3_neural](recsys/ebnerd_v1_3_neural.md) | [EB-NeRD] E15: 신경망 사용자 모델(NRMS-lite·SASRec-lite)과 GBDT 스태킹 대 같은 예산으로 튠한 LightGBM. 사전 등록 판정을 코드가 계산했다(재현·결정론 게이트 통과, 판정 비교 4개 모두 **seed 규칙 불통과**, 주장 `none`, shadow 자격 arm 없음, 서빙 비용 게이트는 미룸). 새 체크포인트 사슬(코드 `381f61d`, 세션 S2b–S4b)의 마지막 묶음에서 Mac `assemble`로 만든 리포트이고 VM이 만든 같은 리포트와 수치 최대 차이 2.2e-16이다. 세션별 CU·유실과 재개·입력 대조는 ADR 0013 A3.12, 판정과 해석은 [ADR 0013 "A3 결과"](../docs/adr/0013-ranker-v2-design.md) | `ebnerd_small`(집계만, 데이터는 저장소 밖) |
| [kr/embedding_check_v1](kr/embedding_check_v1.md) | 한국어 기사에서 BGE-M3 임베딩이 주제 구조를 담는지 보는 점검: 언론사가 URL에 붙인 섹션을 서비스 7개 범주로 옮긴 라벨의 kNN(k=10) leave-one-out 정확도·macro-F1(기사 단위 부트스트랩 구간), 다수 클래스·무작위 기준선, 서술용 민감도(언론사별 섹션, 언론사를 건너는 예측, 언론사 라벨, 거의 같은 기사 제외, k=5·20). 추천 정확도가 아니다. md는 `evaluation/recsys/kr_embedding_check.py`가 JSON에서 만든다. 기준과 해석은 [ADR 0031](../docs/adr/0031-cold-start-and-language-transfer-contract.md) 결정 5 | 한국어 수집 기사(E0 동결 반출본 6,874건의 임베딩 팩, 섹션 라벨은 매일경제·동아일보 1,890건, 집계만) |
| [serving/](serving/README.md) | 요청 시점 추천의 요청 경로 지연. CI 실행 3건의 결과 JSON. 해석은 [ADR 0015](../docs/adr/0015-request-time-recommendation.md)·[0017](../docs/adr/0017-short-term-state-store.md) | 합성, GitHub 호스티드 러너, HTTP 처리 제외 |
| [sim/grid_v1](sim/grid_v1.md) | [SIM] 시뮬레이터 지표 타당성 격자와 사전 등록 판정. 보정 기저율은 `sim/base_rates_v1.json`. 설계와 주장 범위는 [ADR 0019](../docs/adr/0019-user-simulator-design-and-claim-scope.md) | 시뮬레이터(합성 카탈로그, 팀 아카이브 카탈로그). 추천 정확도 수치가 아님 |
| [sim/ope_validation](sim/ope_validation.md) | [SIM] E9: 탐색 칸 로그로 한 오프폴리시 추정값을 같은 세계에서 타깃 정책을 실제로 돌린 값과 비교. 사전 등록 판정은 **실패**(탐색 2칸, 재실험 4칸 모두). 규칙과 해석은 [ADR 0025](../docs/adr/0025-logging-v2-exploration-and-ope.md) 사전 등록 2·보완 A1·증거 9. CI 실행 37400003072의 아티팩트 그대로 | 시뮬레이터(합성 카탈로그, 300명 x 7일 x seed 3개, 실제 요청 시점 추천 경로 + 임베딩 대역. 서빙 코드는 ADR 0033 이전의 것). 추천 정확도·서비스 클릭률이 아님 |
| [sim/fatigue_v1](sim/fatigue_v1.md) | [SIM] E10: 노출 피로 규칙을 세기만 한 실행(log)과 적용한 실행(enforce)의 비교. 사전 등록 판정은 **enforce 조건 미충족**(반복 노출은 줄지만 `similar_share_lift`가 나빠짐). 규칙과 해석은 ADR 0025 사전 등록 4·보완 A1.4·증거 9. 같은 CI 실행의 아티팩트 그대로 | 같음 |
| [ops/embed_job_s0_dry_run_2026-10-17.json](ops/embed_job_s0_dry_run_2026-10-17.json) | 기사 임베딩 원격 잡의 S0 드라이 런: 핀 확인, 처리량, 결정론, fp16 대 fp32 코사인, 연산 단위. 해석은 [ADR 0036](../docs/adr/0036-experiment-data-path-frozen-export-file-stand-ins.md) 증거 7 | 합성 텍스트 300건(지어낸 낱말, 기사 아님), Colab T4 |
| [llm/e0_baseline_v1](llm/e0_baseline_v1.md) | E0 기준선 측정(2026-10-17, run `run-20261017T054231Z`): main 생성 파이프라인을 코드 그대로 실제 수집 기사 15개 클러스터(+ warmup 2)에 돌린 기술 통계 - 완주·스키마·재시도·지연·토큰·비용·결정론 검사기·문체 드리프트·judge v2 shadow·규칙 위반·실패 분류. 사람 라벨은 없다(사용자 결정, A8.13) - 자동 라벨은 아래 `e0_auto_labels_v1`. 원자료는 `e0_baseline_v1.json`. 규칙은 [ADR 0009 A8](../docs/adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md) | 한국어 수집 기사(Tier 0 동결 반출본, 집계만) |
| [llm/e0_auto_labels_v1](llm/e0_auto_labels_v1.md) | E0의 사람 라벨 자리를 채운 자동 라벨(2026-10-17, 사용자 결정): 라벨러 gemini-3-flash-preview(가장 강한 후보가 아니다 - 비용 상한 $0.50), 기사당 앞 1500자, 런 a 전부 + 런 b(재라벨 대상과 예산 안의 나머지)의 자기 일관성, 라벨러 대 judge v2·ClusterEvaluator. 사람 라벨 없음, 라벨러 정확도는 재지 않았고 셋 다 Gemini(같은 계열). 원자료는 `e0_auto_labels_v1.json`. 규칙과 실행 기록은 [ADR 0009 A8.13·A8.14](../docs/adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md) | 한국어 수집 기사(E0 표본, 집계만) |
| [ops/embedding_truncation_2026-09-26.json](ops/embedding_truncation_2026-09-26.json) | BGE-M3 입력 토큰 길이 분포와 `max_length`별 절단 영향. 해석은 [ADR 0006](../docs/adr/0006-runtime-compose-and-scheduler.md) 6절 | 한국어 수집 기사 456건(집계만) |

새 리포트를 커밋할 때 이 표에 행을 더한다.
