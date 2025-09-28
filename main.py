# bot_completed_with_emprunteur.py
import os
import logging
import pandas as pd
import datetime
from telegram import Update
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
# États de la conversation
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
) = range(18)

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
    df_emp = pd.read_excel("/mnt/data/tauxEmp.xlsx", sheet_name="tauxEmp")
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
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Bonjour {user.first_name or ''} !\n\n"
        "Vous souhaitez faire une cotation de :\n"
        "1- Assur'Education\n2- IBEKELIA\n3- FER+\n4- Autre produit\n5- Emprunteur\n6- Sélection Médical\n\n"
        "Répondez par 1, 2, 3, 4, 5 ou 6."
    )
    return PRODUIT

async def choix_produit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip()
    if choix == "1":
        # Parcours Assur'Education (inchangé)
        await update.message.reply_text(
            "Parcours Assur'Education :\n"
            "1- Prestation définie ?\n"
            "2- Cotisation définie ?\n\n"
            "Répondez 1 ou 2."
        )
        return TYPCOT
    elif choix == "2":
        # Parcours IBEKELIA (inchangé)
        await update.message.reply_text("Parcours IBEKELIA :\nEntrez votre année de naissance (AAAA) :")
        return DNAISS_I
    elif choix == "3":
        # Parcours FER+
        await update.message.reply_text(
            "Parcours FER+ :\n\n"
            "Choisissez votre capacité d'épargne (répondez A..H) :\n\n"
            "A - 10 000 (épargne) - 2 000 (décès) - 12 000 (total) - CapDéc 2 000 000\n"
            "B - 20 000 - 4 000 - 24 000 - 4 000 000\n"
            "C - 30 000 - 6 000 - 36 000 - 6 000 000\n"
            "D - 40 000 - 8 000 - 48 000 - 8 000 000\n"
            "E - 60 000 - 12 000 - 72 000 - 12 000 000\n"
            "F - 80 000 - 16 000 - 96 000 - 16 000 000\n"
            "G - 100 000 - 20 000 - 120 000 - 20 000 000\n"
            "H - Je peux cotiser plus de 120 000 par mois (saisie libre)"
        )
        return FER_CHOIX
    elif choix == "5":
        await update.message.reply_text("Parcours EMPRUNTEUR :\nEntrez votre année de naissance (AAAA) :")
        return DNAISS_E
    elif choix == "6":
        await update.message.reply_text("Parcours SÉLECTION MÉDICAL :\nModule en cours de développement…")
        return ConversationHandler.END
    else:
        # Autre produit / construction (conserve le comportement précédent)
        await update.message.reply_text("Parcours en construction…")
        return ConversationHandler.END

# -------------------------
# Assur'Education handlers (identiques)
# -------------------------
async def choix_typcot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt not in ("1", "2"):
        await update.message.reply_text("Choix invalide. Répondez 1 (Prestation) ou 2 (Cotisation).")
        return TYPCOT
    context.user_data["typCot"] = int(txt)
    await update.message.reply_text("Entrez votre année de naissance (AAAA) :")
    return DNAISS

async def saisie_ddnaiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
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
        return ConversationHandler.END

    if typCot == 1:
        mtRente = montant
        cotisation_mensuelle = taux * mtRente
        await update.message.reply_text(
            f"✅ Votre bénéficiaire pourra jouir d'une rente annuelle de : {mtRente:,.2f}\n"
            f"pendant {nb_rente} années contre une cotisation mensuelle de {cotisation_mensuelle:,.2f}."
        )
    else:
        mtCot = montant
        rente_annuelle = mtCot / taux
        await update.message.reply_text(
            f"✅ Avec une cotisation mensuelle de {mtCot:,.2f},\n"
            f"votre bénéficiaire pourra bénéficier d'une rente annuelle de : {rente_annuelle:,.2f}\n"
            f"pendant {nb_rente} années."
        )

    return ConversationHandler.END

# ----- IBEKELIA (identique) -----
async def saisie_ddnaiss_i(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
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
        "U - pour unique"
    )
    return PERIODE_I

async def saisie_periode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    per = update.message.text.strip().upper()
    if per not in ("M", "A", "U"):
        await update.message.reply_text("Périodicité invalide. Répondez M, A ou U.")
        return PERIODE_I
    context.user_data["perCot"] = per
    await update.message.reply_text(
        "Entrez le capital d'assistance obsèques souhaité !\n"
        "1- 1 000 000\n2- 2 000 000\n3- 3 000 000\n4- 4 000 000\n5- 5 000 000"
    )
    return CAPOBSQ_I

async def saisie_capobsq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip()
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
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ Pour une cotisation {per_cot} de {prime:,.2f},\n"
        f"vous garantissez à vos proches un capital de {cap_obsq:,.0f}.\n"
        "Vous les libérez ainsi des soucis financiers et organisationnels liés à vos obsèques, en toute sérénité."
    )
    return ConversationHandler.END

# ----- FER+ handlers (nouveau parcours 3) -----
async def fer_choix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip().upper()
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
            return ConversationHandler.END

        cotMensEp = float(grille["cotMensEp"])
        cotMensPrev = float(grille["cotMensPrev"])
        cotMensTot = float(grille["cotMensTot"])
        capDec = float(grille["capDec"])
        tauxP = context.user_data["fer_tauxP"]
        # calcul
        capAcquis = tauxP * cotMensEp

        await update.message.reply_text(
            f"✅ Pour une cotisation mensuelle de {cotMensTot:,.0f} dont {cotMensEp:,.0f} de prime épargne "
            f"et {cotMensPrev:,.0f} de prime décès pendant {duree} ans, il est garanti :\n\n"
            f"- un capital acquis de {capAcquis:,.2f} en cas de vie au terme du contrat ;\n"
            f"- un capital décès de {capDec:,.0f} + la valeur de l'épargne constituée en cas de décès avant terme."
        )
        return ConversationHandler.END

async def fer_montant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
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
    return ConversationHandler.END

# ----- EMPRUNTEUR handlers (nouveau) -----
async def saisie_ddnaiss_e(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
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
        return ConversationHandler.END

    context.user_data["ddNaiss"] = ddNaiss
    context.user_data["age"] = age
    await update.message.reply_text("Entrez la durée mensuelle du prêt (en mois, ex: 12, 24, 360) :")
    return DUREE_PRET

async def saisie_duree_pret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
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
        return ConversationHandler.END

    context.user_data["dureePret"] = duree
    await update.message.reply_text("Entrez le capital emprunté (ex: 5000000) :")
    return CAP_PRET

async def saisie_cap_pret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
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
        return ConversationHandler.END

    prime = tauxPrime * capPret
    if prime == 0:
        await update.message.reply_text("Rendez-vous chez SUNU pour la prise en charge de votre requête.")
    else:
        await update.message.reply_text(f"✅ La prime unique est de : {prime:,.2f} Fcfa.")

    return ConversationHandler.END

# ----- Cancel -----
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Opération annulée. Tapez /start pour recommencer.")
    return ConversationHandler.END

# -------------------------
# Lancer le bot
# -------------------------
def main():
    token = os.getenv("TELEGRAM_TOKEN", "8484290771:AAGiLz1F20DegARHyx2-xVV5OlyOLVUfipA")
    if token == "8484290771:AAGiLz1F20DegARHyx2-xVV5OlyOLVUfipA":
        logger.warning("Vous utilisez la valeur par défaut pour le token. Remplacez-la par votre token ou définissez TELEGRAM_TOKEN.")

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    logger.info("Bot démarré. En attente de messages...")
    application.run_polling()

if __name__ == "__main__":
    main()
