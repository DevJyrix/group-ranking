"""
Instant Horns Checker API
=========================
Provides an HTTP endpoint to instantly check a single user for horns
and auto-accept/rank them if they own horns.

Useful for:
- Manual triggers
- Webhook integrations
- Discord bots

Usage:
  POST /check-user?user_id=12345
  Returns: {"status": "accepted|ranked|declined|error", "message": "..."}
"""

import os
import sys
import time
from flask import Flask, request, jsonify

# Import from group_ranker
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
    get_all_members_oc,
    accept_join_request,
    decline_join_request,
    set_rank_oc,
    log,
)

app = Flask(__name__)

# Initialize sessions once at startup
init_sessions()
if ROBLOSECURITY:
    refresh_csrf()


@app.route("/check-user", methods=["POST"])
def check_user():
    """
    Instantly check a single user for horn ownership and rank them.

    Query params:
      - user_id (required): Roblox user ID to check
      - rank (optional): 'true' to rank existing member, 'false' to only accept pending

    Returns JSON with status and message.
    """
    user_id_str = request.args.get("user_id", "").strip()
    do_rank = request.args.get("rank", "true").lower() == "true"

    if not user_id_str or not user_id_str.isdigit():
        return jsonify({"status": "error", "message": "user_id must be a valid integer"}), 400

    user_id = int(user_id_str)

    try:
        log.info(f"[API] Instant check for user {user_id}")

        # Check ownership
        owns, item_name = user_owns_horns(user_id)

        if not owns:
            log.info(f"[API] User {user_id} does not own horns")
            return jsonify(
                {"status": "declined", "message": f"User {user_id} does not own any horns"}
            ), 200

        log.info(f"[API] User {user_id} owns {item_name}")

        # Try to accept pending request first
        if accept_join_request(GROUP_ID, user_id):
            log.info(f"[API] Successfully accepted user {user_id}")
            return jsonify(
                {
                    "status": "accepted",
                    "message": f"Accepted {user_id} (owns {item_name}). DRY_RUN={DRY_RUN}",
                }
            ), 200

        # If accept failed, try to rank if they're already a member
        if do_rank:
            members = get_all_members_oc(GROUP_ID)
            for m in members:
                if m["userId"] == user_id:
                    current_role = m["roleId"]
                    if current_role == HORNS_RANK_ID:
                        return jsonify(
                            {
                                "status": "ranked",
                                "message": f"User {user_id} already at horns rank",
                            }
                        ), 200

                    if set_rank_oc(GROUP_ID, m["membershipId"], HORNS_RANK_ID):
                        log.info(f"[API] Successfully ranked user {user_id}")
                        return jsonify(
                            {
                                "status": "ranked",
                                "message": f"Ranked {user_id} to horns role (owns {item_name}). DRY_RUN={DRY_RUN}",
                            }
                        ), 200
                    else:
                        return jsonify(
                            {
                                "status": "error",
                                "message": f"Failed to rank user {user_id}. Check API key permissions.",
                            }
                        ), 500

            # User not found in group
            return jsonify(
                {
                    "status": "error",
                    "message": f"User {user_id} is not a member of group {GROUP_ID}",
                }
            ), 400

        return jsonify(
            {
                "status": "error",
                "message": f"User {user_id} owns horns but is not pending and rank=false",
            }
        ), 400

    except Exception as e:
        log.error(f"[API] Error checking user {user_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "group_id": GROUP_ID}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
