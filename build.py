#!/usr/bin/env python3
import argparse
import csv
import gzip
import io
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import struct
from collections import defaultdict
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

UPSTREAM = 'https://github.com/PMDCollab/SpriteCollab.git'
ROOT = Path(__file__).resolve().parent
LIMIT = 1900 * 1024 * 1024


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def fingerprint():
    result = hashlib.sha256()
    for name in ['convert.py', 'build.py', 'requirements.txt', 'README.md', '.github/workflows/update.yml']:
        result.update(name.encode())
        result.update((ROOT / name).read_bytes())
    return result.hexdigest()


def latest_build(repository, destination):
    response = subprocess.run(['gh', 'api', f'repos/{repository}/releases/latest'], text=True, capture_output=True)
    if response.returncode:
        if 'HTTP 404' in response.stderr:
            return None
        raise RuntimeError(response.stderr)
    release = json.loads(response.stdout)
    if not any(asset['name'] == 'build-info.json' for asset in release['assets']):
        return None
    run('gh', 'release', 'download', release['tag_name'], '--repo', repository,
        '--pattern', 'build-info.json', '--dir', str(destination))
    info = json.loads((destination / 'build-info.json').read_text())
    available = {asset['name']: asset['size'] for asset in release['assets']}
    if not info.get('assets') or any(available.get(asset['name']) != asset['size'] for asset in info['assets']):
        return None
    info['_release'] = release
    return info


def image_digest(data):
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert('RGBA')
        value = hashlib.sha256(struct.pack('>II', *image.size))
        value.update(image.tobytes())
        return value.hexdigest()


def make_index(manifest, read_file, paths):
    files = {}
    paths = set(paths)
    for asset in manifest['assets']:
        variant = asset['id']
        for sheet in asset['sprites']:
            path = 'Graphics/Characters/PMD/' + sheet['file']
            name = 'Walk (full)' if sheet['animation'] == 'WalkFull' else sheet['animation']
            files[path] = {'sha256': image_digest(read_file(path)), 'variant': variant, 'kind': 'sprite', 'name': name, 'frames': sheet['frames'], 'delay': sheet['delay'] if sheet['frames'] > 1 else None}
        for name in asset['portraits']:
            path = f'Graphics/Pictures/PMD Portraits/{variant}/{name}'
            files[path] = {'sha256': image_digest(read_file(path)), 'variant': variant, 'kind': 'portrait', 'name': Path(name).stem + ' portrait'}
        for path in [f'Graphics/Characters/PMD/{variant}/credits.txt', f'Graphics/Pictures/PMD Portraits/{variant}/credits.txt']:
            if path in paths:
                files[path] = {'sha256': hashlib.sha256(read_file(path)).hexdigest(), 'variant': variant, 'kind': 'credits', 'name': 'credits'}
    for path in sorted(paths):
        if path == 'credit_names.txt' or path == 'LICENSE.md' or path.startswith('license_history/'):
            files[path] = {'sha256': hashlib.sha256(read_file(path)).hexdigest(), 'variant': '', 'kind': 'credits' if path == 'credit_names.txt' else 'license', 'name': path}
    return {'version': 1, 'source_commit': manifest['commit'], 'files': files}


def export_index(output):
    manifest = json.loads((output / 'source-manifest.json').read_text())
    return make_index(manifest, lambda name: (output / name).read_bytes(),
                      (path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_file()))


def archive_index(path):
    with ZipFile(path) as archive:
        manifest_path = next(name for name in archive.namelist() if name.endswith('/source-manifest.json'))
        prefix = manifest_path.removesuffix('source-manifest.json')
        manifest = json.loads(archive.read(manifest_path))
        return make_index(manifest, lambda name: archive.read(prefix + name),
                          (name.removeprefix(prefix) for name in archive.namelist() if name.startswith(prefix)))


