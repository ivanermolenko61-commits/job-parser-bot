"""Ручные заказы: извлечение публичной ссылки и устойчивое хранение ввода."""
import re
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit

from database import _connect
from freelance import ai_scorer, journal as jr, sources
from freelance.alot_client import FreelanceOrder

intake_lock = threading.RLock()
LINK = re.compile(r"https?://[^\s<>]+", re.I)
PATTERNS = {
    "fl.ru": ("flru", r"/projects/(\d+)(?:/|$)"),
    "kwork.ru": ("kworkru", r"/projects/(\d+)(?:/|$)"),
    "youdo.com": ("youdoru", r"/(?:t|tasks?/)(\d+)(?:/|$)"),
    "freelance.ru": ("freelanceru", r"/(?:projects?|tasks?)/[^?#]*?(\d+)(?:\.html|/|$)"),
    "t.me": ("telegram", r"/([a-zA-Z0-9_]+/\d+)/?$"),
}


def listing(url):
    try:
        parts = urlsplit(url.rstrip(".,);]"))
        host = (parts.hostname or "").lower().removeprefix("www.")
        if parts.scheme not in ("http", "https") or parts.username or parts.password or parts.port not in (None, 80, 443):
            return None
    except ValueError:
        return None
    if not host:
        return None
    if host not in PATTERNS:
        ids = [(k.lower(), v) for k, v in parse_qsl(parts.query) if k.lower() in ("id", "task", "project") and v.isdecimal()]
        if not parts.path.strip("/") and not ids:
            return None
        path = parts.path.rstrip("/")
        canonical = "https://" + host + path + ("?" + urlencode(sorted(ids)) if ids else "")
        return host, canonical, canonical, None
    site, pattern = PATTERNS[host]
    match = re.fullmatch(pattern, parts.path) if site == "telegram" else re.match(pattern, parts.path)
    if not match:
        return None
    key = match[1].lower()
    direct = None
    if site in ("flru", "kworkru") and len(key) <= 9:
        direct = (sources.FL_ID_OFFSET if site == "flru" else sources.KWORK_ID_OFFSET) + int(key)
    return site, f"{site}:{key}", "https://" + host + parts.path, direct


def pending():
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM freelance_orders WHERE origin='manual' "
                            "AND (tech='queued' AND queue_reason!='no_text' OR tech='assessed' AND delivery='pending') LIMIT 1").fetchone() is not None


def original(url):
    identity = listing(url or "")
    if not identity:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT order_id FROM freelance_orders WHERE manual_key=? AND tech!='duplicate' LIMIT 1",
                           (identity[1],)).fetchone()
        return row[0] if row else None


def complete_from_feed(oid, order):
    """Полная запись из ленты дополняет ручную заготовку по точной ссылке."""
    body = ai_scorer.mask_contacts(order.body or "")[:ai_scorer.BODY_LIMIT]
    if len(body.strip()) < 45:
        return
    title = ai_scorer.mask_contacts(order.title or "")[:180]
    with _connect() as conn:
        conn.execute("UPDATE freelance_orders SET title=?,title_key=?,snippet=?,content_hash=?,price_text=?,"
                     "queue_reason=? WHERE order_id=? AND origin='manual' AND tech='queued' AND queue_reason=?",
                     (title, jr.title_key(order.site,title), body, jr.content_hash(title,body),
                      ai_scorer.mask_contacts(order.price_text or ""),jr.Q_WAIT,oid,jr.Q_NO_TEXT))


def from_row(row):
    return FreelanceOrder(row["order_id"], row["site"], row["title"], row["snippet"] or "",
                         row["price_text"] or "", 0, [], None, row["url"] or "")


def store(message_id, text, urls=(), supplement=None):
    from freelance.pipeline import _find_duplicate

    links = [listing(u) for u in [*LINK.findall(text), *urls] if isinstance(u, str)]
    identity = next((link for link in links if link and link[0] in {s for s, _ in PATTERNS.values()}), None) or next((link for link in links if link), None)
    clean = ai_scorer.mask_contacts(LINK.sub("", text)).strip()[:ai_scorer.BODY_LIMIT]
    oid = sources.MANUAL_ID_OFFSET + message_id
    with intake_lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM freelance_orders WHERE order_id=? AND origin='manual'",
                           (supplement or oid,)).fetchone()
        if old and (not supplement or old["queue_reason"] != jr.Q_NO_TEXT):
            return old["order_id"], old["queue_reason"] == jr.Q_NO_TEXT
        if old:
            oid = old["order_id"]
            clean = "\n".join(filter(None,(old["snippet"],clean)))[:ai_scorer.BODY_LIMIT]
        site, key, url, direct = identity or (
            (old["site"], old["manual_key"], old["url"], None) if old else ("manual", None, "", None))
        found = conn.execute("SELECT order_id,dup_of,queue_reason FROM freelance_orders WHERE order_id!=? "
                             "AND (manual_key=? OR order_id=?) ORDER BY order_id LIMIT 1",
                             (oid, key, direct)).fetchone()
        title = next((s.strip() for s in clean.splitlines() if s.strip()), "Заказ по ссылке")[:180]
        if found and found["queue_reason"] == jr.Q_NO_TEXT and not old:
            previous = conn.execute("SELECT * FROM freelance_orders WHERE order_id=? AND origin='manual'", (found["order_id"],)).fetchone()
            if previous:
                old, oid, found = previous, previous["order_id"], None
                clean = "\n".join(filter(None,(old["snippet"],clean)))[:ai_scorer.BODY_LIMIT]
                title = next((s.strip() for s in clean.splitlines() if s.strip()), title)[:180]
        if not found:
            item = FreelanceOrder(oid,site,title,clean,"",0,[],None,url)
            duplicate = _find_duplicate(item)
        else:
            duplicate = found["dup_of"] or found["order_id"]
        if duplicate is not None and duplicate != oid:
            if old:
                conn.execute("UPDATE freelance_orders SET tech='duplicate',dup_of=?,queue_reason=NULL WHERE order_id=?",
                             (duplicate, oid))
            target = conn.execute("SELECT queue_reason FROM freelance_orders WHERE order_id=?", (duplicate,)).fetchone()
            return duplicate, bool(target and target[0] == jr.Q_NO_TEXT)
        short = len(clean) < 45
        conn.execute("""INSERT INTO freelance_orders
            (order_id,origin,site,manual_key,title,title_key,snippet,content_hash,url,tech,queue_reason,delivery,status,pub_unreliable)
            VALUES (?,'manual',?,?,?,?,?,?,?,'queued',?,'none','pending',1)
            ON CONFLICT(order_id) DO UPDATE SET site=excluded.site,manual_key=excluded.manual_key,title=excluded.title,
            title_key=excluded.title_key,snippet=excluded.snippet,content_hash=excluded.content_hash,
            url=excluded.url,queue_reason=excluded.queue_reason""",
            (oid,site,key,title,jr.title_key(site,title),clean,jr.content_hash(title,clean),url,jr.Q_NO_TEXT if short else jr.Q_WAIT))
        return oid, short
