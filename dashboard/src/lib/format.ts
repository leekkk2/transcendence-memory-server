/**
 * Display formatters — pure functions, no DOM / React dependency. Centralised
 * so a column-of-numbers reads consistently across the dashboard.
 */

export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function formatNumber(n: number | undefined | null): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

export function formatPercent(p: number | undefined | null): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return '—';
  return `${(p * 100).toFixed(1)}%`;
}

export function formatRelativeTs(ts: number | null | undefined): string {
  if (!ts) return '—';
  const delta = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

/**
 * Relative timestamp that tolerates both epoch-seconds (jobs, index-status)
 * and ISO-8601 strings (container last_modified). Returns "—" for anything
 * unparseable so callers don't have to normalise upstream.
 */
export function formatRelative(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—';
  let seconds: number;
  if (typeof value === 'number') {
    seconds = value;
  } else {
    const ms = Date.parse(value);
    if (Number.isNaN(ms)) return '—';
    seconds = Math.floor(ms / 1000);
  }
  return formatRelativeTs(seconds);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/**
 * Tailwind class helper — combines clsx-style conditional class lists with
 * tailwind-merge to dedupe conflicting utilities (e.g. `text-text-dim` over
 * `text-red` when both branches accidentally land on the same element).
 */
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