def previous_index(repository, previous, work):
    if previous is None:
        return None
    release = previous['_release']
    names = {asset['name'] for asset in release['assets']}
    folder = work / 'previous'
    folder.mkdir()
    if 'sprite-index.json.gz' in names:
        run('gh', 'release', 'download', release['tag_name'], '--repo', repository,
            '--pattern', 'sprite-index.json.gz', '--dir', str(folder))
        with gzip.open(folder / 'sprite-index.json.gz', 'rt', encoding='utf-8') as stream:
            index = json.load(stream)
        if index.get('version') != 1 or not isinstance(index.get('files'), dict):
            raise ValueError('Unsupported previous sprite index')
        return index
    print('Indexing the previous pack for its first changelog comparison.', flush=True)
    run('gh', 'release', 'download', release['tag_name'], '--repo', repository,
        '--pattern', 'PMD-Sprites-Essentials-Complete.zip', '--dir', str(folder))
    path = folder / 'PMD-Sprites-Essentials-Complete.zip'
    metadata = next(asset for asset in previous['assets'] if asset['name'] == path.name)
    if sha256(path) != metadata['sha256']:
        raise ValueError('Previous pack checksum mismatch')
    index = archive_index(path)
    path.unlink()
    return index


def release_changes(current, previous):
    if previous is None:
        return 'First release of the complete pack.\n', 'First release of the complete pack.\n'
    groups = {name: defaultdict(set) for name in ['Added', 'Updated', 'Removed']}
    metadata = set()
    def logical_files(index):
        return {(row['kind'], row['variant'], row['name']) if row['kind'] in ('sprite', 'portrait') else path: row
                for path, row in index['files'].items()}
    old, new = logical_files(previous), logical_files(current)
    for path in sorted(old.keys() | new.keys(), key=str):
        status = 'Added' if path not in old else 'Removed' if path not in new else 'Updated'
        if status == 'Updated' and all(old[path].get(field) == new[path].get(field) for field in ('sha256', 'frames', 'delay')):
            continue
        row = old[path] if status == 'Removed' else new[path]
        if row['kind'] in ('sprite', 'portrait'):
            groups[status][row['variant']].add(row['name'])
        else:
            metadata.add('Artist credits updated.' if row['kind'] == 'credits' else 'License information updated.')
    full, short = [], []
    for title, variants in groups.items():
        if not variants:
            continue
        full += ['### ' + title, '']
        short += ['### ' + title, '']
        for number, (variant, names) in enumerate(sorted(variants.items())):
            labels = sorted(names)
            full.append('- ' + variant + ': ' + ', '.join(labels))
            if number < 4:
                brief = ', '.join(labels[:6]) + (', …' if len(labels) > 6 else '')
                short.append('- ' + variant + ': ' + brief)
        full.append('')
        short.append('')
    for line in sorted(metadata):
        full += [line, '']
        short += [line, '']
    if not full:
        message = ('Source repository updated; converted sprites and portraits are unchanged.'
                   if current['source_commit'] != previous['source_commit']
                   else 'Rebuilt pack. No sprite or portrait changes.')
        return message + '\n', message + '\n'
    return '\n'.join(full).strip() + '\n', '\n'.join(short).strip() + '\n'


