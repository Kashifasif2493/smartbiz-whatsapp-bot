import os, re, json, hmac, hashlib, sqlite3, secrets, time, base64, logging
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from cryptography.fernet import Fernet
from flask import Flask, request, session, redirect, render_template_string as R, abort
from werkzeug.security import generate_password_hash as gh, check_password_hash as ck

LOCAL = os.environ.get("LOCAL_DEV") == "1"
SECRET = os.environ.get("SECRET_KEY", "")
if not LOCAL and (len(SECRET) < 24 or SECRET == "change-me"):
    raise RuntimeError("Set SECRET_KEY (24+ random characters) in your environment variables.")
app = Flask(__name__)
app.secret_key = SECRET or "local-dev-only-key-not-for-production"
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=not LOCAL)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smartbiz")

DBURL = os.environ.get("DATABASE_URL", "")
PG = DBURL.startswith("postgres")
DBP = os.environ.get("DB_PATH", "smartbiz.db")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "").strip()  # empty = unversioned URL (always valid)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
SIGNUP_CODE = os.environ.get("SIGNUP_CODE", "")  # empty = public signup is closed
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Karachi"))
CUR = "Rs"
if PG:
    import psycopg2, psycopg2.extras

FER = Fernet(base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest()))


def enc(x):
    return FER.encrypt(x.encode()).decode()


def dec(x):
    try:
        return FER.decrypt(x.encode()).decode()
    except Exception:
        return ""


def db(sql, a=()):
    if PG:
        c = psycopg2.connect(DBURL)
        try:
            cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), a)
            r = cur.fetchall() if cur.description else []
            c.commit()
            return r
        finally:
            c.close()
    c = sqlite3.connect(DBP)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute(sql, a).fetchall()
        c.commit()
        return r
    finally:
        c.close()


def init():
    pk = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY"
    for s_ in [
        f"""CREATE TABLE IF NOT EXISTS biz(id {pk}, email TEXT UNIQUE, pw TEXT, name TEXT, addr TEXT DEFAULT '',
        owner_phone TEXT DEFAULT '', phone_id TEXT DEFAULT '', token TEXT DEFAULT '', wa_status TEXT DEFAULT 'not connected',
        opens TEXT DEFAULT '11:00', closes TEXT DEFAULT '23:00', fee INTEGER DEFAULT 150, free_above INTEGER DEFAULT 2000,
        seq INTEGER DEFAULT 1000, welcome TEXT DEFAULT 'Welcome! Send MENU to see our menu or ORDER to place an order.')""",
        f"CREATE TABLE IF NOT EXISTS prod(id {pk}, biz INTEGER, name TEXT, price INTEGER, cat TEXT, avail INTEGER DEFAULT 1)",
        f"""CREATE TABLE IF NOT EXISTS orders(id {pk}, biz INTEGER, no TEXT, ts INTEGER, phone TEXT, name TEXT, addr TEXT,
        items TEXT, sub INTEGER, fee INTEGER, total INTEGER, status TEXT DEFAULT 'Pending', paid INTEGER DEFAULT 0)""",
        "CREATE TABLE IF NOT EXISTS sess(biz INTEGER, phone TEXT, st TEXT, PRIMARY KEY(biz,phone))",
        "CREATE TABLE IF NOT EXISTS seen(mid TEXT PRIMARY KEY, ts INTEGER)",
        f"CREATE TABLE IF NOT EXISTS inq(id {pk}, biz INTEGER, ts INTEGER, phone TEXT, text TEXT)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_phone ON biz(phone_id) WHERE phone_id<>''",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_order ON orders(biz,no)",
        "CREATE INDEX IF NOT EXISTS ix_prod ON prod(biz)",
        "CREATE INDEX IF NOT EXISTS ix_orders ON orders(biz,id)",
    ]:
        db(s_)


init()


def num(v, d=0):
    try:
        return max(0, int(float(v)))
    except Exception:
        return d


