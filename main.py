# bot_completed_with_emprunteur_v2_with_pdf.py
import os
import logging
import pandas as pd
import datetime
import io
from fpdf import FPDF
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# -------------------------
# Configuration / Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------
# ÉTATS DE LA CONVERSATION
# -------------------------
(
    PRODUIT,
    TYPCOT,
    DNAISS,
    DUREE,
    NBRENTE,
    MONTANT,
    DNAISS_I,
    PERIODE_I,
    CAPOBSQ_I,
    # FER+ states
    FER_CHOIX,
    FER_DUREE,
    FER_MONTANT,
    # EMPRUNTEUR states
    DNAISS_E,
    DUREE_PRET,
    CAP_PRET,
    # SELECTION MEDICAL
    SEL_MED,
    # État pour demander si l'utilisateur veut le PDF
    ASK_PDF,
) = range(17)

# -------------------------
# Charger les fichiers Excel (avec protections)
# -------------------------
try:
    df_taux = pd.read_excel("T_taux_Etudes.xlsx", sheet_name="T_taux_Etudes")
    df_prime = pd.read_excel("T_Prime_IBEKELIA.xlsx", sheet_name="T_Prime_IBEKELIA")
    # FER+ sheets (doit exister)
    df_fer_grille = pd.read_excel("table_taux_FER+.xlsx", sheet_name="grille_FER+")
    df_fer_table = pd.read_excel("table_taux_FER+.xlsx", sheet_name="table_taux_FER+")
    # EMPRUNTEUR rates
    df_emp = pd.read_excel("tauxEmp.xlsx", sheet_name="tauxEmp")
except Exception as e:
    logger.exception("Erreur en lisant les fichiers Excel. Vérifie qu'ils sont présents et nommés correctement.")
    raise SystemExit(e)

# -------------------------
# Normaliser : convertir en str les colonnes et nettoyer les index
# -------------------------
# Taux (Assur'Education)
if "DureeCot-Nbrente" not in df_taux.columns:
    raise SystemExit("La colonne 'DureeCot-Nbrente' n'existe pas dans T_taux_Etudes.xlsx.")
df_taux["DureeCot-Nbrente"] = df_taux["DureeCot-Nbrente"].astype(str).str.strip()
df_taux.set_index("DureeCot-Nbrente", inplace=True)
df_taux.columns = df_taux.columns.astype(str)

# Prime IBEKELIA
if "T_Prime_IBEKELIA" not in df_prime.columns:
    raise SystemExit("La colonne 'T_Prime_IBEKELIA' n'existe pas dans T_Prime_IBEKELIA.xlsx.")
df_prime["T_Prime_IBEKELIA"] = df_prime["T_Prime_IBEKELIA"].astype(str).str.strip()
df_prime.set_index("T_Prime_IBEKELIA", inplace=True)
df_prime.columns = df_prime.columns.astype(str)

# FER+ grille (A..G rows)
required_fer_cols = {"choixCot", "cotMensEp", "cotMensPrev", "cotMensTot", "capDec"}
if not required_fer_cols.issubset(set(df_fer_grille.columns)):
    logger.error("La feuille 'grille_FER+' doit contenir les colonnes : %s", required_fer_cols)
    raise SystemExit("grille_FER+ incorrecte")

# normaliser et indexer grille FER+
df_fer_grille["choixCot"] = df_fer_grille["choixCot"].astype(str).str.strip().str.upper()
df_fer_grille.set_index("choixCot", inplace=True)
# convertir colonnes numériques
for c in ("cotMensEp", "cotMensPrev", "cotMensTot", "capDec"):
    df_fer_grille[c] = pd.to_numeric(df_fer_grille[c], errors="coerce")

# FER+ table taux : dureeCot -> tauxP
if "dureeCot" not in df_fer_table.columns or "tauxP" not in df_fer_table.columns:
    logger.error("La feuille 'table_taux_FER+' doit contenir 'dureeCot' et 'tauxP'")
    raise SystemExit("table_taux_FER+ incorrecte")
df_fer_table["dureeCot"] = pd.to_numeric(df_fer_table["dureeCot"], errors="coerce").astype(int)
df_fer_table["tauxP"] = pd.to_numeric(df_fer_table["tauxP"], errors="coerce")
df_fer_table.set_index("dureeCot", inplace=True)

# EMPRUNTEUR : normaliser le tableau des taux
if "age" not in df_emp.columns:
    # si la colonne s'appelle différemment, tente de trouver la première colonne non-numérique
    raise SystemExit("Le fichier tauxEmp.xlsx doit contenir une colonne 'age'.")
