import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';

/**
 * Authenticated-area chrome. On desktop the sidebar is pinned; below `lg` it
 * collapses behind a hamburger and slides in as an overlay drawer. Topbar is
 * fixed-height, main content scrolls.
 */
export function Shell() {
  const [navOpen, setNavOpen] = useState(false);

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      {/* Desktop sidebar */}
      <div className="hidden lg:block">
        <Sidebar />
      </div>

      {/* Mobile drawer */}
      {navOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0"
            style={{ background: 'rgba(0,0,0,0.55)' }}
            onClick={() => setNavOpen(false)}
          />
          <div className="fade-in absolute left-0 top-0 h-full">
            <Sidebar onNavigate={() => setNavOpen(false)} />
          </div>
        </div>
      ) : null}

      <div className="flex flex-1 flex-col overflow-hidden">
        <Topbar onOpenNav={() => setNavOpen(true)} />
        <main className="flex-1 overflow-y-auto px-4 py-6 sm:px-6 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
