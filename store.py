"""Sauvegarde des recherches, réglages et annonces déjà vues (data/state.json)."""
import json
import os
import uuid

DATA_DIR = os.getenv("DATA_DIR", "data")
PATH = os.path.join(DATA_DIR, "state.json")


def _s(name, query, kind, mn=None, mx=None, exclude=(), prio=False, deals_only=False):
    return {"id": uuid.uuid4().hex[:6], "name": name, "query": query, "kind": kind,
            "min": mn, "max": mx, "exclude": list(exclude), "ref": None,
            "prio": prio, "deals_only": deals_only, "on": True}


def default_searches():
    return [
        _s("Nintendo DS / DS Lite", "nintendo ds lite", "console", 20, 70),
        _s("Nintendo DSi", "nintendo dsi", "console", 25, 70, ["xl"]),
        _s("Game Boy", "game boy", "console", 25, 90),
        _s("Nintendo 3DS", "nintendo 3ds", "console", 35, 110, ["xl"]),
        _s("3DS XL", "3ds xl", "console", 50, 150, prio=True),
        _s("PS5", "ps5 console", "console", 150, 380, prio=True),
        _s("PS Portal", "playstation portal", "console", 80, 170, prio=True),
        _s("Display Pokémon", "display pokemon", "tcg", 60, 220, ["vide"], prio=True),
        _s("ETB Pokémon", "coffret dresseur d elite pokemon", "tcg", 25, 70, ["vide"]),
        _s("Booster Pokémon", "booster pokemon scelle", "tcg", 3, 8),
        _s("Lot cartes Pokémon", "lot cartes pokemon", "tcg", 5, 40),
    ]


class Store:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.data = {"owner": None, "paused": False, "threshold": 35,
                     "searches": default_searches(), "seen": [], "prices": {}}
        if os.path.exists(PATH):
            try:
                with open(PATH, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError):
                pass
        self.seen = set(self.data["seen"])
        self.dirty = False

    # raccourcis
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
        s = _s(d["query"], d["query"], d["kind"], d.get("min"), d.get("max"),
               d.get("exclude", []), d.get("prio", False), d.get("deals_only", False))
        s["ref"] = d.get("ref")
        self.searches.append(s)
        self.save()
        return s

    def mark_seen(self, iid):
        if iid not in self.seen:
            self.seen.add(iid)
            self.data["seen"].append(iid)
            if len(self.data["seen"]) > 30000:
                drop = self.data["seen"][:5000]
                self.data["seen"] = self.data["seen"][5000:]
                self.seen.difference_update(drop)
            self.dirty = True

    def add_price(self, sid, price):
        h = self.data["prices"].setdefault(sid, [])
        h.append(price)
        if len(h) > 150:
            del h[:-150]
        self.dirty = True

    def history(self, sid):
        return self.data["prices"].get(sid, [])

    def save(self):
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, PATH)
        self.dirty = False
