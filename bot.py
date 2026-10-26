"""Sniper Vinted -> Telegram : consoles & Pokémon TCG en direct."""
import asyncio
import html
import json
import logging
import os
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler)
from telegram.ext import filters as tgf

import filters as F
from store import Store
from vinted import Blocked, VintedClient, checkout_url, item_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_TOKEN"]
RATE = float(os.getenv("RATE", "2.5"))                    # requêtes Vinted par seconde
MARKET_EVERY = int(os.getenv("MARKET_EVERY", "60"))       # relevé du marché toutes les X s par recherche

store = Store()
client = VintedClient()
esc = html.escape


# ======================= Mise en forme des alertes =======================
def ago(ts):
    d = max(0, int(time.time() - (ts or time.time())))
    if d < 60:
        return f"{d} s"
    if d < 3600:
        return f"{d // 60} min"
    return f"{d // 3600} h"


def key_label(key: str) -> str:
    if key == "base":
        return "modèle de base"
    return key.replace("|+lot", " + lot").replace("|", " ").replace("+lot", "lot")


def market_line(a: dict) -> str:
    s = a["search"]
    if s.get("ref"):
        return f"📊 Prix de référence (fixé par toi) : {s['ref']:.0f} €"
    if a["ref"]:
        diff = round((a["price"] / a["ref"] - 1) * 100)
        sign = "−" if diff < 0 else "+"
        return (f"📊 Marché ~{a['ref']:.0f} € ({a['n']} annonces « {esc(key_label(a['key']))} ») "
                f"→ {sign}{abs(diff)} %")
    return (f"📊 Marché en apprentissage ({a['n']}/{F.MIN_COMPARABLES} annonces "
            f"« {esc(key_label(a['key']))} »)")


def build_caption(a: dict) -> str:
    it, s = a["item"], a["search"]
    lines = []
    if a.get("deal"):
        lines.append(f"🔥 <b>BONNE AFFAIRE −{a['deal']} %</b>")
    icon = "🃏" if s["kind"] == "tcg" else "🎮"
    lines.append(f"{icon} <i>{esc(s['name'])}</i>")
    lines.append(f"<b>{esc(it.get('title', ''))}</b>")
    price_line = f"💶 <b>{a['price']:.2f} €</b>"
    if a.get("total") and a["total"] > a["price"]:
        price_line += f"  ·  {a['total']:.2f} € avec protection"
    lines.append(price_line)
    lines.append(market_line(a))
    infos = [x for x in (it.get("status"), it.get("brand_title"), it.get("size_title")) if x]
    if infos:
        lines.append("📦 " + esc(" · ".join(infos)))
    user = (it.get("user") or {}).get("login", "?")
    lines.append(f"👤 {esc(user)}  ·  ❤️ {it.get('favourite_count', 0)}  ·  ⏱ repérée il y a {ago(a.get('ts'))}")
    warns = a.get("warnings") or []
    if warns:
        lines.append("⚠️ <b>Attention :</b> " + esc(", ".join(warns)))
    head = "\n".join(lines)

    desc = a.get("desc")
    if desc is None:
        body = "📝 <i>chargement de la description…</i>"
    elif not desc:
        body = "📝 <i>pas de description</i>"
    else:
        room = 1000 - len(head) - 20
        d = desc if len(desc) <= room else desc[: max(room - 1, 0)].rstrip() + "…"
        body = "📝 " + esc(d)
        a["truncated"] = len(desc) > room
    return head + "\n\n" + body


def keyboard(a: dict) -> InlineKeyboardMarkup:
    iid = a["item"]["id"]
    rows = [[InlineKeyboardButton(f"🛒 ACHETER — {a['price']:.0f} €", url=checkout_url(iid))],
            [InlineKeyboardButton("🔗 Voir l'annonce", url=item_url(a["item"]))]]
    if a.get("truncated"):
        rows[1].append(InlineKeyboardButton("📝 Tout lire", callback_data=f"full:{iid}"))
    return InlineKeyboardMarkup(rows)


