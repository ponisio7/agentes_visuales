#!/bin/bash
# rosca.sh — arranca la GUI con log fechado

clear

# Matar cualquier GUI anterior
pkill -f "python.*main.py"
sleep 1
ps -eo pid,cmd | grep -i "python.*main" | grep -v grep

# Arrancar con log fechado
LOGFILE="logs/session_$(date +%Y%m%d_%H%M%S).log"
echo "📝 Log: $LOGFILE"
python main.py 2>&1 | tee "$LOGFILE"