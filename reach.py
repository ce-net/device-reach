"""net.device-reach — reach a named device's TCP ports over ce-net.

The library half. Stdlib only. Owns no device record: it reads the tables that
already exist (ce-devices `access.json` first) and asks the local ce node to
bind each forward, so the capability check stays where it already is.

Three verdicts everywhere: a thing is UP, DOWN, or UNKNOWN. A port that was
never probed is UNKNOWN, never "down".
"""

import json
import os
import socket
import statistics
import subprocess
import time
import urllib.error
import urllib.request

VERSION = "0.1.0"
APP = "net.device-reach"

# Where the device tables live. ce-devices owns the first one; the second is
# this machine's own additions and may not exist.
CE_DEV_TABLE = os.environ.get(
    "CE_DEV_TABLE", os.path.expanduser("~/dev/ce-devices/access.json"))
LOCAL_TABLE = os.environ.get(
    "REACH_TABLE", os.path.expanduser("~/.config/device-reach/devices.json"))
CE = os.environ.get("REACH_CE", "ce")
NODE_API = os.environ.get("CE_NODE_API", "http://127.0.0.1:8844")
TUNNEL_TIMEOUT = float(os.environ.get("REACH_TUNNEL_TIMEOUT", "30"))


class Note:
    """Something a source could not tell us. Never a silent empty list."""

    def __init__(self, source, text):
        self.source, self.text = source, text

    def as_dict(self):
        return {"source": self.source, "note": self.text}


def _read_table(path, notes, source, optional=False):
    if not os.path.exists(path):
        if not optional:
            notes.append(Note(source, "no table at %s" % path))
        return {}
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        notes.append(Note(source, "%s: %s" % (path, e)))
        return {}
    devs = doc.get("devices")
    if not isinstance(devs, dict):
        notes.append(Note(source, "%s has no 'devices' object" % path))
        return {}
    for d in devs.values():
        d["_source"] = source
    return devs


def load_devices():
    """Every device this machine knows how to reach, merged, plus the notes.

    Later tables win per device, but a field the winner does not state keeps
    the earlier value rather than becoming null.
    """
    notes = []
    merged = {}
    for path, source, optional in ((CE_DEV_TABLE, "ce-devices/access.json", False),
                                   (LOCAL_TABLE, "device-reach/devices.json", True)):
        for name, dev in _read_table(path, notes, source, optional).items():
            if name in merged:
                base = dict(merged[name])
                base.update({k: v for k, v in dev.items() if v is not None})
                merged[name] = base
            else:
                merged[name] = dev
    return merged, notes


def declared_ports(dev):
    """{name: remote_port} for one device. ssh is always declared."""
    ports = {"ssh": 22}
    for name, port in (dev.get("ports") or {}).items():
        ports[name] = int(port)
    return ports


def local_port_for(dev, remote):
    """The loopback port a remote port is forwarded to.

    Deliberately the SAME rule ce-dev uses (`10000 + remote` below 10000, with
    `port_local` overriding), so the two tools name the same port and never
    open two forwards to one place. ssh keeps the device's `ssh_local`.
    """
    remote = int(remote)
    override = (dev.get("port_local") or {}).get(str(remote))
    if override:
        return int(override)
    if remote == 22 and dev.get("ssh_local"):
        return int(dev["ssh_local"])
    return 10000 + remote if remote < 10000 else remote


def listening(port, host="127.0.0.1", timeout=0.3):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


def node_up():
    """(bool, detail) — is the local ce node answering?"""
    try:
        with urllib.request.urlopen(NODE_API + "/status", timeout=3) as r:
            doc = json.loads(r.read().decode())
        return True, doc.get("node_id", "")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return False, str(e)


