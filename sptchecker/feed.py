import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone

import requests

from .config import (
    API_MOD_URL, API_MODS_UPDATES_URL, API_URL, APP_VERSION, DC_NS, FEED_URL,
    FEED_UPDATED_URL, MODS_UPDATES_CHUNK_SIZE,
    PUBLISHED_CHUNK_SIZE as _PUBLISHED_CHUNK_SIZE,
    DEFAULT_TARGET_SPT_VERSION,
)
from .utils import parse_dt, parse_version, spt_constraint_floor, spt_constraint_matches

# Sort fallback for an entry with a missing or unparseable timestamp: sorts to
# the bottom rather than raising or landing arbitrarily among real dates.
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)

_MOD_ID_RE = re.compile(r"/mod/(\d+)/")
_VERSION_V_PREFIX_RE = re.compile(r"^\s*[vV](?=\d)")

_session = requests.Session()
_session.headers["User-Agent"] = f"SPTModChecker/{APP_VERSION}"

_API_HEADERS = {"Accept": "application/json"}
# The RSS routes previously sent no Accept at all, leaving content negotiation
# to the server's default -- which is the HTML page, not the feed.
_RSS_HEADERS = {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}

# Rate limits as published by sp-mod.com's maintainer: 50 requests per minute
# per IP against /api/v0/, which earns a one minute timeout once exceeded, and
# 150/minute for everything else before a challenge is served. Metered as two
# independent budgets because that is how the server counts them -- charging
# RSS and thumbnail traffic to the API allowance would throttle local scans for
# no reason at all. Both sit under the published ceiling, since the app cannot
# see what else shares its IP: a second copy running, or the user's browser on
# the Forge in another window.
_API_RATE_LIMIT = 40
_MEDIA_RATE_LIMIT = 100
_RATE_WINDOW_SECONDS = 60.0

# Kept alongside the windows below purely to smooth bursts. Without it a scan
# would spend a full minute's budget in its first ten seconds and then sit
# still, which is compliant but reads to the user as a freeze.
_REQUEST_MIN_INTERVAL = 0.25
_throttle_lock = threading.Lock()
_last_request_ts = 0.0


class _RateLimiter:
    """Sliding-window limiter: at most `limit` requests in any `window`
    seconds, blocking callers past that until the oldest falls back out.

    A moving window rather than a fixed delay per request, so a routine check
    (a handful of requests) still completes promptly while a long local scan
    settles into a sustainable rate instead of front-loading the whole budget.
    Also strictly safer than the fixed windows a server typically counts in:
    no 60 second span can ever contain more than `limit` requests, whereas a
    fixed delay can still stack either side of a window boundary.
    """

    def __init__(self, limit, window):
        self._limit = limit
        self._window = window
        self._hits = deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                cutoff = time.monotonic() - self._window
                while self._hits and self._hits[0] <= cutoff:
                    self._hits.popleft()
                if len(self._hits) < self._limit:
                    self._hits.append(time.monotonic())
                    return
                wait = self._hits[0] - cutoff
            # Slept outside the lock so other threads can still retire their
            # own entries meanwhile; the loop re-checks under it, so waking
            # early or losing the race to another waiter is harmless.
            time.sleep(max(wait, 0.05))


_api_limiter = _RateLimiter(_API_RATE_LIMIT, _RATE_WINDOW_SECONDS)
_media_limiter = _RateLimiter(_MEDIA_RATE_LIMIT, _RATE_WINDOW_SECONDS)


def _limiter_for(url):
    """Pick the budget a URL is metered against -- see the limits above."""
    return _api_limiter if "/api/v0/" in url else _media_limiter


class ForgeBlocked(Exception):
    """The host answered, but refused to serve us at the edge rather than at
    the application -- currently a Cloudflare interactive challenge.

    Distinct from an ordinary HTTP error because the remedy is different and
    nothing the app can retry its way out of: no header, endpoint or backoff
    changes the outcome, since passing the challenge requires executing its
    JavaScript in a real browser to earn a `cf_clearance` cookie. Raised as
    its own type so the check flow can report the real situation instead of
    surfacing a bare "403 Client Error" that reads like a bug in the app.
    """


