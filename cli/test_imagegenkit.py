"""Unit tests for the imagegenkit CLI (stdlib only, no real providers).

    /usr/bin/python3 -m unittest discover -s cli
"""
import atexit
import base64
import importlib.util
import shutil
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="igk-test-")
atexit.register(shutil.rmtree, HOME, True)
os.environ["HOME"] = HOME  # the module reads ~/.config/imagegenkit at import
os.environ["IMAGEGENKIT_NO_UPDATE"] = "1"
_spec = importlib.util.spec_from_file_location("imagegenkit", Path(__file__).with_name("imagegenkit.py"))
igk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(igk)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Handler.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        mode = self.path.split("/")[1]
        raw = {"badjson": b"{nope", "html": b"<html>oops</html>"}.get(mode)
        if raw is None:
            raw = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"cost": 0.01}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def args(**kw):
    a = dict(prompt="a cat", prompt_file=None, out=None, image=None, n=1, model=None, aspect=None, resolution=None,
             size=None, quality=None, background=None, format=None, compression=None, moderation=None, mask=None,
             extra=None, overwrite=False, dry_run=False, provider=None)
    a.update(kw)
    return types.SimpleNamespace(**a)


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_openrouter_sends_the_api_key(self):
        os.environ["ZZ_OR_API_KEY"] = "or-test-key"
        spec = {"name": "zz-or", "label": "OR", "type": "openrouter", "env": "ZZ_OR_API_KEY",
                "base_url": f"{self.base}/ok/api/v1", "model": "m"}
        out = Path(HOME) / "or.png"
        igk.run_openrouter(args(out=str(out), quality="high"), igk.load_config(), spec, None)
        self.assertEqual(Handler.requests[-1]["auth"], "Bearer or-test-key")
        self.assertTrue(out.exists())

    def test_unreadable_answers_are_api_errors(self):
        for mode in ("badjson", "html"):
            with self.assertRaises(igk.ApiError) as e:
                igk.http("POST", f"{self.base}/{mode}/x", {}, b"{}")
            self.assertIn("unreadable", e.exception.message)

    def test_bad_base64_is_an_api_error(self):
        with self.assertRaises(igk.ApiError):
            igk.collect_images({"data": [{"b64_json": "!!!x"}]})

    def test_stop_paid_pauses_only_paid_providers(self):
        cfg = {"limits": {"monthly_images": 1, "monthly_cost": 100, "stop_paid": True}}
        igk.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        ts = igk.dt.datetime.now().isoformat(timespec="seconds")
        igk.LOG_FILE.write_text(json.dumps({"ts": ts, "provider": "grok", "type": "grok-build", "outputs": ["/x"]}) + "\n")
        try:
            igk.check_limits(cfg, {"type": "grok-build"})  # subscription keeps working
            with self.assertRaises(igk.LimitReached):
                igk.check_limits(cfg, {"type": "openai"})
            igk.check_limits({"limits": "bad"}, {"type": "openai"})  # odd config never crashes
        finally:
            igk.LOG_FILE.unlink()

    def test_catalog_providers_without_the_app(self):
        igk.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        repo_catalog = Path(__file__).resolve().parent.parent / "catalog" / "providers.json"
        (igk.CONFIG_DIR / "catalog.json").write_text(repo_catalog.read_text())
        try:
            provs = igk.all_providers(igk.load_config())
            for name in ("openrouter", "recraft", "together", "fal"):
                self.assertIn(name, provs)
            self.assertEqual(provs["recraft"]["size_style"], "aspect")
            self.assertEqual(provs["openai"]["type"], "openai")  # built-ins win
        finally:
            (igk.CONFIG_DIR / "catalog.json").unlink()

    def test_version_compare_never_downgrades(self):
        self.assertLess(igk.version_tuple("v1.0.0"), igk.version_tuple("1.0.5"))
        self.assertLess(igk.version_tuple("1.0.9"), igk.version_tuple("1.0.10"))


class FileHandler(BaseHTTPRequestHandler):
    """Serves FileHandler.files[path] with Range support (model downloads)."""
    files = {}
    ignore_range = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = FileHandler.files.get(self.path.split("?")[0])
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng and not FileHandler.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        self.wfile.write(data[start:])


