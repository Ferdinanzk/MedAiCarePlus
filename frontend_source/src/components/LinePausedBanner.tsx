import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getLineReminders, type LineReminders } from '../lib/line-admin-api';

const REFRESH_MS = 60_000;

export function formatSince(since: string | null, language: string): string {
  if (!since) return '';
  const date = new Date(since);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString(language, { dateStyle: 'medium', timeStyle: 'short' });
}

/** An amber strip while the admin has paused LINE medication reminders. Errors show nothing. */
export default function LinePausedBanner() {
  const { t, i18n } = useTranslation();
  const [state, setState] = useState<LineReminders | null>(null);

  useEffect(() => {
    let alive = true;
    const read = () => getLineReminders().then(next => { if (alive) setState(next); }, () => { if (alive) setState(null); });
    void read();
    const timer = setInterval(() => void read(), REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  if (!state?.paused) return null;
  return (
    <div role="status" className="mb-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 break-words">
      {t('lineAdmin.banner', { time: formatSince(state.since, i18n.language) })}
    </div>
  );
}
