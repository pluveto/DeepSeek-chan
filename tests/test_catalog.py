import importlib.util
import json
from pathlib import Path
import shutil
import sys

from PIL import Image
import pytest
import yaml

spec = importlib.util.spec_from_file_location('catalog', Path(__file__).parents[1] / 'scripts/build.py')
catalog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = catalog
spec.loader.exec_module(catalog)


def build(*args, **kwargs):
    return catalog.CatalogBuilder(*args, **kwargs).build()


def duplicate_pairs(snapshot):
    review = catalog.DuplicateReview(snapshot)
    return review.exact, review.similar


def root(tmp_path, name='catalog'):
    result = tmp_path / name
    (result / 'stickers').mkdir(parents=True)
    (result / 'redirects.yaml').write_text('{}', encoding='utf-8')
    return result


def add(root, ident, color='blue', text='吃饭', sources=None):
    directory = root / 'stickers' / ident
    directory.mkdir(exist_ok=True)
    Image.new('RGB', (50, 50), color).save(directory / 'original.png')
    data = {'title': ident, 'text': text, 'tags': ['干饭'], 'sources': sources or [{'url': f'https://example.org/{ident}'}]}
    (directory / 'metadata.yaml').write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    return directory


def change(directory, **kwargs):
    path = directory / 'metadata.yaml'
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    path.write_text(yaml.safe_dump(data | kwargs, allow_unicode=True), encoding='utf-8')


def test_mixed_mutations_and_source_preservation(tmp_path):
    base = root(tmp_path, 'base')
    add(base, 'edit', 'red')
    add(base, 'replace', 'green')
    add(base, 'delete', 'yellow')
    add(base, 'merge', 'purple')
    add(base, 'survivor', 'blue')
    candidate = tmp_path / 'candidate'
    shutil.copytree(base, candidate)
    add(candidate, 'new', 'pink')
    change(candidate / 'stickers/edit', text='开饭了', tags=['开心'])
    Image.new('RGB', (65, 60), 'orange').save(candidate / 'stickers/replace/original.png')
    shutil.rmtree(candidate / 'stickers/delete')
    shutil.rmtree(candidate / 'stickers/merge')
    change(candidate / 'stickers/survivor', sources=[{'url': 'https://example.org/survivor'}, {'url': 'https://example.org/merge'}])
    (candidate / 'redirects.yaml').write_text('merge: survivor', encoding='utf-8')
    report = build(candidate, tmp_path / 'dist', base, tmp_path / 'reports')
    assert {'ADD', 'UPDATE', 'REPLACE', 'DELETE', 'MERGE', 'SOURCES', 'REDIRECT'} == {c['op'] for c in report['mutations']}
    manifest = json.loads((tmp_path / 'dist/manifest.json').read_text(encoding='utf-8'))
    assert manifest['redirects'] == {'merge': 'survivor'}
    assert {s['id'] for s in manifest['stickers']} == {'edit', 'replace', 'survivor', 'new'}
    assert not list((tmp_path / 'dist').rglob('metadata.yaml'))


def test_merge_cannot_discard_sources(tmp_path):
    base = root(tmp_path, 'base')
    add(base, 'old', 'red')
    add(base, 'new', 'blue')
    after = tmp_path / 'after'
    shutil.copytree(base, after)
    shutil.rmtree(after / 'stickers/old')
    (after / 'redirects.yaml').write_text('old: new')
    with pytest.raises(catalog.CatalogError, match='preserve source'):
        build(after, tmp_path / 'dist', base)


def test_exact_duplicates_block_entire_build(tmp_path):
    source = root(tmp_path)
    add(source, 'a')
    add(source, 'b', text='不一样的标签也不能掩盖同文件')
    with pytest.raises(catalog.CatalogError, match='exact duplicate'):
        build(source, tmp_path / 'dist')
    assert not (tmp_path / 'dist/manifest.json').exists()


def test_short_different_captions_are_variants(tmp_path):
    source = root(tmp_path)
    add(source, 'a', 'blue', '吃了')
    add(source, 'b', 'red', '没吃')
    entries = catalog.CatalogSnapshot.read(source)
    exact, pairs = duplicate_pairs(entries)
    assert not exact
    assert pairs[0]['status'] == 'keep-variant'
    assert pairs[0]['textDistance'] == 2