# ---------------- WhatsApp send (honest: returns True only if Meta accepted) ----------------
def wa_send(b, to, text):
    tok = dec(b["token"]) if b["token"] else ""
    if not (b["phone_id"] and tok and to):
        return False
    ver = GRAPH_VERSION + "/" if GRAPH_VERSION else ""
    url = f"https://graph.facebook.com/{ver}{b['phone_id']}/messages"
    body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}}
    for i in range(3):
        try:
            r = requests.post(url, headers={"Authorization": "Bearer " + tok}, json=body, timeout=15)
        except requests.RequestException as ex:
            log.error("WA send network error biz=%s: %s", b["id"], type(ex).__name__)
            time.sleep(1.5 * (i + 1))
            continue
        if r.ok:
            if b["wa_status"] != "connected":
                db("UPDATE biz SET wa_status='connected' WHERE id=?", (b["id"],))
            return True
        try:
            er = r.json().get("error", {})
        except Exception:
            er = {}
        log.error("WA send failed biz=%s http=%s code=%s msg=%s", b["id"], r.status_code, er.get("code"), str(er.get("message"))[:200])
        if r.status_code == 401 or er.get("code") == 190:
            db("UPDATE biz SET wa_status='token expired or invalid' WHERE id=?", (b["id"],))
            return False
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (i + 1))
            continue
        if er.get("code") == 131047:
            log.warning("biz=%s: outside 24h window, template message needed", b["id"])
        return False
    return False


# ---------------- bot engine ----------------
W = dict(
    menu=["menu", "مینو", "products", "list", "dikhao", "dikhain", "items"],
    deliv=["deliver", "delivery", "ڈیلیوری", "charges"],
    hours=["timing", "timings", "hours", "open", "close", "kab", "khula", "وقت", "address", "location", "پتہ"],
    human=["human", "agent", "person", "insan", "banda", "انسان", "owner", "manager"],
    order=["order", "buy", "chahiye", "chahie", "mangwa", "چاہیے", "آرڈر", "book"],
    status=["status", "track", "اسٹیٹس"],
    hi=["hi", "hello", "salam", "assalam", "سلام", "hey"],
    yes=["yes", "haan", "han", "ji", "confirm", "ok", "okay", "ہاں", "جی"],
    no=["no", "nahi", "nahin", "cancel", "نہیں"],
    price=["price", "prices", "rate", "qeemat", "kitne", "kitna", "cost", "قیمت", "کتنے"],
    done=["done", "checkout", "bas", "finish", "بس"],
)
T = {
    "name": {"en": "Great! Please tell me your name.", "ru": "Zabardast! Apna naam bata dein.", "ur": "براہ کرم اپنا نام بتائیں۔"},
    "addr": {"en": "Please send your full delivery address (house, street, area).", "ru": "Apna poora delivery address likhein.", "ur": "اپنا مکمل ڈیلیوری ایڈریس لکھیں۔"},
    "conf": {"en": "Reply YES to confirm or NO to cancel.", "ru": "Confirm ke liye HAAN, cancel ke liye NAHI likhein.", "ur": "تصدیق کے لیے ہاں، منسوخ کے لیے نہیں لکھیں۔"},
    "hand": {"en": "I'm not sure about that, so I've passed your message to a team member. They will reply soon.",
             "ru": "Mujhe pakka jawab nahi pata, message team member ko bhej diya hai. Woh jald jawab denge.",
             "ur": "مجھے یقینی جواب معلوم نہیں، پیغام ٹیم ممبر کو بھیج دیا ہے۔ وہ جلد جواب دیں گے۔"},
}


def has(l, k):
    tk = re.split(r"[\s,.!?؟:;]+", l)
    return any((w in tk) if (len(w) <= 5 and w.isascii()) else (w in l) for w in W[k])


def lang(t):
    if re.search(r"[\u0600-\u06FF]", t):
        return "ur"
    if re.search(r"\b(kya|hai|hain|chahiye|chahie|kitne|kitna|mujhe|aap|haan|nahi|bhai|salam|assalam|dikhao|karna|kab|shukriya|bata)\b", t, re.I):
        return "ru"
    return "en"


def money(n):
    return f"{CUR} {int(n):,}"


def is_open(b):
    n = datetime.now(TZ)
    m = n.hour * 60 + n.minute
    f = lambda s: int(s[:2]) * 60 + int(s[3:5])
    o, c = f(b["opens"]), f(b["closes"])
    return o <= m < c if o <= c else (m >= o or m < c)


