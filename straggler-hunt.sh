#!/usr/bin/env bash
# straggler-hunt.sh — OneDrive leaves a tail of pinned-but-undownloaded files
# that only move after repeated OneDrive restarts. Bounce, wait, recount.
# Exit 0 = all target docs hydrated; exit 3 = stragglers remain (user action:
# OneDrive tray icon -> View sync problems).

PS='/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
count() {
    "$PS" -NoProfile -Command "
\$b='C:\Users\Chris\OneDrive\02. Work'; \$t=0
foreach (\$d in 'TSP Projects','Geotechnical Documents','Byland Technical') {
  Get-ChildItem -LiteralPath \"\$b\\\$d\" -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { if ((\$_.Attributes -band 0x400000) -and (\$_.Extension -match '^\.(pdf|doc|docx)\$')) {\$t++} }
}
\$t" 2>/dev/null | tr -d '\r\n'
}

for cycle in 1 2 3 4 5 6; do
    n=$(count)
    echo "$(date '+%F %T') cycle $cycle: $n stuck target docs"
    [ "$n" = "0" ] && exit 0
    /mnt/c/Windows/System32/OneDrive.exe /shutdown 2>/dev/null
    sleep 8
    "$PS" -NoProfile -Command "Start-Process 'C:\Windows\System32\OneDrive.exe'" 2>/dev/null
    sleep 300
done
n=$(count)
echo "$(date '+%F %T') final: $n stragglers remain — resolve via OneDrive tray (View sync problems)"
exit 3
