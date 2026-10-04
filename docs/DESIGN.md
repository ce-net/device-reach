# net.device-reach: a Tailscale-shaped overlay on ce-net

**Author:** foreman/microduck-assembly.34.7 · **Date:** 2026-10-04 · **Goals:** 67, 68

Leif, 2026-10-04 21:36: "our own tailscale clone. open source" and "make tools. make an open
source app for this to connet it over ce-net". Earlier the same day, 19:19: "you shouldnt need
to ssh. ce-net should have a tailnet style system so it abstracts it awy to just devices."

This is the data plane for that. It is small on purpose: almost everything a tailnet needs was
already in ce-net, in pieces nobody had put behind one name.

## What ce-net already gives, measured

| Tailscale part | ce-net part today | where |
|---|---|---|
| Node identity | an Ed25519 node key; the node id is its hash, 64 hex | `ce-iam whoami`, `~/dev/ce/crates/ce-cap` |
| Control plane (coordination server) | the DHT plus signed capability chains, no server to sign in to | `ce/docs/capabilities.md` |
| WireGuard data plane | libp2p streams on `/ce/tunnel/1`, TCP spliced both ways | `ce/crates/ce-node/src/lib.rs:876` |
| DERP relays | libp2p relay circuits, kept warm for peers you have talked to | `ce/crates/ce-mesh/src/lib.rs:1481` |
| NAT traversal | relay-assisted dial, with the circuit established before the first stream | `ce/crates/ce-node/src/api.rs:1016` |
| ACLs | capability chains with actions, a resource scope, an expiry, a revocation nonce, and `--allowed-port` for tunnels | `ce-iam grant --help` |
| MagicDNS | `ce-iam name bind/resolve`: signed name to node id, resolved through trusted roots | `ce-iam name --help` (0 bindings held here today) |
| Admin console | net.console on console.ce-net.com, Tailscale's tab set | `~/dev/net-console` |
| `tailscale ssh` | `ce-dev huey`, ssh over a mesh tunnel, one warm connection per device | `~/dev/ce-devices/bin/ce-dev` |

So the honest statement is not "ce-net has no tailnet". It is: **every component existed and
nothing assembled them into one thing with a name, a status, and a way to ask what is reachable.**
`ce tunnel huey 18938:8938` is the whole data plane already; you just had to know the device's
wallet alias, its remote port, and pick a free local port yourself, every time, with nothing
holding it up afterwards.

## What this app adds

1. **A device is a name, and so is each of its ports.** `reach port huey servo` instead of
   `ce tunnel huey 18938:8938`. The port name comes from the device table, not from memory.
2. **One deterministic loopback port per (device, remote port)**, using exactly the rule
   `ce-dev` already used (`10000 + remote` below 10000, `port_local` overriding). Two tools,
   one answer, so they never open two forwards to the same place.
3. **The forwards stay up.** A daemon re-binds anything that is not listening every 30 s.
4. **It can be asked.** `reach status`, `GET /api/status`, and a machines page that is
   Tailscale's table: device, port, address to reach it at, state. Unknown is drawn as unknown.
5. **Its own doors are capability-gated and fail closed.** Opening a forward is
   `device-reach:up` through `ce-py/cegate.py`. No gate, no capability, no verifier, no root of
   trust means a named refusal, never an allow.
6. **It says what it cannot do.** `reach down` answers CANNOT DETERMINE and names the missing
   endpoint, rather than printing "closed" and leaving the listener bound.

## Why no new transport, no new identity, no new daemon on the device

A tailnet is tempting to rebuild from the bottom. Three reasons not to:

- The capability check has one home. `ce tunnel` attaches the wallet entry for the target; the
  node validates the chain and carries its canonical bytes in every stream header; the device at
  the far end checks it again before splicing. Adding a second path to open a stream would add a
  second place that can say yes, and a control with two homes has none.
- The far side needs nothing installed. Huey runs a ce node and a servo daemon on loopback. This
  app put no code on it, which is what makes it work for a board that is hard to reach.
- A new overlay would need its own NAT traversal, its own relays, and its own key distribution,
  all of which are the parts of Tailscale that took years. ce-net has them.

## The shape

```
reach (CLI)  ──►  reach.py  ──►  `ce tunnel <alias> <local>:<remote>`
reachd (:8943) ──┘                     │
   │  keeps forwards bound                  ▼
   │  serves /api/status + the page    local ce node :8844  POST /tunnel
   │  gates /api/up on device-reach:up       │
   └─ reads ce-devices/access.json           ▼  libp2p /ce/tunnel/1, direct or relayed
                                        Huey's ce node ──► 127.0.0.1:8938 (servo bus)
                                                       ──► 127.0.0.1:8940 (walk door)
```

Nothing in this app ever sends a command to a device. It opens a path. Driving Huey goes through
the board's own door on :8940, which has its own deadman and its own stops.

## Where it is weaker than Tailscale, named

| gap | why it matters | what would close it |
|---|---|---|
| **A forward cannot be closed.** | `reach down` refuses honestly; today only restarting the local node frees a port. | A tunnels registry in `ApiState` plus `GET /tunnels` and `DELETE /tunnel` in `ce/crates/ce-node/src/api.rs` (the `open_tunnel` handler at :983 spawns the listener and drops the handle). This is the single highest-value change to the core. |
| **No per-device IP.** Tailscale gives 100.x so `huey:8940` works unchanged in any client. We give `127.0.0.1:18940`, so the port number differs from the real one. | Config written for a LAN does not port over untouched. | A loopback alias per device (`ifconfig lo0 alias 127.100.0.2`, needs root) plus a name binding, so the local port can equal the remote port. `ce-iam name` already holds the naming half. |
| **No DNS.** `huey` is a name in a JSON file on this Mac, not a name the mesh resolves. | Every machine needs its own table. | `ce-iam name bind huey <node id>` and have `load_devices()` resolve names through it, with the table as a fallback. Zero bindings exist today. |
| **No device-to-device policy file.** ACLs exist as capability chains minted one at a time. | There is no one place that says who may reach what. | Compile a policy document into grants; `ce-iam policy` and `ce-iam role` are the hooks. |
| **No UDP, no subnet routes, no exit node.** `/ce/tunnel/1` is a TCP splice. | No DNS-over-UDP, no routing a whole subnet, no using a device as a way out. | A UDP mode in the tunnel protocol; ce-vpn already covers the exit-node job for the GFW case and is a separate product. |
| **No key expiry or device approval in the data plane.** The console has the UI; a grant's `--expires-in` is per grant. | A lost device stays reachable until someone revokes its nonce. | Default expiries, and the console writing them. |
| **One machine measured.** Only this Mac reaching Huey, both on the same Wi-Fi. | The relay path is unproven here. | Run `reach ping` from the Hetzner box, where the path must be relayed. |

## Where it is stronger

- **Capabilities, not a tailnet membership.** Tailscale's unit is "this device is in the
  network"; ours is "this grant allows this action on this resource until this time, and can be
  revoked by nonce". `--allowed-port` already confines a tunnel grant to one remote port, which
  Tailscale expresses only as an ACL on the coordination server. Fail-closed is testable:
  delete the verifier and watch the refusal.
- **No coordination server, so nothing to be signed in to and nothing to go down.**
- **The device rows carry what each machine RUNS**, because ceapps announce services on the
  mesh. A Tailscale row carries an address and a version.

## What is deliberately not here

A GUI, a mobile client, sign-up, and the admin screens: `net.console` owns those and already
looks like Tailscale's console. This repo is the thing underneath that actually moves bytes, and
it is open source because Leif said the core of ce-net is.