# ======================= Moteur de surveillance =======================
class Engine:
    def __init__(self, app: Application):
        self.app = app
        self.due = {}           # prochaine vérification "nouvelles annonces"
        self.mdue = {}          # prochain relevé du marché
        self.primed = set()
        self.last_req = 0.0
        self.backoff = 0
        self.alerts = {}
        self.sent = 0
        self.started = time.time()
        self.last_error = ""
        self.cycle = {}

    async def run(self):
        while True:
            try:
                active = [s for s in store.searches if s["on"]]
                if store.get("paused") or not store.get("owner") or not active:
                    await asyncio.sleep(1)
                    continue
                now = time.monotonic()
                gap = self.last_req + 1 / RATE - now
                if gap > 0:
                    await asyncio.sleep(min(gap, 0.5))
                    continue
                s = min(active, key=lambda x: self.due.get(x["id"], 0))
                m = min(active, key=lambda x: self.mdue.get(x["id"], 0))
                if self.mdue.get(m["id"], 0) <= now and m["id"] not in self.mdue:
                    job = ("market", m)          # nouvelle recherche : on relève le marché d'abord
                elif self.due.get(s["id"], 0) <= now:
                    job = ("new", s)
                elif self.mdue.get(m["id"], 0) <= now:
                    job = ("market", m)
                else:
                    nxt = min(self.due.get(s["id"], 0), self.mdue.get(m["id"], 0))
                    await asyncio.sleep(min(max(nxt - now, 0.05), 0.5))
                    continue
                self.last_req = time.monotonic()
                kind, target = job
                if kind == "market":
                    self.mdue[target["id"]] = self.last_req + MARKET_EVERY
                    await self.poll_market(target)
                else:
                    weight = sum(2 if x.get("prio") else 1 for x in active)
                    interval = weight / RATE * 1.15 / (2 if target.get("prio") else 1)
                    self.due[target["id"]] = self.last_req + interval
                    self.cycle[target["id"]] = interval
                    await self.poll_new(target)
                self.backoff = 0
            except Blocked as e:
                self.backoff = min(max(self.backoff * 2, 15), 300)
                self.last_error = str(e)
                log.warning("%s — pause %ss", e, self.backoff)
                if self.backoff >= 60:
                    await self.notify(f"⚠️ Vinted limite les requêtes (HTTP {e.status}). Reprise auto dans {self.backoff} s.")
                await asyncio.sleep(self.backoff)
                client.cookie_time = 0
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Erreur boucle")
                await asyncio.sleep(2)

    async def notify(self, text):
        try:
            await self.app.bot.send_message(store.get("owner"), text)
        except Exception:
            pass

    # --- relevé du marché : toutes les annonces du produit, sans ton prix max ---
    def record_market(self, s, it, price):
        title = it.get("title", "")
        if price <= 1 or F.excluded(title, s.get("exclude", [])):
            return
        if not F.matches_query(title, s["query"]):
            return
        if s["kind"] != "console" and F.is_tcg_junk(title):
            return
        if s["kind"] == "console" and F.is_unwanted_console(title):
            return
        if F.find_warnings(title, s["kind"]):
            return  # une console HS ne doit pas faire baisser le prix de référence
        store.add_market(s["id"], it.get("id"), price, F.model_key(title))

    async def poll_market(self, s):
        items = await client.search(s["query"], per_page=96)
        for it in items:
            price = F.to_float(it.get("price"))
            if price is not None and it.get("id"):
                self.record_market(s, it, price)

    # --- nouvelles annonces sous ton prix max ---
    async def poll_new(self, s):
        items = await client.search(s["query"], s.get("min"), s.get("max"))
        first = s["id"] not in self.primed
        for it in reversed(items):
            iid = it.get("id")
            if not iid or iid in store.seen:
                continue
            store.mark_seen(iid)
            price = F.to_float(it.get("price"))
            if price is None or not self.wanted(s, it, price):
                continue
            if first:
                self.record_market(s, it, price)
                continue  # 1er passage : on mémorise sans alerter
            ts = ((it.get("photo") or {}).get("high_resolution") or {}).get("timestamp") or time.time()
            await self.alert(s, it, price, ts)
            self.record_market(s, it, price)
        self.primed.add(s["id"])

    def wanted(self, s, it, price):
        title = it.get("title", "")
        if not F.matches_query(title, s["query"]):
            return False
        if s["kind"] != "console" and F.is_tcg_junk(title):
            return False
        if s.get("min") and price < s["min"]:
            return False
        if s.get("max") and price > s["max"]:
            return False
        if F.excluded(title, s.get("exclude", [])):
            return False
        if s["kind"] == "console" and F.is_unwanted_console(title):
            return False
        return True

    async def alert(self, s, it, price, ts):
        title = it.get("title", "")
        key = F.model_key(title)
        comparables = store.market_prices(s["id"], key)
        ref = float(s["ref"]) if s.get("ref") else F.market_price(comparables)
        warnings = F.find_warnings(title, s["kind"])
        deal = 0
        if ref and not warnings and price <= ref * (1 - store.get("threshold") / 100):
            deal = round((1 - price / ref) * 100)
        if s.get("deals_only") and not deal:
            return
        a = {"search": s, "item": it, "price": price, "ts": ts, "ref": ref, "deal": deal,
             "key": key, "n": len(comparables), "warnings": warnings, "desc": None,
             "total": F.to_float(it.get("total_item_price"))}
        chat = store.get("owner")
        photo = (it.get("photo") or {}).get("url")
        try:
            if not photo:
                raise ValueError("pas de photo")
            msg = await self.app.bot.send_photo(chat, photo, caption=build_caption(a),
                                                parse_mode="HTML", reply_markup=keyboard(a))
        except Exception:
            msg = await self.app.bot.send_message(chat, build_caption(a), parse_mode="HTML",
                                                  reply_markup=keyboard(a),
                                                  disable_web_page_preview=True)
        a["msg"] = msg
        self.sent += 1
        self.alerts[it["id"]] = a
        if len(self.alerts) > 500:
            self.alerts.pop(next(iter(self.alerts)))
        asyncio.create_task(self.fill_description(a))

    async def fill_description(self, a):
        try:
            desc = await client.description(item_url(a["item"]))
        except Exception:
            desc = ""
        a["desc"] = desc
        extra = F.find_warnings(desc, a["search"]["kind"])
        if extra:
            a["warnings"] = sorted(set(a["warnings"]) | set(extra))
            a["deal"] = 0  # panne / fake dans la description : ce n'est plus une affaire
        try:
            if a["msg"].photo:
                await a["msg"].edit_caption(build_caption(a), parse_mode="HTML", reply_markup=keyboard(a))
            else:
                await a["msg"].edit_text(build_caption(a), parse_mode="HTML", reply_markup=keyboard(a),
                                         disable_web_page_preview=True)
        except Exception as e:
            log.warning("Édition impossible: %s", e)


