#!/usr/bin/env python3
"""YouTube video → illustrated EPUB, optional Vietnamese (agy), optional Kindle AZW3."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from ebooklib import epub
from PIL import Image

SKIP_RE = re.compile(r"^\[(music|applause|laughter|cheers)\]$", re.I)
ABBREV_RE = re.compile(
    r"(?i)\b(?:mr|mrs|ms|dr|prof|sr|jr|vs|etc|eg|ie|al|fig|eq|approx|u\.s|u\.k)\.$"
)
SLIDE_MAE_KEEP = 6.5
SLIDE_MAE_STRONG = 12.0
DHASH_KEEP = 10
AGY_MODEL = "gemini-3.8-flash-low"
AGY_CHUNK = 28
AGY_TIMEOUT = 180
PROMPT_HEAD = """Dịch các câu bài giảng tiếng Anh sang tiếng Việt tự nhiên, giọng giáo trình kỹ thuật, xưng hô "bạn".
Giữ nguyên các thuật ngữ thường để tiếng Anh: agent, RAG, MCP, LoRA, CUDA, GPU, TPU, NPU, LLM, SDLC, AIDLC, DevOps, MLOps, LLMOps, context window, tool calling, token, embedding, transformer, fine-tune, distillation, prompt caching, red teaming, gateway, eval.
Không thêm số thứ tự, chú thích, hay ngoặc giải thích.
Trả về đúng {n} câu, cùng thứ tự với đầu vào.
"""


@dataclass
class Job:
    workdir: Path
    outdir: Path

    @property
    def video(self) -> Path:
        return self.workdir / "video.mp4"

    @property
    def info_path(self) -> Path:
        return self.workdir / "meta.info.json"

    @property
    def thumb(self) -> Path:
        return self.workdir / "meta.jpg"

    @property
    def thumbs(self) -> Path:
        return self.workdir / "thumbs"

    @property
    def slides(self) -> Path:
        return self.workdir / "slides"

    @property
    def frames(self) -> Path:
        return self.workdir / "frames"

    @property
    def vi_cache(self) -> Path:
        return self.workdir / "vi_cache.json"


@dataclass
class Cue:
    t0: float
    text: str


@dataclass
class Sentence:
    t0: float
    t1: float
    text: str

    @property
    def mid(self) -> float:
        return (self.t0 + self.t1) / 2.0


@dataclass
class Paragraph:
    t0: float
    t1: float
    text: str


@dataclass
class Slide:
    t: float
    path: Path


def dump_info(url: str) -> dict:
    p = subprocess.run(
        ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", url],
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise SystemExit(p.stderr.strip() or "yt-dlp dump-json failed")
    return json.loads(p.stdout)


def pick_sub_lang(info: dict) -> str | None:
    auto = info.get("automatic_captions") or {}
    manual = info.get("subtitles") or {}
    lang = info.get("language") or "en"
    for pool in (manual, auto):
        for cand in (f"{lang}-orig", lang, "en-orig", "en"):
            if cand in pool:
                return cand
        if pool:
            return next(iter(pool))
    return None


def fetch(url: str, job: Job, info: dict | None = None) -> dict:
    job.workdir.mkdir(parents=True, exist_ok=True)
    if info is None:
        info = dump_info(url)
    job.info_path.write_text(json.dumps(info))
    if list(job.workdir.glob("*.json3")) and job.video.exists() and job.video.stat().st_size > 10_000:
        return json.loads(job.info_path.read_text())
    sub = pick_sub_lang(info)
    if not sub:
        raise SystemExit("no captions (manual or auto) on this video")
    meta_out = str(job.workdir / "meta")
    cmd = [
        "yt-dlp",
        "--no-progress",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--write-info-json",
        "--skip-download",
        "-o",
        meta_out,
        url,
    ]
    if sub in (info.get("subtitles") or {}):
        cmd[2:2] = ["--write-sub", "--sub-lang", sub, "--sub-format", "json3"]
    else:
        cmd[2:2] = ["--write-auto-sub", "--sub-lang", sub, "--sub-format", "json3"]
    p = subprocess.run(cmd)
    if p.returncode != 0:
        raise SystemExit("yt-dlp metadata/captions failed")
    if not job.video.exists() or job.video.stat().st_size < 10_000:
        p = subprocess.run(
            [
                "yt-dlp",
                "--no-progress",
                "-f",
                "298/136/bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]",
                "--merge-output-format",
                "mp4",
                "-o",
                str(job.video),
                url,
            ]
        )
        if p.returncode != 0 or not job.video.exists():
            raise SystemExit("yt-dlp video download failed")
    return json.loads(job.info_path.read_text()) if job.info_path.exists() else info


def caption_path(job: Job) -> Path:
    files = sorted(job.workdir.glob("*.json3"))
    if not files:
        raise SystemExit(f"no json3 captions in {job.workdir}")
    orig = [p for p in files if "-orig.json3" in p.name]
    return orig[0] if orig else files[0]


def load_info(job: Job) -> dict:
    return json.loads(job.info_path.read_text())


def load_cues(job: Job) -> list[Cue]:
    data = json.loads(caption_path(job).read_text())
    cues: list[Cue] = []
    prev = ""
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or SKIP_RE.match(text) or text == prev:
            continue
        cues.append(Cue(event.get("tStartMs", 0) / 1000.0, text))
        prev = text
    return cues


def chapters_from_info(info: dict) -> list[dict]:
    raw = info.get("chapters") or []
    duration = float(info.get("duration") or 0)
    chapters = []
    for i, ch in enumerate(raw):
        start = float(ch["start_time"])
        end = float(
            ch.get("end_time")
            or (raw[i + 1]["start_time"] if i + 1 < len(raw) else duration)
        )
        chapters.append({"title": ch["title"], "start": start, "end": end})
    if not chapters:
        chapters.append(
            {"title": info.get("title") or "Video", "start": 0.0, "end": duration}
        )
    return chapters


def sentences_from_cues(cues: list[Cue]) -> list[Sentence]:
    if not cues:
        return []
    parts: list[str] = []
    spans: list[tuple[int, int, float, float]] = []
    pos = 0
    for i, cue in enumerate(cues):
        if parts:
            parts.append(" ")
            pos += 1
        start = pos
        parts.append(cue.text)
        pos += len(cue.text)
        t1 = cues[i + 1].t0 if i + 1 < len(cues) else cue.t0 + 2.0
        spans.append((start, pos, cue.t0, t1))
    full = "".join(parts)

    def t_at(idx: int) -> float:
        idx = max(0, min(idx, len(full) - 1))
        for s, e, t0, _t1 in spans:
            if s <= idx < e:
                return t0
        return spans[-1][2]

    sents: list[Sentence] = []
    start = 0
    i = 0
    n = len(full)
    while i < n:
        ch = full[i]
        if ch in ".!?" and (i == n - 1 or full[i + 1] in " \"'" or full[i + 1].isspace()):
            j = i + 1
            while j < n and full[j] in " \"'":
                j += 1
            frag = full[start : i + 1].rstrip()
            tail = frag[-16:] if len(frag) > 16 else frag
            if ABBREV_RE.search(tail):
                i += 1
                continue
            if j >= n or full[j].isupper() or full[j].isdigit():
                text = full[start : i + 1].strip()
                if text:
                    sents.append(Sentence(t_at(start), t_at(max(start, i - 1)), text))
                start = j
                i = j
                continue
        i += 1
    if start < n:
        text = full[start:].strip()
        if text:
            sents.append(Sentence(t_at(start), t_at(n - 1), text))
    return sents


def chapter_index_for(t: float, chapters: list[dict]) -> int:
    for i, ch in enumerate(chapters):
        if ch["start"] <= t < ch["end"]:
            return i
    if t < chapters[0]["start"]:
        return 0
    return len(chapters) - 1


def paragraphize(sents: list[Sentence]) -> list[Paragraph]:
    if not sents:
        return []
    paras: list[Paragraph] = []
    acc: list[Sentence] = []

    def flush() -> None:
        if not acc:
            return
        paras.append(Paragraph(acc[0].t0, acc[-1].t1, " ".join(s.text for s in acc)))
        acc.clear()

    for sent in sents:
        if acc and sent.t0 - acc[-1].t1 > 4.5:
            flush()
        acc.append(sent)
        if len(acc) >= 4 or sum(len(s.text) for s in acc) > 560:
            flush()
    flush()
    return paras


def luma_small(path: Path, size: tuple[int, int] = (64, 36)) -> list[int]:
    img = Image.open(path).convert("L").resize(size, Image.Resampling.LANCZOS)
    return list(img.get_flattened_data())


def mae(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def dhash(path: Path, size: int = 12) -> int:
    img = Image.open(path).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = list(img.get_flattened_data())
    bits = 0
    for row in range(size):
        row_off = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (
                1 if pixels[row_off + col] > pixels[row_off + col + 1] else 0
            )
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def ensure_thumbs(job: Job, duration: float) -> None:
    job.thumbs.mkdir(parents=True, exist_ok=True)
    existing = list(job.thumbs.glob("*.jpg"))
    if len(existing) >= max(10, int(duration) - 8):
        return
    p = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(job.video),
            "-an",
            "-vf",
            "fps=1,scale=480:-2",
            "-q:v",
            "7",
            str(job.thumbs / "%06d.jpg"),
        ]
    )
    if p.returncode != 0:
        raise SystemExit("ffmpeg 1fps thumbs failed")


def collapse_consecutive(times: list[float]) -> list[float]:
    if not times:
        return []
    kept: list[float] = []
    run_last = times[0]
    for t in times[1:]:
        if t == run_last + 1:
            run_last = t
            continue
        kept.append(run_last)
        run_last = t
    kept.append(run_last)
    return kept


def detect_slide_times(job: Job, duration: float) -> list[float]:
    ensure_thumbs(job, duration)
    files = sorted(job.thumbs.glob("*.jpg"))
    if not files:
        return []
    changed: list[float] = []
    prev_luma: list[int] | None = None
    prev_hash: int | None = None
    for path in files:
        t = float(int(path.stem) - 1)
        luma = luma_small(path)
        h = dhash(path)
        if prev_luma is None:
            changed.append(t)
            prev_luma, prev_hash = luma, h
            continue
        diff = mae(luma, prev_luma)
        ham = hamming(h, prev_hash) if prev_hash is not None else 99
        if diff >= SLIDE_MAE_STRONG or (diff >= SLIDE_MAE_KEEP and ham >= DHASH_KEEP):
            changed.append(t)
        prev_luma, prev_hash = luma, h
    kept = collapse_consecutive(changed)
    print(f"raw_changes={len(changed)} settled_slides={len(kept)}", flush=True)
    return kept


def extract_frame(job: Job, t: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(job.video),
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2",
            "-q:v",
            "3",
            "-y",
            str(dest),
        ]
    )
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 2000


def extract_slides(job: Job, times: list[float]) -> list[Slide]:
    job.slides.mkdir(parents=True, exist_ok=True)
    jobs = [(t, job.slides / f"s_{i:04d}_{int(t):05d}.jpg") for i, t in enumerate(times)]

    def run_one(item: tuple[float, Path]) -> Slide | None:
        t, dest = item
        if dest.exists() and dest.stat().st_size > 2000:
            return Slide(t, dest)
        if extract_frame(job, t, dest):
            return Slide(t, dest)
        return None

    slides: list[Slide] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed([pool.submit(run_one, j) for j in jobs]):
            s = fut.result()
            if s is not None:
                slides.append(s)
    slides.sort(key=lambda s: s.t)
    return slides


def load_existing_slides(job: Job) -> list[Slide]:
    slides: list[Slide] = []
    for path in sorted(job.slides.glob("s_*.jpg")):
        if path.stat().st_size <= 2000:
            continue
        slides.append(Slide(float(path.stem.split("_")[-1]), path))
    slides.sort(key=lambda s: s.t)
    return slides


def compress_jpeg(src: Path, dest: Path, max_w: int = 1280, quality: int = 84) -> None:
    img = Image.open(src).convert("RGB")
    w, h = img.size
    if w > max_w:
        img = img.resize((max_w, int(h * max_w / w)), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=quality, optimize=True)


def fmt_clock(seconds: float) -> str:
    s = int(max(0, seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:60] or "chapter"


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


CSS = """
body {
  font-family: "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  line-height: 1.55;
  font-size: 1.05em;
  color: #1a1a1a;
  margin: 1.2em 1.4em 2em;
}
h1 { font-size: 1.7em; line-height: 1.25; margin: 0 0 0.4em; }
p { margin: 0.7em 0; }
.meta, .stamp, .src { color: #555; font-size: 0.9em; font-family: sans-serif; }
.stamp { margin: 0.2em 0 0.8em; }
figure { margin: 1em 0; text-align: center; }
img.shot { max-width: 100%; height: auto; border: 1px solid #ddd; }
figcaption { font-size: 0.82em; color: #666; margin-top: 0.35em; font-family: sans-serif; }
.title-page { margin-top: 18%; text-align: center; }
.title-page h1 { font-size: 2em; }
"""


def figure_html(ch_title: str, t: float, src: str) -> str:
    return (
        f"<figure><img class='shot' src='{src}' alt='{html_escape(ch_title)} at {fmt_clock(t)}'/>"
        f"<figcaption>{html_escape(ch_title)} · {fmt_clock(t)}</figcaption></figure>"
    )


def epub_filename(title: str, lang: str) -> str:
    base = slug(title) or "youtube-video"
    return f"{base}{'' if lang == 'en' else '-' + lang}.epub"


def build_epub(
    job: Job,
    info: dict,
    chapters: list[dict],
    chapter_paras: list[list[Paragraph]],
    chapter_slides: list[list[Slide]],
    *,
    lang: str,
    outfile: str,
    blurb: str,
) -> Path:
    title = info.get("title") or "YouTube video"
    author = info.get("uploader") or info.get("channel") or "Unknown"
    url = info.get("webpage_url") or info.get("original_url") or ""
    duration = float(info.get("duration") or 0)
    n_slides = sum(len(s) for s in chapter_slides)

    book = epub.EpubBook()
    suffix = "" if lang == "en" else f"-{lang}"
    book.set_identifier(
        "yt-" + (info.get("id") or hashlib.md5(title.encode()).hexdigest()[:12]) + suffix
    )
    book.set_title(title)
    book.set_language(lang)
    book.add_author(author)
    if url:
        book.add_metadata("DC", "source", url)
    book.add_metadata("DC", "description", blurb)

    job.frames.mkdir(parents=True, exist_ok=True)
    if job.thumb.exists():
        cover_path = job.frames / "cover.jpg"
        compress_jpeg(job.thumb, cover_path, max_w=1400, quality=85)
        book.set_cover("images/cover.jpg", cover_path.read_bytes())

    style = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=CSS.encode("utf-8"),
    )
    book.add_item(style)

    title_html = epub.EpubHtml(title="Title", file_name="title.xhtml", lang=lang)
    title_html.add_item(style)
    stats = (
        f"{fmt_duration(duration)} · {len(chapters)} chương · {n_slides} slide"
        if lang == "vi"
        else f"{fmt_duration(duration)} · {len(chapters)} chapters · {n_slides} slides"
    )
    title_html.content = f"""
<div class="title-page">
  <h1>{html_escape(title)}</h1>
  <p class="meta">{html_escape(author)}</p>
  <p class="meta">{html_escape(stats)}</p>
  <p class="src"><a href="{html_escape(url)}">{html_escape(url)}</a></p>
  <p>{html_escape(blurb)}</p>
</div>
"""
    book.add_item(title_html)

    spine = [title_html]
    toc = [title_html]
    img_index = 0

    for i, ch in enumerate(chapters):
        paras = chapter_paras[i]
        slides = chapter_slides[i]
        parts = [
            f"<h1>{html_escape(ch['title'])}</h1>",
            f"<p class='stamp'>{fmt_clock(ch['start'])} – {fmt_clock(ch['end'])}</p>",
        ]
        pi = 0
        si = 0
        while pi < len(paras) or si < len(slides):
            take_slide = si < len(slides) and (
                pi == len(paras) or slides[si].t <= paras[pi].t0
            )
            if take_slide:
                slide = slides[si]
                img_index += 1
                name = f"images/s_{i:02d}_{si:03d}.jpg"
                dest = job.frames / f"epub_{lang}_s_{i:02d}_{si:03d}.jpg"
                compress_jpeg(slide.path, dest)
                book.add_item(
                    epub.EpubItem(
                        uid=f"img{lang}{img_index}",
                        file_name=name,
                        media_type="image/jpeg",
                        content=dest.read_bytes(),
                    )
                )
                parts.append(figure_html(ch["title"], slide.t, name))
                si += 1
            else:
                parts.append(f"<p>{html_escape(paras[pi].text)}</p>")
                pi += 1
        if not paras and not slides:
            parts.append("<p><em>No captions in this chapter.</em></p>")
        page = epub.EpubHtml(
            title=ch["title"],
            file_name=f"ch_{i:02d}_{slug(ch.get('title_en') or ch['title'])}.xhtml",
            lang=lang,
        )
        page.add_item(style)
        page.content = "\n".join(parts)
        book.add_item(page)
        spine.append(page)
        toc.append(page)

    book.toc = toc
    book.spine = ["nav"] + spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    job.outdir.mkdir(parents=True, exist_ok=True)
    out = job.outdir / outfile
    epub.write_epub(str(out), book, {})
    return out


def assign_chapters(
    sents: list[Sentence], chapters: list[dict]
) -> tuple[list[list[Paragraph]], list[list[Sentence]]]:
    chapter_sents: list[list[Sentence]] = [[] for _ in chapters]
    for sent in sents:
        chapter_sents[chapter_index_for(sent.mid, chapters)].append(sent)
    return [paragraphize(ss) for ss in chapter_sents], chapter_sents


def assign_slides(slides: list[Slide], chapters: list[dict]) -> list[list[Slide]]:
    out: list[list[Slide]] = [[] for _ in chapters]
    for sl in slides:
        out[chapter_index_for(sl.t, chapters)].append(sl)
    return out


def build_source_epub(job: Job, info: dict) -> Path:
    chapters = chapters_from_info(info)
    sents = sentences_from_cues(load_cues(job))
    print(
        f"title={info.get('title')} chapters={len(chapters)} sentences={len(sents)}",
        flush=True,
    )
    chapter_paras, _ = assign_chapters(sents, chapters)
    duration = float(info.get("duration") or 0)
    slides = load_existing_slides(job)
    if not slides:
        times = detect_slide_times(job, duration)
        slides = extract_slides(job, times)
    print(f"slides={len(slides)}", flush=True)
    chapter_slides = assign_slides(slides, chapters)
    name = epub_filename(info.get("title") or "video", "en")
    out = build_epub(
        job,
        info,
        chapters,
        chapter_paras,
        chapter_slides,
        lang="en",
        outfile=name,
        blurb=(
            "Illustrated transcript. Sentences stay whole across chapter boundaries; "
            "every distinct slide is included."
        ),
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)", flush=True)
    return out


def agy_bin() -> str:
    found = shutil.which("agy") or str(Path.home() / ".local/bin/agy")
    if not Path(found).exists():
        raise SystemExit("agy CLI not found")
    return found


def agy_lines(lines: list[str]) -> list[str]:
    n = len(lines)
    schema = {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": n,
                "maxItems": n,
            }
        },
        "required": ["lines"],
    }
    numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(lines))
    prompt = PROMPT_HEAD.format(n=n) + "\n" + numbered
    with tempfile.TemporaryDirectory() as empty:
        p = subprocess.run(
            [
                agy_bin(),
                "--output-format",
                "json",
                "--disable-slash-commands",
                "--model",
                AGY_MODEL,
                f"--print-timeout={AGY_TIMEOUT}s",
                f"--json-schema={json.dumps(schema, ensure_ascii=False)}",
                f"--print={prompt}",
            ],
            cwd=empty,
            capture_output=True,
            text=True,
            timeout=AGY_TIMEOUT + 40,
        )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "?").strip().splitlines()
        raise RuntimeError(err[-1] if err else "agy failed")
    out = json.loads(p.stdout)
    if out.get("status") != "SUCCESS":
        raise RuntimeError(f"agy status {out.get('status')}: {out.get('error') or ''}")
    got = (out.get("structured_output") or {}).get("lines")
    if not isinstance(got, list) or len(got) != n:
        raise RuntimeError(
            f"bad line count: expected {n}, got {0 if not isinstance(got, list) else len(got)}"
        )
    cleaned = [str(x).strip() for x in got]
    if any(not x for x in cleaned):
        raise RuntimeError("empty translated line")
    return cleaned


def translate_chunk(lines: list[str]) -> list[str]:
    if not lines:
        return []
    try:
        return agy_lines(lines)
    except Exception as first:
        if len(lines) == 1:
            raise
        print(f"  retry-split n={len(lines)} err={first}", flush=True)
        mid = len(lines) // 2
        return translate_chunk(lines[:mid]) + translate_chunk(lines[mid:])


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"book_title": None, "titles": {}, "sentences": {}}


def save_cache(path: Path, cache: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    tmp.replace(path)


def build_vi_epub(job: Job, info: dict) -> Path:
    chapters = chapters_from_info(info)
    sents = sentences_from_cues(load_cues(job))
    cache = load_cache(job.vi_cache)
    titles_map: dict[str, str] = cache.get("titles") or {}
    sent_map: dict[str, str] = cache.get("sentences") or {}
    print(
        f"vi: chapters={len(chapters)} sentences={len(sents)} cached={len(sent_map)}",
        flush=True,
    )
    if not cache.get("book_title"):
        cache["book_title"] = translate_chunk([info.get("title") or ""])[0]
        save_cache(job.vi_cache, cache)
        print("  title", cache["book_title"], flush=True)
    missing_titles = [ch["title"] for ch in chapters if ch["title"] not in titles_map]
    if missing_titles:
        translated = translate_chunk(missing_titles)
        for src, dst in zip(missing_titles, translated):
            titles_map[src] = dst
        cache["titles"] = titles_map
        save_cache(job.vi_cache, cache)
    pending = [s.text for s in sents if s.text not in sent_map]
    total = max(1, (len(pending) + AGY_CHUNK - 1) // AGY_CHUNK)
    for i, batch in enumerate(chunks(pending, AGY_CHUNK), start=1):
        t0 = time.time()
        translated = translate_chunk(batch)
        for src, dst in zip(batch, translated):
            sent_map[src] = dst
        cache["sentences"] = sent_map
        save_cache(job.vi_cache, cache)
        print(
            f"  batch {i}/{total} n={len(batch)} {time.time() - t0:.1f}s cache={len(sent_map)}",
            flush=True,
        )
    missing = [s.text for s in sents if s.text not in sent_map]
    if missing:
        raise SystemExit(f"still missing {len(missing)} sentences")

    info_vi = dict(info)
    info_vi["title"] = cache["book_title"]
    chapters_vi = [
        {**ch, "title_en": ch["title"], "title": titles_map[ch["title"]]}
        for ch in chapters
    ]
    sents_vi = [Sentence(s.t0, s.t1, sent_map[s.text]) for s in sents]
    chapter_paras, _ = assign_chapters(sents_vi, chapters_vi)
    slides = load_existing_slides(job)
    if not slides:
        raise SystemExit("no slides; build the source EPUB first")
    chapter_slides = assign_slides(slides, chapters_vi)
    name = epub_filename(info.get("title") or "video", "vi")
    out = build_epub(
        job,
        info_vi,
        chapters_vi,
        chapter_paras,
        chapter_slides,
        lang="vi",
        outfile=name,
        blurb=(
            "Bản dịch tiếng Việt từ phụ đề tự động. Câu giữ nguyên vẹn khi sang chương; "
            "mỗi slide khác nhau đều được đưa vào."
        ),
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)", flush=True)
    return out


def find_kindle_documents() -> Path:
    user = os.environ.get("USER") or os.getlogin()
    p = Path(f"/run/media/{user}/Kindle/documents")
    if p.is_dir():
        return p
    raise SystemExit("Kindle not mounted at /run/media/$USER/Kindle")


def to_kindle(epubs: list[Path]) -> list[Path]:
    convert = shutil.which("ebook-convert")
    if not convert:
        raise SystemExit("ebook-convert (calibre) not found")
    dest = find_kindle_documents()
    copied: list[Path] = []
    for epub_path in epubs:
        azw = epub_path.with_suffix(".azw3")
        p = subprocess.run(
            [
                convert,
                str(epub_path),
                str(azw),
                "--output-profile",
                "kindle_pw3",
            ]
        )
        if p.returncode != 0 or not azw.exists():
            raise SystemExit(f"ebook-convert failed for {epub_path}")
        target = dest / azw.name
        shutil.copy2(azw, target)
        stale = dest / epub_path.name
        if stale.exists():
            stale.unlink()
        copied.append(target)
        print(f"kindle {target}", flush=True)
    subprocess.run(["sync"])
    return copied


def default_outdir() -> Path:
    home_art = Path.home() / "Work" / "artifacts"
    return home_art if home_art.is_dir() else Path.cwd()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YouTube → illustrated EPUB")
    p.add_argument("url", help="YouTube URL or 11-char id")
    p.add_argument("--out", type=Path, default=default_outdir())
    p.add_argument("--workdir", type=Path, default=None)
    p.add_argument("--vi", action="store_true", help="also build Vietnamese EPUB via agy")
    p.add_argument("--kindle", action="store_true", help="copy AZW3 onto a mounted Kindle")
    return p.parse_args()


def normalize_url(url: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return f"https://www.youtube.com/watch?v={url}"
    return url


def main() -> int:
    args = parse_args()
    url = normalize_url(args.url)
    info = dump_info(url)
    vid = info.get("id") or "video"
    workdir = args.workdir or Path(f"/tmp/yt-epub-{vid}")
    job = Job(workdir=workdir, outdir=args.out)
    print(f"id={vid} workdir={job.workdir} out={job.outdir}", flush=True)
    info = fetch(url, job, info)
    produced = [build_source_epub(job, info)]
    if args.vi:
        produced.append(build_vi_epub(job, info))
    if args.kindle:
        to_kindle(produced)
    for path in produced:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
