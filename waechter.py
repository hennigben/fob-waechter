#!/usr/bin/env python3
"""Fall-Out-Boy-Waechter: schaut regelmaessig auf falloutboy.com nach Neuem.

Jeder Lauf holt Startseite und Sitemap. Hat sich dort etwas geaendert oder sind
seit dem letzten Komplettlauf zehn Minuten vergangen, werden alle bekannten
Seiten geholt. Aenderungen landen als Zeile in ereignisse.log
("<Zeit> WICHTIG|INFO <Text>") und gehen als Push an den ntfy-Kanal
(Umgebungsvariable NTFY_KANAL oder Datei ntfy-kanal.txt). Auf dem Mac erzeugt
WICHTIG zusaetzlich eine Mac-Mitteilung.

Laeuft als GitHub-Action (hennigben/fob-waechter; geplant alle 5 Minuten, GitHub
startet sie aber oft nur etwa stuendlich) und auf dem Mac jede Minute, solange er wach
ist. Beide alarmieren selbst; doppelte Pushs sind der Preis fuer schnelle Alarme.
"""
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

HIER = os.path.dirname(os.path.abspath(__file__))
STAND = os.path.join(HIER, "stand.json")
LOG = os.path.join(HIER, "ereignisse.log")
KANAL = os.path.join(HIER, "ntfy-kanal.txt")
SPERRE = os.path.join(HIER, ".sperre")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HOST = "www.falloutboy.com"
START = f"https://{HOST}/"
SITEMAP = f"https://{HOST}/sitemap.xml"
NEBENSEITEN = ["https://twoquartersarcade.com/"]  # nur Titel und Beschreibung
MALL = "https://twilighttownmall.falloutboy.com/"  # Startseite der Mall, jede Minute
VOLL_ALLE = 600
MAX_SEITEN = 40
STICHWORTE = re.compile(
    r"free|giveaway|\bwin\b|winner|first \d|first to|be among|limited|sign ?up|rsvp|"
    r"register|\benter\b|pre-?sale|tickets?|\btour\b|album|pre-?order|merch|shirt|\btee\b|"
    r"coupon|\bcode\b|\bdrop\b|now open|grand opening|launch|exclusive|reward|prize|"
    r"contest|sweepstake|release|out now|single|video", re.I)
FLUECHTIG = {"form_build_id", "form_token"}


class Leser(HTMLParser):
    """Sammelt sichtbaren Text, Links, Formulare, Skripte, Iframes und Bilder."""

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.text, self.links, self.skripte, self.iframes, self.bilder = [], set(), set(), set(), set()
        self.formulare, self._form, self._still = [], None, 0
        self.titel, self._im_titel = "", False

    def _abs(self, wert):
        return urljoin(self.url, html.unescape(wert or "").strip()).split("#")[0]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self._still += 1
            if tag == "script" and a.get("src"):
                u = self._abs(a["src"])
                self.skripte.add(u.split("?")[0])
        elif tag == "title":
            self._im_titel = True
        elif tag in ("a", "area") and a.get("href"):
            self.links.add(self._abs(a["href"]))
        elif tag == "iframe" and a.get("src"):
            self.iframes.add(self._abs(a["src"]))
        elif tag == "img" and a.get("src"):
            self.bilder.add(self._abs(a["src"]).split("?")[0])
        elif tag == "form":
            self._form = {"action": self._abs(a.get("action") or self.url).split("?")[0], "felder": []}
            self.formulare.append(self._form)
        elif tag in ("input", "select", "textarea") and self._form is not None:
            name = a.get("name")
            if name and name not in FLUECHTIG:
                self._form["felder"].append(name)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._still:
            self._still -= 1
        elif tag == "title":
            self._im_titel = False
        elif tag == "form":
            self._form = None

    def handle_data(self, data):
        if self._im_titel:
            self.titel += data
            return
        if self._still:
            return
        t = re.sub(r"\s+", " ", data).strip()
        if t:
            self.text.append(t)


