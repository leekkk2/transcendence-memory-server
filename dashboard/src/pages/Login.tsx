import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLogin } from '../lib/auth';
import { LanguageSwitcher } from '../components/LanguageSwitcher';

/**
 * Login page. Single password-input + button + minimal error surface. No
 * persistence in localStorage — the api key flows through the form to the
 * server in one POST and is then represented by an HttpOnly cookie.
 */
export default function Login() {
  const { t } = useTranslation();
  const [apiKey, setApiKey] = useState('');
  const login = useLogin();

  return (
    <div className="flex h-screen w-screen items-center justify-center px-4" style={{ background: 'var(--bg)' }}>
      <div className="absolute right-4 top-4">
        <LanguageSwitcher />
      </div>
      <div className="panel w-full max-w-[360px] p-6">
        <div className="mb-1 text-lg font-semibold">Transcendence Memory</div>
        <div className="text-dim mb-6 text-sm">{t('login.subtitle')}</div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (apiKey.trim()) login.mutate(apiKey.trim());
          }}
        >
          <label className="text-dim mono mb-1 block text-xs uppercase tracking-wider">
            {t('login.apiKeyLabel')}
          </label>
          <input
            type="password"
            autoFocus
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={t('login.apiKeyPlaceholder')}
            className="input mono w-full text-sm"
          />
          {login.error ? (
            <div className="mt-2 text-xs" style={{ color: 'var(--red)' }}>
              {login.error.status === 429 ? t('login.tooManyAttempts') : t('login.invalidKey')}
            </div>
          ) : null}
          <button
            type="submit"
            disabled={login.isPending || !apiKey.trim()}
            className="btn btn-accent mt-4 w-full text-sm"
          >
            {login.isPending ? t('login.signingIn') : t('login.signIn')}
          </button>
        </form>
        <div className="text-dim mt-6 text-xs leading-relaxed">{t('login.hint')}</div>
      </div>
    </div>
  );
}
