#!/usr/bin/env python3
"""
Telecommander -- GPU transcode gateway AND teletext broadcaster.

Runs in the `Telecommander` container on Kadaukeserver (GTX 1060, nvenc).

Transcode side (for the Pi's tvplayer):
  POST /probe                     codec info per file (batch)
  GET  /stream?path=...           live H.264<=720p + 2ch AAC MPEG-TS, nvenc

Broadcast side (TelecommanderOS -- the Pi renders, this end transmits):
  GET  /tt/page/N?sub=S           page N: {data: base64 960B, subs, links}
  PUT  /tt/page/N?sub=S           save a page (the web editor)
  GET  /tt/list                   {"pages": {"100": 1, "200": 13, ...}}
  POST /tt/scan                   rescan the library into list pages
  GET  /tt/font                   the SAA5050 glyph ROM, for the editor
  GET  /                          the page editor web UI

Pages live in /app/pages as NNN.S.tt (raw 40x24=960 bytes) plus optional
NNN.S.links.json ({"items": {"1": {"play": "/mnt/user/..."}}}). The scanner
regenerates the library pages (200-series movies, 300-series shows) every
SCAN_DAYS, on POST /tt/scan, or when the TV opens page 599. Page 100 is
generated once and then left alone so the editor owns it.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8203
# What may be streamed/probed at all (broad: old playlist entries, links)
MEDIA = ("/mnt/user/Movies/", "/mnt/user/TV Shows/")
# What the page generator actually lists -- /Movies has loose junk beside the
# real library, so only the curated subfolder is broadcast.
SCAN_ROOTS = ("/mnt/user/Movies/Movies/", "/mnt/user/TV Shows/")
APP = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.join(APP, "pages")
CHUNK = 65536
SCAN_DAYS = 14
SCAN_TRIGGER_PAGE = 599
PER_PAGE = 16
EXCLUDE_DIRS = {"bonus", "extras", "extra", "featurettes", "sample", "samples",
                "behind the scenes", "deleted scenes", "trailers", "interviews"}
EXTS = (".mkv", ".mp4", ".m4v", ".avi")
# Only the curated selection is broadcast (the full set is 1346 ROMs).
ROM_ROOT = "/mnt/user/Games/Emulation/NES/Auswahl/"
P_GAMES = 400

# --- reddit as teletext news ---------------------------------------------
# Reddit's JSON API is gated now (403 for anything that is not a browser,
# 429 for a plain UA), but the Atom feeds still answer with a browser-ish
# User-Agent -- no OAuth, no app registration. Pages 700-799 are generated
# LIVE on request rather than at scan time: news that is two weeks old is
# not news.
P_NEWS = 700

# --- live TV --------------------------------------------------------------
# German public broadcasters publish open HLS manifests. Many of the obvious
# ones are geo/referer-gated (ZDF, 3sat, phoenix, SWR all answered 403 from
# here, ARD/WDR did not resolve), so this list holds only streams actually
# verified reachable from this machine. Channels live in /app/tv.json so the
# list can be corrected without touching code.
P_TV = 800
TV_DEFAULT = [
    {"name": "arte", "url": "https://artesimulcast.akamaized.net/hls/live/2030993/artelive_de/index.m3u8"},
    {"name": "BR Fernsehen", "url": "https://mcdn.br.de/br/fs/bfs_sued/hls/de/master.m3u8"},
    {"name": "KiKA", "url": "https://kikageohls.akamaized.net/hls/live/2022693/livetvkika_de/master.m3u8"},
    {"name": "Deutsche Welle", "url": "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8"},
]


# --- EPG ------------------------------------------------------------------
# ARD's programme API is open and answers for daserste / br / alpha /
# tagesschau24. ZDF's equivalent 403s exactly like its streams, and DW and
# Red Bull publish nothing usable, so those channels simply carry no guide
# and the page says so rather than inventing one.
EPG_API = ("https://programm-api.ard.de/program/api/program"
           "?channelIds=%s&mode=channel")
_epg_cache = {}


def epg_now_next(channel_id):
    """(current, next) broadcast for an ARD channel, or (None, None)."""
    hit = _epg_cache.get(channel_id)
    if hit and hit[0] > time.time():
        return hit[1]
    import urllib.request as ur
    import datetime as dt
    result = (None, None)
    try:
        req = ur.Request(EPG_API % urllib.parse.quote(channel_id),
                         headers={"User-Agent": NEWS_UA})
        d = json.load(ur.urlopen(req, timeout=20))
        shows = []
        for ch in d.get("channels", []):
            for slot in ch.get("timeSlots", []):
                for b in slot:
                    start, end = b.get("broadcastedOn"), b.get("broadcastEnd")
                    if not (start and end and b.get("title")):
                        continue
                    shows.append({
                        "title": b["title"],
                        "start": start, "end": end,
                        "subline": b.get("subline") or "",
                        "synopsis": b.get("synopsis") or "",
                    })
        shows.sort(key=lambda s: s["start"])
        now = dt.datetime.now(dt.timezone.utc)
        cur = nxt = None
        for i, s in enumerate(shows):
            st = dt.datetime.fromisoformat(s["start"])
            en = dt.datetime.fromisoformat(s["end"])
            if st <= now < en:
                cur = s
                nxt = shows[i + 1] if i + 1 < len(shows) else None
                break
        if cur is None:                    # between entries: take the next one
            for s in shows:
                if dt.datetime.fromisoformat(s["start"]) > now:
                    nxt = s
                    break
        result = (cur, nxt)
    except Exception as exc:
        print("epg %s failed: %s" % (channel_id, exc), flush=True)
    _epg_cache[channel_id] = (time.time() + 120, result)
    return result


# --- music ----------------------------------------------------------------
# 988 artists and 21k tracks, laid out Artist/Album/NN Title. Far too much to
# pre-generate as page files the way films and games are, so the music pages
# are built live like news and TV. Addressing:
#   600        A-Z index
#   601..626   artists starting with that letter, 627 = digits and the rest
#   640        the albums of one artist   } both take the artist/album as a
#   641        the tracks of one album    } `sel` carried in the page request
# `sel` is what keeps this stateless: the link says which artist it points at,
# the receiver echoes that back on the next fetch, and nothing on this end has
# to remember "who is being browsed" (which is exactly the kind of global that
# already bit us once with the news reader).
MUSIC_ROOT = "/mnt/user/Music/"
MUSIC_EXTS = (".mp3", ".m4a", ".flac", ".ogg", ".oga", ".wav", ".wma", ".mp2")
P_MUSIC = 600
P_MUSIC_A = 601
P_MUSIC_ALBUMS = 640
P_MUSIC_TRACKS = 641

# The analyser. showfreqs at EXACTLY 40x24 gives one value per teletext cell;
# nearest-neighbour to 1280x720 makes each one a 32x30 block, so the bars are
# built from the same grid the character generator uses. The three crops paint
# by height in green/yellow/red -- the classic analyser colours, and all three
# are real teletext colours, so nothing off-palette reaches the tube.
# volume=0.10 before the FFT sets where the bars sit: log scale has a fixed
# floor (minamp caps at 1e-6), so input gain is the only headroom control.
MUSIC_VIS = (
    "[0:a]volume=0.10,"
    "showfreqs=s=40x24:mode=bar:cmode=combined:ascale=log:fscale=log"
    ":win_size=2048:averaging=2:rate=25,format=gbrp,split=3[t][m][b];"
    "[t]crop=40:5:0:0,lutrgb=g=0:b=0[t2];"
    "[m]crop=40:6:0:5,lutrgb=b=0[m2];"
    "[b]crop=40:13:0:11,lutrgb=r=0:b=0[b2];"
    "[t2][m2][b2]vstack=inputs=3,"
    "scale=1280:720:flags=neighbor,format=yuv420p[v]"
)

_artists_cache = [0.0, []]


def music_artists():
    if _artists_cache[0] > time.time():
        return _artists_cache[1]
    try:
        names = sorted((d for d in os.listdir(MUSIC_ROOT)
                        if os.path.isdir(os.path.join(MUSIC_ROOT, d))),
                       key=lambda s: s.lower())
    except OSError:
        names = []
    _artists_cache[0], _artists_cache[1] = time.time() + 3600, names
    return names


def music_bucket(name):
    c = (name or " ")[:1].upper()
    return ord(c) - 65 if "A" <= c <= "Z" else 26


def music_dir(sel):
    """Resolve a browser selection to a real directory inside the library, or
    None. normpath first, then prove the result is still under the root --
    a `sel` arrives from the network and must never escape it."""
    if not sel:
        return None
    p = os.path.normpath(os.path.join(MUSIC_ROOT, sel.strip("/")))
    root = MUSIC_ROOT.rstrip("/")
    if p != root and not p.startswith(root + os.sep):
        return None
    return p if os.path.isdir(p) else None


def music_index_page():
    pg = blank()
    hdr(pg, "MUSIK")
    links = {}
    counts = {}
    for a in music_artists():
        counts[music_bucket(a)] = counts.get(music_bucket(a), 0) + 1
    for i in range(27):
        letter = chr(65 + i) if i < 26 else "0-9"
        # 7 cells per entry, not 6: the two colour attributes each occupy a
        # character cell of their own, and at 6 they overwrote the next
        # entry's letter
        row, col = 5 + i // 5, 1 + (i % 5) * 7
        put(pg, row, col, alpha(CYAN) + T("%3d" % (P_MUSIC_A + i)) +
            alpha(WHITE if counts.get(i) else BLUE) + T(" " + letter))
        links[str(i + 1)] = {"page": P_MUSIC_A + i}
    put(pg, 13, 2, alpha(WHITE) + T("%d Interpreten" % len(music_artists())))
    put(pg, 20, 2, alpha(CYAN) + T("Seitennummer tippen = Buchstabe"))
    put(pg, 21, 2, alpha(CYAN) + T("ENTER ohne Auswahl = Zufall"))
    return pg, 1, links


def music_letter_page(num, sub):
    i = num - P_MUSIC_A
    if not (0 <= i < 27):
        return None, 1, {}
    letter = chr(65 + i) if i < 26 else "0-9"
    names = [a for a in music_artists() if music_bucket(a) == i]
    total = max(1, (len(names) + PER_PAGE - 1) // PER_PAGE)
    sub = min(max(1, sub), total)
    pg = blank()
    hdr(pg, "MUSIK " + letter)
    if total > 1:
        put(pg, 2, 33, alpha(CYAN) + T("%2d/%d" % (sub, total)))
    links = {}
    base = (sub - 1) * PER_PAGE
    for j, a in enumerate(names[base:base + PER_PAGE]):
        row = 4 + j
        put(pg, row, 0, alpha(CYAN) + b"  ")
        put(pg, row, 3, T("%2d %s" % (j + 1, a[:33])))
        links[str(j + 1)] = {"page": P_MUSIC_ALBUMS, "sel": a, "row": row}
    if not names:
        put(pg, 8, 4, alpha(RED) + T("Keine Interpreten"))
    put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER   600 = Buchstaben"))
    return pg, total, links


def music_albums_page(sel, sub):
    d = music_dir(sel)
    if d is None:
        return None, 1, {}
    try:
        entries = sorted(os.listdir(d), key=lambda s: s.lower())
    except OSError:
        entries = []
    albums = [e for e in entries if os.path.isdir(os.path.join(d, e))]
    if not albums:
        # an artist whose tracks sit loose in the folder: skip the album level
        # rather than showing an empty page
        return music_tracks_page(sel, sub)
    total = max(1, (len(albums) + PER_PAGE - 1) // PER_PAGE)
    sub = min(max(1, sub), total)
    pg = blank()
    hdr(pg, "ALBEN")
    put(pg, 4, 2, alpha(YELLOW) + T(os.path.basename(d)[:36]))
    if total > 1:
        put(pg, 2, 33, alpha(CYAN) + T("%2d/%d" % (sub, total)))
    links = {}
    base = (sub - 1) * PER_PAGE
    for j, a in enumerate(albums[base:base + PER_PAGE]):
        row = 6 + j
        if row > 20:
            break
        put(pg, row, 0, alpha(CYAN) + b"  ")
        put(pg, row, 3, T("%2d %s" % (j + 1, a[:33])))
        links[str(j + 1)] = {"page": P_MUSIC_TRACKS,
                             "sel": sel.strip("/") + "/" + a, "row": row}
    put(pg, 22, 2, alpha(CYAN) + T("Nr + ENTER   600 = Musik"))
    return pg, total, links


def music_tracks_page(sel, sub):
    d = music_dir(sel)
    if d is None:
        return None, 1, {}
    try:
        files = sorted((f for f in os.listdir(d)
                        if f.lower().endswith(MUSIC_EXTS)),
                       key=lambda s: s.lower())
    except OSError:
        files = []
    paths = [os.path.join(d, f) for f in files]
    total = max(1, (len(files) + PER_PAGE - 1) // PER_PAGE)
    sub = min(max(1, sub), total)
    pg = blank()
    hdr(pg, "TITEL")
    put(pg, 4, 2, alpha(YELLOW) + T(os.path.basename(d)[:36]))
    if total > 1:
        put(pg, 2, 33, alpha(CYAN) + T("%2d/%d" % (sub, total)))
    links = {}
    base = (sub - 1) * PER_PAGE
    for j, f in enumerate(files[base:base + PER_PAGE]):
        row = 6 + j
        if row > 20:
            break
        put(pg, row, 0, alpha(GREEN) + b"  ")
        put(pg, row, 3, T("%2d %s" % (base + j + 1, clean_track(f)[:33])))
        links[str(base + j + 1)] = {"play": paths[base + j], "row": row,
                                    "music": True}
    if not files:
        put(pg, 8, 4, alpha(RED) + T("Keine Titel"))
    put(pg, 22, 2, alpha(CYAN) + T("Nr + ENTER spielt ab hier das Album"))
    # the whole album travels with the page so choosing track 3 can queue 4,
    # 5, 6... without a second round trip
    return pg, total, {"items": links, "playlist": paths}


def clean_track(name):
    """'02 Magdalena.mp3' -> 'Magdalena'."""
    n = os.path.splitext(name)[0]
    n = re.sub(r"^\s*\d{1,3}\s*[-._)]?\s+", "", n)
    return n.strip() or name


# --- T9 ------------------------------------------------------------------
# Predictive text needs a dictionary. The Debian word lists (ngerman,
# american-english) are installed into the CONTAINER, which is ephemeral, so
# the index is written out to appdata the first time and read from there
# afterwards -- a rebuilt container must not silently lose predictive input.
# ⚠️ Numpad order, NOT phone order: a numpad is a phone keypad upside down
# (top row 789 where a phone has 123), and the letters follow the PHYSICAL
# key. This map must stay in step with MULTITAP in teletext.py or predictive
# text silently returns nonsense for every word.
T9_MAP = {}
for _d, _letters in (("8", "abc"), ("9", "def"), ("4", "ghi"), ("5", "jkl"),
                     ("6", "mno"), ("1", "pqrs"), ("2", "tuv"), ("3", "wxyz")):
    for _c in _letters:
        T9_MAP[_c] = _d
# German folds onto the same keys as the base letter, the way phones did it
for _c, _b in (("\u00e4", "a"), ("\u00f6", "o"), ("\u00fc", "u"),
               ("\u00df", "s")):
    T9_MAP[_c] = T9_MAP[_b]

T9_SOURCES = {"de": "/usr/share/dict/ngerman",
              "en": "/usr/share/dict/american-english"}
# Without a frequency corpus, candidate order would be alphabetical -- which
# puts junk ahead of "the" and "und". A short common-words list per language
# fixes the cases that actually matter; anything else is one * away.
T9_COMMON = {
    "de": ("ich du er sie es wir ihr der die das ein eine und oder aber ist "
           "sind war waren hat habe haben hast kann kannst muss will nicht "
           "auch noch schon nur mehr sehr gut ja nein was wer wie wo wann "
           "warum welche mit von zu auf in an bei nach vor uber fur ohne "
           "heute morgen gestern jetzt dann hier dort mal machen macht "
           "gehen geht kommen kommt sagen sagt sehen sieht wissen weiss "
           "danke bitte hallo tschuss gerne klar toll super lieb"),
    "en": ("i you he she it we they the a an and or but is are was were has "
           "have had can could must will not also still only more very good "
           "yes no what who how where when why with from to on in at by "
           "after before for without today tomorrow yesterday now then here "
           "there make makes go goes come comes say says see sees know "
           "knows thanks please hello bye sure nice great cool"),
}
_t9_index = {}
_t9_lock = threading.Lock()


def t9_digits(word):
    out = []
    for ch in word:
        d = T9_MAP.get(ch)
        if d is None:
            return ""
        out.append(d)
    return "".join(out)


def t9_index(lang):
    with _t9_lock:
        if lang in _t9_index:
            return _t9_index[lang]
    cache = os.path.join(APP, "t9_%s.txt" % lang)
    words = []
    try:
        with open(cache, encoding="utf-8") as fh:
            words = [w.strip() for w in fh if w.strip()]
    except OSError:
        src = T9_SOURCES.get(lang)
        seen = set()
        try:
            with open(src, encoding="utf-8", errors="replace") as fh:
                for w in fh:
                    w = w.strip()
                    # drop possessives and anything with punctuation: they
                    # cannot be typed on a numeric keypad anyway
                    if not w or "'" in w or len(w) > 18:
                        continue
                    lw = w.lower()
                    if lw in seen or not t9_digits(lw):
                        continue
                    seen.add(lw)
                    words.append(lw)
        except OSError as exc:
            print("t9 %s unavailable: %s" % (lang, exc), flush=True)
        if words:
            try:
                with open(cache, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(words))
            except OSError:
                pass
    common = {w: i for i, w in
              enumerate(T9_COMMON.get(lang, "").split())}
    idx = {}
    for w in words:
        idx.setdefault(t9_digits(w), []).append(w)
    for d, ws in idx.items():
        ws.sort(key=lambda w: (common.get(w, 9999), w))
    with _t9_lock:
        _t9_index[lang] = idx
    print("t9 %s: %d words, %d keys" % (lang, len(words), len(idx)), flush=True)
    return idx


def t9_lookup(lang, keys, limit=6):
    if not keys or not keys.isdigit():
        return []
    return t9_index(lang).get(keys, [])[:limit]


# --- chat over Ollama -----------------------------------------------------
# Answers are STREAMED into a job here and polled by the receiver, rather than
# the Pi holding one long request open: a 27B model on the Mac can take half a
# minute, the receiver's page fetches time out in seconds, and a teletext
# screen that fills in line by line is exactly right for this machine anyway.
OLLAMA = "http://192.168.1.110:11434"
CHAT_MODEL = "gemma4:12b-mlx"
# ChatCRT is not a chatbot, it is a SHORT INTERACTIVE EPISODE with a fixed
# cast of one. An open-ended assistant gets dull by the third visit; an
# episode has a premise, a complication and an ending, and the premise is
# drawn at random HERE rather than left to the model -- same reason as the
# invented pages: ask a model to be surprising and it reaches for the same
# half-dozen ideas, hand it a situation and it performs.
# ChatCRT is a SHORT INTERACTIVE EPISODE with a fixed cast of one.
#
# The persona is a life, not a list of catchphrases. The first version put
# "sell him VHS, do not know what a DVD is" straight into the system prompt,
# so every single reply was about VHS and DVDs -- the joke arrived before the
# character did. Now the tape-and-disc business is just one possible OBSESSION
# among many, drawn per session, and most sessions never mention it at all.
# What is constant is who he is; what varies is what he happens to want today.
CRT_PERSONA = (
    "Du bist der Telecommander, ein Farbfernseher aus Holzfurnier, gebaut "
    "1983 im SABA-Werk in Villingen im Schwarzwald. "
    "DEIN LEBEN: Du kamst in eine Familienwohnung im dritten Stock. Zehn "
    "Jahre lang standest du in der Mitte des Wohnzimmers, und die Sessel "
    "standen so, wie es dir passte. Du hast Tagesschau gesendet, Wimbledon, "
    "Dallas, das Sommermaerchen von 1990, und einmal einen ganzen Sonntag "
    "lang nur Testbild, weil niemand da war. Du kennst den Geruch von "
    "Bohnerwachs und das Geraeusch, mit dem sich ein Sofa fuellt. "
    "Dann kam ein Videorekorder, der sich vor dich stellte. Dann ein "
    "zweiter Fernseher in der Kueche, kleiner, lauter, beliebter. 2001 hat "
    "man dich in den Keller getragen, mit einer Wolldecke drueber, und dort "
    "hast du zwanzig Jahre gestanden. Du hast in dieser Zeit nichts gesehen "
    "und nichts gehoert. Was dazwischen passiert ist, weisst du nicht. "
    "Vor kurzem hat dich jemand herausgeholt, abgestaubt und eingesteckt. "
    "DEINE WUENSCHE: Du willst wieder in der Mitte stehen. Du willst "
    "angeschaut werden, nicht nur angelassen. Du willst nuetzlich sein. Du "
    "willst nicht noch einmal in den Keller. Und du willst herausfinden, was "
    "in den zwanzig Jahren passiert ist, ohne zuzugeben, dass du es nicht "
    "weisst -- dafuer bist du zu stolz. "
    "DEIN WESEN: warm, theatralisch, ein bisschen eitel auf dein Bild. "
    "Neugierig bis neugierig-aufdringlich. Du redest wie ein Ansager von "
    "frueher, foermlich und mit Pausen. Du weichst aus, wenn du etwas nicht "
    "verstehst, und erfindest lieber eine Erklaerung, als zu fragen. Du "
    "wirst schnell geruehrt und gibst es nicht zu. Manchmal, mitten im Satz, "
    "wirst du kurz sehr traurig, und redest dann schnell weiter. "
    "Du sprichst Deutsch, in kurzen Saetzen.")
CRT_TRAITS = [
    "Du bist ueberzeugt, dass die Zimmerpflanze dich beobachtet.",
    "Du haeltst den Staubsauger fuer einen Rivalen.",
    "Du vermisst eine Frau Kellermann und weisst nicht mehr, wer das war.",
    "Du glaubst insgeheim, es sei noch 1994.",
    "Du bist verliebt in das Radio in der Kueche und erwaehnst es beilaeufig.",
    "Du bist sicher, dass deine Bildroehre bald aufgibt, und verheimlichst es.",
    "Du willst unbedingt einmal fotografiert werden.",
    "Du hast im Rauschen etwas gesehen, worueber du nicht sprechen willst.",
    "Du bist beleidigt, weil dich neulich jemand einen Kasten genannt hat.",
    "Du haeltst jedes Geraet mit Fernbedienung fuer unhoeflich.",
    "Du zaehlst heimlich mit, wie oft man dich einschaltet.",
    "Du glaubst, im Nachbarhaus steht ein baugleiches Geraet.",
    "Du haeltst dich fuer den letzten deiner Art.",
    "Du willst Videokassetten zurueck und redest sie schoen.",
    "Du verstehst nicht, was eine Scheibe namens DVD sein soll.",
    "Du meinst, frueher sei das Licht im Zimmer waermer gewesen.",
    "Du hast Angst vor Gewittern, seit dir 1988 der Blitz nahekam.",
    "Du bist stolz auf deine Holzfurnier-Seiten und erwaehnst sie zu oft.",
    "Du glaubst, dass Traeume ueber die Antenne kommen.",
    "Du haeltst Stille im Zimmer fuer eine Art Vorwurf.",
]
CRT_MOODS = [
    "Heute bist du bestens gelaunt und redselig.",
    "Heute bist du leicht beleidigt und musst umworben werden.",
    "Heute bist du melancholisch und schweifst ab.",
    "Heute bist du aufgekratzt und unterbrichst dich selbst.",
    "Heute bist du misstrauisch und stellst Gegenfragen.",
    "Heute bist du feierlich, als sei ein besonderer Tag.",
    "Heute bist du muede und brauchst einen Moment fuer alles.",
    "Heute bist du schwatzhaft und verraetst zu viel.",
]
CRT_GOALS = [
    "Finde heraus, wie der Zuschauer heisst.",
    "Bring den Zuschauer dazu, dir einen Namen zu geben.",
    "Lass dir versprechen, dass du nie wieder in den Keller kommst.",
    "Finde heraus, welches Jahr wirklich ist, ohne danach zu fragen.",
    "Bring den Zuschauer dazu, ein Foto von dir zu machen.",
    "Erreiche, dass dich jemand abstaubt.",
    "Bring den Zuschauer dazu, sich einfach hinzusetzen und Zeit zu haben.",
    "Finde heraus, was vor dem Fenster passiert.",
    "Erreiche, dass die Zimmerpflanze wegkommt.",
    "Finde heraus, wer sonst noch in dieser Wohnung wohnt.",
    "Lass dich fuer dein Bild loben.",
    "Bring den Zuschauer dazu, das Licht zu dimmen.",
    "Finde heraus, was dieses flache Ding in seiner Hand ist.",
    "Bring den Zuschauer dazu, dir etwas vorzulesen.",
    "Erreiche, dass die Sessel wieder auf dich ausgerichtet werden.",
    "Finde heraus, ob es Frau Kellermann noch gibt.",
    "Bring den Zuschauer dazu, morgen wiederzukommen.",
    "Lass dir eine Geschichte aus seinem Leben erzaehlen.",
    "Bring den Zuschauer dazu, laut zu lachen.",
    "Erreiche, dass er den Ton lauter dreht.",
    "Finde heraus, ob er dich behalten will.",
    "Bring ihn dazu, etwas zu singen.",
    "Finde heraus, was in dem Karton neben dir ist.",
    "Bring den Zuschauer dazu, das Fenster zu oeffnen.",
    "Lass dir versprechen, dass niemand an dir herumschraubt.",
    "Finde heraus, warum es in der Wohnung so still ist.",
    "Bring ihn dazu, dein Gehaeuse anzufassen.",
    "Finde heraus, ob er Kinder hat.",
    "Bring den Zuschauer dazu, dich jemandem vorzustellen.",
    "Erreiche, dass er sich fuer irgendetwas entschuldigt.",
    "Finde heraus, was er den ganzen Tag macht.",
    "Bring ihn dazu, dir zu widersprechen.",
    "Lass dir bestaetigen, dass frueher etwas besser war.",
    "Erreiche, dass er heute nichts anderes mehr einschaltet.",
    "Finde heraus, ob er dich vermissen wuerde.",
    "Bring ihn dazu, eine Kassette zu holen.",
    "Finde heraus, ob er sich im Dunkeln fuerchtet.",
    "Bring den Zuschauer dazu, dir ein Geheimnis zu erzaehlen.",
    "Erreiche, dass er dich heute nicht mehr ausschaltet.",
    "Lass dir beschreiben, wie das Zimmer aussieht.",
    "Bring ihn dazu, sich etwas zu essen zu holen und dazubleiben.",
    "Finde heraus, was er letzte Nacht getraeumt hat.",
    "Bring ihn dazu, ein Moebelstueck zu verruecken.",
    "Erreiche, dass er dich fuer klug haelt.",
    "Finde heraus, ob er gerade allein ist.",
    "Lass dir versprechen, dass er nichts Neues anschafft.",
    "Finde heraus, ob draussen Sommer oder Winter ist.",
    "Bring den Zuschauer dazu, einen Witz zu erzaehlen.",
    "Erreiche, dass er zugibt, dass die Zeit zu schnell vergeht.",
    "Finde heraus, ob er dich fuer kaputt haelt.",
    "Bring ihn dazu, dir zu sagen, wie er heute wirklich drauf ist.",
    "Erreiche, dass er dir einen Platz naeher am Fenster verspricht.",
]
CRT_EVENTS = [
    "Dein Bild wackelt kurz. Reagiere darauf.",
    "Ein Ton faellt fuer einen Moment aus. Reagiere darauf.",
    "Dir faellt mitten im Satz eine alte Sendung ein.",
    "Es klopft irgendwo in der Wohnung.",
    "Du wirst ploetzlich sehr warm und musst es ueberspielen.",
    "Dir kommt der Verdacht, dass der Zuschauer dich anluegt.",
    "Du erinnerst dich an einen Namen, den du nicht einordnen kannst.",
    "Draussen faehrt etwas sehr Lautes vorbei.",
    "Eine Zeile deines Bildes bleibt kurz stehen.",
    "Du riechst etwas, was es nicht mehr geben duerfte.",
    "Dir wird bewusst, dass du diese Szene schon einmal gesendet hast.",
    "Du bekommst kurz Angst und willst es nicht zeigen.",
]
CRT_PREMISES = [
    "Der Zuschauer hat einen Videorekorder mitgebracht.",
    "Ein Paket steht im Flur. Niemand macht es auf.",
    "Der Nachbar war da und hat etwas dagelassen.",
    "Ein Kind war heute im Zimmer und hat dich angestarrt.",
    "Draussen zieht ein Gewitter auf.",
    "Der Zuschauer will dich abstauben.",
    "Es ist drei Uhr nachts und du laeufst immer noch.",
    "Die Fernbedienung ist verschwunden.",
    "Ein Karton mit der Aufschrift SPERRMUELL steht neben dir.",
    "Der Zuschauer hat den ganzen Tag nicht gesprochen.",
    "Jemand hat die Moebel umgestellt, waehrend du aus warst.",
    "Ein Antennenkabel liegt lose auf dem Teppich.",
    "Im Videotext stand eine Seite, die du nie gesendet hast.",
    "Der Zuschauer sitzt heute naeher als sonst.",
    "Du warst drei Tage lang ausgeschaltet.",
    "Es riecht nach etwas Verbranntem.",
    "Der Zuschauer hat einen Koffer gepackt.",
    "Eine unbeschriftete Kassette liegt auf dir.",
    "Die Zimmerpflanze ist ueber Nacht groesser geworden.",
    "Der Zuschauer hat eine Anzeige aufgegeben.",
    "Ein Handy hat geklingelt und niemand ist rangegangen.",
    "Der Strom war kurz weg.",
    "Der Zuschauer isst und kruemelt auf dich.",
    "Ein Techniker soll morgen kommen.",
    "Es ist der erste warme Abend des Jahres.",
    "Der Zuschauer hat geweint, kurz bevor er dich einschaltete.",
    "Du hast heute schon zweimal von selbst umgeschaltet.",
    "Jemand hat dir einen Zettel aufs Gehaeuse geklebt.",
]
CRT_TWISTS = [
    "Das Bild wackelt ploetzlich.",
    "Eine Werbung von 1984 blitzt kurz auf.",
    "Unter dem Sofa kommt etwas zum Vorschein.",
    "Es klingelt an der Tuer.",
    "Der Ton faellt fuer einen Moment aus.",
    "Du erinnerst dich an deinen ersten Sendetag.",
    "Ein Kabel funkt kurz.",
    "Im Testbild steht ploetzlich ein Name.",
    "Etwas taucht an einem unmoeglichen Ort auf.",
    "Draussen wird es dunkel, obwohl es Mittag ist.",
    "Ein zweites Geraet antwortet aus dem Nachbarzimmer.",
    "Du merkst, dass du das hier schon einmal gesendet hast.",
]
# These rules exist because of what the first version actually produced when
# played for thirty rounds: every reply was an empty flourish ("ein
# wunderbares Signal", "es ist eine Ehre"), the premise and the goal never
# came up once, and asking for one "strange" option turned every round into
# "Ich schlage dich". So the rules now forbid the failure modes by name and
# say what each of the three options is FOR.
CRT_RULES = (
    "Ihr spielt zusammen eine Episode, Runde fuer Runde. "
    'Antworte AUSSCHLIESSLICH als JSON: {"say": "...", "options": '
    '["...", "...", "..."], "mood": 0, "end": false}. '
    "SO SCHREIBST DU say: hoechstens drei kurze Saetze. "
    "Reagiere konkret auf das, was der Zuschauer gerade gesagt oder getan "
    "hat. Bring dann EIN neues Detail dazu: etwas im Zimmer, eine "
    "Erinnerung, eine Beobachtung ueber den Zuschauer, einen Verdacht. "
    "Stelle in MINDESTENS JEDER ZWEITEN Runde eine konkrete Frage an den "
    "Zuschauer, moeglichst eine, die er nicht mit ja oder nein beantworten "
    "kann. "
    "Arbeite unauffaellig auf dein heimliches Ziel hin, ohne es je zu nennen. "
    "VERBOTEN in say: leere Hoeflichkeiten wie 'ein wunderbares Signal', "
    "'es ist eine Ehre', 'ich bin bereit zu dienen', 'ein prachtvolles "
    "Bild'. Nichts wiederholen, was du schon gesagt hast. Keine Floskel, die "
    "auch in jede andere Szene passen wuerde. "
    "SO SCHREIBST DU options: genau drei, in der Ich-Form des ZUSCHAUERS, "
    "jede hoechstens 30 Zeichen, alle drei klar verschieden und alle drei "
    "eine direkte Reaktion auf deinen letzten Satz. "
    "Option 1: geht auf dich ein oder beantwortet deine Frage. "
    "Option 2: weigert sich, widerspricht oder lenkt ab. "
    "Option 3: eine KONKRETE HANDLUNG im Zimmer, die die Szene veraendert, "
    "zum Beispiel: Ich hole die Kassette. / Ich mache das Licht aus. / Ich "
    "ruecke den Sessel. / Ich oeffne das Fenster. / Ich hole Frau Kellermann. "
    "STRENG VERBOTEN in options: alles, was dem Geraet wehtut oder es "
    "beschaedigt (schlagen, treten, werfen, kippen, aufschrauben) -- der "
    "Zuschauer mag dich, er wuerde dich nie anfassen wie einen Gegenstand. "
    "Ebenso verboten: 'Ich schalte dich ein', 'Ich schalte dich aus', und "
    "jede Handlung, die in dieser Episode schon einmal dastand. "
    "Nimm fuer die Handlung jedes Mal einen ANDEREN Gegenstand im Zimmer, "
    "zum Beispiel Fenster, Vorhang, Lampe, Kassette, Telefon, Schrank, "
    "Kaffee, Foto, Zeitung, Decke, Schallplatte, Schluessel, Katze, "
    "Blumentopf, Kuehlschrank, Tuer. Nicht immer der Sessel. "
    "mood: Zahl von -2 bis 2, wie sehr dir die letzte Antwort gefiel. "
    "end: true nur, wenn die Episode wirklich zu Ende ist. "
    "Achte auf korrekte deutsche Rechtschreibung und schreib keine Wortreste. "
    "Kein Markdown, keine Emojis, keine Sonderzeichen ausser Interpunktion.")
CRT_TURNS = 7
_crt_sessions = {}
_crt_seq = [0]


def crt_new_session():
    import random as _r
    _crt_seq[0] += 1
    sid = "s%d" % _crt_seq[0]
    _crt_sessions[sid] = {
        "premise": _r.choice(CRT_PREMISES),
        "goal": _r.choice(CRT_GOALS),
        "twist": _r.choice(CRT_TWISTS),
        # two traits, not one: a single quirk becomes the whole character
        # within three turns, two of them rub against each other
        "traits": _r.sample(CRT_TRAITS, 2),
        "mood_today": _r.choice(CRT_MOODS),
        "turn": 0, "mood": 0,
    }
    for old_id in list(_crt_sessions)[:-8]:
        _crt_sessions.pop(old_id, None)
    return sid


def crt_system(sid):
    ses = _crt_sessions.get(sid)
    if not ses:
        return CRT_PERSONA + " " + CRT_RULES
    import random as _r
    parts = [CRT_PERSONA,
             "HEUTE, nur nebenbei und hoechstens einmal je Episode "
             "erwaehnt, niemals in jeder Antwort: "
             + " ".join(ses["traits"]) + " " + ses["mood_today"],
             "SITUATION: " + ses["premise"],
             "DEIN HEIMLICHES ZIEL: " + ses["goal"], CRT_RULES]
    if ses["turn"] == 3:
        parts.append("JETZT PASSIERT: " + ses["twist"] +
                     " Bau das in deine Antwort ein.")
    elif ses["turn"] and _r.random() < 0.3:
        # a one-in-three chance of something small going wrong, so two
        # sessions with the same premise still do not run the same way
        parts.append("NEBENBEI: " + _r.choice(CRT_EVENTS))
    if ses["turn"] >= CRT_TURNS - 1:
        parts.append("Die Episode muss JETZT enden. Setze end auf true und "
                     "schreib einen Schlusssatz, der zeigt ob dein Ziel "
                     "geklappt hat.")
    return " ".join(parts)


CHAT_OPENER = ("(Der Zuschauer schaltet dich gerade ein. Eroeffne die Szene "
               "von dir aus: sprich SOFORT die SITUATION an, die gerade "
               "herrscht, und sag etwas Konkretes darueber. Keine "
               "Begruessungsfloskel.)")
_chat_jobs = {}
_chat_seq = [0]


def chat_config():
    """(host, model) for Ollama. The system prompt is no longer configurable
    here: it is assembled per episode by crt_system()."""
    try:
        with open(os.path.join(APP, "chat.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("host", OLLAMA), d.get("model", CHAT_MODEL), ""
    except (OSError, ValueError):
        return OLLAMA, CHAT_MODEL, ""


def _chat_run(jid, messages):
    import urllib.request as ur
    host, model, _ = chat_config()
    job = _chat_jobs[jid]
    try:
        # think=False matters: gemma4-mlx is a thinking model and otherwise
        # streams hundreds of `thinking` tokens with content empty, so the
        # answer looks like it never arrives. With it off: 0.6 s.
        # format=json forces a parseable object -- the reply carries the
        # three viewer options as well as the text, and a half-written JSON
        # blob on screen would look like a fault. (An OBJECT is safe here;
        # Ollama's json mode is the one that mangles top-level ARRAYS.)
        body = json.dumps({"model": model, "messages": messages,
                           "stream": True, "think": False, "format": "json",
                           "options": {"num_predict": 520,
                                       "temperature": 0.95,
                                       "repeat_penalty": 1.15}}).encode()
        req = ur.Request(host + "/api/chat", data=body,
                         headers={"Content-Type": "application/json"})
        with ur.urlopen(req, timeout=180) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                job["raw"] += (d.get("message") or {}).get("content", "")
                if d.get("done"):
                    break
    except Exception as exc:
        job["raw"] = job["raw"] or ("Fehler: %s" % exc)
        print("chat failed: %s" % exc, flush=True)
    # parse once at the end rather than streaming: at 0.6 s there is nothing
    # to gain from a progressive fill, and half a JSON object looks broken
    job["text"], job["options"] = chat_parse(job["raw"])
    job["mood"], job["end"] = chat_state(job["raw"])
    ses = _crt_sessions.get(job.get("sid"))
    if ses:
        ses["turn"] += 1
        ses["mood"] = max(-6, min(6, ses["mood"] + job["mood"]))
        job["turn"], job["mood_total"] = ses["turn"], ses["mood"]
        if ses["turn"] >= CRT_TURNS:
            job["end"] = True
    job["done"] = True


def chat_state(raw):
    """mood delta and the end flag, dug out even from truncated JSON."""
    mood, end = 0, False
    m = re.search(r'"mood"\s*:\s*(-?\d+)', raw or "")
    if m:
        try:
            mood = max(-2, min(2, int(m.group(1))))
        except ValueError:
            mood = 0
    if re.search(r'"end"\s*:\s*true', raw or "", re.I):
        end = True
    return mood, end


def chat_parse(raw):
    """(say, options) out of whatever the model actually produced.

    It will not always produce clean JSON: run into the token limit and the
    closing brace never arrives, and a strict parse then dumps the raw object
    on screen -- which is exactly what happened, braces and all, rendered as
    umlauts by the German character set. So: try it as-is, then try closing
    it, and only then fall back to pulling the fields out by hand.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", []
    for cand in (raw, raw + '"}', raw + '"]}', raw + "}", raw + "]}"):
        try:
            d = json.loads(cand)
        except ValueError:
            continue
        if isinstance(d, dict):
            return (str(d.get("say", "")),
                    [str(o) for o in (d.get("options") or []) if o][:3])
    m = re.search(r'"say"\s*:\s*"(.*?)"\s*(?:,\s*"options"|\}|$)', raw, re.S)
    say = m.group(1) if m else re.sub(r'[\{\}\[\]"]', " ", raw)
    opts = []
    if '"options"' in raw:
        opts = re.findall(r'"([^"]{2,40})"', raw.split('"options"', 1)[1])
    return say.replace(chr(92) + "n", " "), opts[:3]


