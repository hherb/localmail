#!/bin/bash
# Mac-side instrument for the DGX WireGuard drops.
# See docs/operations/wireguard-drop-measurement.md.
#
# v3 adds the two things v2 could not see. v2 established that the DGX side is
# innocent — 18 Starlink IP cycles in 10.6 h produced zero tunnel-specific
# outages — so the fault must lie in the VPS hub or in this Mac's own tunnel
# session, and neither was being watched.
#
# Columns:
#   iso_ts  tunnel=ok|FAIL(n/3)  lan=ok|FAIL(n/3)  hub=ok|FAIL(n/3)
#           utun_rx=<pkts> utun_tx=<pkts>
#
#   tunnel : 10.0.0.3        Mac -> hub -> DGX. The thing under test.
#   lan    : off-tunnel      control. Proves the DGX is alive; if this fails
#                            too, the window says nothing about the tunnel.
#                            The column also reports WHICH address answered.
#   hub    : 135.181.95.235  the VPS itself (vpn.consensus-ai.org), reached
#                            over the public internet, NOT through the tunnel.
#                            Separates "the hub is gone" from "the hub is up
#                            but not relaying".
#   utun_* : this Mac's own wg interface counters. `wg show` would be better
#            (handshake age) but its socket is root-only, so packet counters
#            via `netstat -ib` are the root-free substitute.
#
# Reading it during a tunnel outage:
#   hub FAIL                     -> the VPS, or the path to it, is down.
#   hub ok, utun_tx rising,      -> we transmit into the tunnel and nothing
#     utun_rx flat                  returns: hub up but not relaying.
#   hub ok, utun_rx also rising  -> traffic flows; suspect the DGX leg and
#                                   cross-check the DGX's own rx_pkts.
#
# THREE pings per target, FAIL only if all three are lost: the satellite path
# loses single packets routinely (~5% of samples), and a one-ping sampler
# reported 3 phantom outages in its first 46 samples.
#
# Runs forever; launchd restarts it if it dies. Rotates its own log.

LOG="${1:?usage: tunnel-probe.sh <logfile>}"
MAX_LINES=200000          # ~70 days at 30s; rotate rather than grow unbounded

# The DGX's off-tunnel address is a DHCP lease and has moved three times, across
# two subnets (192.168.68.62 -> .76 -> 192.168.1.99). A single hardcoded value
# silently rots into a permanent FAIL, and then the control column no longer
# distinguishes "the tunnel broke" from "the DGX is off" -- which is exactly the
# question the probe exists to answer. So try each candidate per sample and
# report the first that answers. Override with
# LAN_CANDIDATES="a.b.c.d e.f.g.h" when the lease lands somewhere new; adding
# the address here rather than editing the probe keeps old log lines readable.
LAN_CANDIDATES="${LAN_CANDIDATES:-192.168.1.99 192.168.68.76 192.168.68.62}"

recv() {                  # echo how many of the 3 pings came back
  # $4 of "N packets transmitted, N packets received" is the received count.
  # Do NOT derive it from the loss percentage: int(33.3 * 3 / 100) == 0 would
  # score partial loss as none.
  ping -c 3 -i 0.3 -t 4 "$1" 2>/dev/null | awk '/packets received/{print $4}'
}

recv_any() {              # first candidate that answers: echo "<count> <addr>"
  # Stops at the first responder, so the common case costs one ping burst.
  # A dead DGX pays the full list -- correct, since that is the case where we
  # must be sure it is dead rather than merely moved.
  local addr got
  for addr in $LAN_CANDIDATES; do
    got=$(recv "$addr")
    if [ "${got:-0}" -gt 0 ]; then echo "$got $addr"; return; fi
  done
  echo "0 none"
}

while :; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  t=$(recv 10.0.0.3); h=$(recv 135.181.95.235)
  read -r l lan_addr <<<"$(recv_any)"
  # Link-layer row only; its Address column is blank, so the fields land as
  # Ipkts=$4 Ibytes=$6 Opkts=$7 Obytes=$9.
  read -r urx utx <<<"$(netstat -ib -I utun8 2>/dev/null \
      | awk '$1=="utun8" && $3 ~ /^<Link/{print $4, $7; exit}')"

  [ "${t:-0}" -gt 0 ] && tst=ok || tst=FAIL
  [ "${l:-0}" -gt 0 ] && lst=ok || lst=FAIL
  [ "${h:-0}" -gt 0 ] && hst=ok || hst=FAIL

  printf '%s tunnel=%s(%s/3) lan=%s(%s/3)@%s hub=%s(%s/3) utun_rx=%s utun_tx=%s\n' \
    "$ts" "$tst" "${t:-0}" "$lst" "${l:-0}" "$lan_addr" "$hst" "${h:-0}" \
    "${urx:-NA}" "${utx:-NA}" >> "$LOG"

  # Cheap rotation: only stat the file every ~100 samples.
  if [ $((RANDOM % 100)) -eq 0 ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LINES" ]; then
    tail -n $((MAX_LINES / 2)) "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
  sleep 30
done
