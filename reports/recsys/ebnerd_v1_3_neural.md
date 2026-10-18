# EB-NeRD v1.3 — 신경망 사용자 모델 vs LightGBM (E15) 결과 표

> 증거 등급: **preregistered-run** — 등록한 인자·입력으로 돈 실행이다. 라벨 [EB-NeRD](덴마크어 공개 벤치마크). 한국어 서비스의 성능이 아니다.
> 사전 등록: `ebnerd-neural-e15` (ADR 0013 A3 사전 등록, yaml sha256 `d42622ea1ee4…`, 커밋 `82fcd03b837a20c2070b6c6164c9b8d6852ea5f1`). 코드 SHA `381f61db90ff50e4cf2eb8a2d60d07f876042adc`.
> 값은 nDCG@10 평균 [95% CI](유저 단위 클러스터 부트스트랩), 차이는 같은 노출의 쌍체 차이다. 판정은 `neural_verdict`가 낸다. 신경망 arm이 받는 정보는 "A의 정보 + 원 벡터"다(같은 정보가 아니다).

## 판정

판정용 실행이다.

| 과제 | 비교 | Δ [95% CI] | 결합 하한 | seed 규칙 | 실행 유효성 | 게이트 | 결과 |
|---|---|---|---|---|---|---|---|
| p1 | sel(nrms) − A_star | -0.0215 [-0.0224, -0.0206] | -0.0297 | 실패 | 통과 | cold:pop0=실패, cold:k0=실패, cold:k1=실패, cold:k3=실패, cold:k5=실패, info_control=실패 | seed 규칙 불통과 |
| p1 | D(D) − A_star | -0.0007 [-0.0012, -0.0002] | -0.0028 | 실패 | 통과 | cold:pop0=실패, cold:k0=통과, cold:k1=통과, cold:k3=통과, cold:k5=통과, info_control=통과 | seed 규칙 불통과 |
| p2 | sel(nrms) − A_star | -0.0531 [-0.0562, -0.0498] | -0.0855 | 실패 | 통과 | cold:pop0=실패, cold:k0=실패, cold:k1=실패, cold:k3=실패, cold:k5=실패, info_control=실패 | seed 규칙 불통과 |
| p2 | D(D) − A_star | -0.0046 [-0.0061, -0.0032] | -0.0081 | 실패 | 통과 | cold:pop0=실패, cold:k0=보류(검정력 부족), cold:k1=실패, cold:k3=보류(검정력 부족), cold:k5=보류(검정력 부족), info_control=실패 | seed 규칙 불통과 |

- 주장(claim): `none`. 서빙 비용 게이트: `deferred` — shadow 자격을 얻은 arm: 없음.
- 선택된 family: {'p1': 'nrms', 'p2': 'nrms'}. Holm(4): {'p1:sel': False, 'p1:D': False, 'p2:sel': False, 'p2:D': False}.
- 미측정: 없음.

## 서술용 비교(같은 seed 규칙, 판정 아님)

| 비교 | seed 규칙 | Δ | 결합 하한 | 기록 |
|---|---|---|---|---|
| p1:A_star-vs-A | 실패 | +0.0005 | -0.0007 | - |
| p1:A_plus-vs-A_star | 실패 | +0.0030 | +0.0021 | - |
| p1:sel-vs-A_plus | 실패 | -0.0245 | -0.0319 | - |
| p2:A_star-vs-A | 실패 | -0.0002 | -0.0021 | - |
| p2:A_plus-vs-A_star | 실패 | +0.0032 | +0.0006 | - |
| p2:sel-vs-A_plus | 실패 | -0.0563 | -0.0902 | - |

## p1 — 판정 표본

