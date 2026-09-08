import os
from dotenv import load_dotenv

load_dotenv()

# Token-ul botului - se pune in fisierul .env, NU aici direct
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ---- CULORI ----
COLOR_MAIN = 0x2B2D31       # negru/gri - stil Netherix
COLOR_SUCCESS = 0x57F287    # verde
COLOR_DANGER = 0xED4245     # rosu
COLOR_WARNING = 0xFEE75C    # galben
COLOR_GOLD = 0xF1C40F       # auriu (recenzii/rating)

# ---- NUME SERVER / BRAND ----
STUDIO_NAME = "Mythral Creations"
STUDIO_FOOTER = f"{STUDIO_NAME} • Assistant Bot"

# ---- ID-URI (citite din variabile de mediu / Railway Variables) ----
# Poti sa le schimbi oricand direct din Railway -> Variables, fara sa atingi codul.
# Valorile de mai jos (dupa virgula) sunt folosite doar daca variabila nu e setata deloc.

# Categoria unde se creeaza canalele de tichet
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "1544008985873490060"))

# Canalul unde apar review-urile noi
REVIEWS_CHANNEL_ID = int(os.getenv("REVIEWS_CHANNEL_ID", "1544008601322913972"))

# Canalul unde apar anunturile de nivel client (ex: "a ajuns la Notable Customer")
LEVELS_CHANNEL_ID = int(os.getenv("LEVELS_CHANNEL_ID", "1546745814355935262"))

# Canalul unde se posteaza produsele din magazin
STORE_CHANNEL_ID = int(os.getenv("STORE_CHANNEL_ID", "1544009758636249199"))

# Rolul de Staff care poate gestiona tichete, review-uri, produse etc.
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1544132518662381608"))

# Praguri de nivel client (numar de comenzi finalizate -> rol acordat)
CUSTOMER_LEVELS = {
    1: "New Customer",
    3: "Regular Customer",
    7: "Notable Customer",
    15: "VIP Customer",
}