def test_equal_long_captions_are_advisory_only(tmp_path):
    source = root(tmp_path)
    add(source, 'a', 'blue', '今天也要一起好好吃饭')
    add(source, 'b', 'red', '今天也要一起好好吃饭')
    report = build(source, tmp_path / 'dist')
    assert report['similarPairs'][0]['status'] == 'review'


@pytest.mark.parametrize('text', ['', '吃了'])
def test_empty_caption_never_auto_merges(tmp_path, text):
    source = root(tmp_path)
    add(source, 'a', 'blue', '')
    add(source, 'b', 'red', text)
    assert duplicate_pairs(catalog.CatalogSnapshot.read(source))[1][0]['status'] == 'keep-variant'


@pytest.mark.parametrize('redirects,message', [('old: missing', 'missing'), ('a: b\nb: a', 'cycle'), ('live: other', 'still has')])
def test_bad_redirects(tmp_path, redirects, message):
    source = root(tmp_path)
    add(source, 'live')
    (source / 'redirects.yaml').write_text(redirects)
    with pytest.raises(catalog.CatalogError, match=message):
        dict(catalog.CatalogSnapshot.read(source).redirects)


def test_redirect_chains_flatten(tmp_path):
    source = root(tmp_path)
    add(source, 'live')
    (source / 'redirects.yaml').write_text('old: older\nolder: live')
    assert dict(catalog.CatalogSnapshot.read(source).redirects) == {'old': 'live', 'older': 'live'}


def test_animated_original_is_unchanged(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'animated')
    (directory / 'original.png').unlink()
    im = Image.new('RGB', (50, 50), 'blue')
    im.save(directory / 'original.gif', save_all=True, append_images=[Image.new('RGB', (50, 50), 'red')], duration=200, loop=0)
    build(source, tmp_path / 'dist')
    m = json.loads((tmp_path / 'dist/manifest.json').read_text(encoding='utf-8'))['stickers'][0]
    assert m['animated']
    assert (tmp_path / 'dist' / m['original'].lstrip('/')).read_bytes() == (directory / 'original.gif').read_bytes()


def test_hash_collision_does_not_merge(tmp_path, monkeypatch):
    source = root(tmp_path)
    add(source, 'a', 'red')
    add(source, 'b', 'blue')
    monkeypatch.setattr(catalog.xxhash, 'xxh3_128_hexdigest', lambda _: 'same')
    assert duplicate_pairs(catalog.CatalogSnapshot.read(source))[0] == []


def test_noop_is_deterministic(tmp_path):
    source = root(tmp_path)
    add(source, 'a')
    report = build(source, tmp_path / 'out-a', source)
    build(source, tmp_path / 'out-b', source)
    assert not report['mutations']
    assert (tmp_path / 'out-a/manifest.json').read_bytes() == (tmp_path / 'out-b/manifest.json').read_bytes()


def test_symlink_and_yaml_duplicates_rejected(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'a')
    with (directory / 'metadata.yaml').open('a', encoding='utf-8') as f:
        f.write('\ntitle: duplicated\n')
    with pytest.raises(catalog.CatalogError, match='unique'):
        catalog.CatalogSnapshot.read(source)


def test_symlink_rejected(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'a')
    original = directory / 'original.png'
    target = tmp_path / 'secret.png'
    original.rename(target)
    try:
        original.symlink_to(target)
    except OSError:
        pytest.skip('symlink privileges unavailable')
    with pytest.raises(catalog.CatalogError, match='regular files'):
        catalog.CatalogSnapshot.read(source)


def test_missing_text_and_unsafe_source_rejected(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'a')
    change(directory, text=None)
    with pytest.raises(catalog.CatalogError, match='text'):
        catalog.CatalogSnapshot.read(source)
    change(directory, text='', sources=[{'url': 'javascript:alert(1)'}])
    with pytest.raises(catalog.CatalogError, match='HTTP'):
        catalog.CatalogSnapshot.read(source)


def test_content_address_changes_on_replace(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'a')
    build(source, tmp_path / 'first')
    Image.new('RGB', (50, 50), 'pink').save(directory / 'original.png')
    build(source, tmp_path / 'second')
    paths = [json.loads((tmp_path / folder / 'manifest.json').read_text(encoding='utf-8'))['stickers'][0]['original'] for folder in ['first', 'second']]
    assert paths[0] != paths[1]


