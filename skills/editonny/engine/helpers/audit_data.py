'Audit readable personal data in an exported video with local OCR.\n\nA contact sheet can miss a one-frame leak. Read every frame by default for videos up to three minutes; longer videos sample four frames per second unless --all is requested. Look for supplied terms, email addresses, supported identity-number patterns with valid check digits, telephone numbers, Luhn-valid payment-card patterns, social handles, and heuristic personal names. Treat name guesses and OCR matches as findings to review.\n\nReport observed text, time, frame interval, and bounding box so redaction can be placed or widened. Use Apple Vision on supported macOS systems or available Tesseract elsewhere. If no reader exists, stop with manual-review guidance. Preserve language support for the actual source rather than forcing an English OCR assumption.\n\nUsage: uv run --directory ENGINE python helpers/audit_data.py VIDEO --terms TERMS --terms-file terms.txt --all --json report.json\nUse the interval and pattern controls shown by --help. Exit 0 for no findings, 1 for findings, and 2 for errors. A clean OCR result is not proof that tiny, blurred, rotated, or transient private text is absent; inspect what the reader cannot reliably see.'

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import fmt_time, probe_video, require_binary  # noqa: E402
import screen_text as tnt  # noqa: E402

FPS_DEFAULT = 4.0         # frames per second read when the video is long (or with --fps)
ALL_END_S = 180.0      # up to this length every frame is read by default (a leak can last 1 frame)
BATCH = 200               # frames read per batch (progress and temp disk)
SIDE_MAX = 1920          # OCR on the video size, capped on the long side
CONF_MIN = 0.30
FUZZY = 0.86             # similarity for a term of 6+ letters read with a slip

# common Brazilian first names (IBGE 2010 top names and frequent ones); a name + a capitalized word is flagged
NAMES_VALUE = """
ana maria jose joao antonio francisco carlos paulo pedro lucas luiz marcos luis gabriel rafael daniel marcelo
bruno eduardo felipe raimundo rodrigo manoel mateus andre fernando fabio leonardo gustavo guilherme leandro
tiago thiago anderson ricardo jorge alexandre roberto edson diego vitor victor sergio claudio matheus thiago
julio juliana adriana marcia fernanda patricia aline sandra camila amanda bruna jessica leticia julia luciana
vanessa mariana gabriela vera vitoria larissa claudia beatriz rita luana sonia renata eliane josefa simone
natalia cristiane carla debora rosangela jaqueline rosa daniela aparecida marlene terezinha raimunda andreia
fabiana lucia raquel angela rafaela joana luzia isabel cleide tatiane priscila carolina vivian viviane helena
alice laura manuela valentina sophia sofia heloisa isadora lorena livia cecilia elisa yasmin bianca giovanna
enzo arthur davi heitor bernardo samuel murilo miguel henrique caio igor otavio vinicius kaique joaquim renan
""".split()
NAMES_SET = set(NAMES_VALUE)

# "a@b.com" or, as the OCR sometimes spaces it, "a @ b.com" (never "segue a @handle"); a real ending (.com, .br)
RE_EMAIL = re.compile(r"[\w.+-]{2,}(?:@| @ )[\w-]+(?:\.[\w-]+)*\.[a-zA-Z]{2,6}(?![\w])")
RE_CPF_FMT = re.compile(r"(?<!\d)\d{3}\.\s?\d{3}\.\s?\d{3}\s?-\s?\d{2}(?!\d)")
RE_ELEVEN_DIGITS = re.compile(r"(?<![\d.])\d{11}(?![\d.])")
RE_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}\s?/\s?\d{4}\s?-?\s?\d{2}(?!\d)")
RE_PHONE = re.compile(r"(?<![\d/.])(?:\+?\s?55\s?)?(?:\(\s?\d{2}\s?\)|\d{2})\s?9?\s?\d{4}\s?[-.\s]\s?\d{4}(?![\d/])")
RE_CARD = re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)")
RE_HANDLE = re.compile(r"(?<![\w@.])@[A-Za-z0-9_.]{3,30}")
RE_NAME = re.compile(r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]+)\s+(?:(?:d[aeo]s?)\s+)?([A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]+)")


