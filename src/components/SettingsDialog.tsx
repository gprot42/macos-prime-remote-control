import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  AppConfig,
  AccessCategory,
  PlaybackTarget,
  CATEGORY_COLORS,
  CATEGORY_TEXT,
  CATEGORY_BORDER,
} from "../types";

const TTL_OPTIONS = [
  { label: "1 hour", secs: 3600 },
  { label: "6 hours", secs: 21600 },
  { label: "12 hours", secs: 43200 },
  { label: "24 hours", secs: 86400 },
  { label: "3 days", secs: 259200 },
  { label: "7 days", secs: 604800 },
];

/** Availability categories: label, description, config key, colour category. */
const AVAIL_CATEGORIES: {
  key: "show_prime" | "show_channel" | "show_rent_buy" | "show_other";
  cat: AccessCategory;
  label: string;
  description: string;
}[] = [
  {
    key: "show_prime",
    cat: "prime",
    label: "Available with Prime subscription",
    description: "Movies & TV shows included in the base Prime membership",
  },
  {
    key: "show_channel",
    cat: "channel",
    label: "Channel add-ons",
    description: "Titles requiring a Prime Video channel (e.g. Lionsgate+, Max)",
  },
  {
    key: "show_rent_buy",
    cat: "rent_buy",
    label: "Rent / Buy",
    description: "Titles available to rent or purchase individually",
  },
  {
    key: "show_other",
    cat: "other",
    label: "Unknown / Other",
    description: "Titles whose availability could not be resolved",
  },
];

interface SettingsDialogProps {
  config: AppConfig;
  onClose: () => void;
  onSaved: (cfg: AppConfig) => void;
}