# convertir l'index age
df_emp = df_emp.copy()
df_emp["age"] = df_emp["age"].astype(int)
# Les colonnes restantes représentent la durée (en mois probablement). On les convertit en int.
cols = [c for c in df_emp.columns if c != "age"]
# certaines colonnes sont des nombres d'entiers (1..360)
new_cols = {}
for c in cols:
    try:
        new_c = int(c)
        new_cols[c] = new_c
    except Exception:
        # tenter convertir en float puis int
        try:
            new_cols[c] = int(float(c))
        except Exception:
            # ignorer colonne
            logger.warning("Colonne non reconnue dans tauxEmp: %s", c)
            new_cols[c] = c
# Renommer les colonnes
df_emp.rename(columns=new_cols, inplace=True)
# indexer par age
df_emp.set_index("age", inplace=True)

# -------------------------
# Mapping capital obsèques (choix 1..5 -> montant)
# -------------------------
CAP_OBSEQUES = {
    "1": 1000000,
    "2": 2000000,
    "3": 3000000,
    "4": 4000000,
    "5": 5000000
}

# -------------------------
# Helpers pour validation / recherche
# -------------------------
def available_ages_taux():
    ages = sorted({int(idx.split("-")[0]) for idx in df_taux.index})
    return min(ages), max(ages)


def available_ages_prime():
    ages = sorted({int(idx.split("-")[0]) for idx in df_prime.index})
    return min(ages), max(ages)


def get_taux(age: int, nb_rente: int, duree: int):
    key = f"{age}-{nb_rente}"
    col = str(duree)
    if key not in df_taux.index or col not in df_taux.columns:
        return None
    try:
        return float(df_taux.loc[key, col])
    except Exception:
        logger.exception("Erreur get_taux")
        return None


def get_prime(age: int, per_cot: str, cap_obsq: int):
    key = f"{age}-{per_cot}"
    col = str(cap_obsq)
    if key not in df_prime.index or col not in df_prime.columns:
        return None
    try:
        return float(df_prime.loc[key, col])
    except Exception:
        logger.exception("Erreur get_prime")
        return None

# FER+ helpers
def get_fer_grille(choix: str):
    choix = choix.strip().upper()
    if choix not in df_fer_grille.index:
        return None
    return df_fer_grille.loc[choix]


def get_fer_taux(duree: int):
    if duree not in df_fer_table.index:
        return None
    try:
        return float(df_fer_table.loc[duree, "tauxP"])
    except Exception:
        logger.exception("Erreur get_fer_taux")
        return None

# EMPRUNTEUR helper
def get_emp_taux(age: int, duree_mois: int):
    """Retourne le taux (float) pour l'age et la durée en mois.
    Les colonnes du fichier tauxEmp.xlsx sont supposées être des entiers représentant des durées (1..360).
    """
    if age not in df_emp.index:
        return None
    # si la colonne n'existe pas, retourner None
    if duree_mois not in df_emp.columns:
        return None
    try:
        val = df_emp.loc[age, duree_mois]
        return float(val) if pd.notna(val) else None
    except Exception:
        logger.exception("Erreur get_emp_taux")
        return None

# -------------------------
# UI: menu keyboard (command-style buttons pour éviter ambiguité avec saisies numériques)
# -------------------------
MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Assur'Education", "IBEKELIA"],
        ["FER+", "Emprunteur"],
        ["Sélection Médicale", "Autres produits"],
        ["Menu", "Annuler"],
    ],
    resize_keyboard=True,
)
# -------------------------
# Helpers conversationnels
# -------------------------
async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # nettoyer le contexte pour éviter de réutiliser d'anciennes valeurs
    context.user_data.clear()
    user = update.effective_user
    await update.message.reply_text(
        f"Bonjour {user.first_name or ''} !\n\n"
        "Vous souhaitez faire une cotation de :\n"
        "1- Assur'Education\n"
        "2- IBEKELIA\n"
        "3- FER+\n"
        "4- Emprunteur\n"
        "5- Sélection Médical\n"
        "6- Autres produits\n\n"
        "Vous pouvez aussi utiliser les commandes rapides ci-dessous :\n"
        "/assur  /ibekelia  /fer  /emprunteur  /selection  /autres\n\n"
        "Répondez par 1, 2, 3, 4, 5 ou 6, ou tapez une commande.",
        reply_markup=MENU_KEYBOARD,
    )
    return PRODUIT

