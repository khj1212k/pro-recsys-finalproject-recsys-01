"""run_ebnerd 전 구간 배선 스모크(ebnerd_demo + 가짜 임베딩, 수치 해석 없음). demo 데이터가 없으면(CI) skip."""
import json

import pytest

from evaluation.recsys.ebnerd.loaders import ebnerd_root

DEMO = ebnerd_root() / "ebnerd_demo"
pytestmark = pytest.mark.skipif(not (DEMO / "articles.parquet").exists(),
                                reason="EB-NeRD demo 데이터 없음 (로컬 전용)")


def test_poolneg_chain_writes_adjacent_diffs_and_paired_mmr(tmp_path):
    from evaluation.recsys.ebnerd.models import POOLNEG_ABLATION
    from evaluation.recsys.ebnerd.run_ebnerd import main

    out = tmp_path / "r.json"
    main(["--dataset", "ebnerd_demo", "--root", str(ebnerd_root()), "--fake-dim", "8", "--chain", "poolneg",
          "--seeds", "0", "--max-fit", "1500", "--max-test", "1500", "--n-boot", "20", "--p2-sample", "80",
          "--p2-select-sample", "60", "--mmr-sample", "20", "--mmr-lambdas", "0.5", "1.0", "--threads", "2",
          "--skip", "replay", "--out-json", str(out)])
    d = json.loads(out.read_text())
    chain = [s.name for s in POOLNEG_ABLATION]
    assert set(chain) <= set(d["p1"]) and set(chain) <= set(d["p2"])
    want = {f"{b}-vs-{a}" for a, b in zip(chain, chain[1:])}
    assert set(d["p1_ablation_diffs"]) == want and set(d["p2_ablation_diffs"]) == want
    assert d["protocol"]["chain"] == "poolneg"
    paired = d["p2_mmr"]["paired_vs_lambda_1.0"]
    assert set(paired) == {"0.5"} and {"diff", "ci95", "n"} <= set(paired["0.5"]["ndcg@10"])


def _small_args(out, *extra):
    return ["--dataset", "ebnerd_demo", "--root", str(ebnerd_root()), "--fake-dim", "8", "--seeds", "0",
            "--max-fit", "1500", "--max-test", "1500", "--n-boot", "20", "--p2-sample", "80",
            "--p2-select-sample", "60", "--mmr-sample", "20", "--mmr-lambdas", "0.5", "1.0", "--threads", "2",
            "--out-json", str(out), *extra]


def test_default_inview_chain_with_replay_and_promotion_verdict(tmp_path):
    """v1 본 실행 경로(--chain inview, --skip 없음): 사슬 전체, 실시간 재생(일 배치 학습 모델 포함),
    2단계 쌍체 차이, 승격 판정과 리포트 표 생성까지 한 번에 지나가는지."""
    from evaluation.recsys.ebnerd.make_report import promotion_verdict, render
    from evaluation.recsys.ebnerd.models import ABLATION, NEGATIVE_VARIANTS
    from evaluation.recsys.ebnerd.run_ebnerd import main

    out = tmp_path / "r.json"
    main(_small_args(out, "--replay-max", "200"))
    d = json.loads(out.read_text())

    chain = [s.name for s in ABLATION]
    assert d["protocol"]["chain"] == "inview"
    assert set(chain + [s.name for s in NEGATIVE_VARIANTS]) <= set(d["p1"])
    assert set(d["p1_ablation_diffs"]) == {f"{b}-vs-{a}" for a, b in zip(chain, chain[1:])}

    rp = d["replay"]
    assert 0 < rp["n_impressions"] <= 200 and rp["n_impressions"] <= rp["n_eligible"]
    assert {"ranker_v2|A_daily_batch", "ranker_v2|B_up_to_t", "ranker_v2|C_history_only",
            "ranker_v2_daily_trained|A_daily_batch", "cosine_history|B_up_to_t"} <= set(rp["results"])
    key = "ranker_v2 realtime(B-trained,B-served) - daily(A-trained,A-served)"
    assert {"diff", "ci95", "n"} <= set(rp["diffs"][key]["ndcg@10"])
    assert len(rp["daily_trained_models"]) == 1

    two = d["p2_two_stage"]
    assert set(two) == {"union@50", "union@100", "union@200"}
    assert {"diff", "ci95", "n"} <= set(two["union@50"]["paired_vs_full_pool"]["ndcg@10"])

    v = promotion_verdict(d)  # 통과/실패와 무관하게 비교 항목이 모두 채워져야 한다
    assert set(v["rules"]) == {"R1", "R2", "R3", "R4"} and isinstance(v["passed"], bool)
    assert all(r.get("diff") is not None for k, r in v["rules"].items() if k != "R4")
    assert "승격 규칙 판정" in render(d)


def test_only_models_rerun_command_path(tmp_path):
    """리포트 5.4절 재실행 대기 명령과 같은 경로: 선택 모델만 학습해 2단계 쌍체 차이를 채운다."""
    from evaluation.recsys.ebnerd.make_report import render
    from evaluation.recsys.ebnerd.run_ebnerd import main

    out = tmp_path / "r.json"
    main(_small_args(out, "--only-models", "ranker_v2_poolneg", "--skip", "replay"))
    d = json.loads(out.read_text())

    assert set(d["p1_models"]) == {"ranker_v2_poolneg"}
    assert d["p2_selection"]["chosen"] == "ranker_v2_poolneg"
    assert "paired_vs_full_pool" in d["p2_two_stage"]["union@50"]
    assert "부분 실행이라 판정하지 않음" in render(d)
