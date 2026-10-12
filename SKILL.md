---
name: youtube-epub
description: >
  Turn a YouTube video into an illustrated EPUB (transcript + unique slides),
  optionally a Vietnamese translation via the agy CLI, and optionally Kindle
  AZW3 sideload. Use when the user wants a YouTube video as EPUB/ebook with
  images, sách từ video YouTube, phụ đề thành epub, dịch epub tiếng Việt, or
  copy the book to a connected Kindle. Slash command: /youtube-epub.
---

# YouTube → illustrated EPUB

Produce a readable illustrated book from a YouTube URL. Default is the source
language. Add Vietnamese and/or Kindle only when the user asks.

## Need from the user

A YouTube URL. If they also want Vietnamese, Kindle copy, or a specific output
folder, use those flags. Do not wait for extra options when they only asked for
an EPUB.

## Dependencies

Need `yt-dlp`, `ffmpeg`, Python 3 with `ebooklib` and `Pillow`.

```bash
command -v yt-dlp ffmpeg
```

If Python deps are missing, create a venv next to the workdir and install
`scripts/requirements.txt` with `uv pip` (or `pip`).

Vietnamese needs `agy` on PATH (`~/.local/bin/agy`) and model `gemini-3.8-flash-low`.
Kindle needs `ebook-convert` from `calibre`. Install missing packages with
`pkexec pacman -S --noconfirm --needed <pkg>` on this Omarchy host.

## Run

From this skill directory:

```bash
python3 scripts/youtube_epub.py "https://youtu.be/VIDEO_ID" \
  --out "$HOME/Work/artifacts"
```

Add `--vi` for a Vietnamese EPUB via agy. Add `--kindle` to convert every
produced EPUB to AZW3 and copy into the mounted Kindle `documents/` folder.

Default workdir is `/tmp/yt-epub-<id>`. Reuse it on reruns so thumbs and slides
are not rebuilt.

Open the EPUB in Foliate when a desktop session is available:

```bash
foliate /path/to/book.epub
```

## Quality bar

- Rebuild captions into whole sentences, then assign each sentence to one
  chapter by midpoint. Do not split a sentence across chapter files.
- Extract unique slides (1 fps change detection, keep the last frame of a
  tween run). Interleave each slide with the transcript at that timestamp.
- Keep technical terms in English when translating to Vietnamese.

## Kindle

USB-copied `.epub` does not appear in the Kindle library on this device.
Always convert to `.azw3` with `ebook-convert --output-profile kindle_pw3`
and copy into `documents/`. Remove any `.epub` already sitting there.

After copy, `sync`. Unmount with `gio mount -u file:///run/media/$USER/Kindle`
if nothing holds the mount. If Nautilus has the folder open, tell the user to
Eject in Nautilus, then unplug. The library indexes on disconnect.

## Verify

- EPUB unzips; `dc:language` matches; chapter XHTML starts with a capital
  sentence, not a mid-thought fragment.
- Slide count is in the hundreds for a slide-heavy lecture, not 1–3 per
  chapter.
- Kindle: `.azw3` files exist under `documents/` before asking the user to
  eject.
