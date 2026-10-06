# evaluation

LLM judge 하나에만 의존하지 않고, 클러스터링/뉴스레터 생성 파이프라인의
품질을 재현 가능한 수치로 남기기 위한 순수 평가 모듈 모음이다.
DB나 LLM 호출 없이 동작하는 함수들로만 구성되어 있어 CI에서 가볍게
돌릴 수 있다.

## 모듈

### `evaluation/clustering/metrics.py` — 클러스터링 품질 지표

`ai_workspace/core/clustering/hdbscan_clusterer.py` / `split_v2.py`가
만든 클러스터 라벨을 정량적으로 평가한다.

- `dbcv(X, labels, metric="euclidean")` — HDBSCAN 공식 밀도 기반 검증
  지표(DBCV, `hdbscan.validity.validity_index`). 이 파이프라인의 임베딩은
  L2-정규화된 단위 벡터이므로 `metric="euclidean"`이 기본값이지만
  (단위 벡터에서 euclidean은 cosine과 단조 동치), `metric="cosine"`도
  그대로 지원한다. 빈 입력/전부 노이즈/클러스터 1개/싱글톤 클러스터처럼
  정의가 안 되는 경우에는 예외 대신 `(None, reason)`을 반환한다.
- `basic_stats(labels)` — 클러스터 개수, 노이즈 비율, 클러스터 크기
  분포(최소/중앙값/최대, 크기 구간별 히스토그램).
- `cosine_silhouette(X, labels)` — 노이즈를 제외한 실루엣 점수(cosine
  거리).
- `bcubed(labels_pred, labels_true)` — 정답 라벨이 있을 때의 B-cubed
  precision/recall/F1. 노이즈(-1)는 점마다 독립된 싱글톤 클러스터로
  취급한다.
- `stability_ari(X, cluster_fn, n_runs, frac, seed)` — 부트스트랩
  서브샘플링 후 재군집화한 결과와 전체 군집화 결과의 adjusted Rand
  index를 비교해 군집화 안정성을 측정한다.
- `evaluate_run(...)` — 위 지표들을 한 번에 묶어 JSON 직렬화 가능한
  리포트 dict로 반환한다.

CLI로도 실행할 수 있다 (저장된 임베딩/라벨 `.npy` 파일에 대해):

```bash
python -m evaluation.clustering.metrics \
  --embeddings embeddings.npy \
  --labels labels.npy \
  --truth truth_labels.npy \          # 선택: 있으면 bcubed 포함
  --out report.json
```

### `ai_workspace/core/faithfulness.py` — 한국어 뉴스레터 사실성 검증

> 2026-09-25 `evaluation/llm/`에서 `ai_workspace/core/`로 옮겼다. LangGraph
> 워크플로우가 이 모듈로 생성물을 게이트하므로(ADR 0010) 런타임 패키지에 있어야
> 하고, 평가 코드는 `from core.faithfulness import ...`로 가져다 쓴다(런타임 →
> 평가 방향 의존을 만들지 않기 위함).

원문 기사들로부터 생성된 뉴스레터(및 캐주얼체로 재작성한 버전)가 숫자,
날짜, 인명/기관명, 인용구를 원문과 다르게(환각·누락·훼손) 만들지
않았는지 확인한다.

