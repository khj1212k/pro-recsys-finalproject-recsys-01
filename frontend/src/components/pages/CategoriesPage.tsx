import React, { useState, useEffect } from 'react';
import { Bookmark, X, Clock, Check, Loader2 } from 'lucide-react';
import { categoryNames, categoryColors } from '@/data/category_constants';
import { useUserStore } from '@/store/userStore';
import { Category, NewsArticle } from '@/types';
import { fetchOnboardingNews } from '@/lib/api';
import { mapCategoryKeyToCode, formatDate } from '@/lib/utils';
import { toast } from "sonner";

const renderMarkdownInline = (text: string): React.ReactNode => {
  const parts: React.ReactNode[] = [];
  let key = 0;

  const boldRegex = /\*\*(.+?)\*\*/g;
  let match;
  let lastIndex = 0;

  while ((match = boldRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.substring(lastIndex, match.index));
    }
    parts.push(
      <strong key={`bold-${key++}`} className="font-bold">
        {match[1]}
      </strong>
    );
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(text.substring(lastIndex));
  }

  return parts.length > 0 ? <>{parts}</> : text;
};

const categories: Category[] = ['politics', 'economy', 'it', 'society', 'culture', 'sports', 'world'];

const CategoriesPage: React.FC = () => {
  const [activeCategory, setActiveCategory] = useState<Category>('politics');
  const [selectedArticle, setSelectedArticle] = useState<NewsArticle | null>(null);
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const { user, addReadArticle } = useUserStore();

  useEffect(() => {
    const loadNews = async () => {
      setIsLoading(true);
      try {
        const code = mapCategoryKeyToCode(activeCategory);
        const data = await fetchOnboardingNews(code);
        const mapped: NewsArticle[] = data.map(item => ({
          id: item.news_letter_id.toString(),
          title: item.news_letter_title,
          summary: item.news_letter_sentence,
          context: "",
          facts: [],
          category: activeCategory,
          keywords: item.news_letter_keywords.map((k, i) => ({ id: `k${i}`, term: k, category: activeCategory, savedAt: new Date() })),
          sourceUrl: "#",
          publishedAt: new Date(item.news_letter_created_at),
          hookingSentence: item.news_letter_sentence
        }));
        setArticles(mapped);
      } catch (e) {
        console.error(e);
        toast.error("뉴스를 불러오는데 실패했습니다.");
      } finally {
        setIsLoading(false);
      }
    };
    loadNews();
  }, [activeCategory]);


  const handleCardClick = async (article: NewsArticle) => {
    if (!article.fullContent) {
      try {
        const { fetchNewsletterDetail } = await import('@/lib/api');
        const detail = await fetchNewsletterDetail(parseInt(article.id));
        const detailedArticle = {
          ...article,
          fullContent: detail.news_letter_content,
        };
        setSelectedArticle(detailedArticle);
        addReadArticle(article.id);
        return;
      } catch (e) {
        console.error(e);
        toast.error("상세 내용을 불러오지 못했습니다.");
      }
    }

    setSelectedArticle(article);
    addReadArticle(article.id);
  };

  const isArticleRead = (articleId: string) => {
    return user?.readArticles.includes(articleId) || false;
  };

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-2xl lg:text-3xl font-bold text-foreground mb-2">분야별 뉴스</h1>
        <p className="text-muted-foreground">관심 분야의 심층 뉴스를 읽어보세요</p>
      </div>

      {/* Category Tabs */}
      <div className="flex gap-2 mb-8 overflow-x-auto pb-2 -mx-4 px-4 lg:mx-0 lg:px-0">
        {categories.map((cat) => (
          <button
            key={cat}
            onClick={() => setActiveCategory(cat)}
            className={`px-4 py-2 rounded-xl text-sm font-medium whitespace-nowrap transition-all duration-200 ${activeCategory === cat
              ? categoryColors[cat]
              : 'bg-secondary text-secondary-foreground hover:bg-muted'
              }`}
          >
            {categoryNames[cat]}
          </button>
        ))}
      </div>

      {/* Articles */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {articles.map((article, index) => {
          const isRead = isArticleRead(article.id);
          return (
            <ArticleCard
              key={article.id}
              article={article}
              index={index}
              isRead={isRead}
              onCardClick={handleCardClick}
            />
          );
        })}
      </div>

      {/* News Letter Modal */}
      {selectedArticle && (
        <div className="fixed inset-0 bg-foreground/30 backdrop-blur-sm z-50 flex items-center justify-center p-4 overflow-y-auto">
          <div className="bg-card rounded-3xl p-6 w-full max-w-4xl shadow-medium animate-scale-in my-8 max-h-[90vh] overflow-y-auto">
            {/* Header */}
            <div className="flex items-start justify-between mb-6">
              <div className="flex items-center gap-3">
                <span className={`category-badge ${categoryColors[selectedArticle.category]}`}>
                  {categoryNames[selectedArticle.category]}
                </span>
                <div className="flex items-center gap-1 text-xs text-muted-foreground">
                  <Clock className="w-3.5 h-3.5" />
                  <span>{formatDate(selectedArticle.publishedAt)}</span>
                </div>
              </div>
              <button
                onClick={() => setSelectedArticle(null)}
                className="w-8 h-8 rounded-full bg-secondary flex items-center justify-center hover:bg-muted transition-colors"
                aria-label="Close"
              >
                <X className="w-4 h-4 text-muted-foreground" />
              </button>
            </div>

            {/* Title */}
            <h1 className="text-3xl font-bold text-foreground mb-8 cursor-text select-text">
              {selectedArticle.title}
            </h1>

            {/* 한 줄 요약 */}
            <div className="bg-primary/20 rounded-2xl p-6 mb-8 transform hover:scale-[1.01] transition-transform duration-200">
              <p className="text-sm font-bold text-foreground mb-3 flex items-center gap-2">
                <span className="text-xl">📝</span> 한 줄 요약
              </p>
              <p className="text-foreground text-lg leading-relaxed font-medium">{selectedArticle.summary}</p>
            </div>

            <div className="flex flex-col gap-6 mb-8">

            </div>

            {/* Keywords Section */}
            <div className="border-t border-border pt-8 mb-10">
              <p className="text-sm font-bold text-foreground mb-4">🔑 핵심 키워드</p>
              <div className="flex flex-wrap gap-2">
                {selectedArticle.keywords.map((keyword) => (
                  <span
                    key={keyword.id}
                    className="keyword-chip cursor-default"
                  >
                    {keyword.term}
                  </span>
                ))}
              </div>
            </div>

            {/* Divider */}
            <div className="relative py-8">
              <div className="absolute inset-0 flex items-center">
                <div className="w-full border-t border-border border-dashed"></div>
              </div>
              <div className="relative flex justify-center">
                <span className="bg-card px-4 text-sm text-muted-foreground font-medium">전체 뉴스레터 읽기</span>
              </div>
            </div>

            {/* Full Content */}
            {selectedArticle.fullContent ? (
              <div className="prose prose-lg max-w-none mt-4 animate-fade-in-up">
                <div className="text-foreground leading-relaxed space-y-4">
                  {selectedArticle.fullContent.split('\n').map((line, idx) => {
                    const trimmedLine = line.trimStart();
                    // H3
                    if (trimmedLine.startsWith('### ')) {
                      return (
                        <h3 key={idx} className="text-xl font-bold text-foreground mt-8 mb-4">
                          {renderMarkdownInline(trimmedLine.replace('### ', ''))}
                        </h3>
                      );
                    }
                    // H2
                    if (trimmedLine.startsWith('## ')) {
                      return (
                        <h2 key={idx} className="text-2xl font-bold text-foreground mt-10 mb-6 pb-2 border-b border-border">
                          {renderMarkdownInline(trimmedLine.replace('## ', ''))}
                        </h2>
                      );
                    }
                    // H1
                    if (trimmedLine.startsWith('# ')) {
                      return null;
                    }
                    // Empty Layout
                    if (line.trim() === '') {
                      return <br key={idx} className="block content-[''] h-4" />;
                    }
                    // Paragraph
                    return (
                      <p key={idx} className="text-foreground/90 leading-8 text-[1.05rem]">
                        {renderMarkdownInline(trimmedLine)}
                      </p>
                    );
                  })}
                </div>
              </div>
            ) : (
              <div className="py-10 text-center text-muted-foreground">
                <p>전체 내용을 불러올 수 없습니다.</p>
              </div>
            )}

            {/* Close Button */}
            <div className="border-t border-border pt-8 mt-12 sticky bottom-0 bg-card pb-2">
              <button
                onClick={() => setSelectedArticle(null)}
                className="w-full btn-primary py-4 text-lg shadow-lg hover:shadow-xl transition-shadow"
              >
                닫기
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

interface ArticleCardProps {
  article: NewsArticle;
  index: number;
  isRead: boolean;
  onCardClick: (article: NewsArticle) => void;
}

const ArticleCard: React.FC<ArticleCardProps> = ({
  article,
  index,
  isRead,
  onCardClick,
}) => {
  return (
    <article
      className="card-news animate-slide-up cursor-pointer hover:shadow-medium transition-shadow relative"
      style={{ animationDelay: `${index * 100}ms` }}
      onClick={() => onCardClick(article)}
    >
      {/* Time Info */}
      <div className="absolute top-4 right-4 flex items-center gap-1 text-xs text-muted-foreground">
        <Clock className="w-3.5 h-3.5" />
        <span>{formatDate(article.publishedAt)}</span>
      </div>

      <div className="flex items-center gap-2 mb-3">
        <span className={`category-badge ${categoryColors[article.category]}`}>
          {categoryNames[article.category]}
        </span>
      </div>

      <h2 className="text-lg font-bold text-foreground mb-3">{article.title}</h2>

      <div className="bg-primary/20 rounded-2xl p-4">
        <p className="text-sm font-medium text-foreground mb-2">📝 한 줄 요약</p>
        <p className="text-foreground text-sm line-clamp-3">{article.summary}</p>
      </div>
    </article>
  );
};

export default CategoriesPage;
