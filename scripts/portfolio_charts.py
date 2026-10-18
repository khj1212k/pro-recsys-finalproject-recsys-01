"""커밋된 리포트 JSON에서 README·케이스 스터디용 SVG 차트 3장을 만든다 (표준 라이브러리만 사용).

    python scripts/portfolio_charts.py            # docs/assets/*.svg 를 다시 쓴다
    python scripts/portfolio_charts.py --check    # 쓰지 않고, 커밋된 SVG와 다르면 종료 코드 1

입력(모두 저장소에 커밋된 파일):
    reports/recsys/team_repro_v2.json      -> leakage_before_after_mrr.svg   [team-synth]
    reports/recsys/ebnerd_v1.json          -> ebnerd_negatives_define_task.svg   [EB-NeRD]
    reports/recsys/ebnerd_v1_3_neural.json -> e15_neural_vs_lightgbm.svg   [EB-NeRD]

출력은 입력 JSON이 같으면 바이트까지 같다(시각·난수 없음). 막대·점·CI와 캡션의 표본 크기·부트스트랩 횟수·trial 수·seed 수는
JSON에서 읽는다. 손으로 적은 것은 제목·범례·한계 문구(숫자 없음)뿐이다.
SVG는 흰 카드 배경을 직접 그려서 밝은 테마와 어두운 테마의 GitHub에서 모두 읽힌다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "recsys"
OUT_DIR = ROOT / "docs" / "assets"

# 색: 범주 두 개(밝은 면 #fcfcfb에서 검증, 대비 3:1 이상, 인접 쌍 CVD ΔE 24.7)
BLUE = "#2a78d6"
ORANGE = "#eb6834"
SURFACE = "#fcfcfb"
BORDER = "#d9d8d4"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e6e5e1"
FONT = "-apple-system, 'Apple SD Gothic Neo', 'Noto Sans KR', 'Segoe UI', sans-serif"
MINUS = "−"


def text_width(s: str, size: int) -> float:
    """SVG에는 글자 폭 측정이 없어서 한글(전각)과 그 밖의 글자의 평균 폭으로 어림한다."""
    return sum(size * (1.0 if ord(c) > 0x2E80 else 0.58) for c in s)


def fmt(v: float, digits: int = 4, signed: bool = False) -> str:
    s = f"{abs(v):.{digits}f}"
    if v < 0 and float(s) != 0.0:
        return MINUS + s
    return ("+" if signed else "") + s


def ci_text(lo: float, hi: float, digits: int = 4, signed: bool = True) -> str:
    return f"[{fmt(lo, digits, signed)}, {fmt(hi, digits, signed)}]"


class Svg:
    def __init__(self, width: int, height: int, title: str, desc: str) -> None:
        self.width, self.height = width, height
        self.parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-labelledby="t d" font-family="{FONT}">',
            f"<title id=\"t\">{escape(title)}</title>",
            f"<desc id=\"d\">{escape(desc)}</desc>",
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="8" fill="{SURFACE}" stroke="{BORDER}"/>',
        ]

    def text(self, x: float, y: float, s: str, *, size: int = 12, fill: str = INK, anchor: str = "start",
             weight: str = "normal") -> None:
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(s)}</text>'
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, *, stroke: str = GRID, width: float = 1,
             dash: str = "") -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" '
            f'stroke-width="{width}"{d}/>'
        )

    def rect(self, x: float, y: float, w: float, h: float, fill: str, *, rx: float = 0) -> None:
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}"/>'
        )

    def circle(self, cx: float, cy: float, r: float, fill: str) -> None:
        self.parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill}" stroke="{SURFACE}" stroke-width="2"/>'
        )

    def whisker(self, x_lo: float, x_hi: float, y: float, cap: float = 4, color: str = INK) -> None:
        self.line(x_lo, y, x_hi, y, stroke=color, width=1.5)
        self.line(x_lo, y - cap, x_lo, y + cap, stroke=color, width=1.5)
        self.line(x_hi, y - cap, x_hi, y + cap, stroke=color, width=1.5)

    def legend_swatch(self, x: float, y: float, color: str, label: str) -> float:
        self.rect(x, y - 9, 12, 12, color, rx=2)
        self.text(x + 18, y + 1, label, size=12, fill=INK2)
        return x + 18 + text_width(label, 12) + 22

    def render(self) -> str:
        return "\n".join(self.parts + ["</svg>", ""])


def load(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def x_axis(svg: Svg, scale, ticks: list[float], y_top: float, y_bottom: float, digits: int, *, zero_line: bool = False
           ) -> None:
    for t in ticks:
        x = scale(t)
        is_zero = zero_line and abs(t) < 1e-12
        svg.line(x, y_top, x, y_bottom, stroke=INK2 if is_zero else GRID, width=1.2 if is_zero else 1)
        svg.text(x, y_bottom + 16, fmt(t, digits), size=11, fill=INK2, anchor="middle")


# ---------------------------------------------------------------------------------------------
# 1. 누출 전/후 MRR  [team-synth]
# ---------------------------------------------------------------------------------------------
def chart_leakage(rep: dict) -> str:
    groups = [
        ("team-final", "team-final 코드"),
        ("fix-snapshot", "fix-snapshot"),
        ("current", "current 코드"),
    ]
    width, height = 800, 424
    svg = Svg(
        width, height,
        "같은 모델을 point-in-time과 팀 방식 추론 시점으로 평가한 MRR",
        "코드 버전 세 개 모두 팀 방식 추론 시점(as-written)의 MRR이 point-in-time보다 높고, 쌍 차이의 95% 신뢰구간이 0을 제외한다.",
    )
    primary = [rep["headline_ci"][key]["primary"]["mrr"] for key, _ in groups]
    n_users = sorted({m["n_users"] for m in primary})
    n_boot = sorted({m["n_boot"] for m in primary})
    if len(n_users) != 1 or len(n_boot) != 1:
        raise ValueError(f"코드 버전마다 평가 유저 수나 부트스트랩 횟수가 다르다: {n_users}, {n_boot}")
    svg.text(24, 30, "같은 모델, 추론 시점만 다르다: 누출을 걷어내면 MRR이 내려간다", size=16, weight="bold")
    svg.text(24, 50, f"[team-synth] 팀이 남긴 LLM 합성 클릭, 평가 유저 {n_users[0]}명. 절대값은 성능이 아니라 진단이다.",
             size=12, fill=INK2)
    x = svg.legend_swatch(24, 76, BLUE, "point-in-time (정답을 못 보는 추론)")
    svg.legend_swatch(x, 76, ORANGE, "as-written (팀 방식: 정답이 피처에 들어감)")

    x0, x1 = 170.0, 540.0

    def sx(v: float) -> float:
        return x0 + (v - 0.0) / 1.0 * (x1 - x0)

    top, bar_h = 104.0, 18.0
    group_h = 84.0
    plot_bottom = top + group_h * len(groups) - 10
    x_axis(svg, sx, [0.0, 0.25, 0.5, 0.75, 1.0], top - 6, plot_bottom, 2)
    svg.text(x1 + 62, top - 12, "누출 효과 ΔMRR", size=12, weight="bold")
    svg.text(x1 + 62, top + 2, "(as-written − point-in-time)", size=11, fill=INK2)

    for i, (key, label) in enumerate(groups):
        h = rep["headline_ci"][key]
        pit, asw, leak = h["primary"]["mrr"], h["as_written"]["mrr"], h["leak_effect"]["mrr"]
        gy = top + 14 + i * group_h
        svg.text(24, gy + 14, label, size=13, weight="bold")
        svg.text(24, gy + 32, f"시드 {pit['n_seeds']}개", size=11, fill=INK2)
        for j, (m, color) in enumerate(((pit, BLUE), (asw, ORANGE))):
            y = gy + j * (bar_h + 6)
            svg.rect(x0, y, sx(m["mean"]) - x0, bar_h, color, rx=3)
            svg.whisker(sx(m["ci_lo"]), sx(m["ci_hi"]), y + bar_h / 2)
            svg.text(sx(m["ci_hi"]) + 8, y + bar_h / 2 + 4, fmt(m["mean"]), size=12)
        cy = gy + bar_h + 3
        svg.text(x1 + 62, cy - 2, fmt(leak["effect"], 4, True), size=15, weight="bold")
        svg.text(x1 + 62, cy + 16, ci_text(leak["ci_lo"], leak["ci_hi"]), size=11, fill=INK2)

    svg.text(24, height - 28,
             f"막대 = 시드 평균, 수염 = 시드×유저 nested bootstrap 95% CI({n_boot[0]:,}회). Δ는 시드를 쌍으로 재표본한 쌍 CI.",
             size=11, fill=INK2)
    svg.text(24, height - 12, "출처: reports/recsys/team_repro_v2.json (headline_ci). 다중비교 보정 없음.", size=11, fill=INK2)
    return svg.render()


# ---------------------------------------------------------------------------------------------
# 2. 같은 ablation 사슬, 다른 과제  [EB-NeRD]
# ---------------------------------------------------------------------------------------------
STEPS = [
    ("lambdarank_user_groups-vs-team_binary", "binary → LambdaRank(유저 그룹)"),
    ("lambdarank_impression_groups-vs-lambdarank_user_groups", "유저 그룹 → 노출 그룹"),
    ("inview_negatives-vs-lambdarank_impression_groups", "무작위 → 노출 비클릭 네거티브"),
    ("plus_trailing_popularity-vs-inview_negatives", "+ trailing 인기도"),
    ("plus_short_term_session-vs-plus_trailing_popularity", "+ 단기·세션 벡터"),
    ("ranker_v2-vs-plus_short_term_session", "+ 카테고리 share·히스토리 길이"),
]


def chart_negatives(rep: dict) -> str:
    width, height = 800, 560
    svg = Svg(
        width, height,
        "EB-NeRD ablation 사슬의 단계별 nDCG@10 변화: 노출 재정렬과 전체 풀 랭킹",
        "노출 비클릭 네거티브로 바꾸는 단계는 노출 재정렬(P1)에서 +0.1868, 전체 풀 랭킹(P2)에서 −0.1307이다. 모델 교체(binary에서 LambdaRank)는 두 과제 모두 0에 가깝다.",
    )
    svg.text(24, 30, "네거티브 분포가 과제를 정한다: 같은 단계, 반대 부호", size=16, weight="bold")
    svg.text(24, 50, "[EB-NeRD] ebnerd_small, 단계별 nDCG@10 변화(같은 요청의 쌍체 차이). 이 서비스의 성능이 아니다.",
             size=12, fill=INK2)
    x = svg.legend_swatch(24, 76, BLUE, "개선(95% CI가 0 위)")
    svg.legend_swatch(x, 76, ORANGE, "악화(95% CI가 0 아래)")

    vmin, vmax = -0.15, 0.20
    x0, x1 = 268.0, 740.0

    def sx(v: float) -> float:
        return x0 + (v - vmin) / (vmax - vmin) * (x1 - x0)

    ticks = [-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15, 0.20]
    row_h = 24.0
    n_p1 = rep["p1"]["ranker_v2"]["ndcg@10"]["n"]
    n_p2 = rep["p2"]["ranker_v2"]["ndcg@10"]["n"]
    pool = round(rep["p2_info"]["pool_size"]["mean"])
    panels = [
        ("p1_ablation_diffs", f"P1 노출 재정렬 — 편집자가 보여 준 후보 안의 순위 ({n_p1:,} 노출)"),
        ("p2_ablation_diffs", f"P2 전체 풀 랭킹 — 48시간 안 발행 기사 전부 ({n_p2:,} 요청, 평균 후보 {pool}개, 서빙형 과제)"),
    ]
    y = 108.0
    for key, title in panels:
        svg.text(24, y, title, size=13, weight="bold")
        top = y + 12
        bottom = top + row_h * len(STEPS)
        x_axis(svg, sx, ticks, top, bottom, 2, zero_line=True)
        for i, (step, label) in enumerate(STEPS):
            d = rep[key][step]["ndcg@10"]
            lo, hi = d["ci95"]
            cy = top + i * row_h + row_h / 2
            svg.text(24, cy + 4, label, size=12, fill=INK)
            color = BLUE if lo > 0 else ORANGE if hi < 0 else INK2
            bx = min(sx(0), sx(d["diff"]))
            svg.rect(bx, cy - 6, abs(sx(d["diff"]) - sx(0)), 12, color, rx=2)
            svg.whisker(sx(lo), sx(hi), cy, cap=3)
            if d["diff"] >= 0:
                svg.text(sx(max(hi, d["diff"])) + 8, cy + 4, fmt(d["diff"], 4, True), size=11, weight="bold")
            else:
                svg.text(sx(min(lo, d["diff"])) - 8, cy + 4, fmt(d["diff"], 4, True), size=11, weight="bold",
                         anchor="end")
        y = bottom + 46

    p1, p2 = rep["p1"], rep["p2"]

    def mean(task: dict, model: str) -> str:
        return fmt(task[model]["ndcg@10"]["mean"], 4)

    svg.text(24, height - 44,
             f"같은 노출 비클릭 모델(ranker_v2)의 nDCG@10: P1 {mean(p1, 'ranker_v2')}, P2 {mean(p2, 'ranker_v2')} "
             f"(popularity_6h는 P2 {mean(p2, 'popularity_6h')}).",
             size=11, fill=INK2)
    svg.text(24, height - 28,
             f"48h 풀 무작위 네거티브로 학습한 ranker_v2_poolneg: P2 {mean(p2, 'ranker_v2_poolneg')}, "
             f"P1 {mean(p1, 'ranker_v2_poolneg')} (P1에서는 무작위 수준).",
             size=11, fill=INK2)
    svg.text(24, height - 12,
             "출처: reports/recsys/ebnerd_v1.json (p1_ablation_diffs, p2_ablation_diffs, p1, p2). 수염 = 유저 단위 부트스트랩 95% CI.",
             size=11, fill=INK2)
    return svg.render()


# ---------------------------------------------------------------------------------------------
# 3. E15: 신경망 대 LightGBM  [EB-NeRD]
# ---------------------------------------------------------------------------------------------
def chart_e15(rep: dict) -> str:
    width, height = 800, 352
    svg = Svg(
        width, height,
        "E15 판정: 신경망 arm과 스태킹 arm의 nDCG@10 차이(튜닝한 LightGBM A* 대비)",
        "네 비교 모두 차이의 95% 신뢰구간이 0 아래이고 사전 등록한 seed 규칙을 통과하지 못했다(seed_rule_failed).",
    )
    svg.text(24, 30, "NRMS-lite는 튜닝한 LightGBM을 넘지 못했다 (E15, 사전 등록 판정)", size=16, weight="bold")
    svg.text(24, 50, "[EB-NeRD] 같은 노출의 쌍체 차이 ΔnDCG@10 = arm − A*(튜닝한 LightGBM), 95% CI. 한국어 서비스의 성능이 아니다.",
             size=12, fill=INK2)

    vmin, vmax = -0.06, 0.01
    x0, x1 = 240.0, 560.0

    def sx(v: float) -> float:
        return x0 + (v - vmin) / (vmax - vmin) * (x1 - x0)

    ticks = [-0.06, -0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01]
    top = 92.0
    row_h = 40.0
    rows = [
        ("p1", "sel", "P1 · NRMS-lite(선택된 신경망)"),
        ("p1", "D", "P1 · 신경망 점수 + GBDT 스태킹(D)"),
        ("p2", "sel", "P2 · NRMS-lite(선택된 신경망)"),
        ("p2", "D", "P2 · 신경망 점수 + GBDT 스태킹(D)"),
    ]
    bottom = top + row_h * len(rows)
    x_axis(svg, sx, ticks, top - 8, bottom, 2, zero_line=True)
    svg.text(sx(0) - 8, top - 16, "← A*가 더 높음", size=11, fill=INK2, anchor="end")
    svg.text(sx(0) + 8, top - 16, "arm이 더 높음 →", size=11, fill=INK2)
    svg.text(x1 + 68, top - 16, "판정(기계)", size=11, fill=INK2, weight="bold")
    verdicts = rep["verdict"]["by_task"]
    for i, (task, arm, label) in enumerate(rows):
        v = verdicts[task][arm]
        diff, (lo, hi) = v["comparison"]["diff"], v["comparison"]["ci95"]
        cy = top + i * row_h + row_h / 2
        svg.text(24, cy + 4, label, size=12)
        svg.whisker(sx(lo), sx(hi), cy, cap=5)
        svg.circle(sx(diff), cy, 5.5, ORANGE)
        svg.text(x1 + 68, cy - 2, v["outcome"], size=11, weight="bold")
        svg.text(x1 + 68, cy + 14, f"{fmt(diff, 4, True)} {ci_text(lo, hi)}", size=10, fill=INK2)
    meta = rep["meta"]
    sel = {t: verdicts[t]["sel"]["comparison"] for t in ("p1", "p2")}
    svg.text(24, height - 60,
             f"수염은 유저 단위 부트스트랩 95% CI이고 seed 간 변동을 담지 않는다(NRMS-lite의 seed 간 표준편차 "
             f"P1 {fmt(sel['p1']['sd_seed'])}, P2 {fmt(sel['p2']['sd_seed'])}).",
             size=11, fill=INK2)
    svg.text(24, height - 44,
             f"판정은 seed 규칙(seed마다 CI 하한 > 0, 결합 하한 > 최소 효과)으로 했다. NRMS-lite의 결합 하한은 "
             f"P1 {fmt(sel['p1']['combined_lower'])}, P2 {fmt(sel['p2']['combined_lower'])}.",
             size=11, fill=INK2)
    svg.text(24, height - 28,
             f"신경망 family당 {meta['neural_trials']} trial, A* {meta['gbdt_trials']} trial, seed {len(meta['seeds'])}개. "
             "범위: NRMS-lite(고정 BGE-M3 투영) 한정. 'GBDT가 신경망보다 낫다'로 일반화하지 않는다.",
             size=11, fill=INK2)
    svg.text(24, height - 12, "출처: reports/recsys/ebnerd_v1_3_neural.json (verdict.by_task). 증거 등급 preregistered-run.",
             size=11, fill=INK2)
    return svg.render()


CHARTS = {
    "leakage_before_after_mrr.svg": lambda: chart_leakage(load("team_repro_v2.json")),
    "ebnerd_negatives_define_task.svg": lambda: chart_negatives(load("ebnerd_v1.json")),
    "e15_neural_vs_lightgbm.svg": lambda: chart_e15(load("ebnerd_v1_3_neural.json")),
}


def build_all() -> dict[str, str]:
    return {name: make() for name, make in CHARTS.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="SVG를 쓸 디렉터리 (기본 docs/assets)")
    ap.add_argument("--check", action="store_true", help="쓰지 않고 커밋된 SVG와 다르면 1로 끝낸다")
    args = ap.parse_args(argv)

    built = build_all()
    if args.check:
        stale = [n for n, body in built.items()
                 if not (args.out / n).exists() or (args.out / n).read_text(encoding="utf-8") != body]
        for n in stale:
            print(f"다름: {args.out / n}", file=sys.stderr)
        return 1 if stale else 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name, body in built.items():
        (args.out / name).write_text(body, encoding="utf-8")
        print(f"썼음: {args.out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