# Entry-point et helpers de démarrage pour chaque parcours (pour pouvoir lancer un parcours n'importe quand)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # message d'accueil principal
    user = update.effective_user
    await update.message.reply_text(
        f"Bonjour {user.first_name or ''} !\n\n"
        "Vous souhaitez faire une cotation de :\n"
        "1- Assur'Education\n"
        "2- IBEKELIA\n"
        "3- FER+\n"
        "4- Emprunteur\n"
        "5- Sélection Médical\n"
        "6- Autres produits\n\n"
        "Répondez par 1, 2, 3, 4, 5 ou 6.\n"
        "Vous pouvez aussi utiliser les commandes rapides ci-dessous.\n",
        reply_markup=MENU_KEYBOARD,
    )
    return PRODUIT

async def start_assur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Parcours Assur'Education :\n\n"
        "1- Prestation définie ?\n"
        "2- Cotisation définie ?\n\n"
        "Répondez 1 ou 2.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TYPCOT

async def start_ibekelia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Parcours IBEKELIA :\nEntrez votre année de naissance (AAAA) :", reply_markup=ReplyKeyboardRemove())
    return DNAISS_I

async def start_fer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Parcours FER+ :\n\n"
        "Choisissez votre capacité d'épargne (répondez A..H) :\n\n"
        "Epargne - Décès - Capacité d'épargne total - Capital Déces\n\n"
        "A - 10 000  - 2 000  - 12 000  - 2 000 000\n"
        "B - 20 000  - 4 000  - 24 000  - 4 000 000\n"
        "C - 30 000  - 6 000  - 36 000  - 6 000 000\n"
        "D - 40 000  - 8 000  - 48 000  - 8 000 000\n"
        "E - 60 000  - 12 000 - 72 000  - 12 000 000\n"
        "F - 80 000  - 16 000 - 96 000  - 16 000 000\n"
        "G - 100 000 - 20 000 - 120 000 - 20 000 000\n"
        "H - Je peux cotiser plus de 120 000 par mois (saisie libre)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return FER_CHOIX

async def start_emprunteur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Parcours EMPRUNTEUR :\nEntrez votre année de naissance (AAAA) :", reply_markup=ReplyKeyboardRemove())
    return DNAISS_E

async def start_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Parcours SÉLECTION MÉDICAL :\nModule en cours de construction…", reply_markup=MENU_KEYBOARD)
    return PRODUIT

# -------------------------
# Handlers
# -------------------------
async def choix_produit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip()
    norm = choix.strip().lower()

    # commandes directes (/assur, /fer, etc.)
    if choix.startswith("/"):
        cmd = choix.lstrip("/").lower()
        if cmd in ("assur", "assureducation"):
            return await start_assur(update, context)
        if cmd == "ibekelia":
            return await start_ibekelia(update, context)
        if cmd == "fer":
            return await start_fer(update, context)
        if cmd == "emprunteur":
            return await start_emprunteur(update, context)
        if cmd == "selection":
            return await start_selection(update, context)
        if cmd in ("menu", "start"):
            return await back_to_menu(update, context)
        if cmd in ("cancel", "annuler"):
            return await cancel(update, context)

    # boutons et saisies textuelles (noms complets, numéros, alias)
    if norm in ("1", "assur'education", "assur", "assureducation"):
        return await start_assur(update, context)
    elif norm in ("2", "ibekelia"):
        return await start_ibekelia(update, context)
    elif norm in ("3", "fer+", "fer"):
        return await start_fer(update, context)
    elif norm in ("4", "emprunteur"):
        return await start_emprunteur(update, context)
    elif norm in ("5", "sélection médicale", "selection médicale", "selection", "sélection", "selection medicale"):
        await update.message.reply_text(
            "Parcours SÉLECTION MÉDICALE :\nModule en cours de construction…",
            reply_markup=MENU_KEYBOARD
        )
        return PRODUIT
    elif norm in ("6", "autres produits", "autres"):
        await update.message.reply_text("Parcours en construction…", reply_markup=MENU_KEYBOARD)
        return PRODUIT
    elif norm in ("menu", "start"):
        return await back_to_menu(update, context)
    elif norm in ("annuler", "cancel"):
        return await cancel(update, context)
    else:
        await update.message.reply_text(
            "Choix non reconnu. Utilisez les boutons du menu ou tapez /menu pour revenir au menu principal.",
            reply_markup=MENU_KEYBOARD,
        )
        return PRODUIT

