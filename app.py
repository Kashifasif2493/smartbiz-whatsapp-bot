import os, re, json, hmac, hashlib, sqlite3, secrets, time, base64, logging
from datetime import datetime
from urllib.parse import quote
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
ADSENSE = os.environ.get("ADSENSE_CLIENT", "").strip()  # e.g. ca-pub-1234567890123456
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
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
    for t_, c_ in [("biz", "slug TEXT DEFAULT ''"), ("biz", "about TEXT DEFAULT ''"), ("biz", "btype TEXT DEFAULT 'store'"),
                   ("prod", "unit TEXT DEFAULT ''"), ("prod", "opts TEXT DEFAULT ''"), ("prod", "descr TEXT DEFAULT ''"), ("prod", "img TEXT DEFAULT ''"), ("prod", "minq INTEGER DEFAULT 1"), ("prod", "stock INTEGER DEFAULT -1")]:
        try:
            db(f"ALTER TABLE {t_} ADD COLUMN {c_}")
        except Exception:
            pass  # column already exists
    db("CREATE UNIQUE INDEX IF NOT EXISTS ix_slug ON biz(slug) WHERE slug<>''")
    for r_ in db("SELECT id,name FROM biz WHERE slug='' OR slug IS NULL"):
        db("UPDATE biz SET slug=? WHERE id=?", (uniq_slug(slugify(r_["name"]), r_["id"]), r_["id"]))


def groups(s_):
    """'Size: 6,8,10 | Design: Plain,Rip' -> [{'label':'Size','vals':[...]}, ...]; no colon = one group called Option"""
    out = []
    for part in (s_ or "").split("|"):
        part = part.strip()
        if not part:
            continue
        lab, vals = part.split(":", 1) if ":" in part else ("Option", part)
        v = [x.strip() for x in vals.split(",") if x.strip()]
        if v:
            out.append({"label": lab.strip() or "Option", "vals": v})
    return out


def take_stock(b, items):
    """Atomically reserve stock. Returns None if ok, or the name of an item that is short (and undoes what it took)."""
    done = []
    for i in items:
        p = db("SELECT stock FROM prod WHERE id=? AND biz=?", (i["id"], b["id"]))
        if not p or p[0]["stock"] is None or p[0]["stock"] < 0:
            continue  # unlimited
        if db("UPDATE prod SET stock=stock-? WHERE id=? AND biz=? AND stock>=? RETURNING id", (i["qty"], i["id"], b["id"], i["qty"])):
            done.append(i)
        else:
            for d in done:
                db("UPDATE prod SET stock=stock+? WHERE id=? AND biz=? AND stock>=0", (d["qty"], d["id"], b["id"]))
            return i["name"]
    return None


def slugify(x):
    return re.sub(r"[^a-z0-9]+", "-", x.lower()).strip("-")[:40] or "store"


def uniq_slug(base, bid=0):
    sl, i = base, 2
    while db("SELECT 1 FROM biz WHERE slug=? AND id<>?", (sl, bid)):
        sl, i = f"{base}-{i}", i + 1
    return sl


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
    return db("SELECT * FROM prod WHERE biz=? AND avail=1 AND stock<>0 ORDER BY cat,id", (b["id"],))


def calc(b, cart):
    items, sub = [], 0
    for c in cart:
        p = db("SELECT * FROM prod WHERE id=? AND biz=? AND avail=1 AND stock<>0", (c["id"], b["id"]))
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
        t += f"{n}. {p['name']} - {money(p['price'])}" + (f" / {p['unit']}" if p["unit"] else "") + "\n"
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


NEED = []


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
            if groups(p["opts"]):
                NEED.append(p["name"])
                continue
            out.append({"id": p["id"], "qty": min(max(q, p["minq"] or 1), 50)})
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
            short = take_stock(b, items) if items else None
            if short:
                return finish(f"Sorry, {short} just ran low on stock. Send CLEAR and choose again.")
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
        NEED.clear()
        it = parse_items(b, l)
        if NEED and not it:
            return finish(f"{', '.join(NEED)} needs a size/option choice, so please order it from our website store. I can still add other items here.")
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
<main>{% if b %}<nav><a href=/dash>Orders</a><a href=/menu>Menu</a><a href=/settings>Settings</a><a href="/s/{{b.slug}}" target=_blank>My store</a><a href=/logout>Log out</a></nav>{% endif %}
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
    if request.method == "POST" and request.endpoint not in ("webhook", "store_order") and request.form.get("_c") != session.get("csrf"):
        abort(400)


app.jinja_env.globals["csrf"] = csrf


FAILS = {}


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


