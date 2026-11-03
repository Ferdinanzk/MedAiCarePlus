import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { BellOff, BellRing, Loader2 } from 'lucide-react';
import { getLineAdmin, isLineAdmin, setLineReminders, type LineAdmin } from '../lib/line-admin-api';
import { formatSince } from './LinePausedBanner';

/** The admin's switch for LINE medication reminders. Nothing is rendered for a non-admin (403) or on any error. */
export default function LineAdminCard() {
  const { t, i18n } = useTranslation();
  const [admin, setAdmin] = useState<LineAdmin | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    getLineAdmin().then(next => { if (alive && isLineAdmin(next)) setAdmin(next); }, () => { /* 403 or any error: the card stays hidden */ });
    return () => { alive = false; };
  }, []);

  if (!admin) return null;

  // The state flips only after the PUT succeeds. The request has no timeout, so the button stays off until it ends.
  const press = async () => {
    setBusy(true);
    setFailed(false);
    try {
      const next = await setLineReminders(!admin.paused);
      if (!isLineAdmin(next)) throw new Error('bad_body');
      setAdmin(next);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  const { quota } = admin;
  const usage = quota.error ? t('lineAdmin.usageUnavailable')
    : quota.used === null ? t('lineAdmin.usageUnavailable')
    : quota.limit === null ? t('lineAdmin.usageUnlimited', { used: quota.used })
    : t('lineAdmin.usage', { used: quota.used, limit: quota.limit });

  return (
    <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4" aria-labelledby="line-admin-title">
      <div className="flex items-start gap-3">
        {admin.paused
          ? <BellOff aria-hidden="true" className="mt-0.5 w-5 h-5 shrink-0 text-amber-700" />
          : <BellRing aria-hidden="true" className="mt-0.5 w-5 h-5 shrink-0 text-[#0057B8]" />}
        <div className="min-w-0">
          <h2 id="line-admin-title" className="text-lg font-semibold text-gray-900">{t('lineAdmin.title')}</h2>
          <p id="line-admin-state" className="text-sm text-gray-700 mt-1 break-words">
            {admin.paused
              ? t('lineAdmin.off', { time: formatSince(admin.since, i18n.language), name: admin.changed_by ?? '—' })
              : t('lineAdmin.on')}
          </p>
          <p id="line-admin-help" className="text-xs text-gray-500 mt-1">{t('lineAdmin.help')}</p>
          <p className="text-sm text-gray-700 mt-2">{usage}</p>
        </div>
      </div>
      <button type="button" onClick={() => void press()} disabled={busy}
        aria-describedby="line-admin-state line-admin-help"
        className={`min-h-12 w-full sm:w-auto px-5 py-3 rounded-xl font-semibold disabled:opacity-50 flex items-center justify-center gap-2 ${
          admin.paused ? 'bg-[#0057B8] text-white' : 'border border-[#0057B8] bg-white text-[#0057B8]'}`}>
        {busy && <Loader2 aria-hidden="true" className="w-4 h-4 shrink-0 animate-spin" />}
        {t(admin.paused ? 'lineAdmin.resume' : 'lineAdmin.pause')}
      </button>
      {failed && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t('lineAdmin.failed')}</p>}
    </section>
  );
}
