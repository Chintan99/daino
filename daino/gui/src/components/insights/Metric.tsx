export function Metric({ k, v }: { k: string; v: string | number }) {
  return (
    <div className="metric">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}
