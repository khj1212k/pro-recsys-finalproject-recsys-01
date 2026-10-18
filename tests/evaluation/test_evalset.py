import json
from collections import Counter

import pytest

from evaluation.llm import evalset as es


def _cand(run_id, cluster_id, ids, category="경제", split_v2="no", hard=False):
    return es.ClusterCandidate(
        run_id=run_id,
        cluster_id=cluster_id,
        article_ids=list(ids),
        category=category,
        split_v2=split_v2,
        hard_case=hard,
    )


def _articles(ids):
    return {
        i: es.ArticleRecord(
            raw_news_id=i, url=f"https://news.example/{i}", press_name="테스트일보",
            title=f"제목 {i}", body=f"본문 {i} " * 20,
        )
        for i in ids
    }


def _pool():
    """size 버킷 3종 × 카테고리 2종, 어려운 사례 6개가 섞인 합성 후보 풀.
    기사 id는 클러스터마다 겹치지 않게 1000 단위로 떼어 둔다."""
    cands = []
    cid = 0
    for size, count in [(3, 30), (6, 20), (12, 6)]:
        for k in range(count):
            base = cid * 1000
            cands.append(_cand(1, cid, range(base, base + size),
                               category="경제" if k % 2 else "IT/과학",
                               split_v2="yes" if k % 5 == 0 else "no",
                               hard=(k < 2)))
            cid += 1
    return cands


def test_size_bucket_boundaries():
    assert es.size_bucket(3) == "3-4"
    assert es.size_bucket(4) == "3-4"
    assert es.size_bucket(5) == "5-9"
    assert es.size_bucket(9) == "5-9"
    assert es.size_bucket(10) == "10+"
    assert es.size_bucket(2) is None


def test_sampling_is_deterministic_for_a_seed_and_changes_with_seed():
    pool = _pool()
    a = es.stratified_sample(pool, n=20, seed=7, hard_fraction=0.2, warmup=2)
    b = es.stratified_sample(pool, n=20, seed=7, hard_fraction=0.2, warmup=2)
    c = es.stratified_sample(pool, n=20, seed=8, hard_fraction=0.2, warmup=2)

    key = lambda s: [(x.candidate.run_id, x.candidate.cluster_id, x.split) for x in s]
    assert key(a) == key(b)
    assert key(a) != key(c)


def test_hard_case_quota_is_filled_from_cluster_evaluator_failures():
    sample = es.stratified_sample(_pool(), n=20, seed=1, hard_fraction=0.2, warmup=0)
    hard = [s for s in sample if s.split == "eval" and s.candidate.hard_case]
    assert len(hard) == 4


def test_hard_cases_are_added_on_top_of_n_regular_clusters():
    # 어려운 사례는 ROC용 클러스터 라벨 전용이라 생성하지 않는다 - 생성·발행률 표본(n)을
    # 줄이지 않도록 n개와 별도로 뽑는다 (ADR 0009 A5)
    sample = es.stratified_sample(_pool(), n=20, seed=1, hard_fraction=0.2, warmup=0)
    evals = [s for s in sample if s.split == "eval"]
    assert sum(not s.candidate.hard_case for s in evals) == 20
    assert sum(s.candidate.hard_case for s in evals) == 4


def test_hard_case_quota_shrinks_when_not_enough_failures_exist():
    pool = [c for c in _pool() if not c.hard_case] + [_cand(9, 999, range(900000, 900003), hard=True)]
    sample = es.stratified_sample(pool, n=20, seed=1, hard_fraction=0.5, warmup=0)
    assert sum(s.candidate.hard_case for s in sample) == 1
    assert len(sample) == 21  # 일반 20개는 그대로, 어려운 사례만 있는 만큼(1개)


