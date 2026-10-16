#!/usr/bin/env bash
# Resume the BF16 conversion after the disk-full stop (convert_bf16.sh resumes from args.json),
# then qbench the BF16-sourced quant against the BF16 reference (recomputed: its cache was freed)
Q=/home/daniil/lm-inference/sc-quant; TREE=/home/daniil/lm-inference/exllamav3-1.5.3-patched
PY=/home/daniil/lm-inference/tabbyAPI/venv/bin/python
log() { echo "$(date '+%F %H:%M:%S') $*"; }
log "BF16 conversion resume"
$Q/convert_bf16.sh >> $Q/convert_tb400_bf16.log 2>&1
tail -n 3 $Q/convert_tb400_bf16.log | grep -q "All done" || { log "BF16 conversion did not finish"; exit 1; }
log "qbench BF16-sourced quant"
(cd $TREE && PYTHONPATH=$TREE:$Q/pylibs MPLBACKEND=Agg PYTORCH_ALLOC_CONF=expandable_segments:True \
    systemd-run --user --scope -q --unit=sc-qbench-$(date +%s) -p MemoryMax=57G -p MemorySwapMax=8G \
    $PY -u eval/qbench.py $Q/eval/qbench_bf16src.yaml >> $Q/eval/qbench_bf16src.log 2>&1) \
    && log "qbench done" || log "qbench failed"