def test_empty_catalog_and_empty_sticker_directory(tmp_path):
    source = root(tmp_path)
    build(source, tmp_path / 'dist')
    manifest = json.loads((tmp_path / 'dist/manifest.json').read_text(encoding='utf-8'))
    assert manifest['stickers'] == []
    (source / 'stickers/broken').mkdir()
    with pytest.raises(catalog.CatalogError, match='expected metadata'):
        build(source, tmp_path / 'dist')


def test_repeated_build_replaces_owned_output_and_removes_deleted_assets(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'a')
    output = tmp_path / 'dist'
    build(source, output)
    old = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))['stickers'][0]['original']
    Image.new('RGB', (50, 50), 'pink').save(directory / 'original.png')
    build(source, output)
    current = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))['stickers'][0]['original']
    assert old != current
    assert not (output / old.lstrip('/')).exists()
    assert (output / current.lstrip('/')).exists()


def test_failed_rebuild_keeps_previous_site(tmp_path):
    source = root(tmp_path)
    add(source, 'a')
    output = tmp_path / 'dist'
    build(source, output)
    previous = (output / 'manifest.json').read_bytes()
    add(source, 'b')
    with pytest.raises(catalog.CatalogError, match='exact duplicate'):
        build(source, output)
    assert (output / 'manifest.json').read_bytes() == previous


def test_unowned_output_is_never_removed(tmp_path):
    source = root(tmp_path)
    output = tmp_path / 'dist'
    output.mkdir()
    important = output / 'valuable.txt'
    important.write_text('keep')
    with pytest.raises(catalog.CatalogError, match='not an owned'):
        build(source, output)
    assert important.read_text() == 'keep'
    with pytest.raises(catalog.CatalogError, match='overlaps'):
        build(source, source)


def test_snapshot_captures_original_bytes_and_is_immutable(tmp_path):
    from dataclasses import FrozenInstanceError
    source = root(tmp_path)
    directory = add(source, 'a')
    snapshot = catalog.CatalogSnapshot.read(source)
    original = (directory / 'original.png').read_bytes()
    Image.new('RGB', (50, 50), 'pink').save(directory / 'original.png')
    output = tmp_path / 'publication'
    output.mkdir()
    snapshot.publish(output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert (output / manifest['stickers'][0]['original'].lstrip('/')).read_bytes() == original
    with pytest.raises(FrozenInstanceError):
        snapshot.entries = {}
    with pytest.raises(TypeError):
        snapshot.entries['a'] = None
    with pytest.raises(FrozenInstanceError):
        snapshot.entries['a'].metadata.text = 'changed'


def test_merge_preserves_author_and_license(tmp_path):
    base = root(tmp_path, 'base')
    add(base, 'old', 'red', sources=[{'url': 'https://example.org/art', 'author': 'Artist', 'license': 'CC BY'}])
    add(base, 'new', 'blue')
    after = tmp_path / 'after'
    shutil.copytree(base, after)
    shutil.rmtree(after / 'stickers/old')
    change(after / 'stickers/new', sources=[{'url': 'https://example.org/art'}])
    (after / 'redirects.yaml').write_text('old: new')
    with pytest.raises(catalog.CatalogError, match='preserve source records'):
        build(after, tmp_path / 'dist', base)


@pytest.mark.parametrize('content,match', [
    ('title: &t hello\ntext: *t', 'aliases'),
    ('x: ' + '[' * 30 + '0' + ']' * 30, 'nesting'),
])
def test_complex_yaml_is_rejected(tmp_path, content, match):
    source = root(tmp_path)
    directory = add(source, 'a')
    (directory / 'metadata.yaml').write_text(content)
    with pytest.raises(catalog.CatalogError, match=match):
        catalog.CatalogSnapshot.read(source)


def test_limits_and_wrong_image_extension(tmp_path, monkeypatch):
    source = root(tmp_path)
    directory = add(source, 'a')
    monkeypatch.setattr(catalog.ImageAsset, 'MAX_PIXELS', 2000)
    with pytest.raises(catalog.CatalogError, match='processing limits'):
        catalog.CatalogSnapshot.read(source)
    monkeypatch.setattr(catalog.ImageAsset, 'MAX_PIXELS', 25_000_000)
    monkeypatch.setattr(catalog.CatalogSnapshot, 'MAX_TOTAL_BYTES', 1)
    with pytest.raises(catalog.CatalogError, match='total original byte limit'):
        catalog.CatalogSnapshot.read(source)
    monkeypatch.setattr(catalog.CatalogSnapshot, 'MAX_TOTAL_BYTES', 256 * 1024 * 1024)
    (directory / 'original.png').rename(directory / 'original.jpg')
    with pytest.raises(catalog.CatalogError, match='format'):
        catalog.CatalogSnapshot.read(source)


def test_original_caption_preserved_and_normalized_only_for_comparison(tmp_path):
    source = root(tmp_path)
    add(source, 'a', text='吃 饭！\n开饭啦～')
    snapshot = catalog.CatalogSnapshot.read(source)
    assert snapshot.entries['a'].metadata.text == '吃 饭！\n开饭啦～'
    assert snapshot.entries['a'].metadata.normalized_text == '吃饭开饭啦~'


def test_direct_value_construction_validates_invariants():
    with pytest.raises(catalog.CatalogError, match='HTTP'):
        catalog.Source('javascript:alert(1)')
    with pytest.raises(catalog.CatalogError, match='sources'):
        catalog.Metadata('title', '', (), ())


def test_duplicate_report_is_bounded(tmp_path, monkeypatch):
    source = root(tmp_path)
    for ident, color in [('a', 'red'), ('b', 'green'), ('c', 'blue')]:
        add(source, ident, color)
    monkeypatch.setattr(catalog.DuplicateReview, 'MAX_REPORTED_PAIRS', 1)
    report = build(source, tmp_path / 'dist')
    assert len(report['similarPairs']) == 1
    assert report['similarPairsOmitted'] == 2


def screenshot(directory, border='black'):
    image = Image.new('RGB', (100, 80), border)
    image.paste('white', (10, 15, 90, 65))
    image.paste('blue', (35, 30, 65, 50))
    image.save(directory / 'original.png')
    return image


def published(tmp_path, source):
    output = tmp_path / 'dist'
    report = build(source, output, report_dir=tmp_path / 'reports')
    item = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))['stickers'][0]
    return item, report, output


