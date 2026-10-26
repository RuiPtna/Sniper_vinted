"""Sauvegarde des recherches, réglages, annonces vues et prix du marché (data/state.json)."""
import json
import os
import time
import uuid

DATA_DIR = os.getenv("DATA_DIR", "data")
PATH = os.path.join(DATA_DIR, "state.json")
VERSION = 2
MARKET_DAYS = 30          # on oublie les prix de plus de 30 jours
MARKET_MAX = 600          # annonces gardées par recherche


def new_search(query, kind, mn=None, mx=None, exclude=(), prio=False, deals_only=False, ref=None):
    return {"id": uuid.uuid4().hex[:6], "name": query, "query": query, "kind": kind,
            "min": mn, "max": mx, "exclude": list(exclude), "ref": ref,
            "prio": prio, "deals_only": deals_only, "on": True}


class Store:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.data = {"version": VERSION, "owner": None, "paused": False, "threshold": 30,
                     "searches": [], "seen": [], "market": {}}
        if os.path.exists(PATH):
            try:
                with open(PATH, encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("version") == VERSION:
                    self.data.update(old)
                else:  # ancienne version : on garde juste le propriétaire, on repart à zéro
                    self.data["owner"] = old.get("owner")
            except (OSError, ValueError):
                pass
        self.seen = set(self.data["seen"])
        self.dirty = False

    @property
    def searches(self):
        return self.data["searches"]

    def get(self, key):
        return self.data[key]

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def find(self, sid):
        return next((s for s in self.searches if s["id"] == sid), None)

    def add_search(self, d):
        s = new_search(d["query"], d["kind"], d.get("min"), d.get("max"), d.get("exclude", []),
                       d.get("prio", False), d.get("deals_only", False), d.get("ref"))
        self.searches.append(s)
        self.save()
        return s

    def remove_search(self, s):
        self.searches.remove(s)
        self.data["market"].pop(s["id"], None)
        self.save()

    # ---- annonces déjà envoyées ----
    def mark_seen(self, iid):
        if iid not in self.seen:
            self.seen.add(iid)
            self.data["seen"].append(iid)
            if len(self.data["seen"]) > 30000:
                drop = self.data["seen"][:5000]
                self.data["seen"] = self.data["seen"][5000:]
                self.seen.difference_update(drop)
            self.dirty = True

    # ---- prix du marché : {sid: {item_id: [prix, modèle, date]}} ----
    def add_market(self, sid, iid, price, key):
        m = self.data["market"].setdefault(sid, {})
        k = str(iid)
        if k in m:
            return
        m[k] = [price, key, int(time.time())]
        if len(m) > MARKET_MAX:
            for old in sorted(m, key=lambda x: m[x][2])[: len(m) - MARKET_MAX]:
                del m[old]
        self.dirty = True

    def market_prices(self, sid, key):
        limit = time.time() - MARKET_DAYS * 86400
        return [p for p, k, t in self.data["market"].get(sid, {}).values() if k == key and t >= limit]

    def market_size(self, sid):
        return len(self.data["market"].get(sid, {}))

    def save(self):
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, PATH)
        self.dirty = False
