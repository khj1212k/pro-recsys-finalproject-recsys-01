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
