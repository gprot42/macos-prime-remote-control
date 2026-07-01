use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex as AsyncMutex;
use tokio::time::{timeout, Duration};
use url::Url;

/// Serialize LG TV network commands — the TV accepts one WebSocket client at a time.
fn tv_cmd_lock() -> &'static AsyncMutex<()> {
    static LOCK: OnceLock<AsyncMutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| AsyncMutex::new(()))
}

/// Upper bound for short TV commands (power, transport, seek, position, MAC).
/// A stalled command must never outlive this, or it would hold `tv_cmd_lock`
/// and silently block every later TV command (including playback).
const TV_CMD_TIMEOUT: Duration = Duration::from_secs(45);

/// Upper bound for a full play launch. The Python flow includes profile-picker
/// and title-page waits (~20-30s), so this is generous — but still bounded so a
/// hung launch can't wedge the shared lock forever.
const TV_PLAY_TIMEOUT: Duration = Duration::from_secs(180);

/// Run a `lg-tv-connect.py` subprocess to completion, capturing its output, but
/// never block forever: if it exceeds `limit` the child is killed and an error
/// is returned. This guarantees the shared TV command lock is always released
/// even when the TV is off, unreachable, or its WebSocket stalls mid-command.
async fn run_tv_command(
    mut command: tokio::process::Command,
    limit: Duration,
) -> Result<std::process::Output, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|e| format!("Failed to run lg-tv-connect.py: {e}"))?;

    match timeout(limit, child.wait()).await {
        Ok(status) => {
            let status = status.map_err(|e| format!("Process error: {e}"))?;
            // Output from these commands is a single small JSON/text line, well
            // under the OS pipe buffer, so reading after exit cannot deadlock.
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            if let Some(mut out) = child.stdout.take() {
                let _ = out.read_to_end(&mut stdout).await;
            }
            if let Some(mut err) = child.stderr.take() {
                let _ = err.read_to_end(&mut stderr).await;
            }
            Ok(std::process::Output {
                status,
                stdout,
                stderr,
            })
        }
        Err(_) => {
            // Kill the stalled child so the lock is freed for the next command.
            let _ = child.kill().await;
            Err(format!(
                "TV command timed out after {}s — the TV may be off or unreachable.",
                limit.as_secs()
            ))
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppConfig {
    pub tv_ip: String,
    pub profile: i32,
    pub project_root: String,
    /// Cache TTL in seconds (default 6 hours).
    #[serde(default = "default_ttl")]
    pub cache_ttl_secs: u64,
    /// Show titles that are included with a Prime subscription.
    #[serde(default = "default_true")]
    pub show_prime: bool,
    /// Show titles available via a channel add-on (e.g. Lionsgate+, Max).
    #[serde(default = "default_false")]
    pub show_channel: bool,
    /// Show titles that require renting or buying.
    #[serde(default = "default_false")]
    pub show_rent_buy: bool,
    /// Show titles with unknown / unresolved availability.
    #[serde(default = "default_true")]
    pub show_other: bool,
    /// Detect Prime Video region from IP and clear cache when VPN region changes.
    #[serde(default = "default_true")]
    pub detect_vpn_region: bool,
    /// Default playback target: "tv" or "mac".
    #[serde(default = "default_playback_tv")]
    pub default_playback_target: String,
    /// Optional TV MAC for Wake-on-LAN when powering on from deep standby.
    #[serde(default)]
    pub tv_mac: String,
    /// Default TV volume (0–100) applied when starting playback.
    #[serde(default = "default_tv_volume")]
    pub default_tv_volume: i32,
    /// When true, set default_tv_volume after play and when powering on the TV.
    #[serde(default = "default_true")]
    pub apply_default_tv_volume: bool,
}

fn default_tv_volume() -> i32 {
    13
}

fn default_playback_tv() -> String {
    "tv".to_string()
}

fn default_ttl() -> u64 {
    6 * 3600
}

fn default_true() -> bool {
    true
}

fn default_false() -> bool {
    false
}

impl Default for AppConfig {
    fn default() -> Self {
        AppConfig {
            tv_ip: "192.168.0.79".to_string(),
            profile: 0,
            project_root: default_project_root().to_string_lossy().to_string(),
            cache_ttl_secs: default_ttl(),
            show_prime: true,
            show_channel: false,
            show_rent_buy: false,
            show_other: true,
            detect_vpn_region: true,
            default_playback_target: default_playback_tv(),
            tv_mac: String::new(),
            default_tv_volume: default_tv_volume(),
            apply_default_tv_volume: true,
        }
    }
}

const PRIME_PLAYER_LABEL: &str = "prime-player";

fn validate_prime_video_url(url: &str) -> Result<Url, String> {
    let parsed = Url::parse(url.trim()).map_err(|e| format!("Invalid URL: {e}"))?;
    let host = parsed.host_str().unwrap_or("");
    if parsed.scheme() != "https" || host != "www.primevideo.com" {
        return Err("Only https://www.primevideo.com URLs are allowed".to_string());
    }
    Ok(parsed)
}

async fn resolve_prime_play_url(
    cfg: &AppConfig,
    content_id: &str,
    episode: Option<i32>,
) -> Result<(String, String), String> {
    let root = resolve_project_root(cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("prime-catalog.py")
        .to_string_lossy()
        .to_string();

    let mut command = tokio::process::Command::new(&python);
    command
        .arg(&script)
        .arg("--play-url")
        .arg(content_id)
        .arg("--json");
    if let Some(ep) = episode {
        if ep >= 1 {
            command.arg("--episode").arg(ep.to_string());
        }
    }

    let output = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run prime-catalog.py: {e}"))?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(format!("prime-catalog.py --play-url failed:\n{err}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let value: serde_json::Value =
        serde_json::from_str(&stdout).map_err(|e| format!("Invalid play URL JSON: {e}"))?;
    let url = value
        .get("url")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "play URL response missing url".to_string())?
        .to_string();
    let title = value
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    Ok((url, title))
}

fn open_prime_player_window(app: &tauri::AppHandle, url: Url, title: &str) -> Result<(), String> {
    let window_title = if title.trim().is_empty() {
        "Prime Video".to_string()
    } else {
        format!("{title} — Prime Video")
    };

    if let Some(window) = app.get_webview_window(PRIME_PLAYER_LABEL) {
        window
            .navigate(url)
            .map_err(|e| format!("Failed to navigate player: {e}"))?;
        let _ = window.set_title(&window_title);
        window
            .show()
            .map_err(|e| format!("Failed to show player: {e}"))?;
        window
            .set_focus()
            .map_err(|e| format!("Failed to focus player: {e}"))?;
        return Ok(());
    }

    WebviewWindowBuilder::new(app, PRIME_PLAYER_LABEL, WebviewUrl::External(url))
        .title(window_title)
        .inner_size(1280.0, 720.0)
        .min_inner_size(640.0, 360.0)
        .build()
        .map_err(|e| format!("Failed to open Prime player window: {e}"))?;
    Ok(())
}

fn config_path() -> PathBuf {
    home_dir().join(".config").join("prime-remote-control.json")
}

fn load_config() -> AppConfig {
    let path = config_path();
    if path.exists() {
        if let Ok(data) = std::fs::read_to_string(&path) {
            if let Ok(raw) = serde_json::from_str::<serde_json::Value>(&data) {
                if let Ok(cfg) = serde_json::from_value::<AppConfig>(raw.clone()) {
                    let needs_save = raw.get("default_tv_volume").is_none()
                        || raw.get("apply_default_tv_volume").is_none();
                    if needs_save {
                        let _ = save_config_to_disk(&cfg);
                    }
                    return cfg;
                }
            }
        }
    }
    let cfg = AppConfig::default();
    let _ = save_config_to_disk(&cfg);
    cfg
}

fn save_config_to_disk(cfg: &AppConfig) -> Result<(), String> {
    let path = config_path();
    ensure_dir(&path)?;
    let data = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    std::fs::write(&path, data).map_err(|e| e.to_string())
}

// ─────────────────────────────────────────────────────────────────────────────
// Bookmarks
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Bookmark {
    pub content_id: String,
    pub added_at: u64,
    pub item: serde_json::Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_item: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub episode_content_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub play_episode: Option<i32>,
}

fn bookmarks_path() -> PathBuf {
    home_dir()
        .join(".config")
        .join("prime-remote-control-bookmarks.json")
}

fn load_bookmarks() -> Vec<Bookmark> {
    let path = bookmarks_path();
    if path.exists() {
        if let Ok(data) = std::fs::read_to_string(&path) {
            if let Ok(list) = serde_json::from_str::<Vec<Bookmark>>(&data) {
                return list;
            }
        }
    }
    Vec::new()
}

fn save_bookmarks_to_disk(bookmarks: &[Bookmark]) -> Result<(), String> {
    let path = bookmarks_path();
    ensure_dir(&path)?;
    let data = serde_json::to_string_pretty(bookmarks).map_err(|e| e.to_string())?;
    std::fs::write(&path, data).map_err(|e| e.to_string())
}

fn bookmark_content_id(item: &serde_json::Value) -> Result<String, String> {
    item.get("content_id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| "Missing content_id".to_string())
}

// ─────────────────────────────────────────────────────────────────────────────
// Filesystem helpers
// ─────────────────────────────────────────────────────────────────────────────

fn home_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string()))
}

