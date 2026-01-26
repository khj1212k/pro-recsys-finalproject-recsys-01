export interface User {
  nickname: string;
  interests: string[];
  savedKeywords: Keyword[];
  readArticles: string[];
  quizScores: QuizScore[];
  gender?: 'male' | 'female' | 'none';
  birthYear?: number;
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

// --- Backend API Response Types (DTOs) ---

export interface NewsResponse {
  news_letter_id: number;
  news_letter_title: string;
  news_letter_sentence: string;
  news_letter_keywords: string[];
  news_letter_created_at: string;
}

export interface TodayNewsResponse extends NewsResponse {
  category_id: number;
  category_name: string;
}

export interface NewsDetailResponse extends NewsResponse {
  news_letter_content: string;
  category_id: number;
  category_name: string;
}

export interface OnboardingNewsResponse extends NewsResponse { }

// --- Auth DTOs ---

export interface SignupRequest {
  email: string;
  password: string;
  nickname: string;
  gender?: 'Male' | 'Female' | 'Not specified';
  birth_year?: number;
}

export interface AuthResponse {
  user_id: number;
  user_email: string;
  user_nickname: string;
  user_gender_code?: number;
  user_birth_year?: number;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user_id: number;
  user_email: string;
  user_nickname: string;
}


