#!/usr/bin/env bash
# After the agent runner finishes: stop the pipeline's tabby instance, then continue the truncated
# chat-trace responses with the same model under the same memory cap
cd $HOME/lm-inference/sc-quant
while systemctl --user list-units --type=scope 'sc-runner-*' --no-legend | grep -q .; do sleep 60; done
echo "$(date +%H:%M:%S) runner finished, stopping pipeline tabby"
for u in $(systemctl --user list-units --type=scope 'sc-tabby-*' --no-legend | awk '{print $1}'); do systemctl --user stop "$u"; done
sleep 20
echo "$(date +%H:%M:%S) starting continuation"
cd $HOME/lm-inference/exllamav3-1.5.3-patched
PYTHONPATH=$HOME/lm-inference/exllamav3-1.5.3-patched EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 \
systemd-run --user --scope -q --unit=sc-continue-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
    $HOME/lm-inference/tabbyAPI/venv/bin/python -u $HOME/lm-inference/sc-quant/continue_trace.py \
    -m $HOME/lm-inference/models/Qwen3.8-Flash-Next/EXL3-4.05 -mcl 44 -cs 65536 -ambs 8 -rcs 1 \
    --trace $HOME/lm-inference/sc-quant/trace/chat_trace.json \
    -o $HOME/lm-inference/sc-quant/trace/chat_trace_full.json --max_total 65536
rc=$?; echo "$(date +%H:%M:%S) continuation exited with $rc"
