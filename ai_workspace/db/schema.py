"""
Database schema definitions
리팩토링된 스키마: 4개 테이블 (Press, RSS_URL, News_Raw, News_Letter)
"""
from db.connection import get_connection


def drop_all_tables():
    """
    기존 테이블 모두 삭제
    주의: 모든 데이터가 삭제됩니다!
    """
    print("기존 테이블 삭제 중...")

    conn = get_connection()
    cur = conn.cursor()

    # 기존 테이블들 삭제
    tables_to_drop = [
        'news_event_article',
        'news_event',
        'news_reconstructed_article',
        'reconstructed_news',
        'news_article',
        'raw_rss_item',
        'news_letter_categories',
        'news_letter',
        'news_raw',
        'category',
        'rss_url',
        'press'
    ]

    for table in tables_to_drop:
        try:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
            print(f"  ✓ {table} 삭제")
        except Exception as e:
            print(f"  ✗ {table} 삭제 실패: {e}")

    conn.commit()
    conn.close()
    print("기존 테이블 삭제 완료!\n")


def create_tables():
    """
    새로운 테이블 생성
    1. Press - 언론사 정보
    2. RSS_URL - RSS 피드 주소
    3. News_Raw - 원문 기사
    4. News_Letter - 재구성 뉴스레터
    """
    print("새 테이블 생성 중...", end="")

    conn = get_connection()
    cur = conn.cursor()

    # pgvector 확장 활성화
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception as e:
        print(f"\npgvector 확장 설치 실패: {e}")

    # ========================================
    # 1. Press - 언론사 정보 테이블
    # ========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS press (
            press_id SERIAL PRIMARY KEY,
            press_name VARCHAR(100) UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_press_name ON press(press_name);
    """)

    # ========================================
    # 2. RSS_URL - 언론사 RSS 주소 테이블
    # ========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rss_url (
            rss_raw_id SERIAL PRIMARY KEY,
            press_id INT NOT NULL REFERENCES press(press_id) ON DELETE CASCADE,
            uri VARCHAR(500) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(press_id, uri)
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rss_url_press_id ON rss_url(press_id);
    """)

    # ========================================
    # 3. News_Raw - 원문 기사 저장 테이블
    # ========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS news_raw (
            raw_news_id BIGSERIAL PRIMARY KEY,
            press_id INT NOT NULL REFERENCES press(press_id) ON DELETE CASCADE,
            raw_news_title TEXT NOT NULL,
            raw_news_content TEXT,
            raw_news_url TEXT UNIQUE NOT NULL,
            embedding_result vector(1024),
            raw_news_created_at VARCHAR(100),
            raw_news_crawled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            news_letter_id INT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_raw_press_id ON news_raw(press_id);
        CREATE INDEX IF NOT EXISTS idx_news_raw_url ON news_raw(raw_news_url);
        CREATE INDEX IF NOT EXISTS idx_news_raw_news_letter_id ON news_raw(news_letter_id);
        CREATE INDEX IF NOT EXISTS idx_news_raw_created_at ON news_raw(raw_news_created_at);
        CREATE INDEX IF NOT EXISTS idx_news_raw_crawled_at ON news_raw(raw_news_crawled_at);
    """)

    # 임베딩 인덱스 추가 (코사인 유사도 검색용)
    try:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_raw_embedding
            ON news_raw USING ivfflat (embedding_result vector_cosine_ops)
            WITH (lists = 100);
        """)
    except Exception as e:
        print(f"\n임베딩 인덱스 생성 실패 (데이터 부족 시 정상): {e}")

    # ========================================
    # 4. News_Letter - 생성된 뉴스레터 테이블
    # ========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS news_letter (
            news_letter_id SERIAL PRIMARY KEY,
            news_letter_title TEXT NOT NULL,
            news_letter_sentence TEXT,
            news_letter_content TEXT NOT NULL,
            news_letter_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            news_letter_embedding vector(1024),
            news_letter_keywords JSONB,
            raw_news_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_letter_created_at ON news_letter(news_letter_created_at);
    """)

    # ========================================
    # 5. Category - 뉴스 카테고리 테이블
    # ========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS category (
            category_id SERIAL PRIMARY KEY,
            category_name VARCHAR(100) UNIQUE NOT NULL,
            category_code SMALLINT UNIQUE NOT NULL
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_category_code ON category(category_code);
    """)

    # ========================================
    # 6. News_Letter_Categories - 뉴스레터 카테고리 매핑 테이블 (M:N)
    # ========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS news_letter_categories (
            id SERIAL PRIMARY KEY,
            news_letter_id INT NOT NULL REFERENCES news_letter(news_letter_id) ON DELETE CASCADE,
            category_id INT NOT NULL REFERENCES category(category_id) ON DELETE CASCADE,
            UNIQUE(news_letter_id, category_id)
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_letter_categories_letter_id ON news_letter_categories(news_letter_id);
        CREATE INDEX IF NOT EXISTS idx_news_letter_categories_category_id ON news_letter_categories(category_id);
    """)

    # 임베딩 인덱스 추가
    try:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_letter_embedding
            ON news_letter USING ivfflat (news_letter_embedding vector_cosine_ops)
            WITH (lists = 100);
        """)
    except Exception as e:
        print(f"\n뉴스레터 임베딩 인덱스 생성 실패 (데이터 부족 시 정상): {e}")

    conn.commit()
    conn.close()
    print(" 완료!")


