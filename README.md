# Mythral Creations — Discord Bot

Bot complet pentru gestionarea unei echipe de freelanceri: tickete, profiluri,
recenzii, comenzi client–freelancer cu accept/decline, și nivele de clienți.

## 1. Instalare

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configurare

1. Copiază `.env.example` în `.env` și pune token-ul botului tău:
   ```
   BOT_TOKEN=token_ul_tau_aici
   ```
   Token-ul se ia din [Discord Developer Portal](https://discord.com/developers/applications) → aplicația ta → Bot → Reset Token.

2. Activează în Developer Portal → Bot → **Privileged Gateway Intents**:
   - `SERVER MEMBERS INTENT`
   - `MESSAGE CONTENT INTENT`

3. Deschide `config.py` și completează ID-urile reale (click dreapta pe
   canal/rol în Discord cu Developer Mode activat → Copy ID):
   - `TICKET_CATEGORY_ID` — categoria unde se creează canalele de tichet/comandă
   - `REVIEWS_CHANNEL_ID` — canalul unde apar recenziile noi
   - `LEVELS_CHANNEL_ID` — canalul unde apar anunțurile de nivel client
   - `STORE_CHANNEL_ID` — canalul unde apar produsele
   - `STAFF_ROLE_ID` — rolul de staff cu acces la tichete/comenzi

4. (Opțional) Ajustează `CUSTOMER_LEVELS` — pragurile de comenzi finalizate
   care acordă un rol nou clientului. **Rolurile trebuie să existe deja pe
   server, cu exact același nume** (ex: rol numit "Notable Customer").

## 3. Pornire

```bash
python main.py
```

La prima pornire, botul creează automat baza de date `data/mythral.db`.

## 4. Comenzi disponibile

| Comandă | Cine o poate folosi | Ce face |
|---|---|---|
| `/ticket-panel` | Staff | Trimite panoul cu butoane (Get a quote / Apply / Support) |
| `/set-profile` | Oricine | Creează/actualizează propriul profil de freelancer |
| `/profile @user` | Oricine | Afișează profilul unui freelancer (cu butoane Reviews / Order from) |
| `/review @freelancer` | Oricine | Deschide un formular pentru a lăsa o recenzie |
| `/my-level` | Oricine | Vezi câte comenzi ai finalizat și nivelul tău |
| `/add-completed-order @client` | Staff | Adaugă manual o comandă finalizată (util dacă nu treci prin fluxul de order) |
| `/add-product` | Staff | Publică un produs nou în canalul de magazin |

## 5. Cum funcționează fluxul de comandă (client → freelancer)

1. Un client apasă **"Order from..."** pe profilul unui freelancer.
2. Completează un formular scurt descriind cererea.
3. Se creează automat un canal privat (client + freelancer + staff).
4. Freelancerul vede butoanele **Accept** / **Decline**.
5. Dacă acceptă, apare butonul **"Mark as Completed"**.
6. Când comanda e marcată finalizată, clientul primește automat +1 la
   comenzi finalizate, iar dacă atinge un prag nou din `CUSTOMER_LEVELS`,
   primește rolul corespunzător și se anunță în canalul de nivele.

## 6. Structura proiectului

```
mythral_bot/
├── main.py              # pornirea botului
├── config.py            # ID-uri, culori, praguri de nivel
├── database.py          # tabele SQLite
├── requirements.txt
├── .env.example
└── cogs/
    ├── tickets.py        # panou tickete + creare/închidere canal
    ├── profiles.py       # profiluri freelanceri
    ├── reviews.py        # recenzii (modal + postare embed)
    ├── levels.py         # nivele clienți
    ├── store.py          # magazin produse
    └── interactions.py   # comenzi client-freelancer + accept/decline
```

## 7. Extindere ulterioară

Module care nu sunt incluse încă, dar se pot adăuga ușor peste aceeași
arhitectură (bază de date + cogs): sistem de giveaway-uri cu reacții,
integrare automată cu un magazin extern (ex: Tebex/Sellix), și un panou de
statistici pentru staff.
