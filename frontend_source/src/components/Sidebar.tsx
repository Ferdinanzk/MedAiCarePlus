import { NavLink, useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  LayoutDashboard,
  Pill,
  Camera,
  History,
  User,
  LogOut,
  Settings,
} from 'lucide-react';
import LanguageSwitcher from './LanguageSwitcher';
import { type FaceAuthUser } from '../lib/face-auth';

interface SidebarProps {
  faceUser?: FaceAuthUser | null;
}

export default function Sidebar({ faceUser }: SidebarProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();

  const navItems = [
    { to: '/dashboard', icon: LayoutDashboard, label: t('dashboard.title') },
    { to: '/medications', icon: Pill, label: t('medications.title') },
    { to: '/scan', icon: Camera, label: t('scan.title') },
    { to: '/history', icon: History, label: t('history.title') },
    { to: '/family', icon: User, label: t('family.title') },
    { to: '/settings', icon: Settings, label: t('settings.title') },
  ];

  return (
    <aside className="hidden lg:flex w-64 flex-col h-screen bg-white border-r border-gray-200 fixed left-0 top-0 z-40">
      {/* Logo Header */}
      <div className="h-16 flex items-center justify-between px-6 border-b border-gray-100">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-[#0057B8] rounded-lg flex items-center justify-center">
            <span className="text-white font-bold text-sm">M+</span>
          </div>
          <span className="font-bold text-gray-900 text-lg">MedAiCarePlus</span>
        </div>
        <div className="flex items-center">
          <LanguageSwitcher />
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-3 space-y-1">
        {navItems.map(({ to, icon: Icon, label }) => {
          const isActive =
            location.pathname === to || location.pathname.startsWith(to + '/');
          return (
            <NavLink
              key={to}
              to={to}
              onClick={(e) => {
                if (location.pathname !== to) {
                  e.preventDefault();
                  navigate(to);
                }
              }}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-xl transition-all duration-200 ${
                isActive
                  ? 'bg-blue-50 text-[#0057B8] font-semibold'
                  : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
              }`}
            >
              <div
                className={`w-8 h-8 rounded-lg flex items-center justify-center ${
                  isActive
                    ? 'bg-[#0057B8] text-white'
                    : 'bg-gray-100 text-gray-500'
                }`}
              >
                <Icon className="w-4 h-4" />
              </div>
              <span className="text-sm">{label}</span>
              {isActive && (
                <div className="ml-auto w-1.5 h-1.5 rounded-full bg-[#0057B8]" />
              )}
            </NavLink>
          );
        })}
      </nav>

      {/* Bottom Section */}
      <div className="p-3 border-t border-gray-100 space-y-2">
        {/* User Profile Card */}
        {faceUser && (
          <div className="flex items-center gap-3 px-3 py-3 rounded-xl bg-blue-50/60 border border-blue-100/50">
            {/* Avatar with Initial */}
            <div
              className="w-10 h-10 rounded-full bg-[#0057B8] flex items-center justify-center text-white font-bold text-base shrink-0 shadow-sm"
            >
              {(faceUser?.name || 'U').charAt(0).toUpperCase()}
            </div>

            {/* User Name + Label */}
            <div className="flex flex-col min-w-0 flex-1">
              <span
                className="text-base font-semibold text-gray-900 truncate leading-tight"
                title={faceUser?.name || 'User'}
              >
                {faceUser?.name || 'User'}
              </span>
              <span className="text-2xs text-gray-500 leading-tight">{t('common.loggedInAs')}</span>
            </div>
          </div>
        )}

        {/* Logout Button */}
        <button
          onClick={() => {
            localStorage.clear();
            window.location.href = '/login';
          }}
          className="flex items-center gap-3 px-3 py-2.5 rounded-xl text-gray-500 hover:bg-red-50 hover:text-red-600 transition-all w-full"
        >
          <div className="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center">
            <LogOut className="w-4 h-4" />
          </div>
          <span className="text-sm">{t('common.logout')}</span>
        </button>
      </div>
    </aside>
  );
}
