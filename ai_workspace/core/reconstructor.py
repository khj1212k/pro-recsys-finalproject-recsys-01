# core/reconstructor.py

# 리팩토링된 모듈에서 불러와 그대로 노출
from .reconstruction.generator import NewsReconstructor
from .reconstruction.repository import save_news_letter, generate_newsletter_embedding

__all__ = ['NewsReconstructor', 'save_news_letter', 'generate_newsletter_embedding']