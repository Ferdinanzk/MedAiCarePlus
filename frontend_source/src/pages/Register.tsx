import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { setFaceSession } from '../lib/face-auth';
import { legalLanguage, useLegalDocument, type LegalResponse } from '../lib/consent-api';
import LanguageSwitcher from '../components/LanguageSwitcher';
import LegalDocument from '../components/LegalDocument';
import { Loader2, UserPlus, Mail, Lock, User, CheckCircle2, ChevronRight, FileText } from 'lucide-react';

type RegStep = 1 | 2;

export default function Register() {
  const { t, i18n } = useTranslation();
  const { legal, loading: legalLoading, failed: legalFailed, reload: reloadLegal } = useLegalDocument(i18n.language);
  const [step, setStep] = useState<RegStep>(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [agreedDocument, setAgreedDocument] = useState<LegalResponse | null>(null);
  const [showTerms, setShowTerms] = useState(false);
  const agreed = !!legal && agreedDocument === legal;

  const validate = () => {
    if (!name.trim()) return 'Please enter your name';
    if (!email.trim()) return 'Please enter your email';
    if (password.length < 6) return 'Password must be at least 6 characters';
    if (password !== confirmPassword) return 'Passwords do not match';
    if (!agreed) return t('register.terms.mustAgree');
    return '';
  };

  const handleRegister = async (e: { preventDefault: () => void }) => {
    e.preventDefault();
    if (loading) return;
    if (!legal || legal.language !== legalLanguage(i18n.language)) {
      reloadLegal();
      setAgreedDocument(null);
      setShowTerms(true);
      return;
    }
    const err = validate();
    if (err) { setError(err); return; }
    setLoading(true);
    setError('');

    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim(),
          face_label: name.trim(),
          email: email.trim().toLowerCase(),
          password,
          consent: {
            terms_version: legal.terms_version,
            language: legal.language,
            document_sha256: legal.sha256,
            scopes: { core: true },
          },
        }),
      });

      const data = await res.json();

      if (!res.ok || !data.success) {
        if (res.status === 409 || ['stale_terms_version', 'document_hash_mismatch'].includes(data.error || data.detail)) {
          reloadLegal();
          setAgreedDocument(null);
          setShowTerms(true);
          setError(t('legal.documentChanged'));
          setLoading(false);
          return;
        }
        setError(data.error || data.detail || 'Registration failed. Please try again.');
        setLoading(false);
        return;
      }

      // Set face session using the token returned by the backend
      setFaceSession({ name: name.trim(), loginAt: new Date().toISOString(), u_id: data.u_id }, data.token);

      setLoading(false);
      setStep(2);
      setTimeout(() => { window.location.href = '/onboarding'; }, 1500);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Registration failed. Please try again.');
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#F8F9FA] flex flex-col">
      <header className="bg-white border-b border-gray-100 safe-area-top">
        <div className="max-w-md mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-[#0057B8] rounded-lg flex items-center justify-center">
              <span className="text-white font-bold text-sm">M+</span>
            </div>
            <span className="font-semibold text-gray-900">MedAiCarePlus</span>
          </div>
          <LanguageSwitcher />
        </div>
      </header>

      <div className="flex-1 flex flex-col items-center justify-center px-4 py-8">
        <div className="w-full max-w-sm mx-auto">
          <div className="text-center mb-8">
            <img src="/healsmart-logo.png" alt="HealSmart" className="w-60 max-w-[75%] mx-auto mb-4" />
            <h1 className="text-2xl font-bold text-gray-900">{t('register.title')}</h1>
            <p className="text-gray-500 mt-1">{t('register.subtitle')}</p>
          </div>

          {/* Step indicator */}
          <div className="flex justify-center gap-8 mb-6">
            {[{ num: 1, label: t('register.step1Account') }, { num: 2, label: t('register.step3Done') }].map(({ num, label }) => (
              <div key={num} className={`flex flex-col items-center gap-1 ${step >= num ? 'text-[#0057B8]' : 'text-gray-300'}`}>
                <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold ${step > num ? 'bg-[#0057B8] text-white' : step === num ? 'bg-[#0057B8] text-white' : 'bg-gray-100 border border-gray-200 text-gray-400'}`}>
                  {step > num ? <CheckCircle2 className="w-4 h-4" /> : num}
                </div>
                <span className="text-sm font-medium">{label}</span>
              </div>
            ))}
          </div>

          <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-6">
            {step === 1 && (
              <form onSubmit={handleRegister} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('register.name')}</label>
                  <div className="relative">
                    <User className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                    <input type="text" value={name} onChange={(e) => setName(e.target.value)}
                      className="w-full pl-10 pr-4 py-4 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all min-h-[48px]"
                      placeholder={t('register.namePlaceholder')} required />
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('register.email')}</label>
                  <div className="relative">
                    <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                    <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                      className="w-full pl-10 pr-4 py-4 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all min-h-[48px]"
                      placeholder="your@email.com" required />
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('register.password')}</label>
                  <div className="relative">
                    <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                    <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                      className="w-full pl-10 pr-4 py-4 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all min-h-[48px]"
                      placeholder="••••••••" required minLength={6} />
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">{t('register.confirmPassword')}</label>
                  <div className="relative">
                    <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                    <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                      className="w-full pl-10 pr-4 py-4 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all min-h-[48px]"
                      placeholder="••••••••" required />
                  </div>
                </div>

                {/* Terms Agreement Checkbox */}
                <div className="flex items-start gap-3">
                  <input
                    type="checkbox"
                    id="agree-terms"
                    checked={agreed}
                    onChange={(e) => setAgreedDocument(e.target.checked ? legal : null)}
                    disabled={!legal || loading}
                    className="mt-1 w-5 h-5 rounded border-gray-300 text-[#0057B8] focus:ring-[#0057B8]"
                  />
                  <label htmlFor="agree-terms" className="text-sm text-gray-600">
                    {t('register.terms.checkboxLabel')}
                    <button type="button" onClick={() => setShowTerms(true)} className="text-[#0057B8] hover:text-[#003D82] font-medium underline">
                      {t('register.terms.termsLink')}
                    </button>
                    {t('register.terms.and')}
                    <button type="button" onClick={() => setShowTerms(true)} className="text-[#0057B8] hover:text-[#003D82] font-medium underline">
                      {t('register.terms.privacyLink')}
                    </button>
                  </label>
                </div>

                <button
                  type="submit"
                  disabled={loading || !agreed || !legal}
                  onClick={(e) => { e.preventDefault(); handleRegister(e); }}
                  className="w-full py-4 rounded-xl bg-[#0057B8] text-white text-base font-semibold hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large disabled:opacity-50"
                >
                  {loading ? <><Loader2 className="w-5 h-5 animate-spin" />{t('common.loading')}</> : <><UserPlus className="w-5 h-5" />{t('register.next')}<ChevronRight className="w-4 h-4" /></>}
                </button>
                {error && <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-base text-red-600">{error}</div>}
                {legalLoading && <p role="status" className="text-sm text-gray-500">{t('common.loading')}</p>}
                {legalFailed && <div role="alert" className="space-y-2 text-red-700"><p>{t('legal.loadFailed')}</p><button type="button" onClick={reloadLegal} className="min-h-12 text-[#0057B8] font-medium">{t('legal.retry')}</button></div>}
              </form>
            )}

            {step === 2 && (
              <div className="text-center py-8">
                <div className="w-16 h-16 bg-[#0057B8] rounded-full flex items-center justify-center mx-auto mb-4 shadow-lg">
                  <CheckCircle2 className="w-8 h-8 text-white" />
                </div>
                <h2 className="text-xl font-bold text-gray-900 mb-2">{t('register.success')}</h2>
                <p className="text-gray-500">{t('register.redirecting')}</p>
              </div>
            )}
          </div>

          {step === 1 && (
            <>
              <p className="text-center text-base text-gray-500 mt-6">
                {t('register.haveAccount')}{' '}
                <Link to="/login" className="text-[#0057B8] hover:text-[#003D82] font-medium transition-colors">{t('register.loginLink')}</Link>
              </p>
              <p className="text-center text-sm text-gray-500 mt-2">{t('register.faceOnce')}</p>
            </>
          )}
        </div>
      </div>

      {/* Terms & Conditions Modal */}
      {showTerms && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div role="dialog" aria-modal="true" aria-labelledby="register-terms-title" className="bg-white rounded-2xl w-full max-w-2xl max-h-[80vh] flex flex-col shadow-2xl">
            {/* Header */}
            <div className="p-6 border-b border-gray-100 flex items-center justify-between">
              <h2 id="register-terms-title" className="text-lg font-bold text-gray-900 flex items-center gap-2">
                <FileText className="w-5 h-5 text-[#0057B8]" />
                {t('register.terms.modalTitle')}
              </h2>
              <button onClick={() => setShowTerms(false)} aria-label={t('register.terms.closeButton')} className="p-2 hover:bg-gray-100 rounded-lg">
                ✕
              </button>
            </div>

            {/* Scrollable Content */}
            <div className="flex-1 min-h-0 overflow-y-auto p-5 sm:p-6 space-y-6">
              {legalLoading && <p role="status">{t('common.loading')}</p>}
              {legalFailed && <div role="alert"><p>{t('legal.loadFailed')}</p><button onClick={reloadLegal} className="min-h-12 text-[#0057B8] font-medium">{t('legal.retry')}</button></div>}
              {legal && <LegalDocument document={legal.document} />}
            </div>

            {/* Footer */}
            <div className="p-6 border-t border-gray-100 space-y-3">
              <button
                onClick={() => { setAgreedDocument(legal); setShowTerms(false); }}
                disabled={!legal || loading}
                className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all touch-target-large disabled:opacity-50"
              >
                {t('register.terms.agreeButton')}
              </button>
              <button
                onClick={() => setShowTerms(false)}
                className="w-full py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
              >
                {t('register.terms.closeButton')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
