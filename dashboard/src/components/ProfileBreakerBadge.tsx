/**
 * Tiny pill that renders breaker state for one profile (embedding / reranker /
 * llm / vlm). Server reports closed / open / half-open; we colour them with
 * the design-doc palette (green/red/yellow respectively).
 */
export type BreakerState = 'closed' | 'open' | 'half-open' | 'unknown';

const COLOUR: Record<BreakerState, string> = {
  closed: 'var(--green)',
  open: 'var(--red)',
  'half-open': 'var(--yellow)',
  unknown: 'var(--text-dim)',
};

export function ProfileBreakerBadge({
  name,
  state,
}: {
  name: string;
  state: BreakerState;
}) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: COLOUR[state] ?? COLOUR.unknown }}
      />
      <span className="font-mono text-xs">{name}</span>
      <span className="text-dim text-xs">{state}</span>
    </div>
  );
}
