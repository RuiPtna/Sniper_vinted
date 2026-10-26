"""Filtres, détection d'arnaques/pannes et parsing des commandes."""
import re
import statistics
import unicodedata


def norm(text: str) -> str:
    """minuscules, sans accents, ponctuation -> espaces, bordé d'espaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    return f" {text.strip()} "


def has_any(t: str, words) -> list:
    return [w for w in words if f" {w} " in t]


# ---------- Consoles : exclure les jeux seuls et accessoires seuls ----------
GAME_WORDS = ["jeu", "jeux", "cartouche", "cartouches", "game", "games"]
ACCESSORY_WORDS = [
    "coque", "housse", "etui", "chargeur", "cable", "stylet", "stylets",
    "boite vide", "boite seule", "boitier", "notice", "protection", "skin",
    "sacoche", "pochette", "manette", "support", "dock", "alimentation",
    "batterie", "nappe", "piece detachee", "pieces detachees", "r4", "linker",
    "film", "verre trempe", "sticker", "stickers", "facade", "cover",
]
BUNDLE_MARKERS = ["console", "avec", "pack", "bundle"]


def is_unwanted_console(title: str) -> bool:
    t = norm(title)
    t = re.sub(r" game ?(boy|cube|gear) ", r" game\1 ", t)  # "Game Boy" n'est pas un jeu
    if re.match(r"^ (jeu|jeux|cartouche|cartouches|lot de jeux|lot jeux) ", t):
        return True
    if "+" in t or has_any(t, BUNDLE_MARKERS):
        return False  # console + jeux / accessoires -> on garde
    return bool(has_any(t, GAME_WORDS) or has_any(t, ACCESSORY_WORDS))


# ---------- Mots qui doivent t'alerter (panne, contrefaçon...) ----------
WARN_CONSOLE = [
    "hs", "h s", "en panne", "panne", "pour piece", "pour pieces",
    "ne s allume", "ne s allume pas", "ne fonctionne", "fonctionne pas",
    "defectueux", "defectueuse", "bloque", "bloquee", "ecran casse",
    "ecran fissure", "fissure", "fissuree", "charniere cassee", "pixel mort",
    "pixels morts", "a reparer", "reparation", "bug", "sans ecran", "casse",
    "cassee", "replique", "copie", "contrefacon", "fausse", "faux",
]
WARN_TCG = [
    "proxy", "fake", "replique", "contrefacon", "custom", "reproduction",
    "copie", "ouvert", "ouverte", "sans blister", "pese", "pesee", "repack",
    "vide", "faux", "fausse",
]


def find_warnings(text: str, kind: str) -> list:
    words = WARN_TCG if kind == "tcg" else WARN_CONSOLE
    found = has_any(norm(text), words)
    # dédoublonne les variantes ("ne s allume" / "ne s allume pas")
    out = []
    for w in sorted(found, key=len, reverse=True):
        if not any(w in o for o in out):
            out.append(w)
    return out


def excluded(title: str, exclude_words) -> bool:
    t = norm(title)
    return any(f" {norm(w).strip()} " in t for w in exclude_words if w.strip())


# ---------- Prix ----------
def to_float(v):
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ---------- /ajouter ----------
CONSOLE_HINTS = ["ds", "dsi", "3ds", "2ds", "game boy", "gameboy", "gba",
                 "ps5", "playstation", "portal", "switch", "psp", "console",
                 "nintendo", "xbox", "wii"]
TCG_HINTS = ["pokemon", "display", "etb", "booster", "coffret", "carte",
             "cartes", "tcg", "yu gi oh", "one piece", "dracaufeu"]


def guess_kind(query: str) -> str:
    t = norm(query)
    if has_any(t, TCG_HINTS):
        return "tcg"
    if has_any(t, CONSOLE_HINTS):
        return "console"
    return "autre"


def parse_add(text: str):
    """Ex: '3ds xl max 120 min 40 -coque -jeu prio bonsplans ref 150'"""
    s = " " + (text or "") + " "
    out = {"exclude": []}

    def grab(key):
        nonlocal s
        m = re.search(rf"\s{key}\s*(\d+(?:[.,]\d+)?)\s*€?(?=\s)", s, re.I)
        if m:
            s = s[:m.start()] + " " + s[m.end():]
            return float(m.group(1).replace(",", "."))
        return None

    out["max"] = grab("max")
    out["min"] = grab("min")
    out["ref"] = grab("ref")
    for m in re.findall(r"\s-(\S+)", s):
        out["exclude"].append(m.replace("_", " "))
    s = re.sub(r"\s-\S+", " ", s)
    m = re.search(r"\stype\s+(console|tcg|autre)\s", s, re.I)
    kind = None
    if m:
        kind = m.group(1).lower()
        s = s[:m.start()] + " " + s[m.end():]
    out["prio"] = bool(re.search(r"\sprio\s", s, re.I))
    out["deals_only"] = bool(re.search(r"\sbons?\s?plans?\s", s, re.I))
    s = re.sub(r"\s(prio|bons?\s?plans?)\s", " ", s, flags=re.I)
    query = re.sub(r"\s+", " ", s).strip()
    if not query:
        return None
    out["query"] = query
    out["kind"] = kind or guess_kind(query)
    return out


# ---------- Prix du marché : on ne compare que des annonces équivalentes ----------
SYNONYMS = [
    (r" playstation 5 ", " ps5 "), (r" play station 5 ", " ps5 "),
    (r" game ?boy ", " gb "), (r" gba ", " gb advance "), (r" gbc ", " gb color "),
    (r" 3 ds ", " 3ds "), (r" 2 ds ", " 2ds "), (r" ll ", " xl "),
    (r" digitale ", " digital "), (r" numerique ", " digital "),
    (r" coffret dresseur d elite ", " etb "), (r" elite trainer box ", " etb "),
    (r" tri pack ", " tripack "), (r" boosters ", " booster "),
]
# mots qui changent le modèle (donc le prix)
VARIANT_WORDS = ["ds", "dsi", "3ds", "2ds", "xl", "new", "lite", "slim", "digital",
                 "color", "advance", "sp", "pocket", "micro", "oled", "pro", "portal",
                 "display", "etb", "booster", "tripack", "blister", "upc", "tin",
                 "pokebox", "mini"]
BUNDLE_WORDS = ["jeu", "jeux", "lot", "avec", "pack", "bundle", "manettes", "cartes"]


def model_key(title: str) -> str:
    """Signature du modèle : 'dsi|xl' ≠ 'dsi', et console seule ≠ console + jeux."""
    t = norm(title)
    for pat, rep in SYNONYMS:
        t = re.sub(pat, rep, t)
    parts = set(has_any(t, VARIANT_WORDS))
    if parts & {"display", "etb", "tripack", "upc", "blister", "pokebox", "tin"}:
        parts.discard("booster")  # "display 36 boosters" = un display
    parts = sorted(parts)
    if "+" in t or has_any(t, BUNDLE_WORDS):
        parts.append("+lot")
    return "|".join(parts) or "base"


MIN_COMPARABLES = 20


def market_price(prices: list):
    """Médiane robuste (on enlève les 10 % extrêmes de chaque côté)."""
    if len(prices) < MIN_COMPARABLES:
        return None
    p = sorted(prices)
    cut = len(p) // 10
    p = p[cut: len(p) - cut] or p
    return statistics.median(p)


# ---------- Le titre doit contenir TOUS les mots de ta recherche ----------
# (Vinted renvoie aussi des annonces qui n'ont qu'un mot en commun : "30 ans" -> chaussettes...)
EXTRA_SYNONYMS = [
    (r" coffret dresseur d? ?elite ", " etb "), (r" dresseur d? ?elite ", " etb "),
    (r" elite trainer box ", " etb "), (r" trainer box ", " etb "),
]
# mots facultatifs : souvent absents du titre même quand c'est le bon produit
OPTIONAL_WORDS = {"nintendo", "console", "sony", "playstation",
                  "francais", "fr", "neuf", "scelle", "scellee", "de", "d", "la",
                  "le", "les", "du", "et", "en", "pour", "the", "edition"}


def _stem(w: str) -> str:
    if len(w) > 3 and w[-1] in "sx":
        return w[:-1]          # boosters -> booster, jeux -> jeu
    return w


def query_tokens(text: str) -> list:
    t = norm(text)
    for pat, rep in EXTRA_SYNONYMS + SYNONYMS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)   # "30ans" -> "30 ans" (des deux côtés)
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    return [_stem(w) for w in t.split() if w != "+"]


def matches_query(title: str, query: str) -> bool:
    need = [w for w in query_tokens(query) if w not in OPTIONAL_WORDS]
    have = set(query_tokens(title))
    return all(w in have for w in need)


# Pour le TCG : objets "à thème" qui ne sont pas des cartes / produits scellés
TCG_JUNK = ["chaussette", "chaussettes", "pull", "sweat", "t shirt", "tshirt", "tee shirt",
            "hoodie", "casquette", "bonnet", "pyjama", "ballon", "ballons", "gateau",
            "decoration", "deco", "guirlande", "bougie", "invitation", "peluche", "figurine",
            "lego", "costume", "deguisement", "mug", "tasse", "poster", "puzzle", "sac",
            "trousse", "montre", "lampe", "veilleuse", "coque", "tapis", "drap", "housse de couette"]


def is_tcg_junk(title: str) -> bool:
    return bool(has_any(norm(title), TCG_JUNK))


# ---------- Consoles : reconnaître un jeu même sans le mot "jeu" ----------
# ("Mario Kart DS", "Pokémon Platine DS"... : le titre contient des mots qui ne décrivent pas une console)
_CONSOLE_VOCAB = """
nintendo console ds dsi 3ds 2ds lite xl new gb gameboy color advance sp pocket micro ps 5 ps5
playstation sony portal remote player slim digital standard edition limitee limite collector pro oled
switch fat phat original origine modele version model
noir noire blanc blanche rose rouge bleu bleue vert verte gris grise argent argente or dore violet
violette orange jaune turquoise marine metallise metallisee mat brillant bordeaux corail menthe cosmos
aqua ice pearl perle crimson cobalt flame red blue black white pink silver gold purple green transparent
tres bon bonne bel etat neuf neuve comme parfait parfaite excellent fonctionne fonctionnel fonctionnelle
occasion propre testee teste complete complet boite boitier notice sans chargeur stylet cable housse etui
carte sd memoire go gb avec et en de du la le les un une pour plus region free pal fr francais francaise
japonaise jap us import rare vintage retro manette coque protection alimentation batterie lot pack bundle
console vendu vend vente top super quasi jamais servi utilise peu tbe be rayure rayures leger legere
"""
CONSOLE_VOCAB = {_stem(w) for w in _CONSOLE_VOCAB.split()}
GAME_DESC = ["jeu", "le jeu", "cartouche", "jeu complet", "jeu en", "jeu seul", "jeu fonctionne"]
CONSOLE_DESC = ["chargeur", "stylet", "console", "batterie", "s allume", "ecran", "charniere"]


def title_game_score(title: str, query: str) -> int:
    """Nombre de mots du titre qui ne décrivent pas une console (titre de jeu probable)."""
    q = set(query_tokens(query))
    unknown = [w for w in query_tokens(title)
               if w not in CONSOLE_VOCAB and w not in q and not w.isdigit() and len(w) > 1]
    return len(unknown)


def looks_like_game(title: str, query: str) -> bool:
    t = norm(title)
    if "+" in t or has_any(t, BUNDLE_MARKERS + ["lot"]):
        return False  # "console", "avec", "pack", "lot" -> c'est une console
    return title_game_score(title, query) >= 2


def desc_says_game(desc: str) -> bool:
    d = norm(desc)
    return bool(has_any(d, GAME_DESC)) and not has_any(d, CONSOLE_DESC)
