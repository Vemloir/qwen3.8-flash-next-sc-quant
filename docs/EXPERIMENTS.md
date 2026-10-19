# Experiments and decisions

How the SC 4.05 bpw quant of Qwen3.8-Flash-Next was arrived at: what was measured, what changed the recipe, what did
not work. KL divergence (KLD) is always against the BF16 model, measured with exllamav3 qbench; lower is better.
The reference is turboderp's 4.05 bpw EXL3 quant (`4.05bpw_h6_ng6`).

Model facts that matter below: 48 layers, every fourth (3, 7, ..., 47) full attention with a sparse-attention
indexer, the rest Gated DeltaNet; MoE with 512 routed experts per layer (top-10) of intermediate size 640, plus a
shared expert; hidden size 2560; 12 per-layer n-gram embedding (PLE) layers with a 51.8B-parameter lookup table;
hyper-connection residual streams.

## 1. Source checkpoint: BF16, not FP8

The first quant was converted from the official FP8 checkpoint. FP8 itself is 0.00258 KLD from BF16. With the same
recipe and calibration, converting from BF16 gave 0.00624 KLD on turboderp's qbench trace against 0.00670 from FP8
(-7%). All later quants are converted from BF16.

## 2. Calibration v1 to v2

v1 was generated at xhigh reasoning only. v2 mixes four reasoning levels and adds images (see CALIBRATION.md).
On turboderp's trace v2 reached 0.00600 at 4.00 bpw against 0.00593 for turboderp's 4.05 bpw (equal perplexity,
1.3896; median 0.000291 against 0.000306). On held-out rows of the calibration pool it was -8.7% (95% interval
-20% to +0.5%), and on 36 held-out agent sessions -13.7% (-18.8% to -7.9%), lower on 28 of 36. Those held-out
traces share the task pool with calibration, so the final evaluation (section 9) uses independent data.

## 3. Output head: 8 bits

The head's own error, with its input taken from BF16 (`tools/head_bits_test.py`):

| Head bits | KLD of logits | Top-1 agreement |
|---|---|---|
| 5 | 4.9e-4 | |
| 6 | 1.38e-4 | 99.58% |
| 7 | 4.1e-5 | |
| 8 | 1.24e-5 | 99.89% |

8 bits cost ~150 MB over 6 and cut the head's contribution about tenfold. The head is outside the bits-per-weight
budget, as in every exllamav3 quant.

## 4. Affinity-weighted expert Hessians

Conversion runs every expert on every calibration token, so all 512 experts of a layer share one gate/up Hessian
`sum_t x_t x_t^T` and each down Hessian counts every token equally, although an expert only ever sees the tokens
routed to it. Weighting each token by the expert's router affinity gives each expert its own Hessian:
`H_e ~ sum_t (eps + p_te / mean_t p_te) x_t x_t^T`, `p` the full router softmax.

Prototype (`tools/moe_hessian_test.py`, 48 experts per layer, expert output error on held-out tokens, weighted by
routing): soft weighting with eps = 0.1 gave -11.0% (layer 4), -5.3% (layer 24), -6.7% (layer 44). Weighting by the
top-k routing weights only was unstable (from -11% to +8%), so the soft form was kept. Over all 48 layers
(`tools/layer_sens.py`, 64 experts, 50k calibration tokens) it helped in 47 of 48 (up to -17%, most at the ends of
the model; layer 23 unchanged).

In the converter (`--moe_affinity_eps 0.1`, patch 0003) down Hessians accumulate the weighted sum during the capture
pass; gate/up Hessians (512 x 2560^2 fp32 = 13 GB per layer, too much to keep) are built from a sample of 262k rows a
few experts at a time just before quantization. Upstream exllamav3's YAQA Hessians do not cover fused MoE experts, so
the two do not overlap.

## 5. Bit split inside the experts

Two independent quants put more bits into the experts' down projection than into gate/up: Unsloth's UD GGUFs (down
at Q5_1/Q8_0 against Q4_K gate/up in UD-Q4_K_XL) and ISTA-DASLab's GSQ-RCO GGUFs (down at 4.5 bits in 39 of 48
layers against ~2.9 for gate/up at 3.5 bpw). Both also protect the last layers.

Measured directly (`tools/split_grid.py`): for every layer, 64 experts with each projection quantized at every K in
{2, 2.5, 3, 3.5, 4, 5, 6}, and the expert output error for all 343 gate/up/down combinations. At 4.0 bits average:

| Layers | Best split | Error against 4/4/4 |
|---|---|---|
| 0 | 3.5/3.5/5 (and 3/3/5 at 3.67 bits: -24%) | -52% |
| 1-9, 15 | 3.5/3.5/5 | -3 to -33% |
| 10-14, 16-47 | 4/4/4 | 0 (no split beats it) |

