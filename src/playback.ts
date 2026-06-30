import { invoke } from "@tauri-apps/api/core";
import type { PrimeTitle } from "./types";

// Markers emitted by lg-tv-connect.py / the Rust commands when the TV can't be
// reached (off, in standby, wrong IP, or off the network). Used to surface a
// clear "TV unreachable" state instead of a vague failure.
const TV_UNREACHABLE_PATTERNS = [
  /could not reach tv/i,
  /host is down/i,
  /timed out connecting/i,
  /timed out after \d+s/i,
  /may be off or unreachable/i,
  /cannot connect to host/i,
  /could not connect/i,
];

/** True when a log/error line indicates the TV is unreachable (not a content error). */
export function isTvUnreachableMessage(text: string): boolean {
  return TV_UNREACHABLE_PATTERNS.some((re) => re.test(text));
}

export interface TvRepairReport {
  reachable: boolean;
  ip: string;
  ip_changed: boolean;
  discovered: boolean;
  wifi_restarted: boolean;
  steps: string[];
  advice: string | null;
}

/**
 * Ask the backend to try to restore TV connectivity from the Mac side.
 * Non-disruptive steps (mDNS re-discovery + Wake-on-LAN) always run; pass
 * restartWifi=true to also power-cycle the Mac's Wi-Fi. Progress streams via
 * "repair-progress" events.
 */
export async function repairTvConnection(restartWifi: boolean): Promise<TvRepairReport> {
  return await invoke<TvRepairReport>("repair_tv_connection", { restartWifi });
}

/** Quick check whether the TV's control port is reachable right now. */
export async function checkTvReachable(): Promise<boolean> {
  return await invoke<boolean>("check_tv_reachable");
}

export async function playOnMac(
  item: PrimeTitle,
  options?: { episode?: number | null; contentId?: string | null },
): Promise<void> {
  const contentId = options?.contentId?.trim() || item.content_id;
  const episode = options?.episode ?? null;
  await invoke("play_on_mac", {
    contentId,
    episode: episode != null && episode >= 1 ? episode : null,
    title: item.title,
  });
}

export async function playOnTv(
  item: PrimeTitle,
  config: { tv_ip: string; profile: number },
  options?: { episode?: number | null; contentId?: string | null; startSeconds?: number | null },
): Promise<void> {
  const contentId = options?.contentId?.trim() || item.content_id;
  const episode = options?.contentId ? null : options?.episode ?? null;
  const startSeconds = options?.startSeconds ?? null;
  await invoke("play_on_tv", {
    contentId,
    profile: config.profile,
    tvIp: config.tv_ip,
    episode: episode != null && episode >= 1 ? episode : null,
    startSeconds: startSeconds != null && startSeconds >= 1 ? Math.round(startSeconds) : null,
  });
}