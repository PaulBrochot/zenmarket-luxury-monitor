""" monitor.py — Buyee Mercari → ZenMarket Luxury Monitor
Source : buyee.jp/mercari/search (achat immédiat uniquement)
Alertes : Discord embeds par salon selon la catégorie de l'article
"""
import os
import re
import time
import random
import logging
import datetime
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from db import init_db, is_seen, mark_seen
from db import init_db, is_seen, mark_seen, get_price, update_price

load_dotenv()
RATE_API_BASE = os.getenv("RATE_API_BASE", "https://api.frankfurter.app")
DB_PATH     = os.getenv("DB_PATH", "data/seen_items.db")
CHECK_INTERVAL = (int(os.getenv("CHECK_MIN", "120")),
                   int(os.getenv("CHECK_MAX", "300")))
MAX_PAGES   = int(os.getenv("MAX_PAGES", "3"))



# ────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("buyee-mercari-monitor")



# ────────────────────────────────────────────
# USER AGENTS
# ────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Safari/605.1.15",
]



# ────────────────────────────────────────────
# BRAND MAPPING + COLORS
# ────────────────────────────────────────────
BRAND_MAPPING = {
    "Louis Vuitton": "ルイ・ヴィトン",
    "Prada": "プラダ",
    "Celine": "セリーヌ",
    "Gucci": "グッチ",
    "Hermès": "エルメス",
}


BRAND_COLORS = {
    "Louis Vuitton": 0xA0522D,
    "Prada":       0x2C2C2C,
    "Celine":      0x4A90E2,
    "Gucci":       0xC41E3A,
    "Hermès":      0xE87D3E,
}

# ────────────────────────────────────────────
# SEUILS DE PRIX MAX EN EUR
# ────────────────────────────────────────────
PRICE_MAX_EUR = {
    "WALLET":   60,
    "POCHETTE": 160,
    "SAC":      500,
}

# ────────────────────────────────────────────
# KEYWORDS
# ────────────────────────────────────────────
KEYWORDS_JP = [
    "バッグ", "ハンドバッグ", "ショルダー", "ポシェット",
    "トートバッグ", "クラッチ", "財布", "レザー", "本革",
]
KEYWORDS_EN = [
    "bag", "pochette", "tote", "clutch", "leather", "purse",
    "handbag", "wallet", "speedy", "neverfull", "alma",
    "keepall", "vuitton", "louis", "lv",
]
EXCLUDE_KEYWORDS = [
    "空き箱", "ショップ袋", "紙袋", "保存袋", "ダストバッグ",
    "保存箱", "dustbag", "dust bag", "empty box", "shopping bag only",
]



# ────────────────────────────────────────────
# SESSION
# ────────────────────────────────────────────
_session = requests.Session()
_session_init = False
_ua = random.choice(USER_AGENTS)



def init_session() -> None:
    """Initialize session with Buyee cookies"""
    global _session_init
    if _session_init:
        return

    headers = { "User-Agent": random.choice(USER_AGENTS) }
    log.info("Init session — fetching cookies Buyee")

    _session.get("https://buyee.jp", headers=headers, timeout=20)
    _session.get(
        "https://buyee.jp/mercari/search?keyword=%E3%83%90%E3%83%83%E3%82%B0",
        headers=headers, timeout=20
    )

    ua = random.choice(USER_AGENTS)
    headers = { "User-Agent": ua }
    _session.headers.update(headers)

    log.info(f"Cookies: {list(_session.cookies.keys())}")
    _session_init = True



# ────────────────────────────────────────────
# SEARCH URLS
# ────────────────────────────────────────────
def build_search_urls() -> list[tuple[str, str]]:
    """Build search URLs for each brand + primary keywords"""
    urls = []
    primary_kw = [
    "バッグ", "ハンドバッグ", "ショルダーバッグ", "ポシェット",
    "財布",        # ← WALLET (MANQUANT !)
    "長財布",      # ← long wallet
    "折り財布",    # ← compact wallet
    ]
    for brand_en, brand_jp in BRAND_MAPPING.items():
        for kw in primary_kw:
            keyword = requests.utils.quote(f"{brand_jp} {kw}")
            url = (
                "https://buyee.jp/mercari/search"
                f"?keyword={keyword}&sort=created_time&order=desc"
            )
            urls.append((brand_en, url))
    return urls



