import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import BottomNav from './BottomNav';
import Sidebar from './Sidebar';
import LanguageSwitcher from './LanguageSwitcher';
import { faceLogout, type FaceAuthUser } from '../lib/face-auth';
import { LogOut, Settings } from 'lucide-react';

interface LayoutProps {
  faceUser?: FaceAuthUser | null;
}

export default function Layout({ faceUser }: LayoutProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const isLoginPage = location.pathname === '/login' || location.pathname === '/register';

  const handleLogout = () => {
    faceLogout();
    localStorage.removeItem('onboarding_complete');
    localStorage.removeItem('onboarding_family_done');
    window.location.href = '/login';
  };

  if (isLoginPage) {
    return <Outlet />;
  }

  return (
    <div className="min-h-screen bg-[#F8F9FA]">
      {/* Desktop Sidebar */}
      <Sidebar faceUser={faceUser} />

      {/* Main Content Area */}
      <div className="lg:ml-64 min-h-screen flex flex-col">
        {/* Mobile Header */}
        <header className="lg:hidden sticky top-0 z-50 bg-white/90 backdrop-blur-xl border-b border-gray-100 safe-area-top">
          <div className="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <img src="/healsmart-mark.png" alt="HealSmart" className="w-7 h-7 rounded-lg object-contain" />
              <span className="font-semibold text-gray-900">MedAiCarePlus</span>
            </div>

            <div className="flex items-center gap-3">
              <LanguageSwitcher />
              {faceUser && (
                <>
                  <button
                    onClick={() => navigate('/settings')}
                    className="p-2 text-gray-500 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors touch-target"
                    title={t('settings.title')}
                  >
                    <Settings className="w-5 h-5" />
                  </button>
                  <button
                    onClick={handleLogout}
                    className="p-2 text-gray-500 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors touch-target"
                    title={t('common.logout')}
                  >
                    <LogOut className="w-5 h-5" />
                  </button>
                </>
              )}
            </div>
          </div>
        </header>

        {/* Page Content */}
        <main className="flex-1 px-4 py-6 pb-28 lg:px-6 lg:py-8 lg:pb-8 max-w-5xl mx-auto w-full">
          <Outlet />
        </main>

        {/* Mobile Bottom Navigation */}
        <BottomNav />
      </div>
    </div>
  );
}
