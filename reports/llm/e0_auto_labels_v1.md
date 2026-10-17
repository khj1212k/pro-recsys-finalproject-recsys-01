# E0 자동 라벨 리포트 (e0-auto-labels-v1)

E0 기준선(ADR 0009 A8)의 사람 라벨 자리를 자동 라벨러가 채운 기술 통계다(사용자 결정 2026-10-17, A8.13). **사람 라벨은 없다.** 모든 수치는 자동 라벨러(LLM)의 판단이고 그 정확도는 재지 않았다. 비율에 Wilson 95% CI와 분모를 적는다. 기사·초안·라벨러가 쓴 텍스트는 싣지 않는다(id·라벨 값·건수만).

## 머리말

| 항목 | 값 |
|---|---|
| command | "python -m evaluation.llm.e0_auto_label_report" |
| head_sha | "51d1425229baebeafd5ebe08ed48f443898eeb48" |
| protocol | "e0-auto-labels-v1" |
| protocol_sha256 | "b3aa857d6303227f4d4724892eae7b2325552b1fa67dada7421ed6460248a479" |
| adr | "docs/adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md#A8.13" |
| e0_run | "run-20261017T054231Z" |
| labeler_model | "gemini-3-flash-preview" |
| labeler_model_versions_seen | ["gemini-3-flash-preview"] |
| generator_model | "gemini-3.5-flash-lite" |
| judge_v2_model | "gemini-3.1-flash-lite" |
| seeds | {"a": 20261017, "b": 20261018} |
| temperature | 1.0 |
| thinking_level | "minimal" |
| article_body_chars | 1500 |
| schema_mode_used | "json_schema" |
| thinking_spelling_used | "lower" |
| preflight | {"cluster": "ok", "output": "ok", "coverage": "ok", "tone": "ok"} |
| preflight_changes | [] |
| cost_cap_usd | 0.5 |
| package_expires_at_utc | "2026-11-12T00:05:07Z" |
| human_labels | "없음 - 사용자 결정(2026-10-17, ADR 0009 A8.13 (a))" |
| results_viewed | "A8.13 커밋까지 자동 라벨 0건. 이 리포트가 자동 라벨의 첫 집계다" |
| in_client_spend(run) | {"run_id": "e0-auto-labels-v1", "run_usd": 0.328228, "day_usd": 0.483842, "total_usd": 0.483842} |

## 1. 호출과 비용

- 라벨링 HTTP 시도 98건, 상태 {"200": 98}, 전송 오류 {}, 결과 {"ok": 98}, 종료 사유 {"STOP": 98}
- pre-flight 4건, $0.003013
- 비용(라벨러 단가로 계산): 라벨링 $0.325215 + pre-flight $0.003013; 가드 원장의 이 런 합 $0.328228 (상한 $0.5)
- 토큰(라벨링): {"prompt": 517800, "candidates": 22105, "thoughts": 0, "cached": 104162, "total": 539905}. 응답이 밝힌 캐시 입력(cached)도 정가로 계산했다(ADR 0035의 정산 규칙) - 실제 청구는 이보다 적을 수 있다
- 종류별: {"cluster": {"attempts": 26, "cost_usd": 0.149662}, "output": {"attempts": 24, "cost_usd": 0.135782}, "coverage": {"attempts": 24, "cost_usd": 0.01634}, "tone": {"attempts": 24, "cost_usd": 0.023431}}
- 응답 모델 버전: gemini-3-flash-preview
- 지연(초, 200 응답): {"n": 98, "median": 2.1615, "min": 1.187, "max": 5.864, "sum": 245.861}
- 실행: [{"at": "2026-10-17T06:29:12.001526+00:00", "stop": null, "result": {"stats": {"labeled": 79}, "resent_after_crash": 0, "extension_stopped_before": "r1-c22", "extension_groups_total": 14}, "spent_run_usd": 0.2571175}, {"at": "2026-10-17T06:31:08.809530+00:00", "stop": null, "result": {"stats": {"labeled": 19}, "resent_after_crash": 0, "extension_stopped_before": "r2-c8", "extension_groups_total": 14}, "spent_run_usd": 0.328228}]

## 2. 라벨 단위

