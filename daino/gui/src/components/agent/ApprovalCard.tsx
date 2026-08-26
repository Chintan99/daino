import { useAgentStore, type ApprovalRequest } from "../../store/agentStore";

export function ApprovalCard({ approval }: { approval: ApprovalRequest }) {
  const send = useAgentStore((s) => s.send);
  const removeApproval = useAgentStore((s) => s.removeApproval);

  const resolve = (approved: boolean, remember: boolean) => {
    send?.({
      type: "approval_resolve",
      id: approval.id,
      approved,
      remember,
    });
    removeApproval(approval.id);
  };

  return (
    <div className="approval-card">
      <div>
        <strong>Approval needed</strong>
        {approval.reason && <span className="muted"> — {approval.reason}</span>}
      </div>
      <div className="cmd">{approval.command}</div>
      <div className="actions">
        <button className="btn primary" onClick={() => resolve(true, false)}>
          Allow Once
        </button>
        <button className="btn" onClick={() => resolve(true, true)}>
          Always Allow
        </button>
        <button className="btn danger" onClick={() => resolve(false, false)}>
          Reject
        </button>
      </div>
    </div>
  );
}
