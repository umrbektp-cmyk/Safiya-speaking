#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safiya Speaking Partner - matching backend with database."""
import os, time, threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

LIVEKIT_URL    = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_KEY    = os.environ.get("LIVEKIT_KEY", "")
LIVEKIT_SECRET = os.environ.get("LIVEKIT_SECRET", "")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")


def make_token(identity, name, room):
    from livekit import api
    token = (api.AccessToken(LIVEKIT_KEY, LIVEKIT_SECRET)
             .with_identity(identity).with_name(name)
             .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)))
    return token.to_jwt()


import psycopg2
from psycopg2.extras import RealDictCursor

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        print("WARNING: no DATABASE_URL - running without persistence"); return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS sp_users (
                uid TEXT PRIMARY KEY, name TEXT, photo_url TEXT, level TEXT DEFAULT 'beginner',
                total_seconds INTEGER DEFAULT 0, total_calls INTEGER DEFAULT 0,
                likes_received INTEGER DEFAULT 0, rating_sum INTEGER DEFAULT 0,
                rating_count INTEGER DEFAULT 0, joined TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sp_calls (
                id SERIAL PRIMARY KEY, uid TEXT, partner_uid TEXT, partner_name TEXT,
                seconds INTEGER DEFAULT 0, level TEXT, ended_at TEXT)""")
        conn.commit()

def upsert_user(uid, name, photo_url, level):
    if not DATABASE_URL: return
    k = str(uid); today = time.strftime("%Y-%m-%d")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT uid FROM sp_users WHERE uid=%s", (k,))
            if cur.fetchone():
                cur.execute("UPDATE sp_users SET name=%s, photo_url=%s WHERE uid=%s", (name, photo_url, k))
            else:
                cur.execute("INSERT INTO sp_users (uid,name,photo_url,level,joined) VALUES (%s,%s,%s,%s,%s)",
                            (k, name, photo_url, level or 'beginner', today))
        conn.commit()

def get_profile(uid):
    if not DATABASE_URL: return None
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sp_users WHERE uid=%s", (str(uid),))
            row = cur.fetchone()
            if not row: return None
            row = dict(row); rc = row.get("rating_count") or 0
            row["avg_rating"] = round(row["rating_sum"]/rc, 1) if rc else None
            return row

def record_call(uid, partner_uid, partner_name, seconds, level):
    if not DATABASE_URL or seconds < 5: return
    k = str(uid)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sp_calls (uid,partner_uid,partner_name,seconds,level,ended_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (k, str(partner_uid), partner_name, seconds, level, time.strftime("%Y-%m-%d %H:%M")))
            cur.execute("UPDATE sp_users SET total_seconds=total_seconds+%s, total_calls=total_calls+1 WHERE uid=%s", (seconds, k))
        conn.commit()

def add_like(target_uid):
    if not DATABASE_URL: return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sp_users SET likes_received=likes_received+1 WHERE uid=%s", (str(target_uid),))
        conn.commit()

def add_rating(target_uid, stars):
    if not DATABASE_URL or not (1 <= stars <= 5): return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sp_users SET rating_sum=rating_sum+%s, rating_count=rating_count+1 WHERE uid=%s", (stars, str(target_uid)))
        conn.commit()

def get_call_history(uid, limit=20):
    if not DATABASE_URL: return []
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT partner_name, seconds, level, ended_at FROM sp_calls WHERE uid=%s ORDER BY id DESC LIMIT %s", (str(uid), limit))
            return [dict(r) for r in cur.fetchall()]

def get_leaderboard(kind="minutes", limit=50):
    if not DATABASE_URL: return []
    order = {"minutes":"total_seconds DESC","likes":"likes_received DESC",
             "rating":"(CASE WHEN rating_count>0 THEN rating_sum::float/rating_count ELSE 0 END) DESC"}.get(kind,"total_seconds DESC")
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT uid,name,photo_url,total_seconds,likes_received,rating_sum,rating_count FROM sp_users ORDER BY {order} LIMIT %s", (limit,))
            rows=[]
            for r in cur.fetchall():
                r=dict(r); rc=r.get("rating_count") or 0
                r["avg_rating"]=round(r["rating_sum"]/rc,1) if rc else None
                r["minutes"]=round((r.get("total_seconds") or 0)/60)
                rows.append(r)
            return rows


waiting = {}; matches = {}; recent = {}; lock = threading.Lock()
MATCH_TTL = 600; RECENT_TTL = 8

def _clean(uid):
    for lv in list(waiting.keys()):
        waiting[lv] = [w for w in waiting[lv] if w["uid"] != uid]

def _remember(a, b):
    now = time.time(); recent.setdefault(a, {})[b] = now; recent.setdefault(b, {})[a] = now

def _recently(a, b):
    return b in recent.get(a, {}) and (time.time() - recent[a][b]) < RECENT_TTL


@app.route("/health")
def health():
    return jsonify(ok=True, db=bool(DATABASE_URL))


@app.route("/join", methods=["POST"])
def join():
    d = request.get_json(force=True)
    uid = str(d.get("uid","")).strip(); name = (d.get("name") or "Partner").strip()
    photo = d.get("photo") or ""; level = (d.get("level") or "any").strip()
    if not uid: return jsonify(error="no uid"), 400
    upsert_user(uid, name, photo, level)
    with lock:
        if uid in matches:
            m = matches[uid]
            return jsonify(matched=True, partner_name=m["partner_name"], partner_uid=m["partner_uid"],
                           partner_photo=m.get("partner_photo",""), partner_level=m.get("partner_level",""), room=m["room"])
        _clean(uid)
        pool = waiting.setdefault(level, [])
        partner = None
        for w in pool:
            if w["uid"] != uid and not _recently(uid, w["uid"]):
                partner = w; break
        if partner:
            pool.remove(partner)
            room = f"sp_{uid}_{partner['uid']}_{int(time.time())}"
            matches[uid] = {"partner_uid":partner["uid"],"partner_name":partner["name"],"partner_photo":partner.get("photo",""),"partner_level":level,"room":room,"ts":time.time()}
            matches[partner["uid"]] = {"partner_uid":uid,"partner_name":name,"partner_photo":photo,"partner_level":level,"room":room,"ts":time.time()}
            _remember(uid, partner["uid"])
            return jsonify(matched=True, partner_name=partner["name"], partner_uid=partner["uid"],
                           partner_photo=partner.get("photo",""), partner_level=level, room=room)
        else:
            pool.append({"uid":uid,"name":name,"photo":photo,"ts":time.time()})
            return jsonify(matched=False, queue_pos=len(pool))


@app.route("/poll", methods=["POST"])
def poll():
    d = request.get_json(force=True); uid = str(d.get("uid","")).strip()
    with lock:
        if uid in matches:
            m = matches[uid]
            return jsonify(matched=True, partner_name=m["partner_name"], partner_uid=m["partner_uid"],
                           partner_photo=m.get("partner_photo",""), partner_level=m.get("partner_level",""), room=m["room"])
        return jsonify(matched=False)


@app.route("/token", methods=["POST"])
def token():
    d = request.get_json(force=True)
    uid = str(d.get("uid","")).strip(); name = (d.get("name") or "Student").strip(); room = (d.get("room") or "").strip()
    if not uid or not room: return jsonify(error="missing uid or room"), 400
    if not LIVEKIT_KEY or not LIVEKIT_SECRET: return jsonify(error="livekit not configured"), 500
    return jsonify(token=make_token(uid, name, room), url=LIVEKIT_URL)


@app.route("/status", methods=["POST"])
def status():
    d = request.get_json(force=True); uid = str(d.get("uid","")).strip()
    with lock:
        return jsonify(active=(uid in matches))


@app.route("/end", methods=["POST"])
def end():
    d = request.get_json(force=True)
    uid = str(d.get("uid","")).strip(); seconds = int(d.get("seconds") or 0)
    like = bool(d.get("like")); stars = int(d.get("stars") or 0)
    with lock:
        m = matches.pop(uid, None)
        if m:
            partner = m["partner_uid"]; matches.pop(partner, None); _remember(uid, partner)
            record_call(uid, partner, m["partner_name"], seconds, m.get("partner_level",""))
            if like: add_like(partner)
            if stars: add_rating(partner, stars)
    return jsonify(ok=True)


@app.route("/skip", methods=["POST"])
def skip():
    d = request.get_json(force=True)
    uid = str(d.get("uid","")).strip(); seconds = int(d.get("seconds") or 0); like = bool(d.get("like"))
    with lock:
        m = matches.pop(uid, None)
        if m:
            partner = m["partner_uid"]; matches.pop(partner, None); _remember(uid, partner)
            record_call(uid, partner, m["partner_name"], seconds, m.get("partner_level",""))
            if like: add_like(partner)
    return jsonify(ok=True)


@app.route("/leave", methods=["POST"])
def leave():
    d = request.get_json(force=True)
    uid = str(d.get("uid","")).strip(); seconds = int(d.get("seconds") or 0)
    with lock:
        _clean(uid)
        m = matches.pop(uid, None)
        if m:
            matches.pop(m["partner_uid"], None)
            record_call(uid, m["partner_uid"], m["partner_name"], seconds, m.get("partner_level",""))
    return jsonify(ok=True)


@app.route("/like", methods=["POST"])
def like():
    d = request.get_json(force=True); uid = str(d.get("uid","")).strip()
    with lock:
        m = matches.get(uid)
    if m:
        add_like(m["partner_uid"]); return jsonify(ok=True)
    return jsonify(ok=False)


@app.route("/profile", methods=["POST"])
def profile():
    d = request.get_json(force=True); uid = str(d.get("uid","")).strip()
    p = get_profile(uid)
    if not p: return jsonify(profile=None)
    p["minutes"] = round((p.get("total_seconds") or 0)/60)
    p["history"] = get_call_history(uid)
    return jsonify(profile=p)


@app.route("/leaderboard", methods=["POST"])
def leaderboard():
    d = request.get_json(force=True); kind = (d.get("kind") or "minutes").strip()
    return jsonify(board=get_leaderboard(kind))


def _reaper():
    while True:
        time.sleep(30); now = time.time()
        with lock:
            for uid in list(matches.keys()):
                if now - matches[uid]["ts"] > MATCH_TTL: matches.pop(uid, None)
            for lv in list(waiting.keys()):
                waiting[lv] = [w for w in waiting[lv] if now - w["ts"] < 300]


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


init_db()
threading.Thread(target=_reaper, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
