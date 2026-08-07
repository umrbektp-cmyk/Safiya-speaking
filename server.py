#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safiya Speaking Partner - full backend (v10): matching, profiles, calls, ratings, likes, bios, call-requests."""
import os, time, threading, random
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

LIVEKIT_URL    = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_KEY    = os.environ.get("LIVEKIT_KEY", "")
LIVEKIT_SECRET = os.environ.get("LIVEKIT_SECRET", "")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
APP_URL        = os.environ.get("APP_URL", "")  # public URL of this app
MINIAPP_URL    = os.environ.get("MINIAPP_URL", "")  # t.me/bot/app deep link (opens inside Telegram)
BTN_URL        = MINIAPP_URL or APP_URL  # prefer mini app link for buttons
APP_VERSION    = "16"  # bump on each deploy so clients auto-update

import urllib.request, urllib.parse, json as _json

def tg_send(chat_id, text, button_text=None, button_url=None):
    """Send a Telegram message via the bot. Silent no-op if token missing."""
    if not TELEGRAM_TOKEN or not chat_id or str(chat_id).startswith("guest"):
        return
    try:
        payload = {"chat_id": str(chat_id), "text": text}
        if button_text and button_url:
            payload["reply_markup"] = _json.dumps({"inline_keyboard": [[{"text": button_text, "url": button_url}]]})
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("tg_send error:", str(e)[:150])

def tg_send_buttons(chat_id, text, inline_keyboard):
    """Send a message with callback buttons (for admin approve/reject)."""
    if not TELEGRAM_TOKEN or not chat_id: return
    try:
        payload={"chat_id":str(chat_id),"text":text,"reply_markup":_json.dumps({"inline_keyboard":inline_keyboard})}
        data=urllib.parse.urlencode(payload).encode()
        req=urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("tg_send_buttons error:", str(e)[:150])

def tg_answer_callback(callback_id, text=""):
    if not TELEGRAM_TOKEN: return
    try:
        payload={"callback_query_id":callback_id,"text":text}
        data=urllib.parse.urlencode(payload).encode()
        req=urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", data=data)
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("answer_callback error:", str(e)[:120])

def tg_edit_text(chat_id, message_id, text):
    if not TELEGRAM_TOKEN: return
    try:
        payload={"chat_id":str(chat_id),"message_id":message_id,"text":text}
        data=urllib.parse.urlencode(payload).encode()
        req=urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", data=data)
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("edit_text error:", str(e)[:120])


def make_token(identity, name, room):
    from livekit import api
    t = (api.AccessToken(LIVEKIT_KEY, LIVEKIT_SECRET).with_identity(identity).with_name(name)
         .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)))
    return t.to_jwt()


import psycopg2
from psycopg2.extras import RealDictCursor
from scenarios import SCENARIOS
import random

def get_db():
    return psycopg2.connect(DATABASE_URL)

def _run(sql, params=None):
    """Run one statement in its own connection so a failure can't poison others."""
    if not DATABASE_URL: return
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
    except Exception as e:
        conn.rollback(); print("DB ERROR:", str(e)[:200], "| SQL:", sql[:80])
    finally:
        conn.close()



INVITE_LINES = [
    "{name} is online and looking for a speaking partner 👀",
    "Practice makes perfect — someone's ready to chat right now 🎤",
    "Got 5 minutes? A partner is waiting in the lobby 🔥",
    "Your English won't practice itself 😄 Jump into a quick call!",
    "{count} learners are online now — come say hello 👋",
    "Quick speaking session? Someone's online and ready 💬",
    "{name} wants to practice with you — up for a chat? 😊",
    "{name} is waiting in the lobby. Join her? 🎙️",
]

# Friendly girls' names used in invite messages (engages better)
INVITE_NAMES = ["Malika","Sevara","Nilufar","Dilnoza","Gulnora","Madina","Shahzoda","Zilola",
                "Feruza","Nozima","Kamila","Sabina","Munisa","Dildora","Charos","Rayhona",
                "Oisha","Zarina","Laylo","Nafisa"]

def _reminder_loop():
    import random as _r
    while True:
        time.sleep(7200)  # 2 hours
        if not (TELEGRAM_TOKEN and BTN_URL and DATABASE_URL):
            continue
        try:
            online = count_online(within=3600)  # active in last hour
            # pull a sample of users active in the last 3 days to nudge
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute("SELECT uid,name FROM sp_users WHERE last_seen > %s AND uid NOT LIKE 'guest%%' ORDER BY random() LIMIT 30",
                            (time.time()-3*86400,))
                targets = cur.fetchall()
            conn.close()
            for uid, name in targets:
                line = _r.choice(INVITE_LINES)
                other = _r.choice(INVITE_NAMES)
                msg = line.format(name=other, count=max(online,2))
                tg_send(uid, msg, "🎤 Start practicing", BTN_URL)
                time.sleep(0.05)
            print(f"reminder_loop: nudged {len(targets)} users")
        except Exception as e:
            print("reminder_loop error:", str(e)[:150])

