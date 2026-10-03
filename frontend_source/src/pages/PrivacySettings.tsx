import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Download, Loader2, LogOut, Trash2 } from 'lucide-react';
import ConsentGate from '../components/ConsentGate';
import { ConsentApiError, deleteAccount, exportAccount, logoutAccount } from '../lib/consent-api';
import { deleteAllMemory } from '../lib/memory-api';

export default function PrivacySettings({ limited = false }: { limited?: boolean }) {
  const { t } = useTranslation();
  const [reviewing, setReviewing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState<'export' | 'delete' | null>(null);
  const [error, setError] = useState('');
  const [reauthRequired, setReauthRequired] = useState(false);
  const [memoryCleared, setMemoryCleared] = useState<number | null>(null);

  const download = async () => {
    setBusy('export');
    setError('');
    try {
      await exportAccount();
    } catch {
      setError('legal.exportFailed');
    } finally {
      setBusy(null);
    }
  };

  const removeAccount = async (event: { preventDefault: () => void }) => {
    event.preventDefault();
    if (busy) return;
    setBusy('delete');
    setError('');
    setReauthRequired(false);
    try {
      const result = await deleteAccount(password || undefined);
      if (!result.deleted) throw new Error('delete_failed');
      logoutAccount();
    } catch (error) {
      if (error instanceof ConsentApiError && error.status === 401 && error.message === 'reauth_required') {
        setReauthRequired(true);
        setError('legal.reauthRequired');
      } else {
        setError('legal.deleteFailed');
      }
      setBusy(null);
    } finally {
      setPassword('');
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">{t('legal.privacyCardTitle')}</h1>
      {limited && (
        <div role="status" className="bg-blue-50 border border-blue-200 rounded-2xl p-5 space-y-4">
          <p className="text-blue-900">{t('legal.limitedBanner')}</p>
          <button onClick={() => setReviewing(value => !value)} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold">
            {t(reviewing ? 'legal.closeReview' : 'legal.reviewTerms')}
          </button>
        </div>
      )}
      {limited && reviewing && <ConsentGate source="settings" onNotNow={() => setReviewing(false)} />}
      <div className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4">
        <p className="text-gray-600">{t('legal.privacyCardDesc')}</p>
        <div className="flex flex-wrap gap-4 text-[#0057B8] font-medium">
          <Link to="/terms" className="py-2 underline">{t('register.terms.termsLink')}</Link>
          <Link to="/privacy" className="py-2 underline">{t('register.terms.privacyLink')}</Link>
        </div>
        <button onClick={download} disabled={busy !== null} className="w-full min-h-12 px-4 py-3 rounded-xl bg-[#0057B8] text-white font-semibold flex items-center justify-center gap-2 disabled:opacity-50">
          {busy === 'export' ? <Loader2 className="w-5 h-5 animate-spin" /> : <Download className="w-5 h-5" />}{t('legal.export')}
        </button>
      </div>
      <div className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4">
        <p className="text-sm text-gray-600">{t('memory.privacyHelp')}</p>
        <button onClick={() => { if (confirm(t('memory.deleteAllPrivacyConfirm'))) void deleteAllMemory().then(r => setMemoryCleared(r.deleted), () => setError('memory.saveFailed')); }}
          disabled={busy !== null} className="min-h-12 w-full px-5 py-3 rounded-xl bg-red-50 text-red-700 font-semibold disabled:opacity-50">{t('memory.deleteAll')}</button>
        {memoryCleared !== null && <p role="status" className="text-sm text-gray-600">{t('memory.deleteAllDone', { count: memoryCleared })}</p>}
      </div>
      <div className="bg-white rounded-2xl border border-red-100 p-5 shadow-sm space-y-4">
        {!confirmDelete ? (
          <button onClick={() => setConfirmDelete(true)} disabled={busy !== null} className="w-full min-h-12 px-4 py-3 rounded-xl bg-red-50 text-red-700 font-semibold flex items-center justify-center gap-2 disabled:opacity-50">
            <Trash2 className="w-5 h-5" />{t('legal.delete')}
          </button>
        ) : (
          <form onSubmit={removeAccount} className="space-y-4">
            <p id="delete-confirmation" className="text-red-700 font-medium">{t('legal.deleteConfirm')}</p>
            <label className="block space-y-2">
              <span className="text-gray-700">{t('legal.passwordPrompt')}</span>
              <input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} disabled={busy !== null} className="w-full min-h-12 px-4 py-3 border border-gray-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-[#0057B8]" />
            </label>
            <div className="flex flex-col sm:flex-row gap-3">
              <button type="submit" aria-describedby="delete-confirmation" disabled={busy !== null} className="min-h-12 px-5 py-3 rounded-xl bg-red-600 text-white font-semibold flex items-center justify-center gap-2 disabled:opacity-50">
                {busy === 'delete' && <Loader2 className="w-5 h-5 animate-spin" />}{t('legal.delete')}
              </button>
              <button type="button" onClick={() => { setConfirmDelete(false); setPassword(''); }} disabled={busy !== null} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium disabled:opacity-50">{t('common.cancel')}</button>
            </div>
          </form>
        )}
      </div>
      {error && <p role="alert" className="rounded-xl bg-red-50 p-4 text-red-700">{t(error)}</p>}
      <button onClick={logoutAccount} disabled={busy !== null} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium flex items-center justify-center gap-2 disabled:opacity-50">
        <LogOut className="w-5 h-5" />{t(reauthRequired ? 'legal.signInAgain' : 'common.logout')}
      </button>
    </div>
  );
}
