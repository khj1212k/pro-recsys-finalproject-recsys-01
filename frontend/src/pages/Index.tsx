import React, { useState } from 'react';
import { useUserStore } from '@/store/userStore';
import AuthScreen from '@/components/AuthScreen';
import Onboarding from '@/components/Onboarding';
import Sidebar, { Page } from '@/components/Sidebar';
import HomePage from '@/components/pages/HomePage';
import CategoriesPage from '@/components/pages/CategoriesPage';
import ProfilePage from '@/components/pages/ProfilePage';

const Index = () => {
  const { isLoggedIn, hasCompletedOnboarding } = useUserStore();
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [currentPage, setCurrentPage] = useState<Page>('home');
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);

  // 로그인
  if (!isLoggedIn) {
    return <AuthScreen onAuthComplete={(shouldOnboard) => {
      setShowOnboarding(shouldOnboard);
      setCurrentPage('home');
    }} />;
  }

  //  온보딩 뉴스레터
  if (showOnboarding && !hasCompletedOnboarding) {
    return <Onboarding onComplete={() => setShowOnboarding(false)} />;
  }

  const renderPage = () => {
    switch (currentPage) {
      case 'home':
        return <HomePage />;
      case 'categories':
        return <CategoriesPage />;
      case 'profile':
        return <ProfilePage onLogout={() => { }} />;
      default:
        return <HomePage />;
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Sidebar
        currentPage={currentPage}
        onPageChange={setCurrentPage}
        isMobileOpen={isMobileMenuOpen}
        onMobileToggle={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
      />

      <main className="lg:pt-16 pt-20 min-h-screen">
        <div className="max-w-7xl mx-auto p-4 lg:p-8">
          {renderPage()}
        </div>
      </main>
    </div>
  );
};

export default Index;
