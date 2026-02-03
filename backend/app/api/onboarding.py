from fastapi import APIRouter, Depends, Query, HTTPException
from sqlmodel import Session, select, SQLModel
from typing import List, Optional
from datetime import datetime
from app.database import get_session
from app.models.news import NewsLetter, NewsLetterCategories, Category
from app.models.batch import NewsLettersCategory

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# 뉴스레터 응답 Schema
class OnboardingNewsResponse(SQLModel):
    news_letter_id: int
    news_letter_title: str
    news_letter_sentence: str
    news_letter_keywords: List[str] = []
    news_letter_created_at: datetime

@router.get("/news", response_model=List[OnboardingNewsResponse])
def get_onboarding_news(
    category: int = Query(..., description="Category Code (e.g., 100, 200)"),
    limit: int = Query(6, description="Number of items to return"),
    session: Session = Depends(get_session)
):
    print(f"DEBUG: get_onboarding_news called with category={category}, limit={limit}")
    # 1. 카테고리 코드 기반 ID 조회

    category_obj = session.exec(
        select(Category).where(Category.category_code == category)
    ).first()
    
    if not category_obj:
         raise HTTPException(status_code=404, detail="Category code not found")
    
    target_category_id = category_obj.category_id

    # 2. 최신 Batch ranking 조회
    latest_batch = session.exec(
        select(NewsLettersCategory).order_by(NewsLettersCategory.created_at.desc()).limit(1)
    ).first()
    
    if not latest_batch or not latest_batch.news_letter_ids:
        return []

    ranked_ids = latest_batch.news_letter_ids

    # 3. 카테고리 ID 기반 필터링
    statement = (
        select(NewsLetter)
        .join(NewsLetterCategories)
        .where(NewsLetterCategories.category_id == target_category_id)
        .where(NewsLetter.news_letter_id.in_(ranked_ids))
    )
    
    candidates = session.exec(statement).all()
    
    # 4. 결과 생성 (limit 제한)
    candidates_map = {newsletter.news_letter_id: newsletter for newsletter in candidates}
    
    result = []
    for nid in ranked_ids:
        if nid in candidates_map:
            result.append(candidates_map[nid])
            if len(result) >= limit:
                break
                
    return result
