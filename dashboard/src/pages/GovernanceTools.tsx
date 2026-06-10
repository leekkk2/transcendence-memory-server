import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ChevronDown,
  ChevronRight,
  Globe,
  Loader2,
  Play,
  Search,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from 'lucide-react';
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

// Client-side tool presentation metadata. The server only ships name/scope/
// description (no destructive/needs_llm flags on the wire), so the friendly
// short name + safety class live here; unknown future tools degrade to a
// neutral badge + raw name. zh strings double as t() defaultValue fallbacks
// so the page stays complete before the locale merge lands.
type ToolKind = 'danger' | 'llm' | 'safe';

const TOOL_META: Record<string, { zh: string; kind: ToolKind }> = {
  compress_knowledge_cluster: { zh: '知识聚类压缩', kind: 'llm' },
  update_container_routing: { zh: '路由规则更新', kind: 'safe' },
  snapshot_and_quarantine: { zh: '快照隔离', kind: 'danger' },
  tune_model_parameters: { zh: '模型调参', kind: 'llm' },
  analyze_retrieval_latency: { zh: '检索时延分析', kind: 'safe' },
  manage_token_quotas: { zh: 'Token 额度管理', kind: 'safe' },
};

function useToolName() {
  const { t } = useTranslation();
  return (name: string) =>
    t(`tools.name.${name}`, { defaultValue: TOOL_META[name]?.zh ?? name });
}

