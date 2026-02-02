# core/reconstruction/repository.py
import json
from typing import List, Dict, Optional
from .validator import sanitize_text

CATEGORY_MAP = {
    # 정치
    '정치': '정치', '정부': '정치', '국회': '정치',
    # 경제
    '경제': '경제', '금융': '경제', '증권': '경제', '부동산': '경제', '산업': '경제',
    # 사회
    '사회': '사회', '환경': '사회', '교육': '사회', '노동': '사회', '복지': '사회',
    '사건': '사회', '사고': '사회', '안정': '사회', '인권': '사회',
    # 세계
    '국제': '세계', '세계': '세계', '해외': '세계', '글로벌': '세계',
    # IT/과학
    'IT과학': 'IT/과학', 'IT/과학': 'IT/과학', '과학': 'IT/과학', 'IT': 'IT/과학',
    '기술': 'IT/과학', '테크': 'IT/과학', 'AI': 'IT/과학', '인공지능': 'IT/과학',
    # 생활/문화
    '문화': '생활/문화', '생활': '생활/문화', '생활문화': '생활/문화',
    '생활/문화': '생활/문화', '연예': '생활/문화', '엔터': '생활/문화', '건강': '생활/문화',
    '여행': '생활/문화', '음식': '생활/문화', '패션': '생활/문화',
    # 스포츠
    '스포츠': '스포츠', '야구': '스포츠', '축구': '스포츠', '농구': '스포츠',
}

def save_news_letter(
    conn, 
    article_ids: List[int], 
    reconstructed: Dict,
    run_id: Optional[int] = None,
    generation_history: Optional[Dict] = None
) -> int:
    cur = conn.cursor()

    # Sanitize all text fields
    title = sanitize_text(reconstructed.get('title', ''))
    sentence = sanitize_text(reconstructed.get('sentence', ''))
    content = sanitize_text(reconstructed.get('content', ''))
    keywords = [sanitize_text(k) for k in reconstructed.get('keywords', [])]

    keywords_json = json.dumps(keywords, ensure_ascii=False)
    generation_history_json = json.dumps(generation_history, ensure_ascii=False) if generation_history else None

    cur.execute('''
        INSERT INTO news_letter (
            news_letter_title, news_letter_sentence, news_letter_content,
            news_letter_keywords, raw_news_count, news_letter_created_at,
            run_id, generation_history
        ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
        RETURNING news_letter_id
    ''', (
        title, sentence, content, keywords_json, len(article_ids), 
        run_id, generation_history_json
    ))

    saved_id = cur.fetchone()[0]

    # Save categories
    categories = reconstructed.get('categories', [])
    for category in categories:
        if category:
            mapped_category = CATEGORY_MAP.get(category, category)
            cur.execute('SELECT category_id FROM category WHERE category_name = %s', (mapped_category,))
            cat_row = cur.fetchone()
            if cat_row:
                cur.execute('''
                    INSERT INTO news_letter_categories (news_letter_id, category_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                ''', (saved_id, cat_row[0]))

    # Update associated articles
    for article_id in article_ids:
        cur.execute('''
            UPDATE news_raw
            SET news_letter_id = %s
            WHERE raw_news_id = %s
        ''', (saved_id, article_id))

    conn.commit()
    return saved_id


def generate_newsletter_embedding(conn, news_letter_id: int, content: str) -> bool:
    try:
        from core.embedder import NewsEmbedder
        embedder = NewsEmbedder(verbose=False)
        embedding = embedder.generate_embedding(content)
        
        if embedding:
            cur = conn.cursor()
            cur.execute('''
                UPDATE news_letter
                SET news_letter_embedding = %s
                WHERE news_letter_id = %s
            ''', (embedding, news_letter_id))
            conn.commit()
            return True
    except ImportError:
        pass
    except Exception as e:
        print(f"Newsletter embedding generation failed: {e}")
    
    return False