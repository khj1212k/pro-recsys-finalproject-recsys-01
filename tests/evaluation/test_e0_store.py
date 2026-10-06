"""evaluation/llm/e0_store.py — 운영 코드를 고치지 않고 DB 없이 클러스터링·생성 경로를 돌리는 파일 대역.

세 가지를 본다.
1. 계약: 대역이 바꿔 끼우는 운영 함수의 인자와, 대역이 흉내 내는 운영 함수의 본문이 고정해 둔 값과 같다.
   운영 코드가 바뀌면 여기서 깨지고, 실험 러너는 대역을 끼우기 전에 같은 검사로 멈춘다.
2. 같은 결과: 같은 행에 대해 파일 로더가 운영 로더와 같은 dict를 내고, 저장 대역이 운영 INSERT와 같은 값을 남긴다.
3. 왕복: 합성 기사 50건 → 운영 NewsClusterer(대역 로더) → 운영 그래프(페이크 LLM) → 저장 대역 → 읽기.

기사 문장은 tests/frozen_news_fakes.py에서 지어낸 것이고 임베딩은 난수 단위 벡터다.
"""
import ast
import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import e0_store as e0  # noqa: E402
from evaluation.llm import embedding_pack as ep  # noqa: E402
from evaluation.llm import frozen_export as fx  # noqa: E402
from tests.frozen_news_fakes import CODE, server_row, stream, topic_articles  # noqa: E402

WINDOW = fx.Window.from_iso("2026-10-02T21:00:00", "2026-10-05T21:00:00")
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
PSEUDO_NOW = datetime(2026, 10, 3, 21, 0)  # 창 [10-02 21:00, 10-03 21:00) UTC
DRAFT_CONTENT = "여러 매체가 같은 소식을 전했다. 관계자는 계획을 차례로 진행한다고 밝혔다. " * 4
DRAFT_TITLE = "합성 소식 정리"
DRAFT_SENTENCE = "같은 소식을 한데 모았어요"


def _source(tmp_path, rows, vectors, *, name="t0", dtype="float32", now=NOW):
    dest = tmp_path / "data" / "exports" / name
    fx.write_export(dest, fx.build_export(fx.parse_copy_stream(stream(rows)), WINDOW, code=CODE))
    export = fx.load_export(dest, now=now)
    order = np.argsort([r["id"] for r in rows])
    ep.write_embedding_pack(
        dest / "embeddings", ids=export.ids, vectors=np.asarray(vectors)[order],
        content_sha256=[r["content_sha256"] for r in export.rows],
        export_identity_sha256=export.identity_sha256, model=dict(ep.EXPECTED_MODEL), dtype=dtype,
    )
    return e0.FileArticleSource.open(dest, dest / "embeddings", now=now)


@pytest.fixture
def topics():
    return topic_articles()  # 5주제 × 8건 + 흩어진 10건 = 50건


@pytest.fixture
def source(tmp_path, topics):
    rows, vectors = topics
    return _source(tmp_path, rows, vectors)


@pytest.fixture
def store(tmp_path):
    return e0.FileGenerationStore(tmp_path / "data" / "experiments" / "e0" / "run1")


