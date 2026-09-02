import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useWorkspaceLinks } from "../../api/hooks";
import { useUIStore } from "../../store/uiStore";
import { sendChatMessage } from "../../lib/agent";
import { handOffToCode } from "../../lib/handoff";
import { useDesignStore } from "../../store/designStore";
import type { Workspace } from "../../api/types";

const RELATION: Record<string, string> = {
  derived_from: "from",
  generated_from: "generated from",
  depends_on: "depends on",
  implements: "implements",
  describes: "describes",
  references: "references",
};

/**
 * What came from what, and what has fallen behind since.
 *
 * The warnings are advisory and stay that way: Daino says a document may be
 * outdated and offers to update it, but never rewrites one because something
 * upstream moved. Only the person knows whether the change mattered.
 */
export function LinksPanel({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient();
  const setArtifact = useUIStore((s) => s.setActiveArtifactPath);
  const setTab = useUIStore((s) => s.setActiveWorkspaceTab);
  const setActiveDesign = useDesignStore((s) => s.setActiveDesign);
  const { data } = useWorkspaceLinks(workspace.id);
  const links = data?.links ?? [];
  const stale = data?.stale ?? [];
  if (links.length === 0 && stale.length === 0) return null;

  const external = links.filter((link) => link.source_kind !== "artifact");

  const ignore = async (linkId: string) => {
    await api.acknowledgeWorkspaceLink(workspace.id, linkId);
    await qc.invalidateQueries({ queryKey: qk.workspaceLinks(workspace.id) });
  };

  return (
    <div className="ws-links">
      {stale.length > 0 && (
        <>
          <div className="section-title">May be outdated</div>
          <ul className="ws-stale-list">
            {stale.map((item) => (
              <li key={item.link_id} className="ws-stale">
                <div className="ws-stale-text">
                  <strong>{item.path}</strong> may be outdated because{" "}
                  {item.source_of_truth} changed.
                </div>
                <div className="ws-stale-actions">
                  <button
                    className="btn subtle sm"
                    onClick={() => setArtifact(item.path)}
                    title="Open it and decide for yourself"
                  >
                    Review
                  </button>
                  <button
                    className="btn subtle sm"
                    title="Ask Daino to bring it in line with its source"
                    onClick={() =>
                      void sendChatMessage(
                        `${item.path} was written from ${item.source_of_truth}, which has ` +
                          `changed since. Read both, then update ${item.path} where it no ` +
                          `longer matches — leave the rest of it alone.`,
                      )
                    }
                  >
                    Update
                  </button>
                  <button className="btn subtle sm" onClick={() => void ignore(item.link_id)}>
                    Ignore
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {external.length > 0 && (
        <>
          <div className="section-title">Linked work</div>
          <ul className="ws-link-list">
            {external.map((link) => (
              <li key={link.id} className="ws-link">
                <span className="ws-link-title">{link.title || link.source_path}</span>
                <span className="ws-link-where">
                  {link.source_kind === "design" ? "Created in DESIGN" : "Prepared for CODE"}
                </span>
                <button
                  className="btn subtle sm"
                  onClick={() => {
                    if (link.source_kind === "design") {
                      // The link's source_path is the design id, so opening
                      // DESIGN can land on the canvas this workspace is about
                      // rather than on whatever was last selected.
                      if (link.source_path) setActiveDesign(link.source_path);
                      setTab("design");
                      return;
                    }
                    // A code handoff is a brief in the workspace: open it, then
                    // hand it to the conversation CODE is about to show. The
                    // message is queued rather than sent here, because right now
                    // the shared socket still points at this workspace's thread.
                    setArtifact(link.source_path);
                    handOffToCode(
                      `Build what ${workspace.folder}/${link.source_path} describes. ` +
                        "Read it and the documents it references first.",
                    );
                    setTab("code");
                  }}
                >
                  {link.source_kind === "design" ? "Open in DESIGN" : "Start in CODE"}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {links.length > external.length && (
        <>
          <div className="section-title">Provenance</div>
          <ul className="ws-link-list">
            {links
              .filter((link) => link.source_kind === "artifact")
              .map((link) => (
                <li key={link.id} className="ws-link provenance">
                  <button className="ws-link-path" onClick={() => setArtifact(link.source_path)}>
                    {link.source_path}
                  </button>
                  <span className="ws-link-relation">{RELATION[link.relation]}</span>
                  <button
                    className="ws-link-path"
                    onClick={() => setArtifact(link.target_path)}
                    disabled={!link.target_path}
                  >
                    {link.target_path || "—"}
                  </button>
                </li>
              ))}
          </ul>
        </>
      )}
    </div>
  );
}