def init_db():
    if not DATABASE_URL:
        print("WARNING: no DATABASE_URL"); return
    _run("""CREATE TABLE IF NOT EXISTS sp_users (
        uid TEXT PRIMARY KEY, name TEXT, photo_url TEXT, level TEXT DEFAULT 'beginner', bio TEXT DEFAULT '',
        total_seconds INTEGER DEFAULT 0, total_calls INTEGER DEFAULT 0, likes_received INTEGER DEFAULT 0,
        rating_sum INTEGER DEFAULT 0, rating_count INTEGER DEFAULT 0, last_seen DOUBLE PRECISION DEFAULT 0, joined TEXT)""")
    _run("""CREATE TABLE IF NOT EXISTS sp_calls (
        id SERIAL PRIMARY KEY, uid TEXT, partner_uid TEXT, partner_name TEXT,
        seconds INTEGER DEFAULT 0, level TEXT, ended_at TEXT)""")
    _run("ALTER TABLE sp_users ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT ''")
    _run("ALTER TABLE sp_users ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION DEFAULT 0")
    _run("""CREATE TABLE IF NOT EXISTS sp_follows (
        follower TEXT, following TEXT, created TEXT, PRIMARY KEY (follower, following))""")
    _run("""CREATE TABLE IF NOT EXISTS sp_messages (
        id SERIAL PRIMARY KEY, uid TEXT, name TEXT, photo_url TEXT, text TEXT, created TEXT, ts DOUBLE PRECISION)""")
    _run("""CREATE TABLE IF NOT EXISTS sp_follow_reqs (
        follower TEXT, following TEXT, created TEXT, PRIMARY KEY (follower, following))""")
    _run("""CREATE TABLE IF NOT EXISTS sp_notifs (
        id SERIAL PRIMARY KEY, uid TEXT, kind TEXT, from_uid TEXT, from_name TEXT, from_photo TEXT,
        extra TEXT, seen INTEGER DEFAULT 0, ts DOUBLE PRECISION, created TEXT)""")
    _run("""CREATE TABLE IF NOT EXISTS sp_reviews (
        id SERIAL PRIMARY KEY, uid TEXT, name TEXT, photo_url TEXT, stars INTEGER, text TEXT,
        status TEXT DEFAULT 'pending', ts DOUBLE PRECISION, created TEXT)""")
    print("init_db complete")

def upsert_user(uid, name, photo_url, level=None):
    if not DATABASE_URL: return
    k = str(uid); now = time.time(); today = time.strftime("%Y-%m-%d")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT uid FROM sp_users WHERE uid=%s", (k,))
            if cur.fetchone():
                if level:
                    cur.execute("UPDATE sp_users SET name=%s,photo_url=%s,level=%s,last_seen=%s WHERE uid=%s",(name,photo_url,level,now,k))
                else:
                    cur.execute("UPDATE sp_users SET name=%s,photo_url=%s,last_seen=%s WHERE uid=%s",(name,photo_url,now,k))
            else:
                cur.execute("INSERT INTO sp_users (uid,name,photo_url,level,last_seen,joined) VALUES (%s,%s,%s,%s,%s,%s)",
                            (k,name,photo_url,level or 'beginner',now,today))
        conn.commit()
    except Exception as e:
        conn.rollback(); print("upsert_user ERROR:", str(e)[:200])
    finally:
        conn.close()

def set_level(uid, level):
    if not DATABASE_URL: return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sp_users SET level=%s WHERE uid=%s",(level,str(uid)))
        conn.commit()

def set_bio(uid, bio):
    if not DATABASE_URL: return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sp_users SET bio=%s WHERE uid=%s",(bio[:200],str(uid)))
        conn.commit()

def _profile_row(row):
    row = dict(row); rc = row.get("rating_count") or 0
    row["avg_rating"] = round(row["rating_sum"]/rc,1) if rc else None
    row["minutes"] = round((row.get("total_seconds") or 0)/60)
    return row

def get_profile(uid):
    if not DATABASE_URL: return None
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sp_users WHERE uid=%s",(str(uid),))
            row = cur.fetchone()
            return _profile_row(row) if row else None

def record_call(uid, partner_uid, partner_name, seconds, level):
    if not DATABASE_URL or seconds < 3:
        print(f"record_call skipped: seconds={seconds}"); return
    k = str(uid)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sp_calls (uid,partner_uid,partner_name,seconds,level,ended_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (k,str(partner_uid),partner_name,seconds,level,time.strftime("%Y-%m-%d %H:%M")))
            cur.execute("UPDATE sp_users SET total_seconds=total_seconds+%s,total_calls=total_calls+1 WHERE uid=%s",(seconds,k))
        conn.commit()
        print(f"record_call OK: uid={k} seconds={seconds}")
    except Exception as e:
        conn.rollback(); print("record_call ERROR:", str(e)[:200])
    finally:
        conn.close()

