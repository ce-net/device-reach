"""net.device-reach — reach a named device's TCP ports over ce-net.

The library half. Stdlib only. Owns no device record: it reads the tables that
already exist (ce-devices `access.json` first) and asks the local ce node to
bind each forward, so the capability check stays where it already is.

Three verdicts everywhere: a thing is UP, DOWN, or UNKNOWN. A port that was
never probed is UNKNOWN, never "down".
"""

import json
import os
import re
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


CE_IAM = os.environ.get("REACH_CE_IAM", "ce-iam")
NAME_RE = re.compile(r"^(?P<name>[a-z0-9-]{1,63})\s+->\s+(?P<node>[0-9a-f]{64})\b")


def name_bindings():
    """({name: node_id}, note_or_None) from `ce-iam name ls`.

    These are SIGNED name-to-node bindings, resolved through this machine's accepted roots.
    They are the mesh's own answer to "which node is `huey`", where a device table is only
    this machine's opinion. Bind one with `ce-iam name bind <name> <node id>`.
    """
    try:
        r = subprocess.run([CE_IAM, "name", "ls"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return {}, Note("ce-iam name", "no `%s` on PATH, so no signed names" % CE_IAM)
    except subprocess.TimeoutExpired:
        return {}, Note("ce-iam name", "`%s name ls` did not return in 10 s" % CE_IAM)
    if r.returncode:
        return {}, Note("ce-iam name", (r.stderr or r.stdout).strip()[:200])
    out = {}
    for line in r.stdout.splitlines():
        m = NAME_RE.match(line.strip())
        if m:
            out[m.group("name")] = m.group("node")
    return out, None


def load_devices(names=True):
    """Every device this machine knows how to reach, merged, plus the notes.

    Three sources, in order: the signed name bindings, the ce-devices table, this machine's
    own additions. A later source wins per device, but a field it does not state keeps the
    earlier value rather than becoming null, and a signed binding that DISAGREES with the
    table keeps the table's node id and leaves a note. A disagreement is a fact to look at,
    not a tie to break quietly.
    """
    notes = []
    merged = {}
    if names:
        bound, note = name_bindings()
        if note:
            notes.append(note)
        for name, node_id in bound.items():
            merged[name] = {
                "node_id": node_id, "wallet": name, "_source": "ce-iam name",
                "name_bound": True,
                "what": "named on the mesh; no ports declared in any table",
            }
    for path, source, optional in ((CE_DEV_TABLE, "ce-devices/access.json", False),
                                   (LOCAL_TABLE, "device-reach/devices.json", True)):
        for name, dev in _read_table(path, notes, source, optional).items():
            if name in merged:
                base = dict(merged[name])
                bound_id = base.get("node_id") if base.get("name_bound") else None
                base.update({k: v for k, v in dev.items() if v is not None})
                if bound_id and dev.get("node_id") and dev["node_id"] != bound_id:
                    notes.append(Note("ce-iam name", (
                        "%s: the signed binding says %s… and the table says %s…; using the "
                        "table. Re-bind or fix the table, do not leave both."
                    ) % (name, bound_id[:12], dev["node_id"][:12])))
                    base["name_conflict"] = True
                base["name_bound"] = bool(bound_id)
                merged[name] = base
            else:
                merged[name] = dev
    return merged, notes


def declared_ports(dev):
    """{name: remote_port} for one device.

    A device from a table gets ssh for free, because every such device was reached by ssh
    before this app existed. A device known ONLY from a signed name binding declares nothing:
    the binding says which node the name is, not what that node serves, and guessing an ssh
    port there would have the keepalive dialling machines nobody asked it to.
    """
    ports = {}
    if dev.get("_source") != "ce-iam name":
        ports["ssh"] = 22
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


def udp_held(port):
    """True when something holds 127.0.0.1:<port> for UDP.

    A UDP socket and a TCP listener are different namespaces, so `listening()` would answer
    a different question and answer it confidently. This binds a UDP socket and reads the
    operating system's refusal.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def wallet_cap(alias):
    """(cap_hex, detail). The capability token this machine holds under an alias."""
    try:
        r = subprocess.run([CE_IAM, "wallet", "show", alias, "--json"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "cannot run %s wallet show: %s" % (CE_IAM, e)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:300]
    try:
        cap = (json.loads(r.stdout) or {}).get("cap")
    except ValueError:
        return None, "ce-iam wallet show %s did not answer JSON" % alias
    return (cap, "") if cap else (None, "no capability token under alias %r" % alias)


# POST /udp-tunnel does not exist on a node built before datagrams crossed the mesh. A 404 there
# is a node too old to carry UDP, which is a different thing from a forward that failed.
UDP_UNSUPPORTED = (
    "this ce node cannot forward datagrams: POST /udp-tunnel answered 404. UDP over the mesh is "
    "ce/crates/ce-node/src/api.rs (POST /udp-tunnel) and /ce/udp/1 in ce-mesh, on branch "
    "udp-tunnel."
)


def open_udp_forward(dev, remote, local=None):
    """Make 127.0.0.1:<local> reach the device's <remote> UDP port over ce-net.

    (local_port, state, detail), state one of `already` | `opened` | `failed` | `unsupported`.
    Unlike the TCP path this talks to the node API directly, because `ce tunnel` is TCP only;
    the capability comes from the same wallet alias, so the node still has exactly one place
    that can say yes.
    """
    local = local or local_port_for(dev, remote)
    if udp_held(local):
        return local, "already", "udp 127.0.0.1:%d was already bound" % local
    node = dev.get("node_id")
    if not node:
        return local, "failed", "device has no node id"
    cap, detail = (None, "")
    if dev.get("wallet"):
        cap, detail = wallet_cap(dev["wallet"])
    body = {"node_id": node, "local_port": int(local), "remote_port": int(remote)}
    if cap:
        body["caps"] = cap
    status, doc, why = _node_json("/udp-tunnel", "POST", body, timeout=30.0)
    if status == 404:
        return local, "unsupported", UDP_UNSUPPORTED
    if status == 409:
        return local, "failed", (doc or {}).get("error") or "127.0.0.1:%d is already forwarded" % local
    if status != 200:
        return local, "failed", why or "POST /udp-tunnel answered %s: %s" % (status, doc)
    if not udp_held(local):
        return local, "failed", "the node answered 200 but udp 127.0.0.1:%d never bound" % local
    return local, "opened", "bound udp 127.0.0.1:%d -> %s:%d%s" % (
        local, node[:12], int(remote), "" if cap else " (no capability attached: " + detail + ")")


def _data_dir():
    """The ce data dir, where the node writes api.token."""
    if os.environ.get("CE_DATA_DIR"):
        return os.environ["CE_DATA_DIR"]
    mac = os.path.expanduser("~/Library/Application Support/ce")
    return mac if os.path.isdir(mac) else os.path.expanduser("~/.local/share/ce")


def node_api_token():
    """(token, detail). The node gates every non-GET on this; a missing one is named."""
    if os.environ.get("CE_API_TOKEN"):
        return os.environ["CE_API_TOKEN"], "from CE_API_TOKEN"
    path = os.path.join(_data_dir(), "api.token")
    try:
        with open(path) as f:
            t = f.read().strip()
        return (t, path) if t else (None, "%s is empty" % path)
    except OSError as e:
        return None, "cannot read %s: %s" % (path, e)


def _node_json(path, method="GET", body=None, timeout=6.0):
    """(status, doc, detail) against the local node API. Never raises."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(NODE_API + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        token, detail = node_api_token()
        if not token:
            return None, None, "no node API token (%s); the node refuses every non-GET" % detail
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None), ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), ""
        except ValueError:
            return e.code, None, raw[:300]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return None, None, str(e)[:200]


