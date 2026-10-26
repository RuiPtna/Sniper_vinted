"""Client Vinted : recherche des nouvelles annonces + lecture de la description."""
import html
import json
import logging
import os
import re
import time

from curl_cffi.requests import AsyncSession

log = logging.getLogger("vinted")
DOMAIN = os.getenv("VINTED_DOMAIN", "https://www.vinted.fr").rstrip("/")
# Depuis sept. 2026 la recherche passe par api.vinted.<pays>/svc-catalogue/items
API_DOMAIN = os.getenv("VINTED_API_DOMAIN", DOMAIN.replace("://www.", "://api.")).rstrip("/")

HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Origin": DOMAIN,
    "Referer": DOMAIN + "/",
}

LD_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
META_RE = re.compile(
    r'<meta[^>]+(?:property|name)="(?:og:description|description)"[^>]+content="([^"]*)"', re.S)


class Blocked(Exception):
    def __init__(self, status):
        super().__init__(f"Vinted bloque (HTTP {status})")
        self.status = status


def item_url(item: dict) -> str:
    url = item.get("url") or f"/items/{item.get('id')}"
    return url if url.startswith("http") else DOMAIN + url


def checkout_url(item_id) -> str:
    return f"{DOMAIN}/transaction/buy/new?source_screen=item&transaction%5Bitem_id%5D={item_id}"


def extract_items(data):
    """Trouve la liste d'annonces quelle que soit la forme de la réponse."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "data", "results", "catalog_items", "entities"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                found = extract_items(v)
                if found is not None:
                    return found
    return None


def _first_url(o):
    if isinstance(o, str):
        return o
    if isinstance(o, dict):
        for k in ("url", "full_size_url", "image_url", "src", "thumbnail_url"):
            if isinstance(o.get(k), str):
                return o[k]
    return None


def normalize(raw: dict) -> dict:
    """Ramène une annonce (ancien ou nouveau format) aux champs utilisés par le bot."""
    it = dict(raw)
    it["title"] = raw.get("title") or raw.get("name") or ""
    photo = raw.get("photo")
    if not photo and isinstance(raw.get("photos"), list) and raw["photos"]:
        photo = raw["photos"][0]
    if not photo:
        photo = raw.get("image") or raw.get("thumbnail")
    url = _first_url(photo)
    it["photo"] = dict(photo) if isinstance(photo, dict) else {}
    if url:
        it["photo"]["url"] = url
    if not isinstance(raw.get("user"), dict):
        it["user"] = {"login": raw.get("user_login") or raw.get("seller_login") or "?"}
    box = raw.get("item_box") or {}
    if isinstance(box, dict):
        if not it.get("brand_title") and box.get("first_line"):
            it["brand_title"] = box["first_line"]
        if not it.get("status") and box.get("second_line"):
            it["status"] = box["second_line"]
    if not it.get("url"):
        it["url"] = raw.get("path") or f"/items/{raw.get('id')}"
    return it


class VintedClient:
    def __init__(self):
        self.s = AsyncSession(impersonate="chrome", timeout=15)
        self.cookie_time = 0.0
        self.requests = 0
        self.last_raw = None

    async def warmup(self):
        """Récupère un cookie de session anonyme (nécessaire pour l'API)."""
        try:
            await self.s.close()
        except Exception:
            pass
        self.s = AsyncSession(impersonate="chrome", timeout=15)
        r = await self.s.get(DOMAIN + "/", headers=HTML_HEADERS)
        self.requests += 1
        if r.status_code in (403, 429):
            raise Blocked(r.status_code)
        self.cookie_time = time.time()
        log.info("Session Vinted rafraîchie (%s)", r.status_code)

    def _token_headers(self):
        h = dict(API_HEADERS)
        tok = self.s.cookies.get("access_token_web")
        anon = self.s.cookies.get("anon_id")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if anon:
            h["x-anon-id"] = anon
        return h

    async def search(self, query, price_from=None, price_to=None, per_page=30):
        if not self.cookie_time or time.time() - self.cookie_time > 1800:
            await self.warmup()
        params = {"search_text": query, "order": "newest_first",
                  "per_page": per_page, "page": 1, "currency": "EUR"}
        if price_from:
            params["price_from"] = int(price_from)
        if price_to:
            params["price_to"] = int(price_to)
        for attempt in range(3):
            r = await self.s.get(API_DOMAIN + "/svc-catalogue/items",
                                 params=params, headers=self._token_headers())
            self.requests += 1
            if r.status_code == 200:
                data = r.json()
                raw = extract_items(data)
                if raw is None:
                    self.last_raw = data
                    keys = list(data.keys())[:8] if isinstance(data, dict) else type(data).__name__
                    raise RuntimeError(f"Réponse Vinted inattendue, clés : {keys}")
                if raw:
                    self.last_raw = raw[0]
                return [normalize(x) for x in raw if isinstance(x, dict)]
            if r.status_code in (401, 403) and attempt < 2:
                await self.warmup()
                continue
            if r.status_code in (403, 429):
                raise Blocked(r.status_code)
            raise RuntimeError(f"Recherche Vinted : HTTP {r.status_code} {r.text[:120]}")
        return []

    async def description(self, url: str) -> str:
        r = await self.s.get(url, headers=HTML_HEADERS)
        self.requests += 1
        if r.status_code != 200:
            return ""
        page = r.text
        for block in LD_RE.findall(page):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            stack = [data]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    d = o.get("description")
                    if isinstance(d, str) and d.strip():
                        return html.unescape(d).strip()
                    stack.extend(o.values())
                elif isinstance(o, list):
                    stack.extend(o)
        m = META_RE.search(page)
        return html.unescape(m.group(1)).strip() if m else ""
