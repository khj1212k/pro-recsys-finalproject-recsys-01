import yaml
import os


def test_create_reranker_from_config_uses_configured_mmr_lambda_values():
    from src.core.reranker import create_reranker_from_config

    config = {
        "recommendation": {
            "mmr_lambda": {
                "few_categories": 0.99,
                "medium_categories": 0.55,
                "many_categories": 0.11,
                "default": 0.42,
            },
            "mmr_pool_multiplier": 4,
        }
    }

    reranker = create_reranker_from_config(config)

    assert reranker.lambda_few == 0.99
    assert reranker.lambda_medium == 0.55
    assert reranker.lambda_many == 0.11
    assert reranker.lambda_default == 0.42


def test_config_yaml_declares_mmr_lambda_section():
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "ai_workspace", "recommend_engine", "config", "config.yaml",
    )
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    mmr_lambda = config["recommendation"]["mmr_lambda"]
    assert set(mmr_lambda.keys()) == {"few_categories", "medium_categories", "many_categories", "default"}
