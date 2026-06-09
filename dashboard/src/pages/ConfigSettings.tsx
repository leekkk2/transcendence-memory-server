import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ConfigField, isSensitive, type ConfigDraft } from '../components/ConfigField';
import {
  useConfig,
  useUpdateConfig,
  type ConfigItem,
  type ConfigUpdateResult,
} from '../lib/queries';

/**
 * Config editor — additive page over GET/PUT /admin/config.
 *
 * Items are grouped by `module` (rag / model / token; P6 will add dreaming /
 * tools). Every touched field is staged into `drafts` keyed by config key; the
 * Save button batches all dirty keys into a single PUT. We deliberately keep a
 * single request so the server's per-key result rows map back cleanly and a
 * reject on one key doesn't block the rest (the whole request still 200s).
 *
 * Secrets (api_keys:*) are write-only end to end — we never read or display the
 * value, only the configured ✓ / not-set status the server reports.
 */

// Display order + i18n group key for known modules. Unknown modules fall back
// to their raw name so a future P6 module shows up without a code change.
const MODULE_ORDER = ['rag', 'token', 'model'] as const;

export default function ConfigSettings() {
  const { t } = useTranslation();
  const config = useConfig();
  const update = useUpdateConfig();

  // staged edits: key → value (undefined entry = not touched). A key present
  // with `undefined` is treated as cleared and removed on read.
  const [drafts, setDrafts] = useState<Record<string, ConfigDraft>>({});
  const [results, setResults] = useState<Record<string, ConfigUpdateResult>>({});

  const items = config.data?.items ?? [];

  const grouped = useMemo(() => groupByModule(items), [items]);

  const dirtyKeys = useMemo(
    () => items.filter((it) => isDirty(it, drafts)).map((it) => it.key),
    [items, drafts],
  );

  function setDraft(key: string, value: ConfigDraft) {
    setDrafts((prev) => ({ ...prev, [key]: value }));
    setResults((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }

  function resetField(key: string) {
    // Sensitive (api_keys:*) keys can't take null — the server clears a secret on
    // an empty-string write (set(secret,'') → configured=false). Non-sensitive
    // keys clear with null (→ DB NULL row → back to default). See ConfigField's
    // staged-state contract.
    setDraft(key, isSensitive(key) ? '' : null);
  }

  async function save() {
    const updates = dirtyKeys.map((key) => ({ key, value: drafts[key] as ConfigDraft }));
    if (updates.length === 0) return;
    const resp = await update.mutateAsync(updates);
    const byKey: Record<string, ConfigUpdateResult> = {};
    for (const r of resp.results) byKey[r.key] = r;
    setResults(byKey);
    // Drop drafts that applied cleanly; keep rejected ones so the user can fix.
    setDrafts((prev) => {
      const next: Record<string, ConfigDraft> = {};
      for (const [k, v] of Object.entries(prev)) {
        if (byKey[k]?.ok) continue;
        next[k] = v;
      }
      return next;
    });
  }

  if (config.isLoading) {
    return <div className="text-dim text-sm">{t('common.loading')}</div>;
  }
  if (config.isError) {
    return <div className="text-dim text-sm">{t('common.error')}</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('config.title')}</h1>
        <div className="flex items-center gap-3">
          {update.isError ? (
            <span className="badge badge-red">
              <span className="dot" />
              {t('config.saveError')}
            </span>
          ) : null}
          {update.isSuccess && dirtyKeys.length === 0 && Object.keys(results).length > 0 ? (
            <span className="badge badge-green">
              <span className="dot" />
              {t('config.saved')}
            </span>
          ) : null}
          <span className="text-dim mono text-xs">
            {t('config.dirtyCount', { count: dirtyKeys.length })}
          </span>
          <button
            type="button"
            onClick={save}
            disabled={dirtyKeys.length === 0 || update.isPending}
            className="btn btn-accent text-sm"
          >
            {update.isPending ? t('config.saving') : t('config.save')}
          </button>
        </div>
      </div>

      {grouped.map(({ module, items: groupItems }) => (
        <section key={module} className="panel p-4">
          <div className="text-dim mono mb-1 text-xs uppercase tracking-wider">
            {t(`config.module.${module}`, { defaultValue: module })}
          </div>
          <p className="text-dim mb-3 text-[11px]">
            {t(`config.moduleHint.${module}`, { defaultValue: '' })}
          </p>
          <div className="divide-y" style={{ borderColor: 'var(--border-soft)' }}>
            {groupItems.map((it) => (
              <div key={it.key}>
                <ConfigField
                  item={it}
                  draft={it.key in drafts ? drafts[it.key] : undefined}
                  dirty={isDirty(it, drafts)}
                  onChange={(v) => setDraft(it.key, v)}
                  onReset={() => resetField(it.key)}
                />
                {results[it.key] && !results[it.key].ok ? (
                  <div className="mb-2 text-[11px]" style={{ color: 'var(--red)' }}>
                    {t(`config.reason.${results[it.key].rejected_reason}`, {
                      defaultValue: results[it.key].rejected_reason ?? '',
                    })}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </section>
      ))}

      <p className="text-dim text-[11px]">{t('config.footnote')}</p>
    </div>
  );
}

/** A field is dirty when a staged draft differs from the live value. */
function isDirty(item: ConfigItem, drafts: Record<string, ConfigDraft>): boolean {
  if (!(item.key in drafts)) return false;
  const draft = drafts[item.key];
  // Sensitive (api_keys:*) staged states:
  //   non-empty string → replacement pending (always dirty)
  //   '' (clear intent) → dirty ONLY if a secret is configured (something to
  //                       remove); on an unconfigured secret it's a no-op
  //   undefined         → untouched (filtered out above)
  if (isSensitive(item.key)) {
    if (typeof draft === 'string' && draft.length > 0) return true;
    if (draft === '') return item.configured === true;
    return false;
  }
  return draft !== item.value;
}

function groupByModule(items: ConfigItem[]): { module: string; items: ConfigItem[] }[] {
  const buckets = new Map<string, ConfigItem[]>();
  for (const it of items) {
    if (!buckets.has(it.module)) buckets.set(it.module, []);
    buckets.get(it.module)!.push(it);
  }
  const known = MODULE_ORDER.filter((m) => buckets.has(m)).map((m) => ({
    module: m,
    items: buckets.get(m)!,
  }));
  const rest = [...buckets.keys()]
    .filter((m) => !MODULE_ORDER.includes(m as (typeof MODULE_ORDER)[number]))
    .sort()
    .map((m) => ({ module: m, items: buckets.get(m)! }));
  return [...known, ...rest];
}
