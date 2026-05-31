import type { LucideIcon } from 'lucide-react';

interface ServiceCardProps {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  badge?: string;
  color: string;
  onClick: () => void;
}

export default function ServiceCard({ icon: Icon, title, subtitle, badge, color, onClick }: ServiceCardProps) {
  return (
    <button
      onClick={onClick}
      className="flex flex-col items-center p-4 bg-white rounded-2xl border border-gray-100 shadow-sm hover:shadow-md hover:-translate-y-0.5 active:scale-95 transition-all text-center touch-target"
    >
      <div className={`w-12 h-12 rounded-xl flex items-center justify-center mb-2 ${color}`}>
        <Icon className="w-6 h-6" />
      </div>
      <p className="font-semibold text-base text-gray-900">{title}</p>
      {subtitle && <p className="text-base text-gray-600 mt-1 font-medium">{subtitle}</p>}
      {badge && (
        <span className="mt-1 px-2 py-0.5 bg-red-500 text-white text-sm font-bold rounded-full">
          {badge}
        </span>
      )}
    </button>
  );
}
