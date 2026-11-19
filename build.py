#!/usr/bin/env python3
import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
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
ORIGINAL = "SpriteCollab-Original.zip"
INDEX = "sprite-index.json.gz"
TRACKED = ["convert.py", "build.py", "requirements.txt", ".github/workflows/update.yml"]


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def gh_download(repo, tag, name, folder):
    sh("gh", "release", "download", tag, "--repo", repo, "--pattern", name, "--dir", str(folder))


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(MB):
            h.update(chunk)
    return h.hexdigest()


def bytes_hash(data):
    return hashlib.sha256(data).hexdigest()


def pixel_hash(data):
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGBA")
        h = hashlib.sha256(struct.pack(">II", *img.size))
        h.update(img.tobytes())
        return h.hexdigest()


def converter_hash():
    h = hashlib.sha256()
    for name in TRACKED:
        h.update(name.encode())
        h.update((HERE / name).read_bytes())
    return h.hexdigest()


def last_release(repo, folder):
    res = subprocess.run(["gh", "api", f"repos/{repo}/releases/latest"], text=True, capture_output=True)
    if res.returncode != 0:
        if "HTTP 404" in res.stderr:
            return None
        raise RuntimeError(res.stderr)
    release = json.loads(res.stdout)
    sizes = {a["name"]: a["size"] for a in release["assets"]}
    if "build-info.json" not in sizes:
        return None
    gh_download(repo, release["tag_name"], "build-info.json", folder)
    info = json.loads((folder / "build-info.json").read_text())
    if not info.get("assets"):
        return None
    for a in info["assets"]:
        if sizes.get(a["name"]) != a["size"]:
            return None
    info["_release"] = release
    return info


def build_index(manifest, read, names):
    names = set(names)
    files = {}
    for asset in manifest["assets"]:
        vid = asset["id"]
        for sheet in asset["sprites"]:
            path = f"{SPRITES}/{sheet['file']}"
            anim = sheet["animation"]
            files[path] = {
                "sha256": pixel_hash(read(path)),
                "variant": vid,
                "kind": "sprite",
                "name": "Walk (full)" if anim == "WalkFull" else anim,
                "frames": sheet["frames"],
                "delay": sheet["delay"] if sheet["frames"] > 1 else None,
            }
        for pic in asset["portraits"]:
            path = f"{PORTRAITS}/{vid}/{pic}"
            files[path] = {"sha256": pixel_hash(read(path)), "variant": vid,
                           "kind": "portrait", "name": Path(pic).stem + " portrait"}
        for folder in (SPRITES, PORTRAITS):
            path = f"{folder}/{vid}/credits.txt"
            if path in names:
                files[path] = {"sha256": bytes_hash(read(path)), "variant": vid, "kind": "credits", "name": "credits"}
    for path in sorted(names):
        if path == "credit_names.txt":
            kind = "credits"
        elif path == "LICENSE.md" or path.startswith("license_history/"):
            kind = "license"
        else:
            continue
        files[path] = {"sha256": bytes_hash(read(path)), "variant": "", "kind": kind, "name": path}
    return {"version": 1, "source_commit": manifest["commit"], "files": files}


def index_folder(out):
    manifest = json.loads((out / "source-manifest.json").read_text())
    names = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
    return build_index(manifest, lambda name: (out / name).read_bytes(), names)


def index_zip(path):
    with ZipFile(path) as z:
        all_names = z.namelist()
        manifest_name = next(n for n in all_names if n.endswith("/source-manifest.json"))
        prefix = manifest_name.removesuffix("source-manifest.json")
        manifest = json.loads(z.read(manifest_name))
        names = [n.removeprefix(prefix) for n in all_names if n.startswith(prefix)]
        return build_index(manifest, lambda name: z.read(prefix + name), names)


