import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ConfigField, type ConfigDraft } from '../components/ConfigField';
import {
  useConfig,
  useUpdateConfig,
  useToolMatrix,
  useInvokeTool,
  type ConfigItem,
  type ToolInfo,
  type ToolInvokeResponse,
} from '../lib/queries';

/**
 * Governance toolbox page — additive surface over the P6 governance endpoints.
 *
 * The container × tool matrix renders the resolved enable map; toggling a cell
 * stages the whole container enabled_map json and saves it via PUT /admin/config
 * (config:tools:container:{c}:enabled_map) — no write bypass. The global scalar
 * knobs reuse ConfigField. Each tool has a dry-run "try" button that previews
 * the plan (and reads real balances for the safe token-quota tool) without ever
 * mutating data (dry_run=true).
 */

const GLOBAL_SCALAR_KEYS = [
  'config:tools:global_enabled_map',
  'config:tools:sandbox_mem_limit',
  'config:tools:approval_ttl_days',
  'config:tools:new_tool_default_enabled',
];

export default function GovernanceTools() {
  const { t } = useTranslation();
  const matrix = useToolMatrix();
  const config = useConfig();
  const update = useUpdateConfig();

  // Staged container override maps: container → { tool → bool }.
  const [overrides, setOverrides] = useState<Record<string, Record<string, boolean>>>({});

  const data = matrix.data;
  const containerTools = useMemo(
    () => (data?.tools ?? []).filter((tool) => tool.scope === 'container'),
    [data],
  );
  const globalTools = useMemo(
    () => (data?.tools ?? []).filter((tool) => tool.scope === 'global'),
    [data],
  );

  const items = config.data?.items ?? [];
  const globalScalarItems = useMemo(
    () =>
      GLOBAL_SCALAR_KEYS.map((k) => items.find((it) => it.key === k)).filter(
        (x): x is ConfigItem => !!x,
      ),
    [items],
  );

  const [drafts, setDrafts] = useState<Record<string, ConfigDraft>>({});

  function cellValue(container: string, tool: string, resolved: boolean): boolean {
    const ov = overrides[container];
    if (ov && tool in ov) return ov[tool];
    return resolved;
  }

  function toggleCell(container: string, tool: string, resolvedMap: Record<string, boolean>) {
    setOverrides((prev) => {
      const base = prev[container] ?? { ...resolvedMap };
      const current = tool in base ? base[tool] : !!resolvedMap[tool];
      return { ...prev, [container]: { ...base, [tool]: !current } };
    });
  }

  const matrixDirty = Object.keys(overrides).length > 0;
  const scalarDirtyKeys = useMemo(
    () => Object.keys(drafts).filter((k) => scalarDirty(k, items, drafts)),
    [drafts, items],
  );
  const totalDirty = (matrixDirty ? Object.keys(overrides).length : 0) + scalarDirtyKeys.length;

  async function save() {
    const updates: { key: string; value: ConfigDraft }[] = [];
    for (const [container, map] of Object.entries(overrides)) {
      updates.push({
        key: `config:tools:container:${container}:enabled_map`,
        value: JSON.stringify(map),
      });
    }
    for (const key of scalarDirtyKeys) updates.push({ key, value: drafts[key] });
    if (updates.length === 0) return;
    await update.mutateAsync(updates);
    setOverrides({});
    setDrafts({});
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">{t('tools.title')}</h1>
        <div className="flex items-center gap-3">
          {update.isError ? (
            <span className="badge badge-red">
              <span className="dot" />
              {t('config.saveError')}
            </span>
          ) : null}
          <span className="text-dim mono text-xs">
            {t('config.dirtyCount', { count: totalDirty })}
          </span>
          <button
            type="button"
            onClick={save}
            disabled={totalDirty === 0 || update.isPending}
            className="btn btn-accent text-sm"
          >
            {update.isPending ? t('config.saving') : t('config.save')}
          </button>
        </div>
      </div>
      <p className="text-dim text-[11px]">{t('tools.intro')}</p>

      {/* Container × tool activation matrix */}
      <section className="panel overflow-x-auto">
        <div className="text-dim mono px-4 pt-4 text-xs uppercase tracking-wider">
          {t('tools.matrixTitle')}
        </div>
        <p className="text-dim px-4 pt-1 text-[11px]">{t('tools.matrixHint')}</p>
        {matrix.isLoading ? (
          <div className="text-dim p-4 text-sm">{t('common.loading')}</div>
        ) : (
          <table className="tbl mt-2">
            <thead>
              <tr>
                <th>{t('tools.colContainer')}</th>
                {containerTools.map((tool) => (
                  <th key={tool.name} title={tool.description} className="mono text-[10px]">
                    {tool.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(data?.containers ?? []).length === 0 ? (
                <tr>
                  <td colSpan={containerTools.length + 1} className="text-dim py-8 text-center text-sm">
                    {t('tools.noContainers')}
                  </td>
                </tr>
              ) : null}
              {(data?.containers ?? []).map((c) => (
                <tr key={c.container}>
                  <td className="mono text-xs">
                    {c.container}
                    {c.raw_map ? <span className="badge badge-cyan ml-2">{t('tools.override')}</span> : null}
                  </td>
                  {containerTools.map((tool) => {
                    const on = cellValue(c.container, tool.name, !!c.resolved_map[tool.name]);
                    return (
                      <td key={tool.name}>
                        <CellToggle
                          on={on}
                          onClick={() => toggleCell(c.container, tool.name, c.resolved_map)}
                        />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Global-scope tools */}
      {globalTools.length > 0 ? (
        <section className="panel p-4 space-y-3">
          <div className="text-dim mono text-xs uppercase tracking-wider">
            {t('tools.globalToolsTitle')}
            <span className="badge badge-cyan ml-2">{t('tools.globalTag')}</span>
          </div>
          {globalTools.map((tool) => (
            <ToolCard key={tool.name} tool={tool} globalScope />
          ))}
        </section>
      ) : null}

      {/* Container tools — descriptions + dry-run try */}
      <section className="panel p-4 space-y-3">
        <div className="text-dim mono text-xs uppercase tracking-wider">{t('tools.toolsTitle')}</div>
        {containerTools.map((tool) => (
          <ToolCard key={tool.name} tool={tool} globalScope={false} />
        ))}
      </section>

      {/* Global scalar config */}
      <section className="panel p-4">
        <div className="text-dim mono mb-1 text-xs uppercase tracking-wider">
          {t('tools.globalConfig')}
        </div>
        <p className="text-dim mb-3 text-[11px]">{t('tools.globalConfigHint')}</p>
        {config.isLoading ? (
          <div className="text-dim text-sm">{t('common.loading')}</div>
        ) : (
          <div className="divide-y" style={{ borderColor: 'var(--border-soft)' }}>
            {globalScalarItems.map((it) => (
              <ConfigField
                key={it.key}
                item={it}
                draft={it.key in drafts ? drafts[it.key] : undefined}
                dirty={scalarDirty(it.key, items, drafts)}
                onChange={(v) => setDrafts((prev) => ({ ...prev, [it.key]: v }))}
                onReset={() => setDrafts((prev) => ({ ...prev, [it.key]: null }))}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function CellToggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onClick}
      className="relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors"
      style={{
        background: on ? 'color-mix(in srgb, var(--accent) 70%, transparent)' : 'var(--bg)',
        borderColor: on ? 'var(--accent)' : 'var(--border)',
      }}
    >
      <span
        className="inline-block h-3.5 w-3.5 rounded-full transition-transform"
        style={{
          background: on ? 'var(--accent)' : 'var(--text-dim)',
          transform: on ? 'translateX(18px)' : 'translateX(2px)',
        }}
      />
    </button>
  );
}

function ToolCard({ tool, globalScope }: { tool: ToolInfo; globalScope: boolean }) {
  const { t } = useTranslation();
  const invoke = useInvokeTool();
  const [scope, setScope] = useState('');
  const [result, setResult] = useState<ToolInvokeResponse | null>(null);

  async function run() {
    const r = await invoke.mutateAsync({
      tool: tool.name,
      container: globalScope || scope.trim() === '' ? null : scope.trim(),
      dry_run: true,
    });
    setResult(r);
  }

  return (
    <div className="py-2" style={{ borderTop: '1px solid var(--border-soft)' }}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono text-xs">{tool.name}</span>
        <span className="text-dim text-[11px]">{tool.description}</span>
        {!globalScope ? (
          <input
            type="text"
            value={scope}
            placeholder={t('tools.scopePlaceholder')}
            onChange={(e) => setScope(e.target.value)}
            className="input mono ml-auto w-40 text-xs"
          />
        ) : null}
        <button
          type="button"
          onClick={run}
          disabled={invoke.isPending}
          className={globalScope ? 'btn btn-ghost ml-auto text-xs' : 'btn btn-ghost text-xs'}
        >
          {invoke.isPending ? t('tools.trying') : t('tools.tryRun')}
        </button>
      </div>
      {invoke.isError ? (
        <div className="mt-1 text-[11px]" style={{ color: 'var(--red)' }}>
          {t('tools.tryError')}
        </div>
      ) : null}
      {result ? (
        <div className="mt-2 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <InvokeStatusBadge status={result.status} />
            {result.applied ? (
              <span className="badge badge-yellow">{t('tools.applied')}</span>
            ) : (
              <span className="badge">{t('dreaming.reportOnly')}</span>
            )}
            {result.notes ? <span className="text-dim text-[11px]">{result.notes}</span> : null}
          </div>
          <pre
            className="mono overflow-x-auto rounded p-2 text-[11px]"
            style={{ background: 'var(--bg)', color: 'var(--text-dim)' }}
          >
            {JSON.stringify(result.result, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function InvokeStatusBadge({ status }: { status: string }) {
  const cls =
    status === 'ok'
      ? 'badge badge-green'
      : status === 'error'
        ? 'badge badge-red'
        : status === 'disabled'
          ? 'badge badge-yellow'
          : 'badge badge-cyan';
  return (
    <span className={cls}>
      <span className="dot" />
      {status}
    </span>
  );
}

/** Scalar config draft dirty check (mirrors ConfigSettings' non-sensitive path —
 *  no sensitive keys live in the tools group). */
function scalarDirty(key: string, items: ConfigItem[], drafts: Record<string, ConfigDraft>): boolean {
  if (!(key in drafts)) return false;
  const it = items.find((x) => x.key === key);
  if (!it) return true;
  // json keys: ConfigField hands back a string; treat any staged value as a
  // change (the live value is the parsed object, so a strict !== always fires).
  if (it.type === 'json') return drafts[key] !== undefined;
  return drafts[key] !== it.value;
}
