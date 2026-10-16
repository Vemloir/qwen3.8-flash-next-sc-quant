# Qwen3.8-Flash-Next self-calibrated EXL3

Code, recipe and notes behind the self-calibrated EXL3 4.05 bpw quant of
[Qwen/Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next):
[Vemloir/Qwen3.8-Flash-Next-sc-exl3](https://huggingface.co/Vemloir/Qwen3.8-Flash-Next-sc-exl3) (branch
`4.05bpw_sc_h8_ng8`). On two evaluation traces with no overlap with calibration it is 12.4% and 16.9% closer to BF16 (KL
divergence) than turboderp's 4.05 bpw quant of the same size.

- [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md): what was measured and decided, with numbers
- [docs/CALIBRATION.md](docs/CALIBRATION.md): how the calibration data was built and why
- [docs/bit_allocation.md](docs/bit_allocation.md): bits per layer and tensor group ([image](docs/bit_allocation.png))
- Data on Hugging Face: calibration ([agents](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-agents),
  [chat](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-chat),
  [multimodal](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-calib-multimodal); ODbL-1.0) and
  [evaluation](https://huggingface.co/datasets/Vemloir/Qwen3.8-Flash-Next-evaluation)

![Bit allocation](docs/bit_allocation.png)

## Requirements

- exllamav3 1.6.1 with the patches in `patches/exllamav3-1.6.1/` (`git am`): image rows in calibration, low-disk
  conversion, chunked calibration rows, affinity-weighted expert Hessians, qbench extensions, a `sc_measure` fix.
  The quant itself runs on stock exllamav3 >= 1.6.1.
- TabbyAPI serving the model for trace generation; Docker for the agent harnesses; headless Chromium for the
  frontend screenshots.
- The BF16 checkpoint (~340 GB) and ~110 GB of free disk for the conversion.

Scripts assume this layout under `~/lm-inference`: `models/Qwen3.8-Flash-Next/{BF16,...}`, `sc-quant/` (this
repository), `exllamav3-1.6.1-sc/` (patched exllamav3), `tabbyAPI/`, `datasets/MiMo-V2.6-RL-oss/`.

## Reproducing

1. **Chat traces**, per reasoning level: `trace/trace_chat.py` (driver for exllamav3 `sc_trace.py` with extra
   prompts), wrapped by `gen_mixed.sh`.
2. **Agent sessions**: `agent-env/select_tasks.py` picks MiMo tasks; `agent-env/run_agents.py` runs the Codex,
   OpenCode and pi harnesses in Docker against TabbyAPI through `agent-env/trace_proxy.py`, which sets the reasoning
   level per session and logs every request; `agent-env/build_rows.py` turns the logs into token rows
   (`agents_mixed.sh`, `gap_chain.sh`).
3. **Image rows**: `trace/gen_pages.py` (pages and screenshots), `trace/make_image_items.py`,
   `trace/trace_images.py` (`night_img.sh` runs the chain).
4. **Packing**: `pack_v2.py` builds the ~1M-token calibration file with the reasoning-level budgets;
   `split_calibration.py` splits it into the published datasets.
5. **Bit allocation**: `tools/split_grid.py` (per-layer expert error for every bit split),
   `tools/alloc_grid.py` (allocation under a budget), then the recipe `calib/recipe_v3.yaml`
   (`tools/bit_map.py` draws it).
6. **Conversion**: `convert_v3.sh` (BF16 source, recipe, calibration, `--moe_affinity_eps 0.1`, low-disk mode,
   checkpoints every 30 minutes; ~8 hours on an RTX 5070 Ti with 61 GB of RAM).
7. **Evaluation**: `nemotron_eval_trace.py` and `multilingual_eval_trace.py` build the independent traces;
   `eval/qbench_nemotron.yaml` and `eval/qbench_multilingual.yaml` run exllamav3 qbench against BF16.

Experiment tools in `tools/`: `moe_hessian_test.py` (affinity Hessian prototype), `layer_sens.py`, `head_bits_test.py`,
and others. The `night_*.sh` and `*_chain.sh` scripts are the job chains actually run during development; they
document the order of steps and the memory limits used (`capped.sh`).

## License

MIT for the code in this repository. The data is licensed separately (ODbL-1.0 for the calibration datasets); the
model weights follow the Qwen Community License 1.0.
