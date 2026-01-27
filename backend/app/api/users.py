from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from app.database import get_session
from app.schemas.user import UserCategoryUpdate, UserRead
from app.crud.user import update_user_categories
from app.api.user_check import get_current_user
from app.models.user import User, UserPreferredCategories
from app.models.news import Category

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/me", response_model=UserRead)
def read_user_me(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    # Query categories
    statement = (
        select(Category.category_code)
        .join(UserPreferredCategories, UserPreferredCategories.category_id == Category.category_id)
        .where(UserPreferredCategories.user_id == current_user.user_id)
    )
    codes = session.exec(statement).all()
    
    return UserRead(
        user_id=current_user.user_id,
        user_email=current_user.user_email,
        user_nickname=current_user.user_nickname,
        user_gender_code=current_user.user_gender_code,
        user_birth_year=current_user.user_birth_year,
        interests=list(codes)
    )

@router.put("/me/categories")
def update_categories(
    category_update: UserCategoryUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    update_user_categories(session, current_user, category_update.categories)
    return {"message": "Categories updated successfully", "updated_categories": category_update.categories}