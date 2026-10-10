import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Brain, CheckCircle2, Loader2, Lock, Plus, Trash2, TriangleAlert, XCircle } from 'lucide-react';
import { ApiError } from '../lib/reachy-api';
import {
  AI_JOBS, AI_KINDS, addConnection, addModel, deleteConnection, deleteModel, fetchAiSettings, fetchRemoteModels,
  saveJobs, testModel, updateConnection, updateModel,
  type AiJob, type AiJobChoice, type AiKind, type AiModel, type AiSettings, type AiTestResult,
} from '../lib/ai-connections-api';

/** Codes api_ai.py answers with; anything else shows the general message. */
const KNOWN_ERRORS = ['in_use', 'untested_model', 'not_found', 'invalid_base_url', 'invalid_name', 'invalid_kind',
  'key_unreadable', 'no_key', 'already_exists', 'notice_incomplete', 'remote_models_unavailable', 'invalid_request'];
const field = 'w-full px-3 py-2 border border-gray-200 rounded-xl text-sm';
const primary = 'px-3 py-2 rounded-xl text-sm font-medium bg-[#0057B8] text-white disabled:opacity-50';
const secondary = 'px-3 py-2 rounded-xl text-sm font-medium border border-gray-200 text-gray-700 disabled:opacity-50';
const warning = 'rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900';

interface ConnectionDraft { kind: AiKind; name: string; base_url: string; api_key: string; service_name: string }
interface ModelDraft { connection_id: number; model_id: string; company: string; region: string; retention: string }
interface NoticeDraft { ref: number; company: string; region: string; retention: string }

const errorCode = (cause: unknown) => (cause instanceof ApiError && KNOWN_ERRORS.includes(cause.message) ? cause.message : 'failed');

