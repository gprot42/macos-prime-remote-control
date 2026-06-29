/** Short cache-bust token for locally cached poster JPEGs. */
export function imageUrlVersion(url: string): string {
  let h = 0;
  for (let i = 0; i < url.length; i++) {
    h = (Math.imul(31, h) + url.charCodeAt(i)) | 0;
  }
  return Math.abs(h).toString(36);
}

export function cachedImageHttpUrl(
  port: number,
  contentId: string,
  imageUrl: string | null | undefined,
): string | undefined {
  if (!port || !imageUrl) return undefined;
  const stem = contentId.replace(/[^\w\-]/g, "_");
  const v = imageUrlVersion(imageUrl);
  return `http://127.0.0.1:${port}/${stem}.jpg?v=${v}`;
}

export interface PrimeTitle {
  title: string;
  content_id: string;
  entity_type: string | null;
  year: number | null;
  runtime_min: number | null;
  runtime_str: string | null;
  asin: string | null;
  gti: string | null;
  source: string | null;
  container: string | null;
  availability: string | null;
  included_with_prime: boolean | null;
  included_with_channel: string | null;
  rent_from: string | null;
  buy_from: string | null;
  focus_message: string | null;
  prime_catalog: boolean | null;
  image_url: string | null;
  title_logo_url: string | null;
  synopsis: string | null;
}

/** A single episode of a TV season/series, as returned by `list_episodes`. */
export interface PrimeEpisode {
  content_id: string;
  gti: string | null;
  sequence_number: number | null;
  title: string | null;
  runtime_min: number | null;
}

export interface AppConfig {
  tv_ip: string;
  profile: number;
  project_root: string;
  cache_ttl_secs: number;
  /** Show titles included with a Prime subscription (green). */
  show_prime: boolean;
  /** Show titles requiring a channel add-on (purple). */
  show_channel: boolean;
  /** Show titles available to rent or buy (orange). */
  show_rent_buy: boolean;
  /** Show titles with unknown/unresolved availability (grey). */
  show_other: boolean;
  /** Detect VPN/region changes and refresh catalog cache automatically. */
  detect_vpn_region: boolean;
  /** Default play target: TV or Mac in-app Prime window. */
  default_playback_target: PlaybackTarget;
}

export type PlaybackTarget = "tv" | "mac";

export const DEFAULT_CONFIG: AppConfig = {
  tv_ip: "192.168.0.79",
  profile: 0,
  project_root: "",
  cache_ttl_secs: 21600,
  show_prime: true,
  show_channel: false,
  show_rent_buy: false,
  show_other: true,
  detect_vpn_region: true,
  default_playback_target: "tv",
};

export type EntityTypeFilter = "all" | "Movie" | "TV Show";

