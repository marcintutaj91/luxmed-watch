#!/bin/bash
# Zarządza nasłuchem jako usługą launchd (start po zalogowaniu, restart po awarii).
# Użycie: ./service.sh install | uninstall | status | logs
set -euo pipefail

LABEL="com.luxmed-watch"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/luxmed-watch.log"
DOMAIN="gui/$(id -u)"

write_plist() {
  cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/luxmed_watch.py</string>
    <string>watch</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
}

case "${1:-}" in
  install)
    [ -x "$DIR/.venv/bin/python" ] || { echo "Brak .venv w $DIR"; exit 1; }
    [ -f "$DIR/config.toml" ] || { echo "Brak config.toml w $DIR"; exit 1; }
    if pgrep -f "luxmed_watch.py watch" >/dev/null; then
      echo "Działa już inny nasłuch (np. w terminalu). Zatrzymaj go (Ctrl+C) i uruchom ponownie."
      exit 1
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    write_plist "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Zainstalowano i uruchomiono. Logi: $LOG"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Usunięto usługę."
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "Usługa nie jest załadowana."
    ;;
  logs)
    tail -n 50 -f "$LOG"
    ;;
  *)
    echo "Użycie: $0 install | uninstall | status | logs"; exit 1;;
esac