def add_like(target_uid):
    if not DATABASE_URL: return
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sp_users SET likes_received=likes_received+1 WHERE uid=%s",(str(target_uid),))
        conn.commit(); print(f"add_like OK: {target_uid}")
    except Exception as e:
        conn.rollback(); print("add_like ERROR:", str(e)[:200])
    finally:
        conn.close()

def add_rating(target_uid, stars):
    if not DATABASE_URL or not (1<=stars<=5): return
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sp_users SET rating_sum=rating_sum+%s,rating_count=rating_count+1 WHERE uid=%s",(stars,str(target_uid)))
        conn.commit(); print(f"add_rating OK: {target_uid} stars={stars}")
    except Exception as e:
        conn.rollback(); print("add_rating ERROR:", str(e)[:200])
    finally:
        conn.close()

def get_call_history(uid, limit=20):
    if not DATABASE_URL: return []
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT partner_name,seconds,level,ended_at FROM sp_calls WHERE uid=%s ORDER BY id DESC LIMIT %s",(str(uid),limit))
            return [dict(r) for r in cur.fetchall()]

def get_leaderboard(kind="minutes", limit=50):
    if not DATABASE_URL: return []
    order = {"minutes":"total_seconds DESC","likes":"likes_received DESC",
             "rating":"(CASE WHEN rating_count>0 THEN rating_sum::float/rating_count ELSE 0 END) DESC"}.get(kind,"total_seconds DESC")
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT uid,name,photo_url,level,total_seconds,likes_received,rating_sum,rating_count FROM sp_users ORDER BY {order} LIMIT %s",(limit,))
            return [_profile_row(r) for r in cur.fetchall()]

def count_online(within=70):
    if not DATABASE_URL: return 0
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sp_users WHERE last_seen > %s",(time.time()-within,))
            return cur.fetchone()[0]


# ─── Notifications ───────────────────────────────────────────────────────────
def add_notif(uid, kind, from_uid="", from_name="", from_photo="", extra=""):
    if not DATABASE_URL or str(uid)==str(from_uid): return
    _run("""INSERT INTO sp_notifs (uid,kind,from_uid,from_name,from_photo,extra,seen,ts,created)
            VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s)""",
         (str(uid),kind,str(from_uid),from_name,from_photo,extra,time.time(),time.strftime("%m/%d %H:%M")))

def get_notifs(uid, limit=40):
    if not DATABASE_URL: return []
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sp_notifs WHERE uid=%s ORDER BY id DESC LIMIT %s",(str(uid),limit))
            return [dict(r) for r in cur.fetchall()]
    except: return []
    finally: conn.close()

def unseen_notif_count(uid):
    if not DATABASE_URL: return 0
    conn=get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sp_notifs WHERE uid=%s AND seen=0",(str(uid),))
            return cur.fetchone()[0]
    except: return 0
    finally: conn.close()

def mark_notifs_seen(uid):
    _run("UPDATE sp_notifs SET seen=1 WHERE uid=%s",(str(uid),))

# ─── Follow requests (Instagram-private style) ───────────────────────────────
def add_follow_request(follower, following):
    if not DATABASE_URL or str(follower)==str(following): return
    _run("INSERT INTO sp_follow_reqs (follower,following,created) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
         (str(follower),str(following),time.strftime("%Y-%m-%d")))

def follow_request_pending(follower, following):
    if not DATABASE_URL: return False
    conn=get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sp_follow_reqs WHERE follower=%s AND following=%s",(str(follower),str(following)))
            return cur.fetchone() is not None
    except: return False
    finally: conn.close()

def accept_follow_request(follower, following):
    """following accepts follower -> becomes a real follow."""
    _run("DELETE FROM sp_follow_reqs WHERE follower=%s AND following=%s",(str(follower),str(following)))
    _run("INSERT INTO sp_follows (follower,following,created) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
         (str(follower),str(following),time.strftime("%Y-%m-%d")))

def reject_follow_request(follower, following):
    _run("DELETE FROM sp_follow_reqs WHERE follower=%s AND following=%s",(str(follower),str(following)))

def following_count(uid):
    if not DATABASE_URL: return 0
    conn=get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sp_follows WHERE follower=%s",(str(uid),))
            return cur.fetchone()[0]
    except: return 0
    finally: conn.close()

def get_followers(uid):
    if not DATABASE_URL: return []
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT u.uid,u.name,u.photo_url,u.level FROM sp_follows f
                JOIN sp_users u ON u.uid=f.follower WHERE f.following=%s ORDER BY u.name""",(str(uid),))
            return [dict(r) for r in cur.fetchall()]
    except: return []
    finally: conn.close()

# ─── Reviews / testimonials ──────────────────────────────────────────────────
def add_review(uid, name, photo, stars, text):
    if not DATABASE_URL or not text.strip(): return None
    conn=get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO sp_reviews (uid,name,photo_url,stars,text,status,ts,created)
                VALUES (%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id""",
                (str(uid),name,photo,int(stars or 5),text[:400],time.time(),time.strftime("%Y-%m-%d")))
            rid=cur.fetchone()[0]
        conn.commit(); return rid
    except Exception as e:
        conn.rollback(); print("add_review error:",str(e)[:120]); return None
    finally: conn.close()