def insert_initial_press_data():
    """
    Press 테이블에 초기 언론사 데이터 삽입
    """
    print("언론사 초기 데이터 삽입 중...")

    conn = get_connection()
    cur = conn.cursor()

    press_list = [
        '동아일보',
        '경향신문',
        '매일경제',
        '한국경제',
        '국민일보',
        '세계일보',
        '전자신문',
        'AI타임스'
    ]

    for press_name in press_list:
        try:
            cur.execute("""
                INSERT INTO press (press_name)
                VALUES (%s)
                ON CONFLICT (press_name) DO NOTHING;
            """, (press_name,))
            print(f"  ✓ {press_name}")
        except Exception as e:
            print(f"  ✗ {press_name} 삽입 실패: {e}")

    conn.commit()
    conn.close()
    print("언론사 초기 데이터 삽입 완료!\n")


def insert_initial_rss_url_data():
    """
    RSS_URL 테이블에 초기 RSS 주소 데이터 삽입
    """
    print("RSS URL 초기 데이터 삽입 중...")

    conn = get_connection()
    cur = conn.cursor()

    # RSS 피드 매핑 (언론사명, URI)
    rss_feeds = [
        ('동아일보', 'https://rss.donga.com/total.xml'),
        ('경향신문', 'https://www.khan.co.kr/rss/rssdata/total_news.xml'),
        ('매일경제', 'https://www.mk.co.kr/rss/30000001/'),
        ('한국경제', 'https://www.hankyung.com/feed/all-news'),
        ('국민일보', 'https://www.kmib.co.kr/rss/data/kmibRssAll.xml'),
        ('세계일보', 'https://www.segye.com/Articles/RSSList/segye_recent.xml'),
        ('전자신문', 'http://rss.etnews.com/03.xml'),  # IT
        ('전자신문', 'http://rss.etnews.com/04046.xml'),  # AI
        ('전자신문', 'http://rss.etnews.com/20.xml'),  # 과학
        ('AI타임스', 'https://www.aitimes.com/rss/allArticle.xml'),
    ]

    for press_name, uri in rss_feeds:
        try:
            # press_id 조회
            cur.execute("SELECT press_id FROM press WHERE press_name = %s", (press_name,))
            result = cur.fetchone()

            if result:
                press_id = result[0]
                cur.execute("""
                    INSERT INTO rss_url (press_id, uri)
                    VALUES (%s, %s)
                    ON CONFLICT (press_id, uri) DO NOTHING;
                """, (press_id, uri))
                print(f"  ✓ {press_name}: {uri}")
            else:
                print(f"  ✗ {press_name} 언론사를 찾을 수 없습니다.")
        except Exception as e:
            print(f"  ✗ {press_name} RSS URL 삽입 실패: {e}")

    conn.commit()
    conn.close()
    print("RSS URL 초기 데이터 삽입 완료!\n")


def insert_initial_category_data():
    """
    Category 테이블에 초기 카테고리 데이터 삽입
    """
    print("카테고리 초기 데이터 삽입 중...")

    conn = get_connection()
    cur = conn.cursor()

    categories = [
        ('정치', 1),
        ('사회', 2),
        ('경제', 3),
        ('IT/과학', 4),
        ('생활문화', 5),
        ('스포츠', 6),
        ('세계', 7)
    ]

    for name, code in categories:
        try:
            cur.execute("""
                INSERT INTO category (category_name, category_code)
                VALUES (%s, %s)
                ON CONFLICT (category_name) DO NOTHING;
            """, (name, code))
            print(f"  ✓ {name}({code})")
        except Exception as e:
            # UNIQUE 제약조건(category_code) 위반 시 에러 처리
            conn.rollback()
            print(f"  ! {name}({code}) 삽입 건너뜀 (이미 존재하거나 코드 중복): {e}")

    conn.commit()
    conn.close()
    print("카테고리 초기 데이터 삽입 완료!\n")


def full_reset():
    """
    전체 DB 리셋 (테이블 삭제 -> 생성 -> 초기 데이터 삽입)
    """
    print("=" * 60)
    print("DB 전체 리셋 시작")
    print("=" * 60 + "\n")

    drop_all_tables()
    create_tables()
    insert_initial_press_data()
    insert_initial_category_data()
    insert_initial_rss_url_data()

    print("=" * 60)
    print("DB 전체 리셋 완료!")
    print("=" * 60)
