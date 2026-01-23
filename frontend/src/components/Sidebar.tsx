import React from 'react';
import {
  Newspaper,
  Layers,
  BookOpen,
  BarChart3,
  User,
  Menu,
  X
} from 'lucide-react';

export type Page = 'home' | 'categories' | 'profile';

interface SidebarProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
  isMobileOpen: boolean;
  onMobileToggle: () => void;
}

const menuItems: { id: Page; label: string; icon: React.ElementType }[] = [
  { id: 'home', label: '오늘의 이슈', icon: Newspaper },
  { id: 'categories', label: '분야별 뉴스', icon: Layers },
  { id: 'profile', label: '프로필 설정', icon: User },
];

const Sidebar: React.FC<SidebarProps> = ({
  currentPage,
  onPageChange,
  isMobileOpen,
  onMobileToggle,
}) => {
  return (
    <>
      {/* Desktop Top Navigation Bar */}
      <header className="hidden lg:flex fixed top-0 left-0 right-0 h-16 bg-background border-b border-border z-40 items-center justify-between px-8">
        <div
          className="flex items-center gap-3 cursor-pointer"
          onClick={() => onPageChange('home')}
        >
          <div className="w-10 h-10 bg-primary rounded-xl flex items-center justify-center">
            <Newspaper className="w-5 h-5 text-primary-foreground" />
          </div>
          <span className="font-bold text-foreground text-lg">News-Grow</span>
        </div>

        <nav className="flex items-center gap-1">
          {menuItems.map((item) => (
            <button
              key={item.id}
              onClick={() => onPageChange(item.id)}
              className={`px-4 py-2 rounded-xl text-sm font-medium transition-all duration-200 flex items-center gap-2 ${currentPage === item.id
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground hover:bg-secondary'
                }`}
            >
              <item.icon className="w-4 h-4" />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
      </header>

      {/* Mobile Header */}
      <header className="lg:hidden fixed top-0 left-0 right-0 h-16 bg-sidebar border-b border-sidebar-border z-40 flex items-center px-4">
        <button
          onClick={onMobileToggle}
          className="w-10 h-10 rounded-xl bg-secondary flex items-center justify-center mr-3"
        >
          {isMobileOpen ? (
            <X className="w-5 h-5 text-foreground" />
          ) : (
            <Menu className="w-5 h-5 text-foreground" />
          )}
        </button>
        <div
          className="flex items-center gap-3 cursor-pointer"
          onClick={() => onPageChange('home')}
        >
          <div className="w-10 h-10 bg-primary rounded-xl flex items-center justify-center">
            <Newspaper className="w-5 h-5 text-primary-foreground" />
          </div>
          <span className="font-bold text-foreground">News-Grow</span>
        </div>
      </header>

      {/* Mobile Overlay */}
      {isMobileOpen && (
        <div
          className="lg:hidden fixed inset-0 bg-foreground/20 backdrop-blur-sm z-40"
          onClick={onMobileToggle}
        />
      )}

      {/* Mobile Sidebar */}
      <aside
        className={`lg:hidden fixed top-0 left-0 h-full w-64 bg-sidebar border-r border-sidebar-border z-50 transition-transform duration-300 ${isMobileOpen ? 'translate-x-0' : '-translate-x-full'
          }`}
      >
        {/* Logo */}
        <div className="h-20 flex items-center gap-3 px-6 border-b border-sidebar-border">
          <div className="w-11 h-11 bg-primary rounded-2xl flex items-center justify-center">
            <Newspaper className="w-6 h-6 text-primary-foreground" />
          </div>
          <div>
            <h1 className="font-bold text-foreground">News-Grow</h1>
            <p className="text-xs text-muted-foreground">뉴스와 함께 성장</p>
          </div>
        </div>

        {/* Navigation */}
        <nav className="p-4 space-y-2">
          {menuItems.map((item) => (
            <button
              key={item.id}
              onClick={() => {
                onPageChange(item.id);
                onMobileToggle();
              }}
              className={`sidebar-link w-full ${currentPage === item.id ? 'active' : ''}`}
            >
              <item.icon className="w-5 h-5" />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        {/* Footer */}
        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-sidebar-border">
          <p className="text-xs text-muted-foreground text-center">
            © 2025 News-Grow
          </p>
        </div>
      </aside>
    </>
  );
};

export default Sidebar;
