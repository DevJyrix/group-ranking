"""
Roblox Group Horns Ranker / Accepter
=====================================
Runs on Railway as a scheduled cron job.

AUTH SETUP:
  - ROBLOX_API_KEY  : Open Cloud API key (create.roblox.com/credentials)
                      Used for group write actions (accept/rank members).
                      Needs the "Group" resource with Write permission.
  - ROBLOX_COOKIE   : .ROBLOSECURITY cookie of a group admin account.
                      Used ONLY for inventory checks (Open Cloud doesn't
                      expose inventory endpoints yet). If your group's
                      inventory checks are all public, this is optional.

WHAT IT DOES:
  - Scans pending join requests → accepts horn owners, declines the rest
  - Scans existing members → ranks horn owners to your target rank
  Toggle each behaviour with ACCEPT_PENDING and RANK_MEMBERS env vars.

OWNERSHIP DETECTION (works even on private inventories):
  Layer 1 — Avatar/wearing check     (always public, fastest)
  Layer 2 — Collectibles inventory   (works for limiteds even if private,
                                      when your cookie is a group admin)
  Layer 3 — is-owned per asset ID    (public inventories, free fallback)

TRACKED ITEMS (all official Roblox-published Flaming Horns series):
  215718515       Fiery Horns of the Netherworld        (2015 limited)
  74891470        Frozen Horns of the Frigid Planes     (2017 limited)
  1744060292      Poisoned Horns of the Toxic Wasteland (2018/2022 limited)
  76479271580913  Stormbreak Horns of the Tempest Skies (June 2026)

ENVIRONMENT VARIABLES:
  ROBLOX_API_KEY    — Open Cloud key (group read/write)         [required]
  ROBLOX_COOKIE     — .ROBLOSECURITY cookie for inventory reads [recommended]
  GROUP_ID          — numeric ID of your group                   [required]
  HORNS_RANK_ID     — role ID to assign to horn owners           [required if RANK_MEMBERS=true]
  ACCEPT_PENDING    — true/false (default true)
  RANK_MEMBERS      — true/false (default true)
  DECLINE_NON_OWNERS— true/false (default true) — decline pending w/o horns
  DRY_RUN           — true/false (default false) — log without acting
  DISCORD_WEBHOOK   — optional summary webhook URL
"""

import os
import sys
import time
import json
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("horns_ranker")


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default).strip()

def _env_bool(key, default=True):
    v = _env(key).lower()
    if v == "true":  return True
    if v == "false": return False
    return default

ROBLOX_API_KEY    = _env("ROBLOX_API_KEY")       # Open Cloud key — group actions
ROBLOSECURITY     = _env("ROBLOX_COOKIE")         # cookie — inventory checks only
GROUP_ID          = int(_env("GROUP_ID", "0"))
HORNS_RANK_ID     = int(_env("HORNS_RANK_ID", "0"))
ACCEPT_PENDING    = _env_bool("ACCEPT_PENDING",     True)
RANK_MEMBERS      = _env_bool("RANK_MEMBERS",       True)
DECLINE_NON_OWNERS= _env_bool("DECLINE_NON_OWNERS", True)
DRY_RUN           = _env_bool("DRY_RUN",            False)
DISCORD_WEBHOOK   = _env("DISCORD_WEBHOOK")
RANKED_WEBHOOK   = _env("RANKED_WEBHOOK")

# Official Flaming Horns series asset IDs (confirmed from roblox.com/catalog URLs)
HORNS_ASSET_IDS = {
    215718515,        # Fiery Horns of the Netherworld
    74891470,         # Frozen Horns of the Frigid Planes
    1744060292,       # Poisoned Horns of the Toxic Wasteland
    76479271580913,   # Stormbreak Horns of the Tempest Skies
}
HORNS_NAMES = {
    215718515:       "Fiery Horns of the Netherworld",
    74891470:        "Frozen Horns of the Frigid Planes",
    1744060292:      "Poisoned Horns of the Toxic Wasteland",
    76479271580913:  "Stormbreak Horns of the Tempest Skies",
}