def old_index(repo, prev, work):
    if prev is None:
        return None
    release = prev["_release"]
    tag = release["tag_name"]
    available = {a["name"] for a in release["assets"]}
    folder = work / "previous"
    folder.mkdir()

    if INDEX in available:
        gh_download(repo, tag, INDEX, folder)
        with gzip.open(folder / INDEX, "rt", encoding="utf-8") as f:
            index = json.load(f)
        if index.get("version") != 1 or not isinstance(index.get("files"), dict):
            raise ValueError("Unsupported previous sprite index")
        return index

    print("Old release has no index yet, unpacking the full pack once.", flush=True)
    gh_download(repo, tag, PACK, folder)
    pack = folder / PACK
    expected = next(a["sha256"] for a in prev["assets"] if a["name"] == PACK)
    if file_hash(pack) != expected:
        raise ValueError("Previous pack checksum mismatch")
    index = index_zip(pack)
    pack.unlink()
    return index


def flatten_tracker(tree, path=(), names=()):
    flat = {}
    for key, node in tree.items():
        here = path + (key,)
        labels = names + (node["name"],) if node.get("name") else names
        flat[here] = (labels, {k: v for k, v in node.items() if k != "subgroups"})
        flat.update(flatten_tracker(node.get("subgroups", {}), here, labels))
    return flat


def tracker_notes(current, previous):
    old = flatten_tracker(previous)
    new = flatten_tracker(current)
    lines = []
    for key in sorted(old.keys() | new.keys()):
        labels = (new.get(key) or old.get(key))[0]
        before = old[key][1] if key in old else {}
        after = new[key][1] if key in new else {}
        changed = {f for f in before.keys() | after.keys() if before.get(f) != after.get(f)}
        if not changed:
            continue
        links = {f for f in changed if f.endswith("_link")}
        rest = changed - links
        bits = []
        if links:
            bits.append("download links updated")
        if rest:
            bits.append("tracker metadata updated (" + ", ".join(sorted(rest)) + ")")
        name = " / ".join(labels) or "/".join(key)
        lines.append(f"- {name}: {'; '.join(bits)}.")
    if not lines:
        return ""
    return "### Source updates\n\n" + "\n".join(lines) + "\n"


def upstream_notes(src, old):
    if not old:
        return ""
    current = json.loads((src / "tracker.json").read_text())
    raw = subprocess.check_output(
        ["gh", "api", f"repos/PMDCollab/SpriteCollab/contents/tracker.json?ref={old['source_commit']}",
         "-H", "Accept: application/vnd.github.raw+json"],
        text=True,
    )
    return tracker_notes(current, json.loads(raw))


def by_name(index):
    rows = {}
    for path, row in index["files"].items():
        if row["kind"] in ("sprite", "portrait"):
            rows[(row["kind"], row["variant"], row["name"])] = row
        else:
            rows[path] = row
    return rows


def changelog(current, old):
    if old is None:
        text = "First release of the complete pack.\n"
        return text, text

    before = by_name(old)
    after = by_name(current)
    groups = {"Added": defaultdict(set), "Updated": defaultdict(set), "Removed": defaultdict(set)}
    extra = set()
    for key in sorted(before.keys() | after.keys(), key=str):
        if key not in before:
            status = "Added"
        elif key not in after:
            status = "Removed"
        else:
            a, b = before[key], after[key]
            if a.get("sha256") == b.get("sha256") and a.get("frames") == b.get("frames") and a.get("delay") == b.get("delay"):
                continue
            status = "Updated"
        row = before[key] if status == "Removed" else after[key]
        if row["kind"] in ("sprite", "portrait"):
            groups[status][row["variant"]].add(row["name"])
        elif row["kind"] == "credits":
            extra.add("Artist credits updated.")
        else:
            extra.add("License information updated.")

    full = []
    short = []
    for title, variants in groups.items():
        if not variants:
            continue
        full += [f"### {title}", ""]
        short += [f"### {title}", ""]
        for vid, names in sorted(variants.items()):
            names = sorted(names)
            full.append(f"- {vid}: " + ", ".join(names))
            more = ", …" if len(names) > 6 else ""
            short.append(f"- {vid}: " + ", ".join(names[:6]) + more)
        full.append("")
        short.append("")
    for line in sorted(extra):
        full += [line, ""]
        short += [line, ""]

    if not full:
        if current["source_commit"] != old["source_commit"]:
            text = "Source repository updated; converted sprites and portraits are unchanged.\n"
        else:
            text = "Rebuilt pack. No sprite or portrait changes.\n"
        return text, text
    return "\n".join(full).strip() + "\n", "\n".join(short).strip() + "\n"


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


