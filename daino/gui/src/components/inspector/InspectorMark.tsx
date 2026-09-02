import { useQALatest } from "../../api/hooks";

/**
 * A dot on the INSPECTOR tab: pulsing while a scan runs, then coloured by the
 * verdict it landed on.
 *
 * The verdict outlives its notification, so the tab keeps showing whether this
 * checkout is currently cleared to push — from whichever tab you are in.
 *
 * "Currently" is the load-bearing word. A verdict is a statement about the code
 * that was inspected, and the moment the working tree moves it stops describing
 * anything. A stale report is shown greyed and says so, rather than leaving a
 * green "safe to push" over files nobody has looked at.
 */
export function InspectorMark() {
  const { data: qa } = useQALatest();
  if (qa?.running) {
    return <span className="tab-mark running" title="Inspection running" />;
  }
  const verdict = qa?.report?.verdict;
  if (!verdict || verdict === "unknown") return null;
  if (qa?.stale) {
    return (
      <span
        className="tab-mark stale"
        title={
          "The files have changed since the last inspection, so its verdict " +
          "no longer applies. Run it again before pushing."
        }
      />
    );
  }
  return (
    <span
      className={`tab-mark v-${verdict}`}
      title={
        verdict === "pass"
          ? "Last inspection: safe to push"
          : verdict === "warn"
            ? "Last inspection: review before pushing"
            : "Last inspection: do not push"
      }
    />
  );
}
