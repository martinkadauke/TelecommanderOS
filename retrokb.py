#!/usr/bin/env python3
"""
retrokb - bridge an 8BitDo retro mechanical keyboard (plus its Super Buttons,
joystick and ABXY add-ons, and the numpad) into Home Assistant.

Split of responsibility:
  * Home Assistant gets every input as a webhook event, and owns everything
    semantic -- cheat codes, scenes, whatever you hang off the buttons.
  * The moving band of light is rendered here, straight to the WLED ESP32s
    over their UDP realtime protocol. HA cannot express "LEDs 138-146", and
    realtime is an OVERLAY -- it leaves segment config, on/off state and
    everything HA reads untouched, so it cannot break your automations.

Modes:
  retrokb --list      list candidate input devices and exit
  retrokb --probe     print every key event from every keyboard-ish device
                      WITHOUT grabbing anything, then print a paste-ready
                      [bindings] skeleton on Ctrl-C. Run this first.
  retrokb --dry-run   run normally, but log payloads instead of POSTing
  retrokb             normal service mode
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import collections
import os
import selectors
import shutil
import signal
import subprocess
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

from evdev import InputDevice, ecodes, list_devices

import teletext as teletext_mod

LOG = logging.getLogger("retrokb")

REDISCOVER_INTERVAL = 3.0  # seconds between scans for (re)plugged devices
DEFAULT_CONFIG = "/etc/retrokb/retrokb.toml"

DIGIT_KEYS = {}
for _d in range(10):
    DIGIT_KEYS[f"KEY_{_d}"] = str(_d)
    DIGIT_KEYS[f"KEY_KP{_d}"] = str(_d)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def key_names(code: int) -> list[str]:
    """evdev maps some codes to several names (e.g. KEY_KPENTER); return all."""
    name = ecodes.KEY.get(code)
    if name is None:
        return []
    if isinstance(name, (list, tuple)):
        return list(name)
    return [name]


def digit_of(names: list[str]) -> str | None:
    for n in names:
        if n in DIGIT_KEYS:
            return DIGIT_KEYS[n]
    return None


def open_devices() -> list[InputDevice]:
    devs = []
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if ecodes.EV_KEY not in dev.capabilities():
            dev.close()
            continue
        devs.append(dev)
    return devs


def describe(dev: InputDevice) -> str:
    return (f"{dev.path}  {dev.name!r}  "
            f"(vendor=0x{dev.info.vendor:04x} product=0x{dev.info.product:04x})")


def matches(dev: InputDevice, rules: list[dict]) -> bool:
    for rule in rules:
        vendor = rule.get("vendor")
        product = rule.get("product")
        needle = rule.get("name_contains")
        if vendor is not None and dev.info.vendor != int(str(vendor), 0):
            continue
        if product is not None and dev.info.product != int(str(product), 0):
            continue
        if needle and needle.lower() not in dev.name.lower():
            continue
        return True
    return False


# --------------------------------------------------------------------------
# Home Assistant sender (background thread, never blocks the event loop)
# --------------------------------------------------------------------------

class Sender(threading.Thread):
    def __init__(self, url: str, timeout: float, dry_run: bool):
        super().__init__(daemon=True, name="ha-sender")
        self.url = url
        self.enabled = bool(url)
        self.timeout = timeout
        self.dry_run = dry_run
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self._last_error_log = 0.0
        self._failing = False

    def send(self, payload: dict) -> None:
        if not self.enabled:
            return
        # Drop the oldest event rather than block: a stalled HA must never
        # make the joystick feel laggy.
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass

    def stop(self) -> None:
        self.q.put(None)

    def run(self) -> None:
        while True:
            payload = self.q.get()
            if payload is None:
                return
            if self.dry_run:
                LOG.info("DRY-RUN -> %s", json.dumps(payload))
                continue
            self._post(payload)

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            now = time.monotonic()
            if now - self._last_error_log > 30:
                LOG.warning("POST to Home Assistant failed: %s", exc)
                self._last_error_log = now
            self._failing = True
            return
        if self._failing:
            LOG.info("Home Assistant reachable again")
            self._failing = False


# --------------------------------------------------------------------------
# WLED band renderer
# --------------------------------------------------------------------------

class Renderer(threading.Thread):
    """Owns the moving band of light.

    Uses WLED's UDP realtime protocol (DRGBW, port 21324) rather than the JSON
    API. That distinction matters: the JSON API writes STORED CONFIGURATION, so
    painting a band with segments permanently altered each strip -- which changed
    how Home Assistant models the device (a second segment flips the main entity
    to brightness-only) and broke unrelated automations.

    Realtime is an overlay. It touches no segment layout, no on/off state, and
    nothing HA reads. Stop sending and the strip reverts on its own to whatever
    HA last set, after the timeout byte in the packet header.

    So the band is a lease, not a write: held while you are using the stick,
    refreshed by a keepalive, released automatically once you stop.

    Coalescing is the point of the thread: if the stick outruns the network,
    only the newest cursor position gets sent. A stale frame is worthless.
    """

    PROTO_DRGBW = 3

    def __init__(self, cfg: dict, dry_run: bool):
        super().__init__(daemon=True, name="wled")
        w = cfg.get("wled", {})
        # Enabled even in dry-run, so --dry-run exercises the whole cursor and
        # logs what it would send. _send() does the muting.
        self.enabled = bool(w.get("enabled"))
        self.dry_run = dry_run
        self.hosts = {int(s["n"]): s["host"] for s in w.get("shelves", [])}
        # every strip in the room, not just the four the band runs on -- the
        # 666 flash is meant to catch the whole space
        self.flash_hosts = [h for h in (w.get("flash_hosts") or [])] or \
            list(self.hosts.values())
        self.leds = int(w.get("leds", 300))
        self.band_len = int(w.get("band_len", 8))
        self.step_px = int(w.get("step_px", 6))
        self.bri = int(w.get("brightness", 200))
        self.port = int(w.get("port", 21324))
        # Seconds WLED waits after our last packet before handing the strip
        # back. Also a dead-man switch: if retrokb dies, the lights recover.
        self.rt_timeout = int(w.get("realtime_timeout_s", 2))
        self.keepalive = float(w.get("keepalive_s", 0.4))
        # 0 disables the idle timer entirely -- the band then persists until
        # something else touches the shelf (see watch_ha below).
        self.release_after = float(w.get("release_after_s", 0))
        # Poll the active shelf's STORED state. Our overlay provably never
        # changes it, so any change is somebody else -- a Home Assistant
        # automation, the WLED app, a button. That is our cue to let go.
        self.watch = bool(w.get("watch_ha", True))
        self.watch_interval = float(w.get("watch_interval_s", 1.0))
        self.explode_seconds = float(w.get("explode_seconds", 2.0))
        self.color = list(w.get("default_color", [255, 255, 255, 0]))

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cv = threading.Condition()
        self.shelf = min(self.hosts) if self.hosts else 1
        self.pos = 0
        self.active = False
        self.last_input = 0.0
        self._dirty = False
        self._explode = False
        self._flash = None
        self._running = True
        self._baseline = None
        self._baseline_shelf = None
        self._next_watch = 0.0

    # -- called from the event loop; must never block -----------------------

    def _touch(self) -> None:
        with self.cv:
            self.active = True
            self.last_input = time.monotonic()
            self._dirty = True
            self.cv.notify()

    def shelf_step(self, delta: int) -> None:
        if not self.hosts:
            return
        order = sorted(self.hosts)
        i = order.index(self.shelf) if self.shelf in order else 0
        self.shelf = order[max(0, min(len(order) - 1, i + delta))]
        self._touch()

    def band_step(self, delta: int) -> None:
        top = max(0, self.leds - self.band_len)
        self.pos = max(0, min(top, self.pos + delta * self.step_px))
        self._touch()

    def set_color(self, color: list) -> None:
        self.color = list(color)
        self._touch()

    def flash(self, colour=(255, 0, 0), seconds=0.18) -> None:
        """Every strip in the room, one colour, for a moment.

        Realtime UDP again, and that is the whole point: it is an overlay, so
        it needs no JSON write, cannot disturb segment config or anything Home
        Assistant reads, and the strips return to whatever they were showing
        on their own once we stop sending. Nothing has to be saved or restored.
        """
        with self.cv:
            self._flash = (list(colour), float(seconds))
            self.last_input = time.monotonic()
            self.cv.notify()

    def explode(self) -> None:
        with self.cv:
            self.active = True
            self.last_input = time.monotonic()
            self._explode = True
            self.cv.notify()

    def stop(self) -> None:
        with self.cv:
            self._running = False
            self.cv.notify()

    # -- worker ------------------------------------------------------------

    def run(self) -> None:
        while True:
            with self.cv:
                if not self._dirty and not self._explode and not self._flash:
                    # Holding the band means refreshing before the strip times
                    # out; while idle there is nothing to wake for.
                    self.cv.wait(self.keepalive if self.active else None)
                if not self._running:
                    return
                boom, self._explode = self._explode, False
                flash, self._flash = self._flash, None
                self._dirty = False
                shelf, pos = self.shelf, self.pos
                color, active, last = list(self.color), self.active, self.last_input

            if flash:
                self._do_flash(flash[0], flash[1])
                continue
            if boom:
                self._explosion(color)
                continue
            if not active:
                self._baseline = self._baseline_shelf = None
                continue

            now = time.monotonic()
            if self._baseline_shelf != shelf:
                # Moving to a shelf: remember how it looked before we borrowed
                # it, and blank the one we left so it does not ghost.
                if self._baseline_shelf is not None:
                    self._send(self._baseline_shelf, self._frame())
                self._baseline = self._fingerprint(shelf)
                self._baseline_shelf = shelf
                self._next_watch = now + self.watch_interval
            elif self.watch and now >= self._next_watch:
                self._next_watch = now + self.watch_interval
                fp = self._fingerprint(shelf)
                if fp is not None and self._baseline is not None and fp != self._baseline:
                    LOG.info("shelf %s changed externally -- band released", shelf)
                    with self.cv:
                        self.active = False
                    self._baseline = self._baseline_shelf = None
                    continue

            if self.release_after and now - last > self.release_after:
                with self.cv:
                    self.active = False
                self._send(shelf, self._frame())
                self._baseline = self._baseline_shelf = None
                LOG.info("band idle %.0fs -- released", self.release_after)
                continue
            self._send(shelf, self._band_frame(pos, color))

    def _fingerprint(self, shelf: int):
        """Stored state of a strip, as HA sees it. None on any error, which is
        treated as 'no evidence of change' rather than a reason to let go."""
        host = self.hosts.get(shelf)
        if not host or self.dry_run:
            return None
        try:
            with urllib.request.urlopen("http://%s/json/state" % host, timeout=1.5) as r:
                s = json.load(r)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        return json.dumps([s.get("on"), s.get("bri"), s.get("seg")], sort_keys=True)

    # -- frames ------------------------------------------------------------

    def _frame(self) -> bytearray:
        buf = bytearray(2 + self.leds * 4)
        buf[0] = self.PROTO_DRGBW
        buf[1] = self.rt_timeout
        return buf

    def _band_frame(self, pos: int, color: list) -> bytearray:
        buf = self._frame()
        scale = self.bri / 255.0
        px = bytes(min(255, int(c * scale)) for c in (list(color) + [0, 0, 0, 0])[:4])
        for i in range(pos, min(self.leds, pos + self.band_len)):
            off = 2 + i * 4
            buf[off:off + 4] = px
        return buf

    def _do_flash(self, colour: list, seconds: float) -> None:
        fps = 30
        rgbw = (list(colour) + [0, 0, 0, 0])[:4]
        px = bytes(min(255, int(c)) for c in rgbw)
        buf = self._frame()
        for i in range(self.leds):
            off = 2 + i * 4
            buf[off:off + 4] = px
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for host in self.flash_hosts:
                self._send_host(host, buf)
            time.sleep(1.0 / fps)
        # and then simply stop: the realtime timeout hands each strip back

    def _explosion(self, color: list) -> None:
        """A shockwave rendered by us, not a WLED built-in effect -- triggering
        one of those would need a JSON write, the very thing we stopped doing."""
        fps = 25
        frames = max(1, int(self.explode_seconds * fps))
        rgbw = (list(color) + [0, 0, 0, 0])[:4]
        centre = self.leds // 2
        dist = [abs(i - centre) for i in range(self.leds)]
        white = bytes((255, 255, 255, 255))
        for f in range(frames):
            t = f / frames
            radius = t * self.leds * 0.75
            fade = max(0.0, 1.0 - t)
            buf = self._frame()
            if t < 0.06:
                for i in range(self.leds):
                    off = 2 + i * 4
                    buf[off:off + 4] = white
            else:
                for i in range(self.leds):
                    d = dist[i]
                    if d > radius:
                        continue
                    edge = 1.0 - min(1.0, (radius - d) / 40.0)
                    v = fade * (0.2 + 0.8 * edge)
                    off = 2 + i * 4
                    buf[off:off + 4] = bytes(min(255, int(c * v)) for c in rgbw)
            for shelf in self.hosts:
                self._send(shelf, buf)
            time.sleep(1.0 / fps)

    def _send_host(self, host: str, buf: bytearray) -> None:
        if not host or self.dry_run:
            return
        try:
            self.sock.sendto(bytes(buf), (host, self.port))
        except OSError as exc:
            LOG.debug("WLED %s: %s", host, exc)

    def _send(self, shelf: int, buf: bytearray) -> None:
        host = self.hosts.get(shelf)
        if not host:
            return
        if self.dry_run:
            lit = sum(1 for i in range(self.leds) if any(buf[2 + i * 4:6 + i * 4]))
            LOG.info("DRY-RUN wled[%s] %s DRGBW %dB %d lit px",
                     shelf, host, len(buf), lit)
            return
        try:
            self.sock.sendto(bytes(buf), (host, self.port))
        except OSError as exc:
            LOG.warning("WLED shelf %s (%s) send failed: %s", shelf, host, exc)


# --------------------------------------------------------------------------
# mpv event listener: one persistent IPC connection just to notice when
# playback returns to the dead-channel carrier (or goes idle), so the OS can
# come back up on its own. Reconnects forever; mpv restarts are routine.
# --------------------------------------------------------------------------

class MpvEvents(threading.Thread):
    def __init__(self, sock_path: str, on_start_file, on_idle,
                 on_end_file=None, on_playing=None):
        super().__init__(daemon=True, name="mpv-events")
        self.sock_path = sock_path
        self.on_start_file = on_start_file
        self.on_idle = on_idle
        self.on_end_file = on_end_file
        self.on_playing = on_playing

    def run(self) -> None:
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.sock_path)
                # mpv may have started (and fired its events) before we got
                # here -- a restart race. Evaluate the CURRENT state once.
                self.on_start_file()
                buf = b""
                while True:
                    data = s.recv(4096)
                    if not data:
                        raise OSError("mpv closed the event socket")
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            ev = json.loads(line).get("event")
                        except ValueError:
                            continue
                        if ev == "start-file":
                            self.on_start_file()
                        elif ev == "idle":
                            self.on_idle()
                        elif ev == "end-file" and self.on_end_file:
                            self.on_end_file(json.loads(line).get("reason"))
                        elif ev == "playback-restart" and self.on_playing:
                            # fires when frames actually start flowing, which
                            # is later than start-file and is the honest
                            # moment to take the "please wait" card away
                            self.on_playing()
            except OSError:
                time.sleep(2)


# --------------------------------------------------------------------------
# control endpoint: lets the broadcaster's web UI manage this box
# (status, Sendeschluss, service restarts, reboot). LAN-only toy, no auth --
# same trust model as every WLED and Shelly on this network.
# --------------------------------------------------------------------------

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ControlServer(threading.Thread):
    def __init__(self, svc, port: int):
        super().__init__(daemon=True, name="control")
        self.svc, self.port = svc, port

    def run(self) -> None:
        svc = self.svc

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != "/status":
                    return self._out({"error": "unknown"}, 404)
                path = svc._tv_query("path") or ""
                pos = svc._tv_query("time-pos") or 0
                if path.startswith("http"):
                    pos += svc.tt.pipe_offset
                temp = throttled = ""
                try:
                    temp = subprocess.run(
                        ["vcgencmd", "measure_temp"], capture_output=True,
                        text=True, timeout=3).stdout.strip()
                    throttled = subprocess.run(
                        ["vcgencmd", "get_throttled"], capture_output=True,
                        text=True, timeout=3).stdout.strip()
                except OSError:
                    pass
                self._out({
                    "playing": path, "position_s": int(pos),
                    "mode": "os" if svc.tt.visible else "playback",
                    "recent_keys": list(svc.recent),
                    "os_visible": svc.tt.visible,
                    "random": svc.tt.random,
                    "temp": temp, "throttled": throttled})

            def do_POST(self):
                act = self.path.strip("/")
                if act == "endgame":
                    svc.end_game()
                    return self._out({"ok": "game ended"})
                if act == "stop":
                    svc.tt.sendeschluss()
                    return self._out({"ok": "Sendeschluss"})
                if act == "restart-tvplayer":
                    subprocess.Popen(["systemctl", "restart", "tvplayer"])
                    return self._out({"ok": "tvplayer restarting"})
                if act == "restart-retrokb":
                    self._out({"ok": "retrokb restarting"})
                    subprocess.Popen(["systemctl", "restart", "retrokb"])
                    return
                if act == "reboot":
                    self._out({"ok": "rebooting"})
                    subprocess.Popen(["systemctl", "reboot"])
                    return
                if act == "purge-probe":
                    try:
                        os.remove("/var/lib/tvplayer/probecache.json")
                    except OSError:
                        pass
                    subprocess.Popen(["systemctl", "restart", "tvplayer"])
                    return self._out({"ok": "probe cache purged, rescanning"})
                return self._out({"error": "unknown"}, 404)

        try:
            ThreadingHTTPServer(("0.0.0.0", self.port), H).serve_forever()
        except OSError as exc:
            LOG.warning("control server failed: %s", exc)


# --------------------------------------------------------------------------
# cheat-code state machine
# --------------------------------------------------------------------------

class CodeEntry:
    """Buffers numeric input into N-digit codes.

    Buffering is mechanics, so it lives here; deciding what a code *means*
    is policy, so that lives in Home Assistant. We only ever emit the digits.
    """

    def __init__(self, cfg: dict, emit):
        self.cfg = cfg
        self.emit = emit
        self.buf = ""
        self.deadline: float | None = None
        self.armed = not cfg.get("arm_key")  # empty arm_key == always listening

    def _reset(self, reason: str | None = None) -> None:
        was_active = self.armed and (self.buf or self.cfg.get("arm_key"))
        self.buf = ""
        self.deadline = None
        self.armed = not self.cfg.get("arm_key")
        if reason and was_active:
            self.emit(reason, {})

    def handle(self, names: list[str], value: int, now: float) -> bool:
        """Return True if the event was consumed by code entry."""
        if not self.cfg.get("enabled") or value != 1:
            return False

        arm_key = self.cfg.get("arm_key")
        if arm_key and arm_key in names:
            if self.armed:
                self._reset("code_cancel")
            else:
                self.armed = True
                self.buf = ""
                self.deadline = now + self.cfg["timeout_s"]
                self.emit("code_armed", {})
            return True

        if not self.armed:
            return False

        cancel_key = self.cfg.get("cancel_key")
        if cancel_key and cancel_key in names:
            self._reset("code_cancel")
            return True

        digit = digit_of(names)
        if digit is not None:
            self.buf += digit
            self.deadline = now + self.cfg["timeout_s"]
            self.emit("code_digit", {"count": len(self.buf)})
            length = int(self.cfg.get("length") or 0)
            if length and len(self.buf) >= length:
                code = self.buf
                self._reset()
                self.emit("code", {"value": code})
            return True

        submit_key = self.cfg.get("submit_key")
        if submit_key and submit_key in names:
            code = self.buf
            self._reset()
            if code:
                self.emit("code", {"value": code})
            else:
                self.emit("code_cancel", {})
            return True

        # While armed, swallow stray keys so half-typed codes stay clean --
        # but only if an explicit arm key is in use.
        return bool(arm_key)

    def tick(self, now: float) -> None:
        if self.deadline is not None and now >= self.deadline:
            self._reset("code_timeout")

    def next_deadline(self) -> float | None:
        return self.deadline


# --------------------------------------------------------------------------
# probe / list modes
# --------------------------------------------------------------------------

def cmd_list() -> int:
    devs = open_devices()
    if not devs:
        print("No input devices with key capability found (are you root?).")
        return 1
    print(f"{len(devs)} candidate device(s):\n")
    for dev in devs:
        print("  " + describe(dev))
        dev.close()
    return 0


def cmd_probe(rules: list[dict]) -> int:
    devs = open_devices()
    if rules:
        filtered = [d for d in devs if matches(d, rules)]
        if filtered:
            for d in devs:
                if d not in filtered:
                    d.close()
            devs = filtered
    if not devs:
        print("No input devices found (are you root?).")
        return 1

    print("Watching (NOT grabbing -- keys still reach the console):\n")
    for dev in devs:
        print("  " + describe(dev))
    print("\nPress every button / push the stick in every direction.")
    print("Ctrl-C when done to print a config skeleton.\n")

    seen: dict[str, dict] = {}
    sel = selectors.DefaultSelector()
    for dev in devs:
        sel.register(dev, selectors.EVENT_READ, dev)

    try:
        while True:
            for sel_key, _ in sel.select(timeout=1.0):
                dev = sel_key.data
                try:
                    events = list(dev.read())
                except OSError:
                    sel.unregister(dev)
                    continue
                for ev in events:
                    if ev.type != ecodes.EV_KEY or ev.value != 1:
                        continue
                    names = key_names(ev.code)
                    label = names[0] if names else f"CODE_{ev.code}"
                    if label not in seen:
                        seen[label] = {"code": ev.code, "dev": dev.name,
                                       "path": dev.path, "vendor": dev.info.vendor,
                                       "product": dev.info.product}
                    print(f"  {label:<20} code={ev.code:<5} {dev.name}  {dev.path}")
    except KeyboardInterrupt:
        pass

    print("\n\n" + "=" * 68)
    if not seen:
        print("No key presses captured.")
        return 1
    print(f"{len(seen)} distinct key(s) seen, in press order.\n")
    vendors = {(v["vendor"], v["product"], v["dev"]) for v in seen.values()}
    print("# --- paste into retrokb.toml ---")
    for vendor, product, name in sorted(vendors, key=lambda x: x[2]):
        print("[[devices]]")
        print(f"# {name}")
        print(f'vendor  = "0x{vendor:04x}"')
        print(f'product = "0x{product:04x}"')
        print()
    print("[bindings]")
    for label in seen:
        print(f'{label} = "CHANGEME"')
    print("# --- end ---")
    return 0


# --------------------------------------------------------------------------
# service mode
# --------------------------------------------------------------------------

class Service:
    def __init__(self, cfg: dict, dry_run: bool):
        self.cfg = cfg
        ha = cfg.get("homeassistant", {})
        url = str(ha.get("webhook_url") or "")
        if "CHANGE-THIS" in url:
            LOG.warning("webhook_url still holds the placeholder ID -- not posting to HA")
            url = ""
        self.sender = Sender(url, float(ha.get("timeout_s", 2.0)), dry_run)
        self.rules = cfg.get("devices", [])
        binds = cfg.get("bindings", {})
        self.bindings = {k: v for k, v in binds.items() if isinstance(v, str)}
        # Per-device overrides: the numpad's C key emits KEY_ESC (measured),
        # which globally means the snake/tv mode toggle. Same keycode,
        # different device, different meaning -- so bindings can be scoped
        # to a USB product id.
        np = cfg.get("numpad", {})
        self.numpad_product = int(str(np.get("product", "0")), 0)
        self.numpad_bindings = {k: v for k, v in
                                (np.get("bindings") or {}).items()
                                if isinstance(v, str)}
        self.repeating = set(binds.get("repeating", []))
        self.release_for = set(binds.get("release", [])) | self.repeating
        self.hold_interval = float(binds.get("hold_interval_ms", 120)) / 1000.0
        self.grab = bool(cfg.get("grab", True))
        self.codes = CodeEntry(cfg.get("codes", {"enabled": False}), self.emit)

        self.renderer = Renderer(cfg, dry_run)
        w = cfg.get("wled", {})
        acts = w.get("actions", {})
        self.act_shelf_prev = acts.get("shelf_prev")
        self.act_shelf_next = acts.get("shelf_next")
        self.act_band_fwd = acts.get("band_forward")
        self.act_band_back = acts.get("band_back")
        self.act_explode = set(acts.get("explode", []))
        self.colors = {k: list(v) for k, v in (w.get("colors") or {}).items()}

        # -- TV mode: ESC swaps what the arrows/space mean. Joystick and
        # keyboard arrows are the same physical signal (established earlier),
        # so both jobs cannot listen at once -- exactly one mode is active.
        tv = cfg.get("tv", {})
        self.tv_enabled = bool(tv.get("enabled"))
        self.tv_socket = str(tv.get("socket") or "")
        self.tv_ff_speed = float(tv.get("ff_speed", 3.0))
        # Reverse playback is not reliably supported by mpv/ffmpeg on this
        # hardware, so rewind is approximated as repeated seek-backs rather
        # than a true negative speed -- asymmetric with fast-forward (a real
        # speed multiplier) on purpose. rewind_step_s is tuned against
        # hold_interval_ms so held-left removes ~ff_speed seconds of content
        # per real second, to feel comparable to the smooth 3x forward.
        self.tv_rewind_step = float(tv.get("rewind_step_s", 0.18))
        self.tv_vol_steps = max(2, int(tv.get("vol_steps", 10)))
        self.tv_restart_threshold = float(tv.get("restart_threshold_s", 5.0))
        self.tt = teletext_mod.Teletext(cfg, self._tv_cmd, self._tv_query, LOG,
                                        self.launch_game, self.tv_power)
        self.tt.flash = self._flash_room
        # The Pi runs around the clock, the Saba does not. A numpad press
        # wakes its Shelly plug through Home Assistant -- HA owns the action,
        # this end only asks. Deliberately ON-only: nothing you can press by
        # accident may cut the power to a running TV. Switching off lives on
        # page 500, where it takes an explicit choice.
        tp = cfg.get("tv_power", {})
        self.tvp_url = str(tp.get("webhook") or "")
        self.flash_url = str(tp.get("flash_webhook") or "")
        self.tvp_debounce = float(tp.get("wake_debounce_s", 30))
        self._tvp_last = 0.0
        em = cfg.get("emulator", {})
        self.emu_enabled = bool(em.get("enabled", True))
        self.emu_cmd = list(em.get("command", []))
        self.emu_roots = dict(em.get("roots", {}))
        self.emu_user = str(em.get("user", "martin"))
        self.game = None                 # Popen of the running emulator
        self.tv_cmd_timeout = float(tv.get("cmd_timeout_s", 0.3))
        # Off by default: the 4:3 crop provoked a vc4 atomic-commit wedge
        # (see retrokb.toml). Kept behind a flag for a future safe approach.
        self.tv_aspect_enabled = bool(tv.get("aspect_toggle", False))
        # Which joy_* input plays which transport role. Configurable because
        # the joystick is mounted ~90 deg rotated: physical up/down emit
        # KEY_LEFT/RIGHT and physical left/right emit KEY_UP/DOWN. Light mode
        # ended up physically natural through its own action mapping; TV mode
        # needs the same rotation applied here.
        # (the old [tv] prev/next/vol roles are gone with the mode switch --
        # the numpad's own keys carry those now)
        self.recent: collections.deque = collections.deque(maxlen=14)
        # wall-clock actually spent watching the current file, so that
        # skipping to the end does not count as having seen it
        self._w_key = None
        self._w_secs = 0.0
        self._w_last = 0.0
        self.devices: dict[str, InputDevice] = {}
        self.sel = selectors.DefaultSelector()
        # target -> monotonic time its next "hold" tick is due
        self._held: dict[str, float] = {}
        self.running = True

    # -- device lifecycle ---------------------------------------------------

    def discover(self) -> None:
        for dev in open_devices():
            if dev.path in self.devices or not matches(dev, self.rules):
                dev.close()
                continue
            try:
                if self.grab:
                    dev.grab()
            except OSError as exc:
                LOG.warning("cannot grab %s: %s", dev.path, exc)
                dev.close()
                continue
            self.devices[dev.path] = dev
            self.sel.register(dev, selectors.EVENT_READ, dev)
            LOG.info("attached %s", describe(dev))

    def drop(self, dev: InputDevice) -> None:
        try:
            self.sel.unregister(dev)
        except (KeyError, ValueError):
            pass
        self.devices.pop(dev.path, None)
        try:
            dev.close()
        except OSError:
            pass
        LOG.info("detached %s", dev.path)

    # -- events -------------------------------------------------------------

    def emit(self, name: str, extra: dict) -> None:
        payload = {"input": name, "action": extra.pop("action", "event")}
        payload.update(extra)
        LOG.debug("emit %s", payload)
        self.sender.send(payload)

    def handle(self, ev, now: float, product: int = 0) -> None:
        if ev.type != ecodes.EV_KEY:
            return
        names = key_names(ev.code)
        if not names:
            return
        if self.codes.handle(names, ev.value, now):
            return

        if self.game and self.game.poll() is None:
            # a game owns the screen: the keyboard belongs to it (we already
            # ungrabbed it), and the only thing we still answer is C on the
            # numpad, which quits back to tcOS.
            if product != self.numpad_product:
                return
            if ev.value == 1 and "KEY_ESC" in names:
                self.end_game()
            return

        if ev.value == 1 and product == self.numpad_product:
            # the remote wakes the telly; the light snake's own controls
            # (joystick, ABXY) deliberately do not -- they are for the shelf
            # lights and must work with the TV dark
            self.tv_power("tv_on")

        if ev.value == 1:
            # ring buffer of what actually arrived, exposed on /status --
            # the only way to tell "key not received" from "key not routed"
            # without sitting at the TV
            self.recent.append("%s%s" % (
                names[0], "@numpad" if product == self.numpad_product else ""))
        target = None
        if product and product == self.numpad_product:
            for n in names:
                if n in self.numpad_bindings:
                    target = self.numpad_bindings[n]
                    break
        if target is None:
            for n in names:
                if n in self.bindings:
                    target = self.bindings[n]
                    break
        if target is None:
            return

        # Kernel autorepeat (value 2) is deliberately ignored: this keyboard
        # advertises no EV_REP, so nothing would ever arrive. We generate our
        # own hold ticks in tick_holds() instead, at a rate we control.
        if ev.value == 1:
            self.emit(target, {"action": "press"})
            self._route(target, "press")
            if target in self.repeating:
                self._held[target] = now + self.hold_interval
        elif ev.value == 0:
            self._held.pop(target, None)
            if target in self.release_for:
                self.emit(target, {"action": "release"})
                self._route(target, "release")

    def _wled(self, target: str, action: str) -> None:
        """Apply an input to the light cursor. Shelf changes and colours fire
        on press only -- there are just four shelves, so repeating past them
        at 16Hz would be useless. The band itself repeats, which is what makes
        a held stick sweep instead of nudge."""
        r = self.renderer
        if not r.enabled:
            return
        if action == "press":
            if target == self.act_shelf_prev:
                return r.shelf_step(-1)
            if target == self.act_shelf_next:
                return r.shelf_step(1)
            if target in self.act_explode:
                return r.explode()
            if target in self.colors:
                return r.set_color(self.colors[target])
        if target == self.act_band_fwd:
            r.band_step(1)
        elif target == self.act_band_back:
            r.band_step(-1)

    def tick_holds(self, now: float) -> None:
        for target, due in list(self._held.items()):
            if now >= due:
                self._held[target] = now + self.hold_interval
                self.emit(target, {"action": "hold"})
                self._route(target, "hold")

    TRANSPORT = ("tv_volup", "tv_voldown", "tv_playpause",
                 "tv_prevfile", "tv_nextfile")

    def _route(self, target: str, action: str) -> None:
        """ONE routing table, no modes.

        The numpad owns the TV and TelecommanderOS; the joystick and ABXY own
        the light snake. Since transport moved to the numpad's own keys they
        no longer overlap at all, so the snake/tv toggle was pure friction --
        and the source of a whole bug family (numpad dead after a restart,
        ABXY silently swallowed while "tv mode" was on). Deleted.
        """
        if target.startswith("tv_tt_"):
            if action == "press" and self.tt.enabled:
                self.tt.key(target[6:])
            return

        if target == "tv_dot":
            # context key: OS open -> flip subpages; during playback ->
            # aspect toggle. Runtime panscan is safe on the GL path.
            if action == "press":
                if self.tt.enabled and (self.tt.is_chat()
                                        or self.tt.is_news()):
                    self.tt.key("toggle")     # mode / scroll
                elif self.tt.enabled and self.tt.visible:
                    self.tt.key("plus")
                else:
                    ps = self._tv_query("panscan")
                    if isinstance(ps, (int, float)) and ps > 0.05:
                        self._tv_cmd(["set_property", "panscan", 0.0])
                        self._tv_cmd(["show-text", "Original (Letterbox)", 1500])
                    else:
                        self._tv_cmd(["set_property", "panscan",
                                      self._auto_panscan()])
                        self._tv_cmd(["show-text", "Vollbild", 1500])
            return

        if target in self.TRANSPORT:
            # While music plays the OS is not sitting IN FRONT of the player,
            # it IS the player's screen: the analyser is the picture and the
            # page rides on top of it in MIX. So the transport keys have to go
            # on being transport keys, exactly as they do with the OS shut.
            music_on = self.tt.enabled and bool(self.tt.music)
            os_open = self.tt.enabled and self.tt.visible and not music_on
            chat = self.tt.enabled and (self.tt.is_chat()
                                        or self.tt.is_news())
            if target == "tv_volup":
                if os_open:
                    if action == "press":
                        self.tt.key("plus") if chat else self.tt.page_step(1)
                elif action in ("press", "hold"):
                    self._volume_step(1)
            elif target == "tv_voldown":
                if os_open:
                    if action == "press":
                        self.tt.key("minus") if chat else self.tt.page_step(-1)
                elif action in ("press", "hold"):
                    self._volume_step(-1)
            elif target == "tv_playpause" and action == "press":
                if os_open or self.tt.entry:
                    # ENTER stays the terminator of a numeric command -- with
                    # digits half-typed it must finish them even during music,
                    # or 600 ENTER would be unreachable with a track running
                    self.tt.key("enter")
                else:
                    self._tv_cmd(["cycle", "pause"])
            elif target == "tv_prevfile" and action == "press":
                if chat:
                    self.tt.key("prev")       # previous T9 candidate
                elif music_on:
                    self._tv_cmd(["playlist-prev"])
                elif self.tt.live and not os_open:
                    self.tt.channel_step(-1)     # zap down
                else:
                    self._transport_prev()
            elif target == "tv_nextfile" and action == "press":
                if chat:
                    self.tt.key("next")       # next T9 candidate
                elif music_on:
                    self._tv_cmd(["playlist-next"])
                elif os_open:
                    # inside the OS, x shows the QR for whatever page offers
                    # one (news articles) -- there is no "next file" to go to
                    u = self.tt.qr_url()
                    if u:
                        self.tt.show_qr(u, "Beitrag auf dem Handy \u00f6ffnen")
                elif self.tt.live:
                    self.tt.channel_step(1)      # zap up
                elif self.tt.enabled and not self.tt.random:
                    self.tt.play_step(1)
                else:
                    self._tv_cmd(["playlist-next"])
            return

        # everything else is the light snake. "release" fires for every
        # repeating target, and _wled's band-step check is not gated to
        # press/hold -- forwarding it would nudge the band on every let-go.
        if action == "release":
            return
        self._wled(target, action)

    def next_hold(self) -> float | None:
        return min(self._held.values()) if self._held else None

    def _transport_prev(self) -> None:
        """Spotify-style back: restart the current title, or within the
        first seconds jump to the previous one. Outside random mode both
        actions go through the OS's list context; a plain seek-0 is reserved
        for random mode's direct files -- seeking a live transcode pipe
        forces a stream reopen (it once killed a running movie)."""
        pos = self._tv_query("playback-time")
        early = pos is not None and pos < self.tv_restart_threshold
        if self.tt.enabled and not self.tt.random:
            if early:
                self.tt.play_step(-1)
            else:
                self.tt.replay()
        else:
            if early:
                self._tv_cmd(["playlist-prev"])
            else:
                self._tv_cmd(["seek", 0, "absolute"])

    def _volume_step(self, delta: int) -> None:
        """Volume in whole notches, never finer.

        Ten from silent to full: coarse enough that one tap is always audible
        (2%% steps meant holding the key just to hear a difference), and it
        gives the meter something meaningful to show. The level is read back
        from mpv rather than counted here, so it stays right no matter what
        else moved it.
        """
        steps = self.tv_vol_steps
        size = 100.0 / steps
        try:
            level = int(round(float(self._tv_query("volume")) / size))
        except (TypeError, ValueError):
            level = steps
        level = max(0, min(steps, level + delta))
        self._tv_cmd(["set_property", "volume", level * size])
        if self.tt.enabled:
            self.tt.show_volume(level, steps)

    # -- TV mode: drive mpv over its IPC socket ------------------------------

    def _tv_cmd(self, command: list) -> None:
        if not self.tv_socket:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.tv_cmd_timeout)
                s.connect(self.tv_socket)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
        except OSError as exc:
            LOG.debug("mpv command %s failed: %s", command, exc)

    def _tv_query(self, prop: str):
        if not self.tv_socket:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.tv_cmd_timeout)
                s.connect(self.tv_socket)
                s.sendall((json.dumps({"command": ["get_property", prop],
                                       "request_id": 1}) + "\n").encode())
                data = s.recv(4096)
        except OSError as exc:
            LOG.debug("mpv query %s failed: %s", prop, exc)
            return None
        for line in data.decode(errors="ignore").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("request_id") == 1 and "data" in obj:
                return obj["data"]
        return None

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        if self.sender.enabled:
            self.sender.start()
        else:
            LOG.info("Home Assistant output disabled (no webhook_url)")
        if self.tt.enabled and self.tv_enabled and self.tv_socket:
            # a retrokb restart orphans any overlay painted by the previous
            # process (it lives in mpv) -- the fresh state says "not visible"
            # and never removes it. Clear unconditionally at startup.
            self._tv_cmd(["overlay-remove", self.tt.overlay_id])
            self._tv_cmd(["overlay-remove", self.tt.saver.overlay_id])
            MpvEvents(self.tv_socket, self._on_start_file, self._on_idle,
                      self._on_end_file, self._on_playing).start()
            ControlServer(self, 8204).start()
            LOG.info("TelecommanderOS armed (broadcaster %s, control :8204)",
                     self.tt.broadcaster)
        if self.renderer.enabled:
            self.renderer.start()
            LOG.info("WLED renderer: %d shelves, %d leds, band %dpx, step %dpx",
                     len(self.renderer.hosts), self.renderer.leds,
                     self.renderer.band_len, self.renderer.step_px)
        next_scan = 0.0
        while self.running:
            now = time.monotonic()
            if now >= next_scan:
                self.discover()
                next_scan = now + REDISCOVER_INTERVAL

            timeout = next_scan - now
            for deadline in (self.codes.next_deadline(), self.next_hold(),
                             self.tt.next_deadline() if self.tt.enabled else None):
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - now))
            timeout = max(0.01, min(timeout, REDISCOVER_INTERVAL))

            for sel_key, _ in self.sel.select(timeout):
                dev = sel_key.data
                try:
                    events = list(dev.read())
                except OSError:
                    self.drop(dev)
                    continue
                stamp = time.monotonic()
                for ev in events:
                    self.handle(ev, stamp, dev.info.product)

            now = time.monotonic()
            self.tick_holds(now)
            self.codes.tick(now)
            if self.tt.enabled:
                self.tt.tick()
                self._watch_sample()

        for dev in list(self.devices.values()):
            self.drop(dev)
        self.renderer.stop()
        if self.sender.enabled:
            self.sender.stop()
        return 0

    def _auto_panscan(self) -> float:
        """Scope-aware Vollbild: crop fully up to ~16:9 sources, but cap the
        crop for cinemascope so at most ~28% of the width is lost -- a full
        2.39:1 -> 4:3 crop discards 44% and looks like a face-zoom. The thin
        letterbox that remains is how scope was always shown on 4:3 sets."""
        ar = self._tv_query("video-params/aspect")
        if not isinstance(ar, (int, float)) or ar <= 1.45:
            return 1.0
        lose = 1.0 - (4.0 / 3.0) / ar        # width lost at full panscan
        if lose <= 0.28:
            return 1.0
        return max(0.0, min(1.0, 0.28 / lose))

    def _on_start_file(self) -> None:
        """mpv began a file; if it is the carrier, the OS comes up.
        Real content gets the scope-aware crop applied per file."""
        time.sleep(0.3)
        path = self._tv_query("path") or ""
        if path.startswith("av://"):
            if not self.tt.random:
                self.tt.on_carrier()
            return
        self.tt.update_saver()       # authoritative: real file -> no logo
        time.sleep(0.7)      # video params need the decoder to settle
        self._tv_cmd(["set_property", "panscan", self._auto_panscan()])

    def _flash_room(self, seconds: float = 0.18) -> None:
        """A red blink across every light in the room, then straight back.

        The strips go over realtime UDP, which reverts by itself. Anything
        Home Assistant owns and we cannot address directly (the skylights)
        goes through a webhook that snapshots and restores on that side.
        """
        self.renderer.flash((255, 0, 0), seconds)
        if self.flash_url:
            def _post():
                try:
                    req = urllib.request.Request(
                        self.flash_url, data=b"{}", method="POST",
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=3).close()
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    LOG.debug("flash webhook failed: %s", exc)
            threading.Thread(target=_post, daemon=True).start()

    def tv_power(self, action: str) -> None:
        """Ask Home Assistant to switch the Saba's plug. Fire-and-forget in a
        thread: a sleeping HA must never make a keypress feel slow."""
        if not self.tvp_url or action not in ("tv_on", "tv_off"):
            return
        if action == "tv_on":
            now = time.monotonic()
            if now - self._tvp_last < self.tvp_debounce:
                return          # one wake per debounce window, not per key
            self._tvp_last = now

        def _post():
            try:
                req = urllib.request.Request(
                    self.tvp_url, data=json.dumps({"action": action}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=4).read()
                LOG.info("TV plug: %s", action)
            except (OSError, ValueError) as exc:
                LOG.warning("TV plug %s failed: %s", action, exc)

        threading.Thread(target=_post, daemon=True).start()

    # -- emulator ----------------------------------------------------------

    def _local_rom(self, remote: str) -> str:
        for rr, lr in self.emu_roots.items():
            if remote.startswith(rr):
                return lr + remote[len(rr):]
        return remote

    def launch_game(self, remote_rom: str) -> None:
        """Hand the TV over to the emulator.

        Three things have to happen in order, and each is a hard constraint
        rather than a preference:
          1. tvplayer must die first -- DRM has exactly one master, so mpv
             has to release the display before RetroArch can take it.
          2. the KEYBOARD gets ungrabbed so RetroArch can see the joystick
             and ABXY; the NUMPAD stays grabbed so C can always quit back to
             tcOS even while a game owns the screen.
          3. while a game runs we ignore keyboard events ourselves, or the
             same presses would drive the light snake behind the game.
        """
        if not self.emu_enabled or not self.emu_cmd:
            LOG.warning("emulator disabled or not configured")
            return
        if self.game and self.game.poll() is None:
            return
        # Check EVERYTHING before touching the display. Stopping tvplayer to
        # launch a binary that turns out to be missing would black the TV for
        # nothing -- and the emulator is deliberately installed later than
        # the pages that link to it.
        if shutil.which(self.emu_cmd[0]) is None:
            LOG.warning("emulator not installed (%s) -- ignoring launch",
                        self.emu_cmd[0])
            self.tt.err = "Emulator fehlt"
            self.tt.repaint()
            return
        core = None
        for i, a in enumerate(self.emu_cmd):
            if a == "-L" and i + 1 < len(self.emu_cmd):
                core = self.emu_cmd[i + 1]
        if core and not os.path.exists(core):
            LOG.warning("libretro core missing: %s", core)
            self.tt.err = "NES-Kern fehlt"
            self.tt.repaint()
            return
        rom = self._local_rom(remote_rom)
        if not os.path.exists(rom):
            LOG.warning("rom not found: %s", rom)
            self.tt.err = "ROM nicht gefunden"
            self.tt.repaint()
            return
        # Announce it BEFORE the handoff: the card is drawn by mpv, so it
        # dies with mpv -- but it covers the moment the user actually
        # notices, and the console behind it is blanked (see noconsole
        # service) so the gap is black rather than a shell prompt.
        self.tt.saver.deactivate()
        self.tt.visible = False
        self.tt.show_loading("Das Spiel startet gleich")
        time.sleep(1.2)
        self.tt._loading = None
        subprocess.run(["systemctl", "stop", "tvplayer"])
        time.sleep(2)                      # let mpv release DRM master
        for dev in self.devices.values():
            if dev.info.product != self.numpad_product:
                try:
                    dev.ungrab()
                except OSError:
                    pass
        LOG.info("launching %s", os.path.basename(rom))
        try:
            # Run the emulator as the SAME user that owns the display.
            # retrokb is root, but mpv runs as `martin`; launching RetroArch
            # as root left DRM master in a state mpv could not take back --
            # the TV showed the bare console instead of the OS until
            # tvplayer was restarted by hand. Matching users keeps the
            # handoff clean in both directions.
            import pwd
            pw = pwd.getpwnam(self.emu_user)
            env = dict(os.environ,
                       HOME=pw.pw_dir,
                       USER=self.emu_user,
                       XDG_RUNTIME_DIR="/run/user/%d" % pw.pw_uid)
            self.game = subprocess.Popen(
                self.emu_cmd + [rom], env=env,
                user=pw.pw_uid, group=pw.pw_gid,
                extra_groups=[g.gr_gid for g in __import__("grp").getgrall()
                              if self.emu_user in g.gr_mem],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            LOG.warning("emulator failed to start: %s", exc)
            self.game = None
            self.end_game()
            return
        threading.Thread(target=self._await_game, daemon=True).start()

    def _await_game(self) -> None:
        if self.game:
            self.game.wait()
        self.end_game()

    def end_game(self) -> None:
        """Take the TV back: kill the emulator, re-grab the keyboard, restore
        the player, and come up in tcOS where the user left off."""
        g, self.game = self.game, None
        if g and g.poll() is None:
            g.terminate()
            try:
                g.wait(timeout=5)
            except subprocess.TimeoutExpired:
                g.kill()
        for dev in list(self.devices.values()):
            if dev.info.product != self.numpad_product and self.grab:
                try:
                    dev.grab()
                except OSError:
                    pass
        time.sleep(2)          # let the emulator fully release DRM master
        subprocess.run(["systemctl", "start", "tvplayer"])
        # Wait for the new mpv to actually accept IPC before talking to it --
        # tvplayer's socket does not exist the instant systemd returns, and a
        # command sent into the void would leave the screen black with no OS.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.tv_socket and os.path.exists(self.tv_socket)                     and self._tv_query("idle-active") is not None:
                break
            time.sleep(0.4)
        self.tt.after_restart()      # always land back on the index page
        LOG.info("game ended, player restored")

    def _watch_sample(self) -> None:
        """Accumulate viewing time for the file on screen. Sampled rather
        than derived from playback-time: a jump forward must not credit the
        skipped minutes as watched."""
        now = time.monotonic()
        key = self.tt._remote_of_current()
        playing = (key and not self.tt.visible
                   and self._tv_query("pause") is False)
        if key != self._w_key:
            self._w_key, self._w_secs = key, 0.0
        elif playing and self._w_last:
            delta = now - self._w_last
            if delta < 30:            # ignore gaps (suspend, restart, sleep)
                self._w_secs += delta
        self._w_last = now

    def _on_end_file(self, reason) -> None:
        """Count a completion only for a natural end AND only if most of the
        runtime was actually watched -- skipping to the last minute reaches
        eof too, and Martin explicitly does not want that to count."""
        if reason != "eof" or not self._w_key:
            return
        key, secs = self._w_key, self._w_secs
        self._w_key, self._w_secs = None, 0.0
        dur = self.tt._runtime()
        if not dur or secs < 0.85 * dur:
            LOG.info("not counted as watched: %.0fs of %ss", secs, dur or "?")
            return
        try:
            body = json.dumps({"path": key}).encode()
            req = urllib.request.Request(
                self.tt.broadcaster + "/tt/watched", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                n = json.load(r).get("count")
            LOG.info("watched %s (now %s)", key.split("/")[-1], n)
            self.tt._pcache.clear()      # so the new colour shows at once
        except (OSError, ValueError) as exc:
            LOG.warning("could not record watch: %s", exc)

    def _on_playing(self) -> None:
        """Frames are flowing: drop the loading card (unless it is the dead
        channel, which is not something anyone waits for)."""
        path = self._tv_query("path") or ""
        if not path.startswith("av://"):
            self.tt.clear_loading()

    def _on_idle(self) -> None:
        """Nothing left to play: tune the dead channel (OS follows via
        the start-file event it triggers)."""
        if not self.tt.random:
            self._tv_cmd(["loadfile", teletext_mod.CARRIER, "replace", -1,
                          "loop-file=inf"])

    def shutdown(self, *_args) -> None:
        LOG.info("shutting down")
        self.running = False


# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--probe", action="store_true", help="log key events, grab nothing")
    ap.add_argument("--list", action="store_true", help="list input devices and exit")
    ap.add_argument("--dry-run", action="store_true", help="log instead of POSTing")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.list:
        return cmd_list()

    rules: list[dict] = []
    if args.probe:
        try:
            rules = load_config(args.config).get("devices", [])
        except (OSError, tomllib.TOMLDecodeError):
            pass  # probing before a config exists is the normal case
        return cmd_probe(rules)

    try:
        cfg = load_config(args.config)
    except OSError as exc:
        LOG.error("cannot read config %s: %s", args.config, exc)
        return 1
    except tomllib.TOMLDecodeError as exc:
        LOG.error("invalid config %s: %s", args.config, exc)
        return 1

    if not cfg.get("devices"):
        LOG.error("config has no [[devices]] rules -- run `retrokb --probe` first")
        return 1
    if not (cfg.get("homeassistant", {}).get("webhook_url")
            or cfg.get("wled", {}).get("enabled")):
        LOG.error("config has no output: set homeassistant.webhook_url, "
                  "or wled.enabled, or both")
        return 1

    svc = Service(cfg, args.dry_run)
    signal.signal(signal.SIGTERM, svc.shutdown)
    signal.signal(signal.SIGINT, svc.shutdown)
    return svc.run()


if __name__ == "__main__":
    sys.exit(main())
