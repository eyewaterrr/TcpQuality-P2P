#!/usr/bin/env bash
# TcpQuality-P2P Linux bootstrap. GPL-3.0-only.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
  echo '[错误] 需要 Python 3。Ubuntu/Debian: sudo apt-get install -y python3' >&2
  exit 2
fi
SRC="${BASH_SOURCE[0]:-$0}"
DIR=$(cd -- "$(dirname -- "$SRC")" 2>/dev/null && pwd || true)
if [[ -n "$DIR" && -f "$DIR/p2p.py" ]]; then
  exec python3 "$DIR/p2p.py" "$@"
fi
BASE="${TCPQUALITY_P2P_BASE_URL:-https://raw.githubusercontent.com/eyewaterrr/TcpQuality-P2P/main}"
case "$BASE" in https://*) ;; *) echo '[错误] 下载源必须是 https URL' >&2; exit 2 ;; esac
TEMP=$(mktemp)
trap 'rm -f -- "$TEMP"' EXIT
curl --fail --silent --show-error --location --retry 2 --connect-timeout 10 --max-time 40 \
  "${BASE%/}/p2p.py" -o "$TEMP"
python3 "$TEMP" "$@"