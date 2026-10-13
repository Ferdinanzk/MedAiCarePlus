import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Brain, CheckCircle2, Loader2, Lock, Plus, Trash2, TriangleAlert, XCircle } from 'lucide-react';
import { ApiError } from '../lib/reachy-api';
import {
  AI_JOBS, AI_KINDS, LIVE_VOICES, addConnection, addModel, checkLive, deleteConnection, deleteModel, fetchAiSettings,
  fetchRemoteModels, liveChoice, liveUnsaved, mergeJobs, saveJobs, saveLive, testModel, updateConnection, updateModel,
  type AiJob, type AiJobChoice, type AiKind, type AiLiveChoice, type AiModel, type AiSettings, type AiTestResult,
  type AiThinkingLevel,
} from '../lib/ai-connections-api';
import { acceptedLevels, nearestLevel, offeredLevels, sentLevel, thinkingMs, tooSlow } from '../lib/ai-thinking';

/** Codes api_ai.py answers with; anything else shows the general message. */
const KNOWN_ERRORS = ['in_use', 'untested_model', 'not_found', 'invalid_base_url', 'invalid_name', 'invalid_kind',
  'key_unreadable', 'no_key', 'already_exists', 'notice_incomplete', 'remote_models_unavailable', 'invalid_request'];
/** Codes a model's Test result or last call can carry instead of the service's own words (ai_profile.py). */
const MODEL_ERRORS = ['not_chat_model', 'thinking_budget'];
/** Codes a live voice Check can save as its error (live_client.CODES); anything else is shown as it came. */
const LIVE_ERRORS = ['live_auth', 'live_unavailable', 'rate_limit_exceeded', 'content_filter', 'live_closed'];
const field = 'w-full px-3 py-2 border border-gray-200 rounded-xl text-sm';
const primary = 'px-3 py-2 rounded-xl text-sm font-medium bg-[#0057B8] text-white disabled:opacity-50';
const secondary = 'px-3 py-2 rounded-xl text-sm font-medium border border-gray-200 text-gray-700 disabled:opacity-50';
const warning = 'rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900';

interface ConnectionDraft { kind: AiKind; name: string; base_url: string; api_key: string; service_name: string }
interface ModelDraft { connection_id: number; model_id: string; company: string; region: string; retention: string }
interface NoticeDraft { ref: number; company: string; region: string; retention: string }

const errorCode = (cause: unknown) => (cause instanceof ApiError && KNOWN_ERRORS.includes(cause.message) ? cause.message : 'failed');

/** Brings a refusal into view when it appears. Outside the component, so React calls it once per mount, not on
 *  every render (it would pull the page back while the person types). */
const reveal = (node: HTMLElement | null) => { node?.scrollIntoView({ block: 'nearest' }); };

/** A refusal and where it shows: next to what caused it ('connection:<id>', 'newConnection', 'model:<ref>',
 *  'newModel' or 'jobs'), not at the top of a long card. */
interface CardError { code: string; at: string }

