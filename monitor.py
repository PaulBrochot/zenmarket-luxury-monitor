"""
monitor.py — ZenMarket Luxury Bag Monitor
Surveille les annonces de maroquinerie de luxe sur ZenMarket et envoie
des alertes enrichies sur Discord via Webhook.

Fonctionnalités :
  - Recherche en japonais pour maximiser les résultats Yahoo Auctions
  - Rotation User-Agent + jitter entre cycles (anti-ban)
  - Playwright OBLIGATOIRE (ZenMarket est une SPA JavaScript)
  - Attente intelligente du rendu des cartes d'articles
  - Conversion JPY→EUR via Frankfurter (sans clé, gratuit)
  - Déduplication via SQLite
  - Embed Discord esthétique avec image, prix et lien
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

# ─── Chargement des variables d'environnement ─────────────────────────────────
load_dotenv()

WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
RATE_API_BASE: str = os.getenv("RATE_API_BASE", "https://api.frankfurter.app")
DB_PATH: str = os.getenv("DB_PATH", "data/seen_items.db")
CHECK_INTERVAL: tuple[int, int] = (int(os.getenv("CHECK_MIN", "120")), int(os.getenv("CHECK_MAX", "300")))
MAX_PAGES: int = int(os.getenv("MAX_PAGES", "3"))

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("zenmarket-monitor")

# ─── Rotation User-Agent ──────────────────────────────────────────────────────
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# ─── Correspondance marques ↔ Japonais ──────────────────────────────────────
BRAND_MAPPING: dict[str, str] = {
    "Louis Vuitton": "ルイ・ヴィトン",
    "Prada":         "プラダ",
    "Celine":        "セリーヌ",
    "Gucci":         "グッチ",
    "Hermès":        "エルメス",
}

# ─── Mots-clés maroquinerie ───────────────────────────────────────────────────
KEYWORDS_JP: list[str] = [
    "バッグ", "ハンドバッグ", "ショルダー", "ポシェット",
    "トートバッグ", "クラッチ", "財布", "サック", "レザー", "本革",
]
KEYWORDS_EN: list[str] = [
    "bag", "sac", "bandouliere", "pochette", "tote",
    "clutch", "leather", "purse", "handbag", "wallet",
]

# ─── Construction des URLs ZenMarket ─────────────────────────────────────────
# ZenMarket cross-search : /en/auction.aspx cherche sur Yahoo Auctions Japan
ZENMARKET_SEARCH_URL = "https://zenmarket.jp/en/auction.aspx"

def build_search_urls() -> list[tuple[str, str]]:
    urls = []
    primary_kw = ["バッグ", "ハンドバッグ", "ショルダー", "ポシェット"]
    for brand_en, brand_jp in BRAND_MAPPING.items():
        for kw in primary_kw:
            query = f"{brand_jp} {kw}"
            url = f"{ZENMARKET_SEARCH_URL}?q={requests.utils.quote(query)}&categoryId=0"
            urls.append((brand_en, url))
    return urls


# ─── Fetch via Playwright (OBLIGATOIRE — site SPA JavaScript) ─────────────────
def fetch_page(url: str) -> str | None:
    """
    ZenMarket est une Single Page Application (SPA) :
    le HTML statique ne contient PAS les résultats de recherche.
    Playwright charge la page dans un vrai navigateur et attend
    que les cartes d'articles soient rendues avant de retourner le HTML.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        log.info(f"Playwright → {url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="ja-JP",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    "DNT": "1",
                },
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            # ── Attendre que les cartes d'articles soient présentes dans le DOM
            # ZenMarket utilise des éléments <li> avec class contenant "item"
            # ou des divs Angular/React injectés dynamiquement
            selectors_to_try = [
                ".items-list li",        # Liste principale
                "li[class*='item']",      # Tout <li> avec 'item' dans la classe
                "div[class*='item-card']",
                ".search-results li",
                "[ng-repeat]",            # Angular ng-repeat
                ".product-list li",
            ]
            found = False
            for sel in selectors_to_try:
                try:
                    page.wait_for_selector(sel, timeout=8_000)
                    log.info(f"Sélecteur trouvé : '{sel}'")
                    found = True
                    break
                except PWTimeout:
                    continue

            if not found:
                # Dernier recours : attendre simplement 5s pour le JS
                log.warning("Aucun sélecteur connu trouvé, attente JS générique 5s")
                time.sleep(5)

            # Petit délai humain supplémentaire
            time.sleep(random.uniform(1.5, 3.5))
            html = page.content()
            browser.close()
            log.info(f"HTML récupéré : {len(html)} caractères")
            return html
    except Exception as e:
        log.error(f"Playwright exception : {e}")
        return None


