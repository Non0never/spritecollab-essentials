#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

SPRITES = "Graphics/Characters/PMD"
PORTRAITS = "Graphics/Pictures/PMD Portraits"


def clean_species(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub("[^A-Z0-9]", "", name.upper())


def make_label(names):
    parts = [clean_species(names[0])]
    for form in names[1:]:
        parts.append(re.sub("[^A-Z0-9]+", "_", form.upper()).strip("_"))
    return "_".join(parts)


def walk_tracker(tree, path=(), names=()):
    for key, node in tree.items():
        here = path + (key,)
        labels = names + (node["name"],) if node["name"] else names
        yield here, labels, node
        yield from walk_tracker(node.get("subgroups", {}), here, labels)


def follow_copies(anims, name):
    seen = set()
    while name in anims and name not in seen:
        seen.add(name)
        target = anims[name].findtext("CopyOf")
        if not target:
            return name, anims[name]
        name = target
    return None


def make_sheet(folder, anims, anim, label, dest, full_walk=False):
    found = follow_copies(anims, anim)
    if found is None:
        return None
    src_name, data = found
    png = folder / f"{src_name}-Anim.png"
    if not png.is_file():
        raise ValueError(f"Missing image: {png}")

    fw = int(data.findtext("FrameWidth"))
    fh = int(data.findtext("FrameHeight"))
    walk = anim == "Walk" and not full_walk

    with Image.open(png) as img:
        img = img.convert("RGBA")
        if img.width % fw or img.height % fh:
            raise ValueError(f"Invalid source grid: {png}")
        n = img.width // fw
        dirs = img.height // fh
        if dirs not in (1, 8):
            raise ValueError(f"Unsupported directions: {png}")
        cols = [i * n // 4 for i in range(4)] if walk else list(range(n))
        rows = [0, 6, 2, 4] if dirs == 8 else [0, 0, 0, 0]
        frames = []
        for r in rows:
            frames.append([img.crop((c * fw, r * fh, (c + 1) * fw, (r + 1) * fh)) for c in cols])

    boxes = [f.getbbox() for row in frames for f in row]
    boxes = [b for b in boxes if b]
    if not boxes:
        raise ValueError(f"Empty animation: {png}")
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
           max(b[2] for b in boxes), max(b[3] for b in boxes))
    w = (box[2] - box[0]) * 2
    h = (box[3] - box[1]) * 2

    durations = [int(d.text) for d in data.findall("Durations/Duration")]
    if len(durations) != n or min(durations, default=0) <= 0:
        raise ValueError(f"Invalid frame durations: {png}")
    delay = max(1, round(sum(durations) / len(durations) / 3))
    count = 1 if walk else n

    if walk:
        filename = label + ".png"
    elif count > 1:
        filename = f"[{count},{delay}]{label}_{anim}.png"
    else:
        filename = f"{label}_{anim}.png"

    sheet = Image.new("RGBA", (w * 4 * count, h * 4))
    for y, row in enumerate(frames):
        for x, frame in enumerate(row):
            frame = frame.crop(box).resize((w, h), Image.Resampling.NEAREST)
            if walk:
                sheet.paste(frame, (x * w, y * h))
            else:
                for k in range(4):
                    sheet.paste(frame, ((x * 4 + k) * w, y * h))
    sheet.save(dest / filename)

    return {
        "file": filename,
        "animation": "WalkFull" if full_walk else anim,
        "source_animation": src_name,
        "size": list(sheet.size),
        "frames": count,
        "delay": delay,
        "directions": dirs,
        "source_durations": durations,
    }


def convert_one(job):
    src, out, path, names = job
    label = make_label(names)
    where = "/".join(path)
    sprite_dir = src / "sprite" / Path(*path)
    portrait_dir = src / "portrait" / Path(*path)
    xml = sprite_dir / "AnimData.xml"
    pics = sorted(portrait_dir.glob("*.png"))

    record = {"id": label, "source_path": where, "source_names": list(names), "sprites": [], "portraits": []}
    missing = []
    errors = []

    if xml.exists():
        dest = out / SPRITES / label
        dest.mkdir()
        anims = {a.findtext("Name"): a for a in ET.parse(xml).findall("Anims/Anim")}
        todo = [(name, False) for name in anims]
        if "Walk" in anims:
            todo.append(("Walk", True))
        else:
            missing.append({"asset": label, "source": where, "missing": "Walk"})
        for name, full in todo:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
                raise ValueError("Invalid animation name: " + repr(name))
            try:
                sheet = make_sheet(sprite_dir, anims, name, label, dest, full)
            except Exception as e:
                errors.append({"asset": label, "animation": "WalkFull" if full else name, "error": str(e)})
                continue
            if sheet:
                sheet["file"] = f"{label}/{sheet['file']}"
                record["sprites"].append(sheet)
            elif not full:
                errors.append({"asset": label, "animation": name, "error": "Unresolved animation alias: " + name})
        if (sprite_dir / "credits.txt").is_file():
            shutil.copy2(sprite_dir / "credits.txt", dest / "credits.txt")
    else:
        missing.append({"asset": label, "source": where, "missing": "Walk"})

    if pics:
        dest = out / PORTRAITS / label
        dest.mkdir()
        for pic in pics:
            with Image.open(pic) as img:
                if img.size != (40, 40):
                    raise ValueError("Unexpected portrait size: " + str(pic))
                img.convert("RGBA").resize((80, 80), Image.Resampling.NEAREST).save(dest / pic.name)
            record["portraits"].append(pic.name)
        if (portrait_dir / "credits.txt").is_file():
            shutil.copy2(portrait_dir / "credits.txt", dest / "credits.txt")
    else:
        missing.append({"asset": label, "source": where, "missing": "portraits"})

    return record, missing, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 4))
    args = ap.parse_args()
    if args.workers < 1:
        ap.error("--workers must be positive")

    src = args.source.resolve()
    out = args.output.resolve()
    if out.exists():
        ap.error("Output folder already exists; use a new folder.")

    tracker = json.loads((src / "tracker.json").read_text())
    commit = subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"], text=True).strip()
    (out / SPRITES).mkdir(parents=True)
    (out / PORTRAITS).mkdir(parents=True)

    jobs = []
    used = set()
    missing = []
    for path, names, node in walk_tracker(tracker):
        if not all(re.fullmatch(r"\d{4}", p) for p in path):
            raise ValueError("Invalid tracker path: " + repr(path))
        label = make_label(names)
        has_sprite = (src / "sprite" / Path(*path) / "AnimData.xml").exists()
        has_portrait = any((src / "portrait" / Path(*path)).glob("*.png"))
        if not has_sprite and not has_portrait:
            if len(path) == 1:
                missing.append({"asset": label, "source": "/".join(path), "missing": "Walk, portraits"})
            continue
        if label in used:
            raise ValueError("Duplicate output name: " + label)
        used.add(label)
        jobs.append((src, out, path, names))

    records = []
    errors = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for record, absent, failed in pool.map(convert_one, jobs):
            records.append(record)
            missing += absent
            errors += failed
            if len(records) % 250 == 0:
                print(f"{len(records)} variants converted", flush=True)

    shutil.copy2(src / "credit_names.txt", out / "credit_names.txt")
    shutil.copy2(src / "LICENSE.md", out / "LICENSE.md")
    shutil.copytree(src / "license_history", out / "license_history")

    with open(out / "missing-assets.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["asset", "source", "missing"])
        writer.writeheader()
        writer.writerows(missing)

    sheets = [s for r in records for s in r["sprites"]]
    kinds = {s["animation"] for s in sheets}
    manifest = {
        "repository": "https://github.com/PMDCollab/SpriteCollab",
        "commit": commit,
        "scale": 2,
        "animations": sorted(kinds),
        "assets": records,
        "errors": errors,
    }
    (out / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "variants": len(records),
        "walk_sheets": sum(s["animation"] == "Walk" for s in sheets),
        "animation_sheets": sum(s["animation"] != "Walk" for s in sheets),
        "portraits": sum(len(r["portraits"]) for r in records),
        "full_walk_sheets": sum(s["animation"] == "WalkFull" for s in sheets),
        "source_animations": sum(s["animation"] != "WalkFull" for s in sheets),
        "animation_types": len(kinds - {"WalkFull"}),
        "missing_entries": len(missing),
        "conversion_errors": errors,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    if errors:
        raise SystemExit("Conversion errors; inspect source-manifest.json before using the output.")


if __name__ == "__main__":
    main()
