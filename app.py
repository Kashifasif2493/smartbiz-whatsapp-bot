import os, re, json, hmac, hashlib, sqlite3, secrets, time
import requests
from flask import Flask, request, session, redirect, render_template_string as R, abort
from werkzeug.security import generate_password_hash as gh, check_password_hash as ck

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
DBP = os.environ.get("DB_PATH", "smartbiz.db")
GRAPH = "https://graph.facebook.com/v21.0"
CUR = "Rs"


def db(sql, a=()):
    c = sqlite3.connect(DBP)
    c.row_factory = sqlite3.Row
    r = c.execute(sql, a).fetchall()
    c.commit()
    c.close()
    return r


def init():
    for s in [
        """CREATE TABLE IF NOT EXISTS biz(id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw TEXT, name TEXT, addr TEXT DEFAULT '',
        owner_phone TEXT DEFAULT '', phone_id TEXT DEFAULT '', token TEXT DEFAULT '', app_secret TEXT DEFAULT '', verify TEXT,
        open TEXT DEFAULT '11:00', close TEXT DEFAULT '23:00', fee INT DEFAULT 150, free_above INT DEFAULT 2000,
        welcome TEXT DEFAULT 'Welcome! Send MENU to see our menu or ORDER to place an order.')""",
        "CREATE TABLE IF NOT EXISTS prod(id INTEGER PRIMARY KEY, biz INT, name TEXT, price INT, cat TEXT, avail INT DEFAULT 1)",
        """CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, biz INT, no TEXT, ts INT, phone TEXT, name TEXT, addr TEXT,
        items TEXT, sub INT, fee INT, total INT, status TEXT DEFAULT 'Pending', paid INT DEFAULT 0)""",
        "CREATE TABLE IF NOT EXISTS sess(biz INT, phone TEXT, st TEXT, PRIMARY KEY(biz,phone))",
        "CREATE TABLE IF NOT EXISTS seen(mid TEXT PRIMARY KEY, ts INT)",
        "CREATE TABLE IF NOT EXISTS inq(id INTEGER PRIMARY KEY, biz INT, ts INT, phone TEXT, text TEXT)",
    ]:
        db(s)


init()

# ---------------- WhatsApp send ----------------
def wa_send(b, to, text):
    if not (b["phone_id"] and b["token"] and to):
        return False
    try:
        r = requests.post(f"{GRAPH}/{b['phone_id']}/messages",
                          headers={"Authorization": "Bearer " + b["token"]},
                          json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}}, timeout=15)
        return r.ok
    except Exception:
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
    n = time.localtime()
    m = n.tm_hour * 60 + n.tm_min
    f = lambda s: int(s[:2]) * 60 + int(s[3:5])
    o, c = f(b["open"]), f(b["close"])
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
            out.append(f"We are closed right now (open {b['open']}-{b['close']}). You can still place an order; it will be confirmed when we open.")

    def reset():
        s["step"], s["cart"] = "idle", []

    def finish(msg):
        out.append(msg)
        db("INSERT OR REPLACE INTO sess VALUES(?,?,?)", (b["id"], phone, json.dumps(s)))
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
            n = db("SELECT COUNT(*) c FROM orders WHERE biz=?", (b["id"],))[0]["c"]
            no = f"SB-{1001 + n}"
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
        return finish(f"Opening hours: {b['open']}-{b['close']} ({'open now' if is_open(b) else 'closed now'})." + (f"\nAddress: {b['addr']}" if b["addr"] else ""))
    p = find_p(list(avail(b)), l)
    if p and (has(l, "price") or len(l.split()) < 5):
        return finish(f"{p['name']}: {money(p['price'])}. Send \"order\" to start ordering.")
    if has(l, "price"):
        return finish(menu_text(b))
    if has(l, "hi"):
        return finish(b["welcome"])
    db("INSERT INTO inq(biz,ts,phone,text) VALUES(?,?,?,?)", (b["id"], int(time.time()), phone, t))
    return finish(T["hand"][L])


# ---------------- webhook ----------------
HITS = {}