# ─── Parsing des annonces ─────────────────────────────────────────────────────
def parse_listings(html: str, brand: str) -> list[dict]:
    """
    Parse le HTML rendu par Playwright.
    Stratégie multi-sélecteurs pour s'adapter aux changements de structure ZenMarket.
    """
    soup = BeautifulSoup(html, "lxml")
    items = []

    # ── Sélecteurs CSS par ordre de priorité (ZenMarket structure réelle)
    CARD_SELECTORS = [
        "li.item",
        "li[class*='item']",
        "div[class*='item-card']",
        "div[class*='product-card']",
        ".items-list > li",
        ".search-results li",
        "ul.items li",
        # Fallback agressif : tous les <li> avec une image ET un prix
        "li",
    ]

    cards = []
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if len(cards) > 3:  # Seuil : au moins 3 résultats pour valider le sélecteur
            log.debug(f"Sélecteur actif : '{sel}' ({len(cards)} éléments)")
            break

    if not cards:
        log.warning("parse_listings : aucun sélecteur n'a trouvé de cartes")
        return []

    for card in cards:
        try:
            # ── Titre : chercher dans les attributs communs
            title = ""
            for sel in ["[class*='title']", "[class*='name']", "h3", "h4", "h5", "p.title", "a"]:
                tag = card.select_one(sel)
                if tag:
                    t = tag.get_text(strip=True)
                    if len(t) > 5:  # Ignorer les textes trop courts
                        title = t
                        break
            if not title:
                continue

            # ── Filtre maroquinerie
            title_lower = title.lower()
            if not (any(kw in title for kw in KEYWORDS_JP) or
                    any(kw in title_lower for kw in KEYWORDS_EN)):
                continue

            # ── Lien
            link_tag = card.select_one("a[href]")
            item_url = ""
            if link_tag:
                href = str(link_tag.get("href", ""))
                item_url = href if href.startswith("http") else f"https://zenmarket.jp{href}"

            # ── ID depuis URL ou attribut
            item_id = str(card.get("data-id") or card.get("id") or "")
            if not item_id and item_url:
                m = (re.search(r"itemCode=([A-Za-z0-9]+)", item_url) or
                     re.search(r"/([a-z]\d{6,})", item_url) or
                     re.search(r"[?&]q=([^&]+)", item_url))
                item_id = m.group(1) if m else item_url[:80]
            if not item_id:
                continue

            # ── Prix JPY
            price_jpy = 0
            for price_sel in ["[class*='price']", "[class*='bid']", "[class*='current']", "strong", "span"]:
                price_tag = card.select_one(price_sel)
                if price_tag:
                    digits = re.sub(r"[^\d]", "", price_tag.get_text())
                    if digits and int(digits) > 100:  # Sanity check : > ¥100
                        price_jpy = int(digits)
                        break

            # ── Image
            image_url = ""
            img = card.select_one("img[src], img[data-src], img[data-lazy-src]")
            if img:
                src = (str(img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")))
                # Supprimer les suffixes de redimensionnement (_80x80, _200x200, etc.)
                src = re.sub(r"_(\d+x\d+)\.", ".", src)
                image_url = src if src.startswith("http") else f"https://zenmarket.jp{src}"

            items.append({
                "id":        item_id,
                "title":     title,
                "price_jpy": price_jpy,
                "url":       item_url,
                "image_url": image_url,
                "brand":     brand,
            })

        except Exception as e:
            log.debug(f"Erreur parsing card : {e}")
            continue

    log.info(f"  → {len(items)} annonce(s) maroquinerie trouvée(s) pour {brand}")
    return items


# ─── Conversion JPY → EUR ─────────────────────────────────────────────────────
_rate_cache: dict = {}

def get_jpy_eur_rate() -> float:
    cache_key = "JPY_EUR"
    now = time.time()
    if cache_key in _rate_cache:
        rate, fetched_at = _rate_cache[cache_key]
        if now - fetched_at < 3600:
            return rate
    try:
        resp = requests.get(
            f"{RATE_API_BASE}/latest",
            params={"from": "JPY", "to": "EUR"},
            timeout=10,
        )
        data = resp.json()
        rate = data["rates"]["EUR"]
        _rate_cache[cache_key] = (rate, now)
        log.info(f"Taux JPY→EUR actualisé : {rate:.6f}")
        return rate
    except Exception as e:
        log.error(f"Erreur récupération taux change : {e}")
        return _rate_cache.get(cache_key, (0.006, 0))[0]

def jpy_to_eur(jpy: int) -> float:
    return round(jpy * get_jpy_eur_rate(), 2)


# ─── Discord Webhook ──────────────────────────────────────────────────────────
BRAND_COLORS: dict[str, int] = {
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
    brand = item.get("brand", "Inconnue")
    color = BRAND_COLORS.get(brand, 0x7289DA)

    embed: dict = {
        "title":       item["title"][:256],
        "url":         item["url"] or "https://zenmarket.jp",
        "color":       color,
        "description": f"🏷️ Nouvelle annonce sur **ZenMarket** — **{brand}**",
        "fields": [
            {"name": "💴 Prix JPY",        "value": f"¥ {item['price_jpy']:,}" if item['price_jpy'] else "N/A", "inline": True},
            {"name": "💶 Prix EUR (est.)",  "value": f"€ {price_eur:,.2f}" if item['price_jpy'] else "N/A",    "inline": True},
            {"name": "👜 Marque",           "value": brand,                                                      "inline": True},
        ],
        "footer":    {"text": "ZenMarket Luxury Monitor • JPY/EUR en temps réel"},
        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }

    if item.get("image_url"):
        embed["image"]     = {"url": item["image_url"]}
        embed["thumbnail"] = {"url": item["image_url"]}

    payload = {
        "username":   "ZenMarket Monitor 🛍️",
        "avatar_url": "https://zenmarket.jp/Content/img/header-logo.png",
        "embeds":     [embed],
    }

    try:
        resp = requests.post(WEBHOOK_URL, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"✅ Alerte envoyée : {item['title'][:60]}")
            return True
        log.error(f"Webhook Discord → HTTP {resp.status_code} : {resp.text}")
        return False
    except Exception as e:
        log.error(f"Erreur envoi Discord : {e}")
        return False


# ─── Boucle Principale ────────────────────────────────────────────────────────
def run():
    log.info("🚀 Démarrage du ZenMarket Luxury Monitor")
    conn = init_db(DB_PATH)
    search_urls = build_search_urls()
    log.info(f"{len(search_urls)} requêtes de recherche configurées")

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Cycle #{cycle} ──────────────────────────")
        new_items = 0

        for brand, url in search_urls:
            log.info(f"Scraping {brand} → {url}")
            html = fetch_page(url)
            if not html:
                log.warning(f"Aucun HTML récupéré pour {brand}")
                time.sleep(random.uniform(3, 8))
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

            time.sleep(random.uniform(5, 15))

        log.info(f"Cycle #{cycle} terminé — {new_items} nouvelle(s) alerte(s) envoyée(s)")
        wait = random.randint(*CHECK_INTERVAL)
        log.info(f"Prochain cycle dans {wait}s ({wait//60}m {wait%60}s)…")
        time.sleep(wait)


if __name__ == "__main__":
    run()
