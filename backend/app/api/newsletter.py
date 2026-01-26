from fastapi import APIRouter, Depends, Query, HTTPException
from sqlmodel import Session, select, SQLModel
from typing import List
from datetime import datetime
from app.database import get_session
from app.models.news import NewsLetter, NewsLetterCategories, Category
from app.models.batch import NewsLettersCategory

router = APIRouter(prefix="/newsletters", tags=["newsletters"])

# Response Schema (Same as onboarding for consistency)
class NewsResponse(SQLModel):
    news_letter_id: int
    news_letter_title: str
    news_letter_sentence: str
    news_letter_keywords: List[str] = []
    news_letter_created_at: datetime

@router.get("", response_model=List[NewsResponse])
def get_category_news(
    category: int = Query(..., description="Category Code (e.g., 100, 200)"),
    session: Session = Depends(get_session)
):
    """
    Get popular newsletters for a specific category.
    Ranked by the latest batch result in NewsLettersCategory.
    Returns top 40 items.
    """
    
    # 1. Validate Category Code and get ID
    category_obj = session.exec(
        select(Category).where(Category.category_code == category)
    ).first()
    
    if not category_obj:
         raise HTTPException(status_code=404, detail="Category code not found")
    
    target_category_id = category_obj.category_id

    # 2. Get latest batch ranking
    latest_batch = session.exec(
        select(NewsLettersCategory).order_by(NewsLettersCategory.created_at.desc()).limit(1)
    ).first()
    
    if not latest_batch or not latest_batch.news_letter_ids:
        return []

    ranked_ids = latest_batch.news_letter_ids

    # 3. Filter by Category ID
    statement = (
        select(NewsLetter)
        .join(NewsLetterCategories)
        .where(NewsLetterCategories.category_id == target_category_id)
        .where(NewsLetter.news_letter_id.in_(ranked_ids))
    )
    
    candidates = session.exec(statement).all()
    candidates_map = {newsletter.news_letter_id: newsletter for newsletter in candidates}
    
    # 4. Construct result preserving rank order (Limit 40)
    result = []
    for nid in ranked_ids:
        if nid in candidates_map:
            result.append(candidates_map[nid])
            if len(result) >= 40:
                break
                
    return result
