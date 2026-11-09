import { useEffect, useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import MediaPage from './features/media';
import LoginPage from './LoginPage';
import { getStoredMediaToken, subscribeMediaSessionChanges } from './lib/media-client';

function AuthenticatedMedia() {
  const [token, setToken] = useState(getStoredMediaToken);
  useEffect(() => subscribeMediaSessionChanges(() => setToken(getStoredMediaToken())), []);
  return token ? <MediaPage /> : <Navigate to="/login" replace />;
}

export default function App() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="*" element={<AuthenticatedMedia />} />
  </Routes>;
}