def set_review_status(rid, status):
    _run("UPDATE sp_reviews SET status=%s WHERE id=%s",(status,int(rid)))

def get_review(rid):
    if not DATABASE_URL: return None
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sp_reviews WHERE id=%s",(int(rid),))
            r=cur.fetchone(); return dict(r) if r else None
    except: return None
    finally: conn.close()

def get_approved_reviews(limit=30):
    if not DATABASE_URL: return []
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name,photo_url,stars,text,created FROM sp_reviews WHERE status='approved' ORDER BY random() LIMIT %s",(limit,))
            return [dict(r) for r in cur.fetchall()]
    except: return []
    finally: conn.close()

# ─── Last seen formatting (Telegram-style) ───────────────────────────────────
def last_seen_text(ts):
    if not ts: return "recently"
    diff=time.time()-ts
    if diff<70: return "online now"
    if diff<3600: return f"{int(diff//60)} min ago"
    if diff<86400: return f"{int(diff//3600)} hours ago"
    if diff<172800: return "yesterday"
    return time.strftime("%b %d", time.localtime(ts))


# ─── Follows + Community messages ────────────────────────────────────────────
def follow_user(follower, following):
    if not DATABASE_URL or follower==following: return
    _run("INSERT INTO sp_follows (follower,following,created) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
         (str(follower),str(following),time.strftime("%Y-%m-%d")))

def unfollow_user(follower, following):
    _run("DELETE FROM sp_follows WHERE follower=%s AND following=%s",(str(follower),str(following)))

def is_following(follower, following):
    if not DATABASE_URL: return False
    conn=get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sp_follows WHERE follower=%s AND following=%s",(str(follower),str(following)))
            return cur.fetchone() is not None
    except: return False
    finally: conn.close()

def follower_count(uid):
    if not DATABASE_URL: return 0
    conn=get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sp_follows WHERE following=%s",(str(uid),))
            return cur.fetchone()[0]
    except: return 0
    finally: conn.close()

