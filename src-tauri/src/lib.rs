use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use tauri::{Emitter, Manager};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

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
        }
    }
}

fn config_path() -> PathBuf {
    home_dir().join(".config").join("prime-remote-control.json")
}

fn load_config() -> AppConfig {
    let path = config_path();
    if path.exists() {
        if let Ok(data) = std::fs::read_to_string(&path) {
            if let Ok(cfg) = serde_json::from_str::<AppConfig>(&data) {
                return cfg;
            }
        }
    }
    AppConfig::default()
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

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
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
    let key = format!("collection_{collection}");
    cache_age_secs(&key)
}

/// Returns seconds since the search cache was last written (None = no cache).
#[tauri::command]
async fn search_cache_age(query: String) -> Option<u64> {
    let key = format!("search_{}", query.trim().to_lowercase());
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

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — catalog
// ─────────────────────────────────────────────────────────────────────────────

/// Load a Prime Video collection. Serves from disk cache unless `force_refresh`
/// is true or the cache is older than the configured TTL.
#[tauri::command]
async fn load_catalog(collection: String, force_refresh: bool) -> Result<String, String> {
    let cfg = load_config();
    let cache_key = format!("collection_{collection}");

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
    let cache_key = format!("search_{}", query.trim().to_lowercase());
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
async fn run_tv_volume_cmd(args: &[&str]) -> Result<VolumeState, String> {
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
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

    let output = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run lg-tv-connect.py: {e}"))?;

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

/// Get current volume level and mute state.
#[tauri::command]
async fn get_tv_volume() -> Result<VolumeState, String> {
    run_tv_volume_cmd(&["--volume-get"]).await
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

/// Send a media control command to the TV (pause / play / toggle).
#[tauri::command]
async fn media_control(
    app: tauri::AppHandle,
    action: String, // "pause" | "play" | "toggle"
) -> Result<(), String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let flag = match action.as_str() {
        "pause"  => "--media-pause",
        "play" | "resume" => "--media-play",
        "toggle" => "--media-toggle",
        "stop"   => "--media-stop",
        other    => return Err(format!("Unknown media action: {other}")),
    };

    let mut child = tokio::process::Command::new(&python)
        .arg(&script)
        .arg(&cfg.tv_ip)
        .arg(flag)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to run lg-tv-connect.py: {e}"))?;

    let stdout = child.stdout.take().unwrap();
    let app_out = app.clone();
    tokio::spawn(async move {
        let mut lines = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = app_out.emit("play-progress", format!("{line}\n"));
        }
    });

    let stderr = child.stderr.take().unwrap();
    let app_err = app.clone();
    tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = app_err.emit("play-progress", format!("[err] {line}\n"));
        }
    });

    let status = child.wait().await.map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(format!("media control exited with: {status}"));
    }
    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// Tauri commands — play
// ─────────────────────────────────────────────────────────────────────────────

/// Send a title to the LG TV for playback. Streams progress via "play-progress" events.
#[tauri::command]
async fn play_on_tv(
    app: tauri::AppHandle,
    content_id: String,
    profile: i32,
    tv_ip: String,
    episode: Option<i32>,
) -> Result<String, String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

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

    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to launch lg-tv-connect.py: {e}"))?;

    let stdout = child.stdout.take().unwrap();
    let mut stdout_lines = BufReader::new(stdout).lines();
    let app_out = app.clone();
    tokio::spawn(async move {
        while let Ok(Some(line)) = stdout_lines.next_line().await {
            let _ = app_out.emit("play-progress", format!("{line}\n"));
        }
    });

    let stderr = child.stderr.take().unwrap();
    let mut stderr_lines = BufReader::new(stderr).lines();
    let app_err = app.clone();
    tokio::spawn(async move {
        while let Ok(Some(line)) = stderr_lines.next_line().await {
            let _ = app_err.emit("play-progress", format!("[err] {line}\n"));
        }
    });

    let status = child
        .wait()
        .await
        .map_err(|e| format!("Process error: {e}"))?;

    if status.success() {
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
#[tauri::command]
async fn seek_to(seconds: f64, content_id: Option<String>) -> Result<(), String> {
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

    let output = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run lg-tv-connect.py: {e}"))?;

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
    let cfg = load_config();
    let root = resolve_project_root(&cfg);
    let python = python_exe(&root);
    let script = root
        .join("amazon")
        .join("lg-tv-connect.py")
        .to_string_lossy()
        .to_string();

    let output = tokio::process::Command::new(&python)
        .arg(&script)
        .arg(&cfg.tv_ip)
        .arg("--get-position")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run lg-tv-connect.py: {e}"))?;

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

    let output = tokio::process::Command::new(&python)
        .arg(&script)
        .arg(&cfg.tv_ip)
        .arg("--list-episodes")
        .arg("--content-id")
        .arg(&content_id)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run lg-tv-connect.py: {e}"))?;

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
            get_config,
            save_config,
            get_bookmarks,
            add_bookmark,
            remove_bookmark,
            toggle_bookmark,
            load_catalog,
            search_catalog,
            play_on_tv,
            collection_cache_age,
            search_cache_age,
            clear_all_cache,
            prefetch_images,
            list_cached_images,
            get_image_server_port,
            media_control,
            get_tv_volume,
            set_tv_volume,
            volume_step,
            set_tv_mute,
            seek_to,
            get_playback_position,
            list_episodes,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Tauri application");
}
