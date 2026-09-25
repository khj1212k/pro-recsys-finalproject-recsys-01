"""코드 버전(team-final / fix-snapshot / current) 하나를 골라 팀의 실제
train -> inference(MMR) -> evaluate 흐름을 파일 아카이브 위에서 그대로 실행하는
워커. run_repro.py가 버전마다 별도 서브프로세스로 이 스크립트를 호출한다 - 세
버전 모두 `src.data.data_loader` 같은 동일한 모듈 경로를 쓰기 때문에, 한 프로세스
안에서 여러 버전을 동시에 import하면 sys.modules 캐시가 충돌한다. 서브프로세스
분리가 이를 피하는 가장 단순하고 안전한 방법이다.

recommend_engine 소스는 절대 수정하지 않는다. 이 스크립트가 대신하는 것은
main_lgbm.py의 CLI 오케스트레이션 레이어뿐이다(그 레이어가 loader.engine을 직접
건드리는 지점 - get_db_max_timestamp, production 모드 DB insert - 이 있어서
그대로 재사용할 수 없다). FeatureEngineer/LGBMDataset/LGBMRanker/
MMRReranker(create_reranker_from_config)/Evaluator는 전부 각 버전의 실제 클래스를
그대로 import해서 쓴다.
"""
from __future__ import annotations

import argparse
import json
import random as py_random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

TEAM_REPRO_DIR = Path(__file__).resolve().parent


def _install_torch_stub() -> None:
    """src.utils.__init__이 BGE-M3 임베더(torch 필요)를 무조건 re-export하는데, 이
    파이프라인은 실제 임베딩 모델을 쓰지 않으므로 torch 없이 import만 통과시킨다.
    (tests/recommend_engine/conftest.py와 동일한 스텁)"""
    import types

    if "torch" in sys.modules:
        return
    torch_stub = types.ModuleType("torch")
    torch_stub.Tensor = type("Tensor", (), {})
    sys.modules["torch"] = torch_stub