export default function SettingsDialog({ config, onClose, onSaved }: SettingsDialogProps) {
  const [tvIp, setTvIp] = useState(config.tv_ip);
  const [profile, setProfile] = useState(config.profile);
  const [projectRoot, setProjectRoot] = useState(config.project_root);
  const [cacheTtl, setCacheTtl] = useState(config.cache_ttl_secs ?? 21600);
  const [showPrime, setShowPrime] = useState(config.show_prime ?? true);
  const [showChannel, setShowChannel] = useState(config.show_channel ?? false);
  const [showRentBuy, setShowRentBuy] = useState(config.show_rent_buy ?? false);
  const [showOther, setShowOther] = useState(config.show_other ?? true);
  const [detectVpnRegion, setDetectVpnRegion] = useState(config.detect_vpn_region ?? true);
  const [playbackTarget, setPlaybackTarget] = useState<PlaybackTarget>(
    config.default_playback_target ?? "tv",
  );

  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearingPrime, setClearingPrime] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clearMsg, setClearMsg] = useState<string | null>(null);

  const categoryValues: Record<
    "show_prime" | "show_channel" | "show_rent_buy" | "show_other",
    [boolean, (v: boolean) => void]
  > = {
    show_prime: [showPrime, setShowPrime],
    show_channel: [showChannel, setShowChannel],
    show_rent_buy: [showRentBuy, setShowRentBuy],
    show_other: [showOther, setShowOther],
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    const newCfg: AppConfig = {
      tv_ip: tvIp,
      profile,
      project_root: projectRoot,
      cache_ttl_secs: cacheTtl,
      show_prime: showPrime,
      show_channel: showChannel,
      show_rent_buy: showRentBuy,
      show_other: showOther,
      detect_vpn_region: detectVpnRegion,
      default_playback_target: playbackTarget,
    };
    try {
      await invoke("save_config", { cfg: newCfg });
      onSaved(newCfg);
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleClearPrimeLogin = async () => {
    setClearingPrime(true);
    setClearMsg(null);
    try {
      await invoke("clear_prime_login");
      setClearMsg("Prime login cleared. Sign in again in the player window on next play.");
    } catch (err) {
      setClearMsg(`Error: ${err}`);
    } finally {
      setClearingPrime(false);
    }
  };

  const handleClearCache = async () => {
    setClearing(true);
    setClearMsg(null);
    try {
      await invoke("clear_all_cache");
      setClearMsg("Cache cleared. Next load will re-download from Prime Video.");
    } catch (err) {
      setClearMsg(`Error: ${err}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-[#1A242F] rounded-2xl shadow-2xl w-full max-w-lg mx-4 max-h-[90vh] flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-700/60 shrink-0">
          <div className="flex items-center gap-2.5">
            <svg className="w-5 h-5 text-zinc-400" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
              />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <h2 className="text-white font-semibold text-base">Settings</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 text-zinc-400 hover:text-white hover:bg-zinc-700 rounded-lg transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-7">

          {/* ── TV Connection ───────────────────────────────────────────── */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-3">
              LG TV Connection
            </h3>
            <div className="space-y-3">
              <div>
                <label className="block text-sm text-zinc-300 mb-1.5">TV IP Address</label>
                <input
                  type="text"
                  value={tvIp}
                  onChange={(e) => setTvIp(e.target.value)}
                  className="w-full bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2.5 text-sm
                             text-white font-mono focus:outline-none focus:border-emerald-500 transition-colors"
                  placeholder="192.168.0.79"
                />
                <p className="text-xs text-zinc-500 mt-1.5">
                  Run <code className="text-zinc-400 bg-zinc-800 px-1 rounded">./lg-tv-probe</code> to find your TV's IP
                </p>
              </div>
              <div>
                <label className="block text-sm text-zinc-300 mb-1.5">Default Profile Index</label>
                <input
                  type="number"
                  min={0}
                  max={9}
                  value={profile}
                  onChange={(e) => setProfile(Math.max(0, parseInt(e.target.value) || 0))}
                  className="w-24 bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2.5 text-sm
                             text-white text-center focus:outline-none focus:border-emerald-500 transition-colors"
                />
                <p className="text-xs text-zinc-500 mt-1.5">
                  0 = first profile slot in the Prime Video picker
                </p>
              </div>
            </div>
          </section>

          {/* ── Availability categories ────────────────────────────────── */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-1">
              Show Titles By Availability
            </h3>
            <p className="text-xs text-zinc-600 mb-3">
              Tick to show; untick to hide from the catalog grid
            </p>
            <div className="space-y-2">
              {AVAIL_CATEGORIES.map(({ key, cat, label, description }) => {
                const [checked, setChecked] = categoryValues[key];
                const colorDot = CATEGORY_COLORS[cat];
                const textColor = CATEGORY_TEXT[cat];
                const borderColor = CATEGORY_BORDER[cat];

                return (
                  <label
                    key={key}
                    className={`flex items-start gap-3 p-3 rounded-xl cursor-pointer transition-colors
                      ${checked
                        ? "bg-zinc-800/80 border border-zinc-700/60"
                        : "bg-zinc-900/40 border border-zinc-800/40 opacity-60"
                      }`}
                  >
                    {/* Custom colour checkbox */}
                    <div className="relative mt-0.5 shrink-0">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => setChecked(e.target.checked)}
                        className="sr-only"
                      />
                      <div
                        className={`w-5 h-5 rounded-md border-2 flex items-center justify-center
                          transition-all ${borderColor}
                          ${checked ? `${colorDot} border-transparent` : "bg-transparent"}`}
                      >
                        {checked && (
                          <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={3} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                          </svg>
                        )}
                      </div>
                    </div>

                    {/* Label and description */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        {/* Colour swatch */}
                        <span className={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${colorDot}`} />
                        <span className={`text-sm font-medium ${checked ? textColor : "text-zinc-500"}`}>
                          {label}
                        </span>
                      </div>
                      <p className="text-xs text-zinc-600 mt-0.5 leading-relaxed">
                        {description}
                      </p>
                    </div>
                  </label>
                );
              })}
            </div>
          </section>

          {/* ── Cache ───────────────────────────────────────────────────── */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-3">
              Cache
            </h3>
            <div className="space-y-3">
              <div>
                <label className="block text-sm text-zinc-300 mb-1.5">Catalog Cache TTL</label>
                <select
                  value={cacheTtl}
                  onChange={(e) => setCacheTtl(Number(e.target.value))}
                  className="w-full bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2.5 text-sm
                             text-white focus:outline-none focus:border-emerald-500 transition-colors"
                >
                  {TTL_OPTIONS.map((o) => (
                    <option key={o.secs} value={o.secs}>{o.label}</option>
                  ))}
                </select>
                <p className="text-xs text-zinc-500 mt-1.5">
                  How long to keep catalog data before re-downloading from Prime Video.
                </p>
              </div>

              <div>
                <label className="block text-sm text-zinc-300 mb-1.5">Default playback</label>
                <select
                  value={playbackTarget}
                  onChange={(e) => setPlaybackTarget(e.target.value as PlaybackTarget)}
                  className="w-full bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2.5 text-sm
                             text-white focus:outline-none focus:border-emerald-500 transition-colors"
                >
                  <option value="tv">LG TV</option>
                  <option value="mac">Mac (in-app Prime Video)</option>
                </select>
                <p className="text-xs text-zinc-500 mt-1.5">
                  Mac playback opens Prime Video in a separate window. Sign in with your Amazon
                  account the first time you play.
                </p>
              </div>

              <label className="flex items-start gap-3 cursor-pointer group">
                <input
                  type="checkbox"
                  checked={detectVpnRegion}
                  onChange={(e) => setDetectVpnRegion(e.target.checked)}
                  className="mt-1 w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-emerald-500
                             focus:ring-emerald-500 focus:ring-offset-zinc-900"
                />
                <div>
                  <p className="text-sm text-zinc-300 group-hover:text-white transition-colors">
                    Detect VPN region changes
                  </p>
                  <p className="text-xs text-zinc-500 mt-0.5 leading-relaxed">
                    When enabled, the app detects your Prime Video region from your IP,
                    shows it in the header, and clears cached catalog data when the region
                    changes. Turn off to keep one shared cache regardless of VPN.
                  </p>
                </div>
              </label>

              <div className="flex items-center justify-between pt-1">
                <div>
                  <p className="text-sm text-zinc-300">Clear Prime login</p>
                  <p className="text-xs text-zinc-500">Sign out of Amazon in the in-app player window</p>
                </div>
                <button
                  onClick={handleClearPrimeLogin}
                  disabled={clearingPrime}
                  className="shrink-0 px-4 py-2 bg-zinc-700 hover:bg-red-800 text-zinc-300
                             hover:text-white text-xs rounded-xl transition-colors disabled:opacity-40"
                >
                  {clearingPrime ? "Clearing…" : "Clear login"}
                </button>
              </div>

              <div className="flex items-center justify-between pt-1">
                <div>
                  <p className="text-sm text-zinc-300">Clear All Cache</p>
                  <p className="text-xs text-zinc-500">Delete all locally stored catalog & search data</p>
                </div>
                <button
                  onClick={handleClearCache}
                  disabled={clearing}
                  className="shrink-0 px-4 py-2 bg-zinc-700 hover:bg-red-800 text-zinc-300
                             hover:text-white text-xs rounded-xl transition-colors disabled:opacity-40"
                >
                  {clearing ? "Clearing…" : "Clear Cache"}
                </button>
              </div>

              {clearMsg && (
                <p className={`text-xs ${clearMsg.startsWith("Error") ? "text-red-400" : "text-emerald-400"}`}>
                  {clearMsg}
                </p>
              )}
            </div>
          </section>

          {/* ── Project root ────────────────────────────────────────────── */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-3">
              Advanced
            </h3>
            <div>
              <label className="block text-sm text-zinc-300 mb-1.5">Project Root</label>
              <input
                type="text"
                value={projectRoot}
                onChange={(e) => setProjectRoot(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2.5 text-sm
                           text-white font-mono focus:outline-none focus:border-emerald-500 transition-colors"
                placeholder="/home/user/src/lgtv-fun"
              />
              <p className="text-xs text-zinc-500 mt-1.5">
                Directory containing{" "}
                <code className="text-zinc-400 bg-zinc-800 px-1 rounded">amazon/prime-catalog.py</code>.
                Leave blank to auto-detect.
              </p>
            </div>
          </section>

          {error && (
            <div className="bg-red-900/40 border border-red-700 rounded-xl px-4 py-3 text-red-300 text-sm">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-6 py-4 border-t border-zinc-700/60 shrink-0">
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex-1 bg-emerald-600 hover:bg-emerald-500 disabled:bg-zinc-600 text-white
                       font-semibold py-2.5 px-4 rounded-xl transition-colors text-sm"
          >
            {saving ? "Saving…" : "Save Settings"}
          </button>
          <button
            onClick={onClose}
            className="px-5 py-2.5 bg-zinc-700 hover:bg-zinc-600 text-white rounded-xl
                       transition-colors text-sm font-medium"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
