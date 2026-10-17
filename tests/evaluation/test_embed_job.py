"""evaluation/llm/embed_job.py — 원격에서 돌 기사 임베딩 잡의 배치·체크포인트·팩 조립(BGE-M3 없이).

임베더 자리에는 텍스트 해시로 벡터를 만드는 페이크를 넣는다. 운영 임베딩 경로와 같은지는 운영 코드의 식·함수를 직접 읽어
묶는다(텍스트 규칙, Stage3의 배치 함수와 정렬 창). 기사 문장은 tests/frozen_news_fakes.py의 지어낸 문장이다.
"""
import hashlib
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import embed_job as job  # noqa: E402
from evaluation.llm import embedding_pack as ep  # noqa: E402
from evaluation.llm import frozen_export as fx  # noqa: E402
from tests.frozen_news_fakes import BODY_MARK, CODE, TITLE_MARK, stream, topic_articles, use_tmp_main_checkout  # noqa: E402

WINDOW = fx.Window.from_iso("2026-10-02T21:00:00", "2026-10-05T21:00:00")
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


class FakeEmbedder:
    """텍스트마다 정해지는 단위 벡터. fp32 임베더는 아주 조금 다른 벡터를 낸다(정밀도 차이 흉내)."""

    def __init__(self, use_fp16=None, revision=job.MODEL_REVISION, calls=None):
        self.device = "cuda"
        self.use_fp16 = use_fp16
        self.calls = calls if calls is not None else []
        config = type("Config", (), {"_name_or_path": f"/root/.cache/hub/models--BAAI--bge-m3/snapshots/{revision}"})()
        inner = type("Inner", (), {"config": config})()
        self.model = type("Model", (), {"model": inner})()

    def generate_embeddings_batch(self, texts, batch_size=20):
        self.calls.append(len(texts))
        out = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
            vec = np.random.default_rng(seed).normal(size=1024)
            if self.use_fp16 is False:
                vec = vec + np.random.default_rng(seed + 1).normal(scale=1e-3, size=1024)
            out.append((vec / np.linalg.norm(vec)).astype(np.float32).tolist())
        return out, 0.01

    def cleanup(self):
        pass


def factory(calls=None, revision=job.MODEL_REVISION):
    return lambda use_fp16=None: FakeEmbedder(use_fp16=use_fp16, revision=revision, calls=calls)


@pytest.fixture(autouse=True)
def _main_checkout(monkeypatch, tmp_path):
    return use_tmp_main_checkout(monkeypatch, tmp_path)


def _export(tmp_path, n_topics=5, per_topic=8, noise=10, name="t0"):
    rows, _ = topic_articles(topics=n_topics, per_topic=per_topic, noise=noise)
    dest = tmp_path / "data" / "exports" / name
    fx.write_export(dest, fx.build_export(fx.parse_copy_stream(stream(rows)), WINDOW, code=CODE))
    return dest


# ---------------------------------------------------------------- 운영 경로와 같은가


def test_text_rule_and_batching_are_the_production_stage3_ones():
    from pipeline import stages

    source = inspect.getsource(stages.embed_pending_articles)
    assert 'texts = [f"{r[1]} {r[2]}"[:8000] for r in batch]' in source
    assert "sort_window = sort_window or batch_size * 8" in source
    assert "_length_sorted_batches(rows, batch_size, sort_window)" in source
    assert job.embedding_text("제목", "본문" * 5000) == f"{'제목'} {'본문' * 5000}"[:8000]
    assert ep.EXPECTED_MODEL["text_rule"] == 'f"{title} {content}"[:8000]'
    assert ep.EXPECTED_MODEL["name"] == job.MODEL_NAME


