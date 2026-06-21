"""
PoE2 Price Check — lokales Price-Check-Tool fuer Path of Exile 2.

Zwei Modi:
  A) Einzelnes, im Spiel kopiertes Item (Strg+C)  -> /api/price
  B) Kompletter Path-of-Building-Build-Code        -> /api/build

Read-only: das Tool liest nur, fragt die offizielle PoE2-Trade-API ab und zeigt
Ergebnisse an. Es automatisiert KEINE Spiel-Eingaben (kein Whisper, kein Macro).
"""

from __future__ import annotations

import base64
import difflib
import html
import json
import math
import os
import re
import threading
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
TRADE_BASE = "https://www.pathofexile.com/api/trade2"
SITE_BASE = "https://www.pathofexile.com/trade2"
DEFAULT_UA = "Poe2PriceCheck/1.0 (local tool; set a contact in config.json)"

DEFAULT_CONFIG = {
    "poesessid": "",
    "league": "Standard",
    "realm": "poe2",
    "user_agent": DEFAULT_UA,
    "exalted_per_divine": 200,
    "currency_rates": {},
    "default_use_mods": False,
}


def load_config() -> dict:
    """Liest config.json (oder faellt auf config.example.json / Defaults zurueck)."""
    cfg = dict(DEFAULT_CONFIG)
    for name in ("config.json", "config.example.json"):
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cfg.update({k: v for k, v in json.load(fh).items() if v is not None})
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[config] Konnte {name} nicht lesen: {exc}")
            break
    return cfg


CONFIG = load_config()


def build_rates(cfg: dict) -> Dict[str, float]:
    """Erzeugt eine Tabelle 'Divine pro 1 Einheit Waehrung'."""
    rates: Dict[str, float] = {"divine": 1.0, "div": 1.0}
    try:
        epd = float(cfg.get("exalted_per_divine") or 0)
    except (TypeError, ValueError):
        epd = 0.0
    if epd > 0:
        for alias in ("exalted", "exalt", "ex"):
            rates[alias] = 1.0 / epd
    for key, val in (cfg.get("currency_rates") or {}).items():
        try:
            rates[str(key).lower()] = float(val)
        except (TypeError, ValueError):
            continue
    return rates


RATES = build_rates(CONFIG)

# --------------------------------------------------------------------------- #
# Fehlerklassen
# --------------------------------------------------------------------------- #


class SessionError(Exception):
    """POESESSID fehlt / ist abgelaufen."""


class RateLimitError(Exception):
    """Rate-Limit erreicht, auch nach Wartezeit."""


class ApiError(Exception):
    """Sonstiger API-/Netzwerkfehler."""


class DecodeError(Exception):
    """PoB-Code konnte nicht dekodiert werden."""


# --------------------------------------------------------------------------- #
# Rate-Limiter (eine sliding-window Instanz pro Endpoint-Typ)
# --------------------------------------------------------------------------- #