@app.route("/wa/<int:bid>", methods=["GET", "POST"])
def webhook(bid):
    r = db("SELECT * FROM biz WHERE id=?", (bid,))
    if not r:
        abort(404)
    b = r[0]
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(request.args.get("hub.verify_token", ""), b["verify"]):
            return request.args.get("hub.challenge", ""), 200
        abort(403)
    raw = request.get_data()
    if b["app_secret"]:
        sig = request.headers.get("X-Hub-Signature-256", "")
        good = "sha256=" + hmac.new(b["app_secret"].encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            abort(403)
    try:
        for e in request.get_json(force=True).get("entry", []):
            for ch in e.get("changes", []):
                for m in ch.get("value", {}).get("messages", []):
                    mid, ph = m.get("id"), m.get("from")
                    if m.get("type") != "text" or not mid:
                        continue
                    if db("SELECT 1 FROM seen WHERE mid=?", (mid,)):
                        continue  # duplicate protection
                    db("INSERT INTO seen VALUES(?,?)", (mid, int(time.time())))
                    now = time.time()
                    HITS[ph] = [x for x in HITS.get(ph, []) if now - x < 60] + [now]
                    if len(HITS[ph]) > 20:
                        continue  # rate limit: 20 msgs/min per customer
                    wa_send(b, ph, handle(b, ph, m["text"]["body"]))
    except Exception as ex:
        app.logger.error("webhook error: %s", ex)
    return "ok", 200  # always 200 so Meta does not retry endlessly


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
        if request.endpoint not in ("home",):
            abort(400)


app.jinja_env.globals["csrf"] = csrf


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        f = request.form
        em, pw = f["email"].strip().lower(), f["pw"]
        if f.get("mode") == "signup":
            if len(pw) < 8:
                return page(AUTH, err="Password must be at least 8 characters.")
            if db("SELECT 1 FROM biz WHERE email=?", (em,)):
                return page(AUTH, err="Email already registered.")
            db("INSERT INTO biz(email,pw,name,verify) VALUES(?,?,?,?)", (em, gh(pw), f.get("name", "My Business")[:80], secrets.token_urlsafe(16)))
            session["bid"] = db("SELECT id FROM biz WHERE email=?", (em,))[0]["id"]
            return redirect("/settings")
        r = db("SELECT * FROM biz WHERE email=?", (em,))
        if not r or not ck(r[0]["pw"], pw):
            return page(AUTH, err="Wrong email or password.")
        session["bid"] = r[0]["id"]
        return redirect("/dash")
    csrf()
    return page(AUTH)


AUTH = """<h2>SmartBiz WhatsApp AI</h2><div class=c><form method=post><input type=hidden name=_c value="{{csrf()}}">
<label>Email</label><input name=email type=email required><label>Password</label><input name=pw type=password required>
<button name=mode value=login>Log in</button></form></div>
<div class=c><h3>New business</h3><form method=post><input type=hidden name=_c value="{{csrf()}}"><input type=hidden name=mode value=signup>
<label>Business name</label><input name=name required><label>Email</label><input name=email type=email required>
<label>Password (min 8)</label><input name=pw type=password minlength=8 required><button>Create business</button></form></div>"""


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
                wa_send(b, o[0]["phone"], f"Update: your order {o[0]['no']} is now {f['status']}.")
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
                b, orders=orders, n=len(ok), sales=money(sum(o["total"] for o in ok)), inq=inq)


@app.route("/menu", methods=["GET", "POST"])
def menu():
    b = me()
    if not b:
        return redirect("/")
    if request.method == "POST":
        f = request.form
        if f["act"] == "add":
            db("INSERT INTO prod(biz,name,price,cat) VALUES(?,?,?,?)", (b["id"], f["name"][:80], max(0, int(f["price"])), f["cat"][:40]))
        elif f["act"] == "tg":
            db("UPDATE prod SET avail=1-avail WHERE id=? AND biz=?", (f["id"], b["id"]))
        elif f["act"] == "price":
            db("UPDATE prod SET price=? WHERE id=? AND biz=?", (max(0, int(f["price"])), f["id"], b["id"]))
        elif f["act"] == "del":
            db("DELETE FROM prod WHERE id=? AND biz=?", (f["id"], b["id"]))
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
    if request.method == "POST":
        f = request.form
        for k in ["name", "addr", "owner_phone", "phone_id", "open", "close", "welcome"]:
            db(f"UPDATE biz SET {k}=? WHERE id=?", (f.get(k, "")[:300], b["id"]))
        for k in ["fee", "free_above"]:
            db(f"UPDATE biz SET {k}=? WHERE id=?", (max(0, int(f.get(k) or 0)), b["id"]))
        for k in ["token", "app_secret"]:  # secrets: only overwritten when a new value is typed
            if f.get(k, "").strip():
                db(f"UPDATE biz SET {k}=? WHERE id=?", (f[k].strip(), b["id"]))
        return redirect("/settings")
    return page("""<h2>Settings</h2><div class=c><b>Webhook URL</b><br><code>{{url}}</code><br><b>Verify token</b><br><code>{{b.verify}}</code></div>
<form class=c method=post><input type=hidden name=_c value="{{csrf()}}">
<label>Business name</label><input name=name value="{{b.name}}"><label>Address</label><input name=addr value="{{b.addr}}">
<label>Owner WhatsApp (country code, digits only, e.g. 923001234567)</label><input name=owner_phone value="{{b.owner_phone}}">
<label>Opens</label><input type=time name=open value="{{b.open}}"><label>Closes</label><input type=time name=close value="{{b.close}}">
<label>Delivery fee</label><input type=number name=fee value="{{b.fee}}"><label>Free delivery from</label><input type=number name=free_above value="{{b.free_above}}">
<label>Welcome message</label><input name=welcome value="{{b.welcome}}">
<label>WhatsApp Phone Number ID</label><input name=phone_id value="{{b.phone_id}}">
<label>Access token {{'(saved)' if b.token else '(not set)'}}</label><input name=token type=password placeholder="Paste to replace">
<label>Meta App Secret {{'(saved)' if b.app_secret else '(not set)'}}</label><input name=app_secret type=password placeholder="Paste to replace">
<button>Save</button></form>""", b, url=request.host_url.rstrip("/").replace("http://", "https://") + f"/wa/{b['id']}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
