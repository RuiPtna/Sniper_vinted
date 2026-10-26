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

HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": DOMAIN + "/catalog",
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


class VintedClient:
    def __init__(self):
        self.s = AsyncSession(impersonate="chrome", timeout=15)
        self.cookie_time = 0.0
        self.requests = 0

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

    async def search(self, query, price_from=None, price_to=None, per_page=30):
        if not self.cookie_time or time.time() - self.cookie_time > 1800:
            await self.warmup()
        params = {"search_text": query, "order": "newest_first",
                  "per_page": per_page, "page": 1, "currency": "EUR"}
        if price_from:
            params["price_from"] = price_from
        if price_to:
            params["price_to"] = price_to
        for attempt in range(2):
            r = await self.s.get(DOMAIN + "/api/v2/catalog/items",
                                 params=params, headers=API_HEADERS)
            self.requests += 1
            if r.status_code == 200:
                return r.json().get("items", []) or []
            if r.status_code == 401 and attempt == 0:
                await self.warmup()
                continue
            if r.status_code in (403, 429):
                raise Blocked(r.status_code)
            raise RuntimeError(f"Recherche Vinted : HTTP {r.status_code}")
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
