"""
GPT-based news reconstructor
Ported from news/reconstructor/news_reconstructor.py with feedback loop support
"""
import os
import json
from typing import Dict, List, Optional, Any

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    OPENAI_AVAILABLE = False


class NewsReconstructor:
    """GPT-based news reconstructor with iterative refinement support"""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        if not OPENAI_AVAILABLE:
            raise ImportError("openai library required: pip install openai")

        self.model = model
        self.client = self._get_client(api_key)

    def _get_client(self, api_key: Optional[str] = None) -> Any:
        """Create OpenAI client"""
        if api_key:
            return OpenAI(api_key=api_key)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            from dotenv import load_dotenv
            load_dotenv()
            api_key = os.environ.get("OPENAI_API_KEY")

        if not api_key:
            raise ValueError("OPENAI_API_KEY 환경변수를 설정하세요")

        return OpenAI(api_key=api_key)

    def reconstruct(self, articles: List[Dict], feedback: Optional[str] = None) -> Optional[Dict]:
        """
        Reconstruct multiple articles into a unified newsletter.
        
        Args:
            articles: List of article dicts (raw_news_id, press_name, title, content)
            feedback: Optional feedback from previous evaluation to guide regeneration
            
        Returns:
            result: Reconstructed news dict or None
        """
        if not articles:
            return None

        # Limit number of articles to avoid context length issues
        # Sort by content length (descending) to keep most informative articles
        MAX_ARTICLES = 10
        if len(articles) > MAX_ARTICLES:
            articles.sort(key=lambda x: len(x.get('content') or ''), reverse=True)
            articles = articles[:MAX_ARTICLES]

        # Build articles text
        articles_text = ""
        for i, art in enumerate(articles, 1):
            content_preview = art.get('content', '')[:2000] if art.get('content') else "(본문 없음)"
            articles_text += f"""
---
[기사 {i}]
출처: {art.get('press_name', '알 수 없음')}
제목: {art.get('title', '')}
본문:
{content_preview}
---
"""

        # Build feedback instruction if provided
        feedback_instruction = ""
        if feedback:
            feedback_instruction = f"""
⚠️ 이전 작성이 다음 이유로 거부되었습니다:
{feedback}

위 피드백을 반영하여 수정된 뉴스레터를 작성하세요.
"""

        prompt = f"""
당신은 여러 언론사의 기사를 큐레이션해
독자가 한눈에 이 이슈가 '무엇에 관한 이야기이고,
지금 어떤 방향으로 전개되고 있는지'를 이해할 수 있도록 정리하는
뉴스레터 에디터입니다.

{feedback_instruction}

아래 기사들은 같은 주제·맥락을 공유하는 뉴스들입니다.
이 기사들을 종합해 하나의 **주제형 뉴스 브리핑**을 작성하세요.

원본 기사들:
{articles_text}

작성 원칙:

1. **첫 문장 규칙 (가장 중요)**:
   - 본문 첫 문장은 **'핵심 주체·이슈(명사) + 현재 벌어지는 구체적 흐름(동사·변화)'**가
     하나의 문장 안에 자연스럽게 드러나도록 작성하세요.
   - 독자가 첫 문장만 읽고도 "아, 지금 이 이슈가 어떤 방향으로 가고 있구나"를
     이해할 수 있어야 합니다.

2. **주제 중심 정리**:
   - 개별 사건을 억지로 하나의 인과 서사로 엮지 마세요.
   - 기사 전체에서 공통적으로 드러나는 **문제의식, 갈등 축, 정책 방향**을 중심으로 정리하세요.

    3. **구조화된 브리핑 형식**:
       - 제목 (공백 포함 20자 이내): '핵심 키워드 + 서술어' 형태로 짧고 강렬하게. (예: "비트코인, 사상 최고가 경신", "의대 증원 갈등, 파국 치닫나")
       - 한줄요약 (30자 내외): **무엇이 왜 주목받는지**가 드러나도록 작성
       - 본문 (500~800자, '~다' 체):
         * 첫 문단: 핵심 주체·이슈 + 현재 흐름 요약
         * 중간 문단: 중심 사건과 관련 사건들을 역할별로 정리
         * 마지막 문단: 이 흐름의 의미와 향후 관전 포인트

    4. **객관적 톤 유지 (매우 중요)**:
       - '충격', '경악', '논란', '파문', '결국' 등 감정적/주관적 수식어 절대 사용 금지.
       - '긴장이 고조되고 있다' -> '긴장이 이어지고 있다'와 같이 건조하게 서술.
       - 사실 관계 위주로만 작성.

5. **키워드 선정 기준** (5개):
   - 핵심 주체 (1~2)
   - 핵심 이슈·사건명 (1~2)
   - 정책·제도·개념 (1~2)

출력 형식 (JSON):
{{
    "title": "주제의 핵심을 드러내는 제목",
    "sentence": "주제를 요약하는 한줄 설명",
    "content": "주제형 뉴스 브리핑 본문",
    "keywords": ["주제 키워드", "사건명", "핵심 개념", "고유명사", "행위"],
    "categories": ["정치/경제/사회/국제/IT과학/문화/스포츠 중 1~2개"]
}}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "당신은 20년 경력의 뉴스 에디터입니다. 여러 언론사의 기사를 통합하여 객관적이고 사실 중심의 균형 잡힌 기사를 작성합니다. 모든 응답은 JSON 형식으로 제공합니다."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                response_format={"type": "json_object"}
            )

            result = json.loads(response.choices[0].message.content)
            return result

        except Exception as e:
            print(f"GPT 호출 실패: {e}")
            return None


def save_news_letter(conn, article_ids: List[int], reconstructed: Dict) -> int:
    """
    Save reconstructed news to news_letter table.
    Updates news_raw.news_letter_id for associated articles.
    
    Args:
        conn: DB connection
        article_ids: Original article IDs
        reconstructed: Reconstructed result dict
        
    Returns:
        saved_id: Saved record ID
    """
    cur = conn.cursor()

    keywords_json = json.dumps(reconstructed.get('keywords', []), ensure_ascii=False)

    cur.execute('''
        INSERT INTO news_letter (
            news_letter_title, news_letter_sentence, news_letter_content,
            news_letter_keywords,
            raw_news_count
        ) VALUES (%s, %s, %s, %s, %s)
        RETURNING news_letter_id
    ''', (
        reconstructed.get('title', ''),
        reconstructed.get('sentence', ''),
        reconstructed.get('content', ''),
        keywords_json,
        len(article_ids)
    ))

    saved_id = cur.fetchone()[0]

    # Save categories to news_letter_categories table
    # Category mapping for flexible matching
    CATEGORY_MAP = {
        '정치': '정치',
        '경제': '경제',
        '사회': '사회',
        '국제': '세계',
        '세계': '세계',
        'IT과학': 'IT/과학',
        'IT/과학': 'IT/과학',
        '과학': 'IT/과학',
        'IT': 'IT/과학',
        '문화': '생활문화',
        '생활': '생활문화',
        '생활문화': '생활문화',
        '스포츠': '스포츠',
    }
    
    categories = reconstructed.get('categories', [])
    for category in categories:
        if category:
            # Map to DB category name
            mapped_category = CATEGORY_MAP.get(category, category)
            
            cur.execute('SELECT category_id FROM category WHERE category_name = %s', (mapped_category,))
            cat_row = cur.fetchone()
            if cat_row:
                cur.execute('''
                    INSERT INTO news_letter_categories (news_letter_id, category_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                ''', (saved_id, cat_row[0]))

    # Update news_raw.news_letter_id for associated articles
    for article_id in article_ids:
        cur.execute('''
            UPDATE news_raw
            SET news_letter_id = %s, updated_at = NOW()
            WHERE raw_news_id = %s
        ''', (saved_id, article_id))

    conn.commit()

    return saved_id


def generate_newsletter_embedding(conn, news_letter_id: int, content: str) -> bool:
    """
    Generate and save embedding for a newsletter.
    
    Args:
        conn: DB connection
        news_letter_id: Newsletter ID to update
        content: Newsletter content to embed
        
    Returns:
        success: True if embedding was saved
    """
    try:
        # Try to import embedder (optional dependency)
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
        # Embedder not available, skip embedding generation
        pass
    except Exception as e:
        print(f"Newsletter embedding generation failed: {e}")
    
    return False