CALL_DELAY  = 0.4   # seconds between per-user checks
BATCH_PAUSE = 1.5   # seconds between page fetches
CACHE_FILE  = ".horns_cache.json"  # track checked members to avoid re-checking


def get_group_roles(group_id: int) -> list[dict]:
    """Return all roles for the group, or [] if the request fails."""
    data = inv_get(f"https://groups.roblox.com/v1/groups/{group_id}/roles")
    if not data:
        return []
    return data.get("roles", []) if isinstance(data.get("roles", []), list) else []


def resolve_horns_role_id(group_id: int, role_id: int) -> int:
    """Return a valid group role ID for the requested value.

    If the env value is already a role ID, return it when valid.
    Otherwise, treat the value as a role rank and map it to the matching role ID.
    """
    if role_id <= 0:
        return 0
    roles = get_group_roles(group_id)
    if not roles:
        log.warning("Unable to validate HORNS_RANK_ID because group roles could not be fetched")
        return role_id

    for role in roles:
        if role.get("id") == role_id:
            return role_id

    for role in roles:
        if role.get("rank") == role_id:
            mapped = role.get("id")
            log.info(
                "Mapped HORNS_RANK_ID env value %s to actual role id %s (%s)",
                role_id, mapped, role.get("name", "<unknown>"),
            )
            return mapped

    log.warning(
        "HORNS_RANK_ID %s is not a valid role ID or role rank for group %s",
        role_id, group_id,
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  CACHE MANAGER — track already-checked members
# ─────────────────────────────────────────────────────────────────────────────
class Cache:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                    data["checked_pending"] = set(data.get("checked_pending", []))
                    data.setdefault("checked_members", {})
                    return data
            except (json.JSONDecodeError, IOError):
                pass
        return {"checked_members": {}, "checked_pending": set()}

    def save(self):
        # Convert sets to lists for JSON serialization
        save_data = {
            "checked_members": self.data["checked_members"],
            "checked_pending": list(self.data["checked_pending"]),
        }
        with open(self.filepath, "w") as f:
            json.dump(save_data, f, indent=2)

    def mark_member_checked(self, user_id: int, role_id: int):
        """Record that we checked a member (store their role for next run)."""
        self.data["checked_members"][str(user_id)] = {"role_id": role_id, "checked_at": time.time()}

    def is_member_checked(self, user_id: int, current_role_id: int) -> bool:
        """Skip if already checked AND still at same rank (not an owner yet)."""
        cached = self.data["checked_members"].get(str(user_id))
        if cached and cached.get("role_id") == current_role_id:
            return True  # Don't re-check
        return False

    def mark_pending_checked(self, request_id: str):
        """Record that we processed a pending request."""
        self.data["checked_pending"].add(str(request_id))

    def was_pending_checked(self, request_id: str) -> bool:
        """Skip pending requests we already processed (declined or accepted)."""
        return str(request_id) in self.data["checked_pending"]

    def reset_pending(self):
        """Clear pending cache each run (it changes frequently)."""
        self.data["checked_pending"] = set()


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP SESSIONS
#  Two sessions: one for Open Cloud (API key auth), one for legacy endpoints
#  (cookie auth, used only for inventory reads).
# ─────────────────────────────────────────────────────────────────────────────
def _make_session(extra_headers: dict = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PATCH", "DELETE"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "Mozilla/5.0 RobloxGroupRanker/2.0"})
    if extra_headers:
        s.headers.update(extra_headers)
    return s

# Session A — Open Cloud (group management)
OC_SESSION: requests.Session = None
# Session B — Cookie auth (inventory reads only)
INV_SESSION: requests.Session = None
# CSRF for legacy endpoints that need it
CSRF_TOKEN = ""


def init_sessions():
    global OC_SESSION, INV_SESSION
    OC_SESSION = _make_session({"x-api-key": ROBLOX_API_KEY})
    INV_SESSION = _make_session()
    if ROBLOSECURITY:
        INV_SESSION.cookies.set(".ROBLOSECURITY", ROBLOSECURITY, domain=".roblox.com")


def refresh_csrf():
    """Get a fresh CSRF token for legacy POST/PATCH/DELETE endpoints."""
    global CSRF_TOKEN
    try:
        r = INV_SESSION.post("https://auth.roblox.com/v2/logout", timeout=8)
        token = r.headers.get("x-csrf-token", "")
        if token:
            CSRF_TOKEN = token
            INV_SESSION.headers["X-Csrf-Token"] = CSRF_TOKEN
    except requests.RequestException:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  GENERIC REQUEST HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def oc_get(url, params=None):
    """GET via Open Cloud session."""
    try:
        r = OC_SESSION.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        log.debug("OC GET %s → %s", url, r.status_code)
    except requests.RequestException as e:
        log.warning("OC GET error %s: %s", url, e)
    return None


def oc_post(url, json_body=None):
    """POST via Open Cloud session."""
    try:
        r = OC_SESSION.post(url, json=json_body or {}, timeout=10)
        return r
    except requests.RequestException as e:
        log.warning("OC POST error %s: %s", url, e)
    return None


def oc_patch(url, json_body=None):
    """PATCH via Open Cloud session."""
    try:
        r = OC_SESSION.patch(url, json=json_body or {}, timeout=10)
        return r
    except requests.RequestException as e:
        log.warning("OC PATCH error %s: %s", url, e)
    return None


def oc_delete(url):
    """DELETE via Open Cloud session."""
    try:
        r = OC_SESSION.delete(url, timeout=10)
        return r
    except requests.RequestException as e:
        log.warning("OC DELETE error %s: %s", url, e)
    return None


def inv_get(url, params=None):
    """GET via cookie session (inventory reads)."""
    try:
        r = INV_SESSION.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        log.debug("INV GET %s → %s", url, r.status_code)
    except requests.RequestException as e:
        log.warning("INV GET error %s: %s", url, e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  OWNERSHIP DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def check_wearing(user_id: int) -> tuple[bool, str]:
    """
    Layer 1: Avatar wearing check.
    The avatar endpoint is always fully public — no auth, no privacy settings
    can hide it. Fastest layer, costs 1 request.
    """
    data = inv_get(f"https://avatar.roblox.com/v1/users/{user_id}/avatar")
    if data:
        for asset in data.get("assets", []):
            aid = asset.get("id", 0)
            if aid in HORNS_ASSET_IDS:
                return True, HORNS_NAMES.get(aid, str(aid))
    return False, ""


def check_is_owned(user_id: int) -> tuple[bool, str]:
    """
    Layer 2: is-owned per specific asset ID.
    Works for public inventories and returns bare true/false.
    One request per asset — we short-circuit on first hit.
    """
    for asset_id in HORNS_ASSET_IDS:
        try:
            r = INV_SESSION.get(
                f"https://inventory.roblox.com/v1/users/{user_id}/items/Asset/{asset_id}/is-owned",
                timeout=8,
            )
            if r.status_code == 200 and r.text.strip().lower() == "true":
                return True, HORNS_NAMES.get(asset_id, str(asset_id))
        except requests.RequestException:
            pass
        time.sleep(0.15)
    return False, ""


def check_collectibles(user_id: int) -> tuple[bool, str]:
    """
    Layer 3: Collectibles/limiteds inventory scan.
    The inventory v2 endpoint exposes tradable items to authenticated
    group admins even when the user has their inventory set to private.
    Used as the deeper fallback after layers 1 and 2 miss.
    """
    cursor = None
    while True:
        data = inv_get(
            f"https://inventory.roblox.com/v2/users/{user_id}/inventory",
            params={
                "assetTypes": "Hat,Accessory",
                "limit": 100,
                "sortOrder": "Asc",
                **({"cursor": cursor} if cursor else {}),
            },
        )
        if not data:
            break
        for item in data.get("data", []):
            aid = item.get("assetId", 0)
            if aid in HORNS_ASSET_IDS:
                return True, HORNS_NAMES.get(aid, str(aid))
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(CALL_DELAY)
    return False, ""


def user_owns_horns(user_id: int) -> tuple[bool, str]:
    """
    Run all ownership layers in order, short-circuit on first positive.
    Order chosen to minimise request count:
      1. Avatar (1 req, always works)
      2. is-owned (4 reqs max, fast, works on public accounts)
      3. Collectibles scan (N reqs, works on private accounts with admin cookie)
    Returns (True, item_name) or (False, "").
    """
    found, name = check_wearing(user_id)
    if found:
        return True, name
    time.sleep(CALL_DELAY)

    found, name = check_is_owned(user_id)
    if found:
        return True, name
    time.sleep(CALL_DELAY)

    if ROBLOSECURITY:  # only worth trying if we have a cookie
        found, name = check_collectibles(user_id)
        if found:
            return True, name

    return False, ""


def get_user_details(user_id: int) -> tuple[str, str]:
    """Return account creation date and avatar image URL for a Roblox user."""
    created = "unknown"
    avatar_url = ""

    try:
        r = OC_SESSION.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=10)
        if r.status_code == 200:
            profile = r.json()
            created = profile.get("created", "unknown")
    except requests.RequestException:
        pass

    try:
        r = INV_SESSION.get(
            f"https://avatar.roblox.com/v1/users/{user_id}/avatar", timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                avatar_url = data.get("imageUrl", "") or ""
    except requests.RequestException:
        pass

    return created, avatar_url


# ─────────────────────────────────────────────────────────────────────────────
#  GROUP API — using Open Cloud where possible, legacy endpoints as fallback
#
#  Open Cloud group endpoints (v2):
#    GET  /cloud/v2/groups/{groupId}/memberships
#    POST /cloud/v2/groups/{groupId}/memberships  (accept join request)
#    PATCH /cloud/v2/groups/{groupId}/memberships/{membershipId}  (change role)
#
#  Legacy endpoints (still needed for join-request decline and some reads):
#    GET  /v1/groups/{groupId}/join-requests
#    DELETE /v1/groups/{groupId}/join-requests/users/{userId}
# ─────────────────────────────────────────────────────────────────────────────
OC_BASE = "https://apis.roblox.com/cloud/v2"


def get_pending_members_legacy(group_id: int) -> list[dict]:
    """Fetch pending join requests via legacy endpoint (most reliable for this)."""
    pending = []
    cursor = None
    while True:
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        data = inv_get(
            f"https://groups.roblox.com/v1/groups/{group_id}/join-requests",
            params=params,
        )
        if not data:
            break
        for req in data.get("data", []):
            u = req.get("requester", {})
            uid = u.get("userId")
            request_id = req.get("id") or req.get("requestId") or uid
            if uid:
                pending.append({
                    "requestId": request_id,
                    "userId":    uid,
                    "username":  u.get("username", f"uid:{uid}"),
                })
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(BATCH_PAUSE)
    return pending


def accept_join_request(group_id: int, user_id: int) -> bool:
    """Accept a pending join request via Open Cloud v2, with a legacy fallback."""
    if DRY_RUN:
        log.info("    [DRY RUN] would accept uid=%s", user_id)
        return True

    r = oc_post(
        f"{OC_BASE}/groups/{group_id}/memberships",
        json_body={"userId": f"users/{user_id}"},
    )
    if r is not None and r.status_code in (200, 201, 204):
        log.info("    accept succeeded: uid=%s status=%s", user_id, r.status_code)
        return True

    if r is not None and r.status_code == 404:
        log.warning("    OC accept returned 404, falling back to legacy accept endpoint")
        return accept_join_request_legacy(group_id, user_id)

    if r is not None:
        log.warning("    accept failed: uid=%s status=%s body=%s", user_id, r.status_code, r.text[:400])
    else:
        log.warning("    accept failed: uid=%s no response", user_id)
    return False


def accept_join_request_legacy(group_id: int, user_id: int) -> bool:
    """Use legacy group endpoint to accept a pending join request."""
    if DRY_RUN:
        log.info("    [DRY RUN] would legacy-accept uid=%s", user_id)
        return True
    if not ROBLOSECURITY:
        log.warning("    legacy accept requires ROBLOX_COOKIE")
        return False

    url = f"https://groups.roblox.com/v1/groups/{group_id}/join-requests/users/{user_id}"
    try:
        r = INV_SESSION.post(url, timeout=10)
        if r.status_code == 403 and "Token Validation Failed" in r.text:
            log.info("    legacy accept auth failed, refreshing CSRF token")
            refresh_csrf()
            r = INV_SESSION.post(url, timeout=10)
        if r.status_code in (200, 204):
            log.info("    legacy accept succeeded: uid=%s status=%s", user_id, r.status_code)
            return True
        log.warning("    legacy accept failed: uid=%s status=%s body=%s", user_id, r.status_code, r.text[:400])
    except requests.RequestException as e:
        log.warning("    legacy accept error: uid=%s %s", user_id, e)
    return False


def decline_join_request(group_id: int, user_id: int) -> bool:
    """Decline a pending join request (legacy endpoint — OC v2 has no decline)."""
    if DRY_RUN:
        log.info("    [DRY RUN] would decline uid=%s", user_id)
        return True
    try:
        r = INV_SESSION.delete(
            f"https://groups.roblox.com/v1/groups/{group_id}/join-requests/users/{user_id}",
            timeout=10,
        )
        if r.status_code == 403 and "Token Validation Failed" in r.text:
            log.info("    decline auth failed, refreshing CSRF token")
            refresh_csrf()
            r = INV_SESSION.delete(
                f"https://groups.roblox.com/v1/groups/{group_id}/join-requests/users/{user_id}",
                timeout=10,
            )
        if r is not None and r.status_code in (200, 204):
            log.info("    decline succeeded: uid=%s status=%s", user_id, r.status_code)
            return True
        if r is not None:
            log.warning("    decline failed: uid=%s status=%s body=%s", user_id, r.status_code, r.text[:400])
        else:
            log.warning("    decline failed: uid=%s no response", user_id)
        return False
    except requests.RequestException as e:
        log.warning("decline error: %s", e)
        return False


def get_all_members_oc(group_id: int) -> list[dict]:
    """
    Fetch all group members via Open Cloud v2 memberships endpoint.
    Returns list of {userId, username, membershipId, roleId}.
    """
    members = []
    page_token = None
    while True:
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        data = oc_get(f"{OC_BASE}/groups/{group_id}/memberships", params=params)
        if not data:
            break
        for m in data.get("groupMemberships", []):
            # OC format: "path": "groups/123/memberships/456"
            #            "user": "users/789"
            #            "role": "groups/123/roles/999"
            path   = m.get("path", "")
            user   = m.get("user", "")
            role   = m.get("role", "")
            uid    = int(user.split("/")[-1]) if user else 0
            role_id = int(role.split("/")[-1]) if role else 0
            mem_id = path.split("/")[-1] if path else ""
            if uid:
                members.append({
                    "userId":       uid,
                    "username":     f"uid:{uid}",   # OC doesn't return username here
                    "membershipId": mem_id,
                    "roleId":       role_id,
                })
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(BATCH_PAUSE)
    return members


def set_rank_oc(group_id: int, membership_id: str, role_id: int) -> bool:
    """Change a member's rank via Open Cloud v2."""
    if DRY_RUN:
        log.info("    [DRY RUN] would set membership %s → role %s", membership_id, role_id)
        return True
    r = oc_patch(
        f"{OC_BASE}/groups/{group_id}/memberships/{membership_id}",
        json_body={"role": f"groups/{group_id}/roles/{role_id}"},
    )
    if r is not None and r.status_code in (200, 204):
        return True
    if r is not None:
        log.warning("    rank change failed: %s %s", r.status_code, r.text[:200])
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  DISCORD SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def send_summary(stats: dict):
    if not DISCORD_WEBHOOK:
        return
    lines = [
        f"**Accepted (horn owners):** {stats['accepted']}",
        f"**Declined (no horns):**    {stats['declined']}",
        f"**Ranked (existing mbrs):** {stats['ranked']}",
        f"**Already correct rank:**   {stats['skipped']}",
        f"**Errors:**                 {stats['errors']}",
        f"**Dry run:**                {'yes' if DRY_RUN else 'no'}",
    ]
    embed = {
        "title": f"Horns Ranker Run — Group {GROUP_ID}",
        "description": "\n".join(lines),
        "color": 0x9B59B6,
        "footer": {"text": "Roblox Group Horns Ranker"},
    }
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=8)
    except requests.RequestException:
        pass


def send_ranked_webhook(user_id: int, username: str, role_id: int, item_name: str, owns: bool, created: str, avatar_url: str):
    if not RANKED_WEBHOOK:
        return

    profile_url = f"https://www.roblox.com/users/{user_id}/profile"
    ownership_text = "owns the tracked item" if owns else "does not own the tracked item"

    embed = {
        "title": f"User ranked: {username}",
        "url": profile_url,
        "description": (
            f"**User:** [{username}]({profile_url})\n"
            f"**User ID:** {user_id}\n"
            f"**Ranked role ID:** {role_id}\n"
            f"**Ownership:** {ownership_text} ({item_name})\n"
            f"**Account created:** {created}"
        ),
        "color": 0x00FF00,
        "thumbnail": {"url": avatar_url},
        "footer": {"text": "Roblox Group Horns Ranker"},
    }
    try:
        requests.post(RANKED_WEBHOOK, json={"embeds": [embed]}, timeout=8)
    except requests.RequestException:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def run():
    # ── Validate ──
    missing = []
    if not ROBLOX_API_KEY:  missing.append("ROBLOX_API_KEY")
    if GROUP_ID == 0:       missing.append("GROUP_ID")
    if RANK_MEMBERS and HORNS_RANK_ID == 0:
        missing.append("HORNS_RANK_ID (needed when RANK_MEMBERS=true)")
    if ACCEPT_PENDING and DECLINE_NON_OWNERS and not ROBLOSECURITY:
        log.warning("ROBLOX_COOKIE not set — decline requests will fail "
                    "(decline uses legacy endpoint that needs cookie auth). "
                    "Set DECLINE_NON_OWNERS=false to suppress this warning.")
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    if DRY_RUN:
        log.info("=== DRY RUN MODE — no changes will be made ===")

    init_sessions()
    if ROBLOSECURITY:
        refresh_csrf()

    horns_rank_id = HORNS_RANK_ID
    if HORNS_RANK_ID:
        horns_rank_id = resolve_horns_role_id(GROUP_ID, HORNS_RANK_ID)
        if horns_rank_id == 0 and RANK_MEMBERS:
            log.error(
                "HORNS_RANK_ID %s could not be resolved to a valid role; "
                "fix the env var or set RANK_MEMBERS=false",
                HORNS_RANK_ID,
            )
            sys.exit(1)

    if HORNS_RANK_ID and horns_rank_id != HORNS_RANK_ID:
        log.info("Using resolved horn rank role ID %s", horns_rank_id)

    # Load cache
    cache = Cache(CACHE_FILE)
    cache.reset_pending()  # Pending list changes frequently, always re-check

    stats = {"accepted": 0, "declined": 0, "ranked": 0, "skipped": 0, "errors": 0, "cached_skipped": 0}

    # ─────────────────────────────────────────────────────────────
    #  STEP 1: pending join requests
    # ─────────────────────────────────────────────────────────────
    if ACCEPT_PENDING:
        log.info("Fetching pending join requests for group %s...", GROUP_ID)
        pending = get_pending_members_legacy(GROUP_ID)
        log.info("Found %d pending request(s).", len(pending))

        for p in pending:
            raw_request_id = p.get("requestId")
            request_id = str(raw_request_id) if raw_request_id is not None else None
            uid  = p["userId"]
            name = p["username"]

            # Skip if we already processed this pending request
            if request_id is not None and cache.was_pending_checked(request_id):
                log.info("Skipping pending (already processed): %s (uid=%s, requestId=%s)", name, uid, request_id)
                stats["cached_skipped"] += 1
                continue

            log.info("Checking pending: %s (uid=%s)", name, uid)

            owns, item = user_owns_horns(uid)

            if owns:
                log.info("  ✅ Owns '%s' → accepting", item)
                if accept_join_request(GROUP_ID, uid):
                    stats["accepted"] += 1
                    if request_id is not None:
                        cache.mark_pending_checked(request_id)
                else:
                    stats["errors"] += 1
            else:
                log.info("  ❌ No horns detected")
                if DECLINE_NON_OWNERS:
                    log.info("     → declining")
                    if decline_join_request(GROUP_ID, uid):
                        stats["declined"] += 1
                        if request_id is not None:
                            cache.mark_pending_checked(request_id)
                    else:
                        stats["errors"] += 1

            time.sleep(CALL_DELAY)

    # ─────────────────────────────────────────────────────────────
    #  STEP 2: rank existing members
    # ─────────────────────────────────────────────────────────────
    if RANK_MEMBERS:
        log.info("Fetching existing members for group %s...", GROUP_ID)
        members = get_all_members_oc(GROUP_ID)
        log.info("Found %d member(s) total.", len(members))

        for m in members:
            uid    = m["userId"]
            cur    = m["roleId"]
            mem_id = m["membershipId"]

            if cur == horns_rank_id:
                stats["skipped"] += 1
                continue

            # Skip if cached and still at the same rank (hasn't bought horns yet)
            if cache.is_member_checked(uid, cur):
                log.debug("Skipping member uid=%s (cached, still at role %s)", uid, cur)
                stats["cached_skipped"] += 1
                continue

            log.info("Checking member uid=%s (current role=%s)", uid, cur)
            owns, item = user_owns_horns(uid)

            if owns:
                log.info("  ✅ Owns '%s' → ranking to %s", item, horns_rank_id)
                if set_rank_oc(GROUP_ID, mem_id, horns_rank_id):
                    stats["ranked"] += 1
                    cache.mark_member_checked(uid, horns_rank_id)
                    created, avatar_url = get_user_details(uid)
                    send_ranked_webhook(uid, m.get("username", f"uid:{uid}"), horns_rank_id, item, owns, created, avatar_url)
                else:
                    stats["errors"] += 1
            else:
                log.debug("  no horns, leaving rank unchanged")
                cache.mark_member_checked(uid, cur)

            time.sleep(CALL_DELAY)

    # Save cache
    cache.save()

    # ─────────────────────────────────────────────────────────────
    #  SUMMARY
    # ─────────────────────────────────────────────────────────────
    log.info(
        "Done. accepted=%d declined=%d ranked=%d skipped=%d cached_skipped=%d errors=%d",
        stats["accepted"], stats["declined"], stats["ranked"],
        stats["skipped"], stats["cached_skipped"], stats["errors"],
    )
    send_summary(stats)


if __name__ == "__main__":
    run()
