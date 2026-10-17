#!/bin/bash
# Runs ON the Colab G4 VM (RTX PRO 6000 Blackwell 96 GB, 176 GB RAM): fetch NVIDIA's
# Qwen3.8-Flash-Next-FP4-Local, unpack the community vLLM container (the checkpoint's scale format
# is not in upstream vLLM yet) with udocker, since Colab has no Docker daemon, run it in a chroot and serve on
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
# with MTP every decode step runs 1 + 3 draft tokens per sequence: 32 sequences need graphs up to 128
# tokens; the model card's 64 sent batches above 16 sequences to eager mode (270 tok/s at 32 vs 545 at 8)
MAX_CG=${MAX_CG:-256}
cd /content
echo "== $(date -u +%T) start"

pip install -q -U "huggingface_hub[hf_xet]" udocker
udocker --allow-root install > /dev/null

# model and container in parallel: HF runs ~90-160 MB/s, ghcr ~25 MB/s. Plain HTTP for the model
# (the xet transfer hung at 31 GB on the first run), both retried until they are complete;
# udocker pull exits 0 even when a layer fails, so the image is checked explicitly.
fetch_model() {
    for i in $(seq 8); do
        if HF_HUB_DISABLE_XET=1 hf download "$MODEL" --local-dir /content/model >> /content/dl_model.log 2>&1; then
            echo "== $(date -u +%T) model done"; return 0
        fi
        echo "model download attempt $i failed" >> /content/dl_model.log; sleep 20
    done
    echo "== model download failed"; return 1
}
fetch_model &
MODEL_PID=$!
if ! udocker --allow-root ps 2>/dev/null | grep -q "\['vllm'\]"; then
    for i in $(seq 6); do
        udocker --allow-root pull "$IMAGE" >> /content/dl_image.log 2>&1 || true
        udocker --allow-root verify "$IMAGE" > /dev/null 2>&1 && break
        echo "image pull attempt $i incomplete" >> /content/dl_image.log; sleep 20
    done
    udocker --allow-root verify "$IMAGE" > /dev/null 2>&1 || { echo "== image pull failed"; exit 1; }
    udocker --allow-root rm vllm > /dev/null 2>&1 || true
    udocker --allow-root create --name=vllm "$IMAGE" > /dev/null
    udocker --allow-root setup --nvidia --force vllm > /dev/null
fi
echo "== $(date -u +%T) image done"
wait $MODEL_PID
du -sh /content/model

cat > /content/run_vllm.sh <<'EOF'
export B12X_DENSE_SPLITK_TURBO=1 B12X_DYNAMIC_DETERMINISTIC_OUTPUT=0 B12X_DYNAMIC_DIRECT_EXPERT_SCALES=1
export B12X_DYNAMIC_SKIP_SPLIT_BARRIER_RESET=1 B12X_DYNAMIC_SPLIT_COMPUTE_MAC=224 B12X_DYNAMIC_SPLIT_FAST_PREPARE=1
export B12X_DYNAMIC_SPLIT_LOW_SMEM=1 B12X_DYNAMIC_SPLIT_ROUTE_COMPUTE=1 B12X_DYNAMIC_WORK_SOURCE=persistent_grid
export B12X_W4A16_A4_PREFILL=1 B12X_DISK_BACKEND=io_uring B12X_MHC_PDL=1 B12X_PCIE_ONESHOT_BLOCK_LIMIT=4
export B12X_PCIE_ONESHOT_PDL=1 B12X_PCIE_ONESHOT_THREADS=512 CUDA_BINARY_LOADER_THREAD_COUNT=8
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_MODULE_LOADING=LAZY CUTE_DSL_ARCH=sm_120a
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
    --gpu-memory-utilization 0.95 --kv-cache-dtype fp8 --no-language-model-only --linear-backend b12x \
    --load-format safetensors --mamba-cache-mode align --mamba-ssm-cache-dtype auto \
    --max-cudagraph-capture-size "$MAX_CG" --max-model-len 262144 --max-num-batched-tokens 6019 \
    --max-num-seqs "$MAX_SEQS" --mm-encoder-tp-mode data --mm-processor-cache-gb 0.0 --moe-backend b12x \
    --quantization modelopt_mixed --reasoning-parser qwen3 --tool-call-parser qwen3_xml \
    --speculative-config '{"method":"mtp","draft_sample_method":"probabilistic","rejection_sample_method":"standard","moe_backend":"b12x","num_speculative_tokens":3}'
EOF

# Run inside the unpacked image with a plain chroot (we are root on the VM). udocker's default
# PRoot mode rewrites paths, which pushed vLLM's ZMQ IPC socket path past the 107-byte limit.
ROOT=$(udocker --allow-root inspect -p vllm)
udocker --allow-root inspect "$IMAGE" > /content/image.json
python3 -c "import json; d=json.load(open('/content/image.json')); c=d.get('config') or d.get('Config') or {}; print('\n'.join(c.get('Env') or []))" > /content/image.env
mkdir -p "$ROOT/content"
mountpoint -q "$ROOT/content" || mount --bind /content "$ROOT/content"
mountpoint -q "$ROOT/dev" || mount --rbind /dev "$ROOT/dev"
mountpoint -q "$ROOT/proc" || mount --rbind /proc "$ROOT/proc"   # Colab cannot mount a fresh proc
mountpoint -q "$ROOT/sys" || mount --rbind /sys "$ROOT/sys"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"

echo "== $(date -u +%T) starting vllm"
pkill -f "[v]llm serve" || true; sleep 5
REGEN_API_KEY="$REGEN_API_KEY" MAX_SEQS="$MAX_SEQS" MAX_CG="$MAX_CG" setsid chroot "$ROOT" /bin/bash -c \
    'while IFS= read -r l; do [ -n "$l" ] && export "$l"; done < /content/image.env; export HOME=/root; cd /workspace; exec bash /content/run_vllm.sh' \
    > /content/vllm.log 2>&1 < /dev/null &
for i in $(seq 180); do
    if curl -sf -H "Authorization: Bearer $REGEN_API_KEY" http://127.0.0.1:8000/v1/models > /dev/null; then
        echo "== $(date -u +%T) vllm ready"; exit 0
    fi
    sleep 10
done
echo "== vllm not ready after 30 min"; tail -40 /content/vllm.log; exit 1
