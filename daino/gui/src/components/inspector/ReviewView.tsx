import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  qk,
  useReviewFileDiff,
  useReviewHistory,
  useReviewLatest,
  useReviewSubject,
} from "../../api/hooks";
import { api, ApiError } from "../../api/client";
import type { ChangedFile, ChangeReview, ReviewScope } from "../../api/types";
import { Metric } from "../insights/Metric";
import { fmtDateTime, prettyStatus, statusClass } from "../insights/format";
import { openFileInEditor } from "../../lib/openFile";
import { FindingsTable } from "./FindingsTable";
import { VerdictBanner } from "./VerdictBanner";
import { REVIEW_VERDICT, VERDICT } from "./severity";

const SCOPES: { id: ReviewScope; label: string; hint: string }[] = [
  {
    id: "working",
    label: "WORKING",
    hint: "Everything uncommitted, including files you have just created",
  },
  { id: "staged", label: "STAGED", hint: "Only what is staged, as it would be committed" },
  {
    id: "branch",
    label: "BRANCH",
    hint: "This branch against its base — the pull request you would open",
  },
];

const KIND_MARK: Record<ChangedFile["kind"], string> = {
  added: "A",
  modified: "M",
  deleted: "D",
  renamed: "R",
  binary: "B",
};

/**
 * Review one change before it lands.
 *
 * The scan asks whether the repository is sound; this asks whether the change
 * is. It runs mechanically first — syntax, conflict markers, credentials,
 * debugging left in, test gaps — and then has reviewers read the diff for what
 * a regex cannot judge.
 */
