import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  LayoutDashboard,
  Pill,
  Camera,
  History,
  User,
} from 'lucide-react';

export default function BottomNav() {
  const { t } = useTranslation();

  const navItems = [
    { to: '/dashboard', icon: LayoutDashboard, label: t('dashboard.title') },
    { to: '/medications', icon: Pill, label: t('medications.title') },
    { to: '/history', icon: History, label: t('history.title') },
    { to: '/family', icon: User, label: t('nav.profile') },
  ];

  return (
    <nav className="lg:hidden fixed bottom-0 left-0 right-0 bg-white border-t border-gray-100 z-50 safe-area-bottom">
      <div className="flex justify-around items-end px-2 pt-2 pb-safe">
        {/* Left nav items */}
        {navItems.slice(0, 2).map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            title={label}
            className={({ isActive }) =>
              `flex flex-col items-center justify-center gap-1 min-w-[64px] min-h-[64px] rounded-xl transition-all duration-200 ${
                isActive
                  ? 'text-[#0057B8]'
                  : 'text-gray-400 hover:text-gray-600'
              }`
            }
          >
            <Icon className="w-7 h-7" />
          </NavLink>
        ))}

        {/* Center Scan Button */}
        <NavLink
          to="/scan"
          title={t('scan.title')}
          className={({ isActive }) =>
            `flex flex-col items-center justify-center min-w-[64px] min-h-[64px] -mt-5 ${isActive ? 'text-[#0057B8]' : 'text-gray-600'}`
          }
        >
          <div className="w-14 h-14 bg-[#0057B8] rounded-full flex items-center justify-center shadow-fab hover:bg-[#003D82] active:scale-95 transition-all">
            <Camera className="w-7 h-7 text-white" />
          </div>
        </NavLink>

        {/* Right nav items */}
        {navItems.slice(2).map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            title={label}
            className={({ isActive }) =>
              `flex flex-col items-center justify-center gap-1 min-w-[64px] min-h-[64px] rounded-xl transition-all duration-200 ${
                isActive
                  ? 'text-[#0057B8]'
                  : 'text-gray-400 hover:text-gray-600'
              }`
            }
          >
            <Icon className="w-7 h-7" />
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
