# 지표 타당성 격자 v1

> [SIM] 시스템 반응 지표만, 정확도 무주장 (ADR 0019)

- 실행: 60건, 시드 [0, 1, 2], 커밋 `cea8e90`
- 판정 기준: docs/adr/0019 "사전 등록" 절 (P: 시드별 전부, H: 3시드 평균, X: 보고만)

## 사전 등록 판정

| ID | 카탈로그 | 프리셋 | 주장 | 판정 | 값 |
|---|---|---|---|---|---|
| P1 | synthetic | category_only | first_view_coverage: static_batch = 0, 나머지 = 1 | 통과 | {static_batch: [0, 0, 0], static_batch_fallback: [1, 1, 1], reactive: [1, 1, 1], reactive_explore: [1, 1, 1], random: [1, 1, 1]} |
| P2 | synthetic | category_only | 빈 응답·폴백률이 정책 설계와 일치 | 통과 | {static_batch: {empty_rate: [0.01533, 0.01261, 0.01332], fallback_rate: [0.01533, 0.01261, 0.01332]}, static_batch_fallback: {empty_rate: [0, 0, 0], fallback_rate: [0.01777, 0.01552, 0.01565]}, reactive: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, reactive_explore: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, random: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}} |
| P3 | synthetic | category_only | 모든 실행에서 error_rate = 0, click_ack_rate = 1 | 통과 | {static_batch: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, static_batch_fallback: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive_explore: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, random: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}} |
| P4 | synthetic | category_only | static_batch: 클릭 직후 Jaccard = 1, similar_share_lift = 0 | 통과 | {jaccard: [1, 1, 1], lift: [0, 0, 0]} |
| P5 | synthetic | category_only | reactive: 클릭 직후 Jaccard < 1, similar_share_lift > 0 | 통과 | {jaccard: [0.6896, 0.701, 0.6834], lift: [0.03565, 0.03349, 0.04518]} |
| H1 | synthetic | category_only | 첫 응답 온보딩 카테고리 비율: reactive > static_batch_fallback, reactive > random | 통과 | {reactive: 1, static_batch_fallback: 0.3422, random: 0.3478} |
| H2 | synthetic | category_only | 클릭 직후 Jaccard: random < reactive_explore < reactive | 통과 | {random: 0.04666, reactive_explore: 0.3812, reactive: 0.6913} |
| H3 | synthetic | category_only | 클릭 후 유사 아이템 비율: reactive > reactive_explore | 통과 | {reactive: 0.9322, reactive_explore: 0.783} |
| H4 | synthetic | category_only | similar_share_lift: reactive > random | 통과 | {reactive: 0.03811, random: -0.002351} |
| H5 | synthetic | category_only | drift: reactive adapted_rate ≥ static_batch, requests_to_adapt_median ≤ static_batch | 통과 | {adapted_rate: {reactive: 0.1238, static_batch: 0.1019}, requests_to_adapt_median: {reactive: 2.667, static_batch: 2.667}} |
| H6 | synthetic | category_only | ctr_top_k: reactive > random (평균과 시드별) | 통과 | {reactive: [0.02831, 0.02967, 0.02898], random: [0.0143, 0.01516, 0.01455]} |
| H7 | synthetic | category_only | random의 ctr_top_k가 [1%, 3%] 안 | 통과 | {random: 0.01467} |
| X1 | synthetic | category_only | random의 drift 지표 = 임계값 지표의 우연 수준 | - | {random: {adapted_rate: 0.7533, requests_to_adapt_median: 3.667}, static_batch: {adapted_rate: 0.1019, requests_to_adapt_median: 2.667}, reactive: {adapted_rate: 0.1238, requests_to_adapt_median: 2.667}} |
| X2 | synthetic | category_only | static_batch 대비 reactive의 ctr_top_k | - | {static_batch: 0.02092, reactive: 0.02898} |
| P1 | synthetic | default | first_view_coverage: static_batch = 0, 나머지 = 1 | 통과 | {static_batch: [0, 0, 0], static_batch_fallback: [1, 1, 1], reactive: [1, 1, 1], reactive_explore: [1, 1, 1], random: [1, 1, 1]} |
| P2 | synthetic | default | 빈 응답·폴백률이 정책 설계와 일치 | 통과 | {static_batch: {empty_rate: [0.01417, 0.01163, 0.01224], fallback_rate: [0.01417, 0.01163, 0.01224]}, static_batch_fallback: {empty_rate: [0, 0, 0], fallback_rate: [0.01687, 0.01532, 0.01409]}, reactive: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, reactive_explore: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, random: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}} |
| P3 | synthetic | default | 모든 실행에서 error_rate = 0, click_ack_rate = 1 | 통과 | {static_batch: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, static_batch_fallback: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive_explore: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, random: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}} |
| P4 | synthetic | default | static_batch: 클릭 직후 Jaccard = 1, similar_share_lift = 0 | 통과 | {jaccard: [1, 1, 1], lift: [0, 0, 0]} |
| P5 | synthetic | default | reactive: 클릭 직후 Jaccard < 1, similar_share_lift > 0 | 통과 | {jaccard: [0.7062, 0.7112, 0.694], lift: [0.02305, 0.02175, 0.03292]} |
| H1 | synthetic | default | 첫 응답 온보딩 카테고리 비율: reactive > static_batch_fallback, reactive > random | 통과 | {reactive: 1, static_batch_fallback: 0.3389, random: 0.3489} |
| H2 | synthetic | default | 클릭 직후 Jaccard: random < reactive_explore < reactive | 통과 | {random: 0.04729, reactive_explore: 0.3854, reactive: 0.7038} |
| H3 | synthetic | default | 클릭 후 유사 아이템 비율: reactive > reactive_explore | 통과 | {reactive: 0.9118, reactive_explore: 0.7891} |
| H4 | synthetic | default | similar_share_lift: reactive > random | 통과 | {reactive: 0.0259, random: 0.00359} |
| H5 | synthetic | default | drift: reactive adapted_rate ≥ static_batch, requests_to_adapt_median ≤ static_batch | **위반** | {adapted_rate: {reactive: 0.123, static_batch: 0.1352}, requests_to_adapt_median: {reactive: 3.833, static_batch: 3.833}} |
| H6 | synthetic | default | ctr_top_k: reactive > random (평균과 시드별) | 통과 | {reactive: [0.03757, 0.03812, 0.03802], random: [0.01401, 0.01535, 0.01478]} |
| H7 | synthetic | default | random의 ctr_top_k가 [1%, 3%] 안 | 통과 | {random: 0.01471} |
| X1 | synthetic | default | random의 drift 지표 = 임계값 지표의 우연 수준 | - | {random: {adapted_rate: 0.8207, requests_to_adapt_median: 4}, static_batch: {adapted_rate: 0.1352, requests_to_adapt_median: 3.833}, reactive: {adapted_rate: 0.123, requests_to_adapt_median: 3.833}} |
| X2 | synthetic | default | static_batch 대비 reactive의 ctr_top_k | - | {static_batch: 0.03077, reactive: 0.0379} |
| P1 | team_archive | category_only | first_view_coverage: static_batch = 0, 나머지 = 1 | 통과 | {static_batch: [0, 0, 0], static_batch_fallback: [1, 1, 1], reactive: [1, 1, 1], reactive_explore: [1, 1, 1], random: [1, 1, 1]} |
| P2 | team_archive | category_only | 빈 응답·폴백률이 정책 설계와 일치 | 통과 | {static_batch: {empty_rate: [0.01598, 0.01304, 0.01379], fallback_rate: [0.01598, 0.01304, 0.01379]}, static_batch_fallback: {empty_rate: [0, 0, 0], fallback_rate: [0.01812, 0.01548, 0.01634]}, reactive: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, reactive_explore: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, random: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}} |
| P3 | team_archive | category_only | 모든 실행에서 error_rate = 0, click_ack_rate = 1 | 통과 | {static_batch: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, static_batch_fallback: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive_explore: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, random: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}} |
| P4 | team_archive | category_only | static_batch: 클릭 직후 Jaccard = 1, similar_share_lift = 0 | 통과 | {jaccard: [1, 1, 1], lift: [0, 0, 0]} |
| P5 | team_archive | category_only | reactive: 클릭 직후 Jaccard < 1, similar_share_lift > 0 | 통과 | {jaccard: [0.6965, 0.6994, 0.6876], lift: [0.06956, 0.07059, 0.07765]} |
| H1 | team_archive | category_only | 첫 응답 온보딩 카테고리 비율: reactive > static_batch_fallback, reactive > random | 통과 | {reactive: 0.9833, static_batch_fallback: 0.3544, random: 0.3378} |
| H2 | team_archive | category_only | 클릭 직후 Jaccard: random < reactive_explore < reactive | 통과 | {random: 0.09055, reactive_explore: 0.3865, reactive: 0.6945} |
| H3 | team_archive | category_only | 클릭 후 유사 아이템 비율: reactive > reactive_explore | 통과 | {reactive: 0.6343, reactive_explore: 0.511} |
| H4 | team_archive | category_only | similar_share_lift: reactive > random | 통과 | {reactive: 0.0726, random: 0.002947} |
| H5 | team_archive | category_only | drift: reactive adapted_rate ≥ static_batch, requests_to_adapt_median ≤ static_batch | 통과 | {adapted_rate: {reactive: 0.3042, static_batch: 0.1808}, requests_to_adapt_median: {reactive: 2.667, static_batch: 2.667}} |
| H6 | team_archive | category_only | ctr_top_k: reactive > random (평균과 시드별) | 통과 | {reactive: [0.022, 0.02274, 0.02311], random: [0.01196, 0.01257, 0.01169]} |
| H7 | team_archive | category_only | random의 ctr_top_k가 [1%, 3%] 안 | 통과 | {random: 0.01208} |
| X1 | team_archive | category_only | random의 drift 지표 = 임계값 지표의 우연 수준 | - | {random: {adapted_rate: 0.6628, requests_to_adapt_median: 3.333}, static_batch: {adapted_rate: 0.1808, requests_to_adapt_median: 2.667}, reactive: {adapted_rate: 0.3042, requests_to_adapt_median: 2.667}} |
| X2 | team_archive | category_only | static_batch 대비 reactive의 ctr_top_k | - | {static_batch: 0.01722, reactive: 0.02262} |
| P1 | team_archive | default | first_view_coverage: static_batch = 0, 나머지 = 1 | 통과 | {static_batch: [0, 0, 0], static_batch_fallback: [1, 1, 1], reactive: [1, 1, 1], reactive_explore: [1, 1, 1], random: [1, 1, 1]} |
| P2 | team_archive | default | 빈 응답·폴백률이 정책 설계와 일치 | 통과 | {static_batch: {empty_rate: [0.01597, 0.01303, 0.01373], fallback_rate: [0.01597, 0.01303, 0.01373]}, static_batch_fallback: {empty_rate: [0, 0, 0], fallback_rate: [0.01813, 0.01602, 0.01611]}, reactive: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, reactive_explore: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}, random: {empty_rate: [0, 0, 0], fallback_rate: [0, 0, 0]}} |
| P3 | team_archive | default | 모든 실행에서 error_rate = 0, click_ack_rate = 1 | 통과 | {static_batch: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, static_batch_fallback: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, reactive_explore: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}, random: {error_rate: [0, 0, 0], click_ack_rate: [1, 1, 1]}} |
| P4 | team_archive | default | static_batch: 클릭 직후 Jaccard = 1, similar_share_lift = 0 | 통과 | {jaccard: [1, 1, 1], lift: [0, 0, 0]} |
| P5 | team_archive | default | reactive: 클릭 직후 Jaccard < 1, similar_share_lift > 0 | 통과 | {jaccard: [0.6976, 0.701, 0.6913], lift: [0.06968, 0.06819, 0.07261]} |
| H1 | team_archive | default | 첫 응답 온보딩 카테고리 비율: reactive > static_batch_fallback, reactive > random | 통과 | {reactive: 0.9833, static_batch_fallback: 0.3411, random: 0.3578} |
| H2 | team_archive | default | 클릭 직후 Jaccard: random < reactive_explore < reactive | 통과 | {random: 0.09068, reactive_explore: 0.389, reactive: 0.6967} |
| H3 | team_archive | default | 클릭 후 유사 아이템 비율: reactive > reactive_explore | 통과 | {reactive: 0.6289, reactive_explore: 0.5104} |
| H4 | team_archive | default | similar_share_lift: reactive > random | 통과 | {reactive: 0.07016, random: 0.003021} |
| H5 | team_archive | default | drift: reactive adapted_rate ≥ static_batch, requests_to_adapt_median ≤ static_batch | **위반** | {adapted_rate: {reactive: 0.3042, static_batch: 0.1805}, requests_to_adapt_median: {reactive: 2.333, static_batch: 1.667}} |
| H6 | team_archive | default | ctr_top_k: reactive > random (평균과 시드별) | 통과 | {reactive: [0.0225, 0.02364, 0.02371], random: [0.01212, 0.01197, 0.0127]} |
| H7 | team_archive | default | random의 ctr_top_k가 [1%, 3%] 안 | 통과 | {random: 0.01226} |
| X1 | team_archive | default | random의 drift 지표 = 임계값 지표의 우연 수준 | - | {random: {adapted_rate: 0.7184, requests_to_adapt_median: 3.167}, static_batch: {adapted_rate: 0.1805, requests_to_adapt_median: 1.667}, reactive: {adapted_rate: 0.3042, requests_to_adapt_median: 2.333}} |
| X2 | team_archive | default | static_batch 대비 reactive의 ctr_top_k | - | {static_batch: 0.01742, reactive: 0.02328} |
| H8 | synthetic | default vs category_only | ctr(reactive)/ctr(random): default > category_only | 통과 | {default: 2.576, category_only: 1.975} |
| H8 | team_archive | default vs category_only | ctr(reactive)/ctr(random): default > category_only | 통과 | {default: 1.899, category_only: 1.873} |