def _setup_sys_path(engine_root: Path) -> None:
    for p in (str(engine_root), str(TEAM_REPRO_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _default_lightgbm_params(version: str, seed: int) -> dict:
    if version in ("team-final", "fix-snapshot"):
        return {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "verbose": -1,
            "random_state": seed,
        }
    return {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [5, 10],
        "label_gain": [0, 1],
        "boosting_type": "gbdt",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "random_state": seed,
    }


def build_config(version: str, seed: int, top_k: int, objective_override: Optional[str]) -> dict:
    params = _default_lightgbm_params(version, seed)
    if objective_override == "binary":
        params = dict(params)
        params.update({"objective": "binary", "metric": "auc"})
        params.pop("ndcg_eval_at", None)
        params.pop("label_gain", None)

    return {
        "data": {"max_history_days": 28, "validation_ratio": 0.2},
        "time_decay": {"news_half_life_days": 7, "min_weight": 0.01},
        "ranking": {"group_key": "user_timestamp"},
        "lightgbm": {
            "params": params,
            "num_boost_round": 1000,
            "early_stopping_rounds": 50,
            "negative_sample_ratio": 5,
        },
        "recommendation": {
            "method": "lgbm",
            "top_k": top_k,
            "use_mmr": True,
            "mmr_pool_multiplier": 4,
            "mmr_lambda": {
                "few_categories": 0.8,
                "medium_categories": 0.7,
                "many_categories": 0.6,
                "default": 0.7,
            },
        },
        "output": {"results_dir": "/tmp", "checkpoint_dir": "/tmp"},
        "execution_env": "debug",
    }


def resolve_candidate_pool(bundle, mode: str, pinned_now: datetime, seed: int) -> Optional[List[int]]:
    """decomposition (b): 추론 후보 풀 크기.

    195건 전체(뉴스레터 export)가 실제로는 전부 클릭 로그 시작(2026-01-30 19:26) 이전에
    작성돼 있다(created_at 최댓값이 2026-01-29) - 즉 '검증 당일에 이미 존재했던
    뉴스레터'라는 필터를 곧이곧대로 적용하면 전체 195건과 동일해져 유의미한 실험이
    되지 않는다. 그래서:

      - full_195: 팀 코드가 실제로 하는 것과 동일 - 필터 없음, 전체 195건.
      - small_recent_15: 가장 최근 생성일(2026-01-29, 15건)만 후보로 좁힌 '작은 풀'
        버전. 팀이 스스로 의심한 "candidate pool이 작아서 쉬운 과제였다"는 가설을
        직접 테스트한다.
      - padded_400: 보고된 팀 배포 규모(뉴스레터 405건)에 맞춰, 195건을 복제 +
        임베딩에 약한 가우시안 잡음을 더해 400건까지 부풀린 근사 풀이다. 실제 405건의
        임베딩은 우리에게 없으므로(재임베딩은 195건만 수행됨) '완전히 새로운 콘텐츠'가
        아니라 '기존 콘텐츠의 근접 변형'이라는 한계가 있다 - 보고서에 명시한다.
    """
    all_ids = bundle.newsletters["news_letter_id"].tolist()
    if mode == "full_195":
        return None
    if mode == "small_recent_15":
        cutoff_date = bundle.newsletters["created_at"].max().date()
        subset = bundle.newsletters[bundle.newsletters["created_at"].dt.date == cutoff_date]
        return subset["news_letter_id"].tolist()
    if mode == "padded_400":
        return all_ids  # 패딩 로직은 build_file_data_loader 호출 전 별도로 뉴스 dict 자체를 확장한다 (아래 참고)
    raise ValueError(f"알 수 없는 candidate_pool 모드: {mode}")


def run_one(args: argparse.Namespace) -> dict:
    t0 = time.time()
    engine_root = Path(args.engine_root)
    _setup_sys_path(engine_root)
    _install_torch_stub()

    from src.data.data_loader import DataLoader, NewsItem  # noqa: E402
    from src.features.feature_engineer import FeatureEngineer  # noqa: E402
    from src.data.lgbm_dataset import LGBMDataset  # noqa: E402
    from src.models.lgbm_ranker import LGBMRanker  # noqa: E402
    from src.core.reranker import create_reranker_from_config  # noqa: E402
    from src.core.evaluator import Evaluator  # noqa: E402

    import file_loader as FL  # noqa: E402
    import metrics as M  # noqa: E402

    py_random.seed(args.seed)
    np.random.seed(args.seed)

    bundle = FL.load_archive_bundle()
    pinned_now = bundle.dataset_end_time.to_pydatetime()

    config = build_config(args.version, args.seed, args.top_k, args.objective_override)

    candidate_ids = resolve_candidate_pool(bundle, args.candidate_pool, pinned_now, args.seed)

    if args.candidate_pool == "padded_400":
        bundle = _pad_newsletters(bundle, target_size=400, seed=args.seed)

    loader = FL.build_file_data_loader(
        DataLoader,
        NewsItem,
        config,
        bundle,
        pinned_now,
        label_mode=FL.LabelMode(args.label_mode),
        candidate_pool_ids=candidate_ids,
        history_leakage_mode=args.leakage_mode,
    )

    fe = FeatureEngineer(loader)
    dataset_kwargs = {}
    try:
        dataset = LGBMDataset(loader, fe, seed=args.seed)
    except TypeError:
        # team-final의 LGBMDataset은 seed 인자를 받지 않는다 - 대신 파이썬 전역 random을
        # 쓰므로 위에서 이미 py_random.seed(args.seed)로 고정했다.
        dataset = LGBMDataset(loader, fe)

    neg_ratio = config["lightgbm"]["negative_sample_ratio"]

    if args.negative_source == "random":
        full_df = dataset.create_train_dataset(neg_ratio=neg_ratio)
    else:
        full_df = _build_impression_negative_dataset(fe, bundle, args.label_mode)

    if full_df.empty:
        raise RuntimeError("학습 데이터가 비어 있습니다.")

    group_key = config["ranking"]["group_key"]
    train_df, valid_df, valid_period = _split_train_valid(
        args.version, full_df, config["data"]["validation_ratio"], group_key
    )

    try:
        ranker = LGBMRanker(params=config["lightgbm"]["params"], group_key=group_key)
    except TypeError:
        ranker = LGBMRanker(params=config["lightgbm"]["params"])

    # team-final/fix-snapshot의 LGBMRanker.train()/predict() drop_cols에는 _timestamp가
    # 없다(main_lgbm.py가 학습/추론 직전에 직접 drop한다) - _timestamp를 피처로 흘려
    # 넣으면 예측 자체가 깨지므로(DType 에러) 여기서도 동일하게 미리 제거한다. current
    # 버전은 LGBMRanker가 자체적으로 _timestamp를 그룹 계산에만 쓰고 피처에서 제외한다.
    needs_manual_timestamp_drop = args.version in ("team-final", "fix-snapshot")
    if needs_manual_timestamp_drop and "_timestamp" in train_df.columns:
        train_df = train_df.drop(columns=["_timestamp"])
        valid_df_for_train = valid_df.drop(columns=["_timestamp"])
    else:
        valid_df_for_train = valid_df

    ranker.train(train_df, valid_df=valid_df_for_train)

    # ---- valid set AUC (팀이 학습 중 보던 것과 동일한 정의) ------------------
    valid_scores_df = ranker.predict(valid_df_for_train)
    auc = M.compute_auc(valid_scores_df["label"], valid_scores_df["score"])

    # ---- 추론 (Cartesian) -----------------------------------------------
    inference_df = dataset.create_inference_dataset(target_user_ids=None, eval_timestamp=pinned_now)
    if needs_manual_timestamp_drop and "_timestamp" in inference_df.columns:
        inference_df = inference_df.drop(columns=["_timestamp"])
    scored_df = ranker.predict(inference_df)

    reranker = create_reranker_from_config(config)
    news_dict = loader.load_embedded_news()
    pref_cats = loader.load_user_preferred_categories()
    user_cat_counts = pref_cats.groupby("user_id")["category_id"].count().to_dict()

    top_k = config["recommendation"]["top_k"]
    recommendations: Dict[int, List[int]] = {}
    for uid, group in scored_df.groupby("user_id"):
        group = group.sort_values("score", ascending=False)
        valid_nids = [nid for nid in group["news_id"] if nid in news_dict]
        if not valid_nids:
            continue
        filtered = group[group["news_id"].isin(valid_nids)]
        scores = filtered["score"].values
        embeddings = np.array([news_dict[nid].embedding for nid in valid_nids])
        num_cats = user_cat_counts.get(uid, 0)
        selected = reranker.rerank_for_user(
            scores=scores, embeddings=embeddings, top_k=top_k, num_preferred_categories=num_cats
        )
        recommendations[int(uid)] = [valid_nids[idx] for idx, _ in selected]

    # ---- 정답(ground truth) ------------------------------------------------
    ground_truth = _load_ground_truth(args, bundle, valid_period, pinned_now)

    category_map = FL.category_ids_by_newsletter(bundle)
    evaluator = Evaluator(k_values=[5, 10, 20])
    per_user = M.per_user_metrics(evaluator, recommendations, ground_truth, category_map)
    agg = M.aggregate(per_user)

    elapsed = time.time() - t0
    return {
        "version": args.version,
        "protocol": args.protocol,
        "label_mode": args.label_mode,
        "leakage_mode": args.leakage_mode,
        "candidate_pool": args.candidate_pool,
        "negative_source": args.negative_source,
        "objective_override": args.objective_override,
        "seed": args.seed,
        "n_train": len(train_df),
        "n_valid": len(valid_df),
        "n_candidates": len(candidate_ids) if candidate_ids is not None else len(bundle.newsletters),
        "valid_start": valid_period["valid_start"].isoformat() if valid_period else None,
        "valid_end": valid_period["valid_end"].isoformat() if valid_period else None,
        "auc": auc,
        "aggregate_metrics": agg,
        "per_user_metrics": {str(k): v for k, v in per_user.items()},
        "elapsed_sec": round(elapsed, 2),
    }


def _pad_newsletters(bundle, target_size: int, seed: int):
    """decomposition (b) padded_400: 195건을 target_size까지 부풀린다. 원본 임베딩에
    작은 가우시안 잡음(sigma=0.02, L2 재정규화)을 더해 '근접 변형' 사본을 만든다 -
    완전히 새로운 콘텐츠가 아니라는 한계는 보고서에 명시한다."""
    rng = np.random.default_rng(seed)
    n_needed = target_size - len(bundle.newsletters)
    if n_needed <= 0:
        return bundle
    src = bundle.newsletters
    sampled_idx = rng.integers(0, len(src), size=n_needed)
    next_id = int(src["news_letter_id"].max()) + 1
    new_rows = []
    for i, idx in enumerate(sampled_idx):
        row = src.iloc[idx].copy()
        vec = np.asarray(row["embedding"], dtype=np.float32)
        noisy = vec + rng.normal(scale=0.02, size=vec.shape).astype(np.float32)
        norm = np.linalg.norm(noisy)
        if norm > 0:
            noisy = noisy / norm
        row["news_letter_id"] = next_id + i
        row["embedding"] = noisy
        row["title"] = f"{row['title']} (synthetic-pad-{i})"
        new_rows.append(row)
    padded_df = pd.concat([src, pd.DataFrame(new_rows)], ignore_index=True)

    # 패딩된 항목의 카테고리는 복제 원본과 동일하게 부여
    src_by_id = dict(zip(bundle.categories["news_letter_id"], bundle.categories["category_id"]))
    extra_cats = []
    for i, idx in enumerate(sampled_idx):
        orig_id = int(src.iloc[idx]["news_letter_id"])
        extra_cats.append(
            {
                "news_letter_id": next_id + i,
                "category_id": src_by_id.get(orig_id, 1),
                "category_name": None,
                "source": "padded_copy",
                "confidence": 0.0,
            }
        )
    categories_df = pd.concat([bundle.categories, pd.DataFrame(extra_cats)], ignore_index=True)

    from dataclasses import replace

    return replace(bundle, newsletters=padded_df, categories=categories_df)


def _split_train_valid(version: str, full_df: pd.DataFrame, val_ratio: float, group_key: str):
    if version == "current":
        from src.data.time_split import time_ordered_group_safe_split

        train_df, valid_df = time_ordered_group_safe_split(full_df, val_ratio, group_key=group_key)
    else:
        # team-final / fix-snapshot: 그룹 안전성 없는 단순 시간순 정렬 + 슬라이스
        # (main_lgbm.py의 원래 로직을 그대로 복제 - 두 버전 모두 이 방식을 쓴다).
        sorted_df = full_df.sort_values(by="_timestamp", ascending=True).reset_index(drop=True)
        split_idx = int(len(sorted_df) * (1 - val_ratio))
        train_df = sorted_df.iloc[:split_idx].copy()
        valid_df = sorted_df.iloc[split_idx:].copy()

    valid_period = None
    if "_timestamp" in valid_df.columns and len(valid_df):
        valid_period = {
            "valid_start": pd.Timestamp(valid_df["_timestamp"].min()).to_pydatetime(),
            "valid_end": pd.Timestamp(valid_df["_timestamp"].max()).to_pydatetime(),
        }
    return train_df, valid_df, valid_period


def _build_impression_negative_dataset(fe, bundle, label_mode: str) -> pd.DataFrame:
    """decomposition (c): 랜덤 negative 대신, 실제 노출됐지만 클릭하지 않은 행
    (is_clicked==0)을 negative로 쓴다. FeatureEngineer.create_features()는 그대로
    (수정 없이) 재사용한다 - (uid, nid, label, ts) 튜플 구성 방식만 바꾼다."""
    logs = bundle.ctr_logs
    pos = logs[logs["is_clicked"] == 1]
    neg = logs[logs["is_clicked"] == 0]
    uids = pos["user_id"].tolist() + neg["user_id"].tolist()
    nids = pos["news_letter_id"].tolist() + neg["news_letter_id"].tolist()
    labels = [1] * len(pos) + [0] * len(neg)
    ts = pos["timestamp"].tolist() + neg["timestamp"].tolist()
    return fe.create_features(user_ids=uids, news_ids=nids, labels=labels, timestamps=ts)


def _load_ground_truth(args, bundle, valid_period, pinned_now):
    """scripts/evaluate_results.py와 동일한 로직: valid_period가 있으면 그 시작
    시각 이후의 클릭 로그를 정답으로 쓰고, 없으면 최근 6일 근사치로 폴백한다."""
    logs = bundle.ctr_logs
    if args.label_mode == "clicks_only":
        logs = logs[logs["is_clicked"] == 1]

    if args.protocol == "generator_split":
        # 생성기 자체 분할: valid 쪽 로그 자체를 정답으로 쓴다.
        gt_logs = bundle.generator_valid_keys
        if args.label_mode == "clicks_only":
            gt_logs = gt_logs[gt_logs["is_clicked"] == 1]
    elif valid_period is not None:
        gt_logs = logs[logs["timestamp"] >= valid_period["valid_start"]]
    else:
        gt_logs = logs[logs["timestamp"] >= (pinned_now - timedelta(days=6))]

    return gt_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--version", required=True, choices=["team-final", "fix-snapshot", "current"])
    parser.add_argument("--protocol", default="team_split", choices=["team_split", "generator_split"])
    parser.add_argument("--label-mode", default="clicks_only", choices=["clicks_only", "all_rows"])
    parser.add_argument("--leakage-mode", default="fixed", choices=["fixed", "leaky"])
    parser.add_argument(
        "--candidate-pool", default="full_195", choices=["full_195", "small_recent_15", "padded_400"]
    )
    parser.add_argument("--negative-source", default="random", choices=["random", "impression"])
    parser.add_argument("--objective-override", default="none", choices=["none", "binary"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result = run_one(args)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "per_user_metrics"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
