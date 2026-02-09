from sqlmodel import Session, select
from app.models.user import User, UserPreferredCategories, UserPreferredNewsletter
from app.models.news import Category
from app.schemas.user import UserCreate
from app.security import get_password_hash

def get_user_by_email(session: Session, email: str) -> User | None:
    statement = select(User).where(User.user_email == email)
    return session.exec(statement).first()

def create_user(session: Session, user: UserCreate) -> User:
    # 성별 정수 코드로 매핑
    gender_map = {
        "Male": 1,
        "Female": 2,
        "Not Specified": 0
    }
    
    gender_input = user.gender.title() if user.gender else "Not Specified"
    gender_code = gender_map.get(gender_input, 0)
    
    db_user = User(
        user_email=user.email,
        user_password_hash=get_password_hash(user.password),
        user_nickname=user.nickname,
        user_gender_code=gender_code,
        user_birth_year=user.birth_year,
        user_embedding=None 
    )
    
    try:
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        return db_user
    except Exception as e:
        session.rollback()
        print(f"회원가입 중 에러 발생: {e}")

        raise e

def update_user_categories(session: Session, user: User, category_codes: list[int]) -> User:
    try:
        cat_stmt = select(Category.category_id).where(Category.category_code.in_(category_codes))
        target_category_ids = session.exec(cat_stmt).all()

        if not target_category_ids and category_codes:
            print(f"Warning: No matching categories found for codes {category_codes}")

        statement = select(UserPreferredCategories).where(UserPreferredCategories.user_id == user.user_id)
        results = session.exec(statement).all()
        for result in results:
            session.delete(result)
            
        for cat_id in target_category_ids:
            user_pref = UserPreferredCategories(user_id=user.user_id, category_id=cat_id)
            session.add(user_pref)
            
        session.commit()
        session.refresh(user)
        return user
    except Exception as e:
        session.rollback()
        raise e

def update_user_newsletters(session: Session, user: User, news_letter_ids: list[int]) -> User:
    try:
        statement = select(UserPreferredNewsletter).where(UserPreferredNewsletter.user_id == user.user_id)
        results = session.exec(statement).all()
        for result in results:
            session.delete(result)
            
        for nl_id in news_letter_ids:
            user_pref = UserPreferredNewsletter(user_id=user.user_id, news_letter_id=nl_id)
            session.add(user_pref)
            
        session.commit()
        session.refresh(user)
        return user
    except Exception as e:
        session.rollback()
        raise e