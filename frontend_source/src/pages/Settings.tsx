import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import ReachyCard from '../components/ReachyCard';
import { getFaceToken } from '../lib/face-auth';
import { forgetRefusals } from '../lib/doses';
import { rememberOverdoseProtection } from '../lib/notify-api';
import { Bell, ShieldAlert, ShieldCheck, Users, Save, CheckCircle2, AlertTriangle, Loader2 } from 'lucide-react';

interface NotificationSettings {
  remind_before_minutes: number;
  remind_after_minutes: number;
  remind_after_retries: number;
  notify_family_on_missed: boolean;
  notify_family_on_bad_mood: boolean;
  notify_family_on_taken: boolean;
  /** Refuse doses too early, too close together, past the daily maximum or missed (server default: on). */
  overdose_protection: boolean;
}

const TOGGLE_CLASS =
  "w-11 h-6 bg-gray-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-[#0057B8]";

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function Settings() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<NotificationSettings>({
    remind_before_minutes: 5,
    remind_after_minutes: 10,
    remind_after_retries: 3,
    notify_family_on_missed: true,
    notify_family_on_bad_mood: true,
    notify_family_on_taken: true,
    overdose_protection: true,
  });
  // What the server has: the protection switch saves at once with these, not with unsaved edits in the form below.
  const [saved, setSaved] = useState<NotificationSettings | null>(null);
  const [protectionBusy, setProtectionBusy] = useState(false);
  const [protectionError, setProtectionError] = useState('');

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  // Split UI states for enabling/disabling reminders
  const [remindBeforeEnabled, setRemindBeforeEnabled] = useState(true);
  const [remindAfterEnabled, setRemindAfterEnabled] = useState(true);

  useEffect(() => {
    fetchSettings();
  }, []);

  const fetchSettings = async () => {
    setLoading(true);
    const headers = getAuthHeaders();
    try {
      const res = await fetch('/api/notify/settings', { headers });
      if (res.ok) {
        const body = await res.json();
        // An older server does not send the switch; it is on by default.
        const data: NotificationSettings = { ...body, overdose_protection: body.overdose_protection !== false };
        setSettings(data);
        setSaved(data);
        rememberOverdoseProtection(data.overdose_protection);
        setRemindBeforeEnabled(data.remind_before_minutes > 0);
        setRemindAfterEnabled(data.remind_after_retries > 0);
      }
    } catch {
      setMessage({ type: 'error', text: t('settings.fetchFailed') });
    }
    setLoading(false);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setMessage(null);

    const payload: NotificationSettings = {
      ...settings,
      remind_before_minutes: remindBeforeEnabled ? settings.remind_before_minutes : 0,
      remind_after_retries: remindAfterEnabled ? settings.remind_after_retries : 0,
    };

    const headers = getAuthHeaders();
    try {
      const res = await fetch('/api/notify/settings', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        setSaved(payload);
        setMessage({ type: 'success', text: t('settings.savedSuccess') });
      } else {
        setMessage({ type: 'error', text: t('settings.saveFailed') });
      }
    } catch {
      setMessage({ type: 'error', text: t('settings.networkError') });
    }
    setSaving(false);
  };

  // Saved at once, like the robot's switches. Turning it off is confirmed first: the server tells family on LINE.
  const toggleProtection = async (value: boolean) => {
    if (!saved || (!value && !confirm(t('overdose.offConfirm')))) return;
    setProtectionBusy(true);
    setProtectionError('');
    try {
      const next = { ...saved, overdose_protection: value };
      const res = await fetch('/api/notify/settings', {
        method: 'POST',
        headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSaved(next);
      setSettings((current) => ({ ...current, overdose_protection: value }));
      rememberOverdoseProtection(value);
      forgetRefusals();
    } catch {
      setProtectionError(t('overdose.saveFailed'));
    }
    setProtectionBusy(false);
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <Loader2 className="animate-spin h-10 w-10 text-[#0057B8]" />
        <span className="text-gray-500 text-base">{t('settings.loading')}</span>
      </div>
    );
  }

  return (
    <div className="max-w-xl mx-auto space-y-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold text-gray-900">{t('settings.title')}</h1>
        <p className="text-sm text-gray-500">{t('settings.subtitle')}</p>
      </div>

      <Link to="/privacy-settings" className="block bg-white rounded-2xl border border-gray-100 p-5 shadow-sm hover:border-[#0057B8] transition-colors">
        <h2 className="text-base font-semibold text-[#0057B8]">{t('legal.privacyCardTitle')}</h2>
        <p className="text-sm text-gray-500 mt-1">{t('legal.privacyCardDesc')}</p>
      </Link>

      <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-3" aria-labelledby="overdose-title">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-green-50 text-green-700 rounded-xl flex items-center justify-center shrink-0">
            <ShieldCheck className="w-5 h-5" />
          </div>
          <div>
            <h2 id="overdose-title" className="text-base font-semibold text-gray-900">{t('overdose.title')}</h2>
            <p className="text-xs text-gray-500">{t('overdose.subtitle')}</p>
          </div>
          <label className="relative inline-flex items-center cursor-pointer ml-auto shrink-0">
            <input
              type="checkbox"
              className="sr-only peer"
              aria-labelledby="overdose-title"
              aria-describedby="overdose-help"
              checked={settings.overdose_protection}
              disabled={!saved || protectionBusy}
              onChange={(e) => void toggleProtection(e.target.checked)}
            />
            <div className={`${TOGGLE_CLASS} peer-disabled:opacity-50`}></div>
          </label>
        </div>
        <p id="overdose-help" className="text-sm text-gray-600">{t('overdose.help')}</p>
        {saved && !settings.overdose_protection && (
          <p role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">{t('overdose.offWarning')}</p>
        )}
        {protectionError && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{protectionError}</p>}
      </section>

      <ReachyCard />

      {message && (
        <div className={`p-4 rounded-2xl flex items-center gap-3 border ${
          message.type === 'success' ? 'bg-green-50 border-green-200 text-green-700' : 'bg-red-50 border-red-200 text-red-700'
        }`}>
          {message.type === 'success' ? <CheckCircle2 className="w-5 h-5" /> : <AlertTriangle className="w-5 h-5" />}
          <span className="text-sm font-medium">{message.text}</span>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* 1. Before Intake Notification */}
        <div className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-blue-50 text-[#0057B8] rounded-xl flex items-center justify-center">
              <Bell className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-gray-900">{t('settings.beforeIntakeTitle')}</h2>
              <p className="text-xs text-gray-500">{t('settings.beforeIntakeDesc')}</p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer ml-auto">
              <input
                type="checkbox"
                className="sr-only peer"
                checked={remindBeforeEnabled}
                onChange={(e) => setRemindBeforeEnabled(e.target.checked)}
              />
              <div className={TOGGLE_CLASS}></div>
            </label>
          </div>

          {remindBeforeEnabled && (
            <div className="pl-13 space-y-2">
              <label className="block text-sm font-medium text-gray-700">{t('settings.remindMe')}</label>
              <select
                value={settings.remind_before_minutes}
                onChange={(e) => setSettings({ ...settings, remind_before_minutes: parseInt(e.target.value) })}
                className="w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-[#0057B8] outline-none"
              >
                <option value={5}>{t('settings.minutesBefore', { count: 5 })}</option>
                <option value={10}>{t('settings.minutesBefore', { count: 10 })}</option>
                <option value={15}>{t('settings.minutesBefore', { count: 15 })}</option>
                <option value={30}>{t('settings.minutesBefore', { count: 30 })}</option>
              </select>
            </div>
          )}
        </div>

        {/* 2. Missed Intake Notification */}
        <div className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-amber-50 text-amber-600 rounded-xl flex items-center justify-center">
              <ShieldAlert className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-gray-900">{t('settings.missedIntakeTitle')}</h2>
              <p className="text-xs text-gray-500">{t('settings.missedIntakeDesc')}</p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer ml-auto">
              <input
                type="checkbox"
                className="sr-only peer"
                checked={remindAfterEnabled}
                onChange={(e) => setRemindAfterEnabled(e.target.checked)}
              />
              <div className={TOGGLE_CLASS}></div>
            </label>
          </div>

          {remindAfterEnabled && (
            <div className="pl-13 grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="block text-sm font-medium text-gray-700">{t('settings.interval')}</label>
                <select
                  value={settings.remind_after_minutes}
                  onChange={(e) => setSettings({ ...settings, remind_after_minutes: parseInt(e.target.value) })}
                  className="w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-[#0057B8] outline-none"
                >
                  <option value={10}>{t('settings.everyMinutes', { count: 10 })}</option>
                  <option value={15}>{t('settings.everyMinutes', { count: 15 })}</option>
                  <option value={20}>{t('settings.everyMinutes', { count: 20 })}</option>
                  <option value={30}>{t('settings.everyMinutes', { count: 30 })}</option>
                </select>
              </div>
              <div className="space-y-2">
                <label className="block text-sm font-medium text-gray-700">{t('settings.repeatCount')}</label>
                <select
                  value={settings.remind_after_retries}
                  onChange={(e) => setSettings({ ...settings, remind_after_retries: parseInt(e.target.value) })}
                  className="w-full px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-[#0057B8] outline-none"
                >
                  <option value={1}>{t('settings.time', { count: 1 })}</option>
                  <option value={2}>{t('settings.time', { count: 2 })}</option>
                  <option value={3}>{t('settings.time', { count: 3 })}</option>
                  <option value={5}>{t('settings.time', { count: 5 })}</option>
                </select>
              </div>
            </div>
          )}
        </div>

        {/* 3. Alert Family Notification */}
        <div className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-red-50 text-red-600 rounded-xl flex items-center justify-center">
              <Users className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-gray-900">{t('settings.familyAlertTitle')}</h2>
              <p className="text-xs text-gray-500">{t('settings.familyAlertDesc')}</p>
            </div>
          </div>

          <div className="pl-13 space-y-4 pt-2">
            <div className="flex items-center justify-between">
              <div>
                <label className="block text-sm font-medium text-gray-800">{t('settings.alertOnMissed')}</label>
                <span className="text-xs text-gray-400">{t('settings.alertOnMissedDesc')}</span>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  className="sr-only peer"
                  checked={settings.notify_family_on_missed}
                  onChange={(e) => setSettings({ ...settings, notify_family_on_missed: e.target.checked })}
                />
                <div className={TOGGLE_CLASS}></div>
              </label>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <label className="block text-sm font-medium text-gray-800">{t('settings.alertOnBadMood')}</label>
                <span className="text-xs text-gray-400">{t('settings.alertOnBadMoodDesc')}</span>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  className="sr-only peer"
                  checked={settings.notify_family_on_bad_mood}
                  onChange={(e) => setSettings({ ...settings, notify_family_on_bad_mood: e.target.checked })}
                />
                <div className={TOGGLE_CLASS}></div>
              </label>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <label className="block text-sm font-medium text-gray-800">{t('settings.alertOnTaken')}</label>
                <span className="text-xs text-gray-400">{t('settings.alertOnTakenDesc')}</span>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  className="sr-only peer"
                  checked={settings.notify_family_on_taken}
                  onChange={(e) => setSettings({ ...settings, notify_family_on_taken: e.target.checked })}
                />
                <div className={TOGGLE_CLASS}></div>
              </label>
            </div>
          </div>
        </div>

        <button
          type="submit"
          disabled={saving}
          className="w-full py-4 bg-[#0057B8] hover:bg-[#003D82] text-white text-base font-semibold rounded-2xl shadow-sm transition-all duration-200 flex items-center justify-center gap-2 active:scale-98 disabled:opacity-50"
        >
          {saving ? <Loader2 className="w-5 h-5 animate-spin" /> : <Save className="w-5 h-5" />}
          {t('settings.savePreferences')}
        </button>
      </form>
    </div>
  );
}