# -------------------------
# Assur'Education handlers (identiques)
# -------------------------
async def choix_typcot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    # permettre de revenir au menu à tout moment via la commande /menu
    if txt == "/menu":
        return await back_to_menu(update, context)

    if txt not in ("1", "2"):
        await update.message.reply_text("Choix invalide. Répondez 1 (Prestation) ou 2 (Cotisation).")
        return TYPCOT
    context.user_data["typCot"] = int(txt)
    await update.message.reply_text("Entrez votre année de naissance (AAAA) :")
    return DNAISS

async def saisie_ddnaiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        ddNaiss = int(text)
        if ddNaiss < 1900 or ddNaiss > datetime.datetime.now().year:
            raise ValueError
    except Exception:
        await update.message.reply_text("Année invalide. Entrez l'année de naissance au format AAAA (ex: 1985).")
        return DNAISS

    # Calcul âge et validation par rapport au tableau
    age = datetime.datetime.now().year - ddNaiss
    min_age, max_age = available_ages_taux()
    if age < min_age or age > max_age:
        await update.message.reply_text(
            f"Âge hors grille (âge calculé = {age}). Les âges disponibles pour les taux vont de {min_age} à {max_age}.\n"
            "Entrez une autre année de naissance ou /cancel."
        )
        return DNAISS

    context.user_data["ddNaiss"] = ddNaiss
    context.user_data["age"] = age
    await update.message.reply_text("Entrez la durée de cotisation (5 à 20) :")
    return DUREE

async def saisie_duree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        duree = int(text)
    except Exception:
        await update.message.reply_text("Durée invalide. Entrez un nombre entier entre 5 et 20.")
        return DUREE
    if not (5 <= duree <= 20):
        await update.message.reply_text("Durée hors intervalle. Entrez une durée entre 5 et 20.")
        return DUREE
    # vérifier que la colonne existe
    if str(duree) not in df_taux.columns:
        await update.message.reply_text(f"Aucune colonne de durée {duree} trouvée dans le fichier. Choisissez une autre durée.")
        return DUREE

    context.user_data["dureeCot"] = duree
    await update.message.reply_text("Entrez le nombre de rentes (1 à 7) :")
    return NBRENTE

async def saisie_nb_rente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        nb_rente = int(text)
    except Exception:
        await update.message.reply_text("nombre de rentes invalide. Entrez un entier (1 à 7).")
        return NBRENTE
    if not (1 <= nb_rente <= 7):
        await update.message.reply_text("Nombre de rentes hors intervalle. Entrez entre 1 et 7.")
        return NBRENTE

    age = context.user_data.get("age")
    # vérifier que la clé age-nb_rente existe
    key = f"{age}-{nb_rente}"
    if key not in df_taux.index:
        # proposer les nb_rente disponibles pour cet âge
        possibles = [int(idx.split("-")[1]) for idx in df_taux.index if idx.split("-")[0] == str(age)]
        if possibles:
            await update.message.reply_text(
                f"Aucun tarif exact pour {age}-{nb_rente}. Les nombres de rentes disponibles pour l'âge {age} sont : {sorted(set(possibles))}.\n"
                "Entrez un autre nombre de rentes (ou /cancel)."
            )
        else:
            await update.message.reply_text(
                f"Aucun tarif trouvé pour l'âge {age}. Revenez au début avec /start ou /cancel."
            )
        return NBRENTE

    context.user_data["nbRente"] = nb_rente

    # 🔹 Texte personnalisé en fonction du type de prestation choisi
    typCot = context.user_data.get("typCot")
    if typCot == 1:
        message = "Entrez le montant de la rente annuelle :"
    else:
        message = "Entrez la cotisation mensuelle :"

    await update.message.reply_text(message)
    return MONTANT

