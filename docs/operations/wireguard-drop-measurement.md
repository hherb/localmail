# The DGX "WireGuard drops" — SOLVED: the DGX loses power

**Resolved 2026-08-04.** The DGX (`spark-0d2d`) periodically became unreachable
at its WireGuard address `10.0.0.3`. Five explanations were proposed across four
sessions and all five were wrong, because all five assumed a *network* fault.

**There is no tunnel fault. The host is off.** The sustained outages are
unclean power losses; the tunnel is a bystander that comes back by itself when
the machine does.

This document keeps the measurement that settled it, because the reasoning is
reusable and because "the DGX is unreachable" will be said again.

## The answer, and how it was proven

The persistent probes caught one sustained outage: the Mac saw
`tunnel=FAIL` from **2026-08-03T23:31:17Z through 2026-08-04T00:01:42Z**,
recovering at 00:02:21Z — 30.5 minutes. In AEST (the DGX's clock, UTC+10) that
is **09:31:17 → 10:02:21**.

Three independent signals, none of them a network measurement, agree:

| signal | reading |
|---|---|
| `journalctl --list-boots` | boot `-1` **ends** `09:30:21 AEST`; boot `0` **begins** `10:02:07 AEST` |
| DGX probe log | **31.5-minute gap**, `09:30:43` → `10:02:13`, in a service that samples every 30 s and is `Restart=always` |
| `wg0` counters at the first post-gap sample | `0 0 0 0` — the interface was created fresh |

A probe that systemd restarts on death cannot leave a 31-minute hole unless the
whole machine is gone. The counters resetting rule out a `wg-quick` bounce
being *inside* a live boot. And the shutdown was **not clean**:

```bash
ssh <dgx> 'journalctl -b -1 --no-pager | grep -icE \
   "Reached target (Shutdown|Power-Off|Reboot)|systemd-shutdown"'   # 0
```

Zero. The journal simply stops mid-session, on a `systemd-logind` session
teardown line. That is a power cut or a hard power-off, not `reboot(8)`.

The reboot cadence is consistent with it: `Aug 1 07:55`, `Aug 2 07:27`,
`Aug 3 19:06` (that one deliberate — the cold-boot proof), `Aug 4 10:02`.

Meanwhile the hub was never implicated. **`hub=FAIL` appears in 0 of 1971
samples**, including every sample of the outage — the VPS was reachable over the
public internet throughout.

## The short blips are satellite loss, not drops

Two other `tunnel=FAIL` samples appear in the same 18.5 h. Both are **single
30-second samples** bracketed by `ok`, with no counter anomaly and `hub=ok`.
On a hairpin with ~900 ms RTT and a 4 s ping deadline, losing three packets in a
row is ordinary Starlink behaviour.

Do not treat an isolated FAIL sample as an outage. The three-pings-per-sample
design exists precisely because a one-ping sampler reported three phantom
outages in its first 46 samples; three pings reduce that, they do not eliminate
it. **Sustained** means several consecutive samples.

## The LAN escape hatch moved — and it is not stable

The DGX is now **`192.168.1.99`**, not `192.168.68.76`. It rejoined SSID
`STARLINK` after the power loss and took a DHCP lease on a different subnet
from the one the Mac is on (`192.168.68.69/22`, gateway `192.168.68.1`).

It is still the right way in — `45 ms` versus ~900 ms through the tunnel, and it
works whenever the host is actually up. But **look the address up, do not
assume it**: it is a DHCP lease and the host demonstrably lands on different
subnets across boots.

```bash
# From the DGX, once you are on it by any route:
ip -4 -o addr show | grep -v " lo "
# Or scan, if you have neither route:
arp -a | grep -i <mac-oui>
```

## Diagnosing the next occurrence

Ask **"is the host up?"** before asking anything about WireGuard.

```bash
# 1. Sustained, or a single-sample blip?
grep 'tunnel=FAIL' ~/localmail-probe/tunnel-probe.log | tail -40

# 2. Was the hub reachable throughout?  (If hub=FAIL, it is a different fault.)
grep 'tunnel=FAIL' ~/localmail-probe/tunnel-probe.log | grep -c 'hub=ok'

# 3. THE QUESTION.  A boot boundary inside the outage window closes it.
ssh <dgx> 'journalctl --list-boots | tail -5; last reboot | head -5'

# 4. Was it clean?  0 means power loss.
ssh <dgx> 'journalctl -b -1 --no-pager | grep -icE \
   "Reached target (Shutdown|Power-Off|Reboot)|systemd-shutdown"'
```

If a boot boundary lines up with the outage, stop. It is the power, and no
amount of WireGuard configuration will change that. The remedy is electrical —
a UPS, or a different outlet — not `wg0.conf`.

## Discarded explanations

Recorded so they are not re-proposed. Note that (5) subsumes the rest: every one
of them accepted the framing that a *network* was failing.

1. **"Stale NAT mapping — add `PersistentKeepalive = 25`."** Already set. The
   claim was made before the config file had been read.
2. **"One outbound packet from the DGX restores the tunnel."** Inferred from a
   single coincidental observation; connectivity had returned on its own hours
   earlier. This is also why the DGX-side instrument stays passive — if the
   claim were true, an active prober would mask the fault.
3. **"The upstream internet flaps, and that is the outage."** The transitions
   are 1–4 s, far too short, and that reading rested on a truncated log view.
4. **"Starlink cycles the IP and the tunnel is slow to re-converge."** The most
   plausible of the five, and measurement killed it: 18 cycles, zero
   tunnel-specific outages, and no WAN event at the time an outage was observed.
5. **"It is a tunnel problem at all."** It never was. Three sessions of network
   theorising were spent on a host that was switched off, and the thing that
   finally answered it was `journalctl --list-boots` — which nobody had run,
   because nobody had questioned the premise.

**Do not edit `/etc/wireguard/wg0.conf`.** `PersistentKeepalive = 25` is set,
18 IP cycles passed without a tunnel outage, and the tunnel has never been shown
to fail while the host was up.

## The instruments

Both sample every 30 s and run persistently — they restart on crash and survive
reboot, and each rotates its own log at 200k lines (~70 days). Left running:
they cost nothing and the boot-gap signal above is only legible because the DGX
probe was sampling continuously.

| host | mechanism | unit | log |
|---|---|---|---|
| Mac | launchd agent, `KeepAlive` | `com.localmail.tunnelprobe` | `~/localmail-probe/tunnel-probe.log` |
| DGX | systemd user service, `Restart=always`, `Linger=yes` | `localmail-wgprobe` | `~/localmail-probe/wg-probe.log` |

**Mac** (`tunnel-probe.sh`) — three pings each to the tunnel address, the LAN
control, and the hub over the public internet, plus this Mac's own `utun8`
packet counters:

```
iso_ts tunnel=ok|FAIL(n/3) lan=ok|FAIL(n/3) hub=ok|FAIL(n/3) utun_rx= utun_tx=
```

**DGX** (`wg-probe.sh`) — **passive by design**, sends nothing. Any outbound
packet refreshes the hub's stored endpoint for this peer and would repair the
very failure being measured:

```
iso_ts nm_connectivity rx_bytes rx_pkts tx_bytes tx_pkts
```

`tx_pkts` rising ~1 per 25–30 s while idle is `PersistentKeepalive`, not
traffic. Keepalives are one-way and draw no reply, so TX-up/RX-flat is normal.

**The Mac logs UTC; the DGX logs +10:00.** Convert before correlating — the
30-minute outage above looks like two unrelated events if you do not.
