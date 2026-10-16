#!/usr/bin/env bash
# After both FP8 halves finish, fetch the BF16 shards holding the n-gram table
cd $HOME/lm-inference/sc-quant
until grep -q "ALL DONE" dl_fp8_a.log && grep -q "ALL DONE" dl_fp8_b.log; do sleep 60; done
python3 -u hfdl.py --repo Qwen/Qwen3.8-Flash-Next --out $HOME/lm-inference/models/Qwen3.8-Flash-Next/BF16-table --threads 12 \
    --files config.json model.safetensors.index.json $(cat $HOME/lm-inference/sc-quant/bf16_table_files.txt)