def avail(b):
    return db("SELECT * FROM prod WHERE biz=? AND avail=1 ORDER BY cat,id", (b["id"],))


def calc(b, cart):
    items, sub = [], 0
    for c in cart:
        p = db("SELECT * FROM prod WHERE id=? AND biz=? AND avail=1", (c["id"], b["id"]))
        if p:
            items.append({"id": p[0]["id"], "name": p[0]["name"], "price": p[0]["price"], "qty": c["qty"]})
            sub += p[0]["price"] * c["qty"]
    fee = 0 if (not items or (b["free_above"] > 0 and sub >= b["free_above"])) else b["fee"]
    return items, sub, fee, sub + fee


def summ(b, cart):
    items, sub, fee, tot = calc(b, cart)
    return "\n".join(f"• {i['qty']} x {i['name']} = {money(i['price'] * i['qty'])}" for i in items) + \
        f"\nSubtotal: {money(sub)}\nDelivery: {money(fee) if fee else 'Free'}\n*Total: {money(tot)}*"


def menu_text(b):
    a = avail(b)
    if not a:
        return "The menu is empty right now."
    t, cat = f"*{b['name']} menu*\n", ""
    for n, p in enumerate(a, 1):
        if p["cat"] != cat:
            cat = p["cat"]
            t += f"\n*{cat}*\n"
        t += f"{n}. {p['name']} - {money(p['price'])}\n"
    return t + '\nTo order send item number and quantity, e.g. "1 x2" or "2 zinger".'


def find_p(a, txt):
    l = txt.lower().strip()
    if not l:
        return None
    for p in a:
        if p["name"].lower() in l or l in p["name"].lower():
            return p
    for p in a:
        if any(len(w) > 3 and w in l.split() for w in re.split(r"[\s()]+", p["name"].lower())):
            return p


def parse_items(b, t):
    a, out = list(avail(b)), []
    for seg in re.split(r",|\band\b|\baur\b|اور|\+", t.lower()):
        seg = seg.strip()
        nums = re.findall(r"\d+", seg)
        txt = re.sub(r"\bx\b|×", " ", re.sub(r"\d+", " ", seg)).strip()
        p = q = None
        if not txt and nums:
            i = int(nums[0]) - 1
            p = a[i] if 0 <= i < len(a) else None
            q = int(nums[1]) if len(nums) > 1 else 1
        elif txt:
            p = find_p(a, txt)
            q = int(nums[0]) if nums else 1
        if p and q and q > 0:
            out.append({"id": p["id"], "qty": min(q, 50)})
    return out