# ======================= Interface Telegram =======================
HELP = (
    "🎯 <b>Sniper Vinted</b> — les nouvelles annonces arrivent ici en direct.\n\n"
    "1️⃣ Touche <b>➕ Ajouter une recherche</b> : ce que tu cherches, ton prix max, et si tu veux "
    "toutes les annonces ou seulement les bonnes affaires.\n"
    "2️⃣ Sous chaque alerte : <b>🛒 ACHETER</b> = page de paiement directe.\n\n"
    "<b>🔥 Comment le bot repère une affaire</b>\n"
    "Il relève en continu le prix de <i>toutes</i> les annonces du produit (même au-dessus de ton max) "
    "et ne compare que les annonces équivalentes : une DSi avec des DSi, une DSi XL avec des DSi XL, "
    "une console seule ≠ un lot avec jeux. Les annonces HS / fake sont écartées. "
    "Dès {n} annonces comparables, une annonce {t} % sous le prix du marché = 🔥.\n\n"
    "<b>Commandes</b>\n"
    "/liste — tes recherches (✅/⏸ activer · 📬/🔥 toutes ou seulement affaires · 🗑 supprimer)\n"
    "/marche — les prix du marché que le bot a appris\n"
    "/max <i>n° prix</i> · /min <i>n° prix</i> — changer un prix\n"
    "/seuil <i>%</i> — écart pour un 🔥 (actuel : {t} %)\n"
    "/pause · /reprendre · /statut\n\n"
    "Mode rapide : <code>/ajouter nintendo dsi max 30 -coque</code>"
)


def owner_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat.id
        if store.get("owner") is None:
            store.set("owner", chat)
        if chat != store.get("owner"):
            return
        return await fn(update, ctx)
    return wrap


def search_label(i, s):
    price = f" ≤{s['max']:.0f}€" if s.get("max") else ""
    return f"{'✅' if s['on'] else '⏸'} {i}. {s['name']}{price}"


