import React, { useState, useEffect } from 'react';
import { Newspaper, Check, ArrowRight, ArrowLeft, Loader2 } from 'lucide-react';
import { categoryNames, categoryColors } from '@/data/category_constants';
import { useUserStore } from '@/store/userStore';
import { Category, NewsArticle } from '@/types';
import { fetchOnboardingNews, updateUserNewsletters } from '@/lib/api';
import { mapCategoryKeyToCode, mapCategoryIdToKey } from '@/lib/utils';
import { toast } from "sonner";

interface OnboardingProps {
  onComplete: () => void;
}
const allCategories = Object.keys(categoryNames) as Category[];

const Onboarding: React.FC<OnboardingProps> = ({ onComplete }) => {
  const [step, setStep] = useState<'categories' | 'articles'>('categories');
  const [selectedCategories, setSelectedCategories] = useState<Category[]>([]);
  const [currentCategoryIndex, setCurrentCategoryIndex] = useState(0);
  const [selectedArticles, setSelectedArticles] = useState<Record<Category, string[]>>({} as Record<Category, string[]>);
  const { completeOnboarding, accessToken } = useUserStore();

  const [loadedArticles, setLoadedArticles] = useState<Record<string, NewsArticle[]>>({});
  const [isLoading, setIsLoading] = useState(false);

  const handleCategoryToggle = (category: Category) => {
    setSelectedCategories((prev) =>
      prev.includes(category)
        ? prev.filter((c) => c !== category)
        : [...prev, category]
    );
  };

  const handleCategoryNext = () => {
    if (selectedCategories.length > 0) {
      // 카테고리 별 뉴스레터
      const initialArticles: Record<Category, string[]> = {} as Record<Category, string[]>;
      selectedCategories.forEach((cat) => {
        initialArticles[cat] = [];
      });
      setSelectedArticles(initialArticles);
      setStep('articles');
    }
  };

  useEffect(() => {
    const fetchArticles = async () => {
      if (step !== 'articles') return;

      const currentCategory = selectedCategories[currentCategoryIndex];
      if (!currentCategory) return;

      if (loadedArticles[currentCategory]) return;

      setIsLoading(true);
      try {
        const code = mapCategoryKeyToCode(currentCategory);
        const data = await fetchOnboardingNews(code);

        const mapped: NewsArticle[] = data.map(item => ({
          id: item.news_letter_id.toString(),
          title: item.news_letter_title,
          summary: item.news_letter_sentence,
          context: "context",
          facts: [],
          category: currentCategory,
          keywords: item.news_letter_keywords.map((k, i) => ({ id: `k${i}`, term: k, category: currentCategory, savedAt: new Date() })),
          sourceUrl: "#",
          publishedAt: new Date(item.news_letter_created_at),
          hookingSentence: item.news_letter_sentence
        }));

        setLoadedArticles(prev => ({
          ...prev,
          [currentCategory]: mapped
        }));

      } catch (error) {
        console.error(error);
        toast.error("데이터를 불러오지 못했습니다.");
      } finally {
        setIsLoading(false);
      }
    };

    fetchArticles();
  }, [step, currentCategoryIndex, selectedCategories, loadedArticles]);


  const handleArticleToggle = (articleId: string) => {
    const currentCategory = selectedCategories[currentCategoryIndex];
    const currentSelected = selectedArticles[currentCategory] || [];

    if (currentSelected.includes(articleId)) {
      setSelectedArticles((prev) => ({
        ...prev,
        [currentCategory]: currentSelected.filter((id) => id !== articleId),
      }));
    } else if (currentSelected.length < 3) {
      setSelectedArticles((prev) => ({
        ...prev,
        [currentCategory]: [...currentSelected, articleId],
      }));
    }
  };

  const handleArticleNext = () => {
    if (currentCategoryIndex < selectedCategories.length - 1) {
      setCurrentCategoryIndex((prev) => prev + 1);
    } else {
      const allSelectedNewsletterIds = Object.values(selectedArticles)
        .flat()
        .map(id => parseInt(id, 10))
        .filter(id => !isNaN(id));

      if (accessToken) {
        updateUserNewsletters(allSelectedNewsletterIds, accessToken)
          .then(() => {
            completeOnboarding(selectedCategories);
            onComplete();
          })
          .catch(err => {
            console.error("Failed to save newsletters", err);
            toast.error("뉴스레터 저장 중 오류가 발생했습니다.");
            completeOnboarding(selectedCategories);
            onComplete();
          });
      } else {
        completeOnboarding(selectedCategories);
        onComplete();
      }
    }
  };

  const handleArticlePrev = () => {
    if (currentCategoryIndex > 0) {
      setCurrentCategoryIndex((prev) => prev - 1);
    } else {
      setStep('categories');
    }
  };

  const getArticlesForCategory = (category: Category): NewsArticle[] => {
    return loadedArticles[category] || [];
  };

  const currentCategory = selectedCategories[currentCategoryIndex];
  const categoryArticles = currentCategory ? getArticlesForCategory(currentCategory) : [];
  const currentSelectedCount = currentCategory ? (selectedArticles[currentCategory]?.length || 0) : 0;

  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-2xl">
        {/* Header */}
        <div className="text-center mb-8 animate-fade-in">
          <div className="inline-flex items-center justify-center w-20 h-20 rounded-[2rem] mb-5 overflow-hidden">
            <img src="/favicon.ico" alt="Logo" className="w-full h-full object-cover scale-105" />
          </div>
          {step === 'categories' ? (
            <>
              <h1 className="text-2xl font-bold text-foreground mb-2">관심 분야를 선택해주세요</h1>
              <p className="text-muted-foreground">
                어떤 뉴스에 관심이 있으신가요? (복수 선택 가능)
              </p>
            </>
          ) : (
            <>
              <h1 className="text-2xl font-bold text-foreground mb-2">
                <span className={`${categoryColors[currentCategory]} px-3 py-1 rounded-xl text-sm mr-2`}>
                  {categoryNames[currentCategory]}
                </span>
                관심있는 뉴스를 선택해주세요
              </h1>
              <p className="text-muted-foreground">
                최대 3개까지 선택할 수 있어요 ({currentSelectedCount}/3)
              </p>
            </>
          )}
        </div>

        {step === 'articles' && (
          <div className="flex gap-2 mb-6">
            {selectedCategories.map((_, index) => (
              <div
                key={index}
                className={`h-1.5 flex-1 rounded-full transition-colors duration-300 ${index <= currentCategoryIndex ? 'bg-primary' : 'bg-muted'
                  }`}
              />
            ))}
          </div>
        )}

        {/* 1: 관심 분야 선택 */}
        {step === 'categories' && (
          <div className="animate-fade-in">
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 mb-8">
              {allCategories.map((category) => {
                const isSelected = selectedCategories.includes(category);
                return (
                  <button
                    key={category}
                    onClick={() => handleCategoryToggle(category)}
                    className={`relative p-6 rounded-3xl border-2 transition-all duration-300 ${isSelected
                      ? 'border-primary bg-primary/10 shadow-lg scale-105'
                      : 'border-border bg-card hover:border-primary/50 hover:shadow-md'
                      }`}
                  >
                    {isSelected && (
                      <div className="absolute top-3 right-3 w-6 h-6 bg-primary rounded-full flex items-center justify-center">
                        <Check className="w-4 h-4 text-primary-foreground" />
                      </div>
                    )}
                    <div className={`inline-block ${categoryColors[category]} px-3 py-1 rounded-xl text-sm font-medium mb-2`}>
                      {categoryNames[category]}
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {getCategoryDescription(category)}
                    </p>
                  </button>
                );
              })}
            </div>

            <button
              onClick={handleCategoryNext}
              disabled={selectedCategories.length === 0}
              className={`w-full py-4 rounded-2xl font-semibold text-lg flex items-center justify-center gap-2 transition-all duration-200 ${selectedCategories.length > 0
                ? 'bg-primary text-primary-foreground hover:opacity-90'
                : 'bg-muted text-muted-foreground cursor-not-allowed'
                }`}
            >
              다음 단계로 <ArrowRight className="w-5 h-5" />
            </button>


          </div>
        )}

        {/* 2: 관심 분야 뉴스레터 선택 */}
        {step === 'articles' && currentCategory && (
          <div className="animate-fade-in">
            <div className="mb-8">
              {isLoading ? (
                <div className="flex justify-center py-20">
                  <Loader2 className="w-8 h-8 animate-spin text-primary" />
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  {categoryArticles.slice(0, 6).map((article) => {
                    const isSelected = selectedArticles[currentCategory]?.includes(article.id);
                    return (
                      <button
                        key={article.id}
                        onClick={() => handleArticleToggle(article.id)}
                        disabled={!isSelected && currentSelectedCount >= 3}
                        className={`p-5 rounded-3xl border-2 text-left transition-all duration-200 h-full flex flex-col ${isSelected
                          ? 'border-primary bg-primary/10 shadow-md'
                          : currentSelectedCount >= 3
                            ? 'border-border bg-muted/50 opacity-50 cursor-not-allowed'
                            : 'border-border bg-card hover:border-primary/50 hover:shadow-sm'
                          }`}
                      >
                        <div className="flex items-start justify-between gap-2 mb-3">
                          <h4 className="font-bold text-foreground text-sm leading-tight line-clamp-2">
                            {article.title}
                          </h4>
                          {isSelected && (
                            <div className="flex-shrink-0 w-5 h-5 bg-primary rounded-full flex items-center justify-center">
                              <Check className="w-3 h-3 text-primary-foreground" />
                            </div>
                          )}
                        </div>

                        <p className="text-xs text-stone-600 font-bold mb-3 leading-relaxed">
                          {article.hookingSentence}
                        </p>

                        <div className="flex flex-wrap gap-1 mt-auto">
                          {article.keywords.slice(0, 3).map((kw) => (
                            <span
                              key={kw.id}
                              className="text-[10px] bg-background border border-border px-2 py-1 rounded-full text-muted-foreground"
                            >
                              #{kw.term}
                            </span>
                          ))}
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Navigation */}
            <div className="flex gap-4">
              <button
                onClick={handleArticlePrev}
                className="flex-1 py-4 rounded-2xl font-semibold border-2 border-border text-foreground hover:bg-muted transition-all duration-200 flex items-center justify-center gap-2"
              >
                <ArrowLeft className="w-5 h-5" /> 이전
              </button>
              <button
                onClick={handleArticleNext}
                className="flex-1 py-4 rounded-2xl font-semibold bg-primary text-primary-foreground hover:opacity-90 transition-all duration-200 flex items-center justify-center gap-2"
              >
                {currentCategoryIndex < selectedCategories.length - 1 ? (
                  <>다음 분야 <ArrowRight className="w-5 h-5" /></>
                ) : (
                  <>시작하기 <ArrowRight className="w-5 h-5" /></>
                )}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

function getCategoryDescription(category: Category): string {
  const descriptions: Record<Category, string> = {
    politics: '국회, 정부, 선거 소식',
    economy: '금리, 주식, 부동산 이야기',
    it: 'AI, 테크, 물리, 화학',
    society: '일상 속 사회 이슈',
    culture: '문화, 라이프스타일',
    sports: '배구, 축구, 야구, 농구',
    world: '글로벌 뉴스와 트렌드',
  };
  return descriptions[category];
}

export default Onboarding;
