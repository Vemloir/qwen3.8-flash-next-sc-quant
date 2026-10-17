#!/bin/bash
# Runs ON a Colab High-RAM A100 VM (80 GB GPU, 167 GB RAM, ~194 GB disk) with Google Drive mounted: convert
# Qwen3.8-Flash-Next from the BF16 copy on Drive with calibration set 2, same recipe and flags as the
# published 4.05 bpw quant (recipe_v3, affinity-weighted Hessians eps 0.1, output scales always,
# vision 8 bpw), so the calibration data is the only change.
#
#   exllamav3 1.6.1 from the release wheel (cu128 build; torch 2.11 cu128 installed to match) with
#   the Python-only patches from sc-quant/patches/exllamav3-1.6.1 applied to the v1.6.1 source tree
#   the n-gram table: our 8-bit table from the published model (two parts, merged into one file in
#   the output directory, where convert.py takes it without another copy)
#   checkpoints stay on local disk (-ld keeps one copy); the finished model is copied to Drive
#   needs the High-RAM A100 (80 GB GPU, 167 GB RAM): 2.15M calibration tokens take ~85 GB of state
#   after the hyper-connection expansion, plus 52 GB for the n-gram table in /dev/shm
#
#   (calib_v3/ uploaded to /content/calib_v3)  bash a100_convert.sh
set -euo pipefail
DRIVE=/content/drive/MyDrive/sc-quant
BF16=$DRIVE/BF16
CAL=${CAL:-/content/calib_v3/cal_trace.safetensors}
MANIFEST=$(dirname "$CAL")/trace_manifest.json
NAME=${NAME:-SC2-EXL3-4.05}
OUT=/content/$NAME
WORK=/content/work
log() { echo "== $(date -u +%T) $*"; }

[ -f "$BF16/config.json" ] || { echo "BF16 not found on Drive, mount it first (colab drivemount)"; exit 1; }
[ -f "$CAL" ] || { echo "calibration file $CAL not found"; exit 1; }
log "start, $(nproc) cpus, $(free -g | awk 'NR==2{print $2}') GB RAM, $(df -BG --output=avail / | tail -1) free"

if ! python3 -c "import torch, exllamav3; assert torch.version.cuda.startswith(\"12\")" 2>/dev/null; then
    log "installing torch 2.11 cu128 and exllamav3 1.6.1"
    pip install -q -U "huggingface_hub[hf_xet]"
    # the exact local version: a plain torch==2.11.0 counts the preinstalled cu130 build as a match
    pip install -q "torch==2.11.0+cu128" --index-url https://download.pytorch.org/whl/cu128
    pip install -q "https://github.com/turboderp-org/exllamav3/releases/download/v1.6.1/exllamav3-1.6.1+cu128.torch2.11.0-cp313-cp313-linux_x86_64.whl"
fi
if [ ! -d /content/exl3 ]; then
    git clone -q --depth 1 -b v1.6.1 https://github.com/turboderp-org/exllamav3 /content/exl3
    git clone -q --depth 1 https://github.com/Vemloir/qwen3.8-flash-next-sc-quant /content/sc-quant
    (cd /content/exl3 && git apply /content/sc-quant/patches/exllamav3-1.6.1/*.patch)
fi
python3 -c "import torch, exllamav3; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'exllamav3', __import__('importlib.metadata').metadata.version('exllamav3'), torch.cuda.get_device_name(0))"

mkdir -p "$OUT" "$WORK"
# The table is read at random for every calibration token; Colab's network disk serves that at
# ~12 MB/s, so it lives in /dev/shm (RAM) with a symlink where convert.py expects it (same path,
# so no copy is made)
SHM=/dev/shm/ngram_embedding.safetensors
if [ ! -f "$SHM" ]; then
    log "fetching the 8-bit n-gram table"
    HF_HUB_DISABLE_XET=1 hf download Vemloir/Qwen3.8-Flash-Next-sc-exl3 --revision 4.05bpw_sc_h8_ng8 \
        --include "ngram_embedding-*" --local-dir /content/ngram > /dev/null
    python3 /content/sc-quant/regen/merge_safetensors.py --delete -o "$SHM" \
        /content/ngram/ngram_embedding-00001-of-00002.safetensors /content/ngram/ngram_embedding-00002-of-00002.safetensors
fi
ln -sf "$SHM" "$OUT/ngram_embedding.safetensors"

ROWS=$(python3 -c "import json; print(len(json.load(open('$MANIFEST'))['cal']))")
COLS=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['cols'])")
RESUME=""; [ -f "$WORK/args.json" ] && RESUME="-r"
log "converting: $ROWS rows, width $COLS ${RESUME:-(fresh)}"
cd /content/exl3
PYTHONPATH=/content/exl3 PYTORCH_ALLOC_CONF=expandable_segments:True \
python3 -u convert.py -i "$BF16" -w "$WORK" -o "$OUT" \
    -rcp /content/sc-quant/calib/recipe_v3.yaml -cd "$CAL" -cr "$ROWS" -cc "$COLS" -cch 16384 \
    -ngf "$OUT/ngram_embedding.safetensors" -vb 8 -ld -d 0 -cpi 1800 --out_scales always -mae 0.1 $RESUME
log "conversion done"

log "copying the model (without the n-gram table) to Drive"
mkdir -p "$DRIVE/$NAME"
for f in "$OUT"/*; do
    [ "$(basename "$f")" = ngram_embedding.safetensors ] && continue
    cp "$f" "$DRIVE/$NAME/"
done
log "ALL DONE"