def list_markup():
    rows = [[InlineKeyboardButton(search_label(i, s), callback_data=f"tog:{s['id']}"),
             InlineKeyboardButton("🔥" if s.get("deals_only") else "📬", callback_data=f"mod:{s['id']}"),
             InlineKeyboardButton("🗑", callback_data=f"del:{s['id']}")]
            for i, s in enumerate(store.searches, 1)]
    rows.append([InlineKeyboardButton("➕ Ajouter une recherche", callback_data="add")])
    return InlineKeyboardMarkup(rows)


LIST_TITLE = ("Tes recherches :\n✅/⏸ = active ou en pause · 📬 = toutes les annonces · "
              "🔥 = seulement les bonnes affaires")


def by_number(ctx):
    try:
        return store.searches[int(ctx.args[0]) - 1]
    except (IndexError, ValueError):
        return None


def parse_price(text):
    try:
        v = float(text.strip().lower().replace("€", "").replace("eur", "").replace(",", ".").strip())
        return v if v > 0 else None
    except ValueError:
        return None


CANCEL = InlineKeyboardButton("✖️ Annuler", callback_data="cancel")


async def ask_query(message, ctx):
    ctx.user_data["add"] = {"step": "query"}
    await message.reply_text("🔎 Tu cherches quoi ?\nEx : nintendo dsi · 3ds xl · display pokemon 151",
                             reply_markup=InlineKeyboardMarkup([[CANCEL]]))


async def ask_mode(message, ctx):
    d = ctx.user_data["add"]
    d["step"] = "mode"
    lim = f"≤ {d['max']:.0f} €" if d.get("max") else "à tous les prix"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📬 Toutes les annonces {lim}", callback_data="mode:all")],
        [InlineKeyboardButton("🔥 Seulement les bonnes affaires", callback_data="mode:deals")],
        [CANCEL]])
    await message.reply_text("Tu veux recevoir quoi ?", reply_markup=kb)


async def finish_add(message, ctx, deals_only):
    d = ctx.user_data.pop("add")
    s = store.add_search({"query": d["query"], "kind": F.guess_kind(d["query"]),
                          "max": d.get("max"), "deals_only": deals_only})
    lim = f"max {s['max']:.0f} €" if s.get("max") else "sans prix max"
    mode = "seulement les bonnes affaires 🔥" if deals_only else "toutes les annonces 📬"
    await message.reply_text(
        f"✅ Recherche ajoutée : {s['name']} ({lim}) — {mode}\n\n"
        f"Je relève d'abord le prix du marché. Les 🔥 s'activent dès {F.MIN_COMPARABLES} annonces "
        f"comparables (souvent en quelques minutes).")
    await message.reply_text(LIST_TITLE, reply_markup=list_markup())


@owner_only
async def cmd_start(update, ctx):
    await update.message.reply_html(HELP.format(t=store.get("threshold"), n=F.MIN_COMPARABLES))
    if not store.searches:
        await update.message.reply_text("Tu n'as encore aucune recherche.",
                                        reply_markup=InlineKeyboardMarkup(
                                            [[InlineKeyboardButton("➕ Ajouter une recherche", callback_data="add")]]))
    else:
        await update.message.reply_text(LIST_TITLE, reply_markup=list_markup())


@owner_only
async def cmd_list(update, ctx):
    await update.message.reply_text(LIST_TITLE, reply_markup=list_markup())


@owner_only
async def cmd_add(update, ctx):
    if not ctx.args:
        await ask_query(update.message, ctx)
        return
    d = F.parse_add(" ".join(ctx.args))
    if not d:
        await update.message.reply_html("Exemple : <code>/ajouter nintendo dsi max 30 -coque</code>")
        return
    s = store.add_search(d)
    txt = f"✅ Ajouté : <b>{esc(s['query'])}</b>"
    if s["min"] or s["max"]:
        txt += f"\n💶 {s['min'] or 0:.0f} € → {s['max'] or '∞'} €"
    if s["exclude"]:
        txt += "\n🚫 exclus : " + esc(", ".join(s["exclude"]))
    txt += "\n" + ("🔥 seulement les bonnes affaires" if s["deals_only"] else "📬 toutes les annonces")
    await update.message.reply_html(txt)


