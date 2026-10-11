"""Parst Titel/Jahr/S/E aus Dateinamen und Captions (xRelease/Scene-Konventionen)."""
from __future__ import annotations

import re

EXT_RE = re.compile(r"\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v|ts|mpg|mpeg|3gp|divx)$", re.I)
QUALITY_TOKENS = {
    "x264", "x265", "h264", "h265", "hevc", "avc", "1080p", "1080i", "720p", "2160p", "480p", "4k",
    "bluray", "bdrip", "brrip", "dvdrip", "dvdscr", "webrip", "web", "webdl", "web-dl", "hdtv", "hdts",
    "hdcam", "cam", "ts", "telesync", "tc", "proper", "repack", "extended", "uncut", "remux",
    "aac", "ac3", "dd5", "ddp5", "dts", "dtshd", "dts-hd", "hd", "fhd", "uhd", "sdr", "hdr", "hdr10",
    "multi", "german", "deutsch", "ger", "dub", "dubbed", "sub", "subbed", "10bit", "8bit",
    "xvid", "divx", "imax", "limited", "unrated", "internal", "seeded", "by", "group",
    "dd51", "dd5", "ddp5", "dl", "nfuhd", "uhd", "18p", "tvs", "4sf", "webdl",
}
# Genre-/Beschreibungs-Suffixe, die in der Caption oft NACH "Titel -" kleben
# ("Masters of the Universe - Fantasy Sci-Fi Abenteuer Action", "Mutiny -
# Keine Gnade auf hoher See"). Sobald eines auftaucht ist der Film-Titel zu Ende.
GENRE_TOKENS = {
    "komödie", "komoedie", "drama", "action", "abenteuer", "fantasy", "sci-fi", "science",
    "thriller", "horror", "romantik", "romanze", "liebesfilm", "familie", "family", "familienfilm",
    "western", "kriegsfilm", "dokumentarfilm", "doku", "musical", "musik", "unsagbar",
    "zeichentrick", "animation", "krimi", "mystery", "kriminalfilm", "biopic", "geschichte",
    "historisch", "satire", "schwarze", "slapstick", "teenie", "vorabversion", "vorab", "trailer",
    "hardbranding", "komplett", "german", "ut", "deutsch", "grosse", "kategorie",
}
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
SE_RE = re.compile(r"\bS(\d{1,2})[\s._-]?E(\d{1,3})\b", re.I)
SE2_RE = re.compile(r"(?:^|[\s._-])(\d{1,2})x(\d{2,3})(?:$|[\s._-])")
NUM_RE = re.compile(r"^\d{2,5}[pi]?$", re.I)


