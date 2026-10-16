"""E0 기준선 측정의 등록값과 시작 검사 (ADR 0009 A8.2·A8.10, evaluation/llm/preregistration/e0-baseline-v1.yaml).

러너(`evaluation.llm.e0_baseline`)가 쓰는 값은 전부 이 모듈의 상수다. 사람이 읽는 원본은 A8이고 기계가 읽는 원본은
yaml이다. 셋이 어긋나지 않게 두 겹으로 묶는다.
- 테스트(tests/evaluation/test_e0_registered.py)가 yaml을 읽어 `effective_config()`와 같은지 본다.
- 러너는 시작할 때 같은 비교를 다시 하고(환경변수가 Settings를 바꿨을 수 있다), 다르면 시작하지 않는다.
등록값을 바꾸려면 A8.x 개정 + yaml `amendments` + 이 모듈을 고치는 코드 PR이 함께 가야 한다(새 `code_sha`).

시작 검사(A8.2 a~e)도 여기 있다: HEAD가 main에 있는 커밋인지, 작업 트리가 깨끗한지, 코드 경로가 `code_sha`와
같은지, yaml의 규칙 키 변경이 `amendments`로 선언됐는지, `pre_run_record`가 다 채워졌는지, 그리고 러너가 읽은 사전 등록·
표본 매니페스트가 HEAD에 커밋된 그 파일인지.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PREREG_REL = "evaluation/llm/preregistration/e0-baseline-v1.yaml"
PREREG_PATH = REPO_ROOT / PREREG_REL
SAMPLE_MANIFEST_REL = "evaluation/llm/evalsets/e0-baseline-v1.jsonl"
PREREG_ID = "e0-baseline-v1"
BASE_COMMIT = "b64da14"  # A8을 쓴 시점의 main(A8.10의 ai_workspace diff 기준)

# ---------------------------------------------------------------- 등록값 (A8.1·A8.2·A8.4·A8.5·A8.8)

SAMPLE = {
    "function": "evaluation.llm.evalset.stratified_sample",
    "n": 15, "seed": 20261006, "hard_fraction": 0.0, "warmup": 2, "min_size": 3,
    "size_buckets": [[3, 4], [5, 9], [10, None]],
    "short_bucket": "reallocate_as_coded",
}
CLUSTERING = {
    "code": "NewsClusterer.cluster_news",
    "min_cluster_size": 3, "min_samples": 2, "lookback_hours": 24,
    "pseudo_now_kst": "06:00",
    "window": "t_k - 24h <= crawled_at < t_k",
    "days": 3,
    "extend_days_if_short": {"max_days": 5, "need_eval_plus_warmup": 17, "void_if_eval_below": 9},
}
ORDER = {"eval": "sort by (run_k, cluster_id) then random.Random(20261007).shuffle", "warmup_first": True,
         "recorded_in": "sample_manifest.order"}
ORDER_SEED = 20261007

ROLES = {
    "generator": {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
    "judge": {"provider": "gemini", "model": "gemini-3.1-flash-lite"},
    "tone": {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
}
SETTINGS = {
    "FAITHFULNESS_GATE_MODE": "enforce",
    "FAITHFULNESS_BLOCKING_TYPES": "numbers,quotes",
    "TONE_DRIFT_GATE_MODE": "enforce",
    "TONE_DRIFT_BLOCKING_TYPES": "numbers,dates,entities_added",
    "MAX_RETRY_TONE_DRIFT": 1,
    "JUDGE_GATE_MODE": "shadow",
    "JUDGE_MIN_CRITERION_SCORE": 3,
    "JUDGE_MAX_UNSUPPORTED_CLAIMS": 0,
    "MAX_RETRY_CLUSTER_EVAL": 2,
    "MAX_RETRY_NEWSLETTER_EVAL": 3,
    "MAX_RETRY_TONE_VALIDATION": 2,
    "MAX_LLM_CALL_RETRIES": 10,
    "LLM_REQUEST_TIMEOUT_S": 60,
    "LLM_CALL_DEADLINE_S": 180,
    "NEWSLETTER_WORKERS": 3,
    "HDBSCAN_MIN_CLUSTER_SIZE": 3,
    "HDBSCAN_MIN_SAMPLES": 2,
    "CLUSTER_LOOKBACK_HOURS": 24,
}
EFFECTIVE_OUTSIDE_SETTINGS = {
    "resolve_role_config_equals_roles": True,
    "gemini_rate_limiter_min_interval_s": 0.0,
    "LLM_KILL_SWITCH": "unset",
    "kill_switch_file": "absent",
}
CALL_PARAMS = {
    "cluster_eval": {"temperature": 0.1, "max_tokens": 2048},
    "newsletter_content_gen": {"temperature": 0.2, "max_tokens": 8192},
    "newsletter_meta_gen": {"temperature": 0.2, "max_tokens": 1024},
    "newsletter_eval": {"temperature": 0.1, "max_tokens": 2048},
    "tone_convert": {"temperature": 0.4, "max_tokens": 4096},
}
CONCURRENCY = {"function": "pipeline.stages.run_clusters_bounded", "max_workers": 3, "fill": [], "min_target": 0}
PATCHED_NAMES = ["NewsClusterer._load_data_from_db", "workflow.nodes.get_connection",
                 "workflow.nodes.release_connection", "workflow.nodes.save_news_letter"]
# A8.10 덧붙임: db.connection을 막는 가드는 등록되지 않았다 - 러너는 forbid_database=False로 끼운다.
FORBID_DATABASE = False

HTTP_ATTEMPT_FIELDS = [
    "run", "cluster_id", "run_k", "graph_attempt", "purpose", "call_seq", "attempt_seq", "provider", "model_requested",
    "model_responded", "system_fingerprint", "status", "transport_error", "finish_reason", "prompt_chars", "max_tokens",
    "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens_reported", "hidden_tokens", "cost_usd",
    "cost_kind", "latency_s", "started_at",
]
CALL_FIELDS = [
    "run", "cluster_id", "run_k", "graph_attempt", "purpose", "call_seq", "provider", "model", "prompt_sha12",
    "prompt_chars", "temperature", "max_tokens", "attempts", "http_attempts", "schema_failures", "parsed", "responded",
    "first_try_schema_pass", "error", "http_status", "latency_s", "wall_s", "cost_usd", "parsed_keyword_count",
    "parsed_invalid_category_count", "started_at",
]

BUDGET = {
    "cap_usd_valid_run": 1.00,
    "cap_usd_e0_cumulative": 1.50,
    "expected_usd": 0.54,
    "worst_case_tokens_per_char": 1.5,
    "stop_on_infra": [401, 402, 403, 404, "kill_switch", "missing_api_key"],
    "void_if_processed_eval_clusters_below": 8,
    "wall_clock_cap_hours": 2,
    "rate_limit_429": "measure_only",
    "preflight": {"schemas": ["ClusterEval", "NewsletterContent", "NewsletterMeta", "NewsletterEvalV2", "ToneResult"],
                  "input": "synthetic", "calls": 5, "on_infra_failure": "stop_before_warmup",
                  "on_model_failure": "record_and_proceed"},
    "valid_runs": 1,
    "void_reasons": ["code_identity", "settings_mismatch", "sha_mismatch", "infra_below_8", "calls_outside_runner",
                     "frame_too_small"],
}
INFRA_HTTP_STATUSES = (401, 402, 403, 404)

OUTCOMES = ["completed", "skipped", "failed:<failure_reason>", "error", "aborted_budget", "aborted_infra",
            "interrupted", "not_started"]
ROOT_CAUSES = ["aborted_infra", "aborted_budget", "interrupted", "exception", "save_error", "transport", "length",
               "content_filter", "schema", "faithfulness_block", "judge_fail", "cluster_eval_fail", "generator_fallback",
               "judge_unavailable", "none"]
NATURAL_OUTCOMES = ("completed", "skipped", "failed", "error")  # n_p의 분모(A8.3)

RELABEL = {"outputs": 5, "clusters": 3, "min_hours_between_rounds": 48}
RELABEL_REDUCED = {"outputs": 3, "clusters": 2}
LABEL_SUBSETS = ("full", "reduced_first10")
NEWSLETTER_EMBEDDERS = ("record_only_mac", "real_colab_cpu")
REDUCED_FIRST_N = 10

# 지출 상한(ADR 0035)과의 결합 - A8.10 덧붙임(2026-10-17). 클라이언트 안의 상한은 끌 수 없고 기본값(런 $0.20, 일 $0.30)은
# A8.5의 상한($1.00)보다 낮아 등록한 규칙보다 먼저 멈춘다. 러너는 E0 프로세스에서만 런·일 상한을 아래 값으로 두고(바깥 안전망),
# 등록한 E0 가드가 먼저 닿게 한다. 전체 상한은 건드리지 않고 남은 여유가 IN_CLIENT_MIN_TOTAL_HEADROOM_USD 이상인지만 본다.
IN_CLIENT_RUN_DAY_CAP_USD = "2.00"
IN_CLIENT_MIN_TOTAL_HEADROOM_USD = 2.00


def _bucket_spec() -> List[List[Optional[int]]]:
    return [list(b) for b in SAMPLE["size_buckets"]]


# ---------------------------------------------------------------- 실제 값


def live_settings() -> Dict[str, Any]:
    """`config.settings.Settings`에서 지금 이 프로세스가 쓰는 값. MAX_LLM_CALL_RETRIES는 어댑터가 임포트 때 복사한 값까지 본다."""
    from config.settings import Settings
    import core.llm.adapters as adapters
    import core.llm_client as llm_client

    out: Dict[str, Any] = {key: getattr(Settings, key) for key in SETTINGS}
    if adapters.MAX_RETRIES != Settings.MAX_LLM_CALL_RETRIES or llm_client.MAX_RETRIES != Settings.MAX_LLM_CALL_RETRIES:
        out["MAX_LLM_CALL_RETRIES"] = f"adapters={adapters.MAX_RETRIES}, llm_client={llm_client.MAX_RETRIES}"
    return out


def live_outside_settings() -> Dict[str, Any]:
    from config.settings import Settings
    from core.llm.registry import resolve_role_config

    roles_ok = all(resolve_role_config(role) == (spec["provider"], spec["model"]) for role, spec in ROLES.items())
    interval = float(os.getenv("GEMINI_LLM_MIN_INTERVAL", os.getenv("LLM_MIN_INTERVAL", "0.0")))
    env_kill = os.getenv("LLM_KILL_SWITCH", "")
    path = Settings.LLM_KILL_SWITCH_FILE
    return {
        "resolve_role_config_equals_roles": roles_ok,
        "gemini_rate_limiter_min_interval_s": interval,
        "LLM_KILL_SWITCH": "unset" if not env_kill.strip() else "set",
        "kill_switch_file": "present" if (path and os.path.exists(path)) else "absent",
    }


def live_roles() -> Dict[str, Dict[str, str]]:
    from core.llm.registry import resolve_role_config

    return {role: dict(zip(("provider", "model"), resolve_role_config(role))) for role in ROLES}


def effective_config() -> Dict[str, Any]:
    """러너가 실제로 쓸 설정. yaml의 같은 자리와 비교된다(`registered_view`)."""
    return {
        "sample.clustering": {k: CLUSTERING[k] for k in ("min_cluster_size", "min_samples", "lookback_hours",
                                                          "pseudo_now_kst", "window", "days", "extend_days_if_short")},
        "sample.stratified_sample": {**{k: SAMPLE[k] for k in ("function", "n", "seed", "hard_fraction", "warmup",
                                                              "min_size", "short_bucket")},
                                     "size_buckets": _bucket_spec()},
        "sample.processing_order": dict(ORDER),
        "sample.manifest_path": SAMPLE_MANIFEST_REL,
        "pipeline.roles": live_roles(),
        "pipeline.settings": live_settings(),
        "pipeline.effective_values_outside_settings": live_outside_settings(),
        "pipeline.call_params_as_in_code": {k: dict(v) for k, v in CALL_PARAMS.items()},
        "pipeline.reasoning_effort": "unset",
        "pipeline.concurrency": dict(CONCURRENCY),
        "pipeline.declared_deviations[0].patched": list(PATCHED_NAMES),
        "records.http_attempt_fields": list(HTTP_ATTEMPT_FIELDS),
        "records.call_fields": list(CALL_FIELDS),
        "budget": {k: BUDGET[k] for k in ("cap_usd_valid_run", "cap_usd_e0_cumulative", "worst_case_tokens_per_char",
                                          "stop_on_infra", "void_if_processed_eval_clusters_below",
                                          "wall_clock_cap_hours", "rate_limit_429", "valid_runs", "void_reasons")},
        "budget.preflight": dict(BUDGET["preflight"]),
        "metrics.failure_taxonomy": {"outcome": list(OUTCOMES), "root_cause_first_match": list(ROOT_CAUSES)},
        "human_labels.relabel": {**RELABEL},
        "human_labels.reduced_option.relabel": dict(RELABEL_REDUCED),
    }


def _get(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        match = re.fullmatch(r"(\w+)\[(\d+)\]", part)
        if match:
            node = node[match.group(1)][int(match.group(2))]
        else:
            node = node[part]
    return node


def registered_view(prereg: Dict[str, Any]) -> Dict[str, Any]:
    """yaml에서 `effective_config()`와 같은 자리의 값을 꺼낸다."""
    view = {}
    for key in effective_config_keys():
        value = _get(prereg, key)
        if key == "budget":
            value = {k: value[k] for k in ("cap_usd_valid_run", "cap_usd_e0_cumulative", "worst_case_tokens_per_char",
                                           "stop_on_infra", "void_if_processed_eval_clusters_below",
                                           "wall_clock_cap_hours", "rate_limit_429", "valid_runs", "void_reasons")}
        elif key == "budget.preflight":
            value = {k: value[k] for k in BUDGET["preflight"]}
        elif key == "sample.clustering":
            value = {k: value[k] for k in CLUSTERING if k in value and k != "code"}
        elif key == "sample.stratified_sample":
            value = {k: value[k] for k in ("function", "n", "seed", "hard_fraction", "warmup", "min_size",
                                           "short_bucket", "size_buckets")}
        elif key == "metrics.failure_taxonomy":
            value = {k: value[k] for k in ("outcome", "root_cause_first_match")}
        elif key == "human_labels.relabel":
            value = {k: value[k] for k in RELABEL}
        view[key] = value
    return view


def effective_config_keys() -> List[str]:
    return ["sample.clustering", "sample.stratified_sample", "sample.processing_order", "sample.manifest_path",
            "pipeline.roles", "pipeline.settings", "pipeline.effective_values_outside_settings",
            "pipeline.call_params_as_in_code", "pipeline.reasoning_effort", "pipeline.concurrency",
            "pipeline.declared_deviations[0].patched", "records.http_attempt_fields", "records.call_fields", "budget",
            "budget.preflight", "metrics.failure_taxonomy", "human_labels.relabel",
            "human_labels.reduced_option.relabel"]


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def config_differences(prereg: Dict[str, Any]) -> List[str]:
    """등록값과 실제 값이 다른 자리. 비어 있어야 시작한다(A8.2 "설정 중 하나라도 다름"이면 void 사유)."""
    effective, registered = effective_config(), registered_view(prereg)
    out = []
    for key in effective_config_keys():
        if _normalize(effective[key]) != _normalize(registered[key]):
            out.append(f"{key}: 실제 {effective[key]!r} ≠ 등록 {registered[key]!r}")
    return out


def load_prereg(path: Path = PREREG_PATH) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 실행 전 기록 (A8.10, yaml pre_run_record)

PRE_RUN_FIELDS = ("code_sha", "ai_workspace_diff_since_b64da14", "adr_0023_amendment_commit", "export_t0_kst",
                  "export_rows", "export_manifest_sha256", "export_body_expires_at", "embeddings_sha256",
                  "embeddings_fp16_vs_fp32_cos", "colab_cu_used", "day_windows_utc", "snapshot_sha256",
                  "sample_manifest_sha256", "bucket_allocation", "user_decisions")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")


def check_pre_run_record(record: Optional[Dict[str, Any]]) -> List[str]:
    """비어 있거나 형식이 틀린 칸. void_runs 말고는 전부 채워져 있어야 한다(A8.2 e)."""
    if not isinstance(record, dict):
        return ["pre_run_record가 없다"]
    problems = []
    for key in PRE_RUN_FIELDS:
        if record.get(key) in (None, "", [], {}):
            problems.append(f"{key}: 비어 있다")
    if problems:
        return problems
    for key in ("code_sha", "adr_0023_amendment_commit"):
        if not _SHA40.fullmatch(str(record[key])):
            problems.append(f"{key}: 40자 커밋 sha가 아니다")
    for key in ("export_manifest_sha256", "embeddings_sha256", "sample_manifest_sha256"):
        if not _SHA64.fullmatch(str(record[key])):
            problems.append(f"{key}: sha256이 아니다")
    cos = record["embeddings_fp16_vs_fp32_cos"]
    if not isinstance(cos, dict) or any(cos.get(k) is None for k in ("p10", "min", "n")):
        problems.append("embeddings_fp16_vs_fp32_cos: p10·min·n이 다 있어야 한다")
    windows = record["day_windows_utc"]
    if not isinstance(windows, list) or not 3 <= len(windows) <= 5 or any(len(w) != 2 for w in windows):
        problems.append("day_windows_utc: [시작, 끝) 3~5개")
    snaps = record["snapshot_sha256"]
    if not isinstance(snaps, dict) or (isinstance(windows, list) and sorted(snaps) != [f"run{k}" for k in
                                                                                       range(1, len(windows) + 1)]):
        problems.append("snapshot_sha256: 창마다 run1..runK의 sha256")
    elif any(not _SHA64.fullmatch(str(v)) for v in snaps.values()):
        problems.append("snapshot_sha256: sha256이 아닌 값")
    alloc = record["bucket_allocation"]
    if not isinstance(alloc, dict) or sorted(alloc) != ["10+", "3-4", "5-9"]:
        problems.append("bucket_allocation: 3-4 / 5-9 / 10+")
    decisions = record["user_decisions"] or {}
    if decisions.get("newsletter_embedder") not in NEWSLETTER_EMBEDDERS:
        problems.append(f"user_decisions.newsletter_embedder: {NEWSLETTER_EMBEDDERS} 중 하나")
    if decisions.get("label_subset") not in LABEL_SUBSETS:
        problems.append(f"user_decisions.label_subset: {LABEL_SUBSETS} 중 하나")
    if not isinstance(record.get("void_runs", []), list):
        problems.append("void_runs: 목록이어야 한다")
    if not isinstance(record["export_rows"], int) or record["export_rows"] <= 0:
        problems.append("export_rows: 양의 정수")
    return problems


# ---------------------------------------------------------------- 코드 동일성 (A8.2 a~d)

CODE_PATHS = ["ai_workspace", "evaluation", "scripts", "jobs"]
CODE_PATHS_EXCLUDE = ["evaluation/llm/preregistration", "evaluation/llm/evalsets"]
RULE_KEYS_EXCLUDE = ("pre_run_record", "amendments")


def _git(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def flatten(data: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out or {prefix: {}}
    return {prefix: data}


def rule_key_changes(at_code_sha: Dict[str, Any], at_head: Dict[str, Any]) -> List[str]:
    a = flatten({k: v for k, v in (at_code_sha or {}).items() if k not in RULE_KEYS_EXCLUDE})
    b = flatten({k: v for k, v in (at_head or {}).items() if k not in RULE_KEYS_EXCLUDE})
    return sorted(k for k in set(a) | set(b) if a.get(k, "<없음>") != b.get(k, "<없음>"))


def undeclared_rule_changes(at_code_sha: Dict[str, Any], at_head: Dict[str, Any]) -> List[str]:
    declared = [str(k) for amendment in (at_head.get("amendments") or []) for k in (amendment.get("changed_keys") or [])]
    return [k for k in rule_key_changes(at_code_sha, at_head)
            if not any(k == d or k.startswith(d + ".") for d in declared)]


def head_problems(repo: Path, *, fetch: bool = True, remote_main: str = "origin/main",
                  git: Callable[..., "subprocess.CompletedProcess[str]"] = _git) -> List[str]:
    """A8.2 (a)(b): 직전에 fetch한 뒤 HEAD가 main에 있는 커밋인지, 작업 트리가 깨끗한지. 비어 있으면 통과.

    fetch를 먼저 하는 까닭: 낡은 `origin/main` 참조로 재면 main에서 되감긴 커밋도 "main에 있다"로 보인다.
    러너의 시작 검사와 `prepare`가 같이 쓴다."""
    if fetch:
        fetched = git(repo, "fetch", "--quiet", remote_main.split("/", 1)[0])
        if fetched.returncode != 0:
            return ["(a) git fetch 실패 - main과의 관계를 확인할 수 없다"]
    problems: List[str] = []
    if git(repo, "merge-base", "--is-ancestor", "HEAD", remote_main).returncode != 0:
        problems.append(f"(a) HEAD가 {remote_main}에 있는 커밋이 아니다")
    status = git(repo, "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        problems.append("(b) 작업 트리가 깨끗하지 않다(git status --porcelain)")
    return problems


def check_code_identity(repo: Path, code_sha: str, *, fetch: bool = True, remote_main: str = "origin/main",
                        git: Callable[..., "subprocess.CompletedProcess[str]"] = _git) -> List[str]:
    """A8.2 (a)~(d). 비어 있으면 통과. HEAD가 code_sha 자체일 필요는 없다(실행 전 기록 커밋은 자기 해시를 담을 수 없다)."""
    problems = head_problems(repo, fetch=fetch, remote_main=remote_main, git=git)
    if problems and problems[0].startswith("(a) git fetch"):
        return problems
    if not _SHA40.fullmatch(code_sha or ""):
        return problems + ["code_sha: 40자 커밋 sha가 아니다"]
    if git(repo, "cat-file", "-e", f"{code_sha}^{{commit}}").returncode != 0:
        return problems + [f"code_sha {code_sha[:12]}…: 이 저장소에 없는 커밋이다"]
    if git(repo, "merge-base", "--is-ancestor", code_sha, remote_main).returncode != 0:
        problems.append(f"code_sha {code_sha[:12]}…가 {remote_main}에 없다(병합 커밋이어야 한다)")
    spec = list(CODE_PATHS) + [f":(exclude){p}" for p in CODE_PATHS_EXCLUDE]
    diff = git(repo, "diff", "--quiet", code_sha, "HEAD", "--", *spec)
    if diff.returncode == 1:
        problems.append(f"(c) 코드 경로({', '.join(CODE_PATHS)})가 code_sha와 다르다")
    elif diff.returncode != 0:
        problems.append("(c) git diff를 실행하지 못했다")
    shown = git(repo, "show", f"{code_sha}:{PREREG_REL}")
    head_yaml = git(repo, "show", f"HEAD:{PREREG_REL}")
    if shown.returncode != 0 or head_yaml.returncode != 0:
        problems.append("(d) code_sha 또는 HEAD의 사전 등록 yaml을 읽지 못했다")
    else:
        undeclared = undeclared_rule_changes(yaml.safe_load(shown.stdout), yaml.safe_load(head_yaml.stdout))
        if undeclared:
            problems.append("(d) code_sha 뒤에 바뀐 규칙 키가 amendments.changed_keys에 없다: " + ", ".join(undeclared[:10]))
    return problems


def _git_show_bytes(repo: Path, rev_path: str) -> Optional[bytes]:
    out = subprocess.run(["git", "-C", str(repo), "show", rev_path], capture_output=True,
                         env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"})
    return out.stdout if out.returncode == 0 else None


def committed_input_problems(repo: Path, prereg: Dict[str, Any], manifest_path: Path,
                             show: Callable[[Path, str], Optional[bytes]] = _git_show_bytes) -> List[str]:
    """(e) 실행이 쓰는 사전 등록과 표본 매니페스트가 HEAD에 커밋된 바로 그 파일인가. 비어 있으면 통과.

    실행 전 기록과 매니페스트는 main에 병합한 문서 전용 커밋으로만 들어간다(A8.0 4단계). (a)~(d)는 HEAD와 작업 트리를
    보므로, 다른 경로의 사본이나 메모리에서 고친 값으로 시작하면 그 검사를 다 통과하고도 main에 없는 기록으로 유료
    호출을 하게 된다. 그래서 러너가 실제로 읽은 값을 `git show HEAD:<경로>`와 바이트(yaml은 읽은 값) 단위로 대조한다."""
    problems: List[str] = []
    committed_yaml = show(repo, f"HEAD:{PREREG_REL}")
    if committed_yaml is None:
        problems.append(f"(e) HEAD에 {PREREG_REL}이 없다")
    elif yaml.safe_load(committed_yaml.decode("utf-8")) != prereg:
        problems.append(f"(e) 실행에 쓰는 사전 등록이 HEAD에 커밋된 {PREREG_REL}과 다르다 - "
                        "실행 전 기록은 main에 병합한 것만 쓴다(A8.0 4단계)")
    committed_manifest = show(repo, f"HEAD:{SAMPLE_MANIFEST_REL}")
    path = Path(manifest_path)
    if committed_manifest is None:
        problems.append(f"(e) HEAD에 표본 매니페스트 {SAMPLE_MANIFEST_REL}이 없다(실행 전 기록 PR에 함께 커밋한다)")
    elif not path.is_file() or path.read_bytes() != committed_manifest:
        problems.append(f"(e) 실행에 쓰는 표본 매니페스트가 HEAD에 커밋된 {SAMPLE_MANIFEST_REL}과 다르다")
    return problems


def ai_workspace_diff_stat(repo: Path, code_sha: str, git=_git) -> str:
    out = git(repo, "diff", "--stat", BASE_COMMIT, code_sha, "--", "ai_workspace")
    return out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else "(변경 없음)"
