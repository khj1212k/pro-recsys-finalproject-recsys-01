# E0 기준선 측정 리포트 (e0-baseline-v1)

기술 통계 기준선이다(ADR 0009 A8). 가설·결정 규칙은 없고, 짝지은 전/후 비교가 아니다. 모든 비율에 Wilson 95% CI와 분모를 적는다. 기사·뉴스레터 텍스트는 싣지 않는다(id·sha256·건수만).

## 머리말

| 항목 | 값 |
|---|---|
| command | "python -m evaluation.llm.e0_baseline run --export-dir data/exports/20261017T024007Z --pack-dir data/exports/20261017T024007Z/embeddings" |
| code_sha | "35d0a1df97a38dad089f0daea676650072f592d6" |
| head_sha | "8d4cd8df44b0d3bcef9cf2b760fdf0586aaeddba" |
| run | "run-20261017T054231Z" |
| run_at | "2026-10-17T05:42:31Z" |
| export_t0 | "2026-10-17T11:40:07.286148+09:00" |
| embeddings_sha256 | "7bb202ed6ce1aaef2a4e7dc5dedd35cefcef390a785a0db2c4b051ff551fb1b2" |
| export_manifest_sha256 | "0c4d9f89d5580aa291f016c2096b23c4a40403baec07a01768baae839a9444e7" |
| sample_manifest_sha256 | "ca5f0c4a49d13a2070059e6d98e23eb9c58f09b9706859ed2da1980b008187c2" |
| n_eval | 15 |
| n_warmup | 2 |
| n_processed | 15 |
| n_aborted_or_not_started | 0 |
| bucket_allocation | {"3-4": 5, "5-9": 5, "10+": 5} |
| seed | {"sample": 20261006, "order": 20261007} |
| cost_usd | 0.155614 |
| billing_fx | 1400.0 |
| cost_krw | 217.9 |
| colab_cu | 0.43 |
| label_hours_actual | null |
| resume_count | 0 |
| results_viewed | "등록 시점(A8): 미열람. 이 리포트를 커밋하는 것이 첫 열람이다" |
| stop | null |
| void | null |
| partial_run | false |
| wall_clock_reached | null |
| in_client_cap | {"LLM_BUDGET_DAY_USD": "2.00", "LLM_BUDGET_RUN_USD": "2.00", "LLM_BUDGET_TOTAL_USD": "unchanged", "LLM_RUN_ID": "e0-baseline-v1:run-20261017T054231Z"} |
| forbid_database | false |
| user_decisions | {"newsletter_embedder": "record_only_mac", "label_subset": "full"} |

처리 순서: r1-c96, r3-c17, r1-c51, r3-c11, r3-c149, r1-c22, r2-c14, r2-c19, r2-c8, r2-c70, r3-c2, r2-c60, r1-c70, r1-c29, r1-c111, r3-c52, r1-c52

날짜 창(UTC): [["2026-10-12T21:00:00", "2026-10-13T21:00:00"], ["2026-10-13T21:00:00", "2026-10-14T21:00:00"], ["2026-10-14T21:00:00", "2026-10-15T21:00:00"]]

## 1. 완주 분포

- outcome(eval): {"completed": 14, "skipped": 1}
- 완주율 completed / n_p: 14/15 = 0.933 [0.702, 0.988]
- 분모에서 뺀 것: {"aborted_budget": {"count": 0, "cost_usd": 0}, "aborted_infra": {"count": 0, "cost_usd": 0}, "interrupted": {"count": 0, "cost_usd": 0}, "not_started": {"count": 0, "cost_usd": 0}}

## 2. 첫 시도 스키마 통과율

- 전체: 72/72 = 1.000 [0.949, 1.000]
- cluster_eval: 16/16 = 1.000 [0.806, 1.000]
- newsletter_content_gen: 14/14 = 1.000 [0.785, 1.000]
- newsletter_meta_gen: 14/14 = 1.000 [0.785, 1.000]
- newsletter_eval: 14/14 = 1.000 [0.785, 1.000]
- tone_convert: 14/14 = 1.000 [0.785, 1.000]

## 3. 재시도

