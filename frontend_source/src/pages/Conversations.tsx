import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Bot, ChevronDown, ChevronUp, Clock, Download, Loader2, MessageCircle, Trash2, User } from 'lucide-react';
import {
  deleteConversation, fetchConversation, fetchConversationMetricTurns, fetchConversationMetricsSummary, fetchConversations,
  type ConversationDetail, type ConversationMetricTurn, type ConversationMetricsSummary, type ConversationSummary,
  type ConversationTurn, type Mood, type TurnMetrics,
} from '../lib/reachy-api';
import MemoryPanel from '../components/MemoryPanel';

const PAGE_SIZE = 20;
const POLL_MS = 3000;
// With nothing live, the first page still looks for a check-in that has just started, but slowly.
const IDLE_POLL_MS = 15000;
// The robot ends a check-in within 5 minutes (CHECKIN_MAX). One still open long after that was never closed
// (robot crash, power or network loss), and nothing on the server closes it, so it is no longer live.
const LIVE_WINDOW_MS = 15 * 60_000;
const TIMING_DAYS = 7;
// The wait the patient feels first, then each step of a turn in order.
const STAGES = [
  'speech_end_to_first_sound_ms', 'vad_release_ms', 'stt_ms', 'handover_ms',
  'round_trip_ms', 'llm_ms', 'tts_first_audio_ms', 'tts_total_ms',
] as const;

const MOOD_STYLE: Record<Mood, string> = {
  happy: 'bg-green-50 text-green-700',
  calm: 'bg-blue-50 text-blue-700',
  sad: 'bg-indigo-50 text-indigo-700',
  worried: 'bg-amber-50 text-amber-800',
  angry: 'bg-red-50 text-red-700',
  unknown: 'bg-gray-100 text-gray-600',
};

export function MoodBadge({ mood }: { mood: Mood | null }) {
  const { t } = useTranslation();
  const value: Mood = mood ?? 'unknown';
  return <span className={`px-2.5 py-1 rounded-full text-sm font-medium ${MOOD_STYLE[value]}`}>{t(`conversations.mood.${value}`)}</span>;
}

const onVisibilityChange = (notify: () => void) => {
  document.addEventListener('visibilitychange', notify);
  return () => document.removeEventListener('visibilitychange', notify);
};
const pageVisible = () => document.visibilityState === 'visible';

/** Runs `tick` every `intervalMs` while `active` and the tab is visible. A slow tick delays the next one instead of overlapping it. */
function usePolling(tick: () => Promise<void>, active: boolean, intervalMs = POLL_MS) {
  const visible = useSyncExternalStore(onVisibilityChange, pageVisible);
  useEffect(() => {
    if (!active || !visible) return;
    let stopped = false;
    let timer = 0;
    const run = async () => {
      try { await tick(); } catch { /* the next tick tries again */ }
      if (!stopped) timer = window.setTimeout(run, intervalMs);
    };
    timer = window.setTimeout(run, intervalMs);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [tick, active, visible, intervalMs]);
}

/** Not ended, and started recently enough that the robot can still be talking (`now` = when the list was fetched). */
const isLive = (conversation: { started_at: string; ended_at: string | null }, now: number) =>
  conversation.ended_at === null && now - Date.parse(conversation.started_at) < LIVE_WINDOW_MS;

/** metrics.robot or metrics.server; {} when the turn has none. */
function group(metrics: TurnMetrics | null, key: 'robot' | 'server'): Record<string, unknown> {
  const value = metrics?.[key];
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

const msValue = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null);

const seconds = (ms: number) => (ms / 1000).toFixed(ms < 1000 ? 2 : 1);

/** The model that wrote the reply: the last usable attempt. None when the fixed fallback line was used. */
function servedModel(attempts: unknown): string | null {
  if (!Array.isArray(attempts)) return null;
  const used = [...attempts].reverse().find((attempt) => attempt?.usable === true) as Record<string, unknown> | undefined;
  if (!used) return null;
  const model = used.model_served || used.model_requested;
  return typeof model === 'string' ? model : null;
}

/** The grey timing line under a transcript bubble; nothing for turns recorded without timings. */
function TurnTiming({ turn }: { turn: ConversationTurn }) {
  const { t } = useTranslation();
  const robot = group(turn.metrics, 'robot');
  const parts: string[] = [];
  const add = (key: string, ms: number | null) => { if (ms !== null) parts.push(t(key, { s: seconds(ms) })); };
  if (turn.role === 'patient') {
    add('conversations.timing.heard', msValue(robot.handover_ms));
    add('conversations.timing.stt', msValue(robot.stt_ms));
  } else {
    const server = group(turn.metrics, 'server');
    const llm = msValue(server.llm_ms);
    // Safety, goodbye and turn-limit replies make no AI call, so they have no reply time.
    add('conversations.timing.reply', llm !== null && llm > 0 ? llm : null);
    const model = servedModel(server.attempts);
    if (model) parts.push(model);
    if (server.fallback_used === true) parts.push(t('conversations.timing.fallback'));
    add('conversations.timing.firstSound', msValue(robot.tts_first_audio_ms));
  }
  if (parts.length === 0) return null;
  return <p className="mt-1 px-2 text-xs text-gray-500">{parts.join(' · ')}</p>;
}

/** {"robot": {...}, "server": {...}} → {"robot.stt_ms": ..., "server.attempts": "[...]"}; lists become JSON. */
function flatten(value: Record<string, unknown>, prefix = '', into: Record<string, unknown> = {}) {
  for (const [key, inner] of Object.entries(value)) {
    const name = prefix ? `${prefix}.${key}` : key;
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) flatten(inner as Record<string, unknown>, name, into);
    else into[name] = Array.isArray(inner) ? JSON.stringify(inner) : inner;
  }
  return into;
}

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  let text = String(value);
  // A text cell starting with = + - @ would run as a spreadsheet formula.
  if (typeof value === 'string' && /^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** One row per turn, one column per metric key (sorted, so robot.* then server.*). */
function timingsCsv(items: ConversationMetricTurn[]): string {
  const rows = items.map((item) => ({ item, metrics: flatten(item.metrics ?? {}) }));
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row.metrics)))].sort();
  const header = ['conversation_id', 'turn_id', 'role', 'created_at', 'text_chars', ...keys];
  const lines = rows.map(({ item, metrics }) =>
    [item.conversation_id, item.turn_id, item.role, item.created_at, item.text_chars, ...keys.map((key) => metrics[key])]);
  return [header, ...lines].map((cells) => cells.map(csvCell).join(',')).join('\r\n') + '\r\n';
}

