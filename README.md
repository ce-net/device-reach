# device-reach

**`net.device-reach` — reach a named device's TCP ports over ce-net.** A device is a name, each
of its ports is a name, and `reach port huey servo` prints a loopback URL that goes to that
machine wherever it is: direct over libp2p on a LAN, relayed when it is not.

```
$ reach ls
huey       75ccba6e3540f4e5 Huey the Microduck: Arduino UNO Q CPH08 (servo bus daemon :8938)
    servo    up   :8938   -> http://127.0.0.1:18938
    ssh      up   :22     -> 127.0.0.1:2238
    walk     up   :8940   -> http://127.0.0.1:18940

$ reach get huey walk /api/state
{"app": "huey.walk-command", "mode": "manual", "stopped": false, ...}

$ reach ping huey servo --path /api/bus -n 30
huey servo over ce-net (http://127.0.0.1:18938/api/bus): PASS
  n=30 answered=30  p50 42.8 ms  p90 72.1 ms  min 20.9  max 248.0
```

This is the open-source data plane of ce-net's tailnet: the part that moves bytes.
[`net.console`](https://console.ce-net.com) is the admin half and already looks like
Tailscale's console. **What it adds over `ce tunnel`, why it is built on top rather than beside,
and every way it is still weaker than Tailscale: [docs/DESIGN.md](docs/DESIGN.md).**

## Install

Needs a running ce node (`ce start`, HTTP API on 127.0.0.1:8844) and a `tunnel` capability in
the local wallet for each device you want to reach. Python 3.9+, stdlib only.

```bash
git clone https://github.com/ce-net/device-reach ~/dev/device-reach
ln -s ~/dev/device-reach/bin/reach ~/.local/bin/reach
reach doctor
```

`reach doctor` names every missing piece: no node, no device table, a device with no wallet
alias, a port not forwarded. It exits 0 PASS / 1 FAIL / 2 CANNOT DETERMINE like everything else
here.

## Commands

| command | what it does |
|---|---|
| `reach ls` | devices and their ports, with the loopback URL for each and whether it is bound |
| `reach up [device]` | open every declared forward, or one device's |
| `reach status [--probe]` | the same, live; `--probe` also GETs each HTTP port |
| `reach port <device> <name>` | one port; prints `http://127.0.0.1:<local>` |
| `reach ping <device> [port] [--path /p] [-n 30]` | p50/p90 over the forward, or CANNOT DETERMINE |
| `reach get <device> <port> <path>` | a read-only GET through the forward |
| `reach down <device> [port]` / `--all` | close those forwards and free the ports; on a node without the close route it says so |
| `reach doctor` | node, tables, wallet, every forward, with reasons |
| `reach serve [port]` | the daemon and its machines page (default 8943) |

`--json` on `ls` and `status`.

## The daemon

```bash
python3 reachd.py 8943          # http://127.0.0.1:8943/
```

Loopback only. It re-binds any declared forward that is not listening every 30 s, serves the
machines page, and answers:

| route | auth | |
|---|---|---|
| `GET /` | open on loopback | the machines page |
| `GET /api/health` | open | carries the marker `net.device-reach` |
| `GET /api/devices` | open | the merged table, without key paths |
| `GET /api/status[?probe=1]` | open | every forward, bound or not |
| `POST /api/up` | **`device-reach:up`** | open a device's forwards |
| `POST /api/down` | **`device-reach:down`** | 501 CANNOT DETERMINE, with the reason |
| anything else | | 404 JSON — the negative control for the launchpad probe |

Mutating routes go through the ce authorization system (`ce-py/cegate.py`) and **fail closed**:
no gate, no capability, no verifier, no root of trust gives a named refusal, never an allow.
Measured 2026-10-04: with no capability, `401 no_capability`; with a capability for
`device-reach:up`, the forward opened; the same token on `/api/down`, `401 denied: capability
does not grant 'device-reach:down'`; with the gate removed, `503 no_authorization_gate`.

```bash
ME=$(ce-iam whoami)
TOK=$(ce-iam grant --to "$ME" --action device-reach:up --expires-in 900 --nonce $RANDOM)
curl -s -X POST -H "Authorization: Bearer $TOK" -H "X-CE-Requester: $ME" \
     -H 'Content-Type: application/json' -d '{"device":"huey"}' \
     http://127.0.0.1:8943/api/up
```

## Devices

It owns no device record. It reads, in order:

1. `~/dev/ce-devices/access.json` — the table `ce-dev` already uses (wallet alias, node id,
   ssh user and key path, LAN address, named ports). No secrets: the capability lives in the ce
   wallet and the ssh key in `~/.ssh`.
2. `~/.config/device-reach/devices.json` — this machine's own additions, same shape, optional.

Later wins per device, but a field the winner does not state keeps the earlier value rather than
becoming null. A table that cannot be read becomes a **note** on every answer, never a silently
short list.

Adding a device is one entry there plus `ce wallet add <name> <node id> --cap <token>` from a
grant run on the device.

The loopback port for a remote port is `10000 + remote` below 10000, `port_local` overriding,
and ssh keeps `ssh_local`. That is exactly `ce-dev`'s rule, on purpose: two tools that name
different ports would open two forwards to one place.

## Limits, stated

- **A forward can only be closed on a node that has the close route.** The listener lives
  inside the ce node, which until recently kept no registry of open tunnels. `reach down` and
  `reach doctor` read `GET /tunnels`: on a node that answers 404 they say the forward cannot be
  closed and that restarting the node is the only way, rather than printing "closed" while the
  listener stays bound. The route (a tunnels registry in `ApiState`, `GET /tunnels`,
  `DELETE /tunnel`) is written and tested on branch `tunnel-close` of the `ce` repo and ships
  with the next node build.
- **No per-device IP and no DNS.** You reach `127.0.0.1:18940`, not `huey:8940`. `ce-iam name`
  holds the naming half already and has no bindings yet.
- **TCP only.** `/ce/tunnel/1` splices TCP. No UDP, no subnet routes, no exit node.
- **No relayed path measured.** Both devices tested are on one Wi-Fi, so both directions were
  direct libp2p. The relay fallback is unproven here.

The full comparison with Tailscale and headscale, in both directions, is in
[docs/DESIGN.md](docs/DESIGN.md).

## It runs on the small device too

`reach` is stdlib Python, so it runs on the board as well as on the Mac. With a `tunnel` grant
for the Mac in the board's ce wallet, Huey's UNO Q (Python 3.13.5, aarch64 Linux) reached the
Mac's loopback-only daemon by name: `reach ping mac reachd --path /api/health -n 30` gave
p50 37.5 ms, p90 105.3 ms, 30 of 30. The overlay is symmetric and the far end needs nothing
but a ce node.

## Tests

```bash
python3 -m unittest discover -s tests          # 22, no network needed
REACH_LIVE=1 python3 -m unittest discover -s tests   # also reaches Huey for real
```

`GateRefuses` deletes the authorization gate and asserts the mutating route answers 503 with the
reason, because a control whose failure looks like success is not a control.

## Licence

AGPL-3.0-or-later, or a commercial licence from Leif. See [LICENSING.md](LICENSING.md). Same
terms as the rest of ce-net.
