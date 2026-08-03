# Measuring the DGX WireGuard drops

Started 2026-08-03. The DGX (`spark-0d2d`) periodically becomes unreachable at
its WireGuard address `10.0.0.3` while remaining perfectly healthy on the LAN at
`192.168.68.76`. This document describes the measurement set up to settle *why*,
because three successive explanations were asserted past the evidence and all
three were wrong (see "Discarded explanations" below).

## What is known

- The DGX peers with a **VPS hub** — `Endpoint = vpn.consensus-ai.org:51820`,
  `AllowedIPs = 10.0.0.0/24`. Mac↔DGX is hub-and-spoke and hairpins through the
  internet, hence ~700 ms RTT to a LAN-adjacent host.
- `PersistentKeepalive = 25` **is already set** in the DGX's `[Peer]` block.
- The DGX is on **Starlink**, which cycles its public IP. Its journal shows the
  fingerprint: **19 connectivity transitions in ~36 h, each lasting only 1–4 s**,
  spaced ~1–2 h apart.

  ```bash
  ssh 192.168.68.76 'journalctl -b -1 -u NetworkManager --no-pager \
    | grep -E "state is now (CONNECTED_SITE|CONNECTED_GLOBAL)"'
  ```

- **The IP-cycling hypothesis is REFUTED (2026-08-04).** A 10.6 h measurement
  spanned **18 Starlink IP cycles** and recorded **zero tunnel-specific
  outages**:

  ```
  1188 Mac samples / 10.6 h    tunnel FAIL samples: 1
                               longest consecutive FAIL run: 1
  ```

  The single FAIL is disqualified by its own control — it reads
  `lan=FAIL(0/3)` alongside `tunnel=FAIL(0/3)`, so both paths died together and
  it was not tunnel-specific.

  So any re-convergence gap after an IP change is **under 30 s**, below the
  sampling resolution. `PersistentKeepalive = 25` is doing its job.

- **The clincher.** The previous boot covers Aug 2 23:45 AEST, when the tunnel
  *was* observed dead from the Mac. The longest WAN interruption anywhere in
  that entire boot was **4 s**, and around 23:45 there is no transition at all
  (nearest: 22:34 and 00:04). **The DGX had continuous internet while the tunnel
  was unreachable.**

- **Therefore the fault is not on the DGX side at all.** Not its WAN, not IP
  cycling. The remaining suspects are the **VPS hub** and **this Mac's own
  tunnel session** — neither of which the first two instruments watched, which
  is what v3 adds.

## The two instruments

Both sample every 30 s and **run persistently** — the fault has recurred roughly
every day or two, so 12-hour bursts kept missing it. They restart on crash and
survive reboot, and each rotates its own log at 200k lines (~70 days).

| host | mechanism | unit | log |
|---|---|---|---|
| Mac | launchd agent, `KeepAlive` | `com.localmail.tunnelprobe` | `~/localmail-probe/tunnel-probe.log` |
| DGX | systemd user service, `Restart=always`, `Linger=yes` | `localmail-wgprobe` | `~/localmail-probe/wg-probe.log` |

```bash
# Mac
launchctl list | grep tunnelprobe
launchctl bootout gui/$(id -u)/com.localmail.tunnelprobe     # stop
# DGX
ssh 192.168.68.76 'systemctl --user status localmail-wgprobe'
ssh 192.168.68.76 'systemctl --user disable --now localmail-wgprobe'   # stop
```

### DGX side — passive, root-free

`~/wg-probe.sh` → `~/wg-probe.log`, columns:
`iso_ts  nm_connectivity  rx_bytes rx_pkts  tx_bytes tx_pkts`

It reads `ip -s link show wg0` and `nmcli networking connectivity` and
**deliberately sends no traffic**. That is the critical design constraint: any
outbound packet from the DGX would reach the hub, refresh the hub's stored
endpoint for this peer, and *repair the very failure being measured*. A ping
logger on the DGX would have silently masked the bug.

`wg show` would be the better instrument (latest-handshake age, endpoint) but
needs root, and the DGX has no passwordless sudo.

### Mac side — the probe