async def saisie_montant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        montant = float(text.replace(",", "."))
    except Exception:
        await update.message.reply_text("Montant invalide. Entrez un nombre (ex : 12000).")
        return MONTANT

    data = context.user_data
    typCot = data.get("typCot")
    age = data.get("age")
    duree = data.get("dureeCot")
    nb_rente = data.get("nbRente")

    taux = get_taux(age, nb_rente, duree)
    if taux is None or taux == 0:
        await update.message.reply_text("Désolé, aucun taux trouvé pour vos paramètres (ou taux nul). Recommencez avec /start.")
        return await back_to_menu(update, context)

    if typCot == 1:
        mtRente = montant
        cotisation_mensuelle = taux * mtRente
        await update.message.reply_text(
            f"✅ Votre bénéficiaire pourra jouir d'une rente annuelle de : {mtRente:,.2f}\n"
            f"pendant {nb_rente} années contre une cotisation mensuelle de {cotisation_mensuelle:,.2f}."
        )

        # Préparer le récapitulatif pour le PDF
        context.user_data["last_recap"] = {
            "product": "Assur'Education",
            "title": "Assur'Education - Récapitulatif",
            "inputs": {
                "Type de cotisation": "Prestation",
                "Année de naissance": data.get("ddNaiss"),
                "Âge": age,
                "Durée cotisation (ans)": duree,
                "Nombre de rentes": nb_rente,
                "Montant rente annuelle": mtRente,
            },
            "results": {
                "Taux": taux,
                "Cotisation mensuelle": f"{cotisation_mensuelle:,.2f}",
            },
        }

    else:
        mtCot = montant
        rente_annuelle = mtCot / taux
        await update.message.reply_text(
            f"✅ Avec une cotisation mensuelle de {mtCot:,.2f},\n"
            f"votre bénéficiaire pourra bénéficier d'une rente annuelle de : {rente_annuelle:,.2f}\n"
            f"pendant {nb_rente} années."
        )

        context.user_data["last_recap"] = {
            "product": "Assur'Education",
            "title": "Assur'Education - Récapitulatif",
            "inputs": {
                "Type de cotisation": "Cotisation",
                "Année de naissance": data.get("ddNaiss"),
                "Âge": age,
                "Durée cotisation (ans)": duree,
                "Nombre de rentes": nb_rente,
                "Cotisation mensuelle saisie": mtCot,
            },
            "results": {
                "Taux": taux,
                "Rente annuelle": f"{rente_annuelle:,.2f}",
            },
        }

    # Demander à l'utilisateur s'il souhaite le PDF
    return await ask_pdf_and_store(update, context)