# ────────────────────────────────────────────
# FETCH
# ────────────────────────────────────────────
def fetch_page(url: str) -> str | None:
    """Fetch page HTML with session & rotated UA"""
    init_session()

    _ua = random.choice(USER_AGENTS)
    _session.headers["User-Agent"] = _ua

    for attempt in range(3):
        try:
            resp = _session.get(url, timeout=25)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding
                html = resp.text
                log.info(f"HTML récupéré: {len(html)} caractères")
                return html
            log.warning(f"HTTP {resp.status_code} — attempt {attempt + 1}/3")
            time.sleep(3 + attempt * 5)
        except Exception as e:
            log.warning(f"Erreur réseau: {e} — attempt {attempt + 1}/3")
            time.sleep(3 + attempt * 5)

    return None



# ────────────────────────────────────────────
# PARSE LISTINGS
# ────────────────────────────────────────────
def parse_listings(html: str, brand: str) -> list[dict]:
    """Parse Mercari listings, filter excludes, extract item data"""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen_ids = set()

    a_tags = soup.select(
        "li a[href*='/mercari/item/'], "
        "li a[href*='/mercari/shop/']"
    )
    log.info(f"Found {len(a_tags)} item links")

    for a in a_tags:
        href = a.get("href", "")
        if not (href.startswith("/mercari/item/") or href.startswith("/mercari/shop/")):
            continue
        if not href.endswith((".html", "Mercari_DirectSearch")):  # Support les 2 formats d'URL
            continue

        # Extraction ID
        if "?" in href:
            item_id = href.split("/")[-1].split("?")[0]
        else:
            item_id = href.split("/")[-1].replace(".html", "")
            
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        # Image
        img_tag = a.find("img")
        image_url = ""
        if img_tag:
            src = (img_tag.get("data-bind", "") or
                   img_tag.get("data-src", "") or
                   img_tag.get("src", "") or
                   "")
            # Extraction de l'URL depuis data-bind si présent
            if "imagePath:" in src:
                match = re.search(r"imagePath:\s*'([^']+)'", src)
                if match:
                    image_url = "https:" + match.group(1).replace("@jpg", "")
            elif src and not src.startswith("data:"):
                image_url = src

        # Titre
        title_tag = a.find(["p", "h2"], class_=re.compile(r"name|txt"))
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Prix - CORRIGÉ
        price_tag = a.find(["span", "p"], class_=re.compile(r"price"))
        price_text = price_tag.get_text(strip=True) if price_tag else ""
        price_jpy = 0
        m = re.search(r"(\d{1,3}(?:,\d{3})*)", price_text)
        if m:
            price_jpy = int(m.group(1).replace(",", ""))

        if price_jpy == 0 or price_jpy < 100:
            continue

        if any(ex in title.lower() for ex in EXCLUDE_KEYWORDS):
            continue

        title_clean = "".join(title.split())
        if len(title_clean) < 3:
            continue

        item = {
            "id": item_id,
            "title": title_clean,
            "price_jpy": price_jpy,
            "brand": brand,
            "image_url": image_url,
            "url": item_to_zenmarket_url(item_id),
            "buyee_url": f"https://buyee.jp{href}",
        }
        items.append(item)

    log.info(f"  → {len(items)} annonce(s) pour {brand}")
    return items


def item_to_zenmarket_url(item_id: str) -> str:
    return f"https://zenmarket.jp/en/mercariproduct.aspx?itemCode={item_id}"



# ────────────────────────────────────────────
# EXCHANGE RATE
# ────────────────────────────────────────────
_rate_cache: tuple[float, float] = (0.0, 0.0)


def jpy_to_eur(jpy_amount: int) -> float:
    """Fetch JPY→EUR rate with 1-hour cache"""
    global _rate_cache
    if jpy_amount <= 0:
        return 0.0

    now = time.time()
    if _rate_cache[0] and (now - _rate_cache[0]) < 3600:
        rate = _rate_cache[1]
    else:
        try:
            resp = requests.get(f"{RATE_API_BASE}/latest?from=JPY&to=EUR", timeout=10)
            data = resp.json()
            rate = data.get("rates", {}).get("EUR", 0.0)
            _rate_cache = (now, rate)
        except Exception as e:
            log.warning(f"Erreur lors de la récupération du taux JPY→EUR: {e}")
            rate = 0.0065  # fallback

    return jpy_amount * rate

    # ────────────────────────────────────────────
