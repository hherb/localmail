#!/bin/bash
# Mac-side reachability probe for the DGX.
#   tunnel : 10.0.0.3      via WireGuard, Mac -> VPS hub -> DGX
#   lan    : 192.168.68.76 direct, the control — proves the host is alive
#            and the daemon healthy even while the tunnel is down.
# Probing the tunnel does NOT mask the bug: if the hub holds a stale endpoint
# the packet dies at the hub, so the DGX never transmits and never refreshes it.
LOG="$1"
for i in $(seq 1 1440); do          # 30s x 1440 = 12h
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ping -c 1 -t 3 10.0.0.3      >/dev/null 2>&1 && tun=ok || tun=FAIL
  ping -c 1 -t 3 192.168.68.76 >/dev/null 2>&1 && lan=ok || lan=FAIL
  printf '%s tunnel=%s lan=%s\n' "$ts" "$tun" "$lan" >> "$LOG"
  sleep 30
done
