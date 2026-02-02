import { NewsDetailResponse, TodayNewsResponse, OnboardingNewsResponse, SignupRequest, AuthResponse, LoginRequest, LoginResponse, UserProfileResponse, LogResponse } from "@/types";

// Base URL
const BASE_URL = "/api";

// 임시 유저 ID
const TEMP_USER_ID = "1";

const getToken = (): string | null => {
  try {
    const storage = localStorage.getItem('news-grow-user-v3');
    if (!storage) return null;
    const { state } = JSON.parse(storage);
    return state.accessToken || state.token || null;
  } catch {
    return null;
  }
};
const getHeaders = (token?: string) => {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const effectiveToken = token || getToken();
  if (effectiveToken) {
    headers["Authorization"] = `Bearer ${effectiveToken}`;
  } else {
    headers["x-user-id"] = TEMP_USER_ID;
  }
  return headers;
};

// --- API ---

// 1. 오늘의 뉴스레터
export async function fetchTodayNews(): Promise<TodayNewsResponse[]> {
  const response = await fetch(`${BASE_URL}/newsletters/today`, {
    method: "GET",
    headers: getHeaders(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch today's news: ${response.status}`);
  }

  return response.json();
}

// 2. 뉴스레터 상세
export async function fetchNewsletterDetail(id: number): Promise<NewsDetailResponse> {
  const response = await fetch(`${BASE_URL}/newsletters/${id}`, {
    method: "GET",
    headers: getHeaders(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch newsletter details: ${response.status}`);
  }

  return response.json();
}

// 3. 온보딩 뉴스레터
// categoryCode: 100(Politics), 200(Economy), ...
export async function fetchOnboardingNews(categoryCode: number): Promise<OnboardingNewsResponse[]> {
  const response = await fetch(`${BASE_URL}/onboarding/news?category=${categoryCode}`, {
    method: "GET",
    headers: getHeaders(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch onboarding news: ${response.status}`);
  }

  return response.json();
}

// 4. 회원가입
export async function registerUser(data: SignupRequest): Promise<AuthResponse> {
  const response = await fetch(`${BASE_URL}/auth/signup`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    let errorMessage = "Failed to register";
    try {
      const errorData = await response.json();
      errorMessage = errorData.detail || errorMessage;
    } catch (e) {
      console.error("No error details from server");
    }
    throw new Error(errorMessage);
  }

  return response.json();
}

// 5. 로그인
export async function loginUser(data: LoginRequest): Promise<LoginResponse> {
  const response = await fetch(`${BASE_URL}/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    let errorMessage = "Failed to login";
    try {
      const errorData = await response.json();
      errorMessage = errorData.detail || errorMessage;
    } catch (e) {
      console.error("No error details from server");
    }
    throw new Error(errorMessage);
  }

  return response.json();
}

// 6. 관심 카테고리 업데이트
export async function updateUserCategories(categories: number[], token: string): Promise<any> {
  const response = await fetch(`${BASE_URL}/users/me/categories`, {
    method: "PUT",
    headers: getHeaders(token),
    body: JSON.stringify({ categories }),
  });

  if (!response.ok) {
    throw new Error(`Failed to update categories: ${response.status}`);
  }

  return response.json();
}

// 7. 유저 프로필 조회
export async function fetchUserProfile(token: string): Promise<UserProfileResponse> {
  const response = await fetch(`${BASE_URL}/users/me`, {
    method: "GET",
    headers: getHeaders(token),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch user profile: ${response.status}`);
  }

  return response.json();
}

// 8. 회원가입 - 관심 뉴스레터 선택
export async function updateUserNewsletters(newsLetterIds: number[], token: string): Promise<any> {
  const response = await fetch(`${BASE_URL}/users/me/newsletters`, {
    method: "PUT",
    headers: getHeaders(token),
    body: JSON.stringify({ news_letter_ids: newsLetterIds }),
  });

  if (!response.ok) {
    throw new Error(`Failed to update newsletters: ${response.status}`);
  }

  return response.json();
}

// 9. 뉴스레터 클릭 로그 전송
export async function sendNewsletterClickLog(newsLetterId: number): Promise<LogResponse> {
  const token = getToken();
  // 로그인은 필수지만, 토큰이 없으면 전송하지 않음 (Silent Fail)
  if (!token) {
    return { status: "fail", log_id: -1 };
  }

  const response = await fetch(`${BASE_URL}/logs/newsletter/click`, {
    method: "POST",
    headers: getHeaders(token),
    body: JSON.stringify({ news_letter_id: newsLetterId }),
  });

  if (!response.ok) {
    throw new Error(`Failed to send log: ${response.status}`);
  }

  return response.json();
}