- 런 a: {"cluster": {"labeled": 15}, "output": {"labeled": 14}, "coverage": {"labeled": 14}, "tone": {"labeled": 14}}
- 런 b: {"cluster": {"labeled": 11}, "output": {"labeled": 10}, "coverage": {"labeled": 10}, "tone": {"labeled": 10}}
- 다시 물어 라벨이 된 단위 0, 라벨이 안 된 단위의 위반 코드 {}
- 분모: {"n_p": 15, "clusters_in_package": 15, "outputs_in_package": 14, "outputs_with_saved_conversion": 14, "relabel_targets": {"clusters": ["r1-c51", "r2-c14", "r3-c149"], "outputs": ["o1200206d98", "o1acd87ba0a", "o432cfeb6b5", "o9ad5016cc8", "oa610149f6d"]}}

## 3. 런 a - 클러스터 라벨

- 라벨 15개, single_event 11/15 = 0.733 [0.480, 0.891], 이상치 있음 4/15 = 0.267 [0.109, 0.519], 주된 사건 없음 0
- 핵심 사실 수 {"n": 15, "median": 5.0, "min": 4.0, "max": 6.0, "sum": 78.0}, 이상치 수 {"n": 15, "median": 0.0, "min": 0.0, "max": 5.0, "sum": 9.0}
- 파이프라인 결과 × single_event(A8.8 쓰임 2): {"saved": {"single_event_Y": 11, "single_event_N": 3}, "skipped": {"single_event_Y": 0, "single_event_N": 1}}
- ClusterEvaluator 첫 판정 × single_event: 13/15 = 0.867 [0.621, 0.963], κ(참고) 0.5946, 불일치 r1-c29, r3-c2, 표 {"pass_single_Y": 11, "pass_single_N": 2, "fail_single_Y": 0, "fail_single_N": 2}

## 4. 런 a - 출력 라벨

- 발행 가능률(A8.8 쓰임 1, 자동 라벨): 주 지표(저장본 Y / n_p) 11/15 = 0.733 [0.480, 0.891]; 저장본만 11/14 = 0.786 [0.524, 0.924]; 첫 초안만 11/14 = 0.786 [0.524, 0.924]
- 라벨러 자신의 publishable(규칙 합성 전) 11/14 = 0.786 [0.524, 0.924]; 지켜지지 않은 규칙별 출력 수(한 출력이 여러 규칙에 걸릴 수 있다, A8.13 (b)6) {"coverage_ok": 1, "labeler_publishable": 3, "no_fact_errors": 3}
- 사실 오류 ≥1 3/14 = 0.214 [0.076, 0.476]; 출력당 {"n": 14, "median": 0.0, "min": 0.0, "max": 1.0, "sum": 3.0}; 유형 {"date": 1, "other": 2}; 위치 못 찾음 0, 여러 위치(첫 위치 사용) 0
- style {"4": 4, "5": 10}; 제목·한줄소개 불일치 0/14 = 0.000 [0.000, 0.215]
- 핵심 사실 커버리지(사실 단위) 58/74 = 0.784 [0.677, 0.862]; 기준 {"a": 14}
- tone_drift(의미 기준, 저장 변환본) 0/14 = 0.000 [0.000, 0.215], 대상 아님 0
- 결정론 검사기 대 자동 라벨(첫 초안, A8.8 쓰임 3의 계산 - 정답 대비가 아니다): {"precision": {"k": 0, "n": 0, "rate": null, "wilson95": null}, "recall_number": {"k": 0, "n": 0, "rate": null, "wilson95": null}, "recall_quote": {"k": 0, "n": 0, "rate": null, "wilson95": null}, "draft_level_2x2": {"blocked_and_error": 0, "blocked_no_error": 0, "passed_and_error": 0, "passed_no_error": 14}, "computed_values_counted_as_fp": null, "note": "A8.8 쓰임 3의 계산을 자동 라벨(런 a)에 그대로 한 값이다. 사람 정답이 아니므로 '정밀도·재현율'이 아니라 검사기 차단 구간 중 라벨러 구간과 겹친 비율(precision 칸), 라벨러 number·quote 구간 중 검사기와 겹친 비율(recall_* 칸)로 읽는다."}
- 저장본: {"gate_miss_number_quote_errors": 0, "advisory_entity_precision": {"k": 0, "n": 3, "rate": 0.0, "wilson95": [0.0, 0.5615]}}

## 5. 런 a 대 런 b (자기 일관성 - A8.8 재라벨 자리)