def test_phone_screenshot_processing_publishes_crop_and_keeps_submission(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'phone')
    original = screenshot(directory)
    raw = (directory / 'original.png').read_bytes()
    item, report, output = published(tmp_path, source)
    assert item['processingReport']['crop'] == [10, 15, 90, 65]
    assert [item['width'], item['height']] == [80, 50]
    assert (output / item['original'].lstrip('/')).read_bytes() == raw
    with Image.open(output / item['display'].lstrip('/')) as display:
        assert display.convert('RGB').tobytes() == original.crop((10, 15, 90, 65)).tobytes()
    with Image.open(output / item['preview'].lstrip('/')) as preview:
        assert preview.size == (80, 50)
    assert report['processing']['phone'] == item['processingReport']
    assert (tmp_path / 'reports/review.html').is_file()


@pytest.mark.parametrize('border', ['white', (0, 0, 0, 0)])
def test_white_or_transparent_edges_are_not_automatically_trimmed(tmp_path, border):
    source = root(tmp_path)
    directory = add(source, 'safe')
    image = Image.new('RGBA', (100, 80), border)
    image.paste('blue', (20, 20, 80, 60))
    image.save(directory / 'original.png')
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [0, 0, 100, 80]


def test_status_bar_with_icons_is_not_guessed(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'phone')
    image = Image.new('RGB', (100, 80), 'white')
    image.paste('black', (0, 0, 100, 15))
    image.paste('white', (6, 0, 15, 12))
    image.paste('blue', (30, 30, 70, 60))
    image.save(directory / 'original.png')
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [0, 0, 100, 80]


def test_explicit_crop_removes_status_bar_without_upscaling(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'manual')
    screenshot(directory)
    change(directory, processing={'crop': [10, 15, 90, 65], 'trim': False, 'max_edge': 960})
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [10, 15, 90, 65]
    assert item['processingReport']['outputSize'] == [80, 50]


