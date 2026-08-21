#!/usr/bin/env python3
"""
tvgate - starts/stops tvplayer based on whether the Saba TV has power,
and watchdogs the vc4 DRM wedge.

The TV is fed through a Shelly Plus Plug S ("telecommander", 192.168.1.216).
Its state is polled directly over the Shelly's local RPC -- deliberately NOT
via Home Assistant, so the HA VM migrating or restarting can never take the
TV logic down with it. HA still sees the same plug for everything else.

Why gate at all: tvplayer streams from the Unraid array around the clock;
with the TV off that is pure HDD wear for a picture nobody sees. Stopping
tvplayer also lets the NFS automounts (x-systemd.idle-timeout=600) release,
so the array disks can actually spin down.

Unreachable plug = no change: better to leave the current state alone than
to kill a running movie because of a WiFi blip.

THE WEDGE WATCHDOG: the Pi 2's vc4 driver can get stuck rejecting every
atomic commit ("Failed to commit atomic request: Error number 22" flooding
the journal). mpv never backs out: audio keeps playing, the screen shows
nothing, the retry loop pegs the CPU until even sshd starves. Observed
triggers so far: a display-lease race during service restart, and a 4:3
overlay-plane reconfig (that one is disabled in retrokb). Because the box
becomes unreachable, recovery cannot depend on a human: tvgate scans the
journal for the flood, restarts tvplayer, and if the wedge survives several
restarts (kernel-level), reboots the Pi outright. An unattended toy
appliance rebooting itself beats a dead TV and a walk to the power plug.
"""
import json
import subprocess
import sys
import time
import urllib.request

SHELLY = "http://192.168.1.216/rpc/Switch.GetStatus?id=0"
UNIT = "tvplayer.service"
# 4 s, not 15: this is the delay between switching the set on and getting a
# picture, and the whole of it is spent looking at a blank screen. One cheap
# HTTP call to a plug on the same LAN is not worth stretching that out.
POLL_S = 4

# Two distinct ways the display goes wrong, both needing the same cure:
#  1. the vc4 atomic-commit loop (audio plays, screen frozen/black)
#  2. mpv losing the DRM handoff entirely -- after an emulator exits or a
#     restart races, mpv comes up with NO video output at all. That one is a
#     single line, not a flood, so it gets its own threshold of 1.
WEDGE_MARKER = "Failed to commit atomic request"
WEDGE_THRESHOLD = 20          # marker lines in the last 30s = wedged
NOVIDEO_MARKER = "Error opening/initializing the selected video_out"
WEDGE_RESTART_LIMIT = 3       # wedge-restarts within this window -> reboot
WEDGE_WINDOW_S = 900


def log(msg):
    print("tvgate: %s" % msg, file=sys.stderr, flush=True)


def tv_has_power():
    try:
        with urllib.request.urlopen(SHELLY, timeout=4) as r:
            return bool(json.load(r).get("output"))
    except (OSError, ValueError):
        return None


def unit_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0


def wedged() -> bool:
    out = subprocess.run(
        ["journalctl", "-u", UNIT, "--since", "-30s", "-o", "cat"],
        capture_output=True, text=True)
    if NOVIDEO_MARKER in out.stdout:
        log("mpv has no video output -- lost the DRM handoff")
        return True
    return out.stdout.count(WEDGE_MARKER) >= WEDGE_THRESHOLD


def main() -> int:
    log("watching Shelly plug %s -> %s (wedge watchdog armed)"
        % (SHELLY.split("/rpc")[0], UNIT))
    unreachable_logged = False
    wedge_restarts = []
    while True:
        # -- watchdog first: a wedged box may not survive to the next poll --
        if unit_active() and wedged():
            now = time.monotonic()
            wedge_restarts = [t for t in wedge_restarts if now - t < WEDGE_WINDOW_S]
            wedge_restarts.append(now)
            if len(wedge_restarts) >= WEDGE_RESTART_LIMIT:
                log("DRM wedge survived %d restarts in %ds -- REBOOTING"
                    % (len(wedge_restarts), WEDGE_WINDOW_S))
                subprocess.run(["systemctl", "reboot"])
                time.sleep(60)
                continue
            log("DRM commit wedge detected -- restarting %s (%d/%d in window)"
                % (UNIT, len(wedge_restarts), WEDGE_RESTART_LIMIT))
            subprocess.run(["systemctl", "stop", UNIT])
            time.sleep(3)   # let the dying mpv fully release DRM master
            subprocess.run(["systemctl", "start", UNIT])
            time.sleep(POLL_S)
            continue

        power = tv_has_power()
        if power is None:
            if not unreachable_logged:
                log("plug unreachable -- leaving %s as it is" % UNIT)
                unreachable_logged = True
        else:
            unreachable_logged = False
            active = unit_active()
            if power and not active:
                log("TV powered on -> starting %s" % UNIT)
                subprocess.run(["systemctl", "start", UNIT])
            elif not power and active:
                log("TV powered off -> stopping %s" % UNIT)
                subprocess.run(["systemctl", "stop", UNIT])
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
