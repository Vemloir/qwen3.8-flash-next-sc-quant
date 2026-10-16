#!/usr/bin/env bash
# Close the text gaps of the mixed-effort trace on our FP8-sourced 4.00 bpw quant, then convert the
# same recipe and calibration from BF16 and compare both quants against the BF16 reference:
#   1. multi-turn chats at off / low / medium (23 conversations with follow-ups per mode)
#   2. agent sessions missing at off / low / medium: webdev, terminal_bench, cyber-pi at medium
#   3. drop the MiMo task images and the local FP8-sourced quant (backed up on HF)
#   4. convert from BF16 (convert_bf16.sh), qbench vs BF16
set -u
Q=/home/daniil/lm-inference/sc-quant; T=$Q/trace; R=$Q/agent-runs-mixed
M=/home/daniil/lm-inference/models/Qwen3.8-Flash-Next; TREE=/home/daniil/lm-inference/exllamav3-1.5.3-patched
PY=/home/daniil/lm-inference/tabbyAPI/venv/bin/python
log() { echo "$(date '+%F %H:%M:%S') $*"; }

log "multi-turn chats"
split() { python3 -c "import json; print(','.join(map(str, json.load(open('$T/mixed/multiturn_split.json'))['$1'])))"; }
chat() {  # name, template vars, max_new_tokens
    (cd $TREE && PYTHONPATH=$TREE EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 \
        systemd-run --user --scope -q --unit=sc-chat-$1-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
        $PY -u $T/trace_chat.py --only $(split $1) -m $M/SC-EXL3-4.00 -mcl 44 -cs 65536 -ambs 8 -rcs 1 \
        -o $T/mixed/chat_mt_$1.json -co $T/mixed/chat_mt_$1.safetensors -cr 1000 -cc 8192 \
        -tv "$2" --max_new_tokens $3 --target_tokens 10000000 --max_epochs 1 > $T/mixed/chat_mt_$1.log 2>&1)
    log "chat $1 exit $?"
}
chat off    '{"enable_thinking": false}'                            3072
chat low    '{"enable_thinking": true, "reasoning_effort": "low"}'    4096
chat medium '{"enable_thinking": true, "reasoning_effort": "medium"}' 6144

log "agent gaps"
cd /home/daniil/lm-inference
systemd-run --user --scope -q --unit=sc-tabby-mixed-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
    python3 qwen38_flash_next_sc400.py > $Q/tabby_mixed.log 2>&1 &
for i in $(seq 1 120); do curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health && break; sleep 5; done
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then
    $Q/capped.sh proxy 512M $PY $Q/agent-env/trace_proxy.py --upstream http://127.0.0.1:1337 --port 1338 \
        --log_dir $R/logs --api_key_file /home/daniil/lm-inference/tabbyAPI/api_tokens.yml >> $R/proxy.log 2>&1 &
    sleep 3
    (cd $Q/agent-env && $Q/capped.sh runner 2G $PY -u run_agents.py --tasks tasks_gap.json --runs $R --split trace \
        --parallel 1 --cap_tokens 60000 --max_minutes 30 --idle_minutes 10 --mem 6g --cpus 8 >> $R/runner.log 2>&1)
    log "agents finished"
else
    log "tabby did not come up, agent gaps skipped"
fi
for u in $(systemctl --user list-units --type=scope 'sc-proxy-*' 'sc-tabby-mixed-*' --no-legend | awk '{print $1}'); do
    systemctl --user stop $u
done
docker images --format '{{.Repository}}:{{.Tag}}' | grep '^xiaomimimo/' | xargs -r docker rmi > /dev/null 2>&1
log "tabby and proxy stopped, MiMo task images removed"

# the FP8-sourced quant is on HF (Vemloir/Qwen3.8-Flash-Next-exl3, branch 4.00bpw_tb400_fp8src);
# delete the local copy only if every file is there with the right size
if $PY - <<'PYEOF'
import os, sys
from huggingface_hub import HfApi
d = "/home/daniil/lm-inference/models/Qwen3.8-Flash-Next/SC-EXL3-4.00"
remote = {f.path: f.size for f in HfApi().list_repo_tree("Vemloir/Qwen3.8-Flash-Next-exl3",
          revision = "4.00bpw_tb400_fp8src", recursive = True) if hasattr(f, "size")}
bad = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and f.endswith((".safetensors", ".json"))
       and f != "ngram_embedding.safetensors" and remote.get(f) != os.path.getsize(os.path.join(d, f))]
sys.exit(1 if bad else 0)
PYEOF
then
    rm -rf $M/SC-EXL3-4.00
    log "local FP8-sourced quant removed (verified on HF)"
else
    log "HF copy incomplete, FP8-sourced quant kept; stopping before the BF16 conversion"
    exit 1
fi

log "BF16 conversion"
$Q/convert_bf16.sh >> $Q/convert_tb400_bf16.log 2>&1
grep -q "All done" $Q/convert_tb400_bf16.log || { log "BF16 conversion did not finish"; exit 1; }
log "qbench BF16-sourced quant"
(cd $TREE && PYTHONPATH=$TREE:$Q/pylibs MPLBACKEND=Agg PYTORCH_ALLOC_CONF=expandable_segments:True \
    systemd-run --user --scope -q --unit=sc-qbench-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
    $PY -u eval/qbench.py $Q/eval/qbench_bf16src.yaml >> $Q/eval/qbench_bf16src.log 2>&1) \
    && log "qbench done" || log "qbench failed"
