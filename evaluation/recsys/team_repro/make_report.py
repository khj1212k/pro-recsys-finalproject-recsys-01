"""reports/recsys/team_repro_v1.json(+_raw.json)을 읽어 한국어 Markdown 리포트
(team_repro_v1.md)를 생성한다. 숫자를 다시 계산하지 않는다 - run_repro.py가 이미
계산해 저장한 값을 그대로 표로 옮길 뿐이다(리포트 생성과 실험 실행을 분리)."""
from __future__ import annotations

import json
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = WORKTREE_ROOT / "reports" / "recsys"


def pct(x, digits=4):
    if x is None:
        return "N/A"
    return f"{x:.{digits}f}"


def fmt_effect(d: dict) -> str:
    return f"{d['effect']:+.4f} (95% CI [{d['ci_lo']:+.4f}, {d['ci_hi']:+.4f}], n={d['n_users']})"


def main() -> None:
    data = json.loads((REPORT_DIR / "team_repro_v1.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    headline = data["headline"]
    headline_ci = data["headline_ci"]
    gen = data["generator_split"]
    baselines = data["baselines"]
    decomp = data["decomposition_summary"]
    boot = data["decomposition_bootstrap"]

    lines = []
    A = lines.append

    A("# 팀 베이스라인 재현 및 분해 실험 (team_repro v1)")
    A("")
    A(f"- 생성 시각: {meta['generated_at']}")
    A(f"- 현재(fixed) 코드 브랜치: `{meta['git']['current_branch']}` @ `{meta['git']['current_head']}`")
    A(f"- team-final(리포트 당시 코드, main) SHA: `{meta['git']['team_final_sha']}`")
    A(f"- fix-snapshot(중간 수정본) SHA: `{meta['git']['fix_snapshot_sha']}`")
    A("- 데이터 sha256:")
    for k, v in meta["data_sha256"].items():
        A(f"  - `{k}`: `{v}`")
    A("")
    A("## 0. 이 문서가 재현할 수 없는 것 (먼저 밝힘)")
    A("")
    A(
        "- DB 테이블 `user_newsletter_ctr_log`에는 `is_clicked` 컬럼이 없고, "
        "`DataLoader.load_ctr_logs()`는 그 테이블의 모든 행을 그대로 '클릭'으로 읽는다. "
        "합성 CSV(`synthetic_ctr_logs.csv`)에는 `is_clicked` 컬럼이 남아있지만, 이걸 "
        "실제로 어떻게 DB에 적재했는지(1인 행만 넣었는지/전부 넣었는지) 알려주는 "
        "로더 스크립트는 접근 불가능한 Notion 페이지에만 있다. 이 문서는 "
        "`is_clicked==1`만 클릭으로 보는 것을 1차 가정(clicks_only)으로 삼고, "
        "'모든 행을 클릭으로 취급'하는 대안(all_rows)도 별도로 보고한다 (4-(d))."
    )
    A(
        "- 후보 풀 실험(4-(b))의 `padded_400`은 실제 405건 규모의 새 콘텐츠가 아니라, "
        "195건의 임베딩에 약한 가우시안 잡음을 더한 근접 변형 사본이다 - 진짜 더 큰 "
        "카탈로그가 실제로 어떻게 행동할지의 근사치일 뿐이다."
    )
    A(
        "- `small_recent_15`도 문자 그대로 '검증 당일 존재했던 뉴스레터'는 아니다 - "
        "195건 전체가 클릭 로그 시작(2026-01-30 19:26) 이전에 이미 작성되어 있어서 "
        "(가장 최근 생성일도 2026-01-29) 그 정의로는 195건이 그대로 나온다. 대신 "
        "가장 최근 생성일(15건)로 '작은 후보 풀'을 조작적으로 정의했다."
    )
    A("")

    A("## 1. 재현 vs 팀이 보고한 수치")
    A("")
    A(
        "팀 최종 Notion 보고: MRR 0.897, Precision@5 0.798, nDCG@5 0.801, Coverage@5 0.408 "
        "(103명 유저, 14,784 로그, 405 뉴스레터, 팀이 스스로 '쉬운 과제' 의심 + 멘토도 동일 지적)."
    )
    A("")
    A(
        f"여기서 쓴 아카이브 스냅샷은 **100명 유저, {meta['dataset_window']['n_ctr_logs']}건 로그 "
        f"({meta['dataset_window']['n_clicks']}건 클릭, "
        f"{meta['dataset_window']['n_clicks']/meta['dataset_window']['n_ctr_logs']*100:.1f}%), "
        f"195건 뉴스레터**, 로그 구간은 "
        f"`{meta['dataset_window']['start']}` ~ `{meta['dataset_window']['end']}` "
        "(약 6시간 16분) 로 팀 보고 스냅샷(103명/5일 내외로 추정/405건)과 규모가 다르다 - "
        "숫자를 직접 비교하려는 게 아니라, '고쳐도 여전히 쉬운 과제인가'를 같은 저울 "
        "위에서 보려는 것이다."
    )
    A("")
    A("### 1-1. team_split 프로토콜 (팀 main_lgbm.py의 동적 시간순 20% 분할), clicks_only, 후보 195건 전체")
    A("")
    A("| 코드 버전 | 시드 수 | MRR | Precision@5 | nDCG@5 | nDCG@10 | Coverage@5 | AUC | 평가 유저 수 |")
    A("|---|---|---|---|---|---|---|---|---|")
    for version in ["team-final", "fix-snapshot", "current"]:
        s = headline[version]["summary"]
        label = {"team-final": "team-final (팀 최종, 리포트 당시 코드)", "fix-snapshot": "fix-snapshot (중간 수정본)", "current": "current (이 브랜치, FIX #4/#5 반영)"}[version]
        n_users_mean = s.get("num_users", {}).get("mean")
        n_users_str = f"~{n_users_mean:.0f}" if n_users_mean is not None else "-"
        A(
            f"| {label} | {s.get('n_seeds','-')} "
            f"| {pct(s['mrr']['mean'])} ± {pct(s['mrr']['std'])} "
            f"| {pct(s['precision@5']['mean'])} ± {pct(s['precision@5']['std'])} "
            f"| {pct(s['ndcg@5']['mean'])} ± {pct(s['ndcg@5']['std'])} "
            f"| {pct(s.get('ndcg@10',{}).get('mean'))} ± {pct(s.get('ndcg@10',{}).get('std'))} "
            f"| {pct(s['coverage@5']['mean'])} ± {pct(s['coverage@5']['std'])} "
            f"| {pct(s.get('auc',{}).get('mean'))} "
            f"| {n_users_str} |"
        )
    A("")
    A("부트스트랩 95% CI (유저 리샘플 1000회, current/team-final/fix-snapshot 각각):")
    A("")
    A("| 코드 버전 | MRR (CI) | Precision@5 (CI) | nDCG@5 (CI) | Coverage@5 (CI) |")
    A("|---|---|---|---|---|")
    for version, ci in headline_ci.items():
        A(
            f"| {version} "
            f"| {pct(ci['mrr']['mean'])} [{pct(ci['mrr']['ci_lo'])}, {pct(ci['mrr']['ci_hi'])}] "
            f"| {pct(ci['precision@5']['mean'])} [{pct(ci['precision@5']['ci_lo'])}, {pct(ci['precision@5']['ci_hi'])}] "
            f"| {pct(ci['ndcg@5']['mean'])} [{pct(ci['ndcg@5']['ci_lo'])}, {pct(ci['ndcg@5']['ci_hi'])}] "
            f"| {pct(ci['coverage@5']['mean'])} [{pct(ci['coverage@5']['ci_lo'])}, {pct(ci['coverage@5']['ci_hi'])}] |"
        )
    A("")
    A("### 1-2. generator_split 프로토콜 (`ctr_logs_train.csv` / `ctr_logs_valid.csv`, 생성기 자체 랜덤 분할)")
    A("")
    A("| 코드 버전 | 시드 수 | MRR | Precision@5 | nDCG@5 | nDCG@10 | Coverage@5 | AUC |")
    A("|---|---|---|---|---|---|---|---|")
    for version, d in gen.items():
        s = d["summary"]
        A(
            f"| {version} | {s.get('n_seeds','-')} "
            f"| {pct(s['mrr']['mean'])} ± {pct(s['mrr']['std'])} "
            f"| {pct(s['precision@5']['mean'])} ± {pct(s['precision@5']['std'])} "
            f"| {pct(s['ndcg@5']['mean'])} ± {pct(s['ndcg@5']['std'])} "
            f"| {pct(s.get('ndcg@10',{}).get('mean'))} ± {pct(s.get('ndcg@10',{}).get('std'))} "
            f"| {pct(s['coverage@5']['mean'])} ± {pct(s['coverage@5']['std'])} "
            f"| {pct(s.get('auc',{}).get('mean'))} |"
        )
    A("")

    A("## 2. 베이스라인 비교 (동일 후보 195건, 동일 정답 창)")
    A("")
    for proto in ["team_split", "generator_split"]:
        A(f"### {proto}")
        A("")
        A("| 방법 | MRR | Precision@5 | nDCG@5 | Coverage@5 |")
        A("|---|---|---|---|---|")
        for name, m in baselines[proto].items():
            A(
                f"| {name} | {pct(m.get('mrr'))} | {pct(m.get('precision@5'))} "
                f"| {pct(m.get('ndcg@5'))} | {pct(m.get('coverage@5'))} |"
            )
        # current 모델 요약도 같은 표에 병기
        model_key = "current"
        model_summary = (headline[model_key]["summary"] if proto == "team_split" else gen[model_key]["summary"])
        A(
            f"| **current 모델 (LightGBM+MMR)** | {pct(model_summary['mrr']['mean'])} "
            f"| {pct(model_summary['precision@5']['mean'])} | {pct(model_summary['ndcg@5']['mean'])} "
            f"| {pct(model_summary['coverage@5']['mean'])} |"
        )
        A("")

    A("## 3. 분해 실험 (모두 `current` 코드 위에서 단일 변수만 바꿈)")
    A("")
    A("| 실험 | 조건 A | 조건 B | 효과 (B-A, MRR) | 95% CI | 효과 (B-A, P@5) | 95% CI | 효과 (B-A, nDCG@5) | 95% CI | 효과 (B-A, Coverage@5) | 95% CI |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    label_map = {
        "leakage": ("fixed (point-in-time)", "leaky (팀 버그 재현)"),
        "negatives": ("random negative (팀 방식)", "impression negative (is_clicked==0)"),
        "label_assumption": ("clicks_only", "all_rows (모든 행=클릭)"),
        "objective": ("lambdarank (FIX #5)", "binary override (팀 방식)"),
    }
    for name, (a_label, b_label) in label_map.items():
        b = boot[name]
        A(
            f"| {name} | {a_label} | {b_label} "
            f"| {fmt_effect(b['mrr']).split(' (95%')[0]} | 95% {fmt_effect(b['mrr']).split('95% ')[1]} "
            f"| {fmt_effect(b['precision@5']).split(' (95%')[0]} | 95% {fmt_effect(b['precision@5']).split('95% ')[1]} "
            f"| {fmt_effect(b['ndcg@5']).split(' (95%')[0]} | 95% {fmt_effect(b['ndcg@5']).split('95% ')[1]} "
            f"| {fmt_effect(b['coverage@5']).split(' (95%')[0]} | 95% {fmt_effect(b['coverage@5']).split('95% ')[1]} |"
        )
    A("")
    A("### 3-1. 후보 풀 크기 (full_195 기준 대비)")
    A("")
    cp = boot["candidate_pool"]
    A("| 비교 | MRR 효과 | Precision@5 효과 | nDCG@5 효과 | Coverage@5 효과 |")
    A("|---|---|---|---|---|")
    A(
        f"| full_195 → small_recent_15 | {fmt_effect(cp['full_vs_small_mrr'])} "
        f"| {fmt_effect(cp['full_vs_small_precision@5'])} | {fmt_effect(cp['full_vs_small_ndcg@5'])} "
        f"| {fmt_effect(cp['full_vs_small_coverage@5'])} |"
    )
    A(
        f"| full_195 → padded_400 | {fmt_effect(cp['full_vs_padded_mrr'])} "
        f"| {fmt_effect(cp['full_vs_padded_precision@5'])} | {fmt_effect(cp['full_vs_padded_ndcg@5'])} "
        f"| {fmt_effect(cp['full_vs_padded_coverage@5'])} |"
    )
    A("")

    A("각 조건의 원본 평균(시드 평균)은 `team_repro_v1.json`의 `decomposition_summary`에 있다.")
    A("")

    A("## 4. 합성 데이터가 뒷받침할 수 있는 것 / 없는 것")
    A("")
    A(
        "- 100명의 유저는 실제 사람이 아니라 (연령대 x Scanner/Regular/Deep 성향 x 성별) "
        "페르소나를 HyperCLOVA/LLM에 준 뒤 그 페르소나가 '클릭했을 법한' 뉴스레터를 "
        "LLM이 추론해 만든 로그다. 클릭 라벨 자체가 category_match 같은 명시적 카테고리 "
        "신호와 같은 LLM 추론 과정에서 나왔을 가능성이 있어, category_match_count/"
        "is_category_match 피처와 클릭 라벨 사이에 순환성(circularity)이 있을 수 있다 - "
        "모델이 '카테고리가 맞으면 클릭했다'는, 데이터 생성 규칙 자체를 학습하는 것일 "
        "수 있다는 뜻이다. 이 문서의 모든 절대 수치(MRR/P@5 등)는 이 순환성 위에 "
        "있을 수 있으므로 실제 서비스 성능의 추정치가 아니라 '팀 파이프라인이 논리적으로 "
        "일관되게 도는가'를 보는 화이트박스 진단으로 읽어야 한다."
    )
    A(
        "- 반대로 이 데이터가 뒷받침하는 것: 코드 결함(시간 유출, 그룹 정의, 후보 풀 "
        "point-in-time 여부)이 '같은 데이터'에서 지표를 어느 방향/규모로 움직이는지의 "
        "**상대적 효과**는 위 순환성과 무관하게 유효하다 - 순환성이 있어도 leaky와 fixed "
        "둘 다 같은 순환적 라벨을 보고 학습하므로, 그 차이(3절의 효과)는 '유출이 있고 "
        "없고'의 순수한 차이를 반영한다."
    )
    A(
        "- 클릭 로그 구간이 실제로는 약 6시간 16분(2026-01-30 19:26 ~ 2026-01-31 01:42)"
        "뿐이라, 뉴스 신선도(half-life 7일) 피처가 사실상 거의 변별력이 없고(전체 뉴스레터가 "
        "그 구간 전에 이미 발행), 시간 기반 검증 분할도 몇 시간 단위로 쪼개진다 - "
        "팀이 스스로 의심한 '짧은 윈도우'가 이 아카이브에서 문자 그대로 확인된다."
    )
    A("")

    A("## 5. 다음 단계에 대한 시사점")
    A("")
    A(
        "- **공개 벤치마크(EB-NeRD 등)로 교차검증**: 순환성 문제(위 4절)를 피하려면, "
        "클릭이 실제 사람에게서 나온 공개 뉴스 추천 데이터셋(예: EB-NeRD)에서 같은 "
        "파이프라인(FeatureEngineer/LGBMRanker/MMR)을 돌려, 이 리포트의 상대적 효과 "
        "(leakage/negative/objective)가 재현되는지 확인해야 실서비스 성능을 주장할 수 있다."
    )
    A(
        "- **노출(impression) 로깅**: is_clicked==0인 '보여줬지만 안 클릭' 신호를 DB 스키마 "
        "자체에 남기지 않으면(현재 `user_newsletter_ctr_log`에 `is_clicked` 컬럼이 없다), "
        "negative가 전부 랜덤/미노출 샘플링에 의존하게 되고 3-(negatives) 효과 같은 "
        "'진짜 유저가 무시한 아이템' 신호를 프로덕션에서는 아예 쓸 수 없다."
    )
    A(
        "- **요청 시점(request-time) 추천**: 현재 구조는 배치(`news_letter_today_batch`)로 "
        "하루치를 미리 계산해 저장하는 방식이라, `eval_timestamp`가 사실상 '배치 실행 시점' "
        "하나로 고정된다. 실제 서비스처럼 요청이 올 때마다 그 순간의 point-in-time 피처로 "
        "추론하려면 지금의 `create_inference_dataset` Cartesian 방식(유저x전체뉴스)이 "
        "레이턴시상 감당이 안 될 수 있어(후보 풀 축소·ANN 등) 별도 설계가 필요하다."
    )
    A("")

    A("## 부록: 실행 환경")
    A("")
    A(f"- 헤드라인 시드: {meta['seeds']['headline']}")
    A(f"- 분해실험/generator_split 시드: {meta['seeds']['decomposition_secondary']}")
    A(f"- 데이터셋 유저 수: {meta['dataset_window']['n_users']}, 뉴스레터 수: {meta['dataset_window']['n_newsletters']}")
    A("")

    out_path = REPORT_DIR / "team_repro_v1.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"리포트 저장: {out_path}")


if __name__ == "__main__":
    main()