class ForgeRateLimited(Exception):
    """The Forge refused a request because we asked too often.

    Raised only once the 429 retry budget in _forge_request is spent, so by
    the time a caller sees this, waiting it out has already been tried. Kept
    as its own type because it means "we could not find out", not "there is
    nothing there" -- a local-mod lookup that quietly swallowed it would
    report an installed mod as missing from the Forge while its listing sits
    there perfectly intact, which is a worse lie than admitting the check
    didn't happen.
    """


def get_session():
    return _session


def media_request(method, url, **kw):
    """Fetch a non-API asset -- mod thumbnails on files.sp-mod.com -- under
    the non-API budget.

    Separate from _forge_request because these aren't API calls: no JSON, no
    challenge handling, and no 429 retry, since a thumbnail that doesn't
    arrive is cosmetic and falls back to a placeholder. They are still
    requests to the same IP-metered host though, so they have to be counted;
    they previously went straight out through the bare session and were the
    one thing in the app no limiter could see.
    """
    _media_limiter.acquire()
    return _session.request(method, url, **kw)


def _is_challenge(resp):
    """True when a response is a Cloudflare bot challenge rather than content.

    `Cf-Mitigated: challenge` is the explicit signal and is checked first; the
    status/content-type pair is a fallback for edge configs that omit it. Both
    matter because the challenge is served as 403 with an HTML body, which is
    otherwise indistinguishable from a genuine application-level 403.
    """
    if resp.headers.get("Cf-Mitigated", "").lower() == "challenge":
        return True
    return (resp.status_code in (403, 503)
            and resp.headers.get("Server", "").lower() == "cloudflare"
            and "text/html" in resp.headers.get("Content-Type", ""))


def _forge_request(method, url, retries=3, **kw):
    """Single chokepoint for all Forge requests: enforces a minimum interval
    between requests app-wide and retries on 429 -- a silently-swallowed 429
    looks identical to a real "not found" to callers otherwise. Returns the
    response without raising; callers check status.

    Two conditions raise instead of returning. An edge challenge raises
    ForgeBlocked: it is not a per-request condition callers can meaningfully
    handle one at a time, and retrying only burns the rate-limit budget. A 429
    that outlives the retry budget raises ForgeRateLimited rather than handing
    back the 429 for callers to mistake for an empty result.
    """
    global _last_request_ts
    limiter = _limiter_for(url)
    for attempt in range(retries + 1):
        limiter.acquire()
        with _throttle_lock:
            wait = _REQUEST_MIN_INTERVAL - (time.monotonic() - _last_request_ts)
            if wait > 0:
                time.sleep(wait)
            _last_request_ts = time.monotonic()
        resp = _session.request(method, url, **kw)
        if _is_challenge(resp):
            raise ForgeBlocked(
                "sp-mod.com is blocking automated requests (Cloudflare challenge)"
            )
        if resp.status_code != 429:
            return resp
        if attempt == retries:
            raise ForgeRateLimited("sp-mod.com is rate limiting this client")
        time.sleep(float(resp.headers.get("Retry-After", 2)))


def strip_html(raw):
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


CHANGELOG_MAX_CHARS = 5000


