from db.connection import get_connection, release_connection



def _safe_truncate(cur, table: str):
    try:
        cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
    except Exception as e:
        print(f"Warning: Failed to truncate {table}: {e}")



def insert_initial_press_data():
    # Press 테이블에 초기 언론사 데이터 삽입
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
        'AI타임스',
        # RSS가 아니라 공공데이터포털 Open API로 수집한다(crawler/policy_briefing.py, ADR 0023)
        '정책브리핑',
    ]

    for press_name in press_list:
        try:
            cur.execute("SELECT 1 FROM press WHERE press_name = %s", (press_name,))
            if not cur.fetchone():
                cur.execute("INSERT INTO press (press_name) VALUES (%s)", (press_name,))
                print(f"  ✓ {press_name}")
            else:
                print(f"  - {press_name} (이미 존재)")
        except Exception as e:
            print(f"  ✗ {press_name} 삽입 실패: {e}")

    conn.commit()
    release_connection(conn)
    print("언론사 초기 데이터 삽입 완료!\n")


def insert_initial_rss_url_data():
    # RSS_URL 테이블에 초기 RSS 주소 데이터 삽입
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
                # rss_uri 테이블 확인 (테이블명 수정: rss_url -> rss_uri)
                cur.execute("SELECT 1 FROM rss_uri WHERE press_id = %s AND uri = %s", (press_id, uri))
                if not cur.fetchone():
                    cur.execute("INSERT INTO rss_uri (press_id, uri) VALUES (%s, %s)", (press_id, uri))
                    print(f"  ✓ {press_name}: {uri}")
                else:
                    print(f"  - {press_name}: {uri} (이미 존재)")
            else:
                print(f"  ✗ {press_name} 언론사를 찾을 수 없습니다.")
        except Exception as e:
            print(f"  ✗ {press_name} RSS URL 삽입 실패: {e}")

    conn.commit()
    release_connection(conn)
    print("RSS URL 초기 데이터 삽입 완료!\n")


def insert_initial_category_data():
    # Category 테이블에 초기 카테고리 데이터 삽입
    print("카테고리 초기 데이터 삽입 중...")

    conn = get_connection()
    cur = conn.cursor()

    categories = [
        ('정치', 1),
        ('사회', 2),
        ('경제', 3),
        ('IT/과학', 4),
        ('생활/문화', 5),  # 프롬프트와 일치하도록 '/' 추가
        ('스포츠', 6),
        ('세계', 7)
    ]

    for name, code in categories:
        try:
            cur.execute("SELECT 1 FROM category WHERE category_name = %s", (name,))
            if not cur.fetchone():
                cur.execute("INSERT INTO category (category_name, category_code) VALUES (%s, %s)", (name, code))
                print(f"  ✓ {name}({code})")
            else:
                print(f"  - {name}({code}) (이미 존재)")
        except Exception as e:
            # UNIQUE 제약조건(category_code) 위반 시 에러 처리
            conn.rollback()
            print(f"  ! {name}({code}) 삽입 건너뜀 (에러): {e}")

    conn.commit()
    release_connection(conn)
    print("카테고리 초기 데이터 삽입 완료!\n")


def full_reset():
    # 데이터만 초기화 (스키마 변경 없음)
    print("=" * 60)
    print("DB 데이터 리셋 시작 (스키마 유지)")
    print("=" * 60 + "\n")
    conn = get_connection()
    cur = conn.cursor()
    try:
        # 뉴스 원문 및 뉴스레터 관련 데이터 초기화 (TRUNCATE로 ID 초기화 포함)
        # CASCADE 옵션으로 연관된 자식 테이블(summary, news_letter_categories 등)도 함께 삭제됨
        
        # 1. 뉴스레터 테이블 (부모)
        _safe_truncate(cur, "news_letter")
        
        # 2. 뉴스 원문 테이블 (부모)
        _safe_truncate(cur, "news_raw")
        
        # 3. 그 외 로그성 테이블 (CASCADE에 포함되지 않았을 경우를 대비해 명시)
        _safe_truncate(cur, "user_preferred_newsletter")
        _safe_truncate(cur, "user_newsletter_ctr_log")
        _safe_truncate(cur, "cluster_history")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"데이터 리셋 실패: {e}")
    finally:
        release_connection(conn)

    # 초기 참조 데이터 보장
    insert_initial_press_data()
    insert_initial_category_data()
    insert_initial_rss_url_data()

    print("=" * 60)
    print("DB 데이터 리셋 완료!")
    print("=" * 60)