# The node answers 404 on /tunnels until it carries the tunnel registry. That is a node too old
# to close a forward, and it is reported as such rather than as "no tunnels open".
CLOSE_UNSUPPORTED = (
    "this ce node has no tunnel registry: GET /tunnels answered 404. A forward on such a node "
    "cannot be closed and lasts until the node restarts. The route is "
    "ce/crates/ce-node/src/api.rs (GET /tunnels, DELETE /tunnel) on branch tunnel-close."
)


def node_tunnels():
    """(rows, supported, detail). Every forward the local node holds, or why we cannot know."""
    status, doc, detail = _node_json("/tunnels")
    if status == 404:
        return [], False, CLOSE_UNSUPPORTED
    if status != 200 or not isinstance(doc, list):
        return [], None, detail or "GET /tunnels answered %s" % status
    return doc, True, ""


def close_forward(local_port):
    """(ok, detail). Stop forwarding a local port and free it, or say why not."""
    status, doc, detail = _node_json("/tunnel", "DELETE", {"local_port": int(local_port)})
    if status == 200:
        d = doc or {}
        return True, "closed 127.0.0.1:%s -> %s:%s after %ss and %s connection(s)" % (
            local_port, (d.get("node_id") or "?")[:12], d.get("remote_port"),
            d.get("open_for_secs"), d.get("conns"))
    if status == 404:
        if isinstance(doc, dict) and "no forward" in str(doc.get("error", "")):
            return False, "the node holds no forward on 127.0.0.1:%s" % local_port
        return False, CLOSE_UNSUPPORTED
    if status == 401:
        return False, "the node refused: no API token (see `reach doctor`)"
    return False, detail or "DELETE /tunnel answered %s: %s" % (status, doc)