@owner_only
async def on_text(update, ctx):
    d = ctx.user_data.get("add")
    msg = update.message
    if not d:
        await msg.reply_text("Touche ➕ pour ajouter une recherche.", reply_markup=list_markup())
        return
    if d["step"] == "query":
        q = " ".join(msg.text.split())[:60]
        if len(q) < 2:
            await msg.reply_text("Écris le nom du produit, ex : nintendo dsi")
            return
        d["query"], d["step"] = q, "max"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Pas de prix max", callback_data="nomax")], [CANCEL]])
        await msg.reply_text(f"💶 Prix max pour « {q} » ?\nÉcris juste le nombre, ex : 30", reply_markup=kb)
    elif d["step"] == "max":
        v = parse_price(msg.text)
        if v is None:
            await msg.reply_text("Écris juste un nombre, ex : 30")
            return
        d["max"] = v
        await ask_mode(msg, ctx)
    else:
        await msg.reply_text("Choisis une des options ci-dessus 👆")


async def set_price(update, ctx, key):
    s = by_number(ctx)
    val = parse_price(ctx.args[1]) if len(ctx.args) > 1 else None
    if not s or (val is None and (len(ctx.args) < 2 or ctx.args[1] != "0")):
        await update.message.reply_text(f"Exemple : /{key} 3 120  (recherche n°3 → {key} 120 €, 0 = aucun)")
        return
    s[key] = val
    store.save()
    await update.message.reply_text(f"✅ {s['name']} : {key} = {f'{val:.0f} €' if val else 'aucun'}")


@owner_only
async def cmd_max(update, ctx):
    await set_price(update, ctx, "max")


@owner_only
async def cmd_min(update, ctx):
    await set_price(update, ctx, "min")


@owner_only
async def cmd_seuil(update, ctx):
    try:
        v = int(ctx.args[0].rstrip("%"))
        assert 5 <= v <= 90
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Exemple : /seuil 30  (🔥 si 30 % sous le prix du marché)")
        return
    store.set("threshold", v)
    await update.message.reply_text(f"✅ Bonne affaire = au moins {v} % sous le prix du marché")


@owner_only
async def cmd_market(update, ctx):
    if not store.searches:
        await update.message.reply_text("Aucune recherche pour l'instant.")
        return
    targets = [by_number(ctx)] if ctx.args and by_number(ctx) else store.searches
    blocks = []
    for s in targets:
        groups = {}
        for p, k, _ in store.data["market"].get(s["id"], {}).values():
            groups.setdefault(k, []).append(p)
        lines = [f"<b>{esc(s['name'])}</b> ({store.market_size(s['id'])} prix relevés)"]
        for k, ps in sorted(groups.items(), key=lambda x: -len(x[1]))[:6]:
            ref = F.market_price(ps)
            val = f"~{ref:.0f} €" if ref else f"apprentissage {len(ps)}/{F.MIN_COMPARABLES}"
            lines.append(f"  • {esc(key_label(k))} : {val} ({len(ps)})")
        if len(lines) == 1:
            lines.append("  • rien encore, patiente une minute")
        blocks.append("\n".join(lines))
    await update.message.reply_html("📊 <b>Prix du marché appris</b>\n\n" + "\n\n".join(blocks)[:3900])


@owner_only
async def cmd_pause(update, ctx):
    store.set("paused", True)
    await update.message.reply_text("⏸ Alertes en pause. /reprendre pour relancer.")


@owner_only
async def cmd_resume(update, ctx):
    store.set("paused", False)
    await update.message.reply_text("▶️ C'est reparti, je surveille.")


@owner_only
async def cmd_status(update, ctx):
    eng: Engine = ctx.application.bot_data["engine"]
    up = int(time.time() - eng.started)
    active = [s for s in store.searches if s["on"]]
    cyc = [eng.cycle[s["id"]] for s in active if s["id"] in eng.cycle]
    avg = f"{sum(cyc) / len(cyc):.1f} s" if cyc else "—"
    txt = (f"{'⏸ En pause' if store.get('paused') else '🟢 En marche'} depuis {up // 3600} h {up % 3600 // 60} min\n"
           f"🔎 {len(active)} recherches actives · chacune vérifiée toutes les ~{avg}\n"
           f"📨 {eng.sent} alertes envoyées · {client.requests} requêtes Vinted\n"
           f"🔥 Seuil bonne affaire : {store.get('threshold')} %")
    if eng.backoff:
        txt += f"\n⚠️ Ralenti par Vinted (pause {eng.backoff} s)"
    if eng.last_error:
        txt += f"\nDernière erreur : {eng.last_error[:150]}"
    await update.message.reply_text(txt)


