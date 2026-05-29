import { Languages } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { LANG_LABELS, SUPPORTED_LANGS, type Lang } from '../lib/i18n';

/**
 * Compact language switcher. Writes through i18next, whose detector caches the
 * choice to localStorage (`tm-admin-lang`) so it survives reloads.
 */
export function LanguageSwitcher() {
  const { i18n, t } = useTranslation();
  const current = (SUPPORTED_LANGS as readonly string[]).includes(i18n.resolvedLanguage ?? '')
    ? (i18n.resolvedLanguage as Lang)
    : 'zh-CN';

  return (
    <label className="flex items-center gap-1.5" title={t('topbar.language')}>
      <Languages size={14} className="text-dim" />
      <select
        value={current}
        onChange={(e) => i18n.changeLanguage(e.target.value)}
        aria-label={t('topbar.language')}
        className="input mono cursor-pointer px-1.5 py-1 text-xs"
      >
        {SUPPORTED_LANGS.map((lng) => (
          <option key={lng} value={lng}>
            {LANG_LABELS[lng]}
          </option>
        ))}
      </select>
    </label>
  );
}