- 둘 다 있는 것: 클러스터 11, 출력 10
- single_event 11/11 = 1.000 [0.741, 1.000], κ(참고) 1.0
- 이상치 집합 완전 일치 10/11 = 0.909 [0.623, 0.984], κ(참고) 0.7179, 불일치 r1-c29; 주된 사건 유무 11/11 = 1.000 [0.741, 1.000], κ 정의 안 됨
- publishable 8/10 = 0.800 [0.490, 0.943], κ(참고) 0.4118, 불일치 o1acd87ba0a, o432cfeb6b5; 라벨러 자신의 publishable 9/10 = 0.900 [0.596, 0.982], κ(참고) 0.6154, 불일치 o432cfeb6b5
- 사실 오류 유무 9/10 = 0.900 [0.596, 0.982], κ(참고) 0.6154, 불일치 o432cfeb6b5; 오류 수 같음 9/10 = 0.900 [0.596, 0.982], κ(참고) 0.6154, 불일치 o432cfeb6b5
- style 정확 8/10 = 0.800 [0.490, 0.943], κ(참고) 0.375, 불일치 o1e0fd1317e, o9ad5016cc8; ±1 10/10 = 1.000 [0.723, 1.000]
- 제목 불일치 9/10 = 0.900 [0.596, 0.982], κ(참고) 0.0, 불일치 o432cfeb6b5; tone_drift 10/10 = 1.000 [0.723, 1.000], κ 정의 안 됨
- 커버리지(같은 핵심 사실, 사실 단위) 51/53 = 0.962 [0.873, 0.990], κ(참고) 0.8767, 불일치 o1acd87ba0a#2, o432cfeb6b5#2
- κ는 정의되면 참고로만 적고 해석하지 않는다(A8.8 재라벨의 보고 규칙). 런 b의 coverage는 런 a의 핵심 사실로 물었다.

## 6. 라벨러 대 judge v2 shadow

- judge 판정 × publishable: 7/14 = 0.500 [0.268, 0.732], κ(참고) 0.1967, 불일치 o0d8d6d6ee0, o1acd87ba0a, o9ad5016cc8, oa50cd38cf2, ob65239c2cc, oc5801514de, oca51eacbc4, 표 {"judge_pass_pub_Y": 4, "judge_pass_pub_N": 0, "judge_fail_pub_Y": 7, "judge_fail_pub_N": 3}
- judge 근거 없는 주장 ≥1 × 라벨러 사실 오류 ≥1: 7/14 = 0.500 [0.268, 0.732], κ(참고) 0.1967, 불일치 o0d8d6d6ee0, o1acd87ba0a, o9ad5016cc8, oa50cd38cf2, ob65239c2cc, oc5801514de, oca51eacbc4, 표 {"judge_unsupported_ge1_err_Y": 3, "judge_unsupported_ge1_err_N": 7, "judge_unsupported_0_err_Y": 0, "judge_unsupported_0_err_N": 4}
- judge v2 shadow(첫 초안, 기본 임계값)와 자동 라벨러(런 a). 둘 다 Gemini - 같은 계열이고 어느 쪽도 정답이 아니다.

## 7. 크기 버킷별(런 a)

- 3-4: 클러스터 5(저장 4), single_event 4/5 = 0.800 [0.376, 0.964], 출력 4, publishable 3/4 = 0.750 [0.301, 0.954], 사실 오류 ≥1 1/4 = 0.250 [0.046, 0.699]
- 5-9: 클러스터 5(저장 5), single_event 3/5 = 0.600 [0.231, 0.882], 출력 5, publishable 4/5 = 0.800 [0.376, 0.964], 사실 오류 ≥1 1/5 = 0.200 [0.036, 0.625]
- 10+: 클러스터 5(저장 5), single_event 4/5 = 0.800 [0.376, 0.964], 출력 5, publishable 4/5 = 0.800 [0.376, 0.964], 사실 오류 ≥1 1/5 = 0.200 [0.036, 0.625]

## 8. 런 b 요약

- 클러스터 11, single_event 9/11 = 0.818 [0.523, 0.949]
- 출력 10, publishable 7/10 = 0.700 [0.397, 0.892], 사실 오류 ≥1 2/10 = 0.200 [0.057, 0.510]

## 9. 행(id·라벨 값만)

