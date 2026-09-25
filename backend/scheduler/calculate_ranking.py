import sys
import os
import math
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from sqlmodel import Session, select

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from app.models.news import NewsLetter
from app.models.batch import NewsLettersCategory


def compute_scores(newsletters, cutoff_utc: datetime) -> List[Dict]:
    """뉴스레터 리스트에 인기도-최신성 점수를 매겨 내림차순 정렬해 반환 (순수 함수, DB 비의존).

    Score = exp(-age_hours/48) + log1p(raw_news_count)/5
    """
    scored_newsletters = []

    for nl in newsletters:
        created_at = nl.news_letter_created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age_delta = cutoff_utc - created_at
        age_hours = age_delta.total_seconds() / 3600.0

        if age_hours < 0:
            age_hours = 0

        popularity = nl.raw_news_count

        score = math.exp(-age_hours / 48.0) + math.log1p(popularity) / 5.0

        scored_newsletters.append({
            "id": nl.news_letter_id,
            "score": score,
            "title": nl.news_letter_title,
            "age": age_hours,
            "pop": popularity
        })

    scored_newsletters.sort(key=lambda x: x["score"], reverse=True)
    return scored_newsletters


def get_kst_day_utc_bounds(cutoff_kst: datetime):
    """cutoff_kst가 속한 KST 달력일의 [00:00, 다음날 00:00) 구간을 UTC로 변환해 반환.

    idempotency 체크(오늘 배치가 이미 존재하는지)를 DB의 timezone-naive 비교 없이
    정확한 UTC 범위 쿼리로 수행하기 위해 사용한다.
    """
    day_start_kst = cutoff_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_kst = day_start_kst + timedelta(days=1)
    return day_start_kst.astimezone(timezone.utc), day_end_kst.astimezone(timezone.utc)


def calculate_ranking():
    """
    인기도-최신성 기반 뉴스레터 순위 계산
    대상 : 17:45 KST 이전에 생성된 모든 뉴스레터
    Score = exp(-age_hours/48) + log1p(raw_news_count)/5

    Idempotent: 같은 KST 날짜에 재실행(Airflow 재시도/backfill 등)되면 새 row를
    추가하지 않고 기존 배치를 갱신한다.
    """
    with Session(engine) as session:
        now_utc = datetime.now(timezone.utc)
        korea_tz = timezone(timedelta(hours=9))

        now_kst = now_utc.astimezone(korea_tz)

        cutoff_kst = now_kst.replace(hour=17, minute=45, second=0, microsecond=0)

        cutoff_utc = cutoff_kst.astimezone(timezone.utc)

        print(f"[*] Calculating Ranking for newsletters before: {cutoff_kst} (KST) / {cutoff_utc} (UTC)")

        statement = select(NewsLetter).where(NewsLetter.news_letter_created_at <= cutoff_utc)
        newsletters = session.exec(statement).all()

        print(f"[*] Found {len(newsletters)} newsletters.")

        scored_newsletters = compute_scores(newsletters, cutoff_utc)

        print("[*] Top 5 Ranked Newsletters:")
        for idx, item in enumerate(scored_newsletters[:5]):
            print(f"   {idx+1}. [{item['score']:.4f}] ID:{item['id']} ({item['title']}) - Age:{item['age']:.1f}h, Pop:{item['pop']}")

        ranked_ids = [item["id"] for item in scored_newsletters]

        # Idempotency: 같은 KST 날짜에 이미 배치가 있으면 UPSERT
        day_start_utc, day_end_utc = get_kst_day_utc_bounds(cutoff_kst)
        existing_stmt = select(NewsLettersCategory).where(
            NewsLettersCategory.created_at >= day_start_utc,
            NewsLettersCategory.created_at < day_end_utc,
        )
        existing_batch = session.exec(existing_stmt).first()

        if existing_batch:
            existing_batch.news_letter_ids = ranked_ids
            existing_batch.created_at = datetime.now(timezone.utc)
            session.add(existing_batch)
            session.commit()
            session.refresh(existing_batch)
            print(f"[*] Updated existing batch ID: {existing_batch.news_letter_batch_id} (idempotent re-run) with {len(ranked_ids)} items.")
        else:
            batch_entry = NewsLettersCategory(
                news_letter_ids=ranked_ids,
                created_at=datetime.now(timezone.utc)
            )
            session.add(batch_entry)
            session.commit()
            session.refresh(batch_entry)
            print(f"[*] Successfully saved batch ID: {batch_entry.news_letter_batch_id} with {len(ranked_ids)} items.")

if __name__ == "__main__":
    calculate_ranking()
