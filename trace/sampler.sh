#!/usr/bin/env bash
# Every 120 s: timestamp, finished jobs, cumulative tokens (context + response) from the trace log
T=${T:-$HOME/lm-inference/sc-quant/trace/sampler_out}
while kill -0 215963 2>/dev/null; do
    n=$(grep -ac "tokens (" $T/chat_trace.log)
    tot=$(grep -a "tokens (" $T/chat_trace.log | tail -1 | sed 's/\x1b\[[0-9;]*m//g' | awk '{print $NF}' | tr -d ,)
    echo "$(date +%s) $(date +%H:%M:%S) jobs=$n total=${tot:-0} swapfree=$(awk '/SwapFree/{print $2}' /proc/meminfo)"
    sleep 120
done >> $T/sampler.log
