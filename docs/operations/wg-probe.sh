#!/bin/bash
# DGX-side instrument for the WireGuard drops.
# See docs/operations/wireguard-drop-measurement.md.
#
# PASSIVE BY DESIGN — reads counters and sends NOTHING. Any outbound packet from
# this host reaches the VPS hub and refreshes the hub's stored endpoint for this
# peer, which would repair the very failure being measured. A ping logger here
# would silently mask the bug. Do not "improve" this by adding a ping.
#
# `wg show` (latest-handshake age, endpoint) would be the better instrument but
# needs root, and this host has no passwordless sudo; `ip -s link` counters are
# the root-free substitute.
#
# Columns: iso_ts  nm_connectivity  rx_bytes rx_pkts  tx_bytes tx_pkts
#
# Reading it during an outage seen from the Mac:
#   rx_pkts flat        -> the Mac's packets never arrive: hub -> DGX leg broken.
#   rx_pkts increments  -> they arrive but replies do not return: the return leg.
# rx flat + tx rising while idle is NORMAL and not diagnostic: that is
# PersistentKeepalive, which is one-way and draws no reply.
#
# nm_connectivity is sampled at 30s but the Starlink transitions last 1-4s, so
# it will almost always read "full" — the authoritative record of those events
# is the journal:
#   journalctl -b -u NetworkManager | grep "state is now CONNECTED_SITE"
#
# Runs forever; systemd restarts it if it dies. Rotates its own log.

LOG="${LOG:-$HOME/wg-probe.log}"
MAX_LINES=200000          # ~70 days at 30s

while :; do
  ts=$(date -Is)
  nm=$(nmcli networking connectivity 2>/dev/null || echo "?")
  read -r rxb rxp <<<"$(ip -s link show wg0 2>/dev/null | awk '/RX:/{getline; print $1, $2}')"
  read -r txb txp <<<"$(ip -s link show wg0 2>/dev/null | awk '/TX:/{getline; print $1, $2}')"
  printf '%s %s %s %s %s %s\n' "$ts" "$nm" \
    "${rxb:-NA}" "${rxp:-NA}" "${txb:-NA}" "${txp:-NA}" >> "$LOG"

  if [ $((RANDOM % 100)) -eq 0 ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LINES" ]; then
    tail -n $((MAX_LINES / 2)) "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
  sleep 30
done
