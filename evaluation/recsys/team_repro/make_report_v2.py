"""reports/recsys/team_repro_v2.json(v2.2 형식)을 읽어 한국어 Markdown 리포트
(team_repro_v2.md)를 만든다. 숫자를 다시 계산하지 않는다 - run_repro.py가 저장한 값을
표로 옮긴다.

원칙(v2/v2.1 재검토 대응):
  - 결론 단어(부풀린다/낮춘다/검출되지 않는다, 모델 우위/열위/구분 안 됨)는 전부
    metrics.ci_verdict()가 CI 부호로 고른다. v2는 결론 문장이 숫자와 무관하게
    하드코딩돼 MRR 1.0을 '팀 보고값에 근접'이라고 쓰는 사고가 있었다.
  - '0.897'은 항상 팀 보고값이라는 것, 측정 절차·시점이 기록돼 있지 않다는 것, 검증할
    수 없다는 것을 함께 적는다(TEAM_0897*).
  - 이력서 문장은 팀 귀속/소유 주장 없이, 조건부 문장은 조건(CI 판정)이 맞을 때만 낸다.
  - v2.2: 조기 종료가 일찍 멈춘 원인은 실행마다 기록된 동점 진단 수치로만 말하고,
    버전 비교의 결론은 같은 조건 비교(like_with_like)의 CI에서만 고른다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TEAM_REPRO_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = WORKTREE_ROOT / "reports" / "recsys"

sys.path.insert(0, str(TEAM_REPRO_DIR))
from metrics import ci_verdict  # noqa: E402

TEAM_0897 = (
    "팀 보고값 MRR 0.897(팀 최종 보고서에 값 하나만 적혀 있고 측정 절차·시점은 기록돼 있지 않다. 103명/405건 "
    "스냅샷이 남아 있지 않아 검증할 수 없으며, 팀 최종 코드의 추론 시점과 정답 창으로 계산됐다면 누출로 부풀려졌을 수 있다)"
)
TEAM_0897_SHORT = "팀 보고값 0.897(값 하나만 기재, 측정 절차·시점 미기록, 스냅샷 없음, 검증 불가)"

VERSION_LABELS = {
    "team-final": "team-final (팀 최종 코드)",
    "fix-snapshot": "fix-snapshot (자체 리뷰 중간 수정본)",
    "current": "current (이 브랜치)",
}

BASELINE_LABELS = {
    "random": "random (100회 추첨 평균)",
    "fixed_csv_order": "fixed_csv_order (CSV 행 순서 고정 리스트, 진단용)",
    "popularity": "popularity",
    "recency": "recency",
    "category_match": "category_match",
    "cosine_history": "cosine_history (cold 유저는 popularity 폴백)",
    "onboarding_newsletter_cosine": "onboarding cosine (선택 없으면 popularity 폴백)",
}

# 모델 설정(arm) 표시 이름
ARM_LABELS = {
    "team-final": "team-final: binary, AUC 조기 종료",
    "team_final_fixed100": "team-final: binary, 100라운드 고정",
    "fix-snapshot": "fix-snapshot: lambdarank, 조기 종료(섞음)",
    "current": "current: lambdarank, 조기 종료(섞음)",
    "binary": "current: binary, AUC 조기 종료",
    "fixed100": "current: lambdarank, 100라운드 고정",
    "current_es_engine_order": "current: lambdarank, 조기 종료(엔진 행 순서 - 아티팩트 진단용)",
}

PAIR_LABELS = {
    "as_configured": "config 그대로",
    "same_objective_binary_es": "둘 다 binary·AUC 조기 종료",
    "fixed100_rounds": "둘 다 100라운드 고정",
    "v2_1_engine_order_es": "v2.1 비교 재현(엔진 순서 조기 종료)",
}


# ------------------------------------------------------------------ 포맷 유틸


def get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def pct(x, digits=4):
    if x is None or (isinstance(x, float) and x != x):
        return "N/A"
    return f"{x:.{digits}f}"


def fmt_ms(d: dict, digits=4) -> str:
    if not d or d.get("mean") is None:
        return "N/A"
    return f"{pct(d.get('mean'), digits)} ± {pct(d.get('std'), digits)}"


def fmt_eff(e: dict) -> str:
    if not e or e.get("effect") is None or e.get("effect") != e.get("effect"):
        return "N/A"
    return f"{e['effect']:+.4f} [{e['ci_lo']:+.4f}, {e['ci_hi']:+.4f}]"


def fmt_ci(d: dict) -> str:
    if not d or d.get("mean") is None or d.get("mean") != d.get("mean"):
        return "N/A"
    return f"{pct(d['mean'])} [{pct(d['ci_lo'])}, {pct(d['ci_hi'])}]"


def n_of(e: dict) -> str:
    if not e:
        return "?"
    seeds = e.get("n_seeds_b") or e.get("n_seeds")
    paired = "개, 시드 쌍 재표본" if e.get("paired_seeds") else "개"
    return f"n={e.get('n_users', '?')}명, 시드 {seeds}{paired}"


def say(e: dict, positive: str, negative: str, inconclusive: str) -> str:
    return {
        "positive": positive, "negative": negative, "inconclusive": inconclusive,
    }.get(ci_verdict(e or {}), "계산 불가")


LEAK_WORDS = (
    "누출이 지표를 부풀린다(CI 전체가 0 초과)", "누출이 지표를 낮춘다(CI 전체가 0 미만)",
    "누출 효과가 검출되지 않는다(CI가 0 포함)",
)
DIFF_WORDS = ("양수: CI 전체가 0 초과", "음수: CI 전체가 0 미만", "차이 검출 안 됨: CI가 0 포함")
SHORT_WORDS = ("양수", "음수", "검출 안 됨")
MODEL_WORDS = ("모델 우위", "모델 열위", "구분 안 됨")


def verdict_tag(e: dict, words=DIFF_WORDS) -> str:
    return say(e, *words)


def bi_str(s: dict) -> str:
    """best_iteration 목록 + (1라운드 종료 시드 수, 5라운드 이하 시드 수)."""
    bi = s.get("best_iteration") if s else None
    if not bi or all(b is None for b in bi):
        nt = s.get("n_trees") if s else None
        return f"고정 라운드, 트리 {nt}" if nt else "-"
    n = s.get("n_seeds", len(bi))
    low = s.get("n_low_iteration_seeds")
    low_part = f", 5 이하 {low}/{n}" if low is not None else ""
    return f"{bi} (1라운드 종료 {s.get('n_degenerate_seeds', 0)}/{n}{low_part})"


def tie_range(s: dict, mk="mrr") -> str:
    r = get(s, "tie_draw_range", mk)
    if not r:
        return "-"
    return f"[{pct(r[0], 3)}, {pct(r[1], 3)}]"


def list_str(xs, digits=None) -> str:
    if not xs:
        return "-"
    def one(x):
        if x is None:
            return "-"
        if isinstance(x, float):
            return f"{x:.{digits}f}" if digits is not None else f"{x:g}"
        return str(x)
    return "[" + ", ".join(one(x) for x in xs) + "]"


def mean_of(xs):
    vals = [x for x in (xs or []) if x is not None]
    return (sum(vals) / len(vals)) if vals else None


def rng_str(xs, digits=2) -> str:
    vals = [x for x in (xs or []) if x is not None]
    if not vals:
        return "N/A"
    lo, hi = min(vals), max(vals)
    return pct(lo, digits) if f"{lo:.{digits}f}" == f"{hi:.{digits}f}" else f"{pct(lo, digits)}~{pct(hi, digits)}"


def unshown_verdict_summary(model_entry: dict, baselines) -> str:
    """unshown 필터 비교에서 CI가 0을 벗어난 (베이스라인, 지표)만 '모델 우위/열위'로 묶어 문장으로 만든다."""
    wins, losses = [], []
    for b in baselines:
        for mk, label in (("mrr", "MRR"), ("precision@5", "P@5")):
            e = get(model_entry, b, "unshown_filtered", mk, default={})
            v = ci_verdict(e)
            if v == "positive":
                wins.append(f"{b} {label} {fmt_eff(e)}")
            elif v == "negative":
                losses.append(f"{b} {label} {fmt_eff(e)}")
    parts = []
    if wins:
        parts.append("모델 우위: " + ", ".join(wins))
    if losses:
        parts.append("모델 열위: " + ", ".join(losses))
    if not parts:
        return "모든 비교가 구분되지 않는다"
    return "; ".join(parts) + " (나머지는 구분 안 됨)"


# ------------------------------------------------------------------ 버전 비교 문장


def _pair_items(vc: dict, like_only=None, exclude=()):
    out = []
    for key in vc.get("order", []):
        p = get(vc, "pairs", key, default={})
        if not p or key in exclude:
            continue
        if like_only is not None and bool(p.get("like_with_like")) != like_only:
            continue
        out.append((key, p))
    return out


def _listing(items, field, mk="mrr") -> str:
    return "; ".join(
        f"{PAIR_LABELS.get(k, k)} {fmt_eff(get(p, field, mk, default={}))} "
        f"({say(get(p, field, mk, default={}), *SHORT_WORDS)})"
        for k, p in items
    )


def version_gap_sentences(vc: dict, n_users="?") -> dict:
    """버전 비교(team-final - current)의 결론 문장을 CI 판정에서만 만든다.

    결론은 같은 조건 비교(like_with_like)에서만 고른다. v2.1의 비교(엔진 순서 조기 종료)는
    재현 수치로만 인용한다."""
    like = _pair_items(vc, like_only=True)
    cfg = get(vc, "pairs", "as_configured", default={})
    v21 = get(vc, "pairs", "v2_1_engine_order_es", default={})
    out = {}

    def verdicts(field):
        return [ci_verdict(get(p, field, "mrr", default={})) for _, p in like]

    # 1차(point-in-time) 격차
    pit_all = _pair_items(vc, exclude=("v2_1_engine_order_es",))
    pit_v = [ci_verdict(get(p, "primary", "mrr", default={})) for _, p in pit_all]
    if pit_v and all(v == "inconclusive" for v in pit_v):
        out["pit"] = "point-in-time(1차) 추론에서는 어느 조건에서도 버전 격차가 검출되지 않는다"
    else:
        out["pit"] = f"1차 격차의 판정이 조건에 따라 다르다({_listing(pit_all, 'primary')})"

    # 2차(팀 방식 추론 시점) 격차
    aw_v = verdicts("as_written")
    if aw_v and all(v == "inconclusive" for v in aw_v):
        out["aw"] = "2차(팀 방식 추론 시점) 격차도 같은 조건 비교에서는 검출되지 않는다"
    elif aw_v and all(v == "positive" for v in aw_v):
        out["aw"] = "2차(팀 방식 추론 시점) 격차는 같은 조건 비교 모두에서 양수다(team-final이 높다)"
    else:
        out["aw"] = "2차(팀 방식 추론 시점) 격차의 판정이 같은 조건 비교 안에서 갈린다"
    if ci_verdict(get(v21, "as_written", "mrr", default={})) == "positive" and not (aw_v and all(v == "positive" for v in aw_v)):
        out["aw"] += (
            f" - v2.1이 보고한 양수 2차 격차는 current를 엔진 행 순서 그대로 조기 종료한 비교"
            f"({fmt_eff(get(v21, 'as_written', 'mrr', default={}))})에서 나온 값이다"
        )

    # 누출 효과의 차이(DiD)
    did_v = verdicts("leak_effect_did")
    v21_did = get(v21, "leak_effect_did", "mrr", default={})
    cfg_did = get(cfg, "leak_effect_did", "mrr", default={})
    if did_v and all(v == "inconclusive" for v in did_v):
        text = "누출 효과의 차이(DiD)는 같은 조건 비교 어디에서도 검출되지 않는다"
        out["did_like_with_like"] = "inconclusive"
    elif did_v and all(v == "positive" for v in did_v):
        text = (
            "누출 효과의 차이(DiD)는 같은 조건 비교 모두에서 양수다 - team-final 쪽 누출 효과가 더 크다는 것과 "
            f"일관되지만, 합성 유저 {n_users}명 위의 결과라 코드 버전이 원인이라는 인과 문장으로는 쓰지 않는다"
        )
        out["did_like_with_like"] = "positive"
    else:
        text = "누출 효과의 차이(DiD)의 판정이 같은 조건 비교 안에서 갈린다 - 코드 버전에 따른 누출 민감도 차이는 판단할 수 없다"
        out["did_like_with_like"] = "mixed"
    if ci_verdict(v21_did) == "positive" and out["did_like_with_like"] != "positive":
        text += (
            f". v2.1이 보고한 양수 DiD는 current를 엔진 행 순서 그대로 조기 종료한 모델(트리 수 "
            f"{list_str(v21.get('current_n_trees'))})과의 비교({fmt_eff(v21_did)})에서 나온 값이다. 조기 종료 아티팩트로 "
            "생긴 학습 길이 차이가 섞여 있으므로 'team-final 코드가 추론 시점 누출에 더 민감하다'는 v2.1의 문장은 철회한다"
        )
    elif v21_did and ci_verdict(v21_did) != "positive":
        text += f". v2.1의 양수 DiD는 이번 재현에서 {fmt_eff(v21_did)}({say(v21_did, *SHORT_WORDS)})다"
    if ci_verdict(cfg_did) in ("positive", "negative") and out["did_like_with_like"] != ci_verdict(cfg_did):
        text += (
            f". 각 버전 config 그대로의 비교에서는 DiD가 {fmt_eff(cfg_did)}({say(cfg_did, *SHORT_WORDS)})이지만, 이 비교는 "
            "objective와 조기 종료 지표가 함께 달라 코드 버전의 성질로 읽지 않는다"
        )
    out["did"] = text
    return out


# ------------------------------------------------------------------ 본문


def main() -> None:
    data = json.loads((REPORT_DIR / "team_repro_v2.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    ds = data["data_structure"]
    headline = data["headline"]
    hci = data["headline_ci"]
    vc = data.get("version_comparison", {})
    mle = data.get("model_leak_effects", {})
    art = data.get("early_stopping_tie_artefact", {})
    tfw = data.get("team_final_written", {})
    gen = data.get("generator_split", {})
    base_team = get(data, "baselines", "team_split", "baselines", default={})
    base_team_meta = get(data, "baselines", "team_split", "meta", default={})
    base_gen = get(data, "baselines", "generator_split", "baselines", default={})
    base_small = get(data, "baselines", "small_recent_15", "baselines", default={})
    base_small_meta = get(data, "baselines", "small_recent_15", "meta", default={})
    mvb = data.get("model_vs_baseline", {})
    small_mvb = data.get("small_pool_model_vs_baseline", {})
    decomp = data.get("decomposition_summary", {})
    boot = data.get("decomposition_bootstrap", {})
    repro = data.get("reproduction_check_vs_v21", {})
    cr = meta["category_recovery"]
    seeds_main = get(meta, "seeds", "current_team_final_and_all_current_arms", default=[])
    seeds_fs = get(meta, "seeds", "fix_snapshot", default=[])
    n_users = ds["n_evaluated_users_in_answer_window"]
    low_max = meta.get("low_best_iteration_max", 5)

    lines: list = []
    A = lines.append

    A(f"# 팀 베이스라인 재현 및 분해 실험 (team_repro {meta.get('report_version', 'v2.2')})")
    A("")
    A(
        "> v1([`team_repro_v1.md`](./team_repro_v1.md))은 어드버서리얼 방법론 검토에서 \"unsound\" 판정을 받아 v2로 "
        "재작성했다. v2는 재검토에서 \"sound-with-caveats, blocker 2건\"을 받아 v2.1로 고쳤고(team-final-as-written 정답 "
        "정의 오류, 팀에 대한 사실이 아닌 귀속), v2.1은 다시 \"sound-with-caveats, major 2건\"을 받아 이 v2.2로 고쳤다. "
        "v2.1 대비 바뀐 것:"
    )
    A(">")
    A(
        "> 1. **조기 종료 '붕괴'의 원인 정정 (MAJOR).** v2.1은 lambdarank 조기 종료가 트리 1개에서 멈추는 시드를 "
        "'퇴화'로 표시하고 원인을 작은 inner-valid·정답 밀도·learning_rate로 추정했다. 틀린 추정이었다. 원인은 조기 종료 "
        "지표의 동점 처리다: LightGBM의 NDCG는 동점을 행 순서로 깨는데, 엔진의 학습 데이터 생성이 positive를 negative보다 "
        "먼저 쌓아 inner-valid의 모든 그룹에서 positive가 첫 행이 된다. v2.2는 조기 종료용 inner-valid 행을 고정 시드로 "
        "섞어 쓰고(모든 arm 공통), 엔진 순서 그대로의 조기 종료는 아티팩트를 수치로 보이는 진단 arm으로만 남긴다(0절 4번, 4-0·4-1절)."
    )
    A(
        "> 2. **버전 비교의 교란 제거 (MAJOR).** v2.1의 team-final − current 비교는 한쪽(binary·AUC 조기 종료, 트리 100개 "
        "이상)과 다른 쪽(lambdarank·NDCG 조기 종료, 5개 시드 중 3개가 트리 1개)의 학습 길이가 달랐다. 그래서 '2차 격차가 "
        "양수', 'team-final 코드가 추론 시점 누출에 더 민감하다(DiD 양수)'는 문장은 코드 버전의 성질로 읽을 수 없었다. v2.2는 "
        "같은 조건끼리 비교한다: 둘 다 binary·AUC 조기 종료, 둘 다 조기 종료 없는 100라운드. 결론은 그 CI에서만 고른다(0절 2번, 2-3절)."
    )
    A(
        "> 3. 그 밖의 정정: 팀 보고값의 '한 시점에 한 번 측정' 표현 삭제(출처 없는 서술), team-final-as-written의 '클릭만' "
        "번역을 확정이 아닌 가장 그럴듯한 번역으로 표기, 100라운드 arm이 inner-train만 쓴다는 것과 unshown 필터가 재랭킹이 "
        "아니라는 것 명시, FIX #4 문장에 검출된 설정 수와 다중비교 미보정 명시, 이력서 문장의 current 수치를 설정별 범위로 교체."
    )
    A("")

    git = meta["git"]
    A(f"- 생성 시각: {meta['generated_at']} (리포트 버전 {meta.get('report_version', '?')})")
    A(f"- 하네스 HEAD SHA: `{git['harness_sha']}` (브랜치 `{git.get('current_branch', '?')}`, 작업 트리 dirty: {git.get('harness_dirty')})")
    ih = git.get("input_content_hashes", {})
    if ih:
        A(
            f"- 실행 코드 내용 해시(캐시 키): 하네스 pipeline 입력 `{ih.get('harness_pipeline_inputs', '?')[:16]}`, "
            f"current 엔진 `{ih.get('current_engine', '?')[:16]}`"
        )
    A(f"- team-final(git tag `team-final`) SHA: `{git['team_final_sha']}`")
    A(f"- fix-snapshot(브랜치 `port/fix-snapshot`) SHA: `{git['fix_snapshot_sha']}`")
    A(f"- 고정 정답 구간 시작 시각(answer_start, team_split의 모든 arm/시드/버전 공유): `{meta['answer_start']}`")
    A(
        f"- 시드: current/team-final과 모든 분해·민감도 arm·generator_split {list_str(seeds_main)} ({len(seeds_main)}개), "
        f"fix-snapshot {list_str(seeds_fs)} ({len(seeds_fs)}개). 동점 무작위 추첨 {meta.get('tie_draws')}회, random 베이스라인 "
        f"{meta.get('random_baseline_draws')}회 추첨 평균, 부트스트랩 {meta.get('n_boot')}회."
    )
    A(
        f"- 조기 종료용 inner-valid 행 순서: `{meta.get('es_valid_order_default', 'shuffled')}`(고정 시드로 섞음, 모든 arm 공통). "
        "엔진 순서 그대로는 `current_es_engine_order` 진단 arm에만 쓴다."
    )
    A("- 데이터 sha256:")
    for k, v in meta["data_sha256"].items():
        A(f"  - `{k}`: `{v}`")
    A("- config 해시(버전|objective override -> sha256[:16]):")
    for k, v in meta["config_hashes"].items():
        A(f"  - `{k}`: `{v}`")
    if meta.get("config_overrides"):
        A("- 적용된 objective override:")
        for k, v in meta["config_overrides"].items():
            A(f"  - `{k}`: {', '.join(v)}")
    A("")

    # ---------------------------------------------------------------- 0. 요약
    cur_p = get(headline, "current", "primary_summary", default={})
    cur_tie = get(headline, "current", "primary_tie_random_summary", default={})
    tf_p = get(headline, "team-final", "primary_summary", default={})
    tf_aw = get(headline, "team-final", "as_written_summary", default={})
    le_tf = get(hci, "team-final", "leak_effect", default={})
    # 누출 효과 문장은 설정별 표(model_leak_effects)와 같은 값을 쓴다(같은 실행의 같은 집계).
    tf_aw_mean = get(mle, "team-final", "as_written_mrr_mean", default=get(tf_aw, "mrr", "mean"))
    tf_p_mean = get(mle, "team-final", "primary_mrr_mean", default=get(tf_p, "mrr", "mean"))
    le_tf_mrr = get(mle, "team-final", "leak_effect", "mrr", default=le_tf.get("mrr"))

    A("## 0. 요약")
    A("")
    A(
        f"이 스냅샷은 LLM 페르소나 {ds['n_users_total']}명의 합성 클릭이고 평가 대상은 {n_users}명이다. 절대 수치는 성능이 아니라 "
        "**코드 결함이 지표를 어느 방향으로 움직이는지 보는 화이트박스 진단**으로만 읽는다. 아래 결론 단어는 모두 95% CI "
        "부호에서 나온다(다중비교 보정 없음)."
    )
    A("")

    # 1) 추론 시점 누출 - 모델 설정별
    cur_settings = [k for k in ("current", "binary", "fixed100") if k in mle]
    leak_all = [k for k in ("team-final", "team_final_fixed100", "fix-snapshot", "current", "binary", "fixed100") if k in mle]
    leak_verdicts = {k: ci_verdict(get(mle, k, "leak_effect", "mrr", default={})) for k in leak_all}
    cur_leak_text = ", ".join(
        f"{ARM_LABELS[k].split(': ', 1)[1]} {pct(get(mle, k, 'as_written_mrr_mean'))} → {pct(get(mle, k, 'primary_mrr_mean'))} "
        f"({fmt_eff(get(mle, k, 'leak_effect', 'mrr', default={}))})"
        for k in cur_settings
    )
    if leak_verdicts and all(v == "positive" for v in leak_verdicts.values()):
        leak_concl = f"살펴본 {len(leak_verdicts)}개 모델 설정 모두에서 누출이 지표를 부풀린다(CI 전체가 0 초과)"
    else:
        leak_concl = "설정별 판정: " + ", ".join(
            f"{ARM_LABELS.get(k, k)} {say(get(mle, k, 'leak_effect', 'mrr', default={}), *SHORT_WORDS)}" for k in leak_all
        )
    A(
        f"1. **추론 시점 누출.** 같은 학습 모델을 팀 방식 추론 시점(2차, 데이터셋 끝 시각)과 point-in-time(1차)으로 추론해 "
        f"비교했다(2차 → 1차 MRR, 괄호는 2차 − 1차의 쌍 CI, {n_of(le_tf_mrr)}). team-final: {pct(tf_aw_mean)} → "
        f"{pct(tf_p_mean)} ({fmt_eff(le_tf_mrr)}). current: {cur_leak_text}. {leak_concl} (2-2절)."
    )

    # 2) 버전 격차 - 같은 조건 비교
    vg = version_gap_sentences(vc, n_users)
    like_items = _pair_items(vc, like_only=True)
    A(
        f"2. **v1의 'team-final이 current보다 낫다'(MRR 0.849 vs 0.772) 격차.** team-final − current를 같은 조건끼리 비교했다"
        f"(전체 표는 2-3절). {vg['pit']}. {vg['aw']}(2차 MRR 격차: {_listing(like_items, 'as_written')}). {vg['did']} "
        f"(같은 조건 비교의 DiD: {_listing(like_items, 'leak_effect_did')})."
    )

    # 3) 베이스라인
    cur_mvb = mvb.get("current_lambdarank_es", {})
    pop = get(cur_mvb, "popularity", "plain", default={})
    rnd = get(cur_mvb, "random", "plain", default={})
    onb = get(cur_mvb, "onboarding_newsletter_cosine", "plain", default={})
    cosh = get(cur_mvb, "cosine_history", "plain", default={})
    strong = ["popularity", "onboarding_newsletter_cosine", "cosine_history"]
    wins = [
        b for b in strong
        for mk in ("mrr", "precision@5")
        if ci_verdict(get(cur_mvb, b, "plain", mk, default={})) == "positive"
    ]
    any_model_wins = sorted({
        f"{mname}/{b}"
        for mname, entries in mvb.items()
        for b in strong
        for mk in ("mrr", "precision@5")
        if ci_verdict(get(entries, b, "plain", mk, default={})) == "positive"
    })
    bl_sentence = (
        f"3. **베이스라인 대비(1차, 같은 정답·후보·answer_start, 쌍 nested bootstrap).** current 모델(lambdarank, 섞은 조기 "
        f"종료) − popularity: P@5 {fmt_eff(pop.get('precision@5'))} ({say(pop.get('precision@5'), *MODEL_WORDS)}), MRR "
        f"{fmt_eff(pop.get('mrr'))} ({say(pop.get('mrr'), *MODEL_WORDS)}). − random: MRR {fmt_eff(rnd.get('mrr'))} "
        f"({say(rnd.get('mrr'), *MODEL_WORDS)}), P@5 {fmt_eff(rnd.get('precision@5'))} "
        f"({say(rnd.get('precision@5'), *MODEL_WORDS)}). − onboarding cosine MRR {fmt_eff(onb.get('mrr'))} "
        f"({say(onb.get('mrr'), *MODEL_WORDS)}), − cosine_history MRR {fmt_eff(cosh.get('mrr'))} "
        f"({say(cosh.get('mrr'), *MODEL_WORDS)})."
    )
    if not wins:
        bl_sentence += (
            " popularity·onboarding cosine·cosine_history 중 어느 것에 대해서도 모델 우위 CI가 없다 - v1의 "
            "'모델이 베이스라인보다 낫다'는 결론은 철회한다."
        )
    else:
        bl_sentence += f" CI가 0을 넘는(모델 우위) 비교: {', '.join(sorted(set(wins)))}."
    if not any_model_wins:
        bl_sentence += " 다른 모델 설정(binary, 100라운드, team-final)에서도 이 세 베이스라인에 대한 우위 CI는 없다."
    elif not wins:
        bl_sentence += f" 다른 모델 설정에서 우위 CI가 나온 비교: {', '.join(any_model_wins)} - 설정 하나의 결과라 일반화하지 않는다."
    unshown_mixed = unshown_verdict_summary(cur_mvb, strong)
    bl_sentence += (
        f" 이전 노출 아이템을 양쪽 모두에서 뺀 unshown 비교(정답은 전부 미노출 아이템이다)에서는 {unshown_mixed} - "
        "필터에 따라 판정이 달라질 수 있어 어느 쪽으로도 '모델이 낫다/못하다'를 일반화하지 않는다(3-3절)."
    )
    A(bl_sentence)

    # 4) 조기 종료 지표 아티팩트
    asum = art.get("summary", {})
    paired = art.get("paired_current", [])
    eng = get(decomp, "current_es_engine_order", "primary", default={})
    es_eff = get(boot, "es_valid_order_engine_vs_shuffled", "mrr", default={})
    it1_engine_mean = mean_of([x.get("iter1_engine_order_ndcg5") for x in paired])
    it1_tie_mean = mean_of([x.get("iter1_tie_expected_ndcg5") for x in paired])
    it1_rev_mean = mean_of([x.get("iter1_reversed_order_ndcg5") for x in paired])
    pf_engine = mean_of([x.get("positive_first_share_engine_order") for x in paired])
    pf_shuf = mean_of([x.get("positive_first_share_shuffled") for x in paired])
    n_art = asum.get("n_seeds", len(paired))
    collapse_seeds = asum.get("collapse_seeds_engine_order", [])
    beat_seeds = asum.get("iter1_beats_best_seeds", [])
    same_seeds = sorted(collapse_seeds) == sorted(beat_seeds)
    art_sentence = (
        f"4. **조기 종료가 1라운드에서 멈춘 원인: 지표의 동점 순서 아티팩트.** LightGBM의 NDCG는 동점을 데이터 행 순서로 깬다"
        f"(모든 점수가 같은 모델의 NDCG@5는 positive가 그룹 첫 행이면 1.0, 마지막 행이면 0.0 - 하네스 테스트로 고정). 엔진의 "
        f"학습 데이터 생성은 positive를 그 negative들보다 먼저 쌓고 이후 정렬이 모두 안정 정렬이라, inner-valid에서 첫 행이 "
        f"positive인 그룹 비율이 {pct(pf_engine, 3)}이다(섞은 뒤 {pct(pf_shuf, 3)}). 그래서 동점이 많은 1라운드의 inner-valid "
        f"NDCG@5가 엔진 순서로는 평균 {pct(it1_engine_mean)}, 동점 무작위 기대값으로는 {pct(it1_tie_mean)}, 역순으로는 "
        f"{pct(it1_rev_mean)}다(부풀림 평균 {pct(asum.get('iter1_inflation_mean'), 4)}, 시드별 "
        f"{pct(asum.get('iter1_inflation_min'), 3)}~{pct(asum.get('iter1_inflation_max'), 3)}). 엔진 순서 그대로 조기 종료하면 "
        f"best_iteration {list_str(eng.get('best_iteration'))}로 {n_art}개 시드 중 {asum.get('n_engine_order_stops_at_1', '?')}개가 "
        f"1라운드에서 멈춘다(v2.1의 기준 arm과 같은 동작). "
    )
    if asum.get("n_engine_order_iter1_beats_tie_fair_best") is not None:
        art_sentence += (
            f"같은 시드의 섞은 조기 종료가 고른 라운드와 1라운드를 비교하면, 엔진 순서 NDCG@5는 {n_art}개 중 "
            f"{asum.get('n_engine_order_iter1_beats_tie_fair_best')}개 시드에서 1라운드 값이 더 높다"
            + ("(이 시드들이 정확히 1라운드에서 멈춘 시드다)" if same_seeds and collapse_seeds else
               f"(시드 {list_str(beat_seeds)}; 1라운드 종료 시드는 {list_str(collapse_seeds)})")
            + f". 같은 비교를 동점 무작위 기대값으로 하면 {asum.get('n_tie_expected_improves_after_iter1')}개 시드에서 고른 "
            "라운드 쪽이 더 높다. "
        )
    art_sentence += (
        f"inner-valid를 섞어 조기 종료하면 best_iteration {list_str(cur_p.get('best_iteration'))}(1라운드 종료 "
        f"{asum.get('n_shuffled_stops_at_1', '?')}/{n_art}, {low_max} 이하 {asum.get('n_shuffled_low_iteration', '?')}/{n_art})이고, "
        f"1차 MRR은 {fmt_ms(eng.get('mrr'))} → {fmt_ms(cur_p.get('mrr'))}다(섞음 − 엔진 순서 {fmt_eff(es_eff)}, "
        f"{verdict_tag(es_eff)}). "
    )
    if (asum.get("n_shuffled_stops_at_1") or 0) > 0:
        art_sentence += (
            f"섞은 뒤에도 {asum.get('n_shuffled_stops_at_1')}개 시드가 1라운드에서 멈췄다 - 이 시드는 동점 순서로 설명되지 않으므로 "
            "계속 표시하고 따로 본다. "
        )
    art_sentence += (
        "v2.1 리포트와 ADR이 원인으로 적은 '작은 inner-valid, 그룹당 정답 밀도, learning_rate에서의 첫 트리 이득'은 근거 없는 "
        "추정이었고 철회한다. 엔진의 운영 학습 경로(`main_lgbm.py` → 시간순 안정 분할 → `LGBMRanker.train`의 NDCG 조기 "
        "종료)도 같은 행 순서와 같은 지표를 쓰므로 같은 문제에 노출돼 있다. 이 브랜치는 엔진을 고치지 않고 하네스의 "
        "inner-valid만 섞었다(4-1절, 6절)."
    )
    A(art_sentence)

    # 5) 팀 정답 정의
    tfw_m = get(tfw, "clicks_only", default={})
    tfw_r = get(tfw, "random_reference", "clicks_only", "aggregate", default={})
    tfw_err_m = get(tfw, "all_rows_definition_error", default={})
    tfw_err_r = get(tfw, "random_reference", "all_rows_definition_error", "aggregate", default={})
    n_ans = get(tfw, "mean_answers_per_user", "clicks_only")
    window_h = ds["dataset_window_seconds"] / 3600
    covers = ds["dataset_window_seconds"] < 6 * 86400
    A(
        f"5. **팀 최종 평가 스크립트의 정답 정의.** `scripts/evaluate_results.py`의 창(NOW()-6일)을 이 아카이브(약 "
        f"{window_h:.1f}시간)에 옮기면 창 안의 클릭(유저당 평균 {pct(n_ans, 1)}건)이 정답이 되고, "
        f"{'창이 학습 구간을 포함한 로그 전체를 덮는다' if covers else '창이 로그 일부만 덮는다'}. 이 정의에서 team-final(팀 방식 "
        f"추론) MRR {fmt_ms(tfw_m.get('mrr'))} / P@5 {fmt_ms(tfw_m.get('precision@5'))}이고, **같은 정의에서 무작위 추천도 "
        f"MRR {pct(tfw_r.get('mrr'))} / P@5 {pct(tfw_r.get('precision@5'))}**다 - 이 정의는 어떤 랭킹이든 부풀린다. "
        f"{TEAM_0897}과 방향은 일관되지만, 이 결과는 그 값을 재현하거나 설명하지 않는다. '창 안의 클릭'은 가장 그럴듯한 "
        f"번역이지 확정이 아니다(2-4절). 노출 행 전체를 정답으로 세는 쪽(v2 리포트의 MRR 1.0)에서는 무작위도 MRR "
        f"{pct(tfw_err_r.get('mrr'))}, 모델 {fmt_ms(tfw_err_m.get('mrr'))}라 랭킹 품질과 무관하다."
    )

    # 6) FIX #4
    lk_es = get(boot, "leakage_lambdarank_early_stopping", "mrr", default={})
    lk_bn = get(boot, "leakage_binary", "mrr", default={})
    lk_fx = get(boot, "leakage_lambdarank_fixed100", "mrr", default={})
    lk_settings = [("lambdarank 조기 종료", lk_es), ("binary", lk_bn), ("lambdarank 100라운드", lk_fx)]
    lk_all_inc = all(ci_verdict(e) == "inconclusive" for _, e in lk_settings)
    lk_detected = [(name, e) for name, e in lk_settings if ci_verdict(e) in ("positive", "negative")]
    lk_null = [name for name, e in lk_settings if ci_verdict(e) == "inconclusive"]
    lk_signs = [e.get("effect") for _, e in lk_settings if e.get("effect") is not None]
    if lk_signs and all(x < 0 for x in lk_signs):
        sign_text = f"점추정 부호는 {len(lk_signs)}개 설정 모두 음수"
    elif lk_signs and all(x > 0 for x in lk_signs):
        sign_text = f"점추정 부호는 {len(lk_signs)}개 설정 모두 양수"
    else:
        sign_text = "점추정 부호는 설정에 따라 다르다"
    selection_text = (
        f"{len(lk_settings)}개 설정 중 {len(lk_detected)}개에서만 검출, 다중비교 보정 없음, {sign_text}"
    )
    A(
        f"6. **학습 시점 히스토리 누출(FIX #4)의 효과**(leaky − fixed, 1차 추론): lambdarank 조기 종료 MRR {fmt_eff(lk_es)} "
        f"({verdict_tag(lk_es)}), binary {fmt_eff(lk_bn)} ({verdict_tag(lk_bn)}), lambdarank 100라운드 {fmt_eff(lk_fx)} "
        f"({verdict_tag(lk_fx)}). "
        + (
            f"세 설정 모두 효과가 검출되지 않았다({sign_text}) - 합성 유저 {n_users}명·시드 {len(seeds_main)}개로는 검정력이 "
            "부족해 '효과 없음'의 증거가 아니라 '판단 불가'다. "
            if lk_all_inc else
            f"{selection_text} - 위 판정을 설정별로만 읽고 대표 수치로 쓰지 않는다"
            + (
                " (leaky가 낮게 나온 설정은, 학습 때 미래 클릭이 섞인 히스토리 유사도에 기대도록 학습된 모델이 "
                "point-in-time 추론에서 그 신호를 잃는 학습-추론 불일치로 설명할 수 있다 - 가설이며 이 실험으로 검증하지 않았다). "
                if any(ci_verdict(e) == "negative" for _, e in lk_detected) else ". "
            )
        )
        + "FIX #4는 팀 프로젝트 이후 자체 리뷰에서 찾은 수정이며 팀 시절 코드에는 없었다."
    )
    A(
        f"7. **데이터 구조.** 유저 {ds['n_users_total']}명 중 {ds['n_cold_users']}명이 정답 구간 이전 클릭이 없는 cold "
        f"유저이고(평가 대상 {n_users}명 중 {ds.get('n_evaluated_cold_users', '?')}명), "
        f"정답 {ds['n_answer_clicks']}건 중 answer_start 이전에 노출된 아이템은 "
        f"{ds.get('n_answers_shown_before_answer_start', '?')}건이다. 정답 밀도가 높아(무작위 P@5 기저율 ≈ "
        f"{pct(ds['random_p5_base_rate_mean_answer_density'])}) 절대 지표를 성능으로 읽을 수 없다."
    )
    A("")

    # ---------------------------------------------------------------- 1. 데이터 구조
    A("## 1. 데이터 구조와 기저율")
    A("")
    A(
        f"- 유저(페르소나) {ds['n_users_total']}명, 뉴스레터 {ds['n_newsletters']}건, 로그 {ds['n_ctr_logs']}건"
        f"({ds['n_clicks']}건 클릭, 클릭률 {pct(ds['click_rate'])}). 로그 구간은 `{ds['dataset_window_start']}` ~ "
        f"`{ds['dataset_window_end']}`(약 {window_h:.2f}시간)."
    )
    A(
        f"- 각 페르소나는 {ds['exposures_per_persona_min']}~{ds['exposures_per_persona_max']}건 노출을 받았다(전체 "
        f"{ds['n_newsletters']}건을 한 번씩). 세션 하나의 평균 길이는 약 {ds['session_span_seconds_mean']/60:.1f}분이다. "
        "그래서 전역 80/20 시간 분할은 사실상 유저 단위 분할이 된다."
    )
    A(
        f"- 고정 정답 구간(`{ds['answer_start']}` 이후): 평가 대상 유저 {n_users}명"
        f"(cold {ds.get('n_evaluated_cold_users', '?')}명 / warm {ds.get('n_evaluated_warm_users', '?')}명), 정답 클릭 "
        f"{ds['n_answer_clicks']}건. 전체 유저 기준 cold {ds['n_cold_users']}명 / warm {ds['n_warm_users']}명."
    )
    A(
        f"- **정답은 answer_start 이전에 노출되지 않은 아이템이다**(정답 {ds['n_answer_clicks']}건 중 이전에 노출된 아이템: "
        f"{ds.get('n_answers_shown_before_answer_start', '?')}건). 노출 한 번짜리 로그라, 이미 노출된 아이템"
        f"(클릭 여부 무관)은 정답이 될 수 없다. 미노출 아이템 중 정답 밀도 평균 {pct(ds.get('answer_density_among_unshown_mean'))}. "
        "그래서 seen 필터(클릭한 것 제거)와 unshown 필터(노출된 것 전부 제거)를 둘 다 보고한다(3-1절)."
    )
    A(
        f"- 무작위 추천의 P@5 기저율(유저별 정답 밀도 평균, 후보 {ds['n_newsletters']}건) ≈ "
        f"**{pct(ds['random_p5_base_rate_mean_answer_density'])}**, 실측 `random` 베이스라인 P@5 "
        f"**{pct(get(base_team, 'random', 'aggregate', 'precision@5'))}**."
    )
    g = ds.get("generator_split", {})
    if g:
        A(
            f"- generator_split 파일은 뉴스레터 id로 나뉜다(train {list_str(g.get('train_id_range'))} {g.get('n_train_items')}건 / "
            f"valid {list_str(g.get('valid_id_range'))} {g.get('n_valid_items')}건). valid가 정확히 가장 최근 생성된 "
            f"{g.get('n_valid_items')}건인가: **{g.get('valid_items_are_exactly_newest')}** (train 최신 생성 "
            f"`{g.get('train_items_max_created_at')}`, valid 최초 생성 `{g.get('valid_items_min_created_at')}`)."
        )
    A("")
    A(
        f"카테고리 복원: 직접 라벨 {cr['n_direct_labels']}건 외 {sum(cr['source_counts'].values()) - cr['n_direct_labels']}건은 "
        f"BGE-M3 kNN(k={cr['chosen_k']})으로 채웠다. LOO 정확도 {pct(cr['loo_accuracy_by_k'][str(cr['chosen_k'])])}"
        f"(Wilson 95% CI [{pct(cr['loo_ci_chosen_k_wilson95'][0])}, {pct(cr['loo_ci_chosen_k_wilson95'][1])}], 카테고리 7개, "
        f"무작위 기대값 ≈14.3%) - {cr['loo_ci_note']} 직접 라벨은 generator_split train 쪽 151건 중 25건(17%), valid 쪽 "
        "44건 중 22건(50%)에만 있어 두 쪽의 `category_match` 지표를 비교할 때는 라벨 품질 비대칭을 감안해야 한다."
    )
    A("")

    # ---------------------------------------------------------------- 2. 프로토콜
    A("## 2. 프로토콜: point-in-time(1차) vs 팀 방식 추론 시점(2차)")
    A("")
    A(
        "**공통 학습**: 학습 행은 고정 answer_start 이전 행만 쓰고, 조기 종료는 그 학습 구간을 다시 시간순으로 나눈 "
        "inner-validation으로 한다(정답 구간을 보지 않는다). inner-validation 행은 고정 시드로 섞어서 넘긴다 - LightGBM의 "
        "NDCG가 동점을 행 순서로 깨기 때문이다(0절 4번; AUC는 동점을 묶어 계산하므로 영향이 없다). 단, 일부 학습 쪽 입력은 "
        "전체 로그를 본다: team-final의 학습 피처는 전역 히스토리를 쓰고, 모든 버전의 negative sampler 제외 집합(유저별 클릭 "
        "아이템)은 answer_start 이후 클릭도 포함한다. 외부 재검토가 이 제외 집합을 answer_start 이전 클릭으로 제한해 따로 "
        "돌려본 결과 효과는 검출되지 않았다(MRR −0.014 [−0.119, +0.085], 3시드 - 이 리포트의 실행에 포함되지 않은 검토자 수치)."
    )
    A(
        "**1차(point-in-time)**: 로그 자체를 answer_start 이전으로 물리적으로 잘라낸 두 번째 로더로 피처를 다시 계산해 "
        "추론한다. team-final처럼 cutoff 개념이 없는 코드에도 안전하다."
    )
    A(
        "**2차(팀 방식 추론 시점)**: **같은 point-in-time 학습 모델**에, 팀 배포 코드의 추론 시점(데이터셋/DB 마지막 로그 "
        "시각, `main_lgbm.py` 디버그 모드 `inference_pipeline`의 `get_db_max_timestamp()`)과 미제한 로그 피처를 적용한다. "
        "팀의 학습 절차 전체를 재현한 것이 아니라 추론 시점만 팀 방식으로 바꾼 것이다. 정답 구간은 1차와 같아서 추론 시점 "
        "누출 하나만 분리된다."
    )
    A("")
    A("### 2-1. 결과표 (team_split, clicks_only, 후보 195건)")
    A("")
    A(
        f"`best_iteration` 옆 괄호는 조기 종료가 1라운드에서 멈춘 시드 수와 {low_max}라운드 이하에서 멈춘 시드 수다. `동점 중앙값`은 "
        "평가 유저별로 최고점과 동점인 아이템 수의 중앙값(시드별)이다. '1차 동점 무작위' 행은 같은 모델 점수에서 동점만 무작위로 깬 "
        f"{meta.get('tie_draws')}회 추첨 평균이고, 괄호는 시드·추첨 전체의 MRR 범위다."
    )
    A("")
    A("| 코드 버전 | 추론 | 시드 | MRR | P@5 | nDCG@5 | Coverage@5 | best_iteration | distinct scores | 동점 중앙값 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for version in ["team-final", "fix-snapshot", "current"]:
        h = headline.get(version, {})
        rows = [
            ("primary_summary", "1차 point-in-time"),
            ("primary_tie_random_summary", "1차 동점 무작위"),
            ("as_written_summary", "2차 팀 방식 추론 시점"),
        ]
        for field, label in rows:
            s = h.get(field) or {}
            if not s:
                continue
            mrr = fmt_ms(s.get("mrr"))
            if field == "primary_tie_random_summary":
                mrr += f" {tie_range(s)}"
            A(
                f"| {VERSION_LABELS[version]} | {label} | {s.get('n_seeds', '-')} | {mrr} | {fmt_ms(s.get('precision@5'))} "
                f"| {fmt_ms(s.get('ndcg@5'))} | {fmt_ms(s.get('coverage@5'))} | {bi_str(s)} "
                f"| {list_str(s.get('n_distinct_scores_primary'))} | {list_str(s.get('top_tie_size_eval_users_median'))} |"
            )
    A("")
    A("시드 x 유저 nested bootstrap 95% CI (평균 [하한, 상한]):")
    A("")
    A("| 코드 버전 | 1차 MRR | 1차 P@5 | 2차 MRR | 2차 P@5 |")
    A("|---|---|---|---|---|")
    for version in ["team-final", "fix-snapshot", "current"]:
        c = hci.get(version, {})
        if not c:
            continue
        A(
            f"| {VERSION_LABELS[version]} | {fmt_ci(get(c, 'primary', 'mrr'))} | {fmt_ci(get(c, 'primary', 'precision@5'))} "
            f"| {fmt_ci(get(c, 'as_written', 'mrr'))} | {fmt_ci(get(c, 'as_written', 'precision@5'))} |"
        )
    A("")

    A("### 2-2. 추론 시점 누출 효과 (2차 − 1차, 같은 모델이라 시드 쌍 재표본)")
    A("")
    A("모델 설정별로 본다. 행마다 objective와 트리 수가 함께 다르므로, 설정이 다른 두 행의 효과 크기 차이를 코드 차이로 읽지 않는다(2-3절).")
    A("")
    A("| 모델 설정 | 트리 수(시드별) | 2차 MRR | 1차 MRR | MRR 효과 | P@5 효과 | 판정(MRR) | n |")
    A("|---|---|---|---|---|---|---|---|")
    for key in ["team-final", "team_final_fixed100", "fix-snapshot", "current", "binary", "fixed100", "current_es_engine_order"]:
        m = mle.get(key)
        if not m:
            continue
        le = m.get("leak_effect", {})
        A(
            f"| {ARM_LABELS.get(key, key)} | {list_str(m.get('n_trees'))} | {pct(m.get('as_written_mrr_mean'))} "
            f"| {pct(m.get('primary_mrr_mean'))} | {fmt_eff(le.get('mrr'))} | {fmt_eff(le.get('precision@5'))} "
            f"| {say(le.get('mrr'), *LEAK_WORDS)} | {n_of(le.get('mrr'))} |"
        )
    A("")
    A("동점을 무작위로 깬 추첨 평균으로 본 같은 효과(헤드라인 3버전):")
    A("")
    A("| 코드 버전 | 동점 무작위 MRR 효과 | 판정 |")
    A("|---|---|---|")
    for version in ["team-final", "fix-snapshot", "current"]:
        ltr = get(hci, version, "leak_effect_tie_random", "mrr", default={})
        if not ltr:
            continue
        A(f"| {VERSION_LABELS[version]} | {fmt_eff(ltr)} | {say(ltr, *LEAK_WORDS)} |")
    A("")

    A("### 2-3. 버전 간 비교 (team-final − current, 같은 조건끼리)")
    A("")
    A(
        "team-final과 current는 코드만 다른 게 아니다. config의 objective가 다르고(binary vs lambdarank), 그에 따라 조기 종료 "
        "지표(AUC vs NDCG)와 멈추는 라운드가 다르다. 그래서 세 가지 조건으로 비교한다. 1·2차 격차는 서로 다른 모델이라 시드를 "
        "독립 재표본하고, DiD는 각 arm 안의 누출 효과(2차 − 1차)의 차이다. 결론은 '같은 조건 비교' 행에서만 고른다."
    )
    A("")
    A("| 비교 조건 | 같은 조건 비교 | 남는 차이 | 트리 수 team-final / current | 1차 MRR 격차 | 2차 MRR 격차 | 누출 효과 차이(DiD) MRR | 판정(1차 / 2차 / DiD) |")
    A("|---|---|---|---|---|---|---|---|")
    for key in vc.get("order", []):
        p = get(vc, "pairs", key, default={})
        if not p:
            continue
        A(
            f"| {PAIR_LABELS.get(key, key)}: {p.get('condition', '')} | {'예' if p.get('like_with_like') else '아니오'} "
            f"| {p.get('remaining_differences', '-')} | {list_str(p.get('team_final_n_trees'))} / {list_str(p.get('current_n_trees'))} "
            f"| {fmt_eff(get(p, 'primary', 'mrr'))} | {fmt_eff(get(p, 'as_written', 'mrr'))} "
            f"| {fmt_eff(get(p, 'leak_effect_did', 'mrr'))} "
            f"| {say(get(p, 'primary', 'mrr'), *SHORT_WORDS)} / {say(get(p, 'as_written', 'mrr'), *SHORT_WORDS)} / "
            f"{say(get(p, 'leak_effect_did', 'mrr'), *SHORT_WORDS)} |"
        )
    A("")
    A("P@5로 본 같은 비교:")
    A("")
    A("| 비교 조건 | 1차 P@5 격차 | 2차 P@5 격차 | DiD P@5 |")
    A("|---|---|---|---|")
    for key in vc.get("order", []):
        p = get(vc, "pairs", key, default={})
        if not p:
            continue
        A(
            f"| {PAIR_LABELS.get(key, key)} | {fmt_eff(get(p, 'primary', 'precision@5'))} "
            f"| {fmt_eff(get(p, 'as_written', 'precision@5'))} | {fmt_eff(get(p, 'leak_effect_did', 'precision@5'))} |"
        )
    A("")
    A(f"해석: {vg['pit']}. {vg['aw']}. {vg['did']}.")
    A("")

    A("### 2-4. team-final-as-written (팀 `scripts/evaluate_results.py`의 정답 정의)")
    A("")
    A(
        "추천은 team-final 모델을 팀 방식 추론 시점(2차)으로 만든 것이고, 정답만 팀 스크립트의 창(`created_at >= NOW()-6일`)으로 "
        "바꿨다. 창 안의 `is_clicked==1` 행을 정답으로 보는 것이 **가장 그럴듯한 번역**이다: 테이블(`user_newsletter_ctr_log`)에 "
        "`is_clicked` 컬럼이 없고, 운영 경로에서 이 테이블에 행을 쓰는 것은 클릭 이벤트 API(`POST /logs/newsletter/click`)다. "
        "다만 팀이 합성 로그를 DB에 어떻게 적재했는지(클릭 행만/전체 행)는 문서로 남아 있지 않아 확정할 수 없다. 그래서 노출 행 "
        "전체를 정답으로 세는 쪽을 상한으로 함께 둔다. 무작위 기준값은 같은 정답 정의 위에서 100회 추첨한 평균이다."
    )
    A("")
    A("| 정답 정의 | 유저당 평균 정답 수 | team-final MRR | team-final P@5 | 무작위 MRR | 무작위 P@5 |")
    A("|---|---|---|---|---|---|")
    A(
        f"| 창 안의 클릭 (가장 그럴듯한 번역) | {pct(n_ans, 1)} | {fmt_ms(tfw_m.get('mrr'))} | {fmt_ms(tfw_m.get('precision@5'))} "
        f"| {pct(tfw_r.get('mrr'))} | {pct(tfw_r.get('precision@5'))} |"
    )
    A(
        f"| 노출 행 전체 (상한 - 랭킹 품질과 무관, v2가 쓴 정의) | {pct(get(tfw, 'mean_answers_per_user', 'all_rows_definition_error'), 1)} "
        f"| {fmt_ms(tfw_err_m.get('mrr'))} | {fmt_ms(tfw_err_m.get('precision@5'))} | {pct(tfw_err_r.get('mrr'))} "
        f"| {pct(tfw_err_r.get('precision@5'))} |"
    )
    A("")
    A(
        f"해석: 팀 스크립트의 창은 이 아카이브에서 학습 구간 클릭까지 정답으로 세므로 어느 번역에서든 무작위도 높은 점수를 받는다. "
        f"{TEAM_0897}을 부풀렸을 수 있는 요인 중 하나(추론 시점 누출과 함께)와 **일관되지만 증명은 아니다**."
    )
    A("")

    # ---------------------------------------------------------------- 3. 베이스라인
    A("## 3. 베이스라인 비교 (모델과 같은 로그/후보 풀/정답 구간)")
    A("")
    A("### 3-1. team_split, 1차 정답 구간 - 전체 / seen 필터 / unshown 필터")
    A("")
    cur_len = get(headline, "current", "primary_unshown_list_len", default={})
    pop_len = get(base_team, "popularity", "unshown_filtered", "list_len_stats", default={})
    A(
        "seen 필터는 answer_start 이전에 클릭한 아이템을, unshown 필터는 이전에 노출된 아이템(클릭 여부 무관) 전부를 추천에서 "
        "뺀다. 두 필터 모두 **top-20 리스트에서 사후 제거**한다(미노출 후보만으로 다시 랭킹하는 것이 아니다). 남은 리스트가 "
        "5건 미만이어도 P@5는 5로 나눈다. unshown 필터 뒤 리스트 길이(평가 유저): current 모델 평균 "
        f"{pct(cur_len.get('mean_len'), 1)}건(5건 미만 비율 {pct(cur_len.get('share_len_lt_5'), 3)}), popularity 평균 "
        f"{pct(pop_len.get('mean_len'), 1)}건(5건 미만 비율 {pct(pop_len.get('share_len_lt_5'), 3)}). "
        f"cold 폴백: {base_team_meta.get('cold_fallback', '-')}."
    )
    A("")
    A("| 방법 | MRR | P@5 | nDCG@5 | Coverage@5 | seen MRR | seen P@5 | unshown MRR | unshown P@5 |")
    A("|---|---|---|---|---|---|---|---|---|")
    for name, m in base_team.items():
        agg = m.get("aggregate", {})
        sf = get(m, "seen_filtered", "aggregate", default={})
        uf = get(m, "unshown_filtered", "aggregate", default={})
        A(
            f"| {BASELINE_LABELS.get(name, name)} | {pct(agg.get('mrr'))} | {pct(agg.get('precision@5'))} "
            f"| {pct(agg.get('ndcg@5'))} | {pct(agg.get('coverage@5'))} | {pct(sf.get('mrr'))} | {pct(sf.get('precision@5'))} "
            f"| {pct(uf.get('mrr'))} | {pct(uf.get('precision@5'))} |"
        )
    model_rows = [
        ("current lambdarank 조기 종료 (결정적 동점)", get(headline, "current", default={}), "primary_summary",
         "primary_seen_filtered_summary", "primary_unshown_filtered_summary"),
        ("current lambdarank 조기 종료 (동점 무작위)", get(headline, "current", default={}), "primary_tie_random_summary", None, None),
        ("current lambdarank 100라운드 고정", decomp.get("fixed100", {}), "primary", "primary_seen_filtered", "primary_unshown_filtered"),
        ("current binary", decomp.get("binary", {}), "primary", "primary_seen_filtered", "primary_unshown_filtered"),
        ("team-final", get(headline, "team-final", default={}), "primary_summary",
         "primary_seen_filtered_summary", "primary_unshown_filtered_summary"),
        ("fix-snapshot", get(headline, "fix-snapshot", default={}), "primary_summary",
         "primary_seen_filtered_summary", "primary_unshown_filtered_summary"),
    ]
    for label, block, f_main, f_seen, f_unshown in model_rows:
        s = block.get(f_main) or {}
        if not s:
            continue

        def cell(block_name, mk):
            if not block_name:
                return "-"  # 동점 무작위 추첨에는 필터 변형을 계산하지 않는다
            return fmt_ms((block.get(block_name) or {}).get(mk))

        A(
            f"| **모델: {label}** (시드 {s.get('n_seeds', '?')}) | {fmt_ms(s.get('mrr'))} | {fmt_ms(s.get('precision@5'))} "
            f"| {fmt_ms(s.get('ndcg@5'))} | {fmt_ms(s.get('coverage@5'))} | {cell(f_seen, 'mrr')} | {cell(f_seen, 'precision@5')} "
            f"| {cell(f_unshown, 'mrr')} | {cell(f_unshown, 'precision@5')} |"
        )
    A("")
    A(
        "`fixed_csv_order`는 뉴스레터 CSV 행 순서를 그대로 쓰는 고정 리스트다. 무작위보다 높게 나오는데, 이 순서는 클릭 수와 "
        "상관이 없어(검토자 측정 Spearman ≈ 0) 우연히 유리한 순서다. 점수가 같은 아이템의 순위도 이 순서로 정해지므로, 동점이 "
        "많은 모델(트리 1개)의 지표는 이 순서의 영향을 받는다."
    )
    A("")

    A("### 3-2. cold vs warm 유저 (1차)")
    A("")
    A("| 방법 | cold MRR | cold P@5 | cold n | warm MRR | warm P@5 | warm n |")
    A("|---|---|---|---|---|---|---|")
    for name, m in base_team.items():
        cold, warm = m.get("cold", {}), m.get("warm", {})
        A(
            f"| {BASELINE_LABELS.get(name, name)} | {pct(cold.get('mrr'))} | {pct(cold.get('precision@5'))} | {cold.get('num_users', 0)} "
            f"| {pct(warm.get('mrr'))} | {pct(warm.get('precision@5'))} | {warm.get('num_users', 0)} |"
        )
    for label, cold, warm in [
        ("current lambdarank 조기 종료", get(headline, "current", "primary_cold", default={}), get(headline, "current", "primary_warm", default={})),
        ("current lambdarank 100라운드", get(decomp, "fixed100", "primary_cold", default={}), get(decomp, "fixed100", "primary_warm", default={})),
        ("current binary", get(decomp, "binary", "primary_cold", default={}), get(decomp, "binary", "primary_warm", default={})),
        ("team-final", get(headline, "team-final", "primary_cold", default={}), get(headline, "team-final", "primary_warm", default={})),
        ("fix-snapshot", get(headline, "fix-snapshot", "primary_cold", default={}), get(headline, "fix-snapshot", "primary_warm", default={})),
    ]:
        if not cold and not warm:
            continue
        A(
            f"| **모델: {label}** (시드 평균) | {pct(cold.get('mrr'))} | {pct(cold.get('precision@5'))} | {cold.get('num_users', 0)} "
            f"| {pct(warm.get('mrr'))} | {pct(warm.get('precision@5'))} | {warm.get('num_users', 0)} |"
        )
    A("")

    A("### 3-3. 모델 − 베이스라인 쌍 비교 (유저 쌍, 모델 시드 재표본, 95% CI)")
    A("")
    A(
        "베이스라인은 결정적(random은 100회 추첨 평균)이라 시드 차원이 없다. 판정은 CI 부호로만 정한다: 모델 우위 / 모델 열위 / "
        "구분 안 됨. unshown 열은 모델과 베이스라인 모두 이전 노출 아이템을 뺀 뒤의 비교다."
    )
    A("")
    A("| 모델 | 베이스라인 | MRR | P@5 | 판정(MRR / P@5) | unshown MRR | unshown P@5 |")
    A("|---|---|---|---|---|---|---|")
    model_names = {
        "current_lambdarank_es": "current lambdarank 조기 종료",
        "current_lambdarank_fixed100": "current lambdarank 100라운드",
        "current_binary": "current binary",
        "team_final": "team-final",
    }
    for mname, mlabel in model_names.items():
        for bname, entry in mvb.get(mname, {}).items():
            pl, us = entry.get("plain", {}), entry.get("unshown_filtered", {})
            A(
                f"| {mlabel} | {bname} | {fmt_eff(pl.get('mrr'))} | {fmt_eff(pl.get('precision@5'))} "
                f"| {say(pl.get('mrr'), *MODEL_WORDS)} / {say(pl.get('precision@5'), *MODEL_WORDS)} "
                f"| {fmt_eff(us.get('mrr'))} | {fmt_eff(us.get('precision@5'))} |"
            )
    A("")

    A("### 3-4. generator_split: 가장 최근 생성된 미노출 아이템 찾기 (cold-item 선호 모델링 평가가 아님)")
    A("")
    A(
        "학습은 `ctr_logs_train.csv`(뉴스레터 id 4~154)만, 정답은 `ctr_logs_valid.csv`(155~198)의 클릭이다. 1절대로 valid 아이템은 "
        "정확히 가장 최근 생성된 44건이고 모든 유저가 train 아이템 151건을 이미 노출받았다. 후보는 195건 전체라, 이 과제는 대부분 "
        "'아직 안 본 최신 아이템 고르기'를 보상한다 - recency 베이스라인이 강한 이유다. 새 아이템에 대한 선호 일반화의 증거로 쓰지 않는다."
    )
    A("")
    A("| 방법 | MRR | P@5 | nDCG@5 | Coverage@5 |")
    A("|---|---|---|---|---|")
    for name, m in base_gen.items():
        agg = m.get("aggregate", {})
        A(
            f"| {BASELINE_LABELS.get(name, name)} | {pct(agg.get('mrr'))} | {pct(agg.get('precision@5'))} "
            f"| {pct(agg.get('ndcg@5'))} | {pct(agg.get('coverage@5'))} |"
        )
    for version, d in gen.items():
        s = d.get("summary") or {}
        if not s:
            continue
        A(
            f"| **모델: {VERSION_LABELS.get(version, version)}** (시드 {s.get('n_seeds', '?')}, best_iteration {bi_str(s)}) "
            f"| {fmt_ms(s.get('mrr'))} | {fmt_ms(s.get('precision@5'))} | {fmt_ms(s.get('ndcg@5'))} | {fmt_ms(s.get('coverage@5'))} |"
        )
    A("")

    gen_rand = get(base_gen, "random", "aggregate", "mrr")
    gen_notes = []
    for version, d in gen.items():
        m = get(d, "summary", "mrr", "mean")
        if m is not None and gen_rand is not None:
            gen_notes.append(f"{VERSION_LABELS.get(version, version)} {pct(m)}({'무작위보다 낮다' if m < gen_rand else '무작위 이상'})")
    if gen_notes:
        A(
            f"점추정 비교(CI 없음): 모델 평균 MRR {', '.join(gen_notes)}, random {pct(gen_rand)}. 학습 로그에 valid 아이템의 "
            "상호작용이 전혀 없으므로 클릭 기반 신호가 없는 최신 아이템을 모델이 낮게 매긴다는 것과 일관된다(가설). 이 결과는 "
            "모델의 새 아이템 일반화에 대해 아무것도 증명하지 않는다 - 과제 자체가 '최신 아이템 찾기'다. 이 프로토콜의 "
            f"inner-valid는 시간순 뒤쪽 그룹이라 정답(최신 아이템)과 분포가 달라, 조기 종료가 {low_max}라운드 이하에서 멈추는 시드가 "
            "섞여 있어도 원인을 동점 순서로 단정하지 않는다(4-1절의 arm별 표 참고)."
        )
        A("")

    # ---------------------------------------------------------------- 4. 분해 실험
    A("## 4. 분해·민감도 실험 (1차 point-in-time)")
    A("")
    A(
        "각 비교는 A와 B가 서로 다른 학습 모델이라 시드를 독립 재표본한다(보수적). 예외는 조기 종료용 inner-valid 행 순서만 "
        "다른 비교(4-1절)로, 같은 시드가 학습 데이터와 트리 열을 공유하므로 시드를 쌍으로 재표본한다. v1의 "
        "`label_assumption`(all_rows)은 negative가 없는 실험이라 v2부터 제거했다."
    )
    A("")
    A("### 4-0. 학습 상태 (모든 arm)")
    A("")
    A(
        f"조기 종료 arm은 모두 섞은 inner-valid를 쓴다('엔진 행 순서' 진단 arm 제외). 100라운드 고정 arm은 **inner-train(학습 "
        f"구간의 앞 80%)만으로** {meta.get('fixed_rounds_sensitivity', 100)}라운드를 학습하고 inner-valid는 쓰지 않는다. "
        f"{meta.get('fixed_rounds_sensitivity', 100)}은 실행 전에 정한 값이고 정답 구간으로 고르지 않았다(참고: 섞은 조기 종료가 "
        f"고른 라운드는 current {list_str(cur_p.get('best_iteration'))})."
    )
    A("")
    A("| arm | 시드 | best_iteration | distinct scores | 동점 중앙값 | MRR (결정적) | MRR (동점 무작위, 범위) | P@5 |")
    A("|---|---|---|---|---|---|---|---|")
    arm_rows = [
        ("current: lambdarank, 조기 종료 (기준)", get(headline, "current", "primary_summary", default={}),
         get(headline, "current", "primary_tie_random_summary", default={})),
    ]
    arm_labels = {
        "current_es_engine_order": "current: lambdarank, 조기 종료(엔진 행 순서 - 아티팩트 진단용, v2.1의 기준 arm)",
        "fixed100": "current: lambdarank, 100라운드 고정",
        "binary": "current: binary, AUC 조기 종료",
        "leaky": "leaky 학습 히스토리, lambdarank 조기 종료",
        "leaky_binary": "leaky 학습 히스토리, binary",
        "leaky_fixed100": "leaky 학습 히스토리, lambdarank 100라운드",
        "impression": "impression negative (group_key=user_id)",
        "small_recent_15": "후보 15건 풀 (정답도 풀 안으로 제한)",
        "team_final_fixed100": "team-final: binary, 100라운드 고정",
    }
    for key, label in arm_labels.items():
        if key in decomp:
            arm_rows.append((label, decomp[key].get("primary") or {}, decomp[key].get("primary_tie_random") or {}))
    for label, s, t in arm_rows:
        if not s:
            continue
        A(
            f"| {label} | {s.get('n_seeds', '-')} | {bi_str(s)} | {list_str(s.get('n_distinct_scores_primary'))} "
            f"| {list_str(s.get('top_tie_size_eval_users_median'))} | {fmt_ms(s.get('mrr'))} "
            f"| {fmt_ms(t.get('mrr'))} {tie_range(t)} | {fmt_ms(s.get('precision@5'))} |"
        )
    A("")

    A("### 4-1. 조기 종료 지표의 동점 순서 아티팩트")
    A("")
    A(
        "같은 시드의 두 실행(엔진 행 순서로 조기 종료 / 섞어서 조기 종료)은 학습 데이터와 시드가 같아 같은 트리 열을 만들고, "
        "멈추는 지점만 다르다. 아래 표의 NDCG@5는 모두 inner-valid에서 하네스가 계산한 값이다(엔진 순서 값은 LightGBM이 기록한 "
        "값과 일치 - 하네스 테스트로 확인). '동점 기대값'은 동점 블록 안 순서가 무작위일 때의 기대값으로 행 순서와 무관하다."
    )
    A("")
    A(
        "| 시드 | 엔진 순서: best_iteration | 섞음: best_iteration | 1라운드 NDCG@5 (엔진 순서 / 동점 기대값 / 역순) | 부풀림 "
        "| positive가 negative와 동점인 그룹 비율(1라운드) | 섞음이 고른 라운드의 NDCG@5 (LightGBM 기록값 / 엔진 순서 / 동점 기대값) "
        "| 엔진 순서에서 1라운드가 더 높은가 | 1차 MRR (엔진 순서 → 섞음) |"
    )
    A("|---|---|---|---|---|---|---|---|---|")
    for x in paired:
        A(
            f"| {x.get('seed')} | {x.get('engine_order_best_iteration')} | {x.get('shuffled_best_iteration')} "
            f"| {pct(x.get('iter1_engine_order_ndcg5'))} / {pct(x.get('iter1_tie_expected_ndcg5'))} / {pct(x.get('iter1_reversed_order_ndcg5'))} "
            f"| {pct(x.get('iter1_inflation'), 4)} | {pct(x.get('iter1_tied_positive_group_share'), 3)} "
            f"| {pct(x.get('shuffled_recorded_ndcg5'))} / {pct(x.get('shuffled_best_engine_order_ndcg5'))} / "
            f"{pct(x.get('shuffled_best_tie_expected_ndcg5'))} "
            f"| {'예' if x.get('engine_order_iter1_beats_tie_fair_best') else '아니오'} "
            f"| {pct(x.get('primary_mrr_engine_order'))} → {pct(x.get('primary_mrr_shuffled'))} |"
        )
    A("")
    es_eff_p5 = get(boot, "es_valid_order_engine_vs_shuffled", "precision@5", default={})
    es_eff_tie = get(boot, "es_valid_order_engine_vs_shuffled_tie_random", "mrr", default={})
    A(
        f"- 1차 지표에 대한 효과(섞음 − 엔진 순서, 시드 쌍 재표본): MRR {fmt_eff(es_eff)} ({verdict_tag(es_eff)}), P@5 "
        f"{fmt_eff(es_eff_p5)} ({verdict_tag(es_eff_p5)}), 동점 무작위 MRR {fmt_eff(es_eff_tie)} ({verdict_tag(es_eff_tie)})."
    )
    A(
        "- 읽는 법: 엔진 순서에서 1라운드 NDCG가 이후 라운드보다 높은 시드에서는 LightGBM이 1라운드를 최선으로 기록하고 "
        "patience(50라운드) 뒤 멈춘다. 같은 시드에서 동점 기대값은 이후 라운드가 더 높다 - 모델은 계속 나아지고 있었고, 멈춘 "
        "이유는 지표였다."
    )
    A(
        "- 섞기는 한 번의 추첨이다. 동점인 행의 순서가 무작위가 될 뿐 기대값 자체를 계산하는 것은 아니어서 잡음이 남는다"
        "(위 표에서 LightGBM 기록값과 동점 기대값의 차이가 그 잡음이다). 학습 프레임의 행 순서는 엔진 그대로 두었다 - "
        "lambdarank의 람다 계산도 점수가 같은 행의 순위를 행 순서로 정하지만, 그 영향은 이 실험에서 분리하지 않았다."
    )
    A("")
    by_arm = art.get("by_arm", {})
    if by_arm:
        A(f"NDCG로 조기 종료한 모든 arm의 상태(1라운드 종료 / {low_max}라운드 이하 시드 수):")
        A("")
        A("| arm | inner-valid 순서 | 그룹 키 / inner-valid 그룹 수(시드별) | best_iteration | 1라운드 종료 | 5 이하 | 첫 행이 positive인 그룹 비율(엔진 순서 → 실제 평가) | 1라운드 NDCG@5 엔진 순서 − 동점 기대값(시드 평균) |")
        A("|---|---|---|---|---|---|---|---|")
        for name, v in by_arm.items():
            n = len(v.get("best_iteration") or [])
            infl = mean_of([
                (a - b) for a, b in zip(v.get("iter1_engine_order_ndcg5") or [], v.get("iter1_tie_expected_ndcg5") or [])
                if a is not None and b is not None
            ])
            A(
                f"| {name} | {v.get('es_valid_order')} | {v.get('rank_group_key', '-')} / {list_str(v.get('n_inner_valid_groups'))} "
                f"| {list_str(v.get('best_iteration'))} | {v.get('n_stops_at_1')}/{n} "
                f"| {v.get('n_low_iteration')}/{n} | {pct(mean_of(v.get('positive_first_share_engine_order')), 3)} → "
                f"{pct(mean_of(v.get('positive_first_share_as_evaluated')), 3)} | {pct(infl, 4)} |"
            )
        A("")
        still_low = {name: v for name, v in by_arm.items() if v.get("es_valid_order") == "shuffled" and (v.get("n_low_iteration") or 0) > 0}
        if still_low:
            A(
                f"섞은 뒤에도 {low_max}라운드 이하에서 멈춘 시드가 있는 arm: "
                + ", ".join(f"{name} {list_str(v.get('best_iteration'))}" for name, v in still_low.items())
                + ". 이 시드들은 동점 순서로 설명되지 않는다(섞은 inner-valid에서도 초기 라운드가 최선으로 기록됐다). **원인은 "
                "확인하지 않았다.** 표의 inner-valid 그룹 수가 수십 개 이하인 arm은 NDCG@5 자체의 잡음이 커서 초기 라운드가 "
                "우연히 최선으로 기록될 수 있다는 것은 가설이다. 해당 arm의 효과는 이 표시와 함께만 읽는다."
            )
        else:
            A(f"섞은 조기 종료 arm 가운데 {low_max}라운드 이하에서 멈춘 시드는 없다.")
        A("")

    A("### 4-2. 학습 시점 히스토리 누출 (FIX #4): fixed(A) → leaky(B)")
    A("")
    leaky_s = get(decomp, "leaky", "primary", default={})
    A(
        "FIX #4는 팀 프로젝트 종료 후 자체 리뷰(2026-07)에서 찾아 이 브랜치에 옮긴(2026-09) 수정이다. 팀 시절 코드는 leaky 쪽이다. "
        f"세 가지 모델 설정 위에서 같은 분해를 했다. lambdarank 조기 종료 행의 두 모델은 섞은 inner-valid로 멈췄다"
        f"(best_iteration fixed {list_str(cur_p.get('best_iteration'))}, leaky {list_str(leaky_s.get('best_iteration'))}). "
        f"{selection_text if not lk_all_inc else '세 설정 모두 검출되지 않았다'}."
    )
    A("")
    A("| 모델 설정 | MRR 효과 (B−A) | P@5 효과 (B−A) | 판정(MRR) | n |")
    A("|---|---|---|---|---|")
    for key, label in [
        ("leakage_lambdarank_early_stopping", "lambdarank 조기 종료"),
        ("leakage_lambdarank_early_stopping_tie_random", "lambdarank 조기 종료, 동점 무작위"),
        ("leakage_binary", "binary 조기 종료"),
        ("leakage_lambdarank_fixed100", "lambdarank 100라운드"),
    ]:
        e = boot.get(key, {})
        if not e:
            continue
        A(
            f"| {label} | {fmt_eff(e.get('mrr'))} | {fmt_eff(e.get('precision@5'))} | {verdict_tag(e.get('mrr'))} "
            f"| {n_of(e.get('mrr'))} |"
        )
    A("")

    A("### 4-3. objective와 학습 라운드")
    A("")
    A("| 비교 (A → B) | MRR 효과 (B−A) | P@5 효과 (B−A) | 판정(MRR) |")
    A("|---|---|---|---|")
    for key, label in [
        ("objective_lambdarank_es_vs_binary", "lambdarank 조기 종료 (FIX #5) → binary (팀 방식)"),
        ("objective_lambdarank_fixed100_vs_binary", "lambdarank 100라운드 → binary"),
        ("rounds_es_vs_fixed100", "lambdarank 조기 종료 → 100라운드 고정"),
        ("rounds_es_vs_fixed100_tie_random", "lambdarank 조기 종료 → 100라운드 고정 (동점 무작위)"),
    ]:
        e = boot.get(key, {})
        if not e:
            continue
        A(f"| {label} | {fmt_eff(e.get('mrr'))} | {fmt_eff(e.get('precision@5'))} | {verdict_tag(e.get('mrr'))} |")
    A("")
    obj_es = get(boot, "objective_lambdarank_es_vs_binary", "mrr", default={})
    obj_fx = get(boot, "objective_lambdarank_fixed100_vs_binary", "mrr", default={})
    if ci_verdict(obj_es) == "inconclusive" and ci_verdict(obj_fx) == "inconclusive":
        obj_text = "두 비교 모두 차이가 검출되지 않는다 - 이 데이터로는 'lambdarank가 낫다/못하다'를 말할 수 없다."
    else:
        obj_text = (
            f"판정: 조기 종료 {say(obj_es, *SHORT_WORDS)}, 100라운드 {say(obj_fx, *SHORT_WORDS)}(B−A, B=binary). 설정 하나·합성 유저 "
            f"{n_users}명의 결과라 objective의 우열로 일반화하지 않는다."
        )
    A(f"{obj_text} FIX #5(lambdarank 전환)도 팀 프로젝트 이후 자체 리뷰에서 나온 수정이다.")
    A("")

    A("### 4-4. negative 출처: random → impression (단일 변수 비교가 아님)")
    A("")
    e = boot.get("negatives_random_vs_impression", {})
    imp = decomp.get("impression", {})
    A(f"- 효과(impression − random): MRR {fmt_eff(e.get('mrr'))}, P@5 {fmt_eff(e.get('precision@5'))} ({verdict_tag(e.get('mrr'))}).")
    A(
        f"- 동시에 바뀌는 것: negative 출처, lambdarank 그룹 키(user_timestamp → user_id), 학습 행 수(random "
        f"{list_str(decomp.get('random_negatives_n_train'))} vs impression {list_str(imp.get('n_train'))}), 각 행의 타임스탬프"
        "(positive 시각 → 노출 시각)."
    )
    A(
        f"- inner 분할에서 양쪽에 걸친 그룹 수(그룹 소속으로 검사): {list_str(imp.get('inner_split_groups_on_both_sides'))}. "
        "v2는 인접 행만 검사해 85명 중 32명이 양쪽에 걸쳤다."
    )
    imp_p = imp.get("primary") or {}
    if (imp_p.get("n_low_iteration_seeds") or 0) > 0:
        A(
            f"- impression arm은 섞은 inner-valid에서도 조기 종료가 {low_max}라운드 이하에서 멈춘 시드가 "
            f"{imp_p.get('n_low_iteration_seeds')}/{imp_p.get('n_seeds', '?')}개다(best_iteration {list_str(imp_p.get('best_iteration'))}; "
            "4-1절 - 동점 순서로 설명되지 않고 원인은 확인하지 않았다). 거의 학습되지 않은 모델이 섞여 있으므로 위 효과 크기를 "
            "negative 출처의 효과로 읽지 않는다."
        )
    A(
        "- 페르소나 전원이 195건 전부를 노출받았으므로 '클릭 안 한 나머지'와 '노출됐지만 클릭 안 함'은 사실상 같은 모집단이다. "
        "이 비교로 노출 로그 negative의 우열을 말하지 않는다."
    )
    A("")

    A("### 4-5. 작은 후보 풀 (small_recent_15) - 같은 풀·같은 정답 위의 풀 내부 베이스라인과만 비교")
    A("")
    A(
        f"후보는 가장 최근 생성일의 {base_small_meta.get('n_candidates', '?')}건이고 정답도 이 풀 안으로 제한한다(평가 유저 "
        f"{base_small_meta.get('n_evaluated_users', '?')}명, 풀 안 정답 밀도 평균 {pct(base_small_meta.get('mean_answer_density'))}). "
        "전체 195건 풀과는 정답 집합이 달라 두 풀의 차이를 '풀 크기 효과'로 읽을 수 없다 - v2의 full_195 대비 효과 행은 뺐다."
    )
    A("")
    A("| 방법 (15건 풀) | MRR | P@5 |")
    A("|---|---|---|")
    for name, m in base_small.items():
        agg = m.get("aggregate", {})
        A(f"| {BASELINE_LABELS.get(name, name)} | {pct(agg.get('mrr'))} | {pct(agg.get('precision@5'))} |")
    ss = get(decomp, "small_recent_15", "primary", default={})
    st = get(decomp, "small_recent_15", "primary_tie_random", default={})
    if ss:
        A(f"| **모델: current lambdarank 조기 종료** (시드 {ss.get('n_seeds', '?')}) | {fmt_ms(ss.get('mrr'))} | {fmt_ms(ss.get('precision@5'))} |")
    if st:
        A(f"| **모델: 같은 모델, 동점 무작위** | {fmt_ms(st.get('mrr'))} {tie_range(st)} | {fmt_ms(st.get('precision@5'))} |")
    A("")
    if small_mvb:
        A("| 모델 − 풀 내부 베이스라인 | MRR | P@5 | 판정(MRR / P@5) | 동점 무작위 MRR |")
        A("|---|---|---|---|---|")
        for bname, entry in small_mvb.items():
            pl, tr = entry.get("plain", {}), entry.get("plain_tie_random", {})
            A(
                f"| {bname} | {fmt_eff(pl.get('mrr'))} | {fmt_eff(pl.get('precision@5'))} "
                f"| {say(pl.get('mrr'), *MODEL_WORDS)} / {say(pl.get('precision@5'), *MODEL_WORDS)} | {fmt_eff(tr.get('mrr'))} |"
            )
        A("")
    A(
        "**padded_400 제거 이유**: 195건을 잡음 섞인 사본으로 400건까지 부풀린 arm은 사본이 학습 negative에도 섞여(원본과 "
        "created_at·카테고리를 공유하고 결코 positive가 되지 않음) 학습 자체를 바꿨다. v2의 효과(MRR −0.19)는 1라운드에서 멈춘 "
        "시드에서만 나왔다(26트리 시드는 0.6545 → 0.6244). 풀 크기 효과로 읽을 수 없어 v2.1부터 실행하지 않는다(코드는 재검토용으로 남김)."
    )
    A("")

    # ---------------------------------------------------------------- 5. 증명 불가
    A("## 5. 이 데이터가 증명할 수 없는 것")
    A("")
    A(
        f"- **실서비스 성능.** 클릭은 LLM 페르소나 {ds['n_users_total']}명에서 LLM이 추론해 만든 라벨이라 `category_match` 같은 "
        f"명시적 신호와 순환성이 있을 수 있다. 세션이 약 {window_h:.1f}시간뿐이라 신선도(half-life 7일) 피처는 사실상 죽은 피처다."
    )
    A(
        f"- **{TEAM_0897_SHORT}의 재현이나 원인 규명.** 이 아카이브는 100명/195건 스냅샷이라 규모가 다르다. 2-4절은 '팀 정답 창이 "
        "학습 구간 클릭을 포함하면 어떤 랭킹도 부풀려진다'를 보여줄 뿐, 그 스냅샷의 검증이 아니다."
    )
    A("- **모델이 베이스라인보다 낫다는 것** (0절 3번, 3-3절).")
    A(
        "- **코드 버전(team-final vs current)에 따른 누출 민감도 차이.** 같은 조건 비교의 CI로만 판단한다(2-3절). objective나 "
        "학습 길이가 다른 두 모델의 누출 효과 차이는 코드 차이가 아니다."
    )
    A(
        "- **엔진 행 순서로 조기 종료한 lambdarank 모델의 수치를 모델 효과로 읽는 것.** 1라운드에서 멈춘 시드는 조기 종료 "
        "지표의 아티팩트이고, 그 모델의 순위는 대부분 동점 처리 순서로 정해진다(4-1절). v2.1의 lambdarank 조기 종료 arm 수치가 여기에 해당한다."
    )
    A(
        "- **섞은 조기 종료가 '올바른' 트리 수를 고른다는 것.** 섞기는 지표가 행 순서에 기대지 않게 할 뿐이다. inner-valid는 "
        "학습 구간의 뒤쪽 20%이고 정답 구간과 유저 구성이 다르다."
    )
    A("- **후보 풀 크기의 효과** (padded_400 제거, small_recent_15는 풀 내부 비교만).")
    A(
        f"- **FIX #4 효과가 0이라는 것.** '검출되지 않음'은 n={n_users}명·시드 {len(seeds_main)}개에서의 판단 불가이지 효과 없음의 "
        "증거가 아니다. 검출된 설정이 있더라도 여러 설정 중 일부이고 보정하지 않았다."
    )
    A(
        f"- **카테고리 의존 지표(`category_match`, `Coverage@k`)의 절대값.** 76%가 kNN으로 채워졌고 그 LOO 정확도가 "
        f"{pct(cr['loo_accuracy_by_k'][str(cr['chosen_k'])])}다."
    )
    A(
        "- **여러 CI를 동시에 보며 '유의하다'고 결론짓는 것.** 이 리포트는 수십 개 구간을 병기하지만 다중비교 보정을 하지 않았다. "
        "nested bootstrap도 시드 3~5개 위의 재표본이라 학습 무작위성을 거칠게만 반영한다."
    )
    A("")

    # ---------------------------------------------------------------- 6. 다음 단계
    A("## 6. 다음 단계 시사점")
    A("")
    A(
        "- **공개 벤치마크 교차검증이 정확도 주장의 전제조건이다** (ADR [0007](../../docs/adr/0007-recsys-offline-evaluation-protocol.md)). "
        "EB-NeRD를 1차로(이미 `data/benchmarks/ebnerd`에 있음 - 라이선스상 이 Mac 밖으로 복사 금지), MIND를 보조로 검토한다."
    )
    A(
        f"- **엔진의 lambdarank 조기 종료를 고친다(미수정).** `main_lgbm.py`의 학습 경로는 positive가 그룹 첫 행인 검증 "
        f"프레임에 NDCG 조기 종료를 그대로 쓴다. 이 아카이브에서 그 순서로 조기 종료하면 {n_art}개 시드 중 "
        f"{asum.get('n_engine_order_stops_at_1', '?')}개가 트리 1개 모델이 된다. 검증 그룹 안의 행을 섞거나 동점에 영향받지 않는 "
        "지표로 조기 종료해야 한다. 이 브랜치는 엔진 소스를 고치지 않았다 - 재현 대상 코드를 바꾸지 않는다는 하네스 원칙 "
        "때문이며, 엔진 수정은 별도 변경으로 남긴다."
    )
    A(
        "- **노출(impression) 로깅을 DB 스키마에 남긴다** - 지금 `user_newsletter_ctr_log`에는 `is_clicked` 컬럼이 없어 "
        "negative가 랜덤 샘플링에만 의존한다."
    )
    A(
        "- **요청 시점 추천**: 지금 구조는 배치로 미리 계산해 저장하므로 `eval_timestamp`가 배치 실행 시점 하나로 고정된다. "
        "요청마다 point-in-time으로 추론하려면 Cartesian(유저 x 전체 뉴스) 방식이 레이턴시상 감당이 안 될 수 있다."
    )
    A(
        "- **team-final의 evaluate_results.py(NOW()-6일) 로직을 앞으로 쓰지 않는다** - `valid_period.json` 기반 정답 구간"
        "(current에 있음)을 표준으로 삼는다."
    )
    A("")

    # ---------------------------------------------------------------- 7. 이력서 문장
    A("## 7. 이력서에 쓸 수 있는 문장과 쓰면 안 되는 문장")
    A("")
    A(
        "추천 모델(LightGBM+MMR)은 팀원이 담당한 부분이다. 아래 문장은 그 모델을 **재현·평가한 작업**에 대한 것이고, "
        "모델을 만들었다는 뜻이 아니다. 수치는 이 JSON에서 채운다."
    )
    A("")
    A("### 쓸 수 있는 문장")
    A("")
    rc_arms = get(repro, "arms", default={})
    rc_ok = [k for k, v in rc_arms.items() if v.get("expected_identical") and v.get("identical_to_4dp")]
    rc_expected = [k for k, v in rc_arms.items() if v.get("expected_identical")]
    A(
        "- \"팀원이 담당한 LightGBM+MMR 추천기를 세 git 스냅샷(team-final / 중간 수정본 / 현재 브랜치) 그대로 아카이브 합성 "
        "데이터 위에서 재실행하는 파일 기반 재현 하네스를 만들었다. 실행 코드의 내용 해시·데이터 sha256·버전별 config.yaml "
        "해시·정답 구간 시작 시각을 캐시 키로 고정해, 같은 시드의 재실행이 지표를 소수 4자리까지 재현한다.\""
        + (
            f" (부록: 조기 종료 방식이 바뀌지 않은 arm {len(rc_expected)}개 중 {len(rc_ok)}개가 이전 실행 원자료와 소수 4자리 일치)"
            if rc_expected else ""
        )
    )
    cur_aw_vals = [get(mle, k, "as_written_mrr_mean") for k in cur_settings]
    cur_p_vals = [get(mle, k, "primary_mrr_mean") for k in cur_settings]
    short_names = {"current": "lambdarank 조기 종료", "binary": "binary 조기 종료", "fixed100": "lambdarank 100라운드"}
    cur_setting_names = "·".join(short_names[k] for k in cur_settings)
    if leak_verdicts and all(leak_verdicts.get(k) == "positive" for k in ["team-final", *cur_settings] if k in leak_verdicts):
        A(
            f"- \"재현한 파이프라인에서 평가 시점 누출을 찾았다: 유저 히스토리 피처가 데이터셋 끝 시각 기준으로 계산돼 채점 대상 "
            f"클릭을 포함했다. 같은 모델을 point-in-time으로 추론하면 MRR이 팀 최종 코드에서 {pct(tf_aw_mean, 2)}→"
            f"{pct(tf_p_mean, 2)}(쌍 차이 {fmt_eff(le_tf_mrr)}), 현재 코드에서는 모델 설정 {len(cur_settings)}가지"
            f"({cur_setting_names})에 걸쳐 {rng_str(cur_aw_vals)}→{rng_str(cur_p_vals)}로 떨어졌다(합성 유저 {n_users}명, 시드 "
            f"{len(seeds_main)}개).\""
        )
    else:
        A(
            f"- (설정별 판정이 갈려 범위 문장을 쓰지 않는다) \"point-in-time 추론으로 바꾸면 팀 최종 코드의 MRR이 "
            f"{pct(tf_aw_mean, 2)}→{pct(tf_p_mean, 2)}였다(쌍 차이 {fmt_eff(le_tf_mrr)}, "
            f"{say(le_tf_mrr, *SHORT_WORDS)}; 합성 유저 {n_users}명).\""
        )
    mrr_v = {b: ci_verdict(get(cur_mvb, b, "plain", "mrr", default={})) for b in strong}
    if all(v == "inconclusive" for v in mrr_v.values()):
        mrr_phrase = "MRR은 popularity·onboarding cosine·cosine_history와 구분되지 않았다"
    else:
        mrr_phrase = "MRR은 " + ", ".join(f"{b} 대비 {say(get(cur_mvb, b, 'plain', 'mrr', default={}), *MODEL_WORDS)}" for b in strong)
    if not wins:
        A(
            f"- \"point-in-time으로 고친 뒤, '모델이 단순 베이스라인보다 낫다'던 내 이전(v1) 분석 결론을 철회했다: P@5 "
            f"{pct(get(cur_p, 'precision@5', 'mean'), 2)} vs popularity {pct(get(base_team, 'popularity', 'aggregate', 'precision@5'), 2)}"
            f"(쌍 차이 {fmt_eff(pop.get('precision@5'))}, {say(pop.get('precision@5'), *MODEL_WORDS)}), {mrr_phrase}.\" "
            f"(보충: 이전 노출 아이템을 뺀 비교에서는 {unshown_mixed} - 우위 주장으로 쓰지 않는다)"
        )
    art_ok = (
        (asum.get("n_engine_order_stops_at_1") or 0) > 0
        and (asum.get("n_shuffled_stops_at_1") or 0) < (asum.get("n_engine_order_stops_at_1") or 0)
        and (asum.get("iter1_inflation_mean") or 0) > 0
    )
    if art_ok:
        A(
            f"- \"lambdarank 모델의 조기 종료가 트리 1개에서 멈추던 현상을 추적해, 조기 종료 지표의 아티팩트임을 밝혔다: LightGBM "
            f"NDCG는 동점을 행 순서로 깨는데 학습 데이터 생성 코드가 positive를 먼저 쌓아, 동점이 많은 1라운드의 검증 NDCG@5가 "
            f"{pct(it1_engine_mean, 2)}로 부풀려졌다(동점 무작위 기대값 {pct(it1_tie_mean, 2)}, 시드 {n_art}개 평균). 검증 행을 섞자 "
            f"1라운드 종료가 {asum.get('n_engine_order_stops_at_1')}/{n_art} → {asum.get('n_shuffled_stops_at_1')}/{n_art} 시드로 "
            f"줄었다. 엔진의 운영 학습 경로도 같은 구조라는 것을 확인해 후속 수정 항목으로 남겼다.\" (엔진 자체는 아직 고치지 않았다)"
        )
    A(
        f"- \"아카이브 합성 로그를 프로파일링해, LLM 페르소나 {ds['n_users_total']}명이 뉴스레터 {ds['n_newsletters']}건 전부를 "
        f"약 {ds['session_span_seconds_mean']/60:.0f}분짜리 세션에서 한 번씩 노출받는 구조라 전역 80/20 시간 분할이 사실상 유저 "
        f"분할이 된다는 것을 밝혔다(평가 유저 {n_users}명 중 {ds.get('n_evaluated_cold_users', '?')}명이 분할 이전 히스토리 없음).\""
    )
    if g.get("valid_items_are_exactly_newest"):
        A(
            f"- \"데이터 생성기의 train/valid 파일이 뉴스레터 id로 나뉘고({list_str(g.get('train_id_range'))} vs "
            f"{list_str(g.get('valid_id_range'))}), valid가 정확히 가장 최근 {g.get('n_valid_items')}건이라 그 과제가 최신성을 "
            f"보상한다는 것을 찾았다(recency 베이스라인 MRR {pct(get(base_gen, 'recency', 'aggregate', 'mrr'), 2)}).\""
        )
    A(
        "- \"오프라인 평가 프로토콜 ADR을 작성했다: point-in-time 추론, 모든 arm이 공유하는 고정 정답 구간, 학습 구간 내부 "
        "inner split으로만 조기 종료(검증 행을 섞어 지표의 동점 순서 의존 제거), 같은 정답·후보 위의 베이스라인, 같은 조건끼리만 "
        "버전 비교, 버전별 config 파일, 공개 벤치마크(EB-NeRD) 검증 후에만 정확도 주장.\" (프로세스 결정이지 성능 결과가 아님)"
    )
    if lk_all_inc:
        A(
            f"- (결과가 '판단 불가'라는 형태로만) \"학습 시점 히스토리 누출을 없앤 수정(FIX #4)의 효과는 검출되지 않았다(MRR "
            f"{fmt_eff(lk_es)}, 합성 유저 {n_users}명, 시드 {len(seeds_main)}개 - 검정력 부족; binary·100라운드 모델에서도 동일).\""
        )
    elif lk_detected:
        det = ", ".join(f"{name} {fmt_eff(e)}" for name, e in lk_detected)
        rest = f", {'·'.join(lk_null)} 모델에서는 검출되지 않았다" if lk_null else ""
        A(
            f"- (설정별로만, 선택을 밝히고) \"학습 시점 히스토리 누출이 남은 코드(FIX #4 이전)는 수정본 대비 point-in-time MRR "
            f"차이(leaky − fixed)가 {det}였고{rest}({selection_text}; 합성 유저 {n_users}명, 시드 {len(seeds_main)}개).\" "
            "(FIX #4는 내가 프로젝트 이후에 한 수정이다. 대표 수치나 제목 문장으로 쓰지 않는다)"
        )
    if tfw_r.get("mrr") is not None and covers:
        A(
            f"- (근거가 아니라 가능성으로만) \"팀 최종 평가 스크립트의 '최근 6일' 정답 창이 아카이브에서는 학습 구간을 포함한 로그 "
            f"전체를 덮어, 무작위 추천도 MRR {pct(tfw_r.get('mrr'), 2)} / P@5 {pct(tfw_r.get('precision@5'), 2)}에 이른다는 것을 "
            f"보였다 - {TEAM_0897_SHORT}을 부풀렸을 수 있는 요인이지 증명은 아니다.\""
        )
    A("")
    A("### 쓰면 안 되는 문장")
    A("")
    A(
        f"- \"팀 최종 코드를 as-written으로 재현하면 MRR 1.0(또는 0.99)으로 팀 보고값 0.897에 근접한다\" 또는 {TEAM_0897_SHORT}을 "
        "설명·재현·정량 귀속했다는 모든 문장. \"팀이 그 값을 한 시점에 한 번만 측정했다\"도 쓰지 않는다(출처가 없다)."
    )
    A(
        "- \"팀이 FIX #4로 성능이 좋아졌다고 잘못 귀속했다\", \"팀의 우위 주장을 철회했다\". (FIX #4/#5는 팀 이후의 자체 수정이고, "
        "팀은 절대 지표만 보고했다. 철회한 것은 내 v1 결론이다)"
    )
    A("- \"FIX #4의 효과가 0(또는 거의 0)이다\"를 확정된 null로 쓰는 문장. (CI가 넓다)")
    A(
        "- FIX #4/#5의 효과를 설정(objective·학습 라운드)과 합성 데이터라는 조건 없이 일반화하는 문장(예: \"FIX #4가 추천 성능을 "
        f"N% 개선했다\"). (효과가 검출되더라도 여러 설정 중 일부·합성 유저 {n_users}명 위의 결과다)"
    )
    A("- \"LightGBM+MMR 모델이 베이스라인보다 우수했다\"(또는 \"못했다\")를 일반 문장으로 쓰는 것. (필터와 모델 설정에 따라 판정이 다르다)")
    if vg.get("did_like_with_like") != "positive":
        A(
            "- \"0.849 vs 0.772 격차의 원인은 추론 시점 누출이다\", \"team-final 코드가 current보다 누출에 더 민감하다\", v2.1의 "
            "DiD +0.109를 코드 버전 효과로 인용하는 것. (같은 조건 비교에서는 검출되지 않는다 - 2-3절)"
        )
    else:
        A(
            "- \"0.849 vs 0.772 격차의 원인은 추론 시점 누출이다\"(인과 문장). 쓸 수 있는 형태: '같은 조건 비교에서 team-final 쪽 "
            f"누출 효과가 더 컸다(합성 유저 {n_users}명)'."
        )
    A(
        "- \"inner-validation 조기 종료가 붕괴를 고쳤다\", \"lambdarank 모델은 트리 1개 이후 배울 것이 없다\", 붕괴 원인을 작은 "
        "inner-valid·learning_rate·정답 밀도로 설명하는 문장. (원인은 조기 종료 지표의 동점 순서 아티팩트다 - 4-1절)"
    )
    A("- \"LambdaRank가 랭킹을 개선한다\" 등 lambdarank vs binary 결론. (4-3절: 이 데이터로는 판단할 수 없다)")
    A("- \"엔진의 조기 종료 버그를 고쳤다\". (하네스의 검증 프레임만 섞었고 엔진 소스는 그대로다)")
    A("- 후보 풀 크기에 대한 모든 결론(padded_400, small_recent_15).")
    A("- impression negative와 random negative의 우열에 대한 모든 문장.")
    A("- \"모델이 처음 보는(cold) 뉴스레터에 일반화된다\", generator_split을 cold-item 평가라고 부르는 것. (최신 44건 찾기, recency로 풀림)")
    A("- 이 합성 아카이브의 절대 MRR/P@5/nDCG/Coverage를 시스템 성능으로 인용하는 것, Coverage@5를 팀의 0.408과 비교하는 것.")
    A("- \"팀 파이프라인 결과(103명/405건 스냅샷)를 재현했다\".")
    A("- \"추천 모델을 만들었다/담당했다\". (팀 README상 추천 모델은 다른 팀원 담당)")
    A("- \"팀 DB 테이블에는 클릭 행만 있다\"를 합성 스냅샷에 대한 확정 사실로 쓰는 것. (운영 경로로는 그럴듯하지만 합성 로그 적재 방식은 문서가 없다)")
    A("- \"캐시 설계로 오래된 결과 재사용이 불가능하다\". (내용 해시로 좁혔을 뿐 완전한 보장은 아님)")
    A("- \"nested bootstrap이 학습 무작위성을 완전히 반영한다\". (시드 3~5개, 수십 개 구간 미보정)")
    A("")

    # ---------------------------------------------------------------- 부록
    A("## 부록: 재현 대조와 실행 환경")
    A("")
    if rc_arms:
        A(
            f"이전 실행(v2.1) 원자료(커밋 `{repro.get('v21_raw_commit')}`의 `team_repro_v2_raw.json`)와 같은 시드끼리 MRR/P@5를 "
            "대조했다. v2.2에서 바뀐 것은 조기 종료용 inner-valid 행 순서뿐이므로, AUC로 조기 종료하거나 조기 종료가 없는 arm과 "
            "엔진 순서 진단 arm은 같아야 하고, NDCG로 조기 종료하는 arm은 고른 라운드가 바뀌면 달라진다."
        )
        A("")
        A("| arm | 공통 시드 | 최대 절대 차이 | 소수 4자리 일치 | 같아야 하는가 | best_iteration v2.1 → v2.2 |")
        A("|---|---|---|---|---|---|")
        for name, v in rc_arms.items():
            A(
                f"| {name} | {list_str(v.get('shared_seeds'))} | {pct(v.get('max_abs_diff_mrr_p5'), 6)} | {v.get('identical_to_4dp')} "
                f"| {'예' if v.get('expected_identical') else '아니오(달라질 수 있음)'} "
                f"| {list_str(v.get('best_iteration_v21'))} → {list_str(v.get('best_iteration_v22'))} |"
            )
        A("")
        bad = [k for k, v in rc_arms.items() if v.get("violates_expectation")]
        if bad:
            A(f"**기대와 다른 arm: {', '.join(bad)}** - 같아야 하는데 달라졌다. 이 arm의 수치는 원인을 확인하기 전까지 인용하지 않는다.")
        else:
            A("같아야 하는 arm은 모두 소수 4자리까지 일치한다.")
        A("")
    elif repro.get("error"):
        A(f"이전 실행 원자료 대조 실패: {repro['error']}")
        A("")
    A(f"- 데이터셋 유저 수 {ds['n_users_total']}, 뉴스레터 수 {ds['n_newsletters']}, 평가 유저 {n_users}명.")
    A(
        "- 실행 환경: 로컬 MacBook, `nice -n 19`, BLAS/OpenMP 스레드 2개, 워커 1개(순차 실행), `--max-new-runs`로 10분 이내 "
        "단위로 나눠 실행하고 캐시에서 이어받았다."
    )
    ckf = meta.get("cache_key_fields")
    if ckf:
        A(f"- 캐시 키 구성: {'; '.join(ckf)}.")
    A("")

    out_path = REPORT_DIR / "team_repro_v2.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"리포트 저장: {out_path}")


if __name__ == "__main__":
    main()