- `extract_facts(text, allow_regex_fallback=False)` — 텍스트에서 숫자
  (조/억/만/천 스케일 정규화, %, %p, 명/건/배/bp, $billion/million 등
  단위 포함), 날짜(절대 날짜는 `YYYY-MM-DD`로 정규화, 오늘/어제 같은
  상대 표현은 플래그만), 개체명(Kiwi 형태소 분석 NNP/SL/SH 태그), 인용구
  (`""`/`“”`/`''`/`‘’`)를 추출한다.
  - 개체명 추출은 **kiwipiepy가 설치되어 있어야** 하며, 없으면 기본적으로
    `ImportError`를 던진다("추출기가 없어서 못 찾음"과 "정상 추출했는데
    없음"을 구분하기 위함). `allow_regex_fallback=True`를 주면
    `ai_workspace/core/clustering/split_v2.py`와 같은 방식의 정규식
    기반 폴백(품사 태깅 없음, 더 약한 근사치)으로 대체할 수 있다. 실제
    사용된 추출기는 `Facts.entity_extractor` / `Report.entity_extractor`
    (`"kiwi"` 또는 `"regex_fallback"`)로 확인할 수 있다.
- `check_against_sources(generated, sources, ...)` — 생성된 텍스트의
  사실들이 원문(들)에 있는지 확인해 `Report`(통과 여부, 미지원 숫자/개체
  /인용구 목록, 사용된 임계값과 추출기)를 반환한다. 숫자는 반올림 오차
  허용치(`number_approx_tol`) 내면 근사 일치로, 통화 단위가 없는 스케일
  숫자(예: "12억")는 KRW로만 매칭되고(다른 명시적 통화와는 매칭되지
  않음) 개체/인용구는 `rapidfuzz` 유사도 임계값으로 판정한다.
- `compare_rewrite(original, rewritten, ...)` — 톤 변환(격식체→캐주얼체)
  전후의 숫자/날짜/개체 드리프트(추가/누락)를 비교해 `DriftReport`를
  반환한다. 추후 LangGraph 게이트의 입력이 된다.

### `evaluation/llm/` — LLM bake-off와 judge 보정 (ADR 0009/0010)

규칙은 결과를 보기 전에 [ADR 0009](../docs/adr/0009-llm-eval-protocol-and-preregistered-decision-rule.md)와
`evaluation/llm/preregistration/bakeoff-v1.yaml`에 사전 등록했다. 분석기는 yaml의 상수만 읽는다.
기사 본문·생성물·라벨은 전부 `data/`(gitignore) 아래에만 쓰고, 저장소에는 id·URL·SHA-256만 커밋한다.

| 모듈 | 역할 |
|---|---|
| `evalset.py` | `cluster_history`에서 층화 추출(크기×카테고리×split_v2) + 어려운 사례(n과 별도, 생성 안 함) |
| `bakeoff.py` | 재개 가능한 러너: `generate` / `judge` / `cluster-eval` / `export-blind` |
| `labels.py`, `labeling_app.py` | 라벨 저장소와 로컬 라벨링 UI(127.0.0.1 전용). 정의는 [라벨링 가이드](../docs/eval/labeling-guide.md) |
| `calibration.py` | Cohen's κ·Spearman, 클러스터 단위 2-fold 임계값 선택, 자기선호 DiD, ClusterEvaluator ROC |
| `bakeoff_analysis.py` | 사전 등록 규칙을 기계적으로 적용해 승자·judge·게이트 결정 |
| `gate_probe.py` | 결정론적 게이트의 오탐/검출 프로브(LLM·DB 호출 없음) |
| `frozen_export.py` | 실험 입력의 동결 반출: `news_raw`의 시간 창을 읽기 전용 트랜잭션 하나로 `data/exports/<T0>/`에 뽑고, 매니페스트·본문 30일 정리를 맡는다. CLI는 `scripts/export_news_raw.py` ([ADR 0036](../docs/adr/0036-experiment-data-path-frozen-export-file-stand-ins.md), 절차는 [런북 9절](../docs/runbook-hosting.md)) |
| `embedding_pack.py` | 원격 임베딩 잡이 돌려주는 팩(ids·벡터·본문 해시·매니페스트)의 형식과 반출본 대조. CLI는 `scripts/import_embeddings.py` |
| `e0_store.py` | 운영 코드를 고치지 않고 DB 없이 클러스터링·생성 경로를 돌리는 파일 대역(로더·저장·기록 전용 임베더)과 운영 함수 계약 검사. E0 러너가 쓴다(러너는 아직 없다) |

전체 흐름 (실제 LLM 호출 단계는 비용이 든다 - `config/llm_pricing.yaml` 단가, 킬 스위치를 존중한다):

```bash
# 0) 평가셋 고정 (DB 필요). 매니페스트는 커밋, 본문은 data/evalsets/v1/
python -m evaluation.llm.evalset sample --name v1 --n 40 --seed 20260925 --warmup 2
python -m evaluation.llm.evalset verify --name v1

# 1) pre-flight: 워밍업 2개로 설정 오류만 확인 (본 실행과 다른 run 이름)
python -m evaluation.llm.bakeoff generate --evalset v1 --split warmup --run bakeoff-v1-preflight

# 2) 본 실행 - 중간에 402/킬 스위치로 멈추면 같은 명령으로 이어서 실행
python -m evaluation.llm.bakeoff generate --evalset v1
python -m evaluation.llm.bakeoff judge --evalset v1
python -m evaluation.llm.bakeoff cluster-eval --evalset v1 --provider gemini --model gemini-3.1-flash-lite

# 3) 블라인드 내보내기 -> 라벨링 (클러스터 라벨 먼저, 48시간 뒤 재라벨: /?round=2)
python -m evaluation.llm.bakeoff export-blind --evalset v1
python -m evaluation.llm.labeling_app --run bakeoff-v1

# 4) 분석 (data/bakeoff/bakeoff-v1/analysis.json + 요약 표, 생성 텍스트는 출력하지 않음)
python -m evaluation.llm.bakeoff_analysis --run bakeoff-v1

# 게이트 프로브 (로컬 뉴스레터 JSON이 있으면 실제 문장 변형까지)
python -m evaluation.llm.gate_probe --newsletters 'data/team_archive/newsletters/*.json'
```

### `evaluation/recsys/` — 추천 오프라인 평가 (EB-NeRD 공개 벤치마크)

- `evaluation/recsys/metrics.py` — 그룹(노출/요청)별 AUC·MRR·nDCG@k·Recall@k(풀 밖 정답을
  분모에 넣는 `n_pos_total`), 목록 다양성(ILD)·카테고리 엔트로피·카탈로그 커버리지, 유저 단위
  클러스터 부트스트랩과 쌍체 차이 CI. 점수 동점은 seed 고정 무작위로 깬다.
- `evaluation/recsys/ebnerd/` — EB-NeRD(덴마크 Ekstra Bladet 클릭 로그) 하네스.
  `loaders.py`(parquet → CSR 노출, 기사 본문은 읽지 않음), `prepare.py`(point-in-time 이벤트 인덱스,
  P1 노출 재정렬·P2 48h 전체 풀·네거티브 샘플링), `models.py`(팀 방식 LightGBM → ranker v2
  ablation과 휴리스틱 베이스라인), `embed_articles.py`(BGE-M3, `.venv-embed`·MPS),
  `embedding_sanity.py`(카테고리 kNN), `run_ebnerd.py`(전체 실행), `make_report.py`(JSON → 표,
  ADR 0013 승격 규칙 판정), `click_time_sensitivity.py`(클릭 시각 = 노출 시각 근사의 민감도, 인기도
  베이스라인만). 피처는 최상위 `recsys_core/`(numpy/pandas만, DB 없음)가 계산한다.
- 프로토콜·판정 규칙은 [ADR 0013](../docs/adr/0013-ranker-v2-design.md), 결과는
  `reports/recsys/ebnerd_v1.{md,json}`(본 ablation, `--chain inview`)과 `ebnerd_v1_1_poolneg.json`
  (48h 풀 네거티브 고정 보충 사슬, `--chain poolneg`), `ebnerd_v1_click_time_sensitivity.json`(리포트 부록 B).
- 실행 준비: `run_ebnerd`의 MMR 스윕은 팀 구현(`from src.core.reranker import MMRReranker`)을 그대로 쓰므로
  `ai_workspace/recommend_engine`을 venv에 editable로 설치해야 한다(CI unit 잡과 같은 방식):
  `uv pip install --python .venv/bin/python --no-deps -e ai_workspace/recommend_engine`.
  `recsys_core`는 저장소 루트에서 `python -m evaluation.recsys.ebnerd.run_ebnerd ...`로 실행하면 import된다.
- demo 데이터 기반 테스트(로더·과제 구성·전 구간 스모크)는 데이터가 있어야 돈다. git worktree에서는
  `EBNERD_ROOT=<메인 체크아웃>/data/benchmarks/ebnerd`를 지정해 실행한다(없으면 skip).
- **콜드 regime 사슬(v1.2, 결과 대기)**: 인기도 0·저트래픽·작은 풀·짧은 히스토리에서 랭커가 얼마나 버티는지 재는
  실험 묶음이다. 규칙은 [ADR 0013 "A2 사전 등록"](../docs/adr/0013-ranker-v2-design.md)과
  `ebnerd/preregistration/cold-v1.2.yaml`에 결과 전에 고정했다.
  - `ebnerd/neural/cold.py`(히스토리 절단·인기도 0 강제 — 이후 신경망 비교와 공유), `cold_transforms.py`(학습 마스킹,
    유저 서브샘플, 풀 축소, 요청 내 랭크, 축소 CTR, 릴리스 양자화), `candidate_config.py`(서빙 5출처 라운드로빈·cap),
    `heuristic_fit.py`(4항 가중치 적합), `cold_verdicts.py`(기계 판정), `run_cold.py`(단계별 체크포인트 실행기),
    `make_cold_report.py`(JSON → 표), `synthetic.py`(EB-NeRD 스키마의 합성 데이터 — CI와 드라이 런용).
  - 판정용 실행은 원격 CPU 런타임에서 `scripts/m4_colab_driver.py`로 한다(명령은 ADR 0013 A2.8, 핀은
    `requirements-colab.txt`). 개발용 Mac에서는 돌리지 않는다.
  - 로컬에서는 배선 확인만 한다. 등록한 인자·입력과 다른 실행은 리포트 머리말에 "demo, not evidence"가 찍힌다:
    `python -m evaluation.recsys.ebnerd.synthetic --out /tmp/synth` 뒤
    `python -m evaluation.recsys.ebnerd.run_cold --dataset ebnerd_synth --root /tmp/synth --out-dir /tmp/cold --seeds 0
    --n-boot 20 --p2-sample 150 --sub-cap 100 --stage all`(약 10초).
- **신경망 사용자 모델 비교(E15, 결과 대기)**: NRMS-lite·SASRec-lite·GBDT 스태킹을 같은 예산으로 튠한 LightGBM과 같은
  과제·네거티브·스칼라 행렬 위에서 비교하는 실험이다. 규칙은 [ADR 0013 "A3 사전 등록"](../docs/adr/0013-ranker-v2-design.md)과
  `ebnerd/preregistration/neural-e15.yaml`에 결과 전에 고정했다. 아직 판정용 실행은 한 번도 하지 않았다.
  - `ebnerd/neural/`: `sequences.py`(마지막 N 클릭, 시퀀스 스칼라, 스칼라 블록, 콜드 증강), `datasets.py`(길이별 배치·패딩),
    `tune.py`(같은 예산의 무작위 탐색, 튠한 LambdaRank 학습), `stack.py`(시간 전진 OOF 규칙), `report.py`(기계 판정
    `neural_verdict`와 표) — 여기까지는 torch 없이 돈다. `models.py`·`train.py`(두 family, 후기 융합, 결정론적 학습과
    결정론 게이트)는 torch가 필요하다. `run_neural.py`가 과제(p1|p2)와 단계를 하나씩 받아 체크포인트를 남기며 돈다.
  - 판정용 실행은 원격 런타임에서 `scripts/e15_colab_driver.py`로 세션(s0 드라이 런 → s1 파일럿 → s2 GBDT → s3·s4 GPU)
    단위로 한다(명령은 ADR 0013 A3.11, 핀은 `requirements-colab-gpu.txt`). 개발용 Mac에서는 돌리지 않는다.
  - 로컬에서는 배선 확인만 한다(합성 데이터, CPU, 과제당 약 20초, 결과는 "demo, not evidence"):
    `python -m evaluation.recsys.ebnerd.run_neural --dataset ebnerd_synth --root /tmp/synth --out-dir /tmp/e15 --task p1
    --stage all --seeds 0 --n-boot 30 --p2-sample 150 --p2-cold-sample 200 --p2-select-sample 80 --neural-trials 1
    --gbdt-trials 2 --tune-max-epochs 1 --final-max-epochs 2 --device cpu --resume`.
    macOS에서는 LightGBM과 torch가 각자 OpenMP 런타임을 실어 오므로 torch를 1스레드로 두고 LightGBM을 먼저 임포트한다
    (`neural/train.py`의 주석). torch가 없는 환경에서는 신경망 테스트가 skip되고, torch가 필요한 테스트는 E15 경로가
    바뀔 때만 도는 별도 CI job(`.github/workflows/ebnerd-neural.yml`, CPU torch)이 돈다.
- **라이선스**: EB-NeRD는 연구/비상업 전용이다. 데이터·임베딩은 gitignore된 `data/benchmarks/ebnerd/`(또는
  `EBNERD_ROOT`)에만 두고 저장소에는 집계 수치만 커밋한다. 저장소 커밋·공개 데이터셋·영구 사본은 금지이고, 무거운 재실행은
  비공개 클라우드 런타임에 실행마다 올려서 돌린 뒤 지운다(ADR 0013 "사후 변경 기록"의 통합 기록).
  데이터가 없는 환경(CI)에서는 관련 테스트가 skip된다.

## 설치

```bash
uv pip install -q --python .venv/bin/python -r evaluation/requirements.txt
```

(`evaluation/requirements.txt`에 명시된 버전은 이 프로젝트의 개발 venv에서
`uv pip freeze`로 실제 검증한 버전이다. 모듈이 직접 `import`하는 런타임 의존성
(`rapidfuzz` 포함)만 담았고, `pytest` 같은 테스트 전용 패키지는 제외했다.)

## 테스트 실행

```bash
# evaluation 전체
.venv/bin/python -m pytest -q tests/evaluation/

# 모듈별
.venv/bin/python -m pytest -q tests/evaluation/test_clustering_metrics.py
.venv/bin/python -m pytest -q tests/test_faithfulness.py
```

`tests/evaluation/test_clustering_metrics.py`의 `TestCli` 클래스는
`python -m evaluation.clustering.metrics` CLI를 실제 서브프로세스로
실행해 `--out`에 쓰인 JSON 리포트를 검증한다.
