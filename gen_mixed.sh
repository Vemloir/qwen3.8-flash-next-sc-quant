#!/usr/bin/env bash
# Chat part of the mixed-effort calibration trace: the same 207 conversations sampled at medium,
# low and with thinking off (the existing chat trace covers xhigh), on our 4.00 bpw quant
T=/home/daniil/lm-inference/sc-quant/trace; TREE=/home/daniil/lm-inference/exllamav3-1.5.3-patched
M=/home/daniil/lm-inference/models/Qwen3.8-Flash-Next/SC-EXL3-4.00
cd $TREE
run() {  # name, template vars, max_new_tokens, target tokens
  echo "=== $1 $(date '+%F %H:%M:%S')"
  PYTHONPATH=$TREE EXL3_HOST_MEM_RESERVE_MB=384 EXL3_GR_MIX_TILED=0 \
  /home/daniil/lm-inference/tabbyAPI/venv/bin/python -u $T/trace_chat.py -m $M -mcl 44 -cs 65536 -ambs 8 -rcs 1 \
      -o $T/mixed/chat_$1.json -co $T/mixed/chat_$1.safetensors -cr 1000 -cc 8192 \
      -tv "$2" --max_new_tokens $3 --target_tokens $4 --max_epochs 1 > $T/mixed/chat_$1.log 2>&1
  echo "=== $1 exit $? $(date '+%F %H:%M:%S')"
}
run off    '{"enable_thinking": false}'                            3072 300000
run low    '{"enable_thinking": true, "reasoning_effort": "low"}'    4096 250000
run medium '{"enable_thinking": true, "reasoning_effort": "medium"}' 6144 300000
echo "=== done $(date '+%F %H:%M:%S')"
