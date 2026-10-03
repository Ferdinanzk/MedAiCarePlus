import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Brain, Loader2, Pencil, Plus, Trash2 } from 'lucide-react';
import { addMemory, deleteMemoryFact, fetchMemory, updateMemory, type MemoryFact, type MemoryKind } from '../lib/memory-api';

const ORDER: MemoryKind[] = ['name', 'event', 'person', 'like', 'routine'];
const ERRORS: Record<string, string> = { invalid_fact: 'memory.invalid', memory_consent_required: 'memory.consentRequired' };

export default function MemoryPanel() {
  const { t } = useTranslation();
  const [items, setItems] = useState<MemoryFact[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<MemoryFact | null>(null);
  const [form, setForm] = useState<{ kind: MemoryKind; text: string; event_date: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await fetchMemory();
      setItems(list.items);
      setEnabled(list.enabled);
      setError('');
    } catch {
      setError('memory.loadFailed');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await action();
      setForm(null);
      setEditing(null);
      await load();
    } catch (cause) {
      setError(ERRORS[cause instanceof Error ? cause.message : ''] ?? 'memory.saveFailed');
    } finally {
      setBusy(false);
    }
  };

  const save = () => form && run(() => (editing
    ? updateMemory(editing.kind, editing.subject, { text: form.text, event_date: form.event_date || null })
    : addMemory({ kind: form.kind, text: form.text, event_date: form.event_date || null })));

  const remove = (fact: MemoryFact) => {
    if (!confirm(t('memory.deleteConfirm'))) return;
    void run(() => deleteMemoryFact(fact.kind, fact.subject));
  };

  const startEdit = (fact: MemoryFact) => {
    setEditing(fact);
    setForm({ kind: fact.kind, text: fact.text, event_date: fact.event_date ?? '' });
  };

  if (loading) return <p role="status" className="flex items-center gap-2 text-gray-500"><Loader2 className="w-4 h-4 animate-spin" />{t('common.loading')}</p>;

  return (
    <section aria-labelledby="memory-title" className="bg-white rounded-xl border border-gray-100 shadow-sm p-4 space-y-4">
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 bg-blue-50 rounded-full flex items-center justify-center shrink-0"><Brain className="w-5 h-5 text-[#0057B8]" /></div>
        <div>
          <h3 id="memory-title" className="text-lg font-semibold text-gray-900">{t('memory.title')}</h3>
          <p className="text-sm text-gray-500">{enabled ? t('memory.subtitle') : t('memory.off')}</p>
        </div>
      </div>

      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t(error)}</p>}

      {items.length === 0 && <p className="text-sm text-gray-500">{t('memory.empty')}</p>}

      {ORDER.map(kind => {
        const group = items.filter(item => item.kind === kind);
        if (group.length === 0) return null;
        return (
          <div key={kind} className="space-y-2">
            <h4 className="text-sm font-semibold text-gray-700">{t(`memory.kind.${kind}`)}</h4>
            <ul className="space-y-2">
              {group.map(fact => (
                <li key={`${fact.kind}:${fact.subject}`} className="flex items-start gap-3 rounded-xl bg-gray-50 p-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-base text-gray-900 break-words">{fact.text}</p>
                    <p className="text-xs text-gray-500 mt-1">
                      {fact.event_date && <span className="mr-2">{fact.event_date}</span>}
                      {fact.source === 'patient' ? t('memory.youAdded') : t('memory.fromChats', { count: fact.conversation_ids.length })}
                    </p>
                  </div>
                  {enabled && (
                    <button onClick={() => startEdit(fact)} disabled={busy} aria-label={t('memory.edit')} className="min-h-12 min-w-12 flex items-center justify-center rounded-lg text-gray-600 hover:bg-gray-100">
                      <Pencil className="w-4 h-4" />
                    </button>
                  )}
                  <button onClick={() => remove(fact)} disabled={busy} aria-label={t('common.delete')} className="min-h-12 min-w-12 flex items-center justify-center rounded-lg text-red-600 hover:bg-red-50">
                    <Trash2 className="w-4 h-4" />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        );
      })}

      {enabled && !form && (
        <button onClick={() => { setEditing(null); setForm({ kind: 'person', text: '', event_date: '' }); }} className="min-h-12 px-4 py-2 rounded-xl border border-[#0057B8] text-[#0057B8] font-medium flex items-center gap-2">
          <Plus className="w-4 h-4" />{t('memory.add')}
        </button>
      )}

      {form && (
        <div className="space-y-3 rounded-xl border border-gray-200 p-3">
          {!editing && (
            <label className="block text-sm text-gray-700">{t('memory.kindLabel')}
              <select value={form.kind} onChange={event => setForm({ ...form, kind: event.target.value as MemoryKind })} className="mt-1 block w-full min-h-12 rounded-lg border border-gray-300 px-3">
                {ORDER.map(kind => <option key={kind} value={kind}>{t(`memory.kind.${kind}`)}</option>)}
              </select>
            </label>
          )}
          <label className="block text-sm text-gray-700">{form.kind === 'name' ? t('memory.nameLabel') : t('memory.textLabel')}
            <input value={form.text} maxLength={160} onChange={event => setForm({ ...form, text: event.target.value })} className="mt-1 block w-full min-h-12 rounded-lg border border-gray-300 px-3" />
          </label>
          {form.kind === 'event' && (
            <label className="block text-sm text-gray-700">{t('memory.dateLabel')}
              <input type="date" value={form.event_date} onChange={event => setForm({ ...form, event_date: event.target.value })} className="mt-1 block w-full min-h-12 rounded-lg border border-gray-300 px-3" />
            </label>
          )}
          <div className="flex gap-3">
            <button onClick={() => void save()} disabled={busy || !form.text.trim()} className="min-h-12 px-5 py-2 rounded-xl bg-[#0057B8] text-white font-semibold disabled:opacity-50">{t('memory.save')}</button>
            <button onClick={() => { setForm(null); setEditing(null); }} className="min-h-12 px-5 py-2 rounded-xl bg-gray-100 text-gray-700 font-medium">{t('common.cancel')}</button>
          </div>
        </div>
      )}

      <p className="text-xs text-gray-500">{t('memory.deleteNote')}</p>
    </section>
  );
}
