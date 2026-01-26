from pydantic import BaseModel, Field
from typing import Optional

class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=8)
    nickname: str
    gender: Optional[str] = None # "Male", "Female", "Not specified"
    birth_year: Optional[int] = None

class UserLogin(BaseModel):
    email: str
    password: str

class UserCategoryUpdate(BaseModel):
    categories: list[int]

class UserResponse(BaseModel):
    user_id: int
    user_email: str
    user_nickname: str
    user_gender_code: Optional[int]
    user_birth_year: Optional[int]
    
    class Config:
        from_attributes = True