## 정책별 핵심 지표 (3시드 평균 [최소, 최대])

| 카탈로그/프리셋/정책 | first_view_coverage | first_view_onboarding_category_share | after_click_jaccard_mean | similar_share_after_click | similar_share_lift | adapted_rate | requests_to_adapt_median | empty_rate | fallback_rate | ctr_top_k |
|---|---|---|---|---|---|---|---|---|---|---|
| synthetic/category_only/random | 1 [1, 1] | 0.348 [0.337, 0.353] | 0.0467 [0.045, 0.0481] | 0.507 [0.489, 0.532] | -0.00235 [-0.00891, 0.00262] | 0.753 [0.733, 0.793] | 3.67 [3, 4] | 0 [0, 0] | 0 [0, 0] | 0.0147 [0.0143, 0.0152] |
| synthetic/category_only/reactive | 1 [1, 1] | 1 [1, 1] | 0.691 [0.683, 0.701] | 0.932 [0.925, 0.936] | 0.0381 [0.0335, 0.0452] | 0.124 [0.1, 0.138] | 2.67 [1, 5] | 0 [0, 0] | 0 [0, 0] | 0.029 [0.0283, 0.0297] |
| synthetic/category_only/reactive_explore | 1 [1, 1] | 0.72 [0.703, 0.737] | 0.381 [0.378, 0.384] | 0.783 [0.774, 0.794] | 0.0442 [0.0408, 0.0467] | 0.179 [0.138, 0.233] | 3.17 [1.5, 5] | 0 [0, 0] | 0 [0, 0] | 0.0266 [0.0262, 0.0269] |
| synthetic/category_only/static_batch | 0 [0, 0] | - | 1 [1, 1] | 0.898 [0.889, 0.906] | 0 [0, 0] | 0.102 [0.0667, 0.172] | 2.67 [1, 4] | 0.0138 [0.0126, 0.0153] | 0.0138 [0.0126, 0.0153] | 0.0209 [0.0203, 0.0215] |
| synthetic/category_only/static_batch_fallback | 1 [1, 1] | 0.342 [0.313, 0.37] | 1 [1, 1] | 0.896 [0.886, 0.904] | -1.07e-05 [-3.2e-05, 1.52e-05] | 0.102 [0.0667, 0.172] | 2.67 [1, 4] | 0 [0, 0] | 0.0163 [0.0155, 0.0178] | 0.0208 [0.0201, 0.0211] |
| synthetic/default/random | 1 [1, 1] | 0.349 [0.31, 0.4] | 0.0473 [0.0466, 0.0486] | 0.508 [0.49, 0.541] | 0.00359 [-0.00241, 0.0111] | 0.821 [0.767, 0.862] | 4 [3, 5] | 0 [0, 0] | 0 [0, 0] | 0.0147 [0.014, 0.0154] |
| synthetic/default/reactive | 1 [1, 1] | 1 [1, 1] | 0.704 [0.694, 0.711] | 0.912 [0.9, 0.922] | 0.0259 [0.0217, 0.0329] | 0.123 [0.069, 0.167] | 3.83 [1, 6.5] | 0 [0, 0] | 0 [0, 0] | 0.0379 [0.0376, 0.0381] |
| synthetic/default/reactive_explore | 1 [1, 1] | 0.722 [0.72, 0.723] | 0.385 [0.382, 0.391] | 0.789 [0.777, 0.804] | 0.0352 [0.0323, 0.0401] | 0.236 [0.2, 0.276] | 7 [6, 8] | 0 [0, 0] | 0 [0, 0] | 0.0349 [0.0341, 0.0357] |
| synthetic/default/static_batch | 0 [0, 0] | - | 1 [1, 1] | 0.915 [0.9, 0.923] | 0 [0, 0] | 0.135 [0.0667, 0.172] | 3.83 [1, 7] | 0.0127 [0.0116, 0.0142] | 0.0127 [0.0116, 0.0142] | 0.0308 [0.0302, 0.0318] |
| synthetic/default/static_batch_fallback | 1 [1, 1] | 0.339 [0.327, 0.347] | 1 [1, 1] | 0.914 [0.899, 0.924] | 3.7e-05 [-6.92e-06, 8.09e-05] | 0.135 [0.0667, 0.172] | 3.83 [1, 7] | 0 [0, 0] | 0.0154 [0.0141, 0.0169] | 0.0306 [0.0301, 0.0316] |
| team_archive/category_only/random | 1 [1, 1] | 0.338 [0.31, 0.367] | 0.0905 [0.0903, 0.0909] | 0.165 [0.159, 0.168] | 0.00295 [-0.00234, 0.00843] | 0.663 [0.567, 0.767] | 3.33 [3, 4] | 0 [0, 0] | 0 [0, 0] | 0.0121 [0.0117, 0.0126] |
| team_archive/category_only/reactive | 1 [1, 1] | 0.983 [0.97, 1] | 0.695 [0.688, 0.699] | 0.634 [0.614, 0.664] | 0.0726 [0.0696, 0.0777] | 0.304 [0.233, 0.379] | 2.67 [2, 3] | 0 [0, 0] | 0 [0, 0] | 0.0226 [0.022, 0.0231] |
| team_archive/category_only/reactive_explore | 1 [1, 1] | 0.714 [0.71, 0.717] | 0.387 [0.383, 0.393] | 0.511 [0.502, 0.522] | 0.0935 [0.0872, 0.0974] | 0.28 [0.241, 0.333] | 5.83 [1, 12] | 0 [0, 0] | 0 [0, 0] | 0.0215 [0.0214, 0.0215] |
| team_archive/category_only/static_batch | 0 [0, 0] | - | 1 [1, 1] | 0.61 [0.596, 0.622] | 0 [0, 0] | 0.181 [0.133, 0.276] | 2.67 [1, 4] | 0.0143 [0.013, 0.016] | 0.0143 [0.013, 0.016] | 0.0172 [0.0167, 0.0175] |
| team_archive/category_only/static_batch_fallback | 1 [1, 1] | 0.354 [0.327, 0.383] | 1 [1, 1] | 0.606 [0.591, 0.619] | 6.98e-06 [-2.62e-05, 2.87e-05] | 0.181 [0.133, 0.276] | 2.67 [1, 4] | 0 [0, 0] | 0.0166 [0.0155, 0.0181] | 0.0171 [0.0167, 0.0174] |
| team_archive/default/random | 1 [1, 1] | 0.358 [0.323, 0.38] | 0.0907 [0.0887, 0.0921] | 0.163 [0.159, 0.169] | 0.00302 [-0.000372, 0.00717] | 0.718 [0.633, 0.867] | 3.17 [2, 4.5] | 0 [0, 0] | 0 [0, 0] | 0.0123 [0.012, 0.0127] |
| team_archive/default/reactive | 1 [1, 1] | 0.983 [0.97, 1] | 0.697 [0.691, 0.701] | 0.629 [0.621, 0.645] | 0.0702 [0.0682, 0.0726] | 0.304 [0.267, 0.379] | 2.33 [2, 3] | 0 [0, 0] | 0 [0, 0] | 0.0233 [0.0225, 0.0237] |
| team_archive/default/reactive_explore | 1 [1, 1] | 0.713 [0.703, 0.73] | 0.389 [0.385, 0.397] | 0.51 [0.502, 0.523] | 0.0892 [0.082, 0.0936] | 0.247 [0.207, 0.3] | 5.17 [1.5, 11] | 0 [0, 0] | 0 [0, 0] | 0.0216 [0.0215, 0.0217] |
| team_archive/default/static_batch | 0 [0, 0] | - | 1 [1, 1] | 0.607 [0.595, 0.623] | 0 [0, 0] | 0.18 [0.133, 0.241] | 1.67 [1, 3] | 0.0142 [0.013, 0.016] | 0.0142 [0.013, 0.016] | 0.0174 [0.0169, 0.0179] |
| team_archive/default/static_batch_fallback | 1 [1, 1] | 0.341 [0.323, 0.373] | 1 [1, 1] | 0.604 [0.589, 0.625] | -4.78e-05 [-0.000118, 9.6e-06] | 0.18 [0.133, 0.241] | 1.67 [1, 3] | 0 [0, 0] | 0.0168 [0.016, 0.0181] | 0.0174 [0.0168, 0.0178] |