/** Format a cache age (seconds) as a human-readable string. */
export function formatCacheAge(secs: number): string {
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export interface CatalogGroup {
  label: string;
  items: PrimeTitle[];
}

export interface Bookmark {
  content_id: string;
  added_at: number;
  item: PrimeTitle;
  /** Original series item when bookmarking a specific episode. */
  source_item?: PrimeTitle | null;
  /** Episode detail ID to launch on the TV (most reliable for episode bookmarks). */
  episode_content_id?: string | null;
  /** Episode number to play when opening an episode bookmark. */
  play_episode?: number | null;
}

// ─── Availability label ───────────────────────────────────────────────────────

export type AccessLabel = "Prime" | "Channel" | "Rent/Buy" | "Rent" | "Buy" | "?" | "-";

/** Broad bucket used for category filtering (maps many labels → 4 categories). */
export type AccessCategory = "prime" | "channel" | "rent_buy" | "other";

export function getAccessLabel(item: PrimeTitle): AccessLabel {
  if (item.included_with_prime || item.prime_catalog) return "Prime";
  if (item.included_with_channel) return "Channel";
  if (item.rent_from && item.buy_from) return "Rent/Buy";
  if (item.rent_from) return "Rent";
  if (item.buy_from) return "Buy";
  const s = (item.availability || "").toLowerCase();
  if (!s) return "-";
  // Prime membership trial/renewal copy (not a channel add-on).
  if (
    s.includes("prime trial") ||
    s.includes("free prime") ||
    s.includes("trial of prime") ||
    (s.includes("prime") && s.includes("trial"))
  ) {
    return "Prime";
  }
  // Regional Prime upsell omits the word "Prime" (e.g. "Auto-renews at SEK 69/month after trial").
  if (
    (s.includes("auto-renew") || s.includes("auto renew")) &&
    s.includes("after trial")
  ) {
    return "Prime";
  }
  // Paid channel add-on monthly pricing — but not Prime membership renewal.
  const monthlyPrice = /\d+(?:[.,]\d+)?\s*(?:\/|per\s+|a\s+)mo(?:nth)?\b/.test(s);
  const autoRenewMonthly =
    (s.includes("auto-renew") || s.includes("auto renew")) && s.includes("month");
  if ((monthlyPrice || autoRenewMonthly) && !s.includes("prime")) return "Channel";
  if (s.includes("rent") || s.includes("buy")) return "Rent/Buy";
  if (s.includes("channel")) return "Channel";
  if (
    s.includes("prime") ||
    s.includes("trial") ||
    s.includes("auto-renew") ||
    s.includes("auto renew")
  )
    return "Prime";
  return "?";
}

export function getAccessCategory(label: AccessLabel): AccessCategory {
  if (label === "Prime") return "prime";
  if (label === "Channel") return "channel";
  if (label === "Rent/Buy" || label === "Rent" || label === "Buy") return "rent_buy";
  return "other";
}

/** Filter a title against the config visibility toggles. */
export function isTitleVisible(item: PrimeTitle, cfg: AppConfig): boolean {
  const label = getAccessLabel(item);
  const cat = getAccessCategory(label);
  if (cat === "prime" && !cfg.show_prime) return false;
  if (cat === "channel" && !cfg.show_channel) return false;
  if (cat === "rent_buy" && !cfg.show_rent_buy) return false;
  if (cat === "other" && !cfg.show_other) return false;
  return true;
}

export function groupTitles(items: PrimeTitle[]): CatalogGroup[] {
  const groups = new Map<string, PrimeTitle[]>();
  for (const item of items) {
    const key = item.container || "Other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(item);
  }
  const result: CatalogGroup[] = [];
  for (const [label, groupItems] of groups) {
    result.push({ label, items: groupItems });
  }
  return result;
}

// ─── Color palette (single source of truth) ──────────────────────────────────

/** Tailwind classes for a filled access badge. */
export function accessBadgeStyle(label: AccessLabel): string {
  switch (label) {
    case "Prime":
      // Green = Available with Prime subscription
      return "bg-emerald-600 text-white";
    case "Channel":
      return "bg-purple-600 text-white";
    case "Rent/Buy":
    case "Rent":
    case "Buy":
      return "bg-orange-500 text-white";
    default:
      return "bg-zinc-600 text-zinc-300";
  }
}

/** Solid bg colour for the category swatch in settings checkboxes. */
export const CATEGORY_COLORS: Record<AccessCategory, string> = {
  prime: "bg-emerald-600",
  channel: "bg-purple-600",
  rent_buy: "bg-orange-500",
  other: "bg-zinc-500",
};

/** Text colour accent matching the category (for labels, borders, etc.). */
export const CATEGORY_TEXT: Record<AccessCategory, string> = {
  prime: "text-emerald-400",
  channel: "text-purple-400",
  rent_buy: "text-orange-400",
  other: "text-zinc-400",
};

/** Border colour for checked checkboxes / ring. */
export const CATEGORY_BORDER: Record<AccessCategory, string> = {
  prime: "border-emerald-500 ring-emerald-500",
  channel: "border-purple-500 ring-purple-500",
  rent_buy: "border-orange-500 ring-orange-500",
  other: "border-zinc-500 ring-zinc-500",
};