function saveFile(name: string, text: string) {
  // The byte-order mark makes Excel read the file as UTF-8.
  const url = URL.createObjectURL(new Blob(['﻿', text], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function TimingsPanel({ summary, stages, onError }: {
  summary: ConversationMetricsSummary;
  stages: readonly (typeof STAGES)[number][];
  onError: () => void;
}) {
  const { t } = useTranslation();
  const [downloading, setDownloading] = useState(false);
  const cell = (ms: number | null | undefined) => (ms == null ? '–' : t('conversations.timings.seconds', { s: seconds(ms) }));

  const download = async () => {
    setDownloading(true);
    try {
      const { items } = await fetchConversationMetricTurns(summary.days);
      saveFile(`reachy-timings-${new Date().toISOString().slice(0, 10)}.csv`, timingsCsv(items));
    } catch {
      onError();
    }
    setDownloading(false);
  };

  return (
    <section aria-labelledby="timings-title" className="bg-white rounded-xl border border-gray-100 shadow-sm p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="timings-title" className="flex items-center gap-2 font-semibold text-gray-900">
          <Clock className="w-5 h-5 text-[#0057B8]" />{t('conversations.timings.title', { days: summary.days })}
        </h3>
        <button onClick={() => void download()} disabled={downloading}
          className="flex items-center gap-1 text-sm text-[#0057B8] hover:underline disabled:opacity-50">
          {downloading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
          {t('conversations.timings.download')}
        </button>
      </div>
      {stages.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th className="font-medium py-1">{t('conversations.timings.stage')}</th>
              <th className="font-medium py-1 text-right">{t('conversations.timings.median')}</th>
              <th className="font-medium py-1 pl-2 text-right">{t('conversations.timings.p90')}</th>
              <th className="font-medium py-1 pl-2 text-right hidden sm:table-cell">{t('conversations.timings.count')}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {stages.map((stage) => {
              const timing = summary.stages[stage];
              return (
                <tr key={stage} title={stage}
                  className={stage === 'speech_end_to_first_sound_ms' ? 'font-semibold text-gray-900' : 'text-gray-700'}>
                  <td className="py-1.5 pr-2">{t(`conversations.timings.stages.${stage}`)}</td>
                  <td className="py-1.5 text-right tabular-nums whitespace-nowrap">{cell(timing.median_ms)}</td>
                  <td className="py-1.5 pl-2 text-right tabular-nums whitespace-nowrap">{cell(timing.p90_ms)}</td>
                  <td className="py-1.5 pl-2 text-right tabular-nums text-gray-500 hidden sm:table-cell">{timing.count}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {summary.fallback_rate != null && (
        <p className="text-sm text-gray-700">
          {t('conversations.timings.fallbackRate', { percent: Math.round(summary.fallback_rate * 100) })}
        </p>
      )}
      {(summary.models ?? []).length > 0 && (
        <div className="text-sm">
          <p className="text-gray-500">{t('conversations.timings.models')}</p>
          <ul className="mt-1 space-y-0.5">
            {summary.models.map((model) => (
              <li key={model.model} className="flex flex-wrap justify-between gap-x-3 text-gray-700">
                <span className="break-all">{model.model}</span>
                <span className="tabular-nums text-gray-500">
                  {t('conversations.timings.modelLine', { count: model.count, s: model.median_ms == null ? '–' : seconds(model.median_ms) })}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export default function Conversations() {
  const { t } = useTranslation();
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [open, setOpen] = useState<ConversationDetail | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [timings, setTimings] = useState<ConversationMetricsSummary | null>(null);
  const [timingsVersion, setTimingsVersion] = useState(0);
  // When the list on screen was fetched: the clock that live checks use, so render stays pure.
  const [checkedAt, setCheckedAt] = useState(0);
  const listRequest = useRef(0);
  const wasLive = useRef(false);

  // quiet = a background refresh: no spinner, and a failure keeps the list on screen.
  const load = useCallback(async (quiet = false) => {
    const request = ++listRequest.current;
    if (!quiet) {
      setLoading(true);
      setError('');
    }
    try {
      const page = await fetchConversations(PAGE_SIZE, offset);
      // A refresh still in flight from before a page change must not overwrite the newer page.
      if (request === listRequest.current) {
        const now = Date.now();
        setItems(page.items);
        setTotal(page.total);
        setCheckedAt(now);
        // The last live conversation just ended: its timings are complete, so refresh the summary.
        const liveNow = page.items.some((item) => isLive(item, now));
        if (wasLive.current && !liveNow) setTimingsVersion((version) => version + 1);
        wasLive.current = liveNow;
      }
    } catch {
      if (!quiet) setError(t('conversations.loadFailed'));
    }
    if (!quiet) setLoading(false);
  }, [offset, t]);

  useEffect(() => { void load(); }, [load]);

  // Live update: poll the list every 3 s while a listed conversation is still going, and the open transcript
  // while it is. With nothing live, the first page is still re-read slowly so a new check-in shows up.
  const live = items.some((item) => isLive(item, checkedAt));
  const refreshList = useCallback(() => load(true), [load]);
  usePolling(refreshList, live || offset === 0, live ? POLL_MS : IDLE_POLL_MS);

  const liveId = open && isLive(open, checkedAt) ? open.id : null;
  const refreshOpen = useCallback(async () => {
    if (!liveId) return;
    const detail = await fetchConversation(liveId);
    setOpen((current) => (current?.id === liveId ? detail : current));
  }, [liveId]);
  usePolling(refreshOpen, liveId !== null);

  // Timings load on mount, then again when the last live conversation ends or one is deleted. A live
  // conversation does not hold them back; only a newer request (or leaving the page) discards an answer.
  useEffect(() => {
    let current = true;
    fetchConversationMetricsSummary(TIMING_DAYS)
      .then((summary) => { if (current) setTimings(summary); })
      .catch(() => { /* keep what is shown; the next refresh tries again */ });
    return () => { current = false; };
  }, [timingsVersion]);

  const timingStages = STAGES.filter((stage) => (timings?.stages?.[stage]?.count ?? 0) > 0);
  const showTimings = timings !== null
    && (timingStages.length > 0 || (timings.models ?? []).length > 0 || timings.fallback_rate != null);

  const toggle = async (id: string) => {
    if (open?.id === id) { setOpen(null); return; }
    setOpening(id);
    try {
      setOpen(await fetchConversation(id));
    } catch {
      setError(t('conversations.loadFailed'));
    }
    setOpening(null);
  };

  const remove = async (id: string) => {
    if (!confirm(t('conversations.deleteConfirm'))) return;
    await deleteConversation(id).catch(() => setError(t('conversations.loadFailed')));
    if (open?.id === id) setOpen(null);
    void load();
    setTimingsVersion((version) => version + 1);
  };

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-2xl font-bold text-gray-900">{t('conversations.title')}</h2>
        <p className="text-base text-gray-500 mt-1">{t('conversations.subtitle')}</p>
      </div>

      <MemoryPanel />

      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      {showTimings && timings && (
        <TimingsPanel summary={timings} stages={timingStages}
          onError={() => setError(t('conversations.timings.downloadFailed'))} />
      )}

      {loading ? (
        <div className="flex items-center justify-center h-40 gap-3 text-gray-500">
          <Loader2 className="w-6 h-6 animate-spin text-[#0057B8]" />{t('common.loading')}
        </div>
      ) : items.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-8 text-center text-gray-500">
          <MessageCircle className="w-12 h-12 text-gray-300 mx-auto mb-3" />
          {t('conversations.empty')}
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => {
            const expanded = open?.id === item.id;
            return (
              <article key={item.id} className={`bg-white rounded-xl border shadow-sm ${item.risk_flag ? 'border-red-200' : 'border-gray-100'}`}>
                <button
                  onClick={() => void toggle(item.id)}
                  aria-expanded={expanded}
                  className="w-full flex items-start gap-3 p-4 text-left"
                >
                  <div className="w-10 h-10 bg-blue-50 rounded-full flex items-center justify-center shrink-0">
                    <Bot className="w-5 h-5 text-[#0057B8]" />
                  </div>
                  <div className="flex-1 min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-gray-900">{new Date(item.started_at).toLocaleString()}</span>
                      <MoodBadge mood={item.mood} />
                      {item.risk_flag && (
                        <span className="flex items-center gap-1 px-2.5 py-1 rounded-full text-sm font-medium bg-red-50 text-red-700">
                          <AlertTriangle className="w-3.5 h-3.5" />{t('conversations.safetyAlert')}
                        </span>
                      )}
                    </div>
                    <p className="text-base text-gray-700">
                      {item.summary || item.first_words
                        || (isLive(item, checkedAt) ? t('conversations.inProgress') : t('conversations.noAnswer'))}
                    </p>
                    <p className="text-sm text-gray-500">{t('conversations.turns', { count: item.patient_turns })}</p>
                  </div>
                  {opening === item.id ? <Loader2 className="w-5 h-5 animate-spin text-gray-400" />
                    : expanded ? <ChevronUp className="w-5 h-5 text-gray-400" /> : <ChevronDown className="w-5 h-5 text-gray-400" />}
                </button>
                {expanded && open && (
                  <div className="border-t border-gray-100 p-4 space-y-3">
                    {open.turns.length === 0 && <p className="text-sm text-gray-500">{t('conversations.transcriptExpired')}</p>}
                    {open.turns.map((turn, index) => (
                      <div key={turn.turn_id ?? index} className={`flex gap-2 ${turn.role === 'patient' ? 'justify-end' : ''}`}>
                        {turn.role === 'reachy' && <Bot className="w-5 h-5 text-[#0057B8] shrink-0 mt-1" aria-label="Reachy" />}
                        <div className={`max-w-[80%] flex flex-col ${turn.role === 'patient' ? 'items-end' : 'items-start'}`}>
                          <p className={`rounded-2xl px-4 py-2 text-base ${
                            turn.role === 'patient'
                              ? `bg-[#0057B8] text-white ${turn.flagged ? 'ring-2 ring-red-400' : ''}`
                              : 'bg-gray-100 text-gray-900'}`}>
                            {turn.text}
                          </p>
                          <TurnTiming turn={turn} />
                        </div>
                        {turn.role === 'patient' && <User className="w-5 h-5 text-gray-400 shrink-0 mt-1" aria-label={t('conversations.you')} />}
                      </div>
                    ))}
                    <div className="flex justify-between items-center pt-2">
                      <span className="text-sm text-gray-400">{t('conversations.retention')}</span>
                      <button onClick={() => void remove(item.id)} className="flex items-center gap-1 text-sm text-red-600 hover:underline">
                        <Trash2 className="w-4 h-4" />{t('common.delete')}
                      </button>
                    </div>
                  </div>
                )}
              </article>
            );
          })}
          {total > PAGE_SIZE && (
            <div className="flex justify-between pt-2">
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                className="px-4 py-2 rounded-xl border disabled:opacity-40">{t('history.newer')}</button>
              <button disabled={offset + items.length >= total} onClick={() => setOffset(offset + PAGE_SIZE)}
                className="px-4 py-2 rounded-xl border disabled:opacity-40">{t('history.older')}</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
