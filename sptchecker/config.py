import os
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    _BUNDLE_DIR = Path(__file__).parent.parent

ASSETS_DIR = _BUNDLE_DIR / "assets"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SPTModChecker"
STATE_FILE = DATA_DIR / "spt_mods_state.json"
CACHE_DIR = DATA_DIR / "thumb_cache"

# ── Version ────────────────────────────────────────────────────────────

# The runtime source of truth: the User-Agent sent to the Forge and the
# baseline the self-update check compares releases against. version_info.py
# and the .spec still carry their own copy, since PyInstaller reads those at
# build time and can't import this -- keep all three in step when bumping.
APP_VERSION = "3.3.2"

# This app's own listing on the Forge -- where users actually download it, so
# it's the version that matters for "am I out of date". Checked through the
# same metered request path as everything else, one request per interval.
FORGE_MOD_ID = 2921
FORGE_MOD_PAGE = "https://sp-mod.com/mod/2921/sptchecker"
# Six-hourly, not per check cycle. A release lands every few weeks at most, so
# riding the 15-minute mod poll would spend hundreds of requests a day to learn
# nothing -- against a host that meters us and asked us to ease off.
UPDATE_CHECK_INTERVAL_HOURS = 6

# ── SPT version filter ──────────────────────────────────────────────────

# Only mods with a version whose spt_version_constraint (as published by the
# Forge) covers the target SPT version are shown in the New/Recently Updated
# feed. This is just the seed value for a fresh install (state.json has none
# yet) -- the app's header picker lets the target be changed, or set to
# auto-detect from the Local Mods folder, at runtime; see app.py's
# _resolve_target_spt_version. Overridable without touching source via the
# SPTCHECKER_TARGET_SPT_VERSION env var. Falsy (empty string) disables
# filtering and shows every mod's actual latest version, unfiltered.
DEFAULT_TARGET_SPT_VERSION = os.environ.get("SPTCHECKER_TARGET_SPT_VERSION", "4.0.13")

# ── Feed ───────────────────────────────────────────────────────────────

FEED_URL = "https://sp-mod.com/mods/rss"
FEED_UPDATED_URL = "https://sp-mod.com/mods/rss?sort=updated"
API_URL = "https://sp-mod.com/api/v0/mods"
API_MOD_URL = "https://sp-mod.com/api/v0/mod"
API_MODS_UPDATES_URL = "https://sp-mod.com/api/v0/mods/updates"
FORGE_URL = "https://sp-mod.com/mods"
FORGE_USER_URL = "https://sp-mod.com/user"
DC_NS = "http://purl.org/dc/elements/1.1/"

# The Forge moved to sp-mod.com under new management; the old hosts are fully
# down (HTTP 521), not redirecting, so nothing resolves without this. Mod
# ids and slugs carried over unchanged -- verified live, old
# /mod/<id>/<slug> paths resolve 1:1 on the new domain -- so rewriting just
# the host preserves each mod's identity. That matters because a mod's link
# is its dedupe key in saved state: without this rewrite every already-known
# mod would look brand new on the first check after updating and fire a
# notification storm.
HOST_MIGRATIONS = {
    "forge.sp-tarkov.com": "sp-mod.com",
    "forge-static.sp-tarkov.com": "files.sp-mod.com",
}
# State fields holding a URL that needs the rewrite above.
MIGRATED_URL_FIELDS = ("link", "thumb_url")

# ── Behaviour ──────────────────────────────────────────────────────────

# Matched to the shortest cache window sp-mod.com serves: its maintainer
# confirmed every endpoint carries at least 15 minutes of cached data, so
# checking faster than this spends requests on bytes that cannot have changed.
# Polling every 5 minutes made two of every three checks pure waste, against a
# host that had already turned on bot countermeasures once.
CHECK_INTERVAL_MINUTES = 15
MAX_PER_CATEGORY = 7
THUMB_SIZE = (52, 52)
STATE_FIELDS = (
    "title", "link", "author", "author_id", "version", "category", "published", "updated",
)
DISPLAY_FIELDS = (
    "title", "link", "author", "author_id", "version", "category", "thumb_url", "description",
    "full_description", "changelog", "author_since",
)
THUMB_MAX_AGE_DAYS = 3
NEW_AUTHOR_DAYS = 60
TOP_STATS_WINDOW_DAYS = 30
TREND_WINDOW_DAYS = 30

