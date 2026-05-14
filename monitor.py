"""
monitor.py — ZenMarket Luxury Bag Monitor
Surveille les annonces de maroquinerie de luxe sur ZenMarket et envoie
des alertes enrichies sur Discord via Webhook.

Fonctionnalités :
  - Recherche en japonais pour maximiser les résultats Yahoo Auctions
  - Rotation User-Agent + jitter entre cycles (anti-ban)
  - Fallback Playwright si requests échoue (403 / contenu vide)
  - Conversion JPY→EUR via exchangerate.host (sans clé, gratuit)
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
# Intervalle entre deux cycles complets en secondes (min, max)
CHECK_INTERVAL: tuple[int, int] = (int(os.getenv("CHECK_MIN", "120")), int(os.getenv("CHECK_MAX", "300")))
MAX_PAGES: int = int(os.getenv("MAX_PAGES", "3"))  # Pages à scraper par requête

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
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

# ─── Correspondance marques ↔ Japonais ──────────────────────────────────────
BRAND_MAPPING: dict[str, str] = {
    "Louis Vuitton": "ルイ・ヴィトン",
    "Prada":         "プラダ",
    "Celine":        "セリーヌ",
    "Gucci":         "グッチ",
    "Hermès":        "エルメス",
}

# ─── Mots-clés maroquinerie en japonais (filtre du titre) ────────────────────
KEYWORDS_JP: list[str] = [
    "バッグ",       # bag
    "ハンドバッグ",  # handbag
    "ショルダー",   # shoulder
    "ポシェット",   # pochette
    "トートバッグ", # tote bag
    "クラッチ",    # clutch
    "財布",        # wallet (optionnel)
    "サック",      # sac (translittération)
    "バンドゥリエ", # bandoulière
    "レザー",      # leather
    "本革",        # genuine leather
]

# Mots-clés en anglais/français pour filtre secondaire
KEYWORDS_EN: list[str] = [
    "bag", "sac", "bandouliere", "pochette", "tote", "clutch", "leather",
    "purse", "handbag", "wallet"
]

# ─── Construction des URLs ZenMarket ─────────────────────────────────────────
ZENMARKET_SEARCH_URL = "https://zenmarket.jp/en/auction.aspx"

def build_search_urls() -> list[tuple[str, str]]:
    """
    Retourne une liste de tuples (brand_name, search_url) combinant
    marque japonaise + mot-clé maroquinerie principal.
    """
    urls = []
    primary_kw = ["バッグ", "ハンドバッグ", "ショルダー", "ポシェット"]
    for brand_en, brand_jp in BRAND_MAPPING.items():
        for kw in primary_kw:
            query = f"{brand_jp} {kw}"
            url = f"{ZENMARKET_SEARCH_URL}?q={requests.utils.quote(query)}&categoryId=0"
            urls.append((brand_en, url))
    return urls


# ─── HTTP Helpers ─────────────────────────────────────────────────────────────
def get_random_headers() -> dict[str, str]:
    """Génère des headers HTTP avec User-Agent aléatoire."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://zenmarket.jp/en/",
        "DNT": "1",
        "Connection": "keep-alive",
    }


def fetch_with_requests(url: str) -> str | None:
    """
    Récupère le HTML d'une page via requests.
    Retourne le contenu HTML ou None si erreur.
    """
    try:
        resp = requests.get(url, headers=get_random_headers(), timeout=15)
        if resp.status_code == 200:
            return resp.text
        log.warning(f"requests → HTTP {resp.status_code} pour {url}")
        return None
    except requests.RequestException as e:
        log.error(f"requests exception : {e}")
        return None