| item | 기사 | 버킷 | outcome | a single_event | a 이상치 | a 핵심 사실 | b single_event |
|---|---|---|---|---|---|---|---|
| r1-c111 | 6 | 5-9 | completed | True | 0 | 5 | True |
| r1-c22 | 19 | 10+ | completed | True | 0 | 6 | True |
| r1-c29 | 9 | 5-9 | completed | False | 2 | 5 | False |
| r1-c51 | 5 | 5-9 | completed | True | 0 | 5 | True |
| r1-c52 | 3 | 3-4 | skipped | False | 1 | 4 | False |
| r1-c70 | 3 | 3-4 | completed | True | 0 | 5 | True |
| r2-c14 | 7 | 5-9 | completed | True | 0 | 5 | True |
| r2-c19 | 30 | 10+ | completed | True | 0 | 6 | True |
| r2-c60 | 10 | 10+ | completed | True | 0 | 6 | True |
| r2-c70 | 4 | 3-4 | completed | True | 0 | 5 | True |
| r2-c8 | 41 | 10+ | completed | False | 5 | 6 | None |
| r3-c11 | 4 | 3-4 | completed | True | 0 | 5 | None |
| r3-c149 | 3 | 3-4 | completed | True | 0 | 5 | True |
| r3-c2 | 5 | 5-9 | completed | False | 1 | 5 | None |
| r3-c52 | 14 | 10+ | completed | True | 0 | 5 | None |

| output | item | a publishable | a 라벨러 Y | a 오류 | a style | a 커버 | b publishable | b 오류 | b style |
|---|---|---|---|---|---|---|---|---|---|
| o0d8d6d6ee0 | r1-c111 | True | True | 0 | 5 | 5/5 | True | 0 | 5 |
| o1200206d98 | r1-c70 | True | True | 0 | 5 | 4/5 | True | 0 | 5 |
| o1acd87ba0a | r2-c19 | True | True | 0 | 5 | 4/6 | False | 0 | 5 |
| o1e0fd1317e | r2-c14 | False | False | 1 | 5 | 5/5 | False | 1 | 4 |
| o208f0f1a4c | r2-c8 | False | False | 1 | 5 | 2/6 |  |  |  |
| o432cfeb6b5 | r2-c60 | True | True | 0 | 4 | 5/6 | False | 1 | 4 |
| o4c5eb0674a | r3-c11 | False | False | 1 | 4 | 5/5 |  |  |  |
| o9ad5016cc8 | r3-c149 | True | True | 0 | 4 | 3/5 | True | 0 | 5 |
| oa50cd38cf2 | r3-c2 | True | True | 0 | 5 | 3/5 |  |  |  |
| oa610149f6d | r1-c29 | True | True | 0 | 5 | 3/5 | True | 0 | 5 |
| ob65239c2cc | r2-c70 | True | True | 0 | 5 | 5/5 | True | 0 | 5 |
| oc5801514de | r1-c22 | True | True | 0 | 5 | 4/6 | True | 0 | 5 |
| oca51eacbc4 | r1-c51 | True | True | 0 | 5 | 5/5 | True | 0 | 5 |
| oe245be876f | r3-c52 | True | True | 0 | 4 | 5/5 |  |  |  |

## 계산하지 못한 것(측정하지 않음)

- 사람 발행 가능률(A8.8 쓰임 1, bake-off 1차 지표의 정의 그대로의 값) - 사람 라벨 없음(사용자 결정 2026-10-17)
- judge 보정 - 사람 라벨 대비 κ·Spearman(ADR 0009 judge 선정, ADR 0010) - 사람 라벨 없음
- 결정론 검사기의 정밀도·재현율(사람 정답 대비, ADR 0010 '측정하지 않은 것') - 사람 라벨 없음 - 라벨러 구간과의 겹침만 낸다
- 사람 라벨 신뢰도(intra-rater)와 가이드 5절의 κ < 0.60 규칙 - 사람 라벨 없음
- 자동 라벨러 자신의 정확도 - 정답이 없다 - 일치는 타당성을 보여 주지 않는다
- 라벨 시간 실측(A8.7 label_hours_actual) - 사람 라벨 없음

## 주의

- 라벨러(gemini-3-flash-preview)·generator(gemini-3.5-flash-lite)·judge v2(gemini-3.1-flash-lite)가 모두 Gemini다(같은 계열). 라벨러 대 judge 일치는 독립 검증이 아니다.
- 라벨러는 요청받은 '가장 강한 모델'이 아니다 - 비용 상한 $0.50 때문에 선정 규칙(A8.13 (b)1)이 고른 preview 모델이다.
- 기사당 앞 1500자만 보여 줬다(가이드는 전체 본문). 1500자 뒤에만 있는 사실을 초안이 맞게 썼다면 오류로 세므로 사실 오류율은 높게, 발행 가능률은 낮게 치우칠 수 있다.
- thinking은 최소(minimal), temperature 1.0(기본값). 런 b는 재라벨 대상 + 예산 안의 나머지라 전 항목이 아니다.
- n이 작다(클러스터 15, 출력 14). 비율의 Wilson 95% CI가 넓다. 가설 검정은 없다.