# DISCORD WEBHOOK
# ────────────────────────────────────────────
# ────────────────────────────────────────────
# DISCORD WEBHOOK ROUTING
# ────────────────────────────────────────────
def get_webhook_for_item(item: dict) -> tuple[str | None, str]:
    """Détermine le webhook Discord selon la marque et le type d'article"""
    brand = item["brand"]
    title_lower = item["title"].lower()

    if any(kw in title_lower for kw in [
        "財布", "ウォレット", "小銭入れ", "カードケース",
        "カードホルダー", "キーケース", "コインケース",
        "長財布", "折り財布", "二つ折り", "三つ折り",
        "wallet", "portefeuille", "porte-monnaie",
        "porte-carte", "card holder", "card case", "key case",
    ]):
        item_type = "WALLET"
        item_type_fr = "Portefeuille"
    elif any(kw in title_lower for kw in [
        "ポシェット", "pochette", "ポーチ", "pouch", "クラッチ", "clutch",
    ]):
        item_type = "POCHETTE"
        item_type_fr = "Pochette"
    elif any(kw in title_lower for kw in [
        "バッグ", "bag", "sac", "トート", "tote",
        "ショルダー", "shoulder", "ハンドバッグ", "handbag",
    ]):
        item_type = "SAC"
        item_type_fr = "Sac"
    else:
        item_type = "SAC"
        item_type_fr = "Accessoire"

    webhook_map = {
        ("Louis Vuitton", "WALLET"):   os.getenv("WEBHOOK_LV_WALLET"),
        ("Louis Vuitton", "POCHETTE"): os.getenv("WEBHOOK_LV_POCHETTE"),
        ("Louis Vuitton", "SAC"):      os.getenv("WEBHOOK_LV_SAC"),
        ("Gucci", "WALLET"):           os.getenv("WEBHOOK_GUCCI_WALLET"),
        ("Gucci", "POCHETTE"):         os.getenv("WEBHOOK_GUCCI_POCHETTE"),
        ("Gucci", "SAC"):              os.getenv("WEBHOOK_GUCCI_SAC"),
        ("Prada", "WALLET"):           os.getenv("WEBHOOK_PRADA_WALLET"),
        ("Prada", "POCHETTE"):         os.getenv("WEBHOOK_PRADA_POCHETTE"),
        ("Prada", "SAC"):              os.getenv("WEBHOOK_PRADA_SAC"),
        ("Celine", "WALLET"):          os.getenv("WEBHOOK_CELINE_WALLET"),
        ("Celine", "POCHETTE"):        os.getenv("WEBHOOK_CELINE_POCHETTE"),
        ("Celine", "SAC"):             os.getenv("WEBHOOK_CELINE_SAC"),
        ("Hermès", "WALLET"):          os.getenv("WEBHOOK_HERMES_WALLET"),
        ("Hermès", "POCHETTE"):        os.getenv("WEBHOOK_HERMES_POCHETTE"),
        ("Hermès", "SAC"):             os.getenv("WEBHOOK_HERMES_SAC"),
    }

    webhook = webhook_map.get((brand, item_type))
    if webhook:
        log.info(f"  📤 Routage: {brand} → {item_type}")
    return webhook, item_type_fr


