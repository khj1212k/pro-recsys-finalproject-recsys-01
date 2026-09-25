"""evaluation/recsys/team_repro/categories.py 테스트.

data/team_archive는 gitignore 대상이라 CI 환경에 없을 수 있다 - 그 경우 이 모듈의
모든 테스트를 스킵한다(가짜 데이터로 대체하면 "실제 아카이브를 정확히 읽는가"라는
테스트의 목적 자체가 사라지므로).
"""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "evaluation" / "recsys" / "team_repro"))

import categories as C  # noqa: E402

pytestmark = pytest.mark.skipif(
    not C.DATA_ROOT.exists(), reason="data/team_archive가 없는 환경 (gitignored 아카이브 데이터)"
)


def test_category_id_schemes_are_distinct_and_documented():
    """config.yaml 순서와 schema.py 순서가 실제로 다르다는 전제 자체를 고정한다
    (모듈 docstring이 설명하는 불일치). 둘이 같아지면 docstring이 거짓이 된다."""
    assert C.CATEGORY_ID_TO_NAME != C.CATEGORY_ID_TO_NAME_SCHEMA_PY
    assert C.CATEGORY_ID_TO_NAME[2] == "경제"
    assert C.CATEGORY_ID_TO_NAME_SCHEMA_PY[2] == "사회"
    assert set(C.CATEGORY_ID_TO_NAME) == set(range(1, 8))


def test_load_newsletters_returns_195_unique_ids():
    newsletters = C.load_newsletters()
    ids = [n.news_letter_id for n in newsletters]
    assert len(ids) == 195
    assert len(set(ids)) == 195


def test_load_embeddings_matches_newsletter_ids():
    newsletters = C.load_newsletters()
    embeddings, meta = C.load_embeddings()
    assert meta["n"] == 195
    assert meta["l2_normalized"] is True
    ids = {n.news_letter_id for n in newsletters}
    assert set(embeddings) == ids
    # L2 정규화 확인 (norm ~= 1.0)
    sample = next(iter(embeddings.values()))
    assert abs(np.linalg.norm(sample) - 1.0) < 1e-3


def test_direct_labels_from_onboarding_have_no_internal_conflicts_after_filtering():
    labels = C.direct_labels_from_onboarding()
    # 42건이 명확한 단일 카테고리로 라벨링됨 (실측 확인된 값)
    assert len(labels) == 42
    assert set(labels.values()) <= set(range(1, 8))


def test_direct_labels_from_json_only_accepts_unambiguous_canonical_categories():
    newsletters = C.load_newsletters()
    labels = C.direct_labels_from_json(newsletters)
    # 겹치는 제목 수는 적지만(19건 관측) canonical 카테고리가 정확히 1개인 것만 채택되므로
    # 그보다 적거나 같아야 한다.
    assert 0 < len(labels) <= 19
    for nid, cat in labels.items():
        assert cat in C.CATEGORY_ID_TO_NAME


def test_combine_direct_labels_prefers_onboarding_on_conflict():
    json_labels = {1: 1, 2: 2}
    onboarding_labels = {1: 3}  # id=1 충돌 -> onboarding(3) 우선
    combined, source, disagree = C.combine_direct_labels(json_labels, onboarding_labels)
    assert combined[1] == 3
    assert combined[2] == 2
    assert disagree == 1
    assert source[1].startswith("onboarding_log")
    assert source[2] == "json_title"


def test_knn_predict_one_returns_majority_label_among_k_nearest():
    # 간단한 합성 벡터로 kNN 로직 자체를 검증 (아카이브 데이터와 무관)
    bank = np.array(
        [
            [1.0, 0.0],
            [0.9, 0.1],
            [0.0, 1.0],
        ]
    )
    bank = bank / np.linalg.norm(bank, axis=1, keepdims=True)
    labels = [1, 1, 2]
    query = np.array([1.0, 0.0])
    query = query / np.linalg.norm(query)
    pred, conf = C.knn_predict_one(query, bank, labels, k=3)
    assert pred == 1
    assert conf == pytest.approx(2 / 3)


def test_loo_accuracy_is_1_for_perfectly_separable_clusters():
    rng = np.random.default_rng(0)
    cluster_a = rng.normal(loc=[5, 0], scale=0.01, size=(10, 2))
    cluster_b = rng.normal(loc=[-5, 0], scale=0.01, size=(10, 2))
    vecs = np.vstack([cluster_a, cluster_b])
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    labels = [1] * 10 + [2] * 10
    acc = C.loo_accuracy_for_k(vecs, labels, k=3)
    assert acc == 1.0


def test_choose_k_picks_smallest_k_on_tie():
    rng = np.random.default_rng(1)
    vecs = rng.normal(size=(20, 4))
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    labels = [1] * 20  # 전부 같은 라벨이면 어떤 k든 LOO 정확도가 항상 1.0
    best_k, scores = C.choose_k(vecs, labels, k_grid=[1, 3, 5])
    assert best_k == 1
    assert all(v == 1.0 for v in scores.values())


def test_recover_categories_end_to_end_labels_all_195_and_reports_loo():
    result = C.recover_categories(k_grid=[1, 3, 5, 7])
    assert len(result.rows) == 195
    assert all(r["category_id"] is not None for r in result.rows)
    assert set(result.source_counts) <= {"knn", "onboarding_log", "json_title"}
    # LOO 정확도가 랜덤(1/7 ~= 0.143)보다는 유의미하게 높아야 한다.
    assert result.loo_scores[result.chosen_k] > 1 / 7
