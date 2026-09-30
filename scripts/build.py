"""Build a validated, immutable catalog snapshot into a static website."""
from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from html import escape
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import MappingProxyType
import unicodedata
from urllib.parse import urlsplit
import warnings

import imagehash
from PIL import Image, ImageChops, ImageOps
from rapidfuzz.distance import Levenshtein
import xxhash
import yaml


class CatalogError(ValueError):
    """A contribution violates a catalog invariant."""


class UniqueLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise CatalogError('YAML aliases are not supported')
        self._depth += 1
        try:
            if self._depth > 20:
                raise CatalogError('YAML nesting exceeds limit')
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise CatalogError('YAML keys must be unique strings')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class CatalogDocument:
    """Bounded contributor-owned YAML input; never follows file links."""
    MAX_BYTES = 64_000

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise CatalogError(f'{self.path.name}: expected a regular file')
        if self.path.stat().st_size > self.MAX_BYTES:
            raise CatalogError(f'{self.path.name}: metadata too large')
        return yaml.load(self.path.read_text(encoding='utf-8'), Loader=UniqueLoader)


class Field:
    @staticmethod
    def text(value, name, limit=2000, empty=False):
        if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
            raise CatalogError(f'{name}: expected a string (max {limit}, empty allowed: {empty})')
        return value.strip()

    @staticmethod
    def ident(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', value):
            raise CatalogError(f'invalid sticker ID: {value!r}')
        return value


@dataclass(frozen=True)
class Source:
    url: str
    author: str = ''
    license: str = ''

    def __post_init__(self):
        url = Field.text(self.url, 'source url')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or any(c.isspace() for c in url):
            raise CatalogError('source: expected an HTTP(S) URL without credentials')
        object.__setattr__(self, 'url', url)
        object.__setattr__(self, 'author', Field.text(self.author, 'author', 100, True))
        object.__setattr__(self, 'license', Field.text(self.license, 'license', 300, True))

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {'url', 'author', 'license'}:
            raise CatalogError('source: expected url, optional author and license')
        return cls(data.get('url'), data.get('author', ''), data.get('license', ''))

    def public(self):
        return {'url': self.url, 'author': self.author, 'license': self.license}


@dataclass(frozen=True)
class ProcessingPolicy:
    """Contributor intent, in coordinates after EXIF orientation correction."""
    trim: bool = True
    crop: tuple[int, int, int, int] | None = None
    max_edge: int = 960
    explicit_max_edge: bool = False

    def __post_init__(self):
        if type(self.trim) is not bool or type(self.explicit_max_edge) is not bool:
            raise CatalogError('processing trim: expected boolean')
        if type(self.max_edge) is not int or not 1 <= self.max_edge <= 4096:
            raise CatalogError('processing max_edge: expected integer from 1 to 4096')
        if self.crop is not None:
            if (not isinstance(self.crop, tuple) or len(self.crop) != 4
                    or any(type(v) is not int or v < 0 for v in self.crop)
                    or self.crop[0] >= self.crop[2] or self.crop[1] >= self.crop[3]):
                raise CatalogError('processing crop: expected [left, top, right, bottom] with positive area')

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {'trim', 'crop', 'max_edge'}:
            raise CatalogError('processing: expected trim, crop and/or max_edge')
        crop = data.get('crop')
        if 'crop' in data and (not isinstance(crop, list) or len(crop) != 4):
            raise CatalogError('processing crop: expected four integer coordinates')
        return cls(data.get('trim', True), tuple(crop) if crop is not None else None,
                   data.get('max_edge', 960), 'max_edge' in data)

    def public(self):
        return {'trim': self.trim, 'crop': list(self.crop) if self.crop else None,
                'max_edge': self.max_edge, 'explicit_max_edge': self.explicit_max_edge}


@dataclass(frozen=True)
class Metadata:
    title: str
    text: str
    tags: tuple[str, ...]
    sources: tuple[Source, ...]
    processing: ProcessingPolicy = ProcessingPolicy()

    def __post_init__(self):
        object.__setattr__(self, 'title', Field.text(self.title, 'title', 100))
        object.__setattr__(self, 'text', Field.text(self.text, 'text', empty=True))
        if not isinstance(self.tags, tuple) or len(self.tags) > 20:
            raise CatalogError('tags: expected an immutable tuple (max 20)')
        if not isinstance(self.sources, tuple) or not 1 <= len(self.sources) <= 30 or not all(isinstance(source, Source) for source in self.sources):
            raise CatalogError('sources: expected 1–30 Source values')
        if not isinstance(self.processing, ProcessingPolicy):
            raise CatalogError('processing: expected a ProcessingPolicy value')
        object.__setattr__(self, 'tags', tuple(sorted({Field.text(tag, 'tag', 30) for tag in self.tags})))
        object.__setattr__(self, 'sources', tuple(dict.fromkeys(self.sources)))

    @classmethod
    def read(cls, path):
        data = CatalogDocument(path).read()
        required = {'title', 'text', 'tags', 'sources'}
        if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {'processing'}:
            raise CatalogError('metadata: expected title, text, tags, sources and optional processing')
        tags, sources = data['tags'], data['sources']
        if not isinstance(tags, list) or len(tags) > 20:
            raise CatalogError('tags: expected list (max 20)')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 30:
            raise CatalogError('sources: expected 1–30 source records')
        return cls(Field.text(data['title'], 'title', 100), Field.text(data['text'], 'text', empty=True),
                   tuple(sorted({Field.text(tag, 'tag', 30) for tag in tags})),
                   tuple(dict.fromkeys(Source.parse(source) for source in sources)),
                   ProcessingPolicy.parse(data.get('processing', {})))

    @property
    def normalized_text(self):
        return ''.join(c for c in unicodedata.normalize('NFKC', self.text).casefold()
                       if not c.isspace() and not unicodedata.category(c).startswith('P'))

    def public(self):
        return {'title': self.title, 'text': self.text, 'tags': list(self.tags),
                'sources': [source.public() for source in self.sources]}

    def preserves_sources(self, previous):
        # Preserve attribution and license, not only URLs.
        return set(previous.sources) <= set(self.sources)


@dataclass(frozen=True)
class ProcessedImage:
    raw: bytes
    original_size: tuple[int, int]
    size: tuple[int, int]
    crop: tuple[int, int, int, int]
    warnings: tuple[str, ...]
    status: str
    cache_key: str

    def report(self):
        return {'status': self.status, 'originalSize': list(self.original_size),
                'outputSize': list(self.size), 'crop': list(self.crop),
                'warnings': list(self.warnings)}

    def fingerprint(self):
        with Image.open(BytesIO(self.raw)) as im:
            frame = ImageOps.exif_transpose(im).convert('RGBA')
            white = Image.new('RGBA', frame.size, 'white')
            white.alpha_composite(frame)
            return str(imagehash.phash(white.convert('RGB')))

    def preview(self):
        return self.preview_of(self.raw)

    @staticmethod
    def preview_of(raw):
        with Image.open(BytesIO(raw)) as im:
            thumb = ImageOps.exif_transpose(im).convert('RGBA')
            thumb.thumbnail((480, 480), Image.Resampling.LANCZOS)
            output = BytesIO()
            thumb.save(output, 'WEBP', quality=85, method=6)
            return output.getvalue()


class ImageProcessor:
    """Conservative edge trimming; explicit crop and bounded downscaling only."""
    RECIPE = 'display-v1-preview-v1'
    EDGE_TOLERANCE = 8
    WHITE_THRESHOLD = 245

    def __init__(self, policy):
        if not isinstance(policy, ProcessingPolicy):
            raise CatalogError('image processor requires a ProcessingPolicy')
        self._policy = policy

    def process(self, raw, animated):
        recipe = json.dumps({'recipe': self.RECIPE, 'policy': self._policy.public()}, sort_keys=True)
        key = hashlib.sha256(raw + recipe.encode('utf-8')).hexdigest()
        with Image.open(BytesIO(raw)) as opened:
            im = ImageOps.exif_transpose(opened).convert('RGBA')
        original_size = im.size
        box = (0, 0, im.width, im.height)
        notes = []
        if animated:
            if self._policy.crop is not None or self._policy.explicit_max_edge:
                raise CatalogError('animated images do not support explicit crop or max_edge')
            return ProcessedImage(raw, original_size, original_size, box,
                                  ('Animation preserved; automatic trimming and resizing skipped.',),
                                  'preserved-animation', key)
        if self._policy.crop is not None:
            left, top, right, bottom = self._policy.crop
            if right > im.width or bottom > im.height:
                raise CatalogError('processing crop exceeds EXIF-oriented image bounds')
            box = self._policy.crop
            im = im.crop(box)
        if self._policy.trim:
            trimmed, relative, warning = self._trim(im)
            if warning:
                notes.append(warning)
            box = (box[0] + relative[0], box[1] + relative[1],
                   box[0] + relative[2], box[1] + relative[3])
            im = trimmed
        # Pillow thumbnail never enlarges an image, including after an explicit crop.
        im.thumbnail((self._policy.max_edge, self._policy.max_edge), Image.Resampling.LANCZOS)
        status = 'processed' if box != (0, 0, *original_size) or im.size != original_size else 'unchanged'
        output = BytesIO()
        im.save(output, 'WEBP', lossless=True, exact=True, method=6)
        return ProcessedImage(output.getvalue(), original_size, im.size, box, tuple(notes), status, key)

    def _trim(self, im):
        full = (0, 0, im.width, im.height)
        box = list(full)
        for side in range(4):
            current = im.crop(tuple(box))
            strips = ((0, 0, current.width, 1), (0, current.height - 1, current.width, current.height),
                      (0, 0, 1, current.height), (current.width - 1, 0, current.width, current.height))
            extrema = current.crop(strips[side]).getextrema()
            if extrema[3] != (255, 255) or any(high - low > self.EDGE_TOLERANCE for low, high in extrema[:3]):
                continue
            color = tuple((low + high) // 2 for low, high in extrema[:3])
            if min(color) >= self.WHITE_THRESHOLD:
                continue
            difference = ImageChops.difference(current.convert('RGB'), Image.new('RGB', current.size, color))
            channels = difference.split()
            mask = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
            mask = mask.point(lambda value: 255 if value > self.EDGE_TOLERANCE else 0)
            # Transparent content must not disappear just because its hidden RGB matches the edge.
            opacity = current.getchannel('A').point(lambda value: 255 if value < 255 else 0)
            bounds = ImageChops.lighter(mask, opacity).getbbox()
            if bounds is None:
                return im, full, 'Uniform image retained; no safe content boundary found.'
            if side == 0:
                box[1] += bounds[1]
            elif side == 1:
                box[3] = box[1] + bounds[3]
            elif side == 2:
                box[0] += bounds[0]
            else:
                box[2] = box[0] + bounds[2]
        width, height = box[2] - box[0], box[3] - box[1]
        if width * height < im.width * im.height / 4 or width < im.width / 4 or height < im.height / 4:
            return im, full, 'Automatic trim would remove too much content; retained for manual review.'
        return im.crop(tuple(box)), tuple(box), None


@dataclass(frozen=True)
class ImageAsset:
    """Validated original bytes; later rendering cannot observe a changed source file."""
    filename: str
    raw: bytes
    digest: str
    phash: str
    width: int
    height: int
    animated: bool
    processed: ProcessedImage
    FORMATS = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP', '.gif': 'GIF', '.apng': 'PNG'}
    MAX_BYTES = 20 * 1024 * 1024
    MAX_PIXELS = 25_000_000
    MAX_FRAME_PIXELS = 150_000_000

    @classmethod
    def read(cls, path, policy=ProcessingPolicy()):
        if path.stat().st_size > cls.MAX_BYTES:
            raise CatalogError(f'{path.parent.name}: original exceeds 20 MiB')
        raw = path.read_bytes()
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as im:
                if im.format != cls.FORMATS[path.suffix]:
                    raise CatalogError('image format does not match extension')
                frames = getattr(im, 'n_frames', 1)
                if im.width * im.height > cls.MAX_PIXELS or frames > 500 or im.width * im.height * frames > cls.MAX_FRAME_PIXELS:
                    raise CatalogError('image or animation exceeds processing limits')
                for frame in range(frames):
                    im.seek(frame)
                    if im.width * im.height > cls.MAX_PIXELS:
                        raise CatalogError('animation frame exceeds processing limits')
                    im.load()
                im.seek(0)
                oriented = ImageOps.exif_transpose(im).convert('RGBA')
                width, height = oriented.size
        processed = ImageProcessor(policy).process(raw, frames > 1)
        return cls(path.name, raw, xxhash.xxh3_128_hexdigest(raw), processed.fingerprint(), width, height, frames > 1, processed)

    def equals(self, other):
        return self.digest == other.digest and len(self.raw) == len(other.raw) and self.raw == other.raw

    def publish(self, output):
        asset_id = hashlib.sha256(self.raw).hexdigest()
        relative = Path('media') / asset_id
        directory = output / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / self.filename).write_bytes(self.raw)
        derived = Path('media') / self.processed.cache_key
        derived_directory = output / derived
        derived_directory.mkdir(parents=True, exist_ok=True)
        display = relative / self.filename if self.animated else derived / 'display.webp'
        if not self.animated:
            (output / display).write_bytes(self.processed.raw)
        (derived_directory / 'preview.webp').write_bytes(self.processed.preview())
        return {'original': '/' + (relative / self.filename).as_posix(),
                'display': '/' + display.as_posix(),
                'preview': '/' + (derived / 'preview.webp').as_posix(), 'bytes': len(self.raw),
                'displayBytes': len(self.processed.raw),
                'width': self.processed.size[0], 'height': self.processed.size[1], 'animated': self.animated,
                'processingReport': self.processed.report()}


@dataclass(frozen=True)
class Sticker:
    ident: str
    metadata: Metadata
    image: ImageAsset

    def __post_init__(self):
        Field.ident(self.ident)
        if not isinstance(self.metadata, Metadata) or not isinstance(self.image, ImageAsset):
            raise CatalogError('sticker: expected Metadata and ImageAsset values')

    @classmethod
    def read(cls, directory):
        Field.ident(directory.name)
        if directory.is_symlink() or not directory.is_dir():
            raise CatalogError(f'invalid sticker directory: {directory.name}')
        files = list(directory.iterdir())
        if any(p.is_symlink() or not p.is_file() for p in files):
            raise CatalogError(f'{directory.name}: only regular files are allowed')
        originals = [p for p in files if p.stem == 'original' and p.suffix in ImageAsset.FORMATS]
        if len(originals) != 1 or {p.name for p in files} != {'metadata.yaml', originals[0].name}:
            raise CatalogError(f'{directory.name}: expected metadata.yaml and one original image')
        metadata = Metadata.read(directory / 'metadata.yaml')
        return cls(directory.name, metadata, ImageAsset.read(originals[0], metadata.processing))

    def changes_from(self, previous):
        changes = []
        if not self.image.equals(previous.image):
            changes.append(Mutation('REPLACE', self.ident))
        if (self.metadata.title, self.metadata.text, self.metadata.tags) != (previous.metadata.title, previous.metadata.text, previous.metadata.tags):
            changes.append(Mutation('UPDATE', self.ident))
        if set(self.metadata.sources) != set(previous.metadata.sources):
            changes.append(Mutation('SOURCES', self.ident))
        if self.metadata.processing != previous.metadata.processing:
            changes.append(Mutation('REPROCESS', self.ident))
        return changes

    def publish(self, output):
        return {'id': self.ident, **self.metadata.public(), **self.image.publish(output)}


class RedirectTable:
    def __init__(self, data, entries):
        if not isinstance(data, Mapping):
            raise CatalogError('redirects.yaml must be a mapping')
        for old, target in data.items():
            Field.ident(old)
            Field.ident(target)
            if old in entries:
                raise CatalogError(f'{old}: redirect ID still has a sticker directory')
        resolved = {}
        for old, target in data.items():
            visited = {old}
            while target in data:
                if target in visited:
                    raise CatalogError(f'{old}: redirect cycle')
                visited.add(target)
                target = data[target]
            if target not in entries:
                raise CatalogError(f'{old}: missing redirect target {target}')
            resolved[old] = target
        self.resolved = MappingProxyType(resolved)


@dataclass(frozen=True, init=False)
class CatalogSnapshot:
    """A complete valid final state, including flattened redirects."""
    entries: MappingProxyType
    redirects: MappingProxyType
    MAX_ITEMS = 2500
    MAX_TOTAL_BYTES = 256 * 1024 * 1024

    def __init__(self, entries=None, redirects=None):
        if entries is not None and not isinstance(entries, Mapping):
            raise CatalogError('catalog entries must be a mapping')
        entries = dict(entries) if entries is not None else {}
        if len(entries) > self.MAX_ITEMS:
            raise CatalogError('catalog exceeds item limit')
        for ident, sticker in entries.items():
            Field.ident(ident)
            if not isinstance(sticker, Sticker) or ident != sticker.ident:
                raise CatalogError('catalog keys must match their Sticker identities')
        if sum(len(sticker.image.raw) for sticker in entries.values()) > self.MAX_TOTAL_BYTES:
            raise CatalogError('catalog exceeds total original byte limit')
        object.__setattr__(self, 'entries', MappingProxyType(entries))
        object.__setattr__(self, 'redirects', RedirectTable({} if redirects is None else redirects, self.entries).resolved)

    @classmethod
    def read(cls, root):
        root = Path(root)
        folder = root / 'stickers'
        if folder.is_symlink() or not folder.is_dir():
            raise CatalogError('stickers must be a real directory')
        entries, total = {}, 0
        directories = sorted(folder.iterdir())
        if len(directories) > cls.MAX_ITEMS + 1:
            raise CatalogError('catalog exceeds item limit')
        for directory in directories:
            if directory.name == '.gitkeep' and directory.is_file() and not directory.is_symlink():
                continue
            sticker = Sticker.read(directory)
            total += len(sticker.image.raw)
            if total > cls.MAX_TOTAL_BYTES:
                raise CatalogError('catalog exceeds total original byte limit')
            entries[sticker.ident] = sticker
        path = root / 'redirects.yaml'
        links = CatalogDocument(path).read() if path.exists() or path.is_symlink() else {}
        if not isinstance(links, dict):
            raise CatalogError('redirects.yaml must be a mapping')
        return cls(entries, links)

    def publish(self, output):
        manifest = {'version': 1, 'stickers': [s.publish(output) for s in self.entries.values()],
                    'redirects': dict(self.redirects)}
        (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')


@dataclass(frozen=True)
class Mutation:
    op: str
    ident: str
    target: str | None = None

    def public(self):
        value = {'op': self.op, 'id': self.ident}
        if self.op in {'MERGE', 'REDIRECT'}:
            value['target'] = self.target
        return value


class ChangeSet:
    """All mutations between two snapshots; merge provenance is a cross-entry invariant."""
    def __init__(self, before, after):
        changes = []
        for ident in sorted(before.entries.keys() | after.entries.keys()):
            if ident not in before.entries:
                changes.append(Mutation('ADD', ident))
            elif ident not in after.entries:
                target = after.redirects.get(ident)
                if target and not after.entries[target].metadata.preserves_sources(before.entries[ident].metadata):
                    raise CatalogError(f'{ident}: merge must preserve source records on {target}')
                if target in before.entries and not after.entries[target].metadata.preserves_sources(before.entries[target].metadata):
                    raise CatalogError(f'{ident}: merge must preserve source records of survivor {target}; correct sources separately')
                changes.append(Mutation('MERGE' if target else 'DELETE', ident, target))
            else:
                changes.extend(after.entries[ident].changes_from(before.entries[ident]))
        for ident in sorted(before.redirects.keys() | after.redirects.keys()):
            if before.redirects.get(ident) != after.redirects.get(ident):
                changes.append(Mutation('REDIRECT', ident, after.redirects.get(ident)))
        self.mutations = tuple(changes)

    @property
    def changed_images(self):
        return frozenset(m.ident for m in self.mutations if m.op in {'ADD', 'REPLACE', 'REPROCESS'})


class DuplicateReview:
    """Exact equality blocks; visual/text similarity only assists human review."""
    MAX_REPORTED_PAIRS = 10_000

    def __init__(self, snapshot):
        self.exact, self.similar = [], []
        self.exact_omitted = self.similar_omitted = 0
        items = list(snapshot.entries.values())
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                ids = [left.ident, right.ident]
                if left.image.equals(right.image):
                    if len(self.exact) < self.MAX_REPORTED_PAIRS:
                        self.exact.append({'ids': ids})
                    else:
                        self.exact_omitted += 1
                    continue
                distance = (int(left.image.phash, 16) ^ int(right.image.phash, 16)).bit_count()
                if distance > 8:
                    continue
                a, b = left.metadata.normalized_text, right.metadata.normalized_text
                similarity = Levenshtein.normalized_similarity(a, b)
                review = a and b and (a == b or (min(len(a), len(b)) >= 8 and similarity >= .9))
                if len(self.similar) >= self.MAX_REPORTED_PAIRS:
                    self.similar_omitted += 1
                    continue
                self.similar.append({'ids': ids, 'imageDistance': distance,
                    'textDistance': Levenshtein.distance(a, b), 'textSimilarity': round(similarity, 3),
                    'status': 'review' if review else 'keep-variant',
                    'texts': [left.metadata.text, right.metadata.text]})


class OcrAdvisor:
    def suggest(self, snapshot, changes):
        if not shutil.which('tesseract'):
            return {'status': 'unavailable', 'reason': 'Tesseract is not installed'}
        suggestions = {}
        for ident in sorted(changes.changed_images):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'frame.png'
                with Image.open(BytesIO(snapshot.entries[ident].image.processed.raw)) as im:
                    ImageOps.exif_transpose(im).convert('RGB').save(path)
                try:
                    proc = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'chi_sim+chi_tra+eng', '--psm', '11'], capture_output=True, timeout=30)
                    suggestions[ident] = {'text': proc.stdout.decode('utf-8', errors='replace').strip(), 'ok': proc.returncode == 0}
                except subprocess.TimeoutExpired:
                    suggestions[ident] = {'text': '', 'ok': False}
        return suggestions


