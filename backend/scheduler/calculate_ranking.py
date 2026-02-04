import sys
import os
import math
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from sqlmodel import Session, select
from sqlalchemy import func

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from app.models.news import NewsLetter
from app.models.batch import NewsLettersCategory

def calculate_ranking():
    """
    인기도-최신성 기반 뉴스레터 순위 계산
    대상 : 17:45 KST 이전에 생성된 모든 뉴스레터
    Score = exp(-age_hours/48) + log1p(raw_news_count)/5
    """
    with Session(engine) as session:
        now_utc = datetime.now(timezone.utc)
        korea_tz = timezone(timedelta(hours=9))
        
        now_kst = now_utc.astimezone(korea_tz)
        
        cutoff_kst = now_kst.replace(hour=17, minute=0, second=0, microsecond=0)
        
        cutoff_utc = cutoff_kst.astimezone(timezone.utc)
        
        print(f"[*] Calculating Ranking for newsletters before: {cutoff_kst} (KST) / {cutoff_utc} (UTC)")
        
        statement = select(NewsLetter).where(NewsLetter.news_letter_created_at <= cutoff_utc)
        newsletters = session.exec(statement).all()
        
        print(f"[*] Found {len(newsletters)} newsletters.")

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
        
        print("[*] Top 5 Ranked Newsletters:")
        for idx, item in enumerate(scored_newsletters[:5]):
            print(f"   {idx+1}. [{item['score']:.4f}] ID:{item['id']} ({item['title']}) - Age:{item['age']:.1f}h, Pop:{item['pop']}")
            
        ranked_ids = [item["id"] for item in scored_newsletters]
        
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
