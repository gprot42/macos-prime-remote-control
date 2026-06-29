import { invoke } from "@tauri-apps/api/core";
import type { PrimeTitle } from "./types";

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
  options?: { episode?: number | null; contentId?: string | null },
): Promise<void> {
  const contentId = options?.contentId?.trim() || item.content_id;
  const episode = options?.contentId ? null : options?.episode ?? null;
  await invoke("play_on_tv", {
    contentId,
    profile: config.profile,
    tvIp: config.tv_ip,
    episode: episode != null && episode >= 1 ? episode : null,
  });
}