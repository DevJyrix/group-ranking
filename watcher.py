"""
Instant Horns Watcher
=====================
Continuously monitors pending join requests and instantly accepts/ranks
horn owners as soon as they join (within ~10-30 seconds).

Runs as a separate service from the batch cron job.
Lightweight: only checks pending requests, no existing member scanning.
"""

import os
import sys
import time
import json
import logging

from group_ranker import (
    ROBLOX_API_KEY,
    ROBLOSECURITY,
    GROUP_ID,
    HORNS_RANK_ID,
    DRY_RUN,
    CALL_DELAY,
    init_sessions,
    refresh_csrf,
    user_owns_horns,
    accept_join_request,
    decline_join_request,
    get_pending_members_legacy,
    get_all_members_oc,
    set_rank_oc,
    get_user_details,
    send_ranked_webhook,
    resolve_horns_role_id,
    log,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

# Track join request IDs we've already processed
PROCESSED_PENDING = set()
CACHE_FILE = ".horns_watcher_cache.json"


def load_cache():
    """Load previously seen pending request IDs from cache."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                return set(str(x) for x in data.get("processed", []))
        except (json.JSONDecodeError, IOError):
            pass
    return set()


def save_cache():
    """Save processed request IDs to cache."""
    with open(CACHE_FILE, "w") as f:
        json.dump({"processed": list(PROCESSED_PENDING)}, f, indent=2)


def find_membership_by_user(user_id: int, retries: int = 4, delay: float = 2.0):
    """Return membership info for a user if they are now in the group."""
    for attempt in range(retries):
        members = get_all_members_oc(GROUP_ID)
        for m in members:
            if m["userId"] == user_id:
                return m
        if attempt < retries - 1:
            log.info("    membership not found yet for uid=%s, retrying in %s seconds", user_id, delay)
            time.sleep(delay)
    return None


def watch():
    """
    Continuously monitor pending requests.
    Check new users instantly and accept/rank horn owners.
    """
    global PROCESSED_PENDING

    log.info("=== Horns Watcher Started ===")
    log.info(f"GROUP_ID={GROUP_ID}, HORNS_RANK_ID={HORNS_RANK_ID}")
    log.info(f"DRY_RUN={DRY_RUN}")
    log.info("Polling pending requests every 10 seconds...")

    if DRY_RUN:
        log.info("⚠️  DRY_RUN MODE — no changes will be made")

    init_sessions()
    if ROBLOSECURITY:
        refresh_csrf()

    log.info(f"ROBLOX_API_KEY set: {'yes' if ROBLOX_API_KEY else 'no'}")
    log.info(f"ROBLOX_COOKIE set: {'yes' if ROBLOSECURITY else 'no'}")
    log.info(f"ACCEPT_PENDING={os.environ.get('ACCEPT_PENDING', 'true')}")

    horns_rank_id = HORNS_RANK_ID
    if HORNS_RANK_ID:
        horns_rank_id = resolve_horns_role_id(GROUP_ID, HORNS_RANK_ID)
        if horns_rank_id == 0:
            log.error("HORNS_RANK_ID %s could not be resolved; ranking will be disabled", HORNS_RANK_ID)
        elif horns_rank_id != HORNS_RANK_ID:
            log.info("Resolved HORNS_RANK_ID %s to actual role ID %s", HORNS_RANK_ID, horns_rank_id)

    PROCESSED_PENDING = load_cache()
    log.info(f"Loaded watcher cache: {len(PROCESSED_PENDING)} processed pending uids")

    stats = {"accepted": 0, "declined": 0, "errors": 0, "runs": 0}

    try:
        while True:
            stats["runs"] += 1
            log.info(f"--- Poll #{stats['runs']} ---")

            try:
                pending = get_pending_members_legacy(GROUP_ID)
                log.info(f"Found {len(pending)} pending request(s)")

                for p in pending:
                    request_id = str(p.get("requestId") or p.get("userId"))
                    uid = p["userId"]
                    name = p["username"]

                    if request_id in PROCESSED_PENDING:
                        log.info(f"Skipping {name} (uid={uid}, requestId={request_id}) — already processed")
                        continue

                    log.info(f"🆕 NEW PENDING: {name} (uid={uid}, requestId={request_id})")
                    log.info(f"    pending object: {p}")

                    # Check ownership
                    owns, item = user_owns_horns(uid)
                    log.info(f"    ownership result for {uid}: owns={owns}, item={item}")

                    if owns:
                        log.info(f"  ✅ Owns '{item}' → accepting NOW")
                        if accept_join_request(GROUP_ID, uid):
                            stats["accepted"] += 1
                            if request_id is not None:
                                PROCESSED_PENDING.add(request_id)
                                save_cache()
                        else:
                            stats["errors"] += 1
                    else:
                        log.info(f"  ❌ No horns → declining")
                        if decline_join_request(GROUP_ID, uid):
                            stats["declined"] += 1
                            if request_id is not None:
                                PROCESSED_PENDING.add(request_id)
                                save_cache()
                        else:
                            stats["errors"] += 1

                    # If user was accepted and now has horns, try to rank them immediately.
                    if owns and horns_rank_id and not DRY_RUN:
                        membership = find_membership_by_user(uid)
                        if membership:
                            log.info(f"  🔃 Found accepted user in group, ranking to {HORNS_RANK_ID}")
                            if set_rank_oc(GROUP_ID, membership["membershipId"], HORNS_RANK_ID):
                                log.info(f"  ✅ Ranked user {uid} to {HORNS_RANK_ID}")
                                created, avatar_url = get_user_details(uid)
                                send_ranked_webhook(uid, name, HORNS_RANK_ID, item, owns, created, avatar_url)
                            else:
                                log.warning(f"  ❌ Failed to rank accepted user {uid}")
                        else:
                            log.info(f"  ℹ️ User {uid} not yet in group membership list after acceptance")

                    time.sleep(CALL_DELAY)

                log.info(
                    f"Stats: accepted={stats['accepted']} declined={stats['declined']} errors={stats['errors']}"
                )

            except Exception as e:
                log.error(f"Error during poll: {e}", exc_info=True)

            # Wait before next poll
            time.sleep(10)

    except KeyboardInterrupt:
        log.info("Watcher stopped")
        sys.exit(0)


if __name__ == "__main__":
    watch()
