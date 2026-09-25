"""EB-NeRD demo 로더 테스트. 데이터는 라이선스상 이 Mac에만 있으므로 없으면(CI) skip."""
import numpy as np
import pandas as pd
import pytest

from evaluation.recsys.ebnerd.loaders import (
    click_events,
    ebnerd_root,
    inview_events,
    load_articles,
    load_history_events,
    load_impressions,
)

DEMO = ebnerd_root() / "ebnerd_demo"
pytestmark = pytest.mark.skipif(not (DEMO / "articles.parquet").exists(),
                                reason="EB-NeRD demo 데이터 없음 (로컬 전용)")


@pytest.fixture(scope="module")
def raw_train():
    return pd.read_parquet(DEMO / "train" / "behaviors.parquet")


@pytest.fixture(scope="module")
def imp():
    return load_impressions(DEMO, "train")


def test_impressions_csr_matches_raw_lists(imp, raw_train):
    assert len(imp) == len(raw_train)
    assert imp.inview_ptr[-1] == raw_train["article_ids_inview"].map(len).sum()
    raw = raw_train.set_index("impression_id")
    for i in [0, len(imp) // 2, len(imp) - 1]:
        row = raw.loc[imp.impression_id[i]]
        inview = imp.inview_article[imp.inview_ptr[i]:imp.inview_ptr[i + 1]]
        assert inview.tolist() == list(row["article_ids_inview"])
        clicked = inview[imp.inview_clicked[imp.inview_ptr[i]:imp.inview_ptr[i + 1]]]
        assert set(clicked.tolist()) == set(row["article_ids_clicked"])
        assert imp.time[i] == int(pd.Timestamp(row["impression_time"]).timestamp())


def test_impressions_are_time_sorted(imp):
    assert np.all(np.diff(imp.time) >= 0)


def test_every_impression_has_a_click_inside_inview(imp):
    n_clicked = np.add.reduceat(imp.inview_clicked.astype(int), imp.inview_ptr[:-1])
    assert (n_clicked >= 1).mean() > 0.99


def test_subset_keeps_rows_aligned(imp):
    sub = imp.subset(np.array([5, 2]))
    assert sub.impression_id.tolist() == [imp.impression_id[5], imp.impression_id[2]]
    assert sub.inview_article[sub.inview_ptr[1]:sub.inview_ptr[2]].tolist() == \
        imp.inview_article[imp.inview_ptr[2]:imp.inview_ptr[3]].tolist()


def test_history_events_precede_behaviour_window(imp):
    hist = load_history_events(DEMO, "train")
    assert hist["time"].max() < imp.time.min()
    assert hist["user_id"].isin(imp.user_id).all()


def test_click_and_inview_events_counts(imp):
    clicks = click_events(imp)
    views = inview_events(imp)
    assert len(clicks) == imp.clicked_ptr[-1]
    assert len(views) == imp.inview_ptr[-1]


def test_articles_loader_skips_text_columns():
    arts = load_articles(DEMO)
    assert "body" not in arts and "title" not in arts
    assert arts["article_id"].is_unique
    assert arts["published_ts"].dtype == np.int64
