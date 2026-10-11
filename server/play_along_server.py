#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Play Along - synchronization server (management/broker) for the "Play Along" VLC extension.

Standard library only (Python 3.8+). Usage:

    python play_along_server.py --port 8765

Model
-----
* A session has a short code (e.g. K7PQ), a policy ("anarchy" or "monarchy"), an owner
  (whoever created it) and a playback state: playing, position (s), rate and the instant
  "at" (server monotonic clock) when that state was set. The expected position at any
  instant t is: position + (t - at) * rate (while playing).
* Each device sends reports (/api/sync) with its local state. The server compares them
  with the expected position and, at most every --check-interval seconds per device,
  decides the correction:
    - drift within --tolerance: nothing;
    - ahead by up to --tolerance + --max-pause: pause the device for up to --max-pause s;
    - otherwise: tell it to seek to the expected position.
* Local actions (play, pause, seek, rate) arrive in /api/sync (field "action") and go
  through the policy: in "anarchy" any device changes the session state; in "monarchy"
  only the owner. An accepted action becomes the new state and is broadcast to the
  others; a rejected one produces a correction command for the device that tried.
* /api/sync is a long poll: the answer is held for up to wait_ms (capped by --max-wait-ms)
  until there is a command for the device. That gives low latency without timers in VLC.

Routes (POST, JSON body; every API answer is HTTP 200 with an "ok" field):
  /api/create  {device_id, device_name, policy, state, media}
  /api/join    {code, device_id, device_name, state?, media?}
  /api/leave   {code, device_id}
  /api/end     {code, device_id}                         (owner only)
  /api/sync    {code, device_id, device_name?, state, media?, wait_ms?, action?}
GET routes:
  /              HTML dashboard (auto-refreshing)
  /api/sessions  JSON list of sessions

Commands sent to devices carry a short human-readable "reason" (English only).
Error codes: session_not_found, device_id_required, invalid_policy, owner_only,
  unknown_route, invalid_json, internal_error.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

LOG = logging.getLogger("play_along")

POLICIES = ("anarchy", "monarchy")
POLICY_ALIASES = {"anarquia": "anarchy", "monarquia": "monarchy"}  # accepted for compatibility
ACTION_TYPES = ("play", "pause", "seek", "rate")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion


def now() -> float:
    return time.monotonic()


def fnum(value, default: float) -> float:
    try:
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (TypeError, ValueError):
        return default


