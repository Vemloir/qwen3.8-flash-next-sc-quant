#!/usr/bin/env bash
# Conversion v3 from BF16 with calibration v2 on exllamav3 1.6.1 (+ SC patches): recipe_v3 (4.05 bpw: expert
# bit splits per layer from split_grid, layers 43-47 back to 4, shared experts/k/v/indexer 8, out_proj 7, head 8),
# affinity-weighted expert Hessians (eps 0.1), output scales always (as v2). Skips if a model server is up.
set -u
M=/home/daniil/lm-inference/models/Qwen3.8-Flash-Next; Q=/home/daniil/lm-inference/sc-quant
OUT=$M/SC-EXL3-4.05-v3; W=$Q/convert_v3
log() { echo "$(date '+%F %H:%M:%S') $*"; }
if curl -s -o /dev/null --max-time 5 http://127.0.0.1:1337/health; then log "tabby is running, conversion skipped"; exit 1; fi
mkdir -p $OUT $W
ln -f $M/ngram_k8.safetensors $OUT/ngram_embedding.safetensors
RESUME=""; [ -f $W/args.json ] && RESUME="-r"
log "conversion start ${RESUME:-(fresh)}, free $(df -BG --output=avail / | tail -1 | tr -d ' ')"
cd /home/daniil/lm-inference/exllamav3-1.6.1-sc
PYTHONPATH=$PWD PYTORCH_ALLOC_CONF=expandable_segments:True \
/home/daniil/lm-inference/tabbyAPI/venv/bin/python -u convert.py -i $M/BF16 -w $W -o $OUT \
    -rcp $Q/calib/recipe_v3.yaml -cd $Q/calib_v2/cal_trace.safetensors \
    -cr $(python3 -c "import json; print(len(json.load(open('$Q/calib_v2/trace_manifest.json'))['cal']))") \
    -cc $(python3 -c "import json; print(json.load(open('$Q/calib_v2/trace_manifest.json'))['cols'])") -cch 16384 \
    -ngf $M/ngram_k8.safetensors -vb 8 -ld -d 0 -cpi 1800 --out_scales always -mae 0.1 $RESUME
log "conversion exited with $?"