class RateLimiter:
    """
    Respektiert die GGG-Rate-Limit-Header.

    - Eigene Sliding-Window-Buchhaltung pro Regel (z. B. 8 Anfragen / 10 s).
    - Beachtet vom Server gemeldete Sperren (X-Rate-Limit-*-State, Retry-After).
    Eine Instanz pro Endpoint-Typ ("search", "fetch", "data"), damit sich die
    unterschiedlichen Limits nicht gegenseitig blockieren.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rules: List[Tuple[int, int]] = []  # (max_hits, period_seconds)
        self.hits: deque = deque()
        self.blocked_until = 0.0

    def wait(self) -> None:
        """Blockiert, bis eine weitere Anfrage erlaubt ist."""
        with self.lock:
            now = time.time()
            if now < self.blocked_until:
                time.sleep(self.blocked_until - now)
                now = time.time()

            if self.rules:
                max_period = max(period for _, period in self.rules)
                while self.hits and self.hits[0] < now - max_period:
                    self.hits.popleft()

                wait_for = 0.0
                for max_hits, period in self.rules:
                    cutoff = now - period
                    in_window = [t for t in self.hits if t >= cutoff]
                    if len(in_window) >= max_hits:
                        oldest = min(in_window)
                        wait_for = max(wait_for, oldest + period - now)
                if wait_for > 0:
                    time.sleep(wait_for + 0.05)

            self.hits.append(time.time())

    def note_block(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + seconds)

    def update_from_headers(self, headers) -> None:
        """Liest Limit- und State-Header aus einer Antwort."""
        new_rules: List[Tuple[int, int]] = []
        with self.lock:
            for key, value in headers.items():
                kl = key.lower()
                if not kl.startswith("x-rate-limit-") or kl == "x-rate-limit-rules":
                    continue
                if kl.endswith("-state"):
                    for part in value.split(","):
                        bits = part.split(":")
                        if len(bits) == 3:
                            try:
                                restricted = int(bits[2])
                            except ValueError:
                                continue
                            if restricted > 0:
                                self.blocked_until = max(
                                    self.blocked_until, time.time() + restricted
                                )
                else:
                    for part in value.split(","):
                        bits = part.split(":")
                        if len(bits) == 3:
                            try:
                                new_rules.append((int(bits[0]), int(bits[1])))
                            except ValueError:
                                continue
            if new_rules:
                self.rules = new_rules


_LIMITERS: Dict[str, RateLimiter] = {
    "search": RateLimiter(),
    "fetch": RateLimiter(),
    "data": RateLimiter(),
}
_SESSION = requests.Session()


def build_headers(post: bool = False) -> dict:
    headers = {
        "User-Agent": CONFIG.get("user_agent") or DEFAULT_UA,
        "Accept": "application/json",
        "Origin": "https://www.pathofexile.com",
        "Referer": f"{SITE_BASE}/search/poe2",
    }
    sessid = (CONFIG.get("poesessid") or "").strip()
    if sessid:
        headers["Cookie"] = f"POESESSID={sessid}"
    if post:
        headers["Content-Type"] = "application/json"
    return headers


def api_request(
    method: str,
    url: str,
    kind: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    max_retries: int = 4,
) -> dict:
    """Eine API-Anfrage mit Pflicht-Headern, Rate-Limit-Beachtung und 429-Backoff."""
    limiter = _LIMITERS.get(kind, _LIMITERS["data"])
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        limiter.wait()
        try:
            resp = _SESSION.request(
                method,
                url,
                headers=build_headers(post=(method.upper() == "POST")),
                json=json_body,
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            last_exc = ApiError(f"Netzwerkfehler: {exc}")
            time.sleep(min(2 ** attempt, 16))
            continue

        limiter.update_from_headers(resp.headers)

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = float(retry_after) if retry_after else 5.0 * (attempt + 1)
            except ValueError:
                wait_s = 5.0 * (attempt + 1)
            limiter.note_block(wait_s)
            last_exc = RateLimitError("Rate-Limit erreicht – bitte einen Moment warten.")
            if attempt < max_retries:
                continue
            raise last_exc

        if resp.status_code in (401, 403):
            raise SessionError("POESESSID abgelaufen oder ungültig.")

        ctype = resp.headers.get("Content-Type", "")
        if "text/html" in ctype.lower():
            # Login-Seite statt JSON -> Cookie fehlt/abgelaufen
            raise SessionError("POESESSID abgelaufen oder ungültig (Login-Seite erhalten).")

        if not resp.ok:
            message = f"API-Fehler {resp.status_code}"
            try:
                err = resp.json().get("error", {})
                if err.get("message"):
                    message = f"{message}: {err['message']}"
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass
            raise ApiError(message)

        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ApiError(f"Antwort war kein JSON: {exc}")

    if last_exc:
        raise last_exc
    raise ApiError("Unbekannter Fehler bei der API-Anfrage.")


# --------------------------------------------------------------------------- #
# Daten-Endpoints (Stats / Items) mit Cache
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"[+\-]?\d+(?:\.\d+)?")
_TAG_PREFIX_RE = re.compile(r"^(?:\{[^}]*\})+")
_TAG_SUFFIX_RE = re.compile(
    r"\s*\((?:implicit|crafted|fractured|enchant|rune|scourge|veiled|desecrated)\)\s*$",
    re.IGNORECASE,
)

_DATA_LOCK = threading.Lock()
_STATS_INDEX: Optional[Dict[str, dict]] = None
_STATS_KEYS: Optional[List[str]] = None  # normalisierte Schlüssel für Fuzzy-Matching
_BASE_TYPES: Optional[List[str]] = None


def _cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _load_cached_json(name: str, max_age_s: int = 86400) -> Optional[dict]:
    path = _cache_path(name)
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age_s:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_cached_json(name: str, data: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def normalize_stat_text(text: str) -> str:
    """Macht Item-Mod-Text und Stat-Vorlagen vergleichbar (Zahlen -> '#')."""
    text = text.strip().lower()
    text = _NUM_RE.sub("#", text)
    text = text.replace("+", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_stats_index() -> Dict[str, dict]:
    """Laedt /data/stats (einmal) und baut einen normalisierten Text-Index."""
    global _STATS_INDEX, _STATS_KEYS
    with _DATA_LOCK:
        if _STATS_INDEX is not None:
            return _STATS_INDEX

        data = _load_cached_json("stats.json")
        if data is None:
            data = api_request("GET", f"{TRADE_BASE}/data/stats", "data")
            _save_cached_json("stats.json", data)

        index: Dict[str, dict] = {}
        # Explizite Mods bevorzugen (zuerst), damit sie nicht ueberschrieben werden.
        order = {"explicit": 0, "implicit": 1, "rune": 2, "enchant": 3}
        groups = sorted(
            data.get("result", []),
            key=lambda g: order.get(str(g.get("label", "")).lower(), 9),
        )
        for group in groups:
            for entry in group.get("entries", []):
                norm = normalize_stat_text(entry.get("text", ""))
                if norm and norm not in index:
                    index[norm] = entry
        _STATS_INDEX = index
        _STATS_KEYS = list(index.keys())
        return index


def get_base_types() -> List[str]:
    """Laedt /data/items (einmal) und sammelt alle Basistyp-Namen."""
    global _BASE_TYPES
    with _DATA_LOCK:
        if _BASE_TYPES is not None:
            return _BASE_TYPES

        data = _load_cached_json("items.json")
        if data is None:
            try:
                data = api_request("GET", f"{TRADE_BASE}/data/items", "data")
                _save_cached_json("items.json", data)
            except (ApiError, SessionError, RateLimitError):
                _BASE_TYPES = []
                return _BASE_TYPES

        bases = set()
        for group in data.get("result", []):
            for entry in group.get("entries", []):
                btype = entry.get("type")
                if btype:
                    bases.add(btype)
        # Laengste zuerst -> beste Substring-Treffer bei Magic-Items.
        _BASE_TYPES = sorted(bases, key=len, reverse=True)
        return _BASE_TYPES


# --------------------------------------------------------------------------- #
# Item-Parsing (gleich fuer Clipboard- und PoB-Text)
# --------------------------------------------------------------------------- #

_META_PREFIXES = (
    "item class:",
    "rarity:",
    "sockets:",
    "item level:",
    "itemlevel:",
    "levelreq:",
    "quality:",
    "requirements:",
    "requires",
    "level:",
    "str:",
    "dex:",
    "int:",
    "unique id:",
    "implicits:",
    "prefix:",
    "suffix:",
    "note:",
    "stack size:",
    "limited to:",
    "radius:",
    "talisman tier",
    "area level",
    "catalyst",
    "physical damage:",
    "elemental damage:",
    "chaos damage:",
    "critical hit chance:",
    "critical strike chance:",
    "attacks per second:",
    "weapon range:",
    "armour:",
    "evasion rating:",
    "energy shield:",
    "block chance:",
    "spirit:",
    "reload time:",
)

_FLAG_LINES = {"corrupted", "mirrored", "unidentified", "split", "fractured item"}


def _clean_mod_line(line: str) -> str:
    line = _TAG_PREFIX_RE.sub("", line).strip()
    line = _TAG_SUFFIX_RE.sub("", line).strip()
    return line


def _is_meta(line: str) -> bool:
    low = line.lower()
    if low in _FLAG_LINES:
        return True
    return any(low.startswith(prefix) for prefix in _META_PREFIXES)


def derive_base_from_magic(name_line: str) -> Optional[str]:
    """Versucht, aus einem Magic-Item-Namen den Basistyp herauszuziehen."""
    for base in get_base_types():
        if base.lower() in name_line.lower():
            return base
    return None


def parse_item_text(text: str) -> dict:
    """
    Parst kopierten Item-Text oder PoB-Item-Text in ein einheitliches Dict:
    {name, base_type, rarity, mods[], corrupted, ok, short_mods}.
    """
    # Zeilen voll trimmen: PoB rückt den Item-Text im XML ein, daher müssen
    # auch führende Leerzeichen/Tabs weg, sonst wird "Rarity:" nicht erkannt.
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    result = {
        "name": None,
        "base_type": None,
        "rarity": "UNKNOWN",
        "mods": [],
        "corrupted": False,
        "ok": False,
        "short_mods": "",
    }
    if not lines:
        return result

    # Rarity finden
    rarity_idx = None
    for i, ln in enumerate(lines):
        if ln.lower().startswith("rarity:"):
            rarity_idx = i
            result["rarity"] = ln.split(":", 1)[1].strip().upper()
            break
    if rarity_idx is None:
        # Kein Rarity-Header -> wahrscheinlich nur ein Basistyp
        result["base_type"] = lines[0].strip()
        result["name"] = lines[0].strip()
        result["rarity"] = "NORMAL"
        result["ok"] = True
        return result

    rarity = result["rarity"]
    count = 2 if rarity in ("RARE", "UNIQUE") else 1

    name_lines: List[str] = []
    j = rarity_idx + 1
    while j < len(lines) and len(name_lines) < count:
        ln = lines[j].strip()
        j += 1
        if not ln or ln.startswith("----"):
            continue
        name_lines.append(ln)

    if rarity in ("RARE", "UNIQUE"):
        result["name"] = name_lines[0] if name_lines else None
        result["base_type"] = name_lines[1] if len(name_lines) > 1 else None
    elif rarity == "MAGIC":
        full = name_lines[0] if name_lines else None
        result["name"] = full
        result["base_type"] = derive_base_from_magic(full) if full else None
    else:  # NORMAL / sonstige
        result["base_type"] = name_lines[0] if name_lines else None
        result["name"] = result["base_type"]

    # Mods sammeln
    mods: List[str] = []
    for ln in lines[j:]:
        stripped = ln.strip()
        if not stripped or stripped.startswith("----"):
            continue
        if stripped.lower() in _FLAG_LINES:
            if stripped.lower() == "corrupted":
                result["corrupted"] = True
            continue
        if _is_meta(stripped):
            continue
        cleaned = _clean_mod_line(stripped)
        if cleaned and not _is_meta(cleaned):
            mods.append(cleaned)

    result["mods"] = mods
    short = " · ".join(mods[:3])
    if len(mods) > 3:
        short += " · …"
    result["short_mods"] = short[:160]
    result["ok"] = bool(result["base_type"] or result["name"])
    return result


# --------------------------------------------------------------------------- #
# PoB-Build-Code dekodieren (Modus B)
# --------------------------------------------------------------------------- #


def decode_pob_code(code: str) -> bytes:
    """
    URL-safe-Base64 -> Bytes -> zlib-Dekompression (tolerant).

    Eine kaputte/fehlende Adler-32-Pruefsumme am Stream-Ende soll den Decode
    NICHT abbrechen: dazu wird im Fehlerfall als roher DEFLATE-Stream (ohne
    Pruefsumme) dekomprimiert.
    """
    cleaned = "".join((code or "").split())
    if not cleaned:
        raise DecodeError("Build-Code ist leer.")
    # Falls eine ganze URL eingefuegt wurde: letztes Pfadsegment nehmen.
    if "/" in cleaned and ("http" in cleaned[:8] or "pastebin" in cleaned.lower()):
        cleaned = cleaned.rstrip("/").split("/")[-1]

    cleaned = cleaned.replace("-", "+").replace("_", "/")
    cleaned += "=" * ((-len(cleaned)) % 4)

    try:
        raw = base64.b64decode(cleaned)
    except (ValueError, base64.binascii.Error) as exc:
        raise DecodeError(f"Base64 ungültig: {exc}")

    return _zlib_decompress_tolerant(raw)


def _zlib_decompress_tolerant(raw: bytes) -> bytes:
    # Schnellweg: gueltiger zlib-Stream
    try:
        return zlib.decompress(raw)
    except zlib.error:
        pass

    # Tolerant: als rohen DEFLATE-Stream behandeln (wbits=-15 -> keine
    # Pruefsumme), notfalls den 2-Byte-zlib-Header ueberspringen.
    for payload in ([raw[2:], raw] if len(raw) > 2 else [raw]):
        decomp = zlib.decompressobj(-15)
        out = bytearray()
        try:
            out += decomp.decompress(payload)
            out += decomp.flush()
        except zlib.error:
            # Abgeschnittener Stream: das bisher Dekomprimierte behalten.
            pass
        if out and (out.lstrip()[:1] == b"<" or b"PathOfBuilding" in out[:400] or len(out) > 64):
            return bytes(out)

    raise DecodeError("Build-Code beschädigt – konnte nicht dekomprimiert werden.")


# Kanonische Slot-Reihenfolge fuer die Anzeige
_SLOT_ORDER = {
    name: i
    for i, name in enumerate(
        [
            "Weapon 1", "Weapon 1 Swap", "Weapon 2", "Weapon 2 Swap",
            "Helmet", "Body Armour", "Gloves", "Boots",
            "Amulet", "Ring 1", "Ring 2", "Belt",
            "Flask 1", "Flask 2", "Flask 3", "Flask 4", "Flask 5",
        ]
    )
}


def parse_pob_xml(xml_bytes: bytes) -> Tuple[dict, List[dict]]:
    """
    Liefert (build_info, items).
    build_info: {class, ascendancy, level}
    items: Liste von {slot, parsed-item-Felder, ok}
    Faellt bei kaputtem XML auf Regex-Extraktion zurueck.
    """
    text = xml_bytes.decode("utf-8", errors="replace").lstrip("﻿").strip()
    build_info = {"class": None, "ascendancy": None, "level": None}
    items_by_id: Dict[str, str] = {}
    slot_pairs: List[Tuple[str, str]] = []

    parsed_ok = False
    try:
        root = ET.fromstring(text)
        parsed_ok = True
    except ET.ParseError:
        root = None

    if parsed_ok and root is not None:
        build_el = root.find("Build")
        if build_el is not None:
            build_info["class"] = build_el.get("className")
            build_info["ascendancy"] = build_el.get("ascendClassName")
            build_info["level"] = build_el.get("level")

        items_el = root.find("Items")
        if items_el is not None:
            for item_el in items_el.findall("Item"):
                iid = item_el.get("id")
                if iid and item_el.text:
                    items_by_id[iid] = item_el.text

            active = items_el.get("activeItemSet")
            chosen_set = None
            for set_el in items_el.findall("ItemSet"):
                if active and set_el.get("id") == active:
                    chosen_set = set_el
                    break
            if chosen_set is None:
                chosen_set = items_el.find("ItemSet")

            if chosen_set is not None:
                for slot_el in chosen_set.findall("Slot"):
                    sname, iid = slot_el.get("name"), slot_el.get("itemId")
                    if sname and iid and iid != "0":
                        slot_pairs.append((sname, iid))
    else:
        # --- Regex-Fallback fuer abgeschnittenes / kaputtes XML ---
        bmatch = re.search(r"<Build\b([^>]*)>", text)
        if bmatch:
            attrs = bmatch.group(1)
            cls = re.search(r'className="([^"]*)"', attrs)
            asc = re.search(r'ascendClassName="([^"]*)"', attrs)
            lvl = re.search(r'level="([^"]*)"', attrs)
            build_info["class"] = cls.group(1) if cls else None
            build_info["ascendancy"] = asc.group(1) if asc else None
            build_info["level"] = lvl.group(1) if lvl else None

        for m in re.finditer(r'<Item\b[^>]*\bid="(\d+)"[^>]*>(.*?)</Item>', text, re.DOTALL):
            items_by_id[m.group(1)] = html.unescape(m.group(2))

        seen = set()
        for m in re.finditer(
            r'<Slot\b[^>]*\bname="([^"]*)"[^>]*\bitemId="(\d+)"', text
        ):
            sname, iid = m.group(1), m.group(2)
            if iid != "0" and (sname not in seen):
                seen.add(sname)
                slot_pairs.append((sname, iid))

    # Slots in kanonische Reihenfolge bringen
    slot_pairs.sort(key=lambda p: _SLOT_ORDER.get(p[0], 100 + len(p[0])))

    items: List[dict] = []
    for sname, iid in slot_pairs:
        raw_text = items_by_id.get(iid)
        if not raw_text or not raw_text.strip():
            items.append({"slot": sname, "ok": False, "name": "(beschädigt)", "rarity": "UNKNOWN",
                          "base_type": None, "short_mods": "", "mods": []})
            continue
        try:
            parsed = parse_item_text(raw_text)
            parsed["slot"] = sname
            items.append(parsed)
        except Exception:  # defensives Fallback pro Item
            items.append({"slot": sname, "ok": False, "name": "(beschädigt)", "rarity": "UNKNOWN",
                          "base_type": None, "short_mods": "", "mods": []})

    return build_info, items


# --------------------------------------------------------------------------- #
# Trade-Suche / Pricing (gleich fuer beide Modi)
# --------------------------------------------------------------------------- #


def _mod_value_tolerance() -> float:
    """Faktor, mit dem der Mindestwert eines Mods gesenkt wird (Default 0.9 = -10%)."""
    try:
        tol = float(CONFIG.get("mod_value_tolerance", 0.9))
    except (TypeError, ValueError):
        tol = 0.9
    return min(max(tol, 0.0), 1.0)


def _stat_filters_for(mods: List[str], max_filters: int = 4) -> List[dict]:
    """
    Mappt Item-Mods (Prefixes/Suffixes) auf Trade-Stat-IDs.

    - Erst exakter, dann unscharfer (Fuzzy-)Abgleich gegen /data/stats.
    - Der Mindestwert wird um die Toleranz gesenkt (z. B. 50 -> 45 bei 0.9),
      damit auch etwas schwächere Listings als Treffer gelten.
    """
    index = get_stats_index()
    keys = _STATS_KEYS or []
    tolerance = _mod_value_tolerance()
    filters: List[dict] = []
    seen_ids: set = set()

    for mod in mods:
        norm = normalize_stat_text(mod)
        if not norm:
            continue
        entry = index.get(norm)
        if entry is None and keys:
            # Unscharfer Abgleich für leicht abweichende Formulierungen
            close = difflib.get_close_matches(norm, keys, n=1, cutoff=0.86)
            if close:
                entry = index.get(close[0])
        if entry is None or entry.get("id") in seen_ids:
            continue

        flt: dict = {"id": entry["id"], "disabled": False}
        num = _NUM_RE.search(mod)
        if num:
            try:
                value = float(num.group())
                if value > 0:
                    reduced = math.floor(value * tolerance)
                    if reduced >= 1:
                        flt["value"] = {"min": reduced}
            except ValueError:
                pass
        filters.append(flt)
        seen_ids.add(entry["id"])
        if len(filters) >= max_filters:
            break
    return filters


def _do_search(league: str, query: dict) -> dict:
    url = f"{TRADE_BASE}/search/{CONFIG.get('realm', 'poe2')}/{league}"
    return api_request("POST", url, "search", json_body={"query": query, "sort": {"price": "asc"}})


def _do_fetch(hashes: List[str], query_id: str) -> dict:
    ids = ",".join(hashes[:10])
    url = f"{TRADE_BASE}/fetch/{ids}"
    return api_request(
        "GET", url, "fetch",
        params={"query": query_id, "realm": CONFIG.get("realm", "poe2")},
    )


def to_divine(amount: float, currency: str) -> Optional[float]:
    rate = RATES.get(str(currency).lower())
    return amount * rate if rate is not None else None


def _time_ago(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return "gerade eben"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    if secs < 86400:
        return f"{int(secs // 3600)} h"
    return f"{int(secs // 86400)} d"


def _parse_listings(fetch_data: dict) -> List[dict]:
    listings: List[dict] = []
    for entry in fetch_data.get("result") or []:
        if not entry:
            continue
        listing = entry.get("listing", {})
        price = listing.get("price")
        if not price:
            continue
        amount = price.get("amount")
        currency = price.get("currency")
        if amount is None or currency is None:
            continue
        account = (listing.get("account") or {}).get("name", "?")
        online = bool((listing.get("account") or {}).get("online"))
        indexed = listing.get("indexed")
        divine = to_divine(amount, currency)
        listings.append({
            "amount": amount,
            "currency": currency,
            "divine": round(divine, 3) if divine is not None else None,
            "account": account,
            "online": online,
            "age": _time_ago(indexed),
        })
    return listings


def price_item(parsed: dict, league: str, use_mods: bool) -> dict:
    """
    Fuehrt die Trade-Suche fuer ein geparstes Item aus und liefert:
    {status, listings, total, cheapest_divine, searched_by, query_url}
    status: found | ballpark | no_match | error
    """
    out = {
        "status": "no_match",
        "listings": [],
        "total": 0,
        "cheapest_divine": None,
        "searched_by": "",
        "query_url": None,
    }
    if not parsed.get("ok"):
        out["status"] = "error"
        return out

    rarity = (parsed.get("rarity") or "").upper()
    name = parsed.get("name")
    base = parsed.get("base_type")

    # Such-Strategien in Reihenfolge: (query, status_bei_treffer, beschreibung)
    attempts: List[Tuple[dict, str, str]] = []
    base_status = {"status": {"option": "online"}, "stats": [{"type": "and", "filters": []}]}

    if rarity == "UNIQUE" and name:
        q = dict(base_status, name=name)
        if base:
            q = dict(q, type=base)
            attempts.append((q, "found", f"Name + Basistyp ({name})"))
        attempts.append((dict(base_status, name=name), "found", f"Name ({name})"))
    elif base:
        if use_mods:
            filters = _stat_filters_for(parsed.get("mods", []))
            if filters:
                q = {"status": {"option": "online"}, "type": base,
                     "stats": [{"type": "and", "filters": filters}]}
                attempts.append((q, "found", f"Basistyp + {len(filters)} Mod-Filter"))
        attempts.append((dict(base_status, type=base), "ballpark", f"Basistyp ({base})"))
    elif name:
        attempts.append((dict(base_status, name=name), "ballpark", f"Name ({name})"))

    if not attempts:
        out["status"] = "error"
        return out

    realm = CONFIG.get("realm", "poe2")
    last_api_error: Optional[str] = None
    for query, hit_status, desc in attempts:
        try:
            search = _do_search(league, query)
        except ApiError as exc:
            # z. B. 400 "Unknown item base type" -> nächste Strategie versuchen
            last_api_error = str(exc)
            continue
        result_hashes = search.get("result") or []
        query_id = search.get("id")
        out["searched_by"] = desc
        if query_id:
            out["query_url"] = f"{SITE_BASE}/search/{realm}/{league}/{query_id}"
        if not result_hashes:
            continue

        out["total"] = search.get("total", len(result_hashes))
        fetch_data = _do_fetch(result_hashes, query_id)
        listings = _parse_listings(fetch_data)
        if not listings:
            continue

        out["listings"] = listings
        out["status"] = hit_status
        known = [l["divine"] for l in listings if l["divine"] is not None]
        out["cheapest_divine"] = min(known) if known else None
        return out

    # Alle Strategien ohne Treffer: API-Fehler nur melden, wenn KEINE Suche lief
    if last_api_error and not out["searched_by"]:
        raise ApiError(last_api_error)
    return out


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #

app = FastAPI(title="PoE2 Price Check", docs_url=None, redoc_url=None)


class PriceRequest(BaseModel):
    text: str
    use_mods: bool = True
    league: Optional[str] = None


class BuildRequest(BaseModel):
    code: str
    use_mods: bool = True
    league: Optional[str] = None


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/api/config")
def api_config():
    return {
        "league": current_league(),
        "realm": CONFIG.get("realm", "poe2"),
        "default_use_mods": bool(CONFIG.get("default_use_mods", False)),
        "exalted_per_divine": CONFIG.get("exalted_per_divine"),
        "has_session": bool((CONFIG.get("poesessid") or "").strip()),
        "leagues": _get_leagues(),
    }


# --- Liga-Erkennung: "auto" -> immer die aktuelle (neueste) Challenge-Liga ----

_LEAGUES_CACHE: Dict[str, object] = {"ts": 0.0, "raw": []}
_PERMANENT_LEAGUES = {
    "Standard", "Hardcore", "SSF Standard", "SSF Hardcore",
    "Solo Self-Found", "Ruthless", "HC Ruthless",
}


def _fetch_leagues_raw() -> List[dict]:
    """Holt die Liga-Liste von der Trade-API (im Speicher 1 h gecacht)."""
    now = time.time()
    raw = _LEAGUES_CACHE.get("raw") or []
    if raw and (now - float(_LEAGUES_CACHE.get("ts", 0))) < 3600:
        return raw  # type: ignore[return-value]
    try:
        data = api_request("GET", f"{TRADE_BASE}/data/leagues", "data",
                           params={"realm": CONFIG.get("realm", "poe2")})
        raw = [l for l in data.get("result", []) if l.get("id")]
    except (ApiError, SessionError, RateLimitError):
        raw = []
    if raw:
        _LEAGUES_CACHE["raw"] = raw
        _LEAGUES_CACHE["ts"] = now
    return raw


def _is_variant_league(league_id: str) -> bool:
    """True fuer HC-/SSF-/Ruthless-Varianten – nicht die Standard-Challenge-Liga."""
    low = league_id.lower()
    return (
        low.startswith("hardcore") or low.startswith("hc ")
        or "ssf" in low or "ruthless" in low or "solo self-found" in low
    )


def detect_current_league(raw: List[dict]) -> Optional[str]:
    """
    Findet die aktuelle (neueste) Challenge-Liga: weder permanent (Standard/
    Hardcore) noch eine HC/SSF-Variante. Robust gegenueber der Reihenfolge.
    """
    ids = [l["id"] for l in raw]
    challenge = [i for i in ids if i not in _PERMANENT_LEAGUES and not _is_variant_league(i)]
    if challenge:
        return challenge[0]
    nonperm = [i for i in ids if i not in _PERMANENT_LEAGUES]
    return nonperm[0] if nonperm else (ids[0] if ids else None)


def current_league() -> str:
    """Aufgeloeste Standard-Liga. 'auto'/leer -> neueste Challenge-Liga erkennen."""
    configured = (CONFIG.get("league") or "").strip()
    if configured and configured.lower() != "auto":
        return configured
    return detect_current_league(_fetch_leagues_raw()) or "Standard"


def _get_leagues() -> List[str]:
    """Liste aller Liga-IDs fuer das Dropdown (aktuelle Challenge-Liga zuerst)."""
    ids = [l["id"] for l in _fetch_leagues_raw()]
    if ids:
        cur = current_league()
        if cur in ids:
            ids = [cur] + [i for i in ids if i != cur]
        return ids
    # Offline-Fallback
    fallback = [current_league()]
    for extra in ("Standard", "Hardcore"):
        if extra not in fallback:
            fallback.append(extra)
    return fallback


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, SessionError):
        return JSONResponse(status_code=401, content={"ok": False, "error": str(exc)})
    if isinstance(exc, RateLimitError):
        return JSONResponse(status_code=429, content={"ok": False, "error": str(exc)})
    if isinstance(exc, (DecodeError, ApiError)):
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return JSONResponse(status_code=500, content={"ok": False, "error": f"Unerwarteter Fehler: {exc}"})


@app.post("/api/price")
def api_price(req: PriceRequest):
    league = req.league or current_league()
    parsed = parse_item_text(req.text or "")
    if not parsed.get("ok"):
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Item-Text konnte nicht gelesen werden."})
    try:
        result = price_item(parsed, league, req.use_mods)
    except Exception as exc:  # einheitliche Fehlerausgabe
        return _error_response(exc)

    return {
        "ok": True,
        "item": {
            "name": parsed.get("name"),
            "base_type": parsed.get("base_type"),
            "rarity": parsed.get("rarity"),
            "corrupted": parsed.get("corrupted"),
            "mods": parsed.get("mods"),
        },
        "result": result,
    }


def _item_payload(parsed: dict, index: int) -> dict:
    return {
        "index": index,
        "slot": parsed.get("slot"),
        "name": parsed.get("name") or parsed.get("base_type") or "(unbekannt)",
        "base_type": parsed.get("base_type"),
        "rarity": parsed.get("rarity", "UNKNOWN"),
        "short_mods": parsed.get("short_mods", ""),
        "corrupted": parsed.get("corrupted", False),
        "ok": parsed.get("ok", False),
        "status": "pending" if parsed.get("ok") else "error",
    }


def build_stream(code: str, use_mods: bool, league: str):
    """Generator, der NDJSON-Events streamt (build / progress / priced / done / error)."""
    def line(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + "\n"

    try:
        raw = decode_pob_code(code)
    except DecodeError as exc:
        yield line({"type": "error", "message": str(exc)})
        return

    try:
        build_info, items = parse_pob_xml(raw)
    except Exception as exc:
        yield line({"type": "error", "message": f"Build konnte nicht gelesen werden: {exc}"})
        return

    if not items:
        yield line({"type": "error",
                    "message": "Keine Items im Build gefunden (Code evtl. beschädigt oder leer)."})
        return

    yield line({
        "type": "build",
        "build": build_info,
        "items": [_item_payload(it, i) for i, it in enumerate(items)],
    })

    total = len(items)
    total_divine = 0.0
    for i, it in enumerate(items):
        if not it.get("ok"):
            yield line({"type": "priced", "index": i, "result": {"status": "error"}})
            continue

        yield line({"type": "progress", "current": i + 1, "total": total,
                    "name": it.get("name") or it.get("base_type") or "Item"})
        try:
            result = price_item(it, league, use_mods)
        except SessionError as exc:
            yield line({"type": "error", "message": str(exc)})
            return
        except RateLimitError:
            yield line({"type": "priced", "index": i,
                        "result": {"status": "error", "note": "Rate-Limit"}})
            continue
        except (ApiError, Exception) as exc:
            yield line({"type": "priced", "index": i,
                        "result": {"status": "error", "note": str(exc)[:120]}})
            continue

        if result.get("cheapest_divine") is not None:
            total_divine += result["cheapest_divine"]

        yield line({"type": "priced", "index": i, "result": {
            "status": result["status"],
            "searched_by": result["searched_by"],
            "total": result["total"],
            "cheapest_divine": result["cheapest_divine"],
            "query_url": result["query_url"],
            "listing": result["listings"][0] if result["listings"] else None,
        }})

    yield line({"type": "done", "total_divine": round(total_divine, 2),
                "exalted_per_divine": CONFIG.get("exalted_per_divine")})


@app.post("/api/build")
def api_build(req: BuildRequest):
    league = req.league or current_league()
    return StreamingResponse(
        build_stream(req.code or "", req.use_mods, league),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    print("PoE2 Price Check läuft auf  http://127.0.0.1:8000")
    if not (CONFIG.get("poesessid") or "").strip():
        print("[!] Kein POESESSID in config.json gesetzt – Preisabfragen schlagen fehl.")
    uvicorn.run(app, host="127.0.0.1", port=8000)