def fmt_pos(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


class Config:
    def __init__(self, tolerance=5.0, max_pause=2.0, check_interval=10.0, cooldown=3.0,
                 stale=30.0, max_wait_ms=5000):
        self.tolerance = tolerance            # accepted drift (s)
        self.max_pause = max_pause            # longest pause used as a correction (s)
        self.check_interval = check_interval  # minimum time between drift checks per device (s)
        self.cooldown = cooldown              # after sending a command, no drift checks for this long (s)
        self.stale = stale                    # a device silent for longer than this is dropped (s)
        self.max_wait_ms = max_wait_ms        # long-poll cap (ms)


class Device:
    def __init__(self, device_id: str, name: str, t: float):
        self.id = device_id
        self.name = name
        self.joined_at = t
        self.last_seen = t
        self.report: Optional[dict] = None
        self.media: Optional[dict] = None
        self.queue: List[dict] = []                     # commands ready for delivery
        self.scheduled: List[Tuple[float, dict]] = []   # (due time, command) scheduled for later
        self.last_check = 0.0
        self.cooldown_until = 0.0
        self.last_drift: Optional[float] = None
        self.last_command: str = ""


class Session:
    def __init__(self, code: str, policy: str, owner_id: str, state: dict, media: dict):
        self.code = code
        self.policy = policy
        self.owner_id = owner_id
        self.media = media or {}
        self.devices: Dict[str, Device] = {}
        self.created_at = now()
        self.empty_since: Optional[float] = None
        self.ended = False
        self.state = {"playing": False, "position": 0.0, "rate": 1.0, "at": now()}
        self.set_state(bool(state.get("playing")), fnum(state.get("position"), 0.0),
                       fnum(state.get("rate"), 1.0))

    def set_state(self, playing: bool, position: float, rate: float, t: Optional[float] = None):
        if rate <= 0:
            rate = 1.0
        self.state = {"playing": bool(playing), "position": max(0.0, position),
                      "rate": rate, "at": now() if t is None else t}

    def expected_position(self, t: Optional[float] = None) -> float:
        st = self.state
        t = now() if t is None else t
        if st["playing"]:
            return st["position"] + (t - st["at"]) * st["rate"]
        return st["position"]

    def state_command(self, reason: str) -> dict:
        st = self.state
        return {"type": "set_state", "playing": st["playing"], "position": st["position"],
                "rate": st["rate"], "at": st["at"], "reason": reason}

    def describe(self) -> str:
        st = self.state
        return "%s at %s, %.2fx" % ("playing" if st["playing"] else "paused",
                                    fmt_pos(self.expected_position()), st["rate"])


class Broker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.sessions: Dict[str, Session] = {}

    # ----------------------------------------------------------------- helpers
    def _new_code(self) -> str:
        while True:
            code = "".join(random.choice(CODE_ALPHABET) for _ in range(4))
            if code not in self.sessions:
                return code

    @staticmethod
    def _err(error: str, **extra) -> dict:
        d = {"ok": False, "error": error, "server_now": now()}
        d.update(extra)
        return d

    def _session(self, body: dict) -> Tuple[Optional[Session], Optional[dict]]:
        code = str(body.get("code") or "").strip().upper()
        s = self.sessions.get(code)
        if s is None or s.ended:
            return None, self._err("session_not_found", commands=[{"type": "session_ended"}])
        return s, None

    def _add_device(self, s: Session, device_id: str, name) -> Device:
        t = now()
        name = str(name or "").strip() or ("device-" + device_id[-4:])
        d = Device(device_id, name, t)
        s.devices[device_id] = d
        s.empty_since = None
        LOG.info("[%s] %s joined (%d devices)", s.code, d.name, len(s.devices))
        return d

    def _enqueue(self, d: Device, cmd: dict, t: float):
        d.queue.append(cmd)
        d.last_command = cmd.get("reason", cmd.get("type", ""))
        d.cooldown_until = max(d.cooldown_until, t + self.cfg.cooldown)
        self.cond.notify_all()

    def _schedule(self, d: Device, due: float, cmd: dict):
        d.scheduled.append((due, cmd))
        d.scheduled.sort(key=lambda x: x[0])
        d.cooldown_until = max(d.cooldown_until, due + self.cfg.cooldown)
        self.cond.notify_all()

    def _promote_due(self, d: Device):
        t = now()
        while d.scheduled and d.scheduled[0][0] <= t:
            _, cmd = d.scheduled.pop(0)
            d.queue.append(cmd)
            d.last_command = cmd.get("reason", cmd.get("type", ""))

    def _remove_device(self, s: Session, device_id: str, why: str):
        d = s.devices.pop(device_id, None)
        if d is None:
            return
        LOG.info("[%s] %s left (%s)", s.code, d.name, why)
        if s.owner_id == device_id and s.devices:
            new_owner = min(s.devices.values(), key=lambda x: x.joined_at)
            s.owner_id = new_owner.id
            LOG.info("[%s] %s is now the session owner", s.code, new_owner.name)
        if not s.devices:
            s.empty_since = now()

    def _prune(self, s: Session):
        t = now()
        for dev_id in [d.id for d in s.devices.values() if t - d.last_seen > self.cfg.stale]:
            self._remove_device(s, dev_id, "no report for %.0fs" % self.cfg.stale)

    def _sweep_sessions(self):
        t = now()
        for code in list(self.sessions):
            s = self.sessions[code]
            self._prune(s)
            if s.ended or (not s.devices and s.empty_since is not None
                           and t - s.empty_since > 2 * self.cfg.stale):
                LOG.info("[%s] session removed", code)
                del self.sessions[code]

    def _session_info(self, s: Session, me: Optional[Device]) -> dict:
        t = now()
        devices = []
        for d in sorted(s.devices.values(), key=lambda x: x.joined_at):
            rep = d.report or {}
            devices.append({
                "id": d.id, "name": d.name, "is_owner": d.id == s.owner_id,
                "playing": rep.get("playing"), "position": rep.get("position"),
                "rate": rep.get("rate"), "drift": d.last_drift,
                "seen_ago": round(t - d.last_seen, 1),
            })
        owner = s.devices.get(s.owner_id)
        return {
            "code": s.code, "policy": s.policy,
            "owner_id": s.owner_id, "owner_name": owner.name if owner else None,
            "you_are_owner": bool(me and me.id == s.owner_id),
            "state": {"playing": s.state["playing"], "position": s.expected_position(t),
                      "rate": s.state["rate"]},
            "media": s.media, "devices": devices,
        }

    # ----------------------------------------------------------------- rules
    def _apply_action(self, s: Session, d: Device, action: dict) -> bool:
        atype = str(action.get("type") or "")
        if atype not in ACTION_TYPES:
            LOG.info("[%s] %s: unknown action '%s'", s.code, d.name, atype)
            return False
        t = now()
        rep = d.report or {}
        position = fnum(action.get("position"), fnum(rep.get("position"), s.expected_position(t)))
        rate = fnum(action.get("rate"), fnum(rep.get("rate"), s.state["rate"]))
        if atype == "play":
            playing = True
        elif atype == "pause":
            playing = False
        else:
            playing = bool(action.get("playing", rep.get("playing", s.state["playing"])))

        allowed = s.policy == "anarchy" or d.id == s.owner_id
        if not allowed:
            LOG.info("[%s] %s tried '%s' - rejected (monarchy)", s.code, d.name, atype)
            self._enqueue(d, s.state_command("action '%s' rejected: only the monarch controls" % atype), t)
            return False

        s.set_state(playing, position, rate, t)
        LOG.info("[%s] %s: %s -> session %s", s.code, d.name, atype, s.describe())
        cmd = s.state_command("%s by %s" % (atype, d.name))
        for other in s.devices.values():
            if other.id != d.id:
                self._enqueue(other, dict(cmd), t)
        d.cooldown_until = max(d.cooldown_until, t + self.cfg.cooldown)
        d.last_drift = 0.0
        return True

    def _check_device(self, s: Session, d: Device):
        t = now()
        rep = d.report
        if not rep or t < d.cooldown_until or (t - d.last_check) < self.cfg.check_interval:
            return
        d.last_check = t
        st = s.state
        drift = rep["position"] - s.expected_position(rep["at"])
        d.last_drift = drift

        if rep["playing"] != st["playing"] or abs(rep["rate"] - st["rate"]) > 0.01:
            LOG.info("[%s] %s has a different state (%s, %.2fx) - correcting", s.code, d.name,
                     "playing" if rep["playing"] else "paused", rep["rate"])
            self._enqueue(d, s.state_command("state correction (session %s)" % s.describe()), t)
            return
        if abs(drift) <= self.cfg.tolerance:
            return
        if st["playing"] and 0 < drift <= self.cfg.tolerance + self.cfg.max_pause:
            pause_s = min(drift, self.cfg.max_pause)
            LOG.info("[%s] %s is %.1fs ahead - pausing for %.1fs", s.code, d.name, drift, pause_s)
            self._enqueue(d, {"type": "set_state", "playing": False, "position": None,
                              "rate": st["rate"], "at": t,
                              "reason": "correction: pause for %.1f s (%.1f s ahead)" % (pause_s, drift)}, t)
            self._schedule(d, t + pause_s, {"type": "set_state", "playing": True, "position": None,
                                            "rate": st["rate"], "at": t + pause_s,
                                            "reason": "correction: resume"})
            return
        LOG.info("[%s] %s drifted %+.1fs - seeking to the session position", s.code, d.name, drift)
        self._enqueue(d, s.state_command("correction: seek (drift %+.1f s)" % drift), t)

    # ----------------------------------------------------------------- API
    def create(self, body: dict) -> dict:
        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            return self._err("device_id_required")
        policy = str(body.get("policy") or "").strip().lower()
        policy = POLICY_ALIASES.get(policy, policy)
        if policy not in POLICIES:
            return self._err("invalid_policy")
        state = body.get("state") if isinstance(body.get("state"), dict) else {}
        media = body.get("media") if isinstance(body.get("media"), dict) else {}
        with self.cond:
            self._sweep_sessions()
            code = self._new_code()
            s = Session(code, policy, device_id, state, media)
            self.sessions[code] = s
            d = self._add_device(s, device_id, body.get("device_name"))
            d.report = {"playing": s.state["playing"], "position": s.state["position"],
                        "rate": s.state["rate"], "at": now()}
            d.media = media
            LOG.info("[%s] session created by %s (%s): %s", code, d.name, policy, s.describe())
            return {"ok": True, "session": self._session_info(s, d), "commands": [],
                    "server_now": now()}

    def join(self, body: dict) -> dict:
        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            return self._err("device_id_required")
        with self.cond:
            s, err = self._session(body)
            if err:
                return err
            d = s.devices.get(device_id)
            if d is None:
                d = self._add_device(s, device_id, body.get("device_name"))
            else:
                d.name = str(body.get("device_name") or d.name)
                d.last_seen = now()
            if isinstance(body.get("media"), dict):
                d.media = body["media"]
            t = now()
            cmd = s.state_command("initial sync")
            d.cooldown_until = max(d.cooldown_until, t + self.cfg.cooldown)
            d.last_check = t
            return {"ok": True, "session": self._session_info(s, d), "commands": [cmd],
                    "server_now": now()}

    def leave(self, body: dict) -> dict:
        with self.cond:
            s, err = self._session(body)
            if err:
                return err
            self._remove_device(s, str(body.get("device_id") or ""), "left")
            self.cond.notify_all()
            return {"ok": True, "server_now": now()}

    def end(self, body: dict) -> dict:
        with self.cond:
            s, err = self._session(body)
            if err:
                return err
            if str(body.get("device_id") or "") != s.owner_id:
                return self._err("owner_only")
            s.ended = True
            LOG.info("[%s] session ended by the owner", s.code)
            self.sessions.pop(s.code, None)
            self.cond.notify_all()
            return {"ok": True, "server_now": now()}

    def sync(self, body: dict) -> dict:
        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            return self._err("device_id_required")
        wait_ms = min(max(fnum(body.get("wait_ms"), 1000.0), 0.0), float(self.cfg.max_wait_ms))
        with self.cond:
            s, err = self._session(body)
            if err:
                return err
            d = s.devices.get(device_id)
            if d is None:
                d = self._add_device(s, device_id, body.get("device_name"))
            t = now()
            d.last_seen = t
            state = body.get("state")
            if isinstance(state, dict):
                d.report = {"playing": bool(state.get("playing")),
                            "position": fnum(state.get("position"), 0.0),
                            "rate": fnum(state.get("rate"), 1.0), "at": t}
            if isinstance(body.get("media"), dict):
                d.media = body["media"]

            result = {}
            action = body.get("action")
            if isinstance(action, dict):
                result["accepted"] = self._apply_action(s, d, action)
                wait_ms = 0.0

            self._prune(s)
            if s.devices.get(device_id) is d:
                self._check_device(s, d)

            deadline = t + wait_ms / 1000.0
            while True:
                self._promote_due(d)
                if d.queue or s.ended:
                    break
                remaining = deadline - now()
                if remaining <= 0:
                    break
                timeout = remaining
                if d.scheduled:
                    timeout = min(timeout, max(0.0, d.scheduled[0][0] - now()))
                self.cond.wait(timeout)

            commands, d.queue = d.queue, []
            if s.ended:
                commands.append({"type": "session_ended"})
            result.update({"ok": True, "commands": commands,
                           "session": self._session_info(s, d), "server_now": now()})
            return result

    def list_sessions(self) -> dict:
        with self.cond:
            self._sweep_sessions()
            return {"ok": True, "server_now": now(),
                    "sessions": [self._session_info(s, None) for s in self.sessions.values()]}


# --------------------------------------------------------------------- HTTP
ROUTES = {
    "/api/create": Broker.create,
    "/api/join": Broker.join,
    "/api/leave": Broker.leave,
    "/api/end": Broker.end,
    "/api/sync": Broker.sync,
}

DASHBOARD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2"><title>Play Along</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#eee}
h1{font-size:20px}table{border-collapse:collapse;margin:8px 0 24px}td,th{padding:4px 10px;border-bottom:1px solid #333;text-align:left}
.code{font-family:monospace;font-size:18px;color:#8cf}.muted{color:#888}.warn{color:#fc6}</style></head>
<body><h1>Play Along &middot; server</h1><p class="muted">tolerance %(tol).1f s &middot; max pause %(mp).1f s &middot; check every %(ci).0f s</p>
%(body)s</body></html>"""


def render_dashboard(broker: Broker) -> str:
    data = broker.list_sessions()
    parts = []
    if not data["sessions"]:
        parts.append("<p class='muted'>No active sessions.</p>")
    for s in data["sessions"]:
        st = s["state"]
        parts.append("<h2><span class='code'>%s</span> &middot; %s &middot; owner: %s</h2>" % (
            s["code"], s["policy"], (s.get("owner_name") or "?")))
        parts.append("<p>session: %s at <b>%s</b>, %.2fx &middot; media: %s</p>" % (
            "playing" if st["playing"] else "paused", fmt_pos(st["position"]), st["rate"],
            (s.get("media") or {}).get("name") or "?"))
        parts.append("<table><tr><th>device</th><th>state</th><th>position</th><th>drift</th><th>last seen</th></tr>")
        for d in s["devices"]:
            drift = d.get("drift")
            drift_txt = "%+.1f s" % drift if drift is not None else "-"
            cls = " class='warn'" if drift is not None and abs(drift) > broker.cfg.tolerance else ""
            parts.append("<tr><td>%s%s</td><td>%s</td><td>%s</td><td%s>%s</td><td>%.0f s ago</td></tr>" % (
                d["name"], " (owner)" if d["is_owner"] else "",
                "playing" if d.get("playing") else "paused", fmt_pos(d.get("position")),
                cls, drift_txt, d.get("seen_ago") or 0))
        parts.append("</table>")
    return DASHBOARD % {"tol": broker.cfg.tolerance, "mp": broker.cfg.max_pause,
                        "ci": broker.cfg.check_interval, "body": "\n".join(parts)}


class Handler(BaseHTTPRequestHandler):
    broker: Broker = None  # set in main()
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, data: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: dict, status: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(render_dashboard(self.broker).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/sessions":
            self._send_json(self.broker.list_sessions())
        else:
            self._send_json({"ok": False, "error": "unknown_route"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        route = ROUTES.get(path)
        if route is None:
            self._send_json({"ok": False, "error": "unknown_route"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": "invalid_json", "detail": str(exc)})
            return
        try:
            result = route(self.broker, body)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("error handling %s", path)
            result = {"ok": False, "error": "internal_error", "detail": str(exc), "server_now": now()}
        self._send_json(result)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Play Along server (VLC playback synchronization)")
    ap.add_argument("--host", default="0.0.0.0", help="listen address (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765, help="TCP port (default: 8765)")
    ap.add_argument("--tolerance", type=float, default=5.0, help="accepted drift in seconds (default: 5)")
    ap.add_argument("--max-pause", type=float, default=2.0, help="longest correction pause in seconds (default: 2)")
    ap.add_argument("--check-interval", type=float, default=10.0,
                    help="minimum time between drift checks per device (default: 10)")
    ap.add_argument("--cooldown", type=float, default=3.0,
                    help="after sending a command, time without drift checks (default: 3)")
    ap.add_argument("--stale", type=float, default=30.0,
                    help="seconds without a report before a device is dropped (default: 30)")
    ap.add_argument("--max-wait-ms", type=int, default=5000, help="long-poll cap in ms (default: 5000)")
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose log")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = Config(tolerance=args.tolerance, max_pause=args.max_pause, check_interval=args.check_interval,
                 cooldown=args.cooldown, stale=args.stale, max_wait_ms=args.max_wait_ms)
    Handler.broker = Broker(cfg)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    LOG.info("Play Along listening on http://%s:%d/  (tolerance %.1fs, max pause %.1fs, check every %.0fs)",
             args.host, args.port, cfg.tolerance, cfg.max_pause, cfg.check_interval)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
