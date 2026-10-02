import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Bot, ChevronDown, ChevronUp, Loader2, MessageCircle, Trash2, User } from 'lucide-react';
import {
  deleteConversation, fetchConversation, fetchConversations,
  type ConversationDetail, type ConversationSummary, type Mood,
} from '../lib/reachy-api';

const PAGE_SIZE = 20;

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

export default function Conversations() {
  const { t } = useTranslation();
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [open, setOpen] = useState<ConversationDetail | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const page = await fetchConversations(PAGE_SIZE, offset);
      setItems(page.items);
      setTotal(page.total);
    } catch {
      setError(t('conversations.loadFailed'));
    }
    setLoading(false);
  }, [offset, t]);

  useEffect(() => { void load(); }, [load]);

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
  };

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-2xl font-bold text-gray-900">{t('conversations.title')}</h2>
        <p className="text-base text-gray-500 mt-1">{t('conversations.subtitle')}</p>
      </div>

      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>}

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
                      {item.summary || item.first_words || (item.ended_at ? t('conversations.noAnswer') : t('conversations.inProgress'))}
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
                      <div key={index} className={`flex gap-2 ${turn.role === 'patient' ? 'justify-end' : ''}`}>
                        {turn.role === 'reachy' && <Bot className="w-5 h-5 text-[#0057B8] shrink-0 mt-1" aria-label="Reachy" />}
                        <p className={`max-w-[80%] rounded-2xl px-4 py-2 text-base ${
                          turn.role === 'patient'
                            ? `bg-[#0057B8] text-white ${turn.flagged ? 'ring-2 ring-red-400' : ''}`
                            : 'bg-gray-100 text-gray-900'}`}>
                          {turn.text}
                        </p>
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
