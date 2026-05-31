import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { getFaceSession, type FaceAuthUser } from './lib/face-auth';
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

function App() {
  const [faceUser] = useState<FaceAuthUser | null>(() => getFaceSession());

  const isLoggedIn = !!faceUser;

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

  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/login"
          element={isLoggedIn ? <Navigate to={rootRedirect()} /> : <Login />}
        />
        <Route
          path="/register"
          element={isLoggedIn ? <Navigate to={rootRedirect()} /> : <Register />}
        />
        <Route element={<Layout faceUser={faceUser} />}>
          <Route
            path="/dashboard"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Dashboard />) : <Navigate to="/login" />}
          />
          <Route
            path="/medications"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Medications />) : <Navigate to="/login" />}
          />
          <Route
            path="/schedule"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Schedule />) : <Navigate to="/login" />}
          />
          <Route
            path="/intake"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Intake />) : <Navigate to="/login" />}
          />
          <Route
            path="/intake/:medicationId"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Intake />) : <Navigate to="/login" />}
          />
          <Route
            path="/emotion"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Emotion />) : <Navigate to="/login" />}
          />
          <Route
            path="/scan"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Scan />) : <Navigate to="/login" />}
          />
          <Route
            path="/family"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <Family />) : <Navigate to="/login" />}
          />
          <Route
            path="/history"
            element={isLoggedIn ? (needsOnboarding ? <Navigate to="/onboarding" /> : <HistoryPage />) : <Navigate to="/login" />}
          />
          <Route
            path="/onboarding"
            element={isLoggedIn ? <Onboarding /> : <Navigate to="/login" />}
          />
        </Route>
        <Route path="/" element={<Navigate to={rootRedirect()} />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
