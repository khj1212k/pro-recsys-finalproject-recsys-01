"""reports/recsys/team_repro_v2.json(v2.1 형식)을 읽어 한국어 Markdown 리포트
(team_repro_v2.md)를 만든다. 숫자를 다시 계산하지 않는다 - run_repro.py가 저장한 값을
표로 옮긴다.

v2.1 원칙(v2 재검토 대응):
  - 결론 단어(부풀린다/낮춘다/검출되지 않는다, 모델 우위/열위/구분 안 됨)는 전부
    metrics.ci_verdict()가 CI 부호로 고른다. v2는 결론 문장이 숫자와 무관하게
    하드코딩돼 MRR 1.0을 '팀 보고값에 근접'이라고 쓰는 사고가 있었다.
  - '0.897'은 항상 팀 보고값이라는 것, 누출로 부풀려졌을 가능성, 한 시점 스냅샷
    측정이라는 것을 함께 적는다(TEAM_0897*).
  - 이력서 문장은 팀 귀속/소유 주장 없이, 조건부 문장은 조건(CI 판정)이 맞을 때만 낸다.
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
    "팀 보고값 MRR 0.897(팀 최종 보고서의 수치 - 103명/405건 스냅샷에서 한 시점에 한 번 측정했고, "
    "팀 방식 추론 시점과 학습 구간을 포함할 수 있는 정답 창으로 계산돼 누출로 부풀려졌을 가능성이 크다. "
    "그 스냅샷이 없어 검증할 수 없다)"
)
TEAM_0897_SHORT = "팀 보고값 0.897(한 시점 스냅샷 측정, 누출로 부풀려졌을 가능성, 검증 불가)"

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
MODEL_WORDS = ("모델 우위", "모델 열위", "구분 안 됨")


def verdict_tag(e: dict, words=DIFF_WORDS) -> str:
    return say(e, *words)


def bi_str(s: dict) -> str:
    bi = s.get("best_iteration") if s else None
    if not bi or all(b is None for b in bi):
        nt = s.get("n_trees") if s else None
        return f"고정 라운드, 트리 {nt}" if nt else "-"
    return f"{bi} (퇴화 {s.get('n_degenerate_seeds', 0)}/{s.get('n_seeds', len(bi))})"


def tie_range(s: dict, mk="mrr") -> str:
    r = get(s, "tie_draw_range", mk)
    if not r:
        return "-"
    return f"[{pct(r[0], 3)}, {pct(r[1], 3)}]"


def list_str(xs) -> str:
    if not xs:
        return "-"
    return "[" + ", ".join("-" if x is None else (f"{x:g}" if isinstance(x, float) else str(x)) for x in xs) + "]"


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


# ------------------------------------------------------------------ 본문


def main() -> None:
    data = json.loads((REPORT_DIR / "team_repro_v2.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    ds = data["data_structure"]
    headline = data["headline"]
    hci = data["headline_ci"]
    vc = data.get("version_comparison", {})
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
    repro = data.get("reproduction_check_vs_v2", {})
    cr = meta["category_recovery"]
    seeds_main = get(meta, "seeds", "current_team_final_and_all_current_arms", default=[])
    seeds_fs = get(meta, "seeds", "fix_snapshot", default=[])

    lines: list = []
    A = lines.append

    A("# 팀 베이스라인 재현 및 분해 실험 (team_repro v2.1)")
    A("")
    A(
        "> v1([`team_repro_v1.md`](./team_repro_v1.md))은 어드버서리얼 방법론 검토에서 \"unsound\" 판정을 받아 v2로 "
        "재작성했고, v2는 재검토에서 \"sound-with-caveats, blocker 2건\" 판정을 받아 이 v2.1로 고쳤다. v2 대비 바뀐 것:"
    )
    A(">")
    A(
        "> 1. **team-final-as-written 정답 정의 정정 (BLOCKER).** v2는 클릭 여부를 거르지 않고 노출 행 전체를 정답으로 "
        "셌다. 페르소나마다 195건 전부가 노출되므로 모든 유저의 정답이 195건 전부가 되어 무작위 추천도 MRR/P@5 1.0이 "
        "나오는 정의 오류였다. v2 요약의 'MRR 1.0이 팀 보고값에 근접'은 틀린 문장이었다. v2.1은 창 안의 클릭을 정답으로 "
        "쓰고, 같은 정의의 무작위 추천 기준값을 옆에 둔다(2-4절)."
    )
    A(
        "> 2. **이력서 문장 정정 (BLOCKER).** v2의 '팀이 FIX #4로 성능이 좋아졌다고 잘못 귀속' 문장은 사실이 아니다 - "
        "FIX #4/#5는 팀 프로젝트 이후 자체 리뷰(2026-07)에서 찾아 이 브랜치에 옮긴(2026-09) 수정이라 팀은 그 수정을 가진 "
        "적이 없다. '팀의 우위 주장 철회', '원인을 정량적으로 규명'도 과장이었다. 7절에서 다시 썼다."
    )
    A(
        "> 3. **거의 학습되지 않은 모델 표시 (MAJOR).** lambdarank가 inner-valid 조기 종료에서 트리 1개로 멈추는 시드를 "
        "'퇴화'로 표시하고, 동점 무작위 추첨·조기 종료 없는 100라운드 학습·binary objective 위의 결과를 함께 낸다(2-1절, 4-0절)."
    )
    A(
        "> 4. **후보 풀 arm (MAJOR).** padded_400은 학습 negative까지 바꿔 풀 크기 효과로 읽을 수 없어 제거했고, "
        "small_recent_15는 같은 15건 풀·같은 제한 정답 위의 풀 내부 베이스라인과만 비교한다(4-4절)."
    )
    A(
        "> 5. 모델-베이스라인 쌍 CI, 모델의 cold/warm·seen/unshown 필터 지표, 버전 간 비교와 누출 효과 차이(DiD)를 "
        "추가했고, 같은 모델을 공유하는 비교는 시드를 쌍으로 재표본한다. cold 유저 폴백은 CSV 행 순서 대신 popularity다."
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
        f"- 시드: current/team-final과 current의 모든 분해 arm·generator_split {list_str(seeds_main)} ({len(seeds_main)}개), "
        f"fix-snapshot {list_str(seeds_fs)} ({len(seeds_fs)}개). 동점 무작위 추첨 {meta.get('tie_draws')}회, random 베이스라인 "
        f"{meta.get('random_baseline_draws')}회 추첨 평균, 부트스트랩 {meta.get('n_boot')}회."
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
    cur_aw = get(headline, "current", "as_written_summary", default={})
    cur_tie = get(headline, "current", "primary_tie_random_summary", default={})
    tf_p = get(headline, "team-final", "primary_summary", default={})
    tf_aw = get(headline, "team-final", "as_written_summary", default={})
    le_cur = get(hci, "current", "leak_effect", default={})
    le_tf = get(hci, "team-final", "leak_effect", default={})

    A("## 0. 요약")
    A("")
    A(
        "이 스냅샷은 LLM 페르소나 100명의 합성 클릭이고 평가 대상은 31명이다. 절대 수치는 성능이 아니라 **코드 결함이 "
        "지표를 어느 방향으로 움직이는지 보는 화이트박스 진단**으로만 읽는다. 아래 결론 단어는 모두 95% CI 부호에서 "
        "나온다(다중비교 보정 없음)."
    )
    A("")
    A(
        f"1. **추론 시점 누출.** 같은 학습 모델을 point-in-time(1차)으로 추론하면 current MRR {fmt_ms(cur_p.get('mrr'))}, "
        f"팀 방식 추론 시점(2차, 데이터셋 끝 시각)으로 추론하면 {fmt_ms(cur_aw.get('mrr'))}다. 2차 − 1차 = MRR "
        f"{fmt_eff(le_cur.get('mrr'))}, P@5 {fmt_eff(le_cur.get('precision@5'))} ({n_of(le_cur.get('mrr'))}) - "
        f"{say(le_cur.get('mrr'), *LEAK_WORDS)}. team-final: {fmt_ms(tf_p.get('mrr'))} vs {fmt_ms(tf_aw.get('mrr'))}, "
        f"2차 − 1차 MRR {fmt_eff(le_tf.get('mrr'))} ({say(le_tf.get('mrr'), *LEAK_WORDS)})."
    )
    gap_p = get(vc, "team_final_minus_current_primary", "mrr", default={})
    gap_aw = get(vc, "team_final_minus_current_as_written", "mrr", default={})
    did = get(vc, "leak_effect_did_team_final_minus_current", "mrr", default={})
    gap_sentence = (
        f"2. **v1의 'team-final이 current보다 낫다'(MRR 0.849 vs 0.772) 격차.** team-final − current: 1차 MRR "
        f"{fmt_eff(gap_p)} ({verdict_tag(gap_p)}), 2차 {fmt_eff(gap_aw)} ({verdict_tag(gap_aw)}), 누출 효과의 차이(DiD) "
        f"{fmt_eff(did)} ({verdict_tag(did)})."
    )
    if ci_verdict(gap_p) == "inconclusive" and ci_verdict(gap_aw) == "positive":
        gap_sentence += " 격차는 팀 방식 추론 시점에서만 나타나고 point-in-time 추론에서는 검출되지 않는다."
        if ci_verdict(did) == "inconclusive":
            gap_sentence += (
                " 다만 누출 효과 차이의 CI가 0을 포함하므로, 누출이 격차의 '주된 원인'이라고까지는 말할 수 없다."
            )
        elif ci_verdict(did) == "positive":
            gap_sentence += (
                " 누출 효과 자체도 team-final 쪽이 더 크다(DiD CI가 0 초과) - team-final 코드가 추론 시점 누출에 더 "
                "민감하다는 것과 일관되지만, 합성 유저 31명 위의 결과라 격차의 '원인'이라는 인과 문장으로 쓰지 않는다."
            )
    A(gap_sentence)

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
    bl_sentence = (
        f"3. **베이스라인 대비(1차, 같은 정답·후보·answer_start, 쌍 nested bootstrap).** current 모델 − popularity: "
        f"P@5 {fmt_eff(pop.get('precision@5'))} ({say(pop.get('precision@5'), *MODEL_WORDS)}), MRR "
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
    unshown_mixed = unshown_verdict_summary(cur_mvb, strong)
    if unshown_mixed:
        bl_sentence += (
            f" 이전 노출 아이템을 양쪽 모두에서 뺀 unshown 비교(정답은 전부 미노출 아이템이다)에서는 {unshown_mixed} - "
            "필터에 따라 판정이 바뀌므로 어느 쪽으로도 '모델이 낫다/못하다'를 일반화하지 않는다(3-3절)."
        )
    A(bl_sentence)

    fx = get(decomp, "fixed100", "primary", default={})
    bn = get(decomp, "binary", "primary", default={})
    fixed_csv = get(base_team, "fixed_csv_order", "aggregate", "mrr")
    A(
        f"4. **거의 학습되지 않은 모델.** current(lambdarank, inner-valid 조기 종료) {cur_p.get('n_seeds', '?')}개 시드 중 "
        f"{cur_p.get('n_degenerate_seeds', '?')}개가 best_iteration=1(트리 1개)에서 멈췄다(best_iteration "
        f"{list_str(cur_p.get('best_iteration'))}). 이런 시드는 서로 다른 점수가 수십 개뿐이고(시드별 "
        f"{list_str(cur_p.get('n_distinct_scores_primary'))}), 평가 유저당 최고점 동점 아이템 수 중앙값이 "
        f"{list_str(cur_p.get('top_tie_size_eval_users_median'))}라 순위가 뉴스레터 CSV 행 순서로 정해진다(그 순서만으로 "
        f"MRR {pct(fixed_csv)}인 고정 리스트). 동점을 무작위로 깨면 MRR {pct(get(cur_p, 'mrr', 'mean'))} → "
        f"{pct(get(cur_tie, 'mrr', 'mean'))}(시드·추첨 전체 범위 {tie_range(cur_tie)}). 민감도: 조기 종료 없는 100라운드 "
        f"고정 학습 MRR {fmt_ms(fx.get('mrr'))}, binary objective(best_iteration {list_str(bn.get('best_iteration'))}) MRR "
        f"{fmt_ms(bn.get('mrr'))}. **inner validation은 v1의 '정답 구간으로 트리 수 고르기'는 없앴지만 단일 트리 붕괴는 "
        f"고치지 못했다** - lambdarank 조기 종료 arm의 효과는 퇴화 시드 표시, 동점 무작위, binary·100라운드 결과와 함께만 읽는다."
    )

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
        f"{TEAM_0897}과 방향은 일관되지만, 이 결과는 그 값을 재현하거나 설명하지 않는다. v2 리포트의 이 행(MRR 1.0)은 "
        f"노출 행 전체를 정답으로 센 정의 오류였다(같은 오류 정의에서 무작위도 MRR {pct(tfw_err_r.get('mrr'))}, "
        f"모델 {fmt_ms(tfw_err_m.get('mrr'))})."
    )

    lk_es = get(boot, "leakage_lambdarank_early_stopping", "mrr", default={})
    lk_bn = get(boot, "leakage_binary", "mrr", default={})
    lk_fx = get(boot, "leakage_lambdarank_fixed100", "mrr", default={})
    lk_settings = [("lambdarank 조기 종료", lk_es), ("binary", lk_bn), ("lambdarank 100라운드", lk_fx)]
    lk_all_inc = all(ci_verdict(e) == "inconclusive" for _, e in lk_settings)
    lk_detected = [(name, e) for name, e in lk_settings if ci_verdict(e) in ("positive", "negative")]
    lk_null = [name for name, e in lk_settings if ci_verdict(e) == "inconclusive"]
    A(
        f"6. **학습 시점 히스토리 누출(FIX #4)의 효과**(leaky − fixed, 1차 추론): lambdarank 조기 종료 MRR {fmt_eff(lk_es)} "
        f"({verdict_tag(lk_es)}), binary {fmt_eff(lk_bn)} ({verdict_tag(lk_bn)}), lambdarank 100라운드 {fmt_eff(lk_fx)} "
        f"({verdict_tag(lk_fx)}). "
        + (
            "세 설정 모두 효과가 검출되지 않았다 - 합성 유저 31명·시드 5개로는 검정력이 부족해 '효과 없음'의 증거가 아니라 "
            "'판단 불가'다. "
            if lk_all_inc else
            "설정에 따라 판정이 다르다 - 위 판정을 설정별로만 읽는다"
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
        f"유저이고(평가 대상 {ds['n_evaluated_users_in_answer_window']}명 중 {ds.get('n_evaluated_cold_users', '?')}명), "
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
        f"- 고정 정답 구간(`{ds['answer_start']}` 이후): 평가 대상 유저 {ds['n_evaluated_users_in_answer_window']}명"
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
        "inner-validation으로 한다(정답 구간을 보지 않는다). 단, 일부 학습 쪽 입력은 전체 로그를 본다: team-final의 학습 "
        "피처는 전역 히스토리를 쓰고, 모든 버전의 negative sampler 제외 집합(유저별 클릭 아이템)은 answer_start 이후 클릭도 "
        "포함한다. 외부 재검토가 이 제외 집합을 answer_start 이전 클릭으로 제한해 따로 돌려본 결과 효과는 검출되지 않았다"
        "(MRR −0.014 [−0.119, +0.085], 3시드 - 이 리포트의 실행에 포함되지 않은 검토자 수치)."
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
        "`best_iteration`의 '퇴화'는 조기 종료가 1라운드에서 멈춘(트리 1개) 시드 수다. `동점 중앙값`은 평가 유저별로 "
        "최고점과 동점인 아이템 수의 중앙값(시드별)이다. '1차 동점 무작위' 행은 같은 모델 점수에서 동점만 무작위로 깬 "
        f"{meta.get('tie_draws')}회 추첨 평균이고, 괄호는 시드·추첨 전체의 MRR 범위다."
    )
    A("")
    A("| 코드 버전 | 추론 | 시드 | MRR | P@5 | nDCG@5 | Coverage@5 | best_iteration (퇴화) | distinct scores | 동점 중앙값 |")
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
    A("| 코드 버전 | MRR 효과 | P@5 효과 | 판정(MRR) | 동점 무작위 MRR 효과 | n |")
    A("|---|---|---|---|---|---|")
    for version in ["current", "team-final", "fix-snapshot"]:
        le = get(hci, version, "leak_effect", default={})
        if not le:
            continue
        ltr = get(hci, version, "leak_effect_tie_random", "mrr", default={})
        A(
            f"| {VERSION_LABELS[version]} | {fmt_eff(le.get('mrr'))} | {fmt_eff(le.get('precision@5'))} "
            f"| {say(le.get('mrr'), *LEAK_WORDS)} | {fmt_eff(ltr)} | {n_of(le.get('mrr'))} |"
        )
    A("")

    A("### 2-3. 버전 간 비교 (team-final − current, 서로 다른 모델이라 시드 독립 재표본)")
    A("")
    A("| 비교 | MRR | P@5 | 판정(MRR) |")
    A("|---|---|---|---|")
    for key, label in [
        ("team_final_minus_current_primary", "1차 point-in-time"),
        ("team_final_minus_current_as_written", "2차 팀 방식 추론 시점"),
        ("leak_effect_did_team_final_minus_current", "누출 효과의 차이(DiD)"),
    ]:
        e = vc.get(key, {})
        A(f"| {label} | {fmt_eff(e.get('mrr'))} | {fmt_eff(e.get('precision@5'))} | {verdict_tag(e.get('mrr'))} |")
    A("")

    A("### 2-4. team-final-as-written (팀 `scripts/evaluate_results.py`의 정답 정의)")
    A("")
    A(
        "추천은 team-final 모델을 팀 방식 추론 시점(2차)으로 만든 것이고, 정답만 팀 스크립트의 창(`created_at >= NOW()-6일`)으로 "
        "바꿨다. 팀 DB 테이블(`user_newsletter_ctr_log`)에는 클릭 행만 있으므로 아카이브에서의 올바른 번역은 창 안의 "
        "`is_clicked==1` 행이다. 무작위 기준값은 같은 정답 정의 위에서 100회 추첨한 평균이다."
    )
    A("")
    A("| 정답 정의 | 유저당 평균 정답 수 | team-final MRR | team-final P@5 | 무작위 MRR | 무작위 P@5 |")
    A("|---|---|---|---|---|---|")
    A(
        f"| 창 안의 클릭 (올바른 번역) | {pct(n_ans, 1)} | {fmt_ms(tfw_m.get('mrr'))} | {fmt_ms(tfw_m.get('precision@5'))} "
        f"| {pct(tfw_r.get('mrr'))} | {pct(tfw_r.get('precision@5'))} |"
    )
    A(
        f"| 노출 행 전체 (**v2의 정의 오류 - 해석 대상 아님**) | {pct(get(tfw, 'mean_answers_per_user', 'all_rows_definition_error'), 1)} "
        f"| {fmt_ms(tfw_err_m.get('mrr'))} | {fmt_ms(tfw_err_m.get('precision@5'))} | {pct(tfw_err_r.get('mrr'))} "
        f"| {pct(tfw_err_r.get('precision@5'))} |"
    )
    A("")
    A(
        f"해석: 팀 스크립트의 창은 이 아카이브에서 학습 구간 클릭까지 정답으로 세므로 무작위도 높은 점수를 받는다. {TEAM_0897}을 "
        "부풀렸을 수 있는 요인 중 하나(추론 시점 누출과 함께)와 **일관되지만 증명은 아니다**. 팀의 103명/405건 스냅샷과 그 로그 "
        "구간은 남아 있지 않다."
    )
    A("")

    # ---------------------------------------------------------------- 3. 베이스라인
    A("## 3. 베이스라인 비교 (모델과 같은 로그/후보 풀/정답 구간)")
    A("")
    A("### 3-1. team_split, 1차 정답 구간 - 전체 / seen 필터 / unshown 필터")
    A("")
    A(
        "seen 필터는 answer_start 이전에 클릭한 아이템을, unshown 필터는 이전에 노출된 아이템(클릭 여부 무관) 전부를 추천에서 "
        f"뺀다. cold 폴백: {base_team_meta.get('cold_fallback', '-')}."
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
        "상관이 없어(검토자 측정 Spearman ≈ 0) 우연히 유리한 순서다. 단일 트리 모델의 동점도 이 순서로 깨진다."
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
            "모델의 새 아이템 일반화에 대해 아무것도 증명하지 않는다 - 과제 자체가 '최신 아이템 찾기'다."
        )
        A("")

    # ---------------------------------------------------------------- 4. 분해 실험
    A("## 4. 분해 실험 (current 코드, 1차 point-in-time)")
    A("")
    A(
        "각 비교는 A와 B가 서로 다른 학습 모델이라 시드를 독립 재표본한다(보수적). v1의 `label_assumption`(all_rows)은 "
        "퇴화 실험이라(negative가 없음) v2부터 제거했다."
    )
    A("")
    A("### 4-0. 학습 상태 (모든 arm)")
    A("")
    A("| arm | 시드 | best_iteration (퇴화) | distinct scores | 동점 중앙값 | MRR (결정적) | MRR (동점 무작위, 범위) | P@5 |")
    A("|---|---|---|---|---|---|---|---|")
    arm_rows = [
        ("current: lambdarank, 조기 종료 (기준)", get(headline, "current", "primary_summary", default={}),
         get(headline, "current", "primary_tie_random_summary", default={})),
    ]
    arm_labels = {
        "fixed100": "lambdarank, 100라운드 고정",
        "binary": "binary, 조기 종료",
        "leaky": "leaky 학습 히스토리, lambdarank 조기 종료",
        "leaky_binary": "leaky 학습 히스토리, binary",
        "leaky_fixed100": "leaky 학습 히스토리, lambdarank 100라운드",
        "impression": "impression negative (group_key=user_id)",
        "small_recent_15": "후보 15건 풀 (정답도 풀 안으로 제한)",
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

    A("### 4-1. 학습 시점 히스토리 누출 (FIX #4): fixed(A) → leaky(B)")
    A("")
    A(
        "FIX #4는 팀 프로젝트 종료 후 자체 리뷰(2026-07)에서 찾아 이 브랜치에 옮긴(2026-09) 수정이다. 팀 시절 코드는 leaky 쪽이다. "
        "lambdarank 조기 종료 모델은 퇴화 시드가 많아서 binary와 100라운드 모델 위에서도 같은 분해를 했다."
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

    A("### 4-2. objective와 학습 라운드")
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
    A(
        "조기 종료 lambdarank가 퇴화 시드를 포함하는 한, objective 비교는 'lambdarank가 낫다/못하다'의 근거가 아니다. "
        "FIX #5도 팀 프로젝트 이후 자체 리뷰에서 나온 수정이다."
    )
    A("")

    A("### 4-3. negative 출처: random → impression (단일 변수 비교가 아님)")
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
        f"- inner 분할에서 양쪽에 걸친 그룹 수(v2.1은 그룹 소속으로 검사): {list_str(imp.get('inner_split_groups_on_both_sides'))}. "
        "v2는 인접 행만 검사해 85명 중 32명이 양쪽에 걸쳤다."
    )
    A(
        "- 페르소나 전원이 195건 전부를 노출받았으므로 '클릭 안 한 나머지'와 '노출됐지만 클릭 안 함'은 사실상 같은 모집단이다. "
        "이 비교로 노출 로그 negative의 우열을 말하지 않는다."
    )
    A("")

    A("### 4-4. 작은 후보 풀 (small_recent_15) - 같은 풀·같은 정답 위의 풀 내부 베이스라인과만 비교")
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
        "created_at·카테고리를 공유하고 결코 positive가 되지 않음) 학습 자체를 바꿨다. v2의 효과(MRR −0.19)는 단일 트리 시드에서만 "
        "나왔다(26트리 시드는 0.6545 → 0.6244). 풀 크기 효과로 읽을 수 없어 v2.1에서 실행하지 않는다(코드는 재검토용으로 남김)."
    )
    A("")

    # ---------------------------------------------------------------- 5. 증명 불가
    A("## 5. 이 데이터가 증명할 수 없는 것")
    A("")
    A(
        f"- **실서비스 성능.** 클릭은 LLM 페르소나 100명에서 LLM이 추론해 만든 라벨이라 `category_match` 같은 명시적 신호와 "
        f"순환성이 있을 수 있다. 세션이 약 {window_h:.1f}시간뿐이라 신선도(half-life 7일) 피처는 사실상 죽은 피처다."
    )
    A(
        f"- **{TEAM_0897_SHORT}의 재현이나 원인 규명.** 이 아카이브는 100명/195건 스냅샷이라 규모가 다르다. 2-4절은 '팀 정답 창이 "
        "학습 구간 클릭을 포함하면 어떤 랭킹도 부풀려진다'를 보여줄 뿐, 그 스냅샷의 검증이 아니다."
    )
    A("- **모델이 베이스라인보다 낫다는 것** (0절 3번, 3-3절).")
    A(
        "- **lambdarank 조기 종료 arm의 효과 크기.** 퇴화 시드(트리 1개)는 순위가 동점 처리 순서로 정해진다. 이런 arm의 효과는 "
        "모델 효과가 아니라 '거의 상수인 모델 + 동점 순서 + MMR'의 효과일 수 있다(4-0절)."
    )
    A("- **후보 풀 크기의 효과** (padded_400 제거, small_recent_15는 풀 내부 비교만).")
    A(
        "- **FIX #4 효과가 0이라는 것.** '검출되지 않음'은 n=31명·시드 5개에서의 판단 불가이지 효과 없음의 증거가 아니다."
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
        "- **학습 붕괴부터 고친다.** inner-valid 조기 종료가 1라운드에서 멈추는 원인(작은 inner-valid, 그룹당 정답 밀도, "
        "learning_rate 0.05에서의 첫 트리 이득)을 보고, 최소 라운드나 조기 종료 patience/지표를 사전에 정한 뒤 분해 실험을 다시 한다."
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
    n_users = ds["n_evaluated_users_in_answer_window"]
    rc_ok = [k for k, v in get(repro, "arms", default={}).items() if v.get("identical_to_4dp")]
    A(
        "- \"팀원이 담당한 LightGBM+MMR 추천기를 세 git 스냅샷(team-final / 중간 수정본 / 현재 브랜치) 그대로 아카이브 합성 "
        "데이터 위에서 재실행하는 파일 기반 재현 하네스를 만들었다. git SHA·데이터 sha256·버전별 config.yaml 해시를 고정해, "
        "같은 시드의 재실행이 지표를 소수 4자리까지 재현한다.\""
        + (f" (부록: 같은 시드를 공유하는 arm {len(rc_ok)}개가 v2 원자료와 소수 4자리 일치)" if rc_ok else "")
    )
    A(
        f"- \"재현한 파이프라인에서 평가 시점 누출을 찾았다: 유저 히스토리 피처가 데이터셋 끝 시각 기준으로 계산돼 채점 대상 "
        f"클릭을 포함했다. point-in-time 추론으로 고치면 MRR이 {pct(get(cur_aw, 'mrr', 'mean'), 2)}→{pct(get(cur_p, 'mrr', 'mean'), 2)}"
        f"(현재 코드), {pct(get(tf_aw, 'mrr', 'mean'), 2)}→{pct(get(tf_p, 'mrr', 'mean'), 2)}(팀 최종 코드)로 떨어졌다(합성 유저 "
        f"{n_users}명, 시드 {len(seeds_main)}개).\""
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
    A(
        f"- \"아카이브 합성 로그를 프로파일링해, LLM 페르소나 {ds['n_users_total']}명이 뉴스레터 {ds['n_newsletters']}건 전부를 "
        f"약 {ds['session_span_seconds_mean']/60:.0f}분짜리 세션에서 한 번씩 노출받는 구조라 전역 80/20 시간 분할이 사실상 유저 "
        f"분할이 된다는 것을 밝혔다(평가 유저 {n_users}명 중 {ds.get('n_evaluated_cold_users', '?')}명이 분할 이전 히스토리 없음).\""
    )
    if g.get("valid_items_are_exactly_newest"):
        A(
            f"- \"데이터 생성기의 train/valid 파일이 뉴스레터 id로 나뉘고({list_str(g.get('train_id_range'))} vs "
            f"{list_str(g.get('valid_id_range'))}), valid가 정확히 가장 최근 {g.get('n_valid_items')}건이라는 것을 찾았다.\""
        )
    A(
        "- \"오프라인 평가 프로토콜 ADR을 작성했다: point-in-time 추론, 모든 arm이 공유하는 고정 정답 구간, 학습 구간 내부 "
        "inner split으로만 조기 종료, 같은 정답·후보 위의 베이스라인, 버전별 config 파일, 공개 벤치마크(EB-NeRD) 검증 후에만 "
        "정확도 주장.\" (프로세스 결정이지 성능 결과가 아님)"
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
            f"- (설정별로만) \"학습 시점 히스토리 누출이 남은 코드(FIX #4 이전)는 수정본 대비 point-in-time MRR 차이(leaky − fixed)가 "
            f"{det}였고{rest}(합성 유저 {n_users}명, 시드 {len(seeds_main)}개).\" (조기 종료 lambdarank는 퇴화 시드 포함 - 4-0절)"
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
        f"- \"팀 최종 코드를 as-written으로 재현하면 MRR 1.0으로 팀 보고값 0.897에 근접한다\" 또는 {TEAM_0897_SHORT}을 설명·재현·"
        "정량 귀속했다는 모든 문장. (v2의 1.0은 정의 오류였고, 올바른 정의에서도 재현이 아니다)"
    )
    A(
        "- \"팀이 FIX #4로 성능이 좋아졌다고 잘못 귀속했다\", \"팀의 우위 주장을 철회했다\". (FIX #4/#5는 팀 이후의 자체 수정이고, "
        "팀은 절대 지표만 보고했다)"
    )
    A("- \"FIX #4의 효과가 0(또는 거의 0)이다\"를 확정된 null로 쓰는 문장. (CI가 넓고 lambdarank 조기 종료 모델은 퇴화 시드 포함)")
    A(
        "- FIX #4/#5의 효과를 설정(objective·학습 라운드)과 합성 데이터라는 조건 없이 일반화하는 문장(예: \"FIX #4가 추천 성능을 "
        "N% 개선했다\"). (효과가 검출되더라도 한 설정·합성 유저 31명 위의 결과다)"
    )
    A("- \"LightGBM+MMR 모델이 MRR 0.77(vs 베이스라인 0.60)로 베이스라인보다 우수했다\" 등 모든 모델 우위 주장.")
    A(
        "- \"0.849 vs 0.772 격차의 주된 원인은 추론 시점 누출이다\" (인과 문장). 쓸 수 있는 형태: 'point-in-time 추론에서는 그 격차가 "
        "검출되지 않는다'."
    )
    A("- 후보 풀 크기에 대한 모든 결론(padded_400, small_recent_15).")
    A("- impression negative와 random negative의 우열에 대한 모든 문장.")
    A("- \"LambdaRank가 랭킹을 개선한다\" 등 lambdarank vs binary 결론. (조기 종료 lambdarank는 퇴화 시드 포함)")
    A("- \"모델이 처음 보는(cold) 뉴스레터에 일반화된다\", generator_split을 cold-item 평가라고 부르는 것. (최신 44건 찾기, recency로 풀림)")
    A("- 이 합성 아카이브의 절대 MRR/P@5/nDCG/Coverage를 시스템 성능으로 인용하는 것, Coverage@5를 팀의 0.408과 비교하는 것.")
    A("- \"팀 파이프라인 결과(103명/405건 스냅샷)를 재현했다\".")
    A("- \"추천 모델을 만들었다/담당했다\". (팀 README상 추천 모델은 다른 팀원 담당)")
    A("- \"캐시 설계로 오래된 결과 재사용이 불가능하다\". (내용 해시로 좁혔을 뿐 완전한 보장은 아님)")
    A("- \"nested bootstrap이 학습 무작위성을 완전히 반영한다\". (시드 3~5개)")
    A("")

    # ---------------------------------------------------------------- 부록
    A("## 부록: 재현 대조와 실행 환경")
    A("")
    if get(repro, "arms"):
        A(f"v2 원자료(커밋 `{repro.get('v2_raw_commit')}`의 `team_repro_v2_raw.json`)와 같은 시드끼리 MRR/P@5(1차, 헤드라인은 2차 포함)를 대조했다.")
        A("")
        A("| arm | 공통 시드 | 최대 절대 차이 | 소수 4자리 일치 |")
        A("|---|---|---|---|")
        for name, v in repro["arms"].items():
            A(f"| {name} | {list_str(v.get('shared_seeds'))} | {pct(v.get('max_abs_diff_mrr_p5'), 6)} | {v.get('identical_to_4dp')} |")
        A("")
    elif repro.get("error"):
        A(f"v2 원자료 대조 실패: {repro['error']}")
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
