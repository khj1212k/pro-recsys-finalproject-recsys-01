"""EB-NeRD 스키마의 합성 데이터셋 fixture (실데이터 없이 도는 테스트용, 세션에 한 번만 만든다)."""
import pytest


@pytest.fixture(scope="session")
def synth_root(tmp_path_factory):
    from evaluation.recsys.ebnerd.synthetic import SyntheticSpec, write_synthetic_dataset

    root = tmp_path_factory.mktemp("ebnerd_synth")
    write_synthetic_dataset(root, SyntheticSpec())
    return root


@pytest.fixture(scope="session")
def synth_bench(synth_root):
    from evaluation.recsys.ebnerd.prepare import load_bench

    return load_bench(synth_root / "ebnerd_synth", emb_dir=synth_root / "derived" / "ebnerd_synth")
