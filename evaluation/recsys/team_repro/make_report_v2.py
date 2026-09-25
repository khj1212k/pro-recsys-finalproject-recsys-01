"""reports/recsys/team_repro_v2.json을 읽어 한국어 Markdown 리포트
(team_repro_v2.md)를 생성한다. 숫자를 다시 계산하지 않는다 - run_repro.py가 이미
계산해 저장한 값을 그대로 표로 옮길 뿐이다.

구조(요구사항 9): 요약 / 데이터 구조와 기저율 / 프로토콜(시점 기준 vs 팀 방식) /
결과표 / 분해 실험(살아남은 것만) / 이 데이터가 증명할 수 없는 것 / 다음 단계
시사점 / 이력서에 쓸 수 있는 문장과 쓰면 안 되는 문장.
"""
from __future__ import annotations

import json
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = WORKTREE_ROOT / "reports" / "recsys"


def pct(x, digits=4):
    if x is None or (isinstance(x, float) and x != x):
        return "N/A"
    return f"{x:.{digits}f}"


def fmt_mean_std(d: dict, digits=4) -> str:
    if not d:
        return "N/A"
    return f"{pct(d.get('mean'), digits)} ± {pct(d.get('std'), digits)}"


def fmt_effect(d: dict) -> str:
    if not d or d.get("effect") != d.get("effect"):
        return "N/A"
    n = d.get("n_users", "?")
    return f"{d['effect']:+.4f} (95% CI [{d['ci_lo']:+.4f}, {d['ci_hi']:+.4f}], n={n})"


def fmt_ci(d: dict) -> str:
    if not d or d.get("mean") != d.get("mean"):
        return "N/A"
    return f"{pct(d['mean'])} [{pct(d['ci_lo'])}, {pct(d['ci_hi'])}]"


