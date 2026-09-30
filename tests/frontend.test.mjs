import assert from 'node:assert/strict';
import test from 'node:test';
import { FavoriteStore, StickerCatalog, StickerMedia } from '../web/app.js';

const data = () => ({
  stickers: [
    { id: 'fish-eating', title: '开饭了', text: '干饭！', tags: ['吃饭', '开心'], sources: [] },
    { id: 'fish-sad', title: '委屈鱼', text: '没吃饭', tags: ['委屈'], sources: [] },
  ],
  redirects: { 'fish-old': 'fish-middle', 'fish-middle': 'fish-eating' },
});

class MemoryStorage {
  value;
  constructor(value = '[]') { this.value = value; }
  getItem() { return this.value; }
  setItem(_key, value) { this.value = value; }
}

test('search matches normalized text and requires all words with tag filtering', () => {
  const catalog = new StickerCatalog(data());
  assert.deepEqual(catalog.search({ query: '干饭 开心' }).map(s => s.id), ['fish-eating']);
  assert.deepEqual(catalog.search({ query: '饭', tag: '委屈' }).map(s => s.id), ['fish-sad']);
  assert.equal(catalog.search({ query: '不存在' }).length, 0);
  assert.equal(catalog.search({ query: '干饭！' }).length, 1);
});

test('identity resolves chained merges, rejects cycles, and protects internal records', () => {
  const catalog = new StickerCatalog(data());
  assert.equal(catalog.resolve('fish-old').id, 'fish-eating');
  assert.equal(catalog.resolve('deleted'), null);
  assert.throws(() => catalog.resolve('fish-eating').tags.push('injected'), TypeError);
  const cyclic = new StickerCatalog({ stickers: [], redirects: { a: 'b', b: 'a' } });
  assert.equal(cyclic.resolve('a'), null);
});

test('favorites migrate old IDs, coalesce duplicate references and remove deleted IDs', () => {
  const storage = new MemoryStorage('["fish-old","fish-eating","deleted"]');
  const favorites = new FavoriteStore(() => storage);
  const catalog = new StickerCatalog(data());
  favorites.reconcile(catalog);
  assert.equal(favorites.has('fish-eating'), true);
  assert.equal(favorites.has('fish-old'), false);
  assert.deepEqual(JSON.parse(storage.value), ['fish-eating']);
  assert.deepEqual(catalog.search({ favorites }).map(s => s.id), ['fish-eating']);
  favorites.toggle('fish-eating');
  assert.equal(catalog.search({ favorites }).length, 0);
});

test('corrupt or inaccessible storage does not prevent in-memory favorites', () => {
  for (const storage of [new MemoryStorage('{broken'), new MemoryStorage('{}')]) {
    const favorites = new FavoriteStore(() => storage);
    favorites.toggle('fish-eating');
    assert.equal(favorites.has('fish-eating'), true);
  }
  const favorites = new FavoriteStore(() => { throw new Error('blocked'); });
  assert.equal(favorites.toggle('fish-eating'), false);
  assert.equal(favorites.has('fish-eating'), true);
});

test('media uses processed display while retaining a separately named original download', () => {
  const media = new StickerMedia({ id: 'fish', original: '/media/raw/original.jpg', display: '/media/recipe/display.png',
    processingReport: { originalSize: [1200, 1600], outputSize: [800, 960], crop: [100, 100, 1100, 1300] } });
  assert.equal(media.display, '/media/recipe/display.png');
  assert.deepEqual(media.download(), { href: '/media/recipe/display.png', filename: 'fish.png' });
  assert.deepEqual(media.download('original'), { href: '/media/raw/original.jpg', filename: 'fish-original.jpg' });
  assert.match(media.summary, /已裁剪边缘.*已缩小尺寸/);
  assert.match(media.summary, /原始投稿仍可下载/);
});

test('untouched static media stays quiet, legacy manifests work, and animations explain preservation', () => {
  const original = '/media/raw/original.gif';
  const legacy = new StickerMedia({ id: 'fish', original });
  assert.equal(legacy.display, original);
  assert.equal(legacy.summary, '');
  const untouched = new StickerMedia({ id: 'fish', original, processingReport: {
    originalSize: [200, 200], outputSize: [200, 200], crop: [0, 0, 200, 200],
  } });
  assert.equal(untouched.summary, '');
  const animation = new StickerMedia({ id: 'fish', original, display: original, animated: true });
  assert.equal(animation.download().href, original);
  assert.match(animation.summary, /动图保持原样/);
});

test('catalog owns nested preprocessing report data without exposing mutable arrays', () => {
  const input = data();
  input.stickers[0].processingReport = { originalSize: [500, 600], outputSize: [400, 400], crop: [50, 100, 450, 500], warnings: [] };
  const catalog = new StickerCatalog(input);
  input.stickers[0].processingReport.crop[0] = 0;
  const report = catalog.resolve('fish-eating').processingReport;
  assert.equal(report.crop[0], 50);
  assert.throws(() => report.crop.push(0), TypeError);
  assert.throws(() => { report.outputSize = [1, 1]; }, TypeError);
});
