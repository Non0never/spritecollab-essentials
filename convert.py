#!/usr/bin/env python3
import argparse
import csv
import concurrent.futures
import os
import hashlib
import json
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from PIL import Image


def species_id(name):
    text = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return re.sub('[^A-Z0-9]', '', text.upper())


def variant_id(names):
    return '_'.join([species_id(names[0])] + [re.sub('[^A-Z0-9]+', '_', n.upper()).strip('_') for n in names[1:]])


def entries(nodes, path=(), names=()):
    for key, node in nodes.items():
        current = path + (key,)
        labels = names + ((node['name'],) if node['name'] else ())
        yield current, labels, node
        yield from entries(node.get('subgroups', {}), current, labels)


def resolve_animation(anims, name):
    seen = set()
    while name in anims and name not in seen:
        seen.add(name)
        anim = anims[name]
        target = anim.findtext('CopyOf')
        if not target:
            return name, anim
        name = target
    return None


def convert_animation(folder, anims, animation, label, destination, full_walk=False):
    resolved = resolve_animation(anims, animation)
    if not resolved:
        return None
    source_name, meta = resolved
    source = folder / (source_name + '-Anim.png')
    if not source.is_file():
        raise ValueError(f'Missing image: {source}')
    fw, fh = int(meta.findtext('FrameWidth')), int(meta.findtext('FrameHeight'))
    with Image.open(source) as image:
        image = image.convert('RGBA')
        if image.width % fw or image.height % fh:
            raise ValueError(f'Invalid source grid: {source}')
        frame_count, directions = image.width // fw, image.height // fh
        if directions not in (1, 8):
            raise ValueError(f'Unsupported directions: {source}')
        walk_charset = animation == 'Walk' and not full_walk
        selected = [i * frame_count // 4 for i in range(4)] if walk_charset else list(range(frame_count))
        rows = [0, 6, 2, 4] if directions == 8 else [0] * 4
        frames = [[image.crop((i * fw, d * fh, (i + 1) * fw, (d + 1) * fh)) for i in selected] for d in rows]
    bounds = [frame.getbbox() for row in frames for frame in row if frame.getbbox()]
    if not bounds:
        raise ValueError(f'Empty animation: {source}')
    box = (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds))
    width, height = (box[2] - box[0]) * 2, (box[3] - box[1]) * 2
    count = 1 if walk_charset else frame_count
    durations = [int(d.text) for d in meta.findall('Durations/Duration')]
    if not durations or len(durations) != frame_count or min(durations) <= 0:
        raise ValueError(f'Invalid frame durations: {source}')
    delay = max(1, round(sum(durations) / len(durations) / 3))
    prefix = f'[{count},{delay}]' if count > 1 else ''
    filename = label + '.png' if walk_charset else prefix + label + '_' + animation + '.png'
    result = Image.new('RGBA', (width * 4 * count, height * 4))
    for direction, row in enumerate(frames):
        for index, frame in enumerate(row):
            frame = frame.crop(box).resize((width, height), Image.Resampling.NEAREST)
            if walk_charset:
                result.paste(frame, (index * width, direction * height))
            else:
                for column in range(4):
                    result.paste(frame, ((index * 4 + column) * width, direction * height))
    result.save(destination / filename)
    return {'file': filename, 'animation': 'WalkFull' if full_walk else animation, 'source_animation': source_name,
            'size': list(result.size), 'frames': count, 'delay': delay,
            'directions': directions, 'source_durations': durations}


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(old, new):
    rows = []
    old_files = {p.relative_to(old).as_posix(): p for p in old.rglob('*') if p.is_file()}
    new_files = {p.relative_to(new).as_posix(): p for p in new.rglob('*') if p.is_file()}
    counts = Counter()
    for name in sorted(old_files.keys() | new_files.keys()):
        if name not in old_files:
            status = 'added'
        elif name not in new_files:
            status = 'old_only'
        elif name.lower().endswith('.png'):
            with Image.open(old_files[name]) as a, Image.open(new_files[name]) as b:
                a, b = a.convert('RGBA'), b.convert('RGBA')
                status = 'unchanged' if a.size == b.size and a.tobytes() == b.tobytes() else 'changed'
        else:
            status = 'unchanged' if old_files[name].read_bytes() == new_files[name].read_bytes() else 'changed'
        counts[status] += 1
        rows.append({'file': name, 'status': status})
    return rows, dict(counts)