def get_following(uid):
    """People I follow, with their online status + profile."""
    if not DATABASE_URL: return []
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT u.uid,u.name,u.photo_url,u.level,u.last_seen
                FROM sp_follows f JOIN sp_users u ON u.uid=f.following
                WHERE f.follower=%s ORDER BY u.last_seen DESC""",(str(uid),))
            rows=[]
            now=time.time()
            with lock:
                busy_set=set(matches.keys())
            for r in cur.fetchall():
                r=dict(r); r["online"]=(r.get("last_seen",0) or 0)>now-70; r["busy"]=r["uid"] in busy_set
                r.pop("last_seen",None); rows.append(r)
            return rows
    except Exception as e:
        print("get_following error:",str(e)[:120]); return []
    finally: conn.close()

def post_message(uid,name,photo,text):
    if not DATABASE_URL or not text.strip(): return
    _run("INSERT INTO sp_messages (uid,name,photo_url,text,created,ts) VALUES (%s,%s,%s,%s,%s,%s)",
         (str(uid),name,photo,text[:300],time.strftime("%m/%d %H:%M"),time.time()))

def get_messages(limit=50):
    if not DATABASE_URL: return []
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,uid,name,photo_url,text,created FROM sp_messages ORDER BY id DESC LIMIT %s",(limit,))
            return [dict(r) for r in reversed(cur.fetchall())]
    except: return []
    finally: conn.close()

def delete_message(mid):
    _run("DELETE FROM sp_messages WHERE id=%s",(int(mid),))


# ─── In-memory matchmaking + call requests ──────────────────────────────────
waiting = []          # list of {uid,name,photo,level,only_level,ts}
matches = {}          # uid -> {partner_uid,partner_name,partner_photo,partner_level,room,ts}
recent  = {}
requests_in = {}      # target_uid -> {from_uid,from_name,from_photo,from_level,room,ts}
lock = threading.Lock()
MATCH_TTL = 600; RECENT_TTL = 8; REQ_TTL = 60

def _clean(uid):
    global waiting
    waiting = [w for w in waiting if w["uid"] != uid]

def _remember(a,b):
    now=time.time(); recent.setdefault(a,{})[b]=now; recent.setdefault(b,{})[a]=now

def _recently(a,b):
    return b in recent.get(a,{}) and (time.time()-recent[a][b])<RECENT_TTL

def _make_match(u1, u2, room=None, mode="free", scenario=None):
    """u1,u2 are dicts with uid,name,photo,level. Returns room."""
    if not room: room = f"sp_{u1['uid']}_{u2['uid']}_{int(time.time())}"
    base={"room":room,"ts":time.time(),"mode":mode,"scenario":scenario}
    matches[u1["uid"]] = dict(base, partner_uid=u2["uid"],partner_name=u2["name"],partner_photo=u2.get("photo",""),partner_level=u2.get("level",""), my_side="a")
    matches[u2["uid"]] = dict(base, partner_uid=u1["uid"],partner_name=u1["name"],partner_photo=u1.get("photo",""),partner_level=u1.get("level",""), my_side="b")
    _remember(u1["uid"], u2["uid"])
    return room


@app.route("/health")
def health():
    return jsonify(ok=True, db=bool(DATABASE_URL))


@app.route("/join", methods=["POST"])
def join():
    d = request.get_json(force=True)
    uid = str(d.get("uid","")).strip(); name=(d.get("name") or "Partner").strip()
    photo = d.get("photo") or ""; level=(d.get("level") or "beginner").strip()
    only_level = bool(d.get("only_level"))
    mode = (d.get("mode") or "free").strip()  # "free" or "roleplay"
    if not uid: return jsonify(error="no uid"), 400
    upsert_user(uid, name, photo, level)
    with lock:
        if uid in matches:
            m=matches[uid]; return jsonify(matched=True, **_mpayload(m))
        _clean(uid)
        me = {"uid":uid,"name":name,"photo":photo,"level":level,"only_level":only_level,"mode":mode,"ts":time.time()}
        partner = None
        for w in waiting:
            if w["uid"]==uid or _recently(uid,w["uid"]): continue
            if w.get("mode","free")!=mode: continue  # roleplay matches roleplay, free matches free
            if (only_level or w.get("only_level")) and w.get("level")!=level: continue
            partner=w; break
        if partner:
            waiting.remove(partner)
            sc = random.choice(SCENARIOS) if mode=="roleplay" else None
            _make_match(me, partner, mode=mode, scenario=sc)
            m=matches[uid]; return jsonify(matched=True, **_mpayload(m))
        else:
            waiting.append(me)
            return jsonify(matched=False, queue_pos=len(waiting))

def _mpayload(m):
    return {"partner_name":m["partner_name"],"partner_uid":m["partner_uid"],"partner_photo":m.get("partner_photo",""),"partner_level":m.get("partner_level",""),"room":m["room"],"mode":m.get("mode","free"),"scenario":m.get("scenario"),"my_side":m.get("my_side","a")}


@app.route("/poll", methods=["POST"])
def poll():
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    with lock:
        if uid in matches:
            return jsonify(matched=True, **_mpayload(matches[uid]))
        # also surface an incoming call request while waiting
        if uid in requests_in and time.time()-requests_in[uid]["ts"]<REQ_TTL:
            r=requests_in[uid]; return jsonify(matched=False, incoming=r)
        return jsonify(matched=False)


@app.route("/token", methods=["POST"])
def token():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); name=(d.get("name") or "Student").strip(); room=(d.get("room") or "").strip()
    if not uid or not room: return jsonify(error="missing"),400
    if not LIVEKIT_KEY: return jsonify(error="livekit not configured"),500
    return jsonify(token=make_token(uid,name,room), url=LIVEKIT_URL)


@app.route("/status", methods=["POST"])
def status():
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    with lock:
        return jsonify(active=(uid in matches))


def _finish(uid, seconds):
    """Pop match, record BOTH sides' minutes once, free partner. Returns partner info."""
    m = matches.pop(uid, None)
    if not m: return None
    partner = m["partner_uid"]; matches.pop(partner, None); _remember(uid, partner)
    record_call(uid, partner, m["partner_name"], seconds, m.get("partner_level",""))
    return m


@app.route("/end", methods=["POST"])
def end():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); seconds=int(d.get("seconds") or 0)
    with lock:
        _finish(uid, seconds)
    return jsonify(ok=True)


@app.route("/skip", methods=["POST"])
def skip():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); seconds=int(d.get("seconds") or 0)
    with lock:
        _finish(uid, seconds)
    return jsonify(ok=True)


@app.route("/leave", methods=["POST"])
def leave():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); seconds=int(d.get("seconds") or 0)
    with lock:
        _clean(uid); _finish(uid, seconds)
    return jsonify(ok=True)


# rating + like now take partner_uid DIRECTLY - not dependent on live match
@app.route("/rate", methods=["POST"])
def rate():
    d=request.get_json(force=True)
    target=str(d.get("partner_uid","")).strip(); stars=int(d.get("stars") or 0); like=bool(d.get("like"))
    frm=str(d.get("uid","")).strip(); frm_name=(d.get("name") or "Someone").strip(); frm_photo=d.get("photo") or ""
    if target:
        if stars:
            add_rating(target, stars)
            add_notif(target,"rating",frm,frm_name,frm_photo,str(stars))
        if like:
            add_like(target)
            add_notif(target,"like",frm,frm_name,frm_photo)
    return jsonify(ok=True)


