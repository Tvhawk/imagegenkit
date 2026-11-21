"""Builds catalog/local.json (On this Mac) with exact file sizes and sha256 from the Hugging Face API.

    python3 catalog/build_local.py catalog/local.json

Edit ENGINE (a stable-diffusion.cpp release: macOS arm64 zip, its sha256 and size), COMPONENTS (shared text encoders
and VAEs), RECIPES (model families, from the engine's examples/server/recipes at that release) or MODELS (the examples
in the app's dropdown), then run it. Apps and the CLI pick up the new file from GitHub within a day.
"""
import datetime as dt
import json
import sys
import urllib.request

OUT = sys.argv[1] if len(sys.argv) > 1 else "catalog/local.json"
_trees = {}


def tree(repo):
    if repo not in _trees:
        url = f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true"
        _trees[repo] = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "imagegenkit"})))
    return _trees[repo]


def f(repo, path):
    e = next(x for x in tree(repo) if x.get("path") == path)
    return {"repo": repo, "path": path, "size": e["size"], "sha256": e["lfs"]["oid"]}


ENGINE = {
    "name": "stable-diffusion.cpp",
    "version": "master-1106-532d2c1",
    "url": "https://github.com/leejet/stable-diffusion.cpp/releases/download/master-1106-532d2c1/"
           "sd-master-532d2c1-bin-Darwin-macOS-26.7.2-arm64.zip",
    "sha256": "8146d6ffc75f8ff8f64b62a80fffee8fb714ab65e368e202084135fa07cc6f36",
    "size": 38331254,
    "binary": "sd-cli",
    "license": "MIT",
    "homepage": "https://github.com/leejet/stable-diffusion.cpp",
}

