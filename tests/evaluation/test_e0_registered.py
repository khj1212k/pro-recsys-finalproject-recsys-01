"""E0 등록값 충실도와 시작 검사 (evaluation/llm/e0_registered.py, ADR 0009 A8.2).

1. 사전 등록 yaml을 읽어 러너의 실제 설정(`effective_config()`)과 같은지 본다. 하나라도 다르면 이 테스트가 깨진다 -
   등록값을 바꾸려면 A8.x 개정 + yaml + 이 모듈을 함께 고친다.
2. 실행 중 환경이 Settings·레지스트리·킬 스위치를 바꾸면 러너가 차이를 찾아낸다.
3. 시작 검사 (a)~(e)를 임시 git 저장소(원격 포함)에서 하나씩 어겨 본다.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import e0_registered as reg  # noqa: E402

PREREG = yaml.safe_load((REPO_ROOT / reg.PREREG_REL).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. 충실도


def test_runner_effective_configuration_equals_the_registered_yaml():
    assert reg.config_differences(PREREG) == []


def test_registered_values_used_by_the_runner_are_the_yaml_values():
    from evaluation.llm import e0_baseline, e0_instrument

    sample = PREREG["sample"]["stratified_sample"]
    assert (reg.SAMPLE["n"], reg.SAMPLE["seed"], reg.SAMPLE["hard_fraction"], reg.SAMPLE["warmup"],
            reg.SAMPLE["min_size"]) == (sample["n"], sample["seed"], sample["hard_fraction"], sample["warmup"],
                                        sample["min_size"])
    assert reg.ORDER_SEED == 20261007 and "Random(20261007)" in PREREG["sample"]["processing_order"]["eval"]
    budget = PREREG["budget"]
    cfg_defaults = e0_baseline.RunConfig.__dataclass_fields__
    assert cfg_defaults["cap_run_usd"].default == budget["cap_usd_valid_run"] == 1.00
    assert cfg_defaults["cap_cumulative_usd"].default == budget["cap_usd_e0_cumulative"] == 1.50
    assert cfg_defaults["wall_clock_cap_s"].default == budget["wall_clock_cap_hours"] * 3600
    assert reg.BUDGET["worst_case_tokens_per_char"] == budget["worst_case_tokens_per_char"] == 1.5
    assert e0_instrument.worst_case_usd(1000, 100, (0.30, 2.50)) == pytest.approx((1500 * 0.30 + 100 * 2.50) / 1e6)
    assert set(reg.INFRA_HTTP_STATUSES) == {s for s in budget["stop_on_infra"] if isinstance(s, int)}
    assert reg.CONCURRENCY == PREREG["pipeline"]["concurrency"]
    assert {k: PREREG["human_labels"]["relabel"][k] for k in reg.RELABEL} == reg.RELABEL
    assert reg.RELABEL_REDUCED == PREREG["human_labels"]["reduced_option"]["relabel"]
    assert reg.HTTP_ATTEMPT_FIELDS == PREREG["records"]["http_attempt_fields"]
    assert reg.CALL_FIELDS == PREREG["records"]["call_fields"]
    clustering = PREREG["sample"]["clustering"]
    assert (reg.CLUSTERING["days"], reg.CLUSTERING["extend_days_if_short"]) == (clustering["days"],
                                                                              clustering["extend_days_if_short"])
    assert reg.FORBID_DATABASE is False  # A8.10 덧붙임: 등록하지 않은 가드는 끈다


def test_call_parameters_registered_are_the_ones_in_production_code():
    """온도·max_tokens는 운영 코드 값이다. 러너는 바꾸지 않으므로 운영 소스에 그 값이 그대로 있어야 한다."""
    import inspect

    from core.reconstruction import generator
    from core import tone_converter
    from workflow import evaluators

    sources = {
        "cluster_eval": inspect.getsource(evaluators.ClusterEvaluator.evaluate),
        "newsletter_content_gen": inspect.getsource(generator.NewsReconstructor._generate_content),
        "newsletter_meta_gen": inspect.getsource(generator.NewsReconstructor._generate_meta),
        "newsletter_eval": inspect.getsource(evaluators.NewsletterEvaluator.evaluate),
        "tone_convert": inspect.getsource(tone_converter.ToneConverter.convert),
    }
    for purpose, params in reg.CALL_PARAMS.items():
        src = sources[purpose]
        assert f"temperature={params['temperature']}" in src, purpose
        assert f"max_tokens={params['max_tokens']}" in src, purpose
        assert f'purpose="{purpose}"' in src, purpose


# ---------------------------------------------------------------- 2. 실행 중 환경의 차이


def test_a_settings_override_is_reported(monkeypatch):
    from config.settings import Settings

    monkeypatch.setattr(Settings, "FAITHFULNESS_GATE_MODE", "shadow")
    monkeypatch.setattr(Settings, "LLM_CALL_DEADLINE_S", 181.0)
    diff = reg.config_differences(PREREG)
    assert len(diff) == 1 and "FAITHFULNESS_GATE_MODE" in diff[0] and "LLM_CALL_DEADLINE_S" in diff[0]


def test_role_rate_limit_and_kill_switch_overrides_are_reported(monkeypatch, tmp_path):
    from config.settings import Settings

    monkeypatch.setenv("JUDGE_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("LLM_MIN_INTERVAL", "1.0")
    kill = tmp_path / "LLM_KILL_SWITCH"
    kill.write_text("")
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(kill))
    diff = " ".join(reg.config_differences(PREREG))
    assert "pipeline.roles" in diff and "gemini_rate_limiter_min_interval_s" in diff and "kill_switch_file" in diff


def test_adapter_retry_constant_is_checked_where_the_adapter_reads_it(monkeypatch):
    import core.llm.adapters as adapters

    monkeypatch.setattr(adapters, "MAX_RETRIES", 3)
    assert any("MAX_LLM_CALL_RETRIES" in d for d in reg.config_differences(PREREG))


# ---------------------------------------------------------------- 실행 전 기록 (e)


def _filled_record():
    return {
        "code_sha": "a" * 40, "ai_workspace_diff_since_b64da14": "12 files changed", "adr_0023_amendment_commit": "b" * 40,
        "export_t0_kst": "2026-10-20T21:45:00+09:00", "export_rows": 5000, "export_manifest_sha256": "c" * 64,
        "export_body_expires_at": "2026-11-15T21:00:00.000000Z", "embeddings_sha256": "d" * 64,
        "embeddings_fp16_vs_fp32_cos": {"p10": 0.99999, "min": 0.9999, "n": 100}, "colab_cu_used": 0.3,
        "day_windows_utc": [["2026-10-16T21:00:00", "2026-10-17T21:00:00"], ["2026-10-17T21:00:00", "2026-10-18T21:00:00"],
                            ["2026-10-18T21:00:00", "2026-10-19T21:00:00"]],
        "snapshot_sha256": {"run1": "1" * 64, "run2": "2" * 64, "run3": "3" * 64}, "sample_manifest_sha256": "e" * 64,
        "bucket_allocation": {"3-4": 5, "5-9": 5, "10+": 5},
        "user_decisions": {"newsletter_embedder": "record_only_mac", "label_subset": "full"}, "void_runs": [],
    }


def test_the_committed_record_is_empty_and_refused_field_by_field():
    problems = reg.check_pre_run_record(PREREG["pre_run_record"])
    for field in reg.PRE_RUN_FIELDS:
        if field in ("embeddings_fp16_vs_fp32_cos", "user_decisions"):
            continue  # 칸 안의 값이 비어 있다(아래 형식 검사가 막는다)
        assert any(p.startswith(field) for p in problems), field


def test_a_filled_record_passes_and_each_malformed_value_is_named():
    assert reg.check_pre_run_record(_filled_record()) == []
    for field, bad in (("code_sha", "abc"), ("embeddings_sha256", "x"), ("snapshot_sha256", {"run1": "1" * 64}),
                       ("bucket_allocation", {"small": 15}), ("export_rows", 0),
                       ("user_decisions", {"newsletter_embedder": "x", "label_subset": "full"}),
                       ("embeddings_fp16_vs_fp32_cos", {"p10": None, "min": 0.9, "n": 100})):
        record = _filled_record()
        record[field] = bad
        assert any(p.startswith(field) for p in reg.check_pre_run_record(record)), field
    record = _filled_record()
    record["day_windows_utc"] = record["day_windows_utc"][:2]
    assert reg.check_pre_run_record(record)


# ---------------------------------------------------------------- 3. 코드 동일성 (a)~(d)


def _git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                         env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """원격(bare) + 클론. main에 코드와 사전 등록 yaml을 올린 커밋이 code_sha다."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    (work / "ai_workspace").mkdir()
    (work / "ai_workspace" / "x.py").write_text("X = 1\n")
    (work / "docs").mkdir()
    (work / "docs" / "a.md").write_text("a\n")
    prereg_path = work / reg.PREREG_REL
    prereg_path.parent.mkdir(parents=True)
    prereg_path.write_text(yaml.safe_dump(PREREG, allow_unicode=True))
    (work / ".gitignore").write_text("/data/\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "code")
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _commit(work, path, text, *, push=True):
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"change {path}")
    if push:
        _git(work, "push", "-q", "origin", "main")


