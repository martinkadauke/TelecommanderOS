# TelecommanderOS

A teletext operating system for a 1983 SABA Telecommander colour television.

The set is real, the teletext is real — a software SAA5050 character generator
drawing 40×24 character cells into a framebuffer — and everything the TV can
do is reached by typing a page number on a numeric keypad, the way you reached
anything on television in 1983.

It plays films and series off a NAS, streams live television and internet
radio, browses a 21,000-track music library behind a spectrum analyser, runs
an NES emulator, reads Reddit, argues with you about the news, plays Snake,
and invents pages that did not exist until you asked for them.

```
100  Index                  600  Musik (A–Z → artist → album → track)
101  Bedienung              666  ▓▒░ …
111  Jetzt läuft            700  News → 709 Reddit · 710 ChatCRT News
200  Filme                  800  Fernsehen (live TV, EPG)
300  Serien                 900  Radio
400  Spiele (NES)           950  ChatCRT
500  Einstellungen          any unclaimed number → a page is invented for it
```

> **Status.** This is a working system built for one specific living room. It
> is hardcoded to one network, one NAS, one TV, and it speaks German. Making
> it installable by other people is the long-term plan — see
> [Roadmap](#roadmap) — but it is not there yet.

---

## Contents

- [How it is put together](#how-it-is-put-together)
- [Hardware](#hardware)
- [The pages](#the-pages)
- [Installation](#installation)
- [Configuration](#configuration)
- [The web editor](#the-web-editor)
- [Design notes and hard-won lessons](#design-notes-and-hard-won-lessons)
- [Roadmap](#roadmap)

---

## How it is put together

Two machines, split by what each is good at.

```
     ┌──────────────────────────┐         ┌────────────────────────────┐
     │  Raspberry Pi 2 · "the   │  HTTP   │  Unraid server · "the      │
     │  receiver"               │ ──────► │  broadcaster"              │
     │                          │         │                            │
     │  retrokb    input, state │         │  gateway.py   pages, media │
     │  teletext   SAA5050, UI  │         │  ffmpeg/nvenc transcode    │
     │  mpv        DRM/KMS      │         │  Ollama, SearXNG, Jellyfin │
     └────────────┬─────────────┘         └────────────────────────────┘
                  │ HDMI → RF modulator → SCART
           ┌──────▼──────┐
           │ SABA CRT    │
           └─────────────┘
```

**The receiver** (`retrokb.py`, `teletext.py`) runs on the Pi as root. It
grabs the keypad exclusively with `EVIOCGRAB`, owns all interface state, and
draws teletext pages as BGRA overlays onto mpv, which holds DRM/KMS master.

**The broadcaster** (`gateway.py`) runs in a container on the NAS. It serves
pages, transcodes video on the GPU, talks to Ollama and SearXNG, and stores
anything that must outlive an SD card.

The split is not arbitrary. The Pi 2 needs **246 ms** of pure-Python work to
paint one 1280×720 teletext frame; the server does it in **7 ms**. So the
receiver sends its finished 40×24 character grid to `POST /tt/render` and gets
zlib-compressed pixels back — about 18 KB on the wire, roughly 3× faster end
to end. Local rendering stays as the fallback, because drawing a page must
never depend on the network.

### Components

| File | Runs on | Does |
|---|---|---|
| `retrokb.py` | Pi (root) | input, routing, WLED, emulator, HA webhooks |
| `teletext.py` | Pi | SAA5050 renderer, all pages, all interface state |
| `tvplayer.py` | Pi | starts mpv on DRM/KMS with the right flags |
| `tvgate.py` | Pi | starts/stops the player from a smart plug's state |
| `gateway.py` | NAS | pages, transcode, AI, search, storage, web editor |
| `editor.html` | NAS | browser page editor |

### AI and search

Both are self-hosted; nothing leaves the house.

- **Ollama** writes ChatCRT's dialogue, invents pages, generates the 666
  archive, and summarises news. Default model `gemma4:12b-mlx`.
- **SearXNG** does the searching for ChatCRT News and for fact pages.

---

## Hardware

| Part | Notes |
|---|---|
| SABA Telecommander | 1983 CRT, 4:3, PAL |
| Raspberry Pi 2 Model B | HDMI out, ARMv7 |
| 8BitDo Retro Mechanical Keyboard | plus its Dual Super Buttons, joystick and ABXY pad |
| 8BitDo Retro 18 Numpad | **the remote** — everything is driven from this |
| 4 × WLED strips, 300 px | shelf lighting, driven by the joystick |
| Shelly plug | switches the TV, read and written through Home Assistant |

### The remote

```
 ┌─────┬─────┬─────┬─────┐
 │ Num │  /  │  ×  │  -  │    /  previous · ×  next
 ├─────┼─────┼─────┼─────┤    +  louder  · -  quieter  (10 notches)
 │  7  │  8  │  9  │     │    .  subpages / aspect / mode
 ├─────┼─────┼─────┤  +  │    C  open and close the OS
 │  4  │  5  │  6  │     │    ⌫  back
 ├─────┼─────┼─────┼─────┤    ⏎  confirm a number · play/pause
 │  1  │  2  │  3  │     │
 ├─────┴─────┼─────┤ ⏎   │    Hold C 3 s → stop everything, back to 100
 │     0     │  .  │     │    Hold C 5 s → that, and switch the TV off
 └───────────┴─────┴─────┘
```

**Text entry** uses the numpad's *physical* layout, not its digits. A numpad
is a phone keypad upside down, so the letters follow the key position:

```
7 .,?    8 ABC    9 DEF
4 GHI    5 JKL    6 MNO
1 PQRS   2 TUV    3 WXYZ
         0 space
```

Both multi-tap and predictive T9 are available (`.` switches, and switches
German/English with it). The predictive dictionary is built from the Debian
`wngerman` and `wamerican` word lists.

---

## The pages

### Media

**200 Filme · 300 Serien** — scanned from the NAS. Films play straight off NFS
when the Pi can decode them and through the GPU transcoder when it cannot.
Row colours cycle through seven states by watch count. **111** shows what is
playing with a progress bar, a jump field, and descriptions pulled from
Jellyfin with IMDb ratings.

**600 Musik** — 986 artists, 21k tracks, addressed A–Z → artist → album →
track. The picture is a **spectrum analyser** rendered by ffmpeg at exactly
40×24 — one value per teletext cell — then scaled with nearest-neighbour so
every bar is built from the same grid as the characters, coloured
green/yellow/red by height.

**800 Fernsehen** — 18 live channels. The ZDF family resolves through ZDF's
own public player API at zap time, because their manifest path rotates and
their player token expires; a URL baked into a page goes stale. EPG for the
ARD channels, with descriptions of the current and next programme.

**900 Radio** — 15 public stations. Same analyser as the music, and the
current track comes from **ICY metadata**, the way Winamp knew it.

### AI

**950 ChatCRT** — not a chatbot but a short interactive **episode**. The
Telecommander is a character with a history: built in 1983 at the SABA works
in Villingen, ten years in the middle of a living room, displaced by a video
recorder and then a kitchen TV, carried to the cellar in 2001 under a blanket,
twenty years in the dark, no idea what happened since, too proud to ask. Each
session draws a premise, a secret goal it pursues without ever naming, two
personality quirks, a mood, and a twist that lands on turn three. You answer
by picking one of three options with `+`/`-`; T9 typing is there when you want
to say something else.

**710 ChatCRT News** — five levels deep: sections → stories → one story → one
source → arguing about it. SearXNG searches, the model groups the results into
five stories with real sources, summarises one on demand, and then — option
`2 Was denkst du?` — searches for critique and gives you its opinion with
*dafür*, *dagegen*, and *was ich anders gemacht hätte*, followed by three
replies that keep the conversation going.

**Unclaimed page numbers get invented.** Type any number nothing serves and
the set makes a page for it: *"Diese Seite wird Ihnen gleich ausgestrahlt"*,
then it paints itself in left to right. **The page is then kept** — written to
disk, part of the service from then on — and `.` writes another subpage
continuing from it. Each draws a kind first: a looked-up fact with a real
source, a picture, a table of invented statistics, a quiz, or prose.

### Games and oddities

**400 Spiele** — NES via RetroArch on KMS, launched from the page and quit
back to it.

**1337** — type it and Snake starts, drawn in whole character cells, with
beep-boop when it eats and a descending wail when you die. High scores live on
the server, so they survive a reflash, and beating the top score asks for your
initials by multi-tap.

**666** — a page that should not be on air. 41 written pages, everything up to
666 generated on first visit and kept. The order reshuffles every hour. Some
pages move — blood rising from the bottom, something running down from the
top, a line sweeping the screen looking for something. A low drone plays
while the page is up: 55 Hz against 55.5 Hz, so it beats half a hertz, with
interference at intervals you cannot predict. **And the header decays** the
deeper you go — by subpage 100 the station name has been replaced by something
addressing you directly.

---

## Installation

Not yet a one-command install. Roughly:

**On the server**

```bash
mkdir -p /mnt/user/appdata/tvtranscode
cp gateway.py editor.html tv.json radio.json creep.json /mnt/user/appdata/tvtranscode/
docker run -d --name TelecommanderOS \
  --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all \
  -p 8203:8203 \
  -v /mnt/user/appdata/tvtranscode:/app \
  -v /mnt/user:/mnt/user \
  <an image with python3 and ffmpeg/nvenc> python3 /app/gateway.py
```

**On the Pi**

```bash
sudo ./install.sh
sudo systemctl enable --now tvplayer retrokb noconsole
```

Then edit `/etc/retrokb/retrokb.toml` — at minimum the broadcaster address,
the media roots and the input device product IDs.

`retrokb --list` prints the input devices it can see; `retrokb --probe`
prints every key event without grabbing anything, which is how you find out
what your keypad actually sends.

---

## Configuration

| File | What it holds |
|---|---|
| `retrokb.toml` | devices, key bindings, WLED, mpv, sound, page settings |
| `tv.json` | live TV channels |
| `radio.json` | radio stations |
| `creep.json` | the 666 pages |
| `chat.json` | Ollama host and model |

All the JSON files are re-read on every request, so edits are live.

---

## The web editor

`http://<server>:8203/` is a browser editor for teletext pages: the real
glyph ROM, the mosaic graphics, the colour attributes, and a management panel
that can rescan the library, restart the player, reboot the Pi, purge invented
pages and clear a stuck file. A page saved from the editor is marked as
hand-edited and the generator never touches it again.

---

## Design notes and hard-won lessons

Things that cost real time to find out.

**Teletext**

- Level 1 teletext has **no alpha black**, which is why black-on-white was
  impossible in 1983. This renderer allows `0x00` as alpha black — the byte
  already rendered as a blank spacing cell, so it only adds the colour.
- **A colour change costs a whole character cell**, because it is a spacing
  attribute. Every layout decision follows from this. It is why the food in
  Snake blinks instead of being a second colour, and why photo conversion
  smooths short colour runs away before spending a cell on them.
- **A sextant is 1.2× wider than tall** on a 4:3 screen. A square photo needs
  about 27 columns, not 39. Getting this wrong stretches everything.
- Attributes occupy a cell **on screen too** — a menu laid out on a 6-cell
  pitch silently ate its own letters at 7 cells of content.

**Pictures**

- Converting a photograph: choose the cell colour by **hue**, not least
  squares. White partially matches *any* colour once you scale it, so least
  squares picks white for every desaturated cell and a whole hedge turns
  white.
- Tone must come from **which** sextants are lit — a Bayer dither — never from
  a brightness test, or you get only solid blocks and empty ones.
- Genuinely dark cells must be forced to black: down there a few units of
  sensor noise decide the hue, and the result is confetti.
- A flat illustration needs **no** saturation boost; the boost exists to drag
  muted photographic colour toward eight primaries and it wrecks poster art.

**Video and audio**

- `--gpu-hwdec-interop=drmprime` puts video on the primary plane. Without it
  the vc4 driver wedges in an atomic-commit loop that pegs the CPU and starves
  sshd — while subtitles still render, which is the clue.
- mpv ≥ 0.38 `loadfile`'s third positional argument is an **insert index**;
  options are fourth. Passing options third fails silently.
- `pause` is a **global** mpv property, not per file. One stray keypress on
  the idle carrier pauses everything loaded afterwards.
- `plughw` is a direct hardware PCM with **no mixing**: exactly one process
  can hold it. Sound effects that play alongside the 666 drone have to be
  baked into the drone itself.
- A looping drone must contain a **whole number of cycles** of every component
  or the loop point clicks.

**AI**

- Thinking models stream `thinking` tokens with `content` empty. Set
  `think: false` or the answer looks like it never arrives.
- Ollama's JSON mode mangles top-level **arrays**; objects are fine.
- Parse the model's JSON **tolerantly**. Run into the token limit and the
  closing brace never arrives — a strict parse then dumps the raw object on
  screen, braces and all.
- **Randomness has to come from the code, not the model.** Ask a model to be
  surprising and it reaches for the same half-dozen ideas; hand it a premise
  drawn with `random.choice` and it performs.
- **Measure a persona, do not eyeball it.** The playtest harness plays N turns
  automatically and counts failure modes. Over 90 turns it found ruts that
  reading never would: 13 options that hit the TV, and one action repeated
  seven times. The fix is to **forbid the observed failure modes by name** and
  say what each option is *for*.
- Paywalls are often not paywalls. Most news sites ship the full article in a
  `<script type="application/ld+json">` block for search engines, freely, to
  an unauthenticated request. Read that before giving up.

**Everything else**

- The Pi 2 cannot survive an `apt install` with the backlit keyboard attached
  — it browns out. Unplug it first.
- Anything that takes seconds must not run on the main loop. Building 30 s of
  audio takes the Pi 3 s and froze the interface mid-page.

---

## Roadmap

The goal is a downloadable Raspberry Pi image where TelecommanderOS and the
web UI come up together and are set up from the browser.

- [ ] **Generalise the hardcoded parts** — network addresses, media roots,
      device IDs and service endpoints all come from configuration or
      discovery, with nothing baked into the source.
- [ ] **Internationalise.** Every string on screen is currently German.
      Page text, prompts and personas move to language packs.
- [ ] **Single image.** Receiver and broadcaster on one Pi, with the GPU
      transcoder optional for people who have a server and skipped for those
      who do not.
- [ ] **Browser setup** — first-boot wizard for network, media, and the AI
      backend, instead of hand-editing TOML over SSH.
- [ ] **One-command install** for people who want to keep the split.

---

## Licence

Not yet chosen. Ask before reusing.
