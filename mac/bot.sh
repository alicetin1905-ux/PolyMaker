#!/bin/bash
# Polymarket paper-trading bot op macOS (Mac mini 24/7) via launchd.
#
#   ./mac/bot.sh install     venv + dependencies + tests + automatisch starten (ook na herstart/crash)
#   ./mac/bot.sh status      draait hij? + samenvatting tot nu toe
#   ./mac/bot.sh log         live meekijken (Ctrl-C stopt alleen het meekijken)
#   ./mac/bot.sh stop        bot stoppen (blijft uit tot 'start')
#   ./mac/bot.sh start       bot weer starten (gaat verder waar hij was)
#   ./mac/bot.sh reset       stoppen, oude resultaten archiveren, opnieuw beginnen met nieuwe markten
#   ./mac/bot.sh uninstall   stoppen en automatisch starten verwijderen
#
# Instellingen (optioneel, bij install/reset):  BANKROLL=1000 MARKETS=6 ./mac/bot.sh install
set -euo pipefail

LABEL="local.polymarket-mm"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/.venv"
LOGS="$DIR/logs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
BANKROLL="${BANKROLL:-1000}"
MARKETS="${MARKETS:-6}"

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGS"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$VENV/bin/python</string><string>-m</string><string>mm</string><string>paper</string>
    <string>--resume</string>
    <string>--bankroll</string><string>$BANKROLL</string>
    <string>--markets</string><string>$MARKETS</string>
    <string>--print-every</string><string>20</string>
    <string>--log-dir</string><string>$LOGS</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOGS/bot.log</string>
  <key>StandardErrorPath</key><string>$LOGS/bot.err.log</string>
</dict>
</plist>
EOF
}

start() {
  if is_loaded; then echo "Bot draait al."; return; fi
  [ -f "$PLIST" ] || { echo "Nog niet geïnstalleerd: ./mac/bot.sh install"; exit 1; }
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "Bot gestart. Meekijken: ./mac/bot.sh log"
}

stop() {
  if is_loaded; then launchctl bootout "$DOMAIN/$LABEL" && echo "Bot gestopt."; else echo "Bot draaide niet."; fi
}

case "${1:-}" in
  install)
    PY="$(command -v python3 || true)"
    [ -n "$PY" ] || { echo "python3 niet gevonden. Installeer via: xcode-select --install  (of brew install python)"; exit 1; }
    "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
      || { echo "Python 3.9+ nodig, gevonden: $("$PY" --version)"; exit 1; }
    echo "Python: $("$PY" --version)"
    [ -d "$VENV" ] || "$PY" -m venv "$VENV"
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q -r "$DIR/requirements.txt"
    echo "Tests draaien..."
    (cd "$DIR" && "$VENV/bin/python" -m unittest discover -s tests -q)
    stop >/dev/null 2>&1 || true
    write_plist
    start
    echo
    echo "Klaar. De bot start nu automatisch bij inloggen en na een crash."
    echo "Tip: Systeeminstellingen > Energie: 'Voorkom automatische sluimerstand' aan en"
    echo "     'Start automatisch op na stroomstoring' aan; zet automatisch inloggen aan."
    ;;
  start) start ;;
  stop) stop ;;
  status)
    if is_loaded; then echo "Status: DRAAIT"; else echo "Status: GESTOPT"; fi
    echo
    (cd "$DIR" && "$VENV/bin/python" -m mm report --log-dir "$LOGS")
    if [ -s "$LOGS/bot.err.log" ]; then echo; echo "Laatste fouten:"; tail -n 5 "$LOGS/bot.err.log"; fi
    ;;
  log) tail -n 40 -f "$LOGS/bot.log" ;;
  reset)
    stop || true
    if [ -d "$LOGS" ]; then
      ARCH="$DIR/archief/logs-$(date +%Y%m%d-%H%M%S)"
      mkdir -p "$DIR/archief" && mv "$LOGS" "$ARCH" && echo "Oude resultaten bewaard in $ARCH"
    fi
    write_plist
    start
    ;;
  uninstall)
    stop || true
    rm -f "$PLIST"
    echo "Automatisch starten verwijderd. Resultaten staan nog in $LOGS"
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
