import React, { useState } from 'react';
import { User, LogOut, X, Plus } from 'lucide-react';
import { useUserStore } from '@/store/userStore';
import { Category } from '@/types';
import { categoryNames, categoryColors } from '@/data/category_constants';

interface ProfilePageProps {
  onLogout: () => void;
}

const allCategories: Category[] = ['politics', 'economy', 'it', 'society', 'culture', 'sports', 'world'];

const ProfilePage: React.FC<ProfilePageProps> = ({ onLogout }) => {
  const { user, logout, addInterest, removeInterest } = useUserStore();
  const [showAddInterest, setShowAddInterest] = useState(false);

  const handleLogout = () => {
    logout();
    onLogout();
  };

  const handleAddInterest = (category: Category) => {
    addInterest(category);
    setShowAddInterest(false);
  };

  const handleRemoveInterest = (category: Category) => {
    removeInterest(category);
  };

  const availableCategories = allCategories.filter(
    (cat) => !user?.interests.includes(cat)
  );

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl lg:text-3xl font-bold text-foreground mb-2">프로필 설정</h1>
        <p className="text-muted-foreground">나의 정보를 관리해요</p>
      </div>

      {/* Profile Card */}
      <div className="card-news mb-6">
        <div className="flex items-center gap-4 mb-6">
          <div className="w-16 h-16 bg-primary rounded-2xl flex items-center justify-center">
            <User className="w-8 h-8 text-primary-foreground" />
          </div>
          <div>
            <h2 className="text-lg font-bold text-foreground">{user?.nickname}</h2>
            <p className="text-sm text-muted-foreground">News-Grow 뉴비</p>
          </div>
        </div>
      </div>

      {/* Interests Management */}
      <div className="card-news mb-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-foreground">관심 분야</h3>
          {availableCategories.length > 0 && (
            <button
              onClick={() => setShowAddInterest(!showAddInterest)}
              className="flex items-center gap-2 text-sm text-foreground font-medium hover:text-foreground/80 transition-colors"
            >
              <Plus className="w-4 h-4" />
              추가하기
            </button>
          )}
        </div>

        {/* Selected Interests */}
        {user?.interests && user.interests.length > 0 ? (
          <div className="flex flex-wrap gap-2 mb-4">
            {user.interests.map((interest) => {
              const category = interest as Category;
              return (
                <div
                  key={interest}
                  className={`${categoryColors[category]} px-4 py-2 rounded-xl text-sm font-medium flex items-center gap-2`}
                >
                  <span>{categoryNames[category]}</span>
                  <button
                    onClick={() => handleRemoveInterest(category)}
                    className="hover:bg-white/20 rounded-full p-0.5 transition-colors"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground mb-4">관심 분야를 추가해주세요</p>
        )}

        {/* Interest Dropdown */}
        {showAddInterest && availableCategories.length > 0 && (
          <div className="border-t border-border pt-4">
            <p className="text-sm font-medium text-foreground mb-3">추가할 분야 선택</p>
            <div className="flex flex-wrap gap-2">
              {availableCategories.map((category) => (
                <button
                  key={category}
                  onClick={() => handleAddInterest(category)}
                  className="px-4 py-2 rounded-xl text-sm font-medium bg-secondary text-secondary-foreground hover:bg-muted transition-colors"
                >
                  {categoryNames[category]}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>



      {/* Logout */}
      <button
        onClick={handleLogout}
        className="w-full py-3 rounded-2xl border-2 border-destructive/30 text-destructive font-medium flex items-center justify-center gap-2 hover:bg-destructive/10 transition-colors"
      >
        <LogOut className="w-5 h-5" />
        로그아웃
      </button>
    </div>
  );
};

export default ProfilePage;
