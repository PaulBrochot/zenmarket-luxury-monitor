""" monitor.py — Buyee Mercari → ZenMarket Luxury Monitor
Source : buyee.jp/mercari/search
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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from db import init_db, is_seen, mark_seen, get_price, update_price

load_dotenv()
RATE_API_BASE   = os.getenv("RATE_API_BASE", "https://api.frankfurter.app")
DB_PATH         = os.getenv("DB_PATH", "data/seen_items.db")
CHECK_INTERVAL  = (int(os.getenv("CHECK_MIN", "120")), int(os.getenv("CHECK_MAX", "300")))
MAX_PAGES       = int(os.getenv("MAX_PAGES", "3"))



# ────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("buyee-mercari-monitor")



# ────────────────────────────────────────────
# BRAND MAPPING + COLORS
# ────────────────────────────────────────────
BRAND_MAPPING = {
    "Louis Vuitton": "ルイ・ヴィトン",
    "Prada":         "プラダ",
    "Celine":        "セリーヌ",
    "Gucci":         "グッチ",
    "Hermès":        "エルメス",
}

BRAND_COLORS = {
    "Louis Vuitton": 0xA0522D,
    "Prada":         0x2C2C2C,
    "Celine":        0x4A90E2,
    "Gucci":         0xC41E3A,
    "Hermès":        0xE87D3E,
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
EXCLUDE_KEYWORDS = [
    "空き箱", "ショップ袋", "紙袋", "保存袋", "ダストバッグ",
    "保存箱", "dustbag", "dust bag", "empty box", "shopping bag only",
]



# ────────────────────────────────────────────
# SEARCH URLS
# ────────────────────────────────────────────
def build_search_urls() -> list[tuple[str, str]]:
    """Build search URLs for each brand + primary keywords"""
    primary_kw = [
        "バッグ", "ハンドバッグ", "ショルダーバッグ",
        "ポシェット", "ポーチ", "クラッチ", "トートバッグ",
        "財布", "長財布", "折り財布",
    ]
    urls = []
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
# PLAYWRIGHT FETCH
# ────────────────────────────────────────────
_playwright_instance = None
_browser = None
_context = None


def get_browser_context():
    """Initialise ou réutilise le contexte Playwright"""
    global _playwright_instance, _browser, _context
    if _context is None:
        log.info("🌐 Démarrage Playwright Chromium...")
        _playwright_instance = sync_playwright().start()
        _browser = _playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        _context = _browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
            },
        )
        _context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        log.info("✅ Playwright prêt")
    return _context


def fetch_page(url: str) -> str | None:
    """Fetch page HTML via Playwright (contourne le 403 Buyee)"""
    for attempt in range(3):
        page = None
        try:
            ctx = get_browser_context()
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(random.randint(1500, 3000))
            html = page.content()
            log.info(f"HTML récupéré: {len(html)} caractères")
            return html
        except PlaywrightTimeout:
            log.warning(f"Timeout Playwright — attempt {attempt + 1}/3")
            time.sleep(5)
        except Exception as e:
            log.warning(f"Erreur Playwright: {e} — attempt {attempt + 1}/3")
            time.sleep(5)
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
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
        if not href.endswith((".html", "Mercari_DirectSearch")):
            continue

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
                   img_tag.get("src", "") or "")
            if "imagePath:" in src:
                match = re.search(r"imagePath:\s*'([^']+)'", src)
                if match:
                    image_url = "https:" + match.group(1).replace("@jpg", "")
            elif src and not src.startswith("data:"):
                image_url = src

        # Titre
        title_tag = a.find(["p", "h2"], class_=re.compile(r"name|txt"))
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Prix
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

        items.append({
            "id":        item_id,
            "title":     title_clean,
            "price_jpy": price_jpy,
            "brand":     brand,
            "image_url": image_url,
            "url":       item_to_zenmarket_url(item_id),
            "buyee_url": f"https://buyee.jp{href}",
        })

    log.info(f"  → {len(items)} annonce(s) pour {brand}")
    return items


def item_to_zenmarket_url(item_id: str) -> str:
    return f"https://zenmarket.jp/en/mercariproduct.aspx?itemCode={item_id}"



# ────────────────────────────────────────────
# EXCHANGE RATE
# ────────────────────────────────────────────
_rate_cache: tuple[float, float] = (0.0, 0.0)


def jpy_to_eur(jpy_amount: int) -> float:
    global _rate_cache
    if jpy_amount <= 0:
        return 0.0
    now = time.time()
    if _rate_cache[0] and (now - _rate_cache[0]) < 3600:
        rate = _rate_cache[1]
    else:
        try:
            resp = requests.get(f"{RATE_API_BASE}/latest?from=JPY&to=EUR", timeout=10)
            rate = resp.json().get("rates", {}).get("EUR", 0.0)
            _rate_cache = (now, rate)
        except Exception as e:
            log.warning(f"Erreur taux JPY→EUR: {e}")
            rate = 0.0065
    return jpy_amount * rate



# ────────────────────────────────────────────
# DISCORD WEBHOOK ROUTING
# ────────────────────────────────────────────
def get_webhook_for_item(item: dict) -> tuple[str | None, str]:
    brand = item["brand"]
    title_lower = item["title"].lower()

    if any(kw in title_lower for kw in [
        "財布", "ウォレット", "小銭入れ", "カードケース", "カードホルダー",
        "キーケース", "コインケース", "長財布", "折り財布", "二つ折り", "三つ折り",
        "wallet", "portefeuille", "porte-monnaie", "porte-carte",
        "card holder", "card case", "key case",
    ]):
        item_type, item_type_fr = "WALLET", "Portefeuille"
    elif any(kw in title_lower for kw in [
        "ポシェット", "pochette", "ポーチ", "pouch", "クラッチ", "clutch",
    ]):
        item_type, item_type_fr = "POCHETTE", "Pochette"
    elif any(kw in title_lower for kw in [
        "バッグ", "bag", "sac", "トート", "tote",
        "ショルダー", "shoulder", "ハンドバッグ", "handbag",
    ]):
        item_type, item_type_fr = "SAC", "Sac"
    else:
        item_type, item_type_fr = "SAC", "Accessoire"

    webhook_map = {
        ("Louis Vuitton", "WALLET"):   os.getenv("WEBHOOK_LV_WALLET"),
        ("Louis Vuitton", "POCHETTE"): os.getenv("WEBHOOK_LV_POCHETTE"),
        ("Louis Vuitton", "SAC"):      os.getenv("WEBHOOK_LV_SAC"),
        ("Gucci",  "WALLET"):          os.getenv("WEBHOOK_GUCCI_WALLET"),
        ("Gucci",  "POCHETTE"):        os.getenv("WEBHOOK_GUCCI_POCHETTE"),
        ("Gucci",  "SAC"):             os.getenv("WEBHOOK_GUCCI_SAC"),
        ("Prada",  "WALLET"):          os.getenv("WEBHOOK_PRADA_WALLET"),
        ("Prada",  "POCHETTE"):        os.getenv("WEBHOOK_PRADA_POCHETTE"),
        ("Prada",  "SAC"):             os.getenv("WEBHOOK_PRADA_SAC"),
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


def _send_embed(webhook_url: str, embed: dict) -> None:
    payload = {"username": "ZenmarketBot", "embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info("✅ Alerte Discord envoyée")
        else:
            log.warning(f"Erreur Discord {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"Erreur envoi Discord: {e}")


def send_discord_alert(item: dict) -> None:
    webhook_url, item_type_fr = get_webhook_for_item(item)
    if not webhook_url:
        log.warning(f"Aucun webhook pour {item['brand']} — ignoré")
        return

    price_eur = jpy_to_eur(item["price_jpy"])
    item_type = next((k for k, v in {"WALLET": "Portefeuille", "POCHETTE": "Pochette", "SAC": "Sac"}.items() if v == item_type_fr), "SAC")
    if price_eur > PRICE_MAX_EUR.get(item_type, 9999):
        log.info(f"  💸 Ignoré (€{price_eur:.2f} > seuil) — {item['title'][:40]}")
        return

    brand = item["brand"]
    embed = {
        "author":      {"name": f"{brand} — Nouvelle annonce"},
        "title":       f"{item_type_fr} {brand[:2]}",
        "description": (
            f"📦 **Statut**\n🔵 NOUVELLE ANNONCE\n\n"
            f"💴 **Prix en yen**\n¥{item['price_jpy']:,}\n\n"
            f"💶 **Prix en euros**\n~€{price_eur:.2f}\n\n"
            f"🕐 **Détecté le**\n{datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')}\n\n"
            f"🔗 **[Voir l'annonce]({item['url']})**"
        ),
        "color":       BRAND_COLORS.get(brand, 0x5865F2),
        "image":       {"url": item["image_url"]} if item["image_url"] else None,
        "footer":      {"text": f"ZenmarketBot • {item['id']} • {datetime.datetime.now().strftime('%H:%M')}"},
        "timestamp":   datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _send_embed(webhook_url, embed)


def send_price_drop_alert(item: dict, old_price: int) -> None:
    webhook_url, item_type_fr = get_webhook_for_item(item)
    if not webhook_url:
        return

    price_eur = jpy_to_eur(item["price_jpy"])
    item_type = next((k for k, v in {"WALLET": "Portefeuille", "POCHETTE": "Pochette", "SAC": "Sac"}.items() if v == item_type_fr), "SAC")
    if price_eur > PRICE_MAX_EUR.get(item_type, 9999):
        return

    drop_pct = round((old_price - item["price_jpy"]) / old_price * 100, 1)
    brand = item["brand"]
    embed = {
        "author":      {"name": f"{brand} — Baisse de prix 📉"},
        "title":       f"{item_type_fr} {brand[:2]}",
        "description": (
            f"📦 **Statut**\n📉 BAISSE (¥{old_price:,} → ¥{item['price_jpy']:,})\n\n"
            f"💶 **Prix en euros**\n~€{price_eur:.2f}\n\n"
            f"📊 **Réduction**\n-{drop_pct}%\n\n"
            f"🕐 **Détecté le**\n{datetime.datetime.now().strftime('%d/%m/%Y à %H:%M')}\n\n"
            f"🔗 **[Voir l'annonce]({item['url']})**"
        ),
        "color":       0x00FF00,
        "image":       {"url": item["image_url"]} if item["image_url"] else None,
        "footer":      {"text": f"ZenmarketBot • {item['id']} • {datetime.datetime.now().strftime('%H:%M')}"},
        "timestamp":   datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _send_embed(webhook_url, embed)



# ────────────────────────────────────────────
# MAIN LOOP
# ────────────────────────────────────────────
def main():
    conn = init_db(DB_PATH)
    log.info("🚀 Démarrage du bot Buyee→ZenMarket Monitor (Playwright)")

    urls = build_search_urls()
    log.info(f"📌 {len(urls)} URL(s) de recherche générées")

    get_browser_context()

    while True:
        log.info("─" * 60)
        log.info("🔍 Nouvelle vérification...")
        new_count = drop_count = 0

        for brand, url in urls:
            log.info(f"  Scraping {brand}...")
            html = fetch_page(url)
            if not html:
                log.warning(f"  ⚠️  Impossible de récupérer {brand}")
                continue

            items = parse_listings(html, brand)
            for item in items:
                if not is_seen(conn, item["id"]):
                    send_discord_alert(item)
                    mark_seen(conn, item["id"], item["title"], item["price_jpy"], item["brand"])
                    new_count += 1
                else:
                    old_price = get_price(conn, item["id"])
                    if old_price and item["price_jpy"] < old_price:
                        log.info(f"📉 Baisse: {item['id']} ¥{old_price:,} → ¥{item['price_jpy']:,}")
                        send_price_drop_alert(item, old_price)
                        update_price(conn, item["id"], item["price_jpy"])
                        drop_count += 1

            time.sleep(random.uniform(2, 4))

        log.info(f"✅ Cycle terminé — {new_count} nouvelle(s), {drop_count} baisse(s)")
        wait = random.randint(CHECK_INTERVAL[0], CHECK_INTERVAL[1])
        log.info(f"💤 Attente de {wait}s avant prochain cycle")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\n👋 Arrêt du bot (Ctrl+C)")
        if _browser:
            _browser.close()
        if _playwright_instance:
            _playwright_instance.stop()
    except Exception as e:
        log.error(f"❌ Erreur fatale: {e}", exc_info=True)
