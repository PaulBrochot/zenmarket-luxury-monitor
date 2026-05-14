"""
monitor.py — Yahoo Auctions Japan → ZenMarket Luxury Monitor

Source de données : auctions.yahoo.co.jp (HTML statique, requests suffisant)
Alertes Discord  : lien ZenMarket pour enchérir directement

Fonctionnalités :
  - Recherche en japonais sur Yahoo Auctions Japan
  - Rotation User-Agent + jitter anti-ban
  - Conversion JPY→EUR via Frankfurter (gratuit, sans clé)
  - Déduplication via SQLite
  - Embed Discord avec titre, prix JPY, prix EUR, marque, image, lien ZenMarket
"""

import os
import re
import time
import random
import logging
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from db import init_db, is_seen, mark_seen

# ─── Environnement ────────────────────────────────────────────────────────────────
load_dotenv()

WEBHOOK_URL: str  = os.getenv("DISCORD_WEBHOOK_URL", "")
RATE_API_BASE     = os.getenv("RATE_API_BASE", "https://api.frankfurter.app")
DB_PATH           = os.getenv("DB_PATH", "data/seen_items.db")
CHECK_INTERVAL   = (int(os.getenv("CHECK_MIN", "120")), int(os.getenv("CHECK_MAX", "300")))

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("yahoo-monitor")

# ─── User-Agents ───────────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

# ─── Marques ↔ Japonais ──────────────────────────────────────────────────────────
BRAND_MAPPING = {
    "Louis Vuitton": "ルイ・ヴィトン",
    "Prada":         "プラダ",
    "Celine":        "セリーヌ",
    "Gucci":         "グッチ",
    "Hermès":        "エルメス",
}

# ─── Mots-clés maroquinerie ────────────────────────────────────────────────────
KEYWORDS_JP = [
    "バッグ", "ハンドバッグ", "ショルダー", "ポシェット",
    "トートバッグ", "クラッチ", "財布", "レザー", "本革",
]
KEYWORDS_EN = [
    "bag", "pochette", "tote", "clutch", "leather", "purse", "handbag", "wallet",
]

# ─── URLs de recherche Yahoo Auctions Japan ───────────────────────────────────────
YAHOO_SEARCH = "https://auctions.yahoo.co.jp/search/search"

def build_search_urls() -> list[tuple[str, str]]:
    """
    Génère les URLs de recherche Yahoo Auctions Japan.
    Paramètres : p=mot-clé, ei=utf-8, auccat=0 (toutes catégories),
    s1=new (tri par nouveauté pour capturer les nouvelles annonces en premier)
    """
    urls = []
    primary_kw = ["バッグ", "ハンドバッグ", "ショルダーバッグ", "ポシェット"]
    for brand_en, brand_jp in BRAND_MAPPING.items():
        for kw in primary_kw:
            query = f"{brand_jp} {kw}"
            params = {
                "p":      query,
                "ei":     "utf-8",
                "auccat": "0",
                "s1":     "new",   # Tri par date (plus récent en premier)
                "o1":     "d",
            }
            param_str = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
            url = f"{YAHOO_SEARCH}?{param_str}"
            urls.append((brand_en, url))
    return urls


def yahoo_item_to_zenmarket_url(item_id: str) -> str:
    """Convertit un ID Yahoo Auctions en lien ZenMarket pour enchérir."""
    return f"https://zenmarket.jp/en/auction.aspx?itemCode={item_id}"


# ─── HTTP fetch simple ─────────────────────────────────────────────────────────────
def fetch_page(url: str) -> str | None:
    """
    Yahoo Auctions Japan sert du HTML statique — requests suffit.
    Rotation User-Agent + headers japonais pour maximiser les résultats.
    """
    headers = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept-Language": "ja,en-US;q=0.8",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://auctions.yahoo.co.jp/",
        "DNT":             "1",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "utf-8"
        if resp.status_code == 200:
            log.info(f"HTML récupéré : {len(resp.text)} caractères")
            return resp.text
        log.warning(f"HTTP {resp.status_code} pour {url}")
        return None
    except Exception as e:
        log.error(f"Erreur fetch : {e}")
        return None