def test_manual_crop_coordinates_follow_exif_orientation(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'oriented')
    (directory / 'original.png').unlink()
    image = Image.new('RGB', (80, 100), 'white')
    exif = image.getexif()
    exif[274] = 6
    image.save(directory / 'original.jpg', exif=exif)
    change(directory, processing={'trim': False, 'crop': [10, 5, 95, 75]})
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['originalSize'] == [100, 80]
    assert item['processingReport']['outputSize'] == [85, 70]


def test_manual_and_automatic_crop_report_share_original_coordinates(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'combined')
    screenshot(directory)
    change(directory, processing={'crop': [5, 5, 95, 75]})
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [10, 15, 90, 65]


def test_trim_can_be_disabled_and_default_max_edge_only_downscales(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'large')
    Image.new('RGB', (1400, 700), 'white').save(directory / 'original.png')
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['outputSize'] == [960, 480]
    screenshot(directory)
    change(directory, processing={'trim': False, 'max_edge': 40})
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [0, 0, 100, 80]
    assert item['processingReport']['outputSize'] == [40, 32]


@pytest.mark.parametrize('processing', [
    {'crop': [0, 0, 101, 80]}, {'crop': [30, 0, 20, 10]}, {'crop': [True, 0, 20, 10]},
    {'max_edge': 0}, {'max_edge': 4097}, {'max_edge': True}, {'trim': 'false'}, {'crop': None},
])
def test_invalid_processing_is_rejected(tmp_path, processing):
    source = root(tmp_path)
    directory = add(source, 'bad')
    screenshot(directory)
    change(directory, processing=processing)
    with pytest.raises(catalog.CatalogError, match='processing'):
        published(tmp_path, source)


def test_uniform_and_excessive_trims_are_kept_with_report_warnings(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'safe', 'black')
    item, report, _ = published(tmp_path, source)
    assert item['processingReport']['outputSize'] == [50, 50]
    assert 'Uniform' in report['processing']['safe']['warnings'][0]
    image = Image.new('RGB', (100, 100), 'black')
    image.paste('white', (45, 45, 55, 55))
    image.save(directory / 'original.png')
    item, report, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [0, 0, 100, 100]
    assert 'too much' in report['processing']['safe']['warnings'][0]


def test_similar_edge_pixels_trim_but_watermark_blocks_its_edge(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'phone')
    image = screenshot(directory)
    image.paste((3, 3, 3), (0, 0, 100, 15))
    image.save(directory / 'original.png')
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'] == [10, 15, 90, 65]
    image = screenshot(directory)
    image.paste('white', (30, 0, 40, 10))
    image.save(directory / 'original.png')
    item, _, _ = published(tmp_path, source)
    assert item['processingReport']['crop'][1] == 0


def test_animated_preprocessing_preserves_raw_and_rejects_explicit_transform(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'animated')
    (directory / 'original.png').unlink()
    frame = Image.new('RGB', (50, 50), 'blue')
    frame.save(directory / 'original.gif', save_all=True,
               append_images=[Image.new('RGB', (50, 50), 'red')], duration=200, loop=0)
    item, _, _ = published(tmp_path, source)
    assert item['display'] == item['original']
    assert item['processingReport']['status'] == 'preserved-animation'
    assert item['processingReport']['warnings']
    for policy in ({'max_edge': 960}, {'crop': [0, 0, 40, 40]}):
        change(directory, processing=policy)
        with pytest.raises(catalog.CatalogError, match='animated images'):
            published(tmp_path, source)


def test_processing_recipe_and_policy_change_cache_paths_not_original(tmp_path, monkeypatch):
    source = root(tmp_path)
    directory = add(source, 'cached')
    screenshot(directory)
    first, _, _ = published(tmp_path, source)
    change(directory, processing={'trim': False})
    second, _, _ = published(tmp_path, source)
    monkeypatch.setattr(catalog.ImageProcessor, 'RECIPE', 'next-recipe')
    third, _, _ = published(tmp_path, source)
    assert first['original'] == second['original'] == third['original']
    assert len({first['display'], second['display'], third['display']}) == 3
    assert len({first['preview'], second['preview'], third['preview']}) == 3