def test_parts_are_whole_production_sort_windows_so_batches_do_not_change():
    from config.settings import Settings
    from pipeline.stages import _length_sorted_batches

    batch = Settings.EMBEDDING_BATCH_SIZE
    assert job.PART_ROWS % (batch * 8) == 0
    rows = job.synthetic_rows(2600)
    whole = [tuple(r[0] for r in b) for b in _length_sorted_batches(rows, batch, batch * 8)]
    by_part = [tuple(r[0] for r in b) for start, end in job.plan_parts(len(rows))
               for b in job.production_batches(rows[start:end], batch)]
    assert by_part == whole


def test_default_embedder_is_the_production_one_with_production_defaults():
    source = inspect.getsource(job.load_embedder)
    assert "from core.embedder import NewsEmbedder" in source
    assert "l2_normalize=True" in source and "use_fp16=use_fp16" in source
    from config.settings import Settings

    assert Settings.EMBEDDING_MAX_LENGTH == ep.EXPECTED_MODEL["max_length"] == 8192


# ---------------------------------------------------------------- 잡


def test_job_builds_a_pack_that_the_mac_side_accepts_for_the_same_export(tmp_path):
    dest = _export(tmp_path)
    rows, info = job.rows_from_export(dest, now=NOW)
    out = tmp_path / "remote" / "out"
    record = job.run_job(rows, out, input_info=info, embedder_factory=factory(), part_rows=64, log=lambda m: None)

    export = fx.load_export(dest, now=NOW)
    pack = ep.load_embedding_pack(out / job.PACK_DIR, export)
    assert pack.ids.tolist() == export.ids and pack.manifest["dtype"] == "float16"
    assert pack.manifest["model"]["revision"] == job.MODEL_REVISION
    assert record["parts"] == 1 and record["rows"] == 50 and record["determinism"]["bitwise_equal"] is True
    assert record["fp16_vs_fp32"]["n"] == 50 and 0.99 < record["fp16_vs_fp32"]["stored_f16_vs_fp32"]["min"] < 1.0
    # 잡이 남기는 기록에는 기사 텍스트가 없다
    for path in (out / job.JOB_FILE, out / job.PROGRESS_FILE, out / job.PACK_DIR / ep.MANIFEST_FILE):
        text = path.read_text(encoding="utf-8")
        assert BODY_MARK not in text and TITLE_MARK not in text and "소식" not in text


def test_parts_checkpoint_and_a_budget_stop_resumes_without_reembedding(tmp_path):
    rows = job.synthetic_rows(300)
    calls = []
    out = tmp_path / "out"
    seen = []

    def log(message):
        seen.append(message)
        if message.startswith("part 0:") and "건너뜀" not in message:
            (out / job.STOP_FILE).write_text("{}")  # 드라이버가 CU 상한에서 하는 일

    with pytest.raises(job.JobError) as stop:
        job.run_job(rows, out, input_info={"synthetic": "t"}, embedder_factory=factory(calls), part_rows=128, log=log)
    assert stop.value.code == job.EXIT_BUDGET
    assert set(json.loads((out / job.PROGRESS_FILE).read_text())["parts"]) == {"0"}
    first_calls = sum(calls)
    assert first_calls == 128

    (out / job.STOP_FILE).unlink()
    calls.clear()
    record = job.run_job(rows, out, input_info={"synthetic": "t"}, embedder_factory=factory(calls), part_rows=128,
                         log=lambda m: None)
    # 파트 1·2만 새로 임베딩(172행) + 결정론 확인 64행 + fp32 대조 100행
    assert sum(calls) == 172 + 64 + 100
    straight = tmp_path / "straight"
    job.run_job(rows, straight, input_info={"synthetic": "t"}, embedder_factory=factory(), part_rows=128,
                log=lambda m: None)
    a = np.load(out / job.PACK_DIR / "emb.f16.npy")
    b = np.load(straight / job.PACK_DIR / "emb.f16.npy")
    assert np.array_equal(a, b) and record["parts"] == 3


