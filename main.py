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
    # "cogs.reviews",  # dezactivat complet
    "cogs.levels",
    "cogs.store",
    "cogs.interactions",
]


@bot.event
async def on_ready():
    print(f"[OK] Conectat ca {bot.user} (ID: {bot.user.id})")
    try:
        print("[INFO] Pornire CURĂȚARE TOTALĂ corectă...")
        
        # 1. Ștergem toate comenzile globale din baza de date Discord
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("[OK] Toate comenzile globale au fost ȘTERSE.")
        
        # 2. Ștergem comenzile specifice de pe serverul tău
        ID_SERVER = 1544005370383704207 
        server_obiect = discord.Object(id=ID_SERVER)
        bot.tree.clear_commands(guild=server_obiect)
        await bot.tree.sync(guild=server_obiect)
        print("[OK] Toate comenzile de pe server au fost ȘTERSE.")

        print("[INFO] Reînregistrăm curat comenzile din cogs...")
        # 3. Copiem și înregistrăm comenzile actuale DOAR pe serverul tău (ca să nu mai existe duplicate globale)
        bot.tree.copy_global_to(guild=server_obiect)
        synced = await bot.tree.sync(guild=server_obiect)
        
        print(f"[SUCCESS] {len(synced)} comenzi slash sincronizate DOAR pe server!")
    except Exception as e:
        print(f"[EROARE] Sincronizare comenzi: {e}")


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
