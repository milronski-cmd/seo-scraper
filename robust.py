#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robuustheid-laag: retry-session, robuuste HTML-decode, content-guards, logging.

Doel: nergens crashen op edge-cases (malformed HTML, rare/ontbrekende encoding,
timeouts, niet-HTML, redirect-loops, gigapagina's) en alles netjes loggen.

Alleen stdlib + requests (+ optioneel charset_normalizer/chardet, beide via requests).
"""
from __future__ import annotations
import logging
import re
import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:                      # pragma: no cover
    Retry = None

# meta-charset prescan (eerste KB's), zoals browsers doen
_META_CHARSET = re.compile(rb'<meta[^>]+charset=["\']?\s*([a-zA-Z0-9_\-:]+)', re.I)
_META_HTTPEQUIV = re.compile(
    rb'<meta[^>]+http-equiv=["\']?content-type["\']?[^>]+content=["\'][^"\']*charset=([a-zA-Z0-9_\-:]+)',
    re.I)
_CT_CHARSET = re.compile(r'charset=([a-zA-Z0-9_\-:]+)', re.I)


def make_session(retries=2, backoff=0.6, headers=None, pool=24):
    """requests.Session met automatische retry+exponentiele backoff op
    timeouts/5xx/429 en connection-resets. Faalt zacht als urllib3.Retry mist."""
    s = requests.Session()
    if headers:
        s.headers.update(headers)
    if Retry is not None:
        try:
            retry = Retry(
                total=retries, connect=retries, read=retries, status=retries,
                backoff_factor=backoff,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                raise_on_status=False, respect_retry_after_header=True,
            )
            ad = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
            s.mount("http://", ad)
            s.mount("https://", ad)
        except Exception:
            pass
    return s


def _valid_encoding(enc):
    if not enc:
        return False
    try:
        "probe".encode("ascii").decode(enc)
        return True
    except Exception:
        return False


def _charset_from_content_type(ct):
    if not ct:
        return ""
    m = _CT_CHARSET.search(ct)
    return (m.group(1).strip().strip('"\'').lower() if m else "")


def decode_html(resp, max_bytes=8_000_000):
    """Robuuste bytes->str decode voor HTML.

    Volgorde (WHATWG-achtig): expliciete header-charset > BOM > <meta charset> >
    auto-detectie (charset_normalizer/chardet) > utf-8-fallback (errors=replace).

    Belangrijk: als de server een geldige charset in de Content-Type-header zet
    (zoals bijna elke correcte site), is het resultaat byte-identiek aan
    `resp.text` -> GEEN regressie. De winst zit in header-loze/foute sites,
    waar requests anders terugvalt op latin-1 en mojibake produceert.

    Returnt (text, encoding, source, truncated).
    """
    try:
        raw = resp.content or b""
    except Exception:
        # extreem defensief: val terug op resp.text als .content faalt
        try:
            return resp.text or "", (resp.encoding or "utf-8"), "text", False
        except Exception:
            return "", "utf-8", "empty", False

    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True

    # 1) expliciete header-charset wint
    enc = _charset_from_content_type(resp.headers.get("Content-Type", ""))
    if enc and _valid_encoding(enc):
        try:
            return raw.decode(enc, "replace"), enc, "header", truncated
        except Exception:
            pass

    # 2) BOM
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", "replace"), "utf-8-sig", "bom", truncated
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16", "replace"), "utf-16", "bom", truncated
        except Exception:
            pass

    # 3) <meta charset> prescan (eerste 4KB)
    head = raw[:4096]
    m = _META_CHARSET.search(head) or _META_HTTPEQUIV.search(head)
    if m:
        e = m.group(1).decode("ascii", "ignore").strip().lower()
        if e in ("utf8",):
            e = "utf-8"
        if _valid_encoding(e):
            return raw.decode(e, "replace"), e, "meta", truncated

    # 4) auto-detectie
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None:
            return str(best), (best.encoding or "utf-8").lower(), "detect", truncated
    except Exception:
        try:
            import chardet
            e = (chardet.detect(raw).get("encoding") or "utf-8")
            return raw.decode(e, "replace"), e.lower(), "detect", truncated
        except Exception:
            pass

    # 5) utf-8 fallback
    return raw.decode("utf-8", "replace"), "utf-8", "fallback", truncated


def looks_like_html(content_type, body_head=b""):
    """Is dit (waarschijnlijk) HTML/XML? Voor het overslaan van pdf/img/zip e.d.
    Kijkt eerst naar Content-Type; bij ontbrekend/octet-stream sniffen we de body."""
    ct = (content_type or "").lower()
    if "html" in ct or "xhtml" in ct or "application/xml" in ct or "text/xml" in ct:
        return True
    binary_markers = ("image/", "application/pdf", "application/zip", "video/",
                      "audio/", "font/", "application/octet-stream",
                      "application/vnd", "application/json")
    if any(b in ct for b in binary_markers):
        # json/xml-sitemaps kunnen alsnog tekst zijn -> body-sniff hieronder
        if "json" not in ct:
            return False
    if not ct or "octet-stream" in ct:
        head = (body_head or b"")[:2048].lower()
        return b"<html" in head or b"<!doctype html" in head or b"<?xml" in head
    return ct.startswith("text/")


def detect_redirect_loop(history, final_url, max_hops=12):
    """True alleen bij een ECHTE redirect-loop (exact dezelfde URL keert terug) of
    bij absurd veel hops. We vergelijken volledige URLs (alleen fragment eraf) —
    NIET met trailing-slash-normalisatie, want een legitieme /pad/ -> /pad
    canonicalisatie is geen loop (requests vangt echte loops zelf al af)."""
    try:
        chain = [h.url for h in (history or [])] + [final_url]
        if len(chain) > max_hops:
            return True
        seen = set()
        for u in chain:
            key = (u or "").split("#")[0]
            if key in seen:
                return True
            seen.add(key)
    except Exception:
        return False
    return False


def get_logger(out_root=None, name="seo", verbose=False):
    """Logger naar run.log (UTF-8) + waarschuwingen op de console. Idempotent.
    Maakt stdout meteen UTF-8 zodat unicode-prints op Windows niet crashen."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
    except Exception:
        pass
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if out_root is not None:
        try:
            Path(out_root).mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(Path(out_root) / "run.log", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except Exception:
            pass
    return log
