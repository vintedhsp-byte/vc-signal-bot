"""
VC-Signal Telegram Bot — aggregate VC portfolio additions + listing/unlock signals

What it does (high level):
- Polls known VC portfolio pages for new projects (Binance Labs, Coinbase Ventures, a16z Crypto, Wintermute, + extensible list)
- (Optional) pulls public feeds for listings/unlocks (DropsTab/CoinGecko/CMC — via simple HTTP; keep disabled if you don’t have keys)
- Scores each project (e.g., VC backing + catalyst) and sends a Telegram alert only when threshold is met
- De-dupes and persists state in a lightweight local JSON file

IMPORTANT
- These portfolio pages change HTML often. The scrapers below are written to be resilient and easy to tweak.
- Treat alerts as RESEARCH STARTERS — not buy signals.

Quick start
-----------
1) Python 3.10+
2) `pip install -r requirements.txt` (see bottom of file for the list)
3) Create a bot with @BotFather and grab the token.
4) Create a Telegram group or use your user ID; add the bot to the group.
5) Create a `.env` file (see template below) next to this script.
6) Run: `python vc_signal_bot.py`

.env template
-------------
TELEGRAM_BOT_TOKEN=123456:ABCDEF...
TELEGRAM_CHAT_ID=123456789                # your user ID or group ID
POLL_INTERVAL_SECONDS=900                 # 15 minutes

Optional (set blank to disable)
DROPS_API_KEY=
COINGECKO_NEW_COIN_CHECK=true             # uses public endpoint, rate-limited

Tuning
------
- Edit VC_SOURCES to add/remove VCs. Each entry can use one of the provided parser styles or a custom function.
- Adjust SCORE_WEIGHTS / SCORE_THRESHOLD to control what triggers an alert.

"""
from __future__ import annotations
import os
import re
import time
import json
import html
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv


# -------------------------
# Setup
# -------------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))

DROPS_API_KEY = os.getenv("DROPS_API_KEY", "")
COINGECKO_NEW_COIN_CHECK = os.getenv("COINGECKO_NEW_COIN_CHECK", "true").lower() == "true"

STATE_PATH = os.getenv("STATE_PATH", "vc_signal_state.json")
USER_AGENT = "VC-Signal-Bot/1.0 (+https://example.com)"
REQUIRE_MULTI_VC = os.getenv("REQUIRE_MULTI_VC", "true").lower() == "true"

# --- Recap / digest settings ---
DIGEST_INTERVAL_HOURS = int(os.getenv("DIGEST_INTERVAL_HOURS", "4"))  # one email every 4h
DIGEST_SUBJECT = os.getenv("DIGEST_SUBJECT", "VC Signals — Recap")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Paris")


HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# -------------------------
# Helpers & persistence
# -------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"seen_items": {}}  # { source_key: set([...]) }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "seen_items" not in data:
                data["seen_items"] = {}
            return data
    except Exception:
        logging.exception("Failed to load state; creating new.")
        return {"seen_items": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def normalize_project_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""

# -------------------------
# Notifications (SMS → Email → Telegram fallback)
# -------------------------
import smtplib
from email.mime.text import MIMEText
try:
    from twilio.rest import Client as TwilioClient  # optional
except Exception:
    TwilioClient = None

# Targets (defaults from your message; you can override in .env)
SMS_PHONE = os.getenv("SMS_PHONE", "+33615878230")
EMAIL_TO = os.getenv("EMAIL_TO", "vinted.hsp@gmail.com")
ONE_SHOT = os.getenv("ONE_SHOT", "false").lower() == "true"


# Twilio config (optional for SMS)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")

# SMTP config (optional for email)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "")

PREFERRED_DEST = [s.strip() for s in os.getenv("PREFERRED_DEST", "email").split(",") if s.strip()]


def send_sms(message: str) -> bool:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM and SMS_PHONE):
        return False
    if TwilioClient is None:
        return False
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=SMS_PHONE)
        return True
    except Exception:
        logging.exception("SMS send failed")
        return False


def send_email(subject: str, message: str) -> bool:
    if not (SMTP_HOST and SMTP_FROM and EMAIL_TO):
        return False
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [EMAIL_TO], msg.as_string())
        return True
    except Exception:
        logging.exception("Email send failed")
        return False


