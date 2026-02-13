#!/bin/bash
# Run the Polymarket trading bot continuously.
# Usage:
#   ./run_bot.sh              # Run in foreground
#   ./run_bot.sh --background # Run with nohup (survives terminal close)
#   ./run_bot.sh --stop       # Stop background bot
#   ./run_bot.sh --status     # Check if running

set -e
cd "$(dirname "$0")"

PID_FILE="bot.pid"
LOG_FILE="bot.log"

case "${1:-}" in
    --background)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "Bot is already running (PID $(cat "$PID_FILE"))"
            echo "  Stop:   ./run_bot.sh --stop"
            echo "  Logs:   tail -f $LOG_FILE"
            exit 1
        fi
        echo "Starting bot in background..."
        nohup python3 main.py run --strategy short_scalper --live --interval 15 >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "Bot started! (PID $(cat "$PID_FILE"))"
        echo "  Logs:    tail -f $LOG_FILE"
        echo "  Status:  ./run_bot.sh --status"
        echo "  Stop:    ./run_bot.sh --stop"
        ;;
    --stop)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            kill "$(cat "$PID_FILE")"
            rm -f "$PID_FILE"
            echo "Bot stopped."
        else
            rm -f "$PID_FILE"
            echo "No bot running."
        fi
        ;;
    --status)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "Bot is RUNNING (PID $(cat "$PID_FILE"))"
            echo ""
            echo "Last 10 log lines:"
            tail -10 "$LOG_FILE" 2>/dev/null || echo "  (no log file yet)"
        else
            rm -f "$PID_FILE" 2>/dev/null
            echo "Bot is NOT running."
        fi
        ;;
    *)
        echo "============================================"
        echo "  Polymarket Short Scalper Bot"
        echo "  Strategy: short_scalper (5-15 min Up/Down)"
        echo "  Scan interval: 15 seconds"
        echo "  Mode: LIVE"
        echo "============================================"
        echo ""
        echo "Press Ctrl+C to stop."
        echo ""
        python3 main.py run --strategy short_scalper --live --interval 15 2>&1 | tee -a "$LOG_FILE"
        ;;
esac
