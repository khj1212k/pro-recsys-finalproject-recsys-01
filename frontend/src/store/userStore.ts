import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { User, Keyword, Category, QuizScore } from '@/types';
import { registerUser, loginUser, updateUserCategories } from '@/lib/api';
import { mapCategoryKeyToCode } from '@/lib/utils';

interface UserState {
  user: User | null;
  currentUserEmail: string | null;
  users: Record<string, User>;
  isLoggedIn: boolean;
  accessToken: string | null;
  hasCompletedOnboarding: boolean;

  login: (email: string, password?: string) => Promise<boolean>;
  register: (nickname: string, email: string, gender: 'male' | 'female' | 'none', birthYear: number, password: string) => Promise<boolean>;
  logout: () => void;
  updateNickname: (nickname: string) => void;
  completeOnboarding: (interests: Category[]) => Promise<void>;
  addInterest: (interest: Category) => void;
  removeInterest: (interest: Category) => void;
  saveKeyword: (keyword: Keyword) => void;
  removeKeyword: (keywordId: string) => void;
  addReadArticle: (articleId: string) => void;
  addQuizScore: (score: QuizScore) => void;
  getReadingStats: () => { category: Category; count: number }[];
  updateCategories: (categories: Category[]) => Promise<void>;
}

export const useUserStore = create<UserState>()(
  persist(
    (set, get) => ({
      user: null,
      currentUserEmail: null,
      users: {},
      isLoggedIn: false,
      accessToken: null,
      hasCompletedOnboarding: false,

      login: async (email: string, password?: string) => {
        try {
          if (password) {
            const response = await loginUser({ email, password });
            const user: User = {
              nickname: response.user_nickname,
              interests: [],
              savedKeywords: [],
              readArticles: [],
              quizScores: [],
              gender: 'none',
              birthYear: undefined
            };

            set((state) => ({
              user: user,
              currentUserEmail: email,
              isLoggedIn: true,
              accessToken: response.access_token,
              hasCompletedOnboarding: user.interests.length > 0
            }));
            return true;
          }

          const { users } = get();
          const existingUser = users[email];
          if (existingUser) {
            set({
              user: existingUser,
              currentUserEmail: email,
              isLoggedIn: true,
              accessToken: "demo-token",
              hasCompletedOnboarding: existingUser.interests.length > 0,
            });
            return true;
          }
          return false;
        } catch (error) {
          console.error("Login failed:", error);
          return false;
        }
      },

      register: async (nickname: string, email: string, gender: 'male' | 'female' | 'none', birthYear: number, password: string) => {
        try {
          const genderMap: Record<string, 'Male' | 'Female' | 'Not specified'> = {
            'male': 'Male',
            'female': 'Female',
            'none': 'Not specified'
          };

          await registerUser({
            email,
            password,
            nickname,
            gender: genderMap[gender],
            birth_year: birthYear
          });
          return true;
        } catch (error) {
          console.error("Registration failed:", error);
          return false;
        }
      },

      logout: () => {
        set({
          user: null,
          currentUserEmail: null,
          isLoggedIn: false,
          accessToken: null,
          hasCompletedOnboarding: false,
        });
      },

      updateNickname: (nickname: string) => {
        set((state) => {
          if (!state.user || !state.currentUserEmail) return state;
          const updatedUser = { ...state.user, nickname };
          return {
            user: updatedUser,
            users: { ...state.users, [state.currentUserEmail]: updatedUser }
          };
        });
      },

      completeOnboarding: async (interests: Category[]) => {
        const { updateCategories } = get();
        await updateCategories(interests);

        set((state) => ({
          hasCompletedOnboarding: true
        }));
      },

      addInterest: (interest: Category) => {
        const { user, updateCategories } = get();
        if (!user) return;
        if (user.interests.includes(interest)) return;

        const updatedInterests = [...user.interests, interest];
        updateCategories(updatedInterests);
      },

      removeInterest: (interest: Category) => {
        const { user, updateCategories } = get();
        if (!user) return;

        const updatedInterests = user.interests.filter(i => i !== interest);
        updateCategories(updatedInterests);
      },

      saveKeyword: (keyword: Keyword) => {
        set((state) => ({
          user: state.user
            ? {
              ...state.user,
              savedKeywords: [...state.user.savedKeywords, { ...keyword, savedAt: new Date() }],
            }
            : null,
          users: (state.user && state.currentUserEmail)
            ? {
              ...state.users,
              [state.currentUserEmail]: {
                ...state.user,
                savedKeywords: [...state.user.savedKeywords, { ...keyword, savedAt: new Date() }],
              },
            }
            : state.users,
        }));
      },

      removeKeyword: (keywordId: string) => {
        set((state) => ({
          user: state.user
            ? {
              ...state.user,
              savedKeywords: state.user.savedKeywords.filter((k) => k.id !== keywordId),
            }
            : null,
          users: (state.user && state.currentUserEmail)
            ? {
              ...state.users,
              [state.currentUserEmail]: {
                ...state.user,
                savedKeywords: state.user.savedKeywords.filter((k) => k.id !== keywordId),
              },
            }
            : state.users,
        }));
      },

      addReadArticle: (articleId: string) => {
        set((state) => ({
          user: state.user
            ? {
              ...state.user,
              readArticles: [...new Set([...state.user.readArticles, articleId])],
            }
            : null,
          users: (state.user && state.currentUserEmail)
            ? {
              ...state.users,
              [state.currentUserEmail]: {
                ...state.user,
                readArticles: [...new Set([...state.user.readArticles, articleId])],
              },
            }
            : state.users,
        }));
      },

      addQuizScore: (score: QuizScore) => {
        set((state) => ({
          user: state.user
            ? {
              ...state.user,
              quizScores: [...state.user.quizScores, score],
            }
            : null,
          users: (state.user && state.currentUserEmail)
            ? {
              ...state.users,
              [state.currentUserEmail]: {
                ...state.user,
                quizScores: [...state.user.quizScores, score],
              },
            }
            : state.users,
        }));
      },

      getReadingStats: () => {
        const user = get().user;
        if (!user) return [];

        const categories: Category[] = ['politics', 'economy', 'it', 'society', 'culture', 'science', 'world'];
        return categories.map((category) => ({
          category,
          count: user.readArticles.filter((id) => id && typeof id === 'string' && id.startsWith(category.slice(0, 3))).length + Math.floor(Math.random() * 10),
        }));
      },

      updateCategories: async (categories: Category[]) => {
        // 1. Optimistic Update
        set((state) => {
          if (!state.user || !state.currentUserEmail) return state;
          const updatedUser = { ...state.user, interests: categories };
          return {
            user: updatedUser,
            users: { ...state.users, [state.currentUserEmail]: updatedUser }
          };
        });

        // 2. API Sync
        const { accessToken } = get();
        if (accessToken) {
          const codes = categories.map(c => mapCategoryKeyToCode(c));
          try {
            await updateUserCategories(codes, accessToken);
          } catch (e) {
            console.error("Failed to sync categories:", e);
          }
        }
      },
    }),
    {
      name: 'news-grow-user-v3',
    }
  )
);
