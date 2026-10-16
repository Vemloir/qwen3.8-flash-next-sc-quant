#!/usr/bin/env bash
# Run reason_test.py with the hard prompts on both quants one after another
M=$HOME/lm-inference/models/Qwen3.8-Flash-Next; Q=$HOME/lm-inference/sc-quant
cd $HOME/lm-inference/exllamav3-1.5.3-patched
for q in SC-EXL3-4.00 EXL3-4.05; do
  echo "=== $q $(date '+%F %H:%M:%S')"
  PYTHONPATH=$PWD $HOME/lm-inference/tabbyAPI/venv/bin/python -u $Q/tools/reason_test.py \
      -m $M/$q -mcl 44 -cs 65536 --prompts $Q/eval/hard_prompts.json --out $Q/eval/hard_$q.md
done
echo "=== done $(date '+%F %H:%M:%S')"
