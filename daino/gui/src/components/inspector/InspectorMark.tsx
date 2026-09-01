import { useQALatest } from "../../api/hooks";

/**
 * A dot on the INSPECTOR tab: pulsing while a scan runs, then coloured by the
 * verdict it landed on.
 *
 * The verdict outlives its notification, so the tab keeps showing whether this
 * checkout is currently cleared to push — from whichever tab you are in.
 */
export function InspectorMark() {
  const { data: qa } = useQALatest();
  if (qa?.running) {
    return <span className="tab-mark running" title="Inspection running" />;
  }
  const verdict = qa?.report?.verdict;
  if (!verdict || verdict === "unknown") return null;
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