# ── Local mod scan (opt-in) ──────────────────────────────────────────────

BEPINEX_PLUGINS_SUBPATH = "BepInEx/plugins"
SERVER_MODS_SUBPATH = "SPT/user/mods"
LEGACY_SERVER_MODS_SUBPATH = "user/mods"
FUZZY_MATCH_THRESHOLD = 0.82
# Every SPT install ships with its own core components (SPT.Common,
# SPT.Reflection, etc.) sitting in the same folder shapes as real mods --
# these were never published to the Forge, so every user would otherwise see
# them show up as permanent "Not Found on Forge" clutter.
CORE_SPT_NAME_PREFIX = "spt."
# Standalone .NET helper (see modreader/) that reads real mod metadata via
# actual CLR reflection -- built separately via `dotnet publish` and copied
# into assets/, not built by PyInstaller itself.
MODREADER_EXE = ASSETS_DIR / "ModReader.exe"
MODREADER_TIMEOUT_SECONDS = 120
# Used to read the installed SPT server version straight from the exe's own
# Windows version resource -- the only reliable source, since it's not
# recorded in any plain-text config file on disk.
SPT_SERVER_EXE_SUBPATH = "SPT/SPT.Server.exe"
# GET /api/v0/mods/updates takes a comma-separated `mods` list with no
# documented cap -- chunk defensively so a large install never risks the
# request line getting rejected/truncated by a proxy or server limit.
MODS_UPDATES_CHUNK_SIZE = 40
# Batched published-status lookups are bounded by the API's documented
# per_page maximum of 50.
PUBLISHED_CHUNK_SIZE = 50

# ── Window ─────────────────────────────────────────────────────────────

WINDOW_DEFAULT_GEOMETRY = "780x600"
WINDOW_MIN_WIDTH = 700
WINDOW_MIN_HEIGHT = 500

# ── Windows registry ──────────────────────────────────────────────────

STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "SPTModChecker"

# ── Colors ─────────────────────────────────────────────────────────────

BG = "#1a1a24"
CARD_BG = "#252535"
CARD_HOVER = "#30304a"
TEXT = "#ccccdd"
TEXT_DIM = "#777799"
TEXT_BRIGHT = "#eeeef4"
ACCENT_NEW = "#4caf50"
ACCENT_UPD = "#ffa726"
ACCENT_NEW_AUTHOR = "#e91e63"
STATUS_BG = "#14141c"
SEPARATOR = "#333348"

# Card border color by mod category -- matches Forge's own category list.
# "Other" and any unrecognized category fall back to CATEGORY_COLOR_DEFAULT.
# Hues are deliberately spread >=15 degrees apart (computed, not eyeballed) and
# kept >=20 degrees clear of the reserved green/orange/pink accents above, so
# adjacent categories stay visually distinguishable rather than blurring together.
CATEGORY_COLORS = {
    "Weapons": "#cf4b3f",
    "Overhauls": "#d9d568",
    "Hideout": "#3fcf85",
    "Tools": "#68d9bb",
    "Audio": "#3fcfcf",
    "Locations": "#68bdd9",
    "Scripting": "#3f85cf",
    "Locales": "#6882d9",
    "Quests": "#443fcf",
    "Items": "#8868d9",
    "Clothing": "#8e3fcf",
    "Retextures": "#c368d9",
    "Models": "#cf3fc5",
    # Deliberately desaturated/neutral, distinct from the vibrant hues above --
    # also frees up more spacing between the remaining 13 colorful categories.
    "Bots": "#8d6e63",
    "Traders": "#78909c",
    "Equipment": "#9e9e9e",
}
CATEGORY_COLOR_DEFAULT = SEPARATOR