@app.route("/login", methods=["GET", "POST"])
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
            nm = f.get("name", "My Business")[:80]
            db("INSERT INTO biz(email,pw,name,slug) VALUES(?,?,?,?)", (em, gh(pw), nm, uniq_slug(slugify(nm))))
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
        return redirect("/login")
    if request.method == "POST":
        f = request.form
        o = db("SELECT * FROM orders WHERE biz=? AND no=?", (b["id"], f["no"]))
        if o:
            if f.get("act") == "pay":
                db("UPDATE orders SET paid=1 WHERE id=?", (o[0]["id"],))
            elif f.get("status") in ("Pending", "Accepted", "Preparing", "Dispatched", "Delivered", "Cancelled"):
                if o[0]["status"] == "Cancelled" and f["status"] != "Cancelled":
                    session["msg"] = f"{o[0]['no']} was cancelled and its stock was returned, so it cannot be reopened. Ask the customer to place a new order."
                    return redirect("/dash")
                if f["status"] == "Cancelled" and o[0]["status"] != "Cancelled":
                    for i in json.loads(o[0]["items"]):
                        db("UPDATE prod SET stock=stock+? WHERE id=? AND biz=? AND stock>=0", (i["qty"], i["id"], b["id"]))
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


def pfields(f):
    img = f.get("img", "").strip()
    return (f.get("name", "").strip()[:80], num(f.get("price")), f.get("cat", "").strip()[:40], f.get("unit", "").strip()[:20], f.get("opts", "").strip()[:300],
            f.get("descr", "").strip()[:200], img[:400] if img.startswith("https://") else "", max(1, num(f.get("minq"), 1)),
            -1 if not f.get("stock", "").strip() else num(f.get("stock")))


