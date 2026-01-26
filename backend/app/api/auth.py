from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session
from app.database import get_session
from app.schemas.user import UserCreate, UserResponse, UserLogin
from app.crud.user import create_user, get_user_by_email
from app.security import verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def signup(user: UserCreate, session: Session = Depends(get_session)):
    db_user = get_user_by_email(session, email=user.email)
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    return create_user(session=session, user=user)

@router.post("/login")
def login(user_credentials: UserLogin, session: Session = Depends(get_session)):
    # 1. 이메일로 사용자 조회
    user = get_user_by_email(session, email=user_credentials.email)
    
    # 2. 사용자 존재 여부 및 비밀번호 확인
    if not user or not verify_password(user_credentials.password, user.user_password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 3. 토큰 생성
    access_token = create_access_token(data={"sub": user.user_email})
    
    # 4. 응답 반환
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.user_id,
        "user_email": user.user_email,
        "user_nickname": user.user_nickname
    }