def tg_send(message: str, preview: bool = True) -> bool:
    if not (BOT_TOKEN and CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": not preview,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        return True
    except Exception:
        logging.exception("Telegram send failed")
        return False


def notify(message_text: str) -> None:
    """Queue mode: we don't send per-item anymore."""
    logging.info("Queued signal (digest mode).")


# --- test email optionnel ---
if os.getenv("EMAIL_TEST", "0") == "1":
    notify("Test email VC bot ✅")
    raise SystemExit

# -------------------------
# Fetchers — VC portfolio pages
# -------------------------
"""
Each fetcher returns a list of tuples: [(project_name, project_url)]
The parsing is intentionally lenient — it looks for portfolio-style grids and collects anchor titles/names.
You can refine CSS selectors per site if needed.
"""

@dataclass
class Source:
    key: str
    name: str
    url: str
    parser: Callable[[str, str], List[Tuple[str, str]]]


def generic_portfolio_parser(base_url: str, html_text: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    items: List[Tuple[str, str]] = []

    # Heuristics: look for anchors inside cards/grids
    for a in soup.select("a"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if not href or not text:
            continue
        # Skip internal anchors
        if href.startswith("#"):
            continue
        # Filter noise
        if len(text) > 60 or len(text) < 2:
            continue
        # Very common words to ignore
        if text.lower() in {"learn more", "portfolio", "read more", "apply", "about", "careers", "contact", "news"}:
            continue
        full = urljoin(base_url, href)
        # Avoid own-domain navigational links
        if domain_of(full) == domain_of(base_url):
            # Keep only if looks like a project subpage
            if not re.search(r"/portfolio/|/companies/|/projects/|/investment/|/company/", full, flags=re.I):
                continue
        items.append((normalize_project_name(text), full))

    # Deduplicate by name
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for name, link in items:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((name, link))
    return out


def fetch_source_list(source: Source) -> List[Tuple[str, str]]:
    import importlib, random, time as _t

    try:
        # rotate a couple of UAs lightly
        uas = [
            HEADERS["User-Agent"],
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        ]
        hdr = dict(HEADERS)
        hdr["User-Agent"] = random.choice(uas)

        # requests session (cloudscraper if installed)
        sess = None
        if importlib.util.find_spec("cloudscraper"):
            import cloudscraper
            sess = cloudscraper.create_scraper(browser={"custom": "chrome"})
        else:
            sess = requests

        # simple retry/backoff
        for i in range(3):
            r = sess.get(source.url, headers=hdr, timeout=30)
            if r.status_code in (403, 429) and i < 2:
                _t.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return source.parser(source.url, r.text)

        return []
    except Exception:
        logging.warning("Fetch failed (%s)", source.name)
        return []

# Preconfigured VC sources (add more as needed)
VC_SOURCES: List[Source] = [
    # --- Exchanges / VCs that worked for you ---
    Source(
        key="binance_labs",
        name="Binance Labs Portfolio",
        url="https://labs.binance.com/portfolio",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="a16z_crypto",
        name="a16z Crypto Portfolio",
        url="https://a16z.com/portfolio/",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="wintermute",
        name="Wintermute Investments",
        url="https://www.wintermute.com/portfolio/",
        parser=generic_portfolio_parser,
    ),

    # --- Disable noisy/blocked ones for now ---
    # Source(
    #     key="coinbase_ventures",
    #     name="Coinbase Ventures Portfolio",
    #     url="https://www.coinbase.com/ventures/portfolio",
    #     parser=generic_portfolio_parser,
    # ),
    # Source(
    #     key="chainbroker_recent",
    #     name="ChainBroker Recently Added",
    #     url="https://chainbroker.io/projects/recently-added/",
    #     parser=generic_portfolio_parser,
    # ),

    # --- Add more VCs / funds (scrape-friendly) ---
    Source(
        key="pantera",
        name="Pantera Capital Portfolio",
        url="https://panteracapital.com/portfolio/",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="multicoin",
        name="Multicoin Capital Portfolio",
        url="https://multicoin.capital/portfolio/",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="polychain",
        name="Polychain Capital Portfolio",
        url="https://polychain.capital/portfolio/",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="paradigm",
        name="Paradigm Portfolio",
        url="https://www.paradigm.xyz/companies",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="dragonfly",
        name="Dragonfly Portfolio",
        url="https://www.dragonfly.xyz/portfolio",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="jump_crypto",
        name="Jump Crypto Portfolio",
        url="https://www.jumpcrypto.com/portfolio",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="electric_capital",
        name="Electric Capital Portfolio",
        url="https://www.electriccapital.com/portfolio",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="hashed",
        name="Hashed Portfolio",
        url="https://www.hashed.com/portfolio",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="framework",
        name="Framework Ventures Portfolio",
        url="https://framework.ventures/portfolio/",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="animoca",
        name="Animoca Brands Investments",
        url="https://www.animocabrands.com/investment-portfolio",
        parser=generic_portfolio_parser,
    ),
    Source(
        key="okx_ventures",
        name="OKX Ventures Portfolio",
        url="https://www.okx.com/ventures/portfolio",
        parser=generic_portfolio_parser,
    ),
]


# -------------------------
# Optional: Listings / new coins / unlocks
# -------------------------

def coingecko_new_coins() -> List[dict]:
    if not COINGECKO_NEW_COIN_CHECK:
        return []
    url = "https://api.coingecko.com/api/v3/coins/list?include_platform=false"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        # This returns ALL coins; in practice you’d want /coins/markets with order=new or use CoinMarketCap new listings.
        # We keep it simple and compare against a cached snapshot.
        return r.json()
    except Exception:
        logging.exception("CoinGecko fetch failed")
        return []

# -------------------------
# Scoring & alerting
# -------------------------
SCORE_WEIGHTS = {
    "vc_hit": 10,
    "multi_vc": 8,
    "has_link": 1,
    "coingecko_presence": 2,
}

# For broad discovery (includes aggregators like ChainBroker), a lower threshold is fine.
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "8"))

@dataclass
class Signal:
    name: str
    url: Optional[str]
    tags: List[str]
    score: int


def score_project(name: str, url: Optional[str], tags: List[str], has_cg: bool) -> int:
    score = 0
    if tags:
        score += SCORE_WEIGHTS["vc_hit"]
        if len(tags) >= 2:
            score += SCORE_WEIGHTS["multi_vc"]
    if url:
        score += SCORE_WEIGHTS["has_link"]
    if has_cg:
        score += SCORE_WEIGHTS["coingecko_presence"]
    return score


def build_alert_message(sig: Signal) -> str:
    title = html.escape(sig.name)
    link = f" — <a href=\"{html.escape(sig.url)}\">link</a>" if sig.url else ""
    tags = ", ".join(sig.tags)
    return (
        f"<b>VC-backed project signal</b>\n"
        f"<b>{title}</b>{link}\n"
        f"Sources: {html.escape(tags)}\n"
        f"Score: <b>{sig.score}</b> (threshold {SCORE_THRESHOLD})\n"
        f"Next step: quick DD — tokenomics, unlocks, roadmap, traction."
    )

from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None

def now_local() -> datetime:
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            pass
    return datetime.now()  # fallback

def state_get_list(state: dict, key: str) -> list:
    lst = state.get(key)
    if not isinstance(lst, list):
        lst = []
        state[key] = lst
    return lst

def queue_signal(state: dict, sig: dict) -> None:
    """De-dupe by name (lower) within the current digest window."""
    pending = state_get_list(state, "pending_signals")
    key = sig.get("name","").lower().strip()
    # if already exists, merge sources/tags and max score
    for s in pending:
        if s.get("name","").lower().strip() == key:
            # merge tags, keep first non-empty url, max score
            s["tags"] = sorted(set(s.get("tags",[]) + sig.get("tags",[])))
            if not s.get("url") and sig.get("url"):
                s["url"] = sig["url"]
            s["score"] = max(s.get("score",0), sig.get("score",0))
            return
    sig["ts"] = now_local().isoformat(timespec="seconds")
    pending.append(sig)

def should_send_digest(state: dict) -> bool:
    pending = state_get_list(state, "pending_signals")
    if not pending:
        return False
    last = state.get("last_digest_sent")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    return (now_local() - last_dt) >= timedelta(hours=DIGEST_INTERVAL_HOURS)

def render_digest_html(pending: list) -> str:
    # sort by score desc then name
    items = sorted(pending, key=lambda x: (-int(x.get("score",0)), x.get("name","").lower()))
    # simple badges
    def badge(txt): return f'<span style="display:inline-block;padding:2px 8px;border-radius:12px;background:#eef;border:1px solid #ccd;font-size:12px;margin-right:6px">{txt}</span>'
    rows = []
    for it in items:
        name = html.escape(it.get("name",""))
        url = it.get("url")
        link = f'<a href="{html.escape(url)}" style="text-decoration:none;color:#2563eb">{name}</a>' if url else name
        tags = ", ".join(html.escape(t) for t in it.get("tags",[]))
        score = int(it.get("score",0))
        rows.append(f"""
        <tr>
          <td style="padding:12px 10px;border-bottom:1px solid #eee">
            <div style="font-weight:600;font-size:16px;margin-bottom:4px">{link}</div>
            <div style="margin:6px 0">{badge(f"Score {score}")}{badge(f"Sources: {len(it.get('tags',[]))}")}</div>
            <div style="color:#6b7280;font-size:13px">Sources: {html.escape(tags)}</div>
          </td>
        </tr>
        """)
    body = "".join(rows) or "<tr><td style='padding:16px'>No new items in this window.</td></tr>"
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    return f"""
    <div style="font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f8fafc;padding:24px">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:780px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden">
        <tr>
          <td style="background:#0f172a;color:#fff;padding:18px 20px">
            <div style="font-size:18px;font-weight:700">VC Signals — Recap</div>
            <div style="opacity:.85;font-size:12px">{ts} ({html.escape(TIMEZONE)}) • window: {DIGEST_INTERVAL_HOURS}h</div>
          </td>
        </tr>
        {body}
      </table>
      <div style="max-width:780px;margin:12px auto 0;color:#6b7280;font-size:12px">
        Treat as research starters (not buy signals).
      </div>
    </div>
    """

def send_digest_if_due(state: dict) -> None:
    if not should_send_digest(state):
        return
    pending = state_get_list(state, "pending_signals")
    html_body = render_digest_html(pending)
    # fallback plaintext if needed
    plain = "VC Signals — Recap\n\n" + "\n".join(
        f"- {i.get('name')}  (sources: {', '.join(i.get('tags',[]))})" for i in pending
    )
    # use email HTML send
    ok = send_email_html(DIGEST_SUBJECT, html_body, plain)
    if ok:
        state["last_digest_sent"] = now_local().isoformat(timespec="seconds")
        state["pending_signals"] = []
        logging.info("Digest sent with %d items.", len(pending))
    else:
        logging.warning("Digest send failed; keeping items queued.")

def send_email_html(subject: str, html_body: str, plain_fallback: str = "") -> bool:
    if not (SMTP_HOST and SMTP_FROM and EMAIL_TO):
        return False
    try:
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = EMAIL_TO
        if plain_fallback:
            msg.attach(MIMEText(plain_fallback, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [EMAIL_TO], msg.as_string())
        return True
    except Exception:
        logging.exception("Email (HTML) send failed")
        return False


# -------------------------
# Main loop
# -------------------------

def main() -> None:
    state = load_state()
    seen = state.setdefault("seen_items", {})  # type: ignore

    # 1) Pull VC portfolios
    # name_lower -> {"display": original_casing, "sources": {source_name: url}}
    vc_hits: Dict[str, Dict[str, Dict[str, str]]] = {}

    for src in VC_SOURCES:
        projects = fetch_source_list(src)
        logging.info("Fetched %d items from %s", len(projects), src.name)
        for name, link in projects:
            key = name.lower()
            entry = vc_hits.setdefault(key, {"display": name, "sources": {}})
            entry["display"] = name  # keep latest casing
            entry["sources"][src.name] = link

    # 2) Optional: lightweight presence check (CoinGecko)
    cg_names: Set[str] = set()
    if COINGECKO_NEW_COIN_CHECK:
        coins = coingecko_new_coins()
        for c in coins:
            nm = c.get("name") or ""
            if nm:
                cg_names.add(nm.lower())
        logging.info("CoinGecko list size: %d", len(cg_names))

        # 3) Compute signals and alert on NEW ones only
    for name_lc, entry in vc_hits.items():
        source_map = entry["sources"]
        display_name = entry["display"]
        first_link = next(iter(source_map.values())) if source_map else None
        tags = list(source_map.keys())

        # --- REQUIRE MULTI VC ---
        if REQUIRE_MULTI_VC and len(tags) < 2:
            continue
        # ------------------------

        has_cg = name_lc in cg_names
        score = score_project(display_name, first_link, tags, has_cg)

        if score >= SCORE_THRESHOLD:
            bucket = f"{name_lc}:{score}"
            seen_src = seen.setdefault("vc_signals", [])
            if bucket not in seen_src:
                seen_src.append(bucket)
                # queue for digest
                queue_signal(state, {
                    "name": display_name,
                    "url": first_link,
                    "tags": tags,
                    "score": score,
                })

    # try to send the 4h digest if due
    send_digest_if_due(state)

    save_state(state)



if __name__ == "__main__":
    if "telegram" in PREFERRED_DEST and (not BOT_TOKEN or not CHAT_ID):
        logging.warning("Telegram is not configured (BOT_TOKEN/CHAT_ID missing). Running in dry-run mode.")

    if ONE_SHOT:
        main()
    else:
        try:
            while True:
                main()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("Exiting...")


# requirements.txt (reference)
# requests
# beautifulsoup4
# python-dotenv
# twilio  # only if you want SMS

