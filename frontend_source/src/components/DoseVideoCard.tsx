import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Video } from 'lucide-react';
import LegalDocument from './LegalDocument';
import { fetchLegal, legalLanguage, postConsent, type LegalResponse } from '../lib/consent-api';
import { getFaceAuthHeaders } from '../lib/face-auth';

interface DoseVideoStatus {
  enabled: boolean;
  public_link: boolean;
  stored: number;
  max_age_hours: number;
}

async function fetchDoseVideoStatus(): Promise<DoseVideoStatus> {
  const response = await fetch('/api/dose-videos/status', { headers: getFaceAuthHeaders(), cache: 'no-store' });
  if (!response.ok) throw new Error('request_failed');
  return response.json();
}

/** Opt-in: a short clip of each recorded dose goes to family with the "dose taken" LINE message
 * (app/services/dose_video.py). Switching on shows the video notice first; switching off deletes clips not yet
 * downloaded. */
export default function DoseVideoCard() {
  const { t, i18n } = useTranslation();
  const [status, setStatus] = useState<DoseVideoStatus | null>(null);
  const [notice, setNotice] = useState<LegalResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => setStatus(await fetchDoseVideoStatus()), []);

  useEffect(() => {
    refresh().catch(() => setError('doseVideo.loadFailed'));
  }, [refresh]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch {
      setError('doseVideo.actionFailed');
    } finally {
      setBusy(false);
    }
  };

  const consent = (granted: boolean, legal: LegalResponse) => postConsent({
    kind: 'video', terms_version: legal.terms_version, language: legal.language,
    document_sha256: legal.sha256, scopes: { dose_video: granted }, source: 'settings',
  });

  const toggle = (value: boolean) => run(async () => {
    if (value) {
      setNotice(await fetchLegal('video', i18n.language));
      return;
    }
    if (!confirm(t('doseVideo.offConfirm'))) return;
    await consent(false, await fetchLegal('video', i18n.language));
    await refresh();
  });

  const accept = () => run(async () => {
    if (!notice || notice.language !== legalLanguage(i18n.language)) {
      setNotice(await fetchLegal('video', i18n.language));
      return;
    }
    await consent(true, notice);
    setNotice(null);
    await refresh();
  });

  return (
    <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4" aria-labelledby="dose-video-title">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-blue-50 flex items-center justify-center"><Video className="w-5 h-5 text-[#0057B8]" /></div>
        <div>
          <h3 id="dose-video-title" className="text-base font-semibold text-gray-900">{t('doseVideo.title')}</h3>
          <p className="text-xs text-gray-500">{t('doseVideo.subtitle')}</p>
        </div>
      </div>

      {status === null && !error && (
        <p role="status" className="text-sm text-gray-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t('common.loading')}</p>
      )}

      {status && !notice && (
        <label className="flex items-start gap-3 rounded-xl bg-gray-50 p-4">
          <input type="checkbox" className="mt-1 w-5 h-5" checked={status.enabled} disabled={busy}
            onChange={event => void toggle(event.target.checked)} />
          <span>
            <span className="block text-sm font-medium text-gray-900">{t('doseVideo.switch')}</span>
            <span className="block text-xs text-gray-500 mt-1">{t('doseVideo.help', { hours: status.max_age_hours })}</span>
          </span>
        </label>
      )}

      {notice && (
        <div className="space-y-4">
          <div className="max-h-96 overflow-y-auto rounded-xl border border-gray-100 p-4"><LegalDocument document={notice.document} /></div>
          <div className="flex flex-col sm:flex-row gap-3">
            <button onClick={accept} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">{t('doseVideo.accept')}</button>
            <button onClick={() => setNotice(null)} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium">{t('common.cancel')}</button>
          </div>
        </div>
      )}

      {status?.enabled && !notice && (
        <div className="space-y-2 text-sm">
          {!status.public_link && (
            <p role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-amber-900">{t('doseVideo.noPublicLink')}</p>
          )}
          <p className="text-gray-600">{t('doseVideo.stored', { count: status.stored })}</p>
        </div>
      )}

      {error && <p role="alert" className="text-sm text-red-600">{t(error)}</p>}
    </section>
  );
}
