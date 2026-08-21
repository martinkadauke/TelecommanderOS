"""
TelecommanderOS receiver -- a software SAA5050 that is the Saba's OS.

Architecture is broadcast-authentic: the PAGES (0-999) live on the
"broadcaster" -- the Telecommander container on Kadaukeserver, which scans
the media library, generates list pages, stores hand-edited pages from its
web editor, and serves them all over HTTP. This module is the RECEIVER: it
fetches page bytes, renders them, and turns numpad input into page numbers.
The TV set never knows what a movie is; it renders what the broadcaster
transmits, exactly like 1978.

A page is a 40x24 grid of BYTES: printable bytes are glyphs, 0x00-0x1F are
SPACING ATTRIBUTES that change colour/mode from that cell on and occupy a
visible cell -- the famous teletext gap before coloured text. Mosaics are
characters: six low bits switch six 2x3 sub-blocks. Eight colours, one bit
per channel.

The renderer emulates the Mullard SAA5050 character generator: a 5x7 glyph
ROM (hand-encoded, German national option: [ \\ ] { | } ~ are AE OE UE ae
oe ue ss), stateful per-row attribute decoding, algorithmic mosaics. Output
is a BGRA framebuffer handed to mpv as an overlay -- text mixed over the
picture, the modern form of the original signal mix.

Idle cost is zero by construction: nothing runs between keypresses except a
once-a-minute clock repaint while visible.

Local dynamic pages (need live player state, so they cannot come from the
broadcaster): 111 now-playing, 500 settings. Everything else -- including
the index -- is broadcast, and therefore editable in the web editor.
"""

from __future__ import annotations