`scratchpad/tunnel-probe.sh` → `tunnel-probe.log`, columns:
`iso_ts tunnel={ok,FAIL}(n/3) lan={ok,FAIL}(n/3)`

- `tunnel` pings `10.0.0.3` (Mac → hub → DGX).
- `lan` pings `192.168.68.76` — the **control**. It proves the host is alive and
  the outage is tunnel-specific, not a crashed or rebooting DGX.

**Three pings per sample, `FAIL` only if all three are lost.** The first version
sent a single ping and produced 3 `FAIL`s in 46 samples — every one of them
isolated, and the DGX's `rx_pkts` kept incrementing straight through all three,
so the tunnel had not dropped at all. Starlink loses individual packets at
satellite handover (~15 s cadence); v2 immediately recorded `tunnel=ok(2/3)`
alongside `lan=ok(3/3)`, which is exactly that. A single-ping sampler cannot
distinguish routine satellite loss from the multi-hour outage under
investigation, and at ~6% loss it would bury the real signal in false positives.

Parse the received count from field 4 of the `N packets transmitted, N packets
received` line, not from the loss percentage: `int(33.3 * 3 / 100)` is 0, so
partial loss would be scored as no loss.

Probing the tunnel does **not** mask the bug: if the hub holds a stale endpoint
the packet dies at the hub, so the DGX never receives it, never transmits, and
never refreshes anything.

Note the two logs use different timezones — the DGX writes `+10:00` (AEST), the
Mac writes UTC. Convert before correlating.

## How to read the result

Find a window where the Mac reports `tunnel=FAIL lan=ok` for several consecutive
samples, then look at the DGX's `rx_pkts` across the same window:

| DGX `rx_pkts` during the outage | Meaning |
|---|---|
| **flat** | The Mac's packets never reach the DGX → the **hub→DGX** leg is broken. Consistent with the hub holding a stale endpoint after the Starlink IP change. This is the leading hypothesis. |
| **increments** | Requests arrive but replies do not get back → the **DGX→hub** return leg is broken instead. A different problem, and the keepalive theory would need rethinking. |

`tx_pkts` rising ~1 per 25–30 s throughout is **expected and not diagnostic** —
that is `PersistentKeepalive` doing its job. WireGuard keepalives are one-way and
draw no reply, so TX-up/RX-flat is normal when idle.

If `lan` ever reports `FAIL` too, discard that window: the host was down or
rebooting and it says nothing about the tunnel.

## If the hub→DGX leg is confirmed

The targeted remedy is a NetworkManager dispatcher hook that re-sets the peer
endpoint (or bounces `wg-quick@wg0`) when connectivity returns — **not** a
`wg0.conf` edit. Do not add `PersistentKeepalive`; it is already there.

## Discarded explanations

Recorded so they are not re-proposed:

1. **"Stale NAT mapping — add `PersistentKeepalive = 25`."** It was already set.
   The claim was made before the config file had been read.
2. **"One outbound packet from the DGX restores the tunnel."** Inferred from a
   single coincidental observation; connectivity had returned on its own hours
   earlier. This is also *why* the DGX-side instrument must stay passive — if the
   claim were true, an active prober would mask the fault.
3. **"The upstream internet flaps, and that is the outage."** The transitions are
   1–4 s, far too short to explain it, and that reading rested on a truncated log
   view.
4. **"Starlink cycles the IP and the tunnel is slow to re-converge."** The most
   plausible of the four, and measurement killed it: 18 cycles, zero
   tunnel-specific outages, and no WAN event at all at the time an outage was
   actually observed. Any gap is under 30 s.

## Commands

```bash
# progress
ssh 192.168.68.76 'wc -l ~/wg-probe.log; tail -3 ~/wg-probe.log'
tail -3 "$SCRATCH/tunnel-probe.log"

# outage windows seen from the Mac (tunnel down, host up)
grep 'tunnel=FAIL lan=ok' "$SCRATCH/tunnel-probe.log"

# stop early
ssh 192.168.68.76 'pkill -f wg-probe.sh'
pkill -f tunnel-probe.sh
```