def convert_entry(job):
    source, output, path, names = job
    label = variant_id(names)
    characters = output / 'Graphics/Characters/PMD'
    portraits = output / 'Graphics/Pictures/PMD Portraits'
    sprite_folder = source / 'sprite' / Path(*path)
    portrait_folder = source / 'portrait' / Path(*path)
    xml = sprite_folder / 'AnimData.xml'
    portrait_files = sorted(portrait_folder.glob('*.png'))
    errors, missing = [], []
    record = {'id': label, 'source_path': '/'.join(path), 'source_names': list(names), 'sprites': [], 'portraits': []}
    if xml.exists():
        character_folder = characters / label
        character_folder.mkdir()
        anims = {a.findtext('Name'): a for a in ET.parse(xml).findall('Anims/Anim')}
        for animation in anims:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', animation or ''):
                raise ValueError('Invalid animation name: ' + repr(animation))
            try:
                result = convert_animation(sprite_folder, anims, animation, label, character_folder)
                if result:
                    result['file'] = label + '/' + result['file']
                    record['sprites'].append(result)
                else:
                    raise ValueError('Unresolved animation alias: ' + animation)
            except Exception as error:
                errors.append({'asset': label, 'animation': animation, 'error': str(error)})
        if 'Walk' not in anims:
            missing.append({'asset': label, 'source': '/'.join(path), 'missing': 'Walk'})
        if 'Walk' in anims:
            try:
                result = convert_animation(sprite_folder, anims, 'Walk', label, character_folder, full_walk=True)
                if result:
                    result['file'] = label + '/' + result['file']
                    record['sprites'].append(result)
            except Exception as error:
                errors.append({'asset': label, 'animation': 'WalkFull', 'error': str(error)})
        credit = sprite_folder / 'credits.txt'
        if credit.is_file():
            shutil.copy2(credit, character_folder / 'credits.txt')
    else:
        missing.append({'asset': label, 'source': '/'.join(path), 'missing': 'Walk'})
    if portrait_files:
        target = portraits / label
        target.mkdir()
        for portrait in portrait_files:
            with Image.open(portrait) as image:
                if image.size != (40, 40):
                    raise ValueError('Unexpected portrait size: ' + str(portrait))
                image.convert('RGBA').resize((80, 80), Image.Resampling.NEAREST).save(target / portrait.name)
            record['portraits'].append(portrait.name)
        credit = portrait_folder / 'credits.txt'
        if credit.is_file():
            shutil.copy2(credit, target / 'credits.txt')
    else:
        missing.append({'asset': label, 'source': '/'.join(path), 'missing': 'portraits'})
    return record, missing, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--workers', type=int, default=min(os.cpu_count() or 1, 4))
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        parser.error('Output folder already exists; use a new folder.')
    tracker = json.loads((source / 'tracker.json').read_text())
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    characters = output / 'Graphics/Characters/PMD'
    portraits = output / 'Graphics/Pictures/PMD Portraits'
    characters.mkdir(parents=True)
    portraits.mkdir(parents=True)
    records, missing, errors = [], [], []
    labels = set()
    jobs = []
    for path, names, node in entries(tracker):
        if not all(re.fullmatch(r'\d{4}', part) for part in path):
            raise ValueError('Invalid tracker path: ' + repr(path))
        label = variant_id(names)
        xml = source / 'sprite' / Path(*path) / 'AnimData.xml'
        portrait_folder = source / 'portrait' / Path(*path)
        if not xml.exists() and not any(portrait_folder.glob('*.png')):
            if len(path) == 1:
                missing.append({'asset': label, 'source': '/'.join(path), 'missing': 'Walk, portraits'})
            continue
        if label in labels:
            raise ValueError('Duplicate output name: ' + label)
        labels.add(label)
        jobs.append((source, output, path, names))
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for record, absent, failed in pool.map(convert_entry, jobs):
            records.append(record)
            missing.extend(absent)
            errors.extend(failed)
            if len(records) % 250 == 0:
                print(f'{len(records)} variants converted', flush=True)
    for filename in ['credit_names.txt', 'LICENSE.md']:
        shutil.copy2(source / filename, output / filename)
    shutil.copytree(source / 'license_history', output / 'license_history')
    write_csv(output / 'missing-assets.csv', missing, ['asset', 'source', 'missing'])
    manifest = {'repository': 'https://github.com/PMDCollab/SpriteCollab', 'commit': commit,
                'scale': 2, 'animations': sorted({s['animation'] for r in records for s in r['sprites']}), 'assets': records, 'errors': errors}
    (output / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    rows, counts = compare(args.previous, output) if args.previous else ([], {})
    if args.previous:
        write_csv(output / 'changes.csv', rows, ['file', 'status'])
    summary = {'variants': len(records), 'walk_sheets': sum(s['animation'] == 'Walk' for r in records for s in r['sprites']),
               'animation_sheets': sum(s['animation'] != 'Walk' for r in records for s in r['sprites']),
               'portraits': sum(len(r['portraits']) for r in records),
               'full_walk_sheets': sum(s['animation'] == 'WalkFull' for r in records for s in r['sprites']),
               'source_animations': sum(s['animation'] != 'WalkFull' for r in records for s in r['sprites']),
               'animation_types': len({s['animation'] for r in records for s in r['sprites']} - {'WalkFull'}),
               'comparison': counts,
               'missing_entries': len(missing), 'conversion_errors': errors}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    if errors:
        raise SystemExit('Conversion errors; inspect source-manifest.json before using the output.')


if __name__ == '__main__':
    main()
