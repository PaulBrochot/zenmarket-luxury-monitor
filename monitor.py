"""
monitor.py — Buyee Mercari → ZenMarket Luxury Monitor

Source  : buyee.jp/mercari/search (achat immédiat uniquement)
Alertes : Discord embed avec lien ZenMarket (mercari.aspx) + lien Buyee
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
CHECK_INTERVAL = (int(os.getenv("CHECK_MIN", "120")), int(os.getenv("CHECK_MAX", "300")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("buyee-mercari-monitor")

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
    "speedy", "neverfull", "alma", "keepall", "vuitton", "louis", "lv",
]
EXCLUDE_KEYWORDS = [
    "空笱", "空き笱", "ショップ袋", "紙袋", "保存袋", "ダストバッグ", "保存箱",
    "dustbag", "dust bag", "empty box", "shopping bag only",
]

# Session partagée pour maintenir les cookies
_session = requests.Session()
_session_init = False


def init_session() -> None:
    global _session_init
    if _session_init:
        return
    headers = {
        "User-Agent":      _ua,
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
    }
    _session.headers.update(headers)
    try:
        r1 = _session.get("https://buyee.jp/", timeout=15)
        log.info(f"Init step 1 (root) : {r1.status_code} / {len(r1.text)} chars")
        time.sleep(random.uniform(1.5, 3))
        r2 = _session.get("https://buyee.jp/mercari/", timeout=15,
                          headers={"Referer": "https://buyee.jp/"})
        log.info(f"Init step 2 (mercari) : {r2.status_code} / {len(r2.text)} chars")
        time.sleep(random.uniform(1, 2))
        _session_init = True
        log.info(f"Cookies : {list(_session.cookies.keys())}")
    except Exception as e:
        log.warning(f"Init session erreur : {e}")


def build_search_urls() -> list[tuple[str, str]]:
    urls = []
    primary_kw = ["バッグ", "ハンドバッグ", "ショルダーバッグ", "ポシェット"]
    for brand_en, brand_jp in BRAND_MAPPING.items():
        for kw in primary_kw:
            keyword = requests.utils.quote(f"{brand_jp} {kw}")
            url = f"https://buyee.jp/mercari/search?keyword={keyword}&sort=created_time&order=desc"
            urls.append((brand_en, url))
    return urls


def item_to_zenmarket_url(item_id: str) -> str:
    return f"https://zenmarket.jp/en/mercari.aspx?itemCode={item_id}"


def fetch_page(url: str) -> str | None:
    init_session()
    headers = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://buyee.jp/mercari/",
        "DNT":             "1",
        "Connection":      "keep-alive",
    }
    try:
        resp = _session.get(url, headers=headers, timeout=20)
        resp.encoding = "utf-8"
        if resp.status_code == 200:
            log.info(f"HTML récupéré : {len(resp.text)} caractères")
            return resp.text
        log.warning(f"HTTP {resp.status_code} pour {url}")
        return None
    except Exception as e:
        log.error(f"Erreur fetch : {e}")
        return None


def parse_listings(html: str, brand: str) -> list[dict]:
    soup  = BeautifulSoup(html, "lxml")
    items = []

    # Buyee Mercari : liens directs vers /mercari/item/ID
    links = soup.select("a[href*='/mercari/item/']") or \
            soup.select("a[href*='conversionType=Mercari']")

    seen_ids = set()
    for link in links:
        try:
            href = str(link.get("href", ""))

            # Extraire l'ID : tout ce qui suit /mercari/item/
            m = re.search(r"/mercari/item/([A-Za-z0-9]+)", href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            # Titre : texte du lien, nettoyé
            title = link.get_text(separator=" ", strip=True)
            # Supprimer le badge "Avec Authentification" du début
            title = re.sub(r"^(Avec Authentification|SALE)\s*", "", title).strip()
            if len(title) < 5:
                continue

            title_lower = title.lower()

            # Filtre contenu maroquinerie (le mot de recherche est déjà la marque)
            # On accepte tout article de la recherche de marque
            if any(kw in title for kw in EXCLUDE_KEYWORDS) or \
               any(kw in title_lower for kw in EXCLUDE_KEYWORDS):
                continue

            zenmarket_url = item_to_zenmarket_url(item_id)
            buyee_url = (f"https://buyee.jp{href}" if href.startswith("/") else href)
            # Nettoyer le param de tracking
            buyee_url = buyee_url.split("?")[0]

            # Prix : chercher dans le parent
            price_jpy = 0
            parent = link.parent
            for _ in range(4):  # Remonter jusqu'à 4 niveaux
                if parent is None:
                    break
                price_tag = parent.find(string=re.compile(r"[\d,]+\s*YENS?"))
                if price_tag:
                    digits    = re.sub(r"[^\d]", "", str(price_tag))
                    price_jpy = int(digits) if digits else 0
                    break
                parent = parent.parent

            # Image
            image_url = ""
            img = link.find("img")
            if img:
                src = str(img.get("data-src") or img.get("src", ""))
                if src and "spacer.gif" not in src and "noimage" not in src:
                    image_url = src.split("?")[0]

            items.append({
                "id":        item_id,
                "title":     title,
                "price_jpy": price_jpy,
                "url":       zenmarket_url,
                "buyee_url": buyee_url,
                "image_url": image_url,
                "brand":     brand,
            })

        except Exception as e:
            log.debug(f"Erreur parsing link : {e}")
            continue

    log.info(f"  → {len(items)} annonce(s) pour {brand}")
    return items


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
            f"[🔗 Voir sur Buyee]({item.get('buyee_url', item['url'])})"
        ),
        "fields": [
            {"name": "💴 Prix JPY",       "value": f"¥ {item['price_jpy']:,}" if item['price_jpy'] else "N/A", "inline": True},
            {"name": "💶 Prix EUR (est.)", "value": f"€ {price_eur:,.2f}"        if item['price_jpy'] else "N/A", "inline": True},
            {"name": "👜 Marque",          "value": brand,                                                       "inline": True},
        ],
        "footer":    {"text": "Mercari Japan via Buyee • Achat immédiat • ZenMarket pour commander"},
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


def run():
    log.info("🚀 Démarrage — Buyee Mercari (achat immédiat) → Discord")
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
                time.sleep(random.uniform(0.5, 1.5))

            time.sleep(random.uniform(3, 7))

        log.info(f"Cycle #{cycle} — {new_items} alerte(s) envoyée(s)")
        wait = random.randint(*CHECK_INTERVAL)
        log.info(f"Prochain cycle dans {wait}s …")
        time.sleep(wait)


if __name__ == "__main__":
    run()
