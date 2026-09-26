"""포트폴리오 사례 연구 01(추천 평가 누수)의 그림을 `reports/recsys/team_repro_v2.json`에서
생성한다. 그림에 들어가는 모든 수치는 이 JSON에서 읽으며, 스크립트 안에 숫자를
하드코딩하지 않는다. 함께 쓰인 수치의 발췌본(`docs/portfolio/data/01-evaluation-leakage-numbers.json`)도
같은 JSON에서 만들어 두어, 본문 수치의 출처를 한 파일에서 대조할 수 있게 한다.

사용법 (저장소 루트에서):

    python docs/portfolio/scripts/make_01_figures.py \
        --report reports/recsys/team_repro_v2.json \
        --out docs/portfolio/img \
        --extract docs/portfolio/data/01-evaluation-leakage-numbers.json

`reports/recsys/team_repro_v2.json`은 `eval/team-baseline-repro-v1` 브랜치(커밋 1585ce6)에
있다. 아직 main에 병합되지 않았다면 `git show origin/eval/team-baseline-repro-v1:reports/recsys/team_repro_v2.json > /tmp/team_repro_v2.json`
으로 꺼내 `--report`에 넘긴다.

의존성: matplotlib, numpy (표준 sans 폰트만 사용 - 그림 텍스트는 영어로 두어
한글 폰트 유무에 따라 결과가 달라지지 않게 했다).

라이트/다크 두 변형을 각각 PNG로 저장한다. 문서는 GitHub의 `<picture>` +
`prefers-color-scheme`로 둘 중 하나를 고른다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

# 색 토큰: 라이트/다크 각각 검증된 값(categorical slot 1 blue, slot 2 orange).
# 2슬롯 팔레트는 adjacent CVD ΔE 24.7(light)/dark 별도 검증 통과.
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "text": "#0b0b0b",
        "text2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "pit": "#2a78d6",  # point-in-time (series 1, blue)
        "leaky": "#eb6834",  # as-written / leaky (series 2, orange)
        "model": "#2a78d6",  # emphasis hue
        "baseline": "#a8a69f",  # de-emphasis gray
        "connector": "#c3c2b7",
    },
    "dark": {
        "surface": "#1a1a19",
        "text": "#ffffff",
        "text2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "pit": "#3987e5",
        "leaky": "#d95926",
        "model": "#3987e5",
        "baseline": "#5f5e59",
        "connector": "#4a4946",
    },
}

VERSION_LABELS = {
    "team-final": "team-final\n(team's final code, 2026-02)",
    "fix-snapshot": "fix-snapshot\n(2026-07 self-review)",
    "current": "current\n(this fork, harness branch)",
}
VERSION_ORDER = ["team-final", "fix-snapshot", "current"]

BASELINE_LABELS = {
    "random": "random",
    "recency": "recency",
    "category_match": "category_match",
    "cosine_history": "cosine_history",
    "popularity": "popularity",
    "onboarding_newsletter_cosine": "onboarding cosine",
}


def _fmt_signed(x: float) -> str:
    return f"{x:+.2f}"


def _style(ax, t):
    ax.set_facecolor(t["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(t["axis"])
    ax.spines["bottom"].set_linewidth(1)
    ax.tick_params(colors=t["muted"], labelsize=9, length=0)
    ax.xaxis.label.set_color(t["text2"])
    ax.yaxis.label.set_color(t["text2"])
    ax.grid(axis="x", color=t["grid"], linewidth=1, linestyle="-")
    ax.set_axisbelow(True)
    for lab in ax.get_yticklabels():
        lab.set_color(t["text"])


def _source_note(meta: dict) -> str:
    return (
        "Source: reports/recsys/team_repro_v2.json "
        f"(harness {meta['git']['harness_sha'][:7]}, team-final {meta['git']['team_final_sha'][:7]}, "
        f"fix-snapshot {meta['git']['fix_snapshot_sha'][:7]}). Evidence type: [synthetic team data]."
    )


def fig1_inference_leak(report: dict, theme: str, out_path: Path) -> None:
    """같은 학습된 ranker를 point-in-time / as-written 두 방식으로 추론한 결과의 dumbbell.
    양쪽 점의 whisker = 시드 x 유저 nested bootstrap 95% CI, 오른쪽 주석 = paired 효과와 CI."""
    t = THEMES[theme]
    ci = report["headline_ci"]
    meta = report["meta"]
    metrics = [("mrr", "MRR"), ("precision@5", "Precision@5")]

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.6), sharey=True)
    fig.patch.set_facecolor(t["surface"])

    ys = list(range(len(VERSION_ORDER)))[::-1]
    off = 0.14
    for ax, (mkey, mlabel) in zip(axes, metrics):
        _style(ax, t)
        ax.set_xlim(0, 1.0)
        ax.set_xticks([0, 0.2, 0.4, 0.6, 0.8, 1.0])
        ax.set_xlabel(f"{mlabel} (higher is better)", fontsize=10)
        for y, ver in zip(ys, VERSION_ORDER):
            p = ci[ver]["primary"][mkey]
            w = ci[ver]["as_written"][mkey]
            e = ci[ver]["leak_effect"][mkey]
            # connector between the two means
            ax.plot([p["mean"], w["mean"]], [y + off, y - off], color=t["connector"], linewidth=1.2, zorder=1)
            # whiskers (95% CI) and dots, surface ring on the dots
            for val, col, dy in ((p, t["pit"], off), (w, t["leaky"], -off)):
                ax.plot([val["ci_lo"], val["ci_hi"]], [y + dy, y + dy], color=col, linewidth=2, solid_capstyle="round", zorder=2)
                ax.scatter([val["mean"]], [y + dy], s=64, color=col, edgecolor=t["surface"], linewidth=2, zorder=3)
            # value labels above / below the dots (text tokens, not series color) so they never
            # run into the Δ column in the right margin
            ax.annotate(f"{p['mean']:.2f}", (p["mean"], y + off), xytext=(0, 7), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8.5, color=t["text2"])
            ax.annotate(f"{w['mean']:.2f}", (w["mean"], y - off), xytext=(0, -7), textcoords="offset points",
                        ha="center", va="top", fontsize=8.5, color=t["text2"])
            # paired effect annotation on the right margin
            ax.text(1.04, y, f"Δ {_fmt_signed(e['effect'])}\n[{_fmt_signed(e['ci_lo'])}, {_fmt_signed(e['ci_hi'])}]",
                    transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=8.5, color=t["text"],
                    family="monospace")
        ax.set_yticks(ys)
        ax.set_yticklabels([VERSION_LABELS[v] for v in VERSION_ORDER], fontsize=9)
        ax.set_ylim(-0.6, len(VERSION_ORDER) - 0.4)
        ax.set_title(mlabel, fontsize=11, color=t["text"], loc="left", pad=10)

    n_users = ci["current"]["primary"]["mrr"]["n_users"]
    seeds = meta["seeds"]
    legend_handles = [
        Line2D([0], [0], marker="o", color=t["pit"], markersize=8, linewidth=2, label="point-in-time inference (history cut at answer_start)"),
        Line2D([0], [0], marker="o", color=t["leaky"], markersize=8, linewidth=2, label="as-written inference (history built at dataset end; team's flow)"),
    ]
    fig.legend(handles=legend_handles, loc="upper left", bbox_to_anchor=(0.01, 0.985), ncol=2, frameon=False,
               fontsize=9, labelcolor=t["text"])
    fig.suptitle("Same trained LightGBM+MMR ranker, two inference protocols - the gap is inference-time leakage",
                 x=0.01, y=1.06, ha="left", fontsize=12.5, color=t["text"], fontweight="bold")
    note = (
        f"Dots = mean over seeds; whiskers = 95% nested bootstrap CI (seed × user, {ci['current']['primary']['mrr']['n_boot']:,} resamples); "
        f"Δ = as-written − point-in-time, paired CI. n = {n_users} evaluated synthetic users; seeds: "
        f"{len(seeds['headline_current_team_final'])} (current, team-final), {len(seeds['headline_fix_snapshot'])} (fix-snapshot). "
        "Absolute values are NOT service performance (LLM-generated clicks, one 6.3 h snapshot).\n" + _source_note(meta)
    )
    fig.text(0.01, -0.06, note, ha="left", va="top", fontsize=7.6, color=t["muted"], wrap=True)
    fig.subplots_adjust(left=0.19, right=0.90, top=0.80, bottom=0.17, wspace=0.48)
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor=t["surface"], pad_inches=0.25)
    plt.close(fig)


def fig2_baselines(report: dict, theme: str, out_path: Path) -> None:
    """point-in-time 프로토콜에서 모델(강조색)과 베이스라인(회색)을 같은 정답 구간·후보 풀 위에서 비교."""
    t = THEMES[theme]
    meta = report["meta"]
    bl = report["baselines"]["team_split"]
    ci = report["headline_ci"]
    ds = report["data_structure"]
    metrics = [("precision@5", "Precision@5"), ("mrr", "MRR")]

    rows = []  # (label, kind, {metric: (value, lo, hi)})
    for key, label in BASELINE_LABELS.items():
        agg = bl[key]["aggregate"]
        rows.append((label, "baseline", {m: (agg[m], None, None) for m, _ in metrics}))
    for ver, label in (("current", "current model (point-in-time)"), ("team-final", "team-final model (point-in-time)")):
        vals = {m: (ci[ver]["primary"][m]["mean"], ci[ver]["primary"][m]["ci_lo"], ci[ver]["primary"][m]["ci_hi"]) for m, _ in metrics}
        rows.append((label, "model", vals))
    # one fixed order for both panels: by Precision@5 descending
    rows.sort(key=lambda r: r[2]["precision@5"][0], reverse=True)

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.0), sharey=True)
    fig.patch.set_facecolor(t["surface"])
    ys = list(range(len(rows)))[::-1]
    for ax, (mkey, mlabel) in zip(axes, metrics):
        _style(ax, t)
        ax.set_xlim(0, 0.8 if mkey == "precision@5" else 1.0)
        ax.set_xlabel(f"{mlabel} under point-in-time protocol (higher is better)", fontsize=10)
        for y, (label, kind, vals) in zip(ys, rows):
            v, lo, hi = vals[mkey]
            col = t["model"] if kind == "model" else t["baseline"]
            ax.barh(y, v, height=0.44, color=col, zorder=2)
            if lo is not None:
                ax.plot([lo, hi], [y, y], color=t["text"], linewidth=1.4, solid_capstyle="round", zorder=3)
                ax.scatter([v], [y], s=22, color=t["text"], zorder=4)
                ax.annotate(f"{v:.2f}  [{lo:.2f}, {hi:.2f}]", (hi, y), xytext=(6, 0), textcoords="offset points",
                            ha="left", va="center", fontsize=8.5, color=t["text"])
            elif label in ("popularity", "random", "onboarding cosine"):
                ax.annotate(f"{v:.2f}", (v, y), xytext=(6, 0), textcoords="offset points",
                            ha="left", va="center", fontsize=8.5, color=t["text2"])
        if mkey == "precision@5":
            dens = ds["random_p5_base_rate_mean_answer_density"]
            ax.axvline(dens, color=t["muted"], linewidth=1, zorder=1)
            ax.text(dens, len(rows) - 0.45, f"answer density {dens:.2f}\n(random expectation)", ha="center", va="bottom",
                    fontsize=8, color=t["muted"])
        ax.set_yticks(ys)
        ax.set_yticklabels([r[0] for r in rows], fontsize=9)
        ax.set_ylim(-0.7, len(rows) - 0.3 + 0.6)
        ax.set_title(mlabel, fontsize=11, color=t["text"], loc="left", pad=10)

    handles = [
        Patch(color=t["model"], label="LightGBM+MMR model (mean over seeds; whisker = 95% nested bootstrap CI)"),
        Patch(color=t["baseline"], label="baseline on the same logs, candidate pool and answer window (deterministic)"),
    ]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.01, 0.985), ncol=1, frameon=False, fontsize=9,
               labelcolor=t["text"])
    fig.suptitle("Under point-in-time inference the model does not beat popularity or cosine baselines",
                 x=0.01, y=1.07, ha="left", fontsize=12.5, color=t["text"], fontweight="bold")
    n_users = ci["current"]["primary"]["mrr"]["n_users"]
    note = (
        f"n = {n_users} evaluated synthetic users with clicks after answer_start ({ds['answer_start']}); "
        f"{ds['n_cold_users']} of {ds['n_users_total']} personas had no click before the boundary. "
        f"Candidate pool = all {ds['n_newsletters']} newsletters. Model seeds: 3. "
        "Category-dependent baselines rest on 76% kNN-imputed labels (LOO accuracy 0.30). "
        "Absolute values are NOT service performance.\n" + _source_note(meta)
    )
    fig.text(0.01, -0.05, note, ha="left", va="top", fontsize=7.6, color=t["muted"], wrap=True)
    fig.subplots_adjust(left=0.22, right=0.985, top=0.78, bottom=0.15, wspace=0.30)
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor=t["surface"], pad_inches=0.25)
    plt.close(fig)


def write_extract(report: dict, out_path: Path, report_path: Path) -> None:
    """본문/그림에 쓰인 수치만 발췌해 출처(SHA, 데이터 해시)와 함께 남긴다."""
    ci = report["headline_ci"]
    bl = report["baselines"]["team_split"]
    dec = report["decomposition_bootstrap"]
    hl = report["headline"]

    def _ci(d):
        return {k: d[k] for k in ("mean", "ci_lo", "ci_hi", "n_users", "n_seeds", "n_boot") if k in d}

    def _eff(d):
        return {k: d[k] for k in ("effect", "ci_lo", "ci_hi", "n_users", "n_seeds_a", "n_seeds_b", "n_boot") if k in d}

    extract = {
        "provenance": {
            "source_report": str(report_path.name),
            "source_report_repo_path": "reports/recsys/team_repro_v2.json",
            "source_branch": report["meta"]["git"]["current_branch"],
            "harness_sha": report["meta"]["git"]["harness_sha"],
            "team_final_sha": report["meta"]["git"]["team_final_sha"],
            "fix_snapshot_sha": report["meta"]["git"]["fix_snapshot_sha"],
            "data_sha256": report["meta"]["data_sha256"],
            "generated_at_of_report": report["meta"]["generated_at"],
            "evidence_type": "[synthetic team data]",
        },
        "data_structure": report["data_structure"],
        "seeds": report["meta"]["seeds"],
        "headline_ci": {
            ver: {
                "point_in_time": {m: _ci(ci[ver]["primary"][m]) for m in ("mrr", "precision@5", "ndcg@5")},
                "as_written": {m: _ci(ci[ver]["as_written"][m]) for m in ("mrr", "precision@5", "ndcg@5")},
                "leak_effect": {m: _eff(ci[ver]["leak_effect"][m]) for m in ("mrr", "precision@5", "ndcg@5")},
            }
            for ver in VERSION_ORDER
        },
        "headline_training_diagnostics": {
            ver: {
                "best_iteration": hl[ver]["primary_summary"].get("best_iteration"),
                "n_distinct_scores_primary": hl[ver]["primary_summary"].get("n_distinct_scores_primary"),
                "mrr_values_by_seed_point_in_time": hl[ver]["primary_summary"]["mrr"]["values"],
                "mrr_values_by_seed_as_written": hl[ver]["as_written_summary"]["mrr"]["values"],
            }
            for ver in VERSION_ORDER
        },
        "baselines_point_in_time": {
            key: {m: bl[key]["aggregate"][m] for m in ("mrr", "precision@5", "ndcg@5", "coverage@5")}
            for key in BASELINE_LABELS
        },
        "baselines_cold_warm": {
            key: {
                "cold": {m: bl[key]["cold"][m] for m in ("mrr", "precision@5", "num_users")},
                "warm": {m: bl[key]["warm"][m] for m in ("mrr", "precision@5", "num_users")},
            }
            for key in BASELINE_LABELS
        },
        "decomposition_effects_inconclusive": {
            arm: {m: _eff(dec[arm][m]) for m in ("mrr", "precision@5")} for arm in ("leakage", "objective", "negatives")
        },
        "team_final_as_written_degenerate_row": {
            "mrr": report["team_final_written_summary"]["mrr"],
            "recall@5": report["team_final_written_summary"]["recall@5"],
            "note": "recall@5 = 5/195 means every one of the 195 items counted as an answer for every user; any ranking scores MRR 1.0. Withdrawn - see case study.",
        },
        "generator_split_recency_baseline": {
            "mrr": report["baselines"]["generator_split"]["recency"]["aggregate"]["mrr"],
            "precision@5": report["baselines"]["generator_split"]["recency"]["aggregate"]["precision@5"],
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(extract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", type=Path, default=REPO_ROOT / "reports" / "recsys" / "team_repro_v2.json")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "portfolio" / "img")
    ap.add_argument("--extract", type=Path, default=REPO_ROOT / "docs" / "portfolio" / "data" / "01-evaluation-leakage-numbers.json")
    args = ap.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    if report.get("meta", {}).get("report_version") != "v2":
        raise SystemExit("team_repro_v2.json(report_version == 'v2')만 지원한다.")
    args.out.mkdir(parents=True, exist_ok=True)

    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans", "Helvetica", "Arial"],
        "axes.titleweight": "bold",
        "savefig.transparent": False,
    })
    for theme in THEMES:
        fig1_inference_leak(report, theme, args.out / f"01-fig1-inference-leak-{theme}.png")
        fig2_baselines(report, theme, args.out / f"01-fig2-baselines-{theme}.png")
        print(f"wrote {args.out / f'01-fig1-inference-leak-{theme}.png'}")
        print(f"wrote {args.out / f'01-fig2-baselines-{theme}.png'}")
    write_extract(report, args.extract, args.report)
    print(f"wrote {args.extract}")


if __name__ == "__main__":
    main()
