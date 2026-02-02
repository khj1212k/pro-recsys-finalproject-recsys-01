from fastapi import APIRouter, Depends, Query, HTTPException, Header
from sqlmodel import Session, select, SQLModel
from typing import List, Optional
from datetime import datetime
from app.database import get_session
from app.models.news import NewsLetter, NewsLetterCategories, Category
from app.models.batch import NewsLettersCategory, NewsLetterTodayBatch

from app.models.user import User
from app.api.user_check import get_current_user

router = APIRouter(prefix="/newsletters", tags=["newsletters"])

# 뉴스레터 응답 Schema
class NewsResponse(SQLModel):
    news_letter_id: int
    news_letter_title: str
    news_letter_sentence: str
    news_letter_keywords: List[str] = []
    news_letter_created_at: datetime

# 오늘의 뉴스레터 응답 Schema
class TodayNewsResponse(NewsResponse):
    category_id: int
    category_name: str

@router.get("/today", response_model=List[TodayNewsResponse])
def get_today_news(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    # 1. 사용자별 오늘의 뉴스레터 Batch ID 조회
    today_batch = session.exec(
        select(NewsLetterTodayBatch)
        .where(NewsLetterTodayBatch.user_id == user.user_id)
        .order_by(NewsLetterTodayBatch.created_at.desc())
        .limit(1)
    ).first()
    
    if not today_batch or not today_batch.news_letter_ids:
        return []
    
    target_ids = today_batch.news_letter_ids
    
    # 2. 뉴스레터-카테고리 정보 조회
    results = session.exec(
        select(NewsLetter, Category)
        .join(NewsLetterCategories, NewsLetter.news_letter_id == NewsLetterCategories.news_letter_id)
        .join(Category, NewsLetterCategories.category_id == Category.category_id)
        .where(NewsLetter.news_letter_id.in_(target_ids))
    ).all()
    
    # 3. 응답 생성
    news_map = {}
    for nl, cat in results:
        news_map[nl.news_letter_id] = {
            "news": nl,
            "category": cat
        }
        
    response_list = []
    for nid in target_ids:
        if nid in news_map:
            item = news_map[nid]
            nl = item["news"]
            cat = item["category"]
            
            response_list.append(TodayNewsResponse(
                news_letter_id=nl.news_letter_id,
                news_letter_title=nl.news_letter_title,
                news_letter_sentence=nl.news_letter_sentence,
                news_letter_keywords=nl.news_letter_keywords,
                news_letter_created_at=nl.news_letter_created_at,
                category_id=cat.category_id,
                category_name=cat.category_name
            ))
            
            if len(response_list) >= 20: # 화면에 출력되는 뉴스레터 개수
                break
                
    return response_list

@router.get("", response_model=List[NewsResponse])
def get_category_news(
    category: int = Query(..., description="Category Code (e.g., 100, 200)"),
    session: Session = Depends(get_session)
):
    
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
    candidates_map = {newsletter.news_letter_id: newsletter for newsletter in candidates}
    
    # 4. 결과 생성 (40개 제한)
    result = []
    for nid in ranked_ids:
        if nid in candidates_map:
            result.append(candidates_map[nid])
            if len(result) >= 40:
                break
                
    return result

# 뉴스레터 상세 조회 Schema
class NewsDetailResponse(NewsResponse):
    news_letter_content: str
    category_id: int
    category_name: str

@router.get("/{news_letter_id}", response_model=NewsDetailResponse)
def get_newsletter_detail(
    news_letter_id: int,
    session: Session = Depends(get_session)
):
    # 뉴스레터 ID 기반 조회
    result = session.exec(
        select(NewsLetter, Category)
        .join(NewsLetterCategories, NewsLetter.news_letter_id == NewsLetterCategories.news_letter_id)
        .join(Category, NewsLetterCategories.category_id == Category.category_id)
        .where(NewsLetter.news_letter_id == news_letter_id)
    ).first()
    
    if not result:
        raise HTTPException(status_code=404, detail="Newsletter not found")
        
    nl, cat = result
    
    return NewsDetailResponse(
        news_letter_id=nl.news_letter_id,
        news_letter_title=nl.news_letter_title,
        news_letter_sentence=nl.news_letter_sentence,
        news_letter_content=nl.news_letter_content,
        news_letter_keywords=nl.news_letter_keywords,
        news_letter_created_at=nl.news_letter_created_at,
        category_id=cat.category_id,
        category_name=cat.category_name
    )
