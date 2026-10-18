---
license: other
license_name: qwen-community-1.0
license_link: LICENSE
base_model: Qwen/Qwen3.8-Flash-Next
base_model_relation: quantized
pipeline_tag: image-text-to-text
datasets:
- Vemloir/Qwen3.8-Flash-Next-calib-agents
- Vemloir/Qwen3.8-Flash-Next-calib-chat
- Vemloir/Qwen3.8-Flash-Next-calib-multimodal
tags:
- exl3
- exllamav3
- self-calibrated
- moe
---

# Qwen3.8-Flash-Next EXL3 4.05 bpw, self-calibrated

EXL3 quant of [Qwen/Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next), converted from the BF16
checkpoint. Same model size in memory as turboderp's
[4.05bpw_h6_ng6](https://huggingface.co/turboderp/Qwen3.8-Flash-Next-exl3/tree/4.05bpw_h6_ng6) (68.6 GB against
68.4 GB; the n-gram table streamed from disk is 8-bit here, 13 GB more), measurably closer to BF16 on held-out data
that played no part in calibration. Code, calibration data and evaluation data are all public, so the quant can be
reproduced end to end.

| | |
|---|---|
| Bits per weight (excluding head) | 4.05 (4.0498) |
| Head (`lm_head`) | 8 bpw |
| Vision tower | 8 bpw |
| Converted with | exllamav3 1.6.1 + affinity-weighted expert Hessians (see below) |
| Runs on | stock exllamav3 >= 1.6.1, TabbyAPI |
| n-gram (PLE) table | 8 bpw, 52 GB, see below |

## Open and reproducible

Everything needed to rebuild this quant from the BF16 checkpoint is public:

