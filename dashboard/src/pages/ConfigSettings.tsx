import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  BookOpen,
  Boxes,
  Coins,
  Cpu,
  Moon,
  ScanSearch,
  Search,
  SlidersHorizontal,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react';
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
 * Items are grouped by `module` into card sections. Every touched field is
 * staged into `drafts` keyed by config key; the Save button (in a sticky
 * action bar, always visible) batches all dirty keys into a single PUT. We
 * deliberately keep a single request so the server's per-key result rows map
 * back cleanly and a reject on one key doesn't block the rest (the whole
 * request still 200s).
 *
 * The search box filters by the server-provided friendly label/description
 * (plus the raw key for power users); staged edits in filtered-out fields
 * still count and save — the sticky dirty badge keeps them visible.
 *
 * Secrets (api_keys:*) are write-only end to end — we never read or display the
 * value, only the configured ✓ / not-set status the server reports.
 */

// Display order for known modules. Unknown modules fall back to their raw
// name so a future module shows up without a code change.
const MODULE_ORDER = [
  'rag',
  'token',
  'model',
  'container',
  'dreaming',
  'index_card',
  'tools',
] as const;

// Section icons mirror the sidebar vocabulary where the module has a nav
// sibling (token→Coins, dreaming→Moon, tools→Wrench, container→Boxes).
const MODULE_ICONS: Record<string, LucideIcon> = {
  rag: ScanSearch,
  token: Coins,
  model: Cpu,
  container: Boxes,
  dreaming: Moon,
  index_card: BookOpen,
  tools: Wrench,
};

export default function ConfigSettings() {
  const { t } = useTranslation();
  const config = useConfig();
  const update = useUpdateConfig();

  // staged edits: key → value (undefined entry = not touched). A key present
  // with `undefined` is treated as cleared and removed on read.
  const [drafts, setDrafts] = useState<Record<string, ConfigDraft>>({});
  const [results, setResults] = useState<Record<string, ConfigUpdateResult>>({});
  const [query, setQuery] = useState('');

  const items = config.data?.items ?? [];

  const grouped = useMemo(() => groupByModule(items), [items]);

  const visibleGroups = useMemo(
    () =>
      grouped
        .map((g) => ({ ...g, visible: g.items.filter((it) => matchesQuery(it, query)) }))
        .filter((g) => g.visible.length > 0),
    [grouped, query],
  );

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
      {/* Sticky action bar — title, save state and search stay reachable while
          scrolling a long settings list. -mt-6/pt compensate the <main> top
          padding so the bar sits flush when stuck. */}
      <div
        className="sticky top-0 z-10 -mt-6 pb-3 pt-5"
        style={{ background: 'var(--bg)', borderBottom: '1px solid var(--border-soft)' }}
      >
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold">{t('config.title')}</h1>
          <div className="ml-auto flex flex-wrap items-center gap-3">
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
            {dirtyKeys.length > 0 ? (
              <span className="badge badge-yellow">
                <span className="dot" />
                {t('config.dirtyCount', { count: dirtyKeys.length })}
              </span>
            ) : null}
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
        <p className="text-dim mt-1 text-xs">{t('config.subtitle')}</p>
        <div className="relative mt-3 w-full max-w-sm">
          <Search
            size={14}
            aria-hidden
            className="text-dim pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2"
          />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('config.searchPlaceholder')}
            aria-label={t('config.searchPlaceholder')}
            className="input w-full pl-8 text-xs"
          />
          {query ? (
            <button
              type="button"
              onClick={() => setQuery('')}
              aria-label={t('config.clearSearch')}
              className="text-dim hover:text-text absolute right-2 top-1/2 -translate-y-1/2"
            >
              <X size={14} />
            </button>
          ) : null}
        </div>
      </div>

      {visibleGroups.length === 0 ? (
        <div className="panel p-8 text-center">
          <p className="text-dim text-sm">{t('config.noMatches')}</p>
          <button
            type="button"
            onClick={() => setQuery('')}
            className="btn btn-ghost mt-3 text-xs"
          >
            {t('config.clearSearch')}
          </button>
        </div>
      ) : null}

      {visibleGroups.map(({ module, items: groupItems, visible }) => {
        const Icon = MODULE_ICONS[module] ?? SlidersHorizontal;
        // Count overrides over the whole group (not the filtered view) so the
        // badge stays truthful while a search narrows the list.
        const overrideCount = groupItems.filter((it) => it.is_override).length;
        return (
          <section key={module} className="panel p-4 sm:p-5">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <Icon size={15} className="accent shrink-0" aria-hidden />
              <h2 className="text-sm font-semibold">
                {t(`config.module.${module}`, { defaultValue: module })}
              </h2>
              {overrideCount > 0 ? (
                <span className="badge badge-cyan">
                  {t('config.overrideCount', { count: overrideCount })}
                </span>
              ) : null}
            </div>
            <p className="text-dim mb-3 text-xs">
              {t(`config.moduleHint.${module}`, { defaultValue: '' })}
            </p>
            <div className="divide-y" style={{ borderColor: 'var(--border-soft)' }}>
              {visible.map((it) => (
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
        );
      })}

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

/** Case-insensitive match on the friendly label/description plus the raw key. */
function matchesQuery(item: ConfigItem, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [item.label ?? '', item.description ?? '', item.key]
    .join('\n')
    .toLowerCase()
    .includes(needle);
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