class ReviewReport:
    def __init__(self, changes, duplicates, count, ocr_suggestions=None, processing_stickers=()):
        self._count = count
        self._stickers = tuple(processing_stickers)
        self._data = deepcopy({'mutations': [m.public() for m in changes.mutations],
                     'exactDuplicates': duplicates.exact, 'similarPairs': duplicates.similar,
                     'exactPairsOmitted': duplicates.exact_omitted, 'similarPairsOmitted': duplicates.similar_omitted,
                     'processing': {s.ident: s.image.processed.report() for s in self._stickers}})
        if ocr_suggestions is not None:
            self._data['ocrSuggestions'] = deepcopy(ocr_suggestions)

    def snapshot(self):
        return deepcopy(self._data)

    def write(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'report.json').write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding='utf-8')
        lines = ['# Catalog review', '', f'{self._count} stickers · {len(self._data["mutations"])} mutations', '', '## Mutations', '']
        lines += [f"- `{m['op']}` `{m['id']}`" + (f" → `{m['target']}`" if m.get('target') else '') for m in self._data['mutations']] or ['No catalog changes.']
        lines += ['', '## Duplicate review', '', f"Exact duplicates: {len(self._data['exactDuplicates'])} (blocking). Similar pairs: {len(self._data['similarPairs'])} (advisory)."]
        for pair in self._data['exactDuplicates']:
            lines.append(f"- **EXACT**: {' / '.join(pair['ids'])}")
        for pair in self._data['similarPairs']:
            lines.append(f"- `{pair['status']}`: {' / '.join(pair['ids'])}; pHash distance {pair['imageDistance']}; text distance {pair['textDistance']}")
        lines += ['', f"Additional omitted pairs: {self._data['exactPairsOmitted']} exact, {self._data['similarPairsOmitted']} similar."]
        lines += ['', '## Image preprocessing', '', 'Open review.html from the report artifact for offline before/after previews.']
        for ident, report in self._data['processing'].items():
            lines.append(f"- `{ident}`: {report['originalSize']} → {report['outputSize']}; crop {report['crop']}; {report['status']}")
            lines.extend(f'  - {warning}' for warning in report['warnings'])
        lines += ['', 'Full captions and OCR suggestions (if enabled) are in report.json. Original text is never overwritten.']
        (directory / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self._write_gallery(directory)
        (directory / 'error.txt').unlink(missing_ok=True)

    def _write_gallery(self, directory):
        parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
                 '<meta name="viewport" content="width=device-width,initial-scale=1">',
                 '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">',
                 '<title>预处理审核</title><style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:1rem;background:#f4f7fb}',
                 'article{background:white;margin:1rem 0;padding:1rem;border-radius:1rem}.images{display:flex;flex-wrap:wrap;gap:1rem}',
                 'figure{margin:0;flex:1;min-width:240px}img{max-width:100%;max-height:360px;object-fit:contain}pre{white-space:pre-wrap}a{overflow-wrap:anywhere}</style>',
                 '<h1>图片预处理审核</h1><p>仅列新增、替换或重新处理的图片。缩略图供比较；动画仅展示首帧，完整动画仍保留。</p>']
        for sticker in self._stickers:
            report = sticker.image.processed.report()
            parts.append(f'<article><h2>{escape(sticker.ident)} · {escape(sticker.metadata.title)}</h2><div class="images">')
            previews = (('投稿原图', ProcessedImage.preview_of(sticker.image.raw)),
                        ('处理结果', sticker.image.processed.preview()))
            for label, raw in previews:
                data = base64.b64encode(raw).decode('ascii')
                parts.append(f'<figure><figcaption>{label}</figcaption><img alt="{label}" src="data:image/webp;base64,{data}"></figure>')
            parts.append('</div><pre>' + escape(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre>')
            parts.append('<h3>图片文字</h3><pre>' + escape(sticker.metadata.text or '（无字）') + '</pre><h3>出处</h3><ul>')
            for source in sticker.metadata.sources:
                parts.append(f'<li><a href="{escape(source.url, quote=True)}" rel="noreferrer">{escape(source.url)}</a> '
                             + escape(' · '.join(filter(None, (source.author, source.license)))) + '</li>')
            parts.append('</ul></article>')
        parts.append('</html>')
        (directory / 'review.html').write_text('\n'.join(parts), encoding='utf-8')


class BuildDestination:
    """Replace only an empty directory or this builder's explicitly owned output."""
    MARKER = '.catalog-build.json'

    def __init__(self, path, root):
        supplied = Path(path).absolute()
        if supplied.is_symlink() or any(p.is_symlink() for p in supplied.parents):
            raise CatalogError('output must not traverse symlinks')
        self.path, self.root = supplied.resolve(), root.resolve()
        self.owner = {'builder': 'deepseek-chan-v1', 'root': str(self.root)}
        if self.root == self.path or self.root.is_relative_to(self.path) or self.path.is_relative_to(self.root / 'stickers'):
            raise CatalogError('output overlaps catalog source')
        self.validate()

    def validate(self):
        if not self.path.exists():
            return
        if not self.path.is_dir() or self.path.is_symlink():
            raise CatalogError('output must be a real directory')
        if any(p.is_symlink() for p in self.path.rglob('*')):
            raise CatalogError('output contains symlinks')
        if any(self.path.iterdir()):
            marker = self.path / self.MARKER
            try:
                owned = json.loads(marker.read_text(encoding='utf-8')) == self.owner
            except (OSError, ValueError):
                owned = False
            if not owned:
                raise CatalogError('output is not an owned build directory; choose a fresh directory')

    def publish(self, frontend, snapshot):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # All deletion targets are private temporary directories under the checked parent.
        with tempfile.TemporaryDirectory(prefix='.catalog-stage-', dir=self.path.parent) as temporary:
            staging = Path(temporary) / 'site'
            shutil.copytree(frontend, staging)
            snapshot.publish(staging)
            (staging / self.MARKER).write_text(json.dumps(self.owner), encoding='utf-8')
            (staging / '404.html').write_text('<!doctype html><meta charset="utf-8"><title>没有找到</title><p>这个地址不存在。<a href="/">回到表情墙</a></p>', encoding='utf-8')
            self.validate()
            previous = Path(temporary) / 'previous'
            if self.path.exists():
                self.path.rename(previous)
            try:
                staging.rename(self.path)
            except OSError:
                if previous.exists():
                    previous.rename(self.path)
                raise


class CatalogBuilder:
    """Coordinates snapshot validation, review, and atomic static publication."""
    def __init__(self, root, output, base=None, report_dir=None, ocr=False):
        self.root = Path(root).resolve()
        self.destination = BuildDestination(output, self.root)
        self.base = Path(base).resolve() if base else None
        self.report_dir = Path(report_dir or self.root / 'reports').absolute()
        self.ocr = ocr
        if self.report_dir.is_symlink() or any(p.is_symlink() for p in self.report_dir.parents) or any((self.report_dir / name).is_symlink() for name in ('report.json', 'summary.md', 'error.txt', 'review.html')):
            raise CatalogError('report directory must not traverse symlinks')
        if self.report_dir == self.root or self.report_dir.is_relative_to(self.root / 'stickers') or self.report_dir.is_relative_to(self.destination.path) or self.destination.path.is_relative_to(self.report_dir):
            raise CatalogError('report directory overlaps source or output')

    def build(self):
        snapshot = CatalogSnapshot.read(self.root)
        before = CatalogSnapshot.read(self.base) if self.base else CatalogSnapshot()
        changes = ChangeSet(before, snapshot)
        duplicates = DuplicateReview(snapshot)
        suggestions = OcrAdvisor().suggest(snapshot, changes) if self.ocr else None
        report = ReviewReport(changes, duplicates, len(snapshot.entries), suggestions,
                              (snapshot.entries[ident] for ident in sorted(changes.changed_images)))
        report.write(self.report_dir)
        if duplicates.exact:
            raise CatalogError('exact duplicate images found; see report.json')
        frontend = Path(__file__).resolve().parent.parent / 'web'
        self.destination.publish(frontend, snapshot)
        return report.snapshot()


class CatalogCommand:
    def run(self, argv=None):
        parser = argparse.ArgumentParser()
        parser.add_argument('--root', type=Path, default=Path('.'))
        parser.add_argument('--output', type=Path, default=Path('dist'))
        parser.add_argument('--base', type=Path)
        parser.add_argument('--report-dir', type=Path, default=Path('reports'))
        parser.add_argument('--ocr', action='store_true')
        args = parser.parse_args(argv)
        builder = None
        try:
            builder = CatalogBuilder(args.root, args.output, args.base, args.report_dir, args.ocr)
            report = builder.build()
            print(f"Built catalog: {len(report['mutations'])} mutations; {len(report['similarPairs'])} advisory pairs")
        except (CatalogError, OSError, ValueError, yaml.YAMLError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            if builder is not None:
                builder.report_dir.mkdir(parents=True, exist_ok=True)
                (builder.report_dir / 'error.txt').write_text(str(exc), encoding='utf-8')
            print(f'Catalog validation failed: {exc}', file=sys.stderr)
            return 1
        return 0


if __name__ == '__main__':
    sys.exit(CatalogCommand().run())
