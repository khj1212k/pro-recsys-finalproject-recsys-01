# Recommend Engine

LightGBM(LambdaRank) + MMR 기반 뉴스레터 추천 엔진. `ai_workspace/` 상위 파이프라인(크롤링→클러스터링→LLM 생성)이 만든 뉴스레터를 대상으로, 유저별 클릭 로그를 학습해 개인화 랭킹을 매기고 카테고리 다양성을 고려해 재정렬한다.

## 왜 독립적으로 설치 가능한가
이 서브 디렉토리는 자체 `pyproject.toml`을 갖고 있어, `ai_workspace` 전체를 설치하지 않고도 추천 엔진만 따로 학습/평가/배포할 수 있다.

```bash
cd ai_workspace/recommend_engine
pip install -e .
# 테스트까지 실행하려면:
pip install -e ".[test]"
# 하이퍼파라미터 탐색(Optuna)까지 쓰려면:
pip install -e ".[tuning]"
# MLflow 실험 추적까지 쓰려면:
pip install -e ".[tracking]"
# scripts/generate_embeddings.py로 임베딩을 직접 생성하려면(torch/FlagEmbedding, 수 GB급):
pip install -e ".[embed]"
```

설정은 `ai_workspace/config/settings.py`가 아니라 이 디렉토리의 `config/config.yaml`을 쓴다(상위 파이프라인과는 별도 설정 체계). DB 접속 정보는 YAML에 직접 쓰는 대신 `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME` 환경변수가 있으면 그 값을 우선 사용한다(`.env`는 `ai_workspace/.env`를 공유).

## 실행

```bash
# 학습 (train/valid time-split -> LightGBM lambdarank 학습 -> 버전 저장)
python main_lgbm.py --train

# 추론 (오늘자 랭킹 생성 -> MMR 재정렬 -> 결과 저장)
python main_lgbm.py --inference

# 평가 (evaluate_results.py 참고: Precision/Recall/nDCG/MRR/Coverage)
python scripts/evaluate_results.py

# Ablation Study (binary vs lambdarank 비교)
python scripts/ablation_objective_comparison.py

# 하이퍼파라미터 탐색 (Optuna, 선택)
python scripts/tune_hyperparams.py --n-trials 20
```

## 구조
```
recommend_engine/
├── config/config.yaml       # 이 서브 프로젝트 전용 설정
├── main_lgbm.py              # 학습/추론 엔트리포인트
├── scripts/                  # 평가/통계/탐색 유틸리티 (scripts/README.md 참고)
└── src/
    ├── data/                 # DataLoader, LGBMDataset
    ├── features/             # FeatureEngineer
    ├── models/                # LGBMRanker
    ├── core/                  # Evaluator, Reranker(MMR)
    └── utils/                 # 공통 유틸(로거, 임베더 등)
```

## 모델 버저닝
`main_lgbm.py --train`은 매번 `checkpoints/lgbm_model_{timestamp}.txt`(LightGBM 네이티브 텍스트 포맷, `booster.save_model()`)로 새 버전을 저장하고, 현재 서빙 버전을 가리키는 `checkpoints/latest_model.json` 포인터를 갱신한다. `--inference`는 이 포인터를 따라가 최신 모델을 로드한다.

## 알려진 한계
- `src/utils/embedder.py`의 `BGEEmbedder`는 `ai_workspace/core/embedder.py`의 `NewsEmbedder`와 거의 동일한 BGE-M3 로딩 로직을 별도로 구현한 것이다(중복). 두 서브 프로젝트가 서로 다른 설치 단위로 분리되어 있어 강제 통합 시 임포트 경로 변경 리스크가 있어 우선 문서화만 해두고, 실제 병합은 별도 계획으로 진행 예정이다.
