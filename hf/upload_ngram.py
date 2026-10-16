from huggingface_hub import HfApi
HfApi().upload_large_folder(
    repo_id = "Vemloir/Qwen3.8-Flash-Next-sc-exl3", repo_type = "model", revision = "4.05bpw_sc_h8_ng8",
    folder_path = "/home/daniil/lm-inference/models/Qwen3.8-Flash-Next/SC-EXL3-4.05-v3",
    allow_patterns = ["ngram_embedding.safetensors"],
    num_workers = 4,
)
print("upload done")