def handle(b, phone, text):
    r = db("SELECT st FROM sess WHERE biz=? AND phone=?", (b["id"], phone))
    s = json.loads(r[0]["st"]) if r else {"step": "idle", "cart": [], "lang": "en", "greeted": 0}
    t, l = text.strip(), text.strip().lower()
    s["lang"] = lang(t) if lang(t) != "en" else s.get("lang", "en")
    L = s["lang"]
    out = []
    if not s.get("greeted"):
        s["greeted"] = 1
        if not is_open(b):
            out.append(f"We are closed right now (open {b['opens']}-{b['closes']}). You can still place an order; it will be confirmed when we open.")

    def reset():
        s["step"], s["cart"] = "idle", []

    def finish(msg):
        out.append(msg)
        db("INSERT INTO sess(biz,phone,st) VALUES(?,?,?) ON CONFLICT(biz,phone) DO UPDATE SET st=excluded.st", (b["id"], phone, json.dumps(s)))
        return "\n\n".join(out)

    if has(l, "human"):
        db("INSERT INTO inq(biz,ts,phone,text) VALUES(?,?,?,?)", (b["id"], int(time.time()), phone, t))
        wa_send(b, b["owner_phone"], f"Customer {phone} wants a human: {t}")
        return finish(T["hand"][L])
    st = s["step"]
    if st in ("name", "addr"):
        if l == "cancel":
            reset()
            return finish("Order cancelled. Nothing was saved.")
        if st == "name":
            s["name"], s["step"] = t[:60], "addr"
            return finish(T["addr"][L])
        if len(t) < 8:
            return finish("Please send a more complete address.")
        s["addr"], s["step"] = t, "confirm"
        return finish(f"*Please check your order*\n{summ(b, s['cart'])}\n\nName: {s['name']}\nPhone: {phone}\nAddress: {s['addr']}\n\n{T['conf'][L]}")
    if st == "confirm":
        if has(l, "yes"):
            items, sub, fee, tot = calc(b, s["cart"])
            if not items:
                reset()
                return finish("Sorry, those items are no longer available. Please start again.")
            no = f"SB-{db('UPDATE biz SET seq=seq+1 WHERE id=? RETURNING seq', (b['id'],))[0]['seq']}"  # atomic, never repeats
            db("INSERT INTO orders(biz,no,ts,phone,name,addr,items,sub,fee,total) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (b["id"], no, int(time.time()), phone, s["name"], s["addr"], json.dumps(items), sub, fee, tot))
            wa_send(b, b["owner_phone"], f"New order {no}: {money(tot)} from {s['name']} ({phone}). Check your dashboard.")
            s["last"] = no
            reset()
            return finish(f"Order {no} received! Status: Pending. The business will confirm it soon. Send \"status {no}\" anytime.")
        if has(l, "no"):
            reset()
            return finish("Order cancelled. Nothing was saved.")
        return finish(T["conf"][L])
    m = re.search(r"sb-?\s*(\d{4,})", l)
    if has(l, "status") or m:
        no = f"SB-{m.group(1)}" if m else s.get("last")
        if not no:
            return finish("Please send your order number, e.g. SB-1001.")
        o = db("SELECT * FROM orders WHERE biz=? AND no=? AND phone=?", (b["id"], no, phone))
        return finish(f"Order {no}: *{o[0]['status']}*. Total {money(o[0]['total'])}." + (" Payment verified." if o[0]["paid"] else " Payment not yet verified.") if o else f"I couldn't find order {no} on your number.")
    if st == "cart":
        if l == "cancel":
            reset()
            return finish("Order cancelled.")
        if l == "clear":
            s["cart"] = []
            return finish("Cart cleared.")
        if has(l, "done"):
            if not calc(b, s["cart"])[0]:
                return finish("Your cart is empty. Send an item number or name first.")
            s["step"] = "name"
            return finish(f"*Your cart*\n{summ(b, s['cart'])}\n\n{T['name'][L]}")
        it = parse_items(b, l)
        if it:
            for i in it:
                e = next((c for c in s["cart"] if c["id"] == i["id"]), None)
                if e:
                    e["qty"] = min(50, e["qty"] + i["qty"])
                else:
                    s["cart"].append(i)
            return finish(f"Added.\n{summ(b, s['cart'])}\n\nSend more items, or type DONE to checkout.")
    if has(l, "menu"):
        return finish(menu_text(b))
    if has(l, "order"):
        s["step"] = "cart"
        return finish(menu_text(b))
    if has(l, "deliv"):
        return finish(f"Delivery fee: {money(b['fee'])}" + (f", free on orders of {money(b['free_above'])} or more" if b["free_above"] > 0 else "") +
                      ". We cannot promise a delivery time in chat; the business confirms it when accepting your order.")
    if has(l, "hours"):
        return finish(f"Opening hours: {b['opens']}-{b['closes']} ({'open now' if is_open(b) else 'closed now'})." + (f"\nAddress: {b['addr']}" if b["addr"] else ""))
    p = find_p(list(avail(b)), l)
    if p and (has(l, "price") or len(l.split()) < 5):
        return finish(f"{p['name']}: {money(p['price'])}. Send \"order\" to start ordering.")
    if has(l, "price"):
        return finish(menu_text(b))
    if has(l, "hi"):
        return finish(b["welcome"])
    db("INSERT INTO inq(biz,ts,phone,text) VALUES(?,?,?,?)", (b["id"], int(time.time()), phone, t))
    return finish(T["hand"][L])


# ---------------- webhook (ONE shared URL for the whole Meta app) ----------------
HITS = {}


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if VERIFY_TOKEN and request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(request.args.get("hub.verify_token", ""), VERIFY_TOKEN):
            return request.args.get("hub.challenge", ""), 200
        abort(403)
    if not META_APP_SECRET:
        log.error("META_APP_SECRET not set: rejecting webhook")
        abort(503)
    good = "sha256=" + hmac.new(META_APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), good):
        abort(403)
    data = request.get_json(silent=True) or {}
    for e in data.get("entry", []):
        for ch in e.get("changes", []):
            v = ch.get("value", {})
            pid = v.get("metadata", {}).get("phone_number_id")
            r = db("SELECT * FROM biz WHERE phone_id=?", (pid,)) if pid else []
            if not r:
                log.warning("webhook for unknown phone_number_id=%s", pid)
                continue
            b = r[0]
            for st in v.get("statuses", []):
                if st.get("status") == "failed":
                    log.error("delivery failed biz=%s to=%s errors=%s", b["id"], st.get("recipient_id"), st.get("errors"))
            for m in v.get("messages", []):
                try:
                    mid, ph = m.get("id"), m.get("from")
                    if not mid or not ph:
                        continue
                    if not db("INSERT INTO seen(mid,ts) VALUES(?,?) ON CONFLICT(mid) DO NOTHING RETURNING mid", (mid, int(time.time()))):
                        continue  # duplicate event
                    now = time.time()
                    HITS[ph] = [x for x in HITS.get(ph, []) if now - x < 60] + [now]
                    if len(HITS[ph]) > 20:
                        continue  # rate limit: 20 msgs/min per customer
                    t = m.get("type")
                    if t == "text":
                        txt = m["text"]["body"]
                    elif t == "interactive":
                        i = m["interactive"]
                        txt = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                    else:
                        wa_send(b, ph, "Sorry, I can only read text messages right now. Please type your message.")
                        continue
                    if not wa_send(b, ph, handle(b, ph, txt)):
                        log.error("reply NOT delivered biz=%s to=%s", b["id"], ph)
                except Exception:
                    log.exception("message handling failed")
    return "ok", 200


