#!/usr/bin/env python3
"""
tvplayer - plays a random mix of movies and TV episodes from Unraid over
HDMI (Saba TV via SCART adapter), forever.

Scans /mnt/movies and /mnt/tv, shuffles, and execs into mpv so systemd tracks
mpv's PID directly. Restart=always in the unit means a crash (or an NFS
hiccup) just reshuffles and starts again -- the shelf never sits dark.

Files the Pi 2 cannot play natively are routed through the tvtranscode
gateway on Kadaukeserver (GTX 1060/nvenc) instead of being played directly:
the gateway probes every file's codecs once (cached here across restarts),
and problem files get an http:// playlist entry that arrives as H.264<=720p
+ 2ch AAC -- exactly what the Pi decodes for free. Everything else stays a
plain NFS path. Measured basis for the split: 1080p H.264 + AC3 plays with
zero drops natively, while DTS audio alone pegs the CPU past realtime.

Transcoded entries are live pipes: skip/pause work, rewind/ff do not.

mpv listens on an IPC socket (see [tv] in retrokb.toml) so retrokb can drive
transport controls from the keyboard while in TV mode.
"""
import json
import os
import random
import sys
import urllib.parse
import urllib.request

# /mnt/movies is the whole Movies share; only the curated subfolder is
# real library (matches the broadcaster's SCAN_ROOTS).
ROOTS = ["/mnt/movies/Movies", "/mnt/tv"]
EXTS = (".mkv", ".mp4", ".m4v", ".avi")
PLAYLIST = "/run/tvplayer/playlist.m3u8"
SOCKET = "/run/tvplayer/mpv.sock"

GATEWAY = "http://192.168.1.238:8203"
# NFS paths as the gateway sees them (it runs on the server itself)
REMOTE_ROOT = {"/mnt/movies": "/mnt/user/Movies", "/mnt/tv": "/mnt/user/TV Shows"}
CACHE = "/var/lib/tvplayer/probecache.json"

# DVD-extras-style folders: disproportionately interlaced SD MPEG-2 -- the
# one content class that caused every playback problem so far -- and not
# what "random movie night" means anyway.
EXCLUDE_DIRS = {"bonus", "extras", "extra", "featurettes", "sample", "samples",
                "behind the scenes", "deleted scenes", "trailers", "interviews"}


def log(msg):
    print("tvplayer: %s" % msg, file=sys.stderr, flush=True)


def scan() -> list:
    files = []
    for root in ROOTS:
        if not os.path.isdir(root):
            log("%s not mounted (yet?) -- skipping" % root)
            continue
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDE_DIRS]
            for name in names:
                if name.lower().endswith(EXTS):
                    files.append(os.path.join(dirpath, name))
    return files


def remote_path(local: str) -> str:
    for lroot, rroot in REMOTE_ROOT.items():
        if local.startswith(lroot + "/"):
            return rroot + local[len(lroot):]
    return local


def cache_key(path: str) -> str:
    try:
        st = os.stat(path)
        return "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
    except OSError:
        return path


def gateway_up() -> bool:
    try:
        with urllib.request.urlopen(GATEWAY + "/health", timeout=4) as r:
            return r.status == 200
    except OSError:
        return False


def probe_missing(files: list, cache: dict) -> None:
    """Ask the gateway for codec info on files not yet in the cache."""
    missing = [f for f in files if cache_key(f) not in cache]
    if not missing:
        return
    log("probing %d new files via gateway" % len(missing))
    for i in range(0, len(missing), 100):
        chunk = missing[i:i + 100]
        body = json.dumps({"paths": [remote_path(f) for f in chunk]}).encode()
        req = urllib.request.Request(GATEWAY + "/probe", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                result = json.load(r)
        except (OSError, ValueError) as exc:
            log("probe chunk failed (%s) -- affected files play direct" % exc)
            continue
        for f in chunk:
            info = result.get(remote_path(f))
            if info is not None:
                cache[cache_key(f)] = info
        log("  probed %d/%d" % (min(i + 100, len(missing)), len(missing)))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CACHE)


