#!/bin/bash
# Polymarket paper-trading bot op macOS (Mac mini 24/7) via launchd.
#
#   ./mac/bot.sh install     venv + dependencies + tests + automatisch starten (ook na herstart/crash)
#   ./mac/bot.sh status      draait hij? + instellingen + samenvatting tot nu toe
#   ./mac/bot.sh log         live meekijken (Ctrl-C stopt alleen het meekijken)
#   ./mac/bot.sh dashboard   dashboard openen in je browser (adres voor je telefoon staat erbij)
#   ./mac/bot.sh pages       online versie op GitHub Pages aanzetten (Mac publiceert automatisch)
#   ./mac/bot.sh pages-off   automatisch publiceren naar GitHub Pages stoppen
#   ./mac/bot.sh config      huidige instellingen tonen
#   ./mac/bot.sh apply       nieuwe SIZE/MAX_POS/SPREAD toepassen, resultaten blijven doorlopen
#   ./mac/bot.sh stop        bot stoppen (blijft uit tot 'start')
#   ./mac/bot.sh start       bot weer starten (gaat verder waar hij was)
#   ./mac/bot.sh reset       oude resultaten archiveren, opnieuw beginnen (nieuw startbedrag + nieuwe markten)
#   ./mac/bot.sh uninstall   stoppen en automatisch starten verwijderen
#
# Instellingen (onthouden in bot.conf; geef alleen op wat je wilt wijzigen):
#   BANKROLL=1000   startbedrag nepgeld ($)                  -> werkt bij install/reset
#   MARKETS=12      aantal markten                           -> werkt bij install/reset
#   SIZE=50         aandelen per order (per kant)            -> werkt ook met apply
#   MAX_POS=150     max netto positie per markt (aandelen)   -> werkt ook met apply
#   SPREAD=0.5      afstand tot mid, deel van de beloningsband (0.1-1.0) -> werkt ook met apply
#   MIN_DAYS=30     sla markten over die binnen N dagen eindigen -> werkt bij install/reset
#   DASH_PORT=8790  poort van het dashboard                  -> werkt ook met apply
#   DASH_LAN=1      1 = dashboard ook via je wifi (telefoon), 0 = alleen deze Mac -> werkt ook met apply
#   PAGES_EVERY=10  online versie elke N minuten bijwerken (minimaal 6) -> werkt ook met apply
#   MAX_LOSS=3      blokkeer een markt 24 uur na zoveel dollar verlies (0 = uit) -> werkt ook met apply
#   ROTATE_HOURS=24 elke N uur betere markten zoeken (0 = uit) -> werkt ook met apply
# Voorbeeld:  SIZE=30 MAX_POS=100 ./mac/bot.sh apply
set -euo pipefail

LABEL="local.polymarket-mm"
DLABEL="local.polymarket-mm-dashboard"
PLABEL="local.polymarket-mm-pages"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/.venv"
LOGS="$DIR/logs"
CONF="$DIR/bot.conf"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DPLIST="$HOME/Library/LaunchAgents/$DLABEL.plist"
PPLIST="$HOME/Library/LaunchAgents/$PLABEL.plist"
DOMAIN="gui/$(id -u)"
KEYS="BANKROLL MARKETS SIZE MAX_POS SPREAD MIN_DAYS DASH_PORT DASH_LAN PAGES_EVERY MAX_LOSS ROTATE_HOURS"

# ---- settings: defaults < bot.conf < environment --------------------------
for k in $KEYS; do eval "ENV_$k=\"\${$k:-}\""; done
BANKROLL=1000; MARKETS=12; SIZE=50; MAX_POS=150; SPREAD=0.5; MIN_DAYS=30; DASH_PORT=8790; DASH_LAN=1; PAGES_EVERY=10; MAX_LOSS=3; ROTATE_HOURS=24
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
  awk -v s="$PAGES_EVERY" 'BEGIN { exit !(s >= 6) }' || { echo "PAGES_EVERY moet minimaal 6 (minuten) zijn: GitHub Pages bouwt max ~10x per uur"; exit 1; }
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
  echo "  Verliesgrens (MAX_LOSS):   \$$MAX_LOSS per markt (0 = uit)"
  echo "  Verversen (ROTATE_HOURS):  elke $ROTATE_HOURS uur (0 = uit)"
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
    <string>--max-loss</string><string>$MAX_LOSS</string>
    <string>--rotate-hours</string><string>$ROTATE_HOURS</string>
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
dash_stop() { if dash_loaded; then launchctl bootout "$DOMAIN/$DLABEL" || true; wait_gone "$DLABEL"; fi; }

