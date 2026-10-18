"""
Runs ON a Colab CPU VM with Google Drive mounted: publish a converted quant from Drive to a branch of a Hugging Face
model repo in one commit, without copying it to the VM disk first (Colab's link to the Hub is much faster than a
home uplink). Files are linked into a staging folder next to extra local files (README) and uploaded with
upload_folder; afterwards the LFS sha256 of every uploaded file is printed for comparison with a local copy.

    HF_TOKEN=$(cat /content/.hf_token) python drive_to_hf.py --src /content/drive/MyDrive/sc-quant/SC2-EXL3-4.05 \
        --repo Vemloir/Qwen3.8-Flash-Next-sc-exl3 --revision 4.05bpw_sc_h8_ng8 \
        --files "model-*.safetensors,config.json,quantization_config.json" --extra /content/README.md \
        -m "Recalibrate with calibration set 2"
"""
import argparse, fnmatch, os, shutil, time


def main(args):
    from huggingface_hub import HfApi
    api = HfApi()
    stage = "/content/stage"
    shutil.rmtree(stage, ignore_errors = True)
    os.makedirs(stage)
    pats = args.files.split(",")
    names = sorted(f for f in os.listdir(args.src) if any(fnmatch.fnmatch(f, p) for p in pats))
    for f in names:
        os.symlink(os.path.join(args.src, f), os.path.join(stage, f))
    for f in args.extra:
        shutil.copy(f, stage)
    total = sum(os.path.getsize(os.path.join(stage, f)) for f in os.listdir(stage))
    print(f"{time.strftime('%T')} uploading {len(os.listdir(stage))} files, {total / 1e9:.1f} GB: {sorted(os.listdir(stage))}",
          flush = True)
    t0 = time.time()
    info = api.upload_folder(folder_path = stage, repo_id = args.repo, revision = args.revision,
                             commit_message = args.message, commit_description = args.description or None)
    print(f"{time.strftime('%T')} committed in {time.time() - t0:.0f} s: {info.oid}", flush = True)
    for e in api.list_repo_tree(args.repo, revision = info.oid):
        if e.path in os.listdir(stage):
            sha = e.lfs.sha256 if getattr(e, "lfs", None) else "(not LFS)"
            print(f"remote {sha} {e.size} {e.path}")
    print("UPLOAD DONE", flush = True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required = True)
    ap.add_argument("--repo", required = True)
    ap.add_argument("--revision", required = True)
    ap.add_argument("--files", required = True, help = "comma-separated glob patterns of files in --src")
    ap.add_argument("--extra", nargs = "*", default = [], help = "local files added to the same commit")
    ap.add_argument("-m", "--message", required = True)
    ap.add_argument("--description", default = "")
    main(ap.parse_args())