def holen(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, r.read().decode("utf-8", "replace")


def auswerten(url, roh):
    lp = Leser(url)
    lp.feed(roh)
    intern, extern = set(), set()
    for l in lp.links:
        p = urlparse(l)
        if p.scheme in ("http", "https") and p.netloc == HOST:
            pfad = p.path or "/"
            if not pfad.startswith(("/sites/", "/core/", "/themes/", "/modules/")):
                intern.add(pfad + (("?" + p.query) if p.query and "frontpage" not in p.query else ""))
        elif p.scheme in ("http", "https"):
            extern.add(l)
        elif p.scheme in ("sms", "tel", "mailto"):
            extern.add(l)
    drops = sorted(set(re.findall(r"(?:dropId=|laylo-drop-)([0-9a-f-]{36})", roh)))
    sms = sorted(set(re.findall(r"KEYWORD\s*=\s*'([^']+)'", roh)))
    # Bausteine fuer neue Inhalte, die nicht im sichtbaren Text stehen
    css_klassen = sorted(set(re.findall(r'class="([a-z][a-z0-9_-]*_(?:wrapper|section|popup|modal|form))"', roh, re.I)))
    return {
        "titel": re.sub(r"\s+", " ", lp.titel).strip(),
        "text": lp.text,
        "intern": sorted(intern),
        "extern": sorted(extern),
        "formulare": sorted({f["action"] + " | " + ",".join(f["felder"]) for f in lp.formulare}),
        "skripte": sorted(lp.skripte),
        "iframes": sorted(i.split("?")[0] for i in lp.iframes),
        "bilder": sorted(b for b in lp.bilder if "/files/20" in b),
        "drops": drops,
        "sms": sms,
        "bausteine": css_klassen,
    }


def verzeichnis(roh):
    """Ladenverzeichnis der Startseite: Name -> Link (oder None, solange nicht eroeffnet)."""
    erg = {}
    for stueck in roh.split('class="directory_item"')[1:]:
        stueck = stueck.split('class="directory_item"')[0][:1500]
        name = re.search(r'class="store_name"[^>]*>\s*([^<]+?)\s*<', stueck)
        if not name:
            continue
        href = re.search(r'<a [^>]*href="([^"]+)"', stueck.split("store_name")[0])
        erg[html.unescape(name.group(1)).strip()] = href.group(1) if href else None
    return erg


def kurz(t, n=150):
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def vergleichen(url, alt, neu, ereignisse):
    pfad = urlparse(url).path or "/"
    for feld, stufe, wort in (
        ("formulare", "WICHTIG", "Neues Formular"),
        ("drops", "WICHTIG", "Neuer Laylo-Drop"),
        ("sms", "WICHTIG", "Neues SMS-Stichwort"),
        ("iframes", "WICHTIG", "Neues eingebettetes Element"),
        ("extern", "WICHTIG", "Neuer externer Link"),
        ("intern", "WICHTIG", "Neuer Link"),
        ("skripte", "INFO", "Neues Skript"),
        ("bilder", "INFO", "Neues Bild"),
        ("bausteine", "INFO", "Neuer Seitenbaustein"),
    ):
        neu_dazu = sorted(set(neu.get(feld, [])) - set(alt.get(feld, [])))
        weg = sorted(set(alt.get(feld, [])) - set(neu.get(feld, [])))
        for x in neu_dazu[:6]:
            ereignisse.append((stufe, f"{wort} auf {pfad}: {kurz(x, 180)}"))
        if len(neu_dazu) > 6:
            ereignisse.append((stufe, f"…und {len(neu_dazu) - 6} weitere ({wort}) auf {pfad}"))
        if weg and feld in ("formulare", "drops", "iframes", "intern", "extern"):
            ereignisse.append(("INFO", f"Entfernt auf {pfad} ({feld}): {kurz(', '.join(weg), 180)}"))
    if alt.get("titel") != neu.get("titel"):
        ereignisse.append(("INFO", f"Neuer Titel {pfad}: {kurz(neu.get('titel', ''))}"))
    alt_t, neu_t = set(alt.get("text", [])), neu.get("text", [])
    dazu = [t for t in neu_t if t not in alt_t]
    if dazu:
        zusammen = " / ".join(dict.fromkeys(dazu))
        stufe = "WICHTIG" if STICHWORTE.search(zusammen) else "INFO"
        ereignisse.append((stufe, f"Neuer Text auf {pfad}: {kurz(zusammen, 220)}"))


def push(titel, text, prio=3, klick=None, tags=None):
    """Schickt eine Nachricht an den geheimen ntfy-Kanal (App ntfy auf dem Handy)."""
    kanal = os.environ.get("NTFY_KANAL", "").strip()
    if not kanal:
        try:
            with open(KANAL, encoding="utf-8") as f:
                kanal = f.read().strip()
        except OSError:
            return False
    daten = {"topic": kanal, "title": titel, "message": text[:3500], "priority": int(prio)}
    if klick:
        daten["click"] = klick
    if tags:
        daten["tags"] = tags
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(daten).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "fob-waechter"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def klickziel(texte):
    for t in texte:
        m = re.search(r"https?://[^\s,|]+", t)
        if m:
            return m.group(0)
        m = re.search(r"(?: auf|->) (/[^\s:]*)", t)
        if m:
            return f"https://{HOST}{m.group(1)}"
    return START


def melden(ereignisse):
    jetzt = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        for stufe, text in ereignisse:
            f.write(f"{jetzt} {stufe} {text}\n")
    wichtig = [t for s, t in ereignisse if s == "WICHTIG"]
    info = [t for s, t in ereignisse if s == "INFO"]
    if "--still" not in sys.argv and "--kein-push" not in sys.argv:
        if wichtig:
            push("Fall Out Boy: NEU auf falloutboy.com!", "\n".join(wichtig + info), 5,
                 klickziel(wichtig), ["rotating_light"])
        elif info:
            push("Fall Out Boy: kleine Aenderung", "\n".join(info), 2, klickziel(info), ["mag"])
    if wichtig and "--still" not in sys.argv and sys.platform == "darwin":
        nachricht = kurz(wichtig[0], 200).replace('"', "'").replace("\\", "")
        mehr = f" (+{len(wichtig) - 1})" if len(wichtig) > 1 else ""
        skript = (f'display notification "{nachricht}{mehr}" with title "Fall Out Boy: neu!" '
                  f'sound name "Glass"')
        subprocess.run(["osascript", "-e", skript], check=False, timeout=20)


def main():
    stand = {}
    if os.path.exists(STAND):
        with open(STAND, encoding="utf-8") as f:
            stand = json.load(f)
    erster_lauf = not stand
    seiten = stand.setdefault("seiten", {})
    fehler = stand.setdefault("fehler", {})
    ereignisse = []

    def sicher_holen(url):
        try:
            code, roh = holen(url)
            if fehler.get(url, 0) >= 10:
                ereignisse.append(("INFO", f"Wieder erreichbar: {url}"))
            fehler[url] = 0
            return code, roh
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:  # Netz weg, Zeitueberschreitung
            code = 0
        fehler[url] = fehler.get(url, 0) + 1
        if fehler[url] == 10:
            ereignisse.append(("INFO", f"Seit 10 Versuchen nicht erreichbar ({code}): {url}"))
        return code, None

    # 1. Schnellblick: Startseite und Sitemap
    _, start_roh = sicher_holen(START)
    _, sm_roh = sicher_holen(SITEMAP)
    if start_roh is None:
        stand["letzter_lauf"] = time.time()
        speichern(stand)
        if ereignisse:
            melden(ereignisse)
        return
    start_info = auswerten(START, start_roh)
    fingerabdruck = hashlib.sha256(json.dumps(start_info, sort_keys=True).encode()).hexdigest()
    sm_urls = sorted(set(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm_roh or "")))
    voll = (erster_lauf or fingerabdruck != stand.get("start_hash")
            or (sm_roh is not None and sm_urls != stand.get("sitemap"))
            or time.time() - stand.get("letzter_voll", 0) >= VOLL_ALLE)

    # Ladenverzeichnis
    verz = verzeichnis(start_roh)
    alt_verz = stand.get("verzeichnis", {})
    if not erster_lauf:
        for name, href in verz.items():
            if name not in alt_verz:
                ereignisse.append(("WICHTIG", f"Neuer Laden im Verzeichnis: {name}" + (f" -> {href}" if href else "")))
            elif href and not alt_verz.get(name):
                ereignisse.append(("WICHTIG", f"Laden eroeffnet: {name} -> {href}"))
            elif href != alt_verz.get(name):
                ereignisse.append(("INFO", f"Laden {name}: Link {alt_verz.get(name)} -> {href}"))
        for name in alt_verz:
            if name not in verz:
                ereignisse.append(("INFO", f"Laden verschwunden: {name}"))
    stand["verzeichnis"] = verz

    # Die Mall liegt seit 09.11.2026 auf einer eigenen Subdomain
    _, mall_roh = sicher_holen(MALL)
    if mall_roh is not None:
        m_info = auswerten(MALL, mall_roh)
        m_verz = {n: (urljoin(MALL, h) if h else h) for n, h in verzeichnis(mall_roh).items()}
        m_neu = {"verzeichnis": m_verz, "links": m_info["extern"], "formulare": m_info["formulare"],
                 "drops": m_info["drops"], "sms": m_info["sms"]}
        m_alt = stand.get("mall")
        if m_alt is not None:
            for name, href in m_verz.items():
                if name not in m_alt["verzeichnis"]:
                    ereignisse.append(("WICHTIG", f"Mall: neuer Laden im Verzeichnis: {name}" + (f" => {href}" if href else "")))
                elif href and not m_alt["verzeichnis"].get(name):
                    ereignisse.append(("WICHTIG", f"Mall: Laden eroeffnet: {name} => {href}"))
            for schluessel, wort in (("links", "Link"), ("formulare", "Formular"), ("drops", "Laylo-Drop"), ("sms", "SMS-Stichwort")):
                neu = [x for x in m_neu[schluessel] if x not in m_alt.get(schluessel, [])]
                if neu:
                    ereignisse.append(("WICHTIG", f"Mall: neu ({wort}): {kurz(', '.join(neu), 300)}"))
        stand["mall"] = m_neu

    if sm_roh is not None:
        if not erster_lauf:
            for u in sorted(set(sm_urls) - set(stand.get("sitemap", []))):
                ereignisse.append(("WICHTIG", f"Neue Seite in der Sitemap: {u}"))
        stand["sitemap"] = sm_urls

    if not voll:
        stand["letzter_lauf"] = time.time()
        speichern(stand)
        if ereignisse:
            melden(ereignisse)
        return

    # 2. Komplettlauf ueber alle bekannten Seiten
    ziele = {START}
    ziele.update(u for u in sm_urls if urlparse(u).netloc == HOST)
    ziele.update(f"https://{HOST}{p}" for p in start_info["intern"])
    ziele.update(u for u in seiten if urlparse(u).netloc == HOST)
    ziele = sorted(ziele)[:MAX_SEITEN]
    for url in ziele:
        if url == START:
            info = start_info
        else:
            time.sleep(1)
            code, roh = sicher_holen(url)
            if roh is None:
                if code == 404 and url in seiten and seiten[url].get("status") != 404:
                    ereignisse.append(("INFO", f"Seite weg (404): {url}"))
                    seiten[url] = {"status": 404}
                continue
            info = auswerten(url, roh)
        info["status"] = 200
        alt = seiten.get(url)
        if alt is None and not erster_lauf:
            ereignisse.append(("WICHTIG", f"Neue Seite entdeckt: {url} ({kurz(info['titel'], 80)})"))
        elif alt is not None:
            if alt.get("status") == 404:
                ereignisse.append(("WICHTIG", f"Seite wieder da: {url}"))
            else:
                vergleichen(url, alt, info, ereignisse)
        seiten[url] = info

    for url in NEBENSEITEN:
        time.sleep(1)
        _, roh = sicher_holen(url)
        if roh is None:
            continue
        teile = re.findall(r'<title>([^<]*)</title>|<meta (?:name|property)="(?:description|og:title|og:description)" content="([^"]*)"', roh)
        kopf = " | ".join("".join(t).strip() for t in teile)
        alt = stand.setdefault("nebenseiten", {}).get(url)
        if alt is not None and alt != kopf:
            ereignisse.append(("INFO", f"{url} hat neuen Kopf: {kurz(kopf, 200)}"))
        stand["nebenseiten"][url] = kopf

    stand["start_hash"] = fingerabdruck
    stand["letzter_voll"] = time.time()
    stand["letzter_lauf"] = time.time()
    speichern(stand)
    if erster_lauf:
        ereignisse.append(("INFO", f"Grundstand angelegt: {len(seiten)} Seiten, {len(verz)} Laeden im Verzeichnis"))
    if ereignisse:
        melden(ereignisse)


def speichern(stand):
    if os.environ.get("GITHUB_ACTIONS"):
        # In der Cloud laeuft jeder Lauf komplett; ohne Zeitstempel aendert sich
        # stand.json nur, wenn sich auf der Seite etwas aendert.
        stand = {**stand, "letzter_lauf": 0, "letzter_voll": 0}
    tmp = STAND + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stand, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STAND)


if __name__ == "__main__":
    # Eigene Nachricht schicken: waechter.py --push "Titel" "Text" [Prioritaet 1-5] [Link]
    if len(sys.argv) >= 4 and sys.argv[1] == "--push":
        a = sys.argv[2:]
        ok = push(a[0], a[1], a[2] if len(a) > 2 else 4, a[3] if len(a) > 3 else None)
        print("gesendet" if ok else "FEHLER beim Senden")
        sys.exit(0 if ok else 1)
    import fcntl
    with open(SPERRE, "w") as sperre:
        for _ in range(90):  # Schleife und stuendliche Aufgabe nie gleichzeitig
            try:
                fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(1)
        else:
            sys.exit(0)
        main()
