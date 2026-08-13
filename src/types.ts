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

/** Parse a Prime display runtime string ("54 min", "1 h 23 min") into whole minutes. */
export function runtimeMinutesFromStr(raw: string | null | undefined): number | null {
  if (!raw?.trim()) return null;
  const text = raw.trim();
  let total = 0;
  const hMatch = text.match(/(\d+)\s*h/i);
  const mMatch = text.match(/(\d+)\s*min/i);
  if (hMatch) total += parseInt(hMatch[1], 10) * 60;
  if (mMatch) total += parseInt(mMatch[1], 10);
  if (total > 0) return total;
  if (/^\d+$/.test(text)) return parseInt(text, 10);
  return null;
}

/** Total runtime in seconds from catalog minutes and/or display string. */
export function runtimeSecondsFromTitle(item: PrimeTitle | null | undefined): number | null {
  if (!item) return null;
  if (item.runtime_min != null && item.runtime_min > 0) return item.runtime_min * 60;
  const mins = runtimeMinutesFromStr(item.runtime_str);
  return mins != null ? mins * 60 : null;
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

export interface PrimeProfileOption {
  name: string;
  index: number;
  row: number;
  profile_type: string;
}

export interface AppConfig {
  tv_ip: string;
  profile: number;
  /** Optional named Prime picker profile from Settings. When set, play uses this name. */
  profile_name?: string;
  project_root: string;
  cache_ttl_secs: number;
  /** Show titles included with a Prime subscription (green). */
  show_prime: boolean;
  /** Show titles requiring a channel add-on (HBO, Max, Lionsgate+, …). */
  show_channel: boolean;
  /** Show titles available to rent or buy (orange). */
  show_rent_buy: boolean;
  /** Show titles with unknown/unresolved availability (grey). */
  show_other: boolean;
  /** Detect VPN/region changes and refresh catalog cache automatically. */
  detect_vpn_region: boolean;
  /** Default play target: TV or Mac in-app Prime window. */
  default_playback_target: PlaybackTarget;
  /** Optional MAC for Wake-on-LAN power-on (e.g. AA:BB:CC:DD:EE:FF). */
  tv_mac?: string;
  /** Default TV volume level (0–100) applied when starting playback. */
  default_tv_volume?: number;
  /** When true, set default_tv_volume after play and when powering on the TV. */
  apply_default_tv_volume?: boolean;
  /** Show subtitle controls in the remote bar (on/off toggled there). */
  subtitles_enabled?: boolean;
  /**
   * Last successful caption toggle from this app (diagnostic / future use).
   * The remote button does not restore this on startup — it always starts off
   * (grey) per session so it is not blue while TV captions are off.
   */
  subtitles_active?: boolean;
  /** Preferred subtitle language code (see SUBTITLE_LANGUAGES). */
  subtitle_language?: string;
  /** DOWN-key presses after pause to reach Prime's transport icon row. */
  subtitle_focus_down?: number;
  /**
   * LEFT presses *after RIGHT-homing to Audio* to reach Subtitles CC (default **1**).
   * Bar: Start again → [Next] → Subtitles → Audio. We home right so ENTER never
   * hits Start again (which restarts the title at 00:00). Legacy 1|2 (old
   * RIGHT-from-Start counts) both migrate to 1.
   */
  subtitle_focus_right?: number;
  /** UP presses inside the panel to select Subtitles instead of Audio. */
  subtitle_section_up?: number;
  /** LEFT presses inside the panel to reach the Subtitles column. */
  subtitle_section_left?: number;
  /**
   * DOWN presses in the expanded captions list after Select on Off (-1 = auto).
   * Auto: Off=0, English=1 (title-dependent).
   */
  subtitle_menu_down?: number;
}

/**
 * Map legacy focus-right to LEFT steps from Audio → Subtitles CC.
 * Navigation RIGHT-homes to Audio then LEFT×N (never LEFT-homes to Start again).
 * Legacy RIGHT-from-Start counts 1|2 both meant Subtitles → 1.
 */
export function migrateSubtitleFocusRight(value: number | undefined): number {
  if (value === undefined || value === null || Number.isNaN(value)) return 1;
  if (value === 1 || value === 2) return 1;
  return value;
}

export type PlaybackTarget = "tv" | "mac";

/** Subtitle languages offered in Settings (menu order varies by title). */
export const SUBTITLE_LANGUAGES: { code: string; label: string }[] = [
  { code: "en", label: "English" },
  { code: "en-cc", label: "English [CC]" },
  { code: "sv", label: "Swedish" },
  { code: "de", label: "German" },
  { code: "fr", label: "French" },
  { code: "es", label: "Spanish" },
  { code: "it", label: "Italian" },
  { code: "pt", label: "Portuguese" },
  { code: "nl", label: "Dutch" },
  { code: "no", label: "Norwegian" },
  { code: "da", label: "Danish" },
  { code: "fi", label: "Finnish" },
  { code: "pl", label: "Polish" },
  { code: "ja", label: "Japanese" },
  { code: "ko", label: "Korean" },
];

export const DEFAULT_CONFIG: AppConfig = {
  tv_ip: "192.168.0.79",
  profile: 0,
  profile_name: "",
  project_root: "",
  cache_ttl_secs: 21600,
  show_prime: true,
  show_channel: false,
  show_rent_buy: false,
  show_other: true,
  detect_vpn_region: true,
  default_playback_target: "tv",
  tv_mac: "",
  default_tv_volume: 13,
  apply_default_tv_volume: true,
  subtitles_enabled: false,
  subtitles_active: false,
  subtitle_language: "en",
  subtitle_focus_down: 1,
  // After RIGHT-home to Audio: 1 = LEFT to Subtitles CC (never Start again).
  subtitle_focus_right: 1,
  subtitle_section_up: 0,
  // 0 for dedicated Subtitles CC; 1 only if a combined panel starts on Audio
  subtitle_section_left: 0,
  subtitle_menu_down: -1,
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

/**
 * Titles Amazon still cards on "Included with Prime" that are not watchable
 * with a Prime membership. Unsigned listing copy matches real Prime titles
 * (same trial/auto-renew offer), so we match by name.
 */
export function isKnownNonPrimeMembershipTitle(title: string | null | undefined): boolean {
  const text = (title || "").trim().toLowerCase();
  if (!text) return false;
  return (
    text === "the vampire diaries" ||
    text.startsWith("the vampire diaries ") ||
    text.startsWith("the vampire diaries:") ||
    text.startsWith("the vampire diaries -")
  );
}

export function getAccessLabel(item: PrimeTitle): AccessLabel {
  // Channel add-on is exclusive (not free with Prime membership alone).
  if (item.included_with_channel) return "Channel";

  // Warner / Max series Amazon still parks on Prime storefronts.
  if (isKnownNonPrimeMembershipTitle(item.title)) {
    if (item.rent_from && item.buy_from) return "Rent/Buy";
    if (item.rent_from) return "Rent";
    if (item.buy_from) return "Buy";
    return "Rent/Buy";
  }

  // Entitlement / storefront-forced Prime wins over weak unsigned scrape copy.
  // Genre rows often set included_with_prime=true while availability still says
  // "Free trial of Prime … or rent or buy" — those used to be mislabeled Rent/Buy
  // and hidden when show_rent_buy is off.
  if (item.included_with_prime === true) return "Prime";

  // buybox isPrime / prime_catalog: free with Prime membership. Unsigned pages
  // still list Rent/Buy as non-member alternatives — those must not reclassify
  // membership titles as transactional (e.g. The Restless Garden).
  if (item.prime_catalog === true) return "Prime";

  // Strong structured transactional fields.
  if (item.rent_from && item.buy_from) return "Rent/Buy";
  if (item.rent_from) return "Rent";
  if (item.buy_from) return "Buy";

  const s = (item.availability || "").toLowerCase();
  const focus = (item.focus_message || "").toLowerCase();
  const container = (item.container || "").toLowerCase();
  const blob = `${s} ${focus} ${container}`;

  // Strong paywall / channel signals only. Do NOT treat generic unsigned-scrape
  // copy like "Watch with a 30 day free Prime trial, auto renews…" as paywall —
  // that appears on almost every title when not logged in and was wrongly
  // hiding the whole Included with Prime list.
  if (container.includes("rent or buy")) return "Rent/Buy";
  if (focus.includes("or buy") || focus.includes("or rent")) return "Rent/Buy";
  if (/\brent from\b/.test(blob) || /\bbuy from\b/.test(blob) || /\bpurchase for\b/.test(blob))
    return "Rent/Buy";
  // "Subscribe to MGM+" etc. — not Prime membership trial wording.
  if (/subscribe to (?!prime\b)/.test(focus)) return "Channel";
  if (s.includes("prime video channel") || s.includes("(prime video channel)"))
    return "Channel";

  // Explicitly NOT free with Prime (resolved) and not in membership catalog.
  if (item.included_with_prime === false) {
    if (blob.includes("channel") || /subscribe to (?!prime\b)/.test(focus))
      return "Channel";
    if (
      container.includes("rent or buy") ||
      focus.includes("or buy") ||
      focus.includes("purchase") ||
      /\brent from\b/.test(blob) ||
      /\bbuy from\b/.test(blob)
    )
      return "Rent/Buy";
    // Non-entitled + trial/subscribe focus (e.g. House of Ashur on Prime rows).
    if (focus.includes("trial") || focus.includes("subscribe") || focus.includes("or buy"))
      return "Rent/Buy";
    return "?";
  }

  // Unresolved (null): weak storefront signals.
  if (!s && !focus) return "-";

  if (
    s.includes("prime trial") ||
    s.includes("free prime") ||
    s.includes("trial of prime") ||
    (s.includes("prime") && s.includes("trial"))
  ) {
    return "Prime";
  }
  if (
    (s.includes("auto-renew") || s.includes("auto renew")) &&
    s.includes("after trial")
  ) {
    return "Prime";
  }
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

/** True for genre tabs (genre/action, genre/documentary, …). */
export function isGenreCollection(slug: string): boolean {
  return slug.startsWith("genre/");
}

/** Group a genre catalog into films vs series (merges Prime carousel rows). */
export function groupGenreTitles(items: PrimeTitle[], genreLabel: string): CatalogGroup[] {
  const movies: PrimeTitle[] = [];
  const tv: PrimeTitle[] = [];
  const other: PrimeTitle[] = [];
  for (const item of items) {
    if (item.entity_type === "Movie") movies.push(item);
    else if (item.entity_type === "TV Show" || item.entity_type === "TV Episode") tv.push(item);
    else other.push(item);
  }
  const groups: CatalogGroup[] = [];
  if (movies.length > 0) groups.push({ label: `${genreLabel} films`, items: movies });
  if (tv.length > 0) groups.push({ label: `${genreLabel} series`, items: tv });
  if (other.length > 0) groups.push({ label: "Other", items: other });
  return groups;
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
