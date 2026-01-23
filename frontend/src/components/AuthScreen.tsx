import React, { useState } from 'react';
import { Newspaper, User, Mail, Lock, ArrowRight } from 'lucide-react';
import { useUserStore } from '@/store/userStore';

interface AuthScreenProps {
  onAuthComplete: () => void;
}

const AuthScreen: React.FC<AuthScreenProps> = ({ onAuthComplete }) => {
  const [isLogin, setIsLogin] = useState(true);
  const [nickname, setNickname] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const { login, register } = useUserStore();
  const [error, setError] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (isLogin) {
      // Login Logic: Use Email Only
      if (!email.trim()) {
        setError('이메일을 입력해주세요.');
        return;
      }

      const success = login(email.trim());
      if (success) {
        onAuthComplete();
      } else {
        setError('가입되지 않은 이메일입니다. 회원가입을 먼저 진행해주세요.');
      }
    } else {
      // Sign Up Logic: Nickname + Email
      if (!nickname.trim()) {
        setError('별명을 입력해주세요.');
        return;
      }
      if (!email.trim()) {
        setError('이메일을 입력해주세요.');
        return;
      }

      const success = register(nickname.trim(), email.trim());
      if (success) {
        onAuthComplete();
      } else {
        setError('이미 가입된 이메일입니다.');
      }
    }
  };

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-6">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-10 animate-slide-up">
          <div className="inline-flex items-center justify-center w-20 h-20 bg-primary rounded-[2rem] mb-5 shadow-medium">
            <Newspaper className="w-10 h-10 text-primary-foreground" />
          </div>
          <h1 className="text-3xl font-bold text-foreground">News-Grow</h1>
          <p className="text-muted-foreground mt-2">뉴스와 함께 성장하세요</p>
        </div>

        {/* Auth Card */}
        <div className="card-news animate-scale-in">
          {/* Toggle */}
          <div className="flex bg-secondary rounded-2xl p-1 mb-6">
            <button
              onClick={() => setIsLogin(true)}
              className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 ${isLogin ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
                }`}
            >
              로그인
            </button>
            <button
              onClick={() => setIsLogin(false)}
              className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 ${!isLogin ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
                }`}
            >
              회원가입
            </button>
          </div>

          {error && (
            <div className="mb-4 p-3 rounded-lg bg-destructive/10 text-destructive text-sm font-medium text-center animate-shake">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            {!isLogin && (
              <div className="space-y-2">
                <label className="text-sm font-medium text-foreground">별명</label>
                <div className="relative">
                  <User className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
                  <input
                    type="text"
                    value={nickname}
                    onChange={(e) => setNickname(e.target.value)}
                    placeholder="예: 한라봉"
                    className="input-field pl-12"
                    required
                  />
                </div>
              </div>
            )}

            <div className="space-y-2">
              <label className="text-sm font-medium text-foreground">이메일</label>
              <div className="relative">
                <Mail className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="example@email.com"
                  className="input-field pl-12"
                  required
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-foreground">비밀번호</label>
              <div className="relative">
                <Lock className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="input-field pl-12"
                  required
                />
              </div>
            </div>

            <button type="submit" className="btn-primary w-full flex items-center justify-center gap-2 mt-6">
              {isLogin ? '로그인' : '가입하기'}
              <ArrowRight className="w-5 h-5" />
            </button>
          </form>

          {isLogin && (
            <p className="text-center text-sm text-muted-foreground mt-4">
              계정이 없으신가요?{' '}
              <button
                onClick={() => setIsLogin(false)}
                className="text-foreground font-medium hover:underline"
              >
                회원가입
              </button>
            </p>
          )}
        </div>
      </div>
    </div>
  );
};

export default AuthScreen;
