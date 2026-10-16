#!/usr/bin/env bash
# Night run: pipeline tabby (memory-capped) -> 9 full agent sessions one at a time -> stop tabby ->
# continue the truncated chat-trace responses. Every process runs in its own capped user scope.
set -u
Q=$HOME/lm-inference/sc-quant
R=$Q/agent-runs
PY=$HOME/lm-inference/tabbyAPI/venv/bin/python
log() { echo "$(date '+%F %H:%M:%S') $*"; }

log "night run start"
if ! curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then
    cd $HOME/lm-inference
    systemd-run --user --scope -q --unit=sc-tabby-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
        python3 qwen38_flash_next_exl3_4.05.py > $Q/tabby_agent.log 2>&1 &
    for i in $(seq 1 120); do curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health && break; sleep 5; done
fi
curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health || { log "tabby did not come up"; exit 1; }
log "tabby healthy"

if ! systemctl --user list-units --type=scope 'sc-proxy-*' --no-legend | grep -q .; then
    $Q/capped.sh proxy 512M $PY $Q/agent-env/trace_proxy.py --upstream http://127.0.0.1:1337 --port 1338 \
        --log_dir $R/logs --api_key_file $HOME/lm-inference/tabbyAPI/api_tokens.yml >> $R/proxy.log 2>&1 &
    sleep 3
fi

log "agents start"
cd $Q/agent-env
$Q/capped.sh runner 2G $PY -u run_agents.py --tasks tasks_full.json --runs $R --split trace --parallel 1 \
    --cap_tokens 100000000 --max_minutes 120 --idle_minutes 15 --mem 6g --cpus 8 \
    --keep_images xiaomimimo/mimo-v2.6-rl-oss:general-agent-env-0 >> $R/runner.log 2>&1
log "agents finished"

# chain_continue.sh waits for the runner scope (already gone), stops tabby and runs the continuation
$Q/capped.sh chain 256M $Q/chain_continue.sh >> $Q/chain_continue.log 2>&1
log "night run done"