@app.route("/like", methods=["POST"])
def like():
    d=request.get_json(force=True); target=str(d.get("partner_uid","")).strip()
    frm=str(d.get("uid","")).strip(); frm_name=(d.get("name") or "Someone").strip(); frm_photo=d.get("photo") or ""
    if target:
        add_like(target)
        add_notif(target,"like",frm,frm_name,frm_photo)
    return jsonify(ok=True)


@app.route("/profile", methods=["POST"])
def profile():
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    p=get_profile(uid)
    if not p: return jsonify(profile=None)
    p["history"]=get_call_history(uid)
    p["followers"]=follower_count(uid)
    p["following"]=following_count(uid)
    return jsonify(profile=p)


@app.route("/pubprofile", methods=["POST"])
def pubprofile():
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    viewer=str(d.get("viewer","")).strip()
    p=get_profile(uid)
    if not p: return jsonify(profile=None)
    with lock:
        online = p.get("last_seen",0) > time.time()-70
        busy = uid in matches
    # follow relationship from viewer's perspective
    fstate="none"
    if viewer and viewer!=uid:
        if is_following(viewer,uid): fstate="following"
        elif follow_request_pending(viewer,uid): fstate="requested"
    return jsonify(profile={"uid":p["uid"],"name":p["name"],"photo_url":p.get("photo_url",""),"level":p.get("level",""),
                            "bio":p.get("bio",""),"minutes":p["minutes"],"total_calls":p.get("total_calls",0),
                            "likes_received":p.get("likes_received",0),"avg_rating":p.get("avg_rating"),
                            "followers":follower_count(uid),"following":following_count(uid),
                            "last_seen":last_seen_text(p.get("last_seen",0)),
                            "online":online,"busy":busy,"follow_state":fstate})


@app.route("/setlevel", methods=["POST"])
def setlevel():
    d=request.get_json(force=True)
    set_level(str(d.get("uid","")).strip(), (d.get("level") or "beginner").strip())
    return jsonify(ok=True)


@app.route("/setbio", methods=["POST"])
def setbio():
    d=request.get_json(force=True)
    set_bio(str(d.get("uid","")).strip(), (d.get("bio") or "").strip())
    return jsonify(ok=True)


@app.route("/leaderboard", methods=["POST"])
def leaderboard():
    d=request.get_json(force=True); kind=(d.get("kind") or "minutes").strip()
    return jsonify(board=get_leaderboard(kind))


@app.route("/online", methods=["POST"])
def online():
    return jsonify(count=count_online())


# ─── Call requests (from a profile) ─────────────────────────────────────────
@app.route("/request", methods=["POST"])
def call_request():
    d=request.get_json(force=True)
    frm=str(d.get("uid","")).strip(); frm_name=(d.get("name") or "Someone").strip()
    frm_photo=d.get("photo") or ""; frm_level=(d.get("level") or "").strip()
    target=str(d.get("target_uid","")).strip()
    if not frm or not target: return jsonify(error="missing"),400
    with lock:
        if target in matches: return jsonify(ok=False, reason="busy")
        room=f"req_{frm}_{target}_{int(time.time())}"
        requests_in[target]={"from_uid":frm,"from_name":frm_name,"from_photo":frm_photo,"from_level":frm_level,"room":room,"ts":time.time()}
    # Notify the target through the bot so they see it even if the app is closed
    if APP_URL:
        tg_send(target, f"🎤 {frm_name} wants to practice English with you! Open the app to accept.",
                "Open & accept", BTN_URL)
    return jsonify(ok=True, room=room)

@app.route("/request_check", methods=["POST"])
def request_check():
    """Requester polls to see if target accepted."""
    d=request.get_json(force=True); frm=str(d.get("uid","")).strip()
    with lock:
        if frm in matches:
            return jsonify(accepted=True, **_mpayload(matches[frm]))
    return jsonify(accepted=False)

@app.route("/request_respond", methods=["POST"])
def request_respond():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); name=(d.get("name") or "Partner").strip()
    photo=d.get("photo") or ""; level=(d.get("level") or "").strip()
    accept=bool(d.get("accept"))
    with lock:
        r=requests_in.pop(uid, None)
        if not r: return jsonify(ok=False, reason="expired")
        if not accept: return jsonify(ok=True, accepted=False)
        me={"uid":uid,"name":name,"photo":photo,"level":level}
        frm={"uid":r["from_uid"],"name":r["from_name"],"photo":r["from_photo"],"level":r["from_level"]}
        _clean(uid); _clean(r["from_uid"])
        # frm is side "a" (inviter), me is side "b"
        _make_match(frm, me, room=r.get("room"), mode=r.get("mode","free"), scenario=r.get("scenario"))
        return jsonify(ok=True, accepted=True, **_mpayload(matches[uid]))


