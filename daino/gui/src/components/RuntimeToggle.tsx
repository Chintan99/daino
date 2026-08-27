import { useSettings, useSettingsMutation } from "../api/hooks";

/**
 * The runtime the agent's commands execute in, beside the project it applies to.
 *
 * It lives in the chrome because it is the single most consequential setting in
 * the product — `local` runs the agent's commands unsandboxed on this machine —
 * and because switching it while chasing a container problem should not mean a
 * trip into a menu. `local` is coloured as a caution for the same reason.
 */
const RUNTIMES = [
  { id: "docker", label: "docker", hint: "Sandboxed container (recommended)" },
  { id: "local", label: "local", hint: "This machine, unsandboxed" },
  { id: "ssh", label: "ssh", hint: "A configured remote host" },
] as const;

export function RuntimeToggle() {
  const { data: settings } = useSettings();
  const patch = useSettingsMutation();

  const current = settings?.runtime.default;
  if (!current) return null;

  const hint = RUNTIMES.find((item) => item.id === current)?.hint ?? "";

  return (
    <select
      className={`runtime-toggle ${current}`}
      value={current}
      disabled={patch.isPending}
      title={`Command runtime — ${hint}. Saved to this project.`}
      aria-label="Command runtime"
      onChange={(e) =>
        patch.mutate(
          { runtime: e.target.value as (typeof RUNTIMES)[number]["id"] },
          {
            onError: (err: unknown) =>
              window.alert(
                `Could not switch the runtime: ${
                  err instanceof Error ? err.message : String(err)
                }`,
              ),
          },
        )
      }
    >
      {RUNTIMES.map((item) => (
        <option key={item.id} value={item.id}>
          {item.label}
        </option>
      ))}
    </select>
  );
}
