#!/usr/bin/env bash
# Night 2: continue the truncated chat-trace responses (resumable), stopping at the morning deadline.
# Skips if a tabby server is up on :1337 (the model and the continuation do not fit together).
set -u
Q=$HOME/lm-inference/sc-quant
log() { echo "$(date '+%F %H:%M:%S') $*"; }
# Selected rows not yet continued; the exit code alone is not trusted (a failed run once exited 0)
remaining() {
    python3 - "$Q/trace/chat_trace_full.json" "$@" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))["rows"]
args = sys.argv[2:]
sel = set(json.load(open(args[args.index("--rows") + 1]))["continue"]) if "--rows" in args else set(range(len(rows)))
print(sum(1 for i in sel if rows[i].get("eos_reason") == "max_new_tokens" and not rows[i].get("continued_done")))
PY
}
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then
    log "tabby is running on :1337, continuation skipped"; exit 1
fi
log "continuation start"
cd $HOME/lm-inference/exllamav3-1.5.3-patched
# One full-length row at a time: batching two rows saves almost nothing with CPU-MoE decode (two
# decode steps share ~0.5 of their 10 experts per layer, tools/expert_fingerprint.py), and a second
# attempt resumes from the per-row save if the first one fails.
for CS in 65536 65536; do
    log "continuation with cache $CS"
    PYTHONPATH=$HOME/lm-inference/exllamav3-1.5.3-patched EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 \
    systemd-run --user --scope -q --unit=sc-continue-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
        $HOME/lm-inference/tabbyAPI/venv/bin/python -u $Q/continue_trace.py \
        -m $HOME/lm-inference/models/Qwen3.8-Flash-Next/EXL3-4.05 -mcl 44 -cs $CS -ambs 8 -rcs 1 \
        --trace $Q/trace/chat_trace.json -o $Q/trace/chat_trace_full.json --max_total 65536 \
        --until "$1" "${@:2}"
    rc=$?
    left=$(remaining "${@:2}")
    log "cache $CS exited with $rc, $left selected rows still truncated"
    [ "$left" = "0" ] && break
done
log "continuation finished, $(remaining "${@:2}") selected rows still truncated"