# ----- the mesh path: who is connected, over what, and at what cost -----

# A libp2p PeerId for an Ed25519 key is the identity multihash of the protobuf-encoded public
# key, base58btc. A ce node id IS that public key in hex, so the two are the same fact written
# twice and the mapping needs no lookup and no network. Checked against this node's own
# /status, which prints both.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    for c in raw:
        if c != 0:
            break
        out = "1" + out
    return out


def peer_id_for(node_id):
    """The libp2p peer id of a ce node id (64 hex), or None if that is not a node id."""
    try:
        key = bytes.fromhex(node_id)
    except (ValueError, TypeError):
        return None
    if len(key) != 32:
        return None
    pb = b"\x08\x01\x12\x20" + key          # protobuf PublicKey{Type: Ed25519, Data: key}
    return _b58(b"\x00" + bytes([len(pb)]) + pb)  # identity multihash, base58btc


# An older node answers /netgraph without `addr` and `relayed`, so it can say a peer is 128 ms
# away but not whether those milliseconds went through a relay. That is a node too old to
# answer the question, not a mesh with no relays on it.
PATH_UNSUPPORTED = (
    "this ce node does not report the path behind a measurement: its /netgraph rows carry no "
    "`addr` or `relayed` field, so a relayed hop and a direct one are indistinguishable here. "
    "The fields are ce/crates/ce-node/src/api.rs (GET /netgraph) on branch netgraph-path."
)


def net_paths():
    """(rows, supported, detail). Every connected peer: its RTT, its address, direct or relayed.

    `supported` is False when the node is too old to say, None when nothing could be read, and
    True when the rows carry the path. A row's `device` is filled in when the peer id derives
    from a node id this machine knows by name.
    """
    status_code, doc, detail = _node_json("/netgraph")
    if status_code != 200 or not isinstance(doc, list):
        return [], None, detail or "GET /netgraph answered %s" % status_code
    devs, _ = load_devices()
    by_peer = {}
    for name, dev in devs.items():
        pid = peer_id_for(dev.get("node_id") or "")
        if pid:
            by_peer[pid] = name
    rows = []
    for r in doc:
        if not isinstance(r, dict):
            continue
        rows.append({
            "peer": r.get("peer"),
            "device": by_peer.get(r.get("peer")),
            "rtt_ms": r.get("rtt_ms"),
            "samples": r.get("samples"),
            "last_seen_secs": r.get("last_seen_secs"),
            "addr": r.get("addr"),
            "relayed": r.get("relayed"),
        })
    if not doc:
        return rows, None, "this node has no connected peers, so there is no path to measure"
    supported = all("relayed" in r for r in doc if isinstance(r, dict))
    return rows, supported, "" if supported else PATH_UNSUPPORTED


