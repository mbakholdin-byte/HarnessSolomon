import { Routes, Route, Navigate } from 'react-router-dom';
import { MainLayout } from './layout/MainLayout';
import PrivacyZonesPage from './pages/PrivacyZonesPage';
import HooksPage from './pages/HooksPage';
import ObservabilityPage from './pages/ObservabilityPage';
import PluginsPage from './pages/PluginsPage';
import AuditPage from './pages/AuditPage';
import MarketplacePage from './pages/MarketplacePage';
import SettingsPage from './pages/SettingsPage';
import LoginPage from './pages/LoginPage';

function App(): JSX.Element {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<MainLayout />}>
        <Route index element={<Navigate to="/privacy-zones" replace />} />
        <Route path="privacy-zones" element={<PrivacyZonesPage />} />
        <Route path="hooks" element={<HooksPage />} />
        <Route path="observability" element={<ObservabilityPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="plugins" element={<PluginsPage />} />
        <Route path="marketplace" element={<MarketplacePage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}

export default App;
