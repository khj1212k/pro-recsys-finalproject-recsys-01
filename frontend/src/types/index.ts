export interface User {
  nickname: string;
  interests: string[];
  savedKeywords: Keyword[];
  readArticles: string[];
  quizScores: QuizScore[];
}

export interface Keyword {
  id: string;
  term: string;
  category: Category;
  savedAt: Date;
}

export interface QuizScore {
  date: Date;
  type: QuizType;
  score: number;
  total: number;
}

export type Category = 'politics' | 'economy' | 'it' | 'society' | 'culture' | 'science' | 'world';

export type QuizType = 'ox' | 'multiple' | 'short' | 'application';

export interface NewsArticle {
  id: string;
  title: string;
  summary: string;
  context: string;
  facts: string[];
  category: Category;
  keywords: Keyword[];
  sourceUrl: string;
  publishedAt: Date;
  imageUrl?: string;
  fullContent?: string; // 뉴스 레터 형식의 상세 내용
  hookingSentence?: string; // 사용자 흥미 유발 문장
  popularity?: number; // 토픽 인기도 (관련 기사 수 등)
}

export interface IssueBundle {
  id: string;
  timeSlot: 'morning' | 'lunch' | 'evening';
  articles: NewsArticle[];
  date: Date;
}

export interface OnboardingCard {
  id: string;
  category: Category;
  title: string;
  description: string;
  imageUrl?: string;
}

export interface Quiz {
  id: string;
  type: QuizType;
  question: string;
  options?: string[];
  correctAnswer: string | number;
  explanation: string;
  category: Category;
}

export interface ReadingStats {
  daily: CategoryStats[];
  weekly: CategoryStats[];
  monthly: CategoryStats[];
}

export interface CategoryStats {
  category: Category;
  count: number;
  date: Date;
}