export function ReviewView() {
  const qc = useQueryClient();
  const { data: latest, isLoading } = useReviewLatest();
  const { data: history } = useReviewHistory();
  const [scope, setScope] = useState<ReviewScope>("working");
  const [baseRef, setBaseRef] = useState("");
  const [viewing, setViewing] = useState<ChangeReview | null>(null);
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { data: subject, error: subjectError } = useReviewSubject(scope, baseRef);
  const { data: fileDiff } = useReviewFileDiff(openPath, scope, baseRef);

  const running = !!latest?.running;
  // A saved review the user picked wins until the next live run starts.
  const review = running
    ? (latest?.review ?? null)
    : (viewing ?? latest?.review ?? null);

  // The saved list has no reason to poll, but a finished run adds a row.
  useEffect(() => {
    if (!running) void qc.invalidateQueries({ queryKey: qk.reviewHistory });
  }, [running, qc]);

  useEffect(() => setOpenPath(null), [review?.id]);

  const start = async () => {
    setBusy(true);
    try {
      setViewing(null);
      await api.reviewRun({ scope, base_ref: baseRef });
      await qc.invalidateQueries({ queryKey: qk.reviewLatest });
    } catch (err) {
      window.alert(
        err instanceof ApiError
          ? err.message
          : `Could not start the review: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    await api.reviewCancel();
    await qc.invalidateQueries({ queryKey: qk.reviewLatest });
  };

  const openSaved = async (id: string) => {
    const result = await api.reviewReport(id);
    setViewing(result.review);
  };

  const blocked = subjectError instanceof Error ? subjectError.message : "";

  return (
    <div className="split">
      <div className="split-left" style={{ width: 300 }}>
        <div className="panel-header">
          Saved reviews
          <span className="spacer" />
          <button
            className="btn icon"
            title="Refresh"
            onClick={() => void qc.invalidateQueries({ queryKey: qk.reviewHistory })}
          >
            ⟳
          </button>
        </div>
        <div className="scroll-y" style={{ flex: 1 }}>
          {(history?.reviews.length ?? 0) === 0 && (
            <div className="empty">No review has been saved yet.</div>
          )}
          {history?.reviews.map((item) => (
            <div
              key={item.id}
              className={`ws-doc ${review?.id === item.id ? "active" : ""}`}
              onClick={() => void openSaved(item.id)}
            >
              <div className="ws-doc-head">
                <span className="ws-doc-title">{item.subject}</span>
                <span className={`verdict-pill v-${item.verdict}`}>
                  {VERDICT[item.verdict].label}
                </span>
              </div>
              <div className="ws-doc-meta">
                <span>{fmtDateTime(item.started_at)}</span>
                <span>{item.files.length} files</span>
                <span className="added">+{item.insertions}</span>
                <span className="removed">−{item.deletions}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="split-right">
        <div className="toolbar">
          <div className="segmented">
            {SCOPES.map((item) => (
              <button
                key={item.id}
                className={scope === item.id ? "active" : ""}
                onClick={() => setScope(item.id)}
                title={item.hint}
                disabled={running}
              >
                {item.label}
              </button>
            ))}
          </div>
          {scope === "branch" && (
            <input
              className="input"
              style={{ maxWidth: 190 }}
              value={baseRef}
              placeholder={subject?.base_ref || "base branch"}
              onChange={(e) => setBaseRef(e.target.value)}
              title="The branch to compare against. Empty picks it automatically."
              disabled={running}
            />
          )}
          <button
            className="btn primary"
            disabled={running || busy || !!blocked || subject?.empty}
            onClick={() => void start()}
          >
            {running ? "Reviewing…" : "Review change"}
          </button>
          {running && (
            <button className="btn danger" onClick={() => void cancel()}>
              Cancel
            </button>
          )}
          <span className="grow" />
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
            {blocked ||
              (subject?.empty
                ? "Nothing to review in this scope."
                : subject
                  ? `${subject.files} file(s) — ${subject.label}`
                  : SCOPES.find((item) => item.id === scope)?.hint)}
          </span>
        </div>

        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && !review && (
          <div className="empty">
            No review yet. Run one to see what this change does, what is wrong
            with it, and what it is missing.
          </div>
        )}

        {review && (
          <div className="scroll-y" style={{ flex: 1 }}>
            <VerdictBanner
              report={review}
              running={running}
              wording={REVIEW_VERDICT}
              runningLabel="REVIEWING…"
              runningHint="Mechanical findings land first; the written review follows."
            />

            <div className="metric-row">
              <Metric k="Subject" v={review.subject} />
              <Metric k="Status" v={prettyStatus(review.status)} />
              <Metric k="Files" v={review.files.length} />
              <Metric k="Added" v={`+${review.insertions}`} />
              <Metric k="Removed" v={`−${review.deletions}`} />
              <Metric k="Findings" v={review.findings.length} />
              <Metric k="Started" v={fmtDateTime(review.started_at)} />
            </div>

            {review.commits.length > 0 && (
              <>
                <div className="section-title">Commits — {review.commits.length}</div>
                <ul className="review-commits">
                  {review.commits.slice(0, 12).map((commit, index) => (
                    <li key={index}>{commit}</li>
                  ))}
                  {review.commits.length > 12 && (
                    <li className="muted">
                      … and {review.commits.length - 12} more
                    </li>
                  )}
                </ul>
              </>
            )}

            <div className="section-title">Files</div>
            {review.files.length === 0 ? (
              <div className="empty">This change touches no files.</div>
            ) : (
              <table className="dtable">
                <thead>
                  <tr>
                    <th style={{ width: 34 }} />
                    <th>Path</th>
                    <th style={{ width: 110 }}>Change</th>
                    <th style={{ width: 90 }}>Findings</th>
                  </tr>
                </thead>
                <tbody>
                  {review.files.map((file) => (
                    <FileRow
                      key={file.path}
                      file={file}
                      open={openPath === file.path}
                      onToggle={() =>
                        setOpenPath(openPath === file.path ? null : file.path)
                      }
                      patch={openPath === file.path ? (fileDiff?.patch ?? "") : ""}
                    />
                  ))}
                </tbody>
              </table>
            )}

            <div className="section-title">Findings</div>
            {review.findings.length === 0 ? (
              <div className="empty">
                {running
                  ? "Reading the change…"
                  : "Nothing mechanical to report about this change."}
              </div>
            ) : (
              <FindingsTable findings={review.findings} />
            )}

            <div className="section-title">Checked</div>
            <table className="dtable">
              <thead>
                <tr>
                  <th>Check</th>
                  <th style={{ width: 110 }}>Status</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {review.checks.map((check) => (
                  <tr key={check.id}>
                    <td>{check.label}</td>
                    <td>
                      <span className={statusClass(check.status)}>
                        {prettyStatus(check.status)}
                      </span>
                    </td>
                    <td className="muted">{check.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {review.specialists.length > 0 && (
              <>
                <div className="section-title">Reviewers</div>
                <table className="dtable">
                  <thead>
                    <tr>
                      <th>Reviewer</th>
                      <th style={{ width: 110 }}>Status</th>
                      <th>Result</th>
                      <th style={{ width: 70 }}>Steps</th>
                    </tr>
                  </thead>
                  <tbody>
                    {review.specialists.map((item) => (
                      <tr key={item.id}>
                        <td>
                          {item.label}
                          <div
                            className="muted"
                            style={{ fontSize: "var(--fs-11)" }}
                          >
                            {item.objective}
                          </div>
                        </td>
                        <td>
                          <span className={statusClass(item.status)}>
                            {prettyStatus(item.status)}
                          </span>
                        </td>
                        <td className="ellipsis" title={item.error || item.summary}>
                          {item.error || item.summary.split("\n")[0] || "—"}
                        </td>
                        <td className="num">{item.steps}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            <div className="section-title">The review</div>
            <div className="md-block">
              {review.summary ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {review.summary}
                </ReactMarkdown>
              ) : (
                <span className="muted">
                  The written review appears when the reviewers finish.
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function FileRow({
  file,
  open,
  onToggle,
  patch,
}: {
  file: ChangedFile;
  open: boolean;
  onToggle: () => void;
  patch: string;
}) {
  return (
    <>
      <tr className="click" onClick={onToggle}>
        <td>
          <span className={`change-mark k-${file.kind}`} title={file.kind}>
            {KIND_MARK[file.kind]}
          </span>
        </td>
        <td className="mono ellipsis" title={file.previous_path || file.path}>
          {file.previous_path && (
            <span className="muted">{file.previous_path} → </span>
          )}
          {file.path}
        </td>
        <td className="mono">
          <span className="added">+{file.insertions}</span>{" "}
          <span className="removed">−{file.deletions}</span>
        </td>
        <td className="num">
          {file.findings > 0 ? (
            <span className="badge warn">{file.findings}</span>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={4} style={{ padding: 0 }}>
            <div className="review-diff">
              <div className="review-diff-bar">
                <span className="mono muted">{file.path}</span>
                <span className="grow" />
                <button
                  className="btn subtle sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    void openFileInEditor(file.path);
                  }}
                >
                  Open in CODE
                </button>
              </div>
              {file.binary ? (
                <div className="empty">
                  A binary file has no diff to read.
                </div>
              ) : (
                <pre className="mono">
                  {(patch || "Loading…").split("\n").map((line, index) => (
                    <div key={index} className={patchClass(line)}>
                      {line || " "}
                    </div>
                  ))}
                </pre>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** Colour a patch line by what it does, the way every diff view does. */
function patchClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "patch-head";
  if (line.startsWith("@@")) return "patch-hunk";
  if (line.startsWith("diff --git") || line.startsWith("new file")) return "patch-head";
  if (line.startsWith("+")) return "patch-add";
  if (line.startsWith("-")) return "patch-del";
  return "";
}