export default function AiConnectionsCard() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [jobs, setJobs] = useState<Record<AiJob, AiJobChoice> | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, AiTestResult>>({});
  const [connectionDraft, setConnectionDraft] = useState<ConnectionDraft | null>(null);
  const [modelDraft, setModelDraft] = useState<ModelDraft | null>(null);
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [keyDraft, setKeyDraft] = useState<{ id: number; key: string } | null>(null);
  const [noticeDraft, setNoticeDraft] = useState<NoticeDraft | null>(null);

  useEffect(() => {
    let active = true;
    fetchAiSettings().then(
      next => { if (active) { setSettings(next); setJobs(next.jobs); } },
      () => { if (active) setLoadFailed(true); },
    );
    return () => { active = false; };
  }, []);

  /** One change; the server answers with the whole card. `done` runs only after a success (e.g. closing a form). */
  const run = async (action: () => Promise<AiSettings>, done?: () => void) => {
    setBusy(true);
    setError('');
    try {
      const next = await action();
      setSettings(next);
      setJobs(next.jobs);
      done?.();
    } catch (cause) {
      setError(errorCode(cause));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async (ref: number) => {
    setTesting(ref);
    setError('');
    try {
      const { settings: next, ...result } = await testModel(ref);
      setResults(previous => ({ ...previous, [ref]: result }));
      setSettings(next);
    } catch (cause) {
      setError(errorCode(cause));
    } finally {
      setTesting(null);
    }
  };

  const loadRemoteModels = async (id: number) => {
    setBusy(true);
    setError('');
    try {
      setRemoteModels((await fetchRemoteModels(id)).models);
    } catch (cause) {
      setError(errorCode(cause));
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

  const status = (model: AiModel) => {
    const result = results[model.model_ref];
    if (result && !result.ok) return <span className="text-red-700"><XCircle className="inline w-4 h-4" /> {result.error}</span>;
    if (model.last_error) return <span className="text-red-700">{t('aiConnections.lastCallFailed', { error: model.last_error })}</span>;
    if (model.test_ms != null) {
      return (
        <span className="text-green-700"><CheckCircle2 className="inline w-4 h-4" /> {t('aiConnections.testOk', { seconds: (model.test_ms / 1000).toFixed(1) })}
          {result?.sample ? ` · ${result.sample}` : ''}</span>
      );
    }
    if (model.copied_from_env) return <span className="text-gray-500">{t('aiConnections.copiedFromEnv')}</span>;
    return <span className="text-gray-500">{t('aiConnections.notTested')}</span>;
  };

  return (
    <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-5" aria-labelledby="ai-title">
      {heading}

      {settings.weak_secret && <p role="alert" className={warning}><TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.weakSecret')}</p>}
      {settings.no_key_in_use && <p role="alert" className={warning}>{t('aiConnections.noKeyInUse')}</p>}
      {settings.seeded_at && <p className="text-xs text-gray-500">{t('aiConnections.seeded', { date: new Date(settings.seeded_at).toLocaleDateString() })}</p>}
      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t(`aiConnections.errors.${error}`)}</p>}

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
                  onClick={() => void run(() => deleteConnection(connection.connection_id))}><Trash2 className="w-4 h-4" /></button>
              </div>
            </div>
            {keyDraft?.id === connection.connection_id && (
              <form className="flex gap-2" onSubmit={event => {
                event.preventDefault();
                const key = keyDraft.key.trim();
                if (key) void run(() => updateConnection(connection.connection_id, { api_key: key }), () => setKeyDraft(null));
              }}>
                <input type="password" autoComplete="off" className={field} aria-label={t('aiConnections.apiKey')} value={keyDraft.key}
                  onChange={event => setKeyDraft({ id: connection.connection_id, key: event.target.value })} />
                <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
                <button type="button" className={secondary} onClick={() => setKeyDraft(null)}>{t('aiConnections.cancel')}</button>
              </form>
            )}
          </div>
        ))}
        {connectionDraft ? (
          <form className="rounded-xl border border-gray-100 p-3 space-y-2" onSubmit={event => {
            event.preventDefault();
            const draft = connectionDraft;
            void run(() => addConnection({ ...draft, base_url: draft.kind === 'openai_compatible' ? draft.base_url : undefined }),
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
          </form>
        ) : (
          <button type="button" className={secondary} disabled={busy}
            onClick={() => setConnectionDraft({ kind: 'openai', name: t('aiConnections.kinds.openai'), base_url: '', api_key: '', service_name: t('aiConnections.kinds.openai') })}>
            <Plus className="inline w-4 h-4" /> {t('aiConnections.addConnection')}
          </button>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.models')}</h3>
        {settings.models.map(model => (
          <div key={model.model_ref} role="group" aria-label={label(model)} className="rounded-xl border border-gray-100 p-3 space-y-2">
            <div className="flex items-start justify-between gap-2">
              <div className="text-sm min-w-0">
                <div className="font-medium text-gray-900 break-all">{label(model)}</div>
                <div className="text-xs">{status(model)}</div>
                <div className="text-xs text-gray-500">{[model.company, model.region, model.retention].map(value => value || '—').join(' · ')}</div>
              </div>
              <div className="flex flex-wrap gap-2 justify-end shrink-0">
                <button type="button" className={secondary} disabled={busy || testing !== null} onClick={() => void runTest(model.model_ref)}>
                  {testing === model.model_ref ? <Loader2 className="w-4 h-4 animate-spin" aria-label={t('aiConnections.test')} /> : t('aiConnections.test')}
                </button>
                <button type="button" className={secondary} disabled={busy}
                  onClick={() => setNoticeDraft({ ref: model.model_ref, company: model.company, region: model.region, retention: model.retention })}>
                  {t('aiConnections.editNotice')}
                </button>
                <button type="button" className={secondary} disabled={busy} aria-label={t('aiConnections.removeNamed', { name: model.model_id })}
                  onClick={() => void run(() => deleteModel(model.model_ref))}><Trash2 className="w-4 h-4" /></button>
              </div>
            </div>
            {noticeDraft?.ref === model.model_ref && (
              <form className="space-y-2" onSubmit={event => {
                event.preventDefault();
                const { ref, ...details } = noticeDraft;
                void run(() => updateModel(ref, details), () => setNoticeDraft(null));
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
          </div>
        ))}
        {modelDraft ? (
          <form className="rounded-xl border border-gray-100 p-3 space-y-2" onSubmit={event => {
            event.preventDefault();
            const draft = modelDraft;
            void run(() => addModel(draft), () => setModelDraft(null));
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
          </form>
        ) : (
          <button type="button" className={secondary} disabled={busy || !settings.connections.length}
            onClick={() => {
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
          <div key={job} className="grid gap-2 sm:grid-cols-3 sm:items-center">
            <span className="text-sm text-gray-900">{t(`aiConnections.jobNames.${job}`)}</span>
            <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.main')}`} value={jobs[job].primary_ref}
              onChange={event => setJobs({ ...jobs, [job]: { ...jobs[job], primary_ref: Number(event.target.value) } })}>
              {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
            </select>
            <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.backup')}`} value={jobs[job].fallback_ref ?? ''}
              onChange={event => setJobs({ ...jobs, [job]: { ...jobs[job], fallback_ref: event.target.value ? Number(event.target.value) : null } })}>
              <option value="">{t('aiConnections.noBackup')}</option>
              {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
            </select>
          </div>
        ))}
        <button type="button" className={primary} disabled={busy} onClick={() => void run(() => saveJobs(jobs))}>{t('aiConnections.saveJobs')}</button>
      </div>
    </section>
  );
}