| arm | nDCG@10 [95% CI] | seed 수 | − A* [95% CI] | − A [95% CI] |
|---|---|---|---|---|
| A | 0.6638 [0.6621, 0.6655] | 3 | -0.0005 [-0.0007, -0.0002] | - |
| A_plus | 0.6673 [0.6656, 0.6689] | 3 | +0.0030 [+0.0027, +0.0034] | +0.0035 [+0.0031, +0.0039] |
| A_star | 0.6643 [0.6625, 0.6660] | 3 | - | +0.0005 [+0.0002, +0.0007] |
| A_star_b234 | 0.6631 [0.6614, 0.6647] | 3 | -0.0012 [-0.0016, -0.0008] | -0.0007 [-0.0011, -0.0003] |
| A_star_noaug | 0.6638 [0.6621, 0.6655] | 3 | -0.0005 [-0.0007, -0.0003] | -0.0000 [-0.0000, +0.0000] |
| D | 0.6635 [0.6618, 0.6652] | 3 | -0.0007 [-0.0012, -0.0002] | -0.0003 [-0.0008, +0.0002] |
| nrms | 0.6428 [0.6411, 0.6446] | 3 | -0.0215 [-0.0224, -0.0206] | -0.0210 [-0.0219, -0.0201] |
| sasrec | 0.6410 [0.6392, 0.6428] | 3 | -0.0233 [-0.0241, -0.0224] | -0.0228 [-0.0237, -0.0219] |
| sel_no_scalars | 0.5101 [0.5085, 0.5117] | 3 | -0.1542 [-0.1558, -0.1526] | -0.1537 [-0.1553, -0.1521] |

### p1 — 콜드 조건(같은 조건의 A* 대비 Δ [양측 90% CI])

| 조건 | A_plus | D | nrms |
|---|---|---|---|
| pop0 | -0.0150 [-0.0156, -0.0144] | -0.0304 [-0.0311, -0.0298] | -0.0352 [-0.0369, -0.0335] |
| k0 | -0.0007 [-0.0009, -0.0004] | -0.0031 [-0.0035, -0.0027] | -0.0163 [-0.0170, -0.0156] |
| k1 | -0.0009 [-0.0011, -0.0006] | -0.0031 [-0.0035, -0.0026] | -0.0253 [-0.0260, -0.0245] |
| k3 | +0.0003 [+0.0000, +0.0005] | -0.0015 [-0.0019, -0.0011] | -0.0217 [-0.0224, -0.0210] |
| k5 | +0.0005 [+0.0002, +0.0007] | -0.0011 [-0.0015, -0.0007] | -0.0204 [-0.0212, -0.0197] |

### p1 — 선택(test 미사용)

```json
{
 "family": "nrms",
 "table": {
  "nrms": {
   "best_trial": 10,
   "selection_metric": 0.6647100448608398,
   "config": {
    "d": 256,
    "heads": 4,
    "dropout": 0.3,
    "lr": 0.0007552781805685635,
    "weight_decay": 0.0,
    "batch": 256,
    "n_hist": 50
   },
   "best_epoch_in_tuning": 3
  },
  "sasrec": {
   "best_trial": 2,
   "selection_metric": 0.6611893773078918,
   "config": {
    "d": 128,
    "heads": 4,
    "dropout": 0.3,
    "lr": 0.0006879643560022023,
    "weight_decay": 0.0,
    "batch": 512,
    "n_hist": 20,
    "layers": 2,
    "aux_lambda": 0.5
   },
   "best_epoch_in_tuning": 2
  }
 },
 "rule": "각 family의 튠 best(seed 0)의 선택 표본 지표가 큰 쪽. 동률이면 neural.families의 앞쪽",
 "sample": "es in-view 노출 전체, seen 제외판",
 "sample_sha256": "83801daca02394a8dbb0a59539e3fb2d69329f4f959ac82640ca068a18181f71",
 "sample_requests": 32559,
 "uses_test": false
}
```

## p2 — 판정 표본

