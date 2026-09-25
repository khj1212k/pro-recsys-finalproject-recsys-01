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
