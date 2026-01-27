import React, { useState } from 'react';
import { Newspaper, User, Mail, Lock, ArrowRight, Calendar, ChevronDown } from 'lucide-react';
import { useUserStore } from '@/store/userStore';

interface AuthScreenProps {
  onAuthComplete: (shouldOnboard: boolean) => void;
}

const AuthScreen: React.FC<AuthScreenProps> = ({ onAuthComplete }) => {
  const [isLogin, setIsLogin] = useState(true);
  const [nickname, setNickname] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [gender, setGender] = useState<'male' | 'female' | 'none' | null>(null);
  const [birthYear, setBirthYear] = useState('');
  const [isYearDropdownOpen, setIsYearDropdownOpen] = useState(false);
  const { login, register } = useUserStore();
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (isLogin) {
      // Login Logic: Use Email and Password
      if (!email.trim()) {
        setError('이메일을 입력해주세요.');
        return;
      }
      if (!password.trim()) {
        setError('비밀번호를 입력해주세요.');
        return;
      }

      const success = await login(email.trim(), password.trim());
      if (success) {
        onAuthComplete(false);
      } else {
        setError('로그인에 실패했습니다. 이메일과 비밀번호를 확인해주세요.');
      }
    } else {
      // Sign Up Logic: Nickname + Email + Gender + BirthYear
      if (!nickname.trim()) {
        setError('별명을 입력해주세요.');
        return;
      }
      if (!email.trim()) {
        setError('이메일을 입력해주세요.');
        return;
      }
      if (!password.trim()) {
        setError('비밀번호를 입력해주세요.');
        return;
      }
      if (password.trim().length < 8) {
        setError('비밀번호는 최소 8자 이상이어야 합니다.');
        return;
      }
      if (!gender) {
        setError('성별을 선택해주세요.');
        return;
      }
      if (!birthYear || isNaN(Number(birthYear)) || Number(birthYear) < 1900 || Number(birthYear) > new Date().getFullYear()) {
        setError('올바른 태어난 연도를 입력해주세요.');
        return;
      }

      const success = await register(nickname.trim(), email.trim(), gender, Number(birthYear), password.trim());
      if (success) {
        // Auto-login after registration
        const loginSuccess = await login(email.trim(), password.trim());
        if (loginSuccess) {
          onAuthComplete(true);
        } else {
          setIsLogin(true);
          setError('회원가입 완료! 로그인해주세요.');
        }
      } else {
        setError('이미 가입된 이메일이거나 오류가 발생했습니다.');
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

            {!isLogin && (
              <>
                <div className="space-y-2">
                  <label className="text-sm font-medium text-foreground">성별</label>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setGender('male')}
                      className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 border ${gender === 'male'
                        ? 'bg-primary text-primary-foreground border-primary'
                        : 'bg-background text-muted-foreground border-border hover:bg-secondary'
                        }`}
                    >
                      남
                    </button>
                    <button
                      type="button"
                      onClick={() => setGender('female')}
                      className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 border ${gender === 'female'
                        ? 'bg-primary text-primary-foreground border-primary'
                        : 'bg-background text-muted-foreground border-border hover:bg-secondary'
                        }`}
                    >
                      여
                    </button>
                    <button
                      type="button"
                      onClick={() => setGender('none')}
                      className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 border ${gender === 'none'
                        ? 'bg-primary text-primary-foreground border-primary'
                        : 'bg-background text-muted-foreground border-border hover:bg-secondary'
                        }`}
                    >
                      응답없음
                    </button>
                  </div>
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium text-foreground">태어난 연도</label>
                  <div className="relative">
                    <Calendar className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground pointer-events-none z-10" />
                    <button
                      type="button"
                      onClick={() => setIsYearDropdownOpen(!isYearDropdownOpen)}
                      className={`input-field pl-12 pr-10 w-full text-left relative flex items-center ${!birthYear ? 'text-muted-foreground' : 'text-foreground'}`}
                    >
                      {birthYear ? `${birthYear}년` : '태어난 연도를 선택해주세요'}
                      <ChevronDown className={`absolute right-4 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground transition-transform duration-200 ${isYearDropdownOpen ? 'rotate-180' : ''}`} />
                    </button>

                    {/* Custom Dropdown Menu */}
                    {isYearDropdownOpen && (
                      <div className="absolute top-full left-0 right-0 mt-2 bg-card border border-border rounded-xl shadow-lg max-h-60 overflow-y-auto z-50 animate-fade-in scrollbar-hide">
                        {Array.from({ length: 100 }, (_, i) => new Date().getFullYear() - i).map((year) => (
                          <button
                            key={year}
                            type="button"
                            onClick={() => {
                              setBirthYear(year.toString());
                              setIsYearDropdownOpen(false);
                            }}
                            className={`w-full px-4 py-3 text-left hover:bg-secondary transition-colors text-sm ${birthYear === year.toString() ? 'bg-primary/10 text-primary font-medium' : 'text-foreground'
                              }`}
                          >
                            {year}년
                          </button>
                        ))}
                      </div>
                    )}

                    {/* Backdrop to close dropdown on outside click */}
                    {isYearDropdownOpen && (
                      <div
                        className="fixed inset-0 z-40 bg-transparent"
                        onClick={() => setIsYearDropdownOpen(false)}
                      />
                    )}
                  </div>
                </div>
              </>
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
