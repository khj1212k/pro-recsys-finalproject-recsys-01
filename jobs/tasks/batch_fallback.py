"""batch_fallback: 오늘(KST) 개인화 추천 배치가 없는 사용자에게 인기도 랭킹 기반 목록을 채운다.

train(추론)이 실패했거나 아직 로그가 없는 신규 사용자도 /newsletters/today가 빈
목록을 받지 않게 하는 안전망이다. 오늘 이미 배치가 있는 사용자는 건드리지 않으므로
하루에 여러 번 실행해도 결과가 같다.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Set

from jobs.runtime import JobSkipped

TOP_K = 20  # backend/app/api/newsletter.py::get_today_news가 최대 20개를 내려준다
KST = timezone(timedelta(hours=9))


def build_fallback_list(
    ranked_ids: Iterable[int],
    categories_by_newsletter: Mapping[int, Set[int]],
    preferred_categories: Set[int],
    seen_ids: Set[int],
    k: int = TOP_K,
) -> List[int]:
    """인기도 순서를 유지하되 선호 카테고리 뉴스레터를 앞으로 올리고, 이미 클릭한 것은 뺀다."""
    candidates = [nid for nid in ranked_ids if nid not in seen_ids]
    if not preferred_categories:
        return candidates[:k]
    preferred = [nid for nid in candidates if categories_by_newsletter.get(nid, set()) & preferred_categories]
    preferred_set = set(preferred)
    rest = [nid for nid in candidates if nid not in preferred_set]
    return (preferred + rest)[:k]


def kst_day_start_utc(now_utc: datetime) -> datetime:
    day_start_kst = now_utc.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_kst.astimezone(timezone.utc)


def run(ctx) -> Dict[str, Any]:
    from psycopg2.extras import Json, execute_values

    from db.connection import get_db_connection

    day_start = kst_day_start_utc(datetime.now(timezone.utc))
    with get_db_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT news_letter_batch_id, news_letter_ids FROM news_letters_category "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row or not row[1]:
                    raise JobSkipped("no_popularity_ranking")
                ranking_batch_id, ranked_ids = row[0], [int(x) for x in row[1]]

                cur.execute(
                    """
                    SELECT u.user_id FROM "user" u
                    WHERE NOT EXISTS (
                        SELECT 1 FROM news_letter_today_batch b
                        WHERE b.user_id = u.user_id AND b.created_at >= %s
                    )
                    ORDER BY u.user_id
                    """,
                    (day_start,),
                )
                user_ids = [r[0] for r in cur.fetchall()]
                stats: Dict[str, Any] = {
                    "ranking_batch_id": ranking_batch_id,
                    "ranked_newsletters": len(ranked_ids),
                    "users_missing_today_batch": len(user_ids),
                    "users_filled": 0,
                }
                if not user_ids:
                    return stats

                cur.execute(
                    "SELECT news_letter_id, category_id FROM news_letter_categories WHERE news_letter_id = ANY(%s)",
                    (ranked_ids,),
                )
                categories_by_newsletter: Dict[int, Set[int]] = {}
                for nid, cid in cur.fetchall():
                    categories_by_newsletter.setdefault(nid, set()).add(cid)

                cur.execute(
                    "SELECT user_id, category_id FROM user_preferred_categories WHERE user_id = ANY(%s)",
                    (user_ids,),
                )
                preferred: Dict[int, Set[int]] = {}
                for uid, cid in cur.fetchall():
                    preferred.setdefault(uid, set()).add(cid)

                cur.execute(
                    "SELECT user_id, news_letter_id FROM user_newsletter_ctr_log WHERE user_id = ANY(%s)",
                    (user_ids,),
                )
                seen: Dict[int, Set[int]] = {}
                for uid, nid in cur.fetchall():
                    seen.setdefault(uid, set()).add(nid)

                rows = []
                for uid in user_ids:
                    ids = build_fallback_list(
                        ranked_ids, categories_by_newsletter, preferred.get(uid, set()), seen.get(uid, set())
                    )
                    if ids:
                        rows.append((uid, Json(ids)))
                if rows:
                    execute_values(
                        cur,
                        "INSERT INTO news_letter_today_batch (user_id, news_letter_ids, created_at) VALUES %s",
                        rows,
                        template="(%s, %s, now())",
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    stats["users_filled"] = len(rows)
    return stats
