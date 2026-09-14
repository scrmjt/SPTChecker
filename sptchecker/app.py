import re
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime

import pystray
from PIL import Image, ImageDraw, ImageTk

from .config import (
    ACCENT_NEW, ACCENT_UPD, BG, CARD_BG, CARD_HOVER,
    CATEGORY_COLOR_DEFAULT, CATEGORY_COLORS,
    CHECK_INTERVAL_MINUTES,
    DEFAULT_TARGET_SPT_VERSION,
    DISPLAY_FIELDS, FORGE_MOD_PAGE, FORGE_URL, MAX_PER_CATEGORY,
    SEPARATOR, STATE_FIELDS, STATUS_BG, TEXT, TEXT_BRIGHT, TEXT_DIM,
    UPDATE_CHECK_INTERVAL_HOURS,
    WINDOW_DEFAULT_GEOMETRY, WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH,
)
from .feed import ForgeBlocked, fetch_feeds, list_known_spt_versions, unpublished_links
from .localmods import detect_spt_version, scan_installed_mods
from .matcher import match_local_mods
from .platform import (
    badge_icon, disable_show_animation, is_startup_enabled, load_app_icon,
    refresh_startup_if_stale, send_toast, set_dark_title_bar, set_dpi_aware,
    set_startup_enabled,
)
from .state import (
    compute_stats, download_thumb, load_state, placeholder_thumb, purge_old_thumbs, save_state,
)
from .update import check_for_update
from .widgets import LocalScanSettingsWindow, ModCard, StatsWindow, flat_button

_ICON_SUPERSAMPLE = 4

# Dropdown entry representing "no version filter" -- a display label, not a
# real version string; translated to/from the stored empty-string value at
# the UI boundary (_on_version_picked, _refresh_version_choices) so the rest
# of the app (state, feed.py) only ever deals with "" or a real version.
_UNFILTERED_CHOICE = "All versions (unfiltered)"


def _render_info_icon(color, size=16):
    """Render a small 'i' info icon via PIL + LANCZOS downscale for real anti-aliasing
    -- Tk Canvas primitives (create_oval/create_line) aren't anti-aliased on Windows
    and look jagged/pixelated at these small sizes, independent of display scaling."""
    big = size * _ICON_SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = _ICON_SUPERSAMPLE
    draw.ellipse([pad, pad, big - pad, big - pad], outline=color, width=_ICON_SUPERSAMPLE)
    cx = big // 2
    dot_r = _ICON_SUPERSAMPLE * 1.1
    dot_cy = big * 0.28
    draw.ellipse([cx - dot_r, dot_cy - dot_r, cx + dot_r, dot_cy + dot_r], fill=color)
    draw.line([cx, big * 0.45, cx, big * 0.74], fill=color, width=int(_ICON_SUPERSAMPLE * 1.3))
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _render_dot(color, size=10):
    """Render a small filled circle via PIL + LANCZOS downscale (see _render_info_icon)."""
    big = size * _ICON_SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([0, 0, big - 1, big - 1], fill=color)
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


