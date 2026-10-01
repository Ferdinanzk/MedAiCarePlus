import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { ConsentApiError, legalLanguage, postConsent, useLegalDocument } from '../lib/consent-api';
import LegalDocument from './LegalDocument';

export default function ConsentGate({ source = 'reconsent', onNotNow }: { source?: 'reconsent' | 'settings'; onNotNow?: () => void }) {
  const { t, i18n } = useTranslation();
  const { legal, loading, failed, reload } = useLegalDocument(i18n.language);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const accept = async () => {
    if (!legal || saving || legal.language !== legalLanguage(i18n.language)) return;
    setSaving(true);
    setError('');
    try {
      const status = await postConsent({
        kind: 'core', terms_version: legal.terms_version, language: legal.language,
        document_sha256: legal.sha256, scopes: { core: true }, source,
      });
      if (!status.core_current) throw new Error('consent_required');
      window.location.href = '/dashboard';
    } catch (error) {
      if (error instanceof ConsentApiError && error.status === 409) {
        reload();
        setError('legal.documentChanged');
      } else {
        setError('legal.saveFailed');
      }
      setSaving(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">{t('legal.gateTitle')}</h1>
      <div className="min-w-0 bg-white rounded-2xl border border-gray-100 p-5 sm:p-6 shadow-sm">
        {loading && <p role="status" className="flex items-center gap-2"><Loader2 className="w-5 h-5 animate-spin text-[#0057B8]" />{t('common.loading')}</p>}
        {failed && <div role="alert" className="space-y-3"><p>{t('legal.loadFailed')}</p><button onClick={reload} className="min-h-12 text-[#0057B8] font-medium">{t('legal.retry')}</button></div>}
        {legal && <LegalDocument document={legal.document} showChanges />}
      </div>
      {error && <p role="alert" className="rounded-xl bg-red-50 p-4 text-red-700">{t(error)}</p>}
      <div className="flex flex-col sm:flex-row gap-3">
        <button onClick={accept} disabled={!legal || saving} className="min-h-12 px-6 py-3 rounded-xl bg-[#0057B8] text-white font-semibold hover:bg-[#003D82] disabled:opacity-50 flex items-center justify-center gap-2">
          {saving && <Loader2 className="w-5 h-5 animate-spin" />}{t('legal.accept')}
        </button>
        {onNotNow ? (
          <button onClick={onNotNow} className="min-h-12 px-6 py-3 text-center rounded-xl bg-gray-100 text-gray-700 font-medium">{t('legal.notNow')}</button>
        ) : (
          <Link to="/privacy-settings" className="min-h-12 px-6 py-3 text-center rounded-xl bg-gray-100 text-gray-700 font-medium">{t('legal.notNow')}</Link>
        )}
      </div>
    </div>
  );
}
