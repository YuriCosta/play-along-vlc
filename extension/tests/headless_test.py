#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless integration test for the Play Along extension.

Starts the server, runs VLC WITHOUT a GUI (-I luaintf) executing the real logic of
play_along.lua (through harness_intf.lua) and, with a Python device on the other side,
checks pause/play/seek/rate in both directions, monarchy rejections and session end.

    python headless_test.py [--vlc "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"] [--aout dummy]

Requires VLC 3.x installed. The harness is copied temporarily into the user's lua/intf
folder (e.g. %APPDATA%\\vlc\\lua\\intf\\pa_test.lua) and removed at the end. The user's
play_along.json is backed up and restored.
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "server"))
from sim_client import Client  # noqa: E402

SERVER = os.path.join(ROOT, "server", "play_along_server.py")
EXT = os.path.join(ROOT, "extension", "play_along.lua")
HARNESS = os.path.join(HERE, "harness_intf.lua")
WORKDIR = os.path.join(HERE, "_work")
PORT = 8765
BASE = "http://127.0.0.1:%d" % PORT
CMD = os.path.join(WORKDIR, "pa_cmd.txt")
STATE = os.path.join(WORKDIR, "pa_state.txt")


def user_intf_dir():
    if sys.platform.startswith("win"):
        return os.path.join(os.environ["APPDATA"], "vlc", "lua", "intf")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/org.videolan.vlc/lua/intf")
    return os.path.expanduser("~/.local/share/vlc/lua/intf")


def user_data_dir():
    """VLC user data folder (where the extension writes play_along.json)."""
    if sys.platform.startswith("win"):
        return os.path.join(os.environ["APPDATA"], "vlc")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/org.videolan.vlc")
    return os.path.expanduser("~/.local/share/vlc")


def default_vlc():
    if sys.platform.startswith("win"):
        return r"C:\Program Files\VideoLAN\VLC\vlc.exe"
    if sys.platform == "darwin":
        return "/Applications/VLC.app/Contents/MacOS/VLC"
    return "vlc"


def make_wav(path, seconds=300):
    if os.path.exists(path):
        return
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * (8000 * seconds))


def send(cmd):
    with open(CMD, "w") as f:
        f.write(cmd)


def state():
    try:
        with open(STATE, encoding="utf-8", errors="replace") as f:
            parts = f.read().split("|", 4)
    except (FileNotFoundError, PermissionError):
        return None
    if len(parts) < 5:
        return None
    try:
        return {"status": parts[0], "pos": float(parts[1]), "rate": float(parts[2]),
                "code": parts[3].strip(), "html": parts[4]}
    except ValueError:
        return None


