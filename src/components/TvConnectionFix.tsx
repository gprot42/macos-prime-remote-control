/**
 * TvConnectionFix — guided "TV unreachable" repair.
 *
 * Shown when the app detects it can't reach the LG TV. Runs the backend
 * `repair_tv_connection` flow (re-discover the TV's IP via mDNS, Wake-on-LAN,
 * and optionally a Wi-Fi reset), streams progress, and reports the result with
 * clear next steps when a fix has to happen on the TV/router.
 */
import { useEffect, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { repairTvConnection, type TvRepairReport } from "../playback";

type Status = "idle" | "running" | "done";

export default function TvConnectionFix({
  onResolved,
  className = "",
}: {
  /** Called after each attempt with whether the TV is now reachable. */
  onResolved?: (reachable: boolean) => void;
  className?: string;
}) {
  const [status, setStatus] = useState<Status>("idle");
  const [log, setLog] = useState<string[]>([]);
  const [report, setReport] = useState<TvRepairReport | null>(null);
  const [triedWifi, setTriedWifi] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  // Stream backend progress lines.
  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | null = null;
    listen<string>("repair-progress", (e) => {
      if (!mounted) return;
      setLog((prev) => [...prev, e.payload]);
      requestAnimationFrame(() => logRef.current?.scrollTo({ top: logRef.current.scrollHeight }));
    }).then((fn) => {
      if (mounted) unlisten = fn;
      else fn();
    });
    return () => {
      mounted = false;
      unlisten?.();
    };
  }, []);

  const attempt = async (restartWifi: boolean) => {
    setStatus("running");
    setReport(null);
    setLog([]);
    if (restartWifi) setTriedWifi(true);
    try {
      const r = await repairTvConnection(restartWifi);
      setReport(r);
      onResolved?.(r.reachable);
    } catch (err) {
      setReport({
        reachable: false,
        ip: "",
        ip_changed: false,
        discovered: false,
        wifi_restarted: restartWifi,
        steps: [],
        advice: String(err),
      });
      onResolved?.(false);
    } finally {
      setStatus("done");
    }
  };

  const running = status === "running";
  const reachable = report?.reachable === true;

  return (
    <div className={`space-y-2 ${className}`}>
      {/* Action buttons */}
      {!reachable && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={running}
            onClick={() => void attempt(false)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-semibold
                       bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white transition-colors"
          >
            {running && !triedWifi ? (
              <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            ) : (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
              </svg>
            )}
            Fix connection
          </button>

          {status !== "running" && (
            <button
              type="button"
              disabled={running}
              onClick={() => void attempt(true)}
              title="Briefly turns Wi-Fi off and on to force a fresh connection"
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-semibold
                         bg-zinc-700 hover:bg-zinc-600 disabled:opacity-50 text-white transition-colors"
            >
              {running && triedWifi ? (
                <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : null}
              Reset Wi-Fi &amp; retry
            </button>
          )}
        </div>
      )}

      {/* Streaming progress */}
      {running && log.length > 0 && (
        <div
          ref={logRef}
          className="bg-zinc-900 rounded-lg p-2 max-h-24 overflow-y-auto font-mono
                     text-[11px] leading-relaxed text-zinc-300 border border-zinc-800"
        >
          {log.map((line, i) => (
            <span key={i}>{line}</span>
          ))}
        </div>
      )}

      {/* Result */}
      {status === "done" && reachable && (
        <p className="flex items-center gap-1.5 text-emerald-400 text-sm font-medium">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          </svg>
          Connection restored{report?.ip ? ` (${report.ip})` : ""}. Press Play to try again.
        </p>
      )}
      {status === "done" && !reachable && report?.advice && (
        <p className="text-amber-300/90 text-xs leading-relaxed">{report.advice}</p>
      )}
    </div>
  );
}