def parse_title(raw: str | None) -> dict:
    """-> {title, year, season, episode, media_kind}. raw = filename oder Caption-Zeile."""
    out = {"title": "", "year": None, "season": None, "episode": None, "media_kind": "movie"}
    if not raw:
        return out
    text = EXT_RE.sub("", raw.strip())

    # Caption-Markdown-Link '[Mindhunter](https://.../serie/mindhunter)' -> Reihenname
    md_title = ""
    m = re.search(r"\[([^\]\[]+)\]\([^)]*\)", text)
    if m:
        md_title = m.group(1).strip(" \t:!?.;-")

    # 1) Serie/Episode erkennen und abschneiden
    head = text
    m = SE_RE.search(text)
    se_after = None
    if m:
        out["season"], out["episode"] = int(m.group(1)), int(m.group(2))
        out["media_kind"] = "episode"
        head = text[: m.start()]
        se_after = text[m.end():]
    else:
        m2 = SE2_RE.search(text)
        if m2:
            out["season"], out["episode"] = int(m2.group(1)), int(m2.group(2))
            out["media_kind"] = "episode"
            head = text[: m2.start()] + " " + text[m2.end():]

    # 2) Jahr: letzter 4er-Block >=1920 im verbleibenden Text
    years = list(YEAR_RE.finditer(head))
    years = [y for y in years if int(y.group()) >= 1920] or years
    if years:
        pick = years[-1]
        out["year"] = int(pick.group())
        head = head[: pick.start()] + " " + head[pick.end():]

    # 3) Klammer-Tags + Kanal-Wasserzeichen (@Handle, -1, (1)) entfernen
    head = re.sub(r"[\[({][^\])}]*[\])}]", " ", head)
    head = re.sub(r"[\[({].*$", " ", head)  # ABGESCHNOFFENE Klammer (Truncation) -> Rest weg
    head = re.sub(r"@[\w_]+", " ", head)
    head = re.sub(r"[\s._-]\d{1,2}$", " ", head)  # Anhaenge wie '-1', '_2'

    # 4) Trennzeichen normalisieren: Punkt/Unterstrich -> Leerzeichen, " - " -> Leerzeichen
    head = re.sub(r"[._]+", " ", head)
    head = re.sub(r"\s+-\s+", " ", head)

    # 5) Titel = Woerter bis zum ersten Quality-/Rest-Token
    words, trailing = [], []
    for wtok in head.split():
        if out["media_kind"] == "movie" and (
                wtok.lower() in QUALITY_TOKENS or NUM_RE.match(wtok) or wtok.isdigit()):
            break
        trailing.append(wtok)
    # Episode: fuehrende Zahlen am Ende (Staffel-/Episodennummer) entfernen
    title = " ".join(trailing)
    if out["media_kind"] == "episode":
        title = re.sub(r"(\s+\d{1,3})+\s*$", "", title)
    out["title"] = re.sub(r"\s{2,}", " ", title).strip(" -._")
    # Caption-Markdown-Link bevorzugen, wenn der extrahierte Titel Muell ist
    # ('[Mindhunter](url)... USA ... Staffel 2 ...' -> 'Mindhunter').
    if md_title and not _looks_messy(md_title) and (not out["title"] or _looks_messy(out["title"])):
        out["title"] = md_title
    else:
        # Bracket-first: SE-Tag VOR dem Serientitel ('[S01.E01] S.W.A.T. - Pilotfolge').
        if (not out["title"] or is_shortjunk(out["title"])) and se_after:
            cand = se_after
            part = re.split(r"\s+[-–|]\s+", cand, 1)[0]
            part = re.sub(r"[\[\](){}]", " ", part)
            part = re.sub(r"[._]+", " ", part)
            part = re.sub(r"\s{2,}", " ", part).strip(" -._")
            if part and not is_shortjunk(part):
                if out["media_kind"] == "episode":
                    part = re.sub(r"(\s+\d{1,3})+\s*$", "", part)
                out["title"] = part
    # Scene-Release-Restschmutz abstreifen ('4Sf-Mindhunter-1080P' -> 'Mindhunter').
    cleaned = _scene_clean(out["title"])
    if cleaned:
        out["title"] = cleaned
    return out


def _looks_messy(t: str) -> bool:
    """Caption-Head mit Werbe-/Listen-Muell, der kein Titel ist."""
    t = (t or "").strip()
    if not t:
        return True
    low = t.lower()
    if "|" in t or "staffel" in low or "episoden" in low or "folge" in low:
        return True
    letters = re.sub(r"[^a-z0-9]", "", t, flags=re.I)
    return len(letters) < 3


def _scene_clean(t: str) -> str:
    """Scene-Release-Dateinamen-Titel von Qualitaets-/Resolution-/Audiotags befreien
    ('4Sf-Mindhunter-1080P' -> 'Mindhunter', 'Tvs-Mindhunter-Dd51-Dl-18P-X264' -> 'Mindhunter')."""
    if not t:
        return ""
    parts = [x for x in t.split("-") if x.strip()]
    kept = []
    for i, p in enumerate(parts):
        pl = p.strip().lower().rstrip(".").lstrip(".")
        if not pl:
            continue
        if pl in QUALITY_TOKENS:
            continue
        if re.match(r"^\d{2,4}[pi]?$", pl):
            continue
        if re.match(r"^dd\d{1,2}$", pl):
            continue
        if i == 0 and len(p) <= 4 and (any(c.isdigit() for c in p) or sum(c.isupper() for c in p) >= 2 or pl in ("tvs", "4sf", "nf", "web")):
            continue  # fuehrender Release-Group-Code (z.B. '4Sf', 'TVS', 'NF')
        kept.append(p)
    r = "-".join(kept)
    return re.sub(r"\s{2,}", " ", r).strip(" -._")


def is_shortjunk(t: str) -> bool:
    """Sehr kurzer/Symbol-Junk-Titel ('[', '-', '.', '..'). Als 'leer' behandeln."""
    t = (t or "").strip()
    letters = re.sub(r"[^a-z0-9]", "", t, flags=re.I)
    return len(letters) < 3


