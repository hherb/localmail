# Measuring the DGX WireGuard drops

**Status: still open.** The DGX (`spark-0d2d`) has been reported unreachable at
its WireGuard address `10.0.0.3` every day or two. Five explanations have been
proposed across four sessions and all five were wrong. The instruments below now
run persistently on both hosts; as of 2026-08-04 they have captured **one**
candidate event, and it turned out to have a mundane physical cause, so the
measurement continues.

Read "Discarded explanations" before proposing a sixth.

## The one event captured so far — eliminated, not diagnostic

On 2026-08-03 the Mac saw `tunnel=FAIL` from **23:31:17Z through 00:01:42Z**,
recovering at 00:02:21Z — 30.5 minutes, the first sustained outage any
instrument has recorded. Three non-network signals showed the **host was down**
for essentially that exact window:

| signal | reading |
|---|---|
| `journalctl --list-boots` | boot boundary **inside** the window — previous ends `09:30:21 AEST` (= 23:30:21Z), next begins `10:02:07` (= 00:02:07Z) |
| DGX probe log | **31.5-minute gap**, in a service that samples every 30 s under `Restart=always` |
| `wg0` counters after | `0 0 0 0` — the interface was created fresh |

and the stop was **unclean** — zero `Reached target Shutdown` lines in that boot.

**That event is explained: a redundant power supply was being installed.** It is
therefore *not* an instance of the fault under investigation, and it must not be
generalised into one.

The journal history makes that unambiguous. It holds exactly **one** unclean
stop — this one:

| boot | clean-shutdown lines | gap before next boot |
|---|---|---|
| -4 (Aug 1) | 8 | 48 s |
| -3 (Aug 2) | 16 | 28 s |
| -2 (Aug 3) | 7 | 32 s (a deliberate cold-boot proof) |
| -1 (Aug 4) | **0** | **31m46s** (the PSU installation) |

Every other boundary is an ordinary fast reboot. **The DGX is on a UPS rated for
~5 days**, so power loss is not a candidate explanation for the recurring fault
in the first place.

**Net position: the measurement window contains zero *unexplained* tunnel
outages.** The recurring fault has not yet been caught. Keep the probes running.

## What the data does support

- **`hub=FAIL` appears in 0 of 1971 samples.** The VPS hub was reachable over
  the public internet throughout the window — including every sample of the
  event above. Weak evidence only, since that window contains no unexplained
  outage, but it is the first direct observation of the hub at all.
- **Isolated `tunnel=FAIL` samples are packet loss, not outages.** Two appear in
  the same 18.5 h: single 30-second samples bracketed by `ok`, with `hub=ok` and
  no counter anomaly. On a hairpin with ~900 ms RTT and a 4 s ping deadline,
  losing three packets in a row is ordinary Starlink behaviour. **Sustained**
  means several consecutive samples. The three-pings-per-sample design exists
  because a one-ping sampler reported three phantom outages in its first 46
  samples; three pings reduce that, they do not eliminate it.

## Diagnosing the next occurrence

Step 1 is triage, not a hypothesis: **rule the host out first**, because it is
one command and it demonstrably disposed of the only event captured so far.

```bash
# 1. Sustained, or a single-sample blip?
grep 'tunnel=FAIL' ~/localmail-probe/tunnel-probe.log | tail -40

# 2. TRIAGE: was the host even up?  A boot boundary inside the outage window
#    means the event is not about the tunnel.  Check whether the stop was clean
#    (a planned reboot) or not (something physical).
ssh <dgx> 'journalctl --list-boots | tail -5; last reboot | head -5'
ssh <dgx> 'journalctl -b -1 --no-pager | grep -icE \
   "Reached target (Shutdown|Power-Off|Reboot)|systemd-shutdown"'

# 3. Host was up throughout -> THIS IS THE FAULT.  Now the probes matter:
#    was the hub reachable?
grep 'tunnel=FAIL' ~/localmail-probe/tunnel-probe.log | grep -c 'hub=ok'
```

If the host was up for the whole window, that is the first genuine capture. Read
the DGX's `rx_pkts` across it:

| DGX `rx_pkts` during the outage | Meaning |
|---|---|
| **flat** | The Mac's packets never arrive → the **hub→DGX** leg is broken. |
| **increments** | Requests arrive but replies do not return → the **DGX→hub** return leg. |

