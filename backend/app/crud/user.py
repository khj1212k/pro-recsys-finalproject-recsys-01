from sqlmodel import Session, select
from app.models.user import User
from app.schemas.user import UserCreate
from app.security import get_password_hash

def get_user_by_email(session: Session, email: str) -> User | None:
    statement = select(User).where(User.user_email == email)
    return session.exec(statement).first()

def create_user(session: Session, user: UserCreate) -> User:
    # 성별 문자열을 정수 코드로 변환
    gender_map = {
        "Male": 1,
        "Female": 2,
        "Not Specified": 0
    }
    
    gender_input = user.gender.title() if user.gender else "Not Specified"
    gender_code = gender_map.get(gender_input, 0)
    
    # DB 모델 객체 생성
    db_user = User(
        user_email=user.email,
        user_password_hash=get_password_hash(user.password),
        user_nickname=user.nickname,
        user_gender_code=gender_code,
        user_birth_year=user.birth_year,
        user_embedding=None 
    )
    
    try:
        # 데이터베이스 저장
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        return db_user
    except Exception as e:
        session.rollback()
        print(f"회원가입 중 에러 발생: {e}")
        raise e