# ─── Parsing Yahoo Auctions Japan ──────────────────────────────────────────────────
def parse_listings(html: str, brand: str) -> list[dict]:
    """
    Parse les résultats de recherche Yahoo Auctions Japan.

    Structure HTML Yahoo Auctions (stable depuis des années) :
      <li class="Product">                          ← carte article
        <a class="Product__imageLink" href="...">   ← lien + ID
          <img class="Product__image" src="...">    ← image
        </a>
        <h3 class="Product__title">...</h3>         ← titre
        <span class="Product__price">               ← prix actuel
          <span class="Product__priceValue">12,345</span>
        </span>
      </li>
    """
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Sélecteur principal Yahoo Auctions
    cards = soup.select("li.Product")
    if not cards:
        # Fallback si structure différente
        cards = soup.select("li[class*='Product'], div[class*='Product'], .SearchResult li")
    if not cards:
        cards = soup.select("li")

    log.debug(f"{len(cards)} cartes brutes trouvées")

    for card in cards:
        try:
            # ── Titre
            title_tag = (
                card.select_one(".Product__title") or
                card.select_one("h3") or
                card.select_one("[class*='title']") or
                card.select_one("a")
            )
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if len(title) < 8:
                continue

            # ── Filtre maroquinerie
            title_lower = title.lower()
            if not (any(kw in title for kw in KEYWORDS_JP) or
                    any(kw in title_lower for kw in KEYWORDS_EN)):
                continue

            # ── Lien Yahoo + extraction ID
            link_tag = card.select_one("a[href]")
            yahoo_url = ""
            item_id = ""
            if link_tag:
                yahoo_url = str(link_tag.get("href", ""))
                # ID Yahoo format : lettre + chiffres ex: v1234567890
                m = re.search(r"/([a-z]\d{9,})", yahoo_url)
                if m:
                    item_id = m.group(1)
            if not item_id:
                item_id = card.get("data-auction-id") or card.get("id") or ""
            if not item_id:
                continue

            # ── Lien ZenMarket (pour enchérir)
            zenmarket_url = yahoo_item_to_zenmarket_url(item_id)

            # ── Prix JPY
            price_jpy = 0
            price_tag = (
                card.select_one(".Product__priceValue") or
                card.select_one(".Product__price") or
                card.select_one("[class*='price']") or
                card.select_one("[class*='Price']")
            )
            if price_tag:
                digits = re.sub(r"[^\d]", "", price_tag.get_text())
                price_jpy = int(digits) if digits else 0

            # ── Image
            image_url = ""
            img = card.select_one(".Product__image, img[src]")
            if img:
                src = str(img.get("src") or img.get("data-src", ""))
                # Remplacer taille miniature par image plus grande
                src = re.sub(r"_\d+\.jpg", "_500.jpg", src)  # Yahoo format: _90.jpg → _500.jpg
                src = re.sub(r"Xs\.jpg$", "l.jpg", src)       # Autre format Yahoo
                image_url = src

            items.append({
                "id":        item_id,
                "title":     title,
                "price_jpy": price_jpy,
                "url":       zenmarket_url,
                "yahoo_url": yahoo_url,
                "image_url": image_url,
                "brand":     brand,
            })

        except Exception as e:
            log.debug(f"Erreur parsing card : {e}")
            continue

    log.info(f"  → {len(items)} annonce(s) maroquinerie pour {brand}")
    return items


# ─── Conversion JPY → EUR ─────────────────────────────────────────────────────
_rate_cache: dict = {}