def fetch_with_playwright(url: str) -> str | None:
    """
    Fallback Playwright si requests échoue (Cloudflare, 403, etc.).
    Lance un navigateur Chromium headless, navigue et renvoie le HTML.
    Nécessite : playwright install (chromium)
    """
    try:
        from playwright.sync_api import sync_playwright
        log.info(f"Fallback Playwright pour : {url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="ja-JP",
                extra_http_headers={"DNT": "1"},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Attente aléatoire pour simuler un comportement humain
            time.sleep(random.uniform(2, 5))
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.error(f"Playwright exception : {e}")
        return None


def fetch_page(url: str) -> str | None:
    """
    Tente d'abord requests, bascule sur Playwright si le contenu
    est absent ou si la réponse est invalide.
    """
    html = fetch_with_requests(url)
    if html and len(html) > 2000:  # Seuil minimal de contenu valide
        return html
    log.info("Contenu insuffisant via requests → Playwright")
    return fetch_with_playwright(url)


# ─── Parsing des annonces ─────────────────────────────────────────────────────
def parse_listings(html: str, brand: str) -> list[dict]:
    """
    Parse le HTML ZenMarket pour extraire les annonces.
    Retourne une liste de dicts : {id, title, price_jpy, url, image_url, brand}
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # ZenMarket liste les items dans des cartes .auction-item ou li.item-card
    # Le sélecteur peut évoluer — adaptez si le site change sa structure
    cards = soup.select("li.auction-item, div.item-card, div.product-item")
    if not cards:
        # Fallback générique : chercher tous les liens avec prix en ¥
        cards = soup.select("[class*='item'], [class*='product'], [class*='auction']")

    for card in cards:
        try:
            # ── Titre
            title_tag = card.select_one("[class*='title'], [class*='name'], h3, h4, a")
            title = title_tag.get_text(strip=True) if title_tag else ""
            if not title:
                continue

            # ── Filtre maroquinerie : titre doit contenir au moins un mot-clé
            title_lower = title.lower()
            matched_kw = any(kw in title for kw in KEYWORDS_JP) or \
                         any(kw in title_lower for kw in KEYWORDS_EN)
            if not matched_kw:
                continue

            # ── Lien
            link_tag = card.select_one("a[href]")
            item_url = ""
            if link_tag:
                href = link_tag["href"]
                item_url = href if href.startswith("http") else f"https://zenmarket.jp{href}"

            # ── ID (extrait de l'URL ou attribut data-id)
            item_id = card.get("data-id") or card.get("id") or ""
            if not item_id and item_url:
                # Extraire l'ID depuis l'URL ex : ...itemCode=x12345...
                m = re.search(r"itemCode=([A-Za-z0-9]+)", item_url) or \
                    re.search(r"/([a-z]\d{7,})", item_url)
                item_id = m.group(1) if m else item_url
            if not item_id:
                continue

            # ── Prix en JPY
            price_tag = card.select_one("[class*='price'], [class*='bid'], span.jpy, strong")
            price_jpy = 0
            if price_tag:
                price_text = price_tag.get_text(strip=True)
                # Extraire les chiffres (ex : "¥ 12,500" → 12500)
                digits = re.sub(r"[^\d]", "", price_text)
                price_jpy = int(digits) if digits else 0

            # ── Image
            img_tag = card.select_one("img[src], img[data-src]")
            image_url = ""
            if img_tag:
                image_url = img_tag.get("src") or img_tag.get("data-src", "")
                # Remplacer les miniatures basse résolution par la haute résolution
                image_url = re.sub(r"_\d+x\d+\.", ".", image_url)  # supprime suffixes resize
                if image_url.startswith("/"):
                    image_url = f"https://zenmarket.jp{image_url}"

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
_rate_cache: dict[str, float] = {}  # Cache du taux pour éviter trop d'appels


def get_jpy_eur_rate() -> float:
    """
    Récupère le taux JPY/EUR via Frankfurter (gratuit, sans clé).
    Met en cache le taux pour 1 heure.
    """
    cache_key = "JPY_EUR"
    now = time.time()
    if cache_key in _rate_cache:
        rate, fetched_at = _rate_cache[cache_key]
        if now - fetched_at < 3600:  # Cache valide 1h
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
        return _rate_cache.get(cache_key, (0.006, 0))[0]  # Fallback taux approx.


def jpy_to_eur(jpy: int) -> float:
    """Convertit un prix en JPY en EUR arrondi à 2 décimales."""
    rate = get_jpy_eur_rate()
    return round(jpy * rate, 2)


# ─── Discord Webhook ──────────────────────────────────────────────────────────
# Couleur de l'embed par marque (valeurs hexadécimales → int)
BRAND_COLORS: dict[str, int] = {
    "Louis Vuitton": 0xC49A3A,  # Or LV
    "Prada":         0x000000,  # Noir
    "Celine":        0x8B6F47,  # Taupe
    "Gucci":         0x2E7D32,  # Vert Gucci
    "Hermès":        0xE25C00,  # Orange Hermès
}


def send_discord_alert(item: dict) -> bool:
    """
    Envoie une alerte Discord sous forme d'Embed contenant :
    - Titre de l'annonce (cliquable)
    - Champs : Prix JPY, Prix EUR estimé, Marque
    - Image principale haute résolution
    - Footer avec timestamp
    Retourne True si envoi réussi.
    """
    if not WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL non définie dans .env")
        return False

    price_eur = jpy_to_eur(item["price_jpy"])
    brand = item.get("brand", "Inconnue")
    color = BRAND_COLORS.get(brand, 0x7289DA)  # Bleu Discord par défaut

    embed: dict = {
        "title":       item["title"][:256],  # Limite Discord
        "url":         item["url"],
        "color":       color,
        "description": f"🏷️ Nouvelle annonce détectée sur **ZenMarket** pour **{brand}**",
        "fields": [
            {"name": "💴 Prix JPY",       "value": f"¥ {item['price_jpy']:,}",        "inline": True},
            {"name": "💶 Prix EUR (est.)", "value": f"€ {price_eur:,.2f}",             "inline": True},
            {"name": "👜 Marque",          "value": brand,                             "inline": True},
        ],
        "footer": {"text": "ZenMarket Luxury Monitor • Cours JPY/EUR en temps réel"},
        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }

    # Image haute résolution si disponible
    if item.get("image_url"):
        embed["image"] = {"url": item["image_url"]}
        embed["thumbnail"] = {"url": item["image_url"]}  # Miniature en haut à droite

    payload = {
        "username":   "ZenMarket Monitor 🛍️",
        "avatar_url": "https://zenmarket.jp/Content/img/header-logo.png",
        "embeds":     [embed],
    }

    try:
        resp = requests.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            log.info(f"✅ Alerte envoyée : {item['title'][:60]}")
            return True
        else:
            log.error(f"Webhook Discord → HTTP {resp.status_code} : {resp.text}")
            return False
    except Exception as e:
        log.error(f"Erreur envoi Discord : {e}")
        return False


# ─── Boucle Principale ────────────────────────────────────────────────────────
def run():
    """
    Boucle principale du bot.
    1. Construit les URLs de recherche
    2. Scrape chaque URL
    3. Filtre, déduplique et envoie les alertes
    4. Attend un délai aléatoire avant le prochain cycle
    """
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
                # Petit délai anti-ban même en cas d'échec
                time.sleep(random.uniform(3, 8))
                continue

            listings = parse_listings(html, brand)

            for item in listings:
                if is_seen(conn, item["id"]):
                    continue  # Déjà notifié

                sent = send_discord_alert(item)
                if sent:
                    mark_seen(conn, item["id"], item["title"], item["price_jpy"], brand)
                    new_items += 1

                # Jitter entre chaque envoi pour éviter le spam Discord (rate limit)
                time.sleep(random.uniform(1, 3))

            # Jitter entre chaque URL pour simuler navigation humaine
            time.sleep(random.uniform(5, 15))

        log.info(f"Cycle #{cycle} terminé — {new_items} nouvelle(s) alerte(s) envoyée(s)")

        # Délai aléatoire avant le prochain cycle complet
        wait = random.randint(*CHECK_INTERVAL)
        log.info(f"Prochain cycle dans {wait}s ({wait//60}m {wait%60}s)…")
        time.sleep(wait)


if __name__ == "__main__":
    run()
