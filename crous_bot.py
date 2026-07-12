import json
import os
import sys
import logging
from pathlib import Path
from typing import Optional
 
import requests
from bs4 import BeautifulSoup
 
# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
SEARCH_URL = "https://trouverunlogement.lescrous.fr/tools/47/search"
 
# Le token et le chat_id sont lus depuis les variables d'environnement
# (fournies par les GitHub Secrets), jamais écrits en dur dans le code.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
 
SEEN_FILE = Path(__file__).parent / "seen_logements.json"
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("crous_bot")
 
 
# ------------------------------------------------------------------
# STOCKAGE DES LOGEMENTS DÉJÀ VUS
# ------------------------------------------------------------------
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()
 
 
def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))
 
 
# ------------------------------------------------------------------
# RÉCUPÉRATION + PARSING
# ------------------------------------------------------------------
def fetch_page() -> Optional[str]:
    try:
        resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning("Erreur réseau lors du fetch: %s", e)
        return None
 
 
def is_overload_page(html: str) -> bool:
    return "trop nombreux" in html.lower()
 
 
def parse_logements(html: str) -> dict:
    """
    Retourne un dict {id_logement: {...}}.
    Extrait le JSON intégré par SvelteKit SSR dans une balise <script>.
 
    Structure réelle observée le 12/07/2026 (via recherche nationale) :
      item["id"]                                  -> identifiant unique
      item["label"]                                -> type ("T1", "T2"...)
      item["residence"]["label"]                   -> nom de la résidence
      item["residence"]["entity"]["name"]           -> ville
      item["residence"]["address"]                  -> adresse complète
      item["area"]["min"]                           -> surface en m²
      item["occupationModes"][0]["rent"]["min"]     -> loyer EN CENTIMES
    """
    soup = BeautifulSoup(html, "html.parser")
    logements = {}
 
    script_tag = soup.find(
        "script",
        attrs={"data-sveltekit-fetched": True, "data-url": lambda v: v and "search" in v},
    )
 
    if script_tag is None or not script_tag.string:
        log.warning("Balise script de résultats introuvable dans le HTML.")
        return logements
 
    try:
        outer = json.loads(script_tag.string)
        inner = json.loads(outer["body"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.error("Erreur de parsing du JSON intégré: %s", e)
        return logements
 
    items = inner.get("results", {}).get("items", [])
 
    for item in items:
        logement_id = str(item.get("id") or "")
        if not logement_id:
            continue
 
        residence = item.get("residence", {}) or {}
        nom_residence = residence.get("label") or "Résidence CROUS"
        ville = (residence.get("entity") or {}).get("name") or ""
        adresse = residence.get("address") or ""
        type_logement = item.get("label") or ""
 
        area_info = item.get("area") or {}
        surface = area_info.get("min")
 
        prix = None
        occupation_modes = item.get("occupationModes") or []
        if occupation_modes:
            rent_info = occupation_modes[0].get("rent") or {}
            rent_cents = rent_info.get("min")
            if rent_cents is not None:
                prix = round(rent_cents / 100, 2)
 
        titre = f"{nom_residence} - {ville}" if ville else nom_residence
        if type_logement:
            titre = f"{titre} ({type_logement})"
 
        logements[logement_id] = {
            "titre": titre,
            "adresse": adresse,
            "prix": prix,
            "surface": surface,
            "url": "https://trouverunlogement.lescrous.fr/tools/47/search"
                   "?bounds=-1.7525876_48.1549705_-1.6244045_48.0769155&locationName=Rennes",
        }
 
    return logements
 
 
# ------------------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------------------
def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant (variables d'environnement).")
        return
 
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Erreur envoi Telegram: %s", e)
 
 
# ------------------------------------------------------------------
# UN SEUL PASSAGE (pas de boucle : GitHub Actions relance le script)
# ------------------------------------------------------------------
def main() -> None:
    log.info("Vérification CROUS Rennes (run unique).")
    seen = load_seen()
    log.info("Logements déjà connus: %d", len(seen))
 
    html = fetch_page()
    if html is None:
        log.info("Pas de réponse réseau, on réessaiera au prochain run.")
        sys.exit(0)
 
    if is_overload_page(html):
        log.info("Page d'attente CROUS ('trop nombreux'), on réessaiera au prochain run.")
        sys.exit(0)
 
    logements = parse_logements(html)
    log.info("Logements trouvés sur la page: %d", len(logements))
 
    new_ids = set(logements.keys()) - seen
    if new_ids:
        for lid in new_ids:
            info = logements[lid]
            details = []
            if info.get("prix"):
                details.append(f"{info['prix']} €")
            if info.get("surface"):
                details.append(f"{info['surface']} m²")
            details_str = " · ".join(details)
            msg = (
                f"🏠 Nouveau logement CROUS à Rennes !\n"
                f"{info['titre']}"
                + (f"\n{details_str}" if details_str else "")
                + (f"\n📍 {info['adresse']}" if info.get("adresse") else "")
                + f"\n{info['url']}"
            )
            log.info("Nouveau logement détecté: %s", lid)
            send_telegram_message(msg)
    else:
        log.info("Aucun nouveau logement.")
 
    save_seen(seen | set(logements.keys()))
 
 
if __name__ == "__main__":
    main()