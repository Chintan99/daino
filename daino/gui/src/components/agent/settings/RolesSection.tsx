import { useSettingsMutation } from "../../../api/hooks";
import type { AgentConfig } from "../../../api/types";

/**
 * Which model each agent role uses.
 *
 * D[Ai]NO's "agents" are fixed roles — architect, planner, builder, reviewer,
 * debugger, tester, summarizer, deployer — and what is customizable is the model
 * behind each one. This is the same routing the Settings menu writes, kept here
 * because it belongs beside the conversation it steers.
 */
export function RolesSection({ config }: { config: AgentConfig }) {
  const patch = useSettingsMutation();

  return (
    <div className="cfg-section">
      <div className="section-title">Model per agent role</div>
      <div className="muted field-hint">
        Saved to the project, not the session. Roles without a usable model fall
        back to the first configured profile.
      </div>
      {config.profiles.length === 0 && (
        <div className="empty">No model profile configured yet.</div>
      )}
      {config.roles.map((entry) => (
        <label className="field role-row" key={entry.role}>
          <span>{entry.role}</span>
          <select
            className="input"
            value={entry.profile}
            disabled={config.profiles.length === 0 || patch.isPending}
            onChange={(e) =>
              patch.mutate(
                { routing: { [entry.role]: e.target.value } },
                {
                  onError: (err: unknown) =>
                    window.alert(
                      `Could not route ${entry.role}: ${
                        err instanceof Error ? err.message : String(err)
                      }`,
                    ),
                },
              )
            }
          >
            {!entry.profile && <option value="">unset</option>}
            {config.profiles.map((profile) => (
              <option key={profile} value={profile}>
                {profile}
              </option>
            ))}
          </select>
        </label>
      ))}
    </div>
  );
}
