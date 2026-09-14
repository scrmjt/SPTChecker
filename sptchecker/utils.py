import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_VERSION_PART_RE = re.compile(r"\d+")


def parse_version(v):
    """Dotted version string -> tuple of ints, for numeric comparison.
    Returns None if it doesn't contain anything version-shaped. Leading
    decoration is ignored, so a release tag ("V3.3.1") and a bare version
    ("3.3.1") parse identically."""
    if not v:
        return None
    parts = _VERSION_PART_RE.findall(v)
    return tuple(int(p) for p in parts) if parts else None


def pad_versions(a, b):
    """Zero-pad the shorter tuple so (1, 0, 2) and (1, 0, 2, 0) -- the same
    version, just written with a different number of segments -- compare as
    equal instead of the longer one looking "newer" by tuple length alone."""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def is_newer(available, current):
    """True only if `available` is numerically greater than `current`.

    Shared by mod matching and the app's own update check, which have the
    same requirement: a plain string inequality would call a *downgrade* an
    update purely because the strings differ. Unparseable versions are
    treated as no-update rather than guessed at -- better to miss a rare
    oddly-formatted version than to tell someone to "update" to something
    older.
    """
    a, c = parse_version(available), parse_version(current)
    if a is None or c is None:
        return False
    a, c = pad_versions(a, c)
    return a > c


_SPT_CONSTRAINT_RE = re.compile(
    r"^\s*(~|>=|>)?\s*(\d+)\.(\d+)\.(\d+)\s*$"
)


def _parse_spt_constraint(constraint):
    """Parse a Forge `spt_version_constraint` string into (op, (maj, min,
    patch)), or None if it's empty/unparseable.

    Confirmed live against the Forge API (sp-mod.com/api/v0/mod/<id>), the
    three forms actually in use are:
      - "~X.Y.Z"  -- tilde range: >=X.Y.Z, <X.(Y+1).0. SPT patch releases
        within a minor don't usually break the modding API, so this is a
        mod saying "this patch and the rest of that minor line".
      - "X.Y.Z"   -- bare version: treated as an exact match only. SPT
        patches within a minor line do occasionally break mods, and a mod
        author who pinned an exact version (rather than using "~") is
        telling us they haven't verified anything past it.
      - ">=X.Y.Z" or "> X.Y.Z" -- open-ended lower bound, no upper cap.

    Shared by spt_constraint_matches (does a target version satisfy this?)
    and spt_constraint_floor (what version does this constraint name?).
    """
    if not constraint:
        return None
    m = _SPT_CONSTRAINT_RE.match(constraint)
    if not m:
        return None
    op = m.group(1) or "=="
    lo = tuple(int(m.group(i)) for i in (2, 3, 4))
    return op, lo


def spt_constraint_matches(constraint, target):
    """Does a mod version's `spt_version_constraint` cover this `target` SPT
    version? An empty, missing, or unparseable constraint returns False
    rather than guessing -- for a filter whose whole point is "only tell me
    what's actually compatible", an unverifiable claim of compatibility is
    worse than an omission. See _parse_spt_constraint for the constraint forms.
    """
    if not target:
        return False
    parsed = _parse_spt_constraint(constraint)
    t = parse_version(target)
    if not parsed or t is None:
        return False
    op, lo = parsed
    t3 = (t + (0, 0, 0))[:3]
    if op == "~":
        hi = (lo[0], lo[1] + 1, 0)
        return lo <= t3 < hi
    if op == ">=":
        return t3 >= lo
    if op == ">":
        return t3 > lo
    return t3 == lo


def spt_constraint_floor(constraint):
    """The lower-bound SPT version a Forge `spt_version_constraint` names
    (e.g. "~4.0.13" or ">=4.1.3" -> "4.0.13"/"4.1.3"), regardless of which
    range operator it uses -- every form in use names a specific version as
    its floor. None if empty/unparseable.

    Used to build the target-version picker's choice list from what mods on
    the Forge actually, currently declare -- live data instead of a
    hardcoded, staleness-prone list of SPT's own release history.
    """
    parsed = _parse_spt_constraint(constraint)
    return "%d.%d.%d" % parsed[1] if parsed else None


def parse_dt(ts_str):
    """Parse an ISO or RFC 2822 timestamp (RSS vs API formats) into an aware datetime."""
    if not ts_str:
        return None
    try:
        try:
            dt = parsedate_to_datetime(ts_str)
        except Exception:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
