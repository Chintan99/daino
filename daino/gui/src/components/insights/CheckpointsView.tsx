import { useCheckpoints } from "../../api/hooks";
import { fmtDateTime } from "./format";

export function CheckpointsView() {
  const { data, isLoading } = useCheckpoints();
  const items = data?.checkpoints ?? [];

  return (
    <div className="split-right">
      <div className="toolbar">
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          Recoverable snapshots of the working tree. Restoring one is an
          approval-gated action and stays in the TUI and CLI.
        </span>
        <span className="grow" />
        <span className="badge">{items.length}</span>
      </div>
      <div className="scroll-y" style={{ flex: 1 }}>
        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && items.length === 0 && (
          <div className="empty">No checkpoints recorded yet.</div>
        )}
        {items.length > 0 && (
          <table className="dtable">
            <thead>
              <tr>
                <th>Created</th>
                <th>Description</th>
                <th>Revision</th>
                <th>Mission</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id}>
                  <td className="mono" style={{ whiteSpace: "nowrap" }}>
                    {fmtDateTime(c.created_at)}
                  </td>
                  <td>{c.description}</td>
                  <td className="mono muted">{c.revision.slice(0, 10) || "—"}</td>
                  <td className="mono muted">{c.mission_id || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
