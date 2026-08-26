import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { qk, useRepository } from "../../api/hooks";
import { api } from "../../api/client";
import { Metric } from "./Metric";
import { fmtDateTime } from "./format";
import { BRAND } from "../../lib/branding";

export function RepositoryView() {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useRepository();
  const [busy, setBusy] = useState(false);

  const reindex = async () => {
    setBusy(true);
    try {
      await api.reindex();
      await qc.invalidateQueries({ queryKey: qk.repository });
    } finally {
      setBusy(false);
    }
  };

  const empty = data && data.file_count === 0;

  return (
    <div className="split-right">
      <div className="toolbar">
        <button className="btn primary" disabled={busy} onClick={() => void reindex()}>
          {busy ? "Indexing…" : "Rebuild index"}
        </button>
        <span className="muted" style={{ fontSize: 11 }}>
          What {BRAND} knows about this codebase — the same index the agent
          consults before it plans.
        </span>
        <span className="grow" />
        {data?.generated_at && (
          <span className="badge">Indexed {fmtDateTime(data.generated_at)}</span>
        )}
      </div>
      {isLoading && <div className="empty">Loading…</div>}
      {isError && (
        <div className="empty">
          Repository index unavailable: {String((error as Error)?.message)}.
          Rebuild it above.
        </div>
      )}
      {data && (
        <div className="scroll-y" style={{ flex: 1 }}>
          {empty && (
            <div className="empty">
              No files indexed yet. Select <strong>Rebuild index</strong> to scan
              this repository.
            </div>
          )}
          <div className="metric-row">
            <Metric k="Files" v={data.file_count.toLocaleString()} />
            <Metric k="Languages" v={Object.keys(data.languages).length} />
            <Metric k="Frameworks" v={data.frameworks.length || "none"} />
            <Metric k="API routes" v={data.routes.length} />
            <Metric k="DB models" v={data.database_models.length} />
            <Metric k="Test files" v={data.tests.length} />
          </div>

          <div className="section-title">Summary</div>
          <div className="md-block" style={{ whiteSpace: "pre-wrap" }}>
            {data.summary}
          </div>

          <div className="section-title">Frameworks</div>
          <div
            className="row"
            style={{ flexWrap: "wrap", padding: "0 14px", gap: 6 }}
          >
            {data.frameworks.length === 0 && (
              <span className="muted">None detected.</span>
            )}
            {data.frameworks.map((f) => (
              <span key={f} className="badge info">
                {f}
              </span>
            ))}
          </div>

          <div className="section-title">Entry points</div>
          <div className="md-block mono" style={{ fontSize: 12 }}>
            {data.entrypoints.length === 0
              ? "None detected."
              : data.entrypoints.join("\n")}
          </div>

          <div className="section-title">Languages</div>
          {Object.keys(data.languages).length === 0 ? (
            <div className="empty">None detected.</div>
          ) : (
            <table className="dtable">
              <thead>
                <tr>
                  <th>Language</th>
                  <th>Files</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.languages)
                  .sort((a, b) => b[1] - a[1])
                  .map(([lang, count]) => (
                    <tr key={lang}>
                      <td>{lang}</td>
                      <td className="num">{count}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
