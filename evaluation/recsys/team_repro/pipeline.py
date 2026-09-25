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

=== v2: 어드버서리얼 리뷰(unsound 판정) 이후 재작성 ===

v1은 추론 시점을 데이터셋 끝(pinned_now)에 고정했다. create_inference_dataset이
그 시각을 "기준 시간"으로 넘기면, current/fix-snapshot은 point-in-time 히스토리
계산(compute_history_embedding)이 그 시각 '이전' 로그만 쓰도록 되어 있지만, 그
'이전'이 정답 구간(valid window)까지 포함해버렸다 - 정답 자체가 피처에 들어간
것이다. team-final은 아예 point-in-time 로직이 없어(build_user_profiles가 로더가
주는 로그를 시간 필터 없이 전부 쓴다) 항상 이렇게 샌다. v2는 이를 BLOCKER로 보고
point-in-time 추론을 1차 프로토콜로 삼는다:

  1. 모든 arm/시드/버전이 공유하는 **고정** 정답 구간 시작 시각(answer_start,
     run_repro.py가 한 번만 계산해 넘겨준다)을 기준으로, 그 이전 로그만 학습에
     쓴다(loader #1, 미제한 전체 로그 - 학습 자체는 여전히 전체 로그에서 만든
     `full_df`를 answer_start로 잘라 쓴다).
  2. 조기 종료(early stopping)는 정답 구간이 아니라 학습 구간 자체를 다시 나눈
     inner-validation으로 한다(요구사항 5).
  3. **1차(primary) 추론**은 로그 자체를 answer_start '이전'으로 물리적으로 잘라낸
     두 번째 로더(loader #2)로 만든 FeatureEngineer/LGBMDataset을 쓴다 - 이러면
     team-final도 데이터 자체가 없어 미래를 볼 수 없다(모듈 docstring/
     file_loader.bundle_before 참고). 같은 학습된 ranker를 재사용한다(추론 시점의
     누출 하나만 순수하게 분리하기 위해 - 모델을 다시 학습하면 학습 분할 차이와
     추론 누출이 뒤섞인다).
  4. **2차(as-written/leaky) 행**은 팀이 실제로 배포한 대로(디버그 모드 기준
     main_lgbm.py의 inference_pipeline: eval_time = DB 마지막 로그 시각) 돌린
     결과다 - 같은 ranker, 원래(미제한) 로더/FeatureEngineer(loader #1)로 추론하고,
     '순수 추론 누출' 효과를 보기 위해 정답 구간은 1차와 동일하게 둔다.
  5. team-final에 한해 **team-final-as-written** 행을 추가로 낸다 - 실제
     scripts/evaluate_results.py의 정답 정의(created_at >= NOW()-6 DAYS, is_clicked
     필터 없음)를 그대로 적용한다. 이 아카이브 구간이 6시간 16분뿐이라 사실상
     '로그 전체'가 정답이 된다(보고된 0.897을 설명하는 근거 중 하나).
"""
from __future__ import annotations

import argparse
import json
import random as py_random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

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


def resolve_candidate_pool(bundle, mode: str, pinned_now: datetime, seed: int) -> Optional[List[int]]:
    """decomposition (b): 추론 후보 풀 크기.

    195건 전체(뉴스레터 export)가 실제로는 전부 클릭 로그 시작(2026-01-30 19:26) 이전에
    작성돼 있다(created_at 최댓값이 2026-01-29) - 즉 '검증 당일에 이미 존재했던
    뉴스레터'라는 필터를 곧이곧대로 적용하면 전체 195건과 동일해져 유의미한 실험이
    되지 않는다. 그래서:

      - full_195: 팀 코드가 실제로 하는 것과 동일 - 필터 없음, 전체 195건.
      - small_recent_15: 가장 최근 생성일(2026-01-29, 15건)만 후보로 좁힌 '작은 풀'
        버전. 팀이 스스로 의심한 "candidate pool이 작아서 쉬운 과제였다"는 가설을
        직접 테스트한다. v2: 정답도 이 풀 안으로 제한한다(protocol.restrict_ground_
        truth_to_pool) - v1은 정답을 풀 밖에 그대로 둬 애초에 도달 불가능한 정답을
        놓친 걸로 계산했다(MAJOR).
      - padded_400: 보고된 팀 배포 규모(뉴스레터 405건)에 맞춰, 195건을 복제 +
        임베딩에 약한 가우시안 잡음을 더해 400건까지 부풀린 근사 풀이다. v2:
        run_one()에서 패딩을 먼저 적용한 뒤 이 함수를 호출하므로(v1은 순서가
        반대라 후보 id가 여전히 195건이었다 - BLOCKER), None을 반환해 "패딩된
        bundle.newsletters 전체"를 그대로 후보로 쓴다.
    """
    if mode == "full_195":
        return None
    if mode == "small_recent_15":
        cutoff_date = bundle.newsletters["created_at"].max().date()
        subset = bundle.newsletters[bundle.newsletters["created_at"].dt.date == cutoff_date]
        return subset["news_letter_id"].tolist()
    if mode == "padded_400":
        return None
    raise ValueError(f"알 수 없는 candidate_pool 모드: {mode}")


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


def _build_impression_negative_dataset(fe, bundle, label_mode) -> pd.DataFrame:
    """decomposition (c): 랜덤 negative 대신, 실제 노출됐지만 클릭하지 않은 행
    (is_clicked==0)을 negative로 쓴다. FeatureEngineer.create_features()는 그대로
    (수정 없이) 재사용한다 - (uid, nid, label, ts) 튜플 구성 방식만 바꾼다.

    v2 주의(리포트에 명시): 이 아카이브에서는 페르소나 전원이 195건 전부를 노출
    받았으므로(데이터 구조 절 참고), '클릭 안 한 나머지'는 곧 '노출됐지만 클릭
    안 한 것'과 사실상 같은 집합이다 - random negative와 impression negative가
    이 데이터에서는 거의 같은 모집단에서 뽑힌다. 차이는 표본 크기(전수 vs
    neg_ratio 샘플링)와 각 행이 붙는 타임스탬프(노출 자체 시각 vs positive와
    동일한 시각)뿐이다.
    """
    logs = bundle.ctr_logs
    pos = logs[logs["is_clicked"] == 1]
    neg = logs[logs["is_clicked"] == 0]
    uids = pos["user_id"].tolist() + neg["user_id"].tolist()
    nids = pos["news_letter_id"].tolist() + neg["news_letter_id"].tolist()
    labels = [1] * len(pos) + [0] * len(neg)
    ts = pos["timestamp"].tolist() + neg["timestamp"].tolist()
    return fe.create_features(user_ids=uids, news_ids=nids, labels=labels, timestamps=ts)


def _nearest_group_boundary(group_ids: pd.Series, target_idx: int) -> int:
    """target_idx에 가장 가까우면서 그룹(쿼리)을 자르지 않는 분할 지점을 찾는다.
    current의 src.data.time_split.find_group_safe_split_index와 동일한 알고리즘을
    엔진 버전에 의존하지 않고 재구현한 것 - team-final/fix-snapshot에는 이 모듈이
    없어서, inner-validation 분할(요구사항 5)을 모든 버전에 균일하게 적용하려면
    하네스 자체에 필요하다."""
    n = len(group_ids)
    target_idx = max(0, min(target_idx, n))
    if target_idx in (0, n):
        return target_idx
    values = group_ids.to_numpy()
    if values[target_idx - 1] != values[target_idx]:
        return target_idx
    left = target_idx
    while left > 0 and values[left - 1] == values[target_idx]:
        left -= 1
    right = target_idx
    while right < n and values[right] == values[target_idx]:
        right += 1
    return left if (target_idx - left) <= (right - target_idx) else right


def _group_ids_for(df: pd.DataFrame, group_key: str) -> pd.Series:
    if group_key == "user_id":
        return df["user_id"].astype(str)
    if "_timestamp" not in df.columns:
        raise ValueError("group_key='user_timestamp'에는 '_timestamp' 컬럼이 필요합니다.")
    return df["user_id"].astype(str) + "|" + df["_timestamp"].astype(str)


def _time_group_safe_split(df: pd.DataFrame, val_ratio: float, group_key: str):
    """요구사항 5: 조기 종료용 inner-validation을 '학습 구간 자체'에서, 그룹(쿼리)을
    자르지 않으며 시간순으로 떼어낸다. v1은 team-final/fix-snapshot에 대해 그냥
    시간순 슬라이스만 했는데(그룹 안전성 없음), v2는 모든 버전에 동일하게 그룹
    안전 분할을 적용한다(이 분할은 우리 하네스가 하는 것이지 엔진 코드가 하는
    게 아니므로, 버전마다 다르게 할 이유가 없다)."""
    if len(df) == 0 or "_timestamp" not in df.columns:
        split_idx = int(len(df) * (1 - val_ratio))
        return df.iloc[:split_idx].copy(), df.iloc[split_idx:].copy()
    sorted_df = df.sort_values("_timestamp", kind="mergesort").reset_index(drop=True)
    target_idx = int(len(sorted_df) * (1 - val_ratio))
    group_ids = _group_ids_for(sorted_df, group_key)
    idx = _nearest_group_boundary(group_ids, target_idx)
    return sorted_df.iloc[:idx].copy(), sorted_df.iloc[idx:].copy()


def _drop_timestamp_if_needed(df: pd.DataFrame, needs_drop: bool) -> pd.DataFrame:
    if needs_drop and df is not None and "_timestamp" in df.columns:
        return df.drop(columns=["_timestamp"])
    return df


def _build_recommendations(scored_df: pd.DataFrame, reranker, news_dict, user_cat_counts, top_k: int) -> Dict[int, List[int]]:
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
    return recommendations


def _n_distinct_scores(scored_df: pd.DataFrame) -> int:
    return int(len(set(np.round(scored_df["score"].values, 8).tolist())))


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
    import protocol as PR  # noqa: E402
    import config_loader as CFG  # noqa: E402

    py_random.seed(args.seed)
    np.random.seed(args.seed)

    bundle = FL.load_archive_bundle()
    pinned_now = bundle.dataset_end_time.to_pydatetime()

    objective_override = None if args.objective_override == "none" else args.objective_override
    config, config_hash, cfg_overrides = CFG.load_version_config(
        engine_root, args.seed, args.top_k, objective_override
    )

    if args.candidate_pool == "padded_400":
        bundle = _pad_newsletters(bundle, target_size=400, seed=args.seed)
    candidate_ids = resolve_candidate_pool(bundle, args.candidate_pool, pinned_now, args.seed)

    n_candidates_check = len(candidate_ids) if candidate_ids is not None else len(bundle.newsletters)
    if args.candidate_pool == "padded_400":
        assert n_candidates_check == 400, (
            f"padded_400 arm은 후보가 정확히 400건이어야 합니다 (실제 {n_candidates_check}건) - "
            "BLOCKER #4 회귀 (resolve_candidate_pool을 패딩 이후에 호출해야 함)."
        )

    label_mode = FL.LabelMode(args.label_mode)

    engine = {
        "DataLoader": DataLoader,
        "NewsItem": NewsItem,
        "FeatureEngineer": FeatureEngineer,
        "LGBMDataset": LGBMDataset,
        "LGBMRanker": LGBMRanker,
        "create_reranker_from_config": create_reranker_from_config,
        "Evaluator": Evaluator,
    }

    if args.protocol == "generator_split":
        result = _run_generator_split(
            args, engine, bundle, pinned_now, config, config_hash, cfg_overrides,
            candidate_ids, label_mode, FL, M, PR,
        )
    else:
        result = _run_team_split(
            args, engine, bundle, pinned_now, config, config_hash, cfg_overrides,
            candidate_ids, label_mode, FL, M, PR,
        )

    result["elapsed_sec"] = round(time.time() - t0, 2)
    return result


def _run_team_split(args, engine, bundle, pinned_now, config, config_hash, cfg_overrides, candidate_ids, label_mode, FL, M, PR) -> dict:
    DataLoaderCls, NewsItemCls = engine["DataLoader"], engine["NewsItem"]
    FeatureEngineer, LGBMDataset, LGBMRanker = engine["FeatureEngineer"], engine["LGBMDataset"], engine["LGBMRanker"]
    create_reranker_from_config, Evaluator = engine["create_reranker_from_config"], engine["Evaluator"]

    if not args.answer_start:
        raise ValueError("team_split 프로토콜에는 --answer-start가 필요합니다 (요구사항 1: 모든 arm/시드에 동일한 고정 정답 구간).")
    answer_start = pd.Timestamp(args.answer_start)

    needs_manual_timestamp_drop = args.version in ("team-final", "fix-snapshot")
    effective_group_key = "user_id" if args.negative_source == "impression" else config["ranking"]["group_key"]

    # ---- loader #1: 학습 + as-written(leaky) 추론용, 물리적으로 제한하지 않은 전체 로그 ----
    loader1 = FL.build_file_data_loader(
        DataLoaderCls, NewsItemCls, config, bundle, pinned_now,
        label_mode=label_mode, candidate_pool_ids=candidate_ids, history_leakage_mode=args.leakage_mode,
    )
    fe1 = FeatureEngineer(loader1)
    try:
        dataset1 = LGBMDataset(loader1, fe1, seed=args.seed)
    except TypeError:
        dataset1 = LGBMDataset(loader1, fe1)

    neg_ratio = config["lightgbm"].get("negative_sample_ratio", 5)
    if args.negative_source == "random":
        full_df = dataset1.create_train_dataset(neg_ratio=neg_ratio)
    else:
        full_df = _build_impression_negative_dataset(fe1, bundle, label_mode)

    if full_df.empty:
        raise RuntimeError("학습 데이터가 비어 있습니다.")

    # ---- v2 요구사항 1: 고정 answer_start 이전 행만 학습에 사용 ----------------
    train_all = full_df[full_df["_timestamp"] < answer_start].reset_index(drop=True)
    if train_all.empty:
        raise RuntimeError("answer_start 이전 학습 데이터가 없습니다 (answer_start가 너무 이릅니다).")

    # ---- 요구사항 5: 조기 종료는 학습 구간 내부에서만 (정답 구간을 보지 않음) ----
    train_df, inner_valid_df = _time_group_safe_split(train_all, val_ratio=0.2, group_key=effective_group_key)

    try:
        ranker = LGBMRanker(params=config["lightgbm"]["params"], group_key=effective_group_key)
    except TypeError:
        ranker = LGBMRanker(params=config["lightgbm"]["params"])

    train_df_fit = _drop_timestamp_if_needed(train_df, needs_manual_timestamp_drop)
    inner_valid_fit = _drop_timestamp_if_needed(inner_valid_df, needs_manual_timestamp_drop) if len(inner_valid_df) else None

    ranker.train(train_df_fit, valid_df=inner_valid_fit)
    best_iteration = getattr(ranker.model, "best_iteration", None)

    top_k = config["recommendation"]["top_k"]
    reranker = create_reranker_from_config(config)
    category_map = FL.category_ids_by_newsletter(bundle)
    evaluator = Evaluator(k_values=[5, 10, 20])

    # ---- 1차(primary) 추론: point-in-time (loader #2 - 로그 자체를 answer_start 이전으로 물리적 제한) ----
    pit_bundle = FL.bundle_before(bundle, answer_start)
    loader2 = FL.build_file_data_loader(
        DataLoaderCls, NewsItemCls, config, pit_bundle, answer_start,
        label_mode=label_mode, candidate_pool_ids=candidate_ids, history_leakage_mode="fixed",
    )
    fe2 = FeatureEngineer(loader2)
    try:
        dataset2 = LGBMDataset(loader2, fe2, seed=args.seed)
    except TypeError:
        dataset2 = LGBMDataset(loader2, fe2)

    pit_inference_df = dataset2.create_inference_dataset(target_user_ids=None, eval_timestamp=answer_start)
    pit_inference_df = _drop_timestamp_if_needed(pit_inference_df, needs_manual_timestamp_drop)
    pit_scored_df = ranker.predict(pit_inference_df)
    n_distinct_primary = _n_distinct_scores(pit_scored_df)

    news_dict2 = loader2.load_embedded_news()
    pref_cats2 = loader2.load_user_preferred_categories()
    user_cat_counts2 = pref_cats2.groupby("user_id")["category_id"].count().to_dict()
    pit_recommendations = _build_recommendations(pit_scored_df, reranker, news_dict2, user_cat_counts2, top_k)

    ground_truth = bundle.ctr_logs
    if label_mode == FL.LabelMode.CLICKS_ONLY:
        ground_truth = ground_truth[ground_truth["is_clicked"] == 1]
    ground_truth = ground_truth[ground_truth["timestamp"] >= answer_start]
    ground_truth = ground_truth.groupby("user_id")["news_letter_id"].apply(set).to_dict()
    ground_truth = PR.restrict_ground_truth_to_pool(ground_truth, candidate_ids)

    pit_per_user = M.per_user_metrics(evaluator, pit_recommendations, ground_truth, category_map)
    pit_agg = M.aggregate(pit_per_user)

    clicks_before = bundle.ctr_logs[(bundle.ctr_logs["is_clicked"] == 1) & (bundle.ctr_logs["timestamp"] < answer_start)]
    cold_ids, warm_ids = PR.cold_warm_split(bundle.users["user_id"], clicks_before, answer_start)
    cold_metrics = PR.split_metrics_by_group(pit_per_user, cold_ids)
    warm_metrics = PR.split_metrics_by_group(pit_per_user, warm_ids)

    seen = PR.seen_items_by_user(clicks_before, answer_start)
    pit_recs_filtered = PR.filter_seen(pit_recommendations, seen)
    pit_per_user_filtered = M.per_user_metrics(evaluator, pit_recs_filtered, ground_truth, category_map)
    pit_agg_filtered = M.aggregate(pit_per_user_filtered)
    seen_share_top5 = PR.seen_share_in_topk(pit_recommendations, seen, k=5)

    # ---- 2차(as-written/leaky): 원래(미제한) loader #1/fe1/dataset1, eval_timestamp=pinned_now ----
    aw_inference_df = dataset1.create_inference_dataset(target_user_ids=None, eval_timestamp=pinned_now)
    aw_inference_df = _drop_timestamp_if_needed(aw_inference_df, needs_manual_timestamp_drop)
    aw_scored_df = ranker.predict(aw_inference_df)
    news_dict1 = loader1.load_embedded_news()
    pref_cats1 = loader1.load_user_preferred_categories()
    user_cat_counts1 = pref_cats1.groupby("user_id")["category_id"].count().to_dict()
    aw_recommendations = _build_recommendations(aw_scored_df, reranker, news_dict1, user_cat_counts1, top_k)

    # 순수 '추론 누출' 효과를 보려면 정답 구간은 1차와 동일해야 한다(모델도 동일 ranker).
    aw_per_user = M.per_user_metrics(evaluator, aw_recommendations, ground_truth, category_map)
    aw_agg = M.aggregate(aw_per_user)
    aw_seen_filtered = PR.filter_seen(aw_recommendations, seen)
    aw_per_user_filtered = M.per_user_metrics(evaluator, aw_seen_filtered, ground_truth, category_map)
    aw_agg_filtered = M.aggregate(aw_per_user_filtered)

    result = {
        "version": args.version,
        "protocol": "team_split",
        "label_mode": args.label_mode,
        "leakage_mode": args.leakage_mode,
        "candidate_pool": args.candidate_pool,
        "negative_source": args.negative_source,
        "objective_override": args.objective_override,
        "group_key_used": effective_group_key,
        "seed": args.seed,
        "top_k": top_k,
        "config_hash": config_hash,
        "config_overrides": cfg_overrides,
        "objective_used": config["lightgbm"]["params"].get("objective"),
        "code_sha": args.code_sha,
        "harness_sha": args.harness_sha,
        "n_train": len(train_df),
        "n_inner_valid": len(inner_valid_df),
        "n_candidates": len(candidate_ids) if candidate_ids is not None else len(bundle.newsletters),
        "answer_start": answer_start.isoformat(),
        "best_iteration": int(best_iteration) if best_iteration is not None else None,
        "n_distinct_scores_primary": n_distinct_primary,
        "n_cold": len(cold_ids),
        "n_warm": len(warm_ids),
        "seen_share_top5_primary": seen_share_top5,
        "primary": {
            "aggregate_metrics": pit_agg,
            "per_user_metrics": {str(k): v for k, v in pit_per_user.items()},
            "cold": cold_metrics,
            "warm": warm_metrics,
            "seen_filtered": {
                "aggregate_metrics": pit_agg_filtered,
                "per_user_metrics": {str(k): v for k, v in pit_per_user_filtered.items()},
            },
        },
        "as_written": {
            "aggregate_metrics": aw_agg,
            "per_user_metrics": {str(k): v for k, v in aw_per_user.items()},
            "seen_filtered": {
                "aggregate_metrics": aw_agg_filtered,
                "per_user_metrics": {str(k): v for k, v in aw_per_user_filtered.items()},
            },
        },
    }

    if args.version == "team-final":
        gt_written = bundle.ctr_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()
        tf_per_user = M.per_user_metrics(evaluator, aw_recommendations, gt_written, category_map)
        tf_agg = M.aggregate(tf_per_user)
        result["team_final_written"] = {
            "aggregate_metrics": tf_agg,
            "per_user_metrics": {str(k): v for k, v in tf_per_user.items()},
            "n_users_ground_truth": len(gt_written),
            "note": (
                "team-final의 실제 scripts/evaluate_results.py 정답 정의를 그대로 재현: "
                "is_clicked 필터 없이 created_at >= NOW()-6DAYS인 모든 행. 이 아카이브 구간(약 "
                "6시간16분)에서는 '최근 6일'이 로그 전체와 같다."
            ),
        }

    return result


def _run_generator_split(args, engine, bundle, pinned_now, config, config_hash, cfg_overrides, candidate_ids, label_mode, FL, M, PR) -> dict:
    DataLoaderCls, NewsItemCls = engine["DataLoader"], engine["NewsItem"]
    FeatureEngineer, LGBMDataset, LGBMRanker = engine["FeatureEngineer"], engine["LGBMDataset"], engine["LGBMRanker"]
    create_reranker_from_config, Evaluator = engine["create_reranker_from_config"], engine["Evaluator"]

    needs_manual_timestamp_drop = args.version in ("team-final", "fix-snapshot")
    effective_group_key = "user_id" if args.negative_source == "impression" else config["ranking"]["group_key"]

    # v2 요구사항 3 (BLOCKER 수정): 학습에 쓰이는 로그 자체를 ctr_logs_train.csv로
    # 제한한다 - v1은 여기서 정답만 valid 파일에서 가져오고 학습은 여전히 전체
    # synthetic_ctr_logs.csv(정답 뉴스레터의 상호작용 포함)로 했다.
    train_bundle = FL.bundle_for_generator_split_training(bundle)

    loader = FL.build_file_data_loader(
        DataLoaderCls, NewsItemCls, config, train_bundle, pinned_now,
        label_mode=label_mode, candidate_pool_ids=candidate_ids, history_leakage_mode=args.leakage_mode,
    )
    fe = FeatureEngineer(loader)
    try:
        dataset = LGBMDataset(loader, fe, seed=args.seed)
    except TypeError:
        dataset = LGBMDataset(loader, fe)

    neg_ratio = config["lightgbm"].get("negative_sample_ratio", 5)
    if args.negative_source == "random":
        full_df = dataset.create_train_dataset(neg_ratio=neg_ratio)
    else:
        full_df = _build_impression_negative_dataset(fe, train_bundle, label_mode)
    if full_df.empty:
        raise RuntimeError("학습 데이터가 비어 있습니다 (generator_split).")

    # 이 프로토콜은 뉴스레터 id 자체로 train/valid가 나뉘어 있어 '고정 시각'
    # 경계가 없다 - inner-validation도 학습 구간 자체의 시간순으로만 나눈다.
    train_df, inner_valid_df = _time_group_safe_split(full_df, val_ratio=0.2, group_key=effective_group_key)

    try:
        ranker = LGBMRanker(params=config["lightgbm"]["params"], group_key=effective_group_key)
    except TypeError:
        ranker = LGBMRanker(params=config["lightgbm"]["params"])

    train_df_fit = _drop_timestamp_if_needed(train_df, needs_manual_timestamp_drop)
    inner_valid_fit = _drop_timestamp_if_needed(inner_valid_df, needs_manual_timestamp_drop) if len(inner_valid_df) else None
    ranker.train(train_df_fit, valid_df=inner_valid_fit)
    best_iteration = getattr(ranker.model, "best_iteration", None)

    # 학습 로그 자체에 정답 뉴스레터의 상호작용이 전혀 없으므로(train_bundle이 이미
    # 배제했다), eval_timestamp를 answer_start로 더 좁힐 필요가 없다 - 어차피 볼 수
    # 있는 정보가 없다. 팀 실제 배치(inference_pipeline debug 모드)와 동일하게
    # 데이터셋 끝 시각을 쓴다.
    inference_df = dataset.create_inference_dataset(target_user_ids=None, eval_timestamp=pinned_now)
    inference_df = _drop_timestamp_if_needed(inference_df, needs_manual_timestamp_drop)
    scored_df = ranker.predict(inference_df)
    n_distinct_primary = _n_distinct_scores(scored_df)

    top_k = config["recommendation"]["top_k"]
    reranker = create_reranker_from_config(config)
    news_dict = loader.load_embedded_news()
    pref_cats = loader.load_user_preferred_categories()
    user_cat_counts = pref_cats.groupby("user_id")["category_id"].count().to_dict()
    recommendations = _build_recommendations(scored_df, reranker, news_dict, user_cat_counts, top_k)

    gt_logs = bundle.generator_valid_keys
    if label_mode == FL.LabelMode.CLICKS_ONLY:
        gt_logs = gt_logs[gt_logs["is_clicked"] == 1]
    ground_truth = gt_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()
    ground_truth = PR.restrict_ground_truth_to_pool(ground_truth, candidate_ids)

    category_map = FL.category_ids_by_newsletter(bundle)
    evaluator = Evaluator(k_values=[5, 10, 20])
    per_user = M.per_user_metrics(evaluator, recommendations, ground_truth, category_map)
    agg = M.aggregate(per_user)

    return {
        "version": args.version,
        "protocol": "generator_split",
        "label_mode": args.label_mode,
        "leakage_mode": args.leakage_mode,
        "candidate_pool": args.candidate_pool,
        "negative_source": args.negative_source,
        "objective_override": args.objective_override,
        "group_key_used": effective_group_key,
        "seed": args.seed,
        "top_k": top_k,
        "config_hash": config_hash,
        "config_overrides": cfg_overrides,
        "objective_used": config["lightgbm"]["params"].get("objective"),
        "code_sha": args.code_sha,
        "harness_sha": args.harness_sha,
        "n_train": len(train_df),
        "n_inner_valid": len(inner_valid_df),
        "n_train_source_newsletters": int(train_bundle.ctr_logs["news_letter_id"].nunique()),
        "n_candidates": len(candidate_ids) if candidate_ids is not None else len(bundle.newsletters),
        "answer_start": None,
        "best_iteration": int(best_iteration) if best_iteration is not None else None,
        "n_distinct_scores_primary": n_distinct_primary,
        "primary": {
            "aggregate_metrics": agg,
            "per_user_metrics": {str(k): v for k, v in per_user.items()},
        },
    }


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
    parser.add_argument("--answer-start", default=None, help="team_split 프로토콜의 고정 정답 구간 시작 시각 (ISO 8601)")
    parser.add_argument("--code-sha", default="unknown", help="이 버전 엔진 코드의 git SHA (run_repro.py가 계산)")
    parser.add_argument("--harness-sha", default="unknown", help="이 하네스(evaluation/recsys/team_repro) 자체의 git SHA")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result = run_one(args)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in ("primary", "as_written", "team_final_written")}
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
