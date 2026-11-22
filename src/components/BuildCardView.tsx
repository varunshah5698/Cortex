import { useEffect, useState } from "react";
import type { BuildCard } from "../lib/chat";
import { fmtNumber, previewUrl, subscribe } from "../lib/harness";

const statusLabel: Record<BuildCard["status"], string> = {
  running: "building",
  // Not "built": a finished run has passed its checks, and nothing compiled it.
  done: "shipped",
  error: "failed",
};

/**
 * The build a chat answer turned into. While the run is live this follows its event
 * stream, so the card shows files and the review verdict as they land; once it ends the
 * final numbers are handed back to the thread, where they survive a reload.
 */
export function BuildCardView({
  card,
  onChange,
}: {
  card: BuildCard;
  onChange: (patch: Partial<BuildCard>) => void;
}) {
  const [shown, setShown] = useState(card);

  useEffect(() => setShown(card), [card]);

  useEffect(() => {
    if (card.status !== "running" || !card.runId) return;
    const paths = new Set<string>();
    let tokens = 0;
    let verdict: BuildCard["verdict"] = null;

    const source = subscribe(card.runId, (event) => {
      switch (event.type) {
        case "file_end":
          paths.add(event.path);
          setShown((v) => ({ ...v, files: paths.size }));
          break;
        case "usage":
          tokens += event.total_tokens;
          break;
        case "verify":
          verdict = { ok: event.ok, errors: event.errors, warnings: event.warnings };
          setShown((v) => ({ ...v, verdict }));
          break;
        case "done": {
          const patch: Partial<BuildCard> = {
            status: "done",
            files: paths.size || event.files?.length || card.files,
            tokens: event.metrics?.totals?.total_tokens ?? tokens,
            verdict,
          };
          setShown((v) => ({ ...v, ...patch }));
          onChange(patch);
          break;
        }
        case "error": {
          const patch: Partial<BuildCard> = { status: "error", error: event.message };
          setShown((v) => ({ ...v, ...patch }));
          onChange(patch);
          break;
        }
        default:
          break;
      }
    });
    return () => source.close();
    // A run's stream is opened once per run; `card` is the stored snapshot, not state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.runId, card.status]);

  return (
    <div className={`bcard is-${shown.status}`}>
      <div className="bcard-head">
        <span className="bcard-dot" />
        <b className="bcard-title">Build</b>
        <em>{statusLabel[shown.status]}</em>
        <code className="bcard-id">{card.runId.slice(0, 12) || "starting…"}</code>
      </div>

      <div className="bcard-meta">
        <span>{shown.files} file{shown.files === 1 ? "" : "s"}</span>
        <span>{fmtNumber(shown.tokens)} tokens</span>
        {shown.verdict && (
          <span className={`bcard-verdict ${shown.verdict.ok ? "is-ok" : "is-bad"}`}>
            {shown.verdict.ok
              ? "checks clean"
              : `${shown.verdict.errors} error${shown.verdict.errors === 1 ? "" : "s"}`}
          </span>
        )}
      </div>

      {shown.error && <p className="bcard-error">{shown.error}</p>}

      <div className="bcard-actions">
        <a href={previewUrl(card.runId)} target="_blank" rel="noreferrer">
          Open the preview
        </a>
        <a href={`#/files`} rel="noreferrer">
          Files
        </a>
      </div>
    </div>
  );
}
