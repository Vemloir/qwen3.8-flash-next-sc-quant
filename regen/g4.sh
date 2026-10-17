#!/bin/bash
# Local control of the Colab G4 generation server (Colab CLI + SSH over its WebSocket bridge).
#
#   regen/g4.sh up        allocate a G4 VM and start g4_serve.sh on it (model + container + vLLM)
#   regen/g4.sh wait      follow the setup log until vLLM answers
#   regen/g4.sh forward   keep 127.0.0.1:18000 -> VM 127.0.0.1:8000 open (reconnects; Ctrl-C stops)
#   regen/g4.sh logs      tail of the vLLM log (through colab exec, so it works while forwarding)
#   regen/g4.sh down      stop the VM (everything on it is lost)
#
# Colab allows one SSH connection per VM at a time: while `forward` runs, `logs` goes through
# `colab exec` instead. The server key lives in ~/.config/sc-quant/regen_api_key (created on first
# use); the VM only ever listens on its own loopback.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SESSION=${G4_SESSION:-regen}
PORT=${G4_PORT:-18000}
COLAB=$(command -v colab)
KEYFILE=~/.config/sc-quant/regen_api_key
STATE=~/.config/sc-quant/g4
mkdir -p "$STATE" ~/.config/sc-quant
chmod 700 ~/.config/sc-quant
[ -f "$KEYFILE" ] || (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$KEYFILE")
[ -f ~/.ssh/colab_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -C colab-cli -f ~/.ssh/colab_ed25519
cat > "$STATE/ssh_config" <<EOF
Host $SESSION
  ProxyCommand $COLAB ssh --proxy-mode -s $SESSION -i $HOME/.ssh/colab_ed25519
  User root
  IdentityFile $HOME/.ssh/colab_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
  ServerAliveInterval 30
  ServerAliveCountMax 4
  ExitOnForwardFailure yes
EOF
SSH="ssh -F $STATE/ssh_config $SESSION"

remote_tail() {   # file lines, via colab exec (no SSH slot needed)
    printf 'import subprocess\nprint(subprocess.run("tail -n %s %s", shell=True, capture_output=True, text=True).stdout)\n' "$2" "$1" \
        | timeout 120 "$COLAB" exec -s "$SESSION"
}

case "${1:-}" in
up)
    "$COLAB" new -s "$SESSION" --gpu G4
    "$COLAB" upload -s "$SESSION" "$HERE/g4_serve.sh" /content/g4_serve.sh
    $SSH "REGEN_API_KEY='$(cat "$KEYFILE")' MAX_SEQS='${MAX_SEQS:-32}' setsid nohup bash /content/g4_serve.sh > /content/serve.log 2>&1 < /dev/null &" < /dev/null
    echo "started; follow with: $0 wait"
    "$COLAB" usage | tail -2
    ;;
wait)
    for i in $(seq 240); do
        out=$(remote_tail /content/serve.log 5 2>/dev/null || true)
        echo "$(date +%T) $(echo "$out" | grep '^==' | tail -1)"
        case "$out" in *"vllm ready"*) exit 0;; *"not ready"*) echo "$out"; exit 1;; esac
        sleep 30
    done
    ;;
forward)
    echo "forwarding 127.0.0.1:$PORT -> $SESSION:8000"
    while true; do
        $SSH -N -L "127.0.0.1:$PORT:127.0.0.1:8000" < /dev/null || true
        echo "$(date +%T) forward dropped, reconnecting"; sleep 5
    done
    ;;
logs)
    remote_tail /content/vllm.log "${2:-40}"
    ;;
down)
    "$COLAB" stop -s "$SESSION"
    "$COLAB" usage | tail -2
    ;;
*)
    sed -n 2,13p "$0"; exit 2
    ;;
esac
