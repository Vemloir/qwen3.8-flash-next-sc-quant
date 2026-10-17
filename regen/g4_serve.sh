#!/bin/bash
# Runs ON the Colab G4 VM (RTX PRO 6000 Blackwell 96 GB, 176 GB RAM): fetch NVIDIA's
# Qwen3.8-Flash-Next-FP4-Local, start the community vLLM container (the checkpoint's scale format
# is not in upstream vLLM yet) under udocker, since Colab has no Docker daemon, and serve on
# 127.0.0.1:8000 only; clients reach it through the SSH port forward in g4.sh.
#
# Settings follow the model card's RTX PRO 6000 recipe (PLE table offloaded to host RAM, FP8 KV
# cache, MTP speculative decoding with standard rejection sampling, so the output distribution is
# the model's), with more concurrent sequences for batch generation and no output length cap.
#
#   REGEN_API_KEY=... bash g4_serve.sh        (started by g4.sh up)
set -euo pipefail
: "${REGEN_API_KEY:?}"
IMAGE=ghcr.io/local-inference-lab/vllm:release-10152026
MODEL=nvidia/Qwen3.8-Flash-Next-FP4-Local
MAX_SEQS=${MAX_SEQS:-32}
cd /content
echo "== $(date -u +%T) start"

pip install -q -U "huggingface_hub[hf_xet]" udocker
udocker --allow-root install > /dev/null

# model and container in parallel: HF runs ~160 MB/s, ghcr ~25 MB/s
( hf download "$MODEL" --local-dir /content/model > /content/dl_model.log 2>&1; echo "== $(date -u +%T) model done" ) &
if ! udocker --allow-root inspect vllm > /dev/null 2>&1; then
    udocker --allow-root pull "$IMAGE" > /content/dl_image.log 2>&1
    udocker --allow-root create --name=vllm "$IMAGE" > /dev/null
    udocker --allow-root setup --nvidia --force vllm > /dev/null
fi
echo "== $(date -u +%T) image done"
wait
du -sh /content/model

cat > /content/run_vllm.sh <<'EOF'
export B12X_DENSE_SPLITK_TURBO=1 B12X_DYNAMIC_DETERMINISTIC_OUTPUT=0 B12X_DYNAMIC_DIRECT_EXPERT_SCALES=1
export B12X_DYNAMIC_SKIP_SPLIT_BARRIER_RESET=1 B12X_DYNAMIC_SPLIT_COMPUTE_MAC=224 B12X_DYNAMIC_SPLIT_FAST_PREPARE=1
export B12X_DYNAMIC_SPLIT_LOW_SMEM=1 B12X_DYNAMIC_SPLIT_ROUTE_COMPUTE=1 B12X_DYNAMIC_WORK_SOURCE=persistent_grid
export B12X_W4A16_A4_PREFILL=1 B12X_DISK_BACKEND=io_uring B12X_MHC_PDL=1 B12X_PCIE_ONESHOT_BLOCK_LIMIT=4
export B12X_PCIE_ONESHOT_PDL=1 B12X_PCIE_ONESHOT_THREADS=512 CUDA_BINARY_LOADER_THREAD_COUNT=8
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_MODULE_LOADING=LAZY CUTE_DSL_ARCH=sm_120a INSTANTTENSOR_BACKEND=BUFFERED
export OMP_NUM_THREADS=2 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True SAFETENSORS_FAST_GPU=1
export TORCHINDUCTOR_LOOP_ORDERING_AFTER_FUSION=0 TORCH_CUDA_ARCH_LIST=12.0a
export VLLM_B12X_DENSE_ACTIVATION_MODE=auto VLLM_B12X_MOE_FP4_FORCE_A16=1 VLLM_CAUSAL_CONV1D_UPDATE_HOIST=1
export VLLM_COMPUTE_NANS_IN_LOGITS=0 VLLM_DISABLED_KERNELS=MarlinFP8ScaledMMLinearKernel
export VLLM_GDN_SPEC_DECODE_METADATA_FASTPATH=1 VLLM_LM_HEAD_A16=1 VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0
export VLLM_MTP_NVFP4_LM_HEAD=1 VLLM_MXFP8_LM_HEAD=0 VLLM_PLE_CPU_OFFLOAD=1 VLLM_QWEN3_8_FLASH_NEXT_HC_TP=0
export VLLM_QWEN3_8_FLASH_NEXT_MTP_COMPACT=1 VLLM_QWEN3_8_FLASH_NEXT_OVERLAP=1 VLLM_SSM_CONV_STATE_LAYOUT=DS
export VLLM_USE_FASTOKENS=1 VLLM_USE_V2_MODEL_RUNNER=1 VLLM_WORKER_MULTIPROC_METHOD=spawn
export TORCH_ALLOW_TF32_CUBLAS_OVERRIDE=1
exec vllm serve /content/model \
    --served-model-name nvidia/Qwen3.8-Flash-Next-FP4-Local \
    --host 127.0.0.1 --port 8000 --api-key "$REGEN_API_KEY" \
    --async-scheduling --block-size 64 \
    --compilation-config '{"cudagraph_mode":"FULL_AND_PIECEWISE","custom_ops":["all"]}' \
    --dtype bfloat16 --enable-auto-tool-choice --enable-chunked-prefill --no-enable-flashinfer-autotune \
    --enable-prefix-caching --enable-prompt-tokens-details --gdn-decode-kernel b12x \
    --gpu-memory-utilization 0.975 --kv-cache-dtype fp8 --no-language-model-only --linear-backend b12x \
    --load-format instanttensor --mamba-cache-mode align --mamba-ssm-cache-dtype auto \
    --max-cudagraph-capture-size 64 --max-model-len 262144 --max-num-batched-tokens 6019 \
    --max-num-seqs "$MAX_SEQS" --mm-encoder-tp-mode data --mm-processor-cache-gb 0.0 --moe-backend b12x \
    --quantization modelopt_mixed --reasoning-parser qwen3 --tool-call-parser qwen3_xml \
    --speculative-config '{"method":"mtp","draft_sample_method":"probabilistic","rejection_sample_method":"standard","moe_backend":"b12x","num_speculative_tokens":3}'
EOF

echo "== $(date -u +%T) starting vllm"
REGEN_API_KEY="$REGEN_API_KEY" MAX_SEQS="$MAX_SEQS" \
    udocker --allow-root run --nobanner -v /content:/content -e REGEN_API_KEY -e MAX_SEQS vllm \
    /bin/bash /content/run_vllm.sh > /content/vllm.log 2>&1 &
for i in $(seq 180); do
    if curl -sf -H "Authorization: Bearer $REGEN_API_KEY" http://127.0.0.1:8000/v1/models > /dev/null; then
        echo "== $(date -u +%T) vllm ready"; exit 0
    fi
    sleep 10
done
echo "== vllm not ready after 30 min"; tail -40 /content/vllm.log; exit 1