# ─── Follow / Friends endpoints ──────────────────────────────────────────────
@app.route("/follow", methods=["POST"])
def ep_follow():
    """Now sends a follow REQUEST (Instagram-private style)."""
    d=request.get_json(force=True)
    frm=str(d.get("uid","")).strip(); frm_name=(d.get("name") or "Someone").strip(); frm_photo=d.get("photo") or ""
    target=str(d.get("target_uid","")).strip()
    if not frm or not target or frm==target: return jsonify(ok=False)
    # if already following, no-op
    if is_following(frm,target): return jsonify(ok=True, state="following")
    add_follow_request(frm,target)
    add_notif(target,"follow_request",frm,frm_name,frm_photo)
    if BTN_URL:
        tg_send(target, f"👤 {frm_name} wants to follow you on Safiya Speaking. Open the app to accept.", "Open app", BTN_URL)
    return jsonify(ok=True, state="requested")

@app.route("/follow_accept", methods=["POST"])
def ep_follow_accept():
    d=request.get_json(force=True)
    me=str(d.get("uid","")).strip(); me_name=(d.get("name") or "Someone").strip(); me_photo=d.get("photo") or ""
    follower=str(d.get("follower_uid","")).strip()
    if me and follower:
        accept_follow_request(follower, me)
        add_notif(follower,"follow_accepted",me,me_name,me_photo)
    return jsonify(ok=True)

@app.route("/follow_reject", methods=["POST"])
def ep_follow_reject():
    d=request.get_json(force=True)
    me=str(d.get("uid","")).strip(); follower=str(d.get("follower_uid","")).strip()
    if me and follower: reject_follow_request(follower, me)
    return jsonify(ok=True)

@app.route("/unfollow", methods=["POST"])
def ep_unfollow():
    d=request.get_json(force=True)
    unfollow_user(str(d.get("uid","")).strip(), str(d.get("target_uid","")).strip())
    return jsonify(ok=True)

@app.route("/friends", methods=["POST"])
def ep_friends():
    d=request.get_json(force=True)
    return jsonify(friends=get_following(str(d.get("uid","")).strip()))

@app.route("/followers", methods=["POST"])
def ep_followers():
    d=request.get_json(force=True)
    return jsonify(followers=get_followers(str(d.get("uid","")).strip()))

# ─── Notifications endpoints ─────────────────────────────────────────────────
@app.route("/notifs", methods=["POST"])
def ep_notifs():
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    notifs=get_notifs(uid)
    # annotate follow_request notifs with whether still pending
    for n in notifs:
        if n.get("kind")=="follow_request":
            n["pending"]=follow_request_pending(n.get("from_uid",""), uid)
    return jsonify(notifs=notifs)

@app.route("/notif_count", methods=["POST"])
def ep_notif_count():
    d=request.get_json(force=True)
    return jsonify(count=unseen_notif_count(str(d.get("uid","")).strip()))

@app.route("/notif_seen", methods=["POST"])
def ep_notif_seen():
    d=request.get_json(force=True)
    mark_notifs_seen(str(d.get("uid","")).strip())
    return jsonify(ok=True)

# ─── Reviews / testimonials ──────────────────────────────────────────────────
@app.route("/review_submit", methods=["POST"])
def ep_review_submit():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); name=(d.get("name") or "Learner").strip()
    photo=d.get("photo") or ""; stars=int(d.get("stars") or 5); text=(d.get("text") or "").strip()
    if not uid or not text: return jsonify(ok=False)
    rid=add_review(uid,name,photo,stars,text)
    # ping admin (approval happens inside the app, no webhook needed)
    ADMIN="960055324"
    if TELEGRAM_TOKEN and rid:
        stars_s="⭐"*max(1,min(5,stars))
        tg_send(ADMIN, f"📝 New review from {name} {stars_s}\n\n\"{text}\"\n\nOpen the app → tap the bell 🔔 to approve or reject it.", "Open app", BTN_URL)
    return jsonify(ok=True)

@app.route("/reviews", methods=["POST"])
def ep_reviews():
    return jsonify(reviews=get_approved_reviews())

@app.route("/reviews_pending", methods=["POST"])
def ep_reviews_pending():
    """Admin-only: list reviews awaiting approval."""
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    if uid!="960055324": return jsonify(reviews=[])
    if not DATABASE_URL: return jsonify(reviews=[])
    conn=get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id,name,photo_url,stars,text,created FROM sp_reviews WHERE status='pending' ORDER BY id DESC LIMIT 50")
            return jsonify(reviews=[dict(r) for r in cur.fetchall()])
    except: return jsonify(reviews=[])
    finally: conn.close()