| | |
|---|---|
| Code (MIT) | [Vemloir/qwen3.8-flash-next-sc-quant](https://github.com/Vemloir/qwen3.8-flash-next-sc-quant): calibration data generation (chat, images, agent harnesses in Docker), quality checks, packing, bit allocation, conversion and evaluation scripts, exllamav3 1.6.1 patches |
| Calibration data that went into this quant (ODbL-1.0) | 2.15M tokens: [agents](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-agents), [chat](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-chat), [multimodal](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-multimodal), each also as `cal_trace.safetensors` in exllamav3 `convert.py --cal_data` format |
| All generated data (ODbL-1.0) | 5.0M tokens, a superset of the calibration data with the same quality checks: [Vemloir/Qwen3.8-Flash-Next-traces](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-traces) |
| Evaluation (traces, per-token results) | [Vemloir/Qwen3.8-Flash-Next-evaluation](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-evaluation): the traces and the per-token results behind the numbers on this card |
| Recipe | `recipe.yaml` (per-tensor bits) and `bit_allocation.md` in the [4.05bpw_sc_h8_ng8](https://huggingface.co/Vemloir/Qwen3.8-Flash-Next-sc-exl3/tree/4.05bpw_sc_h8_ng8) branch |
| Toolchain | exllamav3 1.6.1 with patches 0001-0007 from the code repository (the quant itself runs on stock exllamav3 >= 1.6.1); conversion flags in `regen/a100_convert.sh` |

## Results

KL divergence of the output distribution against BF16 (lower is better), teacher-forced on assistant turns, 95%
intervals from a paired bootstrap over rows. Neither trace overlaps the calibration data. Positions the chat template writes itself (`<think>\n`, or `<think>\n\n</think>\n\n` with reasoning off: 166 of 23k scored tokens on the Nemotron trace, 32 of 26k on the multilingual one) are left out for all quants: the model never generates them in real use, while a teacher-forced trace scores them as part of the answer.

**NVIDIA Nemotron 3.5 Super post-training conversations** (48 rows, 23k scored tokens; assistant turns written by
Qwen3.5-397B / GLM-5.2 / Kimi-K2.6):

| Source | This quant | turboderp 4.05 | Difference |
|---|---|---|---|
| Agentic tool calling | 0.01019 | 0.01141 | -10.7% |
| Knowledge-base support agent | 0.00908 | 0.01199 | -24.3% |
| SWE (OpenCode trajectories) | 0.01096 | 0.01307 | -16.1% |
| Chat | 0.00573 | 0.00703 | -18.5% |
| Multi-turn instruction following | 0.00644 | 0.00623 | +3.4% |
| Math proofs | 0.00216 | 0.00302 | -28.3% |
| **All** | **0.00767** | **0.00903** | **-15.1% [-22.4%, -7.3%]**, lower on 39/48 rows |

**Multilingual** (52 rows, 26k scored tokens: Russian GrandMaster prompts with Qwen3-235B answers; 9 more languages
from Nemotron-SFT-Multilingual, translated answers):

| | This quant | turboderp 4.05 | Difference |
|---|---|---|---|
| Russian (16 rows) | 0.00745 | 0.00968 | -23.1% [-36.5%, -9.3%] |
| **All** | **0.00543** | **0.00656** | **-17.2% [-25.5%, -9.5%]**, lower on 42/52 rows |

Per language, the per-source chart and the previous version: see the [model card](https://huggingface.co/Vemloir/Qwen3.8-Flash-Next-sc-exl3).

Caveats:
- The head is 8 bpw here against 6 in turboderp's quant (+~150 MB; the head is outside the bits-per-weight budget
  in both), and the n-gram table 8-bit against 6-bit. Part of the gain comes from them.
- Several things differ from the reference quant at once (calibration data, bit allocation, expert Hessians, head,
  n-gram table, exllamav3 version), so the gain is not attributed to any one of them.
- Multi-turn instruction following (+3.4%) and Italian (+6.0%, 4 rows) are slightly worse than the reference; both
  intervals cross zero.
- Teacher-forced text, not the model's own samples; only 4 rows per language outside Russian.
- Perplexity on the multilingual text is 2.3385 here against 2.3443 for turboderp 4.05 (BF16: 2.3420); on the
  Nemotron trace 1.9275 against 1.9316 (BF16: 1.9285).

The previous version of this branch (calibration set 1, 2026-10-16) is revision
`524a19ebbbd653cd8cf31b766a56e7183133d7a0`; this one differs only in calibration data and is within noise of it
(-2.6% [-10.1%, +5.7%] Nemotron, -0.3% [-4.6%, +4.6%] multilingual).

## How it was made

Calibration data: [agents](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-agents),
[chat](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-chat),
[multimodal](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-multimodal) (ODbL-1.0). Evaluation traces
and results: [Vemloir/Qwen3.8-Flash-Next-evaluation](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-evaluation).
Code, experiment notes and the full bit allocation:
[Vemloir/qwen3.8-flash-next-sc-quant](https://github.com/Vemloir/qwen3.8-flash-next-sc-quant). The recipe and the
allocation map are also in this branch (`recipe.yaml`, `bit_allocation.png`, `bit_allocation.md`).

- **Calibration**: 2.15M tokens generated by the model itself (NVIDIA's FP4-Local checkpoint in vLLM, no output
  length cap, checked for truncation, loops and language slips): 17 agent sessions in Docker (Codex, OpenCode, pi
  harnesses, whole sessions up to 201k tokens), 222 chat conversations (science in 16 languages, everyday Russian and
  English, music; 25% Russian overall), 105 image prompts (charts, documents, frontend screenshots, photos) embedded
  through the vision tower, all at four reasoning levels (xhigh 40%, medium 20%, low 16%, off 24%).
- **Bit allocation**: the bit split between gate/up and down projections of the routed experts was measured per
  layer (every combination of 2-6 bits, expert output error on held-out tokens). Layers 0-9 put more bits into
  down (3/3/5 and 3.5/3.5/5), the rest stay 4/4/4, including the last five layers. Shared experts, attention k/v
  and the sparse-attention indexer are 8 bpw, GDN out_proj 7, other dense layers 6.
- **Affinity-weighted expert Hessians**: each expert is quantized against its own Hessian with calibration tokens
  weighted by `0.1 + p / mean(p)`, `p` the expert's router probability, instead of one unweighted Hessian shared by
  all 512 experts of a layer.

## n-gram table

`ngram_embedding-00001-of-00002.safetensors` and `-00002-of-00002` (52 GB together) are the per-layer n-gram
embedding (PLE) table, quantized to 8 bits from BF16 without calibration (exllamav3 `-ngb 8`); every result above
was measured with it. It is split in two only because Hugging Face caps files at 50 GB: exllamav3 finds the table
shards by tensor name across all files of the model, so nothing needs to be merged. The table is a lookup: TabbyAPI
streams the rows it needs from disk by default (`ngram_ram: false`), so it does not have to fit in RAM. The 6-bit
table from turboderp's 4.05 branch (39 GB) also works with this model if disk space matters, untested here.

## Running

On a single 16 GB GPU with the full 262,144-token context, offload 45 of the 48 expert layers to the CPU (TabbyAPI
`--cpu-moe-offload-layers 45`, ~56 GB of system RAM); with 44 the cache does not fit. Generation ran at ~33 tokens/s
on an RTX 5070 Ti with a Ryzen 9 9950X. With exllamav3 >= 1.6, do not set `EXL3_GR_MIX_TILED=0`: it keeps extra
copies of the hyper-connection tables in VRAM.

## License

Qwen Community License 1.0, inherited from the base model (see `LICENSE`).
