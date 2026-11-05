"""Optional AI image for inserts, through OpenRouter.

The key comes from OPENROUTER_API_KEY in the environment or from `.env` in the skill root.
Without a key the helper exits 2 and explains that this step is optional (fetch_images.py finds
free photos with no key).

The model is picked from OpenRouter's public model list (models whose output includes images),
preferring a fast, cheap, current one; `--model` forces another. The image is resized to cover
`--size` and saved to <edit>/assets/, with an entry in <edit>/assets/credits.json.

Before decoding, the answer (image link or data) is saved to <edit>/assets/.last-image-response.json.
If saving the image fails after a paid generation (link expired, network, bad data), the error
says so and `--reuse-last` tries again from that file without calling the model (no new cost).

Usage:
    uv run python helpers/gen_image.py "<prompt>" --edit-dir DIR [--size 1080x900] [-o name.png]
        [--model ID] [--raw] [--dry-run] [--reuse-last]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_json, now_iso, save_json  # noqa: E402
from fetch_images import EnvFileError, read_env_key  # noqa: E402  (one .env reader for both helpers)

USER_AGENT = "editonny-engine/0.1"
MODELS_URL = "https://openrouter.ai/api/v1/models"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
# Preferred order: current, fast and cheap first. The live list decides what exists.
PREFERRED_MODELS = (
    "google/gemini-3.1-flash-image",
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-2.5-flash-image",
    "openai/gpt-5-image-mini",
    "google/gemini-3-pro-image",
    "openai/gpt-5-image",
)
FALLBACK_MODEL = PREFERRED_MODELS[0]
ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
PROMPT_SUFFIX = ("Photorealistic, natural light, sharp focus. No text, letters, logos, brand marks "
                 "or watermarks anywhere in the image.")


class GenError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def parse_size(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX]\s*(\d{2,5})\s*", value or "")
    if not m:
        raise GenError(f'invalid size: {value!r}. Use LARGURAxALTURA, ex: 1080x900', 2)
    w, h = int(m.group(1)), int(m.group(2))
    if not (64 <= w <= 4096 and 64 <= h <= 4096):
        raise GenError('each image dimension must be between 64 and 4096 pixels', 2)
    return w, h


def nearest_aspect(w: int, h: int) -> str:
    target = w / h

    def ratio(a: str) -> float:
        x, y = a.split(":")
        return int(x) / int(y)

    return min(ASPECTS, key=lambda a: abs(ratio(a) - target) / target)


def slugify(text: str, limit: int = 32) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:limit].rstrip("-")) or "image"


def image_models(timeout: float = 20) -> list[dict]:
    """Public model list filtered to models that can output images (no key needed)."""
    r = requests.get(MODELS_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    out = []
    for m in r.json().get("data", []):
        arch = m.get("architecture") or {}
        if "image" in (arch.get("output_modalities") or []) and not str(m.get("id", "")).startswith("openrouter/"):
            out.append(m)
    return out


def pick_model(requested: str | None) -> tuple[str, str]:
    """-> (model id, note). Uses the live list; falls back to a known id when offline."""
    try:
        models = image_models()
    except (requests.RequestException, ValueError):
        if requested:
            return requested, 'model list unavailable; using the requested model'
        return FALLBACK_MODEL, 'model list unavailable; using the default'
    ids = {m["id"] for m in models}
    if requested:
        if requested not in ids:
            raise GenError(f"model '{requested}' cannot currently generate images through OpenRouter. Options: {', '.join(sorted(ids))}", 2)
        return requested, "requested model"
    for mid in PREFERRED_MODELS:
        if mid in ids:
            return mid, 'preferred model available'
    if not models:
        return FALLBACK_MODEL, 'no image model listed; using the default'

    def price(m: dict) -> float:
        try:
            return float((m.get("pricing") or {}).get("image_output") or 1e9)
        except ValueError:
            return 1e9

    best = sorted(models, key=lambda m: (price(m), -int(m.get("created") or 0)))[0]
    return best["id"], "cheapest image model in the list"


def build_request(prompt: str, model: str, size: tuple[int, int], raw: bool) -> dict:
    text = prompt.strip() if raw else f'{prompt.strip()}\n\n{PROMPT_SUFFIX}'
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": nearest_aspect(*size)},
    }


LAST_RESPONSE = ".last-image-response.json"


def image_from_url(url: str, saved: str) -> bytes:
    """Bytes of an image the model returned (data: URL or link). `saved` names where the answer is kept."""
    if not isinstance(url, str) or not url:
        raise GenError(f'the model returned an empty image URL. Response saved at {saved}.')
    if url.startswith("data:"):
        if "," not in url:
            raise GenError(f'the image format could not be read. Response saved at {saved}.')
        try:
            return base64.b64decode(url.split(",", 1)[1])
        except (binascii.Error, ValueError):
            raise GenError(f'the image contains invalid base64 data. Response saved at {saved}; use --reuse-last to retry without another generation charge')
    if url.startswith("http"):
        try:
            got = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
            got.raise_for_status()
            return got.content
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            why = f'error {code}' if code else e.__class__.__name__
            raise GenError(f'the image was generated but download failed ({why}). The URL was saved at {saved}: use --reuse-last to retry the download without another generation charge')
    raise GenError(f'the model returned an unrecognized image URL. Response saved at {saved}.')


def request_image(payload: dict, key: str, timeout: float = 180, save_to: Path | None = None) -> bytes:
    headers = {"Authorization": f'Bearer {key}', "Content-Type": "application/json",
               "User-Agent": USER_AGENT, "X-Title": "editonny-engine"}
    try:
        r = requests.post(CHAT_URL, headers=headers, data=json.dumps(payload), timeout=timeout)
    except requests.RequestException as e:
        raise GenError(f'could not reach OpenRouter ({e.__class__.__name__}). Check the internet connection.')
    if r.status_code == 401:
        raise GenError('OpenRouter rejected OPENROUTER_API_KEY. Check the configured key.', 2)
    if r.status_code == 402:
        raise GenError('The OpenRouter account has no credit. Add credit or use fetch_images.py.')
    if r.status_code == 429:
        raise GenError("OpenRouter rate limit reached. Try again in a few minutes.")
    if r.status_code >= 400:
        # The error body may echo the submitted key.
        raise GenError(f'OpenRouter returned error {r.status_code}. Check the service before trying again.')
    try:
        data = r.json()
        message = data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise GenError('unexpected OpenRouter response without a message')
    if not isinstance(message, dict):
        raise GenError("Unexpected empty OpenRouter response. Nothing saved; retry.")
    urls = []
    for img in message.get("images") or []:
        url = ((img or {}).get("image_url") or {}).get("url", "") if isinstance(img, dict) else ""
        if isinstance(url, str) and url.startswith(("data:", "http")):
            urls.append(url)
    if not urls:
        raise GenError('The model returned no image. Try a more visual description.')
    saved = "the response"
    if save_to is not None:
        try:
            save_json(save_to, {"model": payload.get("model"), "urls": urls, "saved_at": now_iso()})
            saved = f'assets/{save_to.name}'
        except OSError:
            pass
    return image_from_url(urls[0], saved)


def cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    w, h = size
    s = max(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * s)), max(1, round(img.height * s))
    r = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return r.crop((x, y, x + w, y + h))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Generate an optional image through OpenRouter for a supporting insert.')
    ap.add_argument('prompt', help='image description')
    ap.add_argument("--edit-dir", required=True, help='session edit folder', dest='edit_dir')
    ap.add_argument("--size", default="1080x900", help='final WIDTHxHEIGHT (default 1080x900)', dest='size')
    ap.add_argument("-o", "--output", help='filename in assets/ (default: derived from the description)', dest='output')
    ap.add_argument("--model", help='select an OpenRouter model explicitly', dest='model')
    ap.add_argument("--raw", action="store_true", help='send the exact text without the no-text/no-logo suffix', dest='raw')
    ap.add_argument("--dry-run", action="store_true", help='show the proposed request without generation', dest='dry_run')
    ap.add_argument("--reuse-last", action="store_true",
                    help='save the image from the stored response after a download failure; do not generate again', dest='reuse_last')
    args = ap.parse_args(argv)

    try:
        if not args.prompt.strip():
            raise GenError('the request is empty', 2)
        size = parse_size(args.size)
        edit = Path(args.edit_dir).resolve()
        if not edit.is_dir():
            raise GenError(f'edit folder not found: {edit}', 2)
        try:
            key = read_env_key("OPENROUTER_API_KEY")
        except EnvFileError as e:
            raise GenError(str(e), 2)
        last_path = edit / "assets" / LAST_RESPONSE
        if args.reuse_last:
            last = load_json(last_path) if last_path.exists() else None
            if not isinstance(last, dict) or not last.get("urls"):
                raise GenError('no stored response to reuse. Generate an image first.', 2)
            key = key or "reuse"
        if not key and not args.dry_run:
            raise GenError(
                'OPENROUTER_API_KEY is not configured. Image generation is optional; use fetch_images.py for licensed photos, or configure the provider key in the environment or skill .env file after authorization.', 2)

        if args.reuse_last:
            model, note = str(last.get("model") or args.model or FALLBACK_MODEL), "stored response"
        else:
            model, note = pick_model(args.model)
        payload = build_request(args.prompt, model, size, args.raw)
        name = args.output or f'ai-{slugify(args.prompt)}-{hashlib.sha1((args.prompt + model).encode()).hexdigest()[:6]}.png'
        name = Path(name).name
        if not name.lower().endswith((".png", ".jpg", ".jpeg")):
            name += ".png"
        out_path = edit / "assets" / name

        if args.dry_run:
            print(f'model: {model} ({note})')
            print(f'file: {out_path}')
            print('request (not sent):')
            print(json.dumps({"url": CHAT_URL, "payload": payload, "key": "configured" if key else "missing"},
                             ensure_ascii=False, indent=2))
            return 0

        if args.reuse_last:
            print(f'using the stored response ({model}), without generating again...')
            raw = image_from_url(str(last["urls"][0]), f'assets/{LAST_RESPONSE}')
        else:
            print(f'generating with {model}...')
            (edit / "assets").mkdir(parents=True, exist_ok=True)
            raw = request_image(payload, key or "", save_to=last_path)
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            raise GenError(f'OpenRouter returned a non-image file. Response saved in assets/{LAST_RESPONSE}.')
        final = cover(img, size)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(".tmp-" + out_path.name)
        if name.lower().endswith(".png"):
            final.save(tmp, "PNG")
        else:
            final.save(tmp, "JPEG", quality=93)
        tmp.replace(out_path)

        credits_path = edit / "assets" / "credits.json"
        credits = load_json(credits_path, default=[])
        if not isinstance(credits, list):
            credits = []
        credits.append({
            "file": f'assets/{name}', "source": "openrouter", "model": model, "prompt": args.prompt,
            "url": None, "author": f'AI-generated image ({model})', "license": "AI-generated",
            "width": size[0], "height": size[1], "added_at": now_iso(),
        })
        save_json(credits_path, credits)
        print(f'ok: assets/{name}  {size[0]}x{size[1]}  model {model}')
        return 0
    except GenError as e:
        print(f'error: {e}', file=sys.stderr)
        return e.code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
