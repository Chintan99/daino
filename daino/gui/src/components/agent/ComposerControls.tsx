import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useAgentConfig, useSettings } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import { MOD, SHIFT } from "../../lib/commands";

/**
 * Autonomy and model, beside the box you type into.
 *
 * The terminal client cycles these from the keyboard — Shift+Tab for the
 * autonomy mode, Ctrl+M for the model — so both are bound to the same keys here
 * and are also clickable, because a browser user has no status bar reminding
 * them the binding exists. Clicking cycles rather than opening a menu: with four
 * modes and a short profile list, one click is the whole interaction.
 */
export function ComposerControls() {
  const qc = useQueryClient();
  const sessionId = useAgentStore((s) => s.sessionId);
  const selectedModel = useAgentStore((s) => s.selectedModel);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const { data: config } = useAgentConfig(sessionId);
  const { data: settings } = useSettings();

  const mode = config?.autonomy.mode;
  const options = config?.autonomy.options ?? [];
  const current = options.find((option) => option.id === mode);
  const profiles = settings?.models ?? [];
  const active = profiles.find((profile) => profile.name === selectedModel);

  return (
    <div className="composer-controls">
      <button
        className={`chip-toggle mode-${mode ?? "unknown"}`}
        disabled={!config || !sessionId || turnRunning}
        title={
          current
            ? `${current.label} — ${current.hint}\nClick or press ${SHIFT} Tab to cycle`
            : "Autonomy mode"
        }
        onClick={() => void cycleAutonomy(qc)}
      >
        <span className="dot" />
        {current?.label ?? "mode"}
        <span className="key">{SHIFT}⇥</span>
      </button>

      <button
        className="chip-toggle"
        disabled={profiles.length === 0 || turnRunning}
        title={
          active
            ? `Pinned to ${active.provider} · ${active.model} — a pinned session never escalates.\nClick or press ${MOD} M to cycle`
            : `Auto — routed per agent role, and free to escalate.\nClick or press ${MOD} M to cycle`
        }
        onClick={() => void cycleModel()}
      >
        {active?.name ?? "auto"}
        {profiles.length > 0 && <span className="key">{MOD}M</span>}
      </button>
    </div>
  );
}

/** Step to the next autonomy mode, in the terminal client's own order. */
export async function cycleAutonomy(
  qc: ReturnType<typeof useQueryClient> | null = null,
): Promise<void> {
  const sessionId = useAgentStore.getState().sessionId;
  if (!sessionId) return;
  const client = qc ?? undefined;
  const config = await api.agentConfig(sessionId);
  const order = config.autonomy.options.map((option) => option.id);
  const next = order[(order.indexOf(config.autonomy.mode) + 1) % order.length];
  await api.setAutonomy(sessionId, next);
  await client?.invalidateQueries({ queryKey: qk.agentConfig(sessionId) });
}

/**
 * Step through auto → each configured profile → auto.
 *
 * Auto is part of the cycle rather than a separate control, because "let the
 * router decide" is a real choice — and the only one that leaves a stalled turn
 * able to escalate to a stronger model.
 */
export async function cycleModel(): Promise<void> {
  const store = useAgentStore.getState();
  const settings = await api.settings();
  const choices = ["", ...settings.models.map((model) => model.name)];
  if (choices.length < 2) return;
  const next = choices[(choices.indexOf(store.selectedModel ?? "") + 1) % choices.length];
  store.setModel(next || null);
  if (store.sessionId) {
    await api.selectSessionModel(store.sessionId, next).catch(() => {
      /* the per-message profile still applies */
    });
  }
}
