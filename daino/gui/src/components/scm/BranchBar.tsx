import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { promptFor, confirmFor } from "../../store/dialogStore";

/**
 * The branch you are on, how far it has drifted, and what to do about it.
 *
 * The ahead/behind pair is the reason this exists rather than a plain label:
 * "push" and "pull" are only sensible decisions if you can see which one you
 * need, and a branch 3 behind its upstream needs the other one.
 */
export function BranchBar() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState("");
  const { data } = useQuery({
    queryKey: ["git", "branches"],
    queryFn: api.gitBranches,
  });
  if (!data?.repository) return null;

  const current = data.branches.find((item) => item.current);
  const refresh = () => qc.invalidateQueries({ queryKey: ["git"] });

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

  const create = async () => {
    const name = await promptFor({
      title: "New branch",
      hint: `Branches from ${current?.name ?? "HEAD"}.`,
      initial: "",
      confirmLabel: "Create and switch",
    });
    if (!name?.trim()) return;
    await run("create", () => api.gitSwitchBranch(name.trim(), { create: true }));
  };

  const remove = async (name: string) => {
    const ok = await confirmFor({
      title: `Delete ${name}`,
      message:
        "Git refuses to delete a branch holding unmerged commits. If it does, " +
        "you will be told rather than overridden.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.gitDeleteBranch(name);
      await refresh();
    } catch (err) {
      // The refusal is the useful part: offer the force only after seeing it.
      const forced = await confirmFor({
        title: `${name} has unmerged commits`,
        message: `${err instanceof Error ? err.message : String(err)}\n\nDelete anyway? The commits will be unreachable.`,
        confirmLabel: "Delete anyway",
        danger: true,
      });
      if (forced) await run("delete", () => api.gitDeleteBranch(name, true));
    }
  };

  return (
    <div className="scm-branchbar">
      <div className="scm-branch-row">
        <select
          className="input sm"
          value={current?.name ?? ""}
          disabled={!!busy}
          onChange={(e) => {
            if (e.target.value === "__new__") void create();
            else void run("switch", () => api.gitSwitchBranch(e.target.value));
          }}
          title="Switch branch"
        >
          {data.branches.map((item) => (
            <option key={item.name} value={item.name}>
              {item.name}
              {item.ahead || item.behind
                ? ` (${item.ahead ? `↑${item.ahead}` : ""}${item.behind ? `↓${item.behind}` : ""})`
                : ""}
            </option>
          ))}
          <option value="__new__">＋ New branch…</option>
        </select>
        <button
          className="btn icon"
          title="Fetch from the remote — changes nothing locally"
          disabled={!!busy || data.remotes.length === 0}
          onClick={() => void run("fetch", () => api.gitFetch())}
        >
          ⟲
        </button>
      </div>

      {current && (
        <div className="scm-sync">
          {current.upstream ? (
            <span className="muted" title={`Tracking ${current.upstream}`}>
              {current.gone
                ? "upstream gone"
                : current.ahead === 0 && current.behind === 0
                  ? "up to date"
                  : `${current.ahead} ahead, ${current.behind} behind`}
            </span>
          ) : (
            <span className="muted">no upstream</span>
          )}
          <span className="grow" />
          {current.behind > 0 && (
            <button
              className="btn subtle sm"
              disabled={!!busy}
              title={`Bring in ${current.behind} commit(s) from ${current.upstream}`}
              onClick={() =>
                void run("pull", async () => {
                  const result = await api.gitPull();
                  if (result.conflicted) {
                    window.alert(
                      "The pull left conflicts to resolve. They are listed in " +
                        "Source Control.",
                    );
                  }
                })
              }
            >
              Pull {current.behind}
            </button>
          )}
          {(current.ahead > 0 || !current.upstream) && data.remotes.length > 0 && (
            <button
              className="btn subtle sm"
              disabled={!!busy}
              title={
                current.upstream
                  ? `Send ${current.ahead} commit(s) to ${current.upstream}`
                  : "This branch has never been pushed"
              }
              onClick={() =>
                void run("push", () =>
                  api.gitPush(
                    current.upstream
                      ? {}
                      : {
                          remote: data.remotes[0].name,
                          branch: current.name,
                          set_upstream: true,
                        },
                  ),
                )
              }
            >
              {current.upstream ? `Push ${current.ahead}` : "Publish branch"}
            </button>
          )}
          {!current.current || data.branches.length > 1 ? (
            <button
              className="btn icon"
              title="Delete a branch"
              disabled={!!busy}
              onClick={() => {
                const other = data.branches.find((item) => !item.current);
                if (other) void remove(other.name);
              }}
            >
              🗑
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}
