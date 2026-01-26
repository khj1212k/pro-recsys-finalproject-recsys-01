from sqlmodel import SQLModel, create_engine, Session
from typing import Generator
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# UTF-8 인코딩 설정
engine = create_engine(DATABASE_URL, connect_args={"client_encoding": "utf8"})

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session