export default function AiConnectionsCard() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [jobs, setJobs] = useState<Record<AiJob, AiJobChoice> | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState<CardError | null>(null);
  const [jobsSaved, setJobsSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, AiTestResult>>({});
  const [connectionDraft, setConnectionDraft] = useState<ConnectionDraft | null>(null);
  const [modelDraft, setModelDraft] = useState<ModelDraft | null>(null);
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [keyDraft, setKeyDraft] = useState<{ id: number; key: string } | null>(null);
  const [noticeDraft, setNoticeDraft] = useState<NoticeDraft | null>(null);
  /** The live voice form while it differs from the saved setup; null shows the saved setup (liveChoice). */
  const [liveDraft, setLiveDraft] = useState<AiLiveChoice | null>(null);
  const [liveChecking, setLiveChecking] = useState(false);

  useEffect(() => {
    let active = true;
    fetchAiSettings().then(
      next => { if (active) { setSettings(next); setJobs(next.jobs); } },
      () => { if (active) setLoadFailed(true); },
    );
    return () => { active = false; };
  }, []);

  /** One change; the server answers with the whole card. A refusal shows at `at`. `done` runs only after a success
   *  (e.g. closing a form). Unsaved job choices survive every change except saving the jobs themselves. */
  const run = async (at: string, action: () => Promise<AiSettings>, done?: () => void, savesJobs = false) => {
    setBusy(true);
    setError(null);
    setJobsSaved(false);
    try {
      const next = await action();
      setSettings(next);
      setJobs(current => (savesJobs || !current ? next.jobs
        : mergeJobs(current, next.jobs, next.models.filter(model => model.tested).map(model => model.model_ref))));
      done?.();
    } catch (cause) {
      setError({ code: errorCode(cause), at });
    } finally {
      setBusy(false);
    }
  };

  const runTest = async (ref: number) => {
    setTesting(ref);
    setError(null);
    try {
      const { settings: next, ...result } = await testModel(ref);
      setResults(previous => ({ ...previous, [ref]: result }));
      setSettings(next);
    } catch (cause) {
      setError({ code: errorCode(cause), at: `model:${ref}` });
    } finally {
      setTesting(null);
    }
  };

  /** Check tries the saved live setup; the server saves its time or its error code. `run` catches every failure, so
   *  the spinner always stops. */
  const runLiveCheck = async () => {
    setLiveChecking(true);
    await run('live', checkLive);
    setLiveChecking(false);
  };

  const loadRemoteModels = async (id: number) => {
    setBusy(true);
    setError(null);
    try {
      setRemoteModels((await fetchRemoteModels(id)).models);
    } catch (cause) {
      setError({ code: errorCode(cause), at: 'newModel' });
    } finally {
      setBusy(false);
    }
  };

  const heading = (
    <div className="flex items-center gap-3">
      <div className="w-10 h-10 rounded-xl bg-blue-50 flex items-center justify-center"><Brain className="w-5 h-5 text-[#0057B8]" /></div>
      <div>
        <h2 id="ai-title" className="text-base font-semibold text-gray-900">{t('aiConnections.title')}</h2>
        <p className="text-xs text-gray-500">{t('aiConnections.subtitle')}</p>
      </div>
    </div>
  );

  if (!settings || !jobs) {
    return (
      <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4" aria-labelledby="ai-title">
        {heading}
        {loadFailed
          ? <p className="text-sm text-red-700">{t('aiConnections.errors.loadFailed')}</p>
          : <Loader2 className="w-5 h-5 animate-spin text-gray-400" aria-label={t('aiConnections.loading')} />}
      </section>
    );
  }

  const connectionName = (id: number) => settings.connections.find(c => c.connection_id === id)?.name ?? '';
  const label = (model: AiModel) => `${connectionName(model.connection_id)} · ${model.model_id}`;
  const tested = settings.models.filter(model => model.tested);
  const errorAt = (at: string) => error?.at === at && (
    <p role="alert" ref={reveal} className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t(`aiConnections.errors.${error.code}`)}</p>
  );

  /** A known code in the card's own words; anything else is the service's message, shown as it came. */
  const modelError = (error: string | null) =>
    (error && MODEL_ERRORS.includes(error) ? t(`aiConnections.modelErrors.${error}`) : error);

  const status = (model: AiModel) => {
    if (testing === model.model_ref) return <span className="text-gray-500">{t('aiConnections.testingThinking')}</span>;
    const result = results[model.model_ref];
    if (result && !result.ok) return <span className="text-red-700"><XCircle className="inline w-4 h-4" /> {modelError(result.error)}</span>;
    if (model.last_error) return <span className="text-red-700">{t('aiConnections.lastCallFailed', { error: modelError(model.last_error) })}</span>;
    if (model.test_ms != null) {
      return (
        <span className="text-green-700"><CheckCircle2 className="inline w-4 h-4" /> {t('aiConnections.testOk', { seconds: (model.test_ms / 1000).toFixed(1) })}
          {result?.sample ? ` · ${result.sample}` : ''}</span>
      );
    }
    if (model.copied_from_env) return <span className="text-gray-500">{t('aiConnections.copiedFromEnv')}</span>;
    return <span className="text-gray-500">{t('aiConnections.notTested')}</span>;
  };

  /** "Can think: low · medium · high" or "Can't think", from what the model's Test found; "Not checked yet: press
   *  Test" for a profile saved before Test checked thinking (no `thinking`). Its job offers only off either way. */
  const canThink = (model: AiModel) => {
    if (model.profile?.thinking == null) return t('aiConnections.thinkingNotChecked');
    const levels = acceptedLevels(model);
    return levels.length
      ? t('aiConnections.canThink', { levels: levels.map(level => t(`aiConnections.levelShort.${level}`)).join(t('aiConnections.levelSeparator')) })
      : t('aiConnections.cantThink');
  };

  /** The job's choice with another main model. Its thinking level stays if the new model accepts it, else it becomes
   *  the nearest lower level that model accepts, else off. */
  const withMainModel = (job: AiJob, primary_ref: number): AiJobChoice => {
    const main = settings.models.find(model => model.model_ref === primary_ref);
    return { ...jobs[job], primary_ref, thinking: nearestLevel(main, jobs[job].thinking) };
  };

  /** A job's Thinking list (off, and the levels its main model accepts), the level that model really gets when it
   *  differs, its time at that level in Test, and a warning when that time is over what one call of the job may take. */
  const thinkingControls = (job: AiJob) => {
    const choice = jobs[job];
    const main = settings.models.find(model => model.model_ref === choice.primary_ref);
    const sent = sentLevel(main, choice.thinking);
    const ms = thinkingMs(main, choice.thinking);
    const notes: string[] = [];
    if (sent !== choice.thinking) notes.push(t('aiConnections.thinkingSent', { level: t(`aiConnections.levelNames.${sent}`) }));
    if (ms !== null) notes.push(t('aiConnections.thinkingTime', { seconds: (ms / 1000).toFixed(1) }));
    return (
      <>
        <label className="block text-xs text-gray-600 sm:col-start-2">{t('aiConnections.thinking')}
          <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.thinking')}`} value={choice.thinking}
            onChange={event => {
              setJobsSaved(false);
              setJobs({ ...jobs, [job]: { ...choice, thinking: event.target.value as AiThinkingLevel } });
            }}>
            {offeredLevels(main, choice.thinking).map(level => <option key={level} value={level}>{t(`aiConnections.levelNames.${level}`)}</option>)}
          </select>
        </label>
        {notes.length > 0 && (
          <p className="text-xs text-gray-500 sm:pb-2">{notes.map(note => <span key={note} className="block">{note}</span>)}</p>
        )}
        {tooSlow(job, main, choice.thinking) && (
          <p role="alert" className={`${warning} sm:col-span-3`}><TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.thinkingTooSlow')}</p>
        )}
      </>
    );
  };

  /** A known Check error code in the card's own words; anything else is the server's text, shown as it came. */
  const liveError = (code: string) => (LIVE_ERRORS.includes(code) ? t(`aiConnections.liveErrors.${code}`) : code);

  /** Spec E2: GPT-Live's OpenAI connection, model and voice, and its Check. Shown only with LIVE_VOICE_TEST=1. The
   *  form shows the saved setup until the person changes something; Check tries the saved setup, so a change waits
   *  for Save. The status line is always about the saved setup. */
  const liveSection = () => {
    const saved = settings.live ?? null;
    const choice = liveDraft ?? liveChoice(settings);
    const openai = settings.connections.filter(connection => connection.kind === 'openai');
    const liveStatus = () => {
      if (liveChecking) return <span className="text-gray-500">{t('aiConnections.live.checking')}</span>;
      if (!saved) return <span className="text-gray-500">{t('aiConnections.live.notSaved')}</span>;
      if (saved.last_error) {
        return <span className="text-red-700"><XCircle className="inline w-4 h-4" /> {t('aiConnections.live.checkFailed', { error: liveError(saved.last_error) })}</span>;
      }
      if (saved.tested_at && saved.test_ms != null) {
        return <span className="text-green-700"><CheckCircle2 className="inline w-4 h-4" /> {t('aiConnections.live.checkOk', { seconds: (saved.test_ms / 1000).toFixed(1) })}</span>;
      }
      return <span className="text-gray-500">{t('aiConnections.live.notChecked')}</span>;
    };
    return (
      <div role="group" aria-labelledby="ai-live-title" className="space-y-2">
        <h3 id="ai-live-title" className="text-sm font-semibold text-gray-900">{t('aiConnections.live.title')}</h3>
        <p className="text-xs text-gray-500">{t('aiConnections.live.intro')}</p>
        {choice === null ? (
          <p className="text-sm text-gray-700">{t('aiConnections.live.noOpenAi')}</p>
        ) : (
          <form className="space-y-2" onSubmit={event => {
            event.preventDefault();
            void run('live', () => saveLive({ ...choice, model_id: choice.model_id.trim() }), () => setLiveDraft(null));
          }}>
            <div className="grid gap-2 sm:grid-cols-3">
              <label className="block text-xs text-gray-600">{t('aiConnections.live.connection')}
                <select className={field} aria-label={t('aiConnections.live.connection')} value={choice.connection_id}
                  onChange={event => setLiveDraft({ ...choice, connection_id: Number(event.target.value) })}>
                  {openai.map(connection => <option key={connection.connection_id} value={connection.connection_id}>{connection.name}</option>)}
                </select>
              </label>
              <label className="block text-xs text-gray-600">{t('aiConnections.live.model')}
                <input className={field} maxLength={100} value={choice.model_id}
                  onChange={event => setLiveDraft({ ...choice, model_id: event.target.value })} />
              </label>
              <label className="block text-xs text-gray-600">{t('aiConnections.live.voice')}
                <select className={field} aria-label={t('aiConnections.live.voice')} value={choice.voice}
                  onChange={event => setLiveDraft({ ...choice, voice: event.target.value })}>
                  {LIVE_VOICES.map(voice => <option key={voice} value={voice}>{voice}</option>)}
                </select>
              </label>
            </div>
            <div className="text-xs break-words">{liveStatus()}</div>
            {saved && liveUnsaved(choice, saved) && <p className="text-xs text-gray-500">{t('aiConnections.live.unsaved')}</p>}
            <div className="flex flex-wrap gap-2">
              <button type="submit" className={primary} disabled={busy || !choice.model_id.trim()}>{t('aiConnections.live.save')}</button>
              <button type="button" className={secondary} disabled={busy || liveUnsaved(choice, saved)} onClick={() => void runLiveCheck()}>
                {liveChecking ? <Loader2 className="w-4 h-4 animate-spin" aria-label={t('aiConnections.live.check')} /> : t('aiConnections.live.check')}
              </button>
            </div>
          </form>
        )}
        {!saved?.usable && <p className={warning}>{t('aiConnections.live.notReady')}</p>}
        {errorAt('live')}
      </div>
    );
  };

  return (
    <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-5" aria-labelledby="ai-title">
      {heading}

      {settings.weak_secret && <p role="alert" className={warning}><TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.weakSecret')}</p>}
      {settings.no_key_in_use && <p role="alert" className={warning}>{t('aiConnections.noKeyInUse')}</p>}
      {settings.seeded_at && <p className="text-xs text-gray-500">{t('aiConnections.seeded', { date: new Date(settings.seeded_at).toLocaleDateString() })}</p>}

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.connections')}</h3>
        {settings.connections.map(connection => (
          <div key={connection.connection_id} role="group" aria-label={connection.name} className="rounded-xl border border-gray-100 p-3 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm min-w-0">
                <span className="font-medium text-gray-900">{connection.name}</span>
                <span className="text-gray-500"> · {t(`aiConnections.kinds.${connection.kind}`)}</span>
                <div className="text-xs text-gray-500"><Lock className="inline w-3 h-3" /> {
                  !connection.key_readable ? t('aiConnections.keyUnreadable')
                    : connection.key_last4 ? t('aiConnections.keyShown', { last4: connection.key_last4 }) : t('aiConnections.noKey')}</div>
              </div>
              <div className="flex gap-2 shrink-0">
                <button type="button" className={secondary} disabled={busy}
                  onClick={() => setKeyDraft({ id: connection.connection_id, key: '' })}>{t('aiConnections.replaceKey')}</button>
                <button type="button" className={secondary} disabled={busy} aria-label={t('aiConnections.removeNamed', { name: connection.name })}
                  onClick={() => {
                    if (confirm(t('aiConnections.removeConfirm', { name: connection.name }))) {
                      void run(`connection:${connection.connection_id}`, () => deleteConnection(connection.connection_id));
                    }
                  }}><Trash2 className="w-4 h-4" /></button>
              </div>
            </div>
            {keyDraft?.id === connection.connection_id && (
              <form className="flex gap-2" onSubmit={event => {
                event.preventDefault();
                const key = keyDraft.key.trim();
                if (key) {
                  void run(`connection:${connection.connection_id}`, () => updateConnection(connection.connection_id, { api_key: key }),
                    () => setKeyDraft(null));
                }
              }}>
                <input type="password" autoComplete="off" className={field} aria-label={t('aiConnections.apiKey')} value={keyDraft.key}
                  onChange={event => setKeyDraft({ id: connection.connection_id, key: event.target.value })} />
                <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
                <button type="button" className={secondary} onClick={() => setKeyDraft(null)}>{t('aiConnections.cancel')}</button>
              </form>
            )}
            {errorAt(`connection:${connection.connection_id}`)}
          </div>
        ))}
        {connectionDraft ? (
          <form className="rounded-xl border border-gray-100 p-3 space-y-2" onSubmit={event => {
            event.preventDefault();
            const draft = connectionDraft;
            void run('newConnection', () => addConnection({ ...draft, base_url: draft.kind === 'openai_compatible' ? draft.base_url : undefined }),
              () => setConnectionDraft(null));
          }}>
            <label className="block text-xs text-gray-600">{t('aiConnections.kind')}
              <select className={field} aria-label={t('aiConnections.kind')} value={connectionDraft.kind} onChange={event => {
                const kind = event.target.value as AiKind;
                const name = kind === 'openai_compatible' ? '' : t(`aiConnections.kinds.${kind}`);
                setConnectionDraft({ ...connectionDraft, kind, name, service_name: name });
              }}>
                {AI_KINDS.map(kind => <option key={kind} value={kind}>{t(`aiConnections.kinds.${kind}`)}</option>)}
              </select>
            </label>
            <label className="block text-xs text-gray-600">{t('aiConnections.name')}
              <input className={field} value={connectionDraft.name} onChange={event => setConnectionDraft({ ...connectionDraft, name: event.target.value })} />
            </label>
            {connectionDraft.kind === 'openai_compatible' && (
              <label className="block text-xs text-gray-600">{t('aiConnections.baseUrl')}
                <input className={field} inputMode="url" value={connectionDraft.base_url}
                  onChange={event => setConnectionDraft({ ...connectionDraft, base_url: event.target.value })} />
              </label>
            )}
            <label className="block text-xs text-gray-600">{t('aiConnections.apiKey')}
              <input type="password" autoComplete="off" className={field} value={connectionDraft.api_key}
                onChange={event => setConnectionDraft({ ...connectionDraft, api_key: event.target.value })} />
            </label>
            <label className="block text-xs text-gray-600">{t('aiConnections.serviceName')}
              <input className={field} value={connectionDraft.service_name}
                onChange={event => setConnectionDraft({ ...connectionDraft, service_name: event.target.value })} />
            </label>
            <div className="flex gap-2">
              <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
              <button type="button" className={secondary} onClick={() => setConnectionDraft(null)}>{t('aiConnections.cancel')}</button>
            </div>
            {errorAt('newConnection')}
          </form>
        ) : (
          <button type="button" className={secondary} disabled={busy}
            onClick={() => {
              setError(null);
              setConnectionDraft({ kind: 'openai', name: t('aiConnections.kinds.openai'), base_url: '', api_key: '', service_name: t('aiConnections.kinds.openai') });
            }}>
            <Plus className="inline w-4 h-4" /> {t('aiConnections.addConnection')}
          </button>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.models')}</h3>
        {settings.models.map(model => (
          <div key={model.model_ref} role="group" aria-label={label(model)} className="rounded-xl border border-gray-100 p-3 space-y-2">
            {/* On a phone the buttons go under the text, so the name and the failure reason keep the row's width. */}
            <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
              <div className="text-sm min-w-0">
                <div className="font-medium text-gray-900 break-words">{label(model)}</div>
                <div className="text-xs break-words">{status(model)}</div>
                {model.profile && <div className="text-xs text-gray-500">{canThink(model)}</div>}
                <div className="text-xs text-gray-500 break-words">{[model.company, model.region, model.retention].map(value => value || '—').join(' · ')}</div>
              </div>
              <div className="flex flex-wrap gap-2 sm:justify-end sm:shrink-0">
                <button type="button" className={secondary} disabled={busy || testing !== null} onClick={() => void runTest(model.model_ref)}>
                  {testing === model.model_ref ? <Loader2 className="w-4 h-4 animate-spin" aria-label={t('aiConnections.test')} /> : t('aiConnections.test')}
                </button>
                <button type="button" className={secondary} disabled={busy}
                  onClick={() => setNoticeDraft({ ref: model.model_ref, company: model.company, region: model.region, retention: model.retention })}>
                  {t('aiConnections.editNotice')}
                </button>
                <button type="button" className={secondary} disabled={busy} aria-label={t('aiConnections.removeNamed', { name: model.model_id })}
                  onClick={() => {
                    if (confirm(t('aiConnections.removeModelConfirm', { name: label(model) }))) {
                      void run(`model:${model.model_ref}`, () => deleteModel(model.model_ref));
                    }
                  }}><Trash2 className="w-4 h-4" /></button>
              </div>
            </div>
            {noticeDraft?.ref === model.model_ref && (
              <form className="space-y-2" onSubmit={event => {
                event.preventDefault();
                const { ref, ...details } = noticeDraft;
                void run(`model:${ref}`, () => updateModel(ref, details), () => setNoticeDraft(null));
              }}>
                {(['company', 'region', 'retention'] as const).map(name => (
                  <label key={name} className="block text-xs text-gray-600">{t(`aiConnections.${name}`)}
                    <input className={field} value={noticeDraft[name]} onChange={event => setNoticeDraft({ ...noticeDraft, [name]: event.target.value })} />
                  </label>
                ))}
                <div className="flex gap-2">
                  <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
                  <button type="button" className={secondary} onClick={() => setNoticeDraft(null)}>{t('aiConnections.cancel')}</button>
                </div>
              </form>
            )}
            {errorAt(`model:${model.model_ref}`)}
          </div>
        ))}
        {modelDraft ? (
          <form className="rounded-xl border border-gray-100 p-3 space-y-2" onSubmit={event => {
            event.preventDefault();
            const draft = modelDraft;
            void run('newModel', () => addModel(draft), () => setModelDraft(null));
          }}>
            <label className="block text-xs text-gray-600">{t('aiConnections.connection')}
              <select className={field} aria-label={t('aiConnections.connection')} value={modelDraft.connection_id} onChange={event => {
                setRemoteModels([]);
                setModelDraft({ ...modelDraft, connection_id: Number(event.target.value) });
              }}>
                {settings.connections.map(connection => <option key={connection.connection_id} value={connection.connection_id}>{connection.name}</option>)}
              </select>
            </label>
            <label className="block text-xs text-gray-600">{t('aiConnections.modelId')}
              <input className={field} list="ai-remote-models" value={modelDraft.model_id}
                onChange={event => setModelDraft({ ...modelDraft, model_id: event.target.value })} />
            </label>
            <datalist id="ai-remote-models">{remoteModels.map(id => <option key={id} value={id} />)}</datalist>
            <button type="button" className={secondary} disabled={busy} onClick={() => void loadRemoteModels(modelDraft.connection_id)}>
              {t('aiConnections.loadList')}
            </button>
            {(['company', 'region', 'retention'] as const).map(name => (
              <label key={name} className="block text-xs text-gray-600">{t(`aiConnections.${name}`)}
                <input className={field} value={modelDraft[name]} onChange={event => setModelDraft({ ...modelDraft, [name]: event.target.value })} />
              </label>
            ))}
            <div className="flex gap-2">
              <button type="submit" className={primary} disabled={busy}>{t('aiConnections.add')}</button>
              <button type="button" className={secondary} onClick={() => setModelDraft(null)}>{t('aiConnections.cancel')}</button>
            </div>
            {errorAt('newModel')}
          </form>
        ) : (
          <button type="button" className={secondary} disabled={busy || !settings.connections.length}
            onClick={() => {
              setError(null);
              setRemoteModels([]);
              setModelDraft({ connection_id: settings.connections[0].connection_id, model_id: '', company: '', region: '', retention: '' });
            }}>
            <Plus className="inline w-4 h-4" /> {t('aiConnections.addModel')}
          </button>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.jobs')}</h3>
        <p className="text-xs text-gray-500">{t('aiConnections.onlyTested')}</p>
        {AI_JOBS.map(job => (
          <div key={job} role="group" aria-label={t(`aiConnections.jobNames.${job}`)} className="space-y-2">
            <div className="grid gap-2 sm:grid-cols-3 sm:items-end">
              <span className="text-sm text-gray-900 sm:pb-2">{t(`aiConnections.jobNames.${job}`)}</span>
              <label className="block text-xs text-gray-600">{t('aiConnections.main')}
                <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.main')}`} value={jobs[job].primary_ref}
                  onChange={event => {
                    setJobsSaved(false);
                    setJobs({ ...jobs, [job]: withMainModel(job, Number(event.target.value)) });
                  }}>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
              <label className="block text-xs text-gray-600">{t('aiConnections.backup')}
                <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.backup')}`} value={jobs[job].fallback_ref ?? ''}
                  onChange={event => {
                    setJobsSaved(false);
                    setJobs({ ...jobs, [job]: { ...jobs[job], fallback_ref: event.target.value ? Number(event.target.value) : null } });
                  }}>
                  <option value="">{t('aiConnections.noBackup')}</option>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
              {thinkingControls(job)}
            </div>
            {/* The saved setup, as the server judged it; `?.`: change answers are not shape-checked like the first load. */}
            {settings.jobs_usable?.[job] === false && (
              <p role="alert" className={warning}>
                <TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.jobUnusable')}
                {job !== 'reply' && <span className="block mt-1">{t(`aiConnections.jobUnusableEffect.${job}`)}</span>}
              </p>
            )}
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={primary} disabled={busy}
            onClick={() => void run('jobs', () => saveJobs(jobs), () => setJobsSaved(true), true)}>{t('aiConnections.saveJobs')}</button>
          <p role="status" className="text-sm text-green-700">{jobsSaved ? t('aiConnections.jobsSaved') : ''}</p>
        </div>
        {errorAt('jobs')}
      </div>

      {/* `=== true`: change answers are not shape-checked like the first load, so a missing field hides the section. */}
      {settings.live_voice_test === true && liveSection()}
    </section>
  );
}
