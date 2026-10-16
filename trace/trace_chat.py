# Chat part of the calibration trace: sc_trace's 157 seed conversations plus RU/EN dialogs and
# MiMo music prompts, sampled from the local EXL3 model. Same CLI as sc_trace.py.
import sys, os, json, argparse

TREE = os.path.expanduser("~/lm-inference/exllamav3-1.5.3-patched")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TREE)

import sc_trace
from exllamav3 import model_init

extra = json.load(open(os.path.join(HERE, "extra_conversations.json")))
music = json.load(open(os.path.join(HERE, "music_prompts.json")))
base = len(sc_trace.CONVERSATIONS)
sc_trace.CONVERSATIONS.extend((seed, fu) for seed, fu in extra)
sc_trace.CONVERSATIONS.extend((m["prompt"], []) for m in music)
print(f" -- conversations: {base} sc_trace + {len(extra)} RU/EN + {len(music)} music = {len(sc_trace.CONVERSATIONS)}")

# --only i,j,...: keep just these conversations (indices into the full list above), e.g. to sample
# the multi-turn ones in another reasoning mode
if "--only" in sys.argv:
    k = sys.argv.index("--only")
    only = [int(x) for x in sys.argv[k + 1].split(",")]
    del sys.argv[k:k + 2]
    sc_trace.CONVERSATIONS[:] = [sc_trace.CONVERSATIONS[i] for i in only]
    print(f" -- kept {len(only)} conversations: {only}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev = False)
    model_init.add_args(parser, default_cache_size = 131072, add_draft_model_args = True, default_autosplit_max_batch_size = 32)
    parser.add_argument("-o", "--output", type = str, required = True)
    parser.add_argument("-co", "--cal_out", type = str, default = None)
    parser.add_argument("-cr", "--cal_rows", type = int, default = 250)
    parser.add_argument("-cc", "--cal_cols", type = int, default = 2048)
    parser.add_argument("--target_tokens", type = int, default = None)
    parser.add_argument("--max_new_tokens", type = int, default = 3072)
    parser.add_argument("--max_epochs", type = int, default = 4)
    parser.add_argument("--seed", type = int, default = 0)
    parser.add_argument("-tv", "--template_vars", type = json.loads, default = {})
    sc_trace.main(parser.parse_args())
