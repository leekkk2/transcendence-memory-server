import { useState } from 'react';
import { useLogin } from '../lib/auth';

/**
 * Login page. Single password-input + button + minimal error surface. No
 * persistence in localStorage — the api key flows through the form to the
 * server in one POST and is then represented by an HttpOnly cookie.
 */
export default function Login() {
  const [apiKey, setApiKey] = useState('');
  const login = useLogin();

  return (
    <div
      className="flex h-screen w-screen items-center justify-center"
      style={{ background: 'var(--bg)' }}
    >
      <div className="panel w-[360px] p-6">
        <div className="mb-1 text-lg font-semibold">Transcendence Memory</div>
        <div className="text-dim mb-6 text-sm">admin sign in</div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (apiKey.trim()) login.mutate(apiKey.trim());
          }}
        >
          <label className="text-dim mb-1 block text-xs uppercase tracking-wider mono">
            api key
          </label>
          <input
            type="password"
            autoFocus
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="enter your api key"
            className="w-full rounded border px-3 py-2 font-mono text-sm"
            style={{
              background: 'var(--bg)',
              borderColor: 'var(--border)',
              color: 'var(--text)',
            }}
          />
          {login.error ? (
            <div className="mt-2 text-xs" style={{ color: 'var(--red)' }}>
              {login.error.status === 429
                ? 'too many failed logins; try again later'
                : 'invalid key'}
            </div>
          ) : null}
          <button
            type="submit"
            disabled={login.isPending || !apiKey.trim()}
            className="mt-4 w-full rounded px-3 py-2 text-sm font-semibold disabled:opacity-50"
            style={{ background: 'var(--accent)', color: '#000' }}
          >
            {login.isPending ? 'signing in…' : 'sign in'}
          </button>
        </form>
        <div className="text-dim mt-6 text-xs leading-relaxed">
          the server verifies your api key, then issues a short-lived{' '}
          <span className="mono">HttpOnly</span> session cookie. the key itself
          never lives in the browser.
        </div>
      </div>
    </div>
  );
}