def needs_transcode(info: dict) -> bool:
    if not info or "error" in info:
        return False          # unknown -> try direct rather than punish it
    if info.get("v") != "h264":
        return True           # mpeg2/hevc/vc1/xvid: no (reliable) hw decode
    if info.get("pix") not in (None, "yuv420p"):
        return True           # hi10p etc: v4l2m2m can't
    if info.get("a") in ("dts", "truehd"):
        return True           # measured: DTS decode alone outruns the Pi 2
    if (info.get("height") or 0) > 720:
        # GL compositing (the wedge fix) can't sustain 1080p on the Pi 2 --
        # movies chugged and one crashed the box. The GTX 1060 delivers
        # everything as <=720p instead, which the tube can't outresolve anyway.
        return True
    return False


def main() -> int:
    files = scan()
    if not files:
        log("no video files found under %s -- nothing to play" % ROOTS)
        return 1

    cache = {}
    try:
        with open(CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        pass

    up = gateway_up()
    if up:
        probe_missing(files, cache)
    else:
        log("transcode gateway unreachable -- everything plays direct")

    random.shuffle(files)
    entries, ntrans = [], 0
    for f in files:
        if up and needs_transcode(cache.get(cache_key(f), {})):
            entries.append(GATEWAY + "/stream?path="
                           + urllib.parse.quote(remote_path(f), safe="/"))
            ntrans += 1
        else:
            entries.append(f)
    log("%d files (%d via gpu transcode), shuffled" % (len(files), ntrans))

    os.makedirs(os.path.dirname(PLAYLIST), exist_ok=True)
    with open(PLAYLIST, "w", encoding="utf-8") as f:
        f.write("\n".join(entries) + "\n")

    # Zero-copy path, hard-won: v4l2m2m-copy round-trips every frame through
    # the CPU and drops frames continuously on this hardware. Plain v4l2m2m
    # hands decoded buffers straight to the DRM overlay plane -- confirmed
    # zero drops over a 25s 1080p test, CPU falling rather than climbing.
    # TelecommanderOS: the TV boots into the OS, not into a shuffle. mpv
    # idles on a generated black "carrier" (a dead channel) that the OS
    # overlay renders onto; random mode is an OS setting persisted here.
    random_on = False
    try:
        with open("/var/lib/tvplayer/os-state.json", encoding="utf-8") as fh:
            random_on = bool(json.load(fh).get("random"))
    except (OSError, ValueError):
        pass
    carrier = "av://lavfi:color=c=black:s=1280x720:r=12"

    argv = [
        "mpv",
        "--hwdec=v4l2m2m",
        "--vo=gpu",
        # Import decoded frames into GL (EGL dmabuf) and composite on the
        # PRIMARY plane -- the same plane subtitles and the console use --
        # instead of the drmprime OVERLAY plane. The overlay plane is what
        # kept wedging vc4 into the atomic-commit-failure loop; the telltale
        # was "subtitles visible, video black": primary plane fine, overlay
        # plane dead. Costs some v3d GL work per frame, saves the display.
        "--gpu-hwdec-interop=drmprime",
        "--gpu-context=drm",
        "--drm-mode=preferred",
        # The tube is a 4:3 CRT and the SCART adapter squeezes the whole
        # 1280x720 frame onto it. Telling mpv the display is 4:3 and forcing
        # panscan makes every source FILL the tube: 4:3 content maps 1:1,
        # 16:9 content loses its sides (per Martin). Safe on the GL path --
        # scaling is shader work, not the overlay-plane reconfig that wedged.
        "--monitoraspect=4:3",
        "--panscan=1.0",
        "--ao=alsa",
        # The Pi has TWO alsa outputs -- card 0 "Headphones" (the physical,
        # unconnected 3.5mm jack) and card 1 "vc4hdmi" (real HDMI audio).
        # --ao=alsa alone left mpv on "auto", which picked the dead jack --
        # silent playback with a perfectly healthy audio decoder underneath.
        "--audio-device=alsa/plughw:CARD=vc4hdmi,DEV=0",
        "--volume=70",
        # auto = only frames actually flagged interlaced. NOT "yes": that
        # ran a bob deinterlacer on everything, doubling render output
        # (estimated-vf-fps read exactly 2x container-fps on progressive
        # files) and pushing the Pi past realtime. Measured, not theory.
        "--deinterlace=auto",
        "--input-ipc-server=" + SOCKET,
        "--idle=yes",
        "--keep-open=no",
        "--msg-level=all=warn",
    ]
    if random_on:
        argv += ["--loop-playlist=inf", "--playlist=" + PLAYLIST]
    else:
        argv += ["--loop-file=inf", carrier]
    os.execvp("mpv", argv)
    return 1  # unreachable if exec succeeds


if __name__ == "__main__":
    sys.exit(main())
