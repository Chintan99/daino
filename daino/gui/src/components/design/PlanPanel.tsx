import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { useAgentStore } from "../../store/agentStore";
import { promptFor } from "../../store/dialogStore";
import { openFileInEditor } from "../../lib/openFile";
import { BRAND } from "../../lib/branding";

/**
 * The plan a design has to have before it becomes code.
 *
 * "Propose a plan first" used to be a sentence in the prompt, which the model
 * was free to ignore. It is now a gate: the planning turn has read-only tools
 * and cannot write, and Implement is refused until a plan for *this version* of
 * the canvas has been approved. The button state here comes from the same gate
 * the endpoint uses, so it can never say yes while the server says no.
 */
export function PlanPanel({ designId }: { designId: string }) {
  const qc = useQueryClient();
  const sessionId = useAgentStore((s) => s.sessionId);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const [busy, setBusy] = useState("");
  const { data } = useQuery({
    queryKey: ["design", designId, "plan"],
    queryFn: () => api.designPlan(designId),
  });

  const plan = data?.plan ?? null;
  const refresh = () => qc.invalidateQueries({ queryKey: ["design", designId, "plan"] });

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    try {
      await fn();
      await refresh();
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const propose = () => {
    if (!sessionId) {
      window.alert("No conversation is open yet.");
      return;
    }
    void run("propose", () => api.proposeDesignPlan(designId, sessionId));
  };

  const reject = async () => {
    const reason = await promptFor({
      title: "Reject this plan",
      hint: "What should the next plan do differently? This is passed to the agent.",
      initial: "",
      confirmLabel: "Reject",
    });
    if (reason === null) return;
    await run("reject", () => api.rejectDesignPlan(designId, reason));
  };

  const implement = () => {
    if (!sessionId) return;
    void run("implement", () => api.implementDesign(designId, sessionId));
  };

  return (
    <div className="plan-panel">
      <div className="section-title">
        Implementation
        {plan && <span className={`badge plan-${plan.status}`}>{plan.status}</span>}
      </div>

      {!plan && (
        <>
          <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
            {BRAND} reads the repository and proposes a plan first. It cannot
            write anything until you have approved it.
          </div>
          <button
            className="btn primary"
            style={{ width: "100%", marginTop: 8 }}
            disabled={!!busy || turnRunning || !sessionId}
            onClick={propose}
          >
            {busy === "propose" ? "Planning…" : "Propose a plan"}
          </button>
        </>
      )}

      {plan && (
        <>
          {data?.stale && (
            <div className="plan-warning">
              This plan was written for version {plan.design_version}; the canvas
              is now version {data.design_version}. Propose a new one so it
              describes what is actually there.
            </div>
          )}
          {plan.rejection_reason && plan.status !== "rejected" && (
            <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
              Previously rejected: {plan.rejection_reason}
            </div>
          )}

          {plan.summary && <div className="plan-summary">{plan.summary}</div>}

          {plan.steps.length > 0 && (
            <ol className="plan-steps">
              {plan.steps.map((step, index) => (
                <li key={index}>
                  {step.description}
                  {step.paths.length > 0 && (
                    <div className="plan-paths">
                      {step.paths.map((path) => (
                        <button
                          key={path}
                          className="ws-link-path"
                          onClick={() => void openFileInEditor(path)}
                        >
                          {path}
                        </button>
                      ))}
                    </div>
                  )}
                </li>
              ))}
            </ol>
          )}

          {plan.questions.length > 0 && (
            <div className="plan-questions">
              <strong>Open questions</strong>
              <ul>
                {plan.questions.map((question, index) => (
                  <li key={index}>{question}</li>
                ))}
              </ul>
            </div>
          )}

          {plan.reviewed_paths.length > 0 && (
            <details className="plan-reviewed">
              <summary>Read {plan.reviewed_paths.length} file(s)</summary>
              {plan.reviewed_paths.map((path) => (
                <button
                  key={path}
                  className="ws-link-path"
                  onClick={() => void openFileInEditor(path)}
                >
                  {path}
                </button>
              ))}
            </details>
          )}

          <div className="plan-actions">
            {plan.status === "proposed" && (
              <>
                <button
                  className="btn primary sm"
                  disabled={!!busy}
                  onClick={() => void run("approve", () => api.approveDesignPlan(designId))}
                >
                  Approve
                </button>
                <button
                  className="btn subtle sm"
                  disabled={!!busy}
                  onClick={() => void reject()}
                >
                  Reject
                </button>
              </>
            )}
            {(plan.status === "rejected" || plan.status === "implemented" || data?.stale) && (
              <button
                className="btn subtle sm"
                disabled={!!busy || turnRunning || !sessionId}
                onClick={propose}
              >
                {busy === "propose" ? "Planning…" : "Propose a new plan"}
              </button>
            )}
            {plan.status === "approved" && (
              <button
                className="btn primary sm"
                disabled={!!busy || turnRunning || !data?.can_implement}
                title={data?.reason || `Have ${BRAND} build the approved plan`}
                onClick={implement}
              >
                {busy === "implement" ? "Building…" : `Implement with ${BRAND}`}
              </button>
            )}
          </div>

          {!data?.can_implement && data?.reason && plan.status !== "proposed" && (
            <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
              {data.reason}
            </div>
          )}
        </>
      )}
    </div>
  );
}
