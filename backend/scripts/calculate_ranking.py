
import sys
import os
import math
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from sqlmodel import Session, select
from sqlalchemy import func

# Add backend directory to path to import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine
from app.models.news import NewsLetter
from app.models.batch import NewsLettersCategory

def calculate_ranking():
    """
    Calculate newsletter ranking based on popularity and recency.
    Target: All newsletters created before Today 17:45 KST (UTC+9).
    Score = exp(-age_hours/48) + log1p(raw_news_count)/5
    """
    with Session(engine) as session:
        # 1. Define Cutoff Time (Today 17:45 KST)
        # Server time is likely UTC, so we need to handle timezones carefully.
        # Assuming system time is KST or we convert properly.
        # Let's use UTC for calculation to be safe, knowing KST is UTC+9.
        
        now_utc = datetime.now(timezone.utc)
        korea_tz = timezone(timedelta(hours=9))
        
        # Current time in KST
        now_kst = now_utc.astimezone(korea_tz)
        
        # Target execution is 17:45 KST.
        # If script runs at 17:45, 'today' is correct.
        cutoff_kst = now_kst.replace(hour=17, minute=45, second=0, microsecond=0)
        
        # If we run this *after* 17:45, cutoff is today 17:45.
        # If we run this *before* 17:45 (e.g. testing), we might want yesterday's?
        # Requirement: "All time ~ Today 17:45"
        
        cutoff_utc = cutoff_kst.astimezone(timezone.utc)
        
        print(f"[*] Calculating Ranking for newsletters before: {cutoff_kst} (KST) / {cutoff_utc} (UTC)")

        # 2. Fetch Newsletters (created_at <= cutoff)
        statement = select(NewsLetter).where(NewsLetter.news_letter_created_at <= cutoff_utc)
        newsletters = session.exec(statement).all()
        
        print(f"[*] Found {len(newsletters)} newsletters.")

        # 3. Calculate Scores
        scored_newsletters = []
        
        for nl in newsletters:
            # age_hours calculation
            # nl.news_letter_created_at is UTC (since we store it that way in models usually)
            # Ensure it's timezone aware
            created_at = nl.news_letter_created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
                
            age_delta = cutoff_utc - created_at
            age_hours = age_delta.total_seconds() / 3600.0
            
            # Avoid negative age (if clock skew)
            if age_hours < 0:
                age_hours = 0
                
            popularity = nl.raw_news_count
            
            # Formula: exp(-age/48) + log1p(pop)/5
            # exp part: Decays over time (1.0 at 0 hours, ~0.6 at 24h, ~0.36 at 48h)
            # log part: Increases with popularity, dampened by log.
            
            score = math.exp(-age_hours / 48.0) + math.log1p(popularity) / 5.0
            
            scored_newsletters.append({
                "id": nl.news_letter_id,
                "score": score,
                "title": nl.news_letter_title,
                "age": age_hours,
                "pop": popularity
            })
            
        # 4. Sort (Desc)
        scored_newsletters.sort(key=lambda x: x["score"], reverse=True)
        
        # Debug Output (Top 5)
        print("[*] Top 5 Ranked Newsletters:")
        for idx, item in enumerate(scored_newsletters[:5]):
            print(f"   {idx+1}. [{item['score']:.4f}] ID:{item['id']} ({item['title']}) - Age:{item['age']:.1f}h, Pop:{item['pop']}")
            
        # 5. Store in DB
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
