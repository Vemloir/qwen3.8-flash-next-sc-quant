#!/usr/bin/env bash
# After the chat generation finishes: KLD of the FP8 checkpoint vs BF16 (from the cached FP8
# logits), then the BF16-reference qbench of our quant and 4.05. Then the agent part of the
# mixed-effort trace: serve our 4.00 bpw
# quant with tabby, start the logging proxy (injects the reasoning mode from the -eff_<mode>
# session suffix) and run the 9 full agent tasks at off, low and medium, one session at a time
set -u
Q=/home/daniil/lm-inference/sc-quant; R=$Q/agent-runs-mixed; PY=/home/daniil/lm-inference/tabbyAPI/venv/bin/python
log() { echo "$(date '+%F %H:%M:%S') $*"; }
mkdir -p $R/logs
while systemctl --user is-active -q sc-gen-mixed; do sleep 60; done
log "chat generation finished"
TREE=/home/daniil/lm-inference/exllamav3-1.5.3-patched
qb() {  # project file, log
    (cd $TREE && PYTHONPATH=$TREE:$Q/pylibs MPLBACKEND=Agg PYTORCH_ALLOC_CONF=expandable_segments:True \
        systemd-run --user --scope -q --unit=sc-qbench-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
        $PY -u eval/qbench.py $Q/eval/$1 >> $Q/eval/$2 2>&1)
}
log "qbench FP8 vs BF16"
# the BF16 reference cache only fits once the FP8 one is gone, so a failed FP8 pass keeps the
# FP8 cache and skips the BF16 reference pass
if qb qbench_fp8_vs_bf16.yaml qbench_fp8_vs_bf16.log; then
    log "FP8 vs BF16 done"
    rm -rf $Q/eval/_logit_cache/qbench/logits_9c16abc00f29b0e0_a902cd745d29fc3d
    log "FP8 logit cache removed, qbench BF16 reference"
    qb qbench_bf16.yaml qbench_bf16.log && log "BF16 qbench done" || log "BF16 qbench failed"
else
    log "FP8 vs BF16 failed, FP8 cache kept, BF16 qbench skipped"
fi
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then log "a server is already on :1337, abort"; exit 1; fi
cd /home/daniil/lm-inference
systemd-run --user --scope -q --unit=sc-tabby-mixed-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
    python3 qwen38_flash_next_sc400.py > $Q/tabby_mixed.log 2>&1 &
for i in $(seq 1 120); do curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health && break; sleep 5; done
curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health || { log "tabby did not come up"; exit 1; }
log "tabby healthy"
$Q/capped.sh proxy 512M $PY $Q/agent-env/trace_proxy.py --upstream http://127.0.0.1:1337 --port 1338 \
    --log_dir $R/logs --api_key_file /home/daniil/lm-inference/tabbyAPI/api_tokens.yml >> $R/proxy.log 2>&1 &
sleep 3
log "agents start"
cd $Q/agent-env
$Q/capped.sh runner 2G $PY -u run_agents.py --tasks tasks_mixed.json --runs $R --split trace --parallel 1 \
    --cap_tokens 100000000 --max_minutes 120 --idle_minutes 15 --mem 6g --cpus 8 >> $R/runner.log 2>&1
log "agents finished"
for u in $(systemctl --user list-units --type=scope 'sc-proxy-*' 'sc-tabby-mixed-*' --no-legend | awk '{print $1}'); do
    systemctl --user stop $u
done
log "tabby and proxy stopped"
