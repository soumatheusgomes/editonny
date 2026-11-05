"Search and download authorized images for video inserts.\n\nWikimedia Commons is the default source and needs no key. Default filtering accepts CC0/public-domain images. --allow-attribution permits CC BY with complete author credit; --allow-sharealike permits CC BY-SA with its additional obligations. Reject ambiguous restrictions and missing required author information. Rank permissive results first. A metadata label alone does not establish all personality or trademark rights.\n\nPexels is optional and uses PEXELS_API_KEY from approved local configuration. Without it, report the limitation and use Wikimedia. Store downloaded files in edit/assets and record URL, author, license, attribution requirements, share-alike status, and a ready credit line in assets/credits.json. Repeated searches do not download the same file twice.\n\nUsage: uv run python helpers/fetch_images.py QUERY --edit-dir EDIT --source wikimedia --count 3\nUse the orientation, minimum-width, rights, and JSON options shown by --help. External access must remain within the actual request's authorization."

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SKILL_ROOT, load_json, now_iso, save_json  # noqa: E402

USER_AGENT = "editonny-engine/0.1"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PEXELS_API = "https://api.pexels.com/v1/search"
TIMEOUT = 25
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


class FetchError(Exception):
    pass


class EnvFileError(Exception):
    """The skill's .env exists but cannot be read (message in PT-BR)."""


# ---------------------------------------------------------------- small helpers

_ENV_TIP_SHOWN = False


def _decode_env(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")            # PowerShell 5.1 `echo KEY=... > .env`
    try:
        return raw.decode("utf-8-sig")          # Notepad's "UTF-8 with BOM" too
    except UnicodeDecodeError:
        if b"\x00" in raw[:64]:
            try:
                return raw.decode("utf-16-le")
            except UnicodeDecodeError:
                pass
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError:
            raise EnvFileError('could not read the skill .env file. Save it as UTF-8 and retry')


def read_env_key(name: str) -> str | None:
    """Key from the environment, else from .env in the skill root (never other folders).

    Accepts UTF-8 (with or without BOM) and UTF-16 files, `export KEY=...`, quotes and an inline
    ` # comment` after an unquoted value. Raises EnvFileError when the file cannot be decoded."""
    global _ENV_TIP_SHOWN
    val = os.environ.get(name)
    if val and val.strip():
        return val.strip()
    env_file = SKILL_ROOT / ".env"
    if not env_file.is_file():
        return None
    try:
        raw = env_file.read_bytes()
    except OSError:
        return None
    for line in _decode_env(raw).splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip().removeprefix("export ").strip() != name:
            continue
        v = v.strip()
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        if os.name == "posix" and not _ENV_TIP_SHOWN:
            try:
                if env_file.stat().st_mode & 0o077:
                    print(f'hint: other computer users can read the .env file containing your key. Restrict access with chmod 600 "{env_file}"', file=sys.stderr)
            except OSError:
                pass
            _ENV_TIP_SHOWN = True
        return v or None
    return None


def slugify(text: str, limit: int = 40) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:limit].rstrip("-")) or "image"


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return " ".join(html.unescape(text).split())


def orientation_ok(w: int, h: int, orientation: str | None) -> bool:
    if not orientation or not w or not h:
        return True
    if orientation == "portrait":
        return h > w * 1.05
    if orientation == "landscape":
        return w > h * 1.05
    return True


LICENSE_RANK = {"free": 0, "attribution": 1, "sharealike": 2}


def license_class(short: str) -> str | None:
    """"free" (CC0, public domain), "attribution" (CC BY), "sharealike" (CC BY-SA) or None (no
    commercial use, no derivatives, GFDL, fair use, "No restrictions", unknown)."""
    s = short.lower().strip()
    if not s:
        return None
    parts = set(re.split(r"[\s\-/_.,]+", s))
    if parts & {"nc", "nd"} or "fair use" in s or "commercial" in s or "gfdl" in s:
        return None
    if s.startswith("cc0") or "public domain" in s or s.startswith("pd") or s.startswith("cc-pd"):
        return "free"
    if s.startswith(("cc by", "cc-by")) or "attribution" in s:
        return "sharealike" if ("sa" in parts or "sharealike" in s or "share alike" in s) else "attribution"
    return None


def license_ok(short: str, allow_attribution: bool = False, allow_sharealike: bool = False) -> bool:
    """Whether a license may go into a paid ad: CC0/PD always; CC BY and CC BY-SA only when allowed."""
    cls = license_class(short)
    return cls == "free" or (cls == "attribution" and (allow_attribution or allow_sharealike))\
        or (cls == "sharealike" and allow_sharealike)


def credit_line(item: dict) -> str:
    return f"Foto: {item['author']}, {item['license']}, {item['url']}"


def load_credits(path: Path) -> list:
    data = load_json(path, default=[])
    if isinstance(data, dict):
        data = data.get("items", [])
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------- sources

