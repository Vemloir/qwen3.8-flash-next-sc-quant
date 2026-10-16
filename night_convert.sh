#!/usr/bin/env bash
# Night conversion of the turboderp-style 4.00 recipe with our calibration. Skips if a model server
# is up (the conversion needs nearly all RAM and the GPU).
set -u
M=/home/daniil/lm-inference/models/Qwen3.8-Flash-Next; Q=/home/daniil/lm-inference/sc-quant
OUT=$M/SC-EXL3-4.00; W=$Q/convert_tb400
log() { echo "$(date '+%F %H:%M:%S') $*"; }
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then log "tabby is running, conversion skipped"; exit 1; fi
mkdir -p $OUT $W
ln -f $M/ngram_k8.safetensors $OUT/ngram_embedding.safetensors
RESUME=""; [ -f $W/args.json ] && RESUME="-r"
log "conversion start ${RESUME:-(fresh)}"
cd /home/daniil/lm-inference/exllamav3-1.5.3-patched
PYTHONPATH=$PWD PYTORCH_ALLOC_CONF=expandable_segments:True \
systemd-run --user --scope -q --unit=sc-convert-tb400-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
    /home/daniil/lm-inference/tabbyAPI/venv/bin/python -u convert.py -i $M/FP8 -w $W -o $OUT \
    -rcp $Q/calib/recipe_tb400.yaml -cd $Q/calib/cal_trace.safetensors -cr 124 -cc 135168 -cch 16384 \
    -ngf $M/ngram_k8.safetensors -vb 8 -ld -d 0 -cpi 1800 $RESUME
log "conversion exited with $?"