def search_variants(title: str, max_variants: int = 5) -> list[str]:
    """Abgestufte Suchvarianten, damit Werbe-/Genre-Suffixe die Erkennung
    nicht mehr ausbremsen. Erst der volle Titel, dann immer krasser gekuerzt:
    1) voll    2) ohne Genre-/Werbe-Woerter   3) bis zum ersten Trenner
    4+) Woerter von hinten (max. bis 2 Woerter uebrig)."""
    t = (title or "").strip()
    if not t:
        return []
    out = [t]
    # ohne Genre-/Werbe-Woerter
    words = t.split()
    core = [w for w in words if w.lower().lstrip("*#-_ ").rstrip(".,;:!? ") not in GENRE_TOKENS]
    if core and " ".join(core) != t:
        out.append(" ".join(core))
    # bis zum ersten Trenner (-, |, –)
    m = re.split(r"\s+[-–|]\s+", t)
    if len(m) > 1 and m[0].strip():
        out.append(m[0].strip())
    # Woerter von hinten wegnehmen bis zum ERSTEN Wort (deckt Untertitel/Loglines
    # ab: 'Mutiny Keine Gnade auf hoher See' -> 'Mutiny'). Das exact-Vertrauens-
    # Gate verhindert falsche Einzelwort-Treffer (nur ein Titel-Exaktmatch zaehlt).
    for n in range(len(words) - 1, 0, -1):
        cand = " ".join(words[:n]).strip(" -–|,:;")
        if cand and cand not in out:
            out.append(cand)
        if len(out) >= max_variants:
            break
    return out[:max_variants]


def parse_caption(caption: str | None, filename: str | None) -> dict:
    """Versucht zuerst die Caption (oft 'Titel (Jahr)'), sonst den Dateinamen."""
    best = {"title": "", "year": None, "season": None, "episode": None, "media_kind": "movie", "source": ""}
    for src, raw in (("caption", caption), ("filename", filename)):
        if not raw:
            continue
        line = raw.strip().splitlines()[0] if src == "caption" else raw
        p = parse_title(line)
        better = p["title"] and (not best["title"] or (p["year"] and not best["year"]))
        if better:
            p["source"] = src
            best = p
        if best["year"] and best["title"]:
            break
    return best

def parse_media_specs(filename: str | None, caption: str | None = None) -> dict:
    """Extrahiert robuste Variantenmerkmale aus Dateiname und Caption."""
    text = " ".join(x for x in (filename or "", caption or "") if x).lower()
    quality = ""
    for token, label in (("2160p", "2160p"), ("4k", "4K"), ("uhd", "UHD"),
                         ("1080p", "1080p"), ("1080i", "1080i"), ("720p", "720p"),
                         ("480p", "480p")):
        if token in text:
            quality = label
            break
    codec = ""
    if re.search(r"\b(?:x265|h[ ._-]?265|hevc)\b", text):
        codec = "H.265/HEVC"
    elif re.search(r"\b(?:x264|h[ ._-]?264|avc)\b", text):
        codec = "H.264/AVC"
    elif re.search(r"\bav1\b", text):
        codec = "AV1"
    audio = []
    if re.search(r"\b(?:german|deutsch|ger|dt)\b", text):
        audio.append("DE")
    if re.search(r"\b(?:english|englisch|eng)\b", text):
        audio.append("EN")
    hdr = bool(re.search(r"\b(?:hdr|hdr10|dolby[ .-]?vision|dv)\b", text))
    subtitles = bool(re.search(r"\b(?:sub|subs|subbed|subtitle|untertitel|forced|retail)\b", text))
    release_type = ""
    for token, label in (("bluray", "BluRay"), ("remux", "Remux"),
                         ("web-dl", "WEB-DL"), ("webdl", "WEB-DL"),
                         ("webrip", "WEBRip"), ("hdtv", "HDTV"), ("dvdrip", "DVDRip")):
        if token in text:
            release_type = label
            break
    return {"quality": quality, "codec": codec, "audio_lang": "+".join(audio),
            "hdr": int(hdr), "subtitles": int(subtitles), "release_type": release_type}


def is_junk_title(title: str | None) -> bool:
    """Erkennt Importreste/Junk-Titel, die nicht neu gematcht werden sollen."""
    t = (title or "").strip()
    if not t or len(t) <= 2 or len(t) > 200:
        return True
    if re.search(r"\bS\d{1,2}E\d{1,3}\b", t, re.I):
        return True
    if "plot:" in t.lower() or ".mp4" in t.lower() or "`" in t:
        return True
    return False