fn ensure_dir(path: &PathBuf) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

// ─────────────────────────────────────────────────────────────────────────────
// Image cache helpers
// ─────────────────────────────────────────────────────────────────────────────

fn image_cache_dir() -> PathBuf {
    cache_dir().join("images")
}

/// Sanitise a content_id so it is safe to use as a filename (used by cache-images.py side).
fn safe_filename(content_id: &str) -> String {
    content_id
        .chars()
        .map(|c| if c.is_alphanumeric() || c == '-' { c } else { '_' })
        .collect()
}

// ─────────────────────────────────────────────────────────────────────────────
// Local HTTP image server
// ─────────────────────────────────────────────────────────────────────────────

/// Shared application state holding the image-server port once started.
pub struct ImageServerPort(pub Arc<Mutex<u16>>);

/// Spawn a bare-bones HTTP server on a random localhost port that serves
/// cached JPEG images from `image_cache_dir()`.  Returns the bound port.
async fn start_image_server() -> u16 {
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("image server: bind failed");
    let port = listener.local_addr().unwrap().port();

    tokio::spawn(async move {
        loop {
            if let Ok((conn, _)) = listener.accept().await {
                tokio::spawn(handle_image_conn(conn));
            }
        }
    });

    port
}

async fn handle_image_conn(mut conn: tokio::net::TcpStream) {
    let mut buf = vec![0u8; 2048];
    let n = match conn.read(&mut buf).await {
        Ok(n) if n > 0 => n,
        _ => return,
    };

    // Parse: "GET /0QYW7PZS87HQG6LRN4XMD1AJIH.jpg HTTP/1.1"
    let req = std::str::from_utf8(&buf[..n]).unwrap_or("");
    let filename = req
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .unwrap_or("/")
        .trim_start_matches('/')
        .split('?')
        .next()
        .unwrap_or("")
        .to_string();

    // Only serve bare filenames from the cache directory (no path segments).
    let safe_name = !filename.is_empty()
        && !filename.contains('/')
        && !filename.contains('\\')
        && !filename.contains("..")
        && filename
            .chars()
            .all(|c| c.is_alphanumeric() || c == '-' || c == '_' || c == '.');

    let cache_dir = image_cache_dir();
    let file_path = cache_dir.join(&filename);

    if safe_name {
        if let Ok(data) = tokio::fs::read(&file_path).await {
            let header = format!(
                "HTTP/1.1 200 OK\r\n\
                 Content-Type: image/jpeg\r\n\
                 Content-Length: {}\r\n\
                 Cache-Control: max-age=86400\r\n\
                 Access-Control-Allow-Origin: *\r\n\r\n",
                data.len()
            );
            let _ = conn.write_all(header.as_bytes()).await;
            let _ = conn.write_all(&data).await;
            return;
        }
    }

    let _ = conn
        .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        .await;
}

// ─────────────────────────────────────────────────────────────────────────────
// Catalog cache
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Serialize, Deserialize)]
struct CacheEntry {
    /// Unix timestamp of when the data was fetched.
    timestamp: u64,
    /// Raw JSON array string stored as a parsed value (avoids double-encoding).
    data: serde_json::Value,
}

fn cache_dir() -> PathBuf {
    home_dir().join(".cache").join("prime-catalog-ui")
}

/// Sanitise a cache key to a safe filename.
fn cache_path(key: &str) -> PathBuf {
    let safe: String = key
        .chars()
        .map(|c| if c.is_alphanumeric() || c == '-' { c } else { '_' })
        .collect();
    cache_dir().join(format!("{safe}.json"))
}

/// Return cached JSON string if it exists and is younger than `max_age_secs`.
fn read_cache(key: &str, max_age_secs: u64) -> Option<String> {
    let path = cache_path(key);
    let raw = std::fs::read_to_string(&path).ok()?;
    let entry: CacheEntry = serde_json::from_str(&raw).ok()?;
    let age = now_secs().saturating_sub(entry.timestamp);
    if age > max_age_secs {
        return None;
    }
    serde_json::to_string(&entry.data).ok()
}

/// Write a JSON string to the cache.
fn write_cache(key: &str, json_str: &str) -> Result<(), String> {
    let path = cache_path(key);
    ensure_dir(&path)?;
    let data: serde_json::Value =
        serde_json::from_str(json_str).map_err(|e| e.to_string())?;
    let entry = CacheEntry {
        timestamp: now_secs(),
        data,
    };
    let serialized = serde_json::to_string(&entry).map_err(|e| e.to_string())?;
    std::fs::write(&path, serialized).map_err(|e| e.to_string())
}

/// Return seconds since the cache entry was written, or None if no cache.
fn cache_age_secs(key: &str) -> Option<u64> {
    let path = cache_path(key);
    let raw = std::fs::read_to_string(&path).ok()?;
    let entry: CacheEntry = serde_json::from_str(&raw).ok()?;
    Some(now_secs().saturating_sub(entry.timestamp))
}

fn delete_cache_entry(key: &str) {
    let path = cache_path(key);
    let _ = std::fs::remove_file(path);
}

fn region_state_path() -> PathBuf {
    cache_dir().join("region.txt")
}

fn read_stored_region() -> Option<String> {
    let raw = std::fs::read_to_string(region_state_path()).ok()?;
    let region = raw.trim().to_string();
    if region.is_empty() {
        None
    } else {
        Some(region)
    }
}

fn write_stored_region(region: &str) -> Result<(), String> {
    ensure_dir(&region_state_path())?;
    std::fs::write(region_state_path(), region).map_err(|e| e.to_string())
}

/// Scrape Prime Video's detected storefront country from a lightweight page.
fn detect_prime_region() -> Option<String> {
    const MARKER: &str = "\"countryCode\":\"";
    let response = ureq::get("https://www.primevideo.com/categories")
        .set(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        .set("Accept-Language", "en-GB,en;q=0.9")
        .call()
        .ok()?;
    let html = response.into_string().ok()?;
    let start = html.find(MARKER)? + MARKER.len();
    let rest = &html[start..];
    let end = rest.find('"')?;
    let region = rest[..end].trim().to_string();
    if region.is_empty() {
        None
    } else {
        Some(region)
    }
}

fn clear_catalog_cache_files() {
    let dir = cache_dir();
    if !dir.exists() {
        return;
    }
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) == Some("json") {
                let _ = std::fs::remove_file(path);
            }
        }
    }
}

/// Detect Prime storefront region and wipe catalog/search cache when it changes
/// (e.g. user switched VPN from Sweden to UK).
fn sync_region_cache() -> String {
    let detected = detect_prime_region();
    let current = detected.clone().unwrap_or_else(|| "unknown".to_string());
    let stored = read_stored_region();

    if stored.as_deref() != Some(current.as_str()) {
        clear_catalog_cache_files();
        let _ = write_stored_region(&current);
    }

    current
}

