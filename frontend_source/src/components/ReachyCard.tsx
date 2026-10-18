import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { BellRing, Bot, Copy, Loader2, MessageCircle } from 'lucide-react';
import LegalDocument from './LegalDocument';
import { fetchConsentStatus, fetchLegal, legalLanguage, postConsent, type LegalResponse } from '../lib/consent-api';
import { clockTime, doseRefusal, dueDose, hasReplyFor, nextDose, notDueYet, refusalMessage, rememberRefusal } from '../lib/doses';
import { deleteAllMemory } from '../lib/memory-api';
import { fetchOverdoseProtection, notifyApi } from '../lib/notify-api';
import { ApiError, fetchReachyStatus, fetchTodayDoses, pairReachy, queueReachyTask, startCheckin, unpairReachy, updateReachySettings, type ReachyStatus, type RobotCamera } from '../lib/reachy-api';

const CHECKIN_SCOPES = ['robot_microphone', 'cloud_voice', 'conversation_analysis', 'safety_alerts'];
/** Camera states the robot's watchdog reports when it gets no frames (robot app 0.7.0+). */
const CAMERA_PROBLEMS: RobotCamera['state'][] = ['stalled', 'recovering', 'unavailable'];

/**
 * `key` and its values, or `text` already worded (the server's reply when it refuses the dose).
 * `noDose`: no dose could be used, so point to "Talk to Reachy now" for checking the robot alone.
 */
interface TestNotice { key?: string; text?: string; name?: string; time?: string; from?: string; noDose?: boolean }

const notDueNotice = (name: string, dose: { scheduled_time?: string | null; due_from?: string | null }): TestNotice =>
  ({ key: 'reachy.testAlertNotDue', name, time: clockTime(dose.scheduled_time), from: clockTime(dose.due_from),
     noDose: true });

