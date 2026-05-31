import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import {
  Users,
  Plus,
  Trash2,
  Edit3,
  Bell,
  BellOff,
  CheckCircle2,
  XCircle,
  Loader2,
  Copy,
  X,
  User,
  Settings,
} from 'lucide-react';

interface GlobalNotifySettings {
  notify_family_on_missed: boolean;
  notify_family_on_bad_mood: boolean;
}

interface FamilyContact {
  id: number;
  name: string;
  relationship: string;
  phone: string | null;
  line_id: string | null;
  verified: boolean;
  verification_code: string | null;
  notify_missed: boolean;
  notify_emotion: boolean;
  notify_weekly: boolean;
}

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function getInitials(name: string): string {
  return name
    .split(' ')
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2);
}

function getAvatarColor(name: string): string {
  const colors = [
    'bg-blue-100 text-blue-600',
    'bg-green-100 text-green-600',
    'bg-purple-100 text-purple-600',
    'bg-orange-100 text-orange-600',
    'bg-pink-100 text-pink-600',
    'bg-teal-100 text-teal-600',
  ];
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return colors[Math.abs(hash) % colors.length];
}

export default function Family() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [contacts, setContacts] = useState<FamilyContact[]>([]);
  const [globalSettings, setGlobalSettings] = useState<GlobalNotifySettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [generatingCode, setGeneratingCode] = useState<number | null>(null);
  const [verificationCode, setVerificationCode] = useState<string | null>(null);
  const [codeContactName, setCodeContactName] = useState('');

  const [modalCode, setModalCode] = useState<string | null>(null);

  const [form, setForm] = useState({
    name: '',
    relationship: '',
    phone: '',
    notify_missed: true,
    notify_emotion: true,
    notify_weekly: false,
  });

  useEffect(() => {
    fetchContacts();
    fetchGlobalSettings();
  }, []);

  const fetchContacts = async () => {
    setLoading(true);
    const headers = getAuthHeaders();
    const resp = await fetch('/api/family/contacts', { headers }).catch(() => null);
    if (resp?.ok) {
      const data = await resp.json();
      setContacts(data as FamilyContact[]);
    }
    setLoading(false);
  };

  const fetchGlobalSettings = async () => {
    const headers = getAuthHeaders();
    const resp = await fetch('/api/notify/settings', { headers }).catch(() => null);
    if (resp?.ok) {
      const data = await resp.json();
      setGlobalSettings({
        notify_family_on_missed: !!data.notify_family_on_missed,
        notify_family_on_bad_mood: !!data.notify_family_on_bad_mood,
      });
    }
  };

  const missedOffGlobal = !!globalSettings && !globalSettings.notify_family_on_missed;
  const moodOffGlobal = !!globalSettings && !globalSettings.notify_family_on_bad_mood;

  const resetForm = () => {
    setForm({ name: '', relationship: '', phone: '', notify_missed: true, notify_emotion: true, notify_weekly: false });
    setEditingId(null);
    setModalCode(null);
    setShowForm(false);
  };

  const handleEdit = (c: FamilyContact) => {
    setForm({
      name: c.name,
      relationship: c.relationship,
      phone: c.phone || '',
      notify_missed: c.notify_missed,
      notify_emotion: c.notify_emotion,
      notify_weekly: c.notify_weekly,
    });
    setEditingId(c.id);
    setShowForm(true);
  };

  const handleDelete = async (id: number) => {
    if (!confirm(t('common.delete') + '?')) return;
    const headers = getAuthHeaders();
    await fetch(`/api/family/contacts/${id}`, { method: 'DELETE', headers });
    fetchContacts();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    const headers = getAuthHeaders();
    if (!headers.Authorization) { setSaving(false); return; }

    const payload = { ...form, phone: form.phone || '' };

    if (editingId) {
      await fetch(`/api/family/contacts/${editingId}`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      resetForm();
      fetchContacts();
    } else {
      const resp = await fetch('/api/family/contacts', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data.code) {
          setModalCode(data.code);
          setVerificationCode(data.code);
          setCodeContactName(data.name);
        } else {
          resetForm();
        }
      }
      fetchContacts();
    }
    setSaving(false);
  };

  const generateCodeForContact = async (contactId: number, contactName: string) => {
    setGeneratingCode(contactId);
    const headers = getAuthHeaders();
    try {
      const resp = await fetch('/api/notify/generate-code', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ contact_id: contactId }),
      });
      if (resp.ok) {
        const result = await resp.json();
        if (result.code) {
          setVerificationCode(result.code);
          setCodeContactName(contactName);
        }
      }
    } catch (e) {
      console.error('Failed to generate code:', e);
    }
    setGeneratingCode(null);
    fetchContacts();
  };

  const copyCode = () => {
    if (verificationCode) navigator.clipboard.writeText(verificationCode);
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#0057B8]"></div>
        <span className="text-gray-500 text-base">{t('common.loading')}</span>
      </div>
    );
  }

  return (
    <div className="space-y-6 relative">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold text-gray-900">{t('family.title')}</h2>
      </div>

      {/* Family notification context */}
      {missedOffGlobal || moodOffGlobal ? (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 flex items-start gap-3">
          <BellOff className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-amber-800">{t('family.someAlertsOff')}</p>
            <ul className="mt-1 space-y-0.5">
              {missedOffGlobal && <li className="text-xs text-amber-700">• {t('family.missedAlertsOffGlobal')}</li>}
              {moodOffGlobal && <li className="text-xs text-amber-700">• {t('family.moodAlertsOffGlobal')}</li>}
            </ul>
            <button
              onClick={() => navigate('/settings')}
              className="mt-2 inline-flex items-center gap-1.5 text-xs font-semibold text-amber-800 hover:text-amber-900"
            >
              <Settings className="w-3.5 h-3.5" /> {t('family.goToSettings')}
            </button>
          </div>
        </div>
      ) : (
        <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 flex items-start gap-3">
          <Bell className="w-5 h-5 text-[#0057B8] shrink-0 mt-0.5" />
          <p className="flex-1 text-sm text-gray-600">{t('family.familyReceivesHint')}</p>
        </div>
      )}

      {/* Add Member Form Modal */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm px-4 pb-4">
          <div className="w-full max-w-lg bg-white rounded-2xl shadow-2xl max-h-[90vh] flex flex-col border border-gray-100">
            <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
              <div className="flex items-center gap-2">
                <User className="w-5 h-5 text-[#0057B8]" />
                <h3 className="font-semibold text-gray-900">{editingId ? t('family.editContact') : t('family.addContact')}</h3>
              </div>
              <button onClick={resetForm} className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors">
                <X className="w-5 h-5" />
              </button>
            </div>
            {modalCode ? (
              <div className="p-5 space-y-4 text-center">
                <p className="text-sm text-gray-600">Ask <strong>{codeContactName}</strong> to scan this QR code to add the LINE bot:</p>
                <img
                  src="/line-bot-qr.png"
                  alt="LINE Bot QR Code"
                  className="w-44 h-44 mx-auto rounded-xl border border-gray-200 shadow-sm object-contain"
                />
                <p className="text-sm text-gray-600">Then have them send this verification code to the bot:</p>
                <div className="bg-gray-50 border-2 border-[#0057B8] rounded-2xl p-4">
                  <p className="text-3xl font-bold text-[#0057B8] tracking-[0.3em]">{modalCode}</p>
                </div>
                <p className="text-xs text-gray-400">Their LINE will be linked automatically when the code is received.</p>
                <div className="flex gap-3 pt-2">
                  <button
                    onClick={() => { navigator.clipboard.writeText(modalCode); }}
                    className="flex-1 py-3 rounded-xl bg-gray-100 text-gray-700 text-sm font-medium hover:bg-gray-200 flex items-center justify-center gap-2"
                  >
                    <Copy className="w-4 h-4" /> Copy Code
                  </button>
                  <button
                    onClick={resetForm}
                    className="flex-1 py-3 rounded-xl bg-[#0057B8] text-white text-sm font-semibold hover:bg-[#003D82] flex items-center justify-center gap-2"
                  >
                    Done
                  </button>
                </div>
              </div>
            ) : (
            <form onSubmit={handleSubmit} className="overflow-y-auto flex-1 p-5 space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('register.name')}</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('family.relationship')}</label>
                  <input
                    type="text"
                    value={form.relationship}
                    onChange={(e) => setForm({ ...form, relationship: e.target.value })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                    placeholder="e.g., Son"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('family.phone')}</label>
                  <input
                    type="tel"
                    value={form.phone}
                    onChange={(e) => setForm({ ...form, phone: e.target.value })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                  />
                </div>
              </div>
              <div className="space-y-2">
                <p className="text-sm font-medium text-gray-700">{t('family.notifications')}</p>
                {[
                  { key: 'notify_missed' as const, label: t('family.notifyMissed'), offGlobal: missedOffGlobal, hint: t('family.missedAlertsOffGlobal') },
                  { key: 'notify_emotion' as const, label: t('family.notifyEmotion'), offGlobal: moodOffGlobal, hint: t('family.moodAlertsOffGlobal') },
                  { key: 'notify_weekly' as const, label: t('family.notifyWeekly'), offGlobal: false, hint: '' },
                ].map((item) => (
                  <div key={item.key}>
                    <label className={`flex items-center gap-2 ${item.offGlobal ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}>
                      <input
                        type="checkbox"
                        checked={form[item.key]}
                        disabled={item.offGlobal}
                        onChange={(e) => setForm({ ...form, [item.key]: e.target.checked })}
                        className="w-4 h-4 text-[#0057B8] rounded border-gray-300 focus:ring-[#0057B8] disabled:opacity-50"
                      />
                      <span className="text-sm text-gray-600">{item.label}</span>
                      {item.offGlobal && (
                        <span className="text-2xs px-1.5 py-0.5 bg-amber-50 border border-amber-200 text-amber-700 rounded-full">
                          {t('family.offInSettings')}
                        </span>
                      )}
                    </label>
                    {item.offGlobal && <p className="text-2xs text-amber-600 ml-6 mt-0.5">{item.hint}</p>}
                  </div>
                ))}
              </div>
              <div className="flex gap-3 pt-2">
                <button
                  type="submit"
                  disabled={saving}
                  className="flex-1 py-4 rounded-xl bg-[#0057B8] text-white text-base font-semibold hover:bg-[#003D82] active:scale-95 transition-all disabled:opacity-50 flex items-center justify-center gap-2 touch-target-large"
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.save')}
                </button>
                <button
                  type="button"
                  onClick={resetForm}
                  className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
                >
                  {t('common.cancel')}
                </button>
              </div>
            </form>
            )}
          </div>
        </div>
      )}

      {/* Verification Code */}
      {verificationCode && (
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-gray-900">{t('family.verificationCode')}</h3>
            <button onClick={() => setVerificationCode(null)} className="text-sm text-gray-400 hover:text-gray-600 transition-colors">
              {t('common.close')}
            </button>
          </div>

          {/* LINE Bot QR Code */}
          <div className="flex flex-col items-center gap-2 py-2">
            <img
              src="/line-bot-qr.png"
              alt="LINE Bot QR Code"
              className="w-48 h-48 object-contain rounded-xl border border-gray-200 shadow-sm"
            />
            <p className="text-base text-gray-600 text-center font-medium">
              Scan to add LINE bot, then send the code below.
            </p>
          </div>

          <p className="text-base text-gray-500">{t('family.codeHelp')} ({codeContactName})</p>
          <div className="flex items-center gap-3">
            <div className="flex-1 rounded-xl px-4 py-3 border border-gray-200 text-center bg-gray-50">
              <span className="text-2xl font-bold text-[#0057B8] tracking-widest">{verificationCode}</span>
            </div>
            <button onClick={copyCode} className="p-3 rounded-xl bg-[#0057B8] text-white hover:bg-[#003D82] transition-colors shadow-sm" title="Copy">
              <Copy className="w-5 h-5" />
            </button>
          </div>
        </div>
      )}

      {contacts.length === 0 ? (
        <div className="flex flex-col items-center py-12 text-center">
          <Users className="w-16 h-16 text-gray-200 mb-4" />
          <h3 className="text-xl font-semibold text-gray-900 mb-2">{t('family.title')}</h3>
          <p className="text-base text-gray-500 mb-6 max-w-xs">{t('family.noContacts')}</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {contacts.map((c) => (
            <div key={c.id} className="bg-white rounded-2xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 p-5">
              <div className="flex items-start gap-3">
                <div className={`w-12 h-12 rounded-full flex items-center justify-center shrink-0 text-sm font-bold ${getAvatarColor(c.name)}`}>
                  {getInitials(c.name)}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="font-semibold text-gray-900 text-base">{c.name}</p>
                    <span className="text-2xs px-2 py-0.5 bg-gray-100 text-gray-600 rounded-full">{c.relationship}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    {c.verified ? (
                      <span className="text-2xs px-2 py-0.5 bg-green-50 border border-green-200 text-green-600 rounded-full flex items-center gap-1">
                        <CheckCircle2 className="w-3 h-3" />{t('family.verified')}
                      </span>
                    ) : (
                      <span className="text-2xs px-2 py-0.5 bg-gray-50 border border-gray-200 text-gray-500 rounded-full flex items-center gap-1">
                        <XCircle className="w-3 h-3" />{t('family.notVerified')}
                      </span>
                    )}
                  </div>
                  <div className="mt-2 space-y-1">
                    {c.phone && <p className="text-sm text-gray-500">{c.phone}</p>}
                    {c.line_id && <p className="text-sm text-gray-500">LINE: {c.line_id}</p>}
                  </div>
                  <div className="flex items-center gap-2 mt-2">
                    {c.notify_missed && (
                      <span title={missedOffGlobal ? t('family.missedAlertsOffGlobal') : t('family.notifyMissed')} className="flex">
                        <Bell className={`w-3.5 h-3.5 ${missedOffGlobal ? 'text-gray-300' : 'text-[#0057B8]'}`} />
                      </span>
                    )}
                    {c.notify_emotion && (
                      <span title={moodOffGlobal ? t('family.moodAlertsOffGlobal') : t('family.notifyEmotion')} className="flex">
                        <Bell className={`w-3.5 h-3.5 ${moodOffGlobal ? 'text-gray-300' : 'text-amber-500'}`} />
                      </span>
                    )}
                    {c.notify_weekly && (
                      <span title={t('family.notifyWeekly')} className="flex">
                        <Bell className="w-3.5 h-3.5 text-green-500" />
                      </span>
                    )}
                    {!c.notify_missed && !c.notify_emotion && !c.notify_weekly && (
                      <span className="flex items-center gap-1 text-sm text-gray-400">
                        <BellOff className="w-3.5 h-3.5" /> {t('family.noNotifications')}
                      </span>
                    )}
                  </div>
                  {!c.verified && (
                    <button
                      onClick={() => generateCodeForContact(c.id, c.name)}
                      disabled={generatingCode === c.id}
                      className="mt-3 text-sm px-3 py-2 bg-blue-50 border border-blue-200 text-[#0057B8] rounded-lg hover:bg-blue-100 transition-colors disabled:opacity-50 flex items-center gap-1 touch-target"
                    >
                      {generatingCode === c.id ? <Loader2 className="w-3 h-3 animate-spin" /> : t('family.generateCode')}
                    </button>
                  )}
                </div>
              </div>
              <div className="flex items-center justify-end gap-1 mt-3 pt-3 border-t border-gray-50">
                <button onClick={() => handleEdit(c)} className="p-2 text-gray-400 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors">
                  <Edit3 className="w-4 h-4" />
                </button>
                <button onClick={() => handleDelete(c.id)} className="p-2 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* FAB - Add Contact */}
      <button
        onClick={() => { resetForm(); setShowForm(true); }}
        className="fixed right-4 bottom-24 sm:bottom-6 w-14 h-14 bg-[#0057B8] text-white rounded-full shadow-fab hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center z-40"
        aria-label={t('family.addContact')}
      >
        <Plus className="w-6 h-6" />
      </button>
    </div>
  );
}