import base64
import json
import math
import random
import os
import re
import struct
import subprocess
import time
import wave
import zlib
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# colours (index 0-7, one bit per channel: the broadcast palette)
# ---------------------------------------------------------------------------
PALETTE = [
    (0, 0, 0), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

A_DHEIGHT, A_NHEIGHT, A_BLACKBG, A_NEWBG = 0x0D, 0x0C, 0x1C, 0x1D


def alpha(c): return bytes([c])
def mosaic(c): return bytes([0x10 + c])


NEWBG, BLACKBG, DH = bytes([A_NEWBG]), bytes([A_BLACKBG]), bytes([A_DHEIGHT])

# ---------------------------------------------------------------------------
# glyph ROM: 96 glyphs (0x20-0x7F), 7 rows each, 5 bits per row (bit4 left).
# German national option in the G0 positions, as ARD/ZDF text used it.
# ---------------------------------------------------------------------------
_G = """
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
FONT = bytes(int(b, 16) for b in _G.split())
assert len(FONT) == 96 * 7, len(FONT)

_DE = str.maketrans({"Ä": "[", "Ö": "\\", "Ü": "]",
                     "ä": "{", "ö": "|", "ü": "}", "ß": "~", "°": "`"})


def T(s: str) -> bytes:
    out = s.translate(_DE)
    return bytes(c if 0x20 <= c < 0x80 else 0x3F
                 for c in out.encode("ascii", "replace"))


# ---------------------------------------------------------------------------
# the chip
# ---------------------------------------------------------------------------
class SAA5050:
    def __init__(self):
        self._cache = {}
        self.mix = False

    def _cell_band(self, ch, fg, bg, mos, cw, band, bands):
        key = (ch, fg, bg, mos, cw, band, bands, self.mix)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        fgpx = bytes((PALETTE[fg][2], PALETTE[fg][1], PALETTE[fg][0], 255))
        bgpx = bytes((PALETTE[bg][2], PALETTE[bg][1], PALETTE[bg][0],
                      0 if bg == BLACK and self.mix else 255))
        if mos and (0x20 <= ch < 0x40 or 0x60 <= ch < 0x80):
            bits = (ch & 0x1F) | ((ch & 0x40) >> 1)
            third = bands // 3 or 1
            r = min(band // third, 2)
            left = bits & (1 << (r * 2))
            right = bits & (2 << (r * 2))
            half = cw // 2
            row = (fgpx if left else bgpx) * half + \
                  (fgpx if right else bgpx) * (cw - half)
        else:
            gi = (band * 9) // bands - 1
            if ch < 0x20 or not (0 <= gi < 7):
                bits5 = 0
            else:
                bits5 = FONT[(ch - 0x20) * 7 + gi]
            row = bytearray()
            for x in range(cw):
                lx = (x * 6) // cw
                on = lx < 5 and (bits5 >> (4 - lx)) & 1
                row += fgpx if on else bgpx
            row = bytes(row)
        self._cache[key] = row
        return row

    def render(self, page: list, W: int, H: int, mix: bool,
               margins=(0, 0, 0, 0)) -> bytes:
        """margins: CRT overscan safe area in px per edge (top, bottom,
        left, right) -- tubes hide the raster's outer edge, and rarely
        symmetrically, so each edge gets its own inset. Every real teletext
        service did the same against every real tube."""
        if mix != self.mix:
            self._cache.clear()
        self.mix = mix
        mt, mb, ml, mr = margins
        cw, chh = (W - ml - mr) // 40, (H - mt - mb) // 24
        padpx = b"\x00\x00\x00" + (b"\x00" if mix else b"\xff")
        lpad = ml + ((W - ml - mr) - cw * 40) // 2
        padl, padr = padpx * lpad, padpx * (W - cw * 40 - lpad)
        out, y, skip = [], 0, False
        blankline = padpx * W
        for _ in range(mt + ((H - mt - mb) - 24 * chh) // 2):
            out.append(blankline)
            y += 1
        for r in range(24):
            if skip:
                skip = False
                continue
            row = page[r]
            dh = A_DHEIGHT in row
            rows_h = chh * (2 if dh else 1)
            if dh:
                skip = True
            fg, bg, mos, dbl = WHITE, BLACK, False, False
            cells = []
            for ch in row:
                if ch < 0x20:
                    if 0x00 <= ch <= 0x07:
                        # 0x00 is reserved on a real SAA5050 -- Level 1 has no
                        # alpha black, which is why black-on-white was
                        # impossible in 1983. We allow it: the byte already
                        # rendered as a blank spacing cell, so this only adds
                        # the colour, and it is the one thing a highlight bar
                        # genuinely needs.
                        fg, mos = ch, False
                    elif 0x11 <= ch <= 0x17:
                        fg, mos = ch - 0x10, True
                    elif ch == A_DHEIGHT:
                        dbl = True
                    elif ch == A_NHEIGHT:
                        dbl = False
                    elif ch == A_NEWBG:
                        bg = fg
                    elif ch == A_BLACKBG:
                        bg = BLACK
                    cells.append((0x20, fg, bg, False, dbl))
                else:
                    cells.append((ch, fg, bg, mos, dbl))
            for band in range(rows_h):
                line = [padl]
                for (ch, cfg, cbg, cmos, cdbl) in cells:
                    if dh and not cdbl and band >= chh:
                        line.append(self._cell_band(0x20, cfg, cbg, False,
                                                    cw, 0, chh))
                    else:
                        b, nb = (band, rows_h) if (dh and cdbl) \
                            else (band % chh, chh)
                        line.append(self._cell_band(ch, cfg, cbg, cmos,
                                                    cw, b, nb))
                line.append(padr)
                out.append(b"".join(line))
                y += 1
        while y < H:
            out.append(blankline)
            y += 1
        return b"".join(out[:H])


# ---------------------------------------------------------------------------
# authoring helpers (for the receiver's local dynamic pages)
# ---------------------------------------------------------------------------
def blank_page() -> list:
    return [bytearray(b"\x20" * 40) for _ in range(24)]


def put(page, r, c, data: bytes):
    page[r][c:c + len(data)] = data[:40 - c]


def header(page, num, sub=None):
    put(page, 0, 0, alpha(WHITE) + T("%03d" % num) +
        (T(".%d" % sub) if sub else b""))
    put(page, 0, 8, alpha(CYAN) + T("TELECOMMANDER"))
    clock = time.strftime("%d.%m. %H:%M")
    put(page, 0, 40 - len(clock) - 1, alpha(YELLOW) + T(clock))


# The station name and clock decay the further into 666 you go. Seeded by the
# SUBPAGE NUMBER, not by which page happens to be in that slot, so the decay
# is a property of how deep you have gone -- it advances as you keep pressing
# and never resets just because the hourly shuffle moved the pages around.
CREEP_NAMES = [
    "TELECOMMANDEP", "TELECOMMANOER", "TELECOMMANDE?", "TELE OMMANDER",
    "TELECOM ANDER", "TFLECOMMANDER", "TELECOMMANDER",
]
CREEP_WORSE = [
    "TELE OMM NDER", "TE_ECOMMAN_ER", "T LEC MM NDE ", "TELEKOMMANDER",
    "TELECOMMA DER", "TELE.OMMANDER", "TELECOM_ANDER",
]
CREEP_VOICE = [
    "ICH SEHE DICH", "HINTER IHNEN", "NICHT UMDREHEN", "SIE ATMEN NOCH",
    "WIR ZAEHLEN MIT", "BLEIBEN SIE SITZEN", "ES IST IM FLUR",
    "DIE WAND ATMET", "UNTER DEM BODEN", "ES KENNT DEN NAMEN",
    "NOCH EINE SEITE", "DAS ZIMMER IST VOLL", "SIE SIND ZU ZWEIT",
    "ES SASS HIER", "KEIN AUSGANG", "WIR SEHEN MIT",
    "ES HAT SICH GESETZT", "DEINE STIMME", "ES WAR IMMER HIER",
    "SCHAU NICHT WEG",
]
CREEP_CLOCK = ["%d.%m. %H:%M", "%d.%m. %H:%M", "66.66. 66:66",
               "31.02. 03:33", "--.--. --:--", "00.00. 00:00",
               "%d.%m. 03:33", "13.13. 13:13"]


def creep_header(page, num, sub):
    """The 666 header, decayed by depth."""
    rnd = random.Random(sub * 7919)
    if sub <= 4:                       # still a television station
        return header(page, num, sub)
    put(page, 0, 0, alpha(WHITE) + T("%03d.%d" % (num, sub)))
    if sub < 15:
        name, col = rnd.choice(CREEP_NAMES), CYAN
    elif sub < 40:
        name, col = rnd.choice(CREEP_WORSE), CYAN
    elif sub < 100:
        name = rnd.choice(CREEP_WORSE + CREEP_VOICE[:8])
        col = CYAN if rnd.random() < 0.5 else RED
    else:
        name, col = rnd.choice(CREEP_VOICE), RED
    put(page, 0, 8, alpha(col) + T(name[:22]))
    fmt = CREEP_CLOCK[0] if sub < 15 else rnd.choice(CREEP_CLOCK)
    clock = time.strftime(fmt) if "%" in fmt else fmt
    put(page, 0, max(0, 40 - len(clock) - 1),
        alpha(YELLOW if sub < 40 else RED) + T(clock))
    if sub >= 60:                      # the row itself starts to break up
        for _ in range(min(6, sub // 60)):
            page[0][rnd.randint(1, 38)] = rnd.choice((0x7F, 0x2A, 0x5F, 0x6B))
    return page


def hint_row(page, num=100):
    """The standing key legend. Only page 100 carries it: everywhere else the
    bottom row is better spent on what THAT page can do, and the legend just
    competed with it."""
    if num != 100:
        return page
    put(page, 23, 0, alpha(CYAN) + T("C=TV") +
        b" " + alpha(WHITE) + T(".=Bl{ttern 100=Start 200=Filme".replace(
            "{", chr(0xE4))))


def clean_title(path: str) -> str:
    import re
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"[.\s_-]*(1080p|720p|2160p|480p|WEB-?DL|WEBRip|HDTV|BluRay"
                  r"|BRRip|DVDRip|x264|x265|h264|HEVC|AAC|AC3|DTS|Premiere"
                  r"|REMASTERED|EXTENDED|\[.*?\]|\(\d{4}\)).*", "", name,
                  flags=re.I)
    return (name.replace(".", " ").replace("_", " ").strip(" -")
            or os.path.basename(path))


# ---------------------------------------------------------------------------
# the screensaver: the SABA wordmark drifting DVD-logo style over the dead
# channel. Runs ONLY in true idle (boot, or film quit and OS dismissed) --
# any key, any playback, any page kills it. Rendered from the same glyph ROM
# as everything else; changes to another broadcast colour on every wall hit.
# ---------------------------------------------------------------------------
import threading


class Screensaver(threading.Thread):
    W, H = 1280, 720
    SCALE = 12                     # 5x7 glyphs -> 60x84 letters
    FPS = 12.0
    SPEED = 6                      # px per frame

    def __init__(self, tv_cmd, shm="/dev/shm/retrokb-saver.bgra",
                 overlay_id=2):
        super().__init__(daemon=True, name="saver")
        self.tv_cmd = tv_cmd
        self.shm = shm
        self.overlay_id = overlay_id
        self._on = threading.Event()
        self._variants = None
        self.start()

    # -- logo rendering ----------------------------------------------------

    def _build(self):
        text = "SABA"
        sc, gap = self.SCALE, self.SCALE
        lw = len(text) * (5 * sc + gap) - gap
        lh = 7 * sc
        self.lw, self.lh = lw, lh
        colours = [(255, 255, 255), (255, 255, 0), (0, 255, 255),
                   (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255)]
        self._variants = []
        for (r, g, b) in colours:
            px = bytes((b, g, r, 255))
            off = bytes(4)
            fb = bytearray(lw * lh * 4)
            x0 = 0
            for ch in text:
                glyph = FONT[(ord(ch) - 0x20) * 7:(ord(ch) - 0x20) * 7 + 7]
                for gy in range(7):
                    bits = glyph[gy]
                    for gx in range(5):
                        cell = px if bits & (0x10 >> gx) else off
                        for yy in range(gy * sc, (gy + 1) * sc):
                            row = (yy * lw + x0 + gx * sc) * 4
                            fb[row:row + 4 * sc] = cell * sc
                x0 += 5 * sc + gap
            self._variants.append(bytes(fb))

    # -- control -----------------------------------------------------------

    def activate(self):
        self._on.set()

    def deactivate(self):
        if self._on.is_set():
            self._on.clear()
            self.tv_cmd(["overlay-remove", self.overlay_id])

    # -- the drift ---------------------------------------------------------

    def run(self):
        import random as _rnd
        x, y, dx, dy, ci = 120, 90, self.SPEED, self.SPEED, 0
        while True:
            self._on.wait()
            if self._variants is None:
                self._build()
                with open(self.shm, "wb") as fh:
                    fh.write(self._variants[0])
            while self._on.is_set():
                x += dx
                y += dy
                hit = False
                if x <= 0 or x + self.lw >= self.W:
                    dx, hit = -dx, True
                    x = max(0, min(x, self.W - self.lw))
                if y <= 0 or y + self.lh >= self.H:
                    dy, hit = -dy, True
                    y = max(0, min(y, self.H - self.lh))
                if hit:
                    ci = (ci + 1 + _rnd.randrange(len(self._variants) - 1)) \
                        % len(self._variants)
                    with open(self.shm, "wb") as fh:
                        fh.write(self._variants[ci])
                self.tv_cmd(["overlay-add", self.overlay_id, int(x), int(y),
                             self.shm, 0, "bgra", self.lw, self.lh,
                             self.lw * 4])
                time.sleep(1.0 / self.FPS)


# ---------------------------------------------------------------------------
# the receiver
# ---------------------------------------------------------------------------
P_NOW, P_DESC, P_SET, P_TV = 111, 112, 500, 800
P_MUSIC = 600
P_RADIO = 900
P_CHAT = 950                  # the television, talked to
P_RPG = 410                   # the television, played with
P_NEWS_AI = 710               # ChatCRT News, all five levels on one page
P_CREEP = 666
# (scheme, language). `.` walks this list -- Martin asked for one key that
# switches both multitap/predictive AND German/English.
CHAT_MODES = [("ABC", "de"), ("T9", "de"), ("ABC", "en"), ("T9", "en")]
PUNCT = ".,?!-"            # on 7: the phone's punctuation key by position
VOL_OVERLAY = 3                # 1 = the pages, 2 = the screensaver logo
# --- 1337 ------------------------------------------------------------------
# Snake, drawn with the character generator like everything else. The field is
# whole CHARACTER CELLS, not mosaic sextants: a teletext cell is 2x3 sub-blocks
# whose sub-pixels are nowhere near square, so a sextant-fine snake would move
# in visibly different-sized jumps horizontally and vertically.
SNAKE_TOP, SNAKE_BOT, SNAKE_LEFT, SNAKE_RIGHT = 4, 21, 1, 38
SNAKE_BLOCK = 0x7F             # all six sextants lit
SNAKE_STEP = 0.20              # seconds per move; a page repaint costs ~90 ms
# Multitap, the way a phone did it before predictive text. Deliberately NOT
# predictive for initials: three initials are not a word, so a dictionary has
# nothing to offer and would only get in the way.
#
# ⚠️ THE LETTERS FOLLOW THE PHYSICAL KEY, NOT THE DIGIT. A numpad is a phone
# keypad turned upside down --
#       phone   1 2 3      numpad   7 8 9
#               4 5 6               4 5 6
#               7 8 9               1 2 3
# -- so the top-left key must carry what a phone's top-left key carried. Going
# by digit instead would put ABC on the BOTTOM row and make the muscle memory
# of every person who ever texted on a Nokia useless.
MULTITAP = {"8": "ABC", "9": "DEF", "4": "GHI", "5": "JKL",
            "6": "MNO", "1": "PQRS", "2": "TUV", "3": "WXYZ"}
MT_TIMEOUT = 0.9               # same key within this = cycle, after = new letter
VOL_SHM = "/dev/shm/retrokb-vol.bgra"
MUSIC_EXTS = (".mp3", ".m4a", ".flac", ".ogg", ".oga", ".wav", ".wma", ".mp2")


def is_music(path: str) -> bool:
    return path.lower().endswith(MUSIC_EXTS)


def clean_track(name: str) -> str:
    n = os.path.splitext(os.path.basename(name))[0]
    n = re.sub(r"^\s*\d{1,3}\s*[-._)]?\s+", "", n)
    return n.strip() or name
CARRIER = "av://lavfi:color=c=black:s=1280x720:r=12"


class Teletext:
    def __init__(self, cfg, tv_cmd, tv_query, log, launch_game=None,
                 tv_power=None):
        t = cfg.get("teletext", {})
        self.enabled = bool(t.get("enabled", True))
        self.shm = t.get("shm", "/dev/shm/retrokb-tt.bgra")
        self.overlay_id = int(t.get("overlay_id", 1))
        self.broadcaster = t.get("broadcaster", "http://192.168.1.238:8203")
        self.state_file = t.get("state_file", "/var/lib/tvplayer/os-state.json")
        self.playlist_path = t.get("playlist", "/run/tvplayer/playlist.m3u8")
        self.probe_cache = t.get("probe_cache",
                                 "/var/lib/tvplayer/probecache.json")
        self.remote_roots = {"/mnt/user/Movies": "/mnt/movies",
                             "/mnt/user/TV Shows": "/mnt/tv"}
        # CRT overscan safe area, percent per edge. The Saba's raster sits
        # shifted up-left, so top and right need more than their partners.
        v = float(t.get("overscan_v_pct", 6.0))
        h = float(t.get("overscan_h_pct", 2.0))
        self.overscan = (float(t.get("overscan_top_pct", v)),
                         float(t.get("overscan_bottom_pct", v)),
                         float(t.get("overscan_left_pct", h)),
                         float(t.get("overscan_right_pct", h)))
        self.chip = SAA5050()
        self.tv_cmd, self.tv_query, self.log = tv_cmd, tv_query, log
        self.launch_game = launch_game
        self.tv_power = tv_power
        self.saver = Screensaver(tv_cmd)
        self._boot_carrier_seen = False
        self.visible = False
        self.mixmode = False
        self.page, self.sub = 100, 1
        self.entry = ""
        self.err = None
        self._hist = []
        # digits on 111 are page numbers until ENTER arms the jump field
        self.seek_armed = False
        self._desc = (None, None)      # (remote path, dict) one-entry cache
        self._saver_check_at = 0.0     # periodic re-assertion of the logo
        self._set_msg = ""             # settings page result line
        self._loading = None           # (title, deadline) while a file starts
        self.qr_on = False             # a QR card is covering the page
        self._rand_ask = False         # the "play something random?" card
        self._remote_paint = 0.0       # backoff after a failed remote paint
        self.sel = ""                  # browser selection carried to the page
        self.selkey = ""               # highlighted item on this page
        self.music = None              # the queued album, if music is playing
        self.music_base = 0            # its index of mpv playlist position 0
        self.music_secs = []           # length of each queued track
        self._vol_until = 0.0          # when the volume meter comes off again
        self.snake = None              # the easter egg, while it is running
        self.photo = None              # a picture page, while one is up
        self._photos = (0.0, {})       # (expiry, digit-code -> picture)
        self.chat = None               # the assistant, while its page is open
        self._hum = None               # the 666 drone, while that page is up
        self._hum_building = False     # a variant being rendered offline
        self._hum_kind = "drone"       # drone, or a page's own sound
        self._bt_seen = []             # devices the last scan found
        self._bt_busy = False          # a scan running
        self._bt_sel = ""              # highlighted device
        self._bt_msg = ""              # what pairing is doing
        self._hum_sub = 0              # last 666 subpage seen
        self._hum_due = []             # seconds into the drone where it glitches
        self._hum_at = 0.0             # when the current drone started
        self.flash = None              # set by the router: flash the room
        self._hum_pages = 0            # pages turned since a glitch
        self._hum_thresh = 3           # pages until the next one
        self.invent = None             # a page being made up on the spot
        self.news = None               # the news reader, while it is open
        self._anim = [0.0, 0.0]        # [phase 0..1, next frame due]
        self._glitch_at = 0.0          # next 666 interference noise
        self.live_page = P_TV          # which list the tuned station came from
        self.snd_dev = str(t.get("sound_device",
                                 "plughw:CARD=vc4hdmi,DEV=0"))
        # Effects go out at full HDMI level -- there is no mixer between us
        # and the TV -- so they need their own trim, and it has to be
        # adjustable without touching code.
        self.snd_gain = max(0.0, min(1.0, float(t.get("sound_gain", 0.15))))
        self._reboot_armed = False     # destructive items need two presses
        self._tvoff_armed = False
        # where the currently playing file came from: (page, sub, item-no).
        # This is what makes next/previous mean "next/previous IN THE LIST
        # YOU CHOSE FROM" (e.g. the next episode) instead of mpv's playlist,
        # which holds only one file outside random mode.
        self.play_ctx = None
        self.last_remote = None
        self.live = None               # url of the live stream on air, if any
        self.live_name = ""            # ...and the channel name to show
        self.live_epg = ""             # ARD channel id, if it has a guide
        self.live_key = ""             # broadcaster channel key, for zapping
        self._epg = (0.0, None, None)  # (expiry, now, next)
        self._np = (0.0, "", "")       # (expiry, station, ICY title)
        # seconds already skipped server-side on a transcode pipe (the
        # gateway's start= parameter); mpv's own clock restarts at 0 there,
        # so displays add this back.
        self.pipe_offset = 0
        # (path, position) of a film suspended by opening the OS
        self._resume = None
        self._clock_at = 0.0
        self._meta = {"subs": 1, "links": {}}
        self._pcache = {}          # (page, sub) -> (expiry, rows, meta)
        self._list_cache = (0.0, [])   # (expiry, sorted existing pages)
        self._needs_tc = None      # lazy probe-cache verdicts
        self.random = False
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                self.random = bool(json.load(fh).get("random"))
        except (OSError, ValueError):
            pass

    # -- broadcast fetch ---------------------------------------------------

    def _fetch(self, page: int, sub: int):
        """Page from the broadcaster: (rows, meta) or None."""
        key = (page, sub, self.sel)
        hit = self._pcache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1], hit[2]
        url = "%s/tt/page/%d?sub=%d" % (self.broadcaster, page, sub)
        if self.sel:
            # the selection is part of the address, not hidden state on the
            # broadcaster -- and part of the cache key, or two artists would
            # share one cached page
            url += "&sel=" + urllib.parse.quote(self.sel, safe="")
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                obj = json.load(r)
        except (OSError, ValueError):
            return None
        if "data" not in obj:
            return None
        import base64
        raw = base64.b64decode(obj["data"])
        rows = [bytearray(raw[i * 40:(i + 1) * 40].ljust(40, b" "))
                for i in range(24)]
        meta = {"subs": int(obj.get("subs", 1)),
                "links": obj.get("links") or {},
                "qr": (obj.get("links") or {}).get("qr", ""),
                # a page may ask to be animated; without carrying this through
                # the engine never sees a spec and every page stays still
                "anim": obj.get("anim"),
                "mode": obj.get("mode")}
        self._pcache[key] = (time.monotonic() + 30, rows, meta)
        if len(self._pcache) > 16:
            self._pcache.pop(next(iter(self._pcache)))
        return rows, meta

    # -- playing what the broadcaster linked -------------------------------

    def _page_list(self):
        """Every page that exists: broadcaster inventory + local dynamic
        pages. Cached; falls back to just the locals when off-air."""
        exp, pages = self._list_cache
        if exp > time.monotonic():
            return pages
        found = {P_NOW, P_SET}
        try:
            with urllib.request.urlopen(self.broadcaster + "/tt/list",
                                        timeout=3) as r:
                for k in json.load(r).get("pages", {}):
                    found.add(int(k))
        except (OSError, ValueError):
            pass
        pages = sorted(found)
        self._list_cache = (time.monotonic() + 60, pages)
        return pages

    def page_step(self, step: int):
        """+/- flip through EXISTING pages (100 -> 101 -> 111 ...), like a
        remote's P+/P- but skipping the empty numbers, wrapping around."""
        pages = self._page_list()
        if not pages:
            return
        import bisect
        if step > 0:
            i = bisect.bisect_right(pages, self.page)
            nxt = pages[i % len(pages)]
        else:
            i = bisect.bisect_left(pages, self.page) - 1
            nxt = pages[i % len(pages)]
        self._go(nxt)
        self.repaint()

    def _playables(self):
        """Every playable entry on the CURRENT page, across all its subpages.

        Lists spill over subpages, so a random pick that only looked at the
        subpage on screen would keep landing in the same 16 films. Live
        channels and ROMs are excluded -- "play something random" means a
        film, an episode or a track, never a TV channel or a game.
        """
        out = []
        subs = max(1, int((self._meta or {}).get("subs", 1)))
        for sub in range(1, subs + 1):
            got = self._fetch(self.page, sub)
            if not got:
                continue
            items = ((got[1] or {}).get("links") or {}).get("items") or {}
            for k, v in items.items():
                if "play" in v and not v.get("live") and "rom" not in v:
                    out.append((sub, k, v))
        return out

    def _page_random_ask(self):
        pg = blank_page()
        header(pg, self.page, None)
        put(pg, 4, 2, alpha(YELLOW) + DH + T("ZUFALL"))
        put(pg, 8, 2, alpha(WHITE) +
            T("Etwas zuf{lliges abspielen?".replace("{", chr(0xE4))))
        put(pg, 11, 4, alpha(GREEN) + T("Enter") + alpha(WHITE) + T(" = ja"))
        put(pg, 12, 4, alpha(RED) + T("C    ") + alpha(WHITE) + T(" = nein"))
        return pg

    def _play_random(self):
        import random as _rnd
        picks = self._playables()
        if not picks:
            self.err = "nichts da"
            self.repaint()
            return
        sub, sel, tgt = _rnd.choice(picks)
        if is_music(tgt["play"]):
            self._play_album(tgt["play"])
        else:
            self._play(tgt["play"], (self.page, sub, int(sel)))

    def channels(self):
        """The channel list as the broadcaster publishes it on page 800."""
        got = self._fetch(self.live_page, 1)
        if not got:
            return []
        items = {}
        subs = max(1, int((got[1] or {}).get("subs", 1)))
        for sub in range(1, subs + 1):
            page = got if sub == 1 else self._fetch(self.live_page, sub)
            if page:
                items.update(((page[1] or {}).get("links") or {}).get("items") or {})
        try:
            return [items[k] for k in sorted(items, key=int)]
        except (ValueError, TypeError):
            return list(items.values())

    def channel_step(self, delta: int):
        """Zap to the next/previous channel, wrapping round the list. Tuning
        is a fresh loadfile either way -- there is no position to preserve on
        a live stream, so this is simply "tune that one instead"."""
        chans = self.channels()
        if not chans:
            return
        idx = next((i for i, c in enumerate(chans)
                    if (c.get("ch") and c.get("ch") == self.live_key)
                    or c.get("play") == self.live), None)
        nxt = chans[(idx + delta) % len(chans)] if idx is not None else chans[0]
        self.play_live(self._live_target(nxt), nxt.get("name", ""),
                       nxt.get("epg", ""), nxt.get("ch", ""), self.live_page)

    def nowplaying(self):
        """ICY StreamTitle for the tuned radio station, refreshed slowly."""
        if not self.live_key:
            return ""
        if self._np[0] > time.monotonic() and self._np[1] == self.live_key:
            return self._np[2]
        title = ""
        try:
            with urllib.request.urlopen(
                    "%s/tt/nowplaying?ch=%s"
                    % (self.broadcaster,
                       urllib.parse.quote(self.live_key, safe="")),
                    timeout=6) as r:
                title = json.load(r).get("title") or ""
        except (OSError, ValueError):
            pass
        self._np = (time.monotonic() + 20, self.live_key, title)
        return title

    def epg(self):
        """(current, next) for the tuned channel, refreshed every 2 min."""
        if not self.live_epg:
            return None, None
        if self._epg[0] > time.monotonic():
            return self._epg[1], self._epg[2]
        cur = nxt = None
        try:
            with urllib.request.urlopen(
                    "%s/tt/epg?ch=%s" % (self.broadcaster,
                                         urllib.parse.quote(self.live_epg)),
                    timeout=8) as r:
                d = json.load(r)
            cur, nxt = d.get("now"), d.get("next")
        except (OSError, ValueError):
            pass
        self._epg = (time.monotonic() + 120, cur, nxt)
        return cur, nxt

    @staticmethod
    def _clock(iso: str) -> str:
        try:
            import datetime as dt
            return dt.datetime.fromisoformat(iso).strftime("%H:%M")
        except (ValueError, TypeError):
            return "--:--"

    def _music_url(self, path: str) -> str:
        return "%s/music?path=%s" % (self.broadcaster,
                                     urllib.parse.quote(path, safe=""))

    @staticmethod
    def _music_meta(path: str):
        """(artist, album, title) straight out of Artist/Album/NN Title."""
        parts = path.replace("\\", "/").rstrip("/").split("/")
        title = clean_track(parts[-1]) if parts else ""
        album = parts[-2] if len(parts) > 1 else ""
        artist = parts[-3] if len(parts) > 2 else ""
        if artist.lower() == "music":      # artist with no album folders
            artist, album = album, ""
        return artist, album, title

    def _play_album(self, path: str):
        """Play from this track to the end of the album.

        Music deliberately does NOT go through _play(): there is no watch
        count to keep (nobody "finishes" a song the way they finish a film),
        no probe verdict to consult, and above all the OS must not suspend it
        -- the analyser IS the picture here, so the pages sit on top of it in
        MIX instead of replacing it with the black carrier.
        """
        pl = ((self._meta or {}).get("links") or {}).get("playlist") or []
        try:
            index = pl.index(path)
        except ValueError:
            pl, index = [path], 0
        self.saver.deactivate()
        self._saver_check_at = time.monotonic() + 1.5
        self.live = None
        self.live_name = self.live_key = self.live_epg = ""
        self._resume = None
        self.play_ctx = None
        self.last_remote = None
        self.pipe_offset = 0
        self.music, self.music_base = list(pl), 0
        self.music_secs = list(
            ((self._meta or {}).get("links") or {}).get("seconds") or [])
        urls = [self._music_url(p) for p in pl]
        self.tv_cmd(["playlist-clear"])
        self.tv_cmd(["loadfile", urls[0], "replace"])
        for u in urls[1:]:
            self.tv_cmd(["loadfile", u, "append"])
        if index:
            # queue the WHOLE album and jump, rather than queueing from the
            # chosen track onwards: otherwise "previous track" could never
            # reach the songs before the one that was picked
            self.tv_cmd(["set_property", "playlist-pos", index])
        self.tv_cmd(["set_property", "pause", False])
        self.mixmode = True          # so the bars show through the page
        self.visible = True
        self.page, self.sub, self.sel = P_NOW, 1, ""
        self.show_loading(self._music_meta(path)[2])

    def music_index(self) -> int:
        """Which album track is on: mpv counts from where we started."""
        pos = self.tv_query("playlist-pos")
        try:
            i = self.music_base + int(pos)
        except (TypeError, ValueError):
            i = self.music_base
        return min(max(0, i), len(self.music or [1]) - 1)

    def _timeline(self, pg, row, pos, total):
        """0:42 ----|--------- 3:45

        Plain characters rather than mosaic blocks: every colour change is a
        spacing attribute that eats a cell, and a bar made of them would lose
        four cells of its own width to say so.
        """
        pos = max(0, int(pos or 0))
        total = int(total or 0)
        width = 20
        at = int(width * pos / total) if total > 0 else 0
        at = min(width - 1, max(0, at))
        put(pg, row, 1, alpha(WHITE) + T("%5s " % self._hms(pos)) +
            alpha(CYAN) + T("-" * at) +
            alpha(YELLOW) + T("|") +
            alpha(CYAN) + T("-" * (width - 1 - at)) +
            alpha(WHITE) + T(" %s" % (self._hms(total) if total else "--:--")))
        return pg

    def _page_music(self):
        pg = blank_page()
        header(pg, self.page, None)
        idx = self.music_index()
        artist, album, title = self._music_meta(self.music[idx])
        put(pg, 2, 2, alpha(YELLOW) + DH + T("MUSIK"))
        put(pg, 5, 2, alpha(CYAN) + T(artist[:36]))
        put(pg, 6, 2, alpha(CYAN) + T(album[:36]))
        put(pg, 7, 2, alpha(WHITE) + T(title[:36]))
        secs = self.music_secs[idx] if idx < len(self.music_secs) else 0
        self._timeline(pg, 9, self.tv_query("time-pos") or 0, secs)
        put(pg, 11, 2, alpha(GREEN) +
            T("Titel %d/%d" % (idx + 1, len(self.music))))
        if idx + 1 < len(self.music):
            put(pg, 12, 2, alpha(CYAN) + T("danach: ") + alpha(WHITE) +
                T(self._music_meta(self.music[idx + 1])[2][:27]))
        put(pg, 21, 2, alpha(CYAN) + T("600 = Musik   0 ENTER = Schluss"))
        return pg

    @staticmethod
    def _square(freq, ms, rate=22050, vol=0.20):
        """One square-wave tone. Square rather than sine on purpose -- this is
        meant to sound like a machine from 1983, not a synthesiser."""
        n = int(rate * ms / 1000.0)
        amp = int(32767 * vol)
        half = (rate / float(freq)) / 2.0 if freq else 1e9
        out = bytearray()
        for i in range(n):
            v = amp if (i % (half * 2)) < half else -amp
            if i > n - 220:                 # fade the tail or it clicks
                v = int(v * (n - i) / 220.0)
            out += struct.pack("<h", v)
        return bytes(out)

    def _square_samples(self, freq, ms, vol=0.30, rate=22050):
        n = int(rate * ms / 1000.0)
        amp = int(32767 * vol)
        half = (rate / float(freq)) / 2.0 if freq else 1e9
        out = []
        for i in range(n):
            v = amp if (i % (half * 2)) < half else -amp
            if i > n - 220:                  # fade the tail or it clicks
                v = int(v * (n - i) / 220.0)
            out.append(v)
        return out

    def _fanfare(self, gain=1.0):
        """An ORIGINAL 8-bit flourish for the picture page.

        Two square voices, the way a period sound chip would have done it --
        a melody pulse and a bass pulse summed and clipped. Written from
        scratch; it is not a transcription of anyone else's tune.
        """
        rate = 22050
        # a rising figure that lands on the octave
        melody = [(392, 110), (523, 110), (659, 110), (784, 240), (0, 60),
                  (698, 120), (784, 120), (880, 320), (0, 90),
                  (784, 100), (880, 100), (988, 100), (1047, 560)]
        bass = [(131, 340), (0, 60), (196, 240), (0, 60),
                (175, 240), (196, 320), (0, 90),
                (131, 300), (196, 260), (262, 560)]
        top, low = [], []
        for f, ms in melody:
            top += self._square_samples(f, ms, 0.62 * gain, rate)
        for f, ms in bass:
            low += self._square_samples(f, ms, 0.38 * gain, rate)
        n = max(len(top), len(low))
        top += [0] * (n - len(top))
        low += [0] * (n - len(low))
        out = bytearray()
        for i in range(n):
            v = top[i] + low[i]
            v = 32767 if v > 32767 else (-32768 if v < -32768 else v)
            out += struct.pack("<h", v)
        return bytes(out)

    def _sound(self, name):
        """Fire and forget a sound effect.

        aplay, not mpv: mpv owns the screen, and the black carrier has no
        audio stream at all, so the HDMI device is genuinely free while a
        game is running. Popen without wait -- a busy audio device must never
        stall the game loop.
        """
        if not self.snd_dev:
            return
        # a wav dropped in /etc/retrokb always wins, so sounds can be
        # replaced without touching code
        path = "/etc/retrokb/%s.wav" % name
        if os.path.exists(path):
            self._play_wav(path)
            return
        path = "/dev/shm/retrokb-%s-%02d.wav" % (name, int(self.snd_gain * 99))
        if not os.path.exists(path):
            gain = self.snd_gain
            if name.startswith("glitch"):
                # Interference, not music: kept quieter than the effects and
                # short enough that you are never quite sure you heard it.
                g = gain * 0.55
                if name == "glitch1":            # a burst of dirt
                    data = b""
                    rnd = random.Random(11)
                    amp = int(32767 * g)
                    data = b"".join(struct.pack("<h", rnd.randint(-amp, amp))
                                    for _ in range(1600))
                elif name == "glitch2":          # something falling over
                    data = b""
                    for f in range(1400, 160, -90):
                        data += self._square(f, 12, vol=g)
                else:                            # two dry clicks
                    data = (self._square(2400, 9, vol=g)
                            + self._square(0, 70)
                            + self._square(1900, 7, vol=g))
            elif name == "fanfare":
                data = self._fanfare(gain)
            elif name == "eat":
                data = (self._square(1200, 55, vol=0.7 * gain) +
                        self._square(800, 65, vol=0.7 * gain))
            else:                            # wuwuwu: three falling swoops
                data = b""
                for _ in range(3):
                    for f in range(620, 180, -35):
                        data += self._square(f, 15, vol=0.7 * gain)
                    data += self._square(0, 45)
            fh = wave.open(path, "wb")
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(22050)
            fh.writeframes(data)
            fh.close()
        self._play_wav(path)

    def _play_wav(self, path):
        try:
            return subprocess.Popen(["aplay", "-q", "-D", self.snd_dev, path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            self.snd_dev = ""                # no aplay here: stop trying
            self.log.warning("aplay missing, sound effects off")
            return None

    # Most variants are clean drone; only the last few carry interference.
    # Which one plays is not random for its own sake -- it is tied to how many
    # pages have been turned, so a glitch arrives once every handful of pages
    # rather than several times a minute.
    HUM_CLEAN = 4
    HUM_DIRTY = 3
    HUM_VARIANTS = HUM_CLEAN + HUM_DIRTY

    # A short, hard loop instead of the drone: eight beats over 4.8 s. The
    # tones are chosen so every one completes a WHOLE number of cycles in that
    # time (40 = 192/4.8, 41.04 = 197/4.8, 80 = 384/4.8), which is what lets it
    # repeat without a click. 40 against 41.04 beats roughly once a second and
    # is what makes it feel wrong rather than merely low.
    MENACE_SECS = 4.8
    MENACE_BEATS = 8

    def _menace_beats(self):
        step = self.MENACE_SECS / self.MENACE_BEATS
        return [i * step for i in range(self.MENACE_BEATS)]

    def _menace_file(self):
        rate = 8000
        n = int(rate * self.MENACE_SECS)
        path = "/dev/shm/retrokb-menace-%02d.wav" % int(self.snd_gain * 99)
        if os.path.exists(path):
            return path
        amp = 32767 * 0.42 * self.snd_gain
        buf = []
        for i in range(n):
            t = i / float(rate)
            v = (math.sin(2 * math.pi * 40.0 * t)
                 + math.sin(2 * math.pi * 41.04 * t)
                 + 0.30 * math.sin(2 * math.pi * 80.0 * t))
            buf.append(int(amp * v / 2.3))
        hit = int(32767 * 0.55 * self.snd_gain)
        for k, at_s in enumerate(self._menace_beats()):
            at = int(at_s * rate)
            accent = (k % 4 == 0)
            length = int(rate * (0.16 if accent else 0.09))
            for j in range(length):
                if at + j >= n:
                    break
                decay = 1.0 - j / float(length)
                tt = j / float(rate)
                # a low thud, and on the accents a dissonant tritone above it
                v = math.sin(2 * math.pi * 55.0 * tt)
                if accent:
                    v += 0.5 * math.sin(2 * math.pi * 77.8 * tt)
                buf[at + j] += int(hit * decay * decay * v)
        frames = bytearray()
        for v in buf:
            v = 32767 if v > 32767 else (-32768 if v < -32768 else v)
            frames += struct.pack("<h", v)
        fh = wave.open(path, "wb")
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(frames))
        fh.close()
        return path

    def _hum_marks(self, variant, rate=8000, secs=30):
        """Where the interference sits in a variant, as (sample, kind).

        Drawn from its OWN generator so it can be recomputed later without
        rendering the audio again -- which is what lets the lights be fired at
        exactly the moment the sound happens, from a file that was written
        hours ago.
        """
        if variant < self.HUM_CLEAN:
            return []
        rnd = random.Random(4000 + variant)
        out = [(rnd.randrange(rate, rate * secs - rate * 2),
                rnd.choice(("dirt", "fall", "click")))
               for _ in range(rnd.randint(1, 2))]
        return sorted(out)

    def _hum_file(self, variant=0):
        """A 30 s drone with interference baked in, looping without a seam.

        55 Hz against 55.5 Hz gives a half-hertz beat -- the slow throb is
        what makes it unsettling rather than merely low. The length is chosen
        so every component completes a WHOLE number of cycles (1650, 1665,
        3300), otherwise the loop point clicks every time it comes round.

        ⚠️ The glitches are IN HERE rather than played separately, because
        `plughw` is a direct hardware PCM with no dmix: exactly one process
        may hold it, and the hum already does. A second aplay simply fails.
        Four variants, chosen at random each time the file restarts, so the
        interference never falls in the same place twice running.
        """
        rate, secs = 8000, 30
        path = "/dev/shm/retrokb-hum-%02d-%d.wav" % (
            int(self.snd_gain * 99), variant)
        if os.path.exists(path):
            return path
        n = rate * secs
        amp = 32767 * 0.40 * self.snd_gain
        buf = []
        for i in range(n):
            t = i / float(rate)
            v = (math.sin(2 * math.pi * 55.0 * t)
                 + math.sin(2 * math.pi * 55.5 * t)
                 + 0.35 * math.sin(2 * math.pi * 110.0 * t))
            buf.append(int(amp * v / 2.35))
        rnd = random.Random(1000 + variant)
        gamp = int(32767 * 0.13 * self.snd_gain)
        for at, kind in self._hum_marks(variant, rate, secs):
            if kind == "dirt":                       # a burst of noise
                for k in range(rnd.randint(260, 700)):
                    buf[at + k] += rnd.randint(-gamp, gamp)
            elif kind == "fall":                     # something toppling
                f, k = 1300.0, 0
                while f > 150 and at + k < n - 2:
                    half = (rate / f) / 2.0
                    for _j in range(int(half * 2)):
                        if at + k >= n:
                            break
                        buf[at + k] += gamp if (_j < half) else -gamp
                        k += 1
                    f *= 0.93
            else:                                    # a dry click or two
                for off in ((0,) if rnd.random() < 0.5
                            else (0, rnd.randint(500, 1500))):
                    for k in range(30):
                        if at + off + k < n:
                            buf[at + off + k] += gamp if k % 7 < 3 else -gamp
        frames = bytearray()
        for v in buf:
            v = 32767 if v > 32767 else (-32768 if v < -32768 else v)
            frames += struct.pack("<h", v)
        fh = wave.open(path, "wb")
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(frames))
        fh.close()
        return path

    def _hum_check(self):
        """Hold the drone for as long as 666 is on screen.

        Restarted from the tick rather than looped in a shell: aplay exits at
        the end of the file, and re-launching it here means no process group
        to chase and no orphan left humming if the OS goes away.
        """
        want = bool(self.visible and self.page == P_CREEP
                    and self.snd_dev and self.snd_gain > 0)
        kind = "menace" if (self._meta or {}).get("mode") == "menace" \
            else "drone"
        if want and kind != self._hum_kind:
            # the page changed what it wants to hear: stop the old one now
            # rather than letting it finish its loop
            if self._hum is not None and self._hum.poll() is None:
                self._hum.terminate()
            self._hum, self._hum_due = None, []
            self._hum_kind = kind
        if want and kind == "menace":
            if self._hum is None or self._hum.poll() is not None:
                path = "/dev/shm/retrokb-menace-%02d.wav" % int(
                    self.snd_gain * 99)
                if os.path.exists(path):
                    self._hum = self._play_wav(path)
                    self._hum_at = time.monotonic()
                    self._hum_due = list(self._menace_beats())
                elif not self._hum_building:
                    self._hum_building = True

                    def _build_m():
                        try:
                            self._menace_file()
                        finally:
                            self._hum_building = False
                    threading.Thread(target=_build_m, daemon=True).start()
            return
        if want:
            if self._hum_sub != self.sub:
                self._hum_sub = self.sub
                self._hum_pages += 1
            if self._hum is None or self._hum.poll() is not None:
                if self._hum_pages >= self._hum_thresh:
                    # enough pages have gone by: let the next loop be a dirty
                    # one, then start counting towards a fresh target
                    self._hum_pages = 0
                    # Martin asked for half again as often: 3-9 pages -> 2-6
                    self._hum_thresh = random.randint(2, 6)
                    v = random.randrange(self.HUM_CLEAN, self.HUM_VARIANTS)
                else:
                    v = random.randrange(self.HUM_CLEAN)
                path = "/dev/shm/retrokb-hum-%02d-%d.wav" % (
                    int(self.snd_gain * 99), v)
                if os.path.exists(path):
                    self._hum = self._play_wav(path)
                    # line the lights up with the interference in THIS file
                    self._hum_at = time.monotonic()
                    self._hum_due = [at / 8000.0
                                     for at, _k in self._hum_marks(v)]
                elif not self._hum_building:
                    # 30 s of trigonometry costs this Pi about three seconds.
                    # Building it on the main loop would freeze the OS mid
                    # page, so it happens on a thread and the drone simply
                    # starts a moment later.
                    self._hum_building = True

                    def _build(v=v):
                        try:
                            self._hum_file(v)
                        finally:
                            self._hum_building = False
                    threading.Thread(target=_build, daemon=True).start()
        elif self._hum is not None:
            if self._hum.poll() is None:
                self._hum.terminate()
            self._hum = None
            self._hum_due = []
            self._hum_kind = "drone"

    def _scores(self, game="snake"):
        try:
            with urllib.request.urlopen(
                    "%s/tt/scores?game=%s" % (self.broadcaster, game),
                    timeout=5) as r:
                return json.load(r).get("scores", [])
        except (OSError, ValueError):
            return []

    def _score_submit(self, name, score, game="snake"):
        try:
            body = json.dumps({"game": game, "name": name,
                               "score": score}).encode()
            req = urllib.request.Request(
                "%s/tt/scores" % self.broadcaster, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r).get("scores", [])
        except (OSError, ValueError):
            self.err = "nicht gespeichert"
            return (self.snake or {}).get("hs", [])

    def _snake_over(self):
        """Game over: fetch the table, and only offer the name entry for an
        actual record -- Martin asked for it on beating the TOP score, not on
        merely reaching the board."""
        g = self.snake
        g["hs"] = self._scores()
        g["entering"] = bool(g["score"] > 0 and
                             (not g["hs"] or g["score"] > g["hs"][0]["score"]))
        g["initials"] = ""
        g["mt"] = None

    def _snake_start(self):
        import random as _rnd
        mid_r = (SNAKE_TOP + SNAKE_BOT) // 2
        mid_c = (SNAKE_LEFT + SNAKE_RIGHT) // 2
        self.snake = {
            "body": [(mid_r, mid_c), (mid_r, mid_c - 1), (mid_r, mid_c - 2)],
            "dir": (0, 1), "turn": (0, 1), "score": 0, "dead": False,
            "next": time.monotonic() + SNAKE_STEP, "blink": 0, "rnd": _rnd,
        }
        self._snake_feed()
        self.visible = True
        self.repaint()

    def _snake_feed(self):
        g = self.snake
        free = [(r, c)
                for r in range(SNAKE_TOP, SNAKE_BOT + 1)
                for c in range(SNAKE_LEFT, SNAKE_RIGHT + 1)
                if (r, c) not in g["body"]]
        g["food"] = g["rnd"].choice(free) if free else None

    def _snake_step(self):
        g = self.snake
        if g["dead"]:
            return
        g["dir"] = g["turn"]
        hr, hc = g["body"][0]
        nr, nc = hr + g["dir"][0], hc + g["dir"][1]
        hit_wall = not (SNAKE_TOP <= nr <= SNAKE_BOT
                        and SNAKE_LEFT <= nc <= SNAKE_RIGHT)
        # the tail cell frees up on the same move, so running into it is legal
        if hit_wall or (nr, nc) in g["body"][:-1]:
            g["dead"] = True
            self._sound("over")
            self._snake_over()
            return
        g["body"].insert(0, (nr, nc))
        if (nr, nc) == g["food"]:
            g["score"] += 1
            self._sound("eat")
            self._snake_feed()
        else:
            g["body"].pop()

    def _snake_speed(self):
        # a little quicker every five, but never faster than a page can paint
        return max(0.11, SNAKE_STEP - 0.01 * (self.snake["score"] // 5))

    def _page_snake(self):
        g = self.snake
        pg = blank_page()
        header(pg, self.page, None)
        put(pg, 2, 2, alpha(GREEN) + T("1337"))
        put(pg, 2, 30, alpha(WHITE) + T("%3d" % g["score"]))
        body = set(g["body"])
        for r in range(SNAKE_TOP, SNAKE_BOT + 1):
            row = bytearray(b"\x20" * 40)
            row[0] = 0x10 + GREEN          # one attribute per row, at the edge
            for c in range(SNAKE_LEFT, SNAKE_RIGHT + 1):
                if (r, c) in body:
                    row[c] = SNAKE_BLOCK
            pg[r] = row
        # The food BLINKS rather than being a second colour: a colour change
        # is a spacing attribute, i.e. it would eat a cell of the playfield
        # and shift everything on that row by one.
        if g["food"] and not g["dead"] and g["blink"] % 3 != 2:
            fr, fc = g["food"]
            pg[fr][fc] = SNAKE_BLOCK
        if g["dead"]:
            put(pg, 7, 12, alpha(RED) + NEWBG + alpha(WHITE) + T(" GAME OVER "))
            put(pg, 9, 13, alpha(WHITE) + T("Punkte: %d" % g["score"]))
            if g.get("entering"):
                put(pg, 11, 11, alpha(YELLOW) + T("NEUER REKORD"))
                put(pg, 13, 8, alpha(WHITE) + T("Initialen: ") +
                    alpha(GREEN) + T((g.get("initials") or "") + "_"))
                put(pg, 15, 6, alpha(CYAN) + T("2-9 tippen wie fr}her"
                                               .replace("}", chr(0xFC))))
                put(pg, 16, 6, alpha(CYAN) + T("ENTER speichert"))
            else:
                put(pg, 11, 13, alpha(YELLOW) + T("BESTENLISTE"))
                rows = g.get("hs") or []
                for i in range(3):
                    if i < len(rows):
                        txt = "%d. %-3s %4d" % (i + 1, rows[i].get("name", "?"),
                                                rows[i].get("score", 0))
                    else:
                        txt = "%d. ---    0" % (i + 1)
                    put(pg, 13 + i, 13, alpha(WHITE) + T(txt))
                put(pg, 18, 11, alpha(YELLOW) + T("ENTER = nochmal"))
                put(pg, 19, 11, alpha(YELLOW) + T("C     = Schluss"))
        else:
            put(pg, 23, 0, alpha(CYAN) +
                T("4 6 8 2 = steuern   C = Schluss"))
        return pg

    def panic(self):
        """Everything off, back to the index, whatever was on screen.

        Deliberately blunt: it drops every piece of state rather than trying
        to unwind politely, because the whole point is that it works when
        something is stuck.
        """
        self.snake = self.photo = self.chat = self.invent = None
        self.news = None
        self._rand_ask = self.qr_on = False
        self.live = self.music = self._resume = self._loading = None
        self.live_name = self.live_key = self.live_epg = ""
        self.mixmode = False
        self.entry = self.sel = ""
        self.seek_armed = False
        self._hist = []
        self.tv_cmd(["playlist-clear"])
        self.tv_cmd(["loadfile", CARRIER, "replace", -1, "loop-file=inf"])
        self.tv_cmd(["set_property", "pause", False])
        self.visible = True
        self.page, self.sub = 100, 1
        self._hum_check()
        self.repaint()

    def show_volume(self, level: int, steps: int = 10):
        """A level meter down the right edge, for a third of a second.

        Deliberately its OWN overlay rather than part of a page: the volume
        gets changed just as often with the OS shut over a film as with it
        open, so it cannot live on a teletext page -- and repainting a whole
        page for it would cost ~90 ms and stamp on whatever is on screen.
        Painting a 40px-wide strip instead costs well under a millisecond.

        Placed from the VIDEO's own dimensions, not the carrier's: mpv crops
        overlays to the frame, so a cinemascope film would otherwise take the
        meter off the bottom of its own picture.
        """
        try:
            vw = int(self.tv_query("video-params/w") or 0) or 1280
            vh = int(self.tv_query("video-params/h") or 0) or 720
        except (TypeError, ValueError):
            vw, vh = 1280, 720
        steps = max(2, int(steps))
        dash_h, gap, w = 8, 14, 40
        if steps * dash_h + (steps - 1) * gap > vh - 40:
            gap = max(2, (vh - 40 - steps * dash_h) // max(1, steps - 1))
        h = steps * dash_h + (steps - 1) * gap
        lit = bytes((255, 255, 255, 255)) * w
        # mpv takes bgra premultiplied, so a dim white is the same small
        # value on all four channels -- not white with a low alpha
        dim = bytes((40, 40, 40, 40)) * w
        fb = bytearray(w * h * 4)
        for i in range(steps):
            row = lit if (steps - i) <= level else dim
            y0 = i * (dash_h + gap)
            fb[y0 * w * 4:(y0 + dash_h) * w * 4] = row * dash_h
        with open(VOL_SHM, "wb") as fh:
            fh.write(bytes(fb))
        self.tv_cmd(["overlay-add", VOL_OVERLAY, max(0, vw - w - 40),
                     max(0, (vh - h) // 2), VOL_SHM, 0, "bgra", w, h, w * 4])
        self._vol_until = time.monotonic() + 0.3

    def _hide_volume(self):
        self._vol_until = 0.0
        self.tv_cmd(["overlay-remove", VOL_OVERLAY])

    def is_news(self) -> bool:
        return bool(self.visible and self.page == P_NEWS_AI)

    def is_chat(self) -> bool:
        """True while either talking page is on screen. The numpad means
        different things there, so the router has to be able to ask."""
        return bool(self.visible and self.page in (P_CHAT, P_RPG))

    # ---- ChatCRT News ---------------------------------------------------
    # One page, five levels deep: sections -> stories -> one story -> one
    # source -> arguing about it. Kept as receiver-side state rather than as
    # five page numbers because every level is a live query, and because
    # "back one level" has to mean the level you came from, not a page
    # history that also remembers how you got there.
    NEWS_LEVELS = ("sections", "stories", "story", "article", "talk")

    def _news_init(self):
        if self.news is None:
            self.news = {"level": "sections", "sections": [], "si": 0,
                         "section": "", "label": "", "stories": [], "sti": 0,
                         "story": None, "lines": [], "sources": [], "qi": 0,
                         "art": [], "op": None, "log": [], "opts": [],
                         "oi": 0, "scroll": 0, "job": None, "poll": 0.0,
                         "err": ""}
            self._news_ask("top", section="deutschland", quiet=True)
        return self.news

    def _news_ask(self, mode, quiet=False, **req):
        n = self.news
        if n["job"]:
            return
        req["mode"] = mode
        n["err"] = ""
        try:
            body = json.dumps(req).encode()
            rq = urllib.request.Request(
                "%s/news" % self.broadcaster, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(rq, timeout=10) as r:
                d = json.load(r)
            n["job"], n["mode"] = d.get("id"), mode
            n["poll"] = time.monotonic() + 0.8
        except (OSError, ValueError):
            n["err"] = "Kein Signal zur Redaktion"

    def _news_poll(self):
        n = self.news
        try:
            with urllib.request.urlopen(
                    "%s/news?id=%s" % (self.broadcaster, n["job"]),
                    timeout=8) as r:
                d = json.load(r)
        except (OSError, ValueError):
            n["job"] = None
            n["err"] = "Redaktion antwortet nicht"
            return
        if not n["sections"]:
            n["sections"] = d.get("sections") or []
        if not d.get("done"):
            n["poll"] = time.monotonic() + 0.8
            return
        n["job"] = None
        data = d.get("data") or {}
        mode = n.get("mode")
        if data.get("error"):
            n["err"] = str(data["error"])[:34]
        elif mode == "top":
            n["label"] = data.get("label", "")
            n["stories"] = data.get("stories") or []
            n["sti"], n["level"] = 0, "stories"
        elif mode == "story":
            n["lines"] = data.get("lines") or []
            n["sources"] = data.get("sources") or []
            n["qi"], n["level"], n["scroll"] = 0, "story", 0
        elif mode == "article":
            n["art"] = data.get("lines") or []
            n["partial"] = bool(data.get("partial"))
            n["level"], n["scroll"] = "article", 0
        elif mode == "opinion":
            n["op"] = data
            n["opts"] = data.get("options") or []
            n["oi"], n["level"], n["scroll"] = 0, "talk", 0
            n["log"] = [("crt", " ".join(data.get("lines") or []))]
        else:
            n["log"].append(("crt", data.get("text", "")))
            n["opts"] = data.get("options") or []
            n["oi"], n["scroll"] = 0, 0
        if self.visible:
            self.repaint()

    def _news_context(self):
        n = self.news
        return " ".join(n["lines"] or []) + " " + " ".join(n["art"] or [])

    def _news_back(self):
        n = self.news
        order = self.NEWS_LEVELS
        i = order.index(n["level"])
        if i == 0:
            self.news = None
            self._go(P_NEWS)
            return
        n["level"] = order[i - 1]
        n["scroll"] = 0

    def _page_news(self):
        n = self._news_init()
        pg = blank_page()
        header(pg, self.page, None)
        put(pg, 2, 2, alpha(CYAN) + DH + T("ChatCRT News"))
        lvl = n["level"]
        if n["job"]:
            put(pg, 22, 2, alpha(YELLOW) + T("ChatCRT liest nach ..."))
        elif n["err"]:
            put(pg, 22, 2, alpha(RED) + T(n["err"][:36]))

        if lvl == "sections":
            secs = n["sections"] or [{"label": "wird geladen"}]
            for i, sec in enumerate(secs):
                mark = (alpha(WHITE) + NEWBG + alpha(BLACK)) \
                    if i == n["si"] % max(1, len(secs)) else alpha(CYAN)
                put(pg, 6 + i * 2, 1, mark +
                    T(" %d %s " % (i + 1, sec.get("label", ""))[:36]))
            put(pg, 21, 2, alpha(CYAN) + T("/ x w{hlen  ENTER  C zur}ck"
                                           .replace("{", chr(0xE4))
                                           .replace("}", chr(0xFC))))
        elif lvl == "stories":
            put(pg, 4, 2, alpha(YELLOW) + T(n["label"][:36]))
            for i, st in enumerate(n["stories"][:5]):
                sel = i == n["sti"] % max(1, len(n["stories"]))
                mark = (alpha(WHITE) + NEWBG + alpha(BLACK)) if sel \
                    else alpha(WHITE)
                put(pg, 6 + i * 3, 1, mark + T((" %d %s " % (i + 1,
                                                            st["title"]))[:37]))
                if sel:
                    for k, ln in enumerate(self._wrap(st["teaser"], 34)[:2]):
                        put(pg, 7 + i * 3 + k, 3, alpha(CYAN) + T(ln))
            put(pg, 21, 2, alpha(CYAN) + T("/ x w{hlen  ENTER  4 zur}ck"
                                           .replace("{", chr(0xE4))
                                           .replace("}", chr(0xFC))))
        elif lvl in ("story", "article"):
            body = n["lines"] if lvl == "story" else n["art"]
            title = (n["story"] or {}).get("title", "")
            put(pg, 4, 2, alpha(YELLOW) + T(title[:36]))
            r = 6
            for ln in body[n["scroll"]:n["scroll"] + 9]:
                if ln.strip():
                    put(pg, r, 2, alpha(WHITE) + T(ln[:36]))
                r += 1
            if lvl == "story":
                put(pg, 16, 2, alpha(GREEN) + T("Quellen:"))
                for i, q in enumerate(n["sources"][:5]):
                    put(pg, 17 + i, 2, alpha(CYAN) + T("%d " % (i + 1)) +
                        alpha(WHITE) + T(q.get("host", "")[:30]))
                put(pg, 22, 2, alpha(CYAN) + T("1-5 Quelle  2x. bl{ttern"
                                               .replace("{", chr(0xE4))))
            else:
                put(pg, 16, 1, alpha(GREEN) + T(" 1 N{chste Meldung"
                                                .replace("{", chr(0xE4))))
                put(pg, 17, 1, alpha(GREEN) + T(" 2 Was denkst du?"))
                put(pg, 18, 1, alpha(GREEN) + T(" 3 Andere Quelle"))
                put(pg, 19, 1, alpha(GREEN) + T(" 4 Zur}ck".replace(
                    "}", chr(0xFC))))
                put(pg, 20, 1, alpha(GREEN) + T(" 5 QR-Code aufs Handy"))
                if n.get("partial"):
                    put(pg, 21, 1, alpha(RED) +
                        T(" Quelle stumm: Zusammenfassung"))
        else:                                   # talk
            op = n["op"] or {}
            put(pg, 4, 2, alpha(YELLOW) + T("Was denkst du?"))
            lines = []
            for who, text in n["log"]:
                col = GREEN if who == "du" else WHITE
                for k, ln in enumerate(self._wrap(text, 35) or [""]):
                    lines.append((col, ("> " if who == "du" and not k
                                        else "  ") + ln))
                lines.append((WHITE, ""))
            if len(n["log"]) == 1:
                for tag, key, col in (("Dafuer:", "pro", GREEN),
                                      ("Dagegen:", "con", RED),
                                      ("Ich haette:", "other", CYAN)):
                    if op.get(key):
                        lines.append((col, tag))
                        for x in op[key]:
                            lines.append((WHITE, "  " + x))
            end = max(1, len(lines) - n["scroll"])
            for i, (col, ln) in enumerate(lines[max(0, end - 8):end]):
                if ln.strip():
                    put(pg, 6 + i, 1, alpha(col) + T(ln[:38]))
            for k in range(3):
                if k < len(n["opts"]):
                    sel = k == n["oi"] % max(1, len(n["opts"]))
                    mark = (alpha(WHITE) + NEWBG + alpha(BLACK)) if sel \
                        else alpha(CYAN)
                    put(pg, 16 + k, 1, mark + T((" %s " % n["opts"][k])[:36]))
            put(pg, 20, 1, alpha(GREEN) + T(" 4 Zur}ck".replace("}", chr(0xFC))))
            put(pg, 21, 2, alpha(CYAN) + T("/ x w{hlen   ENTER".replace(
                "{", chr(0xE4))))
        return pg

    def _news_key(self, name):
        n = self._news_init()
        lvl = n["level"]
        if name in ("back",) or (name == "4" and lvl != "sections"):
            self._news_back()
        elif name in ("next", "prev"):
            step = 1 if name == "next" else -1
            if lvl == "sections" and n["sections"]:
                n["si"] = (n["si"] + step) % len(n["sections"])
            elif lvl == "stories" and n["stories"]:
                n["sti"] = (n["sti"] + step) % len(n["stories"])
            elif lvl == "talk" and n["opts"]:
                n["oi"] = (n["oi"] + step) % len(n["opts"])
            else:
                n["scroll"] = max(0, n["scroll"] + step)
        elif name == "more":                    # the . key scrolls long text
            n["scroll"] = n["scroll"] + 6
            if n["scroll"] > 30:
                n["scroll"] = 0
        elif name == "enter":
            if lvl == "sections" and n["sections"]:
                sec = n["sections"][n["si"] % len(n["sections"])]
                n["section"] = sec.get("key", "")
                self._news_ask("top", section=n["section"])
            elif lvl == "stories" and n["stories"]:
                st = n["stories"][n["sti"] % len(n["stories"])]
                n["story"] = st
                self._news_ask("story", title=st["title"],
                               teaser=st["teaser"], sources=st["sources"])
            elif lvl == "talk" and n["opts"]:
                pick = n["opts"][n["oi"] % len(n["opts"])]
                n["log"].append(("du", pick))
                self._news_ask("talk", title=(n["story"] or {}).get("title", ""),
                               context=self._news_context(),
                               history=[{"role": "assistant" if w != "du"
                                         else "user", "text": t}
                                        for w, t in n["log"][-6:]],
                               text=pick)
        elif name in "12345":
            k = int(name)
            if lvl == "story" and k <= len(n["sources"]):
                n["qi"] = k - 1
                # hand the search snippets along: if the page will not give
                # its text up, they are still something true to summarise
                src = n["sources"][k - 1]
                self._news_ask("article", url=src["url"],
                               fallback=" ".join(
                                   q.get("text", "") for q in n["sources"]))
            elif lvl == "article":
                if k == 1:
                    n["sti"] = (n["sti"] + 1) % max(1, len(n["stories"]))
                    st = n["stories"][n["sti"]]
                    n["story"] = st
                    self._news_ask("story", title=st["title"],
                                   teaser=st["teaser"], sources=st["sources"])
                elif k == 2:
                    self._news_ask("opinion",
                                   title=(n["story"] or {}).get("title", ""),
                                   context=self._news_context())
                elif k == 3:
                    n["level"] = "story"
                elif k == 5:
                    # read it properly, on a device that can
                    src = (n["sources"] or [{}])[n["qi"] % max(1, len(
                        n["sources"] or [1]))]
                    if src.get("url"):
                        self.show_qr(src["url"], "Quelle auf dem Handy")
            elif lvl == "sections" and k <= len(n["sections"]):
                n["si"] = k - 1
                n["section"] = n["sections"][k - 1].get("key", "")
                self._news_ask("top", section=n["section"])
            elif lvl == "stories" and k <= len(n["stories"]):
                n["sti"] = k - 1
                st = n["stories"][k - 1]
                n["story"] = st
                self._news_ask("story", title=st["title"],
                               teaser=st["teaser"], sources=st["sources"])
        self.repaint()

    def _chat_init(self):
        if self.chat is None:
            self.chat = {"log": [], "input": "", "mode": 0, "wkeys": "",
                         "cands": [], "ci": 0, "mt": None, "job": None,
                         "poll": 0.0, "scroll": 0, "opts": [], "oi": 0,
                         "turn": 0, "turns": 7, "mood": 0, "premise": "",
                         "end": False, "session": "",
                         "kind": "rpg" if self.page == P_RPG else "chat"}
            # ChatCRT opens the conversation itself rather than sitting there
            # waiting -- it is a television, it has opinions
            self._chat_ask(start=True)
        return self.chat

    def _chat_word(self):
        """What the word being typed currently looks like on screen."""
        c = self.chat
        if not c["wkeys"]:
            return ""
        if c["cands"]:
            return c["cands"][c["ci"] % len(c["cands"])]
        return c["wkeys"]

    def _chat_lookup(self):
        c = self.chat
        lang = CHAT_MODES[c["mode"]][1]
        if not c["wkeys"]:
            c["cands"], c["ci"] = [], 0
            return
        try:
            with urllib.request.urlopen(
                    "%s/t9?lang=%s&keys=%s" % (self.broadcaster, lang,
                                               c["wkeys"]), timeout=3) as r:
                c["cands"] = json.load(r).get("words", [])
        except (OSError, ValueError):
            c["cands"] = []
        c["ci"] = 0

    def _chat_commit(self, trailing=""):
        c = self.chat
        w = self._chat_word()
        if w:
            c["input"] += w + trailing
        elif trailing:
            c["input"] += trailing
        c["wkeys"], c["cands"], c["ci"], c["mt"] = "", [], 0, None

    def _chat_ask(self, text="", start=False):
        c = self.chat
        if c["job"]:
            return
        if text:
            c["log"].append(("du", text))
        c["input"], c["scroll"], c["opts"], c["oi"] = "", 0, [], 0
        hist = [{"role": "assistant" if who != "du" else "user", "text": t}
                for who, t in c["log"][-8:-1]]
        try:
            body = json.dumps({"text": text, "history": hist,
                               "start": start, "kind": c.get("kind", "rpg"),
                               "session": c.get("session", "")}).encode()
            req = urllib.request.Request(
                "%s/chat" % self.broadcaster, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.load(r)
            c["job"], c["session"] = d.get("id"), d.get("session", "")
        except (OSError, ValueError):
            c["log"].append(("crt", "Kein Signal zum Denkapparat."))
            return
        c["log"].append(("crt", ""))
        c["poll"] = time.monotonic() + 0.6

    def _chat_send(self):
        """ENTER sends whatever is typed; with nothing typed it sends the
        highlighted option, which is the ordinary way to talk to ChatCRT."""
        c = self.chat
        self._chat_commit()
        if c["end"] and c.get("kind") == "rpg":
            self.chat = None            # a new episode, new premise
            self._chat_init()
            return
        text = c["input"].strip()
        if not text and c["opts"]:
            text = c["opts"][c["oi"] % max(1, len(c["opts"]))]
        if text:
            self._chat_ask(text)

    def _chat_poll(self):
        c = self.chat
        try:
            with urllib.request.urlopen(
                    "%s/chat?id=%s" % (self.broadcaster, c["job"]),
                    timeout=5) as r:
                d = json.load(r)
        except (OSError, ValueError):
            c["job"] = None
            return
        txt = (d.get("text") or "").strip()
        c["log"][-1] = ("crt", txt or "...")
        if d.get("done"):
            c["job"] = None
            c["opts"] = [o for o in (d.get("options") or []) if o]
            c["oi"] = 0
            c["turn"], c["turns"] = d.get("turn", 0), d.get("turns", 7)
            c["mood"], c["premise"] = d.get("mood", 0), d.get("premise", "")
            if d.get("end"):
                c["end"] = True
                c["opts"] = []
        c["poll"] = time.monotonic() + 0.6
        if self.visible and self.page == P_CHAT:
            self.repaint()

    def _page_chat(self):
        c = self._chat_init()
        scheme, lang = CHAT_MODES[c["mode"]]
        pg = blank_page()
        header(pg, self.page, None)
        rpg = c.get("kind") == "rpg"
        put(pg, 2, 2, alpha(CYAN) + DH + T("ChatRPG" if rpg else "ChatCRT"))
        if rpg and c["premise"]:
            mood = c["mood"]
            bar = ("+" * mood) if mood > 0 else ("-" * -mood)
            put(pg, 4, 1, alpha(MAGENTA) +
                T("Folge %d/%d" % (min(c["turn"], c["turns"]), c["turns"])) +
                alpha(GREEN if mood >= 0 else RED) + T(" " + (bar or "o")))
        lines = []
        for who, text in c["log"]:
            colour = GREEN if who == "du" else WHITE
            for k, extra in enumerate(self._wrap(text, 35) or [""]):
                lines.append((colour, ("> " if who == "du" and not k
                                       else "  ") + extra))
            lines.append((WHITE, ""))
        window = 9
        end = max(1, len(lines) - c["scroll"])
        for i, (colour, text) in enumerate(lines[max(0, end - window):end]):
            if text.strip():
                put(pg, 5 + i, 1, alpha(colour) + T(text[:38]))
        # the three replies: white on green marks the one ENTER would send
        for k in range(3):
            row = 15 + k
            if k < len(c["opts"]):
                txt = (" %s " % c["opts"][k])[:36]
                if k == c["oi"] % max(1, len(c["opts"])):
                    put(pg, row, 1,
                        alpha(WHITE) + NEWBG + alpha(BLACK) + T(txt))
                else:
                    put(pg, row, 1, alpha(CYAN) + T(txt))
        n_opts = len(c["opts"]) + 1
        if c["oi"] % n_opts == n_opts - 1:
            put(pg, 18, 1, alpha(WHITE) + NEWBG + alpha(BLACK) +
                T(" Eingabe: %s %s " % (scheme, lang.upper())))
        else:
            put(pg, 18, 1, alpha(MAGENTA) +
                T(" Eingabe: %s %s" % (scheme, lang.upper())))
        typed = (c["input"] + self._chat_word())
        if typed or c["wkeys"]:
            put(pg, 19, 0, alpha(YELLOW) + T(">") + alpha(WHITE) +
                T(" " + typed[-33:] + "_"))
            if scheme == "T9" and c["cands"]:
                put(pg, 20, 2, alpha(CYAN) + T(" ".join(c["cands"][:4])[:36]))
        elif not c["log"]:
            for k, row in enumerate(("7 .,?   8 ABC   9 DEF",
                                     "4 GHI   5 JKL   6 MNO",
                                     "1 PQRS  2 TUV   3 WXYZ")):
                put(pg, 19 + k, 8, alpha(CYAN) + T(row))
        if c["end"] and rpg:
            put(pg, 18, 1, alpha(YELLOW) + NEWBG + alpha(BLACK) +
                T(" SENDESCHLUSS "))
            put(pg, 21, 2, alpha(CYAN) + T("ENTER = neue Folge   C = zur}ck"
                                           .replace("}", chr(0xFC))))
        elif c["job"]:
            put(pg, 21, 2, alpha(YELLOW) + T("ChatCRT denkt nach ..."))
        else:
            put(pg, 21, 2, alpha(CYAN) +
                T("/ x w{hlen   ENTER   . bl{ttert".replace("{", chr(0xE4))))
        return pg

    def _chat_key(self, name):
        c = self._chat_init()
        scheme, lang = CHAT_MODES[c["mode"]]
        now = time.monotonic()
        # The input-mode switch is the LAST item in the list rather than a key
        # of its own: divide and times move the highlight everywhere else in
        # the OS, so they move it here too, and the mode is just one more
        # thing that can be selected.
        n_opts = len(c["opts"]) + 1
        if name in ("next", "prev"):
            c["oi"] = (c["oi"] + (1 if name == "next" else -1)) % n_opts
        elif name == "more":                         # the "." key: scrolling
            c["scroll"] = c["scroll"] + 3
            if c["scroll"] > max(0, len(c["log"]) * 3):
                c["scroll"] = 0                      # wrap back to the newest
        elif name == "enter":
            if c["oi"] % n_opts == n_opts - 1 and not c["input"] \
                    and not c["wkeys"]:
                self._chat_commit()
                c["mode"] = (c["mode"] + 1) % len(CHAT_MODES)
            else:
                self._chat_send()
        elif name == "back":
            if c["wkeys"]:
                c["wkeys"] = c["wkeys"][:-1]
                if scheme == "T9":
                    self._chat_lookup()
                else:
                    c["wkeys"] = ""
            elif c["input"]:
                c["input"] = c["input"][:-1]
            c["mt"] = None
        elif name == "0" and scheme == "T9" and c["cands"] and c["wkeys"]:
            # while a word is being typed, 0 walks the candidates before it
            # commits one -- there is no spare key left and this is where a
            # phone put "next word" too
            c["ci"] = (c["ci"] + 1) % len(c["cands"])
        elif name == "0":
            self._chat_commit(" ")
        elif name == "7":
            # punctuation, on the key that sits where a phone's 1 sat
            mt = c["mt"]
            if mt and mt[0] == "7" and now < mt[2]:
                i = (mt[1] + 1) % len(PUNCT)
                c["input"] = c["input"][:-1] + PUNCT[i]
                c["mt"] = ("7", i, now + MT_TIMEOUT)
            else:
                self._chat_commit()
                c["input"] += PUNCT[0]
                c["mt"] = ("7", 0, now + MT_TIMEOUT)
        elif name in MULTITAP:
            if scheme == "T9":
                c["wkeys"] += name
                self._chat_lookup()
            else:
                letters = MULTITAP[name]
                mt = c["mt"]
                if mt and mt[0] == name and now < mt[2]:
                    i = (mt[1] + 1) % len(letters)
                    c["input"] = c["input"][:-1] + letters[i].lower()
                    c["mt"] = (name, i, now + MT_TIMEOUT)
                else:
                    c["input"] += letters[0].lower()
                    c["mt"] = (name, 0, now + MT_TIMEOUT)
        self.repaint()

    @staticmethod
    def _rows_text(rows):
        """A page grid back to plain text, for handing one page to the model
        as the context for the next."""
        out = []
        for row in rows:
            line = "".join(chr(b) if 0x20 <= b < 0x80 else " " for b in row)
            if line.strip():
                out.append(line.rstrip())
        return "\n".join(out)

    def _invent_start(self, page: int, sub: int = 1, context: str = ""):
        """Ask the broadcaster to make this page up."""
        self.invent = {"page": page, "sub": sub, "job": None, "text": "",
                       "reveal": 0, "done": False,
                       "poll": time.monotonic() + 0.4}
        try:
            body = json.dumps({"page": page, "sub": sub,
                               "context": context}).encode()
            req = urllib.request.Request(
                "%s/tt/invent" % self.broadcaster, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                self.invent["job"] = json.load(r).get("id")
        except (OSError, ValueError):
            self.invent = None

    def _invent_tick(self):
        inv = self.invent
        now = time.monotonic()
        if now < inv["poll"]:
            return
        inv["poll"] = now + 0.25
        if inv["job"] and not inv["done"]:
            try:
                with urllib.request.urlopen(
                        "%s/tt/invent?id=%s" % (self.broadcaster, inv["job"]),
                        timeout=5) as r:
                    d = json.load(r)
                inv["text"] = d.get("text") or ""
                inv["done"] = bool(d.get("done"))
            except (OSError, ValueError):
                inv["done"] = True
        # The reveal is throttled independently of how fast the model runs:
        # the point is a page painting itself in, left to right, the way a
        # teletext decoder filled one as the lines arrived.
        if inv["reveal"] < len(inv["text"]):
            inv["reveal"] = min(len(inv["text"]), inv["reveal"] + 48)
            if self.visible:
                self.repaint()
        elif inv["done"] and inv["reveal"] >= len(inv["text"]):
            self._pcache.pop((inv["page"], inv.get("sub", 1), self.sel), None)
            self._pcache.pop((inv["page"], 1, self.sel), None)
            if self.visible:
                self.repaint()

    def _page_invent(self):
        inv = self.invent
        pg = blank_page()
        header(pg, self.page, None)
        shown = inv["text"][:inv["reveal"]]
        lines = shown.split("\n")
        if not inv["text"]:
            put(pg, 8, 2, alpha(YELLOW) +
                T("Diese Seite wird Ihnen gleich"))
            put(pg, 9, 2, alpha(YELLOW) + T("ausgestrahlt."))
            put(pg, 12, 2, alpha(CYAN) + T("Bitte haben Sie einen Moment"))
            put(pg, 13, 2, alpha(CYAN) + T("Geduld."))
            return pg
        put(pg, 2, 2, alpha(YELLOW) + DH + T(lines[0][:20]))
        r = 5
        for line in lines[1:]:
            if r > 21:
                break
            if line.strip():
                put(pg, r, 2, alpha(WHITE) + T(line[:36]))
            r += 1
        if inv["done"] and inv["reveal"] >= len(inv["text"]):
            put(pg, 22, 2, alpha(CYAN) + T("vom Ger{t erfunden"
                                           .replace("{", chr(0xE4))))
        return pg

    def _photo_codes(self):
        """{digits: picture} from the broadcaster, fetched once."""
        if self._photos[0] > time.monotonic():
            return self._photos[1]
        codes = {}
        try:
            with urllib.request.urlopen(
                    "%s/tt/photos" % self.broadcaster, timeout=5) as r:
                got = json.load(r).get("photos") or {}
            codes = {str(k): v for k, v in got.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            pass
        self._photos = (time.monotonic() + 300, codes)
        return codes

    def show_photo(self, name: str, caption: str = "", crop: str = "",
                   sound: str = ""):
        """A picture the broadcaster has quantised down to mosaics."""
        q = "name=%s&caption=%s" % (urllib.parse.quote(name, safe=""),
                                    urllib.parse.quote(caption, safe=""))
        if crop:
            q += "&crop=" + urllib.parse.quote(crop, safe="")
        try:
            with urllib.request.urlopen(
                    "%s/tt/photo?%s" % (self.broadcaster, q), timeout=20) as r:
                obj = json.load(r)
            raw = base64.b64decode(obj["data"])
            if len(raw) != 960:
                raise ValueError("short page")
        except (OSError, ValueError, KeyError):
            self.err = "kein Bild"
            self.repaint()
            return
        self.photo = [bytearray(raw[i * 40:(i + 1) * 40]) for i in range(24)]
        self.visible = True
        self.repaint()
        if sound:
            # /etc/retrokb/<sound>.wav wins if it exists, otherwise the
            # built-in flourish
            self._sound(sound)

    def _live_target(self, tgt: dict) -> str:
        """The URL to hand mpv for a channel entry.

        Channels with a `ch` key are resolved by the broadcaster at zap time
        rather than baked into the page: ZDF's manifest path rotates and its
        player token expires, so a URL stored in the page would go stale and
        the channel would simply stop working one day."""
        ch = tgt.get("ch")
        if ch:
            # radio is transcoded like music (it gets the analyser as its
            # picture); TV is a straight redirect to the broadcaster's manifest
            kind = "radio" if tgt.get("radio") else "live"
            return "%s/%s?ch=%s" % (self.broadcaster, kind,
                                    urllib.parse.quote(str(ch), safe=""))
        return tgt.get("play", "")

    def play_live(self, url: str, name: str = "", epg_id: str = "",
                  key: str = "", page: int = 0):
        """Tune a live stream. Deliberately NOT routed through _play(): there
        is no server path to map, no probe verdict to consult, no runtime to
        resume into, and 'watched to the end' is meaningless for something
        that never ends."""
        self.saver.deactivate()
        self._saver_check_at = time.monotonic() + 1.5
        self.play_ctx = None
        self.last_remote = None
        self.live = url
        self.live_name = name
        self.live_epg = epg_id
        self.live_key = key
        self.live_page = page or self.live_page
        self.music = None
        self.mixmode = False
        self._epg = (0.0, None, None)
        self.pipe_offset = 0
        self._resume = None
        self.tv_cmd(["loadfile", url, "replace"])
        self.tv_cmd(["set_property", "pause", False])
        # Radio keeps the OS ON SCREEN, in MIX, exactly like local music: the
        # analyser is the picture, and a picture of bars alone does not tell
        # you which station you are listening to. Television hides it -- there
        # the programme IS the picture.
        radio = (self.live_page == P_RADIO)
        self.mixmode = radio
        self.visible = radio
        self.entry = ""
        if radio:
            self.page, self.sub = P_NOW, 1
        self.show_loading(name or "Live")

    def _local_entry(self, remote_path: str) -> str:
        """Broadcaster links are server paths; decide NFS-direct vs GPU
        stream using the same verdicts tvplayer used (probe cache)."""
        if self._needs_tc is None:
            self._needs_tc = {}
            try:
                with open(self.probe_cache, encoding="utf-8") as fh:
                    for k, info in json.load(fh).items():
                        p = k.split("|", 1)[0]
                        v = (info.get("v") != "h264"
                             or info.get("pix") not in (None, "yuv420p")
                             or info.get("a") in ("dts", "truehd")
                             or (info.get("height") or 0) > 720)
                        self._needs_tc[p] = v
            except (OSError, ValueError):
                pass
        local = remote_path
        for rr, lr in self.remote_roots.items():
            if remote_path.startswith(rr):
                local = lr + remote_path[len(rr):]
                break
        if self._needs_tc.get(local):
            return "%s/stream?path=%s" % (
                self.broadcaster, urllib.parse.quote(remote_path, safe="/"))
        return local

    def _play(self, remote_path: str, ctx=None):
        self.live = None
        self.live_name = ""
        self.live_key = ""
        self.music = None
        self.mixmode = False
        self.saver.deactivate()
        self._saver_check_at = time.monotonic() + 1.5
        self.play_ctx = ctx
        self.last_remote = remote_path
        self.pipe_offset = 0
        self._resume = None      # a fresh choice replaces the suspended film
        self.tv_cmd(["loadfile", self._local_entry(remote_path), "replace"])
        # pause is a GLOBAL mpv property, not per file: one stray play/pause
        # (ENTER on the black carrier does it invisibly) and every film loaded
        # afterwards starts paused on a black screen. Clear it explicitly.
        self.tv_cmd(["set_property", "pause", False])
        title = (self._desc_for_current() or {}).get("title") \
            or clean_title(remote_path)
        self.visible = False
        self.entry = ""
        self.show_loading(title)

    def replay(self):
        """Clean restart of the current title. On a live transcode pipe a
        seek would force a stream reopen (this is what killed a running
        movie once); reloading the entry restarts it safely everywhere."""
        if self.last_remote:
            self.tv_cmd(["loadfile", self._local_entry(self.last_remote),
                         "replace"])
        else:
            self.tv_cmd(["seek", 0, "absolute"])

    @staticmethod
    def _parse_time(e: str) -> int:
        if len(e) <= 2:
            return int(e) * 60              # "31" -> 31 minutes
        if len(e) <= 4:
            return int(e[:-2]) * 60 + int(e[-2:])          # MMSS
        return (int(e[:-4]) * 3600 + int(e[-4:-2]) * 60
                + int(e[-2:]))                              # HMMSS

    def _commit_seek(self):
        secs = self._parse_time(self.entry)
        self.entry = ""
        dur = self._runtime()
        if dur:
            if secs > int(dur):
                self.err = "zu weit"
                self.repaint()
                return
            secs = max(0, min(secs, int(dur) - 3))
        # the scrub target IS the new resume point: commit it directly and
        # drop the suspended-film record so hide() does not double-restore
        path, pos = (self._resume or
                     (self.tv_query("path") or "", None))
        self._resume = None
        if path.startswith("http"):
            # a live transcode pipe cannot seek -- ask the gateway to START
            # the encode at the target instead (one reload, one position)
            import re
            base = re.sub(r"&start=\d+", "", path)
            self.pipe_offset = secs
            self.tv_cmd(["loadfile", "%s&start=%d" % (base, secs), "replace"])
        elif path and not path.startswith("av://"):
            self.tv_cmd(["loadfile", path, "replace", -1, "start=%d" % secs])
        self.hide()

    def _ctx_items(self, page, sub):
        got = self._fetch(page, sub)
        if got is None:
            return None, 0
        links = (got[1] or {}).get("links", {})
        items = links.get("items", links) or {}
        plays = {int(k): v["play"] for k, v in items.items()
                 if isinstance(v, dict) and "play" in v}
        return plays, int((got[1] or {}).get("subs", 1))

    def play_step(self, step: int):
        """Next/previous title within the list the current one came from,
        crossing subpage boundaries."""
        if not self.play_ctx:
            if step < 0:
                self.replay()
            return
        page, sub, idx = self.play_ctx
        plays, subs = self._ctx_items(page, sub)
        if plays is None:
            return
        target = idx + step
        if target in plays:
            self._play(plays[target], (page, sub, target))
            return
        nsub = sub + (1 if step > 0 else -1)
        if 1 <= nsub <= subs:
            plays2, _ = self._ctx_items(page, nsub)
            if plays2:
                t = min(plays2) if step > 0 else max(plays2)
                self._play(plays2[t], (page, nsub, t))

    # -- OS state ----------------------------------------------------------

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump({"random": self.random}, fh)
        except OSError as exc:
            self.log.warning("cannot persist OS state: %s", exc)

    def set_random(self, on: bool):
        self.random = on
        self._save_state()
        if on:
            self.tv_cmd(["loadlist", self.playlist_path, "replace"])
            self.tv_cmd(["set_property", "loop-playlist", "inf"])
        else:
            self.tv_cmd(["loadfile", CARRIER, "replace", -1, "loop-file=inf"])

    def sendeschluss(self):
        """Drop the film entirely; hold the dead channel. The OS comes up
        via the carrier's start-file event if it is not up already."""
        self._resume = None
        self.play_ctx = None
        self.pipe_offset = 0
        self.tv_cmd(["loadfile", CARRIER, "replace", -1, "loop-file=inf"])

    def on_carrier(self):
        """The dead channel started. At boot that means the SABA logo
        drifts until someone presses a key; after a film ends it means the
        OS comes up asking what to play next."""
        if self.visible:
            return
        if not self._boot_carrier_seen:
            self._boot_carrier_seen = True
            self.update_saver()
        else:
            self.show()

    # -- local dynamic pages ----------------------------------------------

    NOW_SUB1_LINES = 6          # description lines on the status subpage
    NOW_SUBN_LINES = 17         # ...and on each continuation subpage

    def _now_desc_lines(self):
        obj = self._desc_for_current() or {}
        text = obj.get("text") or ""
        return self._wrap(text) if text else []

    def now_subs(self) -> int:
        if self.live:
            cur, nxt = self.epg()
            return 1 + (1 if cur else 0) + (1 if nxt else 0)
        return self._now_subs_film()

    def _now_subs_film(self) -> int:
        """Subpages needed: status page, then the description spilling over --
        the same mechanism a real service used for long articles."""
        extra = max(0, len(self._now_desc_lines()) - self.NOW_SUB1_LINES)
        return 1 + (extra + self.NOW_SUBN_LINES - 1) // self.NOW_SUBN_LINES

    def _page_now(self):
        pg = blank_page()
        subs = self.now_subs()
        header(pg, P_NOW, self.sub if subs > 1 else None)
        path = self.tv_query("path") or ""
        on_air = self._resume or self._on_air()
        obj = self._desc_for_current() or {}
        lines = self._now_desc_lines()

        if self.live and self.sub > 1:
            cur, nxt = self.epg()
            show = cur if self.sub == 2 else nxt
            put(pg, 2, 2, alpha(YELLOW) + DH + T("SENDUNG"))
            if not show:
                put(pg, 6, 2, alpha(RED) + T("Keine Daten"))
                return pg
            r = 5
            for line in self._wrap(show["title"], 37)[:2]:
                put(pg, r, 2, alpha(YELLOW) + T(line))
                r += 1
            put(pg, r, 2, alpha(CYAN) + T("%s - %s" % (self._clock(show["start"]),
                                                       self._clock(show["end"]))))
            r += 2
            if show.get("subline"):
                for line in self._wrap(show["subline"], 37)[:2]:
                    put(pg, r, 2, alpha(CYAN) + T(line))
                    r += 1
                r += 1
            text = (show.get("synopsis") or "").replace("*", "-")
            for line in self._wrap(text, 37)[:21 - r]:
                put(pg, r, 2, alpha(WHITE) + T(line))
                r += 1
            put(pg, 23, 0, alpha(CYAN) + T("111 = zur}ck".replace("}", chr(0xFC))))
            return pg

        if self.sub > 1:
            # continuation subpage: description only
            put(pg, 2, 2, alpha(YELLOW) + T((obj.get("title") or "")[:36]))
            base = self.NOW_SUB1_LINES + (self.sub - 2) * self.NOW_SUBN_LINES
            r = 4
            for line in lines[base:base + self.NOW_SUBN_LINES]:
                put(pg, r, 2, alpha(WHITE) + T(line))
                r += 1
            put(pg, 23, 0, alpha(CYAN) + T(".=weiter  111=Anfang"))
            return pg

        put(pg, 2, 2, alpha(YELLOW) + DH + T("JETZT L[UFT".replace("[", "Ä")))

        # live first: a stream has no runtime, no resume point and no
        # "completed" state, so none of the film logic below applies to it
        if self.live:
            cur, nxt = self.epg()
            put(pg, 4, 2, alpha(YELLOW) +
                T((self.live_name or "Live-Fernsehen")[:36]))
            if cur:
                r = 6
                for line in self._wrap(cur["title"], 36)[:2]:
                    put(pg, r, 2, alpha(WHITE) + T(line))
                    r += 1
                put(pg, r + 1, 2, alpha(CYAN) +
                    T("l{uft seit ".replace("{", chr(0xE4)) + self._clock(cur["start"])))
                if nxt:
                    put(pg, 12, 2, alpha(CYAN) + T("als n{chstes:".replace(
                        "{", chr(0xE4))))
                    r = 13
                    for line in self._wrap(nxt["title"], 36)[:2]:
                        put(pg, r, 2, alpha(WHITE) + T(line))
                        r += 1
                    put(pg, r, 2, alpha(CYAN) +
                        T("f{ngt um ".replace("{", chr(0xE4)) +
                          self._clock(nxt["start"]) + " an"))
                put(pg, 18, 2, alpha(GREEN) + T("1") + alpha(WHITE) +
                    T(" Beschreibung"))
                if nxt:
                    put(pg, 19, 2, alpha(GREEN) + T("2") + alpha(WHITE) +
                        T(" Beschreibung n{chste Sendung".replace("{", chr(0xE4))))
            elif self.live_page == P_RADIO:
                title = self.nowplaying()
                if title:
                    r = 7
                    for line in self._wrap(title, 36)[:3]:
                        put(pg, r, 2, alpha(WHITE) + T(line))
                        r += 1
                else:
                    put(pg, 7, 2, alpha(CYAN) + T("Keine Titelangabe"))
                put(pg, 12, 2, alpha(CYAN) +
                    T("l{uft seit ".replace("{", chr(0xE4))
                      + self._hms(self.tv_query("time-pos") or 0)))
            elif self.live_epg:
                put(pg, 7, 2, alpha(CYAN) + T("Programmdaten nicht verf}gbar"
                                              .replace("}", chr(0xFC))))
            else:
                put(pg, 7, 2, alpha(CYAN) + T("Kein Programmf}hrer".replace(
                    "}", chr(0xFC))))
                put(pg, 9, 2, alpha(CYAN) + T("l{uft seit ".replace("{", chr(0xE4))
                                              + self._hms(self.tv_query("time-pos") or 0)))
            put(pg, 21, 2, alpha(CYAN) +
                T("%d = Senderliste" % self.live_page))
            return pg

        if not on_air:
            put(pg, 6, 2, alpha(WHITE) + T("Sendepause"))
            put(pg, 8, 2, alpha(CYAN) + T("Zahl + ENTER w{hlt eine Seite".replace(
                "{", chr(0xE4))))
            put(pg, 9, 2, alpha(CYAN) + T("100 = Startseite"))
            return pg
        title = obj.get("title") or clean_title(self._remote_of_current() or "")
        put(pg, 5, 2, alpha(WHITE) + T(title[:30]))
        if obj.get("rating"):
            put(pg, 5, 33, alpha(YELLOW) + T("%.1f" % obj["rating"]))
        if self._resume:
            path = self._resume[0]        # suspended: the carrier is on air
        pos, dur = self._film_pos(), self._runtime()
        if dur:
            put(pg, 7, 2, alpha(CYAN) + T("%s / %s" % (self._hms(pos),
                                                       self._hms(dur))))
            filled = max(0, min(30, int((pos / dur) * 30)))
            put(pg, 8, 2, mosaic(GREEN) + b"\x7f" * filled +
                mosaic(BLUE) + b"\x23" * (30 - filled))
        else:
            put(pg, 7, 2, alpha(CYAN) + T(self._hms(pos)))
        src = "GPU-Transkode" if path.startswith("http") else "Direkt"
        put(pg, 9, 2, alpha(CYAN) + T("Quelle: ") + alpha(WHITE) + T(src))

        if lines:
            r = 11
            for line in lines[:self.NOW_SUB1_LINES]:
                put(pg, r, 2, alpha(WHITE) + T(line))
                r += 1
            if subs > 1:
                put(pg, 18, 2, alpha(CYAN) + T(". = weiterlesen (%d Seiten)" % subs))
        if self.seek_armed:
            if dur:
                put(pg, 20, 2, alpha(GREEN) + T(
                    "Sprungzeit, max %s  (3143=31:43)" % self._hms(dur)))
            else:
                put(pg, 20, 2, alpha(GREEN) + T("Sprungzeit: 3143 = 31:43"))
        else:
            put(pg, 20, 2, alpha(CYAN) + T("ENTER = Zeit springen"))
        return pg

    @staticmethod
    def _wrap(text: str, width: int = 37):
        """Greedy wrap to the teletext column width."""
        words, out, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 > width:
                out.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            out.append(cur)
        return out

    def _on_air(self) -> bool:
        """Is something real playing (not the dead channel)?"""
        path = self.tv_query("path") or ""
        return bool(path) and not path.startswith("av://")

    def _remote_of_current(self):
        """Server-side path of whatever is playing (or suspended), which is
        the key the broadcaster indexes descriptions by."""
        path = self.tv_query("path") or ""
        if not path or path.startswith("av://"):
            if self._resume:
                path = self._resume[0]
            else:
                return ""
        if path.startswith("http"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            return q.get("path", [""])[0]
        for rr, lr in self.remote_roots.items():
            if path.startswith(lr):
                return rr + path[len(lr):]
        return path

    @staticmethod
    def _hms(secs) -> str:
        secs = max(0, int(secs))
        h, m, sec = secs // 3600, secs % 3600 // 60, secs % 60
        return ("%d:%02d:%02d" % (h, m, sec)) if h else ("%d:%02d" % (m, sec))

    def _film_pos(self):
        """Position in the FILM. While the OS is open the film is suspended
        and mpv is playing the carrier, whose clock is meaningless -- the
        suspension record is the truth then."""
        if self._resume:
            return self._resume[1]
        pos = self.tv_query("time-pos") or 0
        if (self.tv_query("path") or "").startswith("http"):
            pos += self.pipe_offset
        return int(pos)

    def _runtime(self):
        """Runtime of the FILM. mpv's duration is only trustworthy while the
        film actually plays: with the OS open it reports the dead channel's
        (~21s), which silently refused every jump as 'zu weit'. Live
        transcode pipes report none at all. The broadcaster ffprobed the real
        file, so that is the fallback in both cases."""
        if not self._resume and self._on_air():
            d = self.tv_query("duration")
            if d and d > 60:        # a real film, not the carrier
                return int(d)
        return int((self._desc_for_current() or {}).get("duration") or 0)

    def _desc_for_current(self):
        remote = self._remote_of_current()
        if not remote:
            return None
        if self._desc[0] == remote:
            return self._desc[1]
        obj = None
        try:
            with urllib.request.urlopen(
                    "%s/tt/desc?path=%s" % (self.broadcaster,
                                            urllib.parse.quote(remote, safe="/")),
                    timeout=8) as r:
                obj = json.load(r)
        except (OSError, ValueError):
            obj = None
        self._desc = (remote, obj)
        return obj

    SET_MENU = [
        ("1", "Zufallsmodus"),
        ("2", "Bild neu aufbauen"),
        ("3", "Seiten neu laden"),
        ("4", "Server pr}fen".replace("}", chr(0xFC))),
        ("5", "Erfundene Seiten l|schen".replace("|", chr(0xF6))),
        ("9", "Raspberry Pi pr}fen".replace("}", chr(0xFC))),
        ("0", "Bluetooth koppeln"),
        ("6", "TelecommanderOS neu"),
        ("7", "Raspberry Pi neu starten"),
        ("8", "Fernseher ausschalten"),
    ]

    P_STATUS = 501
    P_BLUE = 502

    @staticmethod
    def _pi_health():
        """Voltage, heat and throttling, straight from the firmware.

        `throttled` is a bitmask and the two halves mean different things:
        the low bits are what is happening NOW, bits 16-19 are what has
        happened at any point since boot. A set history bit with a clear
        current bit means the power was bad earlier -- worth showing, but not
        the same as a problem right now.
        """
        def vcgen(what):
            try:
                out = subprocess.run(["vcgencmd", what], capture_output=True,
                                     text=True, timeout=5).stdout.strip()
                return out.split("=", 1)[1] if "=" in out else out
            except (OSError, subprocess.TimeoutExpired, IndexError):
                return ""
        rows = []
        t = vcgen("measure_temp").replace("'C", " C")
        rows.append(("Temperatur", t or "?", GREEN if t and
                     float(t.split()[0] or 0) < 70 else YELLOW))
        volt = vcgen("measure_volts core")
        rows.append(("Kernspannung", volt or "?", WHITE))
        raw = vcgen("get_throttled")
        try:
            bits = int(raw, 16)
        except ValueError:
            bits = -1
        if bits < 0:
            rows.append(("Stromversorgung", "unbekannt", YELLOW))
        else:
            now_bad = bits & 0xF
            ever_bad = (bits >> 16) & 0xF
            rows.append(("Spannung jetzt",
                         "zu niedrig" if bits & 0x1 else "in Ordnung",
                         RED if bits & 0x1 else GREEN))
            rows.append(("Drosselung jetzt",
                         "ja" if now_bad & 0xC else "nein",
                         RED if now_bad & 0xC else GREEN))
            rows.append(("Seit dem Start",
                         "schon mal zu wenig" if ever_bad & 0x1 else "sauber",
                         YELLOW if ever_bad & 0x1 else GREEN))
        try:
            up = float(open("/proc/uptime").read().split()[0])
            rows.append(("Laufzeit", "%d h %d min" % (up // 3600,
                                                      (up % 3600) // 60), WHITE))
        except (OSError, ValueError):
            pass
        try:
            with open("/proc/loadavg") as fh:
                rows.append(("Last", fh.read().split()[0], WHITE))
        except OSError:
            pass
        return rows

    def _page_status(self):
        pg = blank_page()
        header(pg, self.page)
        put(pg, 2, 2, alpha(YELLOW) + DH + T("RASPBERRY PI"))
        r = 6
        for label, value, colour in self._pi_health():
            put(pg, r, 2, alpha(CYAN) + T(label))
            put(pg, r, 22, alpha(colour) + T(str(value)[:16]))
            r += 2
        put(pg, 21, 2, alpha(CYAN) + T("500 = Einstellungen"))
        return pg

    # -- bluetooth ---------------------------------------------------------
    def _bt(self, *args, timeout=8):
        try:
            return subprocess.run(["bluetoothctl"] + list(args),
                                  capture_output=True, text=True,
                                  timeout=timeout).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def bt_scan(self):
        """Look for anything advertising nearby, in the background.

        bluetoothctl blocks for the whole scan, and the interface must not,
        so it runs on a thread and the page shows what has turned up so far.
        """
        if self._bt_busy:
            return
        self._bt_busy = True

        def _run():
            try:
                self._bt("--timeout", "10", "scan", "on", timeout=20)
                self._bt_seen = self._bt_devices()
            finally:
                self._bt_busy = False
        threading.Thread(target=_run, daemon=True).start()

    def _bt_devices(self):
        out = []
        for line in self._bt("devices").splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 3 and parts[0] == "Device":
                out.append((parts[1], parts[2][:24]))
        return out[:9]

    def _page_bluetooth(self):
        pg = blank_page()
        header(pg, self.page)
        put(pg, 2, 2, alpha(YELLOW) + DH + T("BLUETOOTH"))
        info = self._bt("show")
        powered = "Powered: yes" in info
        if not info:
            put(pg, 6, 2, alpha(RED) + T("Kein Adapter gefunden."))
            put(pg, 8, 2, alpha(WHITE) + T("Der Stick steckt nicht oder"))
            put(pg, 9, 2, alpha(WHITE) + T("bekommt zu wenig Strom."))
            put(pg, 21, 2, alpha(CYAN) + T("500 = Einstellungen"))
            return pg
        put(pg, 5, 2, alpha(CYAN) + T("Adapter") +
            alpha(GREEN if powered else RED) +
            T("        " + ("an" if powered else "aus")))
        devs = self._bt_seen or self._bt_devices()
        r = 7
        for i, (mac, name) in enumerate(devs):
            sel = str(i + 1) == self._bt_sel
            mark = (alpha(WHITE) + NEWBG + alpha(BLACK)) if sel else alpha(WHITE)
            put(pg, r, 1, mark + T((" %d %s " % (i + 1, name))[:36]))
            r += 1
        if not devs:
            put(pg, 8, 2, alpha(CYAN) +
                T("Suche l{uft ...".replace("{", chr(0xE4)) if self._bt_busy
                  else "Nichts gefunden."))
        put(pg, 18, 2, alpha(GREEN) + T("1-9") + alpha(WHITE) + T(" ausw{hlen"
                                                                 .replace("{", chr(0xE4))))
        put(pg, 19, 2, alpha(GREEN) + T("ENTER") + alpha(WHITE) +
            T(" koppeln und verbinden"))
        put(pg, 20, 2, alpha(GREEN) + T("0") + alpha(WHITE) + T(" neu suchen"))
        if self._bt_msg:
            put(pg, 22, 2, alpha(YELLOW) + T(self._bt_msg[:36]))
        return pg

    def bt_pair(self, idx):
        devs = self._bt_seen or self._bt_devices()
        if not (0 <= idx < len(devs)):
            return
        mac, name = devs[idx]
        self._bt_msg = "koppelt %s ..." % name[:20]
        self.repaint()

        def _run():
            self._bt("pair", mac, timeout=25)
            self._bt("trust", mac, timeout=10)
            out = self._bt("connect", mac, timeout=25)
            ok = "successful" in out.lower() or "Connected: yes" in out
            self._bt_msg = ("%s verbunden" if ok else "%s ging nicht") % name[:18]
            if self.visible:
                self.repaint()
        threading.Thread(target=_run, daemon=True).start()

    def _page_settings(self):
        pg = blank_page()
        header(pg, P_SET)
        put(pg, 2, 2, alpha(YELLOW) + DH + T("EINSTELLUNGEN"))
        r = 5
        for num, label in self.SET_MENU:
            put(pg, r, 1, alpha(CYAN) + T(" " + num) +
                alpha(WHITE) + T(" " + label))
            if num == "1":
                put(pg, r, 30, (alpha(GREEN) if self.random else alpha(RED)) +
                    T("AN" if self.random else "AUS"))
            if num == "7" and self._reboot_armed:
                put(pg, r, 30, alpha(RED) + T("7 = JA"))
            if num == "8" and self._tvoff_armed:
                put(pg, r, 30, alpha(RED) + T("8 = JA"))
            r += 2
        if self._set_msg:
            for i, line in enumerate(self._wrap(self._set_msg)[:2]):
                put(pg, 22 + i, 2, alpha(GREEN) + T(line))
        put(pg, 21, 2, alpha(CYAN) + T("Nr + ENTER"))
        return pg

    def _settings_action(self, n: str) -> str:
        """Maintenance from the couch. Everything here is a recovery tool for
        the failure modes this box actually has: a stalled transcode pipe, a
        stale page cache, an unreachable broadcaster, a wedged renderer."""
        if n != "7":
            self._reboot_armed = False
        if n != "8":
            self._tvoff_armed = False

        if n == "1":
            self.set_random(not self.random)
            return "Zufallsmodus %s" % ("AN" if self.random else "AUS")

        if n == "2":
            # the stall cure: drops the current transcode pipe and rebuilds
            # the whole player (mpv + its DRM surface) from scratch
            self._resume = None
            self.pipe_offset = 0
            subprocess.Popen(["systemctl", "restart", "tvplayer"])
            return "Player wird neu aufgebaut ..."

        if n == "3":
            self._pcache.clear()
            self._list_cache = (0.0, [])
            self._desc = (None, None)
            try:
                urllib.request.urlopen(self.broadcaster + "/tt/scan",
                                       data=b"", timeout=6).read()
                return "Seiten geleert, Server scannt neu"
            except OSError:
                return "Seiten geleert (Server nicht erreichbar)"

        if n == "4":
            try:
                with urllib.request.urlopen(self.broadcaster + "/tt/status",
                                            timeout=6) as r:
                    st = json.load(r)
                return "OK: %d Seiten, %d Titel, Scan %s" % (
                    st.get("pages", 0), st.get("jellyfin_items", 0),
                    "laeuft" if st.get("scan_running") else "fertig")
            except (OSError, ValueError) as exc:
                return "Server antwortet nicht (%s)" % type(exc).__name__

        if n == "5":
            # only the pages the set made up for itself; the 666 archive it
            # has been writing stays where it is
            self._pcache.clear()
            self.invent = None
            try:
                with urllib.request.urlopen(
                        self.broadcaster + "/tt/purge-invented",
                        data=b"", timeout=10) as r:
                    n_gone = json.load(r).get("purged", 0)
                return "%d erfundene Seiten geloescht" % n_gone
            except (OSError, ValueError):
                return "Server nicht erreichbar"

        if n == "6":
            subprocess.Popen(["systemctl", "restart", "retrokb"])
            return "OS startet neu ..."

        if n == "9":
            self._go(self.P_STATUS)
            return ""

        if n == "0":
            self._bt_seen, self._bt_sel, self._bt_msg = [], "", "sucht ..."
            self.bt_scan()
            self._go(self.P_BLUE)
            return ""

        if n == "7":
            if not self._reboot_armed:
                self._reboot_armed = True
                return "Sicher? 7 nochmal = Neustart"
            subprocess.Popen(["systemctl", "reboot"])
            return "Pi startet neu ..."

        if n == "8":
            # the ONLY way to cut the TV's power from the remote, and it
            # takes two presses -- a numpad key can wake the set but must
            # never be able to kill it mid-film by accident
            if not self._tvoff_armed:
                self._tvoff_armed = True
                return "Sicher? 8 nochmal = Fernseher aus"
            if self.tv_power:
                self.tv_power("tv_off")
                return "Fernseher wird ausgeschaltet"
            return "Keine Verbindung zu Home Assistant"

        return "Unbekannt: %s" % n

    def _page_offair(self):
        pg = blank_page()
        header(pg, self.page)
        put(pg, 8, 4, alpha(RED) + DH + T("KEIN SENDERSIGNAL"))
        put(pg, 12, 4, alpha(WHITE) + T("Telecommander nicht erreichbar"))
        put(pg, 14, 4, alpha(CYAN) + T("Lokal: 111 Jetzt, 500 Optionen"))
        return pg

    # -- compose + output --------------------------------------------------

    def _build(self):
        if self.photo:
            return [bytearray(r) for r in self.photo]
        if self.snake:
            return self._page_snake()
        if self._rand_ask:
            # keep _meta as it was: answering "ja" picks from THIS page's list
            pg = self._page_random_ask()
            hint_row(pg, self.page)
            return pg
        self._meta = {"subs": 1, "links": {}}
        if self.music and self.page in (P_NOW, P_DESC):
            self._meta["subs"] = 1
            pg = self._page_music()
            hint_row(pg, self.page)
            return pg
        if (self.page in (P_NOW, P_DESC) and not self.live
                and not self._resume and not self._on_air()):
            # THE single guard: "Jetzt laeuft" is meaningless with a dead
            # channel behind the pages, and several routes can land on it
            # (the now key, a stale page from an earlier film, coming back
            # from a game). Correcting it here catches all of them at once
            # rather than patching each entry point. Not _go(): a page that
            # should never have been shown does not belong in the history.
            self.page, self.sub = 100, 1
        if self.page == P_NOW:
            self._meta["subs"] = self.now_subs()
            pg = self._page_now()
        elif self.page == P_DESC:
            self.page = P_NOW          # 112 folded into 111's subpages
            pg = self._page_now()
        elif self.page == P_SET:
            pg = self._page_settings()
        elif self.page == self.P_STATUS:
            pg = self._page_status()
        elif self.page == self.P_BLUE:
            pg = self._page_bluetooth()
        elif self.page in (P_CHAT, P_RPG):
            pg = self._page_chat()
        elif self.page == P_NEWS_AI:
            pg = self._page_news()
        else:
            got = self._fetch(self.page, self.sub)
            if got is None and self.sub > 1:
                base = self._fetch(self.page, self.sub - 1) \
                    or self._fetch(self.page, 1)
                if base is not None:
                    # the page exists but this subpage does not yet: spin it
                    # further from what is already standing there
                    key = (self.page, self.sub)
                    if not self.invent or (self.invent.get("page"),
                                           self.invent.get("sub")) != key:
                        self._invent_start(self.page, self.sub,
                                           self._rows_text(base[0]))
                    if self.invent:
                        self._meta = {"subs": self.sub, "links": {}}
                        return self._page_invent()
                self.sub = 1
                got = self._fetch(self.page, 1)
            if got is None:
                # nobody has claimed this number, so the set makes one up --
                # and the broadcaster keeps it, so it is a real page after
                if not self.invent or self.invent["page"] != self.page \
                        or self.invent.get("sub", 1) != 1:
                    self._invent_start(self.page)
                pg = self._page_invent() if self.invent else self._page_offair()
            else:
                pg, self._meta = [bytearray(r) for r in got[0]], got[1]
                spec = self._anim_spec()
                if spec:
                    self._anim_apply(pg, spec)
                self._paint_sel(pg)
                if self.page == P_CREEP:
                    creep_header(pg, self.page, self.sub)
                else:
                    header(pg, self.page,
                           self.sub if self._meta["subs"] > 1 else None)
        if self.entry:
            # white on green: Level 1 teletext has no "alpha black" code, so
            # the old black-text attempt left green-on-green -- an invisible
            # entry line, faithfully rendered
            put(pg, 22, 2, alpha(GREEN) + NEWBG + alpha(WHITE) +
                T(" %s_ " % self.entry))
        if self.err:
            put(pg, 22, 12, alpha(RED) + T(self.err))
            self.err = None
        hint_row(pg, self.page)
        return pg

    ANIM_FPS = 5.0

    def _anim_apply(self, pg, spec):
        """Move something on an otherwise static page.

        Everything here works on the 40x24 CHARACTER grid and touches only a
        few rows per frame, because a full repaint costs ~90 ms -- five frames
        a second is the ceiling, which happens to be exactly right for
        something slowly seeping down a screen.
        """
        kind = str(spec.get("kind", "flicker"))
        col = int(spec.get("colour", RED)) & 7
        phase = self._anim[0]
        if kind == "fill":                       # rises from the bottom
            top = 23 - int(phase * 20)
            for r in range(max(3, top), 24):
                pg[r][0] = 0x10 + col
                for c in range(1, 40):
                    if r == max(3, top) and (c * 7 + r * 13) % 4 == 0:
                        continue                 # ragged surface, not a line
                    pg[r][c] = SNAKE_BLOCK
        elif kind == "drip":                     # runs down from the top
            for c in range(1, 39):
                if (c * 37) % 5:
                    continue
                run = int((((c * 29) % 13) / 13.0 + phase * 1.6) * 15) % 18
                for r in range(2, min(23, 2 + run)):
                    pg[r][0] = 0x10 + col
                    pg[r][c] = SNAKE_BLOCK
        elif kind == "sweep":                    # a bright line crawls down
            r = 3 + int(phase * 19)
            if 3 <= r < 23:
                pg[r][0] = 0x10 + col
                for c in range(1, 40):
                    pg[r][c] = SNAKE_BLOCK
        elif kind == "eyes":                     # something blinks at you
            if phase % 0.5 < 0.34:
                for r, c in ((7, 12), (7, 26), (14, 8), (14, 31), (18, 19)):
                    pg[r][c - 1] = 0x10 + col
                    pg[r][c] = 0x7F
                    pg[r][c + 1] = 0x20
        else:                                    # flicker: cells come and go
            rnd = random.Random(int(phase * 40))
            for _ in range(18):
                r, c = rnd.randint(3, 22), rnd.randint(1, 38)
                pg[r][c] = rnd.choice((0x7F, 0x35, 0x2A, 0x6B, 0x20))
                pg[r][0] = 0x10 + col
        return pg

    def _anim_spec(self):
        spec = (self._meta or {}).get("anim")
        return spec if isinstance(spec, dict) else None

    def _paint(self, pg, w, h):
        """Pixels for a page.

        Painting 1280x720 through the character generator costs this Pi about
        a quarter of a second -- long enough to feel like lag on every
        keypress. The broadcaster does the identical work in single-digit
        milliseconds and sends it back zlib'd (a teletext frame is nearly all
        flat colour, so 3.7 MB becomes ~58 KB), which is roughly seven times
        faster end to end including transfer and decompression.

        Local rendering stays the fallback and is NOT optional: drawing a page
        must never depend on the network. A failure backs off for a minute
        rather than retrying -- and paying the timeout -- on every repaint.
        """
        if time.monotonic() >= self._remote_paint:
            try:
                body = json.dumps({
                    "grid": base64.b64encode(
                        b"".join(bytes(r) for r in pg)).decode(),
                    "w": w, "h": h, "mix": self.mixmode,
                    "margins": list(self._margins()),
                }).encode()
                req = urllib.request.Request(
                    self.broadcaster + "/tt/render", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2.5) as r:
                    raw = int(r.headers.get("X-Raw-Length", "0"))
                    fb = zlib.decompress(r.read())
                if len(fb) == raw == w * h * 4:
                    return fb
                raise ValueError("frame was %d bytes, wanted %d"
                                 % (len(fb), w * h * 4))
            except Exception as exc:
                self.log.warning("remote paint unavailable (%s); "
                                 "rendering on the Pi for the next minute",
                                 exc)
                self._remote_paint = time.monotonic() + 60
        return self.chip.render(pg, w, h, self.mixmode, self._margins())

    def repaint(self):
        if not self.visible:
            return
        # The OS only ever displays on the carrier (1280x720), so the canvas
        # is FIXED. Querying the running video's size was a race: opening the
        # OS over an SD file could size the overlay to the old frame -- and
        # mpv crops overlays to the video frame, so a stale 1280-wide ghost
        # over a 720-wide episode showed as the half-page band in Martin's
        # photo. Fixed canvas kills the whole class.
        w, h = 1280, 720
        fb = self._paint(self._build(), w, h)
        self._hum_check()
        with open(self.shm, "wb") as fh:
            fh.write(fb)
        self.tv_cmd(["overlay-add", self.overlay_id, 0, 0, self.shm,
                     0, "bgra", w, h, w * 4])
        self._clock_at = time.monotonic() + (60 - time.localtime().tm_sec)

    def update_saver(self):
        """THE single authority for the drifting logo. It never infers from
        side state (that is how it ended up on top of films: _commit_seek and
        _play cleared _resume, and hide()'s else-branch read that as "nothing
        is playing"). It asks mpv what is on screen, right now, every time:
        logo ONLY on the dead channel with the OS dismissed."""
        if self.visible or self._loading:
            self.saver.deactivate()
            return
        path = self.tv_query("path") or ""
        if path.startswith("av://"):
            self.saver.activate()
        else:
            self.saver.deactivate()

    def show_loading(self, title: str):
        """A card for the gap between choosing something and seeing it.

        That gap is real: a GPU-transcoded film needs a few seconds for
        ffmpeg to spin up and mpv to buffer, and until now the screen just
        went black with no explanation. Drawn with the same chip and the
        same overlay slot as the pages -- it is simply not the OS, so
        `visible` stays False and the numpad still talks to the player.
        """
        pg = blank_page()
        header(pg, P_NOW)
        put(pg, 2, 2, alpha(YELLOW) + DH + T("BITTE WARTEN"))
        for i, line in enumerate(self._wrap(title, 36)[:2]):
            put(pg, 6 + i, 2, alpha(WHITE) + T(line))
        put(pg, 10, 2, alpha(CYAN) + T("wird geladen ..."))
        # a little mosaic barber-pole, purely so the screen looks alive
        put(pg, 13, 2, mosaic(BLUE) + b"\x7f" * 34)
        self._loading = (title, time.monotonic() + 30)
        self.tv_cmd(["overlay-remove", self.overlay_id])
        fb = self.chip.render(pg, 1280, 720, False, self._margins())
        with open(self.shm, "wb") as fh:
            fh.write(fb)
        self.tv_cmd(["overlay-add", self.overlay_id, 0, 0, self.shm,
                     0, "bgra", 1280, 720, 1280 * 4])

    def show_qr(self, url: str, caption: str = ""):
        """A scannable QR drawn straight into the framebuffer.

        Not built from mosaic characters on purpose: a teletext cell is 2x3
        sub-blocks whose sub-pixels are nowhere near square, so a code made
        of them comes out stretched and scanners choke. Drawing real squares
        into the BGRA buffer also lets us compensate for the 16:9 signal the
        Saba squeezes to 4:3 -- modules are drawn 4:3 WIDER than tall so they
        land square on the actual tube.
        """
        try:
            with urllib.request.urlopen(
                    "%s/tt/qr?url=%s" % (self.broadcaster,
                                         urllib.parse.quote(url, safe="")),
                    timeout=8) as r:
                d = json.load(r)
            rows = d["rows"]
        except (OSError, ValueError, KeyError):
            self.err = "QR nicht m|glich".replace("|", chr(0xF6))
            self.repaint()
            return

        W, H = 1280, 720
        pg = blank_page()
        header(pg, self.page, self.sub if self._meta.get("subs", 1) > 1 else None)
        put(pg, 2, 2, alpha(YELLOW) + DH + T("QR-CODE"))
        for i, line in enumerate(self._wrap(caption, 37)[:2]):
            put(pg, 5 + i, 2, alpha(WHITE) + T(line))
        put(pg, 22, 2, alpha(CYAN) + T("Mit dem Handy scannen"))
        put(pg, 23, 0, alpha(CYAN) + T("x = zur}ck".replace("}", chr(0xFC))))
        fb = bytearray(self.chip.render(pg, W, H, False, self._margins()))

        n = len(rows)
        quiet = 4
        total = n + 2 * quiet
        # 4:3 correction: the frame is squeezed horizontally on this tube
        mh = max(3, min(14, (H - 300) // total))
        mw = max(4, int(mh * 4 / 3))
        qw, qh = total * mw, total * mh
        x0, y0 = (W - qw) // 2, 250
        white = bytes((255, 255, 255, 255))
        black = bytes((0, 0, 0, 255))
        for ry in range(total):
            for py in range(y0 + ry * mh, y0 + (ry + 1) * mh):
                if not (0 <= py < H):
                    continue
                base = py * W * 4
                for rx in range(total):
                    inq = quiet <= rx < quiet + n and quiet <= ry < quiet + n
                    dark = inq and rows[ry - quiet][rx - quiet] == "1"
                    px = black if dark else white
                    off = base + (x0 + rx * mw) * 4
                    fb[off:off + mw * 4] = px * mw
        self._loading = None
        self.qr_on = True
        with open(self.shm, "wb") as fh:
            fh.write(bytes(fb))
        self.tv_cmd(["overlay-add", self.overlay_id, 0, 0, self.shm,
                     0, "bgra", W, H, W * 4])

    def clear_loading(self):
        if self._loading and not self.visible:
            self._loading = None
            self.tv_cmd(["overlay-remove", self.overlay_id])
        else:
            self._loading = None

    def qr_url(self):
        """The permalink the broadcaster attached to the page being read."""
        return (self._meta or {}).get("links", {}).get("qr", "") \
            or (self._meta or {}).get("qr", "")

    def _margins(self):
        ot, ob, ol, orr = self.overscan
        return (int(720 * ot / 100), int(720 * ob / 100),
                int(1280 * ol / 100), int(1280 * orr / 100))

    def show(self):
        self._loading = None
        self.saver.deactivate()
        if self.visible:
            return
        self.visible = True
        # Like a real TV's TEXT mode, the OS REPLACES the picture: the film
        # is suspended (position remembered) and the black carrier takes
        # over. This also guarantees a full-screen canvas -- overlays live
        # in video coordinates, and a cinemascope file's short frame once
        # left the OS rendered as a band instead of a page.
        path = self.tv_query("path") or ""
        if self.live:
            # a live stream has no position to come back to: remember the
            # channel, not a timestamp, and re-tune at the live edge on exit
            self._resume = ("LIVE", self.live, self.live_name,
                            self.live_epg, self.live_key)
            self.tv_cmd(["loadfile", CARRIER, "replace", -1, "loop-file=inf"])
            self._go(P_NOW)
            self.repaint()
            return
        if path and not path.startswith("av://"):
            pos = self.tv_query("time-pos") or 0
            if path.startswith("http"):
                pos += self.pipe_offset
            self._resume = (path, int(pos))
            self.tv_cmd(["loadfile", CARRIER, "replace", -1, "loop-file=inf"])
            # opening the OS over a running film is almost always "what is
            # this / jump / description" -- land on Jetzt laeuft directly.
            # The page you browsed before is one backspace away (history).
            self._go(P_NOW)
        self.repaint()

    def after_restart(self, page: int = 100):
        """The player was replaced under us (the emulator took DRM master and
        tvplayer was restarted afterwards). Nothing the old session knew is
        still loaded, so every reference to it -- resume point, tuned channel,
        loading card -- is stale and must go, or the first keypress would try
        to seek into a file that is no longer open. Come up clean on 100."""
        self._resume = None
        self.live = None
        self.live_name = ""
        self.live_epg = ""
        self.live_key = ""
        self.music = None
        self.music_base = 0
        self.sel = ""
        self.mixmode = False
        self._epg = (0.0, None, None)
        self._loading = None
        self.qr_on = False
        self.err = ""
        self.pipe_offset = 0
        self.visible = False
        self._hist = []
        self._go(page)
        self.show()

    def hide(self):
        self.visible = False
        self.entry = ""
        self._hum_check()          # leaving the pages silences 666
        self.tv_cmd(["overlay-remove", self.overlay_id])
        if self._resume and self._resume[0] == "LIVE":
            _, url, name, epg_id, key = self._resume
            self._resume = None
            self.play_live(url, name, epg_id, key)  # live edge, no seek
            return
        if self._resume:
            path, pos = self._resume
            self._resume = None
            if path.startswith("http"):
                base = re.sub(r"&start=\d+", "", path)
                self.pipe_offset = pos
                self.tv_cmd(["loadfile", "%s&start=%d" % (base, pos),
                             "replace"])
            else:
                self.tv_cmd(["loadfile", path, "replace", -1,
                             "start=%d" % max(0, pos - 2)])
            self.tv_cmd(["set_property", "pause", False])
            self.show_loading((self._desc_for_current() or {}).get("title")
                              or clean_title(path))
        # the film we just told mpv to load has not started yet, so do not
        # ask now -- the start-file event drives update_saver(), and the
        # periodic check below is the backstop.
        self._saver_check_at = time.monotonic() + 1.5

    def next_deadline(self):
        due = [self._saver_check_at or (time.monotonic() + 5)]
        if self.snake and not self.snake["dead"]:
            due.append(self.snake["next"])
        if self.chat and self.chat.get("job"):
            due.append(self.chat["poll"])
        if self.news and self.news.get("job"):
            due.append(self.news["poll"])
        if self._hum is not None:
            due.append(time.monotonic() + 1.0)
        if self._hum_due:
            due.append(self._hum_at + self._hum_due[0])
        if self.invent and self.visible:
            due.append(self.invent["poll"])
        if self.visible and (self.music or
                             (self.live and self.live_page == P_RADIO)):
            due.append(self._np[0])
        if self.visible and self._anim_spec():
            due.append(self._anim[1])
        if self.visible:
            due.append(self._clock_at)
        if self._vol_until:
            due.append(self._vol_until)
        return min(due)

    def tick(self):
        now = time.monotonic()
        if self._loading and now > self._loading[1]:
            # the file never started (bad stream, dead gateway) -- do not
            # leave a "please wait" card on screen forever
            self.log.warning("loading %s timed out", self._loading[0])
            self.clear_loading()
            self.err = "Startet nicht"
            self.show()
        if self.visible and now >= self._clock_at:
            self.repaint()
        if (self.visible and self.music and self.page == P_NOW
                and now >= self._np[0]):
            # the timeline would otherwise only move once a minute, when the
            # clock in the header ticks
            self._np = (now + 1.0, self._np[1], self._np[2])
            self.repaint()
        if self.snake and not self.snake["dead"] and now >= self.snake["next"]:
            self.snake["blink"] += 1
            self._snake_step()
            self.snake["next"] = now + self._snake_speed()
            self.repaint()
        if (self.visible and self.live and self.live_page == P_RADIO
                and self.page == P_NOW and now >= self._np[0]):
            self.repaint()          # pulls a fresh ICY title as it redraws
        if self.news and self.news.get("job") and now >= self.news["poll"]:
            self._news_poll()
        if self.chat and self.chat.get("job") and now >= self.chat["poll"]:
            self._chat_poll()
        self._hum_check()
        while self._hum_due and now >= self._hum_at + self._hum_due[0]:
            # the interference is inside the drone, so its timing is known:
            # fire the room at exactly that moment
            self._hum_due.pop(0)
            if self.flash:
                # on the beat: short and hard. A glitch gets a longer blink.
                self.flash(0.07 if self._hum_kind == "menace" else 0.18)
        spec = self._anim_spec()
        if spec and self.visible and now >= self._anim[1]:
            self._anim[0] = (self._anim[0] + 1.0 / (self.ANIM_FPS *
                                                    float(spec.get("secs", 6)))) % 1.0
            self._anim[1] = now + 1.0 / self.ANIM_FPS
            self.repaint()
        if (self.invent and self.visible
                and self.page == self.invent["page"]
                and self.sub == self.invent.get("sub", 1)):
            self._invent_tick()
        elif self.invent and self.invent["done"]:
            self.invent = None
        if self._vol_until and now >= self._vol_until:
            self._hide_volume()
        if now >= self._saver_check_at:
            self._saver_check_at = now + 5
            self.update_saver()
        if (self.music and not self._loading
                and "/music?path=" not in (self.tv_query("path") or "")):
            # the album ran out (or something else took the screen): stop
            # claiming the now-playing page, and put MIX back the way it was
            self.music = None
            self.mixmode = False
            if self.visible:
                self.repaint()

    # -- input -------------------------------------------------------------

    def _open_item(self, sel):
        """Act on one numbered choice.

        Reached two ways: by typing the number, and by walking the
        highlight onto it and pressing ENTER. Both had to end up in
        the same place or the two would drift apart.
        """
        items = (self._meta or {}).get("links", {}).get("items", {})
        tgt = items.get(sel) or items.get(str(int(sel)))
        if tgt and tgt.get("live"):
            name = ""
            try:      # the row text is the channel name
                raw = self._pcache.get((self.page, self.sub))
                if raw:
                    name = bytes(raw[1][int(tgt["row"])][3:]).decode(
                        "latin1")[3:].strip()
            except Exception:
                pass
            self.play_live(self._live_target(tgt),
                           tgt.get("name") or name,
                           tgt.get("epg", ""), tgt.get("ch", ""),
                           self.page)
            return
        if tgt and "rom" in tgt and self.launch_game:
            self.launch_game(tgt["rom"])
            return
        if tgt and "play" in tgt:
            if is_music(tgt["play"]):
                self._play_album(tgt["play"])
            else:
                self._play(tgt["play"], (self.page, self.sub, int(sel)))
            return
        if tgt and "sub" in tgt:
            # jump to a subpage of THIS page -- how a news list opens
            # the article it points at
            self.sub = int(tgt["sub"])
            self.repaint()
            return
        if tgt and "page" in tgt:
            self._go(int(tgt["page"]), tgt.get("sel", ""))
        else:
            self.err = "ung}ltig".replace("}", "ü")


    def _items(self):
        """The numbered choices on the page showing, in order."""
        items = ((self._meta or {}).get("links") or {}).get("items") or {}
        try:
            return [k for k in sorted(items, key=int)]
        except (TypeError, ValueError):
            return sorted(items)

    def _move_sel(self, step):
        """Walk the highlight through the page's list.

        Typing the number still works and always will -- this is the same
        choice made with two keys instead of three, which is what you want
        when you are holding the remote rather than reading it.
        """
        keys = self._items()
        if not keys:
            return False
        if self.selkey not in keys:
            self.selkey = keys[0] if step > 0 else keys[-1]
        else:
            self.selkey = keys[(keys.index(self.selkey) + step) % len(keys)]
        return True

    def _paint_sel(self, pg):
        """Mark the highlighted row. The links carry each item's row, which is
        also how the watched-colours are painted, so nothing new is needed."""
        items = ((self._meta or {}).get("links") or {}).get("items") or {}
        meta = items.get(self.selkey)
        if not isinstance(meta, dict):
            return pg
        row = meta.get("row")
        if row is None or not (0 <= int(row) < 24):
            return pg
        pg[int(row)][0:3] = alpha(WHITE) + NEWBG + alpha(BLACK)
        return pg

    def _go(self, page: int, sel: str = ""):
        self.seek_armed = False
        if page != P_SET:
            self._set_msg = ""
            self._reboot_armed = False
            self._tvoff_armed = False
        if (self.page, self.sub, self.sel) != (page, 1, sel):
            self._hist.append((self.page, self.sub, self.sel))
            self._hist = self._hist[-32:]
        self.page, self.sub, self.sel = page, 1, sel
        self.selkey = ""               # a new page starts with nothing picked

    def key(self, name: str):
        """'0'-'9', enter, clear, back, plus, minus, toggle, mix, now."""
        if not self.visible:
            if name == "back" and self.live:
                # the remote's "back" during live TV means: show me the
                # channel list again, not a page-history step -- and the
                # right list, TV or radio, whichever this station came from
                self._go(self.live_page)
                self.show()
                return
            if name.isdigit():
                self.show()
            elif name in ("c", "toggle"):
                self.show()
                return
            elif name == "now":
                self.page, self.sub = P_NOW, 1
                self.show()
                return
            else:
                return
        if self.photo:
            self.photo = None          # any key puts the picture away
            self.repaint()
            return
        if self.snake:
            g = self.snake
            if g.get("entering"):
                now = time.monotonic()
                mt = g.get("mt")
                if name in MULTITAP:
                    letters = MULTITAP[name]
                    if mt and mt[0] == name and now < mt[2]:
                        i = (mt[1] + 1) % len(letters)      # cycle in place
                        g["initials"] = g["initials"][:-1] + letters[i]
                        g["mt"] = (name, i, now + MT_TIMEOUT)
                    elif len(g["initials"]) < 3:
                        g["initials"] += letters[0]
                        g["mt"] = (name, 0, now + MT_TIMEOUT)
                elif name == "back":
                    g["initials"] = g["initials"][:-1]
                    g["mt"] = None
                elif name == "enter":
                    g["hs"] = self._score_submit(g["initials"] or "???",
                                                 g["score"])
                    g["entering"] = False
                self.repaint()
                return
            TURN = {"4": (0, -1), "6": (0, 1), "8": (-1, 0), "2": (1, 0)}
            if name in TURN:
                dr, dc = TURN[name]
                # no reversing onto yourself: compare with the direction that
                # was actually MOVED, not a turn queued since the last step
                if (dr, dc) != (-g["dir"][0], -g["dir"][1]):
                    g["turn"] = (dr, dc)
                return
            if name in ("c", "clear"):
                self.snake = None
                self._go(100)
                self.repaint()
                return
            if name == "enter" and g["dead"]:
                self._snake_start()
                return
            return
        if self._rand_ask:
            # a modal question: only yes and no exist while it is up
            self._rand_ask = False
            if name == "enter":
                self._play_random()
                return
            self.repaint()
            if name in ("c", "clear", "toggle"):
                return
        if self.qr_on:
            self.qr_on = False
            self.repaint()
            if name in ("c", "x", "toggle"):
                return
        if self.page == P_NEWS_AI and self.visible and name not in ("c",
                                                                    "clear"):
            self._news_key(name)
            return
        if self.chat and self.page not in (P_CHAT, P_RPG):
            self.chat = None            # the other one starts its own
        if self.page == self.P_BLUE and self.visible \
                and name not in ("c", "clear"):
            if name == "0":
                self._bt_seen, self._bt_msg = [], "sucht ..."
                self.bt_scan()
            elif name.isdigit():
                self._bt_sel = name
            elif name in ("next", "prev"):
                devs = self._bt_seen or self._bt_devices()
                if devs:
                    i = int(self._bt_sel or 1) - 1
                    i = (i + (1 if name == "next" else -1)) % len(devs)
                    self._bt_sel = str(i + 1)
            elif name == "enter" and self._bt_sel:
                self.bt_pair(int(self._bt_sel) - 1)
            elif name == "back":
                self._go(P_SET)
            self.repaint()
            return
        if self.page in (P_CHAT, P_RPG) and self.visible \
                and name not in ("c", "clear"):
            # On the chat page the digits ARE the keyboard, so nothing else
            # can have them -- no page numbers, no subpage stepping. C is the
            # single way out, exactly as it is in the game.
            self._chat_key(name)
            return
        if name == "c":
            # C owns entering/leaving the OS. With digits half-typed it
            # clears them first; the next press leaves. And it never exits
            # into blackness: with nothing to resume and only the dead
            # channel behind the pages, C goes home to 100 instead.
            if self.entry or self.seek_armed:
                self.entry = ""
                self.seek_armed = False
                self.repaint()
            elif self._resume is None and not self._on_air():
                if self.page == 100:
                    self.hide()      # insisting from 100: fine, dead channel
                else:
                    self._go(100)
                    self.repaint()
            else:
                self.hide()
            return
        if name == "toggle":
            # a keyboard-only alias for C, kept because ESC is not the only
            # way people reach for "get me out of this"
            self.hide()
            return
        if name.isdigit():
            shot = self._photo_codes().get(self.entry + name)
            if shot:
                # picture easter eggs come from the broadcaster's personal.json,
                # so nothing private is baked into this source
                self.entry = ""
                self.show_photo(shot.get("name", ""), shot.get("caption", ""),
                                shot.get("crop", ""), shot.get("sound", ""))
                return
            if self.entry == "133" and name == "7":
                # 1337. The 7 acts as its own ENTER -- the entry line caps at
                # three digits, so this has to be caught before that check.
                self.entry = ""
                self._snake_start()
                return
            # Digits only ever fill the entry line; NOTHING happens until
            # ENTER (Martin's rule: every numeric command has a visible
            # buffer, backspace-editable, ENTER-terminated). On 111 with a
            # film the buffer is a jump time (up to 6 digits), everywhere
            # else a page or list number (up to 3).
            cap = 6 if (self.page == P_NOW and self.seek_armed) else 3
            if len(self.entry) < cap:
                self.entry += name
        elif name == "enter":
            if self.entry == "0":
                # Programm 0 = Sendeschluss. Works from anywhere -- mid-film
                # it is just 0 ENTER, since a digit auto-opens the OS.
                self.entry = ""
                self.sendeschluss()
                self.repaint()
                return
            if self.page == P_NOW and self.live and self.entry in ("1", "2"):
                # 1 = what is on now, 2 = what is on next
                self.sub = 2 if self.entry == "1" else 3
                self.entry = ""
                self.repaint()
                return
            if self.page == P_NOW and not self.entry \
                    and (self._resume or self._on_air()):
                # empty ENTER on the now-playing page arms the jump field;
                # until then digits are page numbers like everywhere else
                self.seek_armed = not self.seek_armed
                self.repaint()
                return
            if self.entry and self.page == P_NOW and self.seek_armed:
                self.seek_armed = False
                self._commit_seek()
                return
            if len(self.entry) == 3:
                # three digits + ENTER = page number, from anywhere
                page, self.entry = int(self.entry), ""
                self._go(page)
            elif self.entry and self.page == P_SET:
                self._set_msg = self._settings_action(self.entry)
                self.entry = ""
            elif self.entry:
                sel, self.entry = self.entry, ""
                self._open_item(sel)
            elif self.selkey:
                # nothing typed, but something is highlighted: ENTER opens it,
                # by handing the same code path the number it would have got
                sel, self.entry = self.selkey, ""
                self.selkey = ""
                self._open_item(sel)
            else:
                # ENTER is ONLY the terminator of a numeric command -- but
                # on a list of things to watch, "ENTER without choosing" is
                # a natural "surprise me", so offer exactly that.
                if self._playables():
                    self._rand_ask = True
                # ...otherwise it still does nothing (leaving the OS is C's).
        elif name in ("next", "prev"):
            # move the highlight; with no list to move through, nothing
            self._move_sel(1 if name == "next" else -1)
        elif name == "more":
            # the next subpage, wrapping round
            subs = (self._meta or {}).get("subs", 1)
            self.sub = self.sub % max(1, subs) + 1
            self.selkey = ""
        elif name == "back":
            # undo one step: a typed digit, then a highlight, then the page
            if self.entry:
                self.entry = self.entry[:-1]
            elif self.selkey:
                self.selkey = ""
            elif self.sub > 1:
                self.sub -= 1
            elif self._hist:
                self.page, self.sub, self.sel = self._hist.pop()
        elif name == "clear":
            if self.entry:
                self.entry = ""
            else:
                self._go(100)
        elif name == "mix":
            self.mixmode = not self.mixmode
        elif name == "now":
            self.page, self.sub = P_NOW, 1
            self.selkey = ""
        self.repaint()
