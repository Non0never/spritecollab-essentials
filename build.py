#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

UPSTREAM = "https://github.com/PMDCollab/SpriteCollab.git"
HERE = Path(__file__).resolve().parent
MB = 1024 * 1024
MAX_ASSET = 1900 * MB

SPRITES = "Graphics/Characters/PMD"
PORTRAITS = "Graphics/Pictures/PMD Portraits"
PACK = "PMD-Sprites-Essentials-Complete.zip"


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(MB):
            h.update(chunk)
    return h.hexdigest()


def latest_tag(repo):
    res = subprocess.run(["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
                         text=True, capture_output=True)
    if res.returncode != 0:
        if "HTTP 404" in res.stderr:
            return None
        raise RuntimeError(res.stderr)
    return res.stdout.strip()


def check_png(path, size_ok):
    with Image.open(path) as img:
        if not size_ok(img):
            return False
        img.verify()
    return True


def check_output(src, out):
    manifest = json.loads((out / "source-manifest.json").read_text())
    if manifest["errors"]:
        raise ValueError("Conversion errors: " + repr(manifest["errors"][:5]))

    seen = set()
    anim_total = 0
    portrait_total = 0
    for asset in manifest["assets"]:
        vid = asset["id"]
        xml = src / "sprite" / asset["source_path"] / "AnimData.xml"
        expected = set()
        if xml.exists():
            expected = {a.findtext("Name") for a in ET.parse(xml).findall("Anims/Anim")}
        got = {s["animation"] for s in asset["sprites"]}
        if got - {"WalkFull"} != expected or ("Walk" in expected) != ("WalkFull" in got):
            raise ValueError("Incomplete animation coverage: " + vid)
        anim_total += len(expected)

        for sheet in asset["sprites"]:
            path = out / SPRITES / sheet["file"]
            if path in seen:
                raise ValueError(f"Duplicate export path: {path}")
            seen.add(path)
            grid = 4 * sheet["frames"]
            ok = lambda img: list(img.size) == sheet["size"] and img.width % grid == 0 and img.height % 4 == 0
            if not check_png(path, ok):
                raise ValueError(f"Invalid charset grid: {path}")
        if asset["sprites"] and not (out / SPRITES / vid / "credits.txt").is_file():
            raise ValueError("Missing sprite credits: " + vid)

        for pic in asset["portraits"]:
            path = out / PORTRAITS / vid / pic
            seen.add(path)
            if not check_png(path, lambda img: img.size == (80, 80)):
                raise ValueError(f"Invalid portrait size: {path}")
            portrait_total += 1
        if asset["portraits"] and not (out / PORTRAITS / vid / "credits.txt").is_file():
            raise ValueError("Missing portrait credits: " + vid)

    src_anims = sum(len(ET.parse(x).findall("Anims/Anim")) for x in (src / "sprite").rglob("AnimData.xml"))
    src_portraits = len(list((src / "portrait").rglob("*.png")))
    if anim_total != src_anims or portrait_total != src_portraits:
        raise ValueError("Some source animations or portraits are absent from the tracker/export")
    if seen != set((out / "Graphics").rglob("*.png")):
        raise ValueError("Missing or unlisted exported PNG files")

    with open(out / "animation-index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "animation", "source_animation", "frames", "file"])
        for asset in manifest["assets"]:
            for s in asset["sprites"]:
                w.writerow([asset["id"], s["animation"], s["source_animation"], s["frames"], f"{SPRITES}/{s['file']}"])

    summary = json.loads((out / "summary.json").read_text())
    summary["verified_pngs"] = len(seen)
    summary["source_animations"] = anim_total
    summary["source_portraits"] = portrait_total
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def split(path, limit=MAX_ASSET):
    total = path.stat().st_size
    if total <= limit:
        return [path]
    parts = []
    with open(path, "rb") as f:
        while f.tell() < total:
            part = path.with_name(f"{path.name}.{len(parts) + 1:03d}")
            left = limit
            with open(part, "wb") as dst:
                while left and (chunk := f.read(min(MB, left))):
                    dst.write(chunk)
                    left -= len(chunk)
            parts.append(part)
    path.unlink()
    return parts


def write_sums(folder, files, rel):
    with open(folder / "SHA256SUMS.txt", "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"{file_hash(p)}  {rel(p)}\n")


def make_zip(out, dest):
    files = [p for p in sorted(out.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"]
    write_sums(out, files, lambda p: p.relative_to(out).as_posix())

    pack = dest / PACK
    with ZipFile(pack, "w", compression=ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, Path(out.name) / p.relative_to(out))
        if z.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
    return split(pack)


def publish(repo, sha, dest, work):
    now = datetime.now(timezone.utc)
    tag = f"sprites-{now:%Y%m%d-%H%M%S}-{sha[:8]}"
    notes = work / "release-notes.md"
    notes.write_text(
        "SpriteCollab sprites, animations and portraits, sorted for Pokémon Essentials.\n\n"
        f"Built from SpriteCollab commit `{sha[:8]}`. Artist credits and licenses are included.\n",
        encoding="utf-8",
    )
    sh("gh", "release", "create", tag, "--repo", repo, "--target", os.environ.get("GITHUB_SHA", "main"),
       "--draft", "--title", f"PMD Sprites — {now:%Y-%m-%d}", "--notes-file", str(notes))
    for p in sorted(dest.iterdir()):
        sh("gh", "release", "upload", tag, str(p), "--repo", repo)
    sh("gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest")
    print("Published " + tag, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--source", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()
    repo = args.repository
    if not args.no_publish and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        ap.error("--repository owner/name is required for publishing")

    if args.source:
        sha = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    else:
        sha = subprocess.check_output(["git", "ls-remote", UPSTREAM, "refs/heads/master"], text=True).split()[0]
    if not re.fullmatch("[0-9a-f]{40}", sha):
        raise ValueError("Invalid upstream commit")

    if not args.no_publish and not args.force:
        tag = latest_tag(repo)
        if tag and tag.endswith("-" + sha[:8]):
            print("Nothing new upstream, skipping.", flush=True)
            return

    with tempfile.TemporaryDirectory(prefix="spritecollab-") as tmp:
        work = Path(tmp)
        if args.source:
            src = args.source.resolve()
        else:
            src = work / "source"
            sh("git", "init", str(src))
            sh("git", "-C", str(src), "remote", "add", "origin", UPSTREAM)
            sh("git", "-C", str(src), "fetch", "--depth=1", "origin", sha)
            sh("git", "-C", str(src), "checkout", "--detach", "FETCH_HEAD")

        out = work / "PMD-Sprites-Essentials"
        dest = args.output.resolve() if args.output else work / "release"
        dest.mkdir(parents=True, exist_ok=True)
        if any(dest.iterdir()):
            raise ValueError("Release output directory must be empty")

        print("Converting SpriteCollab " + sha, flush=True)
        sh("python", str(HERE / "convert.py"), str(src), str(out), "--workers", "4")
        check_output(src, out)
        make_zip(out, dest)

        if args.no_publish:
            print(f"Done, the pack is in {dest}", flush=True)
            return
        publish(repo, sha, dest, work)


if __name__ == "__main__":
    main()
