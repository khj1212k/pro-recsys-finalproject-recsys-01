# core/llm - 프로바이더 독립적인 LLM 클라이언트 계층 (ADR 0005)
#
# 호출부는 보통 아래 두 가지만 있으면 된다:
#   from core.llm import get_client
#   from core.llm.schemas import ClusterEval, NewsletterMeta, ...
#
#   client = get_client("judge")
#   result = client.complete(messages, schema=ClusterEval, purpose="cluster_eval")

from core.llm.client import LLMClient, LLMResult, LLMUsage
from core.llm.registry import get_client, resolve_role_config

__all__ = [
    "LLMClient",
    "LLMResult",
    "LLMUsage",
    "get_client",
    "resolve_role_config",
]