def get_jpy_eur_rate() -> float:
    now = time.time()
    if "r" in _rate_cache and now - _rate_cache["t"] < 3600:
        return _rate_cache["r"]
    try:
        resp = requests.get(f"{RATE_API_BASE}/latest",
                            params={"from": "JPY", "to": "EUR"}, timeout=10)
        rate = resp.json()["rates"]["EUR"]
        _rate_cache.update({"r": rate, "t": now})
        log.info(f"Taux JPY→EUR : {rate:.6f}")
        return rate
    except Exception as e:
        log.error(f"Erreur taux change : {e}")
        return _rate_cache.get("r", 0.0059)

def jpy_to_eur(jpy: int) -> float:
    return round(jpy * get_jpy_eur_rate(), 2)


# ─── Discord Webhook ──────────────────────────────────────────────────────────
BRAND_COLORS = {
    "Louis Vuitton": 0xC49A3A,
    "Prada":         0x1A1A1A,
    "Celine":        0x8B6F47,
    "Gucci":         0x2E7D32,
    "Hermès":        0xE25C00,
}

def send_discord_alert(item: dict) -> bool:
    if not WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL non définie dans .env")
        return False

    price_eur = jpy_to_eur(item["price_jpy"])
    brand     = item.get("brand", "Inconnue")
    color     = BRAND_COLORS.get(brand, 0x7289DA)

    embed = {
        "title":       item["title"][:256],
        "url":         item["url"],
        "color":       color,
        "description": f"🏷️ Nouvelle annonce — **{brand}**\n[🛍️ Enchérir sur ZenMarket]({item['url']})",
        "fields": [
            {"name": "💴 Prix JPY",       "value": f"¥ {item['price_jpy']:,}" if item['price_jpy'] else "N/A", "inline": True},
            {"name": "💶 Prix EUR (est.)", "value": f"€ {price_eur:,.2f}"        if item['price_jpy'] else "N/A", "inline": True},
            {"name": "👜 Marque",          "value": brand,                                                       "inline": True},
        ],
        "footer":    {"text": "Yahoo Auctions Japan • via ZenMarket • JPY/EUR temps réel"},
        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }

    if item.get("image_url"):
        embed["image"]     = {"url": item["image_url"]}
        embed["thumbnail"] = {"url": item["image_url"]}

    payload = {
        "username":   "Luxury Monitor 🛍️",
        "avatar_url": "https://zenmarket.jp/Content/img/header-logo.png",
        "embeds":     [embed],
    }

    try:
        resp = requests.post(WEBHOOK_URL, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"✅ Alerté : {item['title'][:60]}")
            return True
        log.error(f"Discord HTTP {resp.status_code} : {resp.text}")
        return False
    except Exception as e:
        log.error(f"Erreur Discord : {e}")
        return False


# ─── Boucle principale ────────────────────────────────────────────────────────────
def run():
    log.info("🚀 Démarrage — source : Yahoo Auctions Japan → liens ZenMarket")
    conn        = init_db(DB_PATH)
    search_urls = build_search_urls()
    log.info(f"{len(search_urls)} requêtes configurées")

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Cycle #{cycle} {'='*30}")
        new_items = 0

        for brand, url in search_urls:
            log.info(f"Scraping {brand}")
            html = fetch_page(url)
            if not html:
                time.sleep(random.uniform(5, 12))
                continue

            listings = parse_listings(html, brand)

            for item in listings:
                if is_seen(conn, item["id"]):
                    continue
                sent = send_discord_alert(item)
                if sent:
                    mark_seen(conn, item["id"], item["title"], item["price_jpy"], brand)
                    new_items += 1
                time.sleep(random.uniform(1, 3))

            # Jitter entre chaque requête
            time.sleep(random.uniform(4, 10))

        log.info(f"Cycle #{cycle} — {new_items} alerte(s) envoyée(s)")
        wait = random.randint(*CHECK_INTERVAL)
        log.info(f"Prochain cycle dans {wait}s…")
        time.sleep(wait)


if __name__ == "__main__":
    run()