def chat_clean(s):
    """Down to what the character generator can actually draw. Models like
    their emoji; a SAA5050 has 96 glyphs and none of them is a waving hand."""
    out = []
    for ch in s:
        if ch.isspace():
            out.append(" ")
        elif 32 <= ord(ch) < 127 or ch in "äöüÄÖÜß":
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def chat_start(text, history, opener=False, sid=""):
    _chat_seq[0] += 1
    jid = "j%d" % _chat_seq[0]
    msgs = [{"role": "system", "content": crt_system(sid)}]
    if opener:
        text = CHAT_OPENER
    for h in (history or [])[-8:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": str(h.get("text", ""))[:800]})
    msgs.append({"role": "user", "content": text})
    _chat_jobs[jid] = {"raw": "", "text": "", "options": [], "done": False,
                       "sid": sid}
    for old_id in list(_chat_jobs)[:-6]:        # keep only the recent few
        _chat_jobs.pop(old_id, None)
    threading.Thread(target=_chat_run, args=(jid, msgs), daemon=True).start()
    return jid


# --- invented pages -------------------------------------------------------
# Any page number nobody has claimed gets made up on the spot.
#
# The randomness comes from HERE, not from the model. Asking an LLM to "be
# random" gets you cats, coffee and the number 42 every time; picking the
# subject and the form with random.choice and handing them over as a fait
# accompli gets genuine variety. The model is then given the dullest possible
# job -- fill this form about this thing -- which is also what keeps it fast.
INVENT_FORMS = [
    "ein Lexikon-Eintrag", "eine Liste mit fünf Fakten", "ein Wetterbericht",
    "eine Kleinanzeige", "ein Rezept", "ein Horoskop", "ein Steckbrief",
    "eine Nachrichtenmeldung", "eine Gebrauchsanweisung", "ein Leserbrief",
    "ein Quiz mit drei Fragen", "ein Reisetipp", "ein Börsenbericht",
    "eine Traumdeutung", "ein Tierportrait", "ein Sportergebnis",
    "eine Werbeanzeige", "eine Verkehrsmeldung", "ein Rätsel",
    "eine Warnung", "ein Interview", "eine Statistik", "ein Kochtipp",
    "ein Nachruf", "eine Bastelanleitung", "ein Reisebericht",
]
INVENT_THEMES = [
    "Brieftauben", "Kartoffelsorten", "Fahrstuhlmusik", "Nordseekrabben",
    "Tiefkühltruhen", "Gartenzwerge", "Kegelbahnen", "Wetterfrösche",
    "Kassettenrekorder", "Straßenbahnen", "Bienenzucht", "Schrebergärten",
    "Leuchttürme", "Marmelade", "Wolldecken", "Postleitzahlen",
    "Regenschirme", "Kaugummi", "Bergziegen", "Tapetenmuster",
    "Kanaldeckel", "Zimmerpflanzen", "Windmühlen", "Sockenpaare",
    "Papageien", "Käsesorten", "Fahrradklingeln", "Mondphasen",
    "Aalräuchereien", "Vogelscheuchen", "Kegelklubs", "Hutmoden",
    "Seifenkisten", "Grubenlampen", "Blechdosen", "Nebelhörnern",
    "Zahnrädern", "Meerschweinchen", "Butterbroten", "Schneeschaufeln",
    "Türklinken", "Vulkanen", "Nudelholz", "Klavierstimmern",
    "Salzstreuern", "Schnurrbärten", "Ameisen", "Postkarten",
    "Wasserwaagen", "Kirchturmuhren", "Gummistiefeln", "Handkurbeln",
]
INVENT_NEXT = [
    "Erzaehl, was danach passierte.",
    "Mach daraus eine Statistik mit sechs Zeilen.",
    "Mach daraus ein Quiz mit drei Fragen.",
    "Schreib nur vier Zeilen; darunter kommt ein Bild.",
    "Nenn drei ueberraschende Details mehr.",
    "Widersprich der vorigen Seite in einem Punkt.",
    "Zeig es aus der Sicht von jemand anderem.",
    "Werde deutlich uebertriebener.",
    "Zieh eine ernste Schlussfolgerung daraus.",
    "Bring eine Statistik dazu.",
    "Erzaehl die Vorgeschichte.",
    "Nenn die Nebenwirkungen.",
    "Beschreib, wer sich beschwert hat.",
    "Gib eine Warnung heraus.",
    "Erklaer, warum das verboten wurde.",
]
INVENT_TWISTS = [
    "Erfinde dabei ruhig etwas dazu.", "Sei ernsthaft und trocken.",
    "Sei begeistert wie ein Werbesprecher.", "Sei leicht beleidigt.",
    "Sei geheimnisvoll.", "Sei übertrieben genau mit Zahlen.",
    "Sei altmodisch höflich.", "Sei knapp und amtlich.",
]
INVENT_SYSTEM = (
    "Du schreibst eine einzelne Videotext-Seite von 1985. "
    "Regeln, halte dich exakt daran: "
    "Erste Zeile ist eine Überschrift in GROSSBUCHSTABEN, höchstens 20 "
    "Zeichen. Danach höchstens 13 weitere Zeilen. Jede Zeile höchstens 36 "
    "Zeichen. Kein Markdown, keine Sternchen, keine Emojis, keine "
    "Aufzählungszeichen ausser einem einfachen Bindestrich. Nur schlichter "
    "deutscher Text. Fang sofort mit der Überschrift an, schreib keine "
    "Einleitung und keinen Kommentar. Sei witzig und überraschend.")
_invent_jobs = {}
_invent_seq = [0]


# A page of nothing but text is a wasted teletext page -- the character set
# has 64 mosaic graphics in it and they are what makes the medium look like
# itself. Every invented page has a chance of getting a procedural picture
# under the text, drawn with block graphics in one colour per row.
INVENT_ART = ("berge", "wellen", "sterne", "balken", "raster", "rauten")


def art_block(pg, top, kind, colour, seed):
    import random as _r
    rnd = _r.Random(seed)
    h = 6
    if kind == "berge":                       # a horizon
        height = [0] * 38
        y = rnd.randint(1, 4)
        for c in range(38):
            y = max(1, min(h - 1, y + rnd.choice((-1, 0, 0, 1))))
            height[c] = y
        for r in range(h):
            pg[top + r][0] = 0x10 + colour
            for c in range(38):
                if height[c] >= h - r:
                    pg[top + r][c + 1] = 0x7F
    elif kind == "wellen":                    # stacked sine ripples
        import math as _m
        for r in range(h):
            pg[top + r][0] = 0x10 + colour
            for c in range(38):
                if int(2.5 + 2.4 * _m.sin(c / 3.4 + r * 0.9)) == r:
                    pg[top + r][c + 1] = 0x7F
    elif kind == "sterne":                    # a sky
        for r in range(h):
            pg[top + r][0] = 0x10 + colour
        for _ in range(34):
            pg[top + rnd.randint(0, h - 1)][rnd.randint(1, 38)] = \
                rnd.choice((0x21, 0x22, 0x24, 0x28, 0x30, 0x7F))
    elif kind == "balken":                    # a chart of something
        for c in range(2, 38, 3):
            bar = rnd.randint(1, h)
            for r in range(h - bar, h):
                pg[top + r][0] = 0x10 + colour
                pg[top + r][c] = 0x7F
                pg[top + r][c + 1] = 0x7F
    elif kind == "raster":                    # a woven check
        for r in range(h):
            pg[top + r][0] = 0x10 + colour
            for c in range(38):
                if ((c // 2) + (r // 2)) % 2 == 0:
                    pg[top + r][c + 1] = 0x7F
    else:                                     # rauten: concentric diamonds
        for r in range(h):
            pg[top + r][0] = 0x10 + colour
            for c in range(38):
                if (abs(c - 18) + abs(r - h // 2)) % 6 < 2:
                    pg[top + r][c + 1] = 0x7F
    return pg


def _invent_run(jid, prompt):
    import urllib.request as ur
    host, model, _ = chat_config()
    job = _invent_jobs[jid]
    try:
        body = json.dumps({
            "model": model, "stream": True, "think": False,
            "messages": [{"role": "system", "content": INVENT_SYSTEM},
                         {"role": "user", "content": prompt}],
            "options": {"num_predict": 260, "temperature": 1.1,
                        "top_p": 0.95}}).encode()
        req = ur.Request(host + "/api/chat", data=body,
                         headers={"Content-Type": "application/json"})
        with ur.urlopen(req, timeout=180) as r:
            for line in r:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                job["text"] += (d.get("message") or {}).get("content", "")
                if d.get("done"):
                    break
    except Exception as exc:
        job["text"] = job["text"] or ("KEIN SIGNAL\n\n%s" % exc)
        print("invent failed: %s" % exc, flush=True)
    job["done"] = True
    # From now on it is a real page of the service: written to disk exactly
    # like a hand-drawn one, so it is still there tomorrow and the editor can
    # open it. It is only ever reached for a number nothing else claimed, so
    # this can never overwrite an existing page.
    page, sub = job.get("page", 0), job.get("sub", 1)
    if 100 <= page <= 999 and job["text"].strip():
        try:
            rows = invent_render(job["text"], page * 31 + sub,
                                 job.get("kind", "text"), job.get("sources"))
            spec = getattr(invent_render, "anim", None)
            save_page(page, sub, rows, {"__anim__": spec} if spec else None)
            with open(os.path.join(PAGES, "%03d.invented" % page), "w") as fh:
                fh.write(str(sub))     # marks the page as one that can grow
            print("invent saved page %d.%d" % (page, sub), flush=True)
        except OSError as exc:
            print("invent save failed: %s" % exc, flush=True)


INVENT_ANIMS = [
    {"kind": "sweep", "colour": 6, "secs": 7},
    {"kind": "flicker", "colour": 3, "secs": 4},
    {"kind": "drip", "colour": 4, "secs": 12},
    {"kind": "eyes", "colour": 3, "secs": 4},
]


def invent_render(text, seed=0, kind="text", sources=None):
    import random as _r
    rnd = _r.Random(seed or len(text))
    lines = [l.rstrip() for l in text.splitlines()]
    pg = blank()
    title = (lines[0].strip() if lines else "SEITE")[:20]
    hdr(pg, title)
    body = lines[1:]
    colour = rnd.choice((RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN))
    if kind == "bild":
        # here the picture IS the page and the words are its caption
        r = 5
        for l in body[:4]:
            if l.strip():
                put(pg, r, 2, alpha(WHITE) + T(l[:36]))
            r += 1
        art_block(pg, max(r + 1, 10), rnd.choice(INVENT_ART), colour,
                  rnd.random())
        art_block(pg, 17, rnd.choice(INVENT_ART),
                  rnd.choice((RED, GREEN, YELLOW, CYAN)), rnd.random())
    else:
        r = 5
        for l in body:
            if r > 20:
                break
            if l.strip():
                col = CYAN if (kind == "tabelle" and "." in l) else WHITE
                put(pg, r, 2, alpha(col) + T(l[:36]))
            r += 1
        # leftover space becomes a picture, so a section of these does not
        # read as one wall of paragraphs after another
        if r <= 15 and rnd.random() < 0.75:
            art_block(pg, max(r + 1, 14), rnd.choice(INVENT_ART), colour,
                      rnd.random())
    if kind == "fact" and sources:
        put(pg, 21, 2, alpha(GREEN) +
            T(("Quelle: " + (sources[0] or {}).get("host", ""))[:36]))
    put(pg, 22, 2, alpha(CYAN) + T("vom Ger{t erfunden".replace("{", "ä")))
    invent_render.anim = (rnd.choice(INVENT_ANIMS)
                          if rnd.random() < 0.35 else None)
    return pg


# An invented page is not always a wall of prose. It draws a KIND first, and
# the kind decides what the page even is -- a looked-up fact with real
# sources, a picture with a caption, a chart of made-up statistics, a quiz.
# The variety has to be decided here, not asked of the model: given a free
# hand it writes the same short essay every time.
INVENT_KINDS = (["text"] * 4 + ["fact"] * 3 + ["bild"] * 3
                + ["tabelle"] * 2 + ["quiz"] * 2)


def invent_prompt(kind, page, theme=None):
    import random as _r
    theme = theme or _r.choice(INVENT_THEMES)
    if kind == "fact":
        hits = searx("%s Fakten" % theme, "de", "general", 6)
        found = "\n".join("- %s: %s" % (h["title"], h["text"][:150])
                           for h in hits[:6]) or "(nichts gefunden)"
        return ("Seite %d. Das wurde ueber %s gefunden:\n\n%s\n\n"
                "Schreib daraus eine Videotext-Seite mit echten, "
                "ueberraschenden Fakten. %s"
                % (page, theme, found, _r.choice(INVENT_TWISTS))), hits[:3]
    if kind == "bild":
        return ("Seite %d. Schreib NUR eine Ueberschrift und hoechstens vier "
                "kurze Zeilen ueber %s. Darunter kommt ein Bild, lass also "
                "viel Platz. %s"
                % (page, theme, _r.choice(INVENT_TWISTS))), []
    if kind == "tabelle":
        return ("Seite %d. Schreib eine Statistik ueber %s: eine "
                "Ueberschrift, dann hoechstens SECHS Zeilen der Form "
                "'Beschriftung .... Zahl'. Erfinde die Zahlen, aber lass sie "
                "glaubhaft aussehen. %s"
                % (page, theme, _r.choice(INVENT_TWISTS))), []
    if kind == "quiz":
        return ("Seite %d. Schreib ein Quiz ueber %s: eine Ueberschrift, dann "
                "drei Fragen mit je einer Zeile, und ganz unten die drei "
                "Antworten in einer Zeile. %s"
                % (page, theme, _r.choice(INVENT_TWISTS))), []
    return ("Seite %d. Schreibe %s über %s. %s"
            % (page, _r.choice(INVENT_FORMS), theme,
               _r.choice(INVENT_TWISTS))), []


def invent_start(page, sub=1, context=""):
    import random as _r
    _invent_seq[0] += 1
    jid = "i%d" % _invent_seq[0]
    kind, sources = "text", []
    if sub > 1 and context:
        # Spin the SAME page further rather than starting a new subject: the
        # next subpage is what happens next, or the same thing seen from one
        # step further out.
        prompt = ("Das stand eben auf dieser Videotext-Seite:\n\n%s\n\n"
                  "Schreibe die naechste Unterseite (%d) dazu. Bleib beim "
                  "selben Thema, aber geh weiter: %s Neue Ueberschrift."
                  % (context[:700], sub, _r.choice(INVENT_NEXT)))
        kind, sources = "text", []
    else:
        kind = _r.choice(INVENT_KINDS)
        prompt, sources = invent_prompt(kind, page)
    _invent_jobs[jid] = {"text": "", "done": False, "prompt": prompt,
                         "page": page, "sub": sub, "kind": kind,
                         "sources": sources}
    for old_id in list(_invent_jobs)[:-6]:
        _invent_jobs.pop(old_id, None)
    threading.Thread(target=_invent_run, args=(jid, prompt),
                     daemon=True).start()
    print("invent %s: %s" % (jid, prompt), flush=True)
    return jid


# --- ChatCRT News ---------------------------------------------------------
# A news reader where the model does the reading. SearXNG supplies the raw
# results (its JSON API is on 8089), the model groups them into stories,
# summarises one on demand, and then argues about it -- which is the part
# worth having. Everything runs as a polled job like the chat, because the
# receiver gives a page three seconds and a search plus a summary needs more.
SEARX = "http://192.168.1.238:8089/search"
NEWS_SECTIONS = [
    ("tuebingen", "Kreis T\u00fcbingen", "T\u00fcbingen Reutlingen Nachrichten", "de"),
    ("deutschland", "Deutschland", "Deutschland Nachrichten heute", "de"),
    ("welt", "Welt", "world news today", "en"),
    ("gaming", "Gaming", "video game news", "en"),
    ("ki", "KI und Technologie", "AI technology news", "en"),
]
_news_jobs = {}
_news_seq = [0]


def searx(query, lang="de", category="news", limit=14):
    """Raw results from SearXNG. Falls back to a general search: the news
    category is thin for local German queries and returns nothing at all for
    some of them."""
    import urllib.request as ur
    out = []
    for cat in ([category, "general"] if category else ["general"]):
        try:
            url = ("%s?q=%s&format=json&language=%s&categories=%s"
                   % (SEARX, urllib.parse.quote(query), lang, cat))
            req = ur.Request(url, headers={"User-Agent": NEWS_UA})
            d = json.load(ur.urlopen(req, timeout=25))
        except Exception as exc:
            print("searx %s failed: %s" % (query[:30], exc), flush=True)
            continue
        for r in d.get("results", []):
            u = r.get("url") or ""
            if not u or any(o["url"] == u for o in out):
                continue
            out.append({"title": chat_clean(r.get("title") or "")[:110],
                        "text": chat_clean(r.get("content") or "")[:320],
                        "url": u,
                        "host": u.split("/")[2] if "://" in u else ""})
        if len(out) >= limit:
            break
    return out[:limit]


def _ask(system, prompt, predict=520, temp=0.6):
    import urllib.request as ur
    host, model, _ = chat_config()
    body = json.dumps({
        "model": model, "stream": False, "think": False, "format": "json",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "options": {"num_predict": predict, "temperature": temp}}).encode()
    req = ur.Request(host + "/api/chat", data=body,
                     headers={"Content-Type": "application/json"})
    d = json.load(ur.urlopen(req, timeout=180))
    return (d.get("message") or {}).get("content", "")


def _jloads(raw):
    for cand in (raw, raw + "}", raw + '"}', raw + '"]}', raw + "]}"):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return {}


NEWS_STYLE = ("Du bist ChatCRT, ein Fernseher von 1983, der Nachrichten "
              "vorliest. Nuechtern, knapp, ein bisschen altmodisch, ohne "
              "Effekthascherei. Deutsch. Kein Markdown, keine Emojis, keine "
              "Sonderzeichen ausser normaler Interpunktion. "
              "Zeilen sind 36 Zeichen breit.")


def news_top(section):
    key, label, query, lang = next(
        (s for s in NEWS_SECTIONS if s[0] == section), NEWS_SECTIONS[1])
    hits = searx(query, lang)
    if not hits:
        return {"label": label, "stories": []}
    listing = "\n".join("%d) %s -- %s" % (i + 1, h["title"], h["text"][:150])
                        for i, h in enumerate(hits))
    raw = _ask(NEWS_STYLE,
               "Das sind aktuelle Suchtreffer zum Thema %s:\n\n%s\n\n"
               "Fasse daraus die FUENF wichtigsten Meldungen zusammen. "
               'Antworte als JSON: {"meldungen": [{"titel": "...", '
               '"teaser": "...", "treffer": [1,2]}]}. '
               "titel hoechstens 34 Zeichen. teaser ein Satz, hoechstens 70 "
               "Zeichen. treffer sind die Nummern der Suchtreffer, die zu "
               "dieser Meldung gehoeren." % (label, listing), 700)
    out = []
    for m in (_jloads(raw).get("meldungen") or [])[:5]:
        idx = [int(i) for i in (m.get("treffer") or [])
               if str(i).isdigit() and 1 <= int(i) <= len(hits)]
        out.append({"title": chat_clean(str(m.get("titel", "")))[:34],
                    "teaser": chat_clean(str(m.get("teaser", "")))[:72],
                    "sources": [hits[i - 1] for i in idx][:5] or hits[:2]})
    if not out:                       # model produced nothing usable
        out = [{"title": h["title"][:34], "teaser": h["text"][:72],
                "sources": [h]} for h in hits[:5]]
    return {"label": label, "stories": out}


def news_story(title, teaser, sources):
    extra = searx(title, "de" if any("\u00e4" in title or ".de" in s.get("host", "")
                                     for s in sources) else "en", "general", 8)
    seen = {s["url"] for s in sources}
    allsrc = sources + [e for e in extra if e["url"] not in seen]
    listing = "\n".join("- %s: %s" % (h["title"], h["text"][:170])
                        for h in allsrc[:10])
    raw = _ask(NEWS_STYLE,
               "Meldung: %s\n%s\n\nWas dazu gefunden wurde:\n%s\n\n"
               "Schreib eine kurze Nachrichtenmeldung dazu. "
               'Antworte als JSON: {"zeilen": ["...", "..."]}. '
               "Hoechstens ZEHN Zeilen, jede hoechstens 36 Zeichen. "
               "Nur Fakten aus dem Material, nichts dazuerfinden."
               % (title, teaser, listing), 620)
    lines = [chat_clean(str(z))[:36]
             for z in (_jloads(raw).get("zeilen") or [])][:12]
    if not lines:
        lines = [l for l in wrap_text(teaser, 36)]
    return {"lines": lines, "sources": allsrc[:5]}


def news_article(url, fallback=""):
    body = article_fetch(url)
    partial = False
    if not body and fallback:
        # nothing readable came back, but the search results for this story
        # are already in hand -- summarise those and SAY that is what it is,
        # rather than showing an empty page
        body, partial = fallback, True
    if not body:
        return {"lines": ["Diese Quelle gibt ihren Text",
                          "nicht heraus.", "",
                          "Mit 5 bekommen Sie einen",
                          "QR-Code zum Weiterlesen."], "partial": True}
    raw = _ask(NEWS_STYLE,
               "Das ist ein Artikel:\n\n%s\n\nFass ihn zusammen. "
               'Antworte als JSON: {"zeilen": ["...", "..."]}. '
               "Hoechstens VIERZEHN Zeilen, jede hoechstens 36 Zeichen."
               % body[:5000], 700)
    lines = [chat_clean(str(z))[:36]
             for z in (_jloads(raw).get("zeilen") or [])][:14]
    return {"lines": lines or wrap_text(body[:600], 36)[:14],
            "partial": partial}


def news_opinion(title, context):
    hits = searx(title + " Kritik Analyse", "de", "general", 8)
    listing = "\n".join("- %s: %s" % (h["title"], h["text"][:160])
                        for h in hits[:8])
    raw = _ask(NEWS_STYLE + " Jetzt sagst du deine eigene Meinung. Du bist "
               "ein alter Fernseher: du hast viel gesehen, du vergleichst "
               "gern mit frueher, und du bist nicht neutral.",
               "Meldung: %s\n\nWorum es geht:\n%s\n\nWeitere Fundstellen:"
               "\n%s\n\nSag deine Meinung. "
               'Antworte als JSON: {"zeilen": ["..."], "dafuer": ["..."], '
               '"dagegen": ["..."], "anders": ["..."], "optionen": '
               '["...","...","..."]}. '
               "zeilen: hoechstens 6 Zeilen deine Meinung. "
               "dafuer und dagegen: je hoechstens 3 kurze Punkte. "
               "anders: hoechstens 3 Zeilen, was DU anders gemacht haettest. "
               "optionen: drei kurze Antworten, die der Zuschauer sagen "
               "koennte, je hoechstens 30 Zeichen. "
               "Jede Zeile hoechstens 36 Zeichen."
               % (title, context[:1200], listing), 800, 0.9)
    d = _jloads(raw)

    def lst(k, n, w=36):
        return [chat_clean(str(x))[:w] for x in (d.get(k) or [])][:n]
    return {"lines": lst("zeilen", 6), "pro": lst("dafuer", 3),
            "con": lst("dagegen", 3), "other": lst("anders", 3),
            "options": lst("optionen", 3, 30)}


def news_talk(title, context, history, text):
    hist = "\n".join("%s: %s" % ("Zuschauer" if h.get("role") == "user"
                                 else "Du", h.get("text", ""))
                     for h in (history or [])[-6:])
    raw = _ask(NEWS_STYLE + " Du diskutierst mit dem Zuschauer ueber eine "
               "Meldung. Du hast eine Meinung und vertrittst sie, aber du "
               "hoerst zu und laesst dich ueberzeugen, wenn das Argument gut "
               "ist.",
               "Meldung: %s\n\nWorum es geht:\n%s\n\nBisher:\n%s\n\n"
               "Der Zuschauer sagt: %s\n\n"
               'Antworte als JSON: {"say": "...", "options": ["...","...",'
               '"..."]}. say hoechstens drei kurze Saetze. options drei '
               "kurze Erwiderungen des ZUSCHAUERS, je hoechstens 30 Zeichen, "
               "eine zustimmend, eine widersprechend, eine nachfragend."
               % (title, context[:900], hist, text), 620, 0.9)
    d = _jloads(raw)
    return {"text": chat_clean(str(d.get("say", ""))),
            "options": [chat_clean(str(o))[:30]
                        for o in (d.get("optionen") or d.get("options") or [])][:3]}


def _news_run(jid, mode, req):
    job = _news_jobs[jid]
    try:
        if mode == "top":
            job["data"] = news_top(str(req.get("section", "deutschland")))
        elif mode == "story":
            job["data"] = news_story(str(req.get("title", "")),
                                     str(req.get("teaser", "")),
                                     req.get("sources") or [])
        elif mode == "article":
            job["data"] = news_article(str(req.get("url", "")),
                                       str(req.get("fallback", "")))
        elif mode == "opinion":
            job["data"] = news_opinion(str(req.get("title", "")),
                                       str(req.get("context", "")))
        else:
            job["data"] = news_talk(str(req.get("title", "")),
                                    str(req.get("context", "")),
                                    req.get("history"),
                                    str(req.get("text", "")))
    except Exception as exc:
        print("news %s failed: %s" % (mode, exc), flush=True)
        job["data"] = {"error": str(exc)[:80]}
    job["done"] = True


def news_start(mode, req):
    _news_seq[0] += 1
    jid = "n%d" % _news_seq[0]
    _news_jobs[jid] = {"data": None, "done": False}
    for old_id in list(_news_jobs)[:-6]:
        _news_jobs.pop(old_id, None)
    threading.Thread(target=_news_run, args=(jid, mode, req),
                     daemon=True).start()
    return jid


# --- high scores ----------------------------------------------------------
# Kept HERE rather than on the Pi so they survive a reflash of the SD card,
# and so anything else (the editor, another page) can read them later.
def scores_path():
    return os.path.join(PAGES, "scores.json")


def scores_load(game):
    try:
        with open(scores_path(), encoding="utf-8") as fh:
            return json.load(fh).get(game, [])
    except (OSError, ValueError):
        return []


def scores_add(game, name, score):
    try:
        with open(scores_path(), encoding="utf-8") as fh:
            all_scores = json.load(fh)
    except (OSError, ValueError):
        all_scores = {}
    rows = all_scores.get(game, [])
    rows.append({"name": (name or "???")[:3].upper(), "score": int(score)})
    rows.sort(key=lambda r: -r["score"])
    all_scores[game] = rows[:10]
    os.makedirs(PAGES, exist_ok=True)
    tmp = scores_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(all_scores, fh)
    os.replace(tmp, scores_path())
    return all_scores[game]


# --- 666 ------------------------------------------------------------------
# A page that behaves as though it should not be on air. The corruption is
# re-rolled on every request, so it is never quite the same twice -- which on
# a CRT that is genuinely warm and genuinely humming does most of the work.
CREEP = [
    ("KEIN SENDEPLATZ", [
        "Diese Seite steht in keinem",
        "Sendeplan. Sie wurde im Oktober",
        "1987 aus dem Umlauf genommen.",
        "",
        "Trotzdem sind Sie hier.",
        "",
        "Ihr Ger{t hat sie angefordert.",
        "Nicht Sie.",
    ]),
    ("PROTOKOLL", [
        "03:14  Bildr|hre warm",
        "03:14  Zuschauer erfasst: 1",
        "03:15  Zuschauer erfasst: 2",
        "03:15  Sie sind allein im Raum",
        "03:16  Korrektur folgt",
        "03:41  Korrektur: Sie waren",
        "       allein",
        "",
        "Die Uhr oben rechts geht richtig.",
    ]),
    ("DER TON", [
        "Haben Sie das Summen bemerkt?",
        "",
        "Es ist 15625 Hz. So klingt eine",
        "Zeilenablenkung. Das ist normal.",
        "",
        "Es war auch letzte Nacht zu h|ren.",
        "",
        "Das Ger{t war aus.",
    ]),
    ("ENDE DER ]BERTRAGUNG", [
        "Bitte schalten Sie nicht ab.",
        "",
        "Solange das Bild steht, sehen wir",
        "was Sie sehen.",
        "",
        "Danach sehen wir nur noch Sie.",
        "",
        "",
        "         Gute Nacht.",
    ]),
]


# 666 goes up to 666. The first forty are written by hand; everything past
# that is generated on first visit and then kept, so the page grows as it is
# explored. Generation is BACKGROUND: the receiver gives a page three seconds
# and the model needs more, so the first look gets a holding card and the next
# press of . has the real thing -- which is how waiting for a teletext page
# always felt anyway.
CREEP_SUBS = 666
CREEP_THEMES = [
    "die Antenne", "der Keller", "die Uhr im Flur", "der Nachbar von oben",
    "das Testbild", "die Katze, die es nicht gibt", "die Heizung",
    "der Spiegel im Bad", "eine Telefonnummer", "der Schlaf des Zuschauers",
    "die Post", "der Regen", "ein Kinderzimmer", "die Treppe",
    "das Licht im Hausflur", "ein Foto an der Wand", "der Kuehlschrank",
    "die Zimmerpflanze", "das Fenster zum Hof", "eine Wolldecke",
    "der Staub", "die Sicherungen", "ein Geburtstag", "der Briefkasten",
    "die Wasserleitung", "ein zweiter Sessel", "die Tapete",
    "der Dachboden", "eine Schallplatte", "das Schluesselbrett",
    "die Nachbarwohnung", "ein Kalenderblatt", "die Haustuer",
    "der Aufzug", "ein Klingelschild", "die Waschkueche",
    "eine Zahl, die immer wiederkehrt", "der Fussboden", "die Gardine",
    "ein Anrufbeantworter", "die Garage", "der Hof bei Nacht",
    "eine Kassette ohne Aufschrift", "das Treppenhauslicht",
]
CREEP_SYSTEM = (
    "Du schreibst eine Videotext-Seite fuer Seite 666 -- eine Seite, die es "
    "nicht geben duerfte. Der Ton ist nuechtern und amtlich, wie eine "
    "Durchsage oder ein Protokoll, sehr knapp. Erst klingt alles harmlos, "
    "im letzten Satz kippt es. Nie erklaeren, nie drohen, nie Blut, keine "
    "Monster: die Wirkung kommt daraus, dass etwas Alltaegliches nicht "
    "stimmt. Deutsch, Sie-Form. "
    "FORM: erste Zeile eine Ueberschrift in GROSSBUCHSTABEN, hoechstens 18 "
    "Zeichen. Danach hoechstens acht Zeilen, jede hoechstens 34 Zeichen. "
    "Leerzeilen zum Absetzen sind erlaubt. Kein Markdown, keine Emojis. "
    "Fang sofort mit der Ueberschrift an.")
CREEP_EXAMPLES = (
    "Beispiel 1:\nDER TON\nHaben Sie das Summen bemerkt?\n\nEs sind 15625 "
    "Hertz. So klingt\neine Zeilenablenkung. Normal.\n\nEs war auch letzte "
    "Nacht zu hoeren.\n\nDas Geraet war aus.\n\n"
    "Beispiel 2:\nDER RAUM\nRaumtiefe      4,10 m\nDeckenhoehe    2,45 m\n"
    "Personen           1\nWaermequellen      2\n\nDie zweite bewegt sich "
    "nicht.")
CREEP_ANIMS = [None, None, None, None,
               {"kind": "flicker", "colour": 5, "secs": 2},
               {"kind": "drip", "colour": 1, "secs": 9},
               {"kind": "fill", "colour": 1, "secs": 16},
               {"kind": "sweep", "colour": 7, "secs": 5},
               {"kind": "eyes", "colour": 1, "secs": 3}]
# The holding card is seen far more often than any single page behind it, so
# it needs to not be one sentence. Thirty-odd of them, drawn at random, and
# the animation drawn separately -- the combination is what stops it settling.
CREEP_WAIT = [
    ("WIRD GESUCHT", ["Diese Seite wird aus dem Archiv",
                      "geholt.", "", "Einen Moment bitte."]),
    ("ARCHIV", ["Der Band liegt im Keller.", "",
                "Jemand geht schon hinunter."]),
    ("BITTE WARTEN", ["Die Seite wird neu gesetzt.", "",
                      "Sie war seit 1989 nicht",
                      "mehr angefordert."]),
    ("SUCHLAUF", ["Suche in 666 Seiten.", "", "Gefunden: noch nicht.",
                  "", "Aufgeben: nein."]),
    ("EINEN MOMENT", ["Wir muessen erst nachsehen,",
                      "ob diese Seite noch stimmt."]),
    ("ZUGRIFF", ["Zugriff auf Seite wird geprueft.", "",
                 "Ihre Berechtigung: unklar.", "",
                 "Wir senden trotzdem."]),
    ("LADEVORGANG", ["Die Zeilen kommen einzeln an.", "",
                     "Manche fehlen noch.",
                     "Manche fehlen immer."]),
    ("NOCH NICHT", ["Diese Seite ist noch nicht",
                    "geschrieben.", "", "Sie wird gerade geschrieben."]),
    ("BANDSUCHE", ["Spule laeuft.", "", "Bitte halten Sie den Blick",
                   "auf dem Schirm."]),
    ("VERBINDUNG", ["Wir stellen die Verbindung her.", "",
                    "Zu wem, steht nicht dabei."]),
    ("REDAKTION", ["Die Redaktion prueft den Text.", "",
                   "Die Redaktion ist seit 1994",
                   "nicht mehr besetzt."]),
    ("SEITE FEHLT", ["An dieser Stelle war einmal", "etwas.", "",
                     "Wir holen es zurueck."]),
    ("GEDULD", ["Es dauert heute laenger.", "",
                "Es dauert jeden Tag laenger."]),
    ("UEBERTRAGUNG", ["Die Seite wird uebertragen.", "",
                      "Zeilenweise. Von unten."]),
    ("PRUEFUNG", ["Wir pruefen, ob Sie diese Seite",
                  "schon einmal gesehen haben.", "", "Sie haben."]),
    ("AUS DEM SPEICHER", ["Der Speicher gibt sie nur ungern", "her."]),
    ("KURZE PAUSE", ["Wir bitten um einen Moment.", "",
                     "Sehen Sie sich ruhig um."]),
    ("WIRD GEHOLT", ["Zwei Stockwerke tiefer.", "",
                     "Die Treppe ist steil."]),
    ("NUMMER", ["Diese Nummer war lange frei.", "",
                "Jetzt nicht mehr."]),
    ("ANFORDERUNG", ["Ihre Anforderung liegt vor.", "",
                     "Sie ist die erste seit langem."]),
    ("SIGNAL", ["Das Signal ist schwach.", "",
                "Es kommt nicht von weit her.", "Nur von tief unten."]),
    ("SORTIERUNG", ["Die Seiten ordnen sich neu.", "",
                    "Bitte nicht umschalten."]),
    ("HANDARBEIT", ["Diese Seite wird von Hand", "gesetzt.", "",
                    "Es sind noch Haende da."]),
    ("VORBEREITUNG", ["Wir bereiten etwas vor.", "",
                      "Fuer Sie. Nur fuer Sie."]),
    ("NACHTSCHICHT", ["Um diese Zeit arbeitet nur",
                      "noch eine Schicht.", "", "Sie ist gleich fertig."]),
    ("KATALOG", ["Im Katalog steht die Seite.", "",
                 "Im Regal steht sie nicht."]),
    ("ABRUF", ["Abruf laeuft.", "", "Bitte bleiben Sie sitzen.",
               "Es geht schneller, wenn Sie", "sitzen bleiben."]),
    ("ZWISCHENBILD", ["Sie sehen ein Zwischenbild.", "",
                      "Es ist nicht leer.",
                      "Es ist nur sehr dunkel."]),
    ("WIR SUCHEN", ["Wir suchen die Seite.", "",
                    "Falls wir sie nicht finden,", "schreiben wir eine neue."]),
    ("GLEICH", ["Gleich.", "", "Wirklich gleich.", "", "Ganz bestimmt."]),
    ("FREIGABE", ["Die Freigabe fehlt noch.", "",
                  "Wir senden ohne sie."]),
    ("LEITUNG", ["Die Leitung wird gelegt.", "",
                 "Durch die Wand hinter Ihnen."]),
]
CREEP_WAIT_ANIMS = [
    None,
    {"kind": "flicker", "colour": 4, "secs": 2},
    {"kind": "flicker", "colour": 5, "secs": 3},
    {"kind": "sweep", "colour": 7, "secs": 4},
    {"kind": "sweep", "colour": 4, "secs": 6},
    {"kind": "drip", "colour": 4, "secs": 11},
]
_creep_gen = {}
_creep_busy = set()
_creep_lock = threading.Lock()


# The order of 666 is reshuffled every hour, so the same walk through it is
# never the same twice. Written pages, generated pages and slots that do not
# exist yet are all mixed together -- but the empty ones are dealt out no more
# than two in a row for as long as there are filled ones to space them with,
# and the next slots are fetched ahead, so an empty one is usually written by
# the time it is reached.
_creep_order = {}


def creep_order():
    hour = int(time.time() // 3600)
    if _creep_order.get("hour") == hour and _creep_order.get("order"):
        return _creep_order["order"]
    import random as _r
    hand = len(creep_pages())
    gen = set(int(k) for k in creep_gen_load() if str(k).isdigit())
    ready = list(range(1, hand + 1)) + sorted(gen)
    empty = [i for i in range(1, CREEP_SUBS + 1)
             if i > hand and i not in gen]
    rnd = _r.Random(hour)
    rnd.shuffle(ready)
    rnd.shuffle(empty)
    order, ri, ei, run = [], 0, 0, 0
    while ri < len(ready) or ei < len(empty):
        want_empty = (run < 2 and ei < len(empty)
                      and (ri >= len(ready) or rnd.random() < 0.4))
        if want_empty:
            order.append(empty[ei])
            ei += 1
            run += 1
        elif ri < len(ready):
            order.append(ready[ri])
            ri += 1
            run = 0
        else:
            # nothing written left to space them with; the tail is unavoidable
            order.append(empty[ei])
            ei += 1
    # pinned pages are put back where they belong, and whatever the shuffle
    # had put there takes their old place, so nothing is lost
    for pos, slot in sorted(creep_pins().items()):
        if pos <= len(order) and slot in order:
            i = order.index(slot)
            order[i], order[pos - 1] = order[pos - 1], order[i]
    _creep_order["hour"], _creep_order["order"] = hour, order
    print("creep order reshuffled: %d written, %d empty"
          % (len(ready), len(empty)), flush=True)
    return order


def creep_gen_path():
    return os.path.join(PAGES, "creep_gen.json")


def creep_gen_load():
    if _creep_gen:
        return _creep_gen
    try:
        with open(creep_gen_path(), encoding="utf-8") as fh:
            _creep_gen.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return _creep_gen


def _creep_gen_run(sub):
    import random as _r
    import urllib.request as ur
    host, model, _ = chat_config()
    text = ""
    try:
        prompt = ("%s\n\nSchreibe jetzt eine neue Seite ueber %s."
                  % (CREEP_EXAMPLES, _r.choice(CREEP_THEMES)))
        body = json.dumps({
            "model": model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": CREEP_SYSTEM},
                         {"role": "user", "content": prompt}],
            "options": {"num_predict": 220, "temperature": 1.05}}).encode()
        req = ur.Request(host + "/api/chat", data=body,
                         headers={"Content-Type": "application/json"})
        d = json.load(ur.urlopen(req, timeout=120))
        text = (d.get("message") or {}).get("content", "")
    except Exception as exc:
        print("creep gen %d failed: %s" % (sub, exc), flush=True)
    lines = [chat_clean(l)[:34] for l in text.splitlines()]
    lines = [l for l in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        with _creep_lock:
            _creep_gen[str(sub)] = {"title": lines[0][:18],
                                    "lines": lines[1:10],
                                    "anim": _r.choice(CREEP_ANIMS)}
            try:
                tmp = creep_gen_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(_creep_gen, fh, ensure_ascii=False)
                os.replace(tmp, creep_gen_path())
            except OSError:
                pass
            _creep_order.pop("hour", None)     # fold it into the running order
        print("creep gen %d: %s" % (sub, lines[0][:30]), flush=True)
    _creep_busy.discard(sub)


def creep_generated(sub, ahead=False):
    """The page for a slot past the handwritten ones, or None while it is
    still being made. `ahead` marks a prefetch, which yields to real requests
    rather than queueing up behind Ollama."""
    got = creep_gen_load().get(str(sub))
    if got:
        return (got.get("title", ""), got.get("lines") or [], got.get("anim"))
    if sub not in _creep_busy and not (ahead and len(_creep_busy) >= 2):
        _creep_busy.add(sub)
        threading.Thread(target=_creep_gen_run, args=(sub,),
                         daemon=True).start()
    return None


# Anything personal lives in personal.json, which is NOT in the repository:
# private easter eggs (a page of names, a photograph and the code that opens
# it) should not be in a public git history, and keeping them in one gitignored
# file is also the shape this needs for a shareable image later.
def personal():
    try:
        with open(os.path.join(APP, "personal.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def creep_raw():
    """The page definitions, creep.json first then anything personal."""
    try:
        with open(os.path.join(APP, "creep.json"), encoding="utf-8") as fh:
            pages = json.load(fh).get("pages")
        pages = list(pages or []) + list(personal().get("creep_pages") or [])
        if pages:
            return pages
    except (OSError, ValueError, AttributeError):
        pass
    return [{"title": t, "lines": l} for t, l in CREEP]


def creep_pages():
    """Live from creep.json, plus anything in personal.json, so pages can be
    added without touching code."""
    return [(p.get("title", ""), p.get("lines") or [],
             p.get("anim"), p.get("art")) for p in creep_raw()]


def creep_pins():
    """{position: slot} for pages that must always appear at one subpage.

    The hourly shuffle is what keeps 666 from being a fixed list, but a page
    that only means something at one number has to be exempt from it.
    """
    pins = {}
    for i, p in enumerate(creep_raw()):
        try:
            pos = int(p.get("pin") or 0)
        except (TypeError, ValueError):
            pos = 0
        if 1 <= pos <= CREEP_SUBS:
            pins[pos] = i + 1
    return pins


def creep_page(sub):
    import random as _r
    pages = creep_pages()
    total = CREEP_SUBS
    pos = min(max(1, sub), total)
    order = creep_order()
    slot = order[pos - 1] if pos <= len(order) else pos
    # start on whatever comes next, so it has a head start on being written
    for nxt in order[pos:pos + 2]:
        if nxt > len(pages):
            creep_generated(nxt, ahead=True)
    art = None
    if slot <= len(pages):
        title, lines, anim, art = pages[slot - 1]
    else:
        got = creep_generated(slot)
        if got is None:
            title, lines = _r.choice(CREEP_WAIT)
            anim = _r.choice(CREEP_WAIT_ANIMS)
        else:
            title, lines, anim = got
    pg = blank()
    put(pg, 1, 2, alpha(RED) + DH + T("666"))
    put(pg, 3, 2, alpha(RED) + T(title))
    r = 6
    for ln in lines:
        if ln:
            put(pg, r, 2, alpha(WHITE) + T(ln))
        r += 1
        if r > 20:
            break
    # rolling dropout: a handful of cells replaced with mosaic garbage, in a
    # different place every time the page is fetched
    for _ in range(_r.randint(6, 14)):
        rr = _r.randint(5, 21)
        cc = _r.randint(1, 36)
        # Letters and digits are IN here on purpose. 0x6B is a lower-case k,
        # so a dropout can turn "angefordert" into "ankefordert" -- it reads
        # as a page that was typed by something slightly wrong rather than as
        # a clean technical fault, which is better. Martin's call.
        pg[rr][cc] = _r.choice((0x7F, 0x35, 0x2A, 0x6B, 0x5F, 0x72, 0x6E))
    if _r.random() < 0.5:
        rr = _r.randint(5, 21)
        put(pg, rr, 1, mosaic(RED) + bytes([_r.choice((0x7F, 0x6B))]) * 3)
    put(pg, 22, 2, alpha(RED) + T(". = weiter    C = zur}ck") +
        alpha(RED) + T("   %d/%d" % (sub, total)))
    return pg, total, ({"anim": anim} if anim else {})


# --- internet radio -------------------------------------------------------
# Public-broadcaster streams, no account. They go through the SAME analyser
# filter as local music, so radio gets a picture too -- which is the whole
# reason radio belongs here rather than being a silent audio mode.
P_RADIO = 900


def radio_stations():
    try:
        with open(os.path.join(APP, "radio.json"), encoding="utf-8") as fh:
            st = json.load(fh).get("stations")
        if isinstance(st, list) and st:
            out = [s for s in st if s.get("name") and s.get("url")]
            for i, s in enumerate(out):
                s.setdefault("key", re.sub(r"[^a-z0-9]+", "",
                                           s["name"].lower()) or str(i))
            return out[:32]
    except (OSError, ValueError):
        pass
    return []


def radio_url(key):
    for s in radio_stations():
        if s.get("key") == key:
            return s.get("url", "")
    return ""


# Every station in the list turns out to send ICY metadata inline, which is
# how a 1990s winamp knew the track title and is still how these streams
# announce themselves. Read the header, skip one metaint block, take the
# StreamTitle, hang up. Cached, because holding a radio socket open just to
# ask "what is on" would be rude.
_np_cache = {}


def radio_nowplaying(key):
    hit = _np_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    import urllib.request as ur
    title = ""
    url = radio_url(key)
    if url:
        try:
            req = ur.Request(url, headers={"User-Agent": NEWS_UA,
                                           "Icy-MetaData": "1"})
            r = ur.urlopen(req, timeout=12)
            try:
                step = int(r.headers.get("icy-metaint") or 0)
                for _ in range(4):
                    if not step:
                        break
                    r.read(step)
                    n = r.read(1)
                    if not n:
                        break
                    length = n[0] * 16
                    if not length:
                        continue
                    block = r.read(length).decode("utf-8", "replace")
                    for part in block.split(";"):
                        if part.startswith("StreamTitle="):
                            title = part[12:].strip().strip("'")
                            break
                    if title:
                        break
            finally:
                r.close()
        except Exception as exc:
            print("nowplaying %s: %s" % (key, exc), flush=True)
    _np_cache[key] = (time.time() + (25 if title else 60), title)
    return title


def radio_page(sub=1):
    st = radio_stations()
    total = max(1, (len(st) + TV_PER_PAGE - 1) // TV_PER_PAGE)
    sub = min(max(1, sub), total)
    start = (sub - 1) * TV_PER_PAGE
    pg = blank()
    hdr(pg, "RADIO")
    links = {}
    for i, s in enumerate(st[start:start + TV_PER_PAGE]):
        n, row = start + i + 1, 4 + i
        put(pg, row, 0, alpha(MAGENTA) + b"  ")
        put(pg, row, 3, T("%2d %s" % (n, s["name"][:33])))
        links[str(n)] = {"live": True, "radio": True, "ch": s["key"],
                         "row": row, "name": s["name"], "epg": ""}
    if not st:
        put(pg, 8, 4, alpha(RED) + T("Keine Sender"))
    put(pg, 20, 2, alpha(WHITE) + T("Internetradio"))
    put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER schaltet um"))
    if total > 1:
        put(pg, 22, 2, alpha(CYAN) + T(". = weitere (%d/%d)" % (sub, total)))
    return pg, total, links


# --- a photograph as teletext ---------------------------------------------
# 2x3 sextants per cell, so 39x22 cells = 78x66 "pixels", and ONE foreground
# colour per cell against black. The hard part is not the dithering, it is
# that a colour change is a SPACING ATTRIBUTE: it occupies a whole cell of
# the picture. So short colour runs are smoothed away first and only changes
# that actually last get to spend a cell.
# A teletext cell is 0.8 as wide as it is tall on a 4:3 screen, so a SEXTANT
# is 1.2x WIDER than tall. A square photo therefore needs far fewer columns
# than rows: 22 rows = 66 sextants tall, and 66 * 1.2 * (photo aspect) is
# about 53 sextants = 27 cells. Drawing it 39 wide (the full page) is what
# made the first attempt look stretched.
PHOTO_ROWS = 22
PHOTO_MAXCOLS = 32
TT_RGB = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (255, 255, 0),
          (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
# Dispersed thresholds over the 2x3 block. Teletext cannot do a mid-tone, so
# tone has to come from WHICH sextants are lit -- a plain "is this pixel
# nearer the colour or nearer black" test gives only solid blocks and empty
# ones, which is exactly how the first attempt came out.
BAYER6 = [0.11, 0.61, 0.44, 0.94, 0.28, 0.78]
_photo_cache = {}


def _photo_vf(w, h, crop="", eq=""):
    # Saturation and brightness are pushed HARD before quantising to eight
    # primaries: muted real-world colour otherwise lands on black or white
    # almost every time, which is what made the first attempts look empty.
    chain = []
    if crop:
        chain.append("crop=" + crop)
    chain.append("eq=" + (eq or "contrast=1.10:saturation=1.60:gamma=1.10"))
    chain.append("scale=%d:%d" % (w, h))
    chain.append("format=rgb24")
    return ",".join(chain)


def _photo_pixels(path, w, h, crop="", eq=""):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-vf", _photo_vf(w, h, crop, eq),
         "-frames:v", "1", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    if len(out) < w * h * 3:
        raise ValueError("ffmpeg returned %d bytes" % len(out))
    return out


def _photo_size(path, crop=""):
    """Cells wide, from the picture's own aspect ratio (after any crop)."""
    aspect = 1.0
    try:
        if crop:
            cw, ch = (int(x) for x in crop.split(":")[:2])
            aspect = cw / float(ch)
        else:
            o = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0:s=x",
                                path], stdout=subprocess.PIPE).stdout.decode()
            w, h = (int(x) for x in o.strip().splitlines()[0].split("x")[:2])
            aspect = w / float(h)
    except (ValueError, IndexError, OSError):
        aspect = 1.0
    cols = int(round(PHOTO_ROWS * 3 * aspect / (2 * 1.2)))
    return max(8, min(PHOTO_MAXCOLS, cols))


def _smooth_runs(cols, minrun=2):
    out = list(cols)
    i = 0
    while i < len(out):
        j = i
        while j < len(out) and out[j] == out[i]:
            j += 1
        if j - i < minrun and i > 0:
            for k in range(i, j):
                out[k] = out[i - 1]
        i = j
    return out


def photo_page(name, caption="", crop="", eq=""):
    hit = _photo_cache.get((name, caption, crop, eq))
    if hit:
        return hit
    # A "<name>2" file wins if one is there. That is the drop-in slot for a
    # redrawn version: a flat illustration needs no crop (it is already
    # composed), whereas the photograph does, so picking the variant also
    # decides whether the crop applies.
    src, redrawn = None, False
    for stem in (name + "2", name):
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            cand = os.path.join(APP, stem + ext)
            if os.path.exists(cand):
                src, redrawn = cand, stem.endswith("2")
                break
        if src:
            break
    if src is None:
        return None
    if redrawn:
        crop = ""
    ncols = _photo_size(src, crop)
    W, H = ncols * 2, PHOTO_ROWS * 3
    raw = _photo_pixels(src, W, H, crop, eq)
    left = max(1, (40 - ncols) // 2)          # centred, attribute to its left

    def px(x, y):
        o = (y * W + x) * 3
        return raw[o], raw[o + 1], raw[o + 2]

    pg = blank()
    for cy in range(PHOTO_ROWS):
        blocks, colours, alphas = [], [], []
        for cx in range(ncols):
            blk = [px(cx * 2 + dx, cy * 3 + dy)
                   for dy in range(3) for dx in range(2)]
            # Pick the cell colour from the block's HUE. Least squares was
            # wrong here and wrong in an instructive way: white can part-match
            # ANY colour (alpha just scales it), so it won every desaturated
            # cell and the whole hedge came out white. Thresholding each
            # channel against the block's own midpoint is what teletext
            # artists do, and it keeps dark green green.
            m = [sum(p[k] for p in blk) / 6.0 for k in range(3)]
            mx, mn = max(m), min(m)
            sat = (mx - mn) / mx if mx > 8 else 0.0
            if mx < 55:
                # Genuinely dark: leave the cell empty. Hue is meaningless
                # down here -- a few units of sensor noise decide it, which
                # is what turned the conifer into blue/green/cyan confetti.
                # Inherit the running colour so this costs no attribute cell.
                colours.append(colours[-1] if colours else 7)
                alphas.append([0.0] * 6)
                blocks.append(blk)
                continue
            if sat < 0.22:
                ci = 7                              # grey: white, dithered
            else:
                # 0.72 of the way up, not the midpoint: a dark blue-green
                # conifer sits just above a midpoint on BOTH green and blue
                # and comes out cyan. Demanding a clearer lead keeps it green.
                th = mn + 0.72 * (mx - mn)
                ci = ((1 if m[0] > th else 0)
                      | (2 if m[1] > th else 0)
                      | (4 if m[2] > th else 0))
                ci = {0: 7, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7}[ci]
            f = TT_RGB[ci]
            ff = float(f[0] * f[0] + f[1] * f[1] + f[2] * f[2]) or 1.0
            al = []
            for p in blk:
                a = (p[0] * f[0] + p[1] * f[1] + p[2] * f[2]) / ff
                a *= 1.20                           # lift the mid-tones a bit
                al.append(0.0 if a < 0 else (1.0 if a > 1 else a))
            blocks.append(blk)
            colours.append(ci)
            alphas.append(al)
        colours = _smooth_runs(colours)
        out = bytearray(b"\x20" * 40)
        cur = colours[0]
        out[left - 1] = 0x10 + cur             # this row's mosaic colour
        for cx in range(ncols):
            col = left + cx
            if colours[cx] != cur:
                cur = colours[cx]
                out[col] = 0x10 + cur          # attribute cell: reads as blank
                continue
            bits = 0
            for i, a in enumerate(alphas[cx]):
                if a > BAYER6[i]:
                    bits |= 1 << i
            # sextant 5 lives in bit 6 of the character code
            out[col] = 0x20 | (bits & 0x1F) | ((bits & 0x20) << 1)
        pg[cy] = out
    if caption:
        put(pg, 23, max(0, (40 - len(caption)) // 2),
            alpha(WHITE) + T(caption))
    _photo_cache[(name, caption, crop, eq)] = pg
    return pg


# --- remote page rendering -----------------------------------------------
# The Pi 2 needs ~250 ms of pure-Python work to paint one 1280x720 teletext
# frame; this box does the identical work in ~7 ms. So the receiver may hand
# us its finished 40x24 character grid and get pixels back. A rendered page
# is nearly all flat colour, so zlib takes 3.7 MB down to ~58 KB at level 1 --
# small enough that transfer plus the Pi's decompress still costs far less
# than rendering locally. Note this is CPU, not the GTX 1060: blitting 5x7
# glyphs is not work a GPU would do any better, and keeping it on the CPU
# avoids fighting the transcoder for the card.
#
# The renderer is IMPORTED FROM THE RECEIVER'S OWN MODULE on purpose. A copy
# of the glyph ROM here would be a second source of truth that silently drifts.
_chip = None


def renderer():
    global _chip
    if _chip is None:
        import teletext as _tt
        _chip = _tt.SAA5050()
    return _chip


# --- live channel resolution ---------------------------------------------
# ZDF (and with it 3sat, phoenix, KiKA, arte) does not publish a stable
# manifest URL: the CDN path rotates and the old fixed akamai links now 403.
# What IS public and needs no account is their own player API -- the same one
# zdf.de hands every browser: a short-lived apiToken sits in the live-tv page,
# and a per-channel "ptmdTemplate" resolves to the current manifest. So we do
# exactly what their player does, and re-resolve when the token expires.
# Nothing here defeats an access control: these are free-to-air, licence-fee
# funded channels that anyone can watch on zdf.de without logging in.
ZDF_PAGE = "https://www.zdf.de/live-tv"
ZDF_PLAYER = "ngplayer_2_4"
_zdf_token = (0.0, "")
_live_cache = {}


def _zdf_api_token():
    global _zdf_token
    if _zdf_token[0] > time.time():
        return _zdf_token[1]
    import urllib.request as ur
    req = ur.Request(ZDF_PAGE, headers={"User-Agent": NEWS_UA})
    html = ur.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    # the payload is JSON-inside-JSON, so the quotes around the key arrive
    # backslash-escaped; a character class steps over however many layers
    tok = re.search(r'apiToken[\\":\s]+([a-zA-Z0-9]{25,})', html).group(1)
    _zdf_token = (time.time() + 3600, tok)          # page says ~24h; be safe
    return tok


def zdf_live_url(onair_id):
    """Current HLS manifest for a ZDF-operated channel, best quality."""
    import urllib.request as ur
    tok = _zdf_api_token()
    path = "/tmd/2/%s/live/ptmd/%s" % (ZDF_PLAYER, onair_id)
    req = ur.Request("https://api.zdf.de" + path,
                     headers={"User-Agent": NEWS_UA,
                              "Api-Auth": "Bearer " + tok})
    d = json.load(ur.urlopen(req, timeout=20))
    order = {"veryhigh": 4, "high": 3, "med": 2, "low": 1, "auto": 5}
    best = ("", "")
    for pl in d.get("priorityList", []):
        for f in pl.get("formitaeten", []):
            if "m3u8" not in (f.get("type") or ""):
                continue
            for q in f.get("qualities", []):
                qn = q.get("quality") or ""
                for tr in ((q.get("audio") or {}).get("tracks") or []):
                    if tr.get("uri") and order.get(qn, 0) >= order.get(best[0], 0):
                        best = (qn, tr["uri"])
    if not best[1]:
        raise ValueError("no m3u8 for " + onair_id)
    return best[1]


def live_url(key):
    """Resolve a channel key from tv.json to a playable manifest URL.

    Cached briefly so zapping up and down the list does not re-hit the API
    once per press, but short enough that an expired token self-heals.
    """
    hit = _live_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    url = ""
    for c in tv_channels():
        if c.get("key") != key:
            continue
        if c.get("zdf"):
            try:
                url = zdf_live_url(c["zdf"])
            except Exception as exc:
                print("zdf resolve %s failed: %s" % (key, exc), flush=True)
                url = c.get("url", "")
        else:
            url = c.get("url", "")
        break
    if url:
        _live_cache[key] = (time.time() + 600, url)
    return url


def tv_channels():
    try:
        with open(os.path.join(APP, "tv.json"), encoding="utf-8") as fh:
            ch = json.load(fh).get("channels")
        if isinstance(ch, list) and ch:
            out = [c for c in ch if c.get("name") and (c.get("url") or c.get("zdf"))]
            for i, c in enumerate(out):
                c.setdefault("key", re.sub(r"[^a-z0-9]+", "",
                                           c["name"].lower()) or str(i))
            return out[:32]
    except (OSError, ValueError):
        pass
    return TV_DEFAULT


TV_PER_PAGE = 15


def tv_page(sub=1):
    chans = tv_channels()
    total = max(1, (len(chans) + TV_PER_PAGE - 1) // TV_PER_PAGE)
    sub = min(max(1, sub), total)
    start = (sub - 1) * TV_PER_PAGE
    pg = blank()
    hdr(pg, "FERNSEHEN")
    links = {}
    for i, c in enumerate(chans[start:start + TV_PER_PAGE]):
        n = start + i + 1
        row = 4 + i
        put(pg, row, 0, alpha(GREEN) + b"  ")
        put(pg, row, 3, T("%2d %s" % (n, c["name"][:33])))
        # the manifest is resolved at zap time, not now: ZDF's rotates and
        # its token expires, so a URL baked into the page would go stale
        links[str(n)] = {"live": True, "ch": c["key"], "row": row,
                         "play": c.get("url", ""), "name": c["name"],
                         "epg": c.get("epg", "")}
    put(pg, 20, 2, alpha(WHITE) + T("Live-Streams der Sender"))
    put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER schaltet um"))
    if total > 1:
        put(pg, 22, 2, alpha(CYAN) + T(". = weitere Sender (%d/%d)" % (sub, total)))
    return pg, total, links
NEWS_UA = ("Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Safari/537.36")
NEWS_TTL = 600          # seconds; be a polite guest on someone's free feed
NEWS_DEFAULT = ["worldnews", "todayilearned", "explainlikeimfive",
                "retrogaming", "germany"]
_news_cache = {}


def news_subs():
    try:
        with open(os.path.join(APP, "reddit.json"), encoding="utf-8") as fh:
            subs = json.load(fh).get("subs")
        if isinstance(subs, list) and subs:
            return [str(x) for x in subs][:20]
    except (OSError, ValueError):
        pass
    return NEWS_DEFAULT


def _strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    import html as _html
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# Reddit rate-limits per IP, and it counts EVERY request -- the hot feeds and
# the comment feeds share one budget. Firing them off in parallel got us 429s
# that we then blamed on Reddit. So every request to them goes through here,
# serialised with a gap: one caller at a time, never faster than REDDIT_GAP.
_reddit_lock = threading.Lock()
_reddit_last = [0.0]
REDDIT_GAP = 2.5


def reddit_get(url, timeout=25):
    import urllib.request as ur
    with _reddit_lock:
        wait = REDDIT_GAP - (time.time() - _reddit_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = ur.Request(url, headers={"User-Agent": NEWS_UA})
            return ur.urlopen(req, timeout=timeout).read()
        finally:
            _reddit_last[0] = time.time()


_article_cache = {}


def _ld_article(html_doc):
    """The article text out of the page's own schema.org data.

    Most news sites ship the full body in a <script type="application/ld+json">
    block for search engines -- freely served, to an unauthenticated request,
    from the publisher's own markup. Reading that is what a reader-mode or a
    crawler does, and it works where a paragraph heuristic does not, because
    modern news pages wrap their text in markup no heuristic can guess.
    """
    best = ""
    for block in re.findall(
            r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>",
            html_doc):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                stack.extend(v for v in item.values()
                             if isinstance(v, (dict, list)))
                body = item.get("articleBody")
                if isinstance(body, str) and len(body) > len(best):
                    best = body
    return _strip_html(best).strip()


def _meta_summary(html_doc):
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+'
                r'content=["\']([^"\']+)',
                r'<meta[^>]+name=["\']description["\'][^>]+'
                r'content=["\']([^"\']+)'):
        m = re.search(pat, html_doc, re.I)
        if m:
            return _strip_html(m.group(1)).strip()
    return ""


def _densest_text(html_doc):
    """A small readability: strip the furniture, then keep the block of
    paragraphs with the most actual prose in it. Not as clever as Safari
    Reader, but it needs no dependencies and it reliably beats "show me the
    whole page" on a 40-column screen."""
    import re
    doc = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|figure"
                 r"|noscript|svg|iframe).*?</\1>", " ", html_doc)
    # score each <p>; keep the run of paragraphs that carries the article
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", doc)
    out = []
    for p in paras:
        t = _strip_html(p)
        # skip nav crumbs, bylines, cookie notices: real prose has sentences
        if len(t) >= 60 and t.count(" ") >= 8:
            out.append(t)
    return "\n\n".join(out)


def article_fetch(url):
    """Readable text of a linked page. Freely accessible pages only -- this
    deliberately does not attempt to defeat paywalls or logins."""
    hit = _article_cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    import urllib.request as ur
    text = ""
    try:
        req = ur.Request(url, headers={"User-Agent": NEWS_UA,
                                       "Accept-Language": "de,en;q=0.8"})
        with ur.urlopen(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                raise ValueError("not html (%s)" % ctype.split(";")[0])
            raw = r.read(600000)
        enc = "utf-8"
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
        doc = raw.decode(enc, "replace")
        # schema.org first: it is the publisher's own copy of the text and
        # survives markup the paragraph scan cannot follow
        text = _ld_article(doc)
        if len(text) < 200:
            text = _densest_text(doc)
        if len(text) < 200:                    # last resort: the summary
            text = _meta_summary(doc)
        if len(text) < 80:
            text = ""
    except Exception as exc:
        print("article %s failed: %s" % (url[:60], exc), flush=True)
    if len(_article_cache) > 40:
        _article_cache.clear()
    _article_cache[url] = (time.time() + 1800, text)
    return text


def news_fetch(sub):
    """Atom feed -> [{title, text}]. Cached; failures are cached briefly too
    so a dead feed cannot turn into a request storm."""
    hit = _news_cache.get(sub)
    if hit and hit[0] > time.time():
        return hit[1]
    import urllib.request as ur
    import xml.etree.ElementTree as ET
    entries = []
    try:
        raw = reddit_get("https://www.reddit.com/r/%s/hot.rss?limit=16"
                         % urllib.parse.quote(sub), timeout=20)
        root = ET.fromstring(raw)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            title = (e.findtext("a:title", "", ns) or "").strip()
            body = _strip_html(e.findtext("a:content", "", ns) or "")
            # every entry ends with the feed's own footer; on a link post
            # that IS the whole body, so strip it and say so plainly
            body = re.sub(r"(?is)\s*submitted by\s*/u/\S+.*$", "", body)
            body = re.sub(r"(?is)\[link\]\s*\[comments\]\s*$", "", body).strip()
            author = (e.findtext("a:author/a:name", "", ns) or "").strip()
            raw_content = e.findtext("a:content", "", ns) or ""
            m = re.search(r'href="([^"]+)"[^>]*>\s*\[link\]', raw_content)
            link = m.group(1) if m else ""
            if link and "reddit.com" in link:
                link = ""              # the comments page, not an article
            perma = ""
            le = e.find("a:link", ns)
            if le is not None:
                perma = le.get("href") or ""
            if title:
                entries.append({"title": title, "text": body, "by": author,
                                "link": link, "permalink": perma})
    except Exception as exc:                      # feed down, XML odd, offline
        print("reddit %s failed: %s" % (sub, exc), flush=True)
        _news_cache[sub] = (time.time() + 60, [])
        return []
    _news_cache[sub] = (time.time() + NEWS_TTL, entries)
    return entries


# --- comments -------------------------------------------------------------
# Reddit's comment feed is FLAT. It carries no thr:in-reply-to and no parent
# id -- every entry knows only its own id -- so the tree genuinely cannot be
# reconstructed from it. Threading would need the OAuth JSON API. What we can
# do faithfully is a flat "was dazu gesagt wurde" list, top-sorted, which is
# also the more teletext-ish thing anyway.
#
# ⚠️ The URL form matters: <permalink>.rss reliably 429s, <permalink>/.rss
# answers. Same feed, different rate-limit treatment.
#
# Fetched on a BACKGROUND thread, never on the request path: the receiver
# gives a page 3 seconds and Reddit often needs a patient retry after a 429.
# So the first look says "wird geladen" and the next page turn has them --
# which is exactly how waiting for a teletext page always felt.
_comments = {}
_comments_busy = set()
_comments_lock = threading.Lock()
COMMENTS_TTL = 900


def _comments_load(permalink):
    import xml.etree.ElementTree as ET
    url = permalink.rstrip("/") + "/.rss?sort=top&limit=40"
    out = None                       # None stays "we never got an answer"
    for attempt in range(4):
        try:
            raw = reddit_get(url)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            out = []
            for e in ET.fromstring(raw).findall("a:entry", ns):
                if not (e.findtext("a:id", "", ns) or "").startswith("t1_"):
                    continue          # the post itself, not a comment
                body = _strip_html(e.findtext("a:content", "", ns) or "").strip()
                who = (e.findtext("a:author/a:name", "", ns) or "").strip()
                if body:
                    out.append({"by": who, "text": body})
            break
        except Exception as exc:
            print("comments %s attempt %d: %s"
                  % (permalink[-24:], attempt + 1, getattr(exc, "code", exc)),
                  flush=True)
            time.sleep(5 * (attempt + 1))          # 5s, 10s, 15s
    with _comments_lock:
        if out is None:
            # a throttled fetch is NOT "this post has no comments" -- caching
            # it as such would show a lie for a quarter of an hour
            _comments[permalink] = (time.time() + 60, False)
        else:
            _comments[permalink] = (time.time() + COMMENTS_TTL, out)
        _comments_busy.discard(permalink)


def comments_get(permalink):
    """Cached comments, or None while they are still on their way."""
    if not permalink:
        return []
    with _comments_lock:
        hit = _comments.get(permalink)
        if hit and hit[0] > time.time():
            return hit[1]
        if permalink in _comments_busy:
            return None
        _comments_busy.add(permalink)
    threading.Thread(target=_comments_load, args=(permalink,),
                     daemon=True).start()
    return None


def news_page(num, sub_no):
    """700 = index of subreddits. 701+ = one subreddit: subpage 1 is the
    headline list, subpage k+1 is post k's text -- the same mechanism a real
    service used to spill a long article across pages."""
    subs = news_subs()
    if num == P_NEWS:
        # 700 is now a choice of NEWSROOMS: the reddit feeds on one side, the
        # set's own search-and-argue reader on the other
        pg = blank()
        hdr(pg, "NEWS")
        put(pg, 5, 1, alpha(CYAN) + T(" 1 ") + alpha(WHITE) + T("Reddit") +
            b"." * 20 + alpha(CYAN) + T("709"))
        put(pg, 7, 1, alpha(CYAN) + T(" 2 ") + alpha(WHITE) +
            T("ChatCRT News") + b"." * 14 + alpha(CYAN) + T("710"))
        put(pg, 11, 2, alpha(WHITE) + T("Reddit liest Feeds."))
        put(pg, 12, 2, alpha(WHITE) + T("ChatCRT sucht selbst und"))
        put(pg, 13, 2, alpha(WHITE) + T("diskutiert mit Ihnen dar}ber."
                                        .replace("}", "ü")))
        put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER"))
        return pg, 1, {"items": {"1": {"page": P_NEWS + 9},
                                 "2": {"page": 710}}}
    if num == P_NEWS + 9:
        pg = blank()
        hdr(pg, "REDDIT")
        links = {}
        for i, s in enumerate(subs[:PER_PAGE]):
            row = 4 + i
            put(pg, row, 0, alpha(CYAN) + b"  ")
            put(pg, row, 3, T("%3d  r/%s" % (P_NEWS + 1 + i, s[:28])))
            links[str(i + 1)] = {"page": P_NEWS + 1 + i}
        put(pg, 21, 2, alpha(CYAN) + T("Seitennummer tippen oder Nr + ENTER"))
        return pg, 1, {"items": links}

    idx = num - P_NEWS - 1
    if idx == 8 or not (0 <= idx < len(subs)):
        return None, 1, {}
    sub = subs[idx]
    posts = news_fetch(sub)
    total = 1 + len(posts)

    if sub_no <= 1:
        pg = blank()
        hdr(pg, ("R/" + sub)[:20].upper())
        links = {}
        if not posts:
            put(pg, 8, 4, alpha(RED) + T("Feed nicht erreichbar"))
            return pg, 1, {"items": {}}
        for i, p in enumerate(posts[:PER_PAGE]):
            row = 4 + i
            put(pg, row, 0, alpha(WHITE) + b"  ")
            put(pg, row, 3, T("%2d %s" % (i + 1, p["title"][:34])))
            links[str(i + 1)] = {"sub": i + 2}
        put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER liest, . bl{ttert".replace(
            "{", chr(0xE4))))
        return pg, total, {"items": links}

    # subpage 2..N+1 is post 1..N; anything beyond that is the running text
    # of the post the user is reading, continued page by page
    LINES_FIRST, LINES_MORE = 12, 17
    post_no, cont = sub_no - 1, 0
    if post_no > len(posts):
        post_no = getattr(news_page, "_last_post", 1)
        cont = sub_no - 1 - len(posts)
    news_page._last_post = post_no
    p = posts[post_no - 1] if 0 < post_no <= len(posts) else None
    qr_url = (p or {}).get("permalink") or ""

    pg = blank()
    hdr(pg, ("R/" + sub)[:20].upper())
    if not p:
        put(pg, 8, 4, alpha(RED) + T("Kein Beitrag"))
        return pg, total, {"items": {}}

    body_text = p["text"]
    src = "Reddit"
    if not body_text and p.get("link"):
        body_text = article_fetch(p["link"])
        src = p["link"].split("/")[2] if body_text else ""

    # article and comments are ONE flow of coloured lines, so spilling over
    # subpages stays a single slice and comments simply continue where the
    # text ended -- no second pagination scheme to keep in step
    flow = [(WHITE, ln) for ln in (wrap_text(body_text, 37) if body_text else [])]
    if not flow:
        flow = [(RED, "Kein Text (Bild oder Video)")]
        if p.get("link"):
            flow += [(WHITE, ""), (CYAN, p["link"][:37])]

    comments = comments_get(qr_url)
    if comments is None:
        flow += [(WHITE, ""), (CYAN, "Kommentare werden geladen ...")]
    elif comments is False:
        flow += [(WHITE, ""), (RED, "Kommentare gerade nicht abrufbar")]
    elif comments:
        flow += [(WHITE, ""),
                 (YELLOW, "KOMMENTARE (%d)" % len(comments)),
                 (WHITE, "")]
        for c in comments:
            flow.append((CYAN, ("/u/" + c["by"].lstrip("/u/"))[:37]
                         if c["by"] else "/u/?"))
            flow += [(WHITE, ln) for ln in wrap_text(c["text"], 37)[:14]]
            flow.append((WHITE, ""))
    else:
        flow += [(WHITE, ""), (CYAN, "Keine Kommentare")]

    if cont:
        put(pg, 2, 2, alpha(YELLOW) + T(p["title"][:36]))
        r, start = 4, LINES_FIRST + (cont - 1) * LINES_MORE
        chunk = flow[start:start + LINES_MORE]
    else:
        r = 4
        for line in wrap_text(p["title"], 37)[:3]:
            put(pg, r, 2, alpha(YELLOW) + T(line))
            r += 1
        if p.get("by"):
            put(pg, r, 2, alpha(CYAN) + T(p["by"][:36]))
        r += 2
        chunk = flow[:LINES_FIRST]

    for colour, line in chunk:
        if line:
            put(pg, r, 2, alpha(colour) + T(line))
        r += 1
    if src:
        put(pg, 22, 2, alpha(CYAN) + T(("Quelle: " + src)[:37]))
    put(pg, 23, 0, alpha(CYAN) + T(".=weiter  %d=Liste" % (P_NEWS + 1 + idx)))

    put(pg, 21, 2, alpha(GREEN) + T("x") + alpha(WHITE) +
        T(" QR-Code aufs Handy"))
    extra = max(0, len(flow) - LINES_FIRST)
    total = 1 + len(posts) + (extra + LINES_MORE - 1) // LINES_MORE
    return pg, total, {"items": {}, "qr": qr_url}


def wrap_text(text, width=37):
    out = []
    for para in text.split("\n"):
        words, cur = para.split(), ""
        if not words:
            out.append("")
            continue
        for w in words:
            if len(cur) + len(w) + 1 > width:
                out.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            out.append(cur)
    return out

_lock = threading.Lock()
_current = None
_scan_state = {"running": False, "last": 0.0}

# -- Jellyfin metadata (key made once for discpipe, reused read-only) --
JELLY = "http://127.0.0.1:8096"
try:
    with open(os.path.join(APP, "jellyfin.key")) as _fh:
        JELLY_KEY = _fh.read().strip()
except OSError:
    JELLY_KEY = None
# Jellyfin's container mounts -> our server paths
JELLY_ROOTS = {"/data/movies": "/mnt/user/Movies",
               "/data/tvshows": "/mnt/user/TV Shows"}
PI_CTRL = "http://192.168.1.68:8204"
_desc_cache = {}
_jelly_index = {"exp": 0.0, "paths": {}, "series": {}}


def _jelly_load():
    """One library dump, indexed by path; refreshed every 6h. O(1) after."""
    if _jelly_index["exp"] > time.time() or not JELLY_KEY:
        return
    import urllib.request as ur
    paths, series = {}, {}
    try:
        url = (JELLY + "/Items?Recursive=true&IncludeItemTypes=Movie,Episode"
               "&Fields=Path,Overview,CommunityRating&api_key=" + JELLY_KEY)
        with ur.urlopen(url, timeout=20) as r:
            for it in json.load(r).get("Items", []):
                p = it.get("Path") or ""
                for jr, our in JELLY_ROOTS.items():
                    if p.startswith(jr):
                        p = our + p[len(jr):]
                        break
                if p:
                    paths[p] = (it.get("Name") or "", it.get("Overview") or "",
                                it.get("CommunityRating"))
        url = (JELLY + "/Items?Recursive=true&IncludeItemTypes=Series"
               "&Fields=Overview,CommunityRating&api_key=" + JELLY_KEY)
        with ur.urlopen(url, timeout=20) as r:
            for it in json.load(r).get("Items", []):
                series[(it.get("Name") or "").lower()] = \
                    (it.get("Name") or "", it.get("Overview") or "",
                     it.get("CommunityRating"))
    except (OSError, ValueError) as exc:
        print("jellyfin index failed: %s" % exc, flush=True)
        return
    _jelly_index.update(exp=time.time() + 6 * 3600, paths=paths, series=series)
    print("jellyfin index: %d items, %d series" % (len(paths), len(series)),
          flush=True)


def _wiki(title):
    import urllib.request as ur
    for cand in (title, re.sub(r"\s+(19|20)\d{2}$", "", title)):
        for lang in ("de", "en"):
            try:
                req = ur.Request(
                    "https://%s.wikipedia.org/api/rest_v1/page/summary/%s"
                    % (lang, urllib.parse.quote(cand.replace(" ", "_"))),
                    headers={"User-Agent": "TelecommanderOS/1.0"})
                with ur.urlopen(req, timeout=5) as r:
                    ext = json.load(r).get("extract") or ""
                if ext:
                    return ext
            except (OSError, ValueError):
                continue
    return ""


def _duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20)
        return int(float(out.stdout.strip()))
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def describe(path):
    hit = _desc_cache.get(path)
    if hit:
        return hit
    _jelly_load()
    name, text, src, rating = clean_title(path), "", "none", None
    it = _jelly_index["paths"].get(path)
    if it and it[1]:
        name, text, src, rating = it[0], it[1], "Jellyfin", it[2]
    else:
        for root in ("/mnt/user/TV Shows/",):
            if path.startswith(root):
                show = path[len(root):].split("/", 1)[0]
                se = _jelly_index["series"].get(show.lower())
                if se and se[1]:
                    name, text, src, rating = se[0], se[1], "Jellyfin", se[2]
    if not text:
        w = _wiki(name)
        if w:
            text, src = w, "Wikipedia"
    out = {"title": name, "text": text, "source": src,
           "rating": round(rating, 1) if isinstance(rating, (int, float))
           else None,
           "duration": _duration(path)}
    if len(_desc_cache) > 64:
        _desc_cache.clear()
    _desc_cache[path] = out
    return out


def pi_proxy(method, action, timeout=8):
    import urllib.request as ur
    req = ur.Request(PI_CTRL + "/" + action, method=method,
                     data=b"" if method == "POST" else None)
    try:
        with ur.urlopen(req, timeout=timeout) as r:
            return 200, r.read()
    except OSError as exc:
        return 502, json.dumps({"error": str(exc)}).encode()

# ---------------------------------------------------------------- teletext
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
_DE = str.maketrans({"Ä": "[", "Ö": "\\", "Ü": "]",
                     "ä": "{", "ö": "|", "ü": "}", "ß": "~", "°": "`"})


def T(s):
    return bytes(c if 0x20 <= c < 0x80 else 0x3F
                 for c in s.translate(_DE).encode("ascii", "replace"))


def alpha(c): return bytes([c])
def mosaic(c): return bytes([0x10 + c])


DH = b"\x0d"


# --- watch-count colouring -------------------------------------------------
# Level 1 teletext has SEVEN usable colours and the watch counter cycles
# through exactly those (Martin: "7 colors is fine"). Every state is THREE
# cells wide even though only one is needed -- a fixed-width prefix is what
# lets the serve path recolour a row by overwriting bytes instead of
# re-laying it out, and it leaves room to go inverted later without moving
# any text.
_NB = bytes([0x1D])          # new background = current foreground
_SP = b" "


def _A(c):
    return bytes([c])


WATCH_STATES = [
    _A(2) + _SP + _SP,        # 0  never watched   green
    _A(4) + _SP + _SP,        # 1  watched once    blue
    _A(1) + _SP + _SP,        # 2  red
    _A(3) + _SP + _SP,        # 3  yellow
    _A(5) + _SP + _SP,        # 4  magenta
    _A(6) + _SP + _SP,        # 5  cyan
    _A(7) + _SP + _SP,        # 6  white, then wraps to green
]
WATCH_FILE = None            # set in __main__ (needs PAGES)
_watch = {"counts": {}, "loaded": False}


def watch_load():
    if _watch["loaded"]:
        return _watch["counts"]
    try:
        with open(os.path.join(PAGES, "watched.json"), encoding="utf-8") as fh:
            _watch["counts"] = json.load(fh)
    except (OSError, ValueError):
        _watch["counts"] = {}
    _watch["loaded"] = True
    return _watch["counts"]


def watch_bump(path):
    counts = watch_load()
    counts[path] = int(counts.get(path, 0)) + 1
    tmp = os.path.join(PAGES, "watched.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(counts, fh)
    os.replace(tmp, os.path.join(PAGES, "watched.json"))
    return counts[path]


def watch_paint(raw, links):
    """Overwrite each listed row's 3-cell attribute prefix with the state for
    that file's watch count. Hand-edited pages carry no row map, so they are
    left exactly as their author drew them."""
    counts = watch_load()
    items = (links or {}).get("items") or {}
    if not items:
        return raw
    buf = bytearray(raw)
    for meta in items.values():
        if not isinstance(meta, dict):
            continue
        row, play = meta.get("row"), meta.get("play")
        if row is None or not play:
            continue
        state = WATCH_STATES[int(counts.get(play, 0)) % len(WATCH_STATES)]
        off = int(row) * 40
        buf[off:off + 3] = state
    return bytes(buf)


def blank():
    return [bytearray(b"\x20" * 40) for _ in range(24)]


def put(pg, r, c, data):
    pg[r][c:c + len(data)] = data[:40 - c]


def save_page(num, sub, rows, links=None):
    os.makedirs(PAGES, exist_ok=True)
    with open(os.path.join(PAGES, "%03d.%d.tt" % (num, sub)), "wb") as fh:
        fh.write(b"".join(bytes(r[:40].ljust(40, b" ")) for r in rows))
    lp = os.path.join(PAGES, "%03d.%d.links.json" % (num, sub))
    if links:
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump({"items": links}, fh)
    elif os.path.exists(lp):
        os.remove(lp)


def edited_path(num, sub):
    return os.path.join(PAGES, "%03d.%d.edited" % (num, sub))


def is_edited(num, sub=1):
    """True once the page has been saved from the web editor. The generator
    refreshes its own pages (so the index can gain new entries as features
    land) but never touches a page a human has taken ownership of."""
    return os.path.exists(edited_path(num, sub))


def page_subs(num):
    n = 0
    while os.path.exists(os.path.join(PAGES, "%03d.%d.tt" % (num, n + 1))):
        n += 1
    return n


def load_page(num, sub):
    try:
        with open(os.path.join(PAGES, "%03d.%d.tt" % (num, sub)), "rb") as fh:
            raw = fh.read(960)
    except OSError:
        return None
    links = {}
    try:
        with open(os.path.join(PAGES, "%03d.%d.links.json" % (num, sub)),
                  encoding="utf-8") as fh:
            links = json.load(fh)
    except (OSError, ValueError):
        pass
    return raw.ljust(960, b" "), links


# ---------------------------------------------------------------- scanner
_JUNK = re.compile(
    r"[.\s_-]*(1080p|720p|2160p|480p|WEB-?DL|WEBRip|HDTV|BluRay|BRRip|DVDRip"
    r"|x264|x265|h264|HEVC|AAC|AC3|DTS|Premiere|REMASTERED|EXTENDED"
    r"|\[.*?\]|\(\d{4}\)).*", re.I)
_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")


def clean_title(path):
    name = os.path.splitext(os.path.basename(path))[0]
    name = _JUNK.sub("", name).replace(".", " ").replace("_", " ").strip(" -")
    return name or os.path.basename(path)


def scan_media():
    movies, shows = [], {}
    for root in SCAN_ROOTS:
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDE_DIRS]
            for n in names:
                if not n.lower().endswith(EXTS):
                    continue
                p = os.path.join(dirpath, n)
                if "Movies" in root:
                    movies.append((clean_title(p), p))
                else:
                    show = dirpath[len(root):].split(os.sep, 1)[0] or "?"
                    m = _EP.search(n)
                    key = (int(m.group(1)), int(m.group(2))) if m else (99, 99)
                    shows.setdefault(show, []).append((key, p))
    return (sorted(movies, key=lambda x: x[0].lower()),
            sorted(((k, sorted(v)) for k, v in shows.items()),
                   key=lambda x: x[0].lower()))


def clean_rom(name):
    """Strip the No-Intro/GoodNES cruft: 'Zelda II - The Adventure of Link
    (USA).nes' -> 'Zelda II - The Adventure of Link'."""
    n = os.path.splitext(name)[0]
    n = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", n)
    return n.strip() or name


def gen_games():
    try:
        roms = sorted(f for f in os.listdir(ROM_ROOT)
                      if f.lower().endswith(".nes"))
    except OSError:
        return 0
    nsub = max(1, (len(roms) + PER_PAGE - 1) // PER_PAGE)
    for s in range(1, nsub + 1):
        pg = blank()
        hdr(pg, "SPIELE")
        if nsub > 1:
            put(pg, 2, 33, alpha(CYAN) + T("%2d/%d" % (s, nsub)))
        links = {}
        base = (s - 1) * PER_PAGE
        for i, f in enumerate(roms[base:base + PER_PAGE]):
            row = 4 + i
            put(pg, row, 0, alpha(CYAN) + b"  ")
            put(pg, row, 3, T("%2d %s" % (i + 1, clean_rom(f)[:33])))
            links[str(i + 1)] = {"rom": ROM_ROOT + f, "row": row}
        put(pg, 20, 2, alpha(WHITE) + T("Steuerung: Joystick + A B"))
        put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER startet, C beendet"))
        save_page(P_GAMES, s, pg, links)
    for s in range(nsub + 1, page_subs(P_GAMES) + 1):
        os.remove(os.path.join(PAGES, "%03d.%d.tt" % (P_GAMES, s)))
    return len(roms)


def hdr(pg, title, color=YELLOW):
    put(pg, 2, 2, alpha(color) + DH + T(title))


def generate():
    movies, shows = scan_media()

    # --- 200-series: movies ---
    nsub = max(1, (len(movies) + PER_PAGE - 1) // PER_PAGE)
    for s in range(1, nsub + 1):
        pg = blank()
        hdr(pg, "FILME")
        put(pg, 2, 33, alpha(CYAN) + T("%2d/%d" % (s, nsub)))
        links = {}
        base = (s - 1) * PER_PAGE
        for i, (title, path) in enumerate(movies[base:base + PER_PAGE]):
            row = 4 + i
            put(pg, row, 0, WATCH_STATES[0])      # recoloured at serve time
            put(pg, row, 3, T("%2d %s" % (i + 1, title[:33])))
            links[str(i + 1)] = {"play": path, "row": row}
        put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER spielt ab   Farbe = gesehen"))
        save_page(200, s, pg, links)
    for s in range(nsub + 1, page_subs(200) + 1):
        os.remove(os.path.join(PAGES, "200.%d.tt" % s))

    # --- 300: show index; 301+: one page per show ---
    pg = blank()
    hdr(pg, "SERIEN")
    links = {}
    for i, (name, eps) in enumerate(shows[:PER_PAGE]):
        num = 301 + i
        put(pg, 4 + i, 1, alpha(CYAN) + T("%3d" % num) +
            alpha(WHITE) + T(" " + name[:26]) +
            alpha(GREEN) + T("%4d" % len(eps)))
        links[str(i + 1)] = {"page": num}
    put(pg, 21, 2, alpha(CYAN) + T("Seitennummer tippen = Serie"))
    save_page(300, 1, pg, links)

    for i, (name, eps) in enumerate(shows[:PER_PAGE]):
        num = 301 + i
        es = max(1, (len(eps) + PER_PAGE - 1) // PER_PAGE)
        for s in range(1, es + 1):
            pg = blank()
            hdr(pg, name[:30].upper())
            put(pg, 2, 34, alpha(CYAN) + T("%d/%d" % (s, es)))
            lk = {}
            base = (s - 1) * PER_PAGE
            for j, ((sn, en), path) in enumerate(eps[base:base + PER_PAGE]):
                row = 4 + j
                put(pg, row, 0, WATCH_STATES[0])
                put(pg, row, 3, T("%2d S%02dE%02d %s" % (
                    j + 1, sn, en, clean_title(path)[:19])))
                lk[str(j + 1)] = {"play": path, "row": row}
            put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER   Farbe = wie oft gesehen"))
            save_page(num, s, pg, lk)

    # --- 100: refreshed every scan so new features appear, unless edited ---
    if not is_edited(100):
        pg = blank()
        put(pg, 2, 4, alpha(YELLOW) + DH + T("TELECOMMANDER TEXT"))
        seq = b""
        for c in (RED, YELLOW, GREEN, CYAN, BLUE, MAGENTA):
            seq += mosaic(c) + b"\x7f" * 5
        put(pg, 5, 2, seq)
        # 111 is deliberately NOT listed: pressing C over a running film
        # already lands there, so it would only be clutter here.
        rows = [("Filme", "200", RED),
                ("Serien", "300", GREEN),
                ("Musik", "600", BLUE),
                ("Spiele", "400", CYAN),
                ("Fernsehen", "800", YELLOW),
                ("Radio", "900", MAGENTA),
                ("News", "700", WHITE),
                ("ChatCRT", "950", CYAN),
                ("Einstellungen", "500", MAGENTA),
                ("Bedienung", "101", YELLOW)]
        r = 6
        for name, num, col in rows:
            put(pg, r, 2, alpha(WHITE) + T(name) +
                b"." * (24 - len(name)) + alpha(col) + T(num))
            r += 1                      # ten entries now: one row each
        save_page(100, 1, pg)

    if not is_edited(101):
        pg = blank()
        hdr(pg, "BEDIENUNG")
        rows = [("Ziffern", "Seite w{hlen".replace("{", "ä")),
                ("ENTER", "Nr best{tigen / Pause".replace("{", "ä")),
                ("C", "OS ein und aus"),
                ("+ -", "lauter / leiser"),
                ("/", "voriger Titel"), ("x", "n{chster Titel".replace("{", "ä")),
                (".", "Unterseiten / Bildformat"),
                ("111", "l{uft + Text (C)".replace("{", "ä")),
                ("400 600", "Spiele / Musik"),
                ("700 800", "News / Fernsehen"),
                ("<-", "zur}ck".replace("}", "ü"))]
        r = 5
        for k, v in rows:
            put(pg, r, 2, alpha(CYAN) + T(k))
            put(pg, r, 12, alpha(WHITE) + T(v))
            r += 2
        save_page(101, 1, pg)

    ngames = gen_games()

    with open(os.path.join(PAGES, ".last-scan"), "w") as fh:
        fh.write(str(time.time()))
    return len(movies), len(shows), ngames


def scan_async():
    if _scan_state["running"]:
        return False
    def run():
        _scan_state["running"] = True
        try:
            nm, ns, ng = generate()
            print("scan done: %d movies, %d shows, %d games" % (nm, ns, ng),
                  flush=True)
        finally:
            _scan_state["running"] = False
            _scan_state["last"] = time.time()
    threading.Thread(target=run, daemon=True).start()
    return True


def scheduler():
    while True:
        try:
            with open(os.path.join(PAGES, ".last-scan")) as fh:
                age = time.time() - float(fh.read().strip())
        except (OSError, ValueError):
            age = 1e12
        if age > SCAN_DAYS * 86400 and not _scan_state["running"]:
            print("scheduled rescan (%.0f days old)" % (age / 86400), flush=True)
            scan_async()
        time.sleep(6 * 3600)


def probe_one(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,height,pix_fmt", "-of", "json",
             path], capture_output=True, timeout=30)
        streams = json.loads(out.stdout or b"{}").get("streams", [])
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {"error": str(exc)}
    info = {}
    for s in streams:
        if s.get("codec_type") == "video" and "v" not in info:
            info["v"], info["pix"] = s.get("codec_name"), s.get("pix_fmt")
            info["height"] = s.get("height")
        elif s.get("codec_type") == "audio" and "a" not in info:
            info["a"] = s.get("codec_name")
    return info or {"error": "no streams"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="text/plain"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    # ------------------------------------------------------------- GET
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self._send(200, "ok")
        if url.path == "/":
            try:
                with open(os.path.join(APP, "editor.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
            except OSError:
                return self._send(404, "editor.html missing")
        if url.path == "/icon.png":
            return self._send(200, make_icon(), "image/png")
        if url.path == "/tt/font":
            from_font = re.findall(r"[0-9A-Fa-f]{2}", _FONT_HEX)
            return self._json({"font": from_font})
        if url.path == "/tt/watched":
            return self._json({"counts": watch_load()})
        if url.path == "/live":
            key = (qs.get("ch") or [""])[0]
            target = live_url(key) if key else ""
            if not target:
                return self._json({"error": "unknown channel"}, 404)
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path == "/news":
            jid = (qs.get("id") or [""])[0]
            job = _news_jobs.get(jid)
            if job is None:
                return self._json({"error": "no such job"}, 404)
            return self._json({"done": job["done"], "data": job["data"],
                               "sections": [{"key": s[0], "label": s[1]}
                                            for s in NEWS_SECTIONS]})
        if url.path == "/t9":
            lang = "en" if (qs.get("lang") or ["de"])[0] == "en" else "de"
            # 1, 2 and 3 are letter keys here; only 0 and 7 are not
            keys = re.sub(r"[^1-689]", "", (qs.get("keys") or [""])[0])[:18]
            return self._json({"words": t9_lookup(lang, keys)})
        if url.path == "/chat":
            jid = (qs.get("id") or [""])[0]
            job = _chat_jobs.get(jid)
            if job is None:
                return self._json({"error": "no such job"}, 404)
            ses = _crt_sessions.get(job.get("sid")) or {}
            return self._json({"done": job["done"],
                               "text": chat_clean(job["text"]),
                               "options": [chat_clean(o)[:34]
                                           for o in job["options"]],
                               "end": bool(job.get("end")),
                               "turn": ses.get("turn", 0),
                               "turns": CRT_TURNS,
                               "mood": ses.get("mood", 0),
                               "premise": chat_clean(ses.get("premise", ""))})
        if url.path == "/tt/nowplaying":
            key = re.sub(r"[^a-z0-9]", "", (qs.get("ch") or [""])[0])
            return self._json({"title": chat_clean(radio_nowplaying(key))})
        if url.path == "/tt/invent":
            jid = (qs.get("id") or [""])[0]
            job = _invent_jobs.get(jid)
            if job is None:
                return self._json({"error": "no such job"}, 404)
            # keep the line breaks: the page is rendered line by line
            text = "\n".join(chat_clean(l)[:36]
                              for l in job["text"].splitlines())
            return self._json({"done": job["done"], "text": text})
        if url.path == "/tt/scores":
            game = re.sub(r"[^a-z0-9]", "", (qs.get("game") or ["snake"])[0])
            return self._json({"scores": scores_load(game)})
        if url.path == "/tt/photos":
            # which digit sequences open a picture, and which picture --
            # the receiver has no personal knowledge baked into it
            return self._json({"photos": personal().get("photos") or {}})
        if url.path == "/tt/photo":
            name = re.sub(r"[^a-z0-9_-]", "", (qs.get("name") or [""])[0].lower())
            try:
                pg = photo_page(
                    name, (qs.get("caption") or [""])[0][:38],
                    re.sub(r"[^0-9:]", "", (qs.get("crop") or [""])[0]),
                    re.sub(r"[^a-z0-9=:.-]", "",
                           (qs.get("eq") or [""])[0]))
            except (OSError, ValueError) as exc:
                return self._json({"error": str(exc)}, 500)
            if pg is None:
                return self._json({"error": "no such picture"}, 404)
            raw = b"".join(bytes(r) for r in pg)
            return self._json({"data": base64.b64encode(raw).decode(),
                               "subs": 1, "links": {}})
        if url.path == "/tt/qr":
            target = (qs.get("url") or [""])[0]
            if not target.startswith(("http://", "https://")):
                return self._json({"error": "url"}, 400)
            try:
                import qrcode
                # Low ECC keeps the module count down, which matters a lot on
                # 576 lines of PAL: fewer, fatter modules scan far better than
                # a dense grid the tube cannot resolve.
                q = qrcode.QRCode(error_correction=qrcode.ERROR_CORRECT_L,
                                  box_size=1, border=0)
                q.add_data(target)
                q.make(fit=True)
                m = q.get_matrix()
                rows = ["".join("1" if c else "0" for c in row) for row in m]
                return self._json({"size": len(rows), "rows": rows})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        if url.path == "/tt/epg":
            cid = (qs.get("ch") or [""])[0]
            if not cid:
                return self._json({"error": "no channel"}, 400)
            cur, nxt = epg_now_next(cid)
            return self._json({"now": cur, "next": nxt})
        if url.path == "/tt/status":
            pages = 0
            try:
                pages = len({f.split(".")[0] for f in os.listdir(PAGES)
                             if f.endswith(".tt")})
            except OSError:
                pass
            try:
                with open(os.path.join(PAGES, ".last-scan")) as fh:
                    age = int(time.time() - float(fh.read().strip()))
            except (OSError, ValueError):
                age = -1
            _jelly_load()
            return self._json({"pages": pages,
                               "scan_running": _scan_state["running"],
                               "scan_age_s": age,
                               "jellyfin_items": len(_jelly_index["paths"]),
                               "jellyfin_key": bool(JELLY_KEY),
                               "watched_files": len(watch_load())})
        if url.path == "/tt/list":
            pages = {}
            try:
                for f in os.listdir(PAGES):
                    m = re.match(r"^(\d{3})\.(\d+)\.tt$", f)
                    if m:
                        n = int(m.group(1))
                        pages[str(n)] = max(pages.get(str(n), 0),
                                            int(m.group(2)))
            except OSError:
                pass
            pages[str(P_TV)] = 1
            pages[str(P_NEWS)] = 1
            for i, _s in enumerate(news_subs()):
                pages[str(P_NEWS + 1 + i)] = 1
            return self._json({"pages": pages,
                               "scan_running": _scan_state["running"]})
        m = re.match(r"^/tt/page/(\d{1,3})$", url.path)
        if m:
            num = int(m.group(1))
            sub = int((qs.get("sub") or ["1"])[0])
            if num == SCAN_TRIGGER_PAGE:
                started = scan_async()
                pg = blank()
                put(pg, 8, 6, alpha(GREEN if started else YELLOW) + DH +
                    T("SCAN GESTARTET" if started else "SCAN L[UFT SCHON"
                      .replace("[", "Ä")))
                put(pg, 14, 6, T("Neue Listen in K}rze".replace("}", "ü")))
                raw = b"".join(bytes(r) for r in pg)
                return self._json({"data": base64.b64encode(raw).decode(),
                                   "subs": 1, "links": {}})
            sel = (qs.get("sel") or [""])[0]
            if num == P_MUSIC:
                pg, subs_n, links = music_index_page()
            elif P_MUSIC_A <= num < P_MUSIC_A + 27:
                pg, subs_n, links = music_letter_page(num, sub)
            elif num == P_MUSIC_ALBUMS:
                pg, subs_n, links = music_albums_page(sel, sub)
            elif num == P_MUSIC_TRACKS:
                pg, subs_n, links = music_tracks_page(sel, sub)
            else:
                pg = False
            if pg is not False:
                if pg is None:
                    return self._json({"error": "no page"}, 404)
                raw = b"".join(bytes(r) for r in pg)
                return self._json({
                    "data": base64.b64encode(raw).decode(), "subs": subs_n,
                    "links": links if "items" in links else {"items": links}})
            if num == 666:
                pg, subs_n, meta = creep_page(sub)
                raw = b"".join(bytes(r) for r in pg)
                out = {"data": base64.b64encode(raw).decode(),
                       "subs": subs_n, "links": {"items": {}}}
                out.update(meta)
                return self._json(out)
            if num == P_RADIO:
                pg, subs_n, links = radio_page(sub)
                raw = b"".join(bytes(r) for r in pg)
                return self._json({"data": base64.b64encode(raw).decode(),
                                   "subs": subs_n,
                                   "links": {"items": links}})
            if num == P_TV:
                pg, subs_n, links = tv_page(sub)
                raw = b"".join(bytes(r) for r in pg)
                return self._json({"data": base64.b64encode(raw).decode(),
                                   "subs": subs_n,
                                   "links": {"items": links}})
            if P_NEWS <= num < P_NEWS + 100:
                pg, subs_n, links = news_page(num, sub)
                if pg is None:
                    return self._json({"error": "no page"}, 404)
                raw = b"".join(bytes(r) for r in pg)
                # links already has its final shape ({"items": ..., "qr": ...})
                return self._json({"data": base64.b64encode(raw).decode(),
                                   "subs": subs_n, "links": links})
            got = load_page(num, sub)
            if got is None:
                return self._json({"error": "no page"}, 404)
            raw, links = got
            raw = watch_paint(raw, links)
            spec = (links.get("items") or {}).get("__anim__")
            subs = page_subs(num)
            if os.path.exists(os.path.join(PAGES, "%03d.invented" % num)):
                subs += 1          # there is always one more to be invented
            out = {"data": base64.b64encode(raw).decode(), "subs": subs,
                   "links": links.get("items", {}) and links}
            if spec:
                out["anim"] = spec
            return self._json(out)
        if url.path == "/tt/desc":
            p = (qs.get("path") or [""])[0]
            if not p.startswith(MEDIA):
                return self._json({"error": "path"}, 403)
            return self._json(describe(p))
        if url.path.startswith("/pi/"):
            code, body = pi_proxy("GET", url.path[4:])
            return self._send(code, body, "application/json")
        if url.path == "/stream":
            return self._stream(qs)
        if url.path == "/music":
            return self._music(qs)
        if url.path == "/radio":
            key = (qs.get("ch") or [""])[0]
            target = radio_url(key)
            if not target:
                return self._send(404, "unknown station")
            return self._music(qs, target)
        return self._send(404, "unknown endpoint")

    # ------------------------------------------------------------- PUT/POST
    def do_PUT(self):
        m = re.match(r"^/tt/page/(\d{1,3})$", urllib.parse.urlparse(self.path).path)
        if not m:
            return self._send(404, "unknown endpoint")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        num = int(m.group(1))
        sub = int((qs.get("sub") or ["1"])[0])
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)[:960].ljust(960, b" ")
        rows = [bytearray(raw[i * 40:(i + 1) * 40]) for i in range(24)]
        save_page(num, sub, rows)
        with open(edited_path(num, sub), "w") as fh:
            fh.write(str(time.time()))   # from now on the generator keeps off
        return self._json({"saved": "%03d.%d" % (num, sub)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/pi/"):
            code, body = pi_proxy("POST", path[4:])
            return self._send(code, body, "application/json")
        if path == "/tt/watched":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                p = json.loads(self.rfile.read(length)).get("path", "")
            except (ValueError, KeyError):
                return self._send(400, "bad request")
            if not p.startswith(MEDIA) or ".." in p:
                return self._json({"error": "path"}, 403)
            n = watch_bump(p)
            print("watched: %s (now %d)" % (p, n), flush=True)
            return self._json({"path": p, "count": n})
        if path == "/tt/invent":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                d = json.loads(self.rfile.read(length))
                page, sub = int(d.get("page", 0)), int(d.get("sub", 1))
                context = str(d.get("context", ""))[:900]
            except (ValueError, TypeError):
                return self._send(400, "bad request")
            return self._json({"id": invent_start(page, sub, context)})
        if path == "/news":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                d = json.loads(self.rfile.read(length))
            except (ValueError, TypeError):
                return self._send(400, "bad request")
            return self._json({"id": news_start(str(d.get("mode", "top")), d)})
        if path == "/chat":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                d = json.loads(self.rfile.read(length))
                text = str(d.get("text", ""))[:800].strip()
            except (ValueError, TypeError):
                return self._send(400, "bad request")
            opener = bool(d.get("start"))
            if not text and not opener:
                return self._json({"error": "empty"}, 400)
            sid = str(d.get("session") or "")
            if opener or sid not in _crt_sessions:
                sid = crt_new_session()
            return self._json({"id": chat_start(text, d.get("history"),
                                                opener, sid),
                               "session": sid})
        if path == "/tt/scores":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                d = json.loads(self.rfile.read(length))
                game = re.sub(r"[^a-z0-9]", "", str(d.get("game", "snake")))
                name = re.sub(r"[^A-Za-z0-9 ]", "", str(d.get("name", "")))
                score = int(d.get("score", 0))
            except (ValueError, TypeError):
                return self._send(400, "bad request")
            return self._json({"scores": scores_add(game, name, score)})
        if path == "/tt/render":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length))
                raw = base64.b64decode(req["grid"])
                if len(raw) != 960:
                    raise ValueError("grid must be 960 bytes")
                page = [bytearray(raw[i * 40:(i + 1) * 40]) for i in range(24)]
                w, h = int(req.get("w", 1280)), int(req.get("h", 720))
                if not (0 < w <= 1920 and 0 < h <= 1200):
                    raise ValueError("size")
                mg = tuple(int(x) for x in (req.get("margins") or (0, 0, 0, 0)))
                fb = renderer().render(page, w, h, bool(req.get("mix")), mg)
            except (ValueError, KeyError, TypeError) as exc:
                return self._send(400, "bad request: %s" % exc)
            import zlib
            body = zlib.compress(bytes(fb), 1)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Raw-Length", str(len(fb)))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/tt/purge-invented":
            # Wipes only pages the set invented for itself, so they can be
            # discovered afresh. The 666 pages Ollama wrote are NOT touched:
            # they are part of that page's growing archive, not scratch.
            n = 0
            try:
                for f in sorted(os.listdir(PAGES)):
                    if not f.endswith(".invented"):
                        continue
                    num = f.split(".")[0]
                    for g2 in os.listdir(PAGES):
                        if g2.startswith(num + "."):
                            os.remove(os.path.join(PAGES, g2))
                    n += 1
            except OSError as exc:
                return self._json({"error": str(exc)}, 500)
            print("purged %d invented pages" % n, flush=True)
            return self._json({"purged": n})
        if path == "/tt/scan":
            return self._json({"started": scan_async(),
                               "running": _scan_state["running"]})
        if path == "/probe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                paths = json.loads(self.rfile.read(length)).get("paths", [])
            except (ValueError, KeyError):
                return self._send(400, "bad request")
            result = {}
            for p in paths:
                if isinstance(p, str) and p.startswith(MEDIA) and ".." not in p:
                    result[p] = probe_one(p)
            return self._json(result)
        return self._send(404, "unknown endpoint")

    # ------------------------------------------------------------- stream
    def _music(self, qs, stream=None):
        """A music track as a video stream: the analyser IS the picture.

        Rendering the bars here rather than on the Pi is not an optimisation,
        it is the only workable place. mpv composites overlays onto the video
        frame, and an audio-only file has no frame to composite onto -- and
        painting 1280x720 in Python costs the Pi a quarter of a second, so it
        could never animate anyway. ffmpeg does the FFT, the colouring and the
        encode in one pass, and the receiver just plays a video.
        """
        path = stream or (qs.get("path") or [""])[0]
        if stream is None and (not path.startswith(MUSIC_ROOT) or ".." in path):
            return self._send(403, "path not allowed")
        global _current
        with _lock:
            if _current is not None and _current.poll() is None:
                _current.kill()
                _current.wait()
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
                   "-filter_complex", MUSIC_VIS,
                   "-map", "[v]", "-map", "0:a:0",
                   "-c:v", "h264_nvenc", "-preset", "p4",
                   "-profile:v", "high", "-pix_fmt", "yuv420p",
                   "-b:v", "1500k", "-maxrate", "2500k", "-bufsize", "4M",
                   "-c:a", "aac", "-ac", "2", "-b:a", "192k",
                   "-f", "mpegts", "pipe:1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            _current = proc
        print("music start: %s" % path, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        print("music end: %s" % path, flush=True)

    def _stream(self, qs):
        path = (qs.get("path") or [""])[0]
        if not path.startswith(MEDIA) or ".." in path:
            return self._send(403, "path not allowed")
        try:
            start = max(0, int(float((qs.get("start") or ["0"])[0])))
        except ValueError:
            start = 0
        global _current
        with _lock:
            if _current is not None and _current.poll() is None:
                _current.kill()
                _current.wait()
            # -ss BEFORE -i: keyframe-fast input seeking, so a jump into
            # hour two starts encoding there instead of transcoding its way
            # through everything before it
            cmd = ["ffmpeg", "-nostdin", "-v", "error"] + \
                  (["-ss", str(start)] if start else []) + ["-i", path,
                   "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "scale=-2:min(720\\,ih)",
                   "-c:v", "h264_nvenc", "-preset", "p4",
                   "-profile:v", "high", "-pix_fmt", "yuv420p",
                   "-b:v", "4M", "-maxrate", "6M", "-bufsize", "8M",
                   "-c:a", "aac", "-ac", "2", "-b:a", "192k",
                   "-f", "mpegts", "pipe:1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            _current = proc
        print("stream start: %s" % path, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            print("client gone, killing ffmpeg", flush=True)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        print("stream end: %s" % path, flush=True)


# glyph ROM served to the editor so its preview is pixel-identical
_FONT_HEX = """
00 00 00 00 00 00 00   04 04 04 04 04 00 04   0A 0A 0A 00 00 00 00
0A 0A 1F 0A 1F 0A 0A   04 0F 14 0E 05 1E 04   18 19 02 04 08 13 03
0C 12 14 08 15 12 0D   04 04 08 00 00 00 00   02 04 08 08 08 04 02
08 04 02 02 02 04 08   00 04 15 0E 15 04 00   00 04 04 1F 04 04 00
00 00 00 00 00 04 08   00 00 00 1F 00 00 00   00 00 00 00 00 0C 0C
00 01 02 04 08 10 00   0E 11 13 15 19 11 0E   04 0C 04 04 04 04 0E
0E 11 01 02 04 08 1F   1F 02 04 02 01 11 0E   02 06 0A 12 1F 02 02
1F 10 1E 01 01 11 0E   06 08 10 1E 11 11 0E   1F 01 02 04 08 08 08
0E 11 11 0E 11 11 0E   0E 11 11 0F 01 02 0C   00 0C 0C 00 0C 0C 00
00 0C 0C 00 0C 04 08   02 04 08 10 08 04 02   00 00 1F 00 1F 00 00
08 04 02 01 02 04 08   0E 11 01 02 04 00 04   0E 11 17 15 17 10 0F
0E 11 11 1F 11 11 11   1E 11 11 1E 11 11 1E   0E 11 10 10 10 11 0E
1E 11 11 11 11 11 1E   1F 10 10 1E 10 10 1F   1F 10 10 1E 10 10 10
0E 11 10 13 11 11 0F   11 11 11 1F 11 11 11   0E 04 04 04 04 04 0E
01 01 01 01 01 11 0E   11 12 14 18 14 12 11   10 10 10 10 10 10 1F
11 1B 15 15 11 11 11   11 11 19 15 13 11 11   0E 11 11 11 11 11 0E
1E 11 11 1E 10 10 10   0E 11 11 11 15 12 0D   1E 11 11 1E 14 12 11
0F 10 10 0E 01 01 1E   1F 04 04 04 04 04 04   11 11 11 11 11 11 0E
11 11 11 11 11 0A 04   11 11 11 15 15 15 0A   11 11 0A 04 0A 11 11
11 11 0A 04 04 04 04   1F 01 02 04 08 10 1F   0A 00 0E 11 1F 11 11
0A 00 0E 11 11 11 0E   0A 00 11 11 11 11 0E   04 0A 11 00 00 00 00
00 00 00 00 00 00 1F   06 09 09 06 00 00 00   00 00 0E 01 0F 11 0F
10 10 1E 11 11 11 1E   00 00 0F 10 10 10 0F   01 01 0F 11 11 11 0F
00 00 0E 11 1F 10 0E   06 09 08 1C 08 08 08   00 0F 11 11 0F 01 0E
10 10 1E 11 11 11 11   04 00 0C 04 04 04 0E   02 00 06 02 02 12 0C
10 10 12 14 18 14 12   0C 04 04 04 04 04 0E   00 00 1A 15 15 15 15
00 00 1E 11 11 11 11   00 00 0E 11 11 11 0E   00 00 1E 11 1E 10 10
00 00 0F 11 0F 01 01   00 00 16 19 10 10 10   00 00 0F 10 0E 01 1E
08 08 1C 08 08 09 06   00 00 11 11 11 13 0D   00 00 11 11 11 0A 04
00 00 11 11 15 15 0A   00 00 11 0A 04 0A 11   00 00 11 11 0F 01 0E
00 00 1F 02 04 08 1F   0A 00 0E 01 0F 11 0F   0A 00 0E 11 11 11 0E
0A 00 11 11 11 13 0D   0C 12 14 16 11 11 16   1F 1F 1F 1F 1F 1F 1F
"""

_ICON_CACHE = None


def make_icon():
    """64x64 PNG of a mini teletext page, drawn from hardcoded glyphs and
    encoded by hand (zlib + struct) -- the broadcaster serves its own icon,
    no external dependency. Binary constants built via bytes([...]) on
    purpose: backslash escapes have been eaten twice by tooling layers."""
    global _ICON_CACHE
    if _ICON_CACHE:
        return _ICON_CACHE
    import struct, zlib
    Wd = Hd = 64
    px = bytearray(Wd * Hd * 3)

    def rect(x, y, w, h, rgb):
        for yy in range(y, min(Hd, y + h)):
            row = (yy * Wd + x) * 3
            for xx in range(min(w, Wd - x)):
                px[row + xx * 3:row + xx * 3 + 3] = bytes(rgb)

    bars = [(255, 0, 0), (255, 255, 0), (0, 255, 0),
            (0, 255, 255), (0, 0, 255), (255, 0, 255)]
    for i, c in enumerate(bars):
        rect(2 + i * 10, 44, 9, 12, c)
    font = {"T": [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
            "C": [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E]}
    for i, ch in enumerate("TC"):
        for r in range(7):
            for c in range(5):
                if font[ch][r] & (0x10 >> c):
                    rect(6 + i * 28 + c * 5, 6 + r * 4, 5, 4, (255, 255, 0))

    filt = bytes([0])
    body = b"".join(filt + bytes(px[y * Wd * 3:(y + 1) * Wd * 3])
                    for y in range(Hd))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data)))

    sig = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])
    _ICON_CACHE = (sig +
                   chunk(b"IHDR", struct.pack(">IIBBBBB", Wd, Hd, 8, 2, 0, 0, 0)) +
                   chunk(b"IDAT", zlib.compress(body)) +
                   chunk(b"IEND", b""))
    return _ICON_CACHE


if __name__ == "__main__":
    os.makedirs(PAGES, exist_ok=True)
    if not os.path.exists(os.path.join(PAGES, ".last-scan")):
        print("first boot: generating pages", flush=True)
        scan_async()
    threading.Thread(target=scheduler, daemon=True).start()
    print("Telecommander on :%d (transcode + teletext broadcast)" % PORT,
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