def send_discord_alert(item: dict) -> None:
    """Send Discord embed for new item (French compact format)"""
    webhook_url, item_type_fr = get_webhook_for_item(item)
    if not webhook_url:
        log.warning(f"Aucun webhook trouvé pour {item['brand']} — alerte ignorée")
        return

    # ── Filtre prix ──
    price_eur = jpy_to_eur(item["price_jpy"])
    item_type = next(
        (k for k, v in {"WALLET": "Portefeuille", "POCHETTE": "Pochette", "SAC": "Sac"}.items()
         if v == item_type_fr), "SAC"
    )
    max_eur = PRICE_MAX_EUR.get(item_type, 9999)
    if price_eur > max_eur:
        log.info(f"  💸 Ignoré (€{price_eur:.2f} > seuil €{max_eur}) — {item['title'][:40]}")
        return

    # ── Envoi Discord ──
    brand = item["brand"]
    color = BRAND_COLORS.get(brand, 0x5865F2)

    description = (
        f"📦 **Statut**\n"
        f"🔵 NOUVELLE ANNONCE\n\n"
        f"💴 **Prix en yen**\n"
        f"¥{item['price_jpy']:,}\n\n"
        f"💶 **Prix en euros**\n"
        f"~€{price_eur:.2f}\n\n"
        f"🕐 **Détecté le**\n"
        f"{datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')}\n\n"
        f"🔗 **[Voir l'annonce]({item['url']})**"
    )

    embed = {
        "author": {"name": f"{brand} — Nouvelle annonce"},
        "title": f"{item_type_fr} {brand[:2]}",
        "description": description,
        "color": color,
        "image": {"url": item["image_url"]} if item["image_url"] else None,
        "footer": {
            "text": f"ZenmarketBot • {item['id']} • Aujourd'hui à {datetime.datetime.now().strftime('%H:%M')}"
        },
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }

    payload = {"username": "ZenmarketBot", "embeds": [embed]}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"✅ Alerte Discord envoyée pour {item['id']}")
        else:
            log.warning(f"Erreur Discord {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"Erreur envoi Discord: {e}")

def send_price_drop_alert(item: dict, old_price: int) -> None:
    """Send Discord embed for price drop"""
    webhook_url, item_type_fr = get_webhook_for_item(item)
    if not webhook_url:
        return

    # ── Filtre prix ──
    price_eur = jpy_to_eur(item["price_jpy"])
    item_type = next(
        (k for k, v in {"WALLET": "Portefeuille", "POCHETTE": "Pochette", "SAC": "Sac"}.items()
         if v == item_type_fr), "SAC"
    )
    max_eur = PRICE_MAX_EUR.get(item_type, 9999)
    if price_eur > max_eur:
        log.info(f"  💸 Baisse ignorée (€{price_eur:.2f} > seuil €{max_eur})")
        return

    brand = item["brand"]
    color = 0x00FF00  # Vert pour baisse de prix
    drop_pct = round((old_price - item["price_jpy"]) / old_price * 100, 1)

    description = (
        f"📦 **Statut**\n"
        f"📉 BAISSE (¥{old_price:,} → ¥{item['price_jpy']:,})\n\n"
        f"💶 **Prix en euros**\n"
        f"~€{price_eur:.2f}\n\n"
        f"📊 **Réduction**\n"
        f"-{drop_pct}%\n\n"
        f"🕐 **Détecté le**\n"
        f"{datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')}\n\n"
        f"🔗 **[Voir l'annonce]({item['url']})**"
    )

    embed = {
        "author": {"name": f"{brand} — Baisse de prix 📉"},
        "title": f"{item_type_fr} {brand[:2]}",
        "description": description,
        "color": color,
        "image": {"url": item["image_url"]} if item["image_url"] else None,
        "footer": {
            "text": f"ZenmarketBot • {item['id']} • Aujourd'hui à {datetime.datetime.now().strftime('%H:%M')}"
        },
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }

    payload = {"username": "ZenmarketBot", "embeds": [embed]}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"✅ Alerte baisse de prix envoyée pour {item['id']}")
        else:
            log.warning(f"Erreur Discord {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"Erreur envoi Discord: {e}")

# ────────────────────────────────────────────
# MAIN LOOP
# ────────────────────────────────────────────
def main():
    """Main monitoring loop"""
    conn = init_db(DB_PATH)
    log.info("🚀 Démarrage du bot Buyee→ZenMarket Monitor")
    
    urls = build_search_urls()
    log.info(f"📌 {len(urls)} URL(s) de recherche générées")

    while True:
        log.info("─" * 60)
        log.info("🔍 Nouvelle vérification...")
        
        new_count = 0
        drop_count = 0
        
        for brand, url in urls:
            log.info(f"  Scraping {brand}...")
            html = fetch_page(url)
            if not html:
                log.warning(f"  ⚠️  Impossible de récupérer {brand}")
                continue
            
            items = parse_listings(html, brand)
            for item in items:
                if not is_seen(conn, item["id"]):
                    # Nouvelle annonce
                    send_discord_alert(item)
                    mark_seen(conn, item["id"], item["title"], item["price_jpy"], item["brand"])
                    new_count += 1
                else:
                    # Annonce déjà vue → vérifie baisse de prix
                    old_price = get_price(conn, item["id"])
                    if old_price and item["price_jpy"] < old_price:
                        log.info(f"📉 Baisse de prix: {item['id']} ¥{old_price:,} → ¥{item['price_jpy']:,}")
                        send_price_drop_alert(item, old_price)
                        update_price(conn, item["id"], item["price_jpy"])
                        drop_count += 1
            
            time.sleep(random.uniform(2, 5))
        
        log.info(f"✅ Cycle terminé — {new_count} nouvelle(s) annonce(s), {drop_count} baisse(s) de prix")
        
        wait = random.randint(CHECK_INTERVAL[0], CHECK_INTERVAL[1])
        log.info(f"💤 Attente de {wait}s avant prochain cycle")
        time.sleep(wait)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\n👋 Arrêt du bot (Ctrl+C)")
    except Exception as e:
        log.error(f"❌ Erreur fatale: {e}", exc_info=True)