"""Markdown reports for E9 (sim.ope_validation) and E10 (sim.fatigue_comparison), from their JSON.

    python -m sim.ope_report --ope <artifact>/ope_validation.json --fatigue <artifact>/fatigue_v1.json \
        --out-dir reports/sim

The JSON files are copied byte for byte and each .md is rendered from its JSON alone, so no number
in a report is typed by hand. Interpretation lives in ADR 0025, which cites these files.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import List, Optional

ESTIMATOR_LABELS = {
    "replay_position_based": "replay_position_based (1차)",
    "replay_exploration": "replay_exploration",
    "snips_slate": "snips_slate",
    "position_based_slate": "position_based_slate",
}
TARGET_LABELS = {
    "random": "random (E에서 균등 무작위 20개)",
    "reactive": "reactive (장난감 콘텐츠 기반 점수순)",
    "shadow_recency": "shadow_recency (shadow 랭커 대역 + MMR)",
}
METRIC_LABELS = {
    "repeat_impression_rate": "반복 노출 비율 (판정)",
    "after_click_jaccard_mean": "after_click_jaccard, k=10 (판정)",
    "similar_share_lift": "similar_share_lift, k=10 (판정)",
    "rule_blocked_impression_rate": "규칙이 직접 막는 노출의 비율 (메커니즘 확인)",
    "cold_user_ild": "콜드 유저(도중 가입자) 화면 ILD",
    "cold_user_category_entropy": "콜드 유저 화면 카테고리 엔트로피(nat)",
    "cold_path_ild": "cold_start_* 경로 화면 ILD",
    "cold_path_category_entropy": "cold_start_* 경로 화면 카테고리 엔트로피(nat)",
    "ctr": "칸당 클릭률 (방향이 구성상 정해짐, 주장 아님)",
    "repeat_impression_rate_all_answers": "반복 노출 비율, 폴백 응답 포함",
    "fallback_answer_share": "폴백으로 응답한 요청의 비율",
    "short_slate_share": "정책 화면 중 20칸 미만의 비율",
    "slots_per_policy_answer": "정책 화면의 평균 칸 수",
}
RATE_METRICS = {"repeat_impression_rate", "rule_blocked_impression_rate", "ctr", "repeat_impression_rate_all_answers",
                "fallback_answer_share", "short_slate_share"}


def metric_format(name: str):
    if name in RATE_METRICS:
        return pct, {"digits": 3 if name == "ctr" else 2}
    return num, {"digits": 2 if name == "slots_per_policy_answer" else 4}


def pct(x: Optional[float], digits: int = 2) -> str:
    return "-" if x is None else f"{100 * x:.{digits}f}%"


def num(x: Optional[float], digits: int = 3) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def ci(pair, fmt=pct, **kw) -> str:
    if not pair or pair[0] is None or pair[1] is None:
        return "-"
    return f"[{fmt(pair[0], **kw)}, {fmt(pair[1], **kw)}]"


def yes(flag: Optional[bool]) -> str:
    return "-" if flag is None else ("예" if flag else "아니오")


def not_for_verdict(meta: dict) -> Optional[str]:
    """Why the numbers of a report decide nothing - or None for the run that carries the registered verdict.

    The verdicts of ADR 0025 belong to one run: the registered configuration, on a GitHub runner, in the
    world A1.1 registered (run 37400003072, code commit 0ccd59f; its JSON has no `serving_path`). The
    harness has since followed the serving code to the repository contract of ADR 0033, so anything it
    produces now is a run of another world, whatever its size and wherever it runs."""
    if not meta["registered_config"]:
        return "사전 등록한 구성이 아니다(스모크). 이 파일의 수치는 어디에도 쓰지 않는다."
    if not meta.get("github_run_id"):
        return ("GitHub Actions 러너 밖에서 돌린 실행이다. 판정용이 아니고(ADR 0025 A1.5), 이 파일의 수치는 "
                "어디에도 쓰지 않는다.")
    if meta.get("serving_path"):
        return ("ADR 0025 A1.1에 등록한 세계와 다른 서빙 경로에서 돌린 실행이다. 판정용이 아니다. 등록한 판정은 "
                "실행 37400003072(코드 커밋 0ccd59f)의 것이고 이 실행으로 바뀌지 않는다(ADR 0025 A1.6).")
    return None


def header(meta: dict, title: str, uncertainty: str) -> List[str]:
    cfg, runner = meta["config"], meta["runner"]
    run = meta.get("github_run_id")
    where = (f"GitHub Actions 실행 `{run}`(시도 {meta.get('github_run_attempt')}), "
             f"저장소 `{meta.get('github_repository')}`, 브랜치 `{meta.get('github_ref')}`"
             if run else "GitHub Actions 밖(로컬)")
    lines = [f"# {title}", "", f"> {meta['label']}", ""]
    banner = not_for_verdict(meta)
    if banner:
        lines += [f"> **{banner}**", ""]
    lines += [
        f"- 사전 등록: {meta['preregistration']} (커밋 `{meta.get('preregistration_commit')}`)",
        f"- 실행: {where}",
    ]
    # keys the registered run's JSON does not have: nothing is printed for it, so its report renders as it did
    if meta.get("run_reason"):
        discards = f", 버리는 실행 `{meta['discards_run']}`" if meta.get("discards_run") else ""
        lines.append(f"- 실행 사유: `{meta['run_reason']}`{discards}")
    if meta.get("serving_path"):
        lines.append(f"- 서빙 경로: {meta['serving_path']}")
    lines += [
        f"- 코드 커밋: `{meta.get('git_sha')}`",
        f"- 실행 시각(UTC): {meta.get('started_utc')} ~ {meta.get('finished_utc')}",
        f"- 명령: `{meta['command']}`",
        f"- 데이터: 시뮬레이터(ADR 0019). 카탈로그 {cfg['catalog']}, 클릭 모델 프리셋 `{cfg['preset']}`, "
        f"임베딩 {cfg['embeddings']}",
        f"- 표본: 사용자 {cfg['n_users']}명 x {cfg['n_days']}일 x seed {cfg['seeds']}",
        f"- 불확실성: {uncertainty}",
        f"- 환경: Python {runner['python']}, numpy {runner['numpy']}, kiwipiepy {runner['kiwipiepy']}, "
        f"{runner['platform']}",
        "",
    ]
    return lines


# ------------------------------------------------------------------------------------- E9


def _verdict_rows(stage: dict, label: str) -> List[str]:
    rows = []
    for name, target in stage["targets"].items():
        v = target["verdict"]
        rows.append(
            f"| {label} | {name} | `{v['verdict_estimator']}` | {num(v['primary_coverage_mean'])} | "
            f"{pct(v['rel_err_mean'], 1)} | {yes(v['rel_err_ok'])} | {pct(v['ess_ratio_min'], 2)} | "
            f"{yes(v['ess_ok'])} | **{'통과' if v['passed'] else '실패'}** |")
    return rows


def _stage_section(stage: dict, title: str) -> List[str]:
    lines = [f"## {title}", "", "### 로그 정책 A (활성 휴리스틱 + MMR + 탐색 칸)", "",
             "| seed | 요청 | 분석 대상 | 제외 | 칸 | 탐색 칸 | 클릭 | 탐색 칸의 클릭 | 칸당 클릭률 | 탐색 칸 | 결정론 칸 | "
             "탐색 풀 크기 평균(최소~최대) | 후보 수 평균 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seed, a in stage["policy_a"].items():
        pool, eligible = a["explore_pool_size"], a["eligible_count"]
        lines.append(
            f"| {seed} | {a['requests']} | {a['requests_analyzed']} | {a['requests_excluded']} | {a['slots']} | "
            f"{a['explore_slots']} | {a['clicks']} | {a['clicks_on_explore_slots']} | {pct(a['ctr'], 3)} | "
            f"{pct(a['ctr_explore_slots'], 3)} | {pct(a['ctr_deterministic_slots'], 3)} | "
            f"{num(pool['mean'], 1)} ({num(pool['min'], 0)}~{num(pool['max'], 0)}) | {num(eligible['mean'], 1)} |")
    lines += ["", "위치 편향(서술용): 탐색 칸에서 맞춘 η̂와 시뮬레이터의 η. 시뮬레이터의 η는 사용자마다 다르다.", "",
              "| seed | η̂ (탐색 칸) | 시뮬레이터 η, 요청 가중 평균 | 최소 ~ 최대 | 탐색 칸 클릭 수 |",
              "|---|---|---|---|---|"]
    for seed, a in stage["policy_a"].items():
        eta = a["simulator_eta"]
        lines.append(f"| {seed} | {num(a['eta_hat'])} | {num(eta['request_weighted_mean'])} | "
                     f"{num(eta['min'], 2)} ~ {num(eta['max'], 2)} | {a['clicks_on_explore_slots']} |")
    lines.append("")

    for name, target in stage["targets"].items():
        v = target["verdict"]
        lines += [f"### 타깃 {TARGET_LABELS.get(name, name)}", "",
                  f"판정 추정기 `{v['verdict_estimator']}` "
                  f"(1차 추정기의 coverage 3 seed 평균 {num(v['primary_coverage_mean'])}"
                  f"{', 0.9 미만 - 탐색 칸의 지지 부족' if v['explore_support_short'] else ''}): "
                  f"상대오차 평균 {pct(v['rel_err_mean'], 1)}, ESS 비율 최소 {pct(v['ess_ratio_min'], 2)} → "
                  f"**{'통과' if v['passed'] else '실패'}**", "",
                  "| seed | 실측 V (95% 구간) | 클릭 / 칸 | 같은 문맥의 기대값 V_ctx | \\|V_ctx − V\\| / V | "
                  "타깃 화면 중 탐색 풀에 있는 아이템 수(평균) |",
                  "|---|---|---|---|---|---|"]
        for row in target["per_seed"]:
            m = row["measured"]
            lines.append(
                f"| {row['seed']} | {pct(m['ctr'], 3)} {ci(m['ci95'], digits=3)} | {m['clicks']} / {m['slots']} | "
                f"{pct(row['same_context_expected_ctr'], 3)} | {pct(row['state_shift_rel'], 1)} | "
                f"{num(row['target_items_in_explore_pool_mean'], 1)} |")
        lines += ["", "| seed | 추정기 | 추정값 (95% 구간) | 상대오차(대 실측 V) | 상대오차(대 V_ctx) | 채점 칸 / 본 칸 | "
                  "ESS 비율 | coverage | 구간이 실측을 포함 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for row in target["per_seed"]:
            for est_name, e in row["estimators"].items():
                mark = " ◀" if est_name == v["verdict_estimator"] else ""
                lines.append(
                    f"| {row['seed']} | {ESTIMATOR_LABELS[est_name]}{mark} | {pct(e['value'], 3)} "
                    f"{ci(e['ci95'], digits=3)} | {pct(e['rel_err'], 1)} | {pct(e['rel_err_vs_same_context'], 1)} | "
                    f"{e['n_matched']} / {e['n_slots']} | {pct(e['ess_ratio'], 2)} | {num(e['coverage'])} | "
                    f"{yes(e['ci_covers_measured'])} |")
        lines += ["", "| 추정기 | 상대오차 평균(대 실측) | 상대오차 평균(대 V_ctx) | coverage 평균 | ESS 비율 최소 | "
                  "ESS / 탐색 칸 수 |", "|---|---|---|---|---|---|"]
        for est_name, summ in v["summary_by_estimator"].items():
            over_explore = "-"
            if est_name == "position_based_slate":
                values = [r["estimators"][est_name].get("ess_over_explore_slots") for r in target["per_seed"]]
                if all(x is not None for x in values):
                    over_explore = f"{pct(min(values), 1)} (최소)"
            lines.append(f"| {ESTIMATOR_LABELS[est_name]} | {pct(summ['rel_err_mean'], 1)} | "
                         f"{pct(summ['rel_err_vs_same_context_mean'], 1)} | {num(summ['coverage_mean'])} | "
                         f"{pct(summ['ess_ratio_min'], 2)} | {over_explore} |")
        lines.append("")
    return lines


def render_ope(result: dict) -> str:
    meta, verdict = result["meta"], result["verdict"]
    first, retry = result["explore_slots_2"], result["explore_slots_4_retry"]
    rule = meta["rule"]
    lines = header(meta, "E9 — 탐색 칸 로그로 한 오프폴리시 추정의 시뮬레이터 검증",
                   f"seed {len(meta['config']['seeds'])}개, 사용자 단위 부트스트랩 {rule['bootstrap']['n']}회 "
                   f"(seed {rule['bootstrap']['seed']}) 95% 백분위 구간. 구간은 보고용이고 판정에 쓰지 않는다")
    lines += [
        "## 판정", "",
        f"**E9: {verdict['text']}** (ADR 0025의 상태: {verdict['adr_status']})", "",
        f"규칙(ADR 0025 A1.3): 타깃마다 판정 추정기 하나 - 1차 추정기의 coverage 3 seed 평균이 "
        f"{rule['coverage_min']} 이상이면 `{rule['primary']}`, 미만이면 `{rule['fallback']}`. 그 추정기의 seed별 "
        f"상대오차 평균 ≤ {pct(rule['rel_err_max'], 0)}, 세 seed 모두 ESS 비율 ≥ {pct(rule['ess_ratio_min'], 0)}. "
        "세 타깃이 모두 통과해야 통과이고, 실패하면 탐색 4칸으로 한 번 다시 돌린다.", "",
        "| 단계 | 타깃 | 판정 추정기 | 1차 coverage 평균 | 상대오차 평균 | ≤ 15% | ESS 비율 최소 | ≥ 5% | 타깃 판정 |",
        "|---|---|---|---|---|---|---|---|---|",
        *_verdict_rows(first, "탐색 2칸"),
        *(_verdict_rows(retry, "탐색 4칸(재실험)") if retry else []),
        "",
        "읽는 법:",
        "- **실측 V**는 같은 seed의 같은 세계에서 타깃 정책을 실제로 7일 돌린 칸당 클릭률이다. 그 세계의 사용자는 "
        "타깃 정책의 화면을 보며 살았다.",
        "- **같은 문맥의 기대값 V_ctx**는 정책 A의 세계의 각 요청에서, 그 사용자가 그 시점에 타깃 화면을 봤다면의 "
        "클릭 확률을 시뮬레이터의 클릭 모델로 계산한 평균이다. 로그 기반 추정이 겨누는 값은 이것이다(서술용).",
        "- 추정값 대 V_ctx는 추정기의 오차를, V_ctx 대 V는 정책이 사용자 상태(노출 이력·클릭)를 바꾸는 효과를 보여 준다.",
        "- ESS 비율은 그 추정기가 본 칸 수로 나눈 값이다(탐색 한정 추정기는 탐색 칸 수, 화면 전체 추정기는 전체 칸 수).",
        "- ◀ 는 그 타깃의 판정 추정기다.", "",
    ]
    lines += _stage_section(first, "탐색 2칸 (운영 기본값)")
    if retry:
        lines += _stage_section(retry, "탐색 4칸 (사전 등록한 재실험, ε = 0.2)")
    else:
        lines += ["## 탐색 4칸 재실험", "", "돌리지 않았다(탐색 2칸에서 통과).", ""]

    lines += ["## 실행 목록", "",
              "| 실행 | 요청 | 클릭 | 칸과 이어지지 않은 클릭 | 응답 출처 | 폴백 | 드라이버 오류율 | 클릭 ACK | 온보딩 실패 | 소요(초) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in meta["runs"].items():
        lines.append(f"| `{name}` | {r['requests']} | {r['clicks']} | {r['clicks_unjoined']} | {r['sources']} | "
                     f"{r['fallbacks'] or '없음'} | {r['driver_error_rate']} | {r['click_ack_rate']} | "
                     f"{r['onboarding_failures']} | {r['wall_seconds']} |")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ E10


def render_fatigue(result: dict) -> str:
    meta, verdict, metrics = result["meta"], result["verdict"], result["metrics"]
    rule = meta["rule"]
    n_seeds = len(meta["config"]["seeds"])
    lines = header(meta, "E10 — 노출 피로 규칙: 세기만(log) 대 적용(enforce)",
                   f"seed {len(meta['config']['seeds'])}개, (seed, 사용자) 군집 {result['clusters']}개의 쌍체 "
                   f"부트스트랩 {rule['bootstrap']['n']}회 (seed {rule['bootstrap']['seed']}) 95% 백분위 구간")
    lines += [
        "## 판정", "",
        f"**E10: {verdict['text']}**", "",
        f"규칙(ADR 0025 A1.4): (1) 반복 노출 비율의 차이 log − enforce의 구간 하한 > 0, (2) 반응성 지표가 나빠지지 않음 - "
        "enforce − log의 구간 전체가 나쁜 쪽(Jaccard는 0 위, similar_share_lift는 0 아래)에 있으면 나빠진 것이다. "
        f"피로 규칙: 최근 {rule['fatigue_hours']}시간에 {rule['fatigue_min_impressions']}번 이상 노출되고 클릭되지 않은 "
        f"아이템. 두 팔 모두 탐색 {rule['explore_slots']}칸.", "",
        "| 조건 | 값 | 결과 |", "|---|---|---|",
        f"| 반복 노출 비율 log − enforce의 95% 구간 | {ci(verdict['log_minus_enforce_repeat_rate_ci95'])} | "
        f"{'하한 > 0' if verdict['repeat_rate_reduced'] else '하한 ≤ 0'} |",
        f"| after_click_jaccard enforce − log의 95% 구간 | "
        f"{ci(metrics['after_click_jaccard_mean']['enforce_minus_log_ci95'], fmt=num, digits=4)} | "
        f"{'나빠짐(하한 > 0)' if verdict['after_click_jaccard_worse'] else '나빠진 것이 검출되지 않음'} |",
        f"| similar_share_lift enforce − log의 95% 구간 | "
        f"{ci(metrics['similar_share_lift']['enforce_minus_log_ci95'], fmt=num, digits=4)} | "
        f"{'나빠짐(상한 < 0)' if verdict['similar_share_lift_worse'] else '나빠진 것이 검출되지 않음'} |",
        "",
        "\"나빠진 것이 검출되지 않음\"은 같다는 증명이 아니다. 구간의 폭을 함께 읽는다. enforce에서 Jaccard가 내려가는 "
        "것은 클릭에 대한 반응이 아니라 규칙이 목록을 바꾼 결과일 수 있어 개선으로 세지 않는다.", "",
        f"## 지표 (seed {n_seeds}개 합산)", "",
        "| 지표 | log (95% 구간) | enforce (95% 구간) | enforce − log (95% 구간) |", "|---|---|---|---|",
    ]
    for name, label in METRIC_LABELS.items():
        m = metrics[name]
        fmt, kw = metric_format(name)
        lines.append(f"| {label} | {fmt(m['log'], **kw)} {ci(m['log_ci95'], fmt=fmt, **kw)} | "
                     f"{fmt(m['enforce'], **kw)} {ci(m['enforce_ci95'], fmt=fmt, **kw)} | "
                     f"{fmt(m['enforce_minus_log'], **kw)} {ci(m['enforce_minus_log_ci95'], fmt=fmt, **kw)} |")
    lines += ["", "## seed별 값", "", "| 지표 | 팔 | " + " | ".join(f"seed {s}" for s in result["metrics_by_seed"]) + " |",
              "|---|---|" + "---|" * len(result["metrics_by_seed"])]
    for name, label in METRIC_LABELS.items():
        fmt, kw = metric_format(name)
        for arm in ("log", "enforce"):
            values = " | ".join(fmt(by_seed[name][arm], **kw) for by_seed in result["metrics_by_seed"].values())
            lines.append(f"| {label} | {arm} | {values} |")
    counts = result["counts"]
    lines += ["", f"## 표본 수 (seed {n_seeds}개 합산)", "",
              "| 팔 | 응답 | 정책이 만든 화면 | 그중 20칸 미만 | 폴백 응답 | 정책 화면의 칸 | 반복 노출 칸 | 규칙이 막는 칸 | 클릭 | "
              "클릭 직후 쌍 | 콜드 유저 요청 | cold_start_* 요청 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in ("log", "enforce"):
        c = counts[arm]
        lines.append(f"| {arm} | {num(c['answers'], 0)} | {num(c['policy_answers'], 0)} | {num(c['short_slates'], 0)} | "
                     f"{num(c['fallback_answers'], 0)} | {num(c['slots'], 0)} | {num(c['repeats'], 0)} | "
                     f"{num(c['rule_repeats'], 0)} | {num(c['clicks'], 0)} | {num(c['pairs'], 0)} | "
                     f"{num(c['cold_requests'], 0)} | {num(c['cold_path_requests'], 0)} |")
    lines += ["", "## 팔·seed별 실행", "",
              "| 팔 | seed | 요청 | 정책이 만든 화면 | 폴백 응답 | 20칸 미만 화면(최소 칸 수) | "
              "규칙에 걸린 후보 수 평균(중앙값, 최대) | 걸린 후보가 있는 요청 비율 | "
              "후보 수 평균(최소) | 탐색 풀 평균 | 폴백 사유 | 소요(초) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, by_seed in result["arms"].items():
        for seed, d in by_seed.items():
            fc, el = d["fatigued_count"], d["eligible_count"]
            lines.append(
                f"| {arm} | {seed} | {d['requests']} | {d['requests_analyzed']} | {d['requests_excluded']} | "
                f"{d['short_slates']} ({d['slate_size_min']}) | "
                f"{num(fc['mean'], 1)} ({num(fc['median'], 0)}, {num(fc['max'], 0)}) | "
                f"{pct(fc['share_of_requests_with_any'], 1)} | {num(el['mean'], 1)} ({num(el['min'], 0)}) | "
                f"{num(d['explore_pool_size_mean'], 1)} | {d['run']['fallbacks'] or '없음'} | "
                f"{d['run']['wall_seconds']} |")
    lines.append("")
    return "\n".join(lines)


RENDERERS = {"ope_validation": render_ope, "fatigue_v1": render_fatigue}


def write_report(source: Path, out_dir: Path, name: str) -> dict:
    """Copies the result JSON as it is and renders the markdown next to it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.json"
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    result = json.loads(target.read_text(encoding="utf-8"))
    (out_dir / f"{name}.md").write_text(RENDERERS[name](result), encoding="utf-8")
    return {"name": name, "json_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "registered_config": result["meta"]["registered_config"]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ope", type=Path, default=None, help="ope_validation.json from sim.ope_validation")
    p.add_argument("--fatigue", type=Path, default=None, help="fatigue_v1.json from sim.fatigue_comparison")
    p.add_argument("--out-dir", type=Path, required=True)
    args = p.parse_args(argv)
    for source, name in ((args.ope, "ope_validation"), (args.fatigue, "fatigue_v1")):
        if source is not None:
            print(json.dumps(write_report(source, args.out_dir, name), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
