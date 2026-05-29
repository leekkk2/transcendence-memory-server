/**
 * i18next bootstrap. Two locales — zh-CN (default) and en (fallback).
 *
 * Language resolution order: persisted choice (localStorage `tm-admin-lang`)
 * → browser `navigator.language`. `convertDetectedLanguage` collapses any
 * `zh*` tag to zh-CN and any `en*` tag to en; everything else falls back to
 * zh-CN so an unknown browser locale still lands on the default language.
 */
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import en from '../locales/en.json';
import zhCN from '../locales/zh-CN.json';

export const SUPPORTED_LANGS = ['zh-CN', 'en'] as const;
export type Lang = (typeof SUPPORTED_LANGS)[number];
export const LANG_STORAGE_KEY = 'tm-admin-lang';

export const LANG_LABELS: Record<Lang, string> = {
  'zh-CN': '简体中文',
  en: 'English',
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      'zh-CN': { translation: zhCN },
      en: { translation: en },
    },
    fallbackLng: 'zh-CN',
    supportedLngs: SUPPORTED_LANGS as unknown as string[],
    detection: {
      order: ['localStorage', 'navigator'],
      lookupLocalStorage: LANG_STORAGE_KEY,
      caches: ['localStorage'],
      // Collapse any zh* tag to zh-CN and any en* tag to en; unknown locales
      // fall back to the default language. (This is a detector option, not a
      // top-level init option.)
      convertDetectedLanguage: (lng: string) => {
        const l = lng.toLowerCase();
        if (l.startsWith('zh')) return 'zh-CN';
        if (l.startsWith('en')) return 'en';
        return 'zh-CN';
      },
    },
    interpolation: { escapeValue: false },
    react: { useSuspense: false },
  });

// Keep <html lang> in sync so the UA, screen readers, and CSS :lang() rules
// reflect the active language.
const syncHtmlLang = (lng: string) => {
  if (typeof document !== 'undefined') document.documentElement.lang = lng;
};
syncHtmlLang(i18n.resolvedLanguage ?? 'zh-CN');
i18n.on('languageChanged', syncHtmlLang);

export default i18n;
