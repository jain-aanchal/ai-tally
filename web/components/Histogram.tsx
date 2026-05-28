// Tiny inline log-scale distribution histogram (no chart lib).
export function Histogram({ buckets }: { buckets: number[] }) {
  const max = Math.max(1, ...buckets);
  return (
    <div className="flex h-8 items-end gap-0.5" aria-label="cost distribution">
      {buckets.map((b, i) => (
        <div
          key={i}
          className="w-1.5 rounded-sm bg-accent/70"
          style={{ height: `${Math.max(2, (b / max) * 100)}%` }}
          title={`${b} runs`}
        />
      ))}
    </div>
  );
}