def _truncate(text, limit):
    """Truncate at a word boundary near `limit` to avoid cutting mid-markdown-token."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit - 50:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _best_compatible_version(versions, target_spt_version):
    """Of `versions` (a mod's full version history, newest first as the
    Forge returns it), the highest-versioned entry whose
    spt_version_constraint covers `target_spt_version` -- or None if the
    mod has never published one.

    Deliberately re-ranks by the version *number* rather than trusting the
    Forge's own ordering: that ordering is by publish date, and a mod that
    back-ported a fix to an older SPT line after already shipping a newer
    SPT-only release would otherwise have the back-port passed over.
    """
    best, best_key = None, None
    for v in versions:
        if not spt_constraint_matches(v.get("spt_version_constraint"), target_spt_version):
            continue
        key = parse_version(v.get("version", ""))
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key, best = key, v
    return best


def _parse_api_mod(item, version=None):
    """Map a raw /api/v0/mods item (as returned with include=versions,category) to
    this app's internal mod dict shape.

    `version` is the specific version entry to surface as this mod's
    "version"/"changelog"/"updated" -- normally the one a caller selected
    for SPT-compatibility (see _best_compatible_version). Callers that don't
    care about compatibility (e.g. local-mod matching, which checks
    compatibility separately via the authoritative /mods/updates endpoint)
    can omit it, and get the Forge's own newest entry -- unchanged from
    this function's original behaviour.
    """
    versions = item.get("versions", [])
    latest = version if version is not None else (versions[0] if versions else {})
    owner = item.get("owner") or {}
    category = item.get("category") or {}

    return {
        "title": item.get("name", ""),
        "slug": item.get("slug", ""),
        "link": item.get("detail_url", ""),
        "guid": item.get("guid", ""),
        "author": owner.get("name", "Unknown"),
        "author_id": owner.get("id", ""),
        "author_since": owner.get("created_at", ""),
        "version": latest.get("version", ""),
        "category": category.get("title", ""),
        "published": item.get("published_at", ""),
        "updated": latest.get("created_at", item.get("updated_at", "")),
        "thumb_url": item.get("thumbnail", ""),
        "description": (item.get("teaser", "") or "")[:300],
        "full_description": item.get("teaser", "") or "",
        "changelog": _truncate(latest.get("description", "") or "", CHANGELOG_MAX_CHARS),
    }


def _fetch_mods(params, timeout=15, target_spt_version=None):
    """Fetch and parse one page of mods from the API; [] on any failure,
    matching this module's fail-soft convention.

    ForgeBlocked and ForgeRateLimited are deliberately not swallowed:
    fail-soft exists to keep one flaky request from taking down a check, but
    neither of those is one flaky request. An edge block affects every request
    equally, and degrading it to [] would present a site-wide outage as "no
    mods found"; a rate limit means the answer is unknown, and local-mod
    matching has to be able to tell that apart from a genuine miss.

    When `target_spt_version` is given, a mod is included only if it has a
    published version compatible with it (see _best_compatible_version), and
    that version -- not the Forge's own newest -- is what gets surfaced as
    the mod's displayed version/changelog. Callers that need identity
    lookups regardless of compatibility (local-mod matching) leave this None.
    """
    try:
        resp = _forge_request("get", API_URL, params={"include": "versions,category", **params},
                              headers=_API_HEADERS, timeout=timeout)
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if target_spt_version is None:
            return [_parse_api_mod(item) for item in items]
        mods = []
        for item in items:
            best = _best_compatible_version(item.get("versions", []), target_spt_version)
            if best is not None:
                mods.append(_parse_api_mod(item, version=best))
        return mods
    except (ForgeBlocked, ForgeRateLimited):
        raise
    except Exception:
        return []


def _fetch_api_mods(sort="-updated_at", target_spt_version=None):
    """Fetch mods from the API with the given sort order."""
    return _fetch_mods({"sort": sort, "per_page": 50}, timeout=30,
                       target_spt_version=target_spt_version)


def list_known_spt_versions(sample_size=50):
    """Distinct SPT versions currently declared compatible by at least one
    published version of at least one mod on the Forge -- the lower bound
    of each version's spt_version_constraint, across a sample of the most
    recently updated mods. Powers the target-version picker with live data
    instead of a hardcoded, staleness-prone list of SPT's own release
    history: as soon as mods start targeting a new SPT release, it appears
    here on its own, and an old line drops off once nothing recent still
    supports it. Sorted newest first. [] on any failure -- callers should
    keep whatever choice list they already have rather than treat this as
    "no versions exist".
    """
    try:
        resp = _forge_request("get", API_URL,
                              params={"include": "versions", "sort": "-updated_at",
                                      "per_page": sample_size},
                              headers=_API_HEADERS, timeout=20)
        resp.raise_for_status()
        items = resp.json().get("data", [])
    except Exception:
        return []
    seen = set()
    for item in items:
        for v in item.get("versions", []):
            floor = spt_constraint_floor(v.get("spt_version_constraint"))
            if floor:
                seen.add(floor)
    return sorted(seen, key=parse_version, reverse=True)


def lookup_by_guid(guid):
    """Exact-match a locally-scanned mod's GUID against the Forge catalog.

    Forge exposes `guid` as a filterable field (confirmed live against
    filter[guid]=<value>), so this is a single indexed lookup rather than a
    search -- the primary local-mod matching strategy. Returns None on no
    match, ambiguous results, or any request failure.
    """
    if not guid:
        return None
    mods = _fetch_mods({"filter[guid]": guid})
    if len(mods) != 1 or mods[0].get("guid") != guid:
        return None
    return mods[0]


def lookup_by_name(name):
    """Fallback search by name for local mods that yielded no clean GUID match.

    filter[name] does a partial/contains-style match server-side, so callers
    should rank the results themselves rather than assume the first is right.
    """
    return _fetch_mods({"filter[name]": name, "per_page": 20}) if name else []


def lookup_by_query(term):
    """Fuzzy/full-text search, for local mods filter[name] can't find.

    filter[name] only matches a term that's a literal substring of the
    stored title -- it can't bridge a local mod's internal name (often
    camelCase/dotted developer shorthand) to a differently-worded Forge
    listing. `query=` is a separate, undocumented parameter (confirmed live
    against the API) that does real fuzzy matching instead, at the cost of
    returning looser candidates -- callers still need to rank results
    themselves, same as lookup_by_name.
    """
    return _fetch_mods({"query": term, "per_page": 20}) if term else []


def lookup_updates(pairs, spt_version):
    """Batch-check locally-installed mods against Forge's own authoritative
    update logic: given (guid, installed_version) pairs and the user's
    actual installed SPT version, Forge judges whether a newer version
    exists, whether it's actually compatible with that SPT version, and
    whether installing it would violate another mod's dependency
    constraint -- none of which a local version-string comparison can know.
    Confirmed live: this catches false "update available" flags that a
    local numeric-newer check alone lets through -- Forge treats a matched
    mod's version history as the source of truth, not just "is the number
    bigger".

    Chunked since the API's mods= list has no documented length cap, and
    merged into one dict of the four Forge result buckets: updates,
    blocked_updates, up_to_date, incompatible_with_spt. A chunk that fails
    (network hiccup, one bad identifier) is skipped rather than failing the
    whole batch -- callers should treat a pair absent from every bucket as
    "no authoritative answer" and keep their own fallback, not as "up to
    date".
    """
    merged = {"updates": [], "blocked_updates": [], "up_to_date": [], "incompatible_with_spt": []}
    if not pairs or not spt_version:
        return merged
    for i in range(0, len(pairs), MODS_UPDATES_CHUNK_SIZE):
        chunk = pairs[i:i + MODS_UPDATES_CHUNK_SIZE]
        mods_param = ",".join(f"{guid}:{version}" for guid, version in chunk)
        try:
            resp = _forge_request("get", API_MODS_UPDATES_URL,
                                  params={"mods": mods_param, "spt_version": spt_version},
                                  headers=_API_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except Exception:
            continue
        for key in merged:
            merged[key].extend(data.get(key, []))
    return merged


def _parse_rss(url):
    """Fetch and parse an RSS feed into mod dicts; [] if the feed is
    unavailable or malformed.

    Fail-soft because RSS is the app's secondary source -- everything it
    carries is also available from the API, which fetch_feeds() has already
    queried by the time this runs. Previously this was the one unguarded
    request in the whole startup path, so an RSS-only problem (a feed route
    that moved, a transient 5xx, a truncated body) aborted the entire check
    and surfaced a raw HTTP error, despite usable API results sitting right
    there. A site-wide block still propagates -- see _fetch_mods.
    """
    try:
        resp = _forge_request("get", url, headers=_RSS_HEADERS, timeout=30)
        resp.raise_for_status()
        return _extract_mods(ET.fromstring(resp.content))
    except ForgeBlocked:
        raise
    except Exception:
        return []


def _extract_mods(root):
    """Extract mod dicts from parsed RSS XML."""
    mods = []
    for item in root.findall(".//item"):
        link = item.findtext("link", "")
        if not link:
            continue
        pub = item.findtext("pubDate", "")
        enc = item.find("enclosure")
        thumb = enc.get("url", "") if enc is not None else ""
        desc_raw = item.findtext("description", "")
        full_desc = strip_html(desc_raw)

        mods.append({
            "title": item.findtext("title", ""),
            "link": link,
            "author": item.findtext(f"{{{DC_NS}}}creator", "Unknown"),
            # RSS writes the version as "v1.2.3" where the API gives "1.2.3",
            # and cards render whichever they're handed verbatim -- so the two
            # sources produced visibly different rows for the same field.
            # Normalise to the API's form, since entries from both now sit in
            # one list together.
            "version": _VERSION_V_PREFIX_RE.sub(
                "", item.findtext(f"{{{DC_NS}}}identifier", "")),
            "category": item.findtext("category", ""),
            "published": pub,
            "updated": item.findtext(f"{{{DC_NS}}}date", pub),
            "thumb_url": thumb,
            "description": full_desc[:300],
            "full_description": full_desc,
        })
    return mods


def unpublished_links(links):
    """Of these mod links, which are no longer published?

    Replaces what used to be one HEAD request per mod against its rendered
    HTML page. sp-mod.com serves API responses roughly 30x faster than mod
    pages (measured: ~0.02s vs ~0.58s each), and a display refresh checks 14
    mods, so the old approach cost ~8s per check and dominated the whole
    cycle. One indexed filter[id] lookup answers for all of them at once.

    Returns the set of links that are definitively gone -- everything else
    stays displayed. Every uncertain path deliberately reports "nothing is
    unpublished" rather than guessing: these mods came out of the live feed
    moments earlier, so a mod vanishing is the rare case and a request
    failure is the likely one. Wrongly hiding a real mod is far worse than
    briefly showing one that just got pulled.
    """
    by_id = {}
    for link in links:
        m = _MOD_ID_RE.search(link)
        # No parseable id -- can't ask about it, so leave it displayed.
        if m:
            by_id.setdefault(m.group(1), []).append(link)
    if not by_id:
        return set()

    found = set()
    ids = list(by_id)
    for i in range(0, len(ids), _PUBLISHED_CHUNK_SIZE):
        chunk = ids[i:i + _PUBLISHED_CHUNK_SIZE]
        try:
            resp = _forge_request("get", API_URL, headers=_API_HEADERS, timeout=20,
                                  params={"filter[id]": ",".join(chunk),
                                          "fields": "id",
                                          "per_page": _PUBLISHED_CHUNK_SIZE})
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception:
            return set()
        # Asking about several live-feed mods and being told none of them
        # exist is far more likely an API contract change than a simultaneous
        # mass unpublish -- treat it as inconclusive rather than blanking the
        # display. A single-id chunk is still trusted, so real one-off
        # unpublishes are still caught.
        if not data and len(chunk) > 1:
            return set()
        found.update(str(item.get("id")) for item in data)

    return {link for mod_id, group in by_id.items() if mod_id not in found
            for link in group}


def lookup_mod_by_id(mod_id):
    """Fetch one mod by its Forge id, in this app's internal mod shape.

    Used for the app's own listing, to find out whether a newer release has
    been published. Routed through _forge_request like everything else, so
    it spends from the same metered API budget rather than sneaking an extra
    request past it. Returns None on any failure -- the caller treats not
    knowing as "no update", which is the quiet outcome.
    """
    try:
        resp = _forge_request("get", f"{API_MOD_URL}/{mod_id}",
                              params={"include": "versions"},
                              headers=_API_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data")
    except Exception:
        return None
    return _parse_api_mod(data) if isinstance(data, dict) else None


def fetch_author_id(mod_link):
    """On-demand lookup of a mod's owner id from its page link.

    Used to backfill an author's Forge profile id when it wasn't captured during
    a regular check -- e.g. an older mod that hasn't cycled back through the
    "most recent" API window since author_id started being tracked. Returns None
    on any failure (bad link, network error, missing owner).
    """
    m = _MOD_ID_RE.search(mod_link)
    if not m:
        return None
    try:
        resp = _forge_request("get", f"{API_MOD_URL}/{m.group(1)}",
                              headers=_API_HEADERS, timeout=15)
        resp.raise_for_status()
        owner = resp.json().get("data", {}).get("owner") or {}
        return owner.get("id")
    except Exception:
        return None


def _merge_api_first(api_mods, rss_mods):
    """Merge both sources, keeping the API's copy of any mod carried by both.

    Order matters, and it used to be the other way round. The API holds the
    real version records, so a mod that has just published a new version shows
    that new version here first; RSS lags behind it. When the stale RSS copy
    won this merge, the mod kept its *old* version string and *old* timestamp,
    which then sorted it back down the list and clean out of the visible
    window -- so a mod could update and simply never appear under "Recently
    Updated", which is that column's entire job. Confirmed live: this app's own
    listing sat 24th showing the previous version, while the API had the new
    one 2nd.

    RSS is still merged in behind it, because the feed reaches further back
    than the single page the API fetches and contributes entries that window
    doesn't cover.
    """
    seen = set()
    merged = []
    for mod in list(api_mods) + list(rss_mods):
        if mod["link"] not in seen:
            seen.add(mod["link"])
            merged.append(mod)
    return merged


def fetch_feeds(target_spt_version=None):
    """Fetch newest and recently updated mods from RSS feeds + API.

    `target_spt_version`, when truthy, filters both columns to mods with a
    version compatible with it (see _best_compatible_version) -- omit it (or
    pass None) to fall back to DEFAULT_TARGET_SPT_VERSION, or pass ""
    explicitly to disable filtering outright and show every mod's actual
    newest version, same as upstream. A caller (the app's header picker) is
    expected to resolve the real target -- manual entry or auto-detected from
    an install folder -- and pass it in explicitly on every check.
    """
    effective = target_spt_version if target_spt_version is not None else DEFAULT_TARGET_SPT_VERSION
    # "" collapses to None here, same as never passing a target -- both mean
    # "don't filter" to _fetch_mods, which only special-cases None.
    filter_arg = effective or None

    api_updated = _fetch_api_mods(sort="-updated_at", target_spt_version=filter_arg)
    api_newest = _fetch_api_mods(sort="-created_at", target_spt_version=filter_arg)

    # RSS entries carry no per-version spt_version_constraint data at all, so
    # there's nothing to check them against -- merging one in under a version
    # filter would silently let an unverified (possibly incompatible) mod
    # through. Skip RSS entirely while filtering is on and rely on the API's
    # own fetched window; with it off, behave exactly as before.
    if filter_arg:
        rss_newest, rss_updated = [], []
    else:
        # Either source going missing degrades to the other rather than
        # blanking a column: a feed failure used to empty "new mods"
        # outright, even with API results already in hand.
        rss_newest = _parse_rss(FEED_URL)
        rss_updated = _parse_rss(FEED_UPDATED_URL)
    newest = _merge_api_first(api_newest, rss_newest)

    # RSS entries lack several API-only fields -- build per-link lookups from the
    # API sets and enrich both columns, whichever source each entry came from.
    all_api = {m["link"]: m for m in api_updated + api_newest}
    enrich_fields = ("changelog", "full_description", "author_since", "author_id")
    lookups = {
        field: {link: m[field] for link, m in all_api.items() if m.get(field)}
        for field in enrich_fields
    }
    # RSS's pubDate reflects when the listing was *created* (often drafted well
    # before it actually goes live), not when it was published -- confirmed live
    # against the API's published_at, which is the true publish timestamp. That
    # skew is what makes the daily-activity graph undercount "today" and only
    # catch up once the date rolls over. The API value always wins when
    # available; RSS's pubDate is only a fallback for mods outside the API's
    # fetched window.
    published_lookup = {link: m["published"] for link, m in all_api.items() if m.get("published")}

    def _enrich(mod):
        api_published = published_lookup.get(mod["link"])
        if api_published:
            mod["published"] = api_published
        for field in enrich_fields:
            mod[field] = mod.get(field) or lookups[field].get(mod["link"], "")

    combined = _merge_api_first(api_updated, rss_updated)
    for mod in newest:
        _enrich(mod)
    for mod in combined:
        _enrich(mod)

    # Sorted on parsed datetimes, not raw strings. The two sources spell the
    # same instant differently ("...T15:22:08.000000Z" against
    # "...T13:31:41+00:00"), and RSS dates entries that predate the API window
    # in RFC 2822 -- string ordering across those is meaningless, and entries
    # from both now share one list. Enrichment runs first, since it replaces
    # RSS's publish time with the API's more accurate one.
    newest.sort(key=lambda m: parse_dt(m.get("published")) or _OLDEST, reverse=True)
    combined.sort(key=lambda m: parse_dt(m.get("updated")) or _OLDEST, reverse=True)

    return newest, combined