def test_reprocess_combines_with_other_mutations_on_same_entry(tmp_path):
    base = root(tmp_path, 'base')
    add(base, 'fish')
    after = tmp_path / 'after'
    shutil.copytree(base, after)
    directory = after / 'stickers/fish'
    screenshot(directory)
    change(directory, text='新的文字', processing={'trim': False},
           sources=[{'url': 'https://example.org/new'}])
    report = build(after, tmp_path / 'dist', base, tmp_path / 'reports')
    assert {m['op'] for m in report['mutations']} == {'UPDATE', 'REPLACE', 'SOURCES', 'REPROCESS'}
    assert set(report['processing']) == {'fish'}


def test_processed_image_drives_visual_similarity_but_raw_drives_exact_duplicates(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'bordered')
    image = screenshot(directory)
    second = add(source, 'cropped')
    image.crop((10, 15, 90, 65)).save(second / 'original.png')
    report = build(source, tmp_path / 'dist')
    assert not report['exactDuplicates']
    assert report['similarPairs'][0]['imageDistance'] == 0


def test_merge_cannot_discard_survivor_sources(tmp_path):
    base = root(tmp_path, 'base')
    add(base, 'old', 'red')
    add(base, 'keep', 'blue')
    after = tmp_path / 'after'
    shutil.copytree(base, after)
    shutil.rmtree(after / 'stickers/old')
    change(after / 'stickers/keep', sources=[{'url': 'https://example.org/old'}])
    (after / 'redirects.yaml').write_text('old: keep')
    with pytest.raises(catalog.CatalogError, match='survivor'):
        build(after, tmp_path / 'dist', base)


def test_snapshot_constructor_enforces_identity_and_total_limits(tmp_path, monkeypatch):
    source = root(tmp_path)
    directory = add(source, 'valid')
    sticker = catalog.Sticker.read(directory)
    for entries in ({'bad ID': sticker}, {'valid': object()}, {'other': sticker}):
        with pytest.raises(catalog.CatalogError):
            catalog.CatalogSnapshot(entries)
    with pytest.raises(catalog.CatalogError, match='mapping'):
        catalog.CatalogSnapshot([])
    with pytest.raises(catalog.CatalogError, match='mapping'):
        catalog.CatalogSnapshot({}, [])
    monkeypatch.setattr(catalog.CatalogSnapshot, 'MAX_ITEMS', 0)
    with pytest.raises(catalog.CatalogError, match='item limit'):
        catalog.CatalogSnapshot({'valid': sticker})
    monkeypatch.setattr(catalog.CatalogSnapshot, 'MAX_ITEMS', 1)
    monkeypatch.setattr(catalog.CatalogSnapshot, 'MAX_TOTAL_BYTES', 1)
    with pytest.raises(catalog.CatalogError, match='byte limit'):
        catalog.CatalogSnapshot({'valid': sticker})


def test_review_report_owns_input_and_returns_detached_snapshot(tmp_path):
    source = root(tmp_path)
    directory = add(source, 'fish', text='<script>alert(1)</script>')
    change(directory, title='<b>title</b>', sources=[{
        'url': 'https://example.org/?q="/><script>alert(2)</script>',
        'author': '<img/src=x/onerror=alert(3)>', 'license': '<b>license</b>'}])
    snapshot = catalog.CatalogSnapshot.read(source)
    changes = catalog.ChangeSet(catalog.CatalogSnapshot(), snapshot)
    duplicates = catalog.DuplicateReview(snapshot)
    ocr = {'fish': {'text': 'advice'}}
    report = catalog.ReviewReport(changes, duplicates, 1, ocr, snapshot.entries.values())
    ocr['fish']['text'] = 'external mutation'
    detached = report.snapshot()
    detached['ocrSuggestions']['fish']['text'] = 'caller mutation'
    assert report.snapshot()['ocrSuggestions']['fish']['text'] == 'advice'
    report.write(tmp_path / 'report')
    html = (tmp_path / 'report/review.html').read_text(encoding='utf-8')
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '<b>title</b>' not in html and 'data:image/webp;base64,' in html
    assert '<img/src=x/onerror=alert(3)>' not in html
    assert 'q=&quot;/&gt;&lt;script&gt;' in html