Adding bits anywhere, the best use is always a wider down projection (4/4/5, or 3.5/3.5/6 in layer 0: -31 to -69%). Gate and up must have the
same width: the decode kernel (`exl3_moe_coop`) rejects anything else at load time.

`tools/alloc_grid.py` solves the allocation over all layers exactly (dynamic programming over half-bit units). At the
budget of the earlier 4.00 recipe it moved layers 43-47 back to 4 bits and took the bits from layers 8, 10, 11, 13-15 and 23,
for -7% summed expert error. Cutting the middle layers on a local metric alone was not convincing, so the budget was
raised to turboderp's size instead (4.05 bpw) with no layer cut. The remaining budget went to dense tensors: shared
experts, attention k/v and the indexer at 8 bits, every GDN out_proj at 7. Map: `docs/bit_allocation.md`.

Output channel scales for the experts' gate/up (since 1.5.4 exllamav3 decides per tensor by default and may leave
them off): turning them off had no effect on this model (within +/-1.5% in every layer, slightly worse in the last ones), so they stay on.

## 6. Layer sensitivity: a measurement that did not work

The 4.00 recipe had cut layers 43-47 to 3.5 bits on the strength of a noise-injection measurement
(`sc_measure.py`). That measurement turned out to run a different model: `sc_measure` passed no `input_ids` to the
forward passes, and the 12 PLE layers fall back to an all-zero token history without them (fixed, patch 0004). After
the fix, swapping one tensor group at a time for its 4.05 bpw version still gave ~0.03-0.07 KLD for every group,
including 6-bit ones, while the whole 4.05 model is ~0.006: a floor that swamps the per-group signal, most likely
from discrete choices in this model (expert routing, the sparse-attention indexer) flipping under any change. No
usable per-layer importance came out of it; the cut of layers 43-47 had no valid basis and was reverted.

## 7. Calibration data for the next round: options considered

- An API provider serving the full model would cost ~$5 for the same volume with prompt caching (OpenRouter prices)
  and gives traces from FP8/BF16 rather than a quant.
- NVIDIA's NVFP4 quant on a single 96 GB GPU would fit (n-gram table in host RAM) but, on GLM-5.3-Flash at least,
  turboderp's plots put NVFP4 further from BF16 than EXL3 4.05 bpw (0.0452 against 0.0346 KLD), so its traces would
  not be closer to the full model than the current ones.

## 8. Runtime

- exllamav3 >= 1.6: do not set `EXL3_GR_MIX_TILED=0` (it used to save VRAM with a local patch on 1.5.3; on 1.6 it
  keeps extra fp16 copies of the hyper-connection tables in VRAM and the model does not fit).
- On a 16 GB GPU with the full 262k context, 45 of 48 expert layers go to the CPU (44 sufficed for the 4.00 quant):
  the GPU-resident last layers are 4 bits instead of 3.5 and the head is 8 bits instead of 6, +0.86 GB of VRAM.
  ~14.0 GB VRAM and ~56 GB RAM in use; ~33 tokens/s generation on an RTX 5070 Ti with a Ryzen 9 9950X.

## 9. Evaluation

Two traces with no overlap with calibration, teacher-forced on assistant turns, with paired-bootstrap 95% intervals
over rows (`nemotron_eval_trace.py`, `multilingual_eval_trace.py`):

| Trace | SC 4.05 | turboderp 4.05 | Difference |
|---|---|---|---|
| Nemotron 3.5 Super post-training conversations (agents, SWE, chat, instruction following, math proofs), 48 rows | 0.00796 | 0.00909 | -12.4% [-18.6%, -6.2%], 38/48 rows |
| Multilingual (16 Russian, 36 in 9 other languages), 52 rows | 0.00544 | 0.00655 | -16.9% [-24.6%, -10.1%], 41/52 rows |

These are plain qbench numbers, with the positions the chat template writes counted in; section 10 leaves those out.

Per source: math proofs -30%, SWE -18%, knowledge agent -15%, chat -13%, tool calling -7%, multi-turn instruction
following 0%; Russian -21%, Korean/Portuguese/Hindi -19 to -27%, Japanese/Spanish -1%. Several things differ from
the reference at once (calibration, bit allocation, Hessians, head, exllamav3 version), so the gain is not
attributed to any one of them. qbench needed two changes for these traces (patch 0005): `crop_logits` (the head
only sees positions from the first scored token on; a 16k-token agent row would otherwise need 8 GB of logits) and
dropping a redundant device copy of each row's state.

## 10. Calibration set 2