# ----- the grant policy: what this machine should hold, against what it holds -----

CE_DEV_POLICY = os.path.expanduser("~/dev/ce-devices/policy.json")
LOCAL_POLICY = os.path.expanduser("~/.config/device-reach/policy.json")

# A capability is only as good as its narrowest field. These are the three that can be wider
# than intended without anything ever failing, which is why they are checked rather than
# trusted: abilities beyond the job, no port restriction, and no expiry.
POLICY_MISSING = (
    "no grant policy on this machine. A policy says what each device's capability SHOULD be, "
    "so a grant that is wider than the job can be seen instead of merely working. Write one at "
    "%s (or %s); `reach policy --example` prints a starting point." % (CE_DEV_POLICY, LOCAL_POLICY)
)


def load_policy():
    """(policy, source, detail). The first policy file that exists, or None with the reason."""
    for path in (LOCAL_POLICY, CE_DEV_POLICY):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            return None, path, "cannot read %s: %s" % (path, e)
        if not isinstance(doc.get("devices"), dict):
            return None, path, "%s has no `devices` object" % path
        return doc, path, ""
    return None, None, POLICY_MISSING


def wallet_grants():
    """(grants, detail). Every held grant, decoded. Keyed by alias; the token is never read."""
    out = {}
    try:
        listing = subprocess.run([CE_IAM, "wallet", "list"], capture_output=True, text=True,
                                 timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return {}, "cannot run %s wallet list: %s" % (CE_IAM, e)
    if listing.returncode != 0:
        return {}, (listing.stderr or listing.stdout).strip()[:300]
    for line in listing.stdout.splitlines():
        alias = line.split()[0] if line.split() else ""
        if not alias:
            continue
        try:
            one = subprocess.run([CE_IAM, "wallet", "show", alias, "--json"],
                                 capture_output=True, text=True, timeout=20)
            doc = json.loads(one.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        scope = doc.get("scope") or {}
        links = scope.get("links") or [{}]
        out[alias] = {
            "alias": alias,
            "node_id": doc.get("node_id") or "",
            "abilities": sorted(scope.get("abilities") or []),
            "resource": scope.get("resource") or "",
            "not_after": scope.get("not_after") or 0,
            # `allowed_ports` is absent on a grant that restricts nothing, and absent is the
            # whole point: it means every port, not no port.
            "ports": sorted(links[0].get("allowed_ports") or []) or None,
            "nonce": links[0].get("nonce"),
            "issuer": links[0].get("issuer") or scope.get("root_issuer") or "",
        }
    return out, ""


def _grant_command(dev_node, self_node, want, nonce):
    ab = " ".join("--action %s" % a for a in want.get("abilities") or [])
    ports = " ".join("--allowed-port %d" % p for p in want.get("ports") or [])
    days = int(want.get("max_days") or 90)
    return ("ce-iam grant --to %s --resource %s %s %s --expires-in %d --nonce %s"
            % (self_node, dev_node, ab, ports, days * 86400, nonce)).replace("  ", " ")


def _grant_findings(g, want, warn_days, now):
    """Every way this one grant is wider, narrower or staler than the policy wants."""
    short, wide = [], []
    need, have = set(want.get("abilities") or []), set(g["abilities"])
    if need - have:
        short.append("missing ability: %s" % ", ".join(sorted(need - have)))
    if have - need:
        wide.append("abilities beyond the policy: %s" % ", ".join(sorted(have - need)))
    ports = want.get("ports")
    if ports and g["ports"] is None:
        wide.append("no port restriction: it tunnels to ANY port, the policy asks for %s"
                    % ", ".join(str(p) for p in ports))
    elif ports and set(g["ports"]) - set(ports):
        wide.append("ports beyond the policy: %s"
                    % ", ".join(str(p) for p in sorted(set(g["ports"]) - set(ports))))
    if not g["not_after"]:
        wide.append("never expires; the policy asks for at most %s days" % want.get("max_days", 90))
    else:
        days = (g["not_after"] - now) / 86400.0
        if days < 0:
            short.append("expired %.1f days ago" % -days)
        elif days < warn_days:
            wide.append("expires in %.1f days" % days)
    return short, wide


def policy_report():
    """(rows, detail). One row per policy line, plus one for everything outside the policy.

    A row's `verdict` is PASS, FAIL or CANNOT DETERMINE, and `findings` says what is wrong in
    words. Nothing here mints, revokes or widens anything: the command that would fix a row is
    printed for a person to run on the device that issues it.

    A device may hold SEVERAL grants. The line is satisfied when one of them covers the job,
    and every grant for that device that is wider than the policy is still a finding of its
    own, named by its alias. A capability nobody looks at is the one that stays too wide.
    """
    policy, source, detail = load_policy()
    if policy is None:
        return [], detail
    grants, gdetail = wallet_grants()
    if gdetail:
        return [], gdetail
    devs, _ = load_devices()
    status_code, doc, _ = _node_json("/status")
    self_node = (doc or {}).get("node_id", "") if status_code == 200 else ""
    defaults = policy.get("defaults") or {}
    warn_days = int(defaults.get("warn_days") or 14)
    now = int(time.time())
    rows, declared = [], set()

    for name, spec in sorted((policy.get("devices") or {}).items()):
        want = dict(defaults, **(spec.get("hold") or spec))
        dev = devs.get(name) or {}
        node = dev.get("node_id") or want.get("node_id") or ""
        if not node:
            rows.append({"device": name, "verdict": UNKNOWN_V, "held": None, "nonce": None,
                         "findings": ["no node id for %r in any device table or in the policy"
                                      % name], "fix": None})
            continue
        mine = [g for g in grants.values() if g["node_id"] == node]
        declared.update(g["alias"] for g in mine)
        if not mine:
            rows.append({"device": name, "verdict": FAIL_V, "held": None, "nonce": None,
                         "findings": ["no grant held for this device"],
                         "fix": _grant_command(node, self_node, want, now)})
            continue
        findings, covered, nonce = [], False, None
        for g in sorted(mine, key=lambda g: g["alias"]):
            short, wide = _grant_findings(g, want, warn_days, now)
            if not short:
                covered = True
                nonce = g["nonce"] if nonce is None else nonce
            findings += ["%s: %s" % (g["alias"], f) for f in short + wide]
        if covered:
            findings = [f for f in findings if "missing ability" not in f]
        else:
            findings.insert(0, "no held grant covers the job (%s)"
                            % ", ".join(want.get("abilities") or ["nothing declared"]))
        rows.append({
            "device": name, "held": ", ".join(sorted(g["alias"] for g in mine)), "nonce": nonce,
            "verdict": PASS_V if not findings else FAIL_V,
            "findings": findings,
            "fix": _grant_command(node, self_node, want, now) if findings else None,
        })

    outside = sorted(a for a in grants if a not in declared)
    if outside:
        shown = outside[:5]
        rows.append({
            "device": None, "held": ", ".join(shown), "nonce": None, "verdict": UNKNOWN_V,
            "findings": ["%d held grant(s) no policy line asks for: %s%s. Outside this app's "
                         "scope, not judged here, and listed so they are not invisible."
                         % (len(outside), ", ".join(shown),
                            "" if len(outside) == len(shown) else ", and %d more"
                            % (len(outside) - len(shown)))],
            "fix": None})
    return rows, ""


PASS_V, FAIL_V, UNKNOWN_V = "PASS", "FAIL", "CANNOT DETERMINE"


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