def make_zips(src, out, dest):
    files = [p for p in sorted(out.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"]
    write_sums(out, files, lambda p: p.relative_to(out).as_posix())

    pack = dest / PACK
    with ZipFile(pack, "w", compression=ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, Path(out.name) / p.relative_to(out))

    original = dest / ORIGINAL
    sh("git", "-C", str(src), "archive", "--format=zip", "--prefix=SpriteCollab/", f"--output={original}", "HEAD")
    listing = subprocess.check_output(["git", "-C", str(src), "ls-files", "-z"]).decode()
    tracked = set(listing.rstrip("\0").split("\0"))

    for z_path in (pack, original):
        with ZipFile(z_path) as z:
            if z.testzip() is not None:
                raise ValueError("ZIP integrity check failed: " + z_path.name)
            if z_path == original:
                inside = {i.filename.removeprefix("SpriteCollab/") for i in z.infolist() if not i.is_dir()}
                if inside != tracked:
                    raise ValueError("Original archive is incomplete")

    return split(pack) + split(original), len(tracked)


def publish(repo, sha, notes_text, dest, work):
    now = datetime.now(timezone.utc)
    tag = f"sprites-{now:%Y%m%d-%H%M%S}-{sha[:8]}"
    notes = work / "release-notes.md"
    notes.write_text(
        "SpriteCollab sprites, animations and portraits, sorted for Pokémon Essentials.\n\n"
        f"{notes_text}\n"
        f"[Full change list](https://github.com/{repo}/releases/download/{tag}/changes.md)\n\n"
        f"`{PACK}` — ready-to-use pack.\n"
        f"`{ORIGINAL}` — original files.\n\n"
        "Artist credits and licenses are included.\n",
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
    conv = converter_hash()

    with tempfile.TemporaryDirectory(prefix="spritecollab-") as tmp:
        work = Path(tmp)
        prev = None if args.no_publish else last_release(repo, work)
        if prev and not args.force and prev.get("upstream_commit") == sha and prev.get("converter_hash") == conv:
            print("Nothing new upstream and the scripts didn't change, skipping.", flush=True)
            return
        old = old_index(repo, prev, work)

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
        summary = check_output(src, out)

        print("Diffing against the last release...", flush=True)
        index = index_folder(out)
        full, short = changelog(index, old)
        if old and index["source_commit"] != old["source_commit"]:
            extra = upstream_notes(src, old)
            if extra:
                full += "\n" + extra
                short += "\n" + extra
        (out / "changes.md").write_text(full, encoding="utf-8")

        assets, original_count = make_zips(src, out, dest)
        with gzip.open(dest / INDEX, "wt", encoding="utf-8") as f:
            json.dump(index, f, separators=(",", ":"))
        (dest / "changes.md").write_text(full, encoding="utf-8")
        assets += [dest / INDEX, dest / "changes.md"]

        info = {
            "upstream_repository": UPSTREAM.removesuffix(".git"),
            "upstream_commit": sha,
            "converter_hash": conv,
            "converter_commit": os.environ.get("GITHUB_SHA"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "original_files": original_count,
            "summary": summary,
            "assets": [{"name": p.name, "size": p.stat().st_size, "sha256": file_hash(p)} for p in assets],
        }
        (dest / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")
        files = [p for p in sorted(dest.iterdir()) if p.is_file() and p.name != "SHA256SUMS.txt"]
        write_sums(dest, files, lambda p: p.name)

        if args.no_publish:
            print(f"Done, release files are in {dest}", flush=True)
            return
        publish(repo, sha, short, dest, work)


if __name__ == "__main__":
    main()
