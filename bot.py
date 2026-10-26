"""Sniper Vinted -> Telegram : consoles & Pokémon TCG en direct."""
import asyncio
import html
import logging
import os
import time

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes)

import filters as F
from store import Store
from vinted import Blocked, VintedClient, checkout_url, item_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_TOKEN"]
RATE = float(os.getenv("RATE", "2.5"))          # requêtes Vinted par seconde
MAX_AGE = int(os.getenv("MAX_AGE", "3600"))      # ignore les annonces plus vieilles (s)

store = Store()
client = VintedClient()
esc = html.escape


# ======================= Mise en forme des alertes =======================
def ago(ts):
    if not ts:
        return ""
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d} s"
    if d < 3600:
        return f"{d // 60} min"
    return f"{d // 3600} h"


def build_caption(a: dict) -> str:
    it, s = a["item"], a["search"]
    lines = []
    if a.get("deal"):
        lines.append(f"🔥 <b>BON PLAN −{a['deal']}%</b> (prix habituel ~{a['ref']:.0f} €)")
    icon = "🃏" if s["kind"] == "tcg" else "🎮"
    lines.append(f"{icon} <i>{esc(s['name'])}</i>")
    lines.append(f"<b>{esc(it.get('title', ''))}</b>")
    price_line = f"💶 <b>{a['price']:.2f} €</b>"
    if a.get("total") and a["total"] > a["price"]:
        price_line += f"  ·  {a['total']:.2f} € avec protection"
    lines.append(price_line)
    infos = [x for x in (it.get("status"), it.get("brand_title"), it.get("size_title")) if x]
    if infos:
        lines.append("📦 " + esc(" · ".join(infos)))
    user = (it.get("user") or {}).get("login", "?")
    lines.append(f"👤 {esc(user)}  ·  ❤️ {it.get('favourite_count', 0)}  ·  ⏱ publiée il y a {ago(a.get('ts'))}")
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
        self.due = {}
        self.primed = set()
        self.last_req = 0.0
        self.backoff = 0
        self.alerts = {}        # item_id -> infos de l'alerte (pour édition / "Tout lire")
        self.sent = 0
        self.started = time.time()
        self.last_error = ""
        self.cycle = {}         # sid -> temps entre deux vérifications

    async def run(self):
        while True:
            try:
                owner = store.get("owner")
                active = [s for s in store.searches if s["on"]]
                if store.get("paused") or not owner or not active:
                    await asyncio.sleep(1)
                    continue
                now = time.monotonic()
                weight = sum(2 if s.get("prio") else 1 for s in active)
                base = weight / RATE
                s = min(active, key=lambda x: self.due.get(x["id"], 0))
                wait = max(self.due.get(s["id"], 0) - now, self.last_req + 1 / RATE - now)
                if wait > 0:
                    await asyncio.sleep(min(wait, 0.5))
                    continue
                self.last_req = time.monotonic()
                interval = base / 2 if s.get("prio") else base
                self.due[s["id"]] = self.last_req + interval
                self.cycle[s["id"]] = interval
                await self.poll(s)
                self.backoff = 0
            except Blocked as e:
                self.backoff = min(max(self.backoff * 2, 15), 300)
                self.last_error = str(e)
                log.warning("%s — pause %ss", e, self.backoff)
                if self.backoff >= 60:
                    await self.notify(f"⚠️ Vinted limite les requêtes (HTTP {e.status}). Reprise auto dans {self.backoff} s.")
                await asyncio.sleep(self.backoff)
                client.cookie_time = 0
            except Exception as e:  # on ne meurt jamais
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Erreur boucle")
                await asyncio.sleep(2)

    async def notify(self, text):
        try:
            await self.app.bot.send_message(store.get("owner"), text)
        except Exception:
            pass

    async def poll(self, s):
        items = await client.search(s["query"], s.get("min"), s.get("max"))
        first = s["id"] not in self.primed
        for it in reversed(items):  # du plus ancien au plus récent
            iid = it.get("id")
            if not iid or iid in store.seen:
                continue
            store.mark_seen(iid)
            price = F.to_float(it.get("price"))
            if price is None:
                continue
            if not self.wanted(s, it, price):
                continue
            store.add_price(s["id"], price)
            if first:
                continue  # 1er passage : on mémorise sans alerter
            ts = ((it.get("photo") or {}).get("high_resolution") or {}).get("timestamp")
            if ts and time.time() - ts > MAX_AGE:
                continue
            await self.alert(s, it, price, ts)
        self.primed.add(s["id"])

    def wanted(self, s, it, price):
        title = it.get("title", "")
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
        ref = F.reference_price(s, store.history(s["id"])[:-1])
        deal = 0
        if ref and price <= ref * (1 - store.get("threshold") / 100):
            deal = round((1 - price / ref) * 100)
        if s.get("deals_only") and not deal:
            return
        a = {"search": s, "item": it, "price": price, "ts": ts, "ref": ref, "deal": deal,
             "total": F.to_float(it.get("total_item_price")),
             "warnings": F.find_warnings(it.get("title", ""), s["kind"]), "desc": None}
        chat = store.get("owner")
        photo = (it.get("photo") or {}).get("url")
        try:
            if photo:
                msg = await self.app.bot.send_photo(chat, photo, caption=build_caption(a),
                                                    parse_mode="HTML", reply_markup=keyboard(a))
            else:
                raise ValueError("pas de photo")
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
        a["warnings"] = sorted(set(a["warnings"]) | set(extra))
        msg = a["msg"]
        try:
            if msg.photo:
                await msg.edit_caption(build_caption(a), parse_mode="HTML", reply_markup=keyboard(a))
            else:
                await msg.edit_text(build_caption(a), parse_mode="HTML", reply_markup=keyboard(a),
                                    disable_web_page_preview=True)
        except Exception as e:
            log.warning("Édition impossible: %s", e)


