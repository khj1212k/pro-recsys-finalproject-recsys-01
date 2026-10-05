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

// 마지막 "오늘의 뉴스레터" 응답: 서버가 붙인 요청 ID(X-Request-Id)와 화면에 나간 순서.
// 그 목록에서 난 클릭을 보낼 때 같이 실어, 서버가 클릭을 그 응답의 몇 번째 칸인지와 잇게 한다.
// 헤더가 없으면(예전 서버) null로 두고 클릭은 전처럼 news_letter_id만 보낸다.
let lastTodayFeed: { requestId: string; ids: number[] } | null = null;

// 1. 오늘의 뉴스레터
export async function fetchTodayNews(): Promise<TodayNewsResponse[]> {
  const response = await fetch(`${BASE_URL}/newsletters/today`, {
    method: "GET",
    headers: getHeaders(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch today's news: ${response.status}`);
  }

  const data: TodayNewsResponse[] = await response.json();
  const requestId = response.headers.get("X-Request-Id");
  lastTodayFeed = requestId ? { requestId, ids: data.map((item) => item.news_letter_id) } : null;
  return data;
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
export async function fetchOnboardingNews(categoryCode: number, limit: number = 6): Promise<OnboardingNewsResponse[]> {
  const response = await fetch(`${BASE_URL}/onboarding/news?category=${categoryCode}&limit=${limit}`, {
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
// fromTodayFeed: "오늘의 뉴스레터" 목록에서 난 클릭이면 true. 그 목록의 요청 ID와 순위(0부터)를 같이 보낸다.
// 다른 화면(카테고리 등)의 클릭은 그 목록의 노출이 아니므로 붙이지 않는다.
export async function sendNewsletterClickLog(
  newsLetterId: number,
  fromTodayFeed: boolean = false,
): Promise<LogResponse> {
  const token = getToken();
  // 로그인은 필수지만, 토큰이 없으면 전송하지 않음 (Silent Fail)
  if (!token) {
    return { status: "fail", log_id: -1 };
  }

  const payload: { news_letter_id: number; request_id?: string; position?: number } = {
    news_letter_id: newsLetterId,
  };
  if (fromTodayFeed && lastTodayFeed) {
    const position = lastTodayFeed.ids.indexOf(newsLetterId);
    if (position >= 0) {
      payload.request_id = lastTodayFeed.requestId;
      payload.position = position;
    }
  }

  const response = await fetch(`${BASE_URL}/logs/newsletter/click`, {
    method: "POST",
    headers: getHeaders(token),
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Failed to send log: ${response.status}`);
  }

  return response.json();
}