def test_resume_refuses_a_different_input(tmp_path):
    out = tmp_path / "out"
    job.run_job(job.synthetic_rows(70), out, input_info={"synthetic": "a"}, embedder_factory=factory(),
                log=lambda m: None, write_pack=False, fp32_check=False)
    with pytest.raises(job.JobError) as refused:
        job.run_job(job.synthetic_rows(71), out, input_info={"synthetic": "a"}, embedder_factory=factory(),
                    log=lambda m: None)
    assert refused.value.code == job.EXIT_INPUT


def test_a_tampered_checkpoint_is_recomputed(tmp_path):
    rows = job.synthetic_rows(130)
    out = tmp_path / "out"
    job.run_job(rows, out, input_info={"synthetic": "t"}, embedder_factory=factory(), part_rows=64,
                log=lambda m: None, write_pack=False, fp32_check=False)
    _, emb = job._part_paths(out, 1)
    data = np.load(emb)
    data[0, 0] += 1.0
    np.save(emb, data)
    calls = []
    job.run_job(rows, out, input_info={"synthetic": "t"}, embedder_factory=factory(calls), part_rows=64,
                log=lambda m: None, write_pack=False, fp32_check=False)
    assert sum(calls) == 64 + 64  # 파트 1 다시 + 결정론 확인


def test_job_stops_when_the_model_snapshot_is_not_the_pinned_revision(tmp_path):
    with pytest.raises(job.JobError) as wrong:
        job.run_job(job.synthetic_rows(10), tmp_path / "out", input_info={"synthetic": "t"},
                    embedder_factory=factory(revision="f" * 40), log=lambda m: None)
    assert wrong.value.code == job.EXIT_ENVIRONMENT


def test_job_refuses_an_export_whose_bodies_were_purged(tmp_path):
    dest = _export(tmp_path)
    fx.purge_export(dest, now=NOW, everything=True)
    with pytest.raises(job.JobError) as refused:
        job.rows_from_export(dest, now=NOW)
    assert refused.value.code == job.EXIT_INPUT


def test_job_refuses_an_export_whose_bodies_expire_now(tmp_path):
    dest = _export(tmp_path)
    with pytest.raises(job.JobError):
        job.rows_from_export(dest, now=datetime(2026, 11, 30, tzinfo=timezone.utc))


def test_job_refuses_a_tampered_export(tmp_path):
    dest = _export(tmp_path)
    data = (dest / fx.ARTICLES_FILE).read_bytes().replace("일련번호".encode(), "일련번오".encode(), 1)
    (dest / fx.ARTICLES_FILE).write_bytes(data)
    with pytest.raises(job.JobError) as refused:
        job.rows_from_export(dest, now=NOW)
    assert refused.value.code == job.EXIT_INPUT


def test_cli_prints_only_numbers_and_types_of_errors(tmp_path, capsys, monkeypatch):
    dest = _export(tmp_path)

    def boom(rows, out_dir, **kwargs):
        raise ValueError(f"본문 조각: {rows[0][2][:30]}")  # 예외 메시지에 텍스트가 섞여도

    monkeypatch.setattr(job, "run_job", boom)
    code = job.main(["--input-dir", str(dest), "--out-dir", str(tmp_path / "out")])
    printed = capsys.readouterr().out
    assert code == job.EXIT_FAILED and "ValueError" in printed and "소식" not in printed and "본문 조각" not in printed


def test_synthetic_rows_are_deterministic_and_shaped_like_the_export_lengths():
    a, b = job.synthetic_rows(400), job.synthetic_rows(400)
    assert a == b and [r[0] for r in a] == sorted(r[0] for r in a)
    lengths = sorted(len(r[2]) for r in a)
    assert 800 < lengths[len(lengths) // 2] < 1600
    assert all(r[0] >= job.SYNTHETIC_ID_BASE for r in a)


def test_fp32_check_rows_are_a_fixed_seeded_sample():
    picks = job.fp32_check_indices(5000)
    assert picks == job.fp32_check_indices(5000) and len(picks) == 100 and picks == sorted(set(picks))
    assert job.fp32_check_indices(40) == list(range(40))
