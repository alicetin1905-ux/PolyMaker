#!/bin/bash
# Polymarket paper-trading bot op macOS (Mac mini 24/7) via launchd.
#
#   ./mac/bot.sh install     venv + dependencies + tests + automatisch starten (ook na herstart/crash)
#   ./mac/bot.sh status      draait hij? + instellingen + samenvatting tot nu toe
#   ./mac/bot.sh log         live meekijken (Ctrl-C stopt alleen het meekijken)
#   ./mac/bot.sh dashboard   dashboard openen in je browser (adres voor je telefoon staat erbij)
#   ./mac/bot.sh config      huidige instellingen tonen
#   ./mac/bot.sh apply       nieuwe SIZE/MAX_POS/SPREAD toepassen, resultaten blijven doorlopen
#   ./mac/bot.sh stop        bot stoppen (blijft uit tot 'start')
#   ./mac/bot.sh start       bot weer starten (gaat verder waar hij was)
#   ./mac/bot.sh reset       oude resultaten archiveren, opnieuw beginnen (nieuw startbedrag + nieuwe markten)
#   ./mac/bot.sh uninstall   stoppen en automatisch starten verwijderen
#
# Instellingen (onthouden in bot.conf; geef alleen op wat je wilt wijzigen):
#   BANKROLL=1000   startbedrag nepgeld ($)                  -> werkt bij install/reset
#   MARKETS=6       aantal markten                           -> werkt bij install/reset
#   SIZE=50         aandelen per order (per kant)            -> werkt ook met apply
#   MAX_POS=150     max netto positie per markt (aandelen)   -> werkt ook met apply
#   SPREAD=0.5      afstand tot mid, deel van de beloningsband (0.1-1.0) -> werkt ook met apply
#   MIN_DAYS=30     sla markten over die binnen N dagen eindigen -> werkt bij install/reset
#   DASH_PORT=8787  poort van het dashboard                  -> werkt ook met apply
#   DASH_LAN=1      1 = dashboard ook via je wifi (telefoon), 0 = alleen deze Mac -> werkt ook met apply
# Voorbeeld:  SIZE=30 MAX_POS=100 ./mac/bot.sh apply
set -euo pipefail

LABEL="local.polymarket-mm"
DLABEL="local.polymarket-mm-dashboard"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/.venv"
LOGS="$DIR/logs"
CONF="$DIR/bot.conf"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DPLIST="$HOME/Library/LaunchAgents/$DLABEL.plist"
DOMAIN="gui/$(id -u)"
KEYS="BANKROLL MARKETS SIZE MAX_POS SPREAD MIN_DAYS DASH_PORT DASH_LAN"

# ---- settings: defaults < bot.conf < environment --------------------------
for k in $KEYS; do eval "ENV_$k=\"\${$k:-}\""; done
BANKROLL=1000; MARKETS=6; SIZE=50; MAX_POS=150; SPREAD=0.5; MIN_DAYS=30; DASH_PORT=8787; DASH_LAN=1
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
for k in $KEYS; do
  v="$(eval "echo \"\$ENV_$k\"")"
  [ -n "$v" ] && eval "$k=\"\$v\""
done

validate() {
  for k in $KEYS; do
    v="$(eval "echo \"\$$k\"")"
    if ! [[ "$v" =~ ^[0-9]+([.][0-9]+)?$ ]]; then echo "Ongeldige waarde: $k=$v (moet een getal zijn)"; exit 1; fi
  done
  if ! [[ "$MARKETS" =~ ^[1-9][0-9]*$ ]]; then echo "MARKETS moet een heel getal >= 1 zijn"; exit 1; fi
  if ! [[ "$DASH_PORT" =~ ^[0-9]+$ ]] || [ "$DASH_PORT" -lt 1024 ] || [ "$DASH_PORT" -gt 65535 ]; then echo "DASH_PORT moet tussen 1024 en 65535 liggen"; exit 1; fi
  if [ "$DASH_LAN" != "0" ] && [ "$DASH_LAN" != "1" ]; then echo "DASH_LAN moet 0 of 1 zijn"; exit 1; fi
  awk -v s="$SPREAD" 'BEGIN { exit !(s >= 0.05 && s <= 1.0) }' || { echo "SPREAD moet tussen 0.05 en 1.0 liggen"; exit 1; }
  awk -v s="$SIZE" 'BEGIN { exit !(s >= 5) }' || { echo "SIZE moet minimaal 5 zijn (Polymarket-minimum)"; exit 1; }
  awk -v s="$MAX_POS" -v z="$SIZE" 'BEGIN { exit !(s >= z) }' || { echo "MAX_POS moet minimaal SIZE zijn"; exit 1; }
}

save_conf() {
  : > "$CONF"
  for k in $KEYS; do echo "$k=$(eval "echo \"\$$k\"")" >> "$CONF"; done
}

