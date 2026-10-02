import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { BellRing, Bot, Copy, Loader2, MessageCircle } from 'lucide-react';
import LegalDocument from './LegalDocument';
import { fetchConsentStatus, fetchLegal, legalLanguage, postConsent, type LegalResponse } from '../lib/consent-api';
import { fetchReachyStatus, fetchTodayDoses, pairReachy, queueReachyTask, setAutoRecord, startCheckin, unpairReachy, type ReachyStatus } from '../lib/reachy-api';

const CHECKIN_SCOPES = ['robot_microphone', 'cloud_voice', 'conversation_analysis', 'safety_alerts'];

export default function ReachyCard() {
  const { t, i18n } = useTranslation();
  const [status, setStatus] = useState<ReachyStatus | null>(null);
  const [consented, setConsented] = useState(false);
  const [listening, setListening] = useState(false);
  const [checkins, setCheckins] = useState(false);
  const [notice, setNotice] = useState<LegalResponse | null>(null);
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [testNotice, setTestNotice] = useState<{ key: string; name?: string } | null>(null);

  const load = useCallback(async () => {
    const [robot, consent] = await Promise.all([fetchReachyStatus(), fetchConsentStatus()]);
    const current = (name: string) => {
      const scope = consent.scopes[name];
      return !!scope?.granted && scope.terms_version === consent.terms_version;
    };
    return { robot, consented: current('robot_camera'), listening: current('robot_microphone'),
             checkins: CHECKIN_SCOPES.every(current) };
  }, []);

  const refresh = useCallback(async () => {
    const next = await load();
    setConsented(next.consented);
    setListening(next.listening);
    setCheckins(next.checkins);
    setStatus(next.robot);
  }, [load]);

  useEffect(() => {
    let active = true;
    load().then(
      next => { if (active) { setConsented(next.consented); setListening(next.listening); setCheckins(next.checkins); setStatus(next.robot); } },
      () => { if (active) setError('reachy.loadFailed'); },
    );
    return () => { active = false; };
  }, [load]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error && cause.message === 'robot_consent_required' ? 'reachy.consentRequired' : 'reachy.actionFailed');
    } finally {
      setBusy(false);
    }
  };

  const openNotice = () => run(async () => setNotice(await fetchLegal('robot', i18n.language)));

  const acceptNotice = () => run(async () => {
    if (!notice || notice.language !== legalLanguage(i18n.language)) {
      setNotice(await fetchLegal('robot', i18n.language));
      return;
    }
    await postConsent({
      kind: 'robot', terms_version: notice.terms_version, language: notice.language,
      document_sha256: notice.sha256, scopes: { robot_camera: true }, source: 'pairing',
    });
    setNotice(null);
    await refresh();
  });

  const pair = () => run(async () => {
    const result = await pairReachy();
    setToken(result.token);
    await refresh();
  });

  const unpair = () => run(async () => {
    if (!confirm(t('reachy.unpairConfirm'))) return;
    setToken('');
    setStatus(await unpairReachy());
  });

  const withdraw = () => run(async () => {
    if (!confirm(t('reachy.withdrawConfirm'))) return;
    const legal = await fetchLegal('robot', i18n.language);
    if (status?.paired) await unpairReachy();
    await postConsent({
      kind: 'robot', terms_version: legal.terms_version, language: legal.language,
      document_sha256: legal.sha256, scopes: { robot_camera: false }, source: 'settings',
    });
    setToken('');
    await refresh();
  });

  const toggleAutoRecord = (value: boolean) => run(async () => setStatus(await setAutoRecord(value)));

  const toggleCheckins = (value: boolean) => run(async () => {
    const legal = await fetchLegal('robot', i18n.language);
    // On: everything a check-in needs (the notice: check-ins only with safety alerts). Off: the conversation
    // scopes only; the microphone stays as the "I finished" switch has it.
    const scopes = value
      ? Object.fromEntries(CHECKIN_SCOPES.map(scope => [scope, true]))
      : { cloud_voice: false, conversation_analysis: false, safety_alerts: false };
    await postConsent({
      kind: 'robot', terms_version: legal.terms_version, language: legal.language,
      document_sha256: legal.sha256, scopes, source: 'settings',
    });
    await refresh();
  });

  const talkNow = () => run(async () => {
    setTestNotice(null);
    await startCheckin();
    setTestNotice({ key: 'conversations.checkinQueued' });
    await refresh();
  });

  const toggleListening = (value: boolean) => run(async () => {
    const legal = await fetchLegal('robot', i18n.language);
    await postConsent({
      kind: 'robot', terms_version: legal.terms_version, language: legal.language,
      document_sha256: legal.sha256, scopes: { robot_microphone: value }, source: 'settings',
    });
    await refresh();
  });

  const sendTestAlert = () => run(async () => {
    setTestNotice(null);
    const doses = await fetchTodayDoses();
    const dose = doses.find(d => d.status === 'pending' || d.status === 'missed');
    if (!dose) {
      setTestNotice({ key: 'reachy.testAlertNoDose' });
      return;
    }
    await queueReachyTask(dose.id);
    setTestNotice({ key: 'reachy.testAlertSent', name: dose.name });
    await refresh();
  });

  return (
    <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4" aria-labelledby="reachy-title">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-blue-50 flex items-center justify-center"><Bot className="w-5 h-5 text-[#0057B8]" /></div>
        <div>
          <h2 id="reachy-title" className="text-base font-semibold text-gray-900">{t('reachy.title')}</h2>
          <p className="text-xs text-gray-500">{t('reachy.subtitle')}</p>
        </div>
      </div>

      {status === null && !error && <p role="status" className="text-sm text-gray-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t('common.loading')}</p>}

      {status && !consented && !notice && (
        <button onClick={openNotice} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">
          {t('reachy.reviewNotice')}
        </button>
      )}

      {notice && (
        <div className="space-y-4">
          <div className="max-h-96 overflow-y-auto rounded-xl border border-gray-100 p-4"><LegalDocument document={notice.document} /></div>
          <div className="flex flex-col sm:flex-row gap-3">
            <button onClick={acceptNotice} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">{t('reachy.acceptCamera')}</button>
            <button onClick={() => setNotice(null)} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium">{t('common.cancel')}</button>
          </div>
        </div>
      )}

      {status && consented && !status.paired && (
        <button onClick={pair} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">{t('reachy.pair')}</button>
      )}

      {token && (
        <div className="rounded-xl bg-amber-50 border border-amber-200 p-4 space-y-2">
          <p className="text-sm font-medium text-amber-900">{t('reachy.tokenOnce')}</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all text-xs bg-white rounded-lg p-2 border border-amber-200">{token}</code>
            <button onClick={() => void navigator.clipboard?.writeText(token)} aria-label={t('reachy.copyToken')} className="min-h-12 min-w-12 flex items-center justify-center rounded-lg bg-white border border-amber-200">
              <Copy className="w-4 h-4" />
            </button>
          </div>
          <p className="text-xs text-amber-800">{t('reachy.tokenHelp')}</p>
        </div>
      )}

      {status?.paired && (
        <div className="space-y-4">
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <dt className="text-gray-500">{t('reachy.connection')}</dt>
            <dd className={status.online ? 'text-green-700 font-medium' : 'text-red-700 font-medium'}>{status.online ? t('reachy.online') : t('reachy.offline')}</dd>
            <dt className="text-gray-500">{t('reachy.lastSeen')}</dt>
            <dd>{status.last_seen_at ? new Date(status.last_seen_at).toLocaleString() : '—'}</dd>
            <dt className="text-gray-500">{t('reachy.cameraRate')}</dt>
            <dd>{status.landmark_fps != null ? `${status.landmark_fps.toFixed(1)} fps` : '—'}</dd>
            <dt className="text-gray-500">{t('reachy.lastTask')}</dt>
            <dd>{status.last_task ? `${new Date(status.last_task.slot_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${status.last_task.status}` : '—'}</dd>
          </dl>
          <label className="flex items-start gap-3 rounded-xl bg-gray-50 p-4">
            <input type="checkbox" className="mt-1 w-5 h-5" checked={!!status.auto_record} disabled={busy}
              onChange={event => void toggleAutoRecord(event.target.checked)} />
            <span>
              <span className="block text-sm font-medium text-gray-900">{t('reachy.autoRecord')}</span>
              <span className="block text-xs text-gray-500 mt-1">{t('reachy.autoRecordHelp')}</span>
            </span>
          </label>
          <label className="flex items-start gap-3 rounded-xl bg-gray-50 p-4">
            <input type="checkbox" className="mt-1 w-5 h-5" checked={listening} disabled={busy}
              onChange={event => void toggleListening(event.target.checked)} />
            <span>
              <span className="block text-sm font-medium text-gray-900">{t('reachy.listen')}</span>
              <span className="block text-xs text-gray-500 mt-1">{t('reachy.listenHelp')}</span>
            </span>
          </label>
          <label className="flex items-start gap-3 rounded-xl bg-gray-50 p-4">
            <input type="checkbox" className="mt-1 w-5 h-5" checked={checkins} disabled={busy}
              onChange={event => void toggleCheckins(event.target.checked)} />
            <span>
              <span className="block text-sm font-medium text-gray-900">{t('reachy.checkins')}</span>
              <span className="block text-xs text-gray-500 mt-1">{t('reachy.checkinsHelp')}</span>
            </span>
          </label>
          {checkins && status.alert_contacts === 0 && (
            <p role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
              {t('reachy.noAlertContacts')}
            </p>
          )}
          <div className="flex flex-col sm:flex-row gap-3">
            <button onClick={sendTestAlert} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50 flex items-center justify-center gap-2">
              <BellRing className="w-4 h-4" />{t('reachy.testAlert')}
            </button>
            {checkins && (
              <button onClick={talkNow} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl border border-[#0057B8] text-[#0057B8] font-semibold disabled:opacity-50 flex items-center justify-center gap-2">
                <MessageCircle className="w-4 h-4" />{t('conversations.talkNow')}
              </button>
            )}
            <button onClick={unpair} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium disabled:opacity-50">{t('reachy.unpair')}</button>
          </div>
          {testNotice && (
            <p role="status" className="rounded-xl bg-blue-50 p-3 text-sm text-blue-800">
              {t(testNotice.key, { name: testNotice.name })}
            </p>
          )}
        </div>
      )}

      {status && consented && (
        <button onClick={withdraw} disabled={busy} className="block text-sm text-red-700 underline py-2 disabled:opacity-50">{t('reachy.withdraw')}</button>
      )}

      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t(error)}</p>}
    </section>
  );
}