COMPONENTS = {
    "qwen3-4b": f("unsloth/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf"),
    "qwen3-4b-instruct": f("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
    "qwen3-8b": f("unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf"),
    "qwen3-vl-4b": f("Qwen/Qwen3-VL-4B-Instruct-GGUF", "Qwen3VL-4B-Instruct-Q4_K_M.gguf"),
    "qwen25-vl-7b": f("mradermacher/Qwen2.5-VL-7B-Instruct-GGUF", "Qwen2.5-VL-7B-Instruct.Q4_K_M.gguf"),
    "qwen25-vl-7b-vision": f("mradermacher/Qwen2.5-VL-7B-Instruct-GGUF", "Qwen2.5-VL-7B-Instruct.mmproj-Q8_0.gguf"),
    "flux2-vae": f("Comfy-Org/flux2-klein-4B", "split_files/vae/flux2-vae.safetensors"),
    "flux-ae": f("Comfy-Org/z_image_turbo", "split_files/vae/ae.safetensors"),
    "qwen-image-vae": f("Comfy-Org/Qwen-Image_ComfyUI", "split_files/vae/qwen_image_vae.safetensors"),
    "clip-l": f("comfyanonymous/flux_text_encoders", "clip_l.safetensors"),
    "t5xxl": f("comfyanonymous/flux_text_encoders", "t5xxl_fp8_e4m3fn.safetensors"),
}

# Model families the engine runs, from its server recipes (examples/server/recipes at the pinned commit):
# which slot holds the downloaded weights, which shared files fill the other slots, and the defaults.
# "base" lists Hugging Face repos a search result's base_model must point to (directly or through fine-tunes).
RECIPES = {
    "flux2-klein-4b": {"name": "FLUX.2 klein 4B", "base": ["black-forest-labs/FLUX.2-klein-4B"],
                       "slots": {"llm": "qwen3-4b", "vae": "flux2-vae"}, "edit": True, "multiple": 16,
                       "defaults": {"steps": 4, "cfg": 1, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux2-klein-base-4b": {"name": "FLUX.2 klein base 4B", "base": ["black-forest-labs/FLUX.2-klein-base-4B"],
                            "slots": {"llm": "qwen3-4b", "vae": "flux2-vae"}, "edit": True, "multiple": 16,
                            "defaults": {"steps": 20, "cfg": 4, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux2-klein-9b": {"name": "FLUX.2 klein 9B", "base": ["black-forest-labs/FLUX.2-klein-9B"],
                       "slots": {"llm": "qwen3-8b", "vae": "flux2-vae"}, "edit": True, "multiple": 16,
                       "defaults": {"steps": 4, "cfg": 1, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux2-klein-base-9b": {"name": "FLUX.2 klein base 9B", "base": ["black-forest-labs/FLUX.2-klein-base-9B"],
                            "slots": {"llm": "qwen3-8b", "vae": "flux2-vae"}, "edit": True, "multiple": 16,
                            "defaults": {"steps": 20, "cfg": 4, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux3-klein": {"name": "FLUX.3 klein", "base": ["black-forest-labs/flux-3-image-klein"],
                    "slots": {"llm": "qwen3-vl-4b", "vae": "flux2-vae"}, "edit": True, "multiple": 16,
                    "defaults": {"steps": 16, "cfg": 1, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux3-klein-base": {"name": "FLUX.3 klein base", "base": ["black-forest-labs/flux-3-image-klein-base"],
                         "slots": {"llm": "qwen3-vl-4b", "vae": "flux2-vae"}, "edit": True, "multiple": 16,
                         "defaults": {"steps": 20, "cfg": 4, "sampler": "euler", "width": 1024, "height": 1024}},
    "z-image-turbo": {"name": "Z-Image Turbo", "base": ["Tongyi-MAI/Z-Image-Turbo"],
                      "slots": {"llm": "qwen3-4b-instruct", "vae": "flux-ae"}, "edit": False, "multiple": 16,
                      "defaults": {"steps": 9, "cfg": 1, "sampler": "euler", "width": 1024, "height": 1024}},
    "z-image": {"name": "Z-Image", "base": ["Tongyi-MAI/Z-Image"],
                "slots": {"llm": "qwen3-4b-instruct", "vae": "flux-ae"}, "edit": False, "multiple": 16,
                "defaults": {"steps": 20, "cfg": 4, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux1-schnell": {"name": "FLUX.1 schnell", "base": ["black-forest-labs/FLUX.1-schnell"],
                      "slots": {"clip_l": "clip-l", "t5xxl": "t5xxl", "vae": "flux-ae"}, "edit": False, "multiple": 16,
                      "defaults": {"steps": 4, "cfg": 1, "sampler": "euler", "width": 1024, "height": 1024}},
    "flux1-dev": {"name": "FLUX.1 dev", "base": ["black-forest-labs/FLUX.1-dev"],
                  "slots": {"clip_l": "clip-l", "t5xxl": "t5xxl", "vae": "flux-ae"}, "edit": False, "multiple": 16,
                  "defaults": {"steps": 20, "cfg": 1, "guidance": 3.5, "sampler": "euler", "width": 1024, "height": 1024}},
    "chroma": {"name": "Chroma", "base": ["lodestones/Chroma1-HD", "lodestones/Chroma"],
               "slots": {"t5xxl": "t5xxl", "vae": "flux-ae"}, "edit": False, "multiple": 16,
               "defaults": {"steps": 20, "cfg": 3, "sampler": "euler", "width": 1024, "height": 1024}},
    "qwen-image": {"name": "Qwen-Image", "base": ["Qwen/Qwen-Image", "Qwen/Qwen-Image-2512"],
                   "slots": {"llm": "qwen25-vl-7b", "vae": "qwen-image-vae"}, "edit": False, "multiple": 16,
                   "defaults": {"steps": 20, "cfg": 4, "flow_shift": 3, "sampler": "euler", "width": 1024, "height": 1024}},
    "qwen-image-edit": {"name": "Qwen-Image Edit", "base": ["Qwen/Qwen-Image-Edit", "Qwen/Qwen-Image-Edit-2509"],
                        "slots": {"llm": "qwen25-vl-7b", "llm_vision": "qwen25-vl-7b-vision", "vae": "qwen-image-vae"},
                        "edit": True, "multiple": 16,
                        "defaults": {"steps": 20, "cfg": 4, "flow_shift": 3, "sampler": "euler", "width": 1024, "height": 1024}},
    "qwen-image-edit-2511": {"name": "Qwen-Image Edit 2511", "base": ["Qwen/Qwen-Image-Edit-2511"],
                             "slots": {"llm": "qwen25-vl-7b", "llm_vision": "qwen25-vl-7b-vision", "vae": "qwen-image-vae"},
                             "edit": True, "multiple": 16, "model_args": "qwen_image_zero_cond_t=true",
                             "defaults": {"steps": 20, "cfg": 4, "flow_shift": 3, "sampler": "euler", "width": 1024, "height": 1024}},
    "sdxl": {"name": "Stable Diffusion XL", "main": "model",
             "base": ["stabilityai/stable-diffusion-xl-base-1.0"], "slots": {}, "edit": False, "multiple": 64,
             "defaults": {"steps": 20, "cfg": 5, "sampler": "euler_a", "width": 1024, "height": 1024}},
    "sdxl-turbo": {"name": "SDXL Turbo", "main": "model", "base": ["stabilityai/sdxl-turbo"], "slots": {},
                   "edit": False, "multiple": 64,
                   "defaults": {"steps": 1, "cfg": 1, "sampler": "euler_a", "width": 512, "height": 512}},
    "sd1": {"name": "Stable Diffusion 1.5", "main": "model",
            "base": ["stable-diffusion-v1-5/stable-diffusion-v1-5", "runwayml/stable-diffusion-v1-5", "CompVis/stable-diffusion-v1-4"],
            "slots": {}, "edit": False, "multiple": 64,
            "defaults": {"steps": 20, "cfg": 7.5, "sampler": "euler_a", "width": 512, "height": 512}},
}

MODELS = [
    {"id": "flux2-klein-4b", "name": "FLUX.2 klein 4B", "recipe": "flux2-klein-4b",
     "note": "Fast. Makes and edits images.", "license": "Apache 2.0", "ram": 16,
     "file": f("leejet/FLUX.2-klein-4B-GGUF", "flux-2-klein-4b-Q4_0.gguf")},
    {"id": "z-image-turbo", "name": "Z-Image Turbo", "recipe": "z-image-turbo",
     "note": "Best photos and text in images.", "license": "Apache 2.0", "ram": 16,
     "file": f("leejet/Z-Image-Turbo-GGUF", "z_image_turbo-Q4_K.gguf")},
    {"id": "flux1-schnell", "name": "FLUX.1 schnell", "recipe": "flux1-schnell",
     "note": "The classic FLUX look. Large download.", "license": "Apache 2.0", "ram": 24,
     "file": f("leejet/FLUX.1-schnell-gguf", "flux1-schnell-q4_k.gguf")},
    {"id": "qwen-image-edit-2511", "name": "Qwen-Image Edit", "recipe": "qwen-image-edit-2511",
     "note": "Strongest edits. Large and slow.", "license": "Apache 2.0", "ram": 32,
     "file": f("unsloth/Qwen-Image-Edit-2511-GGUF", "qwen-image-edit-2511-Q3_K_M.gguf")},
]

for m in MODELS:
    r = RECIPES[m["recipe"]]
    m["edit"] = r["edit"]
    m["download"] = m["file"]["size"] + sum(COMPONENTS[c]["size"] for c in r["slots"].values())

catalog = {"version": 1, "updated": dt.datetime.now().isoformat(timespec="minutes"), "engine": ENGINE, "components": COMPONENTS,
           "recipes": RECIPES, "models": MODELS}
with open(OUT, "w") as fh:
    json.dump(catalog, fh, indent=1)
    fh.write("\n")
for m in MODELS:
    print(f"{m['name']}: {m['download'] / 1e9:.2f} GB")
