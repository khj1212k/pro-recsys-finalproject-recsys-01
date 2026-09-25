from functools import partial

from fastapi import APIRouter, BackgroundTasks, Depends, Query, HTTPException, Header, Response
from sqlmodel import Session, select, SQLModel
from typing import List, Optional
from datetime import datetime
from app.database import get_session
from app.models.news import NewsLetter, NewsLetterCategories, Category
from app.models.batch import NewsLettersCategory, NewsLetterTodayBatch

from app.models.user import User
from app.api.user_check import get_current_user
from app.recsys.runtime import get_recommendation_service
from app.recsys.service import RecommendationService
from app.recsys.sql_repository import SqlRecsysRepository

router = APIRouter(prefix="/newsletters", tags=["newsletters"])

# 뉴스레터 응답 Schema
class NewsResponse(SQLModel):
    news_letter_id: int
    news_letter_title: str
    news_letter_sentence: str
    news_letter_keywords: List[str] = []
    news_letter_created_at: datetime
    raw_news_count: int

# 오늘의 뉴스레터 응답 Schema
class TodayNewsResponse(NewsResponse):
    category_id: int
    category_name: str

def hydrate_today_news(
    session: Session, target_ids: List[int], limit: int = 20
) -> List[TodayNewsResponse]:
    """추천 ID 순서를 유지한 채 화면 응답으로 바꾼다. 카테고리 매핑이 없는 뉴스레터는
    (기존 배치 경로와 동일하게) 빠지고, 최대 limit개까지만 돌려준다."""
    if not target_ids:
        return []

    # 1. 뉴스레터-카테고리 정보 조회
    results = session.exec(
        select(NewsLetter, Category)
        .join(NewsLetterCategories, NewsLetter.news_letter_id == NewsLetterCategories.news_letter_id)
        .join(Category, NewsLetterCategories.category_id == Category.category_id)
        .where(NewsLetter.news_letter_id.in_(target_ids))
    ).all()

    # 2. 응답 생성
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
                raw_news_count=nl.raw_news_count,
                category_id=cat.category_code,
                category_name=cat.category_name
            ))

            if len(response_list) >= limit: # 화면에 출력되는 뉴스레터 개수
                break

    return response_list


def get_request_repo(session: Session = Depends(get_session)) -> SqlRecsysRepository:
    return SqlRecsysRepository(session)


def get_today_hydrator(session: Session = Depends(get_session)):
    return partial(hydrate_today_news, session)


@router.get("/today", response_model=List[TodayNewsResponse])
def get_today_news(
    response: Response,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    service: RecommendationService = Depends(get_recommendation_service),
    request_repo: SqlRecsysRepository = Depends(get_request_repo),
    hydrate=Depends(get_today_hydrator),
):
    # RECSYS_MODE=realtime이면 요청 시점에 계산하고, 실패/시간 초과면 배치 행 -> 인기
    # -> 최신 순으로 폴백한다(app/recsys/service.py, ADR 0015). 응답 본문 형식은 그대로다.
    rec = service.recommend(user.user_id, fallback_repo=request_repo)
    items = hydrate(rec.news_letter_ids)

    response.headers["X-Rec-Source"] = rec.source
    response.headers["X-Model-Version"] = rec.model_version
    response.headers["X-Request-Id"] = rec.request_id
    background_tasks.add_task(
        service.log_impressions, user.user_id, rec, [i.news_letter_id for i in items]
    )
    return items

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
        raw_news_count=nl.raw_news_count,
        category_id=cat.category_code,
        category_name=cat.category_name
    )
