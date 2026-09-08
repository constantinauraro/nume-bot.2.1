import os
import aiosqlite

# Pe Railway, seteaza DB_PATH catre folderul montat ca Volume (ex: /data/mythral.db)
# Local, foloseste implicit data/mythral.db
DB_PATH = os.getenv("DB_PATH", "data/mythral.db")

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS freelancers (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT,
    bio TEXT,
    role TEXT,
    portfolio_url TEXT,
    avatar_url TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    freelancer_id INTEGER,
    reviewer_id INTEGER,
    service TEXT,
    rating INTEGER,
    comment TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    owner_id INTEGER,
    ticket_type TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    freelancer_id INTEGER,
    channel_id INTEGER,
    description TEXT,
    status TEXT DEFAULT 'pending',   -- pending / accepted / declined / completed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS customer_progress (
    user_id INTEGER PRIMARY KEY,
    completed_orders INTEGER DEFAULT 0,
    current_level TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    description TEXT,
    price TEXT,
    image_url TEXT,
    buy_url TEXT
);
"""


async def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        await db.commit()


def get_db():
    return aiosqlite.connect(DB_PATH)
