"""run_repro.py의 캐시 키(요구사항 6, 11: 'cache key changes with config') 테스트.

실제 pipeline.py 서브프로세스는 절대 띄우지 않는다 - run_pipeline을 몽키패치해
어떤 out_tag로 호출되는지만 가로챈다. 이 테스트가 보호하는 것: 하네스+엔진 git
SHA와 config_hash가 바뀌면 run_many_seeds가 만드는 캐시 태그(=출력 파일 이름)도
반드시 달라져야 한다 - 그래야 코드/설정을 고치고도 옛 결과를 캐시로 착각해 재사용하는
사고(v1의 결함)를 하네스 스스로 막는다.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
sys.path.insert(0, str(TEAM_REPRO_DIR))

import run_repro as RR  # noqa: E402


def _fake_result(tag_holder):
    def _run_pipeline(*, out_tag, **kw):
        tag_holder.append(out_tag)
        return {
            "primary": {"aggregate_metrics": {"mrr": 0.5}, "per_user_metrics": {}},
            "as_written": {"aggregate_metrics": {"mrr": 0.5}, "per_user_metrics": {}},
            "elapsed_sec": 0.1,
            "best_iteration": None,
            "n_distinct_scores_primary": 1,
        }
    return _run_pipeline


def _common_kwargs():
    return dict(
        version="current", protocol="team_split", label_mode="clicks_only", leakage_mode="fixed",
        candidate_pool="full_195", negative_source="random", objective_override="none",
    )


def test_cache_tag_changes_when_config_hash_changes(monkeypatch):
    tags = []
    monkeypatch.setattr(RR, "run_pipeline", _fake_result(tags))

    RR.run_many_seeds(
        seeds=[42], code_shas={"current": "sha_a"}, harness_sha="harness_a",
        config_hashes={("current", "none"): "confighash_1"}, answer_start="2026-01-01T00:00:00",
        **_common_kwargs(),
    )
    RR.run_many_seeds(
        seeds=[42], code_shas={"current": "sha_a"}, harness_sha="harness_a",
        config_hashes={("current", "none"): "confighash_2"}, answer_start="2026-01-01T00:00:00",
        **_common_kwargs(),
    )

    assert len(tags) == 2
    assert tags[0] != tags[1]
    assert "confighash_1" in tags[0]
    assert "confighash_2" in tags[1]


def test_cache_tag_changes_when_harness_or_code_sha_changes(monkeypatch):
    tags = []
    monkeypatch.setattr(RR, "run_pipeline", _fake_result(tags))

    RR.run_many_seeds(
        seeds=[1], code_shas={"current": "sha_a"}, harness_sha="harness_a",
        config_hashes={("current", "none"): "cfg"}, answer_start=None, **_common_kwargs(),
    )
    RR.run_many_seeds(
        seeds=[1], code_shas={"current": "sha_b"}, harness_sha="harness_a",
        config_hashes={("current", "none"): "cfg"}, answer_start=None, **_common_kwargs(),
    )
    RR.run_many_seeds(
        seeds=[1], code_shas={"current": "sha_a"}, harness_sha="harness_b",
        config_hashes={("current", "none"): "cfg"}, answer_start=None, **_common_kwargs(),
    )

    assert len(set(tags)) == 3, f"세 태그가 전부 달라야 합니다: {tags!r}"


def test_cache_tag_stable_when_nothing_relevant_changes(monkeypatch):
    """동일 SHA/config_hash/시드/설정이면 태그도 동일해야 캐시가 실제로 재사용된다."""
    tags = []
    monkeypatch.setattr(RR, "run_pipeline", _fake_result(tags))

    kwargs = dict(
        seeds=[7], code_shas={"current": "sha_a"}, harness_sha="harness_a",
        config_hashes={("current", "none"): "cfg"}, answer_start="2026-01-01T00:00:00",
        **_common_kwargs(),
    )
    RR.run_many_seeds(**kwargs)
    RR.run_many_seeds(**kwargs)

    assert tags[0] == tags[1]