@app.route("/review_moderate", methods=["POST"])
def ep_review_moderate():
    """Admin-only: approve or reject a pending review."""
    d=request.get_json(force=True); uid=str(d.get("uid","")).strip()
    if uid!="960055324": return jsonify(ok=False)
    rid=d.get("id"); action=(d.get("action") or "").strip()
    if not rid: return jsonify(ok=False)
    rev=get_review(rid)
    if action=="approve":
        set_review_status(rid,"approved")
        if rev: add_notif(rev.get("uid",""),"review_approved","","Safiya","")
    elif action=="reject":
        set_review_status(rid,"rejected")
    return jsonify(ok=True)

# ─── Version (auto-update) ───────────────────────────────────────────────────
@app.route("/version", methods=["GET","POST"])
def ep_version():
    return jsonify(version=APP_VERSION)

# ─── Telegram webhook (handles admin Approve/Reject taps on reviews) ──────────
@app.route("/tg_webhook", methods=["POST"])
def ep_tg_webhook():
    try:
        upd=request.get_json(force=True)
    except:
        return jsonify(ok=True)
    cq=upd.get("callback_query")
    if cq:
        data=cq.get("data","")
        cid=cq.get("id")
        msg=cq.get("message",{})
        chat_id=msg.get("chat",{}).get("id")
        mid=msg.get("message_id")
        if data.startswith("rev_ok_") or data.startswith("rev_no_"):
            rid=data.split("_")[-1]
            rev=get_review(rid)
            if not rev:
                tg_answer_callback(cid,"Review not found"); return jsonify(ok=True)
            if data.startswith("rev_ok_"):
                set_review_status(rid,"approved")
                tg_answer_callback(cid,"Approved ✅")
                if chat_id and mid: tg_edit_text(chat_id,mid,f"✅ APPROVED — now shown publicly:\\n\\n\"{rev.get('text','')}\" — {rev.get('name','')}")
                add_notif(rev.get("uid",""),"review_approved","","Safiya","")
            else:
                set_review_status(rid,"rejected")
                tg_answer_callback(cid,"Rejected ❌")
                if chat_id and mid: tg_edit_text(chat_id,mid,f"❌ REJECTED (hidden):\\n\\n\"{rev.get('text','')}\" — {rev.get('name','')}")
    return jsonify(ok=True)

# ─── Community chat endpoints ────────────────────────────────────────────────
@app.route("/messages", methods=["POST"])
def ep_messages():
    return jsonify(messages=get_messages())

@app.route("/post", methods=["POST"])
def ep_post():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); name=(d.get("name") or "Guest").strip()
    photo=d.get("photo") or ""; text=(d.get("text") or "").strip()
    if uid and text: post_message(uid,name,photo,text)
    return jsonify(ok=True, messages=get_messages())

@app.route("/delmsg", methods=["POST"])
def ep_delmsg():
    d=request.get_json(force=True)
    uid=str(d.get("uid","")).strip(); mid=d.get("id")
    # only admin or the message author can delete
    ADMIN="960055324"
    if uid==ADMIN and mid: delete_message(mid)
    return jsonify(ok=True, messages=get_messages())

# ─── Roleplay endpoints ──────────────────────────────────────────────────────
@app.route("/roleplay_scenario", methods=["POST"])
def ep_roleplay_scenario():
    """Return a random scenario (used when a roleplay match is made)."""
    sc=random.choice(SCENARIOS)
    return jsonify(scenario=sc)

# roleplay invite reuses request system but tags mode=roleplay
@app.route("/roleplay_invite", methods=["POST"])
def ep_roleplay_invite():
    d=request.get_json(force=True)
    frm=str(d.get("uid","")).strip(); frm_name=(d.get("name") or "Someone").strip()
    frm_photo=d.get("photo") or ""; target=str(d.get("target_uid","")).strip()
    if not frm or not target: return jsonify(error="missing"),400
    sc=random.choice(SCENARIOS)
    with lock:
        if target in matches: return jsonify(ok=False, reason="busy")
        room=f"rp_{frm}_{target}_{int(time.time())}"
        requests_in[target]={"from_uid":frm,"from_name":frm_name,"from_photo":frm_photo,"from_level":"",
                             "room":room,"ts":time.time(),"mode":"roleplay","scenario":sc}
    if BTN_URL:
        tg_send(target, f"🎭 {frm_name} invited you to a Roleplay on Safiya Speaking! Open the app to join.",
                "Open & join", BTN_URL)
    return jsonify(ok=True, room=room)

def _reaper():
    while True:
        time.sleep(30); now=time.time()
        with lock:
            for uid in list(matches.keys()):
                if now-matches[uid]["ts"]>MATCH_TTL: matches.pop(uid,None)
            for t in list(requests_in.keys()):
                if now-requests_in[t]["ts"]>REQ_TTL: requests_in.pop(t,None)
            globals()["waiting"]=[w for w in waiting if now-w["ts"]<300]


@app.route("/")
def index():
    return send_from_directory(".", "index.html")

init_db()
threading.Thread(target=_reaper, daemon=True).start()
threading.Thread(target=_reminder_loop, daemon=True).start()

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)))