export default function ReachyCard() {
  const { t, i18n } = useTranslation();
  const [status, setStatus] = useState<ReachyStatus | null>(null);
  const [consented, setConsented] = useState(false);
  const [listening, setListening] = useState(false);
  const [checkins, setCheckins] = useState(false);
  const [memory, setMemory] = useState(false);
  const [notice, setNotice] = useState<LegalResponse | null>(null);
  const [memoryNotice, setMemoryNotice] = useState<LegalResponse | null>(null);
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [testNotice, setTestNotice] = useState<TestNotice | null>(null);
  const [lineReminderNotice, setLineReminderNotice] = useState('');
  const [robotReminderNotice, setRobotReminderNotice] = useState('');
  /** The call name being typed; null shows the saved one. */
  const [nameDraft, setNameDraft] = useState<string | null>(null);
  const [nameNotice, setNameNotice] = useState<'saved' | 'invalid' | null>(null);

  const load = useCallback(async () => {
    const [robot, consent] = await Promise.all([fetchReachyStatus(), fetchConsentStatus()]);
    const current = (name: string) => {
      const scope = consent.scopes[name];
      return !!scope?.granted && scope.terms_version === consent.terms_version;
    };
    return { robot, consented: current('robot_camera'), listening: current('robot_microphone'),
             checkins: CHECKIN_SCOPES.every(current), memory: current('conversation_memory') };
  }, []);

  const refresh = useCallback(async () => {
    const next = await load();
    setConsented(next.consented);
    setListening(next.listening);
    setCheckins(next.checkins);
    setMemory(next.memory);
    setStatus(next.robot);
  }, [load]);

  useEffect(() => {
    let active = true;
    load().then(
      next => { if (active) { setConsented(next.consented); setListening(next.listening); setCheckins(next.checkins); setMemory(next.memory); setStatus(next.robot); } },
      () => { if (active) setError('reachy.loadFailed'); },
    );
    return () => { active = false; };
  }, [load]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setNameNotice(null);
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

  const postMemory = async (value: boolean) => {
    const legal = await fetchLegal('memory', i18n.language);
    await postConsent({
      kind: 'memory', terms_version: legal.terms_version, language: legal.language,
      document_sha256: legal.sha256, scopes: { conversation_memory: value }, source: 'settings',
    });
  };

  // Withdrawing memory always offers to delete the notes too (memory notice §5).
  const withdrawMemory = async () => {
    await postMemory(false);
    if (confirm(t('memory.deleteAllConfirm'))) await deleteAllMemory();
  };

  const toggleMemory = (value: boolean) => run(async () => {
    if (value) {
      setMemoryNotice(await fetchLegal('memory', i18n.language));
      return;
    }
    await withdrawMemory();
    await refresh();
  });

  const acceptMemory = () => run(async () => {
    await postMemory(true);
    setMemoryNotice(null);
    await refresh();
  });

  const withdraw = () => run(async () => {
    if (!confirm(t('reachy.withdrawConfirm'))) return;
    const legal = await fetchLegal('robot', i18n.language);
    if (status?.paired) await unpairReachy();
    await postConsent({
      kind: 'robot', terms_version: legal.terms_version, language: legal.language,
      document_sha256: legal.sha256, scopes: { robot_camera: false }, source: 'settings',
    });
    if (memory) await withdrawMemory();
    setToken('');
    await refresh();
  });

  const toggleAutoRecord = (value: boolean) => run(async () => setStatus(await updateReachySettings({ auto_record: value })));

  // "" goes back to the registered name. A name the server refuses (422 invalid_call_name) is explained next to the
  // field, and the typed name stays so it can be fixed.
  const saveCallName = (value: string) => run(async () => {
    try {
      setStatus(await updateReachySettings({ call_name: value }));
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 422) {
        setNameNotice('invalid');
        return;
      }
      throw cause;
    }
    setNameDraft(null);
    setNameNotice('saved');
  });

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
    if (!value && memory) await withdrawMemory();
    await refresh();
  });

  const talkNow = () => run(async () => {
    setTestNotice(null);
    await startCheckin();
    setTestNotice({ key: 'conversations.checkinQueued' });
    await refresh();
  });

  const sendLineReminderNow = () => run(async () => {
    setLineReminderNotice('');
    try {
      const result = await notifyApi.sendManualMedicationReminder();
      setLineReminderNotice(t('reachy.manualReminderSent', { count: result.sent }));
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : '';
      setLineReminderNotice(detail === 'no_linked_line'
        ? t('reachy.manualReminderNoLine') : t('reachy.manualReminderFailed'));
    }
  });

  const sendRobotReminderNow = () => run(async () => {
    setRobotReminderNotice('');
    await startCheckin();
    setRobotReminderNotice(t('reachy.robotReminderQueued'));
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

  // The test uses a real dose, so only one that is due now (nearest to its time first), never a later one, nor (with
  // overdose protection on) one missed past halfway to the next, and the notice says which dose it is and that
  // confirming it records it as taken. Even with protection off it never takes a later dose (3 Oct 2026).
  const sendTestAlert = () => run(async () => {
    setTestNotice(null);
    const [doses, protection] = await Promise.all([fetchTodayDoses(), fetchOverdoseProtection()]);
    const now = Date.now();
    const dose = dueDose(doses, now, protection);
    if (!dose) {
      const next = nextDose(doses, now);
      setTestNotice(next ? notDueNotice(next.name, next)
        : doses.length > 0 ? { key: 'reachy.testAlertAllDone', noDose: true } : { key: 'reachy.testAlertNoDose' });
      return;
    }
    try {
      await queueReachyTask(dose.id);
    } catch (cause) {
      const refusal = cause instanceof ApiError ? doseRefusal(cause.body) : null;
      if (!refusal) throw cause;
      rememberRefusal(refusal, dose.med_id);
      // Too soon, daily maximum or missed: the server's own sentence (in this card's words when the patient's
      // language differs from the page's). Not due yet: its times, in this card's words, unless the server worded it.
      const later = hasReplyFor(refusal, i18n.language) ? null : notDueYet(refusal);
      setTestNotice(later ? notDueNotice(dose.name, later)
        : { text: refusalMessage(refusal, t, i18n.language), noDose: true });
      return;
    }
    setTestNotice({ key: 'reachy.testAlertSent', name: dose.name, time: clockTime(dose.scheduled_time) });
    await refresh();
  });

  // Only an online robot's report is current. `idle`: no session is attached, so the camera isn't read.
  const camera = status?.online ? status.camera ?? null : null;
  const cameraProblem = !!camera && CAMERA_PROBLEMS.includes(camera.state);
  // The fps is a 10 s average that still counts the frames from before a stall, so a camera not working shows none.
  const cameraRate = !camera || camera.state === 'idle' ? null
    : cameraProblem ? '—' : camera.fps != null ? `${camera.fps.toFixed(1)} fps` : null;

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
            {cameraRate != null && (
              <>
                <dt className="text-gray-500">{t('reachy.robotCamera')}</dt>
                <dd>{cameraRate}</dd>
              </>
            )}
            <dt className="text-gray-500">{t('reachy.lastTask')}</dt>
            <dd>{status.last_task ? `${new Date(status.last_task.slot_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${status.last_task.status}` : '—'}</dd>
          </dl>
          {cameraProblem && (
            <p role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
              {t('reachy.cameraNotWorking')}
            </p>
          )}
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
          {(checkins || memory) && (
            <label className="flex items-start gap-3 rounded-xl bg-gray-50 p-4">
              <input type="checkbox" className="mt-1 w-5 h-5" checked={memory} disabled={busy}
                onChange={event => void toggleMemory(event.target.checked)} />
              <span>
                <span className="block text-sm font-medium text-gray-900">{t('memory.switch')}</span>
                <span className="block text-xs text-gray-500 mt-1">{t('memory.switchHelp')}</span>
              </span>
            </label>
          )}
          {memoryNotice && (
            <div className="space-y-4">
              <div className="max-h-96 overflow-y-auto rounded-xl border border-gray-100 p-4"><LegalDocument document={memoryNotice.document} /></div>
              <div className="flex flex-col sm:flex-row gap-3">
                <button onClick={acceptMemory} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">{t('memory.acceptNotice')}</button>
                <button onClick={() => setMemoryNotice(null)} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium">{t('common.cancel')}</button>
              </div>
            </div>
          )}
          {checkins && status.alert_contacts === 0 && (
            <p role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
              {t('reachy.noAlertContacts')}
            </p>
          )}
          <form className="rounded-xl bg-gray-50 p-4 space-y-2"
            onSubmit={event => { event.preventDefault(); if (nameDraft !== null) void saveCallName(nameDraft.trim()); }}>
            <label htmlFor="reachy-call-name" className="block text-sm font-medium text-gray-900">{t('reachy.callName')}</label>
            <p id="reachy-call-name-help" className="text-xs text-gray-500">{t('reachy.callNameHelp')}</p>
            <div className="flex flex-col sm:flex-row gap-3">
              <input id="reachy-call-name" type="text" maxLength={40} autoComplete="off" aria-describedby="reachy-call-name-help"
                placeholder={t('reachy.callNamePlaceholder')} value={nameDraft ?? status.call_name ?? ''} disabled={busy}
                onChange={event => { setNameDraft(event.target.value); setNameNotice(null); }}
                className="min-h-12 flex-1 min-w-0 rounded-xl border border-gray-200 bg-white px-4 text-base text-gray-900" />
              <button type="submit" disabled={busy || nameDraft === null} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">
                {t('common.save')}
              </button>
            </div>
            {status.call_name_custom && (
              <button type="button" onClick={() => void saveCallName('')} disabled={busy} className="min-h-12 text-sm text-[#0057B8] underline disabled:opacity-50">
                {t('reachy.callNameReset')}
              </button>
            )}
            {nameNotice === 'invalid' && <p role="alert" className="text-sm text-red-700">{t('reachy.callNameInvalid')}</p>}
            {nameNotice === 'saved' && <p role="status" className="text-sm text-green-700">{t('reachy.callNameSaved')}</p>}
          </form>
          {status.live_voice_test && (
            // Live voice is the default for every check-in while the server's test mode is on: no switch here.
            <div className="rounded-xl bg-gray-50 p-4">
              <span className="block text-sm font-medium text-gray-900">{t('reachy.liveVoice')}</span>
              <span className="block text-xs text-gray-500 mt-1">{t('reachy.liveVoiceHelp')}</span>
            </div>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <button onClick={sendTestAlert} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50 flex items-center justify-center gap-2">
              <BellRing className="w-4 h-4 shrink-0" />{t('reachy.testAlert')}
            </button>
            <button onClick={sendRobotReminderNow} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl border border-[#0057B8] text-[#0057B8] font-semibold disabled:opacity-50 flex items-center justify-center gap-2">
              {busy ? <Loader2 className="w-4 h-4 shrink-0 animate-spin" /> : <Bot className="w-4 h-4 shrink-0" />}{t('reachy.robotReminderNow')}
            </button>
            <button onClick={sendLineReminderNow} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl border border-green-700 text-green-800 font-semibold disabled:opacity-50 flex items-center justify-center gap-2">
              {busy ? <Loader2 className="w-4 h-4 shrink-0 animate-spin" /> : <BellRing className="w-4 h-4 shrink-0" />}{t('reachy.manualReminderNow')}
            </button>
            {checkins && (
              <button onClick={talkNow} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl border border-[#0057B8] text-[#0057B8] font-semibold disabled:opacity-50 flex items-center justify-center gap-2">
                <MessageCircle className="w-4 h-4 shrink-0" />{t('conversations.talkNow')}
              </button>
            )}
            <button onClick={unpair} disabled={busy} className="min-h-12 px-5 py-3 rounded-xl bg-gray-100 text-gray-700 font-medium disabled:opacity-50 flex items-center justify-center">{t('reachy.unpair')}</button>
          </div>
          {testNotice && (
            <p role="status" className="rounded-xl bg-blue-50 p-3 text-sm text-blue-800">
              {testNotice.text ?? t(testNotice.key ?? '', { name: testNotice.name, time: testNotice.time, from: testNotice.from })}
              {testNotice.noDose && checkins && ` ${t('reachy.testAlertTalkInstead', { label: t('conversations.talkNow') })}`}
            </p>
          )}
          {lineReminderNotice && <p role="status" className="rounded-xl bg-green-50 p-3 text-sm text-green-800">{lineReminderNotice}</p>}
          {robotReminderNotice && <p role="status" className="rounded-xl bg-blue-50 p-3 text-sm text-blue-800">{robotReminderNotice}</p>}
        </div>
      )}

      {status && consented && (
        <button onClick={withdraw} disabled={busy} className="block text-sm text-red-700 underline py-2 disabled:opacity-50">{t('reachy.withdraw')}</button>
      )}

      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t(error)}</p>}
    </section>
  );
}
