import { useTranslation } from 'react-i18next';
import { Globe } from 'lucide-react';

export default function LanguageSwitcher() {
  const { i18n } = useTranslation();

  const toggleLanguage = () => {
    const newLang = i18n.language === 'zh-TW' ? 'en' : 'zh-TW';
    i18n.changeLanguage(newLang);
  };

  return (
    <button
      onClick={toggleLanguage}
      className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-gray-600 hover:text-medical-600 hover:bg-medical-50 rounded-lg transition-colors"
    >
      <Globe className="w-4 h-4" />
      <span>{i18n.language === 'zh-TW' ? '中文' : 'EN'}</span>
    </button>
  );
}
