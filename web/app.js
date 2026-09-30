const REPOSITORY = 'https://github.com/pluveto/DeepSeek-chan';

class Elements {
  static get(id) { return document.getElementById(id); }
  static create(tag, className = '', text = null) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== null) element.textContent = text;
    return element;
  }
}

// The catalog owns normalization and identity resolution; views only receive records.
export class StickerCatalog {
  #stickers;
  #byId;
  #redirects;
  #searchText;

  constructor(data) {
    this.#stickers = data.stickers.map(sticker => Object.freeze({
      ...sticker,
      tags: Object.freeze([...sticker.tags]),
      sources: Object.freeze(sticker.sources.map(source => Object.freeze({ ...source }))),
      processingReport: sticker.processingReport ? Object.freeze({
        ...sticker.processingReport,
        originalSize: Object.freeze([...sticker.processingReport.originalSize]),
        outputSize: Object.freeze([...sticker.processingReport.outputSize]),
        crop: Object.freeze([...sticker.processingReport.crop]),
        warnings: Object.freeze([...sticker.processingReport.warnings]),
      }) : undefined,
    }));
    this.#byId = new Map(this.#stickers.map(sticker => [sticker.id, sticker]));
    this.#redirects = new Map(Object.entries(data.redirects || {}));
    this.#searchText = new Map(this.#stickers.map(sticker => [sticker.id,
      StickerCatalog.#normalize([sticker.title, sticker.text, ...sticker.tags].join(' '))]));
  }

  static #normalize(text) { return text.normalize('NFKC').toLocaleLowerCase().replace(/[\p{P}\s]/gu, ''); }

  resolve(id) {
    const visited = new Set();
    while (this.#redirects.has(id)) {
      if (visited.has(id)) return null;
      visited.add(id);
      id = this.#redirects.get(id);
    }
    return this.#byId.get(id) || null;
  }

  search({ query = '', tag = '', favorites = null, sort = 'default' } = {}) {
    const terms = query.trim().split(/\s+/).map(StickerCatalog.#normalize).filter(Boolean);
    const matches = this.#stickers.filter(sticker =>
      (!tag || sticker.tags.includes(tag)) &&
      (!favorites || favorites.has(sticker.id)) &&
      terms.every(term => this.#searchText.get(sticker.id).includes(term)));
    if (sort === 'title') matches.sort((a, b) => a.title.localeCompare(b.title, 'zh-CN'));
    return matches;
  }

  popularTags() {
    const counts = new Map();
    for (const sticker of this.#stickers) {
      for (const tag of sticker.tags) counts.set(tag, (counts.get(tag) || 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([tag]) => tag);
  }

  get featured() { return this.resolve('blue-fish-headpat') || this.#stickers[0] || null; }
}

// Storage failures do not affect the in-memory collection or interrupt browsing.
export class FavoriteStore {
  #ids = new Set();
  #storage;
  #key = 'deepseek-chan:favorites';

  constructor(storageProvider) {
    try {
      this.#storage = storageProvider();
      const saved = JSON.parse(this.#storage.getItem(this.#key) || '[]');
      if (Array.isArray(saved)) this.#ids = new Set(saved.filter(id => typeof id === 'string'));
    } catch { /* Missing, corrupt, or unavailable storage starts an empty collection. */ }
  }

  has(id) { return this.#ids.has(id); }

  toggle(id) {
    this.#ids.has(id) ? this.#ids.delete(id) : this.#ids.add(id);
    return this.#persist();
  }

  reconcile(catalog) {
    this.#ids = new Set([...this.#ids].map(id => catalog.resolve(id)?.id).filter(Boolean));
    this.#persist();
  }

  #persist() {
    try {
      if (!this.#storage) return false;
      this.#storage.setItem(this.#key, JSON.stringify([...this.#ids]));
      return true;
    } catch { return false; }
  }
}

// The media policy keeps display derivatives separate from the untouched submission.
export class StickerMedia {
  #id;
  #original;
  #display;
  #summary;

  constructor(sticker) {
    this.#id = sticker.id;
    this.#original = sticker.original;
    this.#display = sticker.display || sticker.original;
    this.#summary = StickerMedia.#describe(sticker);
  }

  get display() { return this.#display; }
  get summary() { return this.#summary; }

  download(kind = 'display') {
    if (!['display', 'original'].includes(kind)) throw new Error('unknown media kind');
    const href = kind === 'original' ? this.#original : this.#display;
    const suffix = kind === 'original' ? '-original' : '';
    return { href, filename: `${this.#id}${suffix}.${href.split('.').pop()}` };
  }

  static #describe(sticker) {
    if (sticker.animated) return '动图保持原样，下载可保留动画。';
    const report = sticker.processingReport;
    if (!report?.originalSize || !report?.outputSize) return '';
    const [width, height] = report.originalSize;
    const [outWidth, outHeight] = report.outputSize;
    const [left, top, right, bottom] = report.crop || [0, 0, width, height];
    const details = [];
    if (left || top || right !== width || bottom !== height) details.push('已裁剪边缘');
    if (outWidth < right - left || outHeight < bottom - top) details.push('已缩小尺寸');
    if (!details.length) return '';
    return `${details.join(' · ')}（${width} × ${height} → ${outWidth} × ${outHeight}）。原始投稿仍可下载。`;
  }
}

class NoticeView {
  #timer;
  show(message) {
    const toast = Elements.get('toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => { toast.hidden = true; }, 2600);
  }
}

class GalleryView {
  #onOpen;
  #onFavorite;
  #onTag;

  constructor({ onOpen, onFavorite, onTag }) {
    this.#onOpen = onOpen;
    this.#onFavorite = onFavorite;
    this.#onTag = onTag;
  }

  showTags(tags, activeTag) {
    Elements.get('tags').replaceChildren(...['', ...tags].map(tag => {
      const button = Elements.create('button', '', tag || '全部');
      button.setAttribute('aria-pressed', String(tag === activeTag));
      button.addEventListener('click', () => this.#onTag(tag));
      return button;
    }));
  }

  show(stickers, { limit, title, favorites, onlyFavorites }) {
    Elements.get('count').textContent = stickers.length;
    Elements.get('collection-title').firstChild.textContent = `${title} `;
    Elements.get('grid').replaceChildren(...stickers.slice(0, limit).map(sticker => this.#card(sticker, favorites)));
    Elements.get('empty').hidden = stickers.length > 0;
    Elements.get('more').hidden = stickers.length <= limit;
    Elements.get('favorites-filter').setAttribute('aria-pressed', String(onlyFavorites));
  }

  #card(sticker, favorites) {
    const card = Elements.create('article', 'card');
    const open = Elements.create('button', 'card-open');
    open.setAttribute('aria-label', `查看 ${sticker.title}`);
    const image = Elements.create('img');
    image.src = sticker.preview;
    image.alt = sticker.text || sticker.title;
    image.loading = 'lazy';
    image.decoding = 'async';
    image.width = 240;
    image.height = 240;
    open.append(image);
    if (sticker.animated) open.append(Elements.create('span', 'animated', '动图'));
    open.addEventListener('click', () => this.#onOpen(sticker.id));
    const bottom = Elements.create('div', 'card-bottom');
    const name = Elements.create('div', 'card-name');
    name.append(Elements.create('h3', '', sticker.title), Elements.create('small', '', sticker.tags.slice(0, 2).map(tag => `# ${tag}`).join('  ')));
    const heart = Elements.create('button', 'heart', favorites.has(sticker.id) ? '♥' : '♡');
    heart.setAttribute('aria-label', `收藏 ${sticker.title}`);
    heart.setAttribute('aria-pressed', String(favorites.has(sticker.id)));
    heart.addEventListener('click', () => this.#onFavorite(sticker.id));
    bottom.append(name, heart);
    card.append(open, bottom);
    return card;
  }
}

class DetailView {
  #current = null;
  #media = null;
  #favorites;
  #notices;
  #dialog;

  constructor({ favorites, notices, onClose, onFavorite }) {
    this.#favorites = favorites;
    this.#notices = notices;
    this.#dialog = Elements.get('detail');
    Elements.get('close-detail').addEventListener('click', onClose);
    this.#dialog.addEventListener('cancel', event => { event.preventDefault(); onClose(); });
    this.#dialog.addEventListener('click', event => {
      if (event.target !== this.#dialog) return;
      const rect = this.#dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    });
    Elements.get('favorite-detail').addEventListener('click', () => {
      if (this.#current) onFavorite(this.#current.id);
    });
    Elements.get('copy-link').addEventListener('click', () => this.#copyLink());
    Elements.get('copy-image').addEventListener('click', () => this.#copyImage());
  }

  get isOpen() { return this.#dialog.open; }

  show(sticker) {
    this.#current = sticker;
    this.#media = new StickerMedia(sticker);
    Elements.get('detail-title').textContent = sticker.title;
    Elements.get('detail-image').src = this.#media.display;
    Elements.get('detail-image').alt = sticker.text || sticker.title;
    Elements.get('detail-text').textContent = sticker.text || '这张表情没有文字，心情都在画里。';
    Elements.get('detail-tags').replaceChildren(...sticker.tags.map(tag => Elements.create('span', '', `# ${tag}`)));
    for (const [id, kind] of [['download', 'display'], ['download-original', 'original']]) {
      const download = this.#media.download(kind);
      Elements.get(id).href = download.href;
      Elements.get(id).download = download.filename;
    }
    Elements.get('processing-summary').textContent = this.#media.summary;
    Elements.get('processing-summary').hidden = !this.#media.summary;
    Elements.get('edit').href = `${REPOSITORY}/edit/main/stickers/${encodeURIComponent(sticker.id)}/metadata.yaml`;
    Elements.get('sources').replaceChildren(...sticker.sources.map((source, index) => this.#source(source, index)));
    this.refreshFavorite();
    if (!this.#dialog.open) this.#dialog.showModal();
  }

  hide() { this.#current = null; this.#media = null; if (this.#dialog.open) this.#dialog.close(); }

  refreshFavorite() {
    if (!this.#current) return;
    const selected = this.#favorites.has(this.#current.id);
    Elements.get('favorite-detail').textContent = selected ? '♥ 已收藏' : '♡ 收藏';
    Elements.get('favorite-detail').setAttribute('aria-pressed', String(selected));
  }

  #source(source, index) {
    const item = Elements.create('li');
    // Manifest data must never become executable markup or a javascript: link.
    let url;
    try { url = new URL(source.url); } catch { url = null; }
    const archiveSource = url?.hostname === 'github.com' && url.pathname.startsWith('/EDMOK/blue-fish-archive/');
    const label = source.author || (archiveSource ? '蓝色大肥鱼档案馆 · 收录来源' : `来源 ${index + 1}`);
    if (url && ['https:', 'http:'].includes(url.protocol)) {
      const link = Elements.create('a', '', label);
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      item.append(link);
    } else item.textContent = label;
    if (source.license) item.append(document.createTextNode(` · ${source.license}`));
    return item;
  }

  async #copyLink() {
    try { await navigator.clipboard.writeText(location.href); this.#notices.show('表情链接已复制'); }
    catch { this.#notices.show('无法复制，请复制地址栏中的链接'); }
  }

  async #copyImage() {
    if (!this.#current) return;
    if (this.#current.animated) { this.#notices.show('动图请使用「下载表情」，以保留动画'); return; }
    const display = this.#media.display;
    try {
      // Call write during the user gesture, while PNG conversion resolves asynchronously.
      if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') throw new Error('clipboard unavailable');
      const item = new ClipboardItem({ 'image/png': this.#png(display) });
      await navigator.clipboard.write([item]);
      this.#notices.show('图片已复制');
    } catch { this.#notices.show('浏览器未允许复制图片，请使用「下载表情」'); }
  }

  async #png(original) {
    const response = await fetch(original);
    if (!response.ok) throw new Error('image unavailable');
    const bitmap = await createImageBitmap(await response.blob());
    try {
      const canvas = document.createElement('canvas');
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext('2d').drawImage(bitmap, 0, 0);
      return await new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('PNG encoding failed')), 'image/png'));
    } finally { bitmap.close(); }
  }
}

class StickerWallApp {
  #catalog;
  #favorites = new FavoriteStore(() => localStorage);
  #notices = new NoticeView();
  #gallery;
  #detail;
  #activeTag = '';
  #onlyFavorites = false;
  #limit = 60;

  constructor() {
    this.#gallery = new GalleryView({
      onOpen: id => { location.hash = `sticker=${encodeURIComponent(id)}`; },
      onFavorite: id => this.#toggleFavorite(id),
      onTag: tag => { this.#activeTag = tag; this.#limit = 60; this.#render(); },
    });
    this.#detail = new DetailView({
      favorites: this.#favorites,
      notices: this.#notices,
      onClose: () => {
        history.replaceState(null, '', location.pathname + location.search);
        this.#route();
      },
      onFavorite: id => this.#toggleFavorite(id),
    });
    this.#bindControls();
  }

  async start() {
    try {
      const response = await fetch('/manifest.json');
      if (!response.ok) throw new Error('catalog unavailable');
      this.#catalog = new StickerCatalog(await response.json());
      this.#favorites.reconcile(this.#catalog);
      const featured = this.#catalog.featured;
      if (featured) {
        Elements.get('hero-image').src = featured.preview;
        Elements.get('hero-image').hidden = false;
        Elements.get('hero-placeholder').hidden = true;
      }
      Elements.get('status').hidden = true;
      this.#render();
      this.#route();
    } catch {
      Elements.get('status').textContent = '表情暂时没有加载成功，请刷新页面再试。';
    }
  }

  #bindControls() {
    Elements.get('search').addEventListener('input', () => { this.#limit = 60; this.#render(); });
    Elements.get('sort').addEventListener('change', () => this.#render());
    Elements.get('more').addEventListener('click', () => { this.#limit += 60; this.#render(); });
    Elements.get('favorites-filter').addEventListener('click', () => {
      this.#onlyFavorites = !this.#onlyFavorites;
      this.#limit = 60;
      this.#render();
    });
    Elements.get('reset').addEventListener('click', () => {
      this.#activeTag = '';
      this.#onlyFavorites = false;
      this.#limit = 60;
      Elements.get('search').value = '';
      this.#render();
    });
    document.addEventListener('keydown', event => {
      const active = document.activeElement;
      if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(active?.tagName) && !active?.isContentEditable && !this.#detail.isOpen) {
        event.preventDefault();
        Elements.get('search').focus();
      }
    });
    window.addEventListener('hashchange', () => this.#route());
  }

  #toggleFavorite(id) {
    if (!this.#favorites.toggle(id)) this.#notices.show('收藏暂存于本页，浏览器未允许本地存储');
    this.#render();
    this.#detail.refreshFavorite();
  }

  #render() {
    if (!this.#catalog) return;
    const matches = this.#catalog.search({
      query: Elements.get('search').value,
      tag: this.#activeTag,
      favorites: this.#onlyFavorites ? this.#favorites : null,
      sort: Elements.get('sort').value,
    });
    this.#gallery.showTags(this.#catalog.popularTags(), this.#activeTag);
    this.#gallery.show(matches, {
      limit: this.#limit,
      title: this.#onlyFavorites ? '我的收藏' : this.#activeTag || '全部表情',
      favorites: this.#favorites,
      onlyFavorites: this.#onlyFavorites,
    });
  }

  #route() {
    if (!this.#catalog) return;
    const id = new URLSearchParams(location.hash.slice(1)).get('sticker');
    if (!id) { this.#detail.hide(); return; }
    const sticker = this.#catalog.resolve(id);
    if (sticker) this.#detail.show(sticker);
    else { this.#detail.hide(); this.#notices.show('这张表情已下架或不存在'); }
  }
}

if (typeof document !== 'undefined') new StickerWallApp().start();

