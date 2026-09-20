/* ============================================================
   公共工具 —— API 封装、DOM 辅助、字典缓存、侧边栏、提示
   ============================================================ */

const API = '/api';

/* 站点英文标题 —— **只有这一处定义**，侧栏与登录页共用（login.html 也引本文件）。
   改标题只改这里，避免两处各写一份而漂移。
   2026-09-20：由「After-sales Return System」改为含公司名与 Registration 的全称。 */
const SITE_TITLE_EN = 'Beiliang After-Sales Return Registration System';

/* ---------------- 请求 ---------------- */
async function api(path, opts = {}) {
  const opt = { headers: {}, ...opts };
  if (opt.body && typeof opt.body !== 'string') {
    if (typeof FormData !== 'undefined' && opt.body instanceof FormData) {
      // 文件上传：不能设置 Content-Type，需由浏览器自动补 multipart 边界
    } else {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(opt.body);
    }
  }
  const res = await fetch(path.startsWith('http') ? path : API + path, opt);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (res.status === 401) {
    // 会话失效（超时 / 被停用 / 改密后其它设备失效）——统一跳登录页，
    // 不在每个调用点各写一遍。登录页自身不跳，避免循环。
    if (typeof handleUnauthorized === 'function') handleUnauthorized();
    const err401 = new Error('登录状态已失效，请重新登录');
    err401.status = 401;
    throw err401;
  }
  if (!res.ok) {
    const d = data.detail;
    if (d && typeof d === 'object') {
      // 结构化错误（如重复登记会带回 duplicates 明细）
      const err = new Error(d.message || `请求失败 (${res.status})`);
      err.status = res.status;
      err.detail = d;
      throw err;
    }
    const msg = d || data.message || `请求失败 (${res.status})`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ---------------- 提示 ---------------- */
function toast(message, type = 'info', ms = 2600) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `toast toast--${type}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .2s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 220);
  }, ms);
}

/* ---------------- DOM 辅助 ---------------- */
function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v);
  }
  const list = Array.isArray(children) ? children : [children];
  for (const c of list) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, m =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

/* ---------------- 格式化 ---------------- */
function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function fmtDateTime(s) {
  if (!s) return '';
  return String(s).replace('T', ' ').slice(0, 19);
}

function fmtDate(s) {
  if (!s) return '';
  const t = String(s);
  return t.length >= 10 ? t.slice(0, 10) : t;
}

function num(v, digits = 0) {
  if (v === null || v === undefined || v === '') return '0';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/* ---------------- 状态标签 ---------------- */
function completionTag(v) {
  const s = String(v || '').trim();
  if (!s) return h('span', { class: 'tag' }, '未填写');
  if (s.includes('完结')) return h('span', { class: 'tag tag--success' }, s);
  return h('span', { class: 'tag tag--warning' }, s);
}

function matchTag(mode) {
  const map = {
    'history': ['tag--brand', '历史命中'],
    'model_dict': ['tag--info', '型号字典'],
  };
  return map[mode] || ['tag', '—'];
}

/* ---------------- 字典缓存 ---------------- */
const DictCache = {
  _data: null,
  async load(force = false) {
    if (!force && this._data) return this._data;
    this._data = await api('/dict');
    return this._data;
  },
  async get(field, keyword = '') {
    if (keyword) {
      const r = await api(`/dict/${field}?keyword=${encodeURIComponent(keyword)}&limit=50`);
      return r.options || [];
    }
    const all = await this.load();
    return (all[field] || []).map(o => o.value);
  },
  async getWithCount(field) {
    const all = await this.load();
    return all[field] || [];
  },
  invalidate() { this._data = null; },
};

/* ---------------- 可搜索下拉框 ---------------- */
/**
 * 可搜索下拉：输入关键字实时过滤库内已有值；库内没有时可直接录入新值，
 * 提交后由后端自动汇入字典候选，下次即可直接选。
 */
/* ---------------- 身份与权限 ----------------
   身份由后端从会话 Cookie 解析，前端只缓存一份用于：
     ① 按权限过滤导航入口（纯展示层，服务端仍会独立校验）
     ② 顶栏显示当前用户与权限组
     ③ 未登录 / 会话失效时统一跳转登录页
   注意：权限判定绝不能只靠前端 —— 服务端中间件才是真正的拦截点。 */

let CURRENT_USER = null;        // { username, display_name, group_name, perms: [] }
let AUTH_ENABLED = false;       // 登录验证总开关是否开启
let PENDING_PWD_CHANGE = false; // 当前用户是否处于「必须先改密」状态

/** 拉取当前身份（缓存；force=true 时强制刷新）。 */
async function loadIdentity(force = false) {
  if (CURRENT_USER && !force) return CURRENT_USER;
  try {
    const me = await api('/auth/me');
    AUTH_ENABLED = !!me.auth_enabled;
    CURRENT_USER = me.authenticated ? me.user : null;
    PENDING_PWD_CHANGE = !!(me.user && me.user.must_change_pwd);
  } catch {
    CURRENT_USER = null;
  }
  return CURRENT_USER;
}

/** 是否处于「首次登录必须改密」状态。 */
function mustChangePassword() { return PENDING_PWD_CHANGE; }

const PERM_LABELS_LOCAL = {
  'page.index': '工作台', 'page.scan': '退回登记', 'page.inspect': '检测登记',
  'page.handle': '处理登记', 'page.query': '明细查询', 'page.dashboard': '数据看板',
  'page.items': '匹配数据库', 'page.api': '数据接口', 'page.auth': '权限设置',
  'act.create': '新增登记', 'act.edit': '修改明细与照片', 'act.delete': '删除明细',
  'act.export': '导出数据', 'act.items': '物料维护', 'act.openapi': '数据接口配置',
  'act.user': '用户与权限组管理', 'act.settings': '系统开关',
};

/** 当前用户是否拥有某权限点。未启用鉴权时一律放行。 */
function hasPerm(key) {
  if (!AUTH_ENABLED) return true;
  if (!CURRENT_USER) return false;
  return (CURRENT_USER.perms || []).includes(key);
}

/** 会话失效的统一处理：跳登录页并把当前地址带上，登录后跳回来。 */
function handleUnauthorized() {
  if (location.pathname.endsWith('/login.html')) return;
  const next = location.pathname + location.search;
  location.replace('/login.html?next=' + encodeURIComponent(next));
}

function combobox(opts) {
  const {
    name, value = '', placeholder = '', allowNew = true, options = [],
    onChange, onEnter, className = '',
    // 远程候选：async (kw) => [{value, meta}]。给了它就在输入时防抖检索，
    // 结果与本地候选合并去重、排在最前（远程更权威，如匹配数据库的物料主档）。
    remoteSearch = null,
    searchDelay = 220,
    remoteMinChars = 2,
  } = opts;

  let items = options.map(o => (typeof o === 'string' ? { value: o } : o));
  let activeIdx = -1;
  // 远程检索状态：seq 用于丢弃过期响应（打字快时先发的可能后到）
  let remoteItems = [];
  let remoteTimer = null;
  let remoteSeq = 0;
  // 扫码枪识别：连续极快输入视为扫码，不弹面板
  let lastInputAt = 0;
  let fastStreak = 0;

  const input = h('input', {
    class: 'cb__input', type: 'text', autocomplete: 'off',
    placeholder, value, dataset: { field: name },
  });
  const panel = h('div', { class: 'cb__panel hidden' });
  const wrap = h('div', { class: ('cb ' + className).trim() }, [
    input, h('span', { class: 'cb__arrow' }, '▼'), panel,
  ]);

  // 面板内的 mousedown 不转移焦点，避免点击滚动条时输入框失焦导致面板关闭
  panel.addEventListener('mousedown', e => e.preventDefault());

  /**
   * 面板用 fixed 定位并跟随输入框：表格和内容区都是 overflow 容器，
   * absolute 定位会被裁剪，且空间不足时向上翻转。
   */
  function positionPanel() {
    if (panel.classList.contains('hidden')) return;
    const r = input.getBoundingClientRect();
    const gap = 3;
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const below = vh - r.bottom - gap - 10;
    const above = r.top - gap - 10;
    const openUp = below < 150 && above > below;

    let width = Math.max(r.width, 200);
    let left = r.left;
    if (left + width > vw - 8) left = Math.max(8, vw - width - 8);

    panel.style.left = left + 'px';
    panel.style.width = width + 'px';
    panel.style.maxHeight = Math.max(140, Math.min(280, openUp ? above : below)) + 'px';
    if (openUp) {
      panel.style.top = 'auto';
      panel.style.bottom = (vh - r.top + gap) + 'px';
    } else {
      panel.style.bottom = 'auto';
      panel.style.top = (r.bottom + gap) + 'px';
    }
  }

  function bindReposition(on) {
    const fn = on ? 'addEventListener' : 'removeEventListener';
    window[fn]('scroll', onAnyScroll, true);
    window[fn]('resize', positionPanel);
  }
  function onAnyScroll(e) {
    if (e.target === panel) return;   // 面板自身滚动不影响位置
    positionPanel();
  }

  function isNewCandidate() {
    const kw = input.value.trim();
    return allowNew && kw && !items.some(o => String(o.value) === kw);
  }
  function visibleList() {
    const kw = input.value.trim().toLowerCase();
    const local = kw
      ? items.filter(o => String(o.value).toLowerCase().includes(kw))
      : items;
    if (!remoteSearch || !kw || input.value.trim().length < remoteMinChars) {
      return local.slice(0, 300);
    }
    const merged = remoteItems.map(o => Object.assign({}, o, { __remote: true }));
    const seen = new Set(merged.map(o => String(o.value)));
    local.forEach(o => {
      if (!seen.has(String(o.value))) { seen.add(String(o.value)); merged.push(o); }
    });
    return merged.slice(0, 300);
  }

  /** 防抖检索远程候选；只保留最后一次请求的结果 */
  function scheduleRemote() {
    if (!remoteSearch) return;
    clearTimeout(remoteTimer);
    const kw = input.value.trim();
    if (kw.length < remoteMinChars) { remoteItems = []; return; }
    remoteTimer = setTimeout(async () => {
      const seq = ++remoteSeq;
      let list = [];
      try { list = (await remoteSearch(kw)) || []; } catch (e) { list = []; }
      if (seq !== remoteSeq) return;                 // 过期响应，丢弃
      remoteItems = list.map(o => (typeof o === 'string' ? { value: o } : o));
      renderPanel();
    }, searchDelay);
  }
  function renderPanel() {
    const list = visibleList();
    const nodes = [];
    if (isNewCandidate()) {
      nodes.push(h('div', {
        class: 'cb__item cb__item--new',
        onmousedown: e => { e.preventDefault(); pick(input.value.trim()); },
      }, [`＋ 使用新值「${input.value.trim()}」`]));
    }
    list.forEach((o, i) => {
      nodes.push(h('div', {
        class: 'cb__item' + (i === activeIdx ? ' is-active' : '')
          + (String(o.value) === input.value ? ' is-current' : ''),
        onmousedown: e => { e.preventDefault(); pick(o.value); },
      }, [
        h('span', { class: 'cb__val' }, String(o.value)),
        o.meta ? h('span', { class: 'cb__meta', title: String(o.meta) },
          String(o.meta)) : null,
        o.use_count ? h('span', { class: 'cb__cnt' }, o.use_count) : null,
      ]));
    });
    panel.replaceChildren(...(nodes.length ? nodes
      : [h('div', { class: 'cb__empty' }, '无匹配项')]));
    if (!panel.classList.contains('hidden')) positionPanel();
  }
  function open() {
    renderPanel();
    panel.classList.remove('hidden');
    activeIdx = -1;
    positionPanel();
    bindReposition(true);
  }
  function close() {
    if (panel.classList.contains('hidden')) return;
    panel.classList.add('hidden');
    activeIdx = -1;
    bindReposition(false);
  }
  function pick(v) {
    input.value = v ?? '';
    close();
    if (onChange) onChange(input.value);
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  /* 程序化聚焦（如「回车新增一行」后把光标放到新行的格子）时不该弹面板 ——
     面板只在用户点击 / 手动输入时出现。调用方先 suppresseOpenOnce() 再 focus()
     即可；用 setTimeout 兜底清标记，避免「聚焦时元素已聚焦、focus 事件没触发」
     导致标记残留、把下一次真正的聚焦也吞掉。
     注意 click 分支不设此限制：用户主动点格子，面板照旧弹出。 */
  let skipFocusOpen = false;
  wrap.suppressOpenOnce = () => {
    skipFocusOpen = true;
    setTimeout(() => { skipFocusOpen = false; }, 0);
  };
  input.addEventListener('focus', () => {
    if (skipFocusOpen) { skipFocusOpen = false; return; }
    open();
  });
  input.addEventListener('click', open);
  input.addEventListener('input', () => {
    const now = Date.now();
    const gap = now - lastInputAt;
    lastInputAt = now;
    fastStreak = gap < 35 ? fastStreak + 1 : 0;
    activeIdx = -1;
    // 扫码枪是「瞬间灌入」：连续极快输入时不弹候选面板 ——
    // 否则面板闪烁，回车还可能被面板抢去变成「选中第一项」，
    // 而扫码后应照旧触发自动回填。
    if (fastStreak >= 3) { close(); remoteItems = []; return; }
    open();
    scheduleRemote();
  });
  input.addEventListener('blur', () => setTimeout(close, 140));
  input.addEventListener('keydown', e => {
    const list = visibleList();
    const extra = isNewCandidate() ? 1 : 0;
    if (e.key === 'ArrowDown') {
      e.preventDefault(); open();
      activeIdx = Math.min(activeIdx + 1, list.length + extra - 1);
      renderPanel();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      renderPanel();
    } else if (e.key === 'Enter') {
      fastStreak = 0;                       // 扫码结束，恢复正常输入判定
      if (activeIdx >= 0) {
        e.preventDefault();
        // 在事件上留个记号：这次回车是「从候选里选一个」，不是「提交手打的
        // 值」。外层（如退回登记明细表的回车换行）据此跳过「回车开新行」——
        // 否则用键盘选中候选会顺手多出一行。事件对象会继续冒泡，所以记号
        // 能被委托在外层的监听器读到。
        e.cbPicked = true;
        if (extra && activeIdx === 0) pick(input.value.trim());
        else if (list[activeIdx - extra]) pick(list[activeIdx - extra].value);
      } else if (onEnter) {
        e.preventDefault();
        onEnter(input.value.trim());
      }
    } else if (e.key === 'Escape') {
      close();
    }
  });

  wrap.input = input;
  wrap.getValue = () => input.value.trim();
  wrap.setValue = v => { input.value = v ?? ''; };
  // 有的调用方按 `el.value` 取值（如明细查询编辑弹窗的通用保存逻辑），
  // 补一个同名访问器，避免它们读到 undefined 后把字段清空。
  Object.defineProperty(wrap, 'value', {
    get: () => input.value.trim(),
    set: v => { input.value = v ?? ''; },
    configurable: true,
  });
  wrap.setOptions = list =>
    { items = list.map(o => (typeof o === 'string' ? { value: o } : o)); };
  wrap.focus = () => input.focus();
  return wrap;
}

/** 可搜索下拉的远程候选检索 —— 各页面共用同一套来源约定
 *
 *  `f.source === 'items'` → **匹配数据库**（物料主档 1735 条，模糊匹配
 *      料号 / 旧料号 / 规格 / 型号 / 品名，候选带「品名 · 型号」副标题）
 *  其余                    → 该字段**历史登记过的值**（实时去重，非字典表，
 *      所以像 产品编号 / 生产年月 这类「不是字典字段」的也能有候选）
 */
async function fetchCandidates(f, kw) {
  const text = String(kw || '').trim();
  if (text.length < 2) return [];
  if (f.source === 'items') {
    const res = await api(`/items/search?kw=${encodeURIComponent(text)}&limit=20`);
    return res.items || [];
  }
  const res = await api(`/dict/${encodeURIComponent(f.name)}`
    + `?keyword=${encodeURIComponent(text)}&limit=30`);
  return (res.options || []).map(o => ({ value: o.value, use_count: o.use_count }));
}

/** 纯下拉：选项被锁定，不接受自由输入 */
/** 纯下拉（锁定选项）。
 *
 * placeholder 传 **null** 表示「不提供空选项」—— 用于「空值即默认值」的两值
 * 字段（如 ERP 处理：不是已处理就是待处理）。这类字段留一个「请选择」空选项
 * 是误导：用户选中它保存时会被校验拦下，看起来像按钮坏了。
 */
function fixedSelect({ name, value = '', options = [], onChange, placeholder = '请选择' }) {
  const sel = h('select', { class: 'input', dataset: { field: name } }, [
    placeholder === null ? null : h('option', { value: '' }, placeholder),
    ...options.map(o => h('option', {
      value: o, selected: o === value ? 'selected' : null,
    }, o)),
  ]);
  if (onChange) sel.addEventListener('change', () => onChange(sel.value));
  sel.getValue = () => sel.value.trim();
  sel.setValue = v => { sel.value = v ?? ''; };
  return sel;
}

/* ---------------- 照片证据：缩略图网格 + 上传 ---------------- */

/** 解析字段值 → { names, legacy }
 *  兼容三种形态：文件名数组 / 对象（照片 + 历史文本）/ 任意历史文本 */
function parsePhotoValue(raw) {
  const text = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!text) return { names: [], legacy: '' };
  if (text[0] === '[' || text[0] === '{') {
    try {
      const d = JSON.parse(text);
      if (Array.isArray(d)) {
        return { names: d.map(String).filter(s => s.trim()), legacy: '' };
      }
      if (d && typeof d === 'object' && ('photos' in d || 'legacy' in d)) {
        const list = Array.isArray(d.photos) ? d.photos : [];
        return {
          names: list.map(String).filter(s => s.trim()),
          legacy: String(d.legacy || '').trim(),
        };
      }
    } catch (e) { /* 解析失败则整体按历史文本处理 */ }
  }
  return { names: [], legacy: text };
}

function photoThumbName(name) {
  const i = String(name).lastIndexOf('.');
  return i < 0 ? name + '.thumb.jpg' : name.slice(0, i) + '.thumb.jpg';
}

/** 照片证据控件
 *  value   —— 字段当前值（文件名 JSON 或历史文本）
 *  limits  —— { max_per_record, max_mb, exts }，来自 /api/meta
 *  未改动时 getValue() 原样返回旧值，避免「只是打开看看」就把历史文本改写成新格式。
 */
function photoGrid({ name, detailKey = '', orderNo = '', value = '', limits = {}, onChange } = {}) {
  const maxCount = limits.max_per_record || 20;
  const maxMb = limits.max_mb || 10;

  let names = [];
  let legacy = '';
  let original = String(value === null || value === undefined ? '' : value);
  let dirty = false;

  const urlOf = n => `/photos/${encodeURIComponent(orderNo)}/${encodeURIComponent(n)}`;
  const thumbOf = n => `/photos/${encodeURIComponent(orderNo)}/${encodeURIComponent(photoThumbName(n))}`;

  const grid = h('div', { class: 'photo-grid' });
  const legacyBox = h('div', { class: 'photo-legacy' });
  const counter = h('span', { class: 'photo-field__count' });
  const fileInput = h('input', {
    type: 'file', accept: 'image/*', multiple: true,
    class: 'photo-field__file',
    onchange: ev => upload(ev.target.files),
  });
  const addBtn = h('button', {
    class: 'photo-add', type: 'button',
    onclick: () => {
      if (names.length >= maxCount) {
        toast(`每条明细最多 ${maxCount} 张，请先删除后再传`, 'warn');
        return;
      }
      fileInput.click();
    },
  }, ['＋ 上传照片']);

  // 隐藏域放在最前：万一调用方按「取内部 input」的通用逻辑取值，也能拿到当前值
  const hidden = h('input', { type: 'hidden', class: 'photo-field__value', name });
  const root = h('div', { class: 'photo-field', dataset: { field: name } },
    [hidden, grid, legacyBox, h('div', { class: 'photo-field__bar' }, [addBtn, counter, fileInput])]);

  // 拖拽上传
  ['dragenter', 'dragover'].forEach(t => root.addEventListener(t, ev => {
    ev.preventDefault();
    root.classList.add('is-dragover');
  }));
  ['dragleave', 'drop'].forEach(t => root.addEventListener(t, ev => {
    ev.preventDefault();
    if (t === 'dragleave' && root.contains(ev.relatedTarget)) return;
    root.classList.remove('is-dragover');
  }));
  root.addEventListener('drop', ev => {
    const files = ev.dataTransfer && ev.dataTransfer.files;
    if (files && files.length) upload(files);
  });

  function serialize() {
    if (!dirty) return original;
    if (!names.length && !legacy) return '';
    if (!legacy) return JSON.stringify(names);
    return JSON.stringify({ photos: names, legacy });
  }

  function render() {
    grid.replaceChildren(...names.map((n, i) => h('figure', {
      class: 'photo-thumb', title: n,
    }, [
      h('img', {
        src: thumbOf(n), alt: n, loading: 'lazy',
        onclick: () => openLightbox(n),
        onerror: ev => { ev.target.classList.add('is-broken'); },
      }),
      h('figcaption', {}, `${i + 1}`),
      h('button', {
        class: 'photo-thumb__del', type: 'button', title: '删除这张',
        onclick: ev => { ev.stopPropagation(); remove(n); },
      }, '×'),
    ])));

    legacyBox.replaceChildren(...(legacy ? [
      h('span', { class: 'photo-legacy__tag' }, '历史文本'),
      h('span', { class: 'photo-legacy__text mono', title: '金山文档内嵌图片引用，未随导入迁移' }, legacy),
    ] : []));
    legacyBox.classList.toggle('hidden', !legacy);

    counter.textContent = names.length
      ? `已上传 ${names.length} / ${maxCount} 张`
      : `最多 ${maxCount} 张，单张不超过 ${maxMb} MB`;
    addBtn.disabled = names.length >= maxCount;
    hidden.value = serialize();
  }

  function applyValue(raw) {
    const parsed = parsePhotoValue(raw);
    names = parsed.names;
    legacy = parsed.legacy;
    original = String(raw === null || raw === undefined ? '' : raw);
    dirty = false;
    render();
  }

  async function upload(fileList) {
    const list = Array.from(fileList || []);
    if (!list.length) return;
    if (!detailKey) { toast('请先保存记录后再上传照片', 'warn'); return; }
    const remain = maxCount - names.length;
    if (list.length > remain) {
      toast(`每条明细最多 ${maxCount} 张，还能再传 ${remain} 张`, 'warn');
      fileInput.value = '';
      return;
    }
    const fd = new FormData();
    list.forEach(f => fd.append('files', f, f.name));
    root.classList.add('is-busy');
    try {
      const res = await api(`/returns/${encodeURIComponent(detailKey)}/photos`,
        { method: 'POST', body: fd });
      const parsed = parsePhotoValue(res.value);
      names = parsed.names;
      legacy = parsed.legacy;
      dirty = true;
      original = res.value;
      render();
      toast(`已上传 ${res.added} 张照片`, 'success');
      if (onChange) onChange(res);
    } catch (e) {
      toast('上传失败：' + e.message, 'error');
    } finally {
      root.classList.remove('is-busy');
      fileInput.value = '';
    }
  }

  async function remove(n) {
    const ok = await confirmDialog(
      `确定删除照片 <b class="mono">${escapeHtml(n)}</b> 吗？删除后不可恢复。`,
      { title: '删除照片', danger: true });
    if (!ok) return;
    root.classList.add('is-busy');
    try {
      const res = await api(
        `/returns/${encodeURIComponent(detailKey)}/photos/${encodeURIComponent(n)}`,
        { method: 'DELETE' });
      const parsed = parsePhotoValue(res.value);
      names = parsed.names;
      legacy = parsed.legacy;
      dirty = true;
      original = res.value;
      render();
      toast('已删除该照片', 'success');
      if (onChange) onChange(res);
    } catch (e) {
      toast('删除失败：' + e.message, 'error');
    } finally {
      root.classList.remove('is-busy');
    }
  }

  function openLightbox(n) {
    const box = h('div', { class: 'photo-lightbox', onclick: () => close() }, [
      h('img', { class: 'photo-lightbox__img', src: urlOf(n), alt: n }),
      h('div', { class: 'photo-lightbox__cap' }, [
        h('span', { class: 'mono' }, n),
        h('a', {
          class: 'photo-lightbox__link', href: urlOf(n),
          download: n, onclick: ev => ev.stopPropagation(),
        }, '下载原图'),
        h('span', { class: 'muted' }, '点击任意处关闭'),
      ]),
    ]);
    function onKey(ev) { if (ev.key === 'Escape') close(); }
    function close() {
      document.removeEventListener('keydown', onKey);
      box.remove();
    }
    document.addEventListener('keydown', onKey);
    document.body.appendChild(box);
  }

  root.getValue = () => serialize();
  root.setValue = applyValue;
  // 部分调用方按 `el.value` 取值（明细查询页的通用逻辑），这里一并支持
  Object.defineProperty(root, 'value', {
    get: () => serialize(),
    set: v => applyValue(v),
    configurable: true,
  });

  applyValue(value);
  return root;
}

/** 一次性取多个字段的候选值，减少请求往返 */
async function loadDictMap(fields) {
  const all = await DictCache.load();
  const map = {};
  fields.forEach(f => { map[f] = all[f] || []; });
  return map;
}

/* ---------------- 侧边栏 ---------------- */
// perm 字段是「页面访问权」权限点：导航按当前用户权限过滤。
// 即便这里漏配，服务端中间件仍会拦住直接敲 URL 的访问 —— 前端只负责别把
// 点不通的入口摆出来。
const NAV_ITEMS = [
  { key: 'index', href: '/', label: '工作台', perm: 'page.index', icon: 'M3 12l9-8 9 8v8a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z' },
  { key: 'scan', href: '/scan.html', label: '退回登记', perm: 'page.scan', icon: 'M4 7V5a1 1 0 011-1h2M4 17v2a1 1 0 001 1h2M20 7V5a1 1 0 00-1-1h-2M20 17v2a1 1 0 01-1 1h-2M3 12h18' },
  { key: 'inspect', href: '/inspect.html', label: '检测登记', perm: 'page.inspect', icon: 'M9 4a5 5 0 100 10A5 5 0 009 4zM16.2 16.2L20 20M6.8 9.2l1.6 1.6 3-3' },
  { key: 'handle', href: '/handle.html', label: '处理登记', perm: 'page.handle', icon: 'M9 5h10M9 12h10M9 19h10M4.2 4.6l1.3 1.3 2-2M4.2 11.6l1.3 1.3 2-2M4.2 18.6l1.3 1.3 2-2' },
  { key: 'query', href: '/query.html', label: '明细查询', perm: 'page.query', icon: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4' },
  { key: 'dashboard', href: '/dashboard.html', label: '数据看板', perm: 'page.dashboard', icon: 'M4 19V5M4 19h16M8 19v-6M12 19V9M16 19v-3' },
  { key: 'items', href: '/items.html', label: '匹配数据库', perm: 'page.items', icon: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3' },
];

// 系统管理入口：原来只有「同步设置」，现拆为「数据接口」（对外拉取配置）
// 与「权限设置」（登录/权限组/用户/开关）。
const NAV_SECOND = [
  { key: 'api', href: '/api.html', label: '数据接口', perm: 'page.api', icon: 'M4 12a8 8 0 0113.7-5.6M20 12a8 8 0 01-13.7 5.6M17 3v4h-4M7 21v-4h4' },
  { key: 'auth', href: '/auth.html', label: '权限设置', perm: 'page.auth', icon: 'M12 3l7.5 3.2v5c0 4.4-3.1 8.2-7.5 9.3-4.4-1.1-7.5-4.9-7.5-9.3v-5zM9.2 12.2l1.9 1.9 3.7-3.7' },
];

function renderSidebar(activeKey) {
  const svg = d => h('svg', {
    class: 'nav__icon', viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', 'stroke-width': '1.7',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }, [h('path', { d: d })]);

  const item = it => h('a', {
    class: `nav__item${it.key === activeKey ? ' is-active' : ''}`,
    href: it.href,
  }, [svg(it.icon), h('span', {}, it.label)]);

  // 按权限过滤入口。当前页即使无权也保留（否则用户会以为页面坏了），
  // 但服务端已拦下越权访问，正常走不到这里。
  const visible = list => list.filter(
    it => !it.perm || it.key === activeKey || hasPerm(it.perm));

  const bar = h('aside', { class: 'sidebar' }, [
    h('div', { class: 'sidebar__brand' }, [
      // 主图标 = 公司商标的图形部分（不带下方文字：这里只有 26px，
      // 带文字必糊）。图形是亮蓝 #0080c8 —— 2026-09-20 侧栏由深色改浅色后
      // 重新核对过：亮蓝压在 #f6f8fa 上对比度约 3.6:1，达到图形类元素的门槛。
      h('img', { class: 'sidebar__logo', src: '/static/assets/logo.png?v=20260920f',
                 alt: '贝良', width: 26, height: 26 }),
      h('div', {}, [
        h('div', { class: 'sidebar__title' }, '售后返件系统'),
        h('div', { class: 'sidebar__sub' }, SITE_TITLE_EN),
      ]),
    ]),
    h('nav', { class: 'nav' }, [
      h('div', { class: 'nav__label' }, '业务操作'),
      ...visible(NAV_ITEMS).map(item),
      h('div', { class: 'nav__label' }, '系统'),
      ...visible(NAV_SECOND).map(item),
    ]),
    h('div', { class: 'sidebar__foot', id: 'side-status' }, [
      h('span', { class: 'dot dot--off' }), '连接中…',
    ]),
  ]);
  return bar;
}

/* ---------------- 顶栏用户区 ---------------- */
function userChip() {
  const name = CURRENT_USER
    ? (CURRENT_USER.display_name || CURRENT_USER.username) : '未登录';
  const group = CURRENT_USER ? CURRENT_USER.group_name : '';
  const initial = (name || '?').slice(0, 1);
  return h('div', { class: 'topbar__user' }, [
    h('div', { class: 'userchip', title: CURRENT_USER
      ? `${CURRENT_USER.username} · ${group}` : '未启用登录验证' }, [
      h('span', { class: 'userchip__avatar' }, initial),
      h('span', { class: 'userchip__text' }, [
        h('span', { class: 'userchip__name' }, name),
        group ? h('span', { class: 'userchip__group' }, group) : null,
      ]),
    ]),
    h('button', {
      class: 'btn btn--sm btn--ghost', type: 'button',
      title: '修改密码', onclick: openChangePassword,
    }, '改密'),
    h('button', {
      class: 'btn btn--sm btn--ghost', type: 'button',
      title: '退出登录', onclick: doLogout,
    }, '退出'),
  ]);
}

function openChangePassword() {
  const oldI = h('input', { class: 'input', type: 'password', autocomplete: 'current-password' });
  const newI = h('input', { class: 'input', type: 'password', autocomplete: 'new-password' });
  const repI = h('input', { class: 'input', type: 'password', autocomplete: 'new-password' });
  const err = h('div', { class: 'form-error hidden' });
  const btn = h('button', { class: 'btn btn--primary' }, '确认修改');

  const m = openModal({
    title: '修改密码', narrow: true,
    body: h('div', {}, [
      err,
      h('div', { class: 'field' }, [h('label', { class: 'field__label' }, '原密码'), oldI]),
      h('div', { class: 'field' }, [h('label', { class: 'field__label' }, '新密码'), newI]),
      h('div', { class: 'field' }, [h('label', { class: 'field__label' }, '确认新密码'), repI]),
      h('div', { class: 'small muted' }, '至少 4 位。修改后其它设备上的登录会立即失效。'),
    ]),
    footer: [h('button', { class: 'btn', onclick: () => m.close() }, '取消'), btn],
  });

  const fail = msg => { err.textContent = msg; err.classList.remove('hidden'); };
  btn.onclick = async () => {
    err.classList.add('hidden');
    if (!newI.value) return fail('请输入新密码');
    if (newI.value !== repI.value) return fail('两次输入的新密码不一致');
    btn.disabled = true;
    try {
      await api('/auth/password', { method: 'POST', body: {
        old_password: oldI.value, new_password: newI.value } });
      m.close();
      toast('密码已修改', 'success');
    } catch (e) {
      fail(e.message);
    } finally { btn.disabled = false; }
  };
}

async function doLogout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch { /* 已失效也无所谓 */ }
  location.href = '/login.html';
}

/* ---------------- 主框架 ---------------- */
async function mountLayout(activeKey, title, desc, toolbarNodes = []) {
  // 先拿身份：导航要按权限过滤，顶栏要显示当前用户。
  // 未登录时服务端已把页面请求重定向到登录页，正常不会走到这里。
  await loadIdentity();

  const layout = h('div', { class: 'layout' }, [
    renderSidebar(activeKey),
    h('div', { class: 'main' }, [
      h('header', { class: 'topbar' }, [
        h('div', {}, [
          h('div', { class: 'topbar__title' }, title),
          desc ? h('div', { class: 'topbar__desc' }, desc) : null,
        ]),
        h('div', { class: 'topbar__spacer' }),
        ...toolbarNodes,
        userChip(),
      ]),
      h('div', { class: 'content', id: 'content' }),
    ]),
  ]);
  document.body.replaceChildren(layout);

  // 越权跳转回来时给一句明确提示，否则用户只看到「什么都没发生」
  const denied = new URLSearchParams(location.search).get('denied');
  if (denied) {
    toast(`没有访问 ${denied} 的权限，已返回工作台`, 'warn', 4200);
    history.replaceState(null, '', location.pathname);
  }

  try {
    const s = await api('/health');
    const foot = $('#side-status');
    if (foot) {
      foot.replaceChildren(
        h('span', { class: 'dot dot--on' }),
        `已连接 · ${num(s.db.total)} 条`,
      );
    }
  } catch {
    const foot = $('#side-status');
    if (foot) foot.replaceChildren(h('span', { class: 'dot dot--off' }), '服务未连接');
  }
  return $('#content');
}

/* ---------------- 模态框 ---------------- */
function openModal({ title, body, footer, wide, narrow, onClose }) {
  const modal = h('div', {
    class: `modal${wide ? ' modal--wide' : ''}${narrow ? ' modal--narrow' : ''}`,
  }, [
    h('div', { class: 'modal__head' }, [
      h('div', { class: 'modal__title' }, title),
      h('button', { class: 'modal__close', onclick: close }, '×'),
    ]),
    h('div', { class: 'modal__body' }, body),
    footer ? h('div', { class: 'modal__foot' }, footer) : null,
  ]);
  const mask = h('div', {
    class: 'modal-mask',
    onclick: e => { if (e.target === mask) close(); },
  }, [modal]);

  function close() {
    mask.remove();
    document.removeEventListener('keydown', onKey);
    if (onClose) onClose();
  }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);
  document.body.appendChild(mask);
  return { close, modal, mask };
}

function confirmDialog(message, { title = '请确认', danger = false } = {}) {
  return new Promise(resolve => {
    /* 只接受第一次结果。
       关闭弹窗会触发 onClose → resolve(false)，所以必须「先定结果、再关闭」。
       早先的写法是 m.close(); resolve(true); —— 结果被 onClose 抢先解决成
       false，表现为「点了确定没反应」，所有依赖本函数的操作都不会执行。 */
    let settled = false;
    const done = v => { if (!settled) { settled = true; resolve(v); } };

    // message 支持 HTML 片段（调用方负责转义其中的用户数据），
    // 否则 <b>、<br> 会被当纯文本显示出来
    const body = h('div', { style: { fontSize: '13px', lineHeight: '1.7' } });
    body.innerHTML = message;

    const m = openModal({
      title, narrow: true,
      body,
      footer: [
        h('button', { class: 'btn', onclick: () => { done(false); m.close(); } }, '取消'),
        h('button', {
          class: `btn ${danger ? 'btn--danger' : 'btn--primary'}`,
          onclick: () => { done(true); m.close(); },
        }, '确定'),
      ],
      onClose: () => done(false),
    });
  });
}

/* ---------------- 折叠分区 ---------------- */
function section(title, badge, bodyNode, collapsed = false) {
  const sec = h('div', { class: `section${collapsed ? ' is-collapsed' : ''}` });
  const head = h('div', { class: 'section__head' }, [
    h('span', { class: 'section__title' }, title),
    badge ? h('span', { class: 'section__badge' }, badge) : null,
    h('span', { class: 'section__chev' }, '▼'),
  ]);
  head.addEventListener('click', () => sec.classList.toggle('is-collapsed'));
  sec.append(head, h('div', { class: 'section__body' }, bodyNode));
  return sec;
}

/* ---------------- 图表工具 ---------------- */
/* 图表色板 = GitHub Primer 的**数据可视化**专用色（data-viz emphasis 系列）。
 *
 * 为什么不用界面那套（--brand / --success）：界面色是为「文本与图标」调的，
 * 放到面积色块上会偏深偏闷；Primer 专门备了一组彩度更高、彼此区分度更大的
 * 数据色，正是给图表用的。顺序按「相邻色相差最大」排，这样默认分配
 * （第 i 个系列取第 i 个色）时相邻两项不会撞色。
 *
 * 注意：这里是**数据色**，不要跟 UI 主题色混用 —— 改主题时不必动它。 */
const CHART_COLORS = [
  '#006edb', '#30a147', '#eb670f', '#df0c24', '#894ceb',
  '#179b9b', '#ce2c85', '#b88700', '#527a29', '#d43511',
  '#a830e8', '#808fa3', '#167e53', '#856d4c', '#866e04',
];

function baseChartOptions(extra = {}) {
  return Object.assign({
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 320 },
    plugins: {
      legend: {
        labels: { font: { size: 11 }, boxWidth: 12, padding: 10, color: '#59636e' },
      },
      tooltip: {
        backgroundColor: '#25292e', titleFont: { size: 12 }, bodyFont: { size: 12 },
        padding: 9, cornerRadius: 6, displayColors: true,
      },
    },
    scales: {
      x: {
        ticks: { font: { size: 11 }, color: '#59636e', maxRotation: 0, autoSkip: true },
        grid: { display: false }, border: { color: '#d1d9e0' },
      },
      y: {
        beginAtZero: true,
        ticks: { font: { size: 11 }, color: '#59636e', precision: 0 },
        grid: { color: '#eff2f5' }, border: { display: false },
      },
    },
  }, extra);
}

/* 无数据占位 */
function ensureChartEmpty(box, isEmpty) {
  let ph = box.querySelector('.chart-empty');
  if (isEmpty) {
    if (!ph) {
      ph = h('div', { class: 'chart-empty' }, '暂无数据');
      box.appendChild(ph);
    }
  } else if (ph) ph.remove();
}

/* 已存在的图表实例统一管理，避免重复创建 */
const _charts = new Map();
function renderChart(canvasId, config) {
  const cv = document.getElementById(canvasId);
  if (!cv) return null;
  const box = cv.parentElement;
  const hasData = !!(config && config.data && config.data.datasets &&
    config.data.datasets.some(d => (d.data || []).some(v =>
      typeof v === 'object' ? (v && Object.values(v).some(x => x)) : !!v)));
  ensureChartEmpty(box, !hasData);
  if (_charts.has(canvasId)) { _charts.get(canvasId).destroy(); }
  if (!hasData) return null;
  // 单张图渲染失败不能往上抛：看板是「一个 forEach 画一页」，
  // 抛出去会中断循环，**后面所有图都不画**（表现为整页空白，
  // 却只看到一个 toast）。这里兜住并留日志，其余图照常绘制。
  try {
    const chart = new Chart(cv.getContext('2d'), config);
    _charts.set(canvasId, chart);
    return chart;
  } catch (e) {
    console.error('[图表] ' + canvasId + ' 渲染失败：', e);
    return null;
  }
}

/* ---------------- 键盘扫描枪支持 ---------------- */
/**
 * 全局扫码枪监听：扫码枪以高速键盘输入 + 回车结束。
 * 判定规则：连续输入间隔 < 35ms 且长度 >= 6，视为扫码枪输入。
 */
function attachScanner(onScan) {
  let buffer = '';
  let lastTime = 0;
  let timer = null;

  function submit() {
    const code = buffer.trim();
    buffer = '';
    if (code.length >= 4) onScan(code);
  }

  document.addEventListener('keydown', e => {
    const now = Date.now();
    const gap = now - lastTime;
    lastTime = now;

    // 焦点在输入框内时，交给输入框自身的监听处理
    const tag = (document.activeElement || {}).tagName;
    const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

    if (!inInput && e.key.length === 1) {
      if (gap > 120) buffer = '';
      buffer += e.key;
      clearTimeout(timer);
      timer = setTimeout(submit, 120);
    }
  });
}

/* 输入框内的扫码仿真（多字段自动识别） */
function attachInputScanner(inputEl, { onCode, minLength = 4, idleMs = 140 } = {}) {
  let timer = null;
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(timer);
      const v = inputEl.value.trim();
      if (v.length >= minLength) onCode(v);
      return;
    }
    clearTimeout(timer);
  });
  inputEl.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const v = inputEl.value.trim();
      if (v.length >= minLength) onCode(v, { auto: true });
    }, idleMs);
  });
}

/* ---------------- 下载 ---------------- */
function download(url) {
  const a = document.createElement('a');
  a.href = url;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 100);
}

function qs(obj) {
  const p = new URLSearchParams();
  Object.entries(obj || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) p.append(k, v);
  });
  return p.toString();
}
