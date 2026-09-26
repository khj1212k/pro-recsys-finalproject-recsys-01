"""user_embed: 선호 카테고리 + 최근 클릭으로 사용자 임베딩 갱신 (기존 Stage 0)."""
from typing import Any, Dict


def run(ctx) -> Dict[str, Any]:
    from core.user_embedder import UserEmbedder

    return {"user_embed": UserEmbedder().batch_update_all_users()}
