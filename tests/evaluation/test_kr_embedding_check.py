"""한국어 임베딩 점검(ADR 0031 결정 5)의 단위 테스트 - 합성 벡터와 지어낸 반출본만 쓴다(실제 기사 없음)."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from evaluation.recsys import kr_embedding_check as kr
from evaluation.recsys.ebnerd.embedding_sanity import knn_category_accuracy

REPO = Path(__file__).resolve().parents[2]


def _unit(rows):
    x = np.asarray(rows, dtype=np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def _clusters(n_per, n_cls, dim=16, noise=0.6, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_cls, dim))
    x = np.concatenate([c + noise * rng.normal(size=(n_per, dim)) for c in centers]).astype(np.float32)
    y = np.repeat(np.arange(n_cls), n_per)
    return x, y


# ------------------------------------------------------------------ 라벨


@pytest.mark.parametrize("press,url,section", [
    ("매일경제", "https://www.mk.co.kr/news/economy/11234567", "economy"),
    ("매일경제", "https://www.mk.co.kr/news/realestate/11234567/", "realestate"),
    ("동아일보", "https://www.donga.com/news/Politics/article/all/20261012/132000000/2", "Politics"),
    ("동아일보", "https://www.donga.com/news/Inter/article/all/20261012/132000001/1", "Inter"),
    # 다른 모양, 다른 호스트, 규칙 없는 언론사, 공백이 든 섹션
    ("매일경제", "https://www.mk.co.kr/news/economy/", None),
    ("매일경제", "https://m.mk.co.kr/news/economy/11234567", None),
    ("동아일보", "https://www.donga.com/news/K-TECH Global Leaders/article/all/20261012/1/1", None),
    ("경향신문", "https://www.khan.co.kr/article/202610121234001", None),
    ("한국경제", "https://www.hankyung.com/article/2026101212341", None),
])
def test_section_label_reads_only_the_registered_url_shapes(press, url, section):
    assert kr.section_label(press, url) == section


def test_every_mapped_section_goes_to_one_of_the_seven_service_categories():
    assert len(kr.SERVICE_CATEGORIES) == 7
    for press, mapping in kr.SERVICE_MAP.items():
        assert press in kr.SECTION_RULES
        assert set(mapping.values()) <= set(kr.SERVICE_CATEGORIES)
    # 1차에서 빼는 섹션은 대응이 없다
    for section in ("Opinion", "People", "Entertainment", "list"):
        assert kr.service_category("동아일보", section) is None
    assert kr.service_category("매일경제", None) is None
    assert kr.service_category("경향신문", "economy") is None


def test_service_categories_match_the_category_table_in_the_schema_seed():
    text = (REPO / "ai_workspace" / "db" / "schema.py").read_text(encoding="utf-8")
    for name in kr.SERVICE_CATEGORIES:
        assert f"('{name}'," in text


# ------------------------------------------------------------------ kNN


def test_knn_predict_agrees_with_the_danish_check_on_clustered_vectors():
    x, y = _clusters(40, 5, noise=1.2, seed=3)
    pred, _ = kr.knn_predict(x, x, y, 10, self_index=np.arange(len(y)), chunk=37)
    danish = knn_category_accuracy(x, y, k=10, chunk=50)
    assert np.array_equal(pred == y, danish["per_item_correct"])
    assert 0.3 < danish["accuracy"] < 1.0  # 쉬운 경우만 보는 테스트가 아니다


def test_separated_clusters_are_all_correct():
    x, y = _clusters(30, 4, noise=0.05, seed=1)
    res = kr.evaluate(x, [f"c{i}" for i in y], np.arange(len(y)), k=10, n_boot=50)
    assert res["accuracy"] == 1.0 and res["macro_f1"] == 1.0
    assert res["ci95"]["accuracy"] == [1.0, 1.0]
    assert all(c["f1"] == 1.0 for c in res["per_class"])


def test_leave_one_out_excludes_self_and_ties_go_to_the_nearest_neighbour():
    # 쌍둥이 두 쌍: k=1이면 자기 자신이 아니라 쌍둥이의 라벨을 받는다
    x = _unit([[1, 0], [1, 0.01], [0, 1], [0.01, 1]])
    pred, top1 = kr.knn_predict(x, x, np.array([0, 1, 1, 0]), 1, self_index=np.arange(4))
    assert pred.tolist() == [1, 0, 0, 1]
    assert np.all(top1 > 0.99) and np.all(top1 < 1.0)
    # k=2에서 1:1 동률이면 더 가까운 이웃의 라벨
    x = _unit([[1, 0], [1, 0.05], [1, 0.3], [0, 1]])
    pred, _ = kr.knn_predict(x, x, np.array([0, 1, 0, 1]), 2, self_index=np.arange(4))
    assert pred[0] == 1  # 이웃 1(라벨 1)이 2(라벨 0)보다 가깝다


def test_near_duplicate_neighbours_can_be_excluded():
    # 질의 0의 쌍둥이(코사인 ≈ 1)는 라벨 1, 그다음 이웃은 라벨 0
    x = _unit([[1, 0], [1, 0.001], [1, 0.4], [0, 1]])
    labels = np.array([0, 1, 0, 1])
    with_dup, _ = kr.knn_predict(x, x, labels, 1, self_index=np.arange(4))
    without, top1 = kr.knn_predict(x, x, labels, 1, self_index=np.arange(4), max_cos=0.95)
    assert with_dup[0] == 1 and without[0] == 0
    assert top1[0] < 0.95


def test_cross_reference_prediction_uses_only_the_reference_rows():
    xq = _unit([[1, 0.1], [0.1, 1]])
    xr = _unit([[1, 0], [0, 1], [0.9, 0.2]])
    pred, _ = kr.knn_predict(xq, xr, np.array([0, 1, 0]), 1)
    assert pred.tolist() == [0, 1]


def test_too_few_neighbours_is_an_error():
    x = _unit([[1, 0], [0, 1]])
    with pytest.raises(kr.CheckError):
        kr.knn_predict(x, x, np.array([0, 1]), 2, self_index=np.arange(2))
    with pytest.raises(kr.CheckError):
        kr.knn_predict(np.zeros((1, 2)), x, np.array([0, 1]), 1)


# ------------------------------------------------------------------ 지표


def test_classification_metrics_hand_computed():
    y = np.array([0, 0, 0, 1, 1, 2])
    p = np.array([0, 0, 1, 1, 0, 0])
    m = kr.classification_metrics(y, p, 3)
    assert m["accuracy"] == pytest.approx(3 / 6)
    # 0: 정밀도 2/4, 재현율 2/3, F1 4/7. 1: 1/2, 1/2, 1/2. 2: 예측 0개 -> 0.
    assert m["f1"].tolist() == pytest.approx([4 / 7, 0.5, 0.0])
    assert m["macro_f1"] == pytest.approx((4 / 7 + 0.5 + 0.0) / 3)
    assert m["confusion"].tolist() == [[2, 1, 0], [1, 1, 0], [1, 0, 0]]


def test_bootstrap_is_seeded_and_brackets_the_point_estimate():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 4, 500)
    p = np.where(rng.random(500) < 0.7, y, rng.integers(0, 4, 500))
    a = kr.bootstrap_ci(y, p, 4, n_boot=200, seed=0)
    b = kr.bootstrap_ci(y, p, 4, n_boot=200, seed=0)
    assert a == b
    acc = float((y == p).mean())
    assert a["accuracy"][0] < acc < a["accuracy"][1]
    assert a["accuracy"][1] - a["accuracy"][0] < 0.12


def test_baselines_match_their_closed_forms():
    y = np.array([0] * 60 + [1] * 30 + [2] * 10)
    b = kr.baselines(y, 3, draws=400, seed=0, names=["a", "b", "c"])
    assert b["majority"]["label"] == "a" and b["majority"]["accuracy"] == 0.6
    assert b["majority"]["macro_f1"] == pytest.approx(2 * 0.6 / 1.6 / 3)
    assert b["random_uniform"]["accuracy_mean"] == pytest.approx(1 / 3, abs=0.02)
    assert b["random_prior"]["accuracy_mean"] == pytest.approx(0.36 + 0.09 + 0.01, abs=0.02)


def test_verdict_needs_threshold_majority_and_500_items():
    def primary(n, acc, major, lo):
        return {"n": n, "accuracy": acc, "ci95": {"accuracy": [lo, acc]},
                "baselines": {"majority": {"accuracy": major}}}

    assert kr.verdict(primary(600, 0.7, 0.5, 0.65))["met"] is True
    assert kr.verdict(primary(600, 0.7, 0.5, 0.55))["ci_lower_ge_threshold"] is False
    assert kr.verdict(primary(600, 0.59, 0.3, 0.55))["met"] is False
    assert kr.verdict(primary(600, 0.65, 0.66, 0.6))["met"] is False
    assert kr.verdict(primary(499, 0.9, 0.3, 0.85))["met"] is False


# ------------------------------------------------------------------ 반출본 읽기와 산출물


SECRET_TITLE = "지어낸제목문자열"
SECRET_BODY = "지어낸본문문자열입니다"


def _fake_export(root: Path, n_per: int = 12, seed: int = 0) -> Path:
    """news-embedding-pack/1과 frozen-news-export/1의 모양만 흉내 낸 반출본(지어낸 문자열, 기사 아님)."""
    export = root / "20260101T000000Z"
    (export / "embeddings").mkdir(parents=True)
    mk = ["politics", "economy", "it"]
    donga = ["Politics", "Economy", "It", "Opinion"]
    rows, vecs = [], []
    rng = np.random.default_rng(seed)
    centers = {"정치": rng.normal(size=8), "경제": rng.normal(size=8), "IT/과학": rng.normal(size=8),
               None: rng.normal(size=8)}
    next_id = 100
    for press, sections, host in (("매일경제", mk, "www.mk.co.kr"), ("동아일보", donga, "www.donga.com"),
                                  ("경향신문", [None], "www.khan.co.kr")):
        for sec in sections:
            for _ in range(n_per):
                cat = kr.service_category(press, sec)
                url = (f"https://{host}/news/{sec}/{next_id}" if press == "매일경제" else
                       f"https://{host}/news/{sec}/article/all/20260101/{next_id}/1" if press == "동아일보" else
                       f"https://{host}/article/{next_id}")
                rows.append({"id": next_id, "press": press, "url": url, "title": SECRET_TITLE,
                             "body": SECRET_BODY, "body_chars": len(SECRET_BODY)})
                vecs.append(centers[cat] + 0.3 * rng.normal(size=8))
                next_id += 1
    emb = np.asarray(vecs, dtype=np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    order = rng.permutation(len(rows))  # 팩의 순서가 반출 행의 순서와 달라도 id로 맞춘다
    np.save(export / "embeddings" / "emb.f16.npy", emb[order].astype(np.float16))
    np.save(export / "embeddings" / "ids.npy", ids[order])
    (export / "articles.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                           encoding="utf-8")

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    by_press = {}
    for r in rows:
        by_press[r["press"]] = by_press.get(r["press"], 0) + 1
    identity = "f" * 64
    (export / "manifest.json").write_text(json.dumps({
        "format": "frozen-news-export/1", "identity_sha256": identity,
        "identity": {"rows": len(rows), "by_press": by_press,
                     "window_utc": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
                     "crawled_at_range_utc": ["2026-01-01T00:00:00Z", "2026-01-01T23:00:00Z"]},
        "files": {"articles.jsonl": {"sha256": sha(export / "articles.jsonl")}},
    }), encoding="utf-8")
    (export / "embeddings" / "embeddings_manifest.json").write_text(json.dumps({
        "format": "news-embedding-pack/1", "rows": len(rows), "dim": 8, "dtype": "float16",
        "export_identity_sha256": identity,
        "model": {"name": "fake", "revision": "0" * 40, "max_length": 8192, "text_rule": "t",
                  "l2_normalize": True, "precision": "fp16"},
        "files": {"emb.f16.npy": {"sha256": sha(export / "embeddings" / "emb.f16.npy")},
                  "ids.npy": {"sha256": sha(export / "embeddings" / "ids.npy")}},
    }), encoding="utf-8")
    return export


def test_a_run_on_a_fake_export_carries_no_title_body_or_url(tmp_path):
    export = _fake_export(tmp_path)
    result = kr.run(export, command="test", n_boot=50)
    text = kr.dump_json(result) + kr.render_markdown(result)
    assert SECRET_TITLE not in text and SECRET_BODY not in text
    assert "https://" not in text and "article/all/20260101" not in text and "/news/politics/1" not in text
    pr = result["primary"]
    # 매일경제 3섹션 + 동아일보 3섹션(Opinion 제외)이 3개 범주로
    assert pr["n"] == 6 * 12 and pr["classes"] == sorted(["IT/과학", "경제", "정치"])
    assert pr["accuracy"] > 0.9 and pr["verdict"]["n_ge_500"] is False and pr["verdict"]["met"] is False
    assert pr["by_group"]["매일경제"]["n"] == 36 and pr["by_group"]["동아일보"]["n"] == 36
    assert result["label_coverage"]["동아일보"]["unmapped_sections"] == ["Opinion"]
    assert result["label_coverage"]["경향신문"]["with_section"] == 0
    assert result["press_label"]["n"] == (3 + 4 + 1) * 12
    assert set(result["cross_press"]) == {"동아일보<-매일경제", "매일경제<-동아일보"}
    assert result["sensitivity"]["near_dup_excluded"]["top1_cos"]["share_ge_near_dup"] == 0.0
    assert result["meta"]["data"]["pack"]["title_only_vectors"] is False
    assert json.loads(kr.dump_json(result)) == json.loads(json.dumps(result, ensure_ascii=False))


def test_inputs_that_do_not_match_their_manifests_are_refused(tmp_path):
    export = _fake_export(tmp_path)
    kr.load_inputs(export)  # 맞는 반출본은 읽힌다
    with (export / "articles.jsonl").open("a", encoding="utf-8") as f:
        f.write("\n")
    with pytest.raises(kr.CheckError, match="articles.jsonl"):
        kr.load_inputs(export)


def test_a_pack_from_another_export_is_refused(tmp_path):
    export = _fake_export(tmp_path)
    path = export / "embeddings" / "embeddings_manifest.json"
    pman = json.loads(path.read_text(encoding="utf-8"))
    pman["export_identity_sha256"] = "0" * 64
    path.write_text(json.dumps(pman), encoding="utf-8")
    with pytest.raises(kr.CheckError, match="신원"):
        kr.load_inputs(export)


def test_the_committed_report_is_the_render_of_the_committed_json():
    base = REPO / "reports" / "kr" / "embedding_check_v1"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    if not json_path.exists():
        pytest.skip("리포트가 아직 없다")
    result = json.loads(json_path.read_text(encoding="utf-8"))
    assert kr.render_markdown(result) == md_path.read_text(encoding="utf-8")
    assert result["meta"]["format"] == kr.FORMAT and result["meta"]["code"]["dirty"] is False
    assert result["meta"]["n_boot"] == kr.N_BOOT
    assert result["meta"]["params"] == json.loads(json.dumps(kr.params(), ensure_ascii=False))
