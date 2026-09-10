import asyncio
import logging

import discord
from discord.ext import commands

import config
from database import init_db
from cogs.tickets import ensure_schema

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

EXTENSIONS = [
    "cogs.tickets",
    "cogs.profiles",
    # "cogs.reviews",  # dezactivat: conflict la sincronizare
    "cogs.levels",
    "cogs.store",
    "cogs.interactions",
]


@bot.event
async def on_ready():
    print(f"[OK] Conectat ca {bot.user} (ID: {bot.user.id})")
    try:
        print("[INFO] Pornire curățare forțată a comenzilor vechi...")
        
        # 1. Șterge forțat toate comenzile globale vechi din baza de date Discord
        bot.tree.clear(guild=None)
        await bot.tree.sync()
        print("[OK] Toate comenzile globale vechi au fost ȘTERSE din Discord.")
        
        # 2. Șterge comenzile de pe server (în caz că ai înregistrat comanda direct pe server în trecut)
        # NOTĂ: Schimbă 1234567890 de mai jos cu ID-ul REAL al serverului tău de Discord!
        ID_SERVER = 1234567890 
        server_obiect = discord.Object(id=ID_SERVER)
        bot.tree.clear(guild=server_obiect)
        await bot.tree.sync(guild=server_obiect)
        print("[OK] Toate comenzile specifice de server au fost ȘTERSE din Discord.")

        print("[INFO] Reînregistrăm doar comenzile noi și valide...")
        # 3. Încarcă din nou în arbore comenzile din cogs-urile active (cum e ticket.py)
        # discord.py va citi automat modulele încărcate deja în main()
        synced = await bot.tree.sync()
        print(f"[SUCCESS] {len(synced)} comenzi slash înregistrate curat.")
        
    except Exception as e:
        print(f"[EROARE] Problemă la curățare/sincronizare: {e}")


async def main():
    await init_db()
    await ensure_schema()

    async with bot:
        for ext in EXTENSIONS:
            try:
                await bot.load_extension(ext)
                print(f"[OK] Modul incarcat: {ext}")
            except Exception as e:
                print(f"[EROARE] Nu am putut incarca {ext}: {e}")
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
