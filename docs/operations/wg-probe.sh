#!/bin/bash
# PASSIVE WireGuard observer. Reads counters only — deliberately sends NO
# traffic, because any outbound packet from this host would refresh the VPS
# hub's stored endpoint and mask the exact failure being measured.
# Columns: iso_ts  nm_connectivity  rx_bytes rx_pkts  tx_bytes tx_pkts
LOG=~/wg-probe.log
for i in $(seq 1 1440); do          # 30s x 1440 = 12h, then exits on its own
  ts=$(date -Is)
  nm=$(nmcli networking connectivity 2>/dev/null || echo "?")
  read -r rxb rxp <<<"$(ip -s link show wg0 2>/dev/null | awk '/RX:/{getline; print $1, $2}')"
  read -r txb txp <<<"$(ip -s link show wg0 2>/dev/null | awk '/TX:/{getline; print $1, $2}')"
  printf '%s %s %s %s %s %s\n' "$ts" "$nm" "${rxb:-NA}" "${rxp:-NA}" "${txb:-NA}" "${txp:-NA}" >> "$LOG"
  sleep 30
done
