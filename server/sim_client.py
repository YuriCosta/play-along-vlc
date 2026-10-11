#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simulation / test client for the Play Along server.

Simulates devices (no VLC involved) speaking the same protocol as the extension and checks
the anarchy/monarchy rules and the drift corrections. Also a reference for how a client
should behave.

Usage (with the server already running):
    python sim_client.py --server 127.0.0.1:8765

For a fast run, start the server with frequent checks:
    python play_along_server.py --check-interval 1 --cooldown 1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import List, Optional


class Client:
    """Simulated device: keeps its own media clock and applies server commands."""

    def __init__(self, base: str, name: str, position: float = 0.0, playing: bool = True,
                 rate: float = 1.0):
        self.base = base.rstrip("/")
        self.name = name
        self.id = "%s-%d" % (name, int(time.time() * 1000) % 100000)
        self.code: Optional[str] = None
        self._pos = position
        self._mark = time.monotonic()
        self.playing = playing
        self.rate = rate
        self.received: List[dict] = []
        self.last_response: Optional[dict] = None

    # --- simulated media clock ------------------------------------------
    @property
    def position(self) -> float:
        if self.playing:
            return self._pos + (time.monotonic() - self._mark) * self.rate
        return self._pos

    def _freeze(self):
        self._pos = self.position
        self._mark = time.monotonic()

    def set_position(self, pos: float):
        self._freeze()
        self._pos = pos

    def set_playing(self, playing: bool):
        self._freeze()
        self.playing = playing

    def set_rate(self, rate: float):
        self._freeze()
        self.rate = rate

    # --- protocol ----------------------------------------------------------
    def post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def state(self) -> dict:
        return {"playing": self.playing, "position": self.position, "rate": self.rate}

    def create(self, policy: str) -> dict:
        r = self.post("/api/create", {"device_id": self.id, "device_name": self.name,
                                      "policy": policy, "state": self.state(),
                                      "media": {"name": "simulation.mp3", "length": 600}})
        if r.get("ok"):
            self.code = r["session"]["code"]
        self.last_response = r
        return r

    def join(self, code: str) -> dict:
        r = self.post("/api/join", {"code": code, "device_id": self.id, "device_name": self.name,
                                    "state": self.state()})
        if r.get("ok"):
            self.code = code
            self.apply(r.get("commands", []), r.get("server_now"))
        self.last_response = r
        return r

    def leave(self) -> dict:
        return self.post("/api/leave", {"code": self.code, "device_id": self.id})

    def end(self) -> dict:
        return self.post("/api/end", {"code": self.code, "device_id": self.id})

    def sync(self, wait_ms: int = 0, action: Optional[dict] = None) -> dict:
        body = {"code": self.code, "device_id": self.id, "device_name": self.name,
                "state": self.state(), "wait_ms": wait_ms}
        if action:
            body["action"] = action
        r = self.post("/api/sync", body)
        self.last_response = r
        # Even on error (e.g. session_not_found) the server may send commands (session_ended).
        self.apply(r.get("commands", []), r.get("server_now"))
        return r

    def apply(self, commands: List[dict], server_now: Optional[float]):
        for cmd in commands:
            self.received.append(cmd)
            if cmd.get("type") != "set_state":
                continue
            if isinstance(cmd.get("rate"), (int, float)):
                self.set_rate(float(cmd["rate"]))
            if isinstance(cmd.get("position"), (int, float)):
                target = float(cmd["position"])
                if cmd.get("playing") and server_now is not None and cmd.get("at") is not None:
                    target += (server_now - cmd["at"]) * self.rate
                self.set_position(target)
            if isinstance(cmd.get("playing"), bool):
                self.set_playing(cmd["playing"])

    def action(self, atype: str, **kw) -> dict:
        """Performs the action locally and reports it to the server (like the extension does)."""
        if atype == "pause":
            self.set_playing(False)
        elif atype == "play":
            self.set_playing(True)
        elif atype == "seek":
            self.set_position(kw["position"])
        elif atype == "rate":
            self.set_rate(kw["rate"])
        act = {"type": atype, "position": self.position, "rate": self.rate, "playing": self.playing}
        act.update(kw)
        return self.sync(0, act)

    def wait_commands(self, timeout: float, predicate=None) -> List[dict]:
        """Syncs until a command (matching predicate) arrives or the timeout expires."""
        deadline = time.monotonic() + timeout
        got: List[dict] = []
        while time.monotonic() < deadline:
            before = len(self.received)
            self.sync(wait_ms=1000)
            new = self.received[before:]
            got.extend(new)
            if new and (predicate is None or any(predicate(c) for c in new)):
                return got
        return got


