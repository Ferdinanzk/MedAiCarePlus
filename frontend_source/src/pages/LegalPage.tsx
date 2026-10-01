import { useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import LegalDocument from '../components/LegalDocument';
import LanguageSwitcher from '../components/LanguageSwitcher';
import { useLegalDocument } from '../lib/consent-api';

export default function LegalPage() {
  const { t, i18n } = useTranslation();
  const { pathname } = useLocation();
  const { legal, loading, failed, reload } = useLegalDocument(i18n.language);

  useEffect(() => {
    if (legal && pathname === '/privacy') document.getElementById('data')?.scrollIntoView();
  }, [legal, pathname]);

  return (
    <div className="min-h-screen bg-[#F8F9FA]">
      <header className="bg-white border-b border-gray-100">
        <div className="max-w-3xl mx-auto px-4 min-h-14 flex flex-wrap items-center justify-between gap-2">
          <Link to="/" className="font-semibold text-[#0057B8]">MedAiCarePlus</Link>
          <LanguageSwitcher />
        </div>
      </header>
      <main className="max-w-3xl mx-auto px-4 py-6">
        <div className="min-w-0 bg-white rounded-2xl border border-gray-100 p-5 sm:p-6 shadow-sm">
          {loading && <p role="status" className="flex items-center gap-2"><Loader2 className="w-5 h-5 animate-spin text-[#0057B8]" />{t('common.loading')}</p>}
          {failed && <div role="alert" className="space-y-3"><p>{t('legal.loadFailed')}</p><button onClick={reload} className="min-h-12 text-[#0057B8] font-medium">{t('legal.retry')}</button></div>}
          {legal && <LegalDocument document={legal.document} />}
        </div>
      </main>
    </div>
  );
}