# ---------------- dashboard ----------------
BASE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>SmartBiz</title>
<style>body{font:15px system-ui;margin:0;background:#E9F0ED;color:#12353A}main{max-width:700px;margin:auto;padding:12px}
.c{background:#fff;border:1px solid #cbd9d4;border-radius:10px;padding:12px;margin:0 0 10px}input,select{width:100%;padding:10px;margin:4px 0 8px;border:1px solid #cbd9d4;border-radius:8px;font:inherit}
button{background:#12353A;color:#fff;border:0;border-radius:8px;padding:10px 14px;font:inherit;font-weight:600}nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
nav a{padding:8px 12px;background:#fff;border:1px solid #cbd9d4;border-radius:8px;color:#12353A;text-decoration:none}.m{color:#4f6f6b;font-size:13px}.e{color:#C8452B}</style>
<main>{% if b %}<nav><a href=/dash>Orders</a><a href=/menu>Menu</a><a href=/settings>Settings</a><a href=/logout>Log out</a></nav>{% endif %}
{% if err %}<p class=e>{{err}}</p>{% endif %}{{body|safe}}</main>"""


def page(body_tpl, b=None, err=None, **kw):
    return R(BASE, b=b, err=err, body=R(body_tpl, b=b, **kw))


def me():
    return (db("SELECT * FROM biz WHERE id=?", (session.get("bid"),)) or [None])[0]


def csrf():
    session.setdefault("csrf", secrets.token_hex(16))
    return session["csrf"]


@app.before_request
def guard():
    if request.method == "POST" and request.endpoint != "webhook" and request.form.get("_c") != session.get("csrf"):
        abort(400)


app.jinja_env.globals["csrf"] = csrf


FAILS = {}


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        f = request.form
        em, pw = f.get("email", "").strip().lower(), f.get("pw", "")
        if f.get("mode") == "signup":
            if not SIGNUP_CODE or not hmac.compare_digest(f.get("code", ""), SIGNUP_CODE):
                return page(AUTH, err="Signup is by invitation only. Ask the platform owner for an invite code.", sc=bool(SIGNUP_CODE))
            if len(pw) < 8:
                return page(AUTH, err="Password must be at least 8 characters.", sc=True)
            if db("SELECT 1 FROM biz WHERE email=?", (em,)):
                return page(AUTH, err="Email already registered.", sc=True)
            db("INSERT INTO biz(email,pw,name) VALUES(?,?,?)", (em, gh(pw), f.get("name", "My Business")[:80]))
            session.clear()
            session["bid"] = db("SELECT id FROM biz WHERE email=?", (em,))[0]["id"]
            return redirect("/settings")
        key, now = (client_ip(), em), time.time()
        FAILS[key] = [x for x in FAILS.get(key, []) if now - x < 600]
        if len(FAILS[key]) >= 5:
            return page(AUTH, err="Too many failed attempts. Try again in 10 minutes.", sc=bool(SIGNUP_CODE))
        r = db("SELECT * FROM biz WHERE email=?", (em,))
        if not r or not ck(r[0]["pw"], pw):
            FAILS[key].append(now)
            return page(AUTH, err="Wrong email or password.", sc=bool(SIGNUP_CODE))
        session.clear()
        session["bid"] = r[0]["id"]
        return redirect("/dash")
    csrf()
    return page(AUTH, sc=bool(SIGNUP_CODE))


AUTH = """<h2>SmartBiz WhatsApp AI</h2><div class=c><form method=post><input type=hidden name=_c value="{{csrf()}}">
<label>Email</label><input name=email type=email required><label>Password</label><input name=pw type=password required>
<button name=mode value=login>Log in</button></form></div>
{% if sc %}<div class=c><h3>New business</h3><form method=post><label>Invite code</label><input name=code required><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=mode value=signup>
<label>Business name</label><input name=name required><label>Email</label><input name=email type=email required>
<label>Password (min 8)</label><input name=pw type=password minlength=8 required><button>Create business</button></form></div>{% endif %}"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/dash", methods=["GET", "POST"])
def dash():
    b = me()
    if not b:
        return redirect("/")
    if request.method == "POST":
        f = request.form
        o = db("SELECT * FROM orders WHERE biz=? AND no=?", (b["id"], f["no"]))
        if o:
            if f.get("act") == "pay":
                db("UPDATE orders SET paid=1 WHERE id=?", (o[0]["id"],))
            elif f.get("status") in ("Pending", "Accepted", "Preparing", "Dispatched", "Delivered", "Cancelled"):
                db("UPDATE orders SET status=? WHERE id=?", (f["status"], o[0]["id"]))
                if not wa_send(b, o[0]["phone"], f"Update: your order {o[0]['no']} is now {f['status']}."):
                    session["msg"] = f"{o[0]['no']}: status saved, but the WhatsApp update was NOT delivered (24h window passed or connection problem). Contact the customer directly."
        return redirect("/dash")
    orders = [dict(o, items=json.loads(o["items"])) for o in db("SELECT * FROM orders WHERE biz=? ORDER BY id DESC LIMIT 100", (b["id"],))]
    ok = [o for o in orders if o["status"] != "Cancelled"]
    inq = db("SELECT * FROM inq WHERE biz=? ORDER BY id DESC LIMIT 10", (b["id"],))
    return page("""<h2>{{b.name}}</h2><div class=c>Orders: <b>{{n}}</b> · Sales: <b>{{sales}}</b></div>
{% for o in orders %}<div class=c><b>{{o.no}}</b> <span class=m>{{o.phone}} · {{o.name}}</span><br>
{% for i in o['items'] %}{{i.qty}} x {{i.name}}<br>{% endfor %}<b>{{o.total}}</b> <span class=m>{{o.addr}}</span>
<form method=post><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=no value="{{o.no}}">
<select name=status onchange="this.form.submit()">{% for s in ['Pending','Accepted','Preparing','Dispatched','Delivered','Cancelled'] %}<option {{'selected' if s==o.status}}>{{s}}</option>{% endfor %}</select></form>
{% if o.paid %}<span class=m>Payment verified</span>{% else %}<form method=post><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=no value="{{o.no}}"><button name=act value=pay>Mark payment verified</button></form>{% endif %}</div>
{% else %}<div class=c>No orders yet.</div>{% endfor %}
<h3>Needs a human</h3>{% for q in inq %}<div class=c>{{q.phone}}: {{q.text}}</div>{% else %}<p class=m>None.</p>{% endfor %}""",
                b, err=session.pop("msg", None), orders=orders, n=len(ok), sales=money(sum(o["total"] for o in ok)), inq=inq)


@app.route("/menu", methods=["GET", "POST"])
def menu():
    b = me()
    if not b:
        return redirect("/")
    if request.method == "POST":
        f = request.form
        if f["act"] == "add":
            db("INSERT INTO prod(biz,name,price,cat) VALUES(?,?,?,?)", (b["id"], f["name"][:80], num(f["price"]), f["cat"][:40]))
        elif f["act"] == "tg":
            db("UPDATE prod SET avail=1-avail WHERE id=? AND biz=?", (num(f["id"]), b["id"]))
        elif f["act"] == "price":
            db("UPDATE prod SET price=? WHERE id=? AND biz=?", (num(f["price"]), num(f["id"]), b["id"]))
        elif f["act"] == "del":
            db("DELETE FROM prod WHERE id=? AND biz=?", (num(f["id"]), b["id"]))
        return redirect("/menu")
    return page("""<h2>Menu</h2><div class=c><form method=post><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=act value=add>
<input name=name placeholder="Item name" required><input name=price type=number min=0 placeholder="Price" required><input name=cat placeholder="Category" required><button>Add item</button></form></div>
{% for p in ps %}<div class=c><b>{{p.name}}</b> <span class=m>{{p.cat}} · {{'In stock' if p.avail else 'Out of stock'}}</span>
<form method=post><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=id value="{{p.id}}"><input type=hidden name=act value=price><input name=price type=number value="{{p.price}}"><button>Update price</button></form>
<form method=post style="display:inline"><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=id value="{{p.id}}"><button name=act value=tg>Toggle stock</button> <button name=act value=del>Remove</button></form></div>{% endfor %}""",
                b, ps=db("SELECT * FROM prod WHERE biz=? ORDER BY cat,id", (b["id"],)))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    b = me()
    if not b:
        return redirect("/")
    err = None
    if request.method == "POST":
        f = request.form
        try:
            for k in ["name", "addr", "owner_phone", "opens", "closes", "welcome"]:
                db(f"UPDATE biz SET {k}=? WHERE id=?", (f.get(k, "")[:300], b["id"]))
            for k in ["fee", "free_above"]:
                db(f"UPDATE biz SET {k}=? WHERE id=?", (num(f.get(k)), b["id"]))
            db("UPDATE biz SET phone_id=? WHERE id=?", (re.sub(r"\D", "", f.get("phone_id", "")), b["id"]))
            if f.get("token", "").strip():  # stored encrypted; never shown again
                db("UPDATE biz SET token=?, wa_status='not tested' WHERE id=?", (enc(f["token"].strip()), b["id"]))
            return redirect("/settings")
        except Exception:
            log.exception("settings save failed")
            err = "Could not save. That Phone Number ID may already be used by another business."
    b = me()
    return page("""<h2>Settings</h2><div class=c><b>Webhook URL (same for every business)</b><br><code>{{url}}</code><br>
<span class=m>Verify token = the VERIFY_TOKEN value in your server variables.</span><br><b>WhatsApp status:</b> {{b.wa_status}}</div>
<form class=c method=post><input type=hidden name=_c value="{{csrf()}}">
<label>Business name</label><input name=name value="{{b.name}}"><label>Address</label><input name=addr value="{{b.addr}}">
<label>Owner WhatsApp (country code, digits only, e.g. 923001234567)</label><input name=owner_phone value="{{b.owner_phone}}">
<label>Opens</label><input type=time name=opens value="{{b.opens}}"><label>Closes</label><input type=time name=closes value="{{b.closes}}">
<label>Delivery fee</label><input type=number name=fee value="{{b.fee}}"><label>Free delivery from</label><input type=number name=free_above value="{{b.free_above}}">
<label>Welcome message</label><input name=welcome value="{{b.welcome}}">
<label>WhatsApp Phone Number ID</label><input name=phone_id value="{{b.phone_id}}">
<label>Access token {{'(saved, encrypted)' if b.token else '(not set)'}}</label><input name=token type=password placeholder="Paste to replace">
<button>Save</button></form>""", b, err=err, url=request.host_url.rstrip("/").replace("http://", "https://") + "/webhook")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