pages_repo() {  # owner/repo from the origin remote
  git -C "$DIR" remote get-url origin 2>/dev/null | sed -E 's#^(https://|git@)github\.com[:/]##; s#\.git$##; s#/$##'
}
pages_url() { local r; r="$(pages_repo)"; echo "https://$(echo "${r%%/*}" | tr '[:upper:]' '[:lower:]').github.io/${r#*/}/"; }

write_pages_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGS"
  cat > "$PPLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PLABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string><string>-m</string><string>mm</string><string>publish</string>
    <string>--log-dir</string><string>$LOGS</string>
    <string>--site-dir</string><string>$DIR/.pages</string>
    <string>--every</string><string>$PAGES_EVERY</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string><key>GIT_TERMINAL_PROMPT</key><string>0</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>$LOGS/pages.log</string>
  <key>StandardErrorPath</key><string>$LOGS/pages.err.log</string>
</dict>
</plist>
PLIST_END
}

port_owner() {  # prints the process listening on DASH_PORT (empty if free)
  lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $1" (pid "$2")"; exit}'
}

dash_restart() {  # (re)write and start the dashboard, unless another program holds the port
  dash_stop
  write_dash_plist
  local owner
  owner="$(port_owner)"
  if [ -n "$owner" ]; then
    echo "!! Poort $DASH_PORT is al bezet door $owner: dashboard NIET gestart."
    echo "   Kies een vrije poort, bijvoorbeeld:  DASH_PORT=8791 ./mac/bot.sh apply"
    return 0
  fi
  dash_start
}

pages_loaded() { launchctl print "$DOMAIN/$PLABEL" >/dev/null 2>&1; }
pages_start() { if [ -f "$PPLIST" ] && ! pages_loaded; then launchctl bootstrap "$DOMAIN" "$PPLIST"; fi; }
pages_stop() { if pages_loaded; then launchctl bootout "$DOMAIN/$PLABEL" || true; wait_gone "$PLABEL"; fi; }
pages_refresh() { if [ -f "$PPLIST" ]; then pages_stop; write_pages_plist; pages_start; fi; }

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

wait_gone() {  # launchctl bootout returns before the job is really gone: wait up to 15 s
  local i=0
  while launchctl print "$DOMAIN/$1" >/dev/null 2>&1 && [ $i -lt 30 ]; do sleep 0.5; i=$((i + 1)); done
}

stop() {
  if is_loaded; then launchctl bootout "$DOMAIN/$LABEL" || true; wait_gone "$LABEL"; echo "Bot gestopt."; else echo "Bot draaide niet."; fi
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
    dash_restart
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
    dash_restart
    pages_refresh
    ;;
  start) start; dash_start; pages_start ;;
  stop) stop; dash_stop; pages_stop ;;
  pages)
    validate
    [ -x "$VENV/bin/python" ] || { echo "Eerst installeren: ./mac/bot.sh install"; exit 1; }
    save_conf
    echo "Online versie publiceren naar GitHub Pages (branch gh-pages)..."
    echo "Vraagt git om inloggegevens? Gebruikersnaam = je GitHub-naam, wachtwoord = een token:"
    echo "  https://github.com/settings/personal-access-tokens/new"
    echo "  -> Repository access: alleen $(pages_repo)  -> Permissions: Contents = Read and write"
    echo
    if ! (cd "$DIR" && "$VENV/bin/python" -m mm publish --once --log-dir "$LOGS" --site-dir "$DIR/.pages"); then
      echo; echo "Publiceren lukte niet. Controleer je token en probeer opnieuw: ./mac/bot.sh pages"; exit 1
    fi
    pages_stop; write_pages_plist; pages_start
    echo
    echo "Gelukt. Je Mac publiceert nu elke $PAGES_EVERY minuten automatisch."
    echo
    echo "Eenmalig op GitHub aanzetten (als je dat nog niet deed):"
    echo "  https://github.com/$(pages_repo)/settings/pages"
    echo "  -> Source: 'Deploy from a branch'  -> Branch: gh-pages, map: / (root)  -> Save"
    echo
    echo "Daarna (na ~1 minuut) bereikbaar op:  $(pages_url)"
    ;;
  pages-off)
    pages_stop
    rm -f "$PPLIST"
    echo "Automatisch publiceren gestopt. De laatste versie blijft online tot je GitHub Pages uitzet."
    ;;
  status)
    if is_loaded; then echo "Status: DRAAIT"; else echo "Status: GESTOPT"; fi
    echo
    show_conf
    echo
    if dash_loaded; then echo "Dashboard: DRAAIT"; dash_urls; else echo "Dashboard: GESTOPT"; fi
    if pages_loaded; then echo "Online versie: AAN (elke $PAGES_EVERY min) -> $(pages_url)"; if [ -s "$LOGS/pages.log" ]; then echo "  laatst: $(tail -n 1 "$LOGS/pages.log")"; fi; else echo "Online versie: UIT (aanzetten: ./mac/bot.sh pages)"; fi
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
    dash_restart
    pages_refresh
    ;;
  uninstall)
    stop || true
    dash_stop
    pages_stop
    rm -f "$PLIST" "$DPLIST" "$PPLIST"
    echo "Automatisch starten verwijderd. Resultaten staan nog in $LOGS"
    ;;
  *)
    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
