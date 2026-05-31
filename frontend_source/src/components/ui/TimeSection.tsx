import type { LucideIcon } from 'lucide-react';

interface TimeSectionProps {
  title: string;
  timeRange: string;
  icon: LucideIcon;
  iconColor: string;
  bgColor: string;
  children: React.ReactNode;
}

export default function TimeSection({ title, timeRange, icon: Icon, iconColor, bgColor, children }: TimeSectionProps) {
  return (
    <div className="space-y-2">
      {/* Time header */}
      <div className={`flex items-center gap-2 px-3 py-2 ${bgColor} rounded-lg`}>
        <Icon className={`w-4 h-4 ${iconColor}`} />
        <span className={`text-base font-semibold ${iconColor}`}>{title}</span>
        <span className="text-sm text-gray-400 ml-auto">{timeRange}</span>
      </div>
      {children}
    </div>
  );
}