def test_every_size_bucket_is_represented_even_when_rare():
    sample = es.stratified_sample(_pool(), n=12, seed=3, hard_fraction=0.0, warmup=0)
    buckets = Counter(es.size_bucket(len(s.candidate.article_ids)) for s in sample)
    assert set(buckets) == {"3-4", "5-9", "10+"}
    # 균등 배분: 40/3처럼 나눠떨어지지 않아도 버킷 간 차이는 1 이하
    assert max(buckets.values()) - min(buckets.values()) <= 1


def test_warmup_clusters_are_disjoint_from_the_eval_set():
    sample = es.stratified_sample(_pool(), n=20, seed=2, hard_fraction=0.2, warmup=3)
    evals = {(s.candidate.run_id, s.candidate.cluster_id) for s in sample if s.split == "eval"}
    warm = {(s.candidate.run_id, s.candidate.cluster_id) for s in sample if s.split == "warmup"}
    assert len(evals) == 24 and len(warm) == 3  # 일반 20 + 어려운 사례 4
    assert not evals & warm


def test_clusters_sharing_articles_are_never_both_selected():
    # 실패한 클러스터의 기사는 다음 실행에서 다시 군집될 수 있다(news_letter_id NULL)
    pool = [_cand(1, 1, [1, 2, 3]), _cand(2, 1, [2, 3, 4]), _cand(2, 2, [10, 11, 12]), _cand(3, 1, [20, 21, 22])]
    sample = es.stratified_sample(pool, n=4, seed=0, hard_fraction=0.0, warmup=0)
    seen = []
    for s in sample:
        assert not set(s.candidate.article_ids) & set(seen)
        seen.extend(s.candidate.article_ids)
    assert len(sample) == 3


def test_clusters_below_min_size_are_excluded():
    pool = [_cand(1, 1, [1, 2])] + _pool()
    sample = es.stratified_sample(pool, n=10, seed=0, hard_fraction=0.0, warmup=0)
    assert all(len(s.candidate.article_ids) >= 3 for s in sample)


def test_candidates_from_cluster_log_parse_meta_outcomes_and_skip_non_cluster_keys():
    log = {
        "0": [1, 2, 3],
        "1": [4, 5, 6, 7],
        "clustering_stats": {"n_articles": 7},
        "cluster_meta": {"0": {"split_v2": True}, "1": {"split_v2": False}},
        "cluster_outcomes": {"0": {"status": "skipped", "cluster_eval": {"decision": "FAIL", "confidence": 0.8}}},
    }
    cands = es.candidates_from_cluster_log(run_id=5, cluster_log=log, categories_by_cluster={1: "경제"})

    by_id = {c.cluster_id: c for c in cands}
    assert set(by_id) == {0, 1}
    assert by_id[0].split_v2 == "yes" and by_id[0].hard_case is True
    assert by_id[1].split_v2 == "no" and by_id[1].hard_case is False
    assert by_id[1].category == "경제" and by_id[0].category == es.UNCATEGORIZED


def test_fail_without_a_verdict_is_not_a_hard_case():
    """평가 호출이 막혔거나(llm_failure) 응답이 해석되지 않은(parsed=False) FAIL은 ClusterEvaluator의 판정이 아니다.
    두 키가 없는 이전 행은 예전대로 decision만으로 읽는다."""
    fail = {"decision": "FAIL", "confidence": 0.8}
    log = {
        "0": [1, 2, 3], "1": [4, 5, 6], "2": [7, 8, 9], "3": [10, 11, 12],
        "cluster_outcomes": {
            "0": {"status": "skipped", "cluster_eval": dict(fail)},
            "1": {"status": "failed", "cluster_eval": {**fail, "llm_failure": "infra"}},
            "2": {"status": "skipped", "cluster_eval": {**fail, "parsed": False}},
            "3": {"status": "completed", "cluster_eval": {"decision": "PASS", "confidence": 0.9}},
        },
    }

    hard = {c.cluster_id: c.hard_case for c in es.candidates_from_cluster_log(run_id=5, cluster_log=log)}

    assert hard == {0: True, 1: False, 2: False, 3: False}