def normal_value(text_value: str) -> str:
    t = unicodedata.normalize("NFKD", (text_value or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w@.\- ]+", " ", t)).strip()


def cpf_valid(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def luhn(d: str) -> bool:
    tot, doubled = 0, False
    for c in reversed(d):
        v = int(c)
        if doubled:
            v = v * 2 - 9 if v * 2 > 9 else v * 2
        tot += v
        doubled = not doubled
    return tot % 10 == 0


def terms_of_listing(text_value: str) -> list[str]:
    parts_value = re.split(r"[,;\n]", text_value or "")
    return [p.strip() for p in parts_value if p.strip()]


def find_term(line_normal: str, term_normal: str) -> bool:
    if not term_normal:
        return False
    if re.search(r"(?<![a-z0-9])" + re.escape(term_normal) + r"(?![a-z0-9])", line_normal):
        return True
    if len(term_normal.replace(" ", "")) < 6:
        return False
    toks, n = line_normal.split(), len(term_normal.split())
    for i in range(0, max(1, len(toks) - n + 1)):
        win_value = " ".join(toks[i:i + n])
        if difflib.SequenceMatcher(a=win_value, b=term_normal).ratio() >= FUZZY:
            return True
    return False


def findings_in_line(text_value: str, terms: list[str], defaults: bool = True,
                     ignore: list[str] | None = None) -> list[tuple[str, str]]:
    """[(category, detected text)] in one line read by the OCR."""
    out: list[tuple[str, str]] = []
    ln = normal_value(text_value)
    for t in terms:
        if find_term(ln, normal_value(t)):
            out.append(("term", t))
    if defaults:
        for m in RE_EMAIL.finditer(text_value):
            out.append(("e-mail", re.sub(r"\s", "", m.group(0))))
        for m in RE_CPF_FMT.finditer(text_value):
            out.append(("cpf", m.group(0)))
        for m in RE_ELEVEN_DIGITS.finditer(text_value):
            if cpf_valid(m.group(0)):
                out.append(("cpf", m.group(0)))
        for m in RE_CNPJ.finditer(text_value):
            if "/" in m.group(0) or "." in m.group(0):
                out.append(("cnpj", m.group(0)))
        for m in RE_CARD.finditer(text_value):
            if luhn(re.sub(r"\D", "", m.group(0))):
                out.append(("card", m.group(0)))
        for m in RE_PHONE.finditer(text_value):
            dig = re.sub(r"\D", "", m.group(0))
            if 10 <= len(dig) <= 13 and not any(c == "card" and dig in re.sub(r"\D", "", x) for c, x in out):
                out.append(("phone", m.group(0).strip()))
        emails = " ".join(x for c, x in out if c == "e-mail")
        for m in RE_HANDLE.finditer(text_value):
            if m.group(0)[1:] not in emails:
                out.append(("handle", m.group(0)))
        for m in RE_NAME.finditer(text_value):
            if normal_value(m.group(1)) in NAMES_SET:
                out.append(("possible_name", f'{m.group(1)} {m.group(2)}'))
    if ignore:
        ig = [normal_value(x) for x in ignore if x.strip()]
        out = [(c, x) for c, x in out if not any(i and (i in normal_value(x) or normal_value(x) in i) for i in ig)]
    seen_value, uniq = set(), []
    for c, x in out:
        if (c, normal_value(x)) not in seen_value:
            seen_value.add((c, normal_value(x)))
            uniq.append((c, x))
    return uniq


# ------------------------------------------------------------------ reading the frames

def reader_value() -> tuple[str, object] | None:
    """("vision", binary) on macOS, ("tesseract", path) elsewhere, None without any reader."""
    b = tnt.compile_vision()
    if b:
        return "vision", b
    t = shutil.which("tesseract")
    if t:
        return "tesseract", t
    return None


def parse_tesseract_tsv(tsv: str, width_value: int, height_value: int) -> list[dict]:
    """Lines of a `tesseract ... tsv` output, in the same shape as the Vision reader (fractions, y from the bottom)."""
    lines_value: dict[tuple, dict] = {}
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        key = (c[2], c[3], c[4])
        x, y, w, h, conf = int(c[6]), int(c[7]), int(c[8]), int(c[9]), float(c[10])
        ln = lines_value.setdefault(key, {"t": [], "c": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        ln["t"].append(c[11])
        ln["c"].append(conf)
        ln["x0"], ln["y0"] = min(ln["x0"], x), min(ln["y0"], y)
        ln["x1"], ln["y1"] = max(ln["x1"], x + w), max(ln["y1"], y + h)
    out = []
    for ln in lines_value.values():
        out.append({"t": " ".join(ln["t"]), "c": max(0.0, min(ln["c"])) / 100.0,
                    "x": ln["x0"] / width_value, "w": (ln["x1"] - ln["x0"]) / width_value,
                    "y": 1 - ln["y1"] / height_value, "h": (ln["y1"] - ln["y0"]) / height_value})
    return out


def tesseract_languages(installed: list[str]) -> str:
    """Tesseract `-l` value. EDITONNY_OCR_LANGS (for example "fra" or "deu+eng") picks the installed languages to read
    with, in that order: Tesseract weighs the first one most, so a combined eng+fra read loses French accents that fra
    alone keeps. Without it, the guide's spoken languages."""
    wanted = [x for x in re.split(r"[+,\s]+", os.environ.get("EDITONNY_OCR_LANGS", "")) if x in installed]
    return "+".join(wanted) or "+".join(x for x in ("eng", "por", "spa") if x in installed) or "eng"


def read_directory(engine_value: tuple[str, object], directory: Path) -> dict[str, list[dict]]:
    if engine_value[0] == "vision":
        output_value = directory / "ocr.json"
        subprocess.run([str(engine_value[1]), str(directory), str(output_value)], capture_output=True, text=True, check=True)
        return json.loads(output_value.read_text())
    out: dict[str, list[dict]] = {}
    from PIL import Image  # noqa: PLC0415
    installed = subprocess.run([str(engine_value[1]), "--list-langs"], capture_output=True, text=True).stdout.split()
    lang = tesseract_languages(installed)
    for img in sorted(directory.glob("f_*.jpg")):
        w, h = Image.open(img).size
        # A failed read must stop the audit instead of passing as a frame without text.
        r = subprocess.run([str(engine_value[1]), str(img), "-", "-l", lang, "tsv"], capture_output=True, text=True, check=True)
        out[img.name] = parse_tesseract_tsv(r.stdout, w, h)
    return out


def extract_frames(video: Path, directory: Path, first_value: int, n: int, rate: float, fps_video: float, sample_value: bool,
            scale_value: str) -> int:
    """n frames as f_00001.jpg..., starting at index `first_value` of the grid of `rate` per second (every frame of the
    video when not `sample_value`). Counted in frames, not in seconds: a batch cut by time repeated or skipped the frame
    on its border and the frame numbers came out one off."""
    t0 = max(0.0, first_value / rate - 0.5 / fps_video)      # half a frame early: the frame on the grid is the first
    vf = ([f'fps={rate}'] if sample_value else []) + ([scale_value] if scale_value else [])
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f'{t0:.4f}', "-i", str(video), "-frames:v", str(n), "-an"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if not sample_value:
        cmd += ["-fps_mode", "passthrough"]
    cmd += ["-q:v", "2", str(directory / "f_%05d.jpg")]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return len(list(directory.glob("f_*.jpg")))


def audit(video: Path, terms: list[str], *, fps: float | None = FPS_DEFAULT, start_value: float = 0.0,
             end_value: float | None = None, defaults: bool = True, ignore: list[str] | None = None,
             engine_value: tuple[str, object] | None = None, progress=print) -> dict:
    require_binary("ffmpeg")
    info = probe_video(video)
    W, H = int(info.get("display_width") or info["width"]), int(info.get("display_height") or info["height"])
    fps_video = float(info.get("fps") or 30.0)
    total_duration = float(info["duration"])
    end_value = min(total_duration, end_value) if end_value is not None else total_duration
    engine_value = engine_value or reader_value()
    if engine_value is None:
        raise RuntimeError('no text reader available on this computer')
    rate = fps or fps_video
    scale_value = f"scale='if(gt(iw,ih),min(iw,{SIDE_MAX}),-2)':'if(gt(iw,ih),-2,min(ih,{SIDE_MAX}))'"\
        if max(W, H) > SIDE_MAX else ""
    occurrences: list[dict] = []
    idx = int(math.ceil(max(0.0, start_value) * rate - 1e-6))
    last_value = int(math.floor(end_value * rate - 1e-6))          # the last frame starts before the end
    total = max(0, last_value - idx + 1)
    loaded = 0
    started_at = time.monotonic()
    while idx <= last_value:
        n_request = min(BATCH, last_value - idx + 1)
        with tempfile.TemporaryDirectory(prefix="editonny-audit-data-") as tmp:
            directory = Path(tmp)
            n = extract_frames(video, directory, idx, n_request, rate, fps_video, bool(fps), scale_value)
            raw_value = read_directory(engine_value, directory) if n else {}
        for k in range(n):
            tq = (idx + k) / rate
            for it in raw_value.get(f'f_{k + 1:05d}.jpg', []):
                if float(it.get("c", 1)) < CONF_MIN:
                    continue
                for cat, finding in findings_in_line(str(it.get("t", "")), terms, defaults, ignore):
                    x, y, w, h = (float(it[c]) for c in ("x", "y", "w", "h"))
                    occurrences.append({"category": cat, "text": finding, "line": it.get("t", ""), "t": round(tq, 3),
                                  'frame': int(round(tq * fps_video)),
                                  "box": [round(x * W), round((1 - y - h) * H), round(w * W), round(h * H)]})
        loaded += n
        progress(f'  read {loaded} of {total} frames ({time.monotonic() - started_at:.0f} s)')
        idx += n_request
        if n == 0:
            break
    return {"video": str(video), "width": W, "height": H, "fps_video": fps_video,
            'sampling': "all frames" if not fps else f'{fps:g} per second', "reader": engine_value[0],
            "start": round(max(0.0, start_value), 3), "end": round(end_value, 3), 'frames_read': loaded,
            "terms": terms, "patterns": defaults, "findings": join_value(occurrences, 1.0 / rate)}


def join_value(occurrences: list[dict], step_value: float) -> list[dict]:
    """Occurrences of the same text in consecutive frames become one finding with from/to."""
    groups_value: dict[tuple, list[dict]] = {}
    for o in sorted(occurrences, key=lambda o: o["t"]):
        key = (o["category"], normal_value(o["text"]))
        if key not in groups_value:     # the same text read with a slip on one frame ("(17)" for "(11)") joins it
            key = next((k for k in groups_value if k[0] == key[0]
                        and difflib.SequenceMatcher(a=k[1], b=key[1]).ratio() >= FUZZY), key)
        groups_value.setdefault(key, []).append(o)
    out = []
    for (cat, _), oc in groups_value.items():
        windows_value: list[list[dict]] = []
        for o in oc:
            if windows_value and o["t"] - windows_value[-1][-1]["t"] <= step_value * 1.6:
                windows_value[-1].append(o)
            else:
                windows_value.append([o])
        for j in windows_value:
            xs = [o["box"] for o in j]
            x0, y0 = min(c[0] for c in xs), min(c[1] for c in xs)
            x1, y1 = max(c[0] + c[2] for c in xs), max(c[1] + c[3] for c in xs)
            out.append({"category": cat, "text": j[0]["text"], "line": j[0]["line"],
                        "start": j[0]["t"], "end": j[-1]["t"], 'start_frame': j[0]['frame'], 'end_frame': j[-1]['frame'],
                        "occurrences": len(j), "box": [x0, y0, x1 - x0, y1 - y0]})
    return sorted(out, key=lambda a: (a["start"], a["category"]))


NAME_CAT = {"term": "term", "e-mail": "e-mail", "cpf": "CPF", "cnpj": "CNPJ", "phone": "phone",
            "card": 'card', "handle": "handle (@)", "possible_name": 'possible name'}


def report_value(rep: dict) -> list[str]:
    ach = rep["findings"]
    lines_value = [f"audit_data: {rep['frames_read']} frames read ({rep['sampling']}) from {Path(rep['video']).name}, {fmt_time(rep['start'])} to {fmt_time(rep['end'])}, reader {rep['reader']} · {len(ach)} findings"]
    for a in ach:
        when_value = (f"{fmt_time(a['start'])} (frame {a['start_frame']})" if a['start_frame'] == a['end_frame'] else
                  f"{fmt_time(a['start'])} to {fmt_time(a['end'])} (frames {a['start_frame']} to {a['end_frame']})")
        x, y, w, h = a["box"]
        extra = "  verify whether this may appear" if a["category"] == "possible_name" else ""
        lines_value.append(f'''  {NAME_CAT.get(a['category'], a['category'])} "{a['text']}": {when_value}, box x {x} y {y} width {w} height {h}{extra}''')
        if normal_value(a["line"]) != normal_value(a["text"]):
            lines_value.append(f'''      read line: "{a['line'][:90]}"''')
    if ach and rep['sampling'] != "all frames":
        lines_value.append('  For sampled findings, rerun with --all around each interval using --start/--end to locate exact boundaries before expanding the blur')
    lines_value.append('  No detections do not prove the absence of personal data. OCR can miss small, blurred, rotated, or moving text. Inspect first and last frames and every screen change')
    return lines_value


def main(argv: list[str] | None = None) -> int:
    tnt.utf8_console()
    ap = argparse.ArgumentParser(description="Search an exported video's frames for personal data: names, email, identifiers, phones, and supplied terms.")
    ap.add_argument('video', type=Path)
    ap.add_argument("--terms", default="", help='names and words that must not appear, comma-separated', dest='terms')
    ap.add_argument("--terms-file", type=Path, help='file containing one term per line', dest='terms_file')
    ap.add_argument("--ignore", default="", help="allowed terms, such as the user's name or brand", dest='ignore')
    ap.add_argument("--fps", type=float, default=None,
                    help=f'sample this many frames per second (default: every frame up to {ALL_END_S / 60:.0f} minutes, then {FPS_DEFAULT:g} per second in longer videos)', dest='fps')
    ap.add_argument("--all", action="store_true", help='read every frame even in long videos', dest='all_value')
    ap.add_argument("--start", type=float, default=0.0, help='start at this second', dest='start_value')
    ap.add_argument("--end", type=float, help="end at this second", dest='end_value')
    ap.add_argument("--without-defaults", action="store_true", help='search only supplied terms; skip built-in personal-data patterns', dest='without_defaults')
    ap.add_argument("--json", type=Path, help='write the full report to this file', dest='json')
    a = ap.parse_args(argv)
    video = a.video.expanduser().absolute()
    if not video.is_file():
        print(f'ERROR: not found {video}', file=sys.stderr)
        return 2
    terms = terms_of_listing(a.terms)
    if a.terms_file:
        if not a.terms_file.is_file():
            print(f'ERROR: not found {a.terms_file}', file=sys.stderr)
            return 2
        terms += terms_of_listing(a.terms_file.read_text(encoding="utf-8"))
    if a.without_defaults and not terms:
        print('ERROR: --without-defaults requires --terms to search', file=sys.stderr)
        return 2
    engine_value = reader_value()
    if engine_value is None:
        print("ERROR: no text reader available. On macOS install Apple command-line tools for swiftc; on Windows or Linux install Tesseract and the recorded text's language data. Otherwise inspect a four-fps filmstrip and every screen transition frame by frame", file=sys.stderr)
        return 2
    fps = a.fps
    if fps is None and not a.all_value:
        interval_value = (a.end_value if a.end_value is not None else float(probe_video(video)["duration"])) - a.start_value
        if interval_value > ALL_END_S:
            fps = FPS_DEFAULT
            print(f'video duration {interval_value / 60:.1f} min: reading {FPS_DEFAULT:g} frames per second; --all reads every frame')
    try:
        rep = audit(video, terms, fps=fps, start_value=a.start_value, end_value=a.end_value, defaults=not a.without_defaults,
                       ignore=terms_of_listing(a.ignore), engine_value=engine_value, progress=lambda m: print(m, flush=True))
    except (subprocess.CalledProcessError, RuntimeError, KeyError, ValueError) as exc:
        print(f'ERROR: could not read {video.name}: {exc}', file=sys.stderr)
        return 2
    print("\n".join(report_value(rep)))
    if a.json:
        a.json.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f'report: {a.json}')
    return 1 if rep["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