show_conf() {
  echo "Instellingen:"
  echo "  Startbedrag (BANKROLL):    \$$BANKROLL"
  echo "  Markten (MARKETS):         $MARKETS"
  echo "  Ordergrootte (SIZE):       $SIZE aandelen per kant"
  echo "  Max positie (MAX_POS):     $MAX_POS aandelen per markt"
  echo "  Afstand tot mid (SPREAD):  $SPREAD x beloningsband"
  echo "  Min. looptijd (MIN_DAYS):  $MIN_DAYS dagen"
  if [ "$DASH_LAN" = 1 ]; then echo "  Dashboard:                 poort $DASH_PORT, ook via wifi"; else echo "  Dashboard:                 poort $DASH_PORT, alleen deze Mac"; fi
}

dash_host() { if [ "$DASH_LAN" = 1 ]; then echo 0.0.0.0; else echo 127.0.0.1; fi; }

dash_urls() {
  echo "  Op deze Mac:     http://localhost:$DASH_PORT"
  if [ "$DASH_LAN" = 1 ]; then
    local name ip
    name="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
    ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
    echo "  Telefoon (wifi): http://$name.local:$DASH_PORT${ip:+   of  http://$ip:$DASH_PORT}"
  fi
}

write_dash_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGS"
  cat > "$DPLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$DLABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string><string>-m</string><string>mm</string><string>dashboard</string>
    <string>--log-dir</string><string>$LOGS</string>
    <string>--host</string><string>$(dash_host)</string>
    <string>--port</string><string>$DASH_PORT</string>
    <string>--size</string><string>$SIZE</string>
    <string>--max-pos</string><string>$MAX_POS</string>
    <string>--spread-frac</string><string>$SPREAD</string>
    <string>--min-days</string><string>$MIN_DAYS</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGS/dashboard.log</string>
  <key>StandardErrorPath</key><string>$LOGS/dashboard.err.log</string>
</dict>
</plist>
PLIST_END
}

dash_loaded() { launchctl print "$DOMAIN/$DLABEL" >/dev/null 2>&1; }
dash_start() { if [ -f "$DPLIST" ] && ! dash_loaded; then launchctl bootstrap "$DOMAIN" "$DPLIST"; fi; }
dash_stop() { if dash_loaded; then launchctl bootout "$DOMAIN/$DLABEL" || true; fi; }

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
    <string>--size</string><string>$SIZE</string>
    <string>--max-pos</string><string>$MAX_POS</string>
    <string>--spread-frac</string><string>$SPREAD</string>
    <string>--min-days</string><string>$MIN_DAYS</string>
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
    validate
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
    save_conf
    write_plist
    show_conf
    echo
    start
    dash_stop; write_dash_plist; dash_start
    echo
    echo "Dashboard:"
    dash_urls
    echo
    echo "Klaar. Bot en dashboard starten nu automatisch bij inloggen en na een crash."
    echo "Tip: Systeeminstellingen > Energie: 'Voorkom automatische sluimerstand' aan en"
    echo "     'Start automatisch op na stroomstoring' aan; zet automatisch inloggen aan."
    ;;
  config) show_conf ;;
  apply)
    validate
    [ -f "$PLIST" ] || { echo "Nog niet geïnstalleerd: ./mac/bot.sh install"; exit 1; }
    save_conf
    stop >/dev/null 2>&1 || true
    write_plist
    show_conf
    echo "(BANKROLL, MARKETS en MIN_DAYS gelden pas na './mac/bot.sh reset')"
    echo
    start
    dash_stop; write_dash_plist; dash_start
    ;;
  start) start; dash_start ;;
  stop) stop; dash_stop ;;
  status)
    if is_loaded; then echo "Status: DRAAIT"; else echo "Status: GESTOPT"; fi
    echo
    show_conf
    echo
    if dash_loaded; then echo "Dashboard: DRAAIT"; dash_urls; else echo "Dashboard: GESTOPT"; fi
    echo
    (cd "$DIR" && "$VENV/bin/python" -m mm report --log-dir "$LOGS")
    if [ -s "$LOGS/bot.err.log" ]; then echo; echo "Laatste fouten:"; tail -n 5 "$LOGS/bot.err.log"; fi
    ;;
  log) tail -n 40 -f "$LOGS/bot.log" ;;
  dashboard)
    [ -f "$DPLIST" ] || write_dash_plist
    dash_start
    dash_urls
    if command -v open >/dev/null; then sleep 1; open "http://localhost:$DASH_PORT"; fi
    ;;
  reset)
    validate
    stop || true
    if [ -d "$LOGS" ]; then
      ARCH="$DIR/archief/logs-$(date +%Y%m%d-%H%M%S)"
      mkdir -p "$DIR/archief" && mv "$LOGS" "$ARCH" && echo "Oude resultaten bewaard in $ARCH"
    fi
    save_conf
    write_plist
    show_conf
    echo
    start
    dash_stop; write_dash_plist; dash_start
    ;;
  uninstall)
    stop || true
    dash_stop
    rm -f "$PLIST" "$DPLIST"
    echo "Automatisch starten verwijderd. Resultaten staan nog in $LOGS"
    ;;
  *)
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
