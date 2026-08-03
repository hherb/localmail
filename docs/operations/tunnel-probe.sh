#!/bin/bash
# Mac-side reachability probe for the DGX. See
# docs/operations/wireguard-drop-measurement.md.
#
# THREE pings per sample, FAIL only if all three are lost. v1 used a single
# ping and produced 3 false FAILs in 46 samples: the DGX is on Starlink, which
# loses individual packets at satellite handover (~15s cadence). Those single
# drops are not the multi-hour outage under investigation, and counting them as
# outages would bury the real signal.
#
#   tunnel : 10.0.0.3      via WireGuard, Mac -> VPS hub -> DGX
#   lan    : 192.168.68.76 direct — the control, proves the host is alive
LOG="$1"
for i in $(seq 1 1440); do          # 30s x 1440 = 12h
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # $4 of the "N packets transmitted, N packets received" line is the
  # received count — parsed directly rather than derived from the loss
  # percentage, which rounds wrong for partial loss (33.3% of 3 -> int(0.999) = 0).
  trx=$(ping -c 3 -i 0.3 -t 4 10.0.0.3      2>/dev/null | awk '/packets received/{print $4}')
  lrx=$(ping -c 3 -i 0.3 -t 4 192.168.68.76 2>/dev/null | awk '/packets received/{print $4}')
  [ "${trx:-0}" -gt 0 ] && tun=ok || tun=FAIL
  [ "${lrx:-0}" -gt 0 ] && lan=ok || lan=FAIL
  printf '%s tunnel=%s(%s/3) lan=%s(%s/3)\n' "$ts" "$tun" "${trx:-0}" "$lan" "${lrx:-0}" >> "$LOG"
  sleep 30
done
