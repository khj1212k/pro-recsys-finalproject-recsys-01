import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { User, Keyword, Category, QuizScore } from '@/types';
import { registerUser } from '@/lib/api';

interface UserState {
  user: User | null;
  currentUserEmail: string | null;
  users: Record<string, User>; // Stored users by email
  isLoggedIn: boolean;
  hasCompletedOnboarding: boolean;

  // Actions
  login: (email: string) => boolean;
  register: (nickname: string, email: string, gender: 'male' | 'female' | 'none', birthYear: number, password: string) => Promise<boolean>;
  logout: () => void;
  updateNickname: (nickname: string) => void;
  completeOnboarding: (interests: Category[]) => void;
  addInterest: (interest: Category) => void;
  removeInterest: (interest: Category) => void;
  saveKeyword: (keyword: Keyword) => void;
  removeKeyword: (keywordId: string) => void;
  addReadArticle: (articleId: string) => void;
  addQuizScore: (score: QuizScore) => void;
  getReadingStats: () => { category: Category; count: number }[];
}

export const useUserStore = create<UserState>()(
  persist(
    (set, get) => ({
      user: null,
      currentUserEmail: null,
      users: {}, // DB Initialized (Empty)
      isLoggedIn: false,
      hasCompletedOnboarding: false,

      login: (email: string) => {
        const { users } = get();
        const existingUser = users[email];

        if (existingUser) {
          set({
            user: existingUser,
            currentUserEmail: email,
            isLoggedIn: true,
            hasCompletedOnboarding: existingUser.interests.length > 0,
          });
          return true;
        }
        return false;
      },


      register: async (nickname: string, email: string, gender: 'male' | 'female' | 'none', birthYear: number, password: string) => {
        try {
          // Map frontend gender to backend format
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

          // Registration successful
          // Note: We don't verify if email exists locally anymore, as backend handles it.
          // Note: We don't automatically login here, user needs to login.

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

      completeOnboarding: (interests: Category[]) => {
        set((state) => {
          if (!state.user || !state.currentUserEmail) return state;
          const updatedUser = { ...state.user, interests };
          // IMPORTANT: Update the users map as well to persist changes for next login
          return {
            user: updatedUser,
            users: { ...state.users, [state.currentUserEmail]: updatedUser },
            hasCompletedOnboarding: true,
          };
        });
      },

      addInterest: (interest: Category) => {
        set((state) => {
          if (!state.user || !state.currentUserEmail) return state;
          if (state.user.interests.includes(interest)) return state;
          return {
            user: {
              ...state.user,
              interests: [...state.user.interests, interest],
            },
            users: {
              ...state.users,
              [state.currentUserEmail]: {
                ...state.user,
                interests: [...state.user.interests, interest],
              },
            },
          };
        });
      },

      removeInterest: (interest: Category) => {
        set((state) => {
          if (!state.user || !state.currentUserEmail) return state;
          return {
            user: {
              ...state.user,
              interests: state.user.interests.filter((i) => i !== interest),
            },
            users: {
              ...state.users,
              [state.currentUserEmail]: {
                ...state.user,
                interests: state.user.interests.filter((i) => i !== interest),
              },
            },
          };
        });
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
    }),
    {
      name: 'news-grow-user-v2',
    }
  )
);
