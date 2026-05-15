"""
db.py — SQLite helper for ZenMarket monitor.
Stockage des IDs d'annonces déjà traitées pour éviter les doublons.
"""
import sqlite3
from datetime import datetime


def init_db(db_path: str = "data/seen_items.db") -> sqlite3.Connection:
    """Initialise la base de données et crée la table si elle n'existe pas."""
    import os
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            price_jpy   INTEGER,
            brand       TEXT,
            first_seen  TEXT
        )
    """)
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, item_id: str) -> bool:
    """Retourne True si l'annonce a déjà été traitée."""
    cur = conn.execute("SELECT 1 FROM seen_items WHERE id = ?", (item_id,))
    return cur.fetchone() is not None


def mark_seen(
    conn: sqlite3.Connection,
    item_id: str,
    title: str,
    price_jpy: int,
    brand: str,
) -> None:
    """Enregistre une annonce dans la base pour ne plus la retraiter."""
    conn.execute(
        "INSERT OR IGNORE INTO seen_items (id, title, price_jpy, brand, first_seen) VALUES (?, ?, ?, ?, ?)",
        (item_id, title, price_jpy, brand, datetime.utcnow().isoformat()),
    )
    conn.commit()

def get_price(conn: sqlite3.Connection, item_id: str) -> int | None:
    """Retourne le dernier prix connu d'un item, ou None si inconnu."""
    cur = conn.execute("SELECT price_jpy FROM seen_items WHERE id = ?", (item_id,))
    row = cur.fetchone()
    return row[0] if row else None


def update_price(conn: sqlite3.Connection, item_id: str, price_jpy: int) -> None:
    """Met à jour le prix d'un item existant."""
    conn.execute(
        "UPDATE seen_items SET price_jpy = ? WHERE id = ?",
        (price_jpy, item_id),
    )
    conn.commit()