class LocalModelTests(unittest.TestCase):
    """On this Mac: catalog, picking files, family matching, downloads, and a run with a fake engine."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FileHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.cat = igk.local_catalog()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_catalog_is_consistent(self):
        cat = self.cat
        self.assertTrue(cat["engine"]["url"].startswith("https://github.com/leejet/stable-diffusion.cpp/"))
        self.assertEqual(len(cat["engine"]["sha256"]), 64)
        for rid, r in cat["recipes"].items():
            for slot, cid in r.get("slots", {}).items():
                self.assertIn(cid, cat["components"], f"{rid}.{slot}")
            self.assertTrue(r["base"], rid)
        for m in cat["models"]:
            self.assertIn(m["recipe"], cat["recipes"])
            self.assertEqual(len(m["file"]["sha256"]), 64)
            self.assertGreater(m["download"], m["file"]["size"])

    def test_picks_a_4bit_weights_file(self):
        files = [{"path": p, "size": s} for p, s in [("model-Q8_0.gguf", 8), ("model-F16.gguf", 16), ("model-Q4_K_M.gguf", 4),
                                                      ("mmproj-Q8_0.gguf", 1), ("model-Q3_K_M.gguf", 3), ("README.md", 1)]]
        self.assertEqual(igk.pick_weights(files, {"main": "diffusion_model"})["path"], "model-Q4_K_M.gguf")
        self.assertEqual(igk.quant_label("flux-2-klein-4b-Q4_0.gguf"), "Q4_0")
        self.assertEqual(igk.quant_label("Qwen2.5-VL-7B-Instruct.Q4_K_M.gguf"), "Q4_K_M")
        single = [{"path": "sd_xl_turbo_1.0_fp16.safetensors", "size": 6.9e9}, {"path": "unet/x.safetensors", "size": 5e9},
                  {"path": "vae.safetensors", "size": 3e8}]
        self.assertEqual(igk.pick_weights(single, {"main": "model"})["path"], "sd_xl_turbo_1.0_fp16.safetensors")
        self.assertEqual(igk.weights_candidates(single, {"main": "diffusion_model"}), [])

    def test_matches_families_through_fine_tunes(self):
        cat = self.cat
        quant = {"id": "a/klein-gguf", "tags": ["gguf", "base_model:quantized:black-forest-labs/FLUX.2-klein-4B"]}
        self.assertEqual(igk.match_recipe(quant, cat)[0], "flux2-klein-4b")
        lora = {"id": "a/lora", "tags": ["base_model:adapter:black-forest-labs/FLUX.2-klein-4B"]}
        self.assertIsNone(igk.match_recipe(lora, cat)[0])
        real = igk.hf_get
        igk.hf_get = lambda path: {"cardData": {"base_model": "Tongyi-MAI/Z-Image-Turbo"}, "tags": []}
        try:
            tune = {"id": "a/tune-gguf", "cardData": {"base_model": ["someone/z-image-finetune"]}, "tags": []}
            self.assertEqual(igk.match_recipe(tune, cat, {})[0], "z-image-turbo")
        finally:
            igk.hf_get = real
        self.assertIsNone(igk.match_recipe({"id": "x/y", "tags": []}, cat)[0])

    def test_adult_models_are_hidden_in_search_only(self):
        cat = self.cat
        self.assertTrue(igk.local_blocked(cat, ["someone/Qwen-Image-Edit-Uncensored-GGUF"]))
        self.assertTrue(igk.local_blocked(cat, ["a/b"], ["gguf", "not-for-all-audiences"]))
        self.assertTrue(igk.local_blocked(cat, ["a/fine-tune", "x/z-image-turbo-nsfw-v2"]))  # through its base model
        self.assertFalse(igk.local_blocked(cat, ["leejet/FLUX.2-klein-4B-GGUF"], ["gguf", "text-to-image"]))
        self.assertTrue(igk.local_blocked(dict(cat, block={"repos": ["bad/repo"]}), ["bad/repo"]))
        real = igk.hf_get

        def fake(path):  # a tagged adult repo with no base_model: allowed as a link, family from its name
            if "/tree/" in path:
                return [{"type": "file", "path": "z-image-turbo-nsfw-Q4_K.gguf", "size": 4, "lfs": {"oid": "c" * 64}}]
            return {"id": "me/z-image-turbo-nsfw-GGUF", "tags": ["not-for-all-audiences"], "cardData": {}}
        igk.hf_get = fake
        try:
            self.assertEqual(igk.resolve_local("me/z-image-turbo-nsfw-GGUF", cat)["recipe"], "z-image-turbo")
        finally:
            igk.hf_get = real
        self.assertEqual(igk.resolve_local("https://example.com/nude-model.gguf", cat, recipe="sdxl")["recipe"], "sdxl")
        self.assertEqual(igk.recipe_from_name("lesliemore/z-image-turbo-nsfw-v2-GGUF", cat["recipes"]), "z-image-turbo")
        self.assertEqual(igk.recipe_from_name("x/FLUX.2-klein-9B-True-V2", cat["recipes"]), "flux2-klein-9b")
        self.assertIsNone(igk.recipe_from_name("ponpoke/flux2-klein-4b-uncensored-text-encoder", cat["recipes"]))
        self.assertIsNone(igk.recipe_from_name("a/some-other-model", cat["recipes"]))
        self.assertEqual(igk.license_of({"license": "other", "license_name": "flux-non-commercial-license"}),
                         ("flux-non-commercial-license", True))
        self.assertEqual(igk.license_of({"license": "apache-2.0"}), ("apache-2.0", False))
        self.assertEqual(igk.license_of({"license": "cc-by-nc-4.0"}), ("cc-by-nc-4.0", True))
        self.assertEqual(igk.license_of({"license": "other"}), (None, False))

    def test_watermark_survives_jpeg_and_resizing(self):
        import random
        rnd = random.Random(7)
        w = h = 512
        rows = []  # a smooth gradient with a little noise, like a photo background
        for y in range(h):
            r = bytearray()
            for x in range(w):
                base = 60 + (x + y) * 120 // (w + h)
                r += bytes(max(0, min(255, base + rnd.randint(-2, 2) + k * 20)) for k in range(3))
            rows.append(bytes(r))
        work = Path(HOME) / "wm"
        work.mkdir(exist_ok=True)
        self.assertLess(igk.watermark_score(w, h, rows), igk.MARK_THRESHOLD)
        src = work / "plain.bmp"
        igk.write_bmp(src, w, rows)
        png, mime = igk.mark_image(src, "png", "Test", work)
        self.assertEqual(mime, "image/png")
        self.assertIn(igk.DIGITAL_SOURCE.encode(), png)
        marked = work / "marked.png"
        marked.write_bytes(png)
        self.assertGreater(igk.watermark_score(*igk.image_rows(marked, work)), igk.MARK_THRESHOLD)
        jpg, mime = igk.mark_image(src, "jpeg", "Test", work)
        self.assertTrue(jpg.startswith(b"\xff\xd8\xff\xe1") and igk.DIGITAL_SOURCE.encode() in jpg)
        import subprocess
        small = work / "small.jpg"
        subprocess.run(["sips", "-Z", "256", "-s", "format", "jpeg", "-s", "formatOptions", "75", str(marked), "--out", str(small)],
                       capture_output=True, check=True)
        self.assertGreater(igk.watermark_score(*igk.image_rows(small, work)), igk.MARK_THRESHOLD)

    def test_sizes_follow_the_family_grid(self):
        rec = {"multiple": 16, "defaults": {"width": 1024, "height": 1024}}
        self.assertEqual(igk.local_size(args(aspect="16:9", resolution="1K"), rec, []), (1360, 768))
        self.assertEqual(igk.local_size(args(resolution="0.5K"), rec, []), (512, 512))
        self.assertEqual(igk.local_size(args(size="1000x700"), rec, []), (992, 704))
        self.assertEqual(igk.local_size(args(), {"multiple": 64, "defaults": {"width": 512, "height": 512}}, []), (512, 512))

    def test_resolves_curated_ids_and_hugging_face_links(self):
        m = igk.resolve_local("FLUX.2 klein 4B", self.cat)
        self.assertEqual(m["id"], "flux2-klein-4b")
        real = igk.hf_get

        def fake(path):
            if "/tree/" in path:
                return [{"type": "file", "path": "x-Q8_0.gguf", "size": 8, "lfs": {"oid": "a" * 64}},
                        {"type": "file", "path": "x-Q4_K_M.gguf", "size": 4, "lfs": {"oid": "b" * 64}}]
            return {"id": "me/x-GGUF", "tags": ["base_model:quantized:black-forest-labs/FLUX.2-klein-4B"],
                    "cardData": {"license": "apache-2.0"}}
        igk.hf_get = fake
        try:
            m = igk.resolve_local("https://huggingface.co/me/x-GGUF", self.cat)
            self.assertEqual((m["recipe"], m["file"]["path"], m["name"], m["license"]),
                             ("flux2-klein-4b", "x-Q4_K_M.gguf", "x (me Q4_K_M)", "apache-2.0"))
            self.assertEqual(m["id"], "me-x-q4-k-m")
            m = igk.resolve_local("https://huggingface.co/me/x-GGUF/blob/main/x-Q8_0.gguf", self.cat)
            self.assertEqual(m["file"]["path"], "x-Q8_0.gguf")
            self.assertEqual(igk.resolve_local("me/x-GGUF", self.cat)["file"]["sha256"], "b" * 64)
        finally:
            igk.hf_get = real

    def test_downloads_resume_and_are_verified(self):
        data = os.urandom(300_000)
        FileHandler.files["/m.gguf"] = data
        dest = Path(HOME) / "dl" / "m.gguf"
        part = dest.with_name("m.gguf.part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(data[:100_000])  # an earlier, interrupted download
        sha = igk.hashlib.sha256(data).hexdigest()
        progress = igk.Progress(len(data), True)
        out = io_capture(lambda: igk.fetch_file(f"{self.base}/m.gguf", dest, len(data), sha, progress, "m"))
        self.assertEqual(dest.read_bytes(), data)
        self.assertFalse(part.exists())
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["done"], len(data))
        # A server that ignores the range starts over; a wrong checksum deletes the file.
        FileHandler.ignore_range = True
        dest.unlink()
        part.write_bytes(data[:50_000])
        try:
            io_capture(lambda: igk.fetch_file(f"{self.base}/m.gguf", dest, len(data), sha, igk.Progress(len(data), True), "m"))
        finally:
            FileHandler.ignore_range = False
        self.assertEqual(dest.read_bytes(), data)
        dest.unlink()
        with self.assertRaises(igk.ApiError):
            io_capture(lambda: igk.fetch_file(f"{self.base}/m.gguf", dest, len(data), "0" * 64, igk.Progress(len(data), True), "m"))
        self.assertFalse(dest.exists() or part.exists())

    def test_generates_with_the_engine(self):
        """A fake sd-cli: checks the flags, prints progress like the real one and writes the image."""
        engine = Path(HOME) / "fake-sd-cli"
        engine.write_text("#!/usr/bin/env python3\n"
                          "import sys, base64\n"
                          "a = sys.argv\n"
                          "assert '--diffusion-model' in a and '--llm' in a and '--vae' in a, a\n"
                          "assert a[a.index('--steps') + 1] == '4' and a[a.index('-W') + 1] == '1360', a\n"
                          "sys.stdout.write('  |####| 10/10 - 300.0MB/s\\n')\n"
                          "for i in range(1, 5): sys.stdout.write(f'\\r  |===>  | {i}/4 - 1.20s/it')\n"
                          "sys.stdout.write('\\n[I] decoding 1 latents\\n')\n"
                          "out = a[a.index('-o') + 1].replace('%02d', '00')\n"
                          f"open(out, 'wb').write(base64.b64decode('{base64.b64encode(PNG).decode()}'))\n")
        engine.chmod(0o755)
        models = igk.LOCAL_MODELS
        files = {"diffusion_model": "t/d.gguf", "llm": "t/l.gguf", "vae": "t/v.safetensors"}
        for rel in files.values():
            (models / rel).parent.mkdir(parents=True, exist_ok=True)
            (models / rel).write_bytes(b"x")
        igk.save_local_registry({"engine": {"version": "test"}, "models": [
            {"id": "flux2-klein-4b", "name": "FLUX.2 klein 4B", "recipe": "flux2-klein-4b", "files": files, "edit": True}]})
        os.environ["IMAGEGENKIT_SD_CLI"] = str(engine)
        real_arm = igk.apple_silicon
        igk.apple_silicon = lambda: True
        try:
            self.assertEqual(igk.local_readiness(), (True, "1 model"))
            spec = igk.all_providers(igk.load_config())["local"]
            out = Path(HOME) / "local-out.png"
            err = stderr_capture(lambda: igk.run_local(args(aspect="16:9", resolution="1K", out=str(out)), igk.load_config(), spec, None))
            data = out.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG") and b"trainedAlgorithmicMedia" in data)  # marked as AI-made
            self.assertIn("step 4/4", err)
            self.assertIn("loading the model", err)
            entry = json.loads(igk.LOG_FILE.read_text().splitlines()[-1])
            self.assertEqual((entry["provider"], entry["type"], entry["params"]["size"]), ("local", "local", "1360x768"))
            self.assertEqual(igk.estimate_cost(entry, igk.load_config()), 0)
            with self.assertRaises(igk.Unavailable):  # transparency isn't something these models do
                igk.run_local(args(background="transparent"), igk.load_config(), spec, None)
        finally:
            del os.environ["IMAGEGENKIT_SD_CLI"]
            igk.apple_silicon = real_arm
            igk.save_local_registry({"models": []})
            igk.LOG_FILE.unlink()


def io_capture(fn):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def stderr_capture(fn):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
        fn()
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