# ======================= Commandes Telegram =======================
HELP = (
    "🎯 <b>Sniper Vinted</b> — les nouvelles annonces arrivent ici en direct.\n\n"
    "Sous chaque alerte : <b>🛒 ACHETER</b> t'envoie direct sur la page de paiement.\n\n"
    "<b>Commandes</b>\n"
    "/liste — tes recherches (✅/⏸ pour activer, 🗑 pour supprimer)\n"
    "/ajouter <i>recherche</i> [max 120] [min 30] [-motexclu] [prio] [bonsplans] [ref 150]\n"
    "   ex : <code>/ajouter switch oled max 180 min 90 -coque prio</code>\n"
    "/max <i>n° prix</i> · /min <i>n° prix</i> — changer un prix\n"
    "/seuil <i>%</i> — écart pour un 🔥 bon plan (actuel : {t} %)\n"
    "/pause · /reprendre — couper / relancer les alertes\n"
    "/statut — état du bot\n\n"
    "<b>prio</b> = vérifiée 2× plus souvent · <b>bonsplans</b> = alerte seulement si prix cassé · "
    "<b>ref 150</b> = prix habituel fixé à la main"
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
    price = f"≤{s['max']:.0f}€" if s.get("max") else ""
    flag = "⚡" if s.get("prio") else ""
    return f"{'✅' if s['on'] else '⏸'} {i}. {s['name']} {price} {flag}".strip()


def list_markup():
    rows = [[InlineKeyboardButton(search_label(i, s), callback_data=f"tog:{s['id']}"),
             InlineKeyboardButton("🗑", callback_data=f"del:{s['id']}")]
            for i, s in enumerate(store.searches, 1)]
    return InlineKeyboardMarkup(rows)


def by_number(ctx):
    try:
        return store.searches[int(ctx.args[0]) - 1]
    except (IndexError, ValueError):
        return None


@owner_only
async def cmd_start(update, ctx):
    await update.message.reply_html(HELP.format(t=store.get("threshold")))
    await update.message.reply_text("Tes recherches :", reply_markup=list_markup())


@owner_only
async def cmd_list(update, ctx):
    await update.message.reply_text("Tes recherches (touche pour activer/pauser) :",
                                    reply_markup=list_markup())


@owner_only
async def cmd_add(update, ctx):
    d = F.parse_add(" ".join(ctx.args))
    if not d:
        await update.message.reply_html("Exemple : <code>/ajouter 3ds xl max 120 min 40 -coque</code>")
        return
    s = store.add_search(d)
    txt = f"✅ Ajouté : <b>{esc(s['query'])}</b> ({s['kind']})"
    if s["min"] or s["max"]:
        txt += f"\n💶 {s['min'] or 0:.0f} € → {s['max'] or '∞'} €"
    if s["exclude"]:
        txt += "\n🚫 exclus : " + esc(", ".join(s["exclude"]))
    await update.message.reply_html(txt)


async def set_price(update, ctx, key):
    s = by_number(ctx)
    try:
        val = float(ctx.args[1].replace(",", ".").rstrip("€"))
    except (IndexError, ValueError):
        val = None
    if not s or val is None:
        await update.message.reply_text(f"Exemple : /{key} 3 120  (recherche n°3 → {key} 120 €)")
        return
    s[key] = val or None
    store.save()
    await update.message.reply_text(f"✅ {s['name']} : {key} = {val:.0f} €")


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
        await update.message.reply_text("Exemple : /seuil 35  (🔥 si 35 % sous le prix habituel)")
        return
    store.set("threshold", v)
    await update.message.reply_text(f"✅ Bon plan = au moins {v} % sous le prix habituel")


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
    cyc = [eng.cycle.get(s["id"]) for s in active if eng.cycle.get(s["id"])]
    avg = f"{sum(cyc) / len(cyc):.1f} s" if cyc else "—"
    txt = (f"{'⏸ En pause' if store.get('paused') else '🟢 En marche'} depuis {up // 3600} h {up % 3600 // 60} min\n"
           f"🔎 {len(active)} recherches actives · chaque recherche vérifiée toutes les ~{avg}\n"
           f"📨 {eng.sent} alertes envoyées · {client.requests} requêtes Vinted\n"
           f"🔥 Seuil bon plan : {store.get('threshold')} %")
    if eng.backoff:
        txt += f"\n⚠️ Ralenti par Vinted (pause {eng.backoff} s)"
    if eng.last_error:
        txt += f"\nDernière erreur : {eng.last_error[:150]}"
    await update.message.reply_text(txt)


@owner_only
async def on_button(update, ctx):
    q = update.callback_query
    kind, _, arg = q.data.partition(":")
    if kind == "full":
        a = ctx.application.bot_data["engine"].alerts.get(int(arg))
        await q.answer()
        if a and a.get("desc"):
            await q.message.reply_text(f"📝 {a['item'].get('title', '')}\n\n{a['desc'][:4000]}")
        return
    s = store.find(arg)
    if not s:
        await q.answer("Recherche introuvable")
        return
    if kind == "tog":
        s["on"] = not s["on"]
        store.save()
        await q.answer("Activée ✅" if s["on"] else "En pause ⏸")
    elif kind == "del":
        store.searches.remove(s)
        store.save()
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
            BotCommand("statut", "État du bot"),
            BotCommand("pause", "Mettre en pause"),
            BotCommand("reprendre", "Relancer"),
            BotCommand("seuil", "Seuil des bons plans (%)"),
            BotCommand("max", "Prix max d'une recherche"),
            BotCommand("min", "Prix min d'une recherche"),
            BotCommand("aide", "Aide"),
        ])
    except Exception as e:  # pas grave si Telegram est lent au démarrage
        log.warning("Menu des commandes non défini: %s", e)
    app.create_task(engine.run())
    app.create_task(autosave())
    if store.get("owner"):
        await engine.notify("🟢 Sniper Vinted démarré, je surveille.")


def main():
    app = (Application.builder().token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(40)
           .build())
    app.add_handler(CommandHandler(["start", "aide"], cmd_start))
    app.add_handler(CommandHandler("liste", cmd_list))
    app.add_handler(CommandHandler("ajouter", cmd_add))
    app.add_handler(CommandHandler("max", cmd_max))
    app.add_handler(CommandHandler("min", cmd_min))
    app.add_handler(CommandHandler("seuil", cmd_seuil))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("reprendre", cmd_resume))
    app.add_handler(CommandHandler("statut", cmd_status))
    app.add_handler(CallbackQueryHandler(on_button))
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