def search_wikimedia(query: str, want: int, orientation: str | None, min_width: int,
                     allow_attribution: bool = False, allow_sharealike: bool = False) -> list[dict]:
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f'{query} filetype:bitmap', "gsrnamespace": 6, "gsrlimit": 50,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920,
        "iiextmetadatafilter": "LicenseShortName|Artist|LicenseUrl|UsageTerms|AttributionRequired|Restrictions",
    }
    try:
        r = requests.get(COMMONS_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise FetchError(f'could not reach Wikimedia Commons ({e.__class__.__name__}). Check the internet connection.')
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value", "") for k, v in (info.get("extmetadata") or {}).items()}
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        lic = strip_html(meta.get("LicenseShortName"))
        if info.get("mime") not in ALLOWED_MIME or w < min_width:
            continue
        if not orientation_ok(w, h, orientation) or not license_ok(lic, allow_attribution, allow_sharealike):
            continue
        if strip_html(meta.get("Restrictions")):      # trademarked, personality, insignia...
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        cls = license_class(lic)
        author = strip_html(meta.get("Artist"))
        if cls != "free" and not author:
            continue                                   # credit required but nobody to credit
        lic_url = strip_html(meta.get("LicenseUrl"))
        if lic_url.startswith("//"):
            lic_url = "https:" + lic_url
        out.append({
            "source": "wikimedia",
            "id": f"commons-{p.get('pageid')}",
            "title": str(p.get("title", "")).removeprefix("File:"),
            "download_url": url,
            "url": info.get("descriptionurl") or url,
            "author": author or "unknown",
            "license": lic,
            "license_url": lic_url,
            "license_class": cls,
            "attribution_required": cls != "free" or str(meta.get("AttributionRequired", "")).lower() == "true",
            "share_alike": cls == "sharealike",
            "orig_width": w, "orig_height": h,
        })
    out.sort(key=lambda it: LICENSE_RANK.get(it["license_class"], 9))   # stable: search order inside each class
    return out[:max(want * 3, want)]


def search_pexels(query: str, want: int, orientation: str | None, key: str) -> list[dict]:
    params = {"query": query, "per_page": min(80, max(want * 3, 10)), "locale": "pt-BR"}
    if orientation in ("portrait", "landscape"):
        params["orientation"] = orientation
    try:
        r = requests.get(PEXELS_API, params=params, timeout=TIMEOUT,
                         headers={"Authorization": key, "User-Agent": USER_AGENT})
    except requests.RequestException as e:
        raise FetchError(f'could not reach Pexels ({e.__class__.__name__}). Check the internet connection.')
    if r.status_code in (401, 403):
        raise FetchError('Pexels rejected PEXELS_API_KEY. Check the configured key.')
    if r.status_code == 429:
        raise FetchError("Pexels search limit reached. Try again later or use --source wikimedia.")
    try:
        r.raise_for_status()
        photos = r.json().get("photos", [])
    except (requests.RequestException, ValueError) as e:
        raise FetchError(f'invalid Pexels response ({e.__class__.__name__}).')
    out = []
    for ph in photos:
        src = ph.get("src") or {}
        url = src.get("large2x") or src.get("large") or src.get("original")
        if not url:
            continue
        out.append({
            "source": "pexels", "id": f"pexels-{ph.get('id')}", "title": ph.get("alt") or "",
            "download_url": url, "url": ph.get("url") or url,
            "author": ph.get("photographer") or "unknown",
            "license": "Pexels License", "license_url": "https://www.pexels.com/license/",
            "license_class": "free", "attribution_required": False, "share_alike": False,
            "orig_width": int(ph.get("width") or 0), "orig_height": int(ph.get("height") or 0),
        })
    return out


# ---------------------------------------------------------------- download

def download(item: dict, dest_dir: Path, base_name: str) -> dict:
    try:
        r = requests.get(item["download_url"], headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise FetchError(f"failed to download {item['url']} ({e.__class__.__name__})")
    try:
        img = Image.open(io.BytesIO(r.content))
        img.load()
    except Exception:
        raise FetchError(f"downloaded file is not a valid image: {item['url']}")
    fmt = (img.format or "").upper()
    if fmt == "PNG":
        ext, data = ".png", r.content
    elif fmt == "JPEG" and img.mode in ("RGB", "L"):
        ext, data = ".jpg", r.content
    else:
        buf = io.BytesIO()
        rgb = img.convert("RGBA")
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        bg.paste(rgb, mask=rgb.getchannel("A"))
        bg.save(buf, "JPEG", quality=93)
        ext, data = ".jpg", buf.getvalue()
    path = dest_dir / f'{base_name}{ext}'
    tmp = path.with_name(".tmp-" + path.name)
    tmp.write_bytes(data)
    tmp.replace(path)
    return {"file": path.name, "width": img.width, "height": img.height}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Search and download licensed supporting images from Wikimedia Commons or Pexels.')
    ap.add_argument('query', help='search text; English often returns more results, for example "leather handbag"')
    ap.add_argument("--edit-dir", required=True, help='session edit folder', dest='edit_dir')
    ap.add_argument("--source", choices=("wikimedia", "pexels"), default="wikimedia", dest='source')
    ap.add_argument("--count", type=int, default=3, help='number of images to download (default 3)', dest='count')
    ap.add_argument("--orientation", choices=("portrait", "landscape"), help="portrait or landscape", dest='orientation')
    ap.add_argument("--min-width", type=int, default=800, help='minimum original width (default 800)', dest='min_width')
    ap.add_argument("--allow-attribution", action="store_true",
                    help='also accept CC BY; include the required attribution in the delivered credits', dest='allow_attribution')
    ap.add_argument("--allow-sharealike", action="store_true",
                    help='also accept CC BY-SA; attribution and applicable ShareAlike terms must be satisfied', dest='allow_sharealike')
    ap.add_argument("--json", action="store_true", help="print the result as JSON", dest='json')
    args = ap.parse_args(argv)

    if not args.query.strip():
        print('error: search is empty', file=sys.stderr)
        return 2
    if not 1 <= args.count <= 20:
        print('error: --count must be between 1 and 20', file=sys.stderr)
        return 2
    edit = Path(args.edit_dir).resolve()
    if not edit.is_dir():
        print(f'error: edit folder not found: {edit}', file=sys.stderr)
        return 2
    assets = edit / "assets"
    try:
        assets.mkdir(exist_ok=True)
    except OSError as e:
        print(f'error: could not create {assets}: {e}', file=sys.stderr)
        return 2
    credits_path = assets / "credits.json"
    credits = load_credits(credits_path)

    source = args.source
    key = None
    if source == "pexels":
        try:
            key = read_env_key("PEXELS_API_KEY")
        except EnvFileError as e:
            print(f'error: {e}', file=sys.stderr)
            return 2
        if not key:
            print('notice: optional PEXELS_API_KEY is missing; searching Wikimedia Commons')
            source = "wikimedia"

    try:
        if source == "pexels":
            found = search_pexels(args.query, args.count, args.orientation, key or "")
        else:
            found = search_wikimedia(args.query, args.count, args.orientation, args.min_width,
                                     args.allow_attribution, args.allow_sharealike)
    except FetchError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    if not found:
        hint = "" if (args.allow_attribution or source == "pexels") else\
            ' If the required attribution is acceptable, retry with --allow-attribution'
        print(f'no CC0 or public-domain image found for "{args.query}". Try other search terms; English often returns more results' + (" or remove --orientation." if args.orientation else ".") + hint)
        return 1

    by_url = {c.get("url"): c for c in credits if isinstance(c, dict)}
    results, errors = [], []
    slug = slugify(args.query)
    for item in found:
        if len(results) >= args.count:
            break
        known = by_url.get(item["url"])
        if known and (assets / str(known.get("file", "")).removeprefix("assets/")).exists():
            results.append(dict(known, reused=True))
            continue
        base = f"{slug}-{re.sub('[^a-z0-9]+', '', item['id'].lower())}"
        try:
            got = download(item, assets, base)
        except FetchError as e:
            errors.append(str(e))
            continue
        entry = {
            "file": f"assets/{got['file']}", "source": item["source"], "query": args.query,
            "title": item["title"], "url": item["url"], "download_url": item["download_url"],
            "author": item["author"], "license": item["license"], "license_url": item["license_url"],
            "attribution_required": item["attribution_required"], "share_alike": item.get("share_alike", False),
            "credit_line": credit_line(item) if item["attribution_required"] else None,
            "width": got["width"], "height": got["height"], "added_at": now_iso(),
        }
        credits.append(entry)
        by_url[item["url"]] = entry
        save_json(credits_path, credits)
        results.append(entry)

    if args.json:
        print(json.dumps({"query": args.query, "source": source, "images": results, "errors": errors},
                         ensure_ascii=False, indent=2))
    else:
        for e in results:
            tag = ' (already downloaded)' if e.get("reused") else ""
            print(f"ok: {e['file']}  {e['width']}x{e['height']}  {e['license']}  by {e['author']}{tag}")
            if e.get("share_alike"):
                print(f"   attribution and applicable ShareAlike terms required. Credit: {e.get('credit_line') or credit_line(e)}")
            elif e.get("attribution_required"):
                print(f"   required attribution: {e.get('credit_line') or credit_line(e)}")
        for msg in errors:
            print(f'notice: {msg}')
        if results and len(results) < args.count and not (args.allow_attribution or args.allow_sharealike) and source == "wikimedia":
            print(f'notice: only {len(results)} of {args.count} with CC0 or public-domain terms. --allow-attribution also accepts CC BY with required attribution')
        if results:
            print(f'credits saved at {credits_path}')
    if not results:
        print('error: no image downloaded', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
