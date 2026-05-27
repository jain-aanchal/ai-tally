export function Placeholder({ title, ticket }: { title: string; ticket: string }) {
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="max-w-prose text-sm text-muted">
        This workflow is scaffolded but not yet built. Tracked as{" "}
        <span className="font-mono text-gray-300">{ticket}</span>. The app shell, routing, mock-data
        layer, and design tokens are in place — screens land in follow-up PRs.
      </p>
    </div>
  );
}
