# ZenMarket Luxury Monitor 🛍️

Bot Python de monitoring haute performance pour surveiller les articles de **maroquinerie de luxe** (Louis Vuitton, Prada, Celine, Gucci, Hermès) sur [ZenMarket](https://zenmarket.jp) et envoyer des **alertes enrichies sur Discord** via Webhook.

---

## Fonctionnalités

| Feature | Détail |
|---|---|
| 🇯🇵 Recherche en japonais | Mots-clés natifs (ルイ・ヴィトン, プラダ…) pour maximiser les résultats Yahoo Auctions |
| 🔄 Anti-bannissement | Rotation User-Agent + jitter aléatoire entre requêtes |
| 🤖 Playwright fallback | Bascule automatique sur navigateur headless si requests bloqué |
| 💱 Conversion JPY→EUR | Taux en temps réel via [Frankfurter](https://www.frankfurter.app) (gratuit, sans clé) |
| 🗄️ Déduplication SQLite | Aucune alerte dupliquée, même après redémarrage |
| 💬 Discord Embed riche | Titre, Prix JPY, Prix EUR, Marque, Lien, Image haute résolution |

---

## Prérequis

- Python 3.10+
- Un serveur Discord avec accès Webhooks

---

## Installation

### 1. Cloner le dépôt
```bash
git clone https://github.com/PaulBrochot/zenmarket-luxury-monitor.git
cd zenmarket-luxury-monitor
```

### 2. Créer un environnement virtuel et installer les dépendances
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Installer les navigateurs Playwright (pour le fallback)
```bash
playwright install chromium
```
> ⚠️ Cela télécharge ~100–200 Mo. À faire une seule fois.

---

## Configuration

### 4. Créer votre Webhook Discord

1. Ouvrez votre serveur Discord
2. Allez dans **Paramètres du serveur → Intégrations → Webhooks**
3. Cliquez **Créer un Webhook**
4. Choisissez le salon de destination, nommez-le (ex : `#luxury-alerts`)
5. Cliquez **Copier l'URL du Webhook**

### 5. Créer votre fichier `.env`
```bash
cp .env.example .env
```
Puis éditez `.env` et collez votre URL de Webhook :
```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/VOTRE_ID/VOTRE_TOKEN
```

---

## Lancement

```bash
python monitor.py
```

Le bot tournera en boucle infinie et affichera les logs dans le terminal :
```
2026-05-14 18:00:00 [INFO] 🚀 Démarrage du ZenMarket Luxury Monitor
2026-05-14 18:00:01 [INFO] 20 requêtes de recherche configurées
2026-05-14 18:00:01 [INFO] ── Cycle #1 ──────────────────────────
2026-05-14 18:00:03 [INFO] Scraping Louis Vuitton → ...
2026-05-14 18:00:07 [INFO]   → 3 annonce(s) maroquinerie trouvée(s) pour Louis Vuitton
2026-05-14 18:00:08 [INFO] ✅ Alerte envoyée : ルイ・ヴィトン ショルダーバッグ ...
```

---

## Exécution en arrière-plan

### Sur Linux (systemd)
```bash
# Créer /etc/systemd/system/zenmarket-monitor.service
[Unit]
Description=ZenMarket Luxury Monitor
After=network.target

[Service]
User=votre_utilisateur
WorkingDirectory=/chemin/vers/zenmarket-luxury-monitor
ExecStart=/chemin/vers/.venv/bin/python monitor.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target

# Activer et démarrer
sudo systemctl daemon-reload
sudo systemctl enable zenmarket-monitor
sudo systemctl start zenmarket-monitor
sudo systemctl status zenmarket-monitor
```

### Sur macOS / en local (screen)
```bash
screen -S zenmarket
python monitor.py
# Détacher : Ctrl+A puis D
# Réattacher : screen -r zenmarket
```

---

## Structure du projet

```
zenmarket-luxury-monitor/
├── monitor.py          # Script principal (scraping + alertes)
├── db.py               # Utilitaire SQLite (déduplication)
├── requirements.txt    # Dépendances Python
├── .env.example        # Modèle de configuration
├── .env                # Votre config (ignoré par .gitignore)
└── data/
    └── seen_items.db   # Base SQLite créée automatiquement
```

---

## Personnalisation

- **Ajouter une marque** : Ajouter une entrée dans `BRAND_MAPPING` dans `monitor.py`
- **Changer les mots-clés** : Modifier `KEYWORDS_JP` ou `KEYWORDS_EN`
- **Intervalle** : Ajuster `CHECK_MIN` et `CHECK_MAX` dans `.env`
- **Prix minimum** : Ajouter un filtre `if item["price_jpy"] > MIN_PRICE` dans la boucle principale

---

## ⚠️ Note légale

Ce bot utilise les pages publiques de ZenMarket. Respectez les conditions d'utilisation du site, n'effectuez pas de requêtes excessives et utilisez un délai raisonnable entre cycles (`CHECK_MIN >= 120` recommandé).

---

## Licence

MIT — Libre d'utilisation, modification et distribution.
