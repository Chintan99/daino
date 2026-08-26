import { useEffect, useState } from "react";
import { ReactFlowProvider } from "reactflow";
import { useQueryClient } from "@tanstack/react-query";
import { useDesigns, qk } from "../../api/hooks";
import { api } from "../../api/client";
import { useDesignStore } from "../../store/designStore";
import type { DesignType } from "../../api/types";
import { DesignCanvas } from "./DesignCanvas";
import { DesignToolbar } from "./DesignToolbar";
import { DesignInspector } from "./DesignInspector";

const TYPES: DesignType[] = [
  "architecture",
  "flowchart",
  "database",
  "api_flow",
  "ui",
  "prototype",
];

export function DesignWorkspace() {
  const qc = useQueryClient();
  const { data, isLoading } = useDesigns();
  const activeId = useDesignStore((s) => s.activeDesignId);
  const setActive = useDesignStore((s) => s.setActiveDesign);

  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState<DesignType>("architecture");
  const [busy, setBusy] = useState(false);

  const designs = data?.designs ?? [];

  // auto-select the first design if none selected
  useEffect(() => {
    if (!activeId && designs.length > 0) setActive(designs[0].id);
  }, [activeId, designs, setActive]);

  const create = async () => {
    if (!newName.trim()) return;
    setBusy(true);
    try {
      const d = await api.createDesign(newName.trim(), newType);
      setNewName("");
      qc.setQueryData(qk.design(d.id), d);
      await qc.invalidateQueries({ queryKey: qk.designs });
      setActive(d.id);
    } finally {
      setBusy(false);
    }
  };

  const generate = async () => {
    setBusy(true);
    try {
      const d = await api.generateDesignFromCode();
      qc.setQueryData(qk.design(d.id), d);
      await qc.invalidateQueries({ queryKey: qk.designs });
      setActive(d.id);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("Delete this design?")) return;
    await api.deleteDesign(id);
    if (activeId === id) setActive(null);
    await qc.invalidateQueries({ queryKey: qk.designs });
  };

  return (
    <div className="design-workspace">
      <div className="design-list">
        <div className="panel-header">Designs</div>
        <div style={{ padding: 8, borderBottom: "1px solid var(--border)" }}>
          <input
            className="input"
            placeholder="Design name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void create()}
            style={{ marginBottom: 6 }}
          />
          <select
            className="input"
            value={newType}
            onChange={(e) => setNewType(e.target.value as DesignType)}
            style={{ marginBottom: 6 }}
          >
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <button
            className="btn primary"
            style={{ width: "100%", marginBottom: 6 }}
            disabled={busy || !newName.trim()}
            onClick={() => void create()}
          >
            Create
          </button>
          <button
            className="btn"
            style={{ width: "100%" }}
            disabled={busy}
            onClick={() => void generate()}
            title="Generate a design from the current codebase"
          >
            Generate from code
          </button>
        </div>
        <div className="scroll-y" style={{ flex: 1 }}>
          {isLoading && <div className="empty">Loading…</div>}
          {!isLoading && designs.length === 0 && (
            <div className="empty">No designs yet.</div>
          )}
          {designs.map((d) => (
            <div
              key={d.id}
              className={`design-list-item ${activeId === d.id ? "active" : ""}`}
              onClick={() => setActive(d.id)}
            >
              <div className="row">
                <strong className="grow">{d.name}</strong>
                <span
                  className="x"
                  style={{ color: "var(--text-2)" }}
                  onClick={(e) => {
                    e.stopPropagation();
                    void remove(d.id);
                  }}
                >
                  ✕
                </span>
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                {d.type} · {d.node_count} nodes · {d.edge_count} edges
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="design-main">
        {activeId ? (
          <>
            <DesignToolbar designId={activeId} />
            <div className="design-canvas">
              <ReactFlowProvider>
                <DesignCanvas designId={activeId} />
              </ReactFlowProvider>
            </div>
          </>
        ) : (
          <div className="empty" style={{ margin: "auto" }}>
            Create or select a design to start.
          </div>
        )}
      </div>

      {activeId && <DesignInspector designId={activeId} />}
    </div>
  );
}
