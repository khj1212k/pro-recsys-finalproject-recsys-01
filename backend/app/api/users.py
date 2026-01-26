from fastapi import APIRouter, Depends
from sqlmodel import Session
from app.database import get_session
from app.schemas.user import UserCategoryUpdate
from app.crud.user import update_user_categories
from app.api.user_check import get_current_user
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])

@router.put("/me/categories")
def update_categories(
    category_update: UserCategoryUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    update_user_categories(session, current_user, category_update.categories)
    return {"message": "Categories updated successfully", "updated_categories": category_update.categories}
