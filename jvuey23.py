#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual",
#     "requests",
# ]
# ///
"""
Jellyfin TUI Selector - Pick a title and launch in MPV
"""

import os
import re
import sys
import time
import subprocess
import requests
from pathlib import Path
from configparser import ConfigParser
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Header, Footer, Input, ListView, ListItem, Label
from textual.binding import Binding


# ============================================================================
# Constants
# ============================================================================

CONFIG_PATHS = [
    Path.home() / ".config" / "jvuey" / "config.ini",
    Path.home() / ".jvuey.ini",
    Path("config.ini"),
]

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "jvuey" / "config.ini"

# TTY mode detection - set by main() based on environment or --tty flag
# Using a dict as a mutable container so global assignment works properly
_CONFIG = {"tty_mode": False, "chars": None}


def set_tty_mode(enabled: bool) -> None:
    """Set TTY mode and cache the character set"""
    _CONFIG["tty_mode"] = enabled
    _CONFIG["chars"] = CHARS_ASCII if enabled else CHARS_UNICODE


def get_tty_mode() -> bool:
    """Get TTY mode"""
    return _CONFIG["tty_mode"]


def _get_chars() -> dict:
    """Get cached character set"""
    if _CONFIG["chars"] is None:
        _CONFIG["chars"] = CHARS_ASCII if _CONFIG["tty_mode"] else CHARS_UNICODE
    return _CONFIG["chars"]

# Character sets for different display modes
CHARS_UNICODE = {
    "tv": "📺",
    "movie": "🎬",
    "watched": "✓",
    "partial": "◐",
    "unwatched": "○",
    "selected": "●",
    "unselected": "○",
    "play": "▶",
    "separator": "─" * 40,
    "settings": "⚙️",
    "clear": "🗑️",
    "connector": "🔌",
    "display": "📺",
    "audio": "🔊",
    "error": "❌",
    "info": "ℹ️",
    "server": "🖥️",
}

CHARS_ASCII = {
    "tv": "S:",
    "movie": "M:",
    "watched": "+",
    "partial": "~",
    "unwatched": "-",
    "selected": "*",
    "unselected": "o",
    "play": ">",
    "separator": "-" * 40,
    "settings": "==",
    "clear": "xx",
    "connector": ">>",
    "display": "D:",
    "audio": "A:",
    "error": "!!",
    "info": "ii",
    "server": "SV",
}


def is_tty() -> bool:
    """Detect if running on Linux virtual console (TTY) vs terminal emulator"""
    # Check TERM environment variable - Linux console sets TERM=linux
    term = os.environ.get("TERM", "")
    if term == "linux":
        return True
    
    # Check if stdout is connected to a real TTY device (not pseudo-terminal)
    try:
        tty_name = os.ttyname(sys.stdout.fileno())
        # Real TTYs are /dev/tty1, /dev/tty2, etc.
        # Pseudo-terminals are /dev/pts/0, /dev/pts/1, etc.
        if "/dev/tty" in tty_name and "/dev/tty" != tty_name:
            # Extract the part after /dev/tty
            suffix = tty_name.replace("/dev/tty", "")
            if suffix.isdigit():
                return True
    except (OSError, AttributeError):
        pass
    
    return False


# ============================================================================
# Configuration Helpers
# ============================================================================

def get_config_path() -> Path:
    """Get the primary config file path"""
    for config_path in CONFIG_PATHS:
        if config_path.exists():
            return config_path
    return DEFAULT_CONFIG_PATH


def load_config() -> ConfigParser:
    """Load configuration from config file"""
    config = ConfigParser()
    for config_path in CONFIG_PATHS:
        if config_path.exists():
            config.read(config_path)
            break
    return config


def save_config(config: ConfigParser) -> None:
    """Save configuration to the config file"""
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        config.write(f)


# ============================================================================
# Multi-Server Configuration
# ============================================================================

def _migrate_legacy_config(config: ConfigParser) -> bool:
    """
    Migrate old single-server [jellyfin] config to multi-server format.
    Returns True if migration was performed.
    """
    if "jellyfin" not in config:
        return False
    # Already migrated?
    if any(s.startswith("server.") for s in config.sections()):
        # Remove leftover [jellyfin] section
        config.remove_section("jellyfin")
        save_config(config)
        return True

    url = config["jellyfin"].get("url", "")
    api_key = config["jellyfin"].get("api_key", "")
    user_id = config["jellyfin"].get("user_id", "")

    if not all([url, api_key, user_id]):
        return False

    # Derive a server name from the URL
    server_name = _derive_server_name(url, config)

    config[f"server.{server_name}"] = {
        "url": url,
        "api_key": api_key,
        "user_id": user_id,
    }

    if "general" not in config:
        config["general"] = {}
    config["general"]["active_server"] = server_name

    config.remove_section("jellyfin")
    save_config(config)
    return True


