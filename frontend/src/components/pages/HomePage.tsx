import React, { useState, useEffect } from 'react';
import { Sun, Utensils, Moon, Clock, Check, X } from 'lucide-react';
import { categoryNames, categoryColors } from '@/data/category_constants';
import { useUserStore } from '@/store/userStore';
import { NewsArticle } from '@/types';
import { fetchTodayNews, fetchNewsletterDetail, sendNewsletterClickLog } from '@/lib/api';
import { mapCategoryIdToKey, formatDate } from '@/lib/utils';
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

type TimeSlot = 'morning' | 'lunch' | 'evening' | 'custom';

const timeSlots: { id: TimeSlot; label: string; time: string; icon: React.ElementType }[] = [
  { id: 'morning', label: '아침', time: '08:00', icon: Sun },
  { id: 'lunch', label: '점심', time: '12:00', icon: Utensils },
  { id: 'evening', label: '저녁', time: '18:00', icon: Moon },
  { id: 'custom', label: '추천', time: 'Now', icon: Clock },
];

const HomePage: React.FC = () => {
  const [activeSlot, setActiveSlot] = useState<TimeSlot>('evening');
  const [selectedArticle, setSelectedArticle] = useState<NewsArticle | null>(null);
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const { user, addReadArticle } = useUserStore();

  // Fetch API Data
  useEffect(() => {
    const loadNews = async () => {
      setIsLoading(true);
      try {
        const data = await fetchTodayNews();
        const mappedArticles: NewsArticle[] = data.map(item => ({
          id: item.news_letter_id.toString(),
          title: item.news_letter_title,
          summary: item.news_letter_sentence,
          context: "API Context Placeholder",
          facts: [],
          category: mapCategoryIdToKey(item.category_id),
          keywords: item.news_letter_keywords.map((k, i) => ({ id: `k${i}`, term: k, category: mapCategoryIdToKey(item.category_id), savedAt: new Date() })),
          sourceUrl: "#",
          publishedAt: new Date(item.news_letter_created_at),
          hookingSentence: item.news_letter_sentence,
          raw_news_count: item.raw_news_count
        }));
        setArticles(mappedArticles);
      } catch (error) {
        console.error("Failed to fetch news:", error);
        toast.error("뉴스를 불러오는데 실패했습니다.");
      } finally {
        setIsLoading(false);
      }
    };

    if (activeSlot === 'evening') {
      loadNews();
    } else {
      setArticles([]);
    }
  }, [activeSlot]);

  const handleCardClick = async (article: NewsArticle) => {
    sendNewsletterClickLog(parseInt(article.id)).catch(err => {
      console.error("Failed to log click:", err);
    });

    if (!article.fullContent) {
      try {
        const detail = await fetchNewsletterDetail(parseInt(article.id));
        const detailedArticle = {
          ...article,
          fullContent: detail.news_letter_content,
          raw_news_count: detail.raw_news_count,
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

  const cleanNickname = (nickname: string) => {
    return nickname.replace(/_T\d+$/, '');
  };

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="mb-8">
        <h2 className="text-2xl lg:text-3xl font-bold text-foreground mb-2">
          {user?.nickname ? `반가워요, ${cleanNickname(user.nickname)}님! 👋` : '반가워요! 👋'}
        </h2>
        <p className="text-muted-foreground">궁금해하실 만한 이야기들을 모아왔어요 🙌</p>
      </div>

      {/* Time Tabs */}
      <div className="flex gap-3 mb-8 overflow-x-auto pb-2">
        {timeSlots.filter(s => s.id !== 'custom').map((slot) => {
          const isActive = activeSlot === slot.id;
          const isEvening = slot.id === 'evening';

          // 18:00 (Evening Batch)
          if (isEvening) {
            return (
              <button
                key={slot.id}
                onClick={() => setActiveSlot(slot.id)}
                className={`time-tab flex items-center gap-2 whitespace-nowrap transition-all duration-200 
                  ${isActive
                    ? 'active'
                    : 'bg-background border-primary text-primary hover:bg-primary/10'}`}
              >
                <slot.icon className="w-4 h-4" />
                <span>{slot.time}</span>
                <span>{slot.label}</span>
              </button>
            );
          }

          // 08:00 & 12:00 Batch (추후 구현 예정)
          return (
            <button
              key={slot.id}
              onClick={() => setActiveSlot(slot.id)}
              className={`time-tab flex items-center gap-2 whitespace-nowrap transition-all duration-200 
                ${isActive
                  ? '!bg-gray-300 !text-black grayscale-0'
                  : '!bg-gray-200 !text-[#6B7280] border-transparent grayscale hover:!bg-[#D1D5DB]'}`}
            >
              <slot.icon className="w-4 h-4" />
              <span>{slot.time}</span>
              <span>{slot.label}</span>
            </button>
          );
        })}
      </div>

      {/* Issue Cards */}
      {activeSlot === 'evening' ? (
        <div className="min-h-[200px]">
          {isLoading ? (
            <div className="flex justify-center py-20 text-muted-foreground">뉴스를 배달하고 있어요... 🚚</div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {articles.map((article, index) => {
                const isRead = isArticleRead(article.id);

                return (
                  <article
                    key={article.id}
                    onClick={() => handleCardClick(article)}
                    className="card-news animate-slide-up cursor-pointer relative hover:shadow-medium transition-shadow"
                    style={{ animationDelay: `${index * 100}ms` }}
                  >
                    <div className="flex items-start justify-between gap-4 mb-4">
                      <span className={`category-badge ${categoryColors[article.category]}`}>
                        {categoryNames[article.category]}
                      </span>
                    </div>

                    <h2 className="text-lg font-bold text-foreground mb-3">
                      {article.title}
                    </h2>

                    {/* 한 줄 요약 */}
                    <div className="bg-primary/20 rounded-2xl p-4">
                      <p className="text-sm font-medium text-foreground mb-2">📝 한 줄 요약</p>
                      <p className="text-foreground text-sm line-clamp-3">{article.summary}</p>
                    </div>

                    <div className="flex flex-wrap gap-1.5 mt-4">
                      {article.keywords.slice(0, 3).map((kw) => (
                        <span
                          key={kw.id}
                          className="text-xs font-medium text-muted-foreground bg-muted px-2 py-1 rounded-lg"
                        >
                          #{kw.term}
                        </span>
                      ))}
                    </div>
                  </article>
                );
              })}

              {!isLoading && articles.length === 0 && (
                <div className="col-span-2 flex flex-col items-center justify-center py-20 text-center animate-fade-in">
                  <div className="w-20 h-20 bg-secondary rounded-full flex items-center justify-center mb-6">
                    <Clock className="w-10 h-10 text-muted-foreground" />
                  </div>
                  <h3 className="text-xl font-bold text-foreground mb-2">오늘 오후 6시에 배달 예정입니다!</h3>
                  <p className="text-muted-foreground">조금만 기다려주세요 📰</p>
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-20 text-center animate-fade-in">
          <div className="w-20 h-20 bg-secondary rounded-full flex items-center justify-center mb-6">
            <Clock className="w-10 h-10 text-muted-foreground" />
          </div>
          <h3 className="text-xl font-bold text-foreground mb-2">추후 업데이트 예정입니다!</h3>
          <p className="text-muted-foreground">조금만 기다려주세요 🚀</p>
        </div>
      )}

      {/* News Letter Modal */}
      {selectedArticle && (
        <div className="fixed inset-0 bg-foreground/30 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-card rounded-3xl w-full max-w-4xl shadow-medium animate-scale-in my-8 max-h-[90vh] flex flex-col overflow-hidden">
            <div className="flex-1 overflow-y-auto p-6 scroll-smooth">
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
                >
                  <X className="w-4 h-4 text-muted-foreground" />
                </button>
              </div>

              {/* Title & Summary & Keywords */}
              <h1 className="text-3xl font-bold text-foreground mb-8">{selectedArticle.title}</h1>

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
              {selectedArticle.fullContent && (
                <div className="prose prose-lg max-w-none mt-4">
                  <div className="text-foreground leading-relaxed space-y-4">
                    {selectedArticle.fullContent.split('\n').map((line, idx) => {
                      const trimmedLine = line.trimStart();
                      if (trimmedLine.startsWith('### ')) return <h3 key={idx} className="text-xl font-bold mt-8 mb-4">{trimmedLine.replace('### ', '')}</h3>;
                      if (trimmedLine.startsWith('## ')) return <h2 key={idx} className="text-2xl font-bold mt-10 mb-6 pb-2 border-b">{trimmedLine.replace('## ', '')}</h2>;
                      if (line.trim() === '') return <br key={idx} />;
                      return <p key={idx} className="text-foreground/90 leading-8 text-[1.05rem]">{trimmedLine}</p>;
                    })}
                  </div>
                </div>
              )}

              {/* Newsletter Info */}
              <div className="w-full border-t border-dashed my-8"></div>
              <div className="flex justify-center mb-4">
                <p className="text-sm text-muted-foreground font-medium bg-secondary/30 px-4 py-2 rounded-full">
                  이 뉴스레터는 {selectedArticle.raw_news_count}개의 뉴스를 참고하여 만들어졌습니다.
                </p>
              </div>
            </div>

            <div className="bg-card px-6 pb-6 pt-2 z-10">
              <div className="border-t border-border mb-4"></div>
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

export default HomePage;


