import { NewsDetailResponse, TodayNewsResponse, OnboardingNewsResponse, SignupRequest, AuthResponse } from "@/types";

// Base URL handled by Vite Proxy (/api -> backend)
const BASE_URL = "/api";

// Temporary User ID for Dev/Demo
const TEMP_USER_ID = "1";

/**
 * Common headers with Auth injection
 * // TODO: Replace x-user-id with Authorization: Bearer <token> later
 */
const getHeaders = () => ({
  "Content-Type": "application/json",
  "x-user-id": TEMP_USER_ID,
});

// --- API Functions ---

// 1. Get Today's Personalized News
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

// 2. Get Newsletter Detail
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

// 3. Get Onboarding News by Category
// categoryCode: 100(Politics), 200(Economy), etc.
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

// 4. User Signup
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