def validate(source, output):
    manifest = json.loads((output / 'source-manifest.json').read_text())
    if manifest['errors']:
        raise ValueError('Conversion errors: ' + repr(manifest['errors'][:5]))
    exported, definitions, portraits = set(), 0, 0
    for asset in manifest['assets']:
        folder = output / 'Graphics/Characters/PMD' / asset['id']
        xml = source / 'sprite' / asset['source_path'] / 'AnimData.xml'
        expected = {a.findtext('Name') for a in ET.parse(xml).findall('Anims/Anim')} if xml.exists() else set()
        actual = {sheet['animation'] for sheet in asset['sprites']}
        if actual - {'WalkFull'} != expected or ('Walk' in expected) != ('WalkFull' in actual):
            raise ValueError('Incomplete animation coverage: ' + asset['id'])
        definitions += len(expected)
        for sheet in asset['sprites']:
            path = output / 'Graphics/Characters/PMD' / sheet['file']
            if path in exported:
                raise ValueError('Duplicate export path: ' + str(path))
            exported.add(path)
            with Image.open(path) as image:
                if list(image.size) != sheet['size'] or image.width % (4 * sheet['frames']) or image.height % 4:
                    raise ValueError('Invalid charset grid: ' + str(path))
                image.verify()
        if asset['sprites'] and not (folder / 'credits.txt').is_file():
            raise ValueError('Missing sprite credits: ' + asset['id'])
        for name in asset['portraits']:
            path = output / 'Graphics/Pictures/PMD Portraits' / asset['id'] / name
            exported.add(path)
            with Image.open(path) as image:
                if image.size != (80, 80):
                    raise ValueError('Invalid portrait size: ' + str(path))
                image.verify()
            portraits += 1
        if asset['portraits'] and not (output / 'Graphics/Pictures/PMD Portraits' / asset['id'] / 'credits.txt').is_file():
            raise ValueError('Missing portrait credits: ' + asset['id'])
    source_definitions = sum(len(ET.parse(xml).findall('Anims/Anim')) for xml in (source / 'sprite').rglob('AnimData.xml'))
    source_portraits = sum(1 for _ in (source / 'portrait').rglob('*.png'))
    if definitions != source_definitions or portraits != source_portraits:
        raise ValueError('Some source animations or portraits are absent from the tracker/export')
    if exported != set((output / 'Graphics').rglob('*.png')):
        raise ValueError('Missing or unlisted exported PNG files')
    with (output / 'animation-index.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['variant', 'animation', 'source_animation', 'frames', 'file'])
        for asset in manifest['assets']:
            for sheet in asset['sprites']:
                writer.writerow([asset['id'], sheet['animation'], sheet['source_animation'], sheet['frames'], 'Graphics/Characters/PMD/' + sheet['file']])
    summary = json.loads((output / 'summary.json').read_text())
    summary.update(verified_pngs=len(exported), source_animations=definitions, source_portraits=portraits)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def split_asset(path, limit=LIMIT):
    if path.stat().st_size <= limit:
        return [path]
    pieces = []
    size = path.stat().st_size
    with path.open('rb') as source:
        while source.tell() < size:
            piece = path.with_name(path.name + f'.{len(pieces) + 1:03d}')
            remaining = limit
            with piece.open('wb') as target:
                while remaining and (block := source.read(min(1024 * 1024, remaining))):
                    target.write(block)
                    remaining -= len(block)
            pieces.append(piece)
    path.unlink()
    return pieces


def package(source, output, releases):
    with (output / 'SHA256SUMS.txt').open('w', encoding='utf-8') as stream:
        for path in sorted(output.rglob('*')):
            if path.is_file() and path.name != 'SHA256SUMS.txt':
                stream.write(sha256(path) + '  ' + path.relative_to(output).as_posix() + '\n')
    converted = releases / 'PMD-Sprites-Essentials-Complete.zip'
    with ZipFile(converted, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                archive.write(path, Path(output.name) / path.relative_to(output))
    original = releases / 'SpriteCollab-Original.zip'
    run('git', '-C', str(source), 'archive', '--format=zip', '--prefix=SpriteCollab/', f'--output={original}', 'HEAD')
    tracked = set(subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode().rstrip('\0').split('\0'))
    for path in [converted, original]:
        with ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError('ZIP integrity check failed: ' + path.name)
            if path == original:
                names = {i.filename.removeprefix('SpriteCollab/') for i in archive.infolist() if not i.is_dir()}
                if names != tracked:
                    raise ValueError('Original archive is incomplete')
    assets = split_asset(converted) + split_asset(original)
    return assets, len(tracked)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--no-publish', action='store_true')
    args = parser.parse_args()
    if not args.no_publish and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository or ''):
        parser.error('--repository owner/name is required for publishing')
    upstream_sha = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', 'HEAD'], text=True).strip() if args.source else subprocess.check_output(['git', 'ls-remote', UPSTREAM, 'refs/heads/master'], text=True).split()[0]
    if not re.fullmatch('[0-9a-f]{40}', upstream_sha):
        raise ValueError('Invalid upstream commit')
    converter_hash = fingerprint()
    with tempfile.TemporaryDirectory(prefix='spritecollab-') as temp:
        work = Path(temp)
        previous = latest_build(args.repository, work) if not args.no_publish else None
        if previous and not args.force and previous.get('upstream_commit') == upstream_sha and previous.get('converter_hash') == converter_hash:
            print('SpriteCollab and the converter are unchanged; nothing to build.', flush=True)
            return
        old_index = previous_index(args.repository, previous, work) if previous else None
        source = args.source.resolve() if args.source else work / 'source'
        if not args.source:
            run('git', 'init', str(source))
            run('git', '-C', str(source), 'remote', 'add', 'origin', UPSTREAM)
            run('git', '-C', str(source), 'fetch', '--depth=1', 'origin', upstream_sha)
            run('git', '-C', str(source), 'checkout', '--detach', 'FETCH_HEAD')
        output = work / 'PMD-Sprites-Essentials'
        releases = args.output.resolve() if args.output else work / 'release'
        releases.mkdir(parents=True, exist_ok=True)
        if any(releases.iterdir()):
            raise ValueError('Release output directory must be empty')
        print('Converting SpriteCollab ' + upstream_sha, flush=True)
        run('python', str(ROOT / 'convert.py'), str(source), str(output), '--workers', '4')
        shutil.copy2(ROOT / 'README.md', output / 'README.md')
        summary = validate(source, output)
        print('Comparing exported sprites and portraits with the previous release.', flush=True)
        index = export_index(output)
        full_changes, short_changes = release_changes(index, old_index)
        (output / 'changes.md').write_text(full_changes, encoding='utf-8')
        assets, originals = package(source, output, releases)
        index_path = releases / 'sprite-index.json.gz'
        with gzip.open(index_path, 'wt', encoding='utf-8') as stream:
            json.dump(index, stream, separators=(',', ':'))
        changes_path = releases / 'changes.md'
        changes_path.write_text(full_changes, encoding='utf-8')
        assets += [index_path, changes_path]
        info = {'upstream_repository': UPSTREAM.removesuffix('.git'), 'upstream_commit': upstream_sha,
                'converter_hash': converter_hash, 'converter_commit': os.environ.get('GITHUB_SHA'),
                'created_at': datetime.now(timezone.utc).isoformat(), 'original_files': originals,
                'summary': summary, 'assets': [{'name': path.name, 'size': path.stat().st_size, 'sha256': sha256(path)} for path in assets]}
        (releases / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n')
        with (releases / 'SHA256SUMS.txt').open('w') as stream:
            for path in sorted(releases.iterdir()):
                if path.is_file() and path.name != 'SHA256SUMS.txt':
                    stream.write(sha256(path) + '  ' + path.name + '\n')
        if args.no_publish:
            print('Validated release files: ' + str(releases), flush=True)
            return
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        tag = f'sprites-{timestamp}-{upstream_sha[:8]}'
        notes = work / 'release-notes.md'
        notes.write_text('SpriteCollab sprites, animations and portraits, sorted for Pokémon Essentials.\n\n'
                         + short_changes + '\n'
                         + f'[Full change list](https://github.com/{args.repository}/releases/download/{tag}/changes.md)\n\n'
                         + '`PMD-Sprites-Essentials-Complete.zip` — ready-to-use pack.\n'
                         + '`SpriteCollab-Original.zip` — original files.\n\n'
                         + 'Artist credits and licenses are included.\n', encoding='utf-8')
        run('gh', 'release', 'create', tag, '--repo', args.repository, '--target', os.environ.get('GITHUB_SHA', 'main'), '--draft',
            '--title', 'PMD Sprites — ' + datetime.now(timezone.utc).strftime('%Y-%m-%d'), '--notes-file', str(notes))
        for path in sorted(releases.iterdir()):
            run('gh', 'release', 'upload', tag, str(path), '--repo', args.repository)
        run('gh', 'release', 'edit', tag, '--repo', args.repository, '--draft=false', '--latest')
        print('Published ' + tag, flush=True)


if __name__ == '__main__':
    main()
