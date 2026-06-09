import { Navigate, Route, Routes } from 'react-router-dom';
import { ProtectedRoute } from './components/ProtectedRoute';
import { Shell } from './components/Shell';
import Login from './pages/Login';
import Overview from './pages/Overview';
import Containers from './pages/Containers';
import ContainerDetail from './pages/ContainerDetail';
import Memory from './pages/Memory';
import Usage from './pages/Usage';
import Jobs from './pages/Jobs';
import Settings from './pages/Settings';
import ConfigSettings from './pages/ConfigSettings';
import TokenCost from './pages/TokenCost';

/**
 * Top-level router.
 *
 * The auth-gate (`<ProtectedRoute>`) wraps every authenticated route. Login
 * is the only public route, and `/` redirects to /overview because there's
 * no "landing" view that makes sense behind the gate.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <ProtectedRoute>
            <Shell />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<Navigate to="/overview" replace />} />
        <Route path="/overview" element={<Overview />} />
        <Route path="/containers" element={<Containers />} />
        <Route path="/containers/:name" element={<ContainerDetail />} />
        <Route path="/memory" element={<Memory />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/tokens" element={<TokenCost />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/config" element={<ConfigSettings />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/overview" replace />} />
    </Routes>
  );
}
