#!/usr/bin/env bash
# Night run for calibration v2 (mixed reasoning modes + images), quantized from BF16:
#   1. frontend pages + screenshots, image items, image trace rows (on 4.05, with its vision tower)
#   2. pack calibration v2, build the held-out evaluation trace
#   3. qbench v1 (BF16-sourced) and 4.05 on the held-out trace, then free v1 (verified on HF)
#   4. convert from BF16 with calibration v2
#   5. qbench v2 on the held-out trace and on turboderp's trace
# Disk: logit caches are freed between qbench projects; the conversion needs ~100 GB at its peak.
# Skips if a model server is up.
set -u
Q=/home/daniil/lm-inference/sc-quant; M=/home/daniil/lm-inference/models/Qwen3.8-Flash-Next
TREE=/home/daniil/lm-inference/exllamav3-1.5.3-patched; PY=/home/daniil/lm-inference/tabbyAPI/venv/bin/python
T=$Q/trace/mixed; C=$Q/eval/_logit_cache/qbench
log() { echo "$(date '+%F %H:%M:%S') $*"; }
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then log "tabby is running, night run skipped"; exit 1; fi

capped() {  # name, command...: one memory-capped user scope per step
    local name=$1; shift
    systemd-run --user --scope -q --unit=sc-$name-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G "$@"
}
qb() {  # project file, log
    (cd $TREE && PYTHONPATH=$TREE:$Q/pylibs MPLBACKEND=Agg PYTORCH_ALLOC_CONF=expandable_segments:True \
        capped qbench $PY -u eval/qbench.py $Q/eval/$1 >> $Q/eval/$2 2>&1)
}
ref_dirs() { ls -d $C/logits_* 2>/dev/null; }

# 1. image rows
log "pages"
(cd $TREE && PYTHONPATH=$TREE EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 capped pages \
    $PY -u $Q/trace/gen_pages.py -m $M/EXL3-4.05 -mcl 44 -cs 131072 --tasks $Q/agent-env/tasks.json \
    -o $Q/images/pages > $T/gen_pages.log 2>&1) || log "pages failed (continuing without screenshots)"
python3 $Q/trace/make_image_items.py --images $Q/images --pages $Q/images/pages/pages.json \
    -o $T/image_items_cal.json --holdout $T/image_items_eval.json
for part in cal eval; do
    log "image trace $part"
    (cd $TREE && PYTHONPATH=$TREE EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 capped images \
        $PY -u $Q/trace/trace_images.py -m $M/EXL3-4.05 -mcl 44 -cs 131072 --items $T/image_items_$part.json \
        -o $T/images_${part}_trace.json -co $T/images_${part}_cal.safetensors > $T/trace_images_$part.log 2>&1) \
        || log "image trace $part failed"
done

# 2. calibration v2 and held-out trace
IMG=""; [ -f $T/images_cal_cal.safetensors ] && IMG="--images $T/images_cal_cal.safetensors"
$PY $Q/pack_v2.py --out $Q/calib_v2 $IMG || { log "pack_v2 failed"; exit 1; }
$PY $Q/eval_trace.py --manifest $Q/calib_v2/trace_manifest.json -o $Q/eval/heldout_trace.json || { log "eval_trace failed"; exit 1; }
log "calibration v2 packed"

# 3. held-out qbench of v1 and 4.05, then free v1 (on HF) and the logit caches
rm -rf $(ref_dirs)
qb qbench_heldout.yaml qbench_heldout.log && log "held-out qbench v1 / 4.05 done" || log "held-out qbench v1 / 4.05 failed"
rm -rf $(ref_dirs)
if $PY - <<'PYEOF'
import os, sys
from huggingface_hub import HfApi
d = "/home/daniil/lm-inference/models/Qwen3.8-Flash-Next/SC-EXL3-4.00-bf16src"
remote = {f.path: f.size for f in HfApi().list_repo_tree("Vemloir/Qwen3.8-Flash-Next-exl3",
          revision = "4.00bpw_tb400v1_bf16src", recursive = True) if hasattr(f, "size")}
bad = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and f.endswith((".safetensors", ".json"))
       and f != "ngram_embedding.safetensors" and remote.get(f) != os.path.getsize(os.path.join(d, f))]
sys.exit(1 if bad else 0)
PYEOF
then
    rm -rf $M/SC-EXL3-4.00-bf16src
    log "local v1 quant removed (verified on HF)"
else
    log "v1 not complete on HF, kept; stopping before the conversion (not enough disk)"
    exit 1
fi

# 4. conversion
log "conversion v2 (free $(df -BG --output=avail / | tail -1 | tr -d ' '))"
$Q/convert_v2.sh >> $Q/convert_v2.log 2>&1
tail -n 3 $Q/convert_v2.log | grep -q "All done" || { log "conversion v2 did not finish"; exit 1; }
log "conversion v2 done"

# 5. evaluation of v2
qb qbench_heldout_v2.yaml qbench_heldout_v2.log && log "held-out qbench v2 done" || log "held-out qbench v2 failed"
rm -rf $(ref_dirs)
qb qbench_bf16_v2.yaml qbench_bf16_v2.log && log "turboderp-trace qbench v2 done" || log "turboderp-trace qbench v2 failed"
log "night run done"