@pytest.mark.parametrize('extension', ['png', 'jpg', 'webp', 'gif', 'apng'])
def test_all_supported_original_formats_remain_byte_identical(tmp_path, extension):
    source = root(tmp_path)
    directory = add(source, 'original')
    (directory / 'original.png').unlink()
    path = directory / f'original.{extension}'
    image = Image.new('RGB', (100, 80), 'black')
    image.paste('white', (10, 10, 90, 70))
    image.paste('blue', (30, 30, 70, 50))
    image.save(path, format='PNG' if extension == 'apng' else None)
    original = path.read_bytes()
    item, _, output = published(tmp_path, source)
    assert (output / item['original'].lstrip('/')).read_bytes() == original
    assert path.read_bytes() == original


def test_ocr_uses_two_isolated_workers_and_stable_output(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    source = root(tmp_path)
    for ident in ('delta', 'alpha', 'charlie', 'bravo'):
        add(source, ident)
    snapshot = catalog.CatalogSnapshot.read(source)
    changes = catalog.ChangeSet(catalog.CatalogSnapshot(), snapshot)
    monkeypatch.setattr(catalog.shutil, 'which', lambda name: '/usr/bin/tesseract')
    monkeypatch.setenv('OMP_THREAD_LIMIT', '8')
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = 0
    paths = []

    def run(args, *, capture_output, timeout, env):
        nonlocal active, peak
        assert capture_output and timeout == 30
        assert env['OMP_THREAD_LIMIT'] == '1'
        assert args[0] == 'tesseract' and args[3:] == ['-l', 'chi_sim+chi_tra+eng', '--psm', '11']
        assert Path(args[1]).is_file()
        with lock:
            active += 1
            peak = max(peak, active)
            paths.append(args[1])
        try:
            barrier.wait(timeout=5)
            return SimpleNamespace(stdout=' 干饭！\n'.encode(), returncode=0)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(catalog.subprocess, 'run', run)
    suggestions = catalog.OcrAdvisor().suggest(snapshot, changes)
    assert list(suggestions) == ['alpha', 'bravo', 'charlie', 'delta']
    assert all(value == {'text': '干饭！', 'ok': True} for value in suggestions.values())
    assert peak == 2 and len(set(paths)) == 4
    assert all(not Path(path).exists() for path in paths)
    assert catalog.os.environ['OMP_THREAD_LIMIT'] == '8'


def test_ocr_timeout_and_process_failure_remain_per_image_results(tmp_path, monkeypatch):
    source = root(tmp_path)
    add(source, 'timeout', color='red')
    add(source, 'failure', color='blue')
    snapshot = catalog.CatalogSnapshot.read(source)
    changes = catalog.ChangeSet(catalog.CatalogSnapshot(), snapshot)
    monkeypatch.setattr(catalog.shutil, 'which', lambda name: '/usr/bin/tesseract')

    def run(args, *, capture_output, timeout, env):
        from types import SimpleNamespace
        with Image.open(args[1]) as im:
            red, _, blue = im.getpixel((0, 0))
        if red > blue:
            raise catalog.subprocess.TimeoutExpired(args, timeout)
        return SimpleNamespace(stdout=b'partial', returncode=1)

    monkeypatch.setattr(catalog.subprocess, 'run', run)
    assert catalog.OcrAdvisor().suggest(snapshot, changes) == {
        'failure': {'text': 'partial', 'ok': False}, 'timeout': {'text': '', 'ok': False}}


def test_ocr_only_processes_changed_images_and_handles_missing_tool(tmp_path, monkeypatch):
    source = root(tmp_path)
    add(source, 'unchanged')
    before = catalog.CatalogSnapshot.read(source)
    add(source, 'added')
    after = catalog.CatalogSnapshot.read(source)
    changes = catalog.ChangeSet(before, after)
    monkeypatch.setattr(catalog.shutil, 'which', lambda name: None)
    assert catalog.OcrAdvisor().suggest(after, changes) == {
        'status': 'unavailable', 'reason': 'Tesseract is not installed'}
    monkeypatch.setattr(catalog.shutil, 'which', lambda name: '/usr/bin/tesseract')
    observed = []

    def suggest_for(self, sticker):
        observed.append(sticker.ident)
        return {'text': 'suggested', 'ok': True}

    monkeypatch.setattr(catalog.OcrAdvisor, '_suggest_for', suggest_for)
    assert catalog.OcrAdvisor().suggest(after, changes) == {'added': {'text': 'suggested', 'ok': True}}
    assert observed == ['added']