class SPTCheckerApp:
    def __init__(self, start_hidden=False):
        self._start_hidden = start_hidden
        self.state = load_state()

        set_dpi_aware()
        self.root = tk.Tk()
        self.root.title("SPTChecker")
        self.root.configure(bg=BG)
        self.root.geometry(self._load_geometry())
        self.root.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        set_dark_title_bar(self.root, show=not start_hidden)
        disable_show_animation(self.root)

        self._app_icon = load_app_icon()
        self._icon_photo = ImageTk.PhotoImage(self._app_icon.resize((32, 32), Image.LANCZOS))
        self.root.iconphoto(True, self._icon_photo)

        self._photos = {}  # frame -> PhotoImage refs for its current cards
        self._checking = False
        self._scanning = False
        self._local_scan_window = None
        self._next_check_ts = None
        self._timer_after_id = None
        self._new_sig = None
        self._upd_sig = None
        self._tray = None
        self._unread_count = 0
        self._visible = not start_hidden
        self._last_target_version = None
        self._last_version_source = "manual"
        self._last_known_versions = []
        startup_on = is_startup_enabled()
        self._startup_var = tk.BooleanVar(value=startup_on)
        try:
            refresh_startup_if_stale()
        except OSError:
            pass

        self._build_ui()
        self._setup_tray()

        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self.root.after(400, self._check_now)
        # Local mod scanning never runs on its own, even with the feature
        # enabled and a folder already set -- only an explicit click on
        # Scan Now (in _show_local_scan) starts one.

    # ── Window geometry ─────────────────────────────────────────────────

    def _load_geometry(self):
        geometry = self.state.get("window_geometry", "")
        m = re.fullmatch(r"(\d+)x(\d+)", geometry)
        if not m:
            return WINDOW_DEFAULT_GEOMETRY
        w = max(WINDOW_MIN_WIDTH, int(m.group(1)))
        h = max(WINDOW_MIN_HEIGHT, int(m.group(2)))
        return f"{w}x{h}"

    def _save_geometry(self):
        if not self._visible:
            return
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        if w > 1 and h > 1:
            self.state["window_geometry"] = f"{w}x{h}"
            save_state(self.state)

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg=BG, pady=4)
        hdr.pack(fill="x", padx=12)

        self._legend_icon_dim = _render_info_icon(TEXT_DIM)
        self._legend_icon_bright = _render_info_icon(TEXT_BRIGHT)
        self._legend_icon = tk.Label(hdr, image=self._legend_icon_dim, bg=BG, cursor="hand2")
        self._legend_icon.pack(side="left")
        self._legend_icon.bind("<Enter>", self._legend_enter)
        self._legend_icon.bind("<Leave>", self._legend_leave)

        flat_button(hdr, "Stats", self._show_stats).pack(side="left", padx=(6, 0))
        flat_button(hdr, "Local Mods", self._show_local_scan).pack(side="left", padx=(6, 0))

        # ── SPT version filter picker ────────────────────────────────────
        ver_frame = tk.Frame(hdr, bg=BG)
        ver_frame.pack(side="left", padx=(12, 0))
        tk.Label(ver_frame, text="SPT:", font=("Segoe UI", 8),
                 fg=TEXT_DIM, bg=BG).pack(side="left")

        stored = self.state.get("target_spt_version", DEFAULT_TARGET_SPT_VERSION).strip()
        self._target_version_var = tk.StringVar(value=stored or _UNFILTERED_CHOICE)
        # Seeded with just the stored choice until the first live check
        # populates the real list (_refresh_version_choices) -- a dropdown
        # needs *some* menu to build before that first fetch completes.
        self._version_menu = tk.OptionMenu(
            ver_frame, self._target_version_var, self._target_version_var.get(),
        )
        self._version_menu.configure(
            font=("Segoe UI", 8), bg=CARD_BG, fg=TEXT_BRIGHT,
            activebackground=CARD_HOVER, activeforeground=TEXT_BRIGHT,
            relief="flat", highlightthickness=0, padx=6, pady=1, cursor="hand2",
        )
        self._version_menu["menu"].configure(
            bg=CARD_BG, fg=TEXT_BRIGHT,
            activebackground=CARD_HOVER, activeforeground=TEXT_BRIGHT,
        )
        self._version_menu.pack(side="left", padx=(4, 4))
        self._bind_tooltip(
            self._version_menu,
            "Only show mods with a version compatible with the chosen SPT\n"
            "server version. Choices are read live from the Forge's own\n"
            "currently-published mods -- only ever versions mods actually\n"
            "support right now, never a stale hardcoded list.",
        )

        self._auto_detect_var = tk.BooleanVar(
            value=self.state.get("auto_detect_spt_version", False))
        auto_chk = tk.Checkbutton(
            ver_frame, text="auto", font=("Segoe UI", 8),
            fg=TEXT_DIM, bg=BG, selectcolor=CARD_BG,
            activebackground=BG, activeforeground=TEXT,
            variable=self._auto_detect_var, command=self._toggle_auto_detect,
        )
        auto_chk.pack(side="left")
        self._bind_tooltip(
            auto_chk,
            "Detect the target version from your Local Mods install folder\n"
            "(reads SPT.Server.exe) instead of the version picked at left.",
        )
        self._version_menu.configure(state="disabled" if self._auto_detect_var.get() else "normal")

        self._btn = flat_button(hdr, "Check Now", self._check_now)
        self._btn.pack(side="right")
        self._tooltip_id = None
        self._tooltip_win = None
        self._bind_tooltip(self._btn, "Check the Forge for new or updated mods")

        chk = tk.Checkbutton(
            hdr, text="Run on Startup", font=("Segoe UI", 8),
            fg=TEXT_DIM, bg=BG, selectcolor=CARD_BG,
            activebackground=BG, activeforeground=TEXT,
            variable=self._startup_var, command=self._toggle_startup,
        )
        chk.pack(side="right", padx=(0, 10))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(2, weight=1, uniform="col")

        new_hdr = tk.Frame(body, bg=BG)
        new_hdr.grid(row=0, column=0, sticky="w", pady=(0, 3))
        tk.Label(new_hdr, text="● NEW MODS", font=("Segoe UI", 10, "bold"),
                 fg=ACCENT_NEW, bg=BG, anchor="w").pack(side="left")
        # Text is set by _update_filter_labels once the target version is
        # resolved (which reads SPT.Server.exe when auto-detect is on, so it
        # isn't known for certain until then) -- empty until the first check.
        self._new_filter_lbl = tk.Label(new_hdr, text="", font=("Segoe UI", 8),
                                        fg=TEXT_DIM, bg=BG, anchor="w")
        self._new_filter_lbl.pack(side="left")
        self._new_frame = tk.Frame(body, bg=BG)
        self._new_frame.grid(row=1, column=0, sticky="nsew")

        tk.Frame(body, bg=SEPARATOR, width=1).grid(
            row=0, column=1, rowspan=2, sticky="ns", padx=8)

        upd_hdr = tk.Frame(body, bg=BG)
        upd_hdr.grid(row=0, column=2, sticky="w", pady=(0, 3))
        tk.Label(upd_hdr, text="● UPDATED MODS", font=("Segoe UI", 10, "bold"),
                 fg=ACCENT_UPD, bg=BG, anchor="w").pack(side="left")
        self._upd_filter_lbl = tk.Label(upd_hdr, text="", font=("Segoe UI", 8),
                                        fg=TEXT_DIM, bg=BG, anchor="w")
        self._upd_filter_lbl.pack(side="left")
        self._upd_frame = tk.Frame(body, bg=BG)
        self._upd_frame.grid(row=1, column=2, sticky="nsew")
        body.rowconfigure(1, weight=1)

        self._set_placeholder(self._new_frame, "Checking…")
        self._set_placeholder(self._upd_frame, "Checking…")

        # Seed the filter labels immediately (a fast local file read at
        # worst) rather than leaving them blank for the second or two before
        # the first background check reports back.
        self._update_filter_labels(*self._resolve_target_spt_version())

        bar = tk.Frame(self.root, bg=STATUS_BG, pady=3)
        bar.pack(fill="x", side="bottom")
        self._forge_dot = tk.Label(bar, text="●", font=("Segoe UI", 6),
                                   fg=TEXT_DIM, bg=STATUS_BG)
        self._forge_dot.pack(side="left", padx=(10, 2))
        self._bind_tooltip(
            self._forge_dot,
            "Green: last check reached the Forge OK\nRed: last check failed (retrying)",
        )

        self._lbl_status = tk.Label(bar, text="Starting…", font=("Segoe UI", 8),
                                    fg=TEXT_DIM, bg=STATUS_BG)
        self._lbl_status.pack(side="left")

        self._lbl_timer = tk.Label(bar, text="", font=("Segoe UI", 8),
                                   fg=TEXT_DIM, bg=STATUS_BG)
        self._lbl_timer.pack(side="right", padx=10)

        # Built but left unpacked -- it only appears once a newer release is
        # actually found, so the bar stays quiet for anyone already current.
        self._update_url = FORGE_MOD_PAGE
        self._update_lbl = tk.Label(bar, text="", font=("Segoe UI", 8, "bold"),
                                    fg=ACCENT_NEW, bg=STATUS_BG, cursor="hand2")
        self._update_lbl.bind(
            "<Button-1>", lambda _e: webbrowser.open(self._update_url))

    @staticmethod
    def _set_placeholder(frame, text):
        for w in frame.winfo_children():
            w.destroy()
        tk.Label(frame, text=text, font=("Segoe UI", 9), fg=TEXT_DIM,
                 bg=BG, justify="center").pack(pady=20)

    # ── Tooltip ─────────────────────────────────────────────────────────

    def _bind_tooltip(self, widget, text):
        widget.bind("<Enter>", lambda _e: self._tooltip_hover_start(widget, text))
        widget.bind("<Leave>", self._tooltip_hover_end)

    def _tooltip_hover_start(self, widget, text):
        self._tooltip_id = self.root.after(1000, lambda: self._show_tooltip(widget, text))

    def _tooltip_hover_end(self, _e=None):
        if self._tooltip_id:
            self.root.after_cancel(self._tooltip_id)
            self._tooltip_id = None
        if self._tooltip_win:
            self._tooltip_win.destroy()
            self._tooltip_win = None

    def _show_tooltip(self, widget, text):
        self._tooltip_id = None
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=CARD_BG)
        tk.Label(tw, text=text, font=("Segoe UI", 8), fg=TEXT, bg=CARD_BG,
                 padx=8, pady=4, justify="left").pack()
        self._tooltip_win = tw

    def _legend_enter(self, _e):
        self._legend_icon.configure(image=self._legend_icon_bright)
        self._tooltip_id = self.root.after(500, self._show_legend)

    def _legend_leave(self, _e=None):
        self._legend_icon.configure(image=self._legend_icon_dim)
        self._tooltip_hover_end()

    def _show_legend(self):
        self._tooltip_id = None
        x = self._legend_icon.winfo_rootx()
        y = self._legend_icon.winfo_rooty() + self._legend_icon.winfo_height() + 4
        tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=CARD_BG)

        inner = tk.Frame(tw, bg=CARD_BG, padx=10, pady=8)
        inner.pack()
        tk.Label(inner, text="CARD COLOR MEANS CATEGORY", font=("Segoe UI", 8, "bold"),
                 fg=TEXT_DIM, bg=CARD_BG, anchor="w").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        tw._dot_photos = []  # keep PhotoImage refs alive for this popup's lifetime
        cols = 2
        for i, (category, color) in enumerate(CATEGORY_COLORS.items()):
            row, col = i // cols + 1, (i % cols) * 2
            dot = _render_dot(color)
            tw._dot_photos.append(dot)
            tk.Label(inner, image=dot, bg=CARD_BG).grid(
                row=row, column=col, sticky="w", padx=(0, 6), pady=2)
            tk.Label(inner, text=category, font=("Segoe UI", 8), fg=TEXT, bg=CARD_BG,
                     anchor="w").grid(row=row, column=col + 1, sticky="w", padx=(0, 14), pady=2)

        last_row = len(CATEGORY_COLORS) // cols + 2
        other_dot = _render_dot(CATEGORY_COLOR_DEFAULT)
        tw._dot_photos.append(other_dot)
        tk.Label(inner, image=other_dot, bg=CARD_BG).grid(
            row=last_row, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        tk.Label(inner, text="Other / uncategorized", font=("Segoe UI", 8), fg=TEXT, bg=CARD_BG,
                 anchor="w").grid(row=last_row, column=1, columnspan=3, sticky="w", pady=(6, 0))

        self._tooltip_win = tw

    # ── Settings ───────────────────────────────────────────────────────

    def _toggle_startup(self):
        try:
            set_startup_enabled(self._startup_var.get())
        except OSError:
            self._startup_var.set(not self._startup_var.get())

    def _show_stats(self):
        stats = compute_stats(self.state.get("mods", {}))
        StatsWindow(self.root, stats)

    # ── SPT version filter ──────────────────────────────────────────────

    def _resolve_target_spt_version(self):
        """What SPT version should the feed be filtered against right now,
        and where did that answer come from -- "manual" (the typed value),
        "auto" (read live from the Local Mods install folder), or
        "auto-fallback" (auto-detect is on but couldn't read a version, so
        the typed value is being used anyway). Reading the exe's version
        resource is a fast local file read, not a network call, so this is
        cheap enough to call on every check.
        """
        manual = self.state.get("target_spt_version", DEFAULT_TARGET_SPT_VERSION).strip()
        if self.state.get("auto_detect_spt_version"):
            path = self.state.get("spt_install_path", "")
            detected = detect_spt_version(path) if path else None
            if detected:
                return detected, "auto"
            return manual, "auto-fallback"
        return manual, "manual"

    def _update_filter_labels(self, version, source):
        if not version:
            text = ""
        elif source == "auto":
            text = f"  (SPT {version} · auto)"
        elif source == "auto-fallback":
            text = f"  (SPT {version} · auto-detect unavailable)"
        else:
            text = f"  (SPT {version})"
        self._new_filter_lbl.configure(text=text)
        self._upd_filter_lbl.configure(text=text)

    def _select_version(self, choice):
        """Menu command for one dropdown entry -- sets the displayed choice
        and applies it. Bound per-entry in _refresh_version_choices rather
        than via OptionMenu's own `command=` callback so it also fires for
        entries added after construction."""
        self._target_version_var.set(choice)
        value = "" if choice == _UNFILTERED_CHOICE else choice
        if value == self.state.get("target_spt_version", DEFAULT_TARGET_SPT_VERSION):
            return
        self.state["target_spt_version"] = value
        save_state(self.state)
        if not self._auto_detect_var.get():
            self._check_now()

    def _toggle_auto_detect(self):
        enabled = self._auto_detect_var.get()
        self.state["auto_detect_spt_version"] = enabled
        save_state(self.state)
        self._version_menu.configure(state="disabled" if enabled else "normal")
        self._check_now()

    def _refresh_version_choices(self, versions):
        """Rebuild the picker's dropdown from a live list of SPT versions
        (newest first, as returned by feed.list_known_spt_versions) --
        called after every check. Keeps the current selection in the list
        even if it's since dropped off the live data (e.g. no mod currently
        declares it), so a deliberate pick never silently reverts out from
        under the user just because nothing recent still supports it.
        """
        current = self._target_version_var.get()
        choices = [_UNFILTERED_CHOICE] + list(versions)
        if current not in choices:
            choices.append(current)
        menu = self._version_menu["menu"]
        menu.delete(0, "end")
        for choice in choices:
            menu.add_command(label=choice, command=lambda c=choice: self._select_version(c))

    # ── Local mod scan (opt-in) ───────────────────────────────────────

    def _show_local_scan(self):
        if self._local_scan_window and self._local_scan_window.winfo_exists():
            self._local_scan_window.resurface()
            return
        self._local_scan_window = LocalScanSettingsWindow(
            self.root,
            enabled=self.state.get("local_scan_enabled", False),
            spt_path=self.state.get("spt_install_path", ""),
            on_toggle=self._toggle_local_scan,
            on_path_change=self._set_local_scan_path,
            on_scan_now=self._scan_local_now,
        )
        if self._scanning:
            # A scan is already running (e.g. the startup auto-scan) --
            # reflect that instead of showing an idle Scan Now state.
            self._local_scan_window.set_scanning()
        else:
            cached = self.state.get("local_scan_results")
            if cached:
                self._local_scan_window.set_results(cached)

    def _toggle_local_scan(self, enabled):
        self.state["local_scan_enabled"] = enabled
        save_state(self.state)

    def _set_local_scan_path(self, path):
        self.state["spt_install_path"] = path
        save_state(self.state)
        if self._auto_detect_var.get():
            self._check_now()

    def _scan_local_now(self):
        if self._scanning:
            return
        self._scanning = True
        threading.Thread(target=self._bg_scan_local, daemon=True).start()

    def _bg_scan_local(self):
        try:
            spt_path = self.state.get("spt_install_path", "")
            local_mods = scan_installed_mods(spt_path)
            results = match_local_mods(
                local_mods,
                spt_version=detect_spt_version(spt_path),
                on_progress=lambda done, total: self.root.after(
                    0, self._update_scan_progress, done, total),
            )
            for r in results:
                if r["update_available"]:
                    # May be None -- the widget falls back to its shared
                    # placeholder, no need to render one per mod here.
                    r["_pil"] = download_thumb(r["forge"].get("thumb_url"))
            # _pil (a PIL Image) isn't JSON-serializable -- persist a stripped
            # copy, and do the JSON write here on the worker thread so the UI
            # callback only touches widgets.
            self.state["local_scan_results"] = [
                {k: v for k, v in r.items() if k != "_pil"} for r in results
            ]
            save_state(self.state)
            self.root.after(0, self._apply_local_scan, results)
        except Exception as exc:
            self.root.after(0, self._on_local_scan_error, str(exc))

    def _apply_local_scan(self, results):
        self._scanning = False
        if self._local_scan_window and self._local_scan_window.winfo_exists():
            self._local_scan_window.set_results(results)

        updates = [r for r in results if r["update_available"]]
        if updates:
            self._send_list_toast(
                f"{len(updates)} Installed Mod{'s' if len(updates) != 1 else ''} Can Be Updated",
                [f"{r['forge']['title']} {r['current_version']} → {r['available_version']}"
                 for r in updates],
                [r["forge"]["link"] for r in updates],
            )

    def _on_local_scan_error(self, msg):
        self._scanning = False
        if self._local_scan_window and self._local_scan_window.winfo_exists():
            self._local_scan_window.set_error(msg)

    def _update_scan_progress(self, done, total):
        if self._local_scan_window and self._local_scan_window.winfo_exists():
            self._local_scan_window.set_progress(done, total)

    # ── System tray ────────────────────────────────────────────────────

    def _setup_tray(self):
        self._tray_icon_normal = self._app_icon.resize((64, 64), Image.LANCZOS)
        self._tray_icon_unread = badge_icon(self._tray_icon_normal)
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Check Now", self._tray_check),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon(
            "SPTModChecker", self._tray_icon_normal, "SPTChecker", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self._save_geometry()
        self._visible = False
        self.root.withdraw()

    def _tray_show(self, _icon=None, _item=None):
        self.root.after(0, self._do_show)

    def _do_show(self):
        self._visible = True
        # Windows reveals the window the instant deiconify() runs, before Tk
        # has actually painted every widget -- update_idletasks() alone isn't
        # enough to beat that, since the reveal doesn't wait on paint
        # completion. Staying fully transparent until a full update() forces
        # every widget to finish painting means there's nothing left to
        # progressively fill in once we make it visible.
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.state("normal")
        self.root.update()
        self.root.attributes("-alpha", 1.0)
        self.root.lift()
        self.root.focus_force()
        self._clear_unread()
        if self._next_check_ts:
            self._tick_timer()

    def _tray_check(self, _icon=None, _item=None):
        self.root.after(0, self._check_now)

    def _tray_quit(self, _icon=None, _item=None):
        self._save_geometry()
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    def _clear_unread(self):
        self._unread_count = 0
        if self._tray:
            self._tray.icon = self._tray_icon_normal
            self._tray.title = "SPTChecker"

    # ── Self-update check ──────────────────────────────────────────────

    def _schedule_update_check(self, delay_ms=3000):
        """Look for a newer release of the app itself, then re-arm.

        Deliberately off the mod-check cycle and on its own long timer:
        releases are weeks apart, so tying this to the 15-minute poll would
        be hundreds of requests a day to learn nothing. The first run is
        delayed a few seconds so it never competes with the startup check
        for the window's attention.
        """
        self.root.after(delay_ms, lambda: threading.Thread(
            target=self._bg_update_check, daemon=True).start())

    def _bg_update_check(self):
        # Never raises: check_for_update swallows every failure and returns
        # None, since not knowing whether an update exists is not worth
        # bothering anyone about.
        found = check_for_update()
        if found:
            self.root.after(0, self._show_update_available,
                            found["version"], found["url"])
        self.root.after(0, self._schedule_update_check,
                        UPDATE_CHECK_INTERVAL_HOURS * 3600 * 1000)

    def _show_update_available(self, version, url=FORGE_MOD_PAGE):
        """Surface the newer release in the status bar. Packed on first
        discovery only -- re-packing on every subsequent check would shuffle
        the bar's layout for no reason."""
        self._update_url = url
        self._update_lbl.configure(text=f"●  v{version} available")
        if not self._update_lbl.winfo_ismapped():
            self._update_lbl.pack(side="right", padx=(0, 4))
            self._bind_tooltip(
                self._update_lbl,
                f"SPTChecker v{version} is on the Forge.\nClick to open its page.")

    # ── Check logic ────────────────────────────────────────────────────

    def _check_now(self):
        if self._checking:
            return
        self._checking = True
        self._btn.configure(state="disabled", text="Checking…")
        self._lbl_status.configure(text="Fetching mods…")
        threading.Thread(target=self._bg_check, daemon=True).start()

    @staticmethod
    def _strip_for_state(mods):
        return [{k: m[k] for k in DISPLAY_FIELDS if k in m} for m in mods]

    def _bg_check(self):
        try:
            target_version, version_source = self._resolve_target_spt_version()
            self._last_target_version = target_version
            self._last_version_source = version_source
            newest, updated = fetch_feeds(target_spt_version=target_version)
            # Fail-soft ([] on any failure) and cheap next to the two feed
            # fetches above -- just refreshes the picker's dropdown, so a
            # miss here shouldn't take down the actual check.
            self._last_known_versions = list_known_spt_versions()
            known = self.state.get("mods", {})
            first_run = len(known) == 0
            prev_versions = {link: m.get("version", "") for link, m in known.items()}

            for mod in newest + updated:
                known[mod["link"]] = {k: mod[k] for k in STATE_FIELDS if k in mod}
            self.state["mods"] = known
            self.state["last_check"] = datetime.now().isoformat()

            prev_new = self.state.get("display_new", [])
            prev_upd = self.state.get("display_updated", [])

            display_new = newest[:MAX_PER_CATEGORY]
            display_upd = updated[:MAX_PER_CATEGORY]

            # One batched lookup covering both columns, rather than a request
            # per mod -- see unpublished_links().
            gone = unpublished_links([m["link"] for m in display_new + display_upd])
            display_new = [m for m in display_new if m["link"] not in gone]
            display_upd = [m for m in display_upd if m["link"] not in gone]

            for mod in display_upd:
                old_version = prev_versions.get(mod["link"], "")
                if old_version and old_version != mod.get("version", ""):
                    mod["prev_version"] = old_version

            self.state["display_new"] = self._strip_for_state(display_new)
            self.state["display_updated"] = self._strip_for_state(display_upd)
            save_state(self.state)

            purge_old_thumbs()

            for mod in display_new + display_upd:
                pil = download_thumb(mod.get("thumb_url"))
                mod["_pil"] = pil if pil else placeholder_thumb()

            prev_new_links = {m["link"] for m in prev_new}
            prev_upd_links = {m["link"] for m in prev_upd}
            notify_new = [m for m in display_new if m["link"] not in prev_new_links] if not first_run else []
            notify_upd = [m for m in display_upd if m["link"] not in prev_upd_links] if not first_run else []

            # Mark fresh mods for NEW badge
            fresh_new_links = {m["link"] for m in notify_new}
            fresh_upd_links = {m["link"] for m in notify_upd}
            for m in display_new:
                m["is_fresh"] = m["link"] in fresh_new_links
            for m in display_upd:
                m["is_fresh"] = m["link"] in fresh_upd_links

            if not first_run:
                self._send_notifications(notify_new, notify_upd)

            self.root.after(0, self._apply, display_new, display_upd, first_run,
                            len(notify_new), len(notify_upd))
        except ForgeBlocked as exc:
            self.root.after(0, self._on_error, str(exc), "")
        except Exception as exc:
            self.root.after(0, self._on_error, str(exc))

    @staticmethod
    def _send_list_toast(title, lines, links):
        """Common toast shape for a list of mods: up to 3 detail lines plus
        an 'and N more…' tail, clicking through to the mod's own page when
        there's exactly one, or the Forge listing otherwise."""
        shown = lines[:3]
        if len(lines) > 3:
            shown.append(f"and {len(lines) - 3} more…")
        url = links[0] if len(links) == 1 else FORGE_URL
        send_toast(title, "\n".join(shown), launch_url=url)

    def _send_notifications(self, new_mods, updated_mods):
        if new_mods:
            self._send_list_toast(
                f"{len(new_mods)} New SPT Mod{'s' if len(new_mods) != 1 else ''}",
                [f"{m['title']} {m['version']} by {m['author']}" for m in new_mods],
                [m["link"] for m in new_mods],
            )

        if updated_mods:
            def line(m):
                version_str = (f"{m['prev_version']} → {m.get('version', '')}"
                               if m.get("prev_version") else m.get("version", ""))
                return f"{m['title']} {version_str} by {m.get('author', '')}"
            self._send_list_toast(
                f"{len(updated_mods)} SPT Mod{'s' if len(updated_mods) != 1 else ''} Updated",
                [line(m) for m in updated_mods],
                [m["link"] for m in updated_mods],
            )

    def _apply(self, display_new, display_upd, first_run, n_fresh_new=0, n_fresh_upd=0):
        self._checking = False
        self._btn.configure(state="normal", text="Check Now")
        self._forge_dot.configure(fg=ACCENT_NEW)
        now = datetime.now().strftime("%H:%M:%S")
        total = len(self.state.get("mods", {}))

        self._update_filter_labels(self._last_target_version, self._last_version_source)
        if self._auto_detect_var.get():
            # Reflect what was actually detected/used, not the last pick --
            # the menu is disabled while auto is on, so this is
            # display-only and doesn't fight a manual choice made earlier.
            self._target_version_var.set(self._last_target_version or _UNFILTERED_CHOICE)
        self._refresh_version_choices(getattr(self, "_last_known_versions", []))

        self._new_sig = self._render_column(
            self._new_frame, display_new, self._new_sig, first_run,
            "Baseline set — monitoring for new mods…", "No new mods detected yet.")
        self._upd_sig = self._render_column(
            self._upd_frame, display_upd, self._upd_sig, first_run,
            "Baseline set — monitoring for updates…", "No updates detected yet.")

        if first_run:
            self._lbl_status.configure(text=f"Baseline: {total} mods cataloged at {now}")
        elif n_fresh_new or n_fresh_upd:
            self._lbl_status.configure(
                text=f"{n_fresh_new} new, {n_fresh_upd} updated at {now}  •  Tracking {total}"
            )
        else:
            self._lbl_status.configure(text=f"No changes at {now}  •  Tracking {total} mods")

        if not self._visible and not first_run:
            self._unread_count += n_fresh_new + n_fresh_upd

        if self._tray:
            if self._unread_count:
                self._tray.icon = self._tray_icon_unread
                self._tray.title = f"SPTChecker — {self._unread_count} unread"
            else:
                self._tray.icon = self._tray_icon_normal
                self._tray.title = "SPTChecker — no changes"

        self._schedule_next()

    def _on_error(self, msg, prefix="Error: "):
        self._checking = False
        self._btn.configure(state="normal", text="Check Now")
        self._forge_dot.configure(fg="#e53935")
        self._lbl_status.configure(text=f"{prefix}{msg}")
        # A failed check leaves both columns empty on a cold start, since
        # nothing has rendered yet -- fall back to the last results saved to
        # state so the window still shows the most recent known mods (stale,
        # but far better than blank) alongside the reason it couldn't refresh.
        self._show_cached_results()
        self._next_check_ts = time.time() + 300
        self._tick_timer()

    def _show_cached_results(self):
        """Render the last successfully-fetched mods from saved state.

        Only fills genuinely empty columns: once a check has rendered live
        results this session, those are newer than anything on disk and must
        not be replaced by them.
        """
        if self._new_sig is not None or self._upd_sig is not None:
            return
        cached_new = self.state.get("display_new", [])
        cached_upd = self.state.get("display_updated", [])
        if not cached_new and not cached_upd:
            return
        for mod in cached_new + cached_upd:
            # Thumbnails come from the on-disk cache; a miss can't be fetched
            # while the site is unreachable, so it falls back to a placeholder.
            pil = download_thumb(mod.get("thumb_url"))
            mod["_pil"] = pil if pil else placeholder_thumb()
            mod["is_fresh"] = False
        self._new_sig = self._render_column(
            self._new_frame, cached_new, self._new_sig, False,
            "", "No new mods detected yet.")
        self._upd_sig = self._render_column(
            self._upd_frame, cached_upd, self._upd_sig, False,
            "", "No updates detected yet.")

    def _render_column(self, frame, mods, prev_sig, first_run, baseline_text, empty_text):
        """Render one column, returning its new content signature. When the
        signature hasn't changed since last render, the destroy/rebuild of
        every card is skipped -- rebuilding identical cards on every check
        cycle caused a visible flash (including the check that fires as soon
        as you reopen the window after it's been hidden past an interval)."""
        if not mods:
            self._set_placeholder(frame, baseline_text if first_run else empty_text)
            return None
        sig = self._column_signature(mods)
        if sig != prev_sig:
            self._fill_column(frame, mods)
        else:
            # _pil is only consumed by a rebuild -- drop it on the skip path
            # so stale PIL images don't linger on the mod dicts.
            for m in mods:
                m.pop("_pil", None)
        return sig

    def _fill_column(self, frame, mods):
        # PhotoImage refs are kept per-frame: Tk widgets don't hold a Python
        # reference to their images, so clearing another column's refs while
        # skipping its rebuild would blank its thumbnails.
        photos = self._photos[frame] = []
        for w in frame.winfo_children():
            w.destroy()
        for mod in mods:
            photo = ImageTk.PhotoImage(mod.pop("_pil"))
            photos.append(photo)
            accent = CATEGORY_COLORS.get(mod.get("category"), CATEGORY_COLOR_DEFAULT)
            card = ModCard(frame, mod, accent, photo)
            card.pack(fill="x", pady=2)

    @staticmethod
    def _column_signature(mods):
        """Identifies what's actually rendered in a column."""
        return tuple((m["link"], m.get("version"), m.get("prev_version"), m.get("is_fresh"))
                     for m in mods)

    # ── Timer ──────────────────────────────────────────────────────────

    def _schedule_next(self):
        self._next_check_ts = time.time() + CHECK_INTERVAL_MINUTES * 60
        self._tick_timer()

    def _tick_timer(self):
        # _do_show calls this directly (in addition to whatever chain is
        # already pending from before the window was hidden), so cancel any
        # previously scheduled tick first -- otherwise repeated hide/show
        # cycles stack up duplicate timer chains that never get cleaned up.
        if self._timer_after_id is not None:
            self.root.after_cancel(self._timer_after_id)
            self._timer_after_id = None

        if self._next_check_ts is None:
            return
        left = max(0, int(self._next_check_ts - time.time()))
        if left <= 0:
            self._check_now()
            return
        if self._visible:
            m, s = divmod(left, 60)
            self._lbl_timer.configure(text=f"Next check in {m:02d}:{s:02d}")
            self._timer_after_id = self.root.after(1000, self._tick_timer)
        else:
            self._timer_after_id = self.root.after(left * 1000, self._tick_timer)

    # ── Run ────────────────────────────────────────────────────────────

    def run(self):
        self._schedule_update_check()
        self.root.mainloop()
