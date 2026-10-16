#!/usr/bin/env bash
# Image dataset, then calibration v2 with images, then conversion from BF16 and evaluation:
#   1. frontend pages + screenshots, image items, image trace rows (on 4.05 with its vision tower)
#   2. pack calibration v2 with the image rows (stops if there are none), held-out evaluation trace
#   3. convert from BF16 with calibration v2 (recipe tb400, as v1)
#   4. qbench v2 and 4.05 on the held-out trace, then on turboderp's trace
# Nothing is deleted except qbench logit caches between projects.
set -u
Q=$HOME/lm-inference/sc-quant; M=$HOME/lm-inference/models/Qwen3.8-Flash-Next
TREE=$HOME/lm-inference/exllamav3-1.5.3-patched; PY=$HOME/lm-inference/tabbyAPI/venv/bin/python
T=$Q/trace/mixed; C=$Q/eval/_logit_cache/qbench
log() { echo "$(date '+%F %H:%M:%S') $*"; }
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then log "tabby is running, skipped"; exit 1; fi

capped() {  # name, command...: one memory-capped user scope per step
    local name=$1; shift
    systemd-run --user --scope -q --unit=sc-$name-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G "$@"
}
gen() {  # name, script, args...: model-loading generation step on 4.05 (same memory flags as the chat traces)
    local name=$1 script=$2; shift 2
    (cd $TREE && PYTHONPATH=$TREE EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 capped $name \
        $PY -u $script -m $M/EXL3-4.05 -mcl 44 -cs 65536 -ambs 8 -rcs 1 "$@")
}
qb() {  # project file, log
    (cd $TREE && PYTHONPATH=$TREE:$Q/pylibs MPLBACKEND=Agg PYTORCH_ALLOC_CONF=expandable_segments:True \
        capped qbench $PY -u eval/qbench.py $Q/eval/$1 >> $Q/eval/$2 2>&1)
}

# 1. image rows
log "pages"
gen pages $Q/trace/gen_pages.py --tasks $Q/agent-env/tasks.json -o $Q/images/pages > $T/gen_pages.log 2>&1 \
    && log "pages done: $(python3 -c "import json; print(len(json.load(open('$Q/images/pages/pages.json'))))") screenshots" \
    || log "pages failed (continuing without screenshots)"
python3 $Q/trace/make_image_items.py --images $Q/images --pages $Q/images/pages/pages.json \
    -o $T/image_items_cal.json --holdout $T/image_items_eval.json
for part in cal eval; do
    log "image trace $part"
    gen images $Q/trace/trace_images.py --items $T/image_items_$part.json \
        -o $T/images_${part}_trace.json -co $T/images_${part}_cal.safetensors > $T/trace_images_$part.log 2>&1 \
        && log "image trace $part done" || log "image trace $part failed"
done
[ -f $T/images_cal_cal.safetensors ] || { log "no image rows, stopping before the conversion"; exit 1; }

# 2. calibration v2 and held-out trace
$PY $Q/pack_v2.py --out $Q/calib_v2 --images $T/images_cal_cal.safetensors || { log "pack_v2 failed"; exit 1; }
$PY $Q/eval_trace.py --manifest $Q/calib_v2/trace_manifest.json -o $Q/eval/heldout_trace.json || { log "eval_trace failed"; exit 1; }
log "calibration v2 packed: $(python3 -c "import json; m=json.load(open('$Q/calib_v2/trace_manifest.json')); print(len(m['cal']), 'rows, width', m['cols'])")"

# 3. conversion (needs ~100 GB at its peak; no logit cache is kept meanwhile)
rm -rf $C/logits_*
log "conversion v2 (free $(df -BG --output=avail / | tail -1 | tr -d ' '))"
$Q/convert_v2.sh >> $Q/convert_v2.log 2>&1
tail -n 3 $Q/convert_v2.log | grep -q "All done" || { log "conversion v2 did not finish"; exit 1; }
log "conversion v2 done"

# 4. evaluation
rm -rf $C/logits_*
qb qbench_heldout_v2.yaml qbench_heldout_v2.log && log "held-out qbench done" || log "held-out qbench failed"
rm -rf $C/logits_*
qb qbench_bf16_v2.yaml qbench_bf16_v2.log && log "turboderp-trace qbench done" || log "turboderp-trace qbench failed"
log "all done"