def _derive_server_name(url: str, config: ConfigParser) -> str:
    """Derive a human-readable server name from a URL, uniqueified against existing servers."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    hostname = parsed.hostname or "server"
    # Strip common prefixes
    for prefix in ("www.", "jellyfin.", "jf."):
        if hostname.startswith(prefix):
            hostname = hostname[len(prefix):]
    # Use just hostname, replace dots with dashes
    base_name = hostname.replace(".", "-")
    # Strip trailing port-like suffixes if hostname is an IP
    # Keep it simple - just use the base
    return _uniqueify_name(base_name, config)


def _uniqueify_name(base_name: str, config: ConfigParser) -> str:
    """Ensure a server name is unique by appending a number if needed."""
    existing = get_server_names(config)
    if base_name not in existing:
        return base_name
    counter = 2
    while f"{base_name}-{counter}" in existing:
        counter += 1
    return f"{base_name}-{counter}"


def get_server_names(config: ConfigParser) -> list[str]:
    """Get list of configured server names in order."""
    names = []
    for section in config.sections():
        if section.startswith("server."):
            names.append(section[7:])  # strip "server." prefix
    return names


def get_server_config(config: ConfigParser, server_name: str) -> dict | None:
    """Get server configuration by name. Returns dict with url/api_key/user_id or None."""
    section = f"server.{server_name}"
    if section not in config:
        return None
    return {
        "url": config[section].get("url", ""),
        "api_key": config[section].get("api_key", ""),
        "user_id": config[section].get("user_id", ""),
    }


def get_active_server_name(config: ConfigParser) -> str | None:
    """Get the name of the active server."""
    if "general" in config:
        name = config["general"].get("active_server", "")
        if name and f"server.{name}" in config:
            return name
    # Fallback to first server
    names = get_server_names(config)
    return names[0] if names else None


def set_active_server(config: ConfigParser, server_name: str) -> None:
    """Set the active server and save."""
    if "general" not in config:
        config["general"] = {}
    config["general"]["active_server"] = server_name
    save_config(config)


def add_server_to_config(config: ConfigParser, server_name: str,
                         url: str, api_key: str, user_id: str) -> str:
    """
    Add a server to the config. Returns the (possibly uniqueified) name used.
    """
    final_name = _uniqueify_name(server_name, config)
    config[f"server.{final_name}"] = {
        "url": url,
        "api_key": api_key,
        "user_id": user_id,
    }
    # If this is the first server, make it active
    if len(get_server_names(config)) == 1:
        if "general" not in config:
            config["general"] = {}
        config["general"]["active_server"] = final_name
    save_config(config)
    return final_name


def remove_server_from_config(config: ConfigParser, server_name: str) -> None:
    """Remove a server from config. Adjusts active_server if needed."""
    section = f"server.{server_name}"
    if section in config:
        config.remove_section(section)

    # If we just removed the active server, pick another one
    if "general" in config and config["general"].get("active_server") == server_name:
        remaining = get_server_names(config)
        if remaining:
            config["general"]["active_server"] = remaining[0]
        else:
            config["general"]["active_server"] = ""

    save_config(config)


# ============================================================================
# MPV Helpers
# ============================================================================

# Pre-compiled regex patterns for parsing MPV output
_RE_DRM_CONNECTOR = re.compile(r'^Available modes for drm-connector=(\d+)\.(\S+)', re.IGNORECASE)
_RE_DRM_MODE = re.compile(r'^\s*Mode\s+(\d+):\s*(\d+)x(\d+)\s*\((\d+)x(\d+)@([\d.]+)Hz\)')
_RE_AUDIO_DEVICE = re.compile(r"^\s*'([^']+)'\s*\(([^)]+)\)")


def parse_drm_modes() -> dict[str, dict]:
    """
    Parse output of mpv --drm-mode=help to get connectors and their modes.
    Returns a dict mapping connector names to connector info dicts.
    """
    connectors = {}
    
    try:
        result = subprocess.run(
            ["mpv", "--drm-mode=help"],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout + result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"_error": str(e)}
    
    current_connector = None
    
    for line in output.splitlines():
        # Match connector header: "Available modes for drm-connector=0.HDMI-A-1"
        connector_match = _RE_DRM_CONNECTOR.match(line)
        
        if connector_match:
            connector_name = connector_match.group(2)
            current_connector = connector_name
            connectors[current_connector] = {
                "status": "connected",
                "modes": []
            }
            continue
        
        # Match mode lines: "  Mode 0: 3840x2160 (3840x2160@60.00Hz)"
        if current_connector:
            mode_match = _RE_DRM_MODE.match(line)
            if mode_match:
                connectors[current_connector]["modes"].append({
                    "id": mode_match.group(1),
                    "width": mode_match.group(2),
                    "height": mode_match.group(3),
                    "refresh": mode_match.group(6),
                    "display": f"{mode_match.group(2)}x{mode_match.group(3)} @ {mode_match.group(6)}Hz"
                })
    
    return connectors


def parse_audio_devices() -> list[dict]:
    """
    Parse output of mpv --audio-device=help to get available audio devices.
    Returns a list of device dicts with 'id' and 'description'.
    """
    devices = []
    
    try:
        result = subprocess.run(
            ["mpv", "--audio-device=help"],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout + result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return [{"_error": str(e)}]
    
    for line in output.splitlines():
        match = _RE_AUDIO_DEVICE.match(line)
        if match:
            devices.append({
                "id": match.group(1),
                "description": match.group(2),
            })
    
    return devices


# ============================================================================
# Formatting Helpers
# ============================================================================

def format_runtime(ticks: int | None) -> str:
    """Convert Jellyfin ticks (100-nanosecond intervals) to human-readable runtime"""
    if not ticks:
        return ""
    
    total_seconds = ticks // 10_000_000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_item_display(item: dict) -> tuple[str, str]:
    """
    Format an item for display in the list view.
    Returns (title_text, right_info) tuple.
    """
    chars = _get_chars()
    item_type = item.get("Type", "")
    name = item.get("Name", "Unknown")
    user_data = item.get("UserData") or {}
    
    # Get played indicator
    if user_data.get("Played"):
        played = chars["watched"]
    elif user_data.get("PlayedPercentage", 0) > 0 or user_data.get("PlayCount", 0) > 0:
        played = chars["partial"]
    else:
        played = chars["unwatched"]
    
    right_info = ""
    
    if item_type == "Series":
        year = f" ({item['ProductionYear']})" if "ProductionYear" in item else ""
        unplayed = user_data.get("UnplayedItemCount", 0)
        if unplayed > 0:
            right_info = f"{unplayed} unwatched"
        title_text = f"{chars['tv']} {played} {name}{year}"
    elif item_type == "BoxSet":
        child_count = item.get("ChildCount", 0)
        unplayed = user_data.get("UnplayedItemCount", 0)
        if unplayed > 0:
            right_info = f"{unplayed} unwatched"
        elif child_count:
            right_info = f"{child_count} items"
        title_text = f"{chars['movie']} {played} {name}"
    elif item_type == "Season":
        unplayed = user_data.get("UnplayedItemCount", 0)
        if unplayed > 0:
            right_info = f"{unplayed} unwatched"
        title_text = f"  {played} {name}"
    elif item_type == "Episode":
        episode_num = item.get("IndexNumber", "?")
        right_info = format_runtime(item.get("RunTimeTicks"))
        title_text = f"    {played} E{episode_num}: {name}"
    else:  # Movie
        year = f" ({item['ProductionYear']})" if "ProductionYear" in item else ""
        right_info = format_runtime(item.get("RunTimeTicks"))
        title_text = f"{chars['movie']} {played} {name}{year}"
    
    return title_text, right_info


# ============================================================================
# Quick Connect Authentication
# ============================================================================

def get_device_id() -> str:
    """Get or create a persistent device ID for this installation"""
    config = load_config()
    
    if "device" in config and "id" in config["device"]:
        return config["device"]["id"]
    
    import uuid
    device_id = str(uuid.uuid4())
    
    if "device" not in config:
        config["device"] = {}
    config["device"]["id"] = device_id
    save_config(config)
    
    return device_id


def quick_connect_auth(server_url: str) -> tuple[str, str] | None:
    """
    Perform Quick Connect authentication flow.
    Returns (api_key, user_id) on success, None on failure.
    """
    server_url = server_url.rstrip("/")
    device_id = get_device_id()
    
    headers = {
        "X-Emby-Authorization": f'MediaBrowser Client="jvuey", Device="jvuey-tui", DeviceId="{device_id}", Version="1.0.0"'
    }
    
    # Step 1: Check if Quick Connect is available
    try:
        response = requests.get(f"{server_url}/QuickConnect/Enabled", headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"Quick Connect check failed: {response.status_code}")
            return None
        
        if response.text.lower() not in ("true", "\"true\""):
            print("Quick Connect is not enabled on this server.")
            print("Enable it in: Dashboard → General → Quick Connect")
            return None
    except requests.RequestException as e:
        print(f"Failed to check Quick Connect status: {e}")
        return None
    
    # Step 2: Initiate Quick Connect
    try:
        response = requests.post(f"{server_url}/QuickConnect/Initiate", headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"Failed to initiate Quick Connect: {response.status_code}")
            return None
        
        qc_data = response.json()
        secret = qc_data.get("Secret")
        code = qc_data.get("Code")
        
        if not secret or not code:
            print("Invalid Quick Connect response")
            return None
    except requests.RequestException as e:
        print(f"Failed to initiate Quick Connect: {e}")
        return None
    
    # Step 3: Display code and wait for authorization
    print()
    print("=" * 50)
    print("  QUICK CONNECT CODE:")
    print()
    print(f"       {code}")
    print()
    print("  Enter this code in your Jellyfin app:")
    print("  User → Quick Connect → Enter Code")
    print("=" * 50)
    print()
    print("Waiting for authorization...", end="", flush=True)
    
    # Step 4: Poll for authorization
    for _ in range(60):  # 5 minutes at 5-second intervals
        time.sleep(5)
        print(".", end="", flush=True)
        
        try:
            response = requests.get(
                f"{server_url}/QuickConnect/Connect",
                params={"secret": secret},
                headers=headers,
                timeout=10
            )
            
            if response.status_code != 200:
                continue
            
            if response.json().get("Authenticated"):
                print(" Authorized!")
                
                # Step 5: Exchange for access token
                auth_response = requests.post(
                    f"{server_url}/Users/AuthenticateWithQuickConnect",
                    json={"Secret": secret},
                    headers=headers,
                    timeout=10
                )
                
                if auth_response.status_code != 200:
                    print(f"Failed to get access token: {auth_response.status_code}")
                    return None
                
                auth_data = auth_response.json()
                access_token = auth_data.get("AccessToken")
                user_id = auth_data.get("User", {}).get("Id")
                
                if access_token and user_id:
                    return (access_token, user_id)
                print("Missing access token or user ID in response")
                return None
        except requests.RequestException:
            continue
    
    print(" Timed out!")
    return None


def save_auth_to_config(server_url: str, api_key: str, user_id: str) -> None:
    """Save authentication credentials to config file (legacy compat, used by interactive_setup)"""
    config = load_config()
    if _migrate_legacy_config(config):
        config = load_config()

    server_name = _derive_server_name(server_url, config)
    final_name = add_server_to_config(config, server_name, server_url, api_key, user_id)
    set_active_server(config, final_name)
    print(f"Server '{final_name}' saved to {get_config_path()}")


# ============================================================================
# Main Application
# ============================================================================

# Sentinel for server-switch exit
class _ServerSwitch:
    """Returned from app.exit() when user wants to switch servers."""
    def __init__(self, server_name: str):
        self.server_name = server_name


class JellyfinSelector(App):
    """Jellyfin TUI for selecting and launching media in MPV"""
    
    CSS = """
    Screen {
        background: $surface;
    }
    
    #search-container {
        height: auto;
        padding: 1;
        background: $panel;
    }
    
    #library-tabs {
        height: auto;
        width: 100%;
        padding: 0 1;
        background: $surface;
    }
    
    #library-tabs Label {
        padding: 0 2;
    }
    
    .tab-active {
        background: $primary;
        color: $text;
        text-style: bold;
    }
    
    .tab-inactive {
        color: $text-muted;
    }
    
    Input {
        width: 100%;
    }
    
    #list-container {
        height: 1fr;
    }
    
    ListView {
        width: 100%;
        height: 100%;
    }
    
    ListItem {
        padding: 0 1;
    }
    
    ListItem > Label {
        width: 100%;
    }
    
    ListItem > Horizontal {
        width: 100%;
        height: auto;
    }
    
    .item-title {
        width: 1fr;
    }
    
    .item-runtime {
        width: auto;
        text-align: right;
        color: $text-muted;
    }
    """
    
    BINDINGS = [
        Binding("ctrl+c,q", "quit", "Quit", show=True),
        Binding("enter", "select", "Select/Play", show=True),
        Binding("escape,backspace", "back", "Back", show=False),
        Binding("ctrl+f,/", "focus_search", "Search", show=True),
        Binding("s", "settings", "Settings", show=True),
        Binding("w", "toggle_watched", "Watched", show=True),
        Binding("l", "cycle_library", "Library", show=True),
        Binding("i", "show_details", "Details", show=True),
        Binding("p", "play", "Play", show=True),
        Binding("r", "switch_server", "Server", show=True),
    ]
    
    def __init__(self, server_url: str, api_key: str, user_id: str, 
                 drm_connector: str = "", drm_mode: str = "", audio_device: str = "",
                 audio_spdif: str = "", server_name: str = "", all_server_names: list[str] | None = None):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.drm_connector = drm_connector
        self.drm_mode = drm_mode
        self.audio_device = audio_device
        self.audio_spdif = audio_spdif
        self.server_name = server_name
        self.all_server_names = all_server_names or []
        
        # State
        self.all_items = []
        self.list_view = None
        self.library_tabs = None
        self.navigation_stack = []
        self.current_view = "library"
        
        # HTTP session for connection pooling
        self._session = requests.Session()
        self._session.headers["X-MediaBrowser-Token"] = api_key
        
        # Library state
        self.libraries = []
        self.current_library_index = 0
        
        # Series navigation state
        self.current_series_id = None
        self.current_series_name = None
        self.current_season_id = None
        
        # Collection (BoxSet) navigation state
        self.current_collection_id = None
        self.current_collection_name = None
        
        # Settings state (cached)
        self.drm_connectors = {}
        self.audio_devices = []
        self.selected_connector_for_modes = None
        
        # Detail view state
        self.detail_item = None
        self.detail_media_streams = []
        self.selected_video_track = None
        self.selected_audio_track = None
        self.selected_subtitle_track = None
        # Temporary settings for detail view (not saved to config)
        self.detail_drm_connector = None  # None means use default
        self.detail_drm_mode = None
        self.detail_audio_device = None
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(id="library-tabs")
        with Container(id="search-container"):
            yield Input(placeholder="Search titles...", id="search")
        with Container(id="list-container"):
            yield ListView(id="item-list")
        yield Footer()
    
    # ========================================================================
    # Lifecycle
    # ========================================================================
    
    def on_mount(self) -> None:
        """Load items from Jellyfin on mount"""
        self.title = "Jellyfin Selector"
        if self.server_name:
            self.sub_title = f"[{self.server_name}] Loading..."
        else:
            self.sub_title = "Loading..."
        
        self.list_view = self.query_one("#item-list", ListView)
        self.library_tabs = self.query_one("#library-tabs", Horizontal)
        
        try:
            self.libraries = self._fetch_libraries()
            self._update_library_tabs()
            self.all_items = self._fetch_library_items()
            self._populate_list(self.all_items)
            prefix = f"[{self.server_name}] " if self.server_name else ""
            self.sub_title = f"{prefix}{len(self.all_items)} items"
            self.list_view.focus()
            
            # Preload DRM/audio caches in background (non-blocking)
            self._preload_device_caches()
        except Exception as e:
            self.sub_title = f"Error: {str(e)}"
    
    def _preload_device_caches(self) -> None:
        """Preload DRM and audio device caches"""
        # These will be cached for later use in detail view
        if not self.drm_connectors:
            self.drm_connectors = parse_drm_modes()
        if not self.audio_devices:
            self.audio_devices = parse_audio_devices()
    
    # ========================================================================
    # API Methods
    # ========================================================================
    
    def _fetch_libraries(self) -> list[dict]:
        """Fetch available media libraries from Jellyfin"""
        response = self._session.get(f"{self.server_url}/Users/{self.user_id}/Views")
        response.raise_for_status()
        
        libraries = []
        for item in response.json().get("Items", []):
            collection_type = item.get("CollectionType", "")
            if collection_type in ("movies", "tvshows", "mixed", "boxsets", ""):
                libraries.append({
                    "Id": item.get("Id"),
                    "Name": item.get("Name"),
                    "CollectionType": collection_type
                })
        return libraries
    
    def _fetch_library_items(self) -> list[dict]:
        """Fetch all movies and shows from the current library"""
        params = {
            "IncludeItemTypes": "Movie,Series,BoxSet",
            "Recursive": "true",
            "Fields": "ProductionYear,Overview,RunTimeTicks,UserData",
            "SortBy": "SortName",
            "SortOrder": "Ascending"
        }
        
        if self.libraries and 0 <= self.current_library_index < len(self.libraries):
            params["ParentId"] = self.libraries[self.current_library_index]["Id"]
        
        response = self._session.get(
            f"{self.server_url}/Users/{self.user_id}/Items",
            params=params
        )
        response.raise_for_status()
        return response.json().get("Items", [])
    
    def _fetch_seasons(self, series_id: str) -> list[dict]:
        """Fetch seasons for a series"""
        response = self._session.get(
            f"{self.server_url}/Shows/{series_id}/Seasons",
            params={"userId": self.user_id, "Fields": "Overview,UserData"}
        )
        response.raise_for_status()
        return response.json().get("Items", [])
    
    def _fetch_episodes(self, series_id: str, season_id: str) -> list[dict]:
        """Fetch episodes for a season"""
        response = self._session.get(
            f"{self.server_url}/Shows/{series_id}/Episodes",
            params={"seasonId": season_id, "userId": self.user_id, "Fields": "Overview,RunTimeTicks,UserData"}
        )
        response.raise_for_status()
        return response.json().get("Items", [])
    
    def _fetch_item(self, item_id: str) -> dict | None:
        """Fetch fresh item data from the server"""
        response = self._session.get(
            f"{self.server_url}/Users/{self.user_id}/Items/{item_id}",
            params={"Fields": "ProductionYear,Overview,RunTimeTicks,UserData"}
        )
        if response.status_code == 200:
            return response.json()
        return None
    
    def _fetch_media_info(self, item_id: str) -> list[dict]:
        """Fetch media stream info for an item"""
        response = self._session.get(
            f"{self.server_url}/Items/{item_id}/PlaybackInfo",
            params={"UserId": self.user_id}
        )
        if response.status_code == 200:
            media_sources = response.json().get("MediaSources", [])
            if media_sources:
                return media_sources[0].get("MediaStreams", [])
        return []
    
    def _fetch_collection_items(self, collection_id: str) -> list[dict]:
        """Fetch items inside a BoxSet/collection"""
        response = self._session.get(
            f"{self.server_url}/Users/{self.user_id}/Items",
            params={
                "ParentId": collection_id,
                "Fields": "ProductionYear,Overview,RunTimeTicks,UserData",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
            }
        )
        response.raise_for_status()
        return response.json().get("Items", [])
    
    def _set_played_status(self, item_id: str, played: bool) -> None:
        """Set the played status for an item"""
        url = f"{self.server_url}/Users/{self.user_id}/PlayedItems/{item_id}"
        if played:
            self._session.post(url).raise_for_status()
        else:
            self._session.delete(url).raise_for_status()
    
    # ========================================================================
    # UI Update Methods
    # ========================================================================
    
    def _update_library_tabs(self) -> None:
        """Update the library tabs display"""
        self.library_tabs.remove_children()
        for i, lib in enumerate(self.libraries):
            name = lib.get("Name", "Unknown")
            classes = "tab-active" if i == self.current_library_index else "tab-inactive"
            self.library_tabs.mount(Label(f" {name} ", classes=classes))
    
    def _populate_list(self, items: list[dict]) -> None:
        """Populate the ListView with items"""
        self.list_view.clear()
        
        for item in items:
            title_text, right_info = format_item_display(item)
            list_item = ListItem(Horizontal(
                Label(title_text, classes="item-title"),
                Label(right_info, classes="item-runtime")
            ))
            list_item.item_data = item
            self.list_view.append(list_item)
        
        if items:
            self.list_view.index = None
            self.call_after_refresh(self._select_first_item)
    
    def _select_first_item(self) -> None:
        """Select the first item after refresh"""
        if self.list_view and len(self.list_view) > 0:
            self.list_view.index = 0
    
    def _restore_index(self, index: int) -> None:
        """Restore a specific index in the list view"""
        if self.list_view and len(self.list_view) > index:
            self.list_view.index = index
    
    def _refresh_current_item(self, list_item: ListItem, item: dict) -> None:
        """Refresh the display of a single list item"""
        title_text, right_info = format_item_display(item)
        horizontal = list_item.query_one(Horizontal)
        labels = list(horizontal.query(Label))
        if len(labels) >= 2:
            labels[0].update(title_text)
            labels[1].update(right_info)
    
    # ========================================================================
    # Settings UI
    # ========================================================================
    
    def _populate_settings(self) -> None:
        """Populate the settings menu"""
        self.list_view.clear()
        chars = _get_chars()
        
        # DRM Output
        if self.drm_connector and self.drm_mode:
            drm_status = f"{self.drm_connector} mode {self.drm_mode}"
        elif self.drm_connector:
            drm_status = self.drm_connector
        else:
            drm_status = "Not configured"
        
        drm_item = ListItem(Label(f"{chars['display']} DRM Output: {drm_status}"))
        drm_item.item_data = {"Type": "SettingsMenu", "menu": "drm"}
        self.list_view.append(drm_item)
        
        # Audio Device
        audio_status = self.audio_device if self.audio_device else "auto"
        audio_item = ListItem(Label(f"{chars['audio']} Audio Device: {audio_status}"))
        audio_item.item_data = {"Type": "SettingsMenu", "menu": "audio"}
        self.list_view.append(audio_item)
        
        # Audio Passthrough (SPDIF)
        spdif_status = self.audio_spdif if self.audio_spdif else "disabled"
        spdif_item = ListItem(Label(f"{chars['audio']} Audio Passthrough: {spdif_status}"))
        spdif_item.item_data = {"Type": "SettingsMenu", "menu": "spdif"}
        self.list_view.append(spdif_item)
    
    def _populate_drm_settings(self) -> None:
        """Populate DRM settings options"""
        self.list_view.clear()
        chars = _get_chars()
        
        # Current setting
        if self.drm_connector and self.drm_mode:
            current = f"Current: {self.drm_connector} mode {self.drm_mode}"
        elif self.drm_connector:
            current = f"Current: {self.drm_connector} (no mode set)"
        else:
            current = "Current: No DRM output configured"
        
        current_item = ListItem(Label(f"{chars['settings']} {current}"))
        current_item.item_data = {"Type": "SettingInfo"}
        self.list_view.append(current_item)
        
        # Clear option
        clear_item = ListItem(Label(f"{chars['clear']} Clear DRM settings"))
        clear_item.item_data = {"Type": "ClearDRM"}
        self.list_view.append(clear_item)
        
        # Separator
        sep_item = ListItem(Label(chars["separator"]))
        sep_item.item_data = {"Type": "Separator"}
        self.list_view.append(sep_item)
        
        # Load connectors if not cached
        if not self.drm_connectors:
            self.drm_connectors = parse_drm_modes()
        
        if "_error" in self.drm_connectors:
            error_item = ListItem(Label(f"{chars['error']} Error: {self.drm_connectors['_error']}"))
            error_item.item_data = {"Type": "Error"}
            self.list_view.append(error_item)
            return
        
        if not self.drm_connectors:
            info_item = ListItem(Label(f"{chars['info']} No DRM connectors found"))
            info_item.item_data = {"Type": "Info"}
            self.list_view.append(info_item)
            return
        
        watched = chars["watched"]
        connector_char = chars["connector"]
        for name, info in self.drm_connectors.items():
            mode_count = len(info.get("modes", []))
            marker = f"{watched} " if name == self.drm_connector else "  "
            
            item = ListItem(Label(f"{marker}{connector_char} {name} ({mode_count} modes)"))
            item.item_data = {"Type": "Connector", "Name": name, "modes": info.get("modes", [])}
            self.list_view.append(item)
    
    def _populate_audio_settings(self) -> None:
        """Populate audio device options"""
        self.list_view.clear()
        chars = _get_chars()
        
        # Current setting
        current = f"Current: {self.audio_device}" if self.audio_device else "Current: auto (default)"
        current_item = ListItem(Label(f"{chars['settings']} {current}"))
        current_item.item_data = {"Type": "SettingInfo"}
        self.list_view.append(current_item)
        
        # Clear option
        clear_item = ListItem(Label(f"{chars['clear']} Clear audio device (use auto)"))
        clear_item.item_data = {"Type": "ClearAudio"}
        self.list_view.append(clear_item)
        
        # Separator
        sep_item = ListItem(Label(chars["separator"]))
        sep_item.item_data = {"Type": "Separator"}
        self.list_view.append(sep_item)
        
        # Load devices if not cached
        if not self.audio_devices:
            self.audio_devices = parse_audio_devices()
        
        if self.audio_devices and "_error" in self.audio_devices[0]:
            error_item = ListItem(Label(f"{chars['error']} Error: {self.audio_devices[0]['_error']}"))
            error_item.item_data = {"Type": "Error"}
            self.list_view.append(error_item)
            return
        
        if not self.audio_devices:
            info_item = ListItem(Label(f"{chars['info']} No audio devices found"))
            info_item.item_data = {"Type": "Info"}
            self.list_view.append(info_item)
            return
        
        watched = chars["watched"]
        audio_char = chars["audio"]
        for device in self.audio_devices:
            device_id = device.get("id", "")
            description = device.get("description", "")
            marker = f"{watched} " if device_id == self.audio_device else "  "
            display = f"{marker}{audio_char} {device_id}"
            if description:
                display += f" ({description})"
            
            item = ListItem(Label(display))
            item.item_data = {"Type": "AudioDevice", "id": device_id}
            self.list_view.append(item)
    
    # SPDIF codec options for audio passthrough
    SPDIF_CODECS = [
        ("ac3", "AC-3 (Dolby Digital)"),
        ("eac3", "E-AC-3 (Dolby Digital Plus / Atmos)"),
        ("truehd", "TrueHD (Dolby TrueHD / Atmos)"),
        ("dts", "DTS"),
        ("dts-hd", "DTS-HD MA"),
    ]
    
    def _populate_spdif_settings(self) -> None:
        """Populate audio passthrough (SPDIF) codec options"""
        self.list_view.clear()
        chars = _get_chars()
        
        # Current setting
        current = f"Current: {self.audio_spdif}" if self.audio_spdif else "Current: disabled"
        current_item = ListItem(Label(f"{chars['settings']} {current}"))
        current_item.item_data = {"Type": "SettingInfo"}
        self.list_view.append(current_item)
        
        # Clear option
        clear_item = ListItem(Label(f"{chars['clear']} Disable passthrough (decode all)"))
        clear_item.item_data = {"Type": "ClearSPDIF"}
        self.list_view.append(clear_item)
        
        # Enable all
        all_codecs = ",".join(c[0] for c in self.SPDIF_CODECS)
        is_all = self.audio_spdif == all_codecs
        all_marker = f"{chars['watched']} " if is_all else "  "
        all_item = ListItem(Label(f"{all_marker}{chars['audio']} Enable all passthrough"))
        all_item.item_data = {"Type": "SPDIFAll"}
        self.list_view.append(all_item)
        
        # Separator
        sep_item = ListItem(Label(chars["separator"]))
        sep_item.item_data = {"Type": "Separator"}
        self.list_view.append(sep_item)
        
        # Info
        info_item = ListItem(Label(f"{chars['info']} Toggle individual codecs:"))
        info_item.item_data = {"Type": "Info"}
        self.list_view.append(info_item)
        
        # Individual codec toggles
        enabled_codecs = set(self.audio_spdif.split(",")) if self.audio_spdif else set()
        
        for codec_id, codec_name in self.SPDIF_CODECS:
            is_enabled = codec_id in enabled_codecs
            marker = f"{chars['watched']} " if is_enabled else "  "
            
            item = ListItem(Label(f"{marker}{chars['audio']} {codec_name}"))
            item.item_data = {"Type": "SPDIFCodec", "id": codec_id, "name": codec_name}
            self.list_view.append(item)
    
    def _populate_modes(self, connector_name: str, modes: list[dict]) -> None:
        """Populate modes for a connector"""
        self.list_view.clear()
        chars = _get_chars()
        
        if not modes:
            info_item = ListItem(Label(f"{chars['info']} No modes available for this connector"))
            info_item.item_data = {"Type": "Info"}
            self.list_view.append(info_item)
            return
        
        watched = chars["watched"]
        display_char = chars["display"]
        for mode in modes:
            mode_id = mode.get("id", "?")
            display_text = mode.get("display", f"Mode {mode_id}")
            is_current = connector_name == self.drm_connector and mode_id == self.drm_mode
            marker = f"{watched} " if is_current else "  "
            
            item = ListItem(Label(f"{marker}{display_char} {display_text}"))
            item.item_data = {"Type": "Mode", "id": mode_id, "connector": connector_name, **mode}
            self.list_view.append(item)
    
    # ========================================================================
    # Server Switcher UI
    # ========================================================================

    def _populate_server_list(self) -> None:
        """Populate the server switcher list"""
        self.list_view.clear()
        chars = _get_chars()

        if not self.all_server_names:
            info_item = ListItem(Label(f"{chars['info']} No other servers configured"))
            info_item.item_data = {"Type": "Info"}
            self.list_view.append(info_item)
            return

        for name in self.all_server_names:
            is_active = name == self.server_name
            marker = f"{chars['watched']} " if is_active else "  "
            label_text = f"{marker}{chars['server']} {name}"
            if is_active:
                label_text += " (active)"

            item = ListItem(Label(label_text))
            item.item_data = {"Type": "ServerEntry", "server_name": name, "is_active": is_active}
            self.list_view.append(item)

    # ========================================================================
    # Detail View UI
    # ========================================================================
    
    def _populate_details(self, preserve_index: int | None = None) -> None:
        """Populate the detail view with track selection"""
        self.list_view.clear()
        
        if not self.detail_item:
            return
        
        # Get cached character set once
        chars = _get_chars()
        
        # Group streams by type
        video_streams = []
        audio_streams = []
        subtitle_streams = []
        
        for stream in self.detail_media_streams:
            stream_type = stream.get("Type", "")
            if stream_type == "Video":
                video_streams.append(stream)
            elif stream_type == "Audio":
                audio_streams.append(stream)
            elif stream_type == "Subtitle":
                subtitle_streams.append(stream)
        
        selected_char = chars["selected"]
        unselected_char = chars["unselected"]
        
        def marker(selected: bool) -> str:
            return f"{selected_char} " if selected else f"{unselected_char} "
        
        # Play button
        play_item = ListItem(Horizontal(
            Label(f"{chars['play']}  Play with selected tracks", classes="item-title"),
            Label("", classes="item-runtime")
        ))
        play_item.item_data = {"Type": "PlayWithTracks"}
        self.list_view.append(play_item)
        
        # Separator
        sep_item = ListItem(Label(chars["separator"]))
        sep_item.item_data = {"Type": "Separator"}
        self.list_view.append(sep_item)
        
        # Video tracks
        if video_streams:
            header_item = ListItem(Label("VIDEO TRACKS"))
            header_item.item_data = {"Type": "Header"}
            self.list_view.append(header_item)
            
            for stream in video_streams:
                index = stream.get("Index")
                codec = stream.get("Codec", "unknown").upper()
                width = stream.get("Width", 0)
                height = stream.get("Height", 0)
                resolution = f"{width}x{height}" if width and height else ""
                
                # Get FPS - try multiple fields
                fps = ""
                if stream.get("RealFrameRate"):
                    fps = f"{stream['RealFrameRate']:.2f}fps"
                elif stream.get("AverageFrameRate"):
                    fps = f"{stream['AverageFrameRate']:.2f}fps"
                
                parts = [codec]
                if resolution:
                    parts.append(resolution)
                if fps:
                    parts.append(fps)
                
                item = ListItem(Horizontal(
                    Label(f"  {marker(index == self.selected_video_track)}{' / '.join(parts)}", classes="item-title"),
                    Label("", classes="item-runtime")
                ))
                item.item_data = {"Type": "VideoTrack", "Index": index, "stream": stream}
                self.list_view.append(item)
        
        # Audio tracks
        if audio_streams:
            header_item = ListItem(Label("AUDIO TRACKS"))
            header_item.item_data = {"Type": "Header"}
            self.list_view.append(header_item)
            
            for stream in audio_streams:
                index = stream.get("Index")
                codec = stream.get("Codec", "unknown").upper()
                language = stream.get("Language", "")
                channels = stream.get("Channels", 0)
                
                parts = []
                if language:
                    parts.append(language.upper())
                if codec:
                    parts.append(codec)
                if channels == 2:
                    parts.append("Stereo")
                elif channels == 6:
                    parts.append("5.1")
                elif channels == 8:
                    parts.append("7.1")
                elif channels:
                    parts.append(f"{channels}ch")
                
                item = ListItem(Horizontal(
                    Label(f"  {marker(index == self.selected_audio_track)}{' / '.join(parts)}", classes="item-title"),
                    Label("", classes="item-runtime")
                ))
                item.item_data = {"Type": "AudioTrack", "Index": index, "stream": stream}
                self.list_view.append(item)
        
        # Subtitle tracks
        header_item = ListItem(Label("SUBTITLE TRACKS"))
        header_item.item_data = {"Type": "Header"}
        self.list_view.append(header_item)
        
        # None option
        none_item = ListItem(Horizontal(
            Label(f"  {marker(self.selected_subtitle_track is None)}None (disabled)", classes="item-title"),
            Label("", classes="item-runtime")
        ))
        none_item.item_data = {"Type": "SubtitleTrack", "Index": None}
        self.list_view.append(none_item)
        
        for stream in subtitle_streams:
            index = stream.get("Index")
            language = stream.get("Language", "")
            codec = stream.get("Codec", "")
            is_forced = stream.get("IsForced", False)
            
            parts = []
            if language:
                parts.append(language.upper())
            if codec:
                parts.append(codec.upper())
            if is_forced:
                parts.append("Forced")
            
            desc = " / ".join(parts) if parts else f"Track {index}"
            item = ListItem(Horizontal(
                Label(f"  {marker(index == self.selected_subtitle_track)}{desc}", classes="item-title"),
                Label("", classes="item-runtime")
            ))
            item.item_data = {"Type": "SubtitleTrack", "Index": index, "stream": stream}
            self.list_view.append(item)
        
        # Separator before output settings
        sep_item = ListItem(Label(chars["separator"]))
        sep_item.item_data = {"Type": "Separator"}
        self.list_view.append(sep_item)
        
        # DRM Output section
        header_item = ListItem(Label("DRM OUTPUT (for this playback)"))
        header_item.item_data = {"Type": "Header"}
        self.list_view.append(header_item)
        
        # Determine effective DRM settings
        eff_connector = self.detail_drm_connector if self.detail_drm_connector is not None else self.drm_connector
        eff_mode = self.detail_drm_mode if self.detail_drm_mode is not None else self.drm_mode
        
        # "Use default" option
        is_default_drm = self.detail_drm_connector is None
        default_desc = f"Default ({self.drm_connector} mode {self.drm_mode})" if self.drm_connector else "Default (none)"
        default_item = ListItem(Horizontal(
            Label(f"  {marker(is_default_drm)}{default_desc}", classes="item-title"),
            Label("", classes="item-runtime")
        ))
        default_item.item_data = {"Type": "DetailDRMDefault"}
        self.list_view.append(default_item)
        
        # List available DRM modes (flattened - connector + mode together)
        if "_error" not in self.drm_connectors:
            for connector_name, connector_info in self.drm_connectors.items():
                for mode in connector_info.get("modes", []):
                    mode_id = mode.get("id", "?")
                    display = mode.get("display", f"Mode {mode_id}")
                    is_selected = (not is_default_drm and 
                                   eff_connector == connector_name and 
                                   eff_mode == mode_id)
                    
                    item = ListItem(Horizontal(
                        Label(f"  {marker(is_selected)}{connector_name}: {display}", classes="item-title"),
                        Label("", classes="item-runtime")
                    ))
                    item.item_data = {
                        "Type": "DetailDRMMode",
                        "connector": connector_name,
                        "mode_id": mode_id
                    }
                    self.list_view.append(item)
        
        # Audio Device section
        header_item = ListItem(Label("AUDIO DEVICE (for this playback)"))
        header_item.item_data = {"Type": "Header"}
        self.list_view.append(header_item)
        
        # Determine effective audio device
        eff_audio = self.detail_audio_device if self.detail_audio_device is not None else self.audio_device
        
        # "Use default" option
        is_default_audio = self.detail_audio_device is None
        default_desc = f"Default ({self.audio_device})" if self.audio_device else "Default (auto)"
        default_item = ListItem(Horizontal(
            Label(f"  {marker(is_default_audio)}{default_desc}", classes="item-title"),
            Label("", classes="item-runtime")
        ))
        default_item.item_data = {"Type": "DetailAudioDefault"}
        self.list_view.append(default_item)
        
        # List available audio devices
        if self.audio_devices and "_error" not in self.audio_devices[0]:
            for device in self.audio_devices:
                device_id = device.get("id", "")
                description = device.get("description", "")
                is_selected = not is_default_audio and eff_audio == device_id
                
                display = f"{description}" if description else device_id
                item = ListItem(Horizontal(
                    Label(f"  {marker(is_selected)}{display}", classes="item-title"),
                    Label("", classes="item-runtime")
                ))
                item.item_data = {"Type": "DetailAudioDevice", "id": device_id}
                self.list_view.append(item)
        
        # Restore index
        if self.list_view.children:
            if preserve_index is not None:
                self.list_view.index = None
                self.call_after_refresh(lambda: self._restore_index(preserve_index))
            else:
                self.list_view.index = None
                self.call_after_refresh(self._select_first_item)
    
    # ========================================================================
    # Event Handlers
    # ========================================================================
    
    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter list when search input changes"""
        if self.current_view != "library":
            return
        
        query = event.value.lower()
        if not query:
            filtered = self.all_items
        else:
            filtered = [item for item in self.all_items if query in item.get("Name", "").lower()]
        
        self._populate_list(filtered)
        self.sub_title = f"{len(filtered)} items"
    
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle ListView selection (Enter key)"""
        if not event.item or not hasattr(event.item, 'item_data'):
            return
        self._handle_item_selection(event.item.item_data)
    
    def _handle_item_selection(self, item: dict) -> None:
        """Handle selection of an item (shared by event handler and action)"""
        item_type = item.get("Type", "")
        
        if item_type == "Series":
            self._navigate_to_seasons(item)
        elif item_type == "BoxSet":
            self._navigate_to_collection(item)
        elif item_type == "Season":
            self._navigate_to_episodes(item)
        elif item_type in ("Episode", "Movie"):
            self._play_item(item)
        elif item_type == "SettingsMenu":
            self._navigate_to_settings_submenu(item)
        elif item_type == "Connector":
            self._navigate_to_modes(item)
        elif item_type == "Mode":
            self._select_mode(item)
        elif item_type == "ClearDRM":
            self._clear_drm_settings()
        elif item_type == "AudioDevice":
            self._select_audio_device(item)
        elif item_type == "ClearAudio":
            self._clear_audio_settings()
        elif item_type == "ClearSPDIF":
            self._clear_spdif_settings()
        elif item_type == "SPDIFAll":
            self._enable_all_spdif()
        elif item_type == "SPDIFCodec":
            self._toggle_spdif_codec(item.get("id", ""))
        elif item_type == "PlayWithTracks":
            self._play_with_tracks()
        elif item_type == "VideoTrack":
            self.selected_video_track = item.get("Index")
            self._populate_details(preserve_index=self.list_view.index)
        elif item_type == "AudioTrack":
            self.selected_audio_track = item.get("Index")
            self._populate_details(preserve_index=self.list_view.index)
        elif item_type == "SubtitleTrack":
            self.selected_subtitle_track = item.get("Index")
            self._populate_details(preserve_index=self.list_view.index)
        # Detail view DRM/audio selections (temporary, not saved)
        elif item_type == "DetailDRMDefault":
            self.detail_drm_connector = None
            self.detail_drm_mode = None
            self._populate_details(preserve_index=self.list_view.index)
        elif item_type == "DetailDRMMode":
            self.detail_drm_connector = item.get("connector")
            self.detail_drm_mode = item.get("mode_id")
            self._populate_details(preserve_index=self.list_view.index)
        elif item_type == "DetailAudioDefault":
            self.detail_audio_device = None
            self._populate_details(preserve_index=self.list_view.index)
        elif item_type == "DetailAudioDevice":
            self.detail_audio_device = item.get("id")
            self._populate_details(preserve_index=self.list_view.index)
        # Server switcher
        elif item_type == "ServerEntry":
            target_name = item.get("server_name", "")
            if target_name and not item.get("is_active", False):
                self.exit(_ServerSwitch(target_name))
    
    # ========================================================================
    # Navigation Methods
    # ========================================================================
    
    def _navigate_to_seasons(self, series: dict) -> None:
        """Navigate into a series"""
        series_id = series["Id"]
        series_name = series.get("Name", "Unknown")
        
        try:
            seasons = self._fetch_seasons(series_id)
            nav_entry = {"view": self.current_view, "title": self.title}
            if self.current_view == "collection":
                nav_entry["collection_id"] = self.current_collection_id
                nav_entry["collection_name"] = self.current_collection_name
            self.navigation_stack.append(nav_entry)
            self.current_view = "seasons"
            self.current_series_id = series_id
            self.current_series_name = series_name
            
            for season in seasons:
                season["series_id"] = series_id
            
            self._populate_list(seasons)
            self.title = series_name
            self.sub_title = f"{len(seasons)} seasons"
            self.list_view.focus()
        except Exception as e:
            self.sub_title = f"Error loading seasons: {e}"
    
    def _navigate_to_episodes(self, season: dict) -> None:
        """Navigate into a season"""
        season_id = season["Id"]
        series_id = season.get("series_id", season.get("SeriesId", ""))
        season_name = season.get("Name", "Unknown")
        
        try:
            episodes = self._fetch_episodes(series_id, season_id)
            self.navigation_stack.append({
                "view": self.current_view,
                "series_id": self.current_series_id,
                "series_name": self.current_series_name,
                "title": self.title
            })
            self.current_view = "episodes"
            self.current_season_id = season_id
            self._populate_list(episodes)
            self.title = f"{self.current_series_name} - {season_name}"
            self.sub_title = f"{len(episodes)} episodes"
            self.list_view.focus()
        except Exception as e:
            self.sub_title = f"Error loading episodes: {e}"
    
    def _navigate_to_collection(self, boxset: dict) -> None:
        """Navigate into a BoxSet/collection"""
        collection_id = boxset["Id"]
        collection_name = boxset.get("Name", "Unknown")
        
        try:
            items = self._fetch_collection_items(collection_id)
            self.navigation_stack.append({"view": self.current_view, "title": self.title})
            self.current_view = "collection"
            self.current_collection_id = collection_id
            self.current_collection_name = collection_name
            self._populate_list(items)
            self.title = collection_name
            self.sub_title = f"{len(items)} items"
            self.list_view.focus()
        except Exception as e:
            self.sub_title = f"Error loading collection: {e}"
    
    def _navigate_to_settings_submenu(self, item: dict) -> None:
        """Navigate to a settings submenu"""
        menu = item.get("menu", "")
        self.navigation_stack.append({"view": "settings", "title": "Settings"})
        
        if menu == "drm":
            self.current_view = "settings_drm"
            self.title = "Settings - DRM Output"
            self._populate_drm_settings()
            self.sub_title = "Select a connector"
        elif menu == "audio":
            self.current_view = "settings_audio"
            self.title = "Settings - Audio Device"
            self._populate_audio_settings()
            self.sub_title = "Select an audio device"
        elif menu == "spdif":
            self.current_view = "settings_spdif"
            self.title = "Settings - Audio Passthrough"
            self._populate_spdif_settings()
            self.sub_title = "Toggle passthrough codecs"
        
        self.list_view.focus()
    
    def _navigate_to_modes(self, connector: dict) -> None:
        """Navigate to connector modes"""
        connector_name = connector.get("Name", "")
        modes = connector.get("modes", [])
        
        self.navigation_stack.append({"view": "settings_drm", "title": "Settings - DRM Output"})
        self.current_view = "settings_drm_modes"
        self.selected_connector_for_modes = connector_name
        self._populate_modes(connector_name, modes)
        self.title = f"Settings - {connector_name}"
        self.sub_title = f"{len(modes)} modes"
        self.list_view.focus()
    
    # ========================================================================
    # Settings Actions
    # ========================================================================
    
    def _select_mode(self, mode: dict) -> None:
        """Select a DRM mode"""
        self.drm_connector = mode.get("connector", "")
        self.drm_mode = mode.get("id", "")
        self._save_settings()
        self.sub_title = f"Selected: {self.drm_connector} mode {self.drm_mode}"
        
        if self.selected_connector_for_modes and self.selected_connector_for_modes in self.drm_connectors:
            modes = self.drm_connectors[self.selected_connector_for_modes].get("modes", [])
            self._populate_modes(self.selected_connector_for_modes, modes)
    
    def _clear_drm_settings(self) -> None:
        """Clear DRM settings"""
        self.drm_connector = ""
        self.drm_mode = ""
        self._save_settings()
        self.sub_title = "DRM settings cleared"
        self._populate_drm_settings()
    
    def _select_audio_device(self, device: dict) -> None:
        """Select an audio device"""
        self.audio_device = device.get("id", "")
        self._save_settings()
        self.sub_title = f"Selected: {self.audio_device}"
        self._populate_audio_settings()
    
    def _clear_audio_settings(self) -> None:
        """Clear audio device settings"""
        self.audio_device = ""
        self._save_settings()
        self.sub_title = "Audio device cleared (using auto)"
        self._populate_audio_settings()
    
    def _clear_spdif_settings(self) -> None:
        """Clear SPDIF passthrough settings"""
        self.audio_spdif = ""
        self._save_settings()
        self.sub_title = "Audio passthrough disabled"
        self._populate_spdif_settings()
    
    def _enable_all_spdif(self) -> None:
        """Enable all SPDIF codecs"""
        self.audio_spdif = ",".join(c[0] for c in self.SPDIF_CODECS)
        self._save_settings()
        self.sub_title = f"Passthrough: {self.audio_spdif}"
        self._populate_spdif_settings()
    
    def _toggle_spdif_codec(self, codec_id: str) -> None:
        """Toggle a single SPDIF codec"""
        enabled = set(self.audio_spdif.split(",")) if self.audio_spdif else set()
        
        if codec_id in enabled:
            enabled.discard(codec_id)
        else:
            enabled.add(codec_id)
        
        # Preserve canonical order from SPDIF_CODECS
        ordered = [c[0] for c in self.SPDIF_CODECS if c[0] in enabled]
        self.audio_spdif = ",".join(ordered)
        self._save_settings()
        self.sub_title = f"Passthrough: {self.audio_spdif}" if self.audio_spdif else "Passthrough disabled"
        self._populate_spdif_settings()
    
    def _save_settings(self) -> None:
        """Save MPV settings to config file"""
        config = load_config()
        
        if "mpv" not in config:
            config["mpv"] = {}
        
        config["mpv"]["drm_connector"] = self.drm_connector
        config["mpv"]["drm_mode"] = self.drm_mode
        config["mpv"]["audio_device"] = self.audio_device
        config["mpv"]["audio_spdif"] = self.audio_spdif
        
        save_config(config)
    
    # ========================================================================
    # Playback
    # ========================================================================
    
    def _play_item(self, item: dict) -> None:
        """Play a movie or episode"""
        item_id = item["Id"]
        stream_url = f"{self.server_url}/Items/{item_id}/Download?api_key={self.api_key}"
        self.exit((stream_url, self.drm_connector, self.drm_mode, self.audio_device, self.audio_spdif))
    
    def _play_with_tracks(self) -> None:
        """Play with selected tracks"""
        if not self.detail_item:
            return
        
        item_id = self.detail_item["Id"]
        stream_url = f"{self.server_url}/Items/{item_id}/Download?api_key={self.api_key}"
        
        # Use temporary settings if set, otherwise fall back to defaults
        drm_connector = self.detail_drm_connector if self.detail_drm_connector is not None else self.drm_connector
        drm_mode = self.detail_drm_mode if self.detail_drm_mode is not None else self.drm_mode
        audio_device = self.detail_audio_device if self.detail_audio_device is not None else self.audio_device
        
        track_args = []
        if self.selected_video_track is not None:
            track_args.append(f"--vid={self.selected_video_track + 1}")
        if self.selected_audio_track is not None:
            track_args.append(f"--aid={self.selected_audio_track + 1}")
        if self.selected_subtitle_track is not None:
            track_args.append(f"--sid={self.selected_subtitle_track + 1}")
        else:
            track_args.append("--sid=no")
        
        self.exit((stream_url, drm_connector, drm_mode, audio_device, self.audio_spdif, track_args))
    
    # ========================================================================
    # Actions (Key Bindings)
    # ========================================================================
    
    def action_select(self) -> None:
        """Handle select action"""
        if not self.list_view or self.list_view.index is None:
            return
        
        highlighted = self.list_view.highlighted_child
        if highlighted and hasattr(highlighted, 'item_data'):
            self._handle_item_selection(highlighted.item_data)
    
    def action_back(self) -> None:
        """Navigate back"""
        if not self.navigation_stack:
            return
        
        previous = self.navigation_stack.pop()
        self.current_view = previous["view"]
        self.title = previous["title"]
        
        # Clear detail state
        self.detail_item = None
        self.detail_media_streams = []
        
        if self.current_view == "library":
            self.current_series_id = None
            self.current_series_name = None
            self.current_season_id = None
            self.current_collection_id = None
            self.current_collection_name = None
            try:
                self.all_items = self._fetch_library_items()
                self._populate_list(self.all_items)
                self.sub_title = f"{len(self.all_items)} items"
            except Exception as e:
                self._populate_list(self.all_items)
                self.sub_title = f"Refresh failed: {e}"
        elif self.current_view == "seasons":
            series_id = previous.get("series_id")
            series_name = previous.get("series_name")
            if series_id:
                self.current_series_id = series_id
                self.current_series_name = series_name
                self.current_season_id = None
                try:
                    seasons = self._fetch_seasons(series_id)
                    for season in seasons:
                        season["series_id"] = series_id
                    self._populate_list(seasons)
                    self.sub_title = f"{len(seasons)} seasons"
                except Exception as e:
                    self.sub_title = f"Error: {e}"
        elif self.current_view == "episodes":
            series_id = previous.get("series_id")
            series_name = previous.get("series_name")
            season_id = self.current_season_id
            if series_id and season_id:
                self.current_series_id = series_id
                self.current_series_name = series_name
                try:
                    episodes = self._fetch_episodes(series_id, season_id)
                    self._populate_list(episodes)
                    self.sub_title = f"{len(episodes)} episodes"
                except Exception as e:
                    self.sub_title = f"Error: {e}"
        elif self.current_view == "collection":
            collection_id = previous.get("collection_id", self.current_collection_id)
            collection_name = previous.get("collection_name", self.current_collection_name)
            if collection_id:
                self.current_collection_id = collection_id
                self.current_collection_name = collection_name
                try:
                    items = self._fetch_collection_items(collection_id)
                    self._populate_list(items)
                    self.sub_title = f"{len(items)} items"
                except Exception as e:
                    self.sub_title = f"Error: {e}"
        elif self.current_view == "settings":
            self._populate_settings()
            self.sub_title = "MPV options"
        elif self.current_view == "settings_drm":
            self._populate_drm_settings()
            self.sub_title = "Select a connector"
        elif self.current_view == "settings_audio":
            self._populate_audio_settings()
            self.sub_title = "Select an audio device"
        elif self.current_view == "settings_spdif":
            self._populate_spdif_settings()
            self.sub_title = "Toggle passthrough codecs"
        
        self.list_view.focus()
    
    def action_settings(self) -> None:
        """Open settings"""
        if self.current_view.startswith("settings") or self.current_view == "servers":
            return
        
        nav_entry = {"view": self.current_view, "title": self.title}
        if self.current_view in ("seasons", "episodes"):
            nav_entry["series_id"] = self.current_series_id
            nav_entry["series_name"] = self.current_series_name
        elif self.current_view == "collection":
            nav_entry["collection_id"] = self.current_collection_id
            nav_entry["collection_name"] = self.current_collection_name
        
        self.navigation_stack.append(nav_entry)
        self.current_view = "settings"
        self.title = "Settings"
        self._populate_settings()
        self.sub_title = "MPV options"
        
        self.query_one("#search", Input).value = ""
        self.list_view.focus()
    
    def action_switch_server(self) -> None:
        """Open server switcher"""
        if self.current_view == "servers":
            return
        if len(self.all_server_names) < 2:
            self.sub_title = "Only one server configured (use --add-server to add more)"
            return

        nav_entry = {"view": self.current_view, "title": self.title}
        if self.current_view in ("seasons", "episodes"):
            nav_entry["series_id"] = self.current_series_id
            nav_entry["series_name"] = self.current_series_name
        elif self.current_view == "collection":
            nav_entry["collection_id"] = self.current_collection_id
            nav_entry["collection_name"] = self.current_collection_name

        self.navigation_stack.append(nav_entry)
        self.current_view = "servers"
        self.title = "Switch Server"
        self._populate_server_list()
        self.sub_title = "Select a server"

        self.query_one("#search", Input).value = ""
        self.list_view.focus()
    
    def action_focus_search(self) -> None:
        """Focus the search input"""
        if self.current_view.startswith("settings") or self.current_view == "servers":
            return
        self.query_one("#search", Input).focus()
    
    def action_cycle_library(self) -> None:
        """Cycle to the next library"""
        if not self.libraries:
            return
        
        if self.current_view != "library":
            self.navigation_stack.clear()
            self.current_view = "library"
            self.current_series_id = None
            self.current_series_name = None
            self.current_season_id = None
            self.title = "Jellyfin Selector"
        
        self.current_library_index = (self.current_library_index + 1) % len(self.libraries)
        self._update_library_tabs()
        
        try:
            self.all_items = self._fetch_library_items()
            self._populate_list(self.all_items)
            library_name = self.libraries[self.current_library_index].get("Name", "Unknown")
            self.sub_title = f"{library_name}: {len(self.all_items)} items"
        except Exception as e:
            self.sub_title = f"Error: {e}"
        
        self.list_view.focus()
    
    def action_show_details(self) -> None:
        """Show detail view for track selection"""
        if self.current_view.startswith("settings") or self.current_view in ("details", "servers"):
            return
        
        if not self.list_view or self.list_view.index is None:
            return
        
        highlighted = self.list_view.highlighted_child
        if not highlighted or not hasattr(highlighted, 'item_data'):
            return
        
        item = highlighted.item_data
        if item.get("Type") not in ("Movie", "Episode"):
            return
        
        item_id = item.get("Id")
        if not item_id:
            return
        
        self.navigation_stack.append({
            "view": self.current_view,
            "series_id": self.current_series_id,
            "series_name": self.current_series_name,
            "collection_id": self.current_collection_id,
            "collection_name": self.current_collection_name,
            "title": self.title
        })
        
        try:
            self.detail_item = item
            self.detail_media_streams = self._fetch_media_info(item_id)
            
            # Reset track selections
            self.selected_video_track = None
            self.selected_audio_track = None
            self.selected_subtitle_track = None
            
            # Reset temporary DRM/audio settings (will use defaults)
            self.detail_drm_connector = None
            self.detail_drm_mode = None
            self.detail_audio_device = None
            
            for stream in self.detail_media_streams:
                stream_type = stream.get("Type", "")
                if stream_type == "Video" and self.selected_video_track is None:
                    self.selected_video_track = stream.get("Index")
                elif stream_type == "Audio" and self.selected_audio_track is None:
                    self.selected_audio_track = stream.get("Index")
            
            self.current_view = "details"
            self.title = f"Details: {item.get('Name', 'Unknown')}"
            self._populate_details()
            self.sub_title = "Select tracks and press Enter to play"
        except Exception as e:
            self.sub_title = f"Error loading details: {e}"
            self.navigation_stack.pop()
        
        self.list_view.focus()
    
    def action_play(self) -> None:
        """Play the current item (shortcut for detail view)"""
        if self.current_view == "details":
            # In detail view, play with selected tracks
            self._play_with_tracks()
        elif self.current_view not in ("settings", "settings_drm", "settings_drm_modes", "settings_audio", "settings_spdif", "servers"):
            # In library/season/episode view, play highlighted item directly
            if not self.list_view or self.list_view.index is None:
                return
            
            highlighted = self.list_view.highlighted_child
            if not highlighted or not hasattr(highlighted, 'item_data'):
                return
            
            item = highlighted.item_data
            if item.get("Type") in ("Movie", "Episode"):
                self._play_item(item)
    
    def action_toggle_watched(self) -> None:
        """Toggle watched status"""
        if self.current_view.startswith("settings") or self.current_view in ("details", "servers"):
            return
        
        if not self.list_view or self.list_view.index is None:
            return
        
        highlighted = self.list_view.highlighted_child
        if not highlighted or not hasattr(highlighted, 'item_data'):
            return
        
        item = highlighted.item_data
        item_type = item.get("Type", "")
        
        if item_type not in ("Movie", "Episode", "Series", "Season", "BoxSet"):
            return
        
        item_id = item.get("Id")
        if not item_id:
            return
        
        user_data = item.get("UserData", {})
        currently_played = user_data.get("Played", False)
        played_percentage = user_data.get("PlayedPercentage", 0)
        play_count = user_data.get("PlayCount", 0)
        
        is_partially_watched = not currently_played and (played_percentage > 0 or play_count > 0)
        new_played = not (currently_played or is_partially_watched)
        
        try:
            self._set_played_status(item_id, new_played)
            
            if item_type in ("Series", "Season", "BoxSet"):
                fresh_item = self._fetch_item(item_id)
                if fresh_item:
                    item.update(fresh_item)
                    highlighted.item_data = item
            else:
                if "UserData" not in item:
                    item["UserData"] = {}
                item["UserData"]["Played"] = new_played
                item["UserData"]["PlayedPercentage"] = 0
                item["UserData"]["PlayCount"] = 1 if new_played else 0
            
            self._refresh_current_item(highlighted, item)
            self.sub_title = f"Marked as {'watched' if new_played else 'unwatched'}"
        except Exception as e:
            self.sub_title = f"Error: {e}"


# ============================================================================
# CLI Functions
# ============================================================================

def get_credentials(server_name: str | None = None) -> tuple[str, str, str, str, str, str, str, str, list[str]]:
    """
    Load credentials and settings from config.
    Returns (server_url, api_key, user_id, drm_connector, drm_mode,
             audio_device, audio_spdif, active_server_name, all_server_names)
    """
    config = load_config()
    if _migrate_legacy_config(config):
        # Reload from disk after migration to ensure clean ConfigParser state
        config = load_config()

    all_names = get_server_names(config)

    # Determine which server to use
    if server_name:
        active_name = server_name
    else:
        active_name = get_active_server_name(config)

    server_url = ""
    api_key = ""
    user_id = ""

    if active_name:
        srv = get_server_config(config, active_name)
        if srv:
            server_url = srv["url"]
            api_key = srv["api_key"]
            user_id = srv["user_id"]

    # Fallback: try env vars if no server config found
    if not all([server_url, api_key, user_id]):
        env_url = os.getenv("JELLYFIN_URL")
        env_key = os.getenv("JELLYFIN_API_KEY")
        env_uid = os.getenv("JELLYFIN_USER_ID")
        if all([env_url, env_key, env_uid]):
            server_url = env_url
            api_key = env_key
            user_id = env_uid
            active_name = active_name or "env"

    drm_connector = ""
    drm_mode = ""
    audio_device = ""
    audio_spdif = ""
    if "mpv" in config:
        drm_connector = config["mpv"].get("drm_connector", "")
        drm_mode = config["mpv"].get("drm_mode", "")
        audio_device = config["mpv"].get("audio_device", "")
        audio_spdif = config["mpv"].get("audio_spdif", "")

    return (server_url, api_key, user_id, drm_connector, drm_mode,
            audio_device, audio_spdif, active_name or "", all_names)


def save_last_command(cmd_args: list[str]) -> None:
    """Save the last MPV command"""
    config = load_config()
    if "last_command" not in config:
        config["last_command"] = {}
    config["last_command"]["cmd"] = " ".join(cmd_args)
    save_config(config)


def get_last_command() -> str | None:
    """Get the last MPV command"""
    config = load_config()
    if "last_command" in config:
        return config["last_command"].get("cmd")
    return None


def interactive_setup() -> tuple[str, str, str] | None:
    """Interactive setup for first-time configuration"""
    print("Jellyfin TUI Setup")
    print("=" * 40)
    print()
    
    server_url = input("Enter your Jellyfin server URL (e.g., http://localhost:8096): ").strip()
    if not server_url:
        print("Server URL is required.")
        return None
    
    if not server_url.startswith(("http://", "https://")):
        server_url = "http://" + server_url
    
    print()
    print("Choose authentication method:")
    print("  1. Quick Connect (recommended)")
    print("  2. Manual API key entry")
    print()
    choice = input("Enter choice [1]: ").strip() or "1"
    
    if choice == "1":
        result = quick_connect_auth(server_url)
        if result:
            api_key, user_id = result
            save_auth_to_config(server_url, api_key, user_id)
            return server_url, api_key, user_id
        print()
        print("Quick Connect failed. You can try manual entry instead.")
        choice = "2"
    
    if choice == "2":
        print()
        print("To get an API key:")
        print("  1. Go to Dashboard → API Keys")
        print("  2. Create a new key for 'jvuey'")
        print()
        api_key = input("Enter your API key: ").strip()
        if not api_key:
            print("API key is required.")
            return None
        
        print()
        print("To get your User ID:")
        print("  1. Go to Dashboard → Users")
        print("  2. Click on your user")
        print("  3. The User ID is in the URL")
        print()
        user_id = input("Enter your User ID: ").strip()
        if not user_id:
            print("User ID is required.")
            return None
        
        save_auth_to_config(server_url, api_key, user_id)
        return server_url, api_key, user_id
    
    return None


def cli_add_server() -> None:
    """CLI flow: add a new server interactively"""
    print("Add Jellyfin Server")
    print("=" * 40)
    print()

    config = load_config()
    if _migrate_legacy_config(config):
        config = load_config()

    existing = get_server_names(config)
    if existing:
        print(f"Existing servers: {', '.join(existing)}")
        print()

    # Optional custom name
    custom_name = input("Server name (leave blank to auto-generate from URL): ").strip()

    server_url = input("Jellyfin server URL (e.g., http://192.168.1.10:8096): ").strip()
    if not server_url:
        print("Server URL is required.")
        sys.exit(1)

    if not server_url.startswith(("http://", "https://")):
        server_url = "http://" + server_url

    print()
    print("Choose authentication method:")
    print("  1. Quick Connect (recommended)")
    print("  2. Manual API key entry")
    print()
    choice = input("Enter choice [1]: ").strip() or "1"

    api_key = ""
    user_id = ""

    if choice == "1":
        result = quick_connect_auth(server_url)
        if result:
            api_key, user_id = result
        else:
            print()
            print("Quick Connect failed. Trying manual entry...")
            choice = "2"

    if choice == "2":
        print()
        api_key = input("Enter your API key: ").strip()
        if not api_key:
            print("API key is required.")
            sys.exit(1)
        print()
        user_id = input("Enter your User ID: ").strip()
        if not user_id:
            print("User ID is required.")
            sys.exit(1)

    # Reload config (in case quick_connect_auth modified it for device id)
    config = load_config()
    if _migrate_legacy_config(config):
        config = load_config()

    # Determine name
    if custom_name:
        base_name = re.sub(r'[^a-zA-Z0-9_-]', '-', custom_name).strip('-')
        if not base_name:
            base_name = _derive_server_name(server_url, config)
    else:
        base_name = _derive_server_name(server_url, config)

    # add_server_to_config handles uniqueification
    # But we already derived a unique name - pass it through
    final_name = add_server_to_config(config, base_name, server_url, api_key, user_id)

    print()
    print(f"Server '{final_name}' added successfully.")
    print(f"Config saved to {get_config_path()}")

    # Ask if this should be the active server
    if len(get_server_names(config)) > 1:
        make_active = input(f"Make '{final_name}' the active server? [Y/n]: ").strip().lower()
        if make_active in ("", "y", "yes"):
            set_active_server(config, final_name)
            print(f"'{final_name}' is now the active server.")
    else:
        print(f"'{final_name}' is now the active server.")


def cli_delete_server() -> None:
    """CLI flow: delete a server interactively"""
    config = load_config()
    if _migrate_legacy_config(config):
        config = load_config()

    names = get_server_names(config)
    if not names:
        print("No servers configured.")
        sys.exit(0)

    active = get_active_server_name(config)

    print("Configured Servers")
    print("=" * 40)
    for i, name in enumerate(names, 1):
        marker = " (active)" if name == active else ""
        srv = get_server_config(config, name)
        url = srv["url"] if srv else "?"
        print(f"  {i}. {name}{marker}  [{url}]")
    print()

    selection = input("Enter number to delete (or 'q' to cancel): ").strip()
    if selection.lower() in ("q", ""):
        print("Cancelled.")
        return

    try:
        idx = int(selection) - 1
        if not (0 <= idx < len(names)):
            print("Invalid selection.")
            return
    except ValueError:
        print("Invalid input.")
        return

    target = names[idx]
    confirm = input(f"Delete server '{target}'? This cannot be undone. [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Cancelled.")
        return

    remove_server_from_config(config, target)
    print(f"Server '{target}' deleted.")

    remaining = get_server_names(config)
    if remaining:
        new_active = get_active_server_name(config)
        print(f"Active server is now '{new_active}'.")
    else:
        print("No servers remaining. Run with --add-server or --setup to add one.")


def main():
    # Handle --again flag
    if len(sys.argv) > 1 and sys.argv[1] in ("--again", "-a"):
        last_cmd = get_last_command()
        if last_cmd:
            print(f"Re-running: {last_cmd}")
            os.execlp("sh", "sh", "-c", last_cmd)
        else:
            print("No previous command found in config.")
            sys.exit(1)
    
    # Handle --tty flag (force TTY mode) or auto-detect
    if "--tty" in sys.argv:
        set_tty_mode(True)
        sys.argv.remove("--tty")
    elif "--no-tty" in sys.argv:
        set_tty_mode(False)
        sys.argv.remove("--no-tty")
    else:
        set_tty_mode(is_tty())
    
    # Handle --add-server
    if "--add-server" in sys.argv:
        cli_add_server()
        sys.exit(0)

    # Handle --delete-server
    if "--delete-server" in sys.argv:
        cli_delete_server()
        sys.exit(0)

    # Handle --setup flag
    if len(sys.argv) > 1 and sys.argv[1] in ("--setup", "--auth"):
        result = interactive_setup()
        if not result:
            sys.exit(1)
        # Re-read credentials after setup
        (server_url, api_key, user_id, drm_connector, drm_mode,
         audio_device, audio_spdif, server_name, all_server_names) = get_credentials()
    else:
        (server_url, api_key, user_id, drm_connector, drm_mode,
         audio_device, audio_spdif, server_name, all_server_names) = get_credentials()
    
    # Run setup if no config
    if not all([server_url, api_key, user_id]):
        print("No configuration found. Starting setup...")
        print()
        result = interactive_setup()
        if not result:
            print()
            print("Setup cancelled.")
            print()
            print("Create a config file at ~/.config/jvuey/config.ini with:")
            print()
            print("[server.myserver]")
            print("url = http://your-server:8096")
            print("api_key = your-api-key")
            print("user_id = your-user-id")
            print()
            print("[general]")
            print("active_server = myserver")
            sys.exit(1)
        # Re-read after setup
        (server_url, api_key, user_id, drm_connector, drm_mode,
         audio_device, audio_spdif, server_name, all_server_names) = get_credentials()
    
    # Main app loop - supports server switching without restarting the process
    current_server_name = server_name
    while True:
        # Always reload credentials from disk to avoid stale state after
        # migration or config changes from --add-server / --delete-server
        (server_url, api_key, user_id, drm_connector, drm_mode,
         audio_device, audio_spdif, _, all_server_names) = get_credentials(current_server_name)

        app = JellyfinSelector(
            server_url, api_key, user_id,
            drm_connector, drm_mode, audio_device, audio_spdif,
            server_name=current_server_name,
            all_server_names=all_server_names,
        )
        result = app.run()

        if isinstance(result, _ServerSwitch):
            # User wants to switch servers - update active and loop
            config = load_config()
            set_active_server(config, result.server_name)
            current_server_name = result.server_name
            # Re-read all server names in case config changed
            all_server_names = get_server_names(config)
            continue

        if result:
            # Playback - unpack and exec mpv
            if len(result) == 6:
                stream_url, drm_connector, drm_mode, audio_device, audio_spdif, track_args = result
            else:
                stream_url, drm_connector, drm_mode, audio_device, audio_spdif = result
                track_args = []

            # Build MPV command
            mpv_args = ["mpv"]
            if drm_connector:
                mpv_args.append(f"--drm-connector={drm_connector}")
            if drm_mode:
                mpv_args.append(f"--drm-mode={drm_mode}")
            if audio_device:
                mpv_args.append(f"--audio-device={audio_device}")
            if audio_spdif:
                mpv_args.append(f"--audio-spdif={audio_spdif}")
            mpv_args.extend(track_args)
            mpv_args.append(stream_url)

            save_last_command(mpv_args)
            os.execvp("mpv", mpv_args)
        else:
            # User quit normally
            break


if __name__ == "__main__":
    main()