@app.route("/menu", methods=["GET", "POST"])
def menu():
    b = me()
    if not b:
        return redirect("/login")
    if request.method == "POST":
        f = request.form
        act = f.get("act")
        if act in ("add", "save"):
            v = pfields(f)
            if not v[0] or not v[2]:
                session["msg"] = "Item name and category are required."
                return redirect("/menu")
            if act == "add":
                db("INSERT INTO prod(biz,name,price,cat) VALUES(?,?,?,?)", (b["id"], v[0], v[1], v[2]))
                pid = db("SELECT MAX(id) m FROM prod WHERE biz=?", (b["id"],))[0]["m"]
            else:
                pid = num(f.get("id"))
                db("UPDATE prod SET name=?, price=?, cat=? WHERE id=? AND biz=?", (v[0], v[1], v[2], pid, b["id"]))
            db("UPDATE prod SET unit=?, opts=?, descr=?, img=?, minq=?, stock=? WHERE id=? AND biz=?", (*v[3:], pid, b["id"]))
            session["msg"] = "Item saved."
        elif act == "tg":
            db("UPDATE prod SET avail=1-avail WHERE id=? AND biz=?", (num(f["id"]), b["id"]))
        elif act == "del":
            db("DELETE FROM prod WHERE id=? AND biz=?", (num(f["id"]), b["id"]))
        return redirect("/menu")
    ep = None
    if request.args.get("edit"):
        r = db("SELECT * FROM prod WHERE id=? AND biz=?", (num(request.args.get("edit")), b["id"]))
        ep = r[0] if r else None
    return page("""<h2>{{'Edit item' if ep else 'Menu'}}</h2><div class=c><form method=post><input type=hidden name=_c value="{{csrf()}}">
<input type=hidden name=act value="{{'save' if ep else 'add'}}"><input type=hidden name=id value="{{ep.id if ep else ''}}">
<label>Item name</label><input name=name required value="{{ep.name if ep else ''}}"><label>Price</label><input name=price type=number min=0 required value="{{ep.price if ep else ''}}">
<label>Category (e.g. Patti, Rexine, Burgers)</label><input name=cat required value="{{ep.cat if ep else ''}}">
<label>Unit (optional): dozen, meter, pair, kg, plate</label><input name=unit value="{{ep.unit if ep else ''}}">
<label>Options (only what is available now): Size: 6,8,10 | Colour: Black,Brown</label><input name=opts value="{{ep.opts if ep else ''}}">
<label>Short description (optional)</label><input name=descr value="{{ep.descr if ep else ''}}">
<label>Minimum order quantity (default 1)</label><input name=minq type=number min=1 value="{{ep.minq if ep and ep.minq and ep.minq>1 else ''}}">
<label>Stock quantity (empty = unlimited)</label><input name=stock type=number min=0 value="{{ep.stock if ep and ep.stock is not none and ep.stock>=0 else ''}}">
<label>Photo link (optional, https://...)</label><input name=img value="{{ep.img if ep else ''}}">
<button>{{'Save changes' if ep else 'Add item'}}</button>{% if ep %} <a href=/menu>Cancel</a>{% endif %}</form></div>
{% for p in ps %}<div class=c><b>{{p.name}}</b> <span class=m>{{p.cat}}{% if p.unit %} · per {{p.unit}}{% endif %}{% if p.opts %} · {{p.opts}}{% endif %}{% if p.minq and p.minq>1 %} · min {{p.minq}}{% endif %}</span><br>
{{p.price}} · <span class=m>{{'Stock: ' ~ p.stock if p.stock is not none and p.stock>=0 else 'Unlimited stock'}} · {{'Visible' if p.avail else 'Hidden (out of stock)'}}</span>
<form method=post style="margin-top:6px"><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=id value="{{p.id}}"><a href="/menu?edit={{p.id}}" style="padding:10px 14px;display:inline-block;border:1px solid #cbd9d4;border-radius:8px;text-decoration:none;color:#12353A">Edit</a>
<button name=act value=tg>Toggle stock</button> <button name=act value=del onclick="return confirm('Remove this item?')">Remove</button></form></div>{% endfor %}""",
                b, err=session.pop("msg", None), ep=ep, ps=db("SELECT * FROM prod WHERE biz=? ORDER BY cat,id", (b["id"],)))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    b = me()
    if not b:
        return redirect("/login")
    err = None
    if request.method == "POST":
        f = request.form
        try:
            db("UPDATE biz SET slug=?, btype=?, about=? WHERE id=?", (uniq_slug(slugify(f.get("slug") or b["name"]), b["id"]),
               f.get("btype") if f.get("btype") in ("restaurant", "store", "shoes", "clothing", "grocery", "salon") else "store", f.get("about", "")[:300], b["id"]))
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
<label>Business name</label><input name=name value="{{b.name}}">
<label>Store link name (letters, numbers, dashes)</label><input name=slug value="{{b.slug}}"><span class=m>Your public store: {{url.replace('/webhook','')}}/s/{{b.slug}}</span>
<label>Business type</label><select name=btype>{% for t in ['restaurant','store','shoes','clothing','grocery','salon'] %}<option {{'selected' if t==b.btype}}>{{t}}</option>{% endfor %}</select>
<label>Short description for Google (max 300 characters)</label><input name=about value="{{b.about}}"><label>Address</label><input name=addr value="{{b.addr}}">
<label>Owner WhatsApp (country code, digits only, e.g. 923001234567)</label><input name=owner_phone value="{{b.owner_phone}}">
<label>Opens</label><input type=time name=opens value="{{b.opens}}"><label>Closes</label><input type=time name=closes value="{{b.closes}}">
<label>Delivery fee</label><input type=number name=fee value="{{b.fee}}"><label>Free delivery from</label><input type=number name=free_above value="{{b.free_above}}">
<label>Welcome message</label><input name=welcome value="{{b.welcome}}">
<label>WhatsApp Phone Number ID</label><input name=phone_id value="{{b.phone_id}}">
<label>Access token {{'(saved, encrypted)' if b.token else '(not set)'}}</label><input name=token type=password placeholder="Paste to replace">
<button>Save</button></form>""", b, err=err, url=request.host_url.rstrip("/").replace("http://", "https://") + "/webhook")



# ---------------- public website: landing, stores, SEO ----------------
def base_url():
    return request.host_url.rstrip("/").replace("http://", "https://")


def adsense_tag():
    return f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={ADSENSE}" crossorigin="anonymous"></script>' if ADSENSE else ""


PUB = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title><meta name="description" content="{{desc}}"><link rel="canonical" href="{{url}}">
<meta property="og:type" content="website"><meta property="og:title" content="{{title}}"><meta property="og:description" content="{{desc}}"><meta property="og:url" content="{{url}}">
<meta name="twitter:card" content="summary">{% if noindex %}<meta name="robots" content="noindex,follow">{% endif %}
{% if ld %}<script type="application/ld+json">{{ld|tojson}}</script>{% endif %}{{ads|safe}}
<style>:root{--ink:#14201e;--mut:#5c6f6a;--bg:#f3f6f4;--card:#fff;--ac:#0f5a52;--ac2:#e0f0ec;--line:#d5e0dc}
@media(prefers-color-scheme:dark){:root{--ink:#e8f1ee;--mut:#9bb3ac;--bg:#0c1715;--card:#13241f;--ac:#5fd0bf;--ac2:#17332d;--line:#244039}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:auto;padding:20px 16px 60px}h1,h2{font-family:Georgia,"Times New Roman",serif;line-height:1.15}h1{font-size:clamp(30px,6vw,46px);margin:.2em 0}
a{color:var(--ac)}.btn{display:inline-block;background:var(--ac);color:var(--bg);padding:12px 18px;border-radius:999px;font-weight:700;text-decoration:none}
.g{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}.c{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}
footer{border-top:1px solid var(--line);margin-top:40px;padding-top:14px;color:var(--mut);font-size:14px}.m{color:var(--mut)}</style></head><body><main>{{body|safe}}
<footer><a href=/>SmartBiz</a> · <a href=/about>About</a> · <a href=/privacy>Privacy Policy</a> · <a href=/login>Business login</a></footer></main></body></html>"""