| arm | nDCG@10 [95% CI] | seed 수 | − A* [95% CI] | − A [95% CI] |
|---|---|---|---|---|
| A | 0.2682 [0.2635, 0.2726] | 3 | +0.0002 [-0.0008, +0.0012] | - |
| A_plus | 0.2713 [0.2668, 0.2755] | 3 | +0.0032 [+0.0018, +0.0047] | +0.0031 [+0.0015, +0.0044] |
| A_star | 0.2681 [0.2634, 0.2722] | 3 | - | -0.0002 [-0.0012, +0.0008] |
| A_star_b234 | 0.2624 [0.2579, 0.2667] | 3 | -0.0057 [-0.0069, -0.0044] | -0.0058 [-0.0072, -0.0045] |
| A_star_noaug | 0.2681 [0.2636, 0.2720] | 3 | -0.0000 [-0.0009, +0.0010] | -0.0002 [-0.0012, +0.0008] |
| D | 0.2635 [0.2588, 0.2676] | 3 | -0.0046 [-0.0061, -0.0032] | -0.0048 [-0.0062, -0.0034] |
| nrms | 0.2150 [0.2110, 0.2190] | 3 | -0.0531 [-0.0562, -0.0498] | -0.0532 [-0.0566, -0.0500] |
| sasrec | 0.2091 [0.2051, 0.2130] | 3 | -0.0589 [-0.0620, -0.0555] | -0.0591 [-0.0625, -0.0556] |
| sel_no_scalars | 0.0177 [0.0165, 0.0190] | 3 | -0.2504 [-0.2551, -0.2455] | -0.2505 [-0.2555, -0.2458] |

### p2 — 콜드 조건(같은 조건의 A* 대비 Δ [양측 90% CI])

| 조건 | A_plus | D | nrms |
|---|---|---|---|
| pop0 | -0.0033 [-0.0039, -0.0027] | -0.0090 [-0.0096, -0.0085] | -0.1235 [-0.1255, -0.1217] |
| k0 | +0.0010 [+0.0006, +0.0015] | -0.0046 [-0.0053, -0.0040] | -0.0061 [-0.0072, -0.0048] |
| k1 | -0.0002 [-0.0008, +0.0003] | -0.0057 [-0.0064, -0.0050] | -0.0568 [-0.0585, -0.0551] |
| k3 | +0.0010 [+0.0004, +0.0015] | -0.0046 [-0.0053, -0.0039] | -0.0512 [-0.0527, -0.0496] |
| k5 | +0.0010 [+0.0004, +0.0015] | -0.0046 [-0.0053, -0.0039] | -0.0499 [-0.0514, -0.0484] |

### p2 — 선택(test 미사용)

```json
{
 "family": "nrms",
 "table": {
  "nrms": {
   "best_trial": 6,
   "selection_metric": 0.251188725233078,
   "config": {
    "d": 128,
    "heads": 4,
    "dropout": 0.3,
    "lr": 0.0008435961310929635,
    "weight_decay": 0.01,
    "batch": 256,
    "n_hist": 20
   },
   "best_epoch_in_tuning": 1
  },
  "sasrec": {
   "best_trial": 11,
   "selection_metric": 0.2470010668039322,
   "config": {
    "d": 256,
    "heads": 4,
    "dropout": 0.3,
    "lr": 0.0022287752110421322,
    "weight_decay": 0.0001,
    "batch": 512,
    "n_hist": 20,
    "layers": 2,
    "aux_lambda": 0.0
   },
   "best_epoch_in_tuning": 1
  }
 },
 "rule": "각 family의 튠 best(seed 0)의 선택 표본 지표가 큰 쪽. 동률이면 neural.families의 앞쪽",
 "sample": "es 구간 요청 5,000건, 전체 풀",
 "sample_sha256": "b1830352746e9b79dc4a5b335dc254f6ef88cbd51f85c5c3b57e7f9435287713",
 "sample_requests": 5000,
 "uses_test": false
}
```

## 실행 유효성 게이트

