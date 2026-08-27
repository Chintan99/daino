import { api } from "../../api/client";
import { useSettings } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import { useUIStore } from "../../store/uiStore";

/**
 * The quick provider/model switch, next to the conversation it affects.
 *
 * It sits here rather than in the window chrome because the model is a property
 * of the exchange, not of the workspace — and because the gear beside it opens
 * provider setup in this same column, so connecting a model and choosing one are
 * one step apart.
 */
export function ModelBar() {
  const { data: settings } = useSettings();
  const sessionId = useAgentStore((s) => s.sessionId);
  const selectedModel = useAgentStore((s) => s.selectedModel);
  const setModel = useAgentStore((s) => s.setModel);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const setAgentView = useUIStore((s) => s.setAgentView);

  const profiles = settings?.models ?? [];
  const active = profiles.find((p) => p.name === selectedModel);
  const routed = settings?.routing.builder || Object.values(settings?.routing ?? {})[0] || "";

  /**
   * The default is **Auto**, not a pinned profile.
   *
   * Pinning is what the user asks for when they pick a model, and a pinned
   * session is deliberately excluded from escalation — so a browser that always
   * sent a profile silently disabled the recovery path that the terminal client
   * has by default. Auto sends no profile: each role uses its routed model, and
   * a stalled turn can escalate.
   */

  const choose = (profile: string) => {
    setModel(profile || null);
    // Mirror the choice onto the session, so the terminal client agrees — an
    // empty profile clears the pin rather than pinning an empty name.
    if (sessionId) {
      void api.selectSessionModel(sessionId, profile).catch(() => {
        /* the per-message profile still applies */
      });
    }
  };

  return (
    <div className="model-bar">
      <select
        className="model-picker"
        value={selectedModel ?? ""}
        disabled={turnRunning}
        onChange={(e) => choose(e.target.value)}
        title={
          active
            ? `Pinned to ${active.provider} · ${active.model}. A pinned session never escalates to a stronger model.`
            : "Auto — each agent role uses its routed model, and a stalled turn may escalate"
        }
      >
        {profiles.length === 0 && <option value="">no provider</option>}
        {profiles.length > 0 && (
          <option value="">{routed ? `Auto · ${routed}` : "Auto"}</option>
        )}
        {profiles.map((profile) => (
          <option key={profile.name} value={profile.name}>
            {profile.name}
            {profile.local ? " · local" : ""}
          </option>
        ))}
      </select>
      <button
        className="btn icon"
        title="Agent settings — autonomy, instructions, memory, playbooks, providers"
        aria-label="Agent settings"
        onClick={() => setAgentView("settings")}
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="3.1" />
          <path d="M19.4 14.5a1.6 1.6 0 0 0 .32 1.77l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.6 1.6 0 0 0-1.77-.32 1.6 1.6 0 0 0-1 1.47V21a2 2 0 0 1-4 0v-.1a1.6 1.6 0 0 0-1.05-1.46 1.6 1.6 0 0 0-1.77.32l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.6 1.6 0 0 0 .32-1.77 1.6 1.6 0 0 0-1.47-1H3a2 2 0 0 1 0-4h.1a1.6 1.6 0 0 0 1.46-1.05 1.6 1.6 0 0 0-.32-1.77l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.6 1.6 0 0 0 1.77.32H9a1.6 1.6 0 0 0 1-1.47V3a2 2 0 0 1 4 0v.1a1.6 1.6 0 0 0 1 1.47 1.6 1.6 0 0 0 1.77-.32l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.6 1.6 0 0 0-.32 1.77V9a1.6 1.6 0 0 0 1.47 1H21a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" />
        </svg>
      </button>
    </div>
  );
}