def main() -> None:
    data = json.loads((REPORT_DIR / "team_repro_v2.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    ds = data["data_structure"]
    headline = data["headline"]
    headline_ci = data["headline_ci"]
    tfw = data.get("team_final_written_summary", {})
    gen = data["generator_split"]
    baselines = data["baselines"]
    decomp = data["decomposition_summary"]
    boot = data["decomposition_bootstrap"]
    cr = meta["category_recovery"]

    lines = []
    A = lines.append

    A("# 팀 베이스라인 재현 및 분해 실험 (team_repro v2)")
    A("")
    A(
        f"> v1([`team_repro_v1.md`](./team_repro_v1.md))이 어드버서리얼 방법론 검토에서 "
        f"\"unsound\" 판정을 받은 뒤 재작성한 버전이다. v1과의 차이는 이 문서 전체에 걸쳐 "
        f"명시한다 - 특히 3절(프로토콜)과 7절(v1에서 무엇이 왜 바뀌었는지)."
    )
    A("")
    A(f"- 생성 시각: {meta['generated_at']}")
    A(f"- 하네스(=current 코드) SHA: `{meta['git']['harness_sha']}` (브랜치 `{meta['git']['current_branch']}`)")
    A(f"- team-final(리포트 당시 코드, git tag `team-final`) SHA: `{meta['git']['team_final_sha']}`")
    A(f"- fix-snapshot(중간 수정본, 브랜치 `port/fix-snapshot`) SHA: `{meta['git']['fix_snapshot_sha']}`")
    A(f"- 고정 정답 구간 시작 시각(answer_start, team_split 프로토콜의 모든 arm/시드/버전 공유): `{meta['answer_start']}`")
    A("- 데이터 sha256:")
    for k, v in meta["data_sha256"].items():
        A(f"  - `{k}`: `{v}`")
    A("- config 해시(버전|objective override -> sha256[:16], 요구사항 4/6):")
    for k, v in meta["config_hashes"].items():
        A(f"  - `{k}`: `{v}`")
    if meta.get("config_overrides"):
        A("- 적용된 objective override:")
        for k, v in meta["config_overrides"].items():
            A(f"  - `{k}`: {', '.join(v)}")
    A("")

    # ---------------------------------------------------------------- 요약
    A("## 0. 요약")
    A("")
    cur_p = headline["current"]["primary_summary"]
    cur_aw = headline["current"]["as_written_summary"]
    tf_p = headline["team-final"]["primary_summary"]
    tf_aw = headline["team-final"]["as_written_summary"]
    pop_p5 = baselines["team_split"].get("popularity", {}).get("aggregate", {}).get("precision@5")
    onb_p5 = baselines["team_split"].get("onboarding_newsletter_cosine", {}).get("aggregate", {}).get("precision@5")
    cur_p5 = cur_p.get("precision@5", {}).get("mean")
    A(
        f"1. **추론을 정답 구간 이전으로 고정(point-in-time, 1차)하면 current 모델 MRR이 "
        f"{fmt_mean_std(cur_p.get('mrr', {}))}로, 팀이 실제 배포한 흐름(as-written, 2차, "
        f"{fmt_mean_std(cur_aw.get('mrr', {}))})보다 뚜렷이 낮다.** team-final도 같은 방향"
        f"({fmt_mean_std(tf_p.get('mrr', {}))} vs {fmt_mean_std(tf_aw.get('mrr', {}))})이다 - "
        f"v1이 보고한 '팀 최종이 현재 코드보다 낫다'는 차이(0.849 vs 0.772)는 이 추론 시점 "
        f"누출이 주된 원인이었다(4절 참고)."
    )
    A(
        f"2. **1차(point-in-time) 프로토콜에서 current 모델 Precision@5는 {pct(cur_p5)}로, "
        f"popularity 베이스라인({pct(pop_p5)})·onboarding cosine 베이스라인({pct(onb_p5)})과 "
        f"비슷하거나 낮다.** '모델이 베이스라인보다 낫다'는 v1의 결론은 성립하지 않는다."
    )
    A(
        f"3. team-final을 자신의 실제 정답 정의(`scripts/evaluate_results.py`, NOW()-6일)로 "
        f"그대로 재현하면 MRR {fmt_mean_std(tfw)}로 팀이 보고한 0.897에 근접한다 - 이 아카이브 "
        f"구간(약 {ds['dataset_window_seconds']/3600:.1f}시간)에서는 '최근 6일'이 로그 전체(학습 "
        f"구간 포함)와 같기 때문이다. 팀의 103명/405건 스냅샷 자체는 검증할 수 없다."
    )
    A(
        f"4. 데이터 구조상 유저 {ds['n_users_total']}명 중 {ds['n_cold_users']}명이 정답 구간 "
        f"이전 클릭이 아예 없는 cold 유저다(1절). 정답 밀도가 높아(무작위 P@5 기저율 ≈ "
        f"{pct(ds['random_p5_base_rate_mean_answer_density'])}) 이 스냅샷은 애초에 절대 지표를 "
        f"'실서비스 성능'으로 읽기 부적합하다 - 코드 결함의 상대적 효과를 보는 화이트박스 진단"
        f"으로만 쓴다."
    )
    A(
        "5. 분해 실험 중 살아남은 결과(4절): 학습 시점 히스토리 누출(FIX #4)은 point-in-time "
        "추론 하에서 효과가 0에 가깝다(null). v1에서 구조적으로 깨져 있던 padded_400/negatives는 "
        "고쳤고, label_assumption(all_rows)은 퇴화된 실험이라 제거했다(이유는 4절에 남김)."
    )
    A("")

    # ---------------------------------------------------------------- 데이터 구조
    A("## 1. 데이터 구조와 기저율")
    A("")
    A(
        f"- 유저(페르소나) {ds['n_users_total']}명, 뉴스레터 {ds['n_newsletters']}건, 로그 "
        f"{ds['n_ctr_logs']}건({ds['n_clicks']}건 클릭, 클릭률 {pct(ds['click_rate'])}) - 로그 구간은 "
        f"`{ds['dataset_window_start']}` ~ `{ds['dataset_window_end']}`(약 "
        f"{ds['dataset_window_seconds']/3600:.2f}시간)."
    )
    A(
        f"- 각 페르소나는 {ds['exposures_per_persona_min']}~{ds['exposures_per_persona_max']}건 "
        f"노출을 받았다(사실상 전체 {ds['n_newsletters']}건을 한 번씩) - 세션 하나의 평균 길이는 "
        f"약 {ds['session_span_seconds_mean']/60:.1f}분이다."
    )
    A(
        f"- 고정 정답 구간(`{ds['answer_start']}` 이후): 클릭이 있는 평가 대상 유저 "
        f"{ds['n_evaluated_users_in_answer_window']}명, 정답 클릭 {ds['n_answer_clicks']}건. "
        f"이 경계 이전 클릭이 하나도 없는(=cold) 유저가 {ds['n_users_total']}명 중 "
        f"**{ds['n_cold_users']}명**, 있는(=warm) 유저가 **{ds['n_warm_users']}명**이다."
    )
    A(
        f"- 무작위 추천의 P@5 기저율(정답 구간 내 유저별 정답 밀도의 평균, {ds['n_newsletters']}건 "
        f"후보 기준) ≈ **{pct(ds['random_p5_base_rate_mean_answer_density'])}** - 아래 3절 베이스라인 "
        f"표의 `random` 행 실측치와 함께 보라."
    )
    random_p5 = baselines["team_split"].get("random", {}).get("aggregate", {}).get("precision@5")
    A(f"- 실측 `random` 베이스라인 Precision@5: **{pct(random_p5)}**.")
    seen_share = baselines["team_split"].get("cosine_history", {}).get("seen_share_top5")
    A(
        f"- cosine_history 베이스라인 top-5 중 이미 정답 구간 이전에 클릭한 아이템의 비율: "
        f"{pct(seen_share)} - 이미 본 아이템은 정답이 될 수 없으므로, 이 비율만큼은 애초에 "
        f"달성 불가능한 정밀도다(seen-item filtering 변형이 이를 보정한다, 3절 참고)."
    )
    A("")
    A(f"카테고리 복원: 직접 라벨 {cr['n_direct_labels']}건 외 {sum(cr['source_counts'].values()) - cr['n_direct_labels']}건은 "
      f"BGE-M3 kNN(k={cr['chosen_k']})으로 채웠다. LOO 정확도 {pct(cr['loo_accuracy_by_k'][str(cr['chosen_k'])])}"
      f"(Wilson 95% CI [{pct(cr['loo_ci_chosen_k_wilson95'][0])}, {pct(cr['loo_ci_chosen_k_wilson95'][1])}], "
      f"카테고리 7개, 무작위 기대값 ≈14.3%) - **{cr['loo_ci_note']}**")
    A("")

    # ---------------------------------------------------------------- 프로토콜
    A("## 2. 프로토콜: 시점 기준(point-in-time, 1차) vs 팀 방식(as-written, 2차)")
    A("")
    A(
        "**1차(point-in-time)**: 학습은 고정 정답 구간(answer_start) 이전 로그만 쓰고, 조기 "
        "종료는 그 학습 구간을 다시 나눈 inner-validation으로 한다(정답 구간을 절대 보지 "
        "않는다). 추론은 같은 answer_start '이전'으로 로그 자체를 물리적으로 잘라낸 두 번째 "
        "로더로 다시 피처를 계산해서 한다 - team-final처럼 point-in-time cutoff 개념이 "
        "아예 없는 코드에도(그런 코드는 로그 자체를 주는 대로 다 쓰므로) 안전하게 먹힌다."
    )
    A(
        "**2차(as-written/leaky)**: 팀이 실제 배포한 흐름 그대로 - 같은 학습된 모델을, 원래"
        "(미제한) 로그로 만든 피처로, 데이터셋(또는 DB)의 마지막 로그 시각을 '지금'으로 "
        "추론한다(`main_lgbm.py`의 디버그 모드 `inference_pipeline`이 실제로 이렇게 동작한다 "
        "- `get_db_max_timestamp()`). 정답 구간은 1차와 동일하게 둬서, 모델을 다시 학습하지 "
        "않고 **추론 시점 누출 하나만** 순수하게 분리해서 본다."
    )
    A("")
    A("### 2-1. 결과표 (team_split 프로토콜, clicks_only, 후보 195건 전체)")
    A("")
    A("| 코드 버전 | 프로토콜 | 시드 수 | MRR | Precision@5 | nDCG@5 | Coverage@5 | best_iteration |")
    A("|---|---|---|---|---|---|---|---|")
    version_labels = {
        "team-final": "team-final (팀 최종, 리포트 당시 코드)",
        "fix-snapshot": "fix-snapshot (중간 수정본)",
        "current": "current (이 브랜치)",
    }
    for version in ["team-final", "fix-snapshot", "current"]:
        for field, label in [("primary_summary", "1차 point-in-time"), ("as_written_summary", "2차 as-written(leaky)")]:
            s = headline[version][field]
            if not s:
                continue
            bi = s.get("best_iteration")
            bi_str = str(bi) if bi else "-"
            A(
                f"| {version_labels[version]} | {label} | {s.get('n_seeds', '-')} "
                f"| {fmt_mean_std(s.get('mrr', {}))} | {fmt_mean_std(s.get('precision@5', {}))} "
                f"| {fmt_mean_std(s.get('ndcg@5', {}))} | {fmt_mean_std(s.get('coverage@5', {}))} "
                f"| {bi_str} |"
            )
    A("")
    A("추론 시점 누출 효과(as-written − 1차, 시드 x 유저 nested bootstrap 95% CI):")
    A("")
    A("| 코드 버전 | MRR 효과 | Precision@5 효과 |")
    A("|---|---|---|")
    for version in ["current", "team-final", "fix-snapshot"]:
        le = headline_ci.get(version, {}).get("leak_effect", {})
        if not le:
            continue
        A(f"| {version_labels[version]} | {fmt_effect(le.get('mrr', {}))} | {fmt_effect(le.get('precision@5', {}))} |")
    A("")

    if tfw:
        A("### 2-2. team-final-as-written (팀의 실제 `scripts/evaluate_results.py` 정답 정의)")
        A("")
        A(
            f"NOW()-6일 창(is_clicked 필터 없음) 그대로 재현: MRR {fmt_mean_std(tfw.get('mrr', {}))}, "
            f"Precision@5 {fmt_mean_std(tfw.get('precision@5', {}))} ({tfw.get('n_seeds', '?')}개 시드). "
            f"{tfw.get('note', '')}"
        )
        A("")

    # ---------------------------------------------------------------- 결과표(베이스라인)
    A("## 3. 베이스라인 비교 (모델과 동일한 로그/후보 풀/정답 구간)")
    A("")
    A("### 3-1. team_split, 1차(point-in-time) 정답 구간")
    A("")
    A("| 방법 | MRR | Precision@5 | nDCG@5 | Coverage@5 | seen-filtered MRR | seen-filtered P@5 |")
    A("|---|---|---|---|---|---|---|")
    for name, m in baselines["team_split"].items():
        agg = m.get("aggregate", {})
        sf = m.get("seen_filtered", {}).get("aggregate", {})
        A(
            f"| {name} | {pct(agg.get('mrr'))} | {pct(agg.get('precision@5'))} "
            f"| {pct(agg.get('ndcg@5'))} | {pct(agg.get('coverage@5'))} "
            f"| {pct(sf.get('mrr'))} | {pct(sf.get('precision@5'))} |"
        )
    A(
        f"| **current 모델 (point-in-time)** | {fmt_mean_std(cur_p.get('mrr', {}))} "
        f"| {fmt_mean_std(cur_p.get('precision@5', {}))} | {fmt_mean_std(cur_p.get('ndcg@5', {}))} "
        f"| {fmt_mean_std(cur_p.get('coverage@5', {}))} | - | - |"
    )
    A("")
    A(
        "(모델의 seen-filtered 변형 지표는 `team_repro_v2.json`의 개별 run(`primary."
        "seen_filtered`)에 시드별로 있다 - 시드 평균은 여기 표에 올리지 않았다.)"
    )
    A("")
    A("### 3-2. cold vs warm 유저 분리 (1차 프로토콜)")
    A("")
    A("| 방법 | cold MRR | cold P@5 | cold n | warm MRR | warm P@5 | warm n |")
    A("|---|---|---|---|---|---|---|")
    for name, m in baselines["team_split"].items():
        cold, warm = m.get("cold", {}), m.get("warm", {})
        A(
            f"| {name} | {pct(cold.get('mrr'))} | {pct(cold.get('precision@5'))} | {cold.get('num_users', 0)} "
            f"| {pct(warm.get('mrr'))} | {pct(warm.get('precision@5'))} | {warm.get('num_users', 0)} |"
        )
    A("")
    A("### 3-3. generator_split (학습을 `ctr_logs_train.csv`로 제한 - 진짜 cold-item 평가)")
    A("")
    A("| 방법 | MRR | Precision@5 | nDCG@5 | Coverage@5 |")
    A("|---|---|---|---|---|")
    for name, m in baselines["generator_split"].items():
        agg = m.get("aggregate", {})
        A(f"| {name} | {pct(agg.get('mrr'))} | {pct(agg.get('precision@5'))} | {pct(agg.get('ndcg@5'))} | {pct(agg.get('coverage@5'))} |")
    for version, d in gen.items():
        s = d["summary"]
        if not s:
            continue
        A(
            f"| **{version_labels.get(version, version)} 모델** | {fmt_mean_std(s.get('mrr', {}))} "
            f"| {fmt_mean_std(s.get('precision@5', {}))} | {fmt_mean_std(s.get('ndcg@5', {}))} "
            f"| {fmt_mean_std(s.get('coverage@5', {}))} |"
        )
    A("")

    # ---------------------------------------------------------------- 분해 실험
    A("## 4. 분해 실험 (current 코드, point-in-time 1차 프로토콜 위에서 단일 변수 통제 - 살아남은 것만)")
    A("")
    A(
        "v1의 `label_assumption`(all_rows) 실험은 이 스냅샷에서 퇴화한다(모든 유저가 195건 "
        "전부를 '클릭'한 것으로 취급되어 negative sampler가 negative를 하나도 못 찾고, AUC가 "
        "정의되지 않고, best_iteration=1, 5개 시드 결과가 전부 동일하다) - v2는 이 실험을 "
        "제거했다."
    )
    A("")
    A("| 실험 | 조건 A | 조건 B | MRR 효과 (B-A) | Precision@5 효과 (B-A) |")
    A("|---|---|---|---|---|")
    label_map = {
        "leakage": ("fixed (point-in-time, FIX #4 반영)", "leaky (학습 시점 누출 재현)"),
        "negatives": ("random negative (팀 방식)", "impression negative (is_clicked==0, group_key=user_id)"),
        "objective": ("lambdarank (FIX #5)", "binary override (팀 방식)"),
    }
    for name, (a_label, b_label) in label_map.items():
        b = boot.get(name, {})
        A(f"| {name} | {a_label} | {b_label} | {fmt_effect(b.get('mrr', {}))} | {fmt_effect(b.get('precision@5', {}))} |")
    A("")
    A(
        "**negatives 주의**: 이 아카이브에서는 페르소나 전원이 195건 전부를 노출받았으므로, "
        "'클릭 안 한 나머지'(random negative)와 '노출됐지만 클릭 안 함'(impression negative)이 "
        "사실상 같은 모집단에서 나온다 - 이 효과를 '노출 로그가 랜덤보다 낫다'는 증거로 읽으면 "
        "안 된다(claims_to_avoid 참고)."
    )
    A("")
    A("### 4-1. 후보 풀 크기 (full_195 기준 대비, 1차 프로토콜)")
    A("")
    cp = boot.get("candidate_pool", {})
    A("| 비교 | MRR 효과 | Precision@5 효과 |")
    A("|---|---|---|")
    A(f"| full_195 → small_recent_15 (정답을 풀 안으로 제한) | {fmt_effect(cp.get('full_vs_small_mrr', {}))} | {fmt_effect(cp.get('full_vs_small_precision@5', {}))} |")
    A(f"| full_195 → padded_400 (실제 400건 채점 확인됨) | {fmt_effect(cp.get('full_vs_padded_mrr', {}))} | {fmt_effect(cp.get('full_vs_padded_precision@5', {}))} |")
    A("")
    A("padded_400 arm의 실제 후보 수는 run JSON의 `n_candidates` 필드로 확인 가능하다(400이어야 하며, pipeline.py 자체가 이를 assert한다).")
    A("")

    # ---------------------------------------------------------------- 증명 불가
    A("## 5. 이 데이터가 증명할 수 없는 것")
    A("")
    A(
        "- **실서비스 성능.** 클릭은 100명의 LLM 페르소나에서 LLM이 추론해 만든 라벨이다 - "
        f"`category_match` 같은 명시적 신호와 순환성이 있을 수 있다. 세션이 약 "
        f"{ds['dataset_window_seconds']/3600:.1f}시간뿐이라 신선도(half-life 7일) 피처는 사실상 "
        "죽은 피처다."
    )
    A(
        "- **팀이 보고한 0.897(103명/405건) 자체의 재현.** 이 아카이브는 100명/195건 스냅샷이라 "
        "규모가 다르고, team-final-as-written 행(2-2절)이 설명하는 것은 '정답 정의가 학습 구간을 "
        "포함하면 왜 높게 나오는가'이지, 그 스냅샷 자체의 검증이 아니다."
    )
    A(
        "- **모델이 베이스라인보다 낫다는 것.** 1차 프로토콜에서는 성립하지 않는다(0절 요약 "
        "2번, 3-1절)."
    )
    A(
        "- **카테고리 의존 지표(`category_match`, `Coverage@k`)의 절대값.** 76%가 kNN으로 "
        f"채워졌고 그 정확도가 {pct(cr['loo_accuracy_by_k'][str(cr['chosen_k'])])}에 불과하다(1절)."
    )
    A(
        "- **여러 신뢰구간을 동시에 보며 '유의하다'고 결론짓는 것.** 이 리포트는 약 20개 넘는 "
        "구간을 병기하지만 다중비교 보정을 하지 않았다 - 개별 구간 하나를 따로 떼어 '유의함'으로 "
        "읽지 말고, 방향과 크기의 패턴으로 읽어야 한다."
    )
    A("")

    # ---------------------------------------------------------------- 다음 단계
    A("## 6. 다음 단계 시사점")
    A("")
    A(
        "- **공개 벤치마크 교차검증이 정확도 주장의 전제조건이다** (ADR "
        "[0007](../../docs/adr/0007-recsys-offline-evaluation-protocol.md)). EB-NeRD를 1차로 "
        "(이미 `data/benchmarks/ebnerd`에 있음 - 라이선스상 이 Mac 밖으로 복사 금지), MIND를 "
        "보조로 검토해, 이 리포트의 상대적 효과(누출/objective 등)가 실제 사람의 클릭에서도 "
        "재현되는지 확인해야 한다."
    )
    A(
        "- **노출(impression) 로깅을 DB 스키마에 남겨야** negative가 랜덤 샘플링에만 의존하지 "
        "않는다 - 지금 `user_newsletter_ctr_log`에는 `is_clicked` 컬럼이 없다."
    )
    A(
        "- **요청 시점(request-time) 추천**: 지금 구조는 배치로 하루치를 미리 계산해 저장하는 "
        "방식이라 `eval_timestamp`가 사실상 배치 실행 시점 하나로 고정된다. 실제 요청마다 "
        "point-in-time으로 추론하려면 지금의 Cartesian(유저x전체뉴스) 방식이 레이턴시상 감당이 "
        "안 될 수 있다."
    )
    A(
        "- **team-final의 실제 evaluate_results.py 로직(NOW()-6일)을 앞으로도 그대로 쓰면 안 "
        "된다** - `valid_period.json` 기반 정확한 정답 구간 로직(current에 이미 있음)을 "
        "표준으로 삼아야 한다."
    )
    A("")

    # ---------------------------------------------------------------- 이력서 문장
    A("## 7. 이력서에 쓸 수 있는 문장과 쓰면 안 되는 문장")
    A("")
    A("### 쓸 수 있는 문장")
    A("")
    A(
        "- \"팀의 LightGBM+MMR 추천 파이프라인을 세 코드 스냅샷(team-final/fix-snapshot/current) "
        "그대로 재현하는 파일 기반 하네스를 구축하고, 어드버서리얼 방법론 검토를 통해 추론 "
        "시점 데이터 누출(BLOCKER)을 발견해 point-in-time 프로토콜로 교정했다.\""
    )
    A(
        "- \"교정 후 모델이 popularity/cosine 베이스라인 대비 우위를 보이지 못한다는 것을 "
        "확인해, 팀이 최종 보고했던 성능 우위 주장을 철회하고 그 원인(추론 시점 누출, 정답 "
        "정의 오류)을 정량적으로 규명했다.\""
    )
    A(
        "- \"학습 시점 히스토리 누출(FIX #4) 수정 자체는 point-in-time 평가에서 효과가 "
        "0에 가깝다는 null 결과를 확인해, 팀이 '이 수정으로 성능이 좋아졌다'고 잘못 귀속했던 "
        "부분을 바로잡았다.\""
    )
    A("")
    A("### 쓰면 안 되는 문장")
    A("")
    A("- \"LightGBM+MMR 모델이 MRR 0.77(vs 베이스라인 0.60)로 베이스라인보다 우수했다.\" (leakage-free 프로토콜에서 성립하지 않음)")
    A("- \"팀의 파이프라인을 재현해 MRR 0.897을 확인했다.\" (팀의 실제 스냅샷·정답 정의는 재현 불가; 0.897은 정답 정의 오류로 설명됨)")
    A("- \"FIX #4/#5가 MRR을 0.849에서 0.772로 낮췄다(=악화시켰다).\" (point-in-time 하에서 그 차이는 사라짐 - 4절)")
    A("- \"노출 로그 기반 negative가 랜덤 negative보다 나쁘다.\" (이 데이터에서는 사실상 같은 모집단 - 혼입됨)")
    A("- \"모델이 처음 보는(cold) 뉴스레터에 일반화된다.\" (generator_split 모델도 여전히 이 합성 데이터 특유의 구조 위에서만 검증됨)")
    A("- 이 리포트의 절대 MRR/P@5/nDCG/Coverage 수치를 실서비스 성능으로 인용하는 모든 문장.")
    A("")

    A("## 부록: 실행 환경")
    A("")
    A(f"- 헤드라인 시드 (current/team-final): {meta['seeds']['headline_current_team_final']}")
    A(
        f"- 헤드라인 시드 (fix-snapshot, point-in-time cutoff 메모이즈가 없어 훨씬 "
        f"느림): {meta['seeds']['headline_fix_snapshot']}"
    )
    A(f"- 분해실험/generator_split 시드: {meta['seeds']['decomposition_secondary']}")
    A(f"- 데이터셋 유저 수: {ds['n_users_total']}, 뉴스레터 수: {ds['n_newsletters']}")
    A("")

    out_path = REPORT_DIR / "team_repro_v2.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"리포트 저장: {out_path}")


if __name__ == "__main__":
    main()