- complete()당 attempts: n=72, 중앙값 1.0, 최댓값 1.0
- complete()당 HTTP 시도: n=72, 중앙값 1.0, 최댓값 1.0
- 스키마 실패 합: 0
- HTTP 상태(n_p 클러스터의 시도): {"200": 72}
- HTTP 상태(이 run에서 보낸 시도 전체): {"200": 87}
- 429 비율(상태 429 / n_p 클러스터의 시도): 0/72 = 0.000 [0.000, 0.051]
- 429 비율(상태 429 / 이 run에서 보낸 시도 전체 - pre-flight·warmup·중단 포함, A8.3 #3): 0/87 = 0.000 [0.000, 0.042]
- 전송 실패: {"timeout": 0, "connection": 0, "5xx": 0, "other": 0}, deadline 도달 호출 0
- 클러스터당 생성 시도: n=15, 중앙값 1.0, 최댓값 1.0
- 클러스터 평가 재시도: n=15, 중앙값 0.0, 최댓값 1.0
- 문체 재변환(드리프트): n=14, 중앙값 0.0, 최댓값 0.0
- 문체 검증 재시도: n=14, 중앙값 0.0, 최댓값 0.0

## 4. 지연(초)

- cluster_eval: n=16, 중앙값 1.4145, 최댓값 1.668
- newsletter_content_gen: n=14, 중앙값 2.691, 최댓값 3.148
- newsletter_meta_gen: n=14, 중앙값 1.0475, 최댓값 1.45
- newsletter_eval: n=14, 중앙값 1.5505, 최댓값 2.442
- tone_convert: n=14, 중앙값 2.0995, 최댓값 2.896
- 클러스터 벽시계: n=15, 중앙값 9.271, 최댓값 11.582
- 저장본 벽시계: n=14, 중앙값 9.2775, 최댓값 11.582
- 단일 패스 근사(마지막 성공 시도 지연 합): n=14, 중앙값 9.0185, 최댓값 10.297
- n이 작아 p95는 적지 않는다(사실상 최댓값). 뉴스레터 임베딩 시간은 빠져 있다(이탈 6).

## 5. 토큰·비용

- 토큰(처리 완료 eval): {"prompt_tokens": 290881, "completion_tokens": 24137, "total_tokens": 315018, "hidden_tokens": 0, "reasoning_tokens_reported": 0}
- 토큰(전 단계): {"prompt_tokens": 333823, "completion_tokens": 28743, "total_tokens": 362566, "hidden_tokens": 0, "reasoning_tokens_reported": 0}
- 비용 USD: {"processed_eval": 0.133391, "all_phases_run": 0.155614, "preflight": 0.004789, "warmup": 0.017434, "e0_cumulative": 0.155614}
- 비용 근거: {"observed": 87}
- 클러스터당 비용: n=15, 중앙값 0.0077, 최댓값 0.0182, 저장본당: n=14, 중앙값 0.0081, 최댓값 0.0182
- 응답 모델 버전: gemini-3.1-flash-lite, gemini-3.5-flash-lite
- cost = prompt × 입력 단가 + (completion + hidden) × 출력 단가, hidden = max(0, total − prompt − completion)
- 문자/토큰(cluster_eval, 첫 시도): n=16, 중앙값 1.9599, 최댓값 2.3998
- 문자/토큰(newsletter_content_gen, 첫 시도): n=14, 중앙값 1.671, 최댓값 1.8313
- 문자/토큰(newsletter_meta_gen, 첫 시도): n=14, 중앙값 1.8324, 최댓값 1.8885
- 문자/토큰(newsletter_eval, 첫 시도): n=14, 중앙값 1.6571, 최댓값 1.8207
- 문자/토큰(tone_convert, 첫 시도): n=14, 중앙값 1.67, 최댓값 1.7416

## 6. 결정론 사실성 검사기

- 첫 초안 미지원 수치 ≥1: 0/14 = 0.000 [0.000, 0.215]
- 첫 초안 미지원 인용 ≥1: 0/14 = 0.000 [0.000, 0.215]
- 첫 초안 참고 개체명 ≥1: 3/14 = 0.214 [0.076, 0.476]
- 최종 초안 미지원 수치 ≥1: 0/14 = 0.000 [0.000, 0.215]
- 최종 초안 미지원 인용 ≥1: 0/14 = 0.000 [0.000, 0.215]
- 차단으로 인한 재생성: 0, 폴백 초안: 0
- 필드별: {"advisory.entities.content": 2, "advisory.entities.sentence": 1}

## 7. 문체 드리프트

- 차단 numbers: 0/14 = 0.000 [0.000, 0.215]
- 차단 dates: 0/14 = 0.000 [0.000, 0.215]
- 차단 entities_added: 0/14 = 0.000 [0.000, 0.215]
- 형식체 폴백 저장: 0/14 = 0.000 [0.000, 0.215]
- 로컬 폴백(_fallback_convert): 0/14 = 0.000 [0.000, 0.215]
- 저장 sentence 오프라인 드리프트(C4): 0/14 = 0.000 [0.000, 0.215]

## 8. judge v2 shadow

- 기준별 점수: {"faithfulness": {"4": 10, "5": 4}, "coverage": {"5": 14}, "coherence": {"5": 14}, "style": {"3": 1, "4": 2, "5": 11}}
- 기본 임계값 PASS: 4/14 = 0.286 [0.117, 0.546], judge_unavailable 0
- 근거 없는 주장 수: n=14, 중앙값 1.0, 최댓값 4.0, 위치 추적 성공(C17): 12/14 = 0.857 [0.601, 0.960]
- shadow judge는 generator와 같은 계열(Gemini)이다.

## 9. 클러스터 평가기

- 판정(parsed): {"FAIL": 2, "PASS": 14}, 판정(parsed=False): {}
- confidence(parsed): n=16, 중앙값 1.0, 최댓값 1.0
- 이상치 수: n=16, 중앙값 0.0, 최댓값 5.0
- sub_groups 수: n=16, 중앙값 1.0, 최댓값 2.0, 크기: [1, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 9, 10, 27, 36]
- 재시도: n=15, 중앙값 0.0, 최댓값 1.0, 두 번째 서브그룹 ≥3건 폐기(C15): 1

## 10. 연결 정합(C2)

- 저장본당 제외됐는데 연결된 기사 수: n=14, 중앙값 0.0, 최댓값 5.0
- 그런 저장본: 1/14 = 0.071 [0.013, 0.315]

## 11. 규칙 위반(shadow)

- first_draft (n=14)
  - 현행 banned_ge_1: 2/14 = 0.143 [0.040, 0.399]
  - 현행 content_chars_out: 11/14 = 0.786 [0.524, 0.924]
  - 현행 invalid_category_ge_1: 0/14 = 0.000 [0.000, 0.215]
  - 현행 keywords_lt_5: 0/14 = 0.000 [0.000, 0.215]
  - 현행 paragraphs_lt_4: 9/14 = 0.643 [0.388, 0.837]
  - 현행 title_gt_15: 3/14 = 0.214 [0.076, 0.476]
  - v2(참고) banned_first_word: 0/14 = 0.000 [0.000, 0.215]
  - v2(참고) content_chars_out: 9/14 = 0.643 [0.388, 0.837]
  - v2(참고) emoji_gt_3: 0/14 = 0.000 [0.000, 0.215]
  - v2(참고) emotive_ge_1: 2/14 = 0.143 [0.040, 0.399]
  - v2(참고) paragraphs_ne_3: 14/14 = 1.000 [0.785, 1.000]
  - v2(참고) sentence_chars_out: 3/14 = 0.214 [0.076, 0.476]
  - v2(참고) title_gt_20: 0/14 = 0.000 [0.000, 0.215]
- saved_formal_draft (n=14)
  - 현행 banned_ge_1: 2/14 = 0.143 [0.040, 0.399]
  - 현행 content_chars_out: 11/14 = 0.786 [0.524, 0.924]
  - 현행 invalid_category_ge_1: 0/14 = 0.000 [0.000, 0.215]
  - 현행 keywords_lt_5: 0/14 = 0.000 [0.000, 0.215]
  - 현행 paragraphs_lt_4: 9/14 = 0.643 [0.388, 0.837]
  - 현행 title_gt_15: 3/14 = 0.214 [0.076, 0.476]
  - v2(참고) banned_first_word: 0/14 = 0.000 [0.000, 0.215]
  - v2(참고) content_chars_out: 9/14 = 0.643 [0.388, 0.837]
  - v2(참고) emoji_gt_3: 0/14 = 0.000 [0.000, 0.215]
  - v2(참고) emotive_ge_1: 2/14 = 0.143 [0.040, 0.399]
  - v2(참고) paragraphs_ne_3: 14/14 = 1.000 [0.785, 1.000]
  - v2(참고) sentence_chars_out: 3/14 = 0.214 [0.076, 0.476]
  - v2(참고) title_gt_20: 0/14 = 0.000 [0.000, 0.215]

## 12. 입력 통계

- 클러스터당 기사: n=15, 중앙값 6.0, 최댓값 41.0
- 클러스터당 언론사: n=15, 중앙값 4.0, 최댓값 6.0
- 본문 길이: n=163, 중앙값 1192.0, 최댓값 10122.0
- 1,500자 절단: 56/163 = 0.344 [0.275, 0.419]
- 같은 언론사 제목 near-dup 쌍: n=15, 중앙값 0.0, 최댓값 0.0

## 13. 실패 분류

- 근본 원인(처리 완료 eval): {"cluster_eval_fail": 1, "none": 14}

| item | split | outcome | root_cause | 사건 |
|---|---|---|---|---|
| r1-c51 | eval | completed | none | {} |
| r3-c11 | eval | completed | none | {} |
| r3-c149 | eval | completed | none | {} |
| r1-c22 | eval | completed | none | {} |
| r2-c14 | eval | completed | none | {} |
| r2-c19 | eval | completed | none | {} |
| r2-c8 | eval | completed | none | {} |
| r2-c70 | eval | completed | none | {} |
| r3-c2 | eval | completed | none | {} |
| r2-c60 | eval | completed | none | {} |
| r1-c70 | eval | completed | none | {} |
| r1-c29 | eval | completed | none | {} |
| r1-c111 | eval | completed | none | {} |
| r3-c52 | eval | completed | none | {} |
| r1-c52 | eval | skipped | cluster_eval_fail | {} |
| r1-c96 | warmup | completed | none | {} |
| r3-c17 | warmup | completed | none | {} |

## 잔액 대조

- 상태: 미기록
- 원장 합 USD: 0.155614
- 확인하지 못한 것: 잔액 표시 단위, 반영 지연, VAT 포함 여부

## pre-flight

- cluster_eval: parsed=True, error=None, 지연 2.224s, 토큰 604/131/hidden 0
- newsletter_content_gen: parsed=True, error=None, 지연 2.449s, 토큰 1209/365/hidden 0
- newsletter_meta_gen: parsed=True, error=None, 지연 1.0s, 토큰 1261/117/hidden 0
- newsletter_eval: parsed=True, error=None, 지연 2.479s, 토큰 1244/411/hidden 0
- tone_convert: parsed=True, error=None, 지연 2.088s, 토큰 1059/500/hidden 0

## 사람 라벨

- {"status": "none", "reason": "사용자 결정(2026-10-17): 사람 라벨을 달지 않는다 - 자동 라벨로 대체(ADR 0009 A8.13)"}

## 자동 라벨(ADR 0009 A8.13)

- 정의·주의·행별 값은 [reports/llm/e0_auto_labels_v1.md](e0_auto_labels_v1.md). 라벨러 gemini-3-flash-preview (gemini-3-flash-preview), 비용 $0.328228. 자동 라벨러(LLM)의 판단이고 정확도는 재지 않았다. 라벨러·generator·judge v2가 모두 Gemini다.
- 발행 가능률(자동 라벨, 주 지표 저장본 Y / n_p): 11/15 = 0.733 [0.480, 0.891]; 첫 초안만 11/14 = 0.786 [0.524, 0.924]
- 사실 오류 ≥1(출력): 3/14 = 0.214 [0.076, 0.476]; 핵심 사실 커버리지(사실 단위): 58/74 = 0.784 [0.677, 0.862]; single_event: 11/15 = 0.733 [0.480, 0.891]
- 런 a 대 b 일치: publishable 8/10 = 0.800 [0.490, 0.943], single_event 11/11 = 1.000 [0.741, 1.000] (κ는 참고로만)
- 라벨러 대 judge v2 shadow(첫 초안): 7/14 = 0.500 [0.268, 0.732], 표 {"judge_pass_pub_Y": 4, "judge_pass_pub_N": 0, "judge_fail_pub_Y": 7, "judge_fail_pub_N": 3}

## 선언된 이탈(A8.2)

- {"id": 1, "store": "harness_file_stand_ins", "patched": ["NewsClusterer._load_data_from_db", "workflow.nodes.get_connection", "workflow.nodes.release_connection", "workflow.nodes.save_news_letter"], "production_code_changed": false}
- {"id": 2, "cluster_selection": "stratified_sample_in_registered_order"}
- {"id": 3, "bypass": "Stage5.execute"}
- {"id": 4, "graph_run": "app.stream"}
- {"id": 5, "article_embedding": "colab_t4_fp16"}
- {"id": 6, "newsletter_embedder": "record_only"}
- {"id": 7, "window": ["upper_bound_by_runner", "extracted_after_t_k_rows_reported", "no_cross_run_newsletter_exclusion"]}
- {"id": 8, "instrumentation": ["complete_wrapper", "httpx_request_response_hooks", "tone_local_fallback_flag"], "unchanged": ["prompts", "temperature", "max_tokens", "retry_logic", "timeouts"]}