```json
{
 "p1": {
  "reproduction": {
   "arm": "A",
   "checks": {
    "judged": {
     "value": 0.6637978816860359,
     "within": [
      0.662,
      0.6654
     ],
     "inside": true
    },
    "seen_incl": {
     "value": 0.6551712786832815,
     "within": [
      0.6533,
      0.6569
     ],
     "inside": true
    }
   },
   "status": "pass"
  },
  "determinism": {
   "status": "pass",
   "environment": {
    "torch": "2.11.0+cu130",
    "device": "cuda",
    "gpu": "Tesla T4",
    "cuda": "13.0",
    "cudnn": 92700
   },
   "families": {
    "nrms": "pass",
    "sasrec": "pass"
   }
  },
  "rechecks": [
   {
    "arm": "A",
    "checks": {
     "judged": {
      "value": 0.6637978816860359,
      "within": [
       0.662,
       0.6654
      ],
      "inside": true
     },
     "seen_incl": {
      "value": 0.6551712786832815,
      "within": [
       0.6533,
       0.6569
      ],
      "inside": true
     }
    },
    "status": "pass",
    "task": "p1",
    "evidence_grade": "preregistered-run",
    "enforced": true,
    "recheck": "s3",
    "max_abs_diff_vs_first_session": 0.0
   }
  ]
 },
 "p2": {
  "reproduction": {
   "arm": "A",
   "checks": {
    "judged": {
     "value": 0.26823928017392756,
     "within": [
      0.264,
      0.2728
     ],
     "inside": true
    }
   },
   "status": "pass"
  },
  "determinism": {
   "status": "pass",
   "environment": {
    "torch": "2.11.0+cu130",
    "device": "cuda",
    "gpu": "Tesla T4",
    "cuda": "13.0",
    "cudnn": 92700
   },
   "families": {
    "nrms": "pass",
    "sasrec": "pass"
   }
  },
  "rechecks": [
   {
    "arm": "A",
    "checks": {
     "judged": {
      "value": 0.26823928017392756,
      "within": [
       0.264,
       0.2728
      ],
      "inside": true
     }
    },
    "status": "pass",
    "task": "p2",
    "evidence_grade": "preregistered-run",
    "enforced": true,
    "recheck": "s4",
    "max_abs_diff_vs_first_session": 0.0
   }
  ]
 }
}
```

## 한계

- test 창(validation)은 v1·v1.1에서 여러 번 보고됐고 A의 피처 구성은 그 결과와 함께 정해졌다(A·A*·A+에 유리한 방향).
- 임베딩한 기사 본문이 노출 시점 판본이라는 보장이 없다. 양성에는 48h·미열람 필터가 없다(v1과 동일).
- 뉴스 인코더는 고정(BGE-M3 투영)이다. 기각이든 통과든 범위는 그 구성에 한정된다.
- 덴마크어 타블로이드의 클릭 로그다. 한국어 서비스에서 어느 쪽이 나은지는 이 결과로 말할 수 없다.

## 계산 자원