def test_identity_passes_on_the_code_sha_and_after_doc_and_record_only_commits(repo):
    code_sha = _git(repo, "rev-parse", "HEAD")
    assert reg.check_code_identity(repo, code_sha) == []
    _commit(repo, "docs/a.md", "doc change\n")
    data = yaml.safe_load((repo / reg.PREREG_REL).read_text())
    data["pre_run_record"]["code_sha"] = code_sha
    _commit(repo, reg.PREREG_REL, yaml.safe_dump(data, allow_unicode=True))
    _commit(repo, reg.SAMPLE_MANIFEST_REL, "{}\n")
    assert reg.check_code_identity(repo, code_sha) == []


def test_a_head_that_is_not_on_main_is_refused(repo):
    code_sha = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/a.md", "local only\n", push=False)
    assert any(p.startswith("(a)") for p in reg.check_code_identity(repo, code_sha))


def test_a_dirty_tree_is_refused(repo):
    code_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "docs" / "a.md").write_text("edited\n")
    assert any(p.startswith("(b)") for p in reg.check_code_identity(repo, code_sha))
    (repo / "docs" / "a.md").write_text("a\n")
    (repo / "untracked.txt").write_text("u\n")
    assert any(p.startswith("(b)") for p in reg.check_code_identity(repo, code_sha))


