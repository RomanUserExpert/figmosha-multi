#!/usr/bin/env bash
# Start (or restart) the Figmosha bridge inside a detached tmux session.
# Run from WSL: bash ~/figmosha2/start-bridge.sh
set -e
SESSION="figmosha-bridge"
cd "$(dirname "$0")"

# Port comes from project.json — the one place that knows which project this
# copy serves. No config means the copy was never claimed; say so and stop.
PY="./venv/bin/python"
[ -x "$PY" ] || PY="./venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python3"
PORT=$("$PY" -c 'import project,sys; c=project.load(); sys.exit("no project.json — run: python figmosha.py init --name <Project>") if not c else print(c["port"])')
NAME=$("$PY" -c 'import project; print(project.load()["name"])')
echo "[start-bridge] project «$NAME», port $PORT"

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" "$PY bridge.py 2>&1 | tee /tmp/figmosha-bridge.log"

# Wait for it to be up
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
        echo "[start-bridge] up after ${i}00ms"
        curl -s "http://127.0.0.1:$PORT/status"
        echo ""
        echo "[start-bridge] attach with: tmux attach -t $SESSION"
        echo "[start-bridge] stop with:   tmux kill-session -t $SESSION"
        exit 0
    fi
    sleep 0.1
done

echo "[start-bridge] FAILED to start within 1s"
echo "[start-bridge] log:"
cat /tmp/figmosha-bridge.log
exit 1