def test_old_cluster_logs_without_meta_are_unknown_not_guessed():
    cands = es.candidates_from_cluster_log(run_id=1, cluster_log=json.dumps({"3": [1, 2, 3]}))
    assert cands[0].split_v2 == "unknown"
    assert cands[0].hard_case is False


def test_write_keeps_bodies_out_of_the_committed_manifest(tmp_path):
    pool = _pool()
    articles = _articles([i for c in pool for i in c.article_ids])
    sample = es.stratified_sample(pool, n=6, seed=0, hard_fraction=0.0, warmup=1)

    manifest_path = tmp_path / "evalsets" / "t.jsonl"
    bodies_path = tmp_path / "data" / "t" / "articles.jsonl"
    es.write_evalset(sample, articles, manifest_path=manifest_path, bodies_path=bodies_path,
                     meta={"seed": 0})

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "본문" not in manifest_text and "제목" not in manifest_text
    rows = [json.loads(line) for line in manifest_text.splitlines()]
    assert len(rows) == 7
    art = rows[0]["articles"][0]
    assert set(art) == {"raw_news_id", "url", "press_name", "body_sha256", "title_sha256", "body_chars"}

    items = es.load_evalset(manifest_path, bodies_path)
    assert [i.item_id for i in items] == [r["item_id"] for r in rows]
    assert items[0].articles[0].body.startswith("본문")


def test_load_evalset_rejects_bodies_whose_hash_changed(tmp_path):
    pool = _pool()[:3]
    articles = _articles([i for c in pool for i in c.article_ids])
    sample = es.stratified_sample(pool, n=3, seed=0, hard_fraction=0.0, warmup=0)
    manifest_path, bodies_path = tmp_path / "m.jsonl", tmp_path / "b.jsonl"
    es.write_evalset(sample, articles, manifest_path=manifest_path, bodies_path=bodies_path, meta={})

    lines = bodies_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["body"] = row["body"] + " 수정됨"
    lines[0] = json.dumps(row, ensure_ascii=False)
    bodies_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(es.EvalsetIntegrityError):
        es.load_evalset(manifest_path, bodies_path)


class _ScriptedCursor:
    """load_candidates_from_db가 내는 세 조회(cluster_history, news_raw, 카테고리)에 차례로 답한다."""

    def __init__(self, results):
        self.results = list(results)
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchall(self):
        return self.results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ScriptedConn:
    def __init__(self, results):
        self.cur = _ScriptedCursor(results)

    def cursor(self):
        return self.cur


def test_db_loader_drops_clusters_with_purged_or_empty_bodies_and_reports_counts():
    """보존 기한(ADR 0023)이 지나 본문을 비운 기사(NULL)나 빈 본문('')이 든 클러스터는 후보에서 빠지고,
    빠진 수가 보고된다. 본문이 NULL인 기사를 빈 문자열로 바꿔 그대로 쓰지 않는다."""
    log = {"0": [1, 2, 3], "1": [4, 5, 6], "2": [7, 8, 9]}
    rows = [
        (1, "u1", "언론A", "제목1", "본문 1", None), (2, "u2", "언론A", "제목2", "본문 2", None),
        (3, "u3", "언론A", "제목3", "본문 3", None),
        (4, "u4", "언론B", "제목4", None, None), (5, "u5", "언론B", "제목5", "본문 5", None),
        (6, "u6", "언론B", "제목6", "본문 6", None),
        (7, "u7", "언론C", "제목7", "", None), (8, "u8", "언론C", "제목8", "본문 8", None),
        (9, "u9", "언론C", "제목9", "본문 9", None),
    ]
    conn = _ScriptedConn([[(11, log)], rows])
    report = {}

    candidates, articles = es.load_candidates_from_db(conn, report=report)

    assert [c.cluster_id for c in candidates] == [0]
    assert 4 not in articles and 7 not in articles
    assert all(a.body for a in articles.values())
    assert report == {"articles_body_purged": 1, "articles_body_empty": 1, "clusters_without_body": 2}