fn catalog_cache_key(cfg: &AppConfig, collection: &str) -> String {
    if cfg.detect_vpn_region {
        let region = sync_region_cache();
        format!("collection_{region}_{collection}")
    } else {
        format!("collection_{collection}")
    }
}

fn search_cache_key(cfg: &AppConfig, query: &str) -> String {
    if cfg.detect_vpn_region {
        let region = sync_region_cache();
        format!("search_{region}_{}", query.trim().to_lowercase())
    } else {
        format!("search_{}", query.trim().to_lowercase())
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Project root + Python discovery
// ─────────────────────────────────────────────────────────────────────────────

fn default_project_root() -> PathBuf {
    if let Ok(root) = std::env::var("LGTV_FUN_DIR") {
        let p = PathBuf::from(root);
        if p.join("amazon").join("prime-catalog.py").exists() {
            return p;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut p = exe.clone();
        for _ in 0..6 {
            p = match p.parent() {
                Some(parent) => parent.to_path_buf(),
                None => break,
            };
            if p.join("amazon").join("prime-catalog.py").exists() {
                return p;
            }
        }
    }
    home_dir().join("src").join("prime-remote-control")
}

fn resolve_project_root(cfg: &AppConfig) -> PathBuf {
    let p = PathBuf::from(&cfg.project_root);
    if p.join("amazon").join("prime-catalog.py").exists() {
        return p;
    }
    default_project_root()
}

fn python_exe(root: &PathBuf) -> String {
    let venv = root.join(".venv").join("bin").join("python3");
    if venv.exists() {
        venv.to_string_lossy().to_string()
    } else {
        "python3".to_string()
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — external links
// ─────────────────────────────────────────────────────────────────────────────

fn open_themoviedb_url(url: &str) -> Result<(), String> {
    let url = url.trim();
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err("Only http(s) URLs are allowed".to_string());
    }
    if !url.contains("themoviedb.org") {
        return Err("Only themoviedb.org links are allowed".to_string());
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(url)
            .status()
            .map_err(|e| format!("Failed to open URL: {e}"))?;
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        Err("Opening URLs is only supported on macOS".to_string())
    }
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    open_themoviedb_url(&url)
}

fn tmdb_search_id(query: &str, kind: &str) -> Result<Option<u32>, String> {
    let path = if kind == "tv" { "search/tv" } else { "search/movie" };
    let url = format!(
        "https://www.themoviedb.org/{path}?query={}&language=en-US",
        urlencoding::encode(query)
    );
    let response = ureq::get(&url)
        .set("User-Agent", "PrimeRemoteControl/0.1")
        .call()
        .map_err(|e| format!("TMDB search failed: {e}"))?;
    let html = response
        .into_string()
        .map_err(|e| format!("TMDB search read failed: {e}"))?;
    let needle = if kind == "tv" { "/tv/" } else { "/movie/" };
    let mut search_from = 0usize;
    while let Some(rel) = html[search_from..].find(needle) {
        let start = search_from + rel + needle.len();
        let id: String = html[start..]
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .collect();
        if (1..=8).contains(&id.len()) {
            if let Ok(parsed) = id.parse::<u32>() {
                if parsed > 0 {
                    return Ok(Some(parsed));
                }
            }
        }
        search_from = start;
    }
    Ok(None)
}

#[tauri::command]
fn open_tmdb_trailer(query: String, media_kind: String) -> Result<(), String> {
    let query = query.trim();
    if query.is_empty() {
        return Err("Empty TMDB search query".to_string());
    }

    let kinds: Vec<&str> = match media_kind.as_str() {
        "movie" => vec!["movie"],
        "tv" => vec!["tv"],
        _ => vec!["movie", "tv"],
    };

    for kind in kinds {
        if let Some(id) = tmdb_search_id(query, kind)? {
            let url = format!("https://www.themoviedb.org/{kind}/{id}/videos");
            return open_themoviedb_url(&url);
        }
    }

    let fallback_path = match media_kind.as_str() {
        "tv" => "search/tv",
        "movie" => "search/movie",
        _ => "search",
    };
    let fallback = format!(
        "https://www.themoviedb.org/{fallback_path}?query={}&language=en-US",
        urlencoding::encode(query)
    );
    open_themoviedb_url(&fallback)
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — config
// ─────────────────────────────────────────────────────────────────────────────

#[tauri::command]
async fn get_config() -> Result<AppConfig, String> {
    Ok(load_config())
}

#[tauri::command]
async fn save_config(cfg: AppConfig) -> Result<(), String> {
    save_config_to_disk(&cfg)
}

/// Look up the TV MAC via ARP and persist it when found.
async fn autofill_tv_mac(cfg: &mut AppConfig) -> Result<bool, String> {
    let ip = cfg.tv_ip.trim();
    if ip.is_empty() {
        return Ok(false);
    }
    let Some(mac) = run_get_mac_cmd(ip).await? else {
        return Ok(false);
    };
    let current = cfg.tv_mac.trim();
    if !current.is_empty() && current.eq_ignore_ascii_case(&mac) {
        return Ok(false);
    }
    cfg.tv_mac = mac;
    save_config_to_disk(cfg)?;
    Ok(true)
}

async fn run_get_mac_cmd(ip: &str) -> Result<Option<String>, String> {
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script).arg(ip).arg("--get-mac");
    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(format!("TV MAC lookup failed: {err}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let json_line = stdout
        .lines()
        .filter(|l| l.trim_start().starts_with('{'))
        .last()
        .unwrap_or("");
    if json_line.is_empty() {
        return Ok(None);
    }
    let val: serde_json::Value =
        serde_json::from_str(json_line).map_err(|e| e.to_string())?;
    Ok(val["mac"].as_str().map(|s| s.to_string()))
}

/// Discover the TV MAC from ARP (TV should be on) and save it to config.
#[tauri::command]
async fn discover_tv_mac(app: tauri::AppHandle) -> Result<AppConfig, String> {
    let mut cfg = load_config();
    if autofill_tv_mac(&mut cfg).await? {
        let _ = app.emit("config-updated", cfg.clone());
    }
    Ok(cfg)
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — bookmarks
// ─────────────────────────────────────────────────────────────────────────────

#[tauri::command]
async fn get_bookmarks() -> Result<Vec<Bookmark>, String> {
    Ok(load_bookmarks())
}

#[tauri::command]
async fn add_bookmark(item: serde_json::Value) -> Result<(), String> {
    let content_id = bookmark_content_id(&item)?;
    let mut bookmarks = load_bookmarks();
    if bookmarks.iter().any(|b| b.content_id == content_id) {
        return Ok(());
    }
    bookmarks.insert(
        0,
        Bookmark {
            content_id,
            added_at: now_secs(),
            item,
            source_item: None,
            episode_content_id: None,
            play_episode: None,
        },
    );
    save_bookmarks_to_disk(&bookmarks)
}

#[tauri::command]
async fn remove_bookmark(content_id: String) -> Result<(), String> {
    let mut bookmarks = load_bookmarks();
    bookmarks.retain(|b| b.content_id != content_id);
    save_bookmarks_to_disk(&bookmarks)
}

#[tauri::command]
async fn toggle_bookmark(
    item: serde_json::Value,
    source_item: Option<serde_json::Value>,
    episode_content_id: Option<String>,
    play_episode: Option<i32>,
) -> Result<bool, String> {
    let content_id = bookmark_content_id(&item)?;
    let mut bookmarks = load_bookmarks();
    if let Some(pos) = bookmarks.iter().position(|b| b.content_id == content_id) {
        bookmarks.remove(pos);
        save_bookmarks_to_disk(&bookmarks)?;
        Ok(false)
    } else {
        bookmarks.insert(
            0,
            Bookmark {
                content_id,
                added_at: now_secs(),
                item,
                source_item,
                episode_content_id,
                play_episode,
            },
        );
        save_bookmarks_to_disk(&bookmarks)?;
        Ok(true)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — cache info
// ─────────────────────────────────────────────────────────────────────────────

/// Returns seconds since the collection cache was last written (None = no cache).
#[tauri::command]
async fn collection_cache_age(collection: String) -> Option<u64> {
    let cfg = load_config();
    let key = catalog_cache_key(&cfg, &collection);
    cache_age_secs(&key)
}

/// Returns seconds since the search cache was last written (None = no cache).
#[tauri::command]
async fn search_cache_age(query: String) -> Option<u64> {
    let cfg = load_config();
    let key = search_cache_key(&cfg, &query);
    cache_age_secs(&key)
}

/// Delete all cache files.
#[tauri::command]
async fn clear_all_cache() -> Result<(), String> {
    let dir = cache_dir();
    if dir.exists() {
        std::fs::remove_dir_all(&dir).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Return the Prime Video storefront region detected for the current IP (e.g. UK, SE).
#[tauri::command]
fn get_prime_region() -> String {
    let cfg = load_config();
    if !cfg.detect_vpn_region {
        return "unknown".to_string();
    }
    detect_prime_region()
        .or_else(read_stored_region)
        .unwrap_or_else(|| "unknown".to_string())
}

fn public_ip_state_path() -> PathBuf {
    cache_dir().join("public_ip.txt")
}

fn read_stored_public_ip() -> Option<(String, String)> {
    let raw = std::fs::read_to_string(public_ip_state_path()).ok()?;
    let mut parts = raw.trim().splitn(2, '|');
    let ip = parts.next()?.to_string();
    let country = parts.next().unwrap_or("").to_string();
    if ip.is_empty() {
        None
    } else {
        Some((ip, country))
    }
}

fn write_stored_public_ip(ip: &str, country: &str) -> Result<(), String> {
    ensure_dir(&public_ip_state_path())?;
    std::fs::write(public_ip_state_path(), format!("{ip}|{country}")).map_err(|e| e.to_string())
}

/// Detect the outgoing (public) IP address and its country, as seen by an external
/// service — i.e. what Prime Video / Amazon sees, which reflects the active VPN exit.
fn detect_public_ip() -> Option<(String, String)> {
    let response = ureq::get("https://ipinfo.io/json")
        .set(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        .timeout(std::time::Duration::from_secs(5))
        .call()
        .ok()?;
    let body = response.into_string().ok()?;
    let json: serde_json::Value = serde_json::from_str(&body).ok()?;
    let ip = json.get("ip")?.as_str()?.to_string();
    let country = json
        .get("country")
        .and_then(|c| c.as_str())
        .unwrap_or("")
        .to_string();
    if ip.is_empty() {
        None
    } else {
        Some((ip, country))
    }
}

/// Return the outgoing public IP address and country (e.g. the VPN exit), so the
/// user can confirm which network/location Prime Video sees them from.
#[tauri::command]
fn get_public_ip() -> serde_json::Value {
    let cfg = load_config();
    if !cfg.detect_vpn_region {
        return serde_json::json!({ "ip": null, "country": null });
    }
    match detect_public_ip() {
        Some((ip, country)) => {
            let _ = write_stored_public_ip(&ip, &country);
            serde_json::json!({ "ip": ip, "country": country })
        }
        None => match read_stored_public_ip() {
            Some((ip, country)) => serde_json::json!({ "ip": ip, "country": country }),
            None => serde_json::json!({ "ip": null, "country": null }),
        },
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — catalog
// ─────────────────────────────────────────────────────────────────────────────

/// Load a Prime Video collection. Serves from disk cache unless `force_refresh`
/// is true or the cache is older than the configured TTL.
#[tauri::command]
async fn load_catalog(collection: String, force_refresh: bool) -> Result<String, String> {
    let cfg = load_config();
    let cache_key = catalog_cache_key(&cfg, &collection);

    // Try cache first
    if !force_refresh {
        if let Some(cached) = read_cache(&cache_key, cfg.cache_ttl_secs) {
            return Ok(cached);
        }
    }

    // Fetch fresh data
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("prime-catalog.py")
        .to_string_lossy()
        .to_string();

    let output = tokio::process::Command::new(&python)
        .arg(&script)
        .arg("--collection")
        .arg(&collection)
        .arg("--resolve-entitlement")
        .arg("--json")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run prime-catalog.py: {e}"))?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        // On failure, try stale cache as fallback
        if let Some(stale) = read_cache(&cache_key, 30 * 24 * 3600) {
            return Ok(format!(
                "__STALE__{}",
                stale
            ));
        }
        return Err(format!("prime-catalog.py failed:\n{err}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();

    // Cache the result (delete stale before writing)
    delete_cache_entry(&cache_key);
    let _ = write_cache(&cache_key, &stdout);

    Ok(stdout)
}

/// Search the Prime catalog with caching (1-hour TTL for search results).
#[tauri::command]
async fn search_catalog(query: String, force_refresh: bool) -> Result<String, String> {
    let cfg = load_config();
    let cache_key = search_cache_key(&cfg, &query);
    let search_ttl = 3600u64; // 1 hour

    if !force_refresh {
        if let Some(cached) = read_cache(&cache_key, search_ttl) {
            return Ok(cached);
        }
    }

    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("prime-catalog.py")
        .to_string_lossy()
        .to_string();

    let output = tokio::process::Command::new(&python)
        .arg(&script)
        .arg("--search")
        .arg(query.trim())
        .arg("--resolve-entitlement")
        .arg("--json")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run prime-catalog.py: {e}"))?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        if let Some(stale) = read_cache(&cache_key, 7 * 24 * 3600) {
            return Ok(format!("__STALE__{stale}"));
        }
        return Err(format!("prime-catalog.py search failed:\n{err}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    delete_cache_entry(&cache_key);
    let _ = write_cache(&cache_key, &stdout);

    Ok(stdout)
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — image caching
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct ImageItem {
    content_id: String,
    url: String,
}

/// Start background image downloads via cache-images.py.
/// Emits "image-cached" events: { content_id: String, path: String }
#[tauri::command]
async fn prefetch_images(app: tauri::AppHandle, items: Vec<ImageItem>) -> Result<(), String> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    if items.is_empty() {
        return Ok(());
    }

    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("cache-images.py")
        .to_string_lossy()
        .to_string();

    // Serialise items for stdin
    let input =
        serde_json::to_string(&serde_json::json!(items
            .iter()
            .map(|i| serde_json::json!({"content_id": i.content_id, "url": i.url}))
            .collect::<Vec<_>>()))
        .map_err(|e| e.to_string())?;

    let mut child = tokio::process::Command::new(&python)
        .arg(&script)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("Failed to start cache-images.py: {e}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(input.as_bytes()).await;
    }

    // Parse output lines and emit events
    let stdout = child.stdout.take().unwrap();
    let mut lines = BufReader::new(stdout).lines();

    while let Ok(Some(line)) = lines.next_line().await {
        if let Some(rest) = line.strip_prefix("CACHED\t") {
            let mut parts = rest.splitn(2, '\t');
            // Emit just the content_id — frontend constructs URL via image server port
            if let Some(content_id) = parts.next() {
                let stem = safe_filename(content_id);
                let _ = app.emit("image-cached", stem);
            }
        }
    }

    let _ = child.wait().await;
    Ok(())
}

/// Return the list of content_id stems that are already cached on disk.
/// The frontend constructs the HTTP URL using the image-server port.
#[tauri::command]
async fn list_cached_images() -> Result<Vec<String>, String> {
    let dir = image_cache_dir();
    if !dir.exists() {
        return Ok(vec![]);
    }
    let mut ids = Vec::new();
    let mut read_dir = tokio::fs::read_dir(&dir)
        .await
        .map_err(|e| e.to_string())?;
    while let Ok(Some(entry)) = read_dir.next_entry().await {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("jpg") {
            if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                ids.push(stem.to_string());
            }
        }
    }
    Ok(ids)
}

/// Return the port the local image HTTP server is listening on.
#[tauri::command]
fn get_image_server_port(state: tauri::State<ImageServerPort>) -> u16 {
    *state.0.lock().unwrap()
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — media control + volume
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct VolumeState {
    volume: Option<i64>,
    muted: bool,
}

/// Run a volume-related lg-tv-connect.py command and return JSON output.
/// When `caller_holds_lock` is true the caller already holds `tv_cmd_lock` — do not
/// acquire again (tokio::Mutex is not recursive).
async fn run_tv_volume_cmd_with_cfg(
    cfg: &AppConfig,
    args: &[&str],
    caller_holds_lock: bool,
) -> Result<VolumeState, String> {
    let _guard = if caller_holds_lock {
        None
    } else {
        Some(tv_cmd_lock().lock().await)
    };
    let root = resolve_project_root(cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script).arg(&cfg.tv_ip);
    for a in args {
        cmd.arg(a);
    }

    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(format!("TV command failed: {err}"));
    }

    // The last non-empty stdout line should be JSON {volume, muted}
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let json_line = stdout
        .lines()
        .filter(|l| l.trim_start().starts_with('{'))
        .last()
        .unwrap_or("");

    if json_line.is_empty() {
        return Err("No JSON response from TV".to_string());
    }

    let val: serde_json::Value =
        serde_json::from_str(json_line).map_err(|e| e.to_string())?;
    Ok(VolumeState {
        volume: val["volume"].as_i64(),
        muted: val["muted"].as_bool().unwrap_or(false),
    })
}

async fn run_tv_volume_cmd(args: &[&str]) -> Result<VolumeState, String> {
    let cfg = load_config();
    run_tv_volume_cmd_with_cfg(&cfg, args, false).await
}

/// Set the configured default TV volume when the feature is enabled.
/// Returns the resulting volume state (the value we set), or `None` when the
/// feature is disabled, so callers can surface it to the UI without an extra
/// (racy) read against a just-woken TV.
async fn apply_configured_tv_volume(
    cfg: &AppConfig,
    caller_holds_lock: bool,
) -> Result<Option<VolumeState>, String> {
    if !cfg.apply_default_tv_volume {
        return Ok(None);
    }
    let level = cfg.default_tv_volume.clamp(0, 100);
    let level_str = level.to_string();
    let state =
        run_tv_volume_cmd_with_cfg(cfg, &["--volume-set", &level_str], caller_holds_lock).await?;
    Ok(Some(state))
}

/// Get current volume level and mute state.
#[tauri::command]
async fn get_tv_volume(app: tauri::AppHandle) -> Result<VolumeState, String> {
    let result = run_tv_volume_cmd(&["--volume-get"]).await;
    if result.is_ok() {
        let mut cfg = load_config();
        if autofill_tv_mac(&mut cfg).await.unwrap_or(false) {
            let _ = app.emit("config-updated", cfg);
        }
    }
    result
}

/// Set absolute volume level (0–100).
#[tauri::command]
async fn set_tv_volume(level: i32) -> Result<VolumeState, String> {
    run_tv_volume_cmd(&["--volume-set", &level.to_string()]).await
}

/// Step volume up or down by `steps` increments.
#[tauri::command]
async fn volume_step(direction: String, steps: i32) -> Result<VolumeState, String> {
    let flag = match direction.as_str() {
        "up"   => "--volume-up",
        "down" => "--volume-down",
        other  => return Err(format!("Unknown direction: {other}")),
    };
    run_tv_volume_cmd(&[flag, &steps.to_string()]).await
}

/// Mute or unmute the TV.
#[tauri::command]
async fn set_tv_mute(muted: bool) -> Result<VolumeState, String> {
    let flag = if muted { "--mute" } else { "--unmute" };
    run_tv_volume_cmd(&[flag]).await
}

#[derive(Serialize)]
struct TvPowerState {
    on: bool,
}

/// Query whether the TV is powered on (requires network connection).
#[tauri::command]
async fn get_tv_power() -> Result<TvPowerState, String> {
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script).arg(&cfg.tv_ip).arg("--power-state");
    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    if !output.status.success() {
        return Ok(TvPowerState { on: false });
    }

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let json_line = stdout
        .lines()
        .filter(|l| l.trim_start().starts_with('{'))
        .last()
        .unwrap_or("");
    if json_line.is_empty() {
        return Ok(TvPowerState { on: false });
    }
    let val: serde_json::Value =
        serde_json::from_str(json_line).map_err(|e| e.to_string())?;
    Ok(TvPowerState {
        on: val["on"].as_bool().unwrap_or(false),
    })
}

/// Power the TV on or off ("on" | "off").
///
/// On a successful power-on, returns the default volume that was applied (when
/// the feature is enabled) so the UI can display it directly. Reading the volume
/// back separately races a just-woken TV, which can briefly report its previous
/// level and leave the controller showing a stale value.
#[tauri::command]
async fn tv_power(app: tauri::AppHandle, action: String) -> Result<Option<VolumeState>, String> {
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let flag = match action.as_str() {
        "off" => "--power-off",
        "on" => "--power-on",
        other => return Err(format!("Unknown power action: {other}")),
    };

    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script).arg(&cfg.tv_ip).arg(flag);
    if action == "on" && !cfg.tv_mac.trim().is_empty() {
        cmd.arg("--tv-mac").arg(cfg.tv_mac.trim());
    }

    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        let out = String::from_utf8_lossy(&output.stdout).to_string();
        let detail = if err.trim().is_empty() { out } else { err };
        return Err(format!("TV power command failed: {detail}"));
    }

    let mut cfg = load_config();
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if let Some(json_line) = stdout.lines().filter(|l| l.trim_start().starts_with('{')).last() {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(json_line) {
            if let Some(mac) = val["mac"].as_str() {
                if !mac.is_empty() && cfg.tv_mac.trim().is_empty() {
                    cfg.tv_mac = mac.to_string();
                    let _ = save_config_to_disk(&cfg);
                    let _ = app.emit("config-updated", cfg.clone());
                }
            }
        }
    }
    if autofill_tv_mac(&mut cfg).await.unwrap_or(false) {
        let _ = app.emit("config-updated", cfg.clone());
    }

    if action == "on" {
        // `tv_power` already holds `tv_cmd_lock` (acquired above), so the volume
        // command must NOT re-acquire it — the tokio Mutex is not reentrant and
        // doing so self-deadlocks, hanging tv_power and holding the lock forever
        // (which then blocks all later volume / play / transport commands).
        let applied = apply_configured_tv_volume(&cfg, true).await.ok().flatten();
        return Ok(applied);
    }

    Ok(None)
}

/// Run a short media-control lg-tv-connect.py command.
async fn run_tv_media_cmd(flag: &str) -> Result<(), String> {
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let mut cmd = tokio::process::Command::new(&python);
    cmd.current_dir(&root).arg(&script).arg(&cfg.tv_ip).arg(flag);
    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        let out = String::from_utf8_lossy(&output.stdout).to_string();
        let detail = if err.trim().is_empty() { out } else { err };
        return Err(format!("TV media command failed: {detail}"));
    }
    Ok(())
}

/// Send a media control command to the TV (pause / play / toggle / stop).
#[tauri::command]
async fn media_control(action: String) -> Result<(), String> {
    let flag = match action.as_str() {
        "pause"  => "--media-pause",
        "play" | "resume" => "--media-play",
        "toggle" => "--media-toggle",
        "stop"   => "--media-stop",
        other    => return Err(format!("Unknown media action: {other}")),
    };
    run_tv_media_cmd(flag).await
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — play
// ─────────────────────────────────────────────────────────────────────────────

/// Open Prime Video in an in-app browser window for Mac playback.
#[tauri::command]
async fn play_on_mac(
    app: tauri::AppHandle,
    content_id: String,
    episode: Option<i32>,
    title: Option<String>,
) -> Result<(), String> {
    let cfg = load_config();
    let (url, resolved_title) = resolve_prime_play_url(&cfg, &content_id, episode).await?;
    let parsed = validate_prime_video_url(&url)?;
    let display_title = title
        .filter(|t| !t.trim().is_empty())
        .unwrap_or(resolved_title);
    open_prime_player_window(&app, parsed, &display_title)
}

/// Clear saved Amazon/Prime login cookies from the in-app player window.
#[tauri::command]
async fn clear_prime_login(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(PRIME_PLAYER_LABEL) {
        window
            .clear_all_browsing_data()
            .map_err(|e| format!("Failed to clear Prime login: {e}"))?;
        return Ok(());
    }

    let url = validate_prime_video_url("https://www.primevideo.com/")?;
    let window = WebviewWindowBuilder::new(&app, PRIME_PLAYER_LABEL, WebviewUrl::External(url))
        .title("Prime Video")
        .visible(false)
        .build()
        .map_err(|e| format!("Failed to open player for cookie clear: {e}"))?;
    window
        .clear_all_browsing_data()
        .map_err(|e| format!("Failed to clear Prime login: {e}"))?;
    window
        .close()
        .map_err(|e| format!("Failed to close temporary player: {e}"))?;
    Ok(())
}

/// Send a title to the LG TV for playback. Streams progress via "play-progress" events.
#[tauri::command]
async fn play_on_tv(
    app: tauri::AppHandle,
    content_id: String,
    profile: i32,
    tv_ip: String,
    episode: Option<i32>,
    start_seconds: Option<i32>,
) -> Result<String, String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

    // Emit before taking the lock so the UI shows immediate feedback even when
    // another TV command is still in flight ahead of this one.
    let _ = app.emit("play-progress", "Preparing to play...\n".to_string());
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let _ = app.emit("play-progress", format!("Connecting to TV at {tv_ip}...\n"));

    let mut command = tokio::process::Command::new(&python);
    command
        .current_dir(&root)
        .arg(&script)
        .arg(&tv_ip)
        .arg("--launch")
        .arg("amazon")
        .arg("--content-id")
        .arg(&content_id)
        .arg("--profile")
        .arg(profile.to_string())
        .arg("--play");

    // For TV series, specify which episode to play (season deep links don't
    // auto-play on the TV without an episode).
    if let Some(ep) = episode {
        if ep >= 1 {
            command.arg("--episode").arg(ep.to_string());
        }
    }

    // Start playback at a chosen position (seconds) via the autoplay ?t= deep link.
    if let Some(start) = start_seconds {
        if start >= 1 {
            command.arg("--start").arg(start.to_string());
        }
    }

    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to launch lg-tv-connect.py: {e}"))?;

    let stdout = child.stdout.take().unwrap();
    let mut stdout_lines = BufReader::new(stdout).lines();
    let app_out = app.clone();
    let stdout_task = tokio::spawn(async move {
        while let Ok(Some(line)) = stdout_lines.next_line().await {
            let _ = app_out.emit("play-progress", format!("{line}\n"));
        }
    });

    let stderr = child.stderr.take().unwrap();
    let mut stderr_lines = BufReader::new(stderr).lines();
    let app_err = app.clone();
    let stderr_task = tokio::spawn(async move {
        while let Ok(Some(line)) = stderr_lines.next_line().await {
            let _ = app_err.emit("play-progress", format!("[err] {line}\n"));
        }
    });

    let status = match timeout(TV_PLAY_TIMEOUT, child.wait()).await {
        Ok(res) => res.map_err(|e| format!("Process error: {e}"))?,
        Err(_) => {
            // Stalled launch — kill it so the shared TV lock is released and
            // future plays aren't blocked forever.
            let _ = child.start_kill();
            let _ = child.wait().await;
            let _ = stdout_task.await;
            let _ = stderr_task.await;
            let msg = format!(
                "Playback timed out after {}s — the TV may be off or unreachable.",
                TV_PLAY_TIMEOUT.as_secs()
            );
            let _ = app.emit("play-progress", format!("{msg}\n"));
            return Err(msg);
        }
    };

    let _ = stdout_task.await;
    let _ = stderr_task.await;

    if status.success() {
        let _ = apply_configured_tv_volume(&cfg, true).await;
        let _ = app.emit("play-progress", "Done.\n".to_string());
        Ok("Done".to_string())
    } else {
        let msg = format!("Process exited with status: {status}");
        let _ = app.emit("play-progress", format!("{msg}\n"));
        Err(msg)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — seek / position
// ─────────────────────────────────────────────────────────────────────────────

/// Seek the currently playing content to an absolute position.
/// `content_id` is forwarded to Python so it can try re-launching the app
/// at the desired position when the SSAP seek command is unsupported.
/// `episode` lets the seek target a specific episode of a series.
#[tauri::command]
async fn seek_to(seconds: f64, content_id: Option<String>, episode: Option<i32>) -> Result<(), String> {
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let pos_str = format!("{:.0}", seconds.max(0.0));
    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script)
       .arg(&cfg.tv_ip)
       .arg("--seek")
       .arg(&pos_str);

    if let Some(ref id) = content_id {
        cmd.arg("--content-id").arg(id);
    }

    if let Some(ep) = episode {
        if ep >= 1 {
            cmd.arg("--episode").arg(ep.to_string());
        }
    }

    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(format!("seek failed: {err}"));
    }
    Ok(())
}

/// Try to get the current playback position from the TV.
/// Returns `{position: f64|null, duration: f64|null}`.
#[tauri::command]
async fn get_playback_position() -> Result<serde_json::Value, String> {
    let _tv = tv_cmd_lock().lock().await;
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script).arg(&cfg.tv_ip).arg("--get-position");
    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let json_line = stdout
        .lines()
        .filter(|l| l.trim_start().starts_with('{'))
        .last()
        .unwrap_or(r#"{"position":null,"duration":null}"#);

    serde_json::from_str(json_line).map_err(|e| e.to_string())
}

/// List episodes for a TV season/series content_id. Returns the JSON array
/// string produced by `lg-tv-connect.py --list-episodes` (no TV connection
/// needed — it only fetches the Prime Video detail page).
#[tauri::command]
async fn list_episodes(content_id: String) -> Result<String, String> {
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let mut cmd = tokio::process::Command::new(&python);
    cmd.arg(&script)
        .arg(&cfg.tv_ip)
        .arg("--list-episodes")
        .arg("--content-id")
        .arg(&content_id);
    let output = run_tv_command(cmd, TV_CMD_TIMEOUT).await?;

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let json_line = stdout
        .lines()
        .filter(|l| l.trim_start().starts_with('['))
        .last()
        .unwrap_or("[]");

    if !output.status.success() && json_line == "[]" {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(format!("list episodes failed: {err}"));
    }

    Ok(json_line.to_string())
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — TV connection diagnosis & repair (macOS)
//
// The most common "TV unreachable" failure is a Wi-Fi layer-2 problem: the TV
// is powered on and visible via mDNS/Bonjour (multicast), but the Mac can't
// exchange unicast packets with it (it roamed to another radio/AP, client
// isolation, or a stale neighbor/reject route). These helpers let the app
// detect that and offer a one-click fix without dropping to a terminal.
// ─────────────────────────────────────────────────────────────────────────────

/// Can we open the WebOS control port (3000/3001) on `ip` within `timeout`?
/// This is the connectivity check that actually matters for playback.
async fn tcp_port_open(ip: &str, port: u16, timeout: Duration) -> bool {
    let Ok(addr) = format!("{ip}:{port}").parse::<std::net::SocketAddr>() else {
        return false;
    };
    matches!(
        tokio::time::timeout(timeout, tokio::net::TcpStream::connect(addr)).await,
        Ok(Ok(_))
    )
}

async fn tv_control_reachable(ip: &str) -> bool {
    let t = Duration::from_secs(3);
    tcp_port_open(ip, 3000, t).await || tcp_port_open(ip, 3001, t).await
}

/// Discover the LG TV's current IPv4 via mDNS/Bonjour (handles DHCP changes and
/// the "visible but unreachable" case). macOS resolves `lgwebostv.local` for us.
#[cfg(target_os = "macos")]
async fn discover_lg_tv_ip() -> Option<String> {
    // 1) Directory-service cache — fast and reliable for *.local names.
    if let Ok(out) = tokio::process::Command::new("dscacheutil")
        .args(["-q", "host", "-a", "name", "lgwebostv.local"])
        .output()
        .await
    {
        let text = String::from_utf8_lossy(&out.stdout);
        for line in text.lines() {
            if let Some(rest) = line.trim().strip_prefix("ip_address:") {
                let ip = rest.trim();
                if ip.parse::<std::net::Ipv4Addr>().is_ok() {
                    return Some(ip.to_string());
                }
            }
        }
    }
    // 2) Fall back to mDNS name resolution via ping (prints the resolved IP even
    //    when the host doesn't answer ICMP).
    if let Ok(out) = tokio::process::Command::new("ping")
        .args(["-c", "1", "-t", "1", "lgwebostv.local"])
        .output()
        .await
    {
        let text = String::from_utf8_lossy(&out.stdout);
        // e.g. "PING lgwebostv.local (192.168.0.79): 56 data bytes"
        if let Some(start) = text.find('(') {
            if let Some(end) = text[start + 1..].find(')') {
                let ip = &text[start + 1..start + 1 + end];
                if ip.parse::<std::net::Ipv4Addr>().is_ok() {
                    return Some(ip.to_string());
                }
            }
        }
    }
    None
}

#[cfg(not(target_os = "macos"))]
async fn discover_lg_tv_ip() -> Option<String> {
    None
}

/// Flush the stale ARP/neighbor entry and any REJECT host route the kernel keeps
/// for `ip`. After a failed ARP resolution macOS blackholes the host — the
/// classic "visible via mDNS but no unicast" state — and never retries until the
/// entry is cleared. Removing it forces a fresh resolution on the next packet.
///
/// This needs root, so (like the `sudo arp -d` / `sudo route delete` steps in
/// `scripts/fix-tv-connection.sh`) we run it through a single osascript
/// "administrator privileges" prompt, which shows the native macOS password
/// dialog. This is the Mac-side repair the standalone script performs and is
/// usually what actually restores the connection.
#[cfg(target_os = "macos")]
async fn flush_tv_neighbor(ip: &str) -> Result<(), String> {
    // Validate before interpolating into a shell string (defense in depth —
    // `ip` comes from config/mDNS, never raw user input).
    if ip.parse::<std::net::Ipv4Addr>().is_err() {
        return Err(format!("invalid IP: {ip}"));
    }
    let inner = format!(
        "/usr/sbin/arp -d {ip} 2>/dev/null; /sbin/route -n delete {ip} 2>/dev/null; exit 0"
    );
    let script = format!("do shell script \"{inner}\" with administrator privileges");
    let out = tokio::process::Command::new("osascript")
        .args(["-e", &script])
        .output()
        .await
        .map_err(|e| format!("osascript failed: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        Err(if err.is_empty() {
            "authorization cancelled".to_string()
        } else {
            err
        })
    }
}

/// Parse "08:27:A8:6C:B6:72" / "08-27-..." into 6 bytes.
fn parse_mac(mac: &str) -> Option<[u8; 6]> {
    let parts: Vec<&str> = mac.split(|c| c == ':' || c == '-').collect();
    if parts.len() != 6 {
        return None;
    }
    let mut bytes = [0u8; 6];
    for (i, p) in parts.iter().enumerate() {
        bytes[i] = u8::from_str_radix(p.trim(), 16).ok()?;
    }
    Some(bytes)
}

/// Send a Wake-on-LAN magic packet to `mac` (broadcast UDP, ports 9 and 7).
/// Wakes an LG TV that supports network standby ("Mobile TV On").
fn send_wake_on_lan(mac: &str) -> Result<(), String> {
    let bytes = parse_mac(mac).ok_or_else(|| format!("Invalid TV MAC: {mac}"))?;
    let mut packet = vec![0xFFu8; 6];
    for _ in 0..16 {
        packet.extend_from_slice(&bytes);
    }
    let socket = std::net::UdpSocket::bind("0.0.0.0:0")
        .map_err(|e| format!("WoL socket bind failed: {e}"))?;
    socket
        .set_broadcast(true)
        .map_err(|e| format!("WoL broadcast enable failed: {e}"))?;
    let mut sent = false;
    for port in [9u16, 7] {
        if socket
            .send_to(&packet, (std::net::Ipv4Addr::BROADCAST, port))
            .is_ok()
        {
            sent = true;
        }
    }
    if sent {
        Ok(())
    } else {
        Err("Could not send Wake-on-LAN packet".to_string())
    }
}

/// Find the Wi-Fi hardware port's device name (usually en0).
#[cfg(target_os = "macos")]
async fn wifi_interface() -> String {
    if let Ok(out) = tokio::process::Command::new("networksetup")
        .arg("-listallhardwareports")
        .output()
        .await
    {
        let text = String::from_utf8_lossy(&out.stdout);
        let mut wifi = false;
        for line in text.lines() {
            if line.contains("Wi-Fi") || line.contains("AirPort") {
                wifi = true;
            } else if wifi {
                if let Some(dev) = line.trim().strip_prefix("Device:") {
                    return dev.trim().to_string();
                }
            }
        }
    }
    "en0".to_string()
}

#[derive(Serialize)]
struct TvRepairReport {
    reachable: bool,
    ip: String,
    ip_changed: bool,
    discovered: bool,
    wifi_restarted: bool,
    steps: Vec<String>,
    advice: Option<String>,
}

/// Scan the local network for an LG TV via mDNS/Bonjour and return its IP.
/// Updates and saves the config when the discovered IP differs from the saved one.
#[tauri::command]
async fn scan_for_tv(app: tauri::AppHandle) -> Result<String, String> {
    match discover_lg_tv_ip().await {
        Some(ip) => {
            let mut cfg = load_config();
            if cfg.tv_ip.trim() != ip.as_str() {
                cfg.tv_ip = ip.clone();
                let _ = save_config_to_disk(&cfg);
                let _ = app.emit("config-updated", cfg);
            }
            Ok(ip)
        }
        None => Err("No LG TV found on the network via mDNS/Bonjour".to_string()),
    }
}

/// Quick connectivity check used by the UI to re-test after a repair or to
/// decide whether to offer the "Fix connection" flow.
#[tauri::command]
async fn check_tv_reachable() -> Result<bool, String> {
    let cfg = load_config();
    if cfg.tv_ip.trim().is_empty() {
        return Ok(false);
    }
    Ok(tv_control_reachable(cfg.tv_ip.trim()).await)
}

/// Attempt to automatically restore TV connectivity from the Mac side.
///
/// Non-disruptive steps always run: re-discover the TV's current IP via mDNS
/// (updating the saved IP when DHCP moved it) and send Wake-on-LAN. When
/// `restart_wifi` is true it also power-cycles the Mac's Wi-Fi to force a fresh
/// association and clear a stale neighbor/reject route. Progress is streamed via
/// "repair-progress" events; the final report says whether the TV is reachable
/// and, if not, what must be changed on the TV/router.
#[tauri::command]
async fn repair_tv_connection(
    app: tauri::AppHandle,
    restart_wifi: bool,
) -> Result<TvRepairReport, String> {
    let _tv = tv_cmd_lock().lock().await;
    let mut cfg = load_config();
    let mut steps: Vec<String> = Vec::new();
    let emit = |msg: &str| {
        let _ = app.emit("repair-progress", format!("{msg}\n"));
    };

    let mut ip = cfg.tv_ip.trim().to_string();
    let mut ip_changed = false;
    let mut discovered = false;

    macro_rules! note {
        ($($arg:tt)*) => {{
            let line = format!($($arg)*);
            emit(&line);
            steps.push(line);
        }};
    }

    note!("Checking the configured TV address ({})…", if ip.is_empty() { "<none>" } else { &ip });

    // Already reachable? Nothing to do.
    if !ip.is_empty() && tv_control_reachable(&ip).await {
        note!("TV is reachable. No repair needed.");
        return Ok(TvRepairReport {
            reachable: true,
            ip,
            ip_changed: false,
            discovered: false,
            wifi_restarted: false,
            steps,
            advice: None,
        });
    }

    // Step 1 — re-discover the TV on the network via mDNS.
    note!("Looking for the TV on the network (mDNS)…");
    if let Some(found) = discover_lg_tv_ip().await {
        discovered = true;
        if !ip.is_empty() && found != ip {
            note!("TV moved to a new address: {found} (was {ip}). Updating settings.");
            cfg.tv_ip = found.clone();
            let _ = save_config_to_disk(&cfg);
            let _ = app.emit("config-updated", cfg.clone());
            ip = found;
            ip_changed = true;
        } else {
            if ip.is_empty() {
                ip = found.clone();
                cfg.tv_ip = found.clone();
                let _ = save_config_to_disk(&cfg);
                let _ = app.emit("config-updated", cfg.clone());
                ip_changed = true;
            }
            note!("TV is visible via mDNS at {ip}.");
        }
        if tv_control_reachable(&ip).await {
            note!("TV is now reachable at {ip}.");
            return Ok(TvRepairReport {
                reachable: true,
                ip,
                ip_changed,
                discovered,
                wifi_restarted: false,
                steps,
                advice: None,
            });
        }
        note!("TV is visible but not answering control requests (Wi-Fi isolation / roamed AP).");
    } else {
        note!("TV not found via mDNS — it may be powered off or on another network/band.");
    }

    // Step 1.5 — clear a stale neighbor / REJECT host route on the Mac. After a
    // failed ARP, macOS blackholes the TV ("visible but unreachable"); flushing
    // it lets the next packet re-resolve. This is the Mac-side repair that the
    // standalone scripts/fix-tv-connection.sh performs via sudo, and is usually
    // what actually restores a TV that mDNS can see but control requests can't
    // reach — the previous native flow skipped it entirely.
    #[cfg(target_os = "macos")]
    if !ip.is_empty() {
        note!("Clearing the stale network route to the TV (may prompt for your password)…");
        match flush_tv_neighbor(&ip).await {
            Ok(()) => {
                note!("Cleared the stale route. Re-checking the connection…");
                // Re-trigger ARP resolution with a few quick probes, then re-test.
                for _ in 0..3 {
                    let _ = tokio::process::Command::new("ping")
                        .args(["-c", "1", "-t", "1", &ip])
                        .output()
                        .await;
                }
                if tv_control_reachable(&ip).await {
                    note!("TV is now reachable at {ip}.");
                    return Ok(TvRepairReport {
                        reachable: true,
                        ip,
                        ip_changed,
                        discovered,
                        wifi_restarted: false,
                        steps,
                        advice: None,
                    });
                }
            }
            Err(e) => note!("Could not clear the stale route ({e}). Continuing…"),
        }
    }

    // Step 2 — Wake-on-LAN (wakes a TV in network standby).
    if !cfg.tv_mac.trim().is_empty() {
        match send_wake_on_lan(cfg.tv_mac.trim()) {
            Ok(()) => note!("Sent Wake-on-LAN to {}.", cfg.tv_mac.trim()),
            Err(e) => note!("Wake-on-LAN failed: {e}"),
        }
    } else {
        note!("No TV MAC saved — skipping Wake-on-LAN.");
    }

    // Step 3 — optional Wi-Fi power-cycle to force a fresh association and clear
    // a stale neighbor/reject route on the Mac.
    let mut wifi_restarted = false;
    #[cfg(target_os = "macos")]
    if restart_wifi {
        let iface = wifi_interface().await;
        note!("Restarting Wi-Fi ({iface}) to force a fresh connection…");
        let _ = tokio::process::Command::new("networksetup")
            .args(["-setairportpower", &iface, "off"])
            .output()
            .await;
        tokio::time::sleep(Duration::from_secs(3)).await;
        let _ = tokio::process::Command::new("networksetup")
            .args(["-setairportpower", &iface, "on"])
            .output()
            .await;
        // Give the network a few seconds to come back.
        for _ in 0..15 {
            tokio::time::sleep(Duration::from_secs(1)).await;
            if !ip.is_empty() && tv_control_reachable(&ip).await {
                break;
            }
        }
        wifi_restarted = true;
        note!("Wi-Fi reconnected.");

        // The TV may have re-announced a different IP after we rejoined.
        if let Some(found) = discover_lg_tv_ip().await {
            if !found.is_empty() && found != ip {
                note!("TV re-appeared at {found}. Updating settings.");
                cfg.tv_ip = found.clone();
                let _ = save_config_to_disk(&cfg);
                let _ = app.emit("config-updated", cfg.clone());
                ip = found;
                ip_changed = true;
            }
        }
    }
    let _ = restart_wifi; // referenced on non-macOS to avoid unused warning

    // Final re-test.
    let reachable = !ip.is_empty() && tv_control_reachable(&ip).await;
    let advice = if reachable {
        note!("TV is now reachable at {ip}.");
        None
    } else {
        note!("Still unable to reach the TV.");
        Some(
            if restart_wifi {
                "The TV is online but isolated from this Mac on Wi-Fi. On the TV, reconnect its Wi-Fi or reboot it; if you use a mesh/extender or guest network, disable client isolation or put the TV and Mac on the same network. Wiring the TV via Ethernet also fixes this."
            } else {
                "Try the Wi-Fi reset below. If it still fails, reconnect the TV's Wi-Fi or reboot the TV — it's online but isolated from this Mac on the network."
            }
            .to_string(),
        )
    };

    Ok(TvRepairReport {
        reachable,
        ip,
        ip_changed,
        discovered,
        wifi_restarted,
        steps,
        advice,
    })
}

// ─────────────────────────────────────────────────────────────────────────────
// App entry point
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(target_os = "macos")]
fn set_macos_process_name(name: &str) {
    use objc2_foundation::{NSProcessInfo, NSString};
    let process_name = NSString::from_str(name);
    NSProcessInfo::processInfo().setProcessName(&process_name);
}

/// Set the Dock/window icon when running an unbundled binary (avoids the generic "exec" tile).
fn set_app_icon(app: &tauri::App) -> Result<(), String> {
    let icon = tauri::include_image!("icons/icon.png");
    if let Some(window) = app.handle().get_webview_window("main") {
        window.set_icon(icon).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn prevent_default_plugin() -> tauri::plugin::TauriPlugin<tauri::Wry> {
    use tauri_plugin_prevent_default::Flags;

    // Block the native WKWebView menu (dev shows "Reload" only) so our custom menus work.
    tauri_plugin_prevent_default::Builder::new()
        .with_flags(Flags::CONTEXT_MENU | Flags::RELOAD | Flags::pointer())
        .build()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(target_os = "macos")]
    set_macos_process_name("Prime Remote Control");

    let port_state = ImageServerPort(Arc::new(Mutex::new(0u16)));
    let port_arc = Arc::clone(&port_state.0);

    tauri::Builder::default()
        .plugin(prevent_default_plugin())
        .manage(port_state)
        .setup(move |app| {
            set_app_icon(app)?;
            let port_arc = Arc::clone(&port_arc);
            tauri::async_runtime::spawn(async move {
                let port = start_image_server().await;
                *port_arc.lock().unwrap() = port;
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            open_external_url,
            open_tmdb_trailer,
            get_config,
            save_config,
            discover_tv_mac,
            get_bookmarks,
            add_bookmark,
            remove_bookmark,
            toggle_bookmark,
            load_catalog,
            search_catalog,
            play_on_mac,
            clear_prime_login,
            play_on_tv,
            collection_cache_age,
            search_cache_age,
            clear_all_cache,
            get_prime_region,
            get_public_ip,
            prefetch_images,
            list_cached_images,
            get_image_server_port,
            media_control,
            tv_power,
            get_tv_power,
            get_tv_volume,
            set_tv_volume,
            volume_step,
            set_tv_mute,
            seek_to,
            get_playback_position,
            list_episodes,
            check_tv_reachable,
            scan_for_tv,
            repair_tv_connection,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Tauri application");
}