export default function GovernanceTools() {
  const { t } = useTranslation();
  const toolName = useToolName();
  const matrix = useToolMatrix();
  const config = useConfig();
  const update = useUpdateConfig();

  // Staged container override maps: container → { tool → bool }.
  const [overrides, setOverrides] = useState<Record<string, Record<string, boolean>>>({});
  const [query, setQuery] = useState('');

  const data = matrix.data;
  const containerTools = useMemo(
    () => (data?.tools ?? []).filter((tool) => tool.scope === 'container'),
    [data],
  );
  const globalTools = useMemo(
    () => (data?.tools ?? []).filter((tool) => tool.scope === 'global'),
    [data],
  );

  const containers = data?.containers ?? [];
  const visibleContainers = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q === '') return containers;
    return containers.filter((c) => c.container.toLowerCase().includes(q));
  }, [containers, query]);

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
      <section className="panel">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 pt-4">
          <div>
            <div className="text-dim mono text-xs uppercase tracking-wider">
              {t('tools.matrixTitle')}
            </div>
            <p className="text-dim pt-1 text-[11px]">{t('tools.matrixHint')}</p>
          </div>
          <label className="relative">
            <Search
              size={13}
              aria-hidden
              className="text-dim pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('tools.searchContainers', { defaultValue: '搜索容器…' })}
              aria-label={t('tools.searchContainers', { defaultValue: '搜索容器…' })}
              className="input mono w-44 pl-7 text-xs"
            />
          </label>
        </div>
        <div className="text-dim flex flex-wrap items-center gap-x-4 gap-y-1 px-4 pt-2 text-[11px]">
          <span className="inline-flex items-center gap-1.5">
            <LegendSwatch kind="override" />
            {t('tools.legendOverride', { defaultValue: '实心 = 容器覆盖' })}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <LegendSwatch kind="inherited" />
            {t('tools.legendInherited', { defaultValue: '半透明 = 继承全局' })}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <LegendSwatch kind="staged" />
            {t('tools.legendStaged', { defaultValue: '高亮描边 = 待保存' })}
          </span>
        </div>
        {matrix.isLoading ? (
          <div className="text-dim p-4 text-sm">{t('common.loading')}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl mt-2">
              <thead>
                <tr>
                  <th>{t('tools.colContainer')}</th>
                  {containerTools.map((tool) => (
                    <th
                      key={tool.name}
                      title={`${tool.name} — ${tool.description}`}
                      className="cursor-help text-center text-[10px] whitespace-nowrap"
                    >
                      {toolName(tool.name)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {containers.length === 0 ? (
                  <tr>
                    <td
                      colSpan={containerTools.length + 1}
                      className="text-dim py-8 text-center text-sm"
                    >
                      {t('tools.noContainers')}
                    </td>
                  </tr>
                ) : null}
                {containers.length > 0 && visibleContainers.length === 0 ? (
                  <tr>
                    <td
                      colSpan={containerTools.length + 1}
                      className="text-dim py-8 text-center text-sm"
                    >
                      {t('tools.noMatch', { defaultValue: '无匹配容器' })}
                    </td>
                  </tr>
                ) : null}
                {visibleContainers.map((c) => (
                  <tr key={c.container}>
                    <td className="mono text-xs whitespace-nowrap">
                      {c.container}
                      {c.raw_map ? (
                        <span className="badge badge-cyan ml-2">{t('tools.override')}</span>
                      ) : null}
                    </td>
                    {containerTools.map((tool) => {
                      const resolved = !!c.resolved_map[tool.name];
                      const on = cellValue(c.container, tool.name, resolved);
                      const staged =
                        overrides[c.container] !== undefined &&
                        overrides[c.container][tool.name] !== resolved;
                      const overridden = !!c.raw_map && tool.name in c.raw_map;
                      const source: CellSource = staged
                        ? 'staged'
                        : overridden
                          ? 'override'
                          : 'inherited';
                      const stateLabel = on
                        ? t('tools.on', { defaultValue: '开' })
                        : t('tools.off', { defaultValue: '关' });
                      const sourceLabel =
                        source === 'staged'
                          ? t('tools.legendStaged', { defaultValue: '高亮描边 = 待保存' })
                          : source === 'override'
                            ? t('tools.legendOverride', { defaultValue: '实心 = 容器覆盖' })
                            : t('tools.legendInherited', { defaultValue: '半透明 = 继承全局' });
                      return (
                        <td key={tool.name} className="text-center">
                          <CellToggle
                            on={on}
                            source={source}
                            title={`${c.container} · ${toolName(tool.name)}: ${stateLabel} · ${sourceLabel}`}
                            onClick={() => toggleCell(c.container, tool.name, c.resolved_map)}
                          />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Global-scope tools */}
      {globalTools.length > 0 ? (
        <section className="panel space-y-3 p-4">
          <div className="text-dim mono text-xs uppercase tracking-wider">
            {t('tools.globalToolsTitle')}
            <span className="badge badge-cyan ml-2">
              <Globe size={11} aria-hidden />
              {t('tools.globalTag')}
            </span>
          </div>
          {globalTools.map((tool) => (
            <ToolCard key={tool.name} tool={tool} globalScope />
          ))}
        </section>
      ) : null}

      {/* Container tools — descriptions + dry-run try */}
      <section className="panel space-y-3 p-4">
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

// ── Matrix cell ──────────────────────────────────────────────────────────────

type CellSource = 'inherited' | 'override' | 'staged';

function LegendSwatch({ kind }: { kind: CellSource }) {
  return (
    <span
      aria-hidden
      className="inline-block h-2.5 w-2.5 rounded-full"
      style={{
        background: 'var(--accent)',
        opacity: kind === 'inherited' ? 0.35 : 1,
        boxShadow:
          kind === 'staged'
            ? '0 0 0 2px color-mix(in srgb, var(--yellow) 70%, transparent)'
            : undefined,
      }}
    />
  );
}

/** One matrix switch. Solid = container override, faded = inherited from the
 *  global map, yellow ring = staged (unsaved) change. */
function CellToggle({
  on,
  source,
  title,
  onClick,
}: {
  on: boolean;
  source: CellSource;
  title: string;
  onClick: () => void;
}) {
  const inherited = source === 'inherited';
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={title}
      title={title}
      onClick={onClick}
      className="relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border"
      style={{
        background: on
          ? `color-mix(in srgb, var(--accent) ${inherited ? 28 : 70}%, transparent)`
          : 'var(--bg)',
        borderColor: on
          ? inherited
            ? 'color-mix(in srgb, var(--accent) 45%, var(--border))'
            : 'var(--accent)'
          : 'var(--border)',
        boxShadow:
          source === 'staged'
            ? '0 0 0 2px color-mix(in srgb, var(--yellow) 60%, transparent)'
            : undefined,
        opacity: !on && inherited ? 0.55 : 1,
      }}
    >
      <span
        className="inline-block h-3.5 w-3.5 rounded-full transition-transform"
        style={{
          background: on
            ? inherited
              ? 'color-mix(in srgb, var(--accent) 60%, var(--bg))'
              : 'var(--accent)'
            : 'var(--text-dim)',
          transform: on ? 'translateX(18px)' : 'translateX(2px)',
        }}
      />
    </button>
  );
}

// ── Tool cards ───────────────────────────────────────────────────────────────

function ToolKindBadge({ kind }: { kind: ToolKind }) {
  const { t } = useTranslation();
  if (kind === 'danger') {
    return (
      <span className="badge badge-red">
        <TriangleAlert size={11} aria-hidden />
        {t('tools.badgeDanger', { defaultValue: '破坏性 · 默认仅预览' })}
      </span>
    );
  }
  if (kind === 'llm') {
    return (
      <span className="badge badge-yellow">
        <Sparkles size={11} aria-hidden />
        {t('tools.badgeLlm', { defaultValue: '需 LLM · 默认仅预览' })}
      </span>
    );
  }
  return (
    <span className="badge badge-dim">
      <ShieldCheck size={11} aria-hidden />
      {t('tools.badgeSafe', { defaultValue: '安全工具' })}
    </span>
  );
}

function ToolCard({ tool, globalScope }: { tool: ToolInfo; globalScope: boolean }) {
  const { t } = useTranslation();
  const toolName = useToolName();
  const invoke = useInvokeTool();
  const [scope, setScope] = useState('');
  const [result, setResult] = useState<ToolInvokeResponse | null>(null);
  const kind: ToolKind = TOOL_META[tool.name]?.kind ?? 'safe';

  async function run() {
    const r = await invoke.mutateAsync({
      tool: tool.name,
      container: globalScope || scope.trim() === '' ? null : scope.trim(),
      dry_run: true,
    });
    setResult(r);
  }

  return (
    <div
      className="rounded-lg border p-3"
      style={{ borderColor: 'var(--border-soft)', background: 'var(--bg)' }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{toolName(tool.name)}</span>
        <span className="text-dim mono text-[10px]">{tool.name}</span>
        <ToolKindBadge kind={kind} />
        {!globalScope ? (
          <input
            type="text"
            value={scope}
            placeholder={t('tools.scopePlaceholder')}
            aria-label={t('tools.scopePlaceholder')}
            onChange={(e) => setScope(e.target.value)}
            className="input mono ml-auto w-40 text-xs"
          />
        ) : null}
        <button
          type="button"
          onClick={run}
          disabled={invoke.isPending}
          className={`btn btn-ghost inline-flex items-center gap-1.5 text-xs ${
            globalScope ? 'ml-auto' : ''
          }`}
        >
          {invoke.isPending ? (
            <Loader2 size={12} aria-hidden className="animate-spin" />
          ) : (
            <Play size={12} aria-hidden />
          )}
          {invoke.isPending ? t('tools.trying') : t('tools.tryRun')}
        </button>
      </div>
      <p className="text-dim mt-1 text-[11px]">{tool.description}</p>
      {invoke.isError ? (
        <div className="mt-1 text-[11px]" style={{ color: 'var(--red)' }}>
          {t('tools.tryError')}
        </div>
      ) : null}
      {result ? <InvokeResult result={result} /> : null}
    </div>
  );
}

/** Dry-run outcome: status badges + a collapsible mono JSON panel. */
function InvokeResult({ result }: { result: ToolInvokeResponse }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(true);
  return (
    <div className="fade-in mt-2 space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <InvokeStatusBadge status={result.status} />
        {result.applied ? (
          <span className="badge badge-yellow">{t('tools.applied')}</span>
        ) : (
          <span className="badge badge-dim">{t('dreaming.reportOnly')}</span>
        )}
        {result.notes ? <span className="text-dim text-[11px]">{result.notes}</span> : null}
      </div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="text-dim inline-flex cursor-pointer items-center gap-1 text-[11px] hover:underline"
      >
        {open ? (
          <ChevronDown size={12} aria-hidden />
        ) : (
          <ChevronRight size={12} aria-hidden />
        )}
        {t('tools.resultJson', { defaultValue: '结果 JSON' })}
      </button>
      {open ? (
        <pre
          className="mono max-h-64 overflow-auto rounded-md border p-2 text-[11px]"
          style={{
            background: 'var(--bg-code)',
            color: 'var(--text-dim)',
            borderColor: 'var(--border-soft)',
          }}
        >
          {JSON.stringify(result.result, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

function InvokeStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
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
      {t(`tools.status.${status}`, { defaultValue: status })}
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
