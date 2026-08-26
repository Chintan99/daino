import { useApprovals } from "../../api/hooks";
import { fmtDateTime, statusClass } from "./format";

export function ApprovalsView() {
  const { data, isLoading } = useApprovals();
  const items = data?.approvals ?? [];

  return (
    <div className="split-right">
      <div className="toolbar">
        <span className="muted" style={{ fontSize: 11 }}>
          Every risky command, infrastructure change, deployment, and restore the
          agent asked about — and what was decided.
        </span>
        <span className="grow" />
        <span className="badge">{items.length}</span>
      </div>
      <div className="scroll-y" style={{ flex: 1 }}>
        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && items.length === 0 && (
          <div className="empty">No approvals recorded yet.</div>
        )}
        {items.length > 0 && (
          <table className="dtable">
            <thead>
              <tr>
                <th>When</th>
                <th>Category</th>
                <th>Subject</th>
                <th>Decision</th>
                <th>By</th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td className="mono" style={{ whiteSpace: "nowrap" }}>
                    {fmtDateTime(a.created_at)}
                  </td>
                  <td className="muted">{a.category}</td>
                  <td className="mono" style={{ wordBreak: "break-word" }}>
                    {a.subject}
                  </td>
                  <td>
                    <span className={statusClass(a.approved ? "approved" : "rejected")}>
                      {a.approved ? "APPROVED" : "REJECTED"}
                    </span>
                  </td>
                  <td className="muted">{a.approver}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