```json
{
 "sessions": {
  "s2": {
   "python": "3.13.16",
   "machine": "x86_64",
   "system": "Linux",
   "cpu_count": 2,
   "ram_gb": 12.7,
   "runtime": "colab-cpu",
   "environment": {
    "torch": "2.11.0+cpu",
    "cuda_available": false,
    "gpu": null,
    "cuda": null,
    "cudnn": null,
    "torch_pin": "2.11.0"
   },
   "wall_seconds_total": 18212.7,
   "wall_seconds_by_step": {
    "p1:gate": 24.2,
    "p2:gate": 18.5,
    "p1:gbdt_tune": 18.4,
    "p2:gbdt_tune": 12113.9,
    "p1:gbdt_final": 2649.9,
    "p2:gbdt_final": 3358.6
   },
   "rate_cu_per_hour": 0.08,
   "cu_estimated": 0.405,
   "cu_spent_before": 5.953,
   "cu_before": 192.6,
   "budget_rules_active": true,
   "cu_cap_session": 8.0,
   "cu_cap_total": 30.0,
   "cu_warn_total": 24.0,
   "skipped_for_budget": [],
   "budget_lines_crossed_during_step": {},
   "registered_runtime": "cpu",
   "device_args": [
    "auto"
   ],
   "allow_cpu_used": false,
   "invocations": 1,
   "code_commit": "381f61db90ff50e4cf2eb8a2d60d07f876042adc"
  },
  "s3": {
   "python": "3.13.15",
   "machine": "x86_64",
   "system": "Linux",
   "cpu_count": 2,
   "ram_gb": 12.7,
   "runtime": "colab-t4",
   "environment": {
    "torch": "2.11.0+cu130",
    "cuda_available": true,
    "gpu": "Tesla T4",
    "cuda": "13.0",
    "cudnn": 92700,
    "nvidia_driver": "580.82.07",
    "torch_pin": "2.11.0"
   },
   "wall_seconds_total": 11198.7,
   "wall_seconds_by_step": {
    "p1:gate:recheck": 1014.1,
    "p1:determinism": 226.0,
    "p1:neural_tune": 1424.7,
    "p1:select": 14.4,
    "p1:neural_final": 933.5,
    "p1:stack": 1444.2,
    "p1:cold": 3386.9,
    "p1:narrative": 2722.8
   },
   "rate_cu_per_hour": 1.07,
   "cu_estimated": 3.329,
   "cu_spent_before": 6.361,
   "cu_before": 192.2,
   "budget_rules_active": true,
   "cu_cap_session": 9.0,
   "cu_cap_total": 30.0,
   "cu_warn_total": 24.0,
   "skipped_for_budget": [],
   "budget_lines_crossed_during_step": {},
   "registered_runtime": "t4",
   "device_args": [
    "auto"
   ],
   "allow_cpu_used": false,
   "invocations": 1,
   "code_commit": "381f61db90ff50e4cf2eb8a2d60d07f876042adc"
  },
  "s4": {
   "python": "3.13.15",
   "machine": "x86_64",
   "system": "Linux",
   "cpu_count": 2,
   "ram_gb": 12.7,
   "runtime": "colab-t4",
   "environment": {
    "torch": "2.11.0+cu130",
    "cuda_available": true,
    "gpu": "Tesla T4",
    "cuda": "13.0",
    "cudnn": 92700,
    "nvidia_driver": "580.82.07",
    "torch_pin": "2.11.0"
   },
   "wall_seconds_total": 18404.8,
   "wall_seconds_by_step": {
    "p2:gate:recheck": 1572.8,
    "p2:determinism": 287.9,
    "p2:neural_tune": 1629.0,
    "p2:select": 13.2,
    "p2:neural_final": 1042.4,
    "p2:stack": 1428.7,
    "p2:cold": 7522.7,
    "p2:narrative": 4427.1,
    "assemble": 435.0
   },
   "rate_cu_per_hour": 1.07,
   "cu_estimated": 5.47,
   "cu_spent_before": 9.736,
   "cu_before": 188.88,
   "budget_rules_active": true,
   "cu_cap_session": 9.0,
   "cu_cap_total": 30.0,
   "cu_warn_total": 24.0,
   "skipped_for_budget": [],
   "budget_lines_crossed_during_step": {},
   "registered_runtime": "t4",
   "device_args": [
    "auto"
   ],
   "allow_cpu_used": false,
   "invocations": 1,
   "code_commit": "381f61db90ff50e4cf2eb8a2d60d07f876042adc"
  }
 },
 "cu_estimated_sum": 9.204,
 "note": "cu_estimated = rate x 벽시계 시간(세션마다). 단계가 도는 동안 60초마다 시각을 적으므로 세션이 단계 도중에 죽어도 그때까지의 시간이 들어간다. 정확한 값은 실행 전후의 잔액 차이로 따로 적는다(실행 뒤 잔액은 VM 안에서 알 수 없어 사람이 적는다). 예산 규칙은 단계 시작 전에만 보므로 긴 단계 하나가 상한을 그 단계의 길이만큼 넘길 수 있고, 넘긴 단계는 budget_lines_crossed_during_step에 남는다."
}
```