# ----- IBEKELIA (identique) -----
async def saisie_ddnaiss_i(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        ddNaiss = int(text)
        if ddNaiss < 1900 or ddNaiss > datetime.datetime.now().year:
            raise ValueError
    except Exception:
        await update.message.reply_text("Année invalide. Entrez l'année de naissance au format AAAA (ex: 1985).")
        return DNAISS_I

    age = datetime.datetime.now().year - ddNaiss
    min_age, max_age = available_ages_prime()
    if age < min_age or age > max_age:
        await update.message.reply_text(
            f"Âge hors grille (âge_calculé = {age}). Les âges disponibles pour IBEKELIA vont de {min_age} à {max_age}.\n"
            "Entrez une autre année de naissance ou /cancel."
        )
        return DNAISS_I

    context.user_data["ddNaiss"] = ddNaiss
    context.user_data["age"] = age
    await update.message.reply_text(
        "Entrez la périodicité de cotisation !\n"
        "M - pour mensuelle\n"
        "A - pour annuelle\n"
        "U - pour unique",
    )
    return PERIODE_I

async def saisie_periode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    per = update.message.text.strip().upper()
    if per == "/menu":
        return await back_to_menu(update, context)
    if per not in ("M", "A", "U"):
        await update.message.reply_text("Périodicité invalide. Répondez M, A ou U.")
        return PERIODE_I
    context.user_data["perCot"] = per
    await update.message.reply_text(
        "Entrez le capital d'assistance obsèques souhaité !\n"
        "1- 1 000 000\n"
        "2- 2 000 000\n"
        "3- 3 000 000\n"
        "4- 4 000 000\n"
        "5- 5 000 000"
    )
    return CAPOBSQ_I

async def saisie_capobsq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip()
    if choix == "/menu":
        return await back_to_menu(update, context)
    if choix not in CAP_OBSEQUES:
        await update.message.reply_text("Choix invalide. Répondez 1,2,3,4 ou 5.")
        return CAPOBSQ_I
    cap_obsq = CAP_OBSEQUES[choix]
    data = context.user_data
    age = data.get("age")
    per_cot = data.get("perCot")

    prime = get_prime(age, per_cot, cap_obsq)
    if prime is None:
        await update.message.reply_text("Désolé, aucun tarif trouvé pour vos paramètres. Vérifiez la périodicité et l'âge.")
        return await back_to_menu(update, context)

    await update.message.reply_text(
        f"✅ Pour une cotisation {per_cot} de {prime:,.2f},\n"
        f"vous garantissez à vos proches un capital de {cap_obsq:,.0f}.\n"
        "Vous les libérez ainsi des soucis financiers et organisationnels liés à vos obsèques, en toute sérénité."
    )

    # Préparer le récapitulatif
    context.user_data["last_recap"] = {
        "product": "IBEKELIA",
        "title": "IBEKELIA - Récapitulatif",
        "inputs": {
            "Année de naissance": data.get("ddNaiss"),
            "Âge": age,
            "Périodicité": per_cot,
            "Capital obsèques": cap_obsq,
        },
        "results": {
            "Prime": f"{prime:,.2f}",
        },
    }

    return await ask_pdf_and_store(update, context)

# ----- FER+ handlers (nouveau parcours 3) -----
async def fer_choix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip().upper()
    if choix == "/menu":
        return await back_to_menu(update, context)
    # Accept A..G from grille plus H (saisie libre)
    valid_choices = list(df_fer_grille.index) + ["H"]
    if choix not in valid_choices:
        await update.message.reply_text("Choix invalide. Répondez par A, B, C, D, E, F, G ou H.")
        return FER_CHOIX

    context.user_data["fer_choix"] = choix
    await update.message.reply_text("Entrez la durée de cotisation (en années, 1 à 47) :")
    return FER_DUREE

async def fer_duree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        duree = int(text)
    except Exception:
        await update.message.reply_text("Durée invalide. Entrez un entier entre 1 et 47.")
        return FER_DUREE
    if not (1 <= duree <= 47):
        await update.message.reply_text("Durée hors intervalle. Entrez entre 1 et 47.")
        return FER_DUREE

    tauxP = get_fer_taux(duree)
    if tauxP is None:
        await update.message.reply_text(f"Aucun taux trouvé pour la durée {duree}. Vérifiez la durée.")
        return FER_DUREE

    context.user_data["fer_duree"] = duree
    context.user_data["fer_tauxP"] = tauxP

    choix = context.user_data["fer_choix"]
    if choix == "H":
        await update.message.reply_text("Vous avez choisi H (cotisation libre > 120000). Entrez votre cotisation mensuelle (doit être supérieure à 120000) :")
        return FER_MONTANT
    else:
        # lecture des valeurs de la grille
        grille = get_fer_grille(choix)
        if grille is None:
            await update.message.reply_text("Erreur interne : grille introuvable pour ce choix.")
            return await back_to_menu(update, context)

        cotMensEp = float(grille["cotMensEp"]) if pd.notna(grille["cotMensEp"]) else 0
        cotMensPrev = float(grille["cotMensPrev"]) if pd.notna(grille["cotMensPrev"]) else 0
        cotMensTot = float(grille["cotMensTot"]) if pd.notna(grille["cotMensTot"]) else 0
        capDec = float(grille["capDec"]) if pd.notna(grille["capDec"]) else 0
        tauxP = context.user_data["fer_tauxP"]
        # calcul
        capAcquis = tauxP * cotMensEp

        await update.message.reply_text(
            f"✅ Pour une cotisation mensuelle de {cotMensTot:,.0f} dont {cotMensEp:,.0f} de prime épargne "
            f"et {cotMensPrev:,.0f} de prime décès pendant {duree} ans, il est garanti :\n\n"
            f"- un capital acquis de {capAcquis:,.2f} en cas de vie au terme du contrat ;\n"
            f"- un capital décès de {capDec:,.0f} + la valeur de l'épargne constituée en cas de décès avant terme."
        )

        # Préparer récapitulatif
        context.user_data["last_recap"] = {
            "product": "FER+",
            "title": "FER+ - Récapitulatif",
            "inputs": {
                "Choix grille": choix,
                "Durée (ans)": duree,
                "Cot mens ep (épargne)": cotMensEp,
                "Cot mens prev (décès)": cotMensPrev,
                "Cot mens tot": cotMensTot,
            },
            "results": {
                "TauxP": tauxP,
                "Capital acquis": f"{capAcquis:,.2f}",
                "Capital décès garanti": f"{capDec:,.0f}",
            },
        }

        return await ask_pdf_and_store(update, context)

async def fer_montant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        mtCot = float(text)
    except Exception:
        await update.message.reply_text("Montant invalide. Entrez un nombre (ex : 125000).")
        return FER_MONTANT
    if mtCot <= 120000:
        await update.message.reply_text("Pour H, la cotisation doit être strictement supérieure à 120000. Réessayez.")
        return FER_MONTANT

    duree = context.user_data.get("fer_duree")
    tauxP = context.user_data.get("fer_tauxP")
    # formule demandée : capAcquis = tauxPrime * (mtCot - 20000)
    capAcquis = tauxP * (mtCot - 20000)

    await update.message.reply_text(
        f"✅ Pour une cotisation mensuelle de {mtCot:,.0f} dont {mtCot - 20000:,.0f} de prime épargne "
        f"et 20 000 de prime décès pendant {duree} ans, il est garanti :\n\n"
        f"- un capital acquis de {capAcquis:,.2f} en cas de vie au terme du contrat ;\n"
        f"- un capital décès de 20 000 000 + la valeur de l'épargne constituée en cas de décès avant terme."
    )

    # Préparer récapitulatif
    context.user_data["last_recap"] = {
        "product": "FER+",
        "title": "FER+ - Récapitulatif",
        "inputs": {
            "Choix grille": "H (saisie libre)",
            "Durée (ans)": duree,
            "Cotisation mensuelle saisie": mtCot,
        },
        "results": {
            "TauxP": tauxP,
            "Capital acquis": f"{capAcquis:,.2f}",
            "Capital décès garanti": "20 000 000 + épargne",
        },
    }

    return await ask_pdf_and_store(update, context)

# ----- EMPRUNTEUR handlers (nouveau) -----
async def saisie_ddnaiss_e(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        ddNaiss = int(text)
        if ddNaiss < 1900 or ddNaiss > datetime.datetime.now().year:
            raise ValueError
    except Exception:
        await update.message.reply_text("Année invalide. Entrez l'année de naissance au format AAAA (ex: 1985).")
        return DNAISS_E

    age = datetime.datetime.now().year - ddNaiss
    # vérifier que l'âge existe dans la grille emprunteur
    if age not in df_emp.index:
        await update.message.reply_text(
            f"Âge hors grille pour Emprunteur (âge calculé = {age}).\n"
            "Veuillez contacter un conseiller ou recommencer avec /start."
        )
        return await back_to_menu(update, context)

    context.user_data["ddNaiss"] = ddNaiss
    context.user_data["age"] = age
    await update.message.reply_text("Entrez la durée mensuelle du prêt (en mois, ex: 12, 24, 360) :")
    return DUREE_PRET

async def saisie_duree_pret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        duree = int(text)
    except Exception:
        await update.message.reply_text("Durée invalide. Entrez un entier (durée en mois, ex: 12, 24, 360).")
        return DUREE_PRET

    age = context.user_data.get("age")
    # vérifier que la colonne existe
    if duree not in df_emp.columns:
        await update.message.reply_text(
            f"Aucun taux trouvé pour une durée de {duree} mois. Vérifiez la durée ou contactez un conseiller."
        )
        return await back_to_menu(update, context)

    context.user_data["dureePret"] = duree
    await update.message.reply_text("Entrez le capital emprunté (ex: 5000000) :")
    return CAP_PRET

async def saisie_cap_pret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if text == "/menu":
        return await back_to_menu(update, context)
    try:
        capPret = float(text)
    except Exception:
        await update.message.reply_text("Capital invalide. Entrez un nombre (ex : 5000000).")
        return CAP_PRET

    age = context.user_data.get("age")
    duree = context.user_data.get("dureePret")

    tauxPrime = get_emp_taux(age, duree)
    if tauxPrime is None:
        await update.message.reply_text("Désolé, aucun taux trouvé pour vos paramètres. Rendez-vous chez SUNU pour la prise en charge de votre requête.")
        return await back_to_menu(update, context)

    prime = tauxPrime * capPret
    if prime == 0:
        await update.message.reply_text("Rendez-vous chez SUNU pour la prise en charge de votre requête.", reply_markup=MENU_KEYBOARD)
    else:
        await update.message.reply_text(f"✅ La prime unique est de : {prime:,.2f} Fcfa.")

    # Préparer récapitulatif
    context.user_data["last_recap"] = {
        "product": "Emprunteur",
        "title": "Emprunteur - Récapitulatif",
        "inputs": {
            "Année de naissance": context.user_data.get("ddNaiss"),
            "Âge": age,
            "Durée (mois)": duree,
            "Capital emprunté": capPret,
        },
        "results": {
            "TauxPrime": tauxPrime,
            "Prime unique": f"{prime:,.2f}",
        },
    }

    return await ask_pdf_and_store(update, context)

# ----- Cancel -----
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Opération annulée.", reply_markup=MENU_KEYBOARD)
    return PRODUIT

# -------------------------
# PDF utilities
# -------------------------

def generate_pdf_bytes(recap: dict) -> bytes:
    """Génère un PDF en mémoire (bytes) à partir du récapitulatif fourni.
    recap doit contenir : product (str), title (str), inputs (dict), results (dict)
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Logo (en haut à gauche) si présent
    if os.path.exists("Logo_sunu.jpg"):
        try:
            pdf.image("Logo_sunu.jpg", x=10, y=8, w=30)
        except Exception:
            logger.warning("Impossible d'insérer Logo_sunu.jpg dans le PDF (format/police).")

    # Titre
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, f"Simulation - {recap.get('product', '')}", ln=1, align="C")
    pdf.ln(6)

    # Informations saisies
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Informations saisies :", ln=1)
    pdf.set_font("Arial", size=11)
    inputs = recap.get("inputs", {})
    for k, v in inputs.items():
        pdf.multi_cell(0, 7, f"- {k}: {v}")

    pdf.ln(3)

    # Résultats (personnalisation légère selon produit)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Résultats :", ln=1)
    pdf.set_font("Arial", size=11)
    results = recap.get("results", {})
    for k, v in results.items():
        pdf.multi_cell(0, 7, f"- {k}: {v}")

    pdf.ln(6)
    pdf.set_font("Arial", "I", 9)
    pdf.cell(0, 5, "Généré le: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ln=1, align="R")

    out = pdf.output(dest='S')
    if isinstance(out, str):
        return out.encode('latin-1')
    return out


async def ask_pdf_and_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pose la question Oui/Non pour envoyer le PDF."""
    keyboard = ReplyKeyboardMarkup([["Oui", "Non"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Souhaitez-vous recevoir un PDF récapitulatif de cette simulation ? (Oui / Non)",
        reply_markup=keyboard,
    )
    return ASK_PDF


async def handle_pdf_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()

    if txt in ("oui", "o", "yes", "y"):
        recap = context.user_data.get("last_recap")
        if not recap:
            await update.message.reply_text("Aucune donnée disponible pour générer un PDF.", reply_markup=MENU_KEYBOARD)
            return await back_to_menu(update, context)

        pdf_bytes = generate_pdf_bytes(recap)
        bio = io.BytesIO(pdf_bytes)
        bio.name = f"simulation_{recap.get('product','simulation')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        bio.seek(0)

        try:
            # Envoi du document (Telegram gère le téléchargement)
            await update.message.reply_document(document=InputFile(bio, filename=bio.name))
        except Exception as e:
            logger.exception("Erreur en envoyant le PDF : %s", e)
            await update.message.reply_text("Erreur lors de l'envoi du PDF.")

        # Cleanup : on supprime le récapitulatif si vous souhaitez ne pas le garder
        context.user_data.pop("last_recap", None)

        return await back_to_menu(update, context)

    # si non -> retour au menu sans envoi
    return await back_to_menu(update, context)

# -------------------------
# Lancer le bot
# -------------------------
def main():
    token = os.getenv("TELEGRAM_TOKEN", "8484290771:AAGiLz1F20DegARHyx2-xVV5OlyOLVUfipA")
    if token == "8484290771:AAGiLz1F20DegARHyx2-xVV5OlyOLVUfipA":
        logger.warning("Vous utilisez la valeur par défaut pour le token. Remplacez-la par votre token ou définissez TELEGRAM_TOKEN.")

    application = Application.builder().token(token).build()

    # ConversationHandler with multiple entry points (commands) so we can start any parcours at any time
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("menu", start),
            CommandHandler("assur", start_assur),
            CommandHandler("assureducation", start_assur),
            CommandHandler("ibekelia", start_ibekelia),
            CommandHandler("fer", start_fer),
            CommandHandler("emprunteur", start_emprunteur),
            CommandHandler("selection", start_selection),
        ],
        states={
            PRODUIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, choix_produit)],
            # Assur'Education states (inchangés)
            TYPCOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, choix_typcot)],
            DNAISS: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_ddnaiss)],
            DUREE: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_duree)],
            NBRENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_nb_rente)],
            MONTANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_montant)],
            # IBEKELIA states (inchangés)
            DNAISS_I: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_ddnaiss_i)],
            PERIODE_I: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_periode)],
            CAPOBSQ_I: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_capobsq)],
            # FER+ states (nouveau)
            FER_CHOIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, fer_choix)],
            FER_DUREE: [MessageHandler(filters.TEXT & ~filters.COMMAND, fer_duree)],
            FER_MONTANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, fer_montant)],
            # EMPRUNTEUR states (nouveau)
            DNAISS_E: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_ddnaiss_e)],
            DUREE_PRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_duree_pret)],
            CAP_PRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, saisie_cap_pret)],
            # ASK PDF
            ASK_PDF: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pdf_choice)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)

    logger.info("Bot démarré. En attente de messages...")
    application.run_polling()


if __name__ == "__main__":
    main()
