import { useTranslation } from 'react-i18next';
import type { ConfigItem } from '../lib/queries';
import { cn } from '../lib/format';

/**
 * One controlled config row for the ConfigSettings page.
 *
 * Rendering is driven by the server contract (`type` + the sensitive flag):
 *   - bool        → toggle
 *   - int / float → number input
 *   - str         → text input
 *   - sensitive   → write-only secret field (api_keys:*): the server NEVER
 *                   echoes the value, so we only show a "configured ✓ / not set"
 *                   badge and a placeholder input. Typing stages a replacement;
 *                   leaving it untouched means "don't change". We never pre-fill.
 *                   A *configured* secret also gets an explicit "remove" action
 *                   that stages an empty-string clear intent (''), which the PUT
 *                   sends as value:'' so the server clears the override back to
 *                   "not set" (configured=false). '' (clear) and `undefined`
 *                   (untouched) are distinct staged states for sensitive keys.
 *
 * `draft` is the parent-owned staged value (undefined = untouched, falls back to
 * the live `item.value`). `onChange` reports the raw value the PUT body expects.
 * `onReset` clears a persisted override back to the server default.
 *
 * This component is presentational + controlled; the parent batches every
 * dirty field into a single PUT /admin/config.
 */
export type ConfigDraft = string | number | boolean | null;

const SENSITIVE_PREFIX = 'config:model:api_keys:';
const BASE_URL_PREFIX = 'config:model:base_url:';

export function isSensitive(key: string): boolean {
  return key.startsWith(SENSITIVE_PREFIX);
}

interface ConfigFieldProps {
  item: ConfigItem;
  /** staged value; `undefined` means the field has not been touched this session */
  draft: ConfigDraft | undefined;
  dirty: boolean;
  onChange: (value: ConfigDraft) => void;
  onReset: () => void;
}

export function ConfigField({ item, draft, dirty, onChange, onReset }: ConfigFieldProps) {
  const { t } = useTranslation();
  const sensitive = isSensitive(item.key);
  const baseUrl = item.key.startsWith(BASE_URL_PREFIX);
  const label = leafLabel(item.key);

  return (
    <div className="flex flex-col gap-1.5 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono text-xs">{label}</span>
        <span className="text-dim mono text-[10px] uppercase">{item.type}</span>
        {item.is_override ? (
          <span className="badge badge-cyan">{t('config.modified')}</span>
        ) : null}
        {dirty ? (
          <span className="badge badge-yellow">
            <span className="dot" />
            {t('config.unsaved')}
          </span>
        ) : null}
        {item.is_override && !sensitive ? (
          <button
            type="button"
            onClick={onReset}
            className="btn btn-ghost ml-auto px-2 py-0.5 text-[11px]"
          >
            {t('config.resetDefault')}
          </button>
        ) : null}
        {sensitive && item.configured === true ? (
          // Configured secret → explicit remove: stage a '' clear intent so the
          // PUT sends value:'' and the server clears the override (configured
          // → false). Distinct from the input's "clear staged input" (undefined).
          <button
            type="button"
            onClick={onReset}
            className="btn btn-ghost ml-auto px-2 py-0.5 text-[11px]"
          >
            {t('config.removeSecret')}
          </button>
        ) : null}
      </div>

      <ControlFor item={item} draft={draft} onChange={onChange} />

      <div className="flex flex-wrap items-center gap-x-3 text-[11px]">
        {!sensitive ? (
          <span className="text-dim mono">
            {t('config.defaultLabel')}: {renderScalar(item.default)}
          </span>
        ) : null}
        {baseUrl ? <span className="text-dim">{t('config.baseUrlHint')}</span> : null}
      </div>
    </div>
  );
}

function ControlFor({
  item,
  draft,
  onChange,
}: {
  item: ConfigItem;
  draft: ConfigDraft | undefined;
  onChange: (value: ConfigDraft) => void;
}) {
  const { t } = useTranslation();
  const sensitive = isSensitive(item.key);

  if (sensitive) {
    const configured = item.configured === true;
    // Three staged states for a secret:
    //   draft === undefined → untouched (no-op)
    //   draft === ''        → explicit clear intent (remove the stored secret)
    //   non-empty string    → replacement staged
    const clearPending = draft === '';
    const replacePending = typeof draft === 'string' && draft.length > 0;
    return (
      <div className="flex items-center gap-2.5">
        <span
          className="inline-block h-2 w-2 shrink-0 rounded-full"
          style={{
            background: clearPending
              ? 'var(--text-dim)'
              : configured
                ? 'var(--green)'
                : 'var(--text-dim)',
          }}
          title={configured ? t('config.secretSet') : t('config.secretNotSet')}
        />
        <span
          className="mono text-[11px]"
          style={{
            color: clearPending ? 'var(--text-dim)' : configured ? 'var(--green)' : 'var(--text-dim)',
          }}
        >
          {clearPending
            ? t('config.secretWillClear')
            : configured
              ? t('config.secretSet')
              : t('config.secretNotSet')}
        </span>
        <input
          type="password"
          autoComplete="new-password"
          // write-only: never pre-filled with the stored secret. While a clear is
          // staged the field stays empty; typing into it overrides the clear with
          // a replacement.
          value={replacePending ? (draft as string) : ''}
          placeholder={t('config.secretPlaceholder')}
          onChange={(e) => {
            // Typing a non-empty value stages a replacement; emptying the input
            // reverts to untouched (undefined) — it never stages a clear. The
            // explicit "remove" button is the only path to a '' clear intent, so
            // a half-typed-then-deleted secret doesn't accidentally wipe it.
            const v = e.target.value;
            onChange((v === '' ? undefined : v) as unknown as ConfigDraft);
          }}
          className="input mono ml-auto w-full max-w-xs text-xs"
        />
        {replacePending || clearPending ? (
          // Cancel the staged change (replacement OR clear) → back to untouched.
          <button
            type="button"
            onClick={() => onChange(undefined as unknown as ConfigDraft)}
            className="btn btn-ghost px-2 py-0.5 text-[11px]"
          >
            {t('config.clearInput')}
          </button>
        ) : null}
      </div>
    );
  }

  if (item.type === 'bool') {
    const current = (draft !== undefined ? draft : item.value) === true;
    return (
      <button
        type="button"
        role="switch"
        aria-checked={current}
        onClick={() => onChange(!current)}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors',
        )}
        style={{
          background: current
            ? 'color-mix(in srgb, var(--accent) 70%, transparent)'
            : 'var(--bg)',
          borderColor: current ? 'var(--accent)' : 'var(--border)',
        }}
      >
        <span
          className="inline-block h-3.5 w-3.5 rounded-full transition-transform"
          style={{
            background: current ? 'var(--accent)' : 'var(--text-dim)',
            transform: current ? 'translateX(18px)' : 'translateX(2px)',
          }}
        />
      </button>
    );
  }

  if (item.type === 'int' || item.type === 'float') {
    const current = draft !== undefined ? draft : item.value;
    return (
      <input
        type="number"
        step={item.type === 'float' ? 'any' : '1'}
        value={current === null || current === undefined ? '' : String(current)}
        placeholder={t('config.numberPlaceholder')}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === '') {
            // empty number = clear override → null
            onChange(null);
            return;
          }
          const n = item.type === 'float' ? parseFloat(raw) : parseInt(raw, 10);
          onChange(Number.isNaN(n) ? raw : n);
        }}
        className="input mono w-full max-w-xs text-xs"
      />
    );
  }

  // str
  const current = draft !== undefined ? draft : item.value;
  return (
    <input
      type="text"
      value={current === null || current === undefined ? '' : String(current)}
      placeholder={t('config.textPlaceholder')}
      onChange={(e) => onChange(e.target.value)}
      className="input mono w-full max-w-md text-xs"
    />
  );
}

function leafLabel(key: string): string {
  // config:rag:similarity_threshold → similarity_threshold
  // config:model:api_keys:llm → api_keys:llm
  const parts = key.split(':');
  return parts.slice(2).join(':') || key;
}

function renderScalar(v: ConfigDraft): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return v === '' ? '""' : v;
  return String(v);
}
