#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safiya Speaking Partner - matching backend.
Pairs two Telegram users at the same level. No database needed - in-memory queue.
Voice is handled client-side by LiveKit; this server only does matchmaking + issues room tokens.
"""
import os, time, threading, jwt
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ---- LiveKit config (set as Railway environment variables) ----
LIVEKIT_URL    = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_KEY    = os.environ.get("LIVEKIT_KEY", "")
LIVEKIT_SECRET = os.environ.get("LIVEKIT_SECRET", "")

def make_token(identity, name, room):
    """Create a LiveKit access token for a user to join a room."""
    now = int(time.time())
    claims = {
        "iss": LIVEKIT_KEY,
        "sub": identity,
        "name": name,
        "nbf": now,
        "exp": now + 3600,
        "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True},
    }
    return jwt.encode(claims, LIVEKIT_SECRET, algorithm="HS256")

# ---- in-memory state ----
# waiting[level] = list of {uid, name, ts}
# matches[uid]   = {partner_uid, partner_name, room, ts}
waiting = {}
matches = {}
recent = {}   # uid -> set of recently-paired uids (avoid instant rematch)
lock = threading.Lock()

MATCH_TTL = 600   # forget a match after 10 min
RECENT_TTL = 8    # don't rematch same pair within 8s (just enough to avoid instant self-skip)


def _clean(uid):
    """Remove a uid from any waiting list."""
    for lv in list(waiting.keys()):
        waiting[lv] = [w for w in waiting[lv] if w["uid"] != uid]


def _remember(a, b):
    now = time.time()
    recent.setdefault(a, {})[b] = now
    recent.setdefault(b, {})[a] = now


def _recently(a, b):
    now = time.time()
    r = recent.get(a, {})
    return b in r and (now - r[b]) < RECENT_TTL


@app.route("/health")
def health():
    return jsonify(ok=True)


@app.route("/join", methods=["POST"])
def join():
    """User asks for a partner. Returns matched=true immediately if someone waits, else queued."""
    d = request.get_json(force=True)
    uid = str(d.get("uid", "")).strip()
    name = (d.get("name") or "Partner").strip()
    level = (d.get("level") or "any").strip()
    if not uid:
        return jsonify(error="no uid"), 400

    with lock:
        # already matched? return it
        if uid in matches:
            m = matches[uid]
            return jsonify(matched=True, partner_name=m["partner_name"], room=m["room"])

        _clean(uid)
        pool = waiting.setdefault(level, [])
        # find someone not recently paired
        partner = None
        for w in pool:
            if w["uid"] != uid and not _recently(uid, w["uid"]):
                partner = w
                break
        if partner:
            pool.remove(partner)
            room = f"sp_{uid}_{partner['uid']}_{int(time.time())}"
            matches[uid] = {"partner_uid": partner["uid"], "partner_name": partner["name"], "room": room, "ts": time.time()}
            matches[partner["uid"]] = {"partner_uid": uid, "partner_name": name, "room": room, "ts": time.time()}
            _remember(uid, partner["uid"])
            return jsonify(matched=True, partner_name=partner["name"], room=room)
        else:
            pool.append({"uid": uid, "name": name, "ts": time.time()})
            return jsonify(matched=False)


@app.route("/poll", methods=["POST"])
def poll():
    """Waiting user checks if they've been matched."""
    d = request.get_json(force=True)
    uid = str(d.get("uid", "")).strip()
    with lock:
        if uid in matches:
            m = matches[uid]
            return jsonify(matched=True, partner_name=m["partner_name"], room=m["room"])
        return jsonify(matched=False)


@app.route("/token", methods=["POST"])
def token():
    """Issue a LiveKit join token for a user's current room."""
    d = request.get_json(force=True)
    uid = str(d.get("uid", "")).strip()
    name = (d.get("name") or "Student").strip()
    room = (d.get("room") or "").strip()
    if not uid or not room:
        return jsonify(error="missing uid or room"), 400
    if not LIVEKIT_KEY or not LIVEKIT_SECRET:
        return jsonify(error="livekit not configured"), 500
    tk = make_token(uid, name, room)
    return jsonify(token=tk, url=LIVEKIT_URL)


@app.route("/status", methods=["POST"])
def status():
    """User in a call checks whether they still have a match."""
    d = request.get_json(force=True)
    uid = str(d.get("uid", "")).strip()
    with lock:
        if uid in matches:
            return jsonify(active=True)
        return jsonify(active=False)


@app.route("/skip", methods=["POST"])
def skip():
    """Leave current partner, optionally rejoin the queue."""
    d = request.get_json(force=True)
    uid = str(d.get("uid", "")).strip()
    with lock:
        m = matches.pop(uid, None)
        if m:
            p = m["partner_uid"]
            # tell partner they were left: drop their match too
            matches.pop(p, None)
            _remember(uid, p)
    return jsonify(ok=True)


@app.route("/leave", methods=["POST"])
def leave():
    """Fully exit - remove from queue and any match."""
    d = request.get_json(force=True)
    uid = str(d.get("uid", "")).strip()
    with lock:
        _clean(uid)
        m = matches.pop(uid, None)
        if m:
            matches.pop(m["partner_uid"], None)
    return jsonify(ok=True)


def _reaper():
    """Background cleanup of stale matches and queue entries."""
    while True:
        time.sleep(30)
        now = time.time()
        with lock:
            for uid in list(matches.keys()):
                if now - matches[uid]["ts"] > MATCH_TTL:
                    matches.pop(uid, None)
            for lv in list(waiting.keys()):
                waiting[lv] = [w for w in waiting[lv] if now - w["ts"] < 300]


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    threading.Thread(target=_reaper, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
