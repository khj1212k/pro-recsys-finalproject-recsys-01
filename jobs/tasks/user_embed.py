"""user_embed: 사용자 장기 임베딩 갱신.

1. 아직 벡터가 없는 사용자를 채운다(선호 뉴스레터 + 최근 클릭, 기존 Stage 0).
2. 최근 24시간에 클릭한 사용자의 벡터를 다시 계산한다. 1번은 벡터가 NULL인 사용자만 다루므로
   이 단계가 없으면 한 번 만들어진 장기 벡터는 클릭이 쌓여도 바뀌지 않는다. 요청 시점 추천
   (backend/app/recsys)이 이 벡터를 장기 선호로 읽는다.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

# 잡을 하루 한 번 돌리는 것을 기준으로 한 값이다(docker/crontab). 주기를 바꾸면 같이 바꾼다.
REFRESH_WINDOW_HOURS = 24


def run(ctx) -> Dict[str, Any]:
    from core.user_embedder import UserEmbedder

    embedder = UserEmbedder()
    filled = embedder.batch_update_all_users()
    since = datetime.now(timezone.utc) - timedelta(hours=REFRESH_WINDOW_HOURS)
    refreshed = embedder.refresh_recently_active_users(since)
    return {"user_embed": filled, "user_embed_refresh": refreshed}
