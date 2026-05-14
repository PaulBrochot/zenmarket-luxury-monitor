"""
monitor.py — Mercari Japan → ZenMarket Luxury Monitor

Source  : jp.mercari.com (achat immédiat uniquement, pas d'enchères)
Alertes : Discord embed avec lien ZenMarket (achat) + lien Mercari direct
Filtre  : maroquinerie de luxe, mots-clés japonais + anglais
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

load_dotenv()

WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK_URL", "")
RATE_API_BASE   = os.getenv("RATE_API_BASE", "https://api.frankfurter.app")
DB_PATH         = os.getenv("DB_PATH", "data/seen_items.db")
CHECK_INTERVAL  = (int(os.getenv("CHECK_MIN", "120")), int(os.getenv("CHECK_MAX", "300")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("mercari-monitor")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

BRAND_MAPPING = {
    "Louis Vuitton": "ルイ・ヴィトン",
    "Prada":         "プラダ",
    "Celine":        "セリーヌ",
    "Gucci":         "グッチ",
    "Hermès":        "エルメス",
}

KEYWORDS_JP = [
    "バッグ", "ハンドバッグ", "ショルダー", "ポシェット",
    "トートバッグ", "クラッチ", "財布", "レザー", "本革",
]
KEYWORDS_EN = [
    "bag", "pochette", "tote", "clutch", "leather", "purse", "handbag", "wallet",
]

EXCLUDE_KEYWORDS = [
    "空笱", "空き笱", "ショップ袋", "紙袋", "保存袋", "ダストバッグ", "保存箱",
    "dustbag", "dust bag", "empty box", "shopping bag only",
]

# ─── API Mercari Japan ────────────────────────────────────────────────────────
# Mercari expose une API de recherche JSON publique utilisée par son propre site
MERCARI_API = "https://api.mercari.jp/v2/entities:search"

def build_search_queries() -> list[tuple[str, str]]:
    """Retourne (brand_en, keyword_jp) pour chaque combinaison marque x mot-clé."""
    queries = []
    primary_kw = ["バッグ", "ハンドバッグ", "ショルダーバッグ", "ポシェット"]
    for brand_en, brand_jp in BRAND_MAPPING.items():
        for kw in primary_kw:
            queries.append((brand_en, f"{brand_jp} {kw}"))
    return queries


def item_to_zenmarket_url(item_id: str) -> str:
    """Lien ZenMarket pour acheter un article Mercari Japan."""
    return f"https://zenmarket.jp/en/mercari.aspx?itemCode={item_id}"


def item_to_mercari_url(item_id: str) -> str:
    return f"https://jp.mercari.com/item/{item_id}"


# ─── Fetch via API JSON Mercari ───────────────────────────────────────────────
def fetch_mercari(keyword: str) -> list[dict]:
    """
    Utilise l'API JSON interne de Mercari Japan.
    Retourne les articles en vente (status=ITEM_STATUS_ON_SALE), triés par nouveauté.
    """
    headers = {
        "User-Agent":   random.choice(USER_AGENTS),
        "Accept":       "application/json",
        "X-Platform":   "web",
        "DPoP":         "dummy",  # requis par l'API sinon 400
    }
    payload = {
        "serviceFrom": "suruga",
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
        "searchSessionId": "",
        "pageSize": 30,
        "pageToken": "",
        "searchCondition": {
            "keyword": keyword,
            "excludeKeyword": "",
            "sort": "SORT_CREATED_TIME",
            "order": "ORDER_DESC",
            "status": ["ITEM_STATUS_ON_SALE"],
            "sizeId": [],
            "categoryId": [],
            "brandId": [],
            "sellerId": [],
            "priceMin": 0,
            "priceMax": 0,
            "itemConditionId": [],
            "shippingPayerId": [],
            "shippingFromArea": [],
            "shippingMethod": [],
            "colorId": [],
            "hasCoupon": False,
            "attributes": [],
            "itemTypes": [],
            "skuIds": [],
        },
        "defaultDatasets": ["DATASET_TYPE_MERCARI"],
    }
    try:
        resp = requests.post(MERCARI_API, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            log.info(f"Mercari API : {len(items)} résultats pour '{keyword}'")
            return items
        log.warning(f"Mercari API HTTP {resp.status_code} pour '{keyword}'")
        return []
    except Exception as e:
        log.error(f"Erreur Mercari API : {e}")
        return []


# ─── Parsing des résultats Mercari ───────────────────────────────────────────
def parse_mercari_items(raw_items: list[dict], brand: str) -> list[dict]:
    """
    Filtre et normalise les articles Mercari JSON.
    Structure JSON Mercari :
      {
        "id": "m12345678",
        "name": "ルイヴィトン バッグ ...",
        "price": 12000,
        "thumbnails": ["https://..."],
        "itemStatus": "ITEM_STATUS_ON_SALE",
      }
    """
    items = []
    for raw in raw_items:
        try:
            item_id = raw.get("id", "")
            if not item_id:
                continue

            title = raw.get("name", "").strip()
            if len(title) < 8:
                continue

            title_lower = title.lower()

            # Filtre inclusion maroquinerie
            if not (any(kw in title for kw in KEYWORDS_JP) or
                    any(kw in title_lower for kw in KEYWORDS_EN)):
                continue

            # Filtre exclusion
            if any(kw in title for kw in EXCLUDE_KEYWORDS) or \
               any(kw in title_lower for kw in EXCLUDE_KEYWORDS):
                continue

            # Seulement les articles en vente
            if raw.get("itemStatus") not in ("ITEM_STATUS_ON_SALE", None):
                continue

            price_jpy = int(raw.get("price", 0))

            # Image : première thumbnail en haute qualité
            thumbnails = raw.get("thumbnails", [])
            image_url = thumbnails[0] if thumbnails else ""
            # Mercari sert des URLs type: https://static.mercdn.net/item/detail/orig/photos/mXXX_1.jpg
            # Remplacer _1.jpg?... par _1.jpg pour avoir la pleine résolution
            if image_url:
                image_url = image_url.split("?")[0]

            items.append({
                "id":          item_id,
                "title":       title,
                "price_jpy":   price_jpy,
                "url":         item_to_zenmarket_url(item_id),
                "mercari_url": item_to_mercari_url(item_id),
                "image_url":   image_url,
                "brand":       brand,
            })

        except Exception as e:
            log.debug(f"Erreur parsing item Mercari : {e}")
            continue

    log.info(f"  → {len(items)} annonce(s) maroquinerie pour {brand}")
    return items


# ─── Conversion JPY → EUR ────────────────────────────────────────────────────
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
        "description": (
            f"🏷️ Nouvelle annonce — **{brand}**\n"
            f"[🛍️ Acheter sur ZenMarket]({item['url']})  •  "
            f"[🔗 Voir sur Mercari]({item.get('mercari_url', item['url'])})"
        ),
        "fields": [
            {"name": "💴 Prix JPY",       "value": f"¥ {item['price_jpy']:,}" if item['price_jpy'] else "N/A", "inline": True},
            {"name": "💶 Prix EUR (est.)", "value": f"€ {price_eur:,.2f}"        if item['price_jpy'] else "N/A", "inline": True},
            {"name": "👜 Marque",          "value": brand,                                                       "inline": True},
        ],
        "footer":    {"text": "Mercari Japan • Achat immédiat • ZenMarket pour commander • JPY/EUR temps réel"},
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


# ─── Boucle principale ────────────────────────────────────────────────────────
def run():
    log.info("🚀 Démarrage — Mercari Japan (achat immédiat) → Discord")
    conn    = init_db(DB_PATH)
    queries = build_search_queries()
    log.info(f"{len(queries)} requêtes configurées")

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Cycle #{cycle} {'='*30}")
        new_items = 0

        for brand, keyword in queries:
            log.info(f"Recherche : {keyword}")
            raw_items = fetch_mercari(keyword)
            listings  = parse_mercari_items(raw_items, brand)

            for item in listings:
                if is_seen(conn, item["id"]):
                    continue
                sent = send_discord_alert(item)
                if sent:
                    mark_seen(conn, item["id"], item["title"], item["price_jpy"], brand)
                    new_items += 1
                time.sleep(random.uniform(0.5, 1.5))

            time.sleep(random.uniform(2, 5))

        log.info(f"Cycle #{cycle} — {new_items} alerte(s) envoyée(s)")
        wait = random.randint(*CHECK_INTERVAL)
        log.info(f"Prochain cycle dans {wait}s …")
        time.sleep(wait)


if __name__ == "__main__":
    run()
