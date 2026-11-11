#!/usr/bin/env python3
"""
Détection de fermeture du restaurant (page du restaurant sur crous-normandie.fr).

Même logique que le bot « menu » : la page liste un bloc « Menu du <jour> » par
jour ; un jour fermé ne contient que « Fermeture » (ou « Structure fermée »).
Le titre de la page affiche aussi « Fermé aujourd'hui ». Si le jour est fermé,
le bot affluence ne se lance pas (rien n'est lu, rien n'est enregistré).

En cas d'erreur réseau, on considère le restaurant OUVERT (on ne bloque jamais
un vrai service à cause d'un souci de lecture de la page) ; le filet de sécurité
« aucun repas à 12h » de affluence_bot.py prend alors le relais.
"""
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup, Tag

PAGE_URL = "https://www.crous-normandie.fr/restaurant/restaurant-porte-oceane/"

DAYS_RE   = r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
MONTHS_RE = r"janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre"
MONTHS    = ["janvier", "fevrier", "mars", "avril", "mai", "juin",
             "juillet", "aout", "septembre", "octobre", "novembre", "decembre"]

DAY_HEADING_RE = re.compile(
    rf"menu\s+du\s+(?:{DAYS_RE})\s+(\d{{1,2}})\s+({MONTHS_RE})(?:\s+(\d{{4}}))?", re.IGNORECASE)
CLOSED_ITEM_RE   = re.compile(r"^\W*(?:structure\s+)?ferm(?:eture|[eé]e?)\b", re.IGNORECASE)
CLOSED_BANNER_RE = re.compile(r"ferm[eé]e?\s+aujourd['’]hui", re.IGNORECASE)


def _month(name: str) -> int:
    return MONTHS.index(name.lower().replace("é", "e").replace("û", "u")) + 1


def parse_day_blocks(soup, default_year: int) -> list:
    """Un dict {date, closed, has_menu} par bloc « Menu du <jour> » (trié par date).
    Chaque bloc s'arrête au titre suivant : on ne lit jamais la liste d'un autre jour."""
    found = []
    for s in soup.find_all(string=DAY_HEADING_RE):
        text = s.strip()
        m = DAY_HEADING_RE.search(text)
        if not m or len(text) > 60:
            continue
        try:
            d = date(int(m.group(3) or default_year), _month(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
        found.append((s, d))

    blocks, seen = [], set()
    for i, (s, d) in enumerate(found):
        if d in seen:
            continue
        seen.add(d)
        next_s = found[i + 1][0] if i + 1 < len(found) else None
        tag = s.parent
        level = int(tag.name[1]) if tag is not None and re.fullmatch(r"h[1-6]", tag.name or "") else 2
        lis = []
        for el in s.next_elements:
            if next_s is not None and el is next_s:
                break
            if not isinstance(el, Tag):
                continue
            if el.name == "footer" or (re.fullmatch(r"h[1-6]", el.name) and int(el.name[1]) <= level):
                break
            if el.name == "li":
                lis.append(el)
        leaves = [li.get_text(" ", strip=True) for li in lis if not li.find("li")]
        leaves = [t for t in leaves if t and not re.fullmatch(r"\*+", t)]
        has_menu = any("bouclier" in li.get_text().lower() for li in lis)
        closed = (not has_menu) and any(CLOSED_ITEM_RE.match(t) for t in leaves)
        blocks.append({"date": d, "closed": closed, "has_menu": has_menu})
    blocks.sort(key=lambda b: b["date"])
    return blocks


def analyze_page(html: str, today: date) -> dict:
    """{'closed': bool, 'reason': str} pour la date `today`."""
    soup = BeautifulSoup(html, "html.parser")
    block = next((b for b in parse_day_blocks(soup, today.year) if b["date"] == today), None)
    if block is not None and block["has_menu"]:
        return {"closed": False, "reason": "menu publié pour aujourd'hui"}
    if block is not None and block["closed"]:
        return {"closed": True, "reason": "le bloc du jour indique « Fermeture »"}
    if CLOSED_BANNER_RE.search(soup.get_text(" ")):
        return {"closed": True, "reason": "la page indique « Fermé aujourd'hui »"}
    return {"closed": False, "reason": "aucune mention de fermeture"}


def is_closed_today(today: date, retries: int = 3) -> dict:
    """Lit la page du restaurant. Erreur réseau → considéré ouvert."""
    for attempt in range(retries):
        try:
            r = requests.get(PAGE_URL, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            r.raise_for_status()
            return analyze_page(r.text, today)
        except requests.RequestException as e:
            print(f"Page restaurant illisible (essai {attempt + 1}/{retries}) : {e}")
            time.sleep(2 * (attempt + 1))
    return {"closed": False, "reason": "page illisible : on suppose ouvert"}