class SchemaFakeLLM:
    """스키마를 보고 답하는 페이크. 워커 스레드 여러 개가 순서 없이 불러도 된다."""

    provider, model = "fake", "fake-model"

    def __init__(self):
        self.purposes = []
        self._lock = threading.Lock()

    def complete(self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
        from core.llm.client import LLMResult, LLMUsage
        from core.llm import schemas

        with self._lock:
            self.purposes.append(purpose)
        parsed = {
            schemas.ClusterEval: lambda: schemas.ClusterEval(decision="PASS", confidence=0.9, summary="합성 묶음"),
            schemas.NewsletterContent: lambda: schemas.NewsletterContent(content=DRAFT_CONTENT),
            schemas.NewsletterMeta: lambda: schemas.NewsletterMeta(
                title=DRAFT_TITLE, sentence=DRAFT_SENTENCE,
                keywords=["소식", "매체", "계획", "관계자", "진행"], categories=["사회"]),
            schemas.NewsletterEvalV2: lambda: schemas.NewsletterEvalV2(
                scores=schemas.CriterionScores(faithfulness=5, coverage=4, coherence=4, style=4)),
            schemas.ToneResult: lambda: schemas.ToneResult(
                title=DRAFT_TITLE, summary=DRAFT_SENTENCE, content=DRAFT_CONTENT, keywords=["소식", "매체", "계획"]),
        }[schema]()
        return LLMResult(text=None, parsed=parsed, usage=LLMUsage(), latency_s=0.0, attempts=1,
                         provider=self.provider, model=self.model)


@pytest.fixture
def fake_llm(monkeypatch):
    import core.reconstruction.generator as generator_module
    import core.tone_converter as tone_module
    import workflow.evaluators as evaluators_module

    client = SchemaFakeLLM()
    for module in (evaluators_module, generator_module, tone_module):
        monkeypatch.setattr(module, "get_client", lambda role: client)
    return client


# ---------------------------------------------------------------- 1. 계약


def test_production_contract_holds_on_this_checkout():
    assert e0.check_production_contract() == []


def test_contract_names_the_patch_target_whose_parameters_changed(monkeypatch, store):
    from core.clustering.hdbscan_clusterer import NewsClusterer

    def _load_data_from_db(self, exclude_clustered=True, lookback_hours=24, now=None):  # 인자 하나 추가
        return {}

    monkeypatch.setattr(NewsClusterer, "_load_data_from_db", _load_data_from_db)
    problems = e0.check_production_contract()
    assert any("NewsClusterer._load_data_from_db" in p and "인자" in p for p in problems)
    with pytest.raises(e0.ProductionContractError, match="_load_data_from_db"):
        with e0.installed(store):
            pass


def test_contract_fails_when_a_mirrored_production_function_changes_its_body(monkeypatch, store):
    import db.batch_manager as batch_manager

    def save_news_letter(conn, article_ids: list, newsletter_result: dict, run_id=None, generation_history=None) -> int:
        return 1  # 인자는 같고 본문만 다른 운영 변경

    monkeypatch.setattr(batch_manager, "save_news_letter", save_news_letter)
    problems = e0.check_production_contract()
    assert any("db.batch_manager.save_news_letter" in p and "본문" in p for p in problems)
    with pytest.raises(e0.ProductionContractError):
        with e0.installed(store):
            pass


def test_contract_fails_when_a_patch_target_is_gone(monkeypatch):
    import workflow.nodes as nodes

    monkeypatch.delattr(nodes, "get_shared_embedder")
    assert any("workflow.nodes.get_shared_embedder" in p and "없다" in p for p in e0.check_production_contract())


def test_stage5_builds_the_cluster_state_with_the_keys_the_stand_in_reproduces():
    """ClusterRun.state_for가 흉내 내는 것은 Stage5.execute 안 process_cluster의 state dict다."""
    tree = ast.parse((REPO_ROOT / "ai_workspace" / "pipeline" / "stages.py").read_text(encoding="utf-8"))
    process = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "process_cluster")
    state = next(n.value for n in ast.walk(process) if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", None) == "state")
    assert {k.value for k in state.keys} == set(e0.STAGE5_STATE_KEYS)
    source = ast.unparse(tree)
    assert "all_ids = sorted(list(clusters.keys()), reverse=True)" in source


# ---------------------------------------------------------------- 2. 로더: 운영과 같은 dict


def _production_load(rows, vectors, lookback_hours=24):
    """운영 _load_data_from_db를 가짜 커서로 돌린다. 행은 운영 SELECT의 열 순서(id, 제목, 임베딩, 언론사, 본문)다."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (r["id"], r["title"], np.asarray(v, dtype=np.float32), r["press"], r["body"])
        for r, v in sorted(zip(rows, vectors, strict=True), key=lambda pair: pair[0]["id"])
    ]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    with patch("db.connection.get_connection", return_value=conn), patch("db.connection.release_connection"):
        data = NewsClusterer()._load_data_from_db(lookback_hours=lookback_hours)
    return data, cursor.execute.call_args[0]


def test_file_loader_returns_what_the_production_loader_returns_for_the_same_rows(source, topics):
    rows, vectors = topics
    expected, _ = _production_load(rows, vectors)

    got = source.load(pseudo_now=PSEUDO_NOW, lookback_hours=24)

    assert list(got) == list(expected) == ["ids", "titles", "embeddings", "press_names", "contents"]
    assert type(got["ids"]) is type(expected["ids"]) and got["ids"].dtype == expected["ids"].dtype
    assert got["embeddings"].dtype == expected["embeddings"].dtype == np.float32
    assert got["embeddings"].shape == expected["embeddings"].shape == (50, 1024)
    np.testing.assert_array_equal(got["ids"], expected["ids"])
    np.testing.assert_array_equal(got["embeddings"], expected["embeddings"])
    for key in ("titles", "press_names", "contents"):
        assert type(got[key]) is list and got[key] == expected[key]


def test_file_loader_applies_the_filters_of_the_production_query(source):
    """대역이 구현한 조건은 운영 쿼리에서 온 것이다: 본문 있음, 임베딩 있음, 수집 시각 하한, id 순서."""
    _, (query, params) = _production_load([], [])
    normalized = " ".join(query.split())
    for clause in (
        "SELECT N.raw_news_id, N.raw_news_title, N.embedding_result, P.press_name, N.raw_news_content",
        "N.embedding_result IS NOT NULL",
        "N.raw_news_content IS NOT NULL AND N.raw_news_content != ''",
        "N.raw_news_crawled_at >= NOW() - (%s * INTERVAL '1 hour')",
        "AND news_letter_id IS NULL",
        "ORDER BY N.raw_news_id",
    ):
        assert clause in normalized
    assert params == (24,)


def test_empty_window_has_the_same_shape_as_production_with_no_rows(source):
    expected, _ = _production_load([], [])
    got = source.load(pseudo_now=datetime(2026, 10, 2, 21, 0), lookback_hours=24)
    assert got["ids"].shape == expected["ids"].shape == (0,)
    assert got["embeddings"].shape == expected["embeddings"].shape and got["embeddings"].dtype == expected["embeddings"].dtype
    assert got["titles"] == got["contents"] == got["press_names"] == []


def test_window_is_half_open_and_ends_at_the_pseudo_now(tmp_path):
    rows = [
        server_row(1, crawled="2026-10-02T21:00:00.000000Z"),   # 하한과 같은 시각: 포함
        server_row(2, crawled="2026-10-03T20:59:59.999999Z"),   # 상한 직전: 포함
        server_row(3, crawled="2026-10-03T21:00:00.000000Z"),   # 의사 시각과 같은 시각: 제외(운영이라면 아직 없는 기사)
        server_row(4, crawled="2026-10-04T03:00:00.000000Z"),
    ]
    vectors = np.eye(4, ep.EMBEDDING_DIM, dtype=np.float32)
    source = _source(tmp_path, rows, vectors)

    assert source.load(pseudo_now=PSEUDO_NOW, lookback_hours=24)["ids"].tolist() == [1001, 1002]
    assert source.load(pseudo_now=datetime(2026, 10, 4, 21, 0), lookback_hours=24)["ids"].tolist() == [1003, 1004]
    assert source.load(pseudo_now=datetime(2026, 10, 4, 21, 0), lookback_hours=48)["ids"].tolist() == [1001, 1002, 1003, 1004]
    with pytest.raises(ValueError, match="UTC"):
        source.load(pseudo_now=datetime(2026, 10, 3, 21, 0, tzinfo=timezone.utc))


def test_loader_refuses_a_window_that_contains_an_expired_body(tmp_path):
    rows = [server_row(1, crawled="2026-10-02T22:00:00.000000Z"), server_row(2, crawled="2026-10-03T12:00:00.000000Z")]
    vectors = np.eye(2, ep.EMBEDDING_DIM, dtype=np.float32)
    after_first_expiry = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)  # 1번 행만 만료
    source = _source(tmp_path, rows, vectors, now=after_first_expiry)

    with pytest.raises(e0.BodyExpired, match="1건"):
        source.load(pseudo_now=PSEUDO_NOW, lookback_hours=24)
    dropped = source.load(pseudo_now=PSEUDO_NOW, lookback_hours=24, on_expired="drop")
    assert dropped["ids"].tolist() == [1002]  # 운영의 보존 잡이 본문을 비웠을 때 운영 쿼리가 내는 결과와 같다


def test_source_refuses_a_pack_that_does_not_line_up_with_the_export(source):
    pack = source.pack
    shuffled = ep.EmbeddingPack(pack.dir, pack.manifest, pack.ids[::-1].copy(), pack.vectors)
    with pytest.raises(ValueError, match="id"):
        e0.FileArticleSource(source.export, shuffled)


def test_cluster_news_calls_the_loader_the_way_the_stand_in_expects(source):
    from core.clustering.hdbscan_clusterer import NewsClusterer

    original = NewsClusterer._load_data_from_db
    e0.cluster_frozen_window(source, PSEUDO_NOW)
    assert source.calls[-1]["lookback_hours"] == 24 and source.calls[-1]["exclude_clustered"] is True
    assert source.calls[-1]["rows"] == 50
    assert NewsClusterer._load_data_from_db is original  # 끝나면 운영 함수가 제자리에 있다


# ---------------------------------------------------------------- 2. 저장: 운영 INSERT와 같은 값


class _CapturingCursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        last = self.statements[-1][0]
        return (41,) if "RETURNING news_letter_id" in last else None  # 카테고리는 없다고 답한다

    def close(self):
        pass


class _CapturingConn:
    def __init__(self):
        self.cur = _CapturingCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass


TRICKY_NEWSLETTERS = [
    {"title": "제목", "sentence": "한 줄", "content": "본문", "keywords": ["가", "나"], "categories": ["사회"]},
    {"title": None, "summary": "요약만 있는 변환본", "content": b"\xeb\xb3\xb8\xeb\xac\xb8", "keywords": []},
    {"title": "널\x00문자와 반쪽 문자 \ud83d", "sentence": "", "summary": "빈 sentence 뒤의 summary",
     "content": "줄\n바꿈", "keywords": [np.str_("넘파이"), 3], "categories": []},
]


@pytest.mark.parametrize("newsletter", TRICKY_NEWSLETTERS)
def test_save_stand_in_stores_what_production_would_insert(store, newsletter):
    from db.batch_manager import save_news_letter

    history = {"attempts": [{"n": np.int64(1), "draft_title": "초안"}]}
    article_ids = [np.int64(12), 10, 11]
    conn = _CapturingConn()
    assert save_news_letter(conn, article_ids, dict(newsletter), run_id=7, generation_history=history) == 41
    insert_sql, insert_params = conn.cur.statements[0]
    update_sql, update_params = conn.cur.statements[-1]
    assert insert_sql.startswith("INSERT INTO news_letter (") and update_sql.startswith("UPDATE news_raw SET news_letter_id")
    title, sentence, content, keywords_json, raw_news_count, run_id, history_json = insert_params

    saved_id = store.save_news_letter(store.get_connection(), article_ids, dict(newsletter), run_id=7,
                                      generation_history=history)

    record = store.newsletters()[-1]
    assert record["news_letter_id"] == saved_id == 1
    assert (record["title"], record["sentence"], record["content"]) == (title, sentence, content)
    assert record["keywords"] == json.loads(keywords_json)
    assert record["raw_news_count"] == raw_news_count == 3
    assert record["run_id"] == run_id == 7
    assert record["generation_history"] == json.loads(history_json)
    assert record["linked_article_ids"] == update_params[1] == [12, 10, 11]


def test_production_save_node_runs_unchanged_against_the_file_store(store):
    import workflow.nodes as nodes

    state = {
        "newsletter_draft": {"title": DRAFT_TITLE, "sentence": "형식체 한 줄", "content": DRAFT_CONTENT,
                             "keywords": ["소식"], "categories": ["사회"]},
        "converted_newsletter": {"title": DRAFT_TITLE, "summary": DRAFT_SENTENCE, "content": DRAFT_CONTENT},
        "newsletter_embedding": None,
        "current_article_ids": [5001, 5002, 5003], "current_cluster_id": 4, "run_id": 9,
        "generation_history": {"attempts": []}, "completed_newsletters": [],
    }
    with e0.installed(store):
        first = nodes.save_newsletter_to_db(dict(state))
        second = nodes.save_newsletter_to_db(dict(state))

    assert first == {"completed_newsletters": [1]} and second == {"completed_newsletters": [2]}
    saved = store.newsletters()
    assert [r["news_letter_id"] for r in saved] == [1, 2]
    assert saved[0]["sentence"] == DRAFT_SENTENCE  # 변환본의 요약이 저장된다(운영 노드의 규칙 그대로)
    assert saved[0]["linked_article_ids"] == [5001, 5002, 5003] and saved[0]["run_id"] == 9
    assert store.connections_opened == store.connections_released == 2
    store.assert_clean()


def test_record_only_embedder_lets_the_production_embed_node_continue_without_a_vector(store):
    import workflow.nodes as nodes

    draft = {"title": DRAFT_TITLE, "content": DRAFT_CONTENT}
    with e0.installed(store):
        update = nodes.embed_newsletter_node({"newsletter_draft": draft})

    assert update == {"original_newsletter": draft, "newsletter_embedding": None}
    text = f"{DRAFT_TITLE} {DRAFT_CONTENT}"  # 운영 노드가 임베딩에 넣는 문자열
    record = store.embedding_records()[-1]
    assert record["kind"] == "record_only" and record["chars"] == len(text)
    assert record["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert DRAFT_TITLE not in (store.dir / e0.EMBEDDINGS_FILE).read_text(encoding="utf-8")


def test_a_real_newsletter_vector_is_recorded_as_a_hash_when_the_node_stores_it(store):
    import workflow.nodes as nodes

    state = {
        "newsletter_draft": {"title": DRAFT_TITLE, "sentence": "한 줄", "content": DRAFT_CONTENT},
        "newsletter_embedding": [0.25] * 1024,
        "current_article_ids": [1, 2, 3], "current_cluster_id": 0, "run_id": 1,
    }
    with e0.installed(store):
        assert nodes.save_newsletter_to_db(state) == {"completed_newsletters": [1]}
    record = store.embedding_records()[-1]
    assert (record["kind"], record["news_letter_id"], record["dim"]) == ("stored", 1, 1024)
    assert len(record["vector_sha256"]) == 64
    store.assert_clean()


def test_any_other_sql_is_refused_and_remembered(store):
    conn = store.get_connection()
    with pytest.raises(e0.UnexpectedDatabaseAccess):
        conn.cursor().execute("UPDATE news_raw SET news_letter_id = %s WHERE raw_news_id = ANY(%s)", (1, [1]))
    assert len(store.violations) == 1 and "UPDATE news_raw" in store.violations[0]
    with pytest.raises(e0.UnexpectedDatabaseAccess, match="1건"):
        store.assert_clean()


def test_installed_blocks_every_real_database_connection_and_restores_production_afterwards(store):
    import db.batch_manager as batch_manager
    import db.connection as connection
    import workflow.nodes as nodes

    before = {name: getattr(module, name) for module, name in (
        (nodes, "get_connection"), (nodes, "release_connection"), (nodes, "save_news_letter"),
        (nodes, "get_shared_embedder"), (connection, "get_pool"), (connection, "_build_db_config"),
        (batch_manager, "create_new_batch"), (batch_manager, "update_cluster_log"))}

    with pytest.raises(RuntimeError, match="안에서 난 오류"):
        with e0.installed(store, cluster_history=True):
            assert nodes.save_news_letter == store.save_news_letter
            with pytest.raises(e0.UnexpectedDatabaseAccess):
                connection.get_connection()          # 풀을 거치는 모든 연결
            with pytest.raises(e0.UnexpectedDatabaseAccess):
                connection.connect_unpooled("x")     # 풀 밖 전용 연결
            with pytest.raises(e0.UnexpectedDatabaseAccess):
                batch_manager.get_current_run_id()   # 다른 모듈이 임포트해 둔 get_connection도 같은 풀로 간다
            with pytest.raises(e0.ProductionContractError, match="이미"):
                with e0.installed(store):
                    pass
            raise RuntimeError("안에서 난 오류")

    assert len(store.violations) == 3
    for (module, name) in ((nodes, "get_connection"), (nodes, "release_connection"), (nodes, "save_news_letter"),
                           (nodes, "get_shared_embedder"), (connection, "get_pool"),
                           (connection, "_build_db_config"), (batch_manager, "create_new_batch"),
                           (batch_manager, "update_cluster_log")):
        assert getattr(module, name) is before[name]


def test_cluster_history_stand_ins_number_runs_and_keep_the_log(store):
    log = {0: [np.int64(1), 2, 3], "clustering_stats": {"n_articles": 50, "noise_ratio": np.float64(0.2)},
           "cluster_meta": {0: {"split_v2": False}}}
    assert store.create_new_batch(log) == 1
    assert store.create_new_batch(log) == 2
    assert store.update_cluster_log(2, {**log, "cluster_outcomes": {0: {"status": "completed"}}}) is True
    history = store.cluster_history()
    assert [(h["kind"], h["run_id"]) for h in history] == [("created", 1), ("created", 2), ("updated", 2)]
    assert history[0]["cluster_log"]["0"] == [1, 2, 3]  # json을 거치며 키는 문자열이 된다(운영의 json 컬럼과 같다)
    assert history[2]["cluster_log"]["cluster_outcomes"]["0"]["status"] == "completed"


def test_store_continues_numbering_when_reopened(store):
    store.save_news_letter(store.get_connection(), [1, 2, 3], {"title": "가", "sentence": "나", "content": "다"})
    reopened = e0.FileGenerationStore(store.dir)
    assert reopened.save_news_letter(reopened.get_connection(), [4, 5, 6], {"title": "라", "sentence": "마", "content": "바"}) == 2
    assert reopened.create_new_batch({}) == 1


def test_runner_records_share_the_newsletter_file_without_becoming_saved_rows(store):
    store.append_newsletter_record("draft", {"cluster_id": 3, "attempt": 1, "content": DRAFT_CONTENT})
    store.save_news_letter(store.get_connection(), [1, 2, 3], {"title": "가", "sentence": "나", "content": "다"})
    assert [r["news_letter_id"] for r in store.newsletters()] == [1]
    assert store.newsletters("draft")[0]["attempt"] == 1
    assert sorted(p.name for p in store.dir.iterdir()) == [e0.NEWSLETTERS_FILE]  # 초안 텍스트는 이 파일에만 있다
    with pytest.raises(ValueError):
        store.append_newsletter_record("saved", {})


def test_store_refuses_a_directory_git_could_track(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    (repo / ".gitignore").write_text("/data/\n", encoding="utf-8")
    with pytest.raises(fx.DestinationRefused):
        e0.FileGenerationStore(repo / "reports" / "llm" / "e0-run")
    assert not (repo / "reports").exists()
    assert e0.FileGenerationStore(repo / "data" / "experiments" / "e0" / "run1").dir.is_dir()


# ---------------------------------------------------------------- 3. 왕복과 결정론


def test_fifty_synthetic_articles_cluster_generate_save_and_read_back(source, store, fake_llm, topics):
    from workflow.graph import compile_workflow

    rows, _ = topics
    run = e0.cluster_frozen_window(source, PSEUDO_NOW)

    # 주제마다 8건이 한 무리로 묶인다. 그 밖의 무리가 있다면 흩어진 기사끼리다 - HDBSCAN이 한 주제에 붙인 흩어진
    # 기사를 split_v2가 다시 떼어 낼 수 있다(운영 동작 그대로이고, 입력이 같으면 결과도 같다).
    topic_of = {row["id"]: row["title"].rsplit(" 소식", 1)[0] for row in rows}
    whole_topics = sorted(sorted(i for i, t in topic_of.items() if t == name)
                          for name in set(topic_of.values()) - {"흩어진"})
    clustered = sorted(sorted(ids) for ids in run.clusters.values())
    assert len(whole_topics) == 5 and all(len(g) == 8 and g in clustered for g in whole_topics)
    assert all(topic_of[i] == "흩어진" for ids in clustered if ids not in whole_topics for i in ids)
    n_clusters = len(clustered)
    assert run.n_articles == 50 and set(run.cluster_meta) == set(run.clusters)

    app = compile_workflow()
    with e0.installed(store):
        finals = {cid: app.invoke(run.state_for(cid, run_id=3)) for cid in sorted(run.clusters)}

    assert all(final["completed_newsletters"] for final in finals.values())
    saved = store.newsletters()
    assert sorted(r["news_letter_id"] for r in saved) == list(range(1, n_clusters + 1))
    assert sorted(sorted(r["linked_article_ids"]) for r in saved) == clustered
    assert all(r["run_id"] == 3 and r["content"] == DRAFT_CONTENT and r["sentence"] == DRAFT_SENTENCE for r in saved)
    records = store.embedding_records()
    assert len(records) == n_clusters and all(r["kind"] == "record_only" for r in records)
    assert sorted(fake_llm.purposes) == sorted(
        ["cluster_eval", "newsletter_content_gen", "newsletter_meta_gen", "newsletter_eval", "tone_convert"] * n_clusters)
    store.assert_clean()


def test_same_frozen_input_and_pseudo_now_give_the_same_membership(tmp_path, topics):
    rows, vectors = topics
    first = e0.cluster_frozen_window(_source(tmp_path, rows, vectors, name="a"), PSEUDO_NOW).membership_snapshot()
    second = e0.cluster_frozen_window(_source(tmp_path, rows, vectors, name="b"), PSEUDO_NOW).membership_snapshot()

    assert first["membership_sha256"] == second["membership_sha256"]
    assert first["clusters"] == second["clusters"] and first["window_utc"] == ["2026-10-02T21:00:00", "2026-10-03T21:00:00"]
    assert first["params"] == {"min_cluster_size": 3, "min_samples": 2, "lookback_hours": 24}
    text = json.dumps(first, ensure_ascii=False)
    assert all(row["title"] not in text for row in rows)  # 스냅숏에는 id와 해시만 있다

    earlier = e0.cluster_frozen_window(_source(tmp_path, rows, vectors, name="c"), datetime(2026, 10, 3, 6, 0))
    assert earlier.n_articles < 50
    assert earlier.membership_snapshot()["membership_sha256"] != first["membership_sha256"]


def test_float16_pack_gives_the_same_membership_as_float32_on_synthetic_input(tmp_path, topics):
    rows, vectors = topics
    f32 = e0.cluster_frozen_window(_source(tmp_path, rows, vectors, name="f32", dtype="float32"), PSEUDO_NOW)
    f16 = e0.cluster_frozen_window(_source(tmp_path, rows, vectors, name="f16", dtype="float16"), PSEUDO_NOW)
    assert f16.clusters == f32.clusters


def test_unchanged_stage5_runs_end_to_end_on_the_stand_ins(source, store, fake_llm, tmp_path, monkeypatch):
    from config.settings import Settings
    from pipeline.stages import Stage5_NewsletterGeneration

    expected = e0.cluster_frozen_window(source, PSEUDO_NOW).clusters
    monkeypatch.chdir(tmp_path)  # Stage5가 logs/llm_metrics_run*.json을 현재 디렉터리에 쓴다
    with source.patched_loader(PSEUDO_NOW), e0.installed(store, cluster_history=True):
        created = Stage5_NewsletterGeneration(Settings).execute()

    assert created == len(expected) == len(store.newsletters()) and created >= 5
    assert sorted(sorted(r["linked_article_ids"]) for r in store.newsletters()) == sorted(sorted(v) for v in expected.values())
    history = store.cluster_history()
    assert [(h["kind"], h["run_id"]) for h in history] == [("created", 1), ("updated", 1)]
    assert history[0]["cluster_log"]["clustering_stats"]["n_articles"] == 50
    outcomes = history[1]["cluster_log"]["cluster_outcomes"]
    assert len(outcomes) == created and all(o["status"] == "completed" for o in outcomes.values())
    assert all(r["run_id"] == 1 for r in store.newsletters())
    assert os.path.exists(tmp_path / "logs" / "llm_metrics_run1.json")
    store.assert_clean()