def pub(body, title, desc, path="/", ld=None, noindex=False, ads=True):
    return R(PUB, body=body, title=title, desc=desc, url=base_url() + path, ld=ld, noindex=noindex, ads=adsense_tag() if ads else "")


@app.route("/")
def landing():
    body = """<p class=m>SmartBiz · online ordering for local shops</p><h1>Give your shop its own<br>order-taking website.</h1>
<p style="font-size:18px;max-width:560px">Customers browse your menu or catalogue, pick what they want and send an order. You see it in your dashboard and confirm it on WhatsApp. No coding, no app to install.</p>
<p><a class=btn href=/login>Business login</a></p>
<div class=g><div class=c><h2 style="font-size:20px">Restaurants</h2>Menu, categories and add-ons, with delivery fee and free-delivery rules.</div>
<div class=c><h2 style="font-size:20px">Material and general stores</h2>Items sold by pair, meter, kg or piece, with size and colour options.</div>
<div class=c><h2 style="font-size:20px">Every shop separate</h2>Each business has its own link, products, orders and customers.</div></div>
<h2>How it works</h2><div class=g><div class=c><b>1. Add your items</b><br>Names, prices, photos and stock status.</div><div class=c><b>2. Share your link</b><br>Put it on Facebook, Instagram and your WhatsApp status.</div><div class=c><b>3. Receive orders</b><br>Each order gets a number and appears in your dashboard.</div></div>"""
    ld = {"@context": "https://schema.org", "@type": "WebSite", "name": "SmartBiz", "url": base_url() + "/"}
    return pub(body, "SmartBiz: online ordering website for shops and restaurants", "Create an online store link for your restaurant, shoe material store or general store. Customers order, you manage orders from a simple dashboard.", "/", ld)


@app.route("/about")
def about():
    return pub("<h1>About SmartBiz</h1><p>SmartBiz helps small shops and restaurants take orders online without building a website. Every business gets its own store page and a dashboard to manage menu, stock and orders.</p><p>Payment and delivery are handled directly between the customer and the shop.</p>" +
               (f"<p>Contact: {CONTACT_EMAIL}</p>" if CONTACT_EMAIL else ""), "About SmartBiz", "About SmartBiz, an online ordering platform for local shops and restaurants.", "/about")


@app.route("/privacy")
def privacy():
    return pub("""<h1>Privacy Policy</h1><p>When you place an order on a store page we collect your name, phone number, delivery address and the items you order. This is used only so the shop can prepare, confirm and deliver your order, and to show your order status.</p>
<p>Order details are stored securely and are visible only to the shop you ordered from and the platform administrator. We do not sell your data.</p>
<p>This website may show Google AdSense advertisements. Google and its partners may use cookies to show ads based on your visits to this and other sites. You can manage ad personalisation at adssettings.google.com.</p>
<p>To ask for your order data to be deleted, contact the shop or write to us""" + (f" at {CONTACT_EMAIL}" if CONTACT_EMAIL else "") + ".</p>", "Privacy Policy | SmartBiz", "How SmartBiz collects and uses order information.", "/privacy")


@app.route("/robots.txt")
def robots():
    return f"User-agent: *\nAllow: /\nDisallow: /dash\nDisallow: /menu\nDisallow: /settings\nDisallow: /webhook\nDisallow: /login\nSitemap: {base_url()}/sitemap.xml\n", 200, {"Content-Type": "text/plain"}


@app.route("/ads.txt")
def ads_txt():
    return (f"google.com, {ADSENSE.replace('ca-', '')}, DIRECT, f08c47fec0942fa0\n" if ADSENSE else ""), 200, {"Content-Type": "text/plain"}


