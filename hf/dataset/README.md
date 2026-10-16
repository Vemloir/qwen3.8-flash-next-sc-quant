---
license: other
license_name: mixed
pretty_name: Qwen3.8-Flash-Next quantization evaluation data
tags:
- exl3
- exllamav3
- quantization
- evaluation
- kl-divergence
---

# Qwen3.8-Flash-Next: quantization evaluation data

Evaluation traces and results for the self-calibrated EXL3 quant of Qwen3.8-Flash-Next
([Vemloir/Qwen3.8-Flash-Next-sc-exl3](https://huggingface.co/Vemloir/Qwen3.8-Flash-Next-sc-exl3), branch
`4.05bpw_sc_h8_ng8`), measured against BF16 with exllamav3 qbench. The calibration data is in three separate datasets:
[agents](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-agents),
[chat](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-chat),
[multimodal](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-multimodal).

## eval_traces/

qbench `test_trace` files (`rows` with `input_ids`, `response_ids`, optional `score_spans`).

| File | Content | Overlap with calibration |
|---|---|---|
| `nemotron_trace.json` | 48 conversations from NVIDIA's Nemotron 3.5 Super post-training data (Nemotron-SFT-Agentic-v4, Nemotron-SFT-SWE-v3.5, Nemotron-SFT-Instruction-Following-Chat-v4, Nemotron-Math-Proofs-v3-SFT), teacher-forced, scored on assistant turns | none |
| `multilingual_trace.json` | 16 Russian conversations (RefalMachine/GrandMaster-PRO-MAX-Qwen3-synthetic) and 36 in de, es, fr, it, zh, ja, ko, pt, hi (nvidia/Nemotron-SFT-Multilingual-v1/v2, translated answers only) | none |
| `heldout_trace.json` | held-out rows of the calibration pool (chat and agent steps not used for calibration) | same source pool |
| `agent_trace.json` | held-out agent sessions (same task pool) | same source pool |
| `qbench_prompts_turboderp.json` | turboderp's qbench trace for this model | none |

The two independent traces are built by `nemotron_eval_trace.py` and `multilingual_eval_trace.py` (sampled with HTTP
range requests from the source datasets, rendered with the model's chat template).

## results/

- `qb_nemotron_results.json`, `qb_multilingual_results.json` and plots: qbench against BF16 for this quant and
  turboderp's 4.05 bpw.
- `per_token_kld.safetensors`: per-token KL divergence on every scored token of both independent traces
  (`nemotron.sc_4.05`, `nemotron.turboderp_4.05`, `multilingual.sc_4.05`, `multilingual.turboderp_4.05`), in
  trace order; the source of the histograms in the model card.
- `kld_by_source.json`: mean KL divergence per source/language, relative difference and 95% paired-bootstrap
  intervals over rows.
- `split_grid.json`: per layer, the routed experts' output error (relative to the BF16 experts, on held-out tokens)
  for every gate/up/down bit combination of {2, 2.5, 3, 3.5, 4, 5, 6}, with affinity-weighted Hessians; the basis
  of the expert bit split.
- `layer_sens.json`: the same metric for seven fixed variants per layer (bit splits, output scales, Hessian
  weighting).

## Licenses

Each file keeps the terms of its sources:

| File | License |
|---|---|
| `nemotron_trace.json` | CC-BY-4.0 and OpenMDW-1.1 (NVIDIA Nemotron post-training datasets) |
| `multilingual_trace.json` | CC-BY-SA-4.0 (Nemotron-SFT-Multilingual) and Apache-2.0 (GrandMaster-PRO-MAX, its Qwen3 synthetic answers) |
| `heldout_trace.json`, `agent_trace.json` | ODbL-1.0, like the calibration datasets they were held out from |
| `qbench_prompts_turboderp.json` | as published by turboderp with exllamav3 (MIT) |
| `results/` | ODbL-1.0 |
