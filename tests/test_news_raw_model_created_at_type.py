import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"),
)

# tests/conftest.py가 DATABASE_URL을 이미 채워두므로 여기서는 별도 설정이 필요 없다.


def test_raw_news_created_at_is_optional_timezone_aware_datetime():
    """backend/alembic/versions/e725a62ffef1_...py가 news_raw.raw_news_created_at을
    VARCHAR(NOT NULL) -> timestamptz(nullable)로 바꾼 것과, SQLModel 필드 선언이
    실제로 일치하는지 확인한다(둘이 어긋나면 alembic autogenerate가 매번 diff를
    만들어낸다)."""
    from app.models.news import NewsRaw

    column = NewsRaw.__table__.c.raw_news_created_at

    assert column.nullable is True
    assert column.type.timezone is True