@app.route("/sitemap.xml")
def sitemap():
    u = [base_url() + p for p in ("/", "/about", "/privacy")] + [f"{base_url()}/s/{r['slug']}" for r in db("SELECT slug FROM biz WHERE slug<>''")]
    return '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{x}</loc></url>" for x in u) + "</urlset>", 200, {"Content-Type": "application/xml"}


STORE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title><meta name="description" content="{{desc}}"><link rel="canonical" href="{{url}}">
<meta property="og:type" content="website"><meta property="og:title" content="{{title}}"><meta property="og:description" content="{{desc}}"><meta property="og:url" content="{{url}}"><meta property="og:site_name" content="{{b.name}}">
{% if img %}<meta property="og:image" content="{{img}}">{% endif %}<meta name="twitter:card" content="summary">
<script type="application/ld+json">{{ld|tojson}}</script>
<style>:root{--h:{{hue}};--ac:hsl(var(--h) 60% 30%);--ac2:hsl(var(--h) 55% 92%);--ink:#17201f;--mut:#5d6f6b;--bg:#f6f4ef;--card:#fff;--line:#ddd8cc}
@media(prefers-color-scheme:dark){:root{--ac:hsl(var(--h) 65% 68%);--ac2:hsl(var(--h) 35% 17%);--ink:#eef2f0;--mut:#a3b5b0;--bg:#101614;--card:#172220;--line:#2a3a36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--ac);color:var(--bg);padding:26px 16px 22px}@media(prefers-color-scheme:dark){header{color:#0b1210}}
.w{max-width:900px;margin:auto}h1{font:700 clamp(28px,6vw,44px)/1.1 Georgia,"Times New Roman",serif;margin:6px 0}
.badge{display:inline-block;padding:3px 12px;border-radius:999px;background:rgba(255,255,255,.22);font-size:13px;font-weight:700}
.about{max-width:620px;opacity:.95;margin:6px 0 0}#chips{display:flex;gap:8px;overflow-x:auto;padding:14px 16px;max-width:900px;margin:auto}
.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:8px 16px;white-space:nowrap;font:inherit;font-weight:600}.chip.on{background:var(--ac);color:var(--bg);border-color:var(--ac)}
#grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));padding:4px 16px 120px;max-width:900px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:12px;display:flex;flex-direction:column;gap:6px}
.card img,.mono{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:12px}.mono{display:grid;place-items:center;background:var(--ac2);color:var(--ac);font:700 40px Georgia,serif}
.card h3{margin:2px 0 0;font-size:16px;line-height:1.25}.card p{margin:0;color:var(--mut);font-size:14px}.pr{font-weight:800;font-size:18px}.pr small{font-weight:400;color:var(--mut)}
select,input,textarea{width:100%;padding:11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
button.add,button.go{background:var(--ac);color:var(--bg);border:0;border-radius:12px;padding:11px;font:700 15px inherit;margin-top:auto}.sold{color:#b3392b;font-weight:700}.low{color:#b3392b;font-size:13px}.mut{color:var(--mut)}
#bar{position:fixed;left:12px;right:12px;bottom:12px;max-width:876px;margin:auto;background:var(--ac);color:var(--bg);border:0;border-radius:16px;padding:16px;font:700 16px inherit;box-shadow:0 8px 24px rgba(0,0,0,.25)}
#sheet{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:flex-end;justify-content:center;z-index:9}#sheet.on{display:flex}
#panel{background:var(--bg);width:100%;max-width:560px;max-height:92vh;overflow:auto;border-radius:22px 22px 0 0;padding:18px}
.line{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-bottom:1px dashed var(--line)}.q button{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-weight:700}
.tk{background:var(--card);border:2px dashed var(--ac);border-radius:16px;padding:16px;text-align:center}.hp{position:absolute;left:-9999px}label{font-size:13px;color:var(--mut)}
footer{max-width:900px;margin:0 auto;padding:0 16px 100px;color:var(--mut);font-size:13px}footer a{color:var(--mut)}</style></head><body>
<header><div class=w><span class=badge>{{'Open now' if is_open else 'Closed now'}} · {{b.opens}}–{{b.closes}}</span><h1>{{b.name}}</h1>{% if b.about %}<p class=about>{{b.about}}</p>{% endif %}{% if b.addr %}<p class=about>📍 {{b.addr}}</p>{% endif %}</div></header>
<div id=chips></div><div id=grid></div><footer>Order online from {{b.name}}. Payment and delivery are arranged directly with the shop. <a href="/s/{{b.slug}}/track">Track an order</a> · <a href=/privacy>Privacy</a> · Powered by <a href=/>SmartBiz</a></footer>
<button id=bar hidden></button><div id=sheet><div id=panel></div></div>
<script type="application/json" id=d>{{data|tojson}}</script>
<script>
const $=s=>document.querySelector(s),D=JSON.parse($('#d').textContent),C={};let cat='All';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),M=n=>D.cur+' '+n.toLocaleString('en-US');
function draw(){const cs=['All',...new Set(D.prods.map(p=>p.cat))];$('#chips').innerHTML=cs.map(c=>`<button class="chip ${c==cat?'on':''}" data-c="${esc(c)}">${esc(c)}</button>`).join('');
$('#grid').innerHTML=D.prods.filter(p=>cat=='All'||p.cat==cat).map(p=>`<article class=card>${p.img?`<img src="${esc(p.img)}" alt="${esc(p.name)}" loading=lazy>`:`<div class=mono>${esc(p.name[0])}</div>`}<h3>${esc(p.name)}</h3>${p.descr?`<p>${esc(p.descr)}</p>`:''}<div class=pr>${M(p.price)}${p.unit?`<small> / ${esc(p.unit)}</small>`:''}</div>${p.avail?`${p.groups.map((g,i)=>`<label>${esc(g.label)}</label><select id=o${p.id}_${i}>${g.vals.map(o=>`<option>${esc(o)}</option>`).join('')}</select>`).join('')}${p.minq>1?`<small class=mut>Minimum ${p.minq}${p.unit?' '+esc(p.unit):''}</small>`:''}${p.stock>=0&&p.stock<=5?`<small class=low>Only ${p.stock} left</small>`:''}<button class=add data-a=${p.id}>Add to order</button>`:'<span class=sold>Sold out</span>'}</article>`).join('');bar()}
function tot(){let s=0,n=0;for(const k in C){s+=C[k].p.price*C[k].q;n+=C[k].q}const fee=n&&!(D.free>0&&s>=D.free)?D.fee:0;return{s,n,fee,t:s+fee}}
function bar(){const t=tot();$('#bar').hidden=!t.n;$('#bar').textContent=`View order · ${t.n} item${t.n>1?'s':''} · ${M(t.s)}`}
function sheet(){const t=tot();if(!t.n){$('#sheet').classList.remove('on');return}
$('#panel').innerHTML=`<h2 style="font-family:Georgia,serif;margin:0 0 6px">Your order</h2>${Object.entries(C).map(([k,v])=>`<div class=line><span>${esc(v.p.name)}${v.o.length?` (${esc(v.o.join(', '))})`:''}<br><small>${M(v.p.price)}${v.p.unit?' / '+esc(v.p.unit):''}</small></span><span class=q><button data-m="${esc(k)}">−</button> ${v.q} <button data-p="${esc(k)}">+</button></span></div>`).join('')}
<div class=line><span>Subtotal</span><b>${M(t.s)}</b></div><div class=line><span>Delivery</span><b>${t.fee?M(t.fee):'Free'}</b></div><div class=line><span>Total</span><b>${M(t.t)}</b></div>
<p style="margin:12px 0 4px"><label>Your name</label><input id=n autocomplete=name></p><p style="margin:4px 0"><label>Phone number</label><input id=ph type=tel autocomplete=tel></p><p style="margin:4px 0"><label>Delivery address</label><textarea id=ad rows=2 autocomplete=street-address></textarea></p>
<input class=hp id=hp tabindex=-1 autocomplete=off><p id=er style="color:#b3392b;margin:4px 0"></p><button class=go style="width:100%" id=go>Place order</button><button class=chip style="width:100%;margin-top:8px" data-x=1>Keep shopping</button>`;$('#sheet').classList.add('on')}
document.addEventListener('click',async e=>{const t=e.target,c=t.dataset;
if(c.c){cat=c.c;draw()}else if(c.a){const p=D.prods.find(x=>x.id==c.a),o=p.groups.map((g,i)=>$('#o'+p.id+'_'+i).value),k=p.id+'|'+o.join('|'),cap=p.stock>=0?p.stock:99;C[k]=C[k]||{p,o,q:0};C[k].q=C[k].q?Math.min(cap,C[k].q+1):Math.min(cap,p.minq);bar()}
else if(t.id=='bar')sheet();else if(c.p){const v=C[c.p];v.q=Math.min(v.p.stock>=0?v.p.stock:99,v.q+1);sheet();bar()}else if(c.m){const v=C[c.m];if(v.q-1<v.p.minq)delete C[c.m];else v.q--;sheet();bar()}
else if(c.x||t.id=='sheet')$('#sheet').classList.remove('on');
else if(t.id=='go'){t.disabled=true;$('#er').textContent='';try{const r=await fetch('/s/'+D.slug+'/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('#n').value,phone:$('#ph').value,addr:$('#ad').value,website:$('#hp').value,items:Object.values(C).map(v=>({id:v.p.id,sel:v.o,qty:v.q}))})}),j=await r.json();
if(!r.ok){$('#er').textContent=j.error||'Something went wrong.';t.disabled=false;return}
for(const k in C)delete C[k];bar();$('#panel').innerHTML=`<div class=tk><p style="margin:0;color:var(--mut)">Order received</p><h2 style="font-family:Georgia,serif;font-size:34px;margin:4px 0">${esc(j.no)}</h2><p>Total ${M(j.total)}. The shop will confirm your order soon. Save this number to track it.</p>${j.wa?`<a class=go style="display:block;background:var(--ac);color:var(--bg);padding:12px;border-radius:12px;text-decoration:none;font-weight:700" href="${j.wa}">Send this order on WhatsApp</a>`:''}<p><a href="/s/${D.slug}/track?no=${esc(j.no)}">Track my order</a></p></div><button class=chip style="width:100%;margin-top:10px" data-x=1>Close</button>`}catch(x){$('#er').textContent='Network problem. Please try again.';t.disabled=false}}});
draw();
</script></body></html>"""

BT = {"restaurant": "Restaurant", "store": "Store", "shoes": "Store", "clothing": "ClothingStore", "grocery": "GroceryStore", "salon": "HealthAndBeautyBusiness"}


def by_slug(slug):
    r = db("SELECT * FROM biz WHERE slug=?", (slug,))
    return r[0] if r else abort(404)


@app.route("/s/<slug>")
def store(slug):
    b = by_slug(slug)
    ps = db("SELECT * FROM prod WHERE biz=? ORDER BY cat,id", (b["id"],))
    prods = [{"id": p["id"], "name": p["name"], "price": p["price"], "cat": p["cat"], "unit": p["unit"] or "", "descr": p["descr"] or "",
              "img": p["img"] or "", "avail": bool(p["avail"]) and p["stock"] != 0, "groups": groups(p["opts"]),
              "minq": p["minq"] or 1, "stock": -1 if p["stock"] is None else p["stock"]} for p in ps]
    url = f"{base_url()}/s/{b['slug']}"
    kind = {"restaurant": "restaurant", "shoes": "shoe store"}.get(b["btype"], b["btype"] if b["btype"] != "store" else "shop")
    desc = (b["about"] or f"Order online from {b['name']}, a {kind}" + (f" in {b['addr']}" if b["addr"] else "") + ". See prices and place your order in a minute.")[:300]
    node = {"@type": BT.get(b["btype"], "Store"), "@id": url + "#biz", "name": b["name"], "url": url, "description": desc,
            "address": {"@type": "PostalAddress", "streetAddress": b["addr"] or "", "addressCountry": "PK"},
            "openingHoursSpecification": [{"@type": "OpeningHoursSpecification", "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], "opens": b["opens"], "closes": b["closes"]}]}
    if b["owner_phone"]:
        node["telephone"] = "+" + b["owner_phone"]
    if prods and prods[0]["img"]:
        node["image"] = prods[0]["img"]
    graph = [node, {"@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem", "position": 1, "name": "SmartBiz", "item": base_url() + "/"},
                                                                    {"@type": "ListItem", "position": 2, "name": b["name"], "item": url}]}]
    for p in prods[:50]:
        pr = {"@type": "Product", "name": p["name"], "category": p["cat"], "description": p["descr"] or p["name"],
              "offers": {"@type": "Offer", "price": str(p["price"]), "priceCurrency": "PKR", "url": url, "seller": {"@id": url + "#biz"},
                         "availability": "https://schema.org/InStock" if p["avail"] else "https://schema.org/OutOfStock"}}
        if p["img"]:
            pr["image"] = p["img"]
        graph.append(pr)
    hue = int(hashlib.md5(b["name"].encode()).hexdigest()[:4], 16) % 360
    data = {"slug": b["slug"], "cur": CUR, "fee": b["fee"], "free": b["free_above"], "prods": prods}
    return R(STORE, b=b, title=f"{b['name']}: order online | menu and prices"[:70], desc=desc, url=url, ld={"@context": "https://schema.org", "@graph": graph},
             img=next((p["img"] for p in prods if p["img"]), ""), hue=hue, data=data, is_open=is_open(b))


OH = {}


@app.route("/s/<slug>/order", methods=["POST"])
def store_order(slug):
    b = by_slug(slug)
    ip, now = client_ip(), time.time()
    OH[ip] = [x for x in OH.get(ip, []) if now - x < 600]
    if len(OH[ip]) >= 5:
        return {"error": "Too many orders from this device. Please try again in a few minutes."}, 429
    d = request.get_json(silent=True) or {}
    if d.get("website"):
        return {"error": "Rejected."}, 400  # honeypot for bots
    name, phone, addr = str(d.get("name", "")).strip()[:60], re.sub(r"\D", "", str(d.get("phone", "")))[:15], str(d.get("addr", "")).strip()[:200]
    if len(name) < 2 or len(phone) < 10 or len(addr) < 8:
        return {"error": "Please enter your name, a valid phone number and your full address."}, 400
    items, sub = [], 0
    for it in (d.get("items") or [])[:50]:
        if not isinstance(it, dict):
            continue
        q, p = min(num(it.get("qty")), 99), db("SELECT * FROM prod WHERE id=? AND biz=? AND avail=1 AND stock<>0", (num(it.get("id")), b["id"]))
        if not p or q < 1:
            continue
        p = p[0]
        gs, sel = groups(p["opts"]), it.get("sel") if isinstance(it.get("sel"), list) else []
        if len(sel) != len(gs) or any(str(sel[i]) not in g["vals"] for i, g in enumerate(gs)):
            return {"error": f"Please choose {' and '.join(g['label'] for g in gs)} for {p['name']}."}, 400
        if q < (p["minq"] or 1):
            return {"error": f"Minimum order for {p['name']} is {p['minq']}."}, 400
        items.append({"id": p["id"], "name": p["name"] + (f" ({', '.join(str(x) for x in sel)})" if sel else "") + (f" [{p['unit']}]" if p["unit"] else ""), "price": p["price"], "qty": q})
        sub += p["price"] * q
    if not items:
        return {"error": "Your order is empty or the items are no longer available."}, 400
    short = take_stock(b, items)
    if short:
        return {"error": f"Sorry, {short} does not have enough stock for that quantity. Please reduce it."}, 400
    fee = 0 if (b["free_above"] > 0 and sub >= b["free_above"]) else b["fee"]
    total = sub + fee
    no = f"SB-{db('UPDATE biz SET seq=seq+1 WHERE id=? RETURNING seq', (b['id'],))[0]['seq']}"
    db("INSERT INTO orders(biz,no,ts,phone,name,addr,items,sub,fee,total) VALUES(?,?,?,?,?,?,?,?,?,?)", (b["id"], no, int(now), phone, name, addr, json.dumps(items), sub, fee, total))
    OH[ip].append(now)
    wa_send(b, b["owner_phone"], f"New website order {no}: {money(total)} from {name} ({phone}). Check your dashboard.")
    text = f"Order {no} from {b['name']}\n" + "\n".join(f"{i['qty']} x {i['name']} = {money(i['price'] * i['qty'])}" for i in items) + \
        f"\nDelivery: {money(fee) if fee else 'Free'}\nTotal: {money(total)}\n\nName: {name}\nPhone: {phone}\nAddress: {addr}"
    return {"no": no, "total": total, "wa": f"https://wa.me/{b['owner_phone']}?text={quote(text)}" if b["owner_phone"] else ""}


@app.route("/s/<slug>/track")
def track(slug):
    b = by_slug(slug)
    no, ph = request.args.get("no", "").strip().upper(), re.sub(r"\D", "", request.args.get("phone", ""))
    res = ""
    if no and ph:
        o = db("SELECT * FROM orders WHERE biz=? AND no=?", (b["id"], no))
        res = (f"<div class=c><b>{R('{{n}}', n=no)}</b>: {R('{{s}}', s=o[0]['status'])}. Total {money(o[0]['total'])}. " + ("Payment verified." if o[0]["paid"] else "Payment not yet verified.") + "</div>"
               if o and o[0]["phone"][-7:] == ph[-7:] else "<p>No order found with that number and phone.</p>")
    body = f"""<h1>Track your order</h1><p class=m>{R('{{n}}', n=b['name'])}</p><form class=c><label>Order number</label><input name=no value="{R('{{n}}', n=no)}" placeholder="SB-1001" style="width:100%;padding:10px;margin:4px 0 10px">
<label>Phone number used for the order</label><input name=phone type=tel style="width:100%;padding:10px;margin:4px 0 10px"><button class=btn style="border:0">Check status</button></form>{res}<p><a href="/s/{b['slug']}">Back to store</a></p>"""
    return pub(body, f"Track order | {b['name']}", "Check the status of your order.", f"/s/{b['slug']}/track", noindex=True, ads=False)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
      