# --------------------------------------------------------------------------- tests
class Check:
    def __init__(self):
        self.fails = 0
        self.total = 0

    def __call__(self, cond: bool, msg: str):
        self.total += 1
        print(("  OK   " if cond else "  FAIL ") + " - " + msg)
        if not cond:
            self.fails += 1


def reason_has(cmd: dict, word: str) -> bool:
    return word in str(cmd.get("reason", ""))


def run(base: str, wait: float) -> int:
    chk = Check()
    is_state = lambda c: c.get("type") == "set_state"  # noqa: E731

    print("== Monarchy")
    owner = Client(base, "monarch", position=100.0)
    r = owner.create("monarchy")
    chk(r.get("ok") is True and len(owner.code or "") == 4, "session created: %s" % owner.code)

    a = Client(base, "followerA", position=0.0)
    r = a.join(owner.code)
    chk(r.get("ok") is True and abs(a.position - 100) < 1.5,
        "followerA joined and synced to %.1f" % a.position)

    # a follower's action in monarchy is rejected and corrected
    r = a.action("pause")
    chk(r.get("accepted") is False, "follower's pause rejected")
    chk(a.playing is True and abs(a.position - owner.position) < 1.5,
        "followerA corrected (playing at %.1f, monarch at %.1f)" % (a.position, owner.position))

    # large drift -> seek
    b = Client(base, "followerB", position=0.0)
    b.join(owner.code)
    b.set_position(b.position + 20)
    owner.sync()
    got = b.wait_commands(wait, is_state)
    chk(any(reason_has(c, "seek") for c in got), "followerB 20 s ahead received a seek")
    chk(abs(b.position - owner.position) < 1.5, "followerB realigned (%.1f vs %.1f)" % (b.position, owner.position))

    # small drift above tolerance -> short pause
    c = Client(base, "followerC", position=0.0)
    c.join(owner.code)
    c.set_position(c.position + 6)
    got = c.wait_commands(wait, lambda cmd: reason_has(cmd, "pause for"))
    chk(any(reason_has(cmd, "pause for") for cmd in got), "followerC 6 s ahead received a short pause")
    got = c.wait_commands(5, lambda cmd: reason_has(cmd, "resume"))
    chk(any(reason_has(cmd, "resume") for cmd in got) and c.playing,
        "followerC resumed after the pause (now %.1f vs monarch %.1f)" % (c.position, owner.position))

    # the monarch's actions propagate
    r = owner.action("pause")
    chk(r.get("accepted") is True, "monarch's pause accepted")
    a.sync()
    chk(a.playing is False and abs(a.position - owner.position) < 1.5, "followerA paused along")
    owner.action("seek", position=200.0)
    owner.action("play")
    a.sync()
    chk(a.playing is True and abs(a.position - owner.position) < 1.5,
        "followerA followed the seek to 200 and resumed (%.1f)" % a.position)
    owner.action("rate", rate=1.5)
    a.sync()
    chk(abs(a.rate - 1.5) < 0.01, "followerA followed the 1.5x rate")

    # session end
    owner.end()
    r = a.sync()
    chk(any(cmd.get("type") == "session_ended" for cmd in a.received), "followerA notified of the session end")

    print("== Anarchy")
    owner = Client(base, "creator", position=10.0)
    owner.create("anarchy")
    x = Client(base, "anyone", position=0.0)
    x.join(owner.code)
    r = x.action("seek", position=300.0)
    chk(r.get("accepted") is True, "participant's seek accepted in anarchy")
    owner.sync()
    chk(abs(owner.position - x.position) < 1.5, "creator followed the seek (%.1f)" % owner.position)
    r = x.action("pause")
    owner.sync()
    chk(r.get("accepted") is True and owner.playing is False, "participant's pause propagated to the creator")
    x.leave()
    owner.leave()

    print("\n%d checks, %d failures" % (chk.total, chk.fails))
    return 1 if chk.fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Play Along server simulation / test")
    ap.add_argument("--server", default="127.0.0.1:8765", help="server host:port")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="max time waiting for drift corrections (depends on the server's --check-interval)")
    args = ap.parse_args(argv)
    base = args.server if args.server.startswith("http") else "http://" + args.server
    return run(base, args.wait)


if __name__ == "__main__":
    sys.exit(main())