`tx_pkts` rising ~1 per 25–30 s while idle is `PersistentKeepalive`, not traffic;
keepalives are one-way and draw no reply, so TX-up/RX-flat is normal when idle.

## Discarded explanations

Recorded so they are not re-proposed.

1. **"Stale NAT mapping — add `PersistentKeepalive = 25`."** Already set. The
   claim was made before the config file had been read.
2. **"One outbound packet from the DGX restores the tunnel."** Inferred from a
   single coincidental observation; connectivity had returned on its own hours
   earlier. This is also why the DGX-side instrument stays passive — if the claim
   were true, an active prober would mask the fault.
3. **"The upstream internet flaps, and that is the outage."** The transitions are
   1–4 s, far too short, and that reading rested on a truncated log view.
4. **"Starlink cycles the IP and the tunnel is slow to re-converge."** Measurement
   killed it: 18 IP cycles, zero tunnel-specific outages. Any re-convergence gap
   is under 30 s.
5. **"The DGX loses power."** Asserted from the single captured event above —
   host down, unclean stop — without checking whether it generalised. It does
   not: that stop was a scheduled PSU installation, it is the *only* unclean stop
   in the journal, every other boot boundary is a clean fast reboot, and the host
   is on a ~5-day UPS. **The lesson is the recurring one in this file: one
   observation, confidently generalised, is how the previous four got made.**

**Do not edit `/etc/wireguard/wg0.conf`.** `PersistentKeepalive = 25` is set and
18 IP cycles passed without a tunnel outage.

## Reaching the DGX when the tunnel is down

Use the LAN — 45 ms versus ~900 ms hairpinned, and it works whenever the host is
up. But **look the address up rather than assuming it**: it is a DHCP lease and
has been `192.168.68.62`, `192.168.68.76`, and (after the Aug 4 reboot, which
rejoined SSID `STARLINK`) `192.168.1.99` — the last on a different subnet from
the Mac's `192.168.68.69/22`.

```bash
ip -4 -o addr show | grep -v " lo "     # from the DGX, once on it by any route
```

## The instruments

Both sample every 30 s and run persistently — they restart on crash and survive
reboot, and each rotates its own log at 200k lines (~70 days). The fault recurs
every day or two, so 12-hour bursts kept missing it. **Leave them running**: the
boot-gap signal that eliminated the Aug 4 event was only legible because the DGX
probe was sampling continuously.

| host | mechanism | unit | log |
|---|---|---|---|
| Mac | launchd agent, `KeepAlive` | `com.localmail.tunnelprobe` | `~/localmail-probe/tunnel-probe.log` |
| DGX | systemd user service, `Restart=always`, `Linger=yes` | `localmail-wgprobe` | `~/localmail-probe/wg-probe.log` |

**Mac** (`tunnel-probe.sh`) — three pings each to the tunnel address, the LAN
control, and the hub over the public internet, plus this Mac's own `utun8`
packet counters:

```
iso_ts tunnel=ok|FAIL(n/3) lan=ok|FAIL(n/3)@<addr> hub=ok|FAIL(n/3) utun_rx= utun_tx=
```

The `lan` column tries each address in `LAN_CANDIDATES` in turn and reports
which one answered, because the DGX's address is a DHCP lease that has moved
three times across two subnets (`192.168.68.62` → `.76` → `192.168.1.99`). A
single hardcoded target silently rots into a permanent `FAIL`, and a control
that always fails cannot distinguish "the tunnel broke" from "the DGX is off"
— which is the entire question the probe exists to answer. It read `FAIL`
that way from the Aug 4 reboot until this was fixed, so **treat `lan` in log
lines without an `@addr` suffix as unreliable**. When the lease moves again,
prepend the new address:

```bash
LAN_CANDIDATES="192.168.1.42 192.168.1.99" ~/localmail-probe/tunnel-probe.sh <log>
```

Note the two hosts are currently on *different* subnets and the "LAN" path
measures ~35-120 ms, so it is a routed path rather than a local one. It is
still a valid liveness control — it does not traverse the tunnel — but do not
read its latency as a LAN figure.

**DGX** (`wg-probe.sh`) — **passive by design**, sends nothing. Any outbound
packet refreshes the hub's stored endpoint for this peer and would repair the
very failure being measured:

```
iso_ts nm_connectivity rx_bytes rx_pkts tx_bytes tx_pkts
```

**The Mac logs UTC; the DGX logs +10:00.** Convert before correlating — the
30-minute event above looks like two unrelated things if you do not.