def wait_for(pred, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = state()
        if st and pred(st):
            return st
        time.sleep(0.25)
    return None


def scan_check(vlc_exe, aout, wav):
    """Runs VLC with the Qt interface for a few seconds so it scans the extensions folder.

    That scan executes the script in an EMPTY Lua state (no libraries at all), so any
    library call at the top level of play_along.lua makes the extension vanish from the
    View menu. The headless harness cannot catch that; this does.
    Returns (ok, lines) — ok is None when Qt did not scan at all (e.g. no display).
    """
    ext_dir = os.path.join(user_data_dir(), "lua", "extensions")
    os.makedirs(ext_dir, exist_ok=True)
    dst = os.path.join(ext_dir, "pa_scan_check.lua")
    shutil.copyfile(EXT, dst)
    log = os.path.join(WORKDIR, "vlc_scan.log")
    try:
        os.remove(log)
    except FileNotFoundError:
        pass
    try:
        p = subprocess.Popen([vlc_exe, "--no-one-instance", "--qt-start-minimized", "--verbose=2",
                              "--file-logging", "--logfile=" + log, "--aout=" + aout, "--no-video",
                              "--run-time=4", "--play-and-exit", wav])
        try:
            p.wait(40)
        except subprocess.TimeoutExpired:
            p.kill()
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    text = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as f:
            text = f.read()
    lines = [l for l in text.splitlines() if "pa_scan_check.lua" in l]
    if not lines:
        return None, lines
    ok = any("capability flags" in l for l in lines) and not any("Error loading" in l for l in lines)
    return ok, lines


def run(vlc_exe, aout):
    checks = []

    def chk(cond, msg):
        checks.append((bool(cond), msg))
        print(("OK    " if cond else "FAIL  ") + msg, flush=True)

    os.makedirs(WORKDIR, exist_ok=True)
    wav = os.path.join(WORKDIR, "silence5m.wav")
    make_wav(wav)

    print("== Scan: Qt interface loads the extension (empty Lua state)", flush=True)
    ok, lines = scan_check(vlc_exe, aout, wav)
    if ok is None:
        print("SKIP  Qt interface did not scan extensions (no display?)", flush=True)
    else:
        chk(ok, "extension scanned without errors")
        if not ok:
            for l in lines:
                print("      " + l)
    for p in (CMD, STATE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    send("")
    intf_dir = user_intf_dir()
    os.makedirs(intf_dir, exist_ok=True)
    harness_dst = os.path.join(intf_dir, "pa_test.lua")
    shutil.copyfile(HARNESS, harness_dst)

    # The extension writes server/name/language to play_along.json; keep the user's copy.
    cfg_path = os.path.join(user_data_dir(), "play_along.json")
    cfg_backup = None
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            cfg_backup = f.read()

    server_log = open(os.path.join(WORKDIR, "server.log"), "w")
    server = subprocess.Popen([sys.executable, SERVER, "--host", "127.0.0.1", "--port", str(PORT),
                               "--check-interval", "1", "--cooldown", "1", "-v"],
                              stdout=server_log, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    # No ":port" on purpose: exercises the extension's 8765 default.
    # PLAY_ALONG_LANG=en: the harness finds the buttons by their English text.
    env = dict(os.environ, PA_EXT=EXT, PA_WORKDIR=WORKDIR, PA_SERVER="127.0.0.1", PLAY_ALONG_LANG="en")
    vlc_log = os.path.join(WORKDIR, "vlc.log")
    vlc = subprocess.Popen([vlc_exe, "-I", "luaintf", "--lua-intf=pa_test", "--verbose=2",
                            "--file-logging", "--logfile=" + vlc_log, "--aout=" + aout, "--no-video", wav],
                           env=env)
    try:
        st = wait_for(lambda s: s["status"] == "playing" and s["pos"] > 1.0, 30)
        chk(st is not None, "headless VLC playing (pos=%s)" % (st and st["pos"]))
        if st is None:
            return checks

        print("== Scenario 1: VLC creates the session (monarchy), Python follows", flush=True)
        send("create monarchy")
        st = wait_for(lambda s: len(s["code"]) == 4, 10)
        chk(st is not None, "extension created the session: %s" % (st and st["code"]))
        if st is None:
            return checks
        code = st["code"]
        time.sleep(3)
        st = state()
        chk("Devices (1)" in st["html"], "status lists 1 device")
        a = Client(BASE, "pyA", position=0.0)
        r = a.join(code)
        chk(r.get("ok") and abs(a.position - st["pos"]) < 6,
            "pyA joined and synced (%.1f vs VLC %.1f)" % (a.position, st["pos"]))
        r = a.action("pause")
        chk(r.get("accepted") is False and a.playing, "follower pyA's pause rejected and corrected")
        a.set_position(a.position + 20)
        got = a.wait_commands(6, lambda c: "seek" in str(c.get("reason", "")))
        chk(any("seek" in str(c.get("reason", "")) for c in got),
            "pyA 20 s ahead received a seek (now %.1f vs VLC %.1f)" % (a.position, state()["pos"]))
        send("pause")
        wait_for(lambda s: s["status"] == "paused", 5)
        a.wait_commands(6, lambda c: c.get("playing") is False)
        chk(a.playing is False, "local pause in VLC (monarch) propagated to pyA")
        send("play")
        a.wait_commands(6, lambda c: c.get("playing") is True)
        chk(a.playing is True, "local play in VLC propagated to pyA")
        send("seek 200")
        a.wait_commands(8, lambda c: isinstance(c.get("position"), (int, float)) and abs(c["position"] - 200) < 6)
        chk(abs(a.position - 200) < 8, "local seek in VLC to 200 propagated to pyA (%.1f)" % a.position)
        send("rate 1.5")
        a.wait_commands(8, lambda c: abs((c.get("rate") or 0) - 1.5) < 0.01)
        chk(abs(a.rate - 1.5) < 0.01, "rate 1.5x in VLC propagated to pyA")
        send("rate 1")
        a.wait_commands(8, lambda c: abs((c.get("rate") or 0) - 1.0) < 0.01)
        chk(abs(a.rate - 1.0) < 0.01, "rate 1.0x in VLC propagated to pyA")
        send("leave")
        st = wait_for(lambda s: "Not in a session" in s["html"], 8)
        chk(st is not None, "VLC left the session")
        a.leave()

        print("== Scenario 2: Python creates the session (monarchy), VLC follows", flush=True)
        owner = Client(BASE, "pyMonarch", position=100.0)
        owner.create("monarchy")
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                try:
                    owner.sync(wait_ms=500)
                except Exception as exc:  # noqa: BLE001
                    print("pump error:", exc)
                    time.sleep(0.5)

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        send("join " + owner.code)
        st = wait_for(lambda s: abs(s["pos"] - owner.position) < 3 and s["status"] == "playing", 10)
        chk(st is not None, "VLC joined and jumped to the monarch's position (%s vs %.1f)" % (st and st["pos"], owner.position))
        owner.action("pause")
        st = wait_for(lambda s: s["status"] == "paused", 8)
        chk(st is not None, "Python monarch's pause paused VLC")
        owner.action("play")
        st = wait_for(lambda s: s["status"] == "playing", 8)
        chk(st is not None, "Python monarch's play resumed VLC")
        owner.action("seek", position=50.0)
        st = wait_for(lambda s: abs(s["pos"] - owner.position) < 3, 8)
        chk(st is not None, "Python monarch's seek to 50 moved VLC (%s)" % (st and st["pos"]))
        owner.action("rate", rate=1.25)
        st = wait_for(lambda s: abs(s["rate"] - 1.25) < 0.01, 8)
        chk(st is not None, "Python monarch's 1.25x rate applied in VLC")
        owner.action("rate", rate=1.0)
        wait_for(lambda s: abs(s["rate"] - 1.0) < 0.01, 8)
        time.sleep(4)
        send("seek 150")
        st = wait_for(lambda s: abs(s["pos"] - owner.position) < 3 and s["pos"] < 120, 12)
        chk(st is not None, "VLC's local seek (follower) was rejected and undone (%s vs %.1f)" % (st and st["pos"], owner.position))
        stop.set()
        th.join(3)
        owner.end()
        st = wait_for(lambda s: "Not in a session" in s["html"], 8)
        chk(st is not None, "session end by the Python monarch reached VLC")
    finally:
        send("quit")
        try:
            vlc.wait(15)
        except subprocess.TimeoutExpired:
            vlc.kill()
        server.terminate()
        server_log.close()
        try:
            os.remove(harness_dst)
        except OSError:
            pass
        try:
            if cfg_backup is None:
                os.remove(cfg_path)
            else:
                with open(cfg_path, "wb") as f:
                    f.write(cfg_backup)
        except OSError:
            pass
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(description="Headless test of the Play Along extension")
    ap.add_argument("--vlc", default=default_vlc(), help="path to the VLC executable")
    ap.add_argument("--aout", default="dummy", help="audio output module (default: dummy, silent)")
    args = ap.parse_args(argv)
    checks = run(args.vlc, args.aout)
    fails = sum(1 for ok, _ in checks if not ok)
    print("\n%d checks, %d failures (logs in %s)" % (len(checks), fails, WORKDIR))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