def open_forward(dev, remote, local=None):
    """Make 127.0.0.1:<local> reach the device's <remote> port over ce-net.

    Returns (local_port, state, detail) where state is one of
    `already` | `opened` | `failed`. The capability is NOT handled here: `ce
    tunnel` attaches the wallet entry for the target and the node refuses
    without one, so there is exactly one place that can say yes.
    """
    local = local or local_port_for(dev, remote)
    if listening(local):
        return local, "already", "127.0.0.1:%d was already bound" % local
    wallet = dev.get("wallet")
    if not wallet:
        return local, "failed", "device has no wallet alias, so no capability can be attached"
    try:
        r = subprocess.run([CE, "tunnel", wallet, "%d:%d" % (local, int(remote))],
                           capture_output=True, text=True, timeout=TUNNEL_TIMEOUT)
    except FileNotFoundError:
        return local, "failed", "no `%s` binary on PATH" % CE
    except subprocess.TimeoutExpired:
        return local, "failed", "ce tunnel did not return in %.0f s" % TUNNEL_TIMEOUT
    if r.returncode:
        return local, "failed", (r.stderr or r.stdout).strip()[:400]
    deadline = time.time() + 10
    while time.time() < deadline:
        if listening(local):
            return local, "opened", "bound 127.0.0.1:%d -> %s:%d" % (local, wallet, int(remote))
        time.sleep(0.1)
    return local, "failed", "ce tunnel returned 0 but 127.0.0.1:%d never bound" % local


def http_probe(url, timeout=5.0):
    """(status, seconds, detail). status is an int, or None when it did not answer."""
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read(2048)
            return r.status, time.perf_counter() - t, ""
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter() - t, ""
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return None, time.perf_counter() - t, str(e)[:200]


def latency(url, n=20, timeout=5.0):
    """p50/p90 in ms over n GETs, and how many answered. Never invents a number."""
    ok = []
    fails = 0
    for _ in range(n):
        status, secs, _ = http_probe(url, timeout)
        if status is None:
            fails += 1
        else:
            ok.append(secs * 1000.0)
    if not ok:
        return {"n": n, "answered": 0, "p50_ms": None, "p90_ms": None,
                "min_ms": None, "max_ms": None, "verdict": "CANNOT DETERMINE"}
    ok.sort()
    return {
        "n": n, "answered": len(ok), "failed": fails,
        "p50_ms": round(statistics.median(ok), 1),
        "p90_ms": round(ok[min(len(ok) - 1, int(round(0.9 * (len(ok) - 1))))], 1),
        "min_ms": round(ok[0], 1), "max_ms": round(ok[-1], 1),
        "verdict": "PASS" if fails == 0 else "PARTIAL",
    }


def port_rows(name, dev, probe=False):
    """One row per declared port of one device."""
    rows = []
    for pname, remote in sorted(declared_ports(dev).items()):
        local = local_port_for(dev, remote)
        up = listening(local)
        row = {
            "device": name, "port": pname, "remote": remote, "local": local,
            "state": "up" if up else "down",
            "url": "http://127.0.0.1:%d" % local if pname != "ssh" else None,
            "http": None,
        }
        if probe and up and row["url"]:
            status, secs, detail = http_probe(row["url"] + "/", timeout=4)
            row["http"] = {"status": status, "ms": round(secs * 1000, 1), "detail": detail}
        rows.append(row)
    return rows


def status(probe=False):
    """The whole picture: node, devices, forwards, notes."""
    devs, notes = load_devices()
    up, detail = node_up()
    out = {
        "app": APP, "version": VERSION, "ts": time.time(),
        "node": {"reachable": up, "detail": detail, "api": NODE_API},
        "devices": [], "notes": [n.as_dict() for n in notes],
    }
    for name, dev in sorted(devs.items()):
        rows = port_rows(name, dev, probe=probe)
        out["devices"].append({
            "name": name,
            "what": dev.get("what"),
            "node_id": dev.get("node_id"),
            "wallet": dev.get("wallet"),
            "lan": dev.get("lan"),
            "source": dev.get("_source"),
            "ports": rows,
            "reachable_ports": sum(1 for r in rows if r["state"] == "up"),
        })
    return out