def test_code_paths_that_differ_from_code_sha_are_refused(repo):
    code_sha = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "ai_workspace/x.py", "X = 2\n")
    assert any(p.startswith("(c)") for p in reg.check_code_identity(repo, code_sha))


def test_rule_key_changes_need_an_amendment_with_changed_keys(repo):
    code_sha = _git(repo, "rev-parse", "HEAD")
    data = yaml.safe_load((repo / reg.PREREG_REL).read_text())
    data["budget"]["cap_usd_valid_run"] = 1.3
    _commit(repo, reg.PREREG_REL, yaml.safe_dump(data, allow_unicode=True))
    assert any(p.startswith("(d)") and "budget.cap_usd_valid_run" in p for p in reg.check_code_identity(repo, code_sha))
    data["amendments"] = [{"date": "2026-10-20", "reason": "t", "results_viewed": "no",
                           "changed_keys": ["budget.cap_usd_valid_run"]}]
    _commit(repo, reg.PREREG_REL, yaml.safe_dump(data, allow_unicode=True))
    assert reg.check_code_identity(repo, code_sha) == []


def test_a_code_sha_that_is_not_on_main_is_refused(repo):
    _commit(repo, "docs/a.md", "x\n", push=False)
    local = _git(repo, "rev-parse", "HEAD")
    problems = reg.check_code_identity(repo, local)
    assert any("code_sha" in p and "main에 없다" in p for p in problems)
    assert reg.check_code_identity(repo, "z" * 40)