Set 1 had defects found by reading it: reasoning cut off at the 3,072-token per-turn cap, a few loops (IPv6, chess,
ABC notation), history turns holding English reasoning instead of the answer, a Spanish answer with calls to
non-existent tools, agent sessions from only 7 tasks truncated at 25k tokens. Set 2 regenerates everything
(`regen/`): NVIDIA's FP4-Local checkpoint in vLLM on a Colab G4, no output length cap, 40 fresh MiMo tasks for the
agents, science prompts in 16 languages, `qa.py` checks. 5.0M tokens generated, 2.15M packed (17 whole agent sessions
up to 201k tokens, 222 chats, 105 images; 25% Russian; xhigh/medium/low/off 40/20/16/24%).

Conversion: same recipe and flags as the published quant, only the calibration changed. On an A100 40 GB the
hyper-connection expansion ran out of RAM (2.15M tokens need ~85 GB of state); on an A100 80 GB a layer took
~1,300-1,400 s, bound by FP32 expert quantization; on the G4 ~560-635 s, then ~460-560 s with patch 0007 (a
capture-only fast path for block-sparse layers, x1.6 on the capture pass, bitwise identical Hessians). Checkpoints
(88 GB of state at ~70 MB/s) cost ~17-38 minutes each and were cut to one per 6 hours. The published quant was converted
with layers 0-5 on the plain capture path and layers 6-47 with patch 0007 and `EXL3_CAPTURE_TF32=1` (resumed from
the layer 5 checkpoint); exact reproduction needs the same split (`CPI=21600 EXL3_CAPTURE_TF32=1` after layer 5).

| Trace | Set 2 | Set 1 | turboderp 4.05 | Set 2 vs set 1 | Set 2 vs turboderp |
|---|---|---|---|---|---|
| Nemotron, 48 rows | 0.00767 | 0.00788 | 0.00903 | -2.6% [-10.1%, +5.7%], 23/48 rows | -15.1% [-22.4%, -7.3%], 39/48 rows |
| Multilingual, 52 rows | 0.00543 | 0.00545 | 0.00656 | -0.3% [-4.6%, +4.6%], 29/52 rows | -17.2% [-25.5%, -9.5%], 42/52 rows |

The two calibration sets give the same quality within noise on these traces: the defects of set 1 were a small share
of its tokens, and at a fixed bit budget the Hessians barely moved. Per source the picture shifted somewhat
(French from -9.9% to -28.6% against turboderp, Italian from -14.2% to +6.0% on the strength of one row), all within
the wide per-source intervals. The traces reach 16k tokens at most, so the long agent sessions of set 2 are not
tested by them. Set 2 is published anyway: same quality, clean calibration data that matches the published datasets.

The numbers above leave out the positions the chat template writes itself (`<think>\n`, or the empty think block
with reasoning off): 166 of 22,976 scored tokens on the Nemotron trace, 32 of 25,574 on the multilingual one. With
them, as plain qbench scores, multi-turn instruction following came out +13.4% against turboderp, almost all of it
one such token in one row (the newline after `<think>`, KL 2.43); our quants are less sure of these tokens than
turboderp's (4.6 against 2.8 summed KL on the Nemotron trace), which does not matter when the template writes them.
Plain qbench gives -14.0% and -17.2% against turboderp; `tools/kld_results.py --keep_template_tokens` reproduces it.

## 11. Ablation of the expert Hessian scheme (exllamav3 PR #529)

Asked for in review: the published comparison changes too much at once (turboderp's 4.05 bpw quant was made with
exllamav3 1.4.4, before post-LDLQ refit, LDLQ drift compensation and other converter changes), so affinity-weighted
Hessians were tested in isolation. Gemma 4 26B A4B (128 experts, top-8), current dev plus the PR, 3.0 bpw with `-hq`,
built-in calibration (250 x 2048), only the expert Hessian scheme varying; KL divergence against BF16 on 30 x 2048
tokens of openwebtext10k framed with the chat template (BOS), 95% paired bootstrap over rows. Run on a Colab G4.

| Variant | KLD | Against A |
|---|---|---|
| A: every expert sees every calibration token (default) | 0.05613 | |
| B: A + `-mae 0.1` | 0.05411 | -3.6% [-6.2%, -0.8%], lower on 20/30 rows |
| C: `calibration_all_experts` off | fails | an expert with no routed token has no Hessian (`KeyError: block.moe.42.down`) |
| D: every expert active on 10% of rows | 0.05641 | +0.5% [-2.5%, +3.5%] |

The affinity weighting helps a little and consistently (B against D: -4.1% [-7.3%, -0.6%]) for 40 instead of 28
minutes of conversion; the gain against turboderp's quant on Qwen3.8-Flash-Next (15-17%) mostly comes from other
differences. One model and one bitrate. The run also found that the PR's `-mae` was required by mistake (conversion
without it failed); fixed before review. Quants and logs: Drive `sc-quant/ablation-gemma4/`.