@owner_only
async def cmd_debug(update, ctx):
    raw = client.last_raw
    if raw is None:
        await update.message.reply_text("Aucune réponse Vinted reçue pour l'instant.")
        return
    txt = json.dumps(raw, ensure_ascii=False, indent=1)[:3500]
    await update.message.reply_text("Exemple brut renvoyé par Vinted :\n" + txt)


@owner_only
async def on_button(update, ctx):
    q = update.callback_query
    kind, _, arg = q.data.partition(":")
    msg = q.message
    if kind == "full":
        a = ctx.application.bot_data["engine"].alerts.get(int(arg))
        await q.answer()
        if a and a.get("desc"):
            await msg.reply_text(f"📝 {a['item'].get('title', '')}\n\n{a['desc'][:4000]}")
        return
    if kind == "add":
        await q.answer()
        await ask_query(msg, ctx)
        return
    if kind == "cancel":
        ctx.user_data.pop("add", None)
        await q.answer("Annulé")
        await msg.edit_reply_markup(None)
        return
    if kind in ("nomax", "mode"):
        d = ctx.user_data.get("add")
        await q.answer()
        if not d:
            return
        await msg.edit_reply_markup(None)
        if kind == "nomax" and d["step"] == "max":
            d["max"] = None
            await ask_mode(msg, ctx)
        elif kind == "mode" and d["step"] == "mode":
            await finish_add(msg, ctx, deals_only=(arg == "deals"))
        return
    s = store.find(arg)
    if not s:
        await q.answer("Recherche introuvable")
        return
    if kind == "tog":
        s["on"] = not s["on"]
        store.save()
        await q.answer("Activée ✅" if s["on"] else "En pause ⏸")
    elif kind == "mod":
        s["deals_only"] = not s.get("deals_only")
        store.save()
        await q.answer("🔥 Seulement les bonnes affaires" if s["deals_only"] else "📬 Toutes les annonces")
    elif kind == "del":
        store.remove_search(s)
        await q.answer(f"Supprimée : {s['name']}")
    await q.edit_message_reply_markup(list_markup())


# ======================= Lancement =======================
async def autosave():
    while True:
        await asyncio.sleep(15)
        if store.dirty:
            store.save()


async def post_init(app: Application):
    engine = Engine(app)
    app.bot_data["engine"] = engine
    try:
        await app.bot.set_my_commands([
            BotCommand("liste", "Mes recherches"),
            BotCommand("ajouter", "Ajouter une recherche"),
            BotCommand("marche", "Prix du marché appris"),
            BotCommand("statut", "État du bot"),
            BotCommand("pause", "Mettre en pause"),
            BotCommand("reprendre", "Relancer"),
            BotCommand("seuil", "Seuil des bonnes affaires (%)"),
            BotCommand("max", "Prix max d'une recherche"),
            BotCommand("min", "Prix min d'une recherche"),
            BotCommand("aide", "Aide"),
        ])
    except Exception as e:
        log.warning("Menu des commandes non défini: %s", e)
    app.create_task(engine.run())
    app.create_task(autosave())
    if store.get("owner"):
        await engine.notify("🟢 Sniper Vinted démarré. /liste pour tes recherches.")


def main():
    app = (Application.builder().token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(40)
           .build())
    app.add_handler(CommandHandler(["start", "aide"], cmd_start))
    app.add_handler(CommandHandler("liste", cmd_list))
    app.add_handler(CommandHandler("ajouter", cmd_add))
    app.add_handler(CommandHandler("marche", cmd_market))
    app.add_handler(CommandHandler("max", cmd_max))
    app.add_handler(CommandHandler("min", cmd_min))
    app.add_handler(CommandHandler("seuil", cmd_seuil))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("reprendre", cmd_resume))
    app.add_handler(CommandHandler("statut", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(tgf.TEXT & ~tgf.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1, close_loop=False)


def run_forever():
    """Si Telegram ne répond pas au démarrage, on réessaie au lieu de crasher."""
    while True:
        try:
            main()
            return
        except Exception as e:
            log.error("Démarrage raté (%s), nouvel essai dans 10 s", e)
            time.sleep(10)


if __name__ == "__main__":
    run_forever()
