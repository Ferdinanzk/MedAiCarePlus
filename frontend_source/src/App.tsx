import { BrowserRouter, Routes, Route, Navigate, Outlet, Link } from 'react-router-dom';
import { useState, useEffect, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { getFaceSession, type FaceAuthUser } from './lib/face-auth';
import { ConsentApiError, fetchConsentStatus, logoutAccount, watchConsentRequired } from './lib/consent-api';
import './i18n';

import Layout from './components/Layout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Medications from './pages/Medications';
import Schedule from './pages/Schedule';
import Intake from './pages/Intake';
import Emotion from './pages/Emotion';
import Scan from './pages/Scan';
import Family from './pages/Family';
import Onboarding from './pages/Onboarding';
import HistoryPage from './pages/History';
import Register from './pages/Register';
import Settings from './pages/Settings';
import ConsentGate from './components/ConsentGate';
import LanguageSwitcher from './components/LanguageSwitcher';
import LegalPage from './pages/LegalPage';
import PrivacySettings from './pages/PrivacySettings';

function App() {
  const { t } = useTranslation();
  const [faceUser] = useState<FaceAuthUser | null>(() => getFaceSession());
  const [consent, setConsent] = useState<'loading' | 'current' | 'required' | 'error'>('loading');
  const [statusAttempt, setStatusAttempt] = useState(0);

  const isLoggedIn = !!faceUser;

  useEffect(() => {
    if (!isLoggedIn) return;
    let active = true;
    let required = false;
    const stopWatching = watchConsentRequired(() => {
      required = true;
      if (active) setConsent('required');
    });
    fetchConsentStatus().then(
      status => { if (active && !required) setConsent(status.core_current ? 'current' : 'required'); },
      error => {
        if (!active || required) return;
        if (error instanceof ConsentApiError && error.status === 401) logoutAccount();
        else setConsent('error');
      },
    );
    return () => { active = false; stopWatching(); };
  }, [isLoggedIn, statusAttempt]);

  // Reactive onboarding check — updates when localStorage changes
  const [needsOnboarding, setNeedsOnboarding] = useState(() =>
    isLoggedIn && !localStorage.getItem('onboarding_complete')
  );

  useEffect(() => {
    const check = () => {
      setNeedsOnboarding(!!getFaceSession() && !localStorage.getItem('onboarding_complete'));
    };
    window.addEventListener('storage', check);
    return () => window.removeEventListener('storage', check);
  }, []);

  const rootRedirect = () => {
    if (!isLoggedIn) return "/login";
    if (needsOnboarding) return "/onboarding";
    return "/dashboard";
  };

  const protectedPage = (page: ReactNode, onboarding = false) => {
    if (!isLoggedIn) return <Navigate to="/login" />;
    if (consent === 'loading') return (
      <div role="status" className="min-h-64 flex items-center justify-center gap-3 text-gray-600">
        <Loader2 className="w-8 h-8 animate-spin text-[#0057B8]" />{t('common.loading')}
      </div>
    );
    if (consent === 'error') return (
      <div className="space-y-4">
        <p role="alert">{t('legal.loadFailed')}</p>
        <button onClick={() => { setConsent('loading'); setStatusAttempt(value => value + 1); }} className="min-h-12 px-5 py-3 rounded-xl bg-[#0057B8] text-white font-medium">{t('legal.retry')}</button>
        <Link to="/privacy-settings" className="block py-3 text-[#0057B8] underline">{t('legal.privacyCardTitle')}</Link>
      </div>
    );
    if (consent === 'required') return <ConsentGate />;
    return needsOnboarding && !onboarding ? <Navigate to="/onboarding" /> : page;
  };

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/terms" element={<LegalPage />} />
        <Route path="/privacy" element={<LegalPage />} />
        <Route
          path="/login"
          element={isLoggedIn ? <Navigate to={rootRedirect()} /> : <Login />}
        />
        <Route
          path="/register"
          element={isLoggedIn ? <Navigate to={rootRedirect()} /> : <Register />}
        />
        <Route element={!isLoggedIn ? <Navigate to="/login" /> : consent === 'current' ? <Layout faceUser={faceUser} /> : (
          <div className="min-h-screen bg-[#F8F9FA]">
            <header className="bg-white border-b border-gray-100">
              <div className="max-w-3xl mx-auto px-4 min-h-14 flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold text-[#0057B8]">MedAiCarePlus</span>
                <LanguageSwitcher />
              </div>
            </header>
            <main className="max-w-3xl mx-auto px-4 py-6">
              {consent === 'loading' ? (
                <div role="status" className="min-h-64 flex items-center justify-center gap-3 text-gray-600">
                  <Loader2 className="w-8 h-8 animate-spin text-[#0057B8]" />{t('common.loading')}
                </div>
              ) : <Outlet />}
            </main>
          </div>
        )}>
          <Route
            path="/dashboard"
            element={protectedPage(<Dashboard />)}
          />
          <Route
            path="/medications"
            element={protectedPage(<Medications />)}
          />
          <Route
            path="/schedule"
            element={protectedPage(<Schedule />)}
          />
          <Route
            path="/intake"
            element={protectedPage(<Intake />)}
          />
          <Route
            path="/intake/:medicationId"
            element={protectedPage(<Intake />)}
          />
          <Route
            path="/emotion"
            element={protectedPage(<Emotion />)}
          />
          <Route
            path="/scan"
            element={protectedPage(<Scan />)}
          />
          <Route
            path="/family"
            element={protectedPage(<Family />)}
          />
          <Route
            path="/history"
            element={protectedPage(<HistoryPage />)}
          />
          <Route
            path="/settings"
            element={protectedPage(<Settings />)}
          />
          <Route
            path="/onboarding"
            element={protectedPage(<Onboarding />, true)}
          />
          <Route path="/privacy-settings" element={<PrivacySettings limited={consent !== 'current'} />} />
        </Route>
        <Route path="/" element={<Navigate to={rootRedirect()} />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
