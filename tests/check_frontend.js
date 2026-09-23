/**
 * 前端静态校验
 * 用途：检查内联 JS 语法、外链资源是否存在，避免运行时白屏。
 * 运行：node tests/check_frontend.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
// 2026-09-20：sync.html（同步设置）下线，改为 api.html（数据接口）；
// 新增 auth.html（权限设置）与 login.html（登录页，独立于主布局）
const PAGES = ['index.html', 'scan.html', 'inspect.html', 'handle.html',
  'query.html', 'dashboard.html', 'items.html',
  'delivery-apply.html', 'delivery-pending.html',
  'delivery-track.html', 'delivery-ledger.html', 'delivery-detail.html',
  'sync-tasks.html',
  'recycle.html',
  'api.html', 'auth.html', 'login.html'];

let pass = 0;
const fails = [];

function ok(msg) { pass++; console.log('  [PASS] ' + msg); }
function bad(msg) { fails.push(msg); console.log('  [FAIL] ' + msg); }

console.log('='.repeat(62));
console.log('  前端静态校验');
console.log('='.repeat(62));

/* 1. 公共脚本与资源 */
console.log('\n[1] 资源文件');
const assets = [
  'static/assets/app.css',
  'static/assets/common.js',
  'static/assets/lib/chart.umd.js',
  'static/assets/lib/zxing.min.js',
];
assets.forEach(a => {
  const p = path.join(ROOT, a);
  if (fs.existsSync(p) && fs.statSync(p).size > 0) {
    ok(`${a}  (${(fs.statSync(p).size / 1024).toFixed(0)} KB)`);
  } else {
    bad(`${a} 缺失或为空`);
  }
});

/* 2. 公共脚本语法 */
console.log('\n[2] 公共脚本语法');
try {
  new Function(fs.readFileSync(path.join(ROOT, 'static/assets/common.js'), 'utf8'));
  ok('common.js 语法正确');
} catch (e) {
  bad('common.js 语法错误: ' + e.message);
}

/* 3. 页面内联脚本语法 */
console.log('\n[3] 页面内联脚本');
const INLINE = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
const SRCS = /<script[^>]*\bsrc=["']([^"']+)["']/g;
const LINKS = /<link[^>]*\bhref=["']([^"']+)["']/g;
const assetVersions = new Set();

PAGES.forEach(page => {
  const file = path.join(ROOT, 'static', page);
  if (!fs.existsSync(file)) { bad(`${page} 不存在`); return; }
  const html = fs.readFileSync(file, 'utf8');

  let m, n = 0, broken = 0;
  INLINE.lastIndex = 0;
  while ((m = INLINE.exec(html)) !== null) {
    n++;
    const code = m[1].trim();
    if (!code) continue;
    try {
      new Function(code);
    } catch (e) {
      broken++;
      bad(`${page} 内联脚本 #${n} 语法错误: ${e.message}`);
    }
  }
  if (!broken) ok(`${page}  ${n} 段内联脚本语法正确`);

  // 外链资源存在性
  const refs = [];
  SRCS.lastIndex = 0;
  while ((m = SRCS.exec(html)) !== null) refs.push(m[1]);
  LINKS.lastIndex = 0;
  while ((m = LINKS.exec(html)) !== null) refs.push(m[1]);

  refs.filter(r => r.startsWith('/')).forEach(r => {
    const clean = r.split('?')[0];          // 去掉 ?v= 版本参数再判断文件是否存在
    const p = path.join(ROOT, clean.replace(/^\//, '').replace(/\//g, path.sep));
    if (!fs.existsSync(p)) bad(`${page} 引用了不存在的资源: ${r}`);
    if (clean.startsWith('/static/assets/')) {
      const vm = /\?v=([^&]+)/.exec(r);
      if (!vm) bad(`${page} 的资源引用缺少 ?v= 版本号: ${r}`);
      else assetVersions.add(vm[1]);
    }
  });
});

/* 3b. 版本号一致性：各页面应使用同一个版本号，防止漏改 */
console.log('\n[3b] 静态资源版本号');
if (assetVersions.size === 1) {
  ok(`各页面版本号一致：${[...assetVersions][0]}`);
} else if (assetVersions.size === 0) {
  bad('未检测到任何 ?v= 版本号');
} else {
  bad(`各页面版本号不一致：${[...assetVersions].join(' / ')}（改前端资源后需统一递增）`);
}

/* 4. 关键 API 路径一致性 */
console.log('\n[4] 前后端接口一致性');
const appSrc = fs.readFileSync(path.join(ROOT, 'app.py'), 'utf8');
/* 3c. 站点图标（favicon）现在**真的**能取到
 *
 * 背景：PUBLIC_PATHS 里一直写着 "/favicon.ico"（本意是「放行它」），
 * 但服务端**从来没有这条路由** —— `/{page}.html` 只匹配 .html 结尾的路径，
 * 所以 /favicon.ico 落到 404，而日志里看不出任何毛病（HTTP 404 不是异常）。
 * 浏览器每次开页面都在拉一个不存在的东西，一直没人发现。
 * 这里同时守住「文件在」与「有路由」两件事：少任何一件都是 404。 */
console.log('\n[3c] 站点图标');
const iconFile = path.join(ROOT, 'favicon.ico');
if (fs.existsSync(iconFile)) {
  const sz = fs.statSync(iconFile).size;
  ok(`favicon.ico 存在于项目根（${sz} 字节）`);
} else {
  bad('favicon.ico 不在项目根 —— /favicon.ico 会 404（路由按 BASE_DIR 取文件）');
}
if (/@app\.get\(\s*"\/favicon\.ico"/.test(appSrc)) {
  ok('后端有 /favicon.ico 路由');
} else {
  bad('后端缺少 /favicon.ico 路由（白名单里放行了它，但没人处理它）');
}
{
  // 每个页面都应引用图标；否则那一页的页签是空白默认图标
  const noIcon = PAGES.filter(p => {
    const h = fs.readFileSync(path.join(ROOT, 'static', p), 'utf8');
    return !/rel="icon"/.test(h);
  });
  if (noIcon.length === 0) ok(`全部 ${PAGES.length} 个页面都引用了 favicon`);
  else bad(`未引用 favicon 的页面：${noIcon.join(', ')}`);
}
{
  // 主图标图片必须在，且被两个入口引用（侧边栏 + 登录页）
  const png = path.join(ROOT, 'static', 'assets', 'logo.png');
  if (!fs.existsSync(png)) {
    bad('static/assets/logo.png 不存在（主图标图）');
  } else {
    const common = fs.readFileSync(path.join(ROOT, 'static', 'assets', 'common.js'), 'utf8');
    const login = fs.readFileSync(path.join(ROOT, 'static', 'login.html'), 'utf8');
    const used = /assets\/logo\.png/.test(common) && /assets\/logo\.png/.test(login);
    if (used) ok('主图标被侧边栏与登录页共同引用');
    else bad('主图标未被侧边栏或登录页引用（换了图但没接上）');
  }
}

const routes = new Set();
const ROUTE = /@app\.(get|post|put|delete)\(\s*"([^"]+)"/g;
let r;
while ((r = ROUTE.exec(appSrc)) !== null) routes.add(r[2]);

const used = new Set();
[...PAGES, 'assets/common.js'].forEach(f => {
  const p = f.startsWith('assets') ? path.join(ROOT, 'static', f)
    : path.join(ROOT, 'static', f);
  if (!fs.existsSync(p)) return;
  const src = fs.readFileSync(p, 'utf8');
  const CALL = /api\(\s*[`'"]([^`'"?]+)/g;
  let c;
  while ((c = CALL.exec(src)) !== null) used.add(c[1]);
});

let mismatched = 0;
used.forEach(u => {
  if (u.startsWith('http')) return;
  const norm = u.replace(/\/$/, '') || '/';
  const hit = [...routes].some(rt => {
    const base = rt.replace(/\{[^}]+\}/g, '');
    return rt === norm || base === norm ||
      (base.endsWith('/') && norm.startsWith(base));
  });
  if (!hit) { mismatched++; bad(`前端调用了未定义接口: ${u}`); }
});
if (!mismatched) ok(`前端调用的 ${used.size} 个接口在后端均有定义`);

/* 5. 启动脚本与文档一致性
 *
 * 起因：README 里早就写了 `start.bat --lan` / `--public`，但脚本从没实现过 ——
 * 照着文档敲只会静默按本机启动，没有任何报错。这类「文档承诺了、脚本没做」
 * 的偏差只能靠断言守住，人工核对迟早会漏。
 */
console.log('\n[5] 启动脚本与文档一致性');
const launchers = [
  { file: 'start.bat', label: 'start.bat' },
  { file: 'start.sh', label: 'start.sh' },
];
const SWITCHES = ['--lan', '--public', '--local'];
const supported = {};

/* 只认「真实实现」，不认注释里的用法说明。
   踩过：脚本头部 Usage 注释里写着 `start.bat --local`，于是把实现那行删掉
   后断言依然全绿 —— 一个纯粹的假绿。注释行必须排除。 */
function codeOnly(raw, file) {
  return raw.split(/\r?\n/).filter(l => {
    const t = l.trim();
    if (!t) return false;
    if (file === 'start.bat') return !/^rem\b/i.test(t) && !t.startsWith('::');
    return !t.startsWith('#');
  }).join('\n');
}

launchers.forEach(({ file, label }) => {
  const p = path.join(ROOT, file);
  if (!fs.existsSync(p)) { bad(`${file} 不存在`); return; }
  const code = codeOnly(fs.readFileSync(p, 'utf8'), file);
  supported[file] = SWITCHES.filter(sw => code.includes(sw));
  const missing = SWITCHES.filter(sw => !code.includes(sw));
  if (missing.length) bad(`${label} 缺少开关实现: ${missing.join(' ')}`);
  else ok(`${label} 实现了 ${SWITCHES.join(' / ')}`);
});

// 两个平台档位必须一致 —— 否则「Windows 上能用、Linux 上不行」这类
// 差异只有真到换平台时才暴露
const diff = SWITCHES.filter(
  sw => supported['start.bat'].includes(sw) !== supported['start.sh'].includes(sw));
if (diff.length) bad(`start.bat 与 start.sh 档位不一致: ${diff.join(' ')}`);
else ok('start.bat 与 start.sh 档位一致（两平台行为相同）');

const batRaw = fs.existsSync(path.join(ROOT, 'start.bat'))
  ? fs.readFileSync(path.join(ROOT, 'start.bat'), 'utf8') : '';
if (batRaw) {
  // 纯 ASCII：非 ASCII 字符在非 65001 代码页下会乱码，甚至让 rem 行之后的
  // 命令解析错位（注释里写中文是最常见的踩法）。
  const nonAscii = [...batRaw].filter(c => c.charCodeAt(0) > 127);
  if (nonAscii.length) bad(`start.bat 含 ${nonAscii.length} 个非 ASCII 字符（代码页不安全）`);
  else ok('start.bat 为纯 ASCII（任意代码页下安全）');

  // CRLF：LF-only 的 .bat 在括号块 / label 跳转时会出现难查的解析怪象，
  // 而「带 for + if 括号块」正是本次加的东西。
  const lf = (batRaw.match(/\n/g) || []).length;
  const crlf = (batRaw.match(/\r\n/g) || []).length;
  if (crlf !== lf) bad(`start.bat 行尾混用（CRLF ${crlf} / LF 总计 ${lf}）`);
  else ok(`start.bat 行尾统一为 CRLF（${crlf} 行）`);

  // 两个地址独立：--public 必须同时把数据接口也放开，--lan 必须不动它
  const pubBlock = (batRaw.match(/--public"[\s\S]{0,200}/) || [''])[0];
  if (!/ARS_OPEN_API_HOST/.test(pubBlock)) {
    bad('start.bat 的 --public 没有放开数据接口（ARS_OPEN_API_HOST）');
  } else ok('start.bat 的 --public 同时放开主界面与数据接口');

  const lanLines = batRaw.split('\n').filter(l => l.includes('--lan"'));
  if (lanLines.some(l => /ARS_OPEN_API_HOST/.test(l))) {
    bad('start.bat 的 --lan 误改了数据接口地址（两档应各自独立）');
  } else ok('start.bat 的 --lan 不动数据接口地址（两地址相互独立）');
}

// README 提到的每个启动用法，脚本里都必须真的实现
const readme = fs.readFileSync(path.join(ROOT, 'README.md'), 'utf8');
const DOC = /start\.(bat|sh)\s+(--[a-z]+)/g;
const claimed = new Set();
let m2;
while ((m2 = DOC.exec(readme))) claimed.add(`${m2[1]}|${m2[2]}`);
if (!claimed.size) bad('README 里没有出现任何启动脚本用法');
claimed.forEach(c => {
  const [ext, sw] = c.split('|');
  const file = `start.${ext}`;
  const raw = fs.existsSync(path.join(ROOT, file))
    ? fs.readFileSync(path.join(ROOT, file), 'utf8') : '';
  if (!raw.includes(sw)) bad(`README 写了 ${file} ${sw}，但脚本未实现`);
});
if (claimed.size) {
  ok(`README 提到的 ${claimed.size} 个启动用法在脚本中均已实现`);
}

/* 6. confirmDialog 的插值必须转义
 *
 * confirmDialog 内部用 innerHTML 渲染（为的是支持 <b> / <br> 这类排版），
 * 所以调用方拼进去的用户数据必须走 escapeHtml。
 * 踩过：auth.html 的「删除账号 / 删除权限组」直接拼了 ${u.username} 与
 * ${g.name} —— 而用户名是可以原样存 `<b>粗体</b>` 的（已实测），
 * 轻则弹窗显示错乱，重则构成存储型 XSS。
 */
console.log('\n[6] confirmDialog 插值转义');
// strEnd / balancedArg 定义在下方 [7] 段之前 —— 函数声明会提升，这里可先用。
let dlgChecked = 0, dlgBad = [];
// common.js 里也有调用点（删照片的确认框），一并扫
const dlgFiles = PAGES.map(p => ({ label: p, rel: 'static/' + p }))
  .concat([{ label: 'common.js', rel: 'static/assets/common.js' }]);
dlgFiles.forEach(({ label, rel }) => {
  const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  let idx = 0;
  while ((idx = src.indexOf('confirmDialog(', idx)) !== -1) {
    // 只扫**实参本身**里的 ${...}。
    // 踩过：早先用的是「调用点前后固定长度窗口」，把紧邻的**别的**代码
    // （例如下方的 `tr.title = \`本行同步于 ${r.sync_at}\``）也算了进来，
    // 报出一个并不存在的注入点。赋给 DOM 属性本来就是安全的，窗口法分不清，
    // 只会制造假红 —— 假红比漏报更糟，它会让人开始忽略整条断言。
    const openParen = src.indexOf('(', idx);
    const argText = openParen < 0 ? '' : balancedArg(src, openParen);

    // 「先算好再拼」的写法（`const preview = …escapeHtml(k)…`）转义证据在
    // 调用点之前，所以往前仍留一段窗口找证据。
    const before = src.slice(Math.max(0, idx - 300), idx);
    const interps = [...argText.matchAll(/\$\{([^}]*)\}/g)].map(m => m[1]);
    const safe = x => /escapeHtml\s*\(/.test(x)
      || /\.length\s*$/.test(x.trim())
      || /^\s*\d+\s*$/.test(x);
    const risky = interps.filter(x => !safe(x));
    const hasEscape = /escapeHtml\s*\(/.test(before)
      || /escapeHtml\s*\(/.test(argText);
    dlgChecked++;
    if (risky.length && !hasEscape) {
      dlgBad.push(`${label}: \${${risky[0]}}`.slice(0, 48));
    }
    idx += 14;
  }
});
// 本文件用的是 ok()/bad()，没有 check()（那是 check_ui.js 的函数）
if (dlgChecked >= 5) ok(`扫描了 ${dlgChecked} 处 confirmDialog 调用`);
else bad(`只扫描到 ${dlgChecked} 处 confirmDialog 调用（预期 ≥5，选择器可能失效）`);
if (dlgBad.length) {
  bad(`confirmDialog 里有未转义的用户数据：${dlgBad.join(' · ')}`);
} else {
  ok('confirmDialog 的插值均已转义（无 HTML 注入）');
}

/* 7. openModal 的实参必须是它真的认识的键
 *
 * 起因：items.html 的「修改连接配置」弹窗写成
 *     openModal({ title, body, buttons: [...] })
 * 而 common.js 的签名是
 *     function openModal({ title, body, footer, wide, narrow, onClose })
 * —— 根本没有 buttons。多出来的键被**静默忽略**：不报错、不抛异常，
 * 结果是弹窗一个按钮都没有，用户在那一页根本存不了配置。
 * 这类「键名写错」的 bug 语法完全合法，语法检查抓不到，只能拿调用点与签名比对。
 */

// 从 s[i]（应是一个引号）跳到该字符串结束后一位。
// 模板串里的 ${...} 用递归扫描：它内部的引号/花括号不能影响外层配对。
function strEnd(s, i) {
  const q = s[i];
  i += 1;
  while (i < s.length) {
    const c = s[i];
    if (c === '\\') { i += 2; continue; }
    if (q === '`') {
      if (c === '`') return i + 1;
      if (c === '$' && s[i + 1] === '{') {
        let d = 1; i += 2;
        while (i < s.length && d > 0) {
          if (s[i] === '\\') { i += 2; continue; }
          if (s[i] === '"' || s[i] === "'" || s[i] === '`') { i = strEnd(s, i); continue; }
          if (s[i] === '{') d += 1;
          else if (s[i] === '}') d -= 1;
          i += 1;
        }
        continue;
      }
      i += 1;
      continue;
    }
    if (c === q) return i + 1;
    i += 1;
  }
  return i;
}

// 取 src[openIdx]（应是 '('）配对的**实参文本**（不含最外层括号）。
// 用来把断言的范围收在调用本身，而不是「调用点附近的一段代码」。
function balancedArg(src, openIdx) {
  let depth = 0, i = openIdx;
  while (i < src.length) {
    const c = src[i];
    if (c === '"' || c === "'" || c === '`') { i = strEnd(src, i); continue; }
    if (c === '(' || c === '[' || c === '{') { depth += 1; i += 1; continue; }
    if (c === ')' || c === ']' || c === '}') {
      depth -= 1; i += 1;
      if (depth === 0) return src.slice(openIdx + 1, i - 1);
      continue;
    }
    i += 1;
  }
  return src.slice(openIdx + 1);
}

// 取 src[start]（应是 '{'）这一层对象的**顶层键**。
// 只在「上一个有效字符是 { 或 ,」时认键，避免把三元表达式的 `a : b` 当键。
function topKeys(src, start) {
  const keys = [];
  let depth = 0, i = start, prev = '';
  while (i < src.length) {
    const c = src[i];
    if (c === '"' || c === "'" || c === '`') { i = strEnd(src, i); continue; }
    if (c === '/' && src[i + 1] === '/') {
      const e = src.indexOf('\n', i); if (e < 0) return keys; i = e; continue;
    }
    if (c === '/' && src[i + 1] === '*') {
      const e = src.indexOf('*/', i + 2); i = e < 0 ? src.length : e + 2; continue;
    }
    if (c === '{' || c === '[' || c === '(') { depth += 1; i += 1; prev = c; continue; }
    if (c === '}' || c === ']' || c === ')') {
      depth -= 1; i += 1;
      if (depth <= 0) return keys;
      prev = c; continue;
    }
    if (depth === 1 && (prev === '{' || prev === ',')) {
      const m = /^\s*([A-Za-z_$][\w$]*)\s*:/.exec(src.slice(i));
      if (m) { keys.push(m[1]); i += m[0].length; prev = ':'; continue; }
    }
    if (!/\s/.test(c)) prev = c;
    i += 1;
  }
  return keys;
}

console.log('\n[7] openModal 实参键名');
{
  const commonSrc = fs.readFileSync(path.join(ROOT, 'static/assets/common.js'), 'utf8');
  const sig = /function openModal\(\{([^}]*)\}\)/.exec(commonSrc);
  const accepted = sig ? sig[1].split(',').map(x => x.trim()).filter(Boolean) : [];
  if (!accepted.length) {
    bad('未能解析 openModal 的签名（被改名或改写了？）');
  } else {
    if (!accepted.includes('footer')) bad('openModal 签名里没有 footer —— 弹窗将无处放按钮');
    else ok(`openModal 接受键：${accepted.join(' / ')}`);
    if (accepted.includes('buttons')) bad('openModal 签名里出现了 buttons（与既有调用点约定冲突）');
  }

  const callFiles = fs.readdirSync(path.join(ROOT, 'static'))
    .filter(f => f.endsWith('.html')).map(f => 'static/' + f)
    .concat(['static/assets/common.js']);
  const offenders = [];
  let calls = 0;
  callFiles.forEach(rel => {
    const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    let idx = -1;
    while ((idx = src.indexOf('openModal(', idx + 1)) !== -1) {
      if (/function\s+$/.test(src.slice(Math.max(0, idx - 20), idx))) continue;
      const brace = src.indexOf('{', idx);
      if (brace < 0) continue;
      const brace2 = (src.slice(idx, brace).match(/\(/g) || []).length;
      if (brace2 !== 1) continue;   // 第一个参数不是对象字面量，跳过
      const keys = topKeys(src, brace);
      if (!keys) continue;
      calls += 1;
      keys.filter(k => !accepted.includes(k)).forEach(k => {
        offenders.push(`${rel}: openModal 收到未知键 "${k}"（会被静默忽略）`);
      });
    }
  });
  if (calls < 15) bad(`只扫到 ${calls} 处 openModal 调用（预期 ≥15，选择器可能失效）`);
  else ok(`扫描了 ${calls} 处 openModal 调用`);
  offenders.forEach(bad);
  if (!offenders.length) ok('所有 openModal 调用都只用了签名里存在的键');
}

/* 8. 发货明细页的列 / 状态副本必须与后端一致
 *
 * 页面是静态 HTML，读不到 config.py 的常量，COLUMNS 必然是一份副本。
 * 副本与源不一致不会有任何报错：多半是后端加了列、页面没跟上（或反过来），
 * 表现为「导出里有、界面里没有」这类要靠人工对数才发现的问题。
 */
console.log('\n[8] 发货明细列定义一致');
{
  const cfgSrc = fs.readFileSync(path.join(ROOT, 'config.py'), 'utf8');
  const cfgCols = /SHIP_DETAIL_COLUMNS\s*=\s*\(([\s\S]*?)\n\)/.exec(cfgSrc);
  const detailSrc = fs.readFileSync(
    path.join(ROOT, 'static', 'delivery-detail.html'), 'utf8');
  const jsCols = /const COLUMNS = \[([\s\S]*?)\];/.exec(detailSrc);
  if (!cfgCols || !jsCols) {
    bad('未能解析 SHIP_DETAIL_COLUMNS 或页面 COLUMNS（选择器失效）');
  } else {
    const py = [...cfgCols[1].matchAll(/\("([a-z_]+)"/g)].map(m => m[1]);
    const js = [...jsCols[1].matchAll(/\['([a-z_]+)'/g)].map(m => m[1]);
    if (py.length < 10) bad(`config.py 只解析出 ${py.length} 列（预期 14）`);
    else if (py.join() !== js.join()) {
      bad(`发货明细列不一致 —— config.py: ${py.join(',')} ／ 页面: ${js.join(',')}`);
    } else ok(`config.py 与发货明细页都是 ${py.length} 列，顺序一致`);
  }
  const pySt = /SHIP_DETAIL_STATUS_ORDER\s*=\s*\[([^\]]*)\]/.exec(cfgSrc);
  const jsSt = /const STATUS_ORDER = \[([^\]]*)\]/.exec(detailSrc);
  if (!pySt || !jsSt) bad('未能解析状态顺序常量（选择器失效）');
  else {
    const strip = s => s.replace(/["'\s]/g, '');
    if (strip(pySt[1]) !== strip(jsSt[1])) {
      bad(`发货明细状态顺序不一致：config.py ${pySt[1]} ／ 页面 ${jsSt[1]}`);
    } else ok('发货明细状态页签顺序与后端一致');
  }
}

/* 9. 每个页面都要在侧边栏里有入口
 *
 * 加了页面却忘了加导航时，页面本身能打开（直接敲 URL 进得去），
 * 只是**没有人找得到它** —— 不报错，只会被当成「功能没做」。
 */
console.log('\n[9] 侧边栏入口完备');
{
  const commonSrc = fs.readFileSync(path.join(ROOT, 'static/assets/common.js'), 'utf8');
  const navBlock = commonSrc.slice(commonSrc.indexOf('const NAV_ITEMS'),
    commonSrc.indexOf('function renderSidebar'));
  const hrefs = new Set([...navBlock.matchAll(/href:\s*'\/([A-Za-z0-9_.-]+\.html)'/g)]
    .map(m => m[1]));
  if (/href:\s*'\/'/.test(navBlock)) hrefs.add('index.html');
  const missing = PAGES.filter(p => p !== 'login.html' && !hrefs.has(p));
  if (missing.length) bad(`没有侧边栏入口的页面：${missing.join(', ')}`);
  else ok(`${PAGES.length - 1} 个页面都有侧边栏入口（login.html 独立无导航）`);
  const ghost = [...hrefs].filter(h => !PAGES.includes(h));
  if (ghost.length) bad(`侧边栏指向了不在页面清单里的文件：${ghost.join(', ')}`);
  else ok('侧边栏没有指向不存在页面的入口');
}

/* 10. 页面内联样式：CSS 变量必须存在、表格必须拿得到基础样式
 *
 * 这一节是被一次真实的「界面变形」逼出来的 —— 三套自检当时**全绿**，
 * 缺陷是用户看出来的：
 *   ① 发货明细页写了 `table.sd-t { width: 100%; min-width: 1560px; ... }`。
 *      app.css 的「表格」段明确写过 width:100% 的坏处：列一多就被压得比表头
 *      文字还窄，而表头是 nowrap 且没有截断规则，文字会溢出压到隔壁列上。
 *   ② 更糟的是那张表**没带 `.table`**：内边距、分隔线、吸顶表头全都拿不到，
 *      看起来就是「挤成一坨、跟别的页面不一样」。
 *   ③ 五个发货页里还引用了 4 个 `:root` 根本不存在的变量
 *      （--text / --mono / --danger-bg / --border-subtle）。自定义属性不存在时，
 *      那条声明会在「计算值阶段」失效：border-bottom 变 none、background 变透明、
 *      font-family 与 color 变继承 —— 全都不报错、不抛异常。
 *
 * 所以这里只做两件「机械可判」的事：变量名必须能找到定义；
 * 带类名的 <table> 必须有基础样式来源（`.table` 之类的基类，或自己的内边距）。
 */
console.log('\n[10] 页面内联样式：变量存在性 + 表格基础类');
{
  const cssSrc = fs.readFileSync(path.join(ROOT, 'static/assets/app.css'), 'utf8');
  const globalVars = new Set(
    [...cssSrc.matchAll(/(--[a-z0-9-]+)\s*:/g)].map(m => m[1]));
  // 允许页面自定义基类（局部内联表用它自己的样式，不需要 .table）
  const BASE_TABLE = ['table', 'inspect-table', 'line-table', 'stat-table'];

  let varChecked = 0, tableChecked = 0, nakedChecked = 0;
  const varBad = [], tblBad = [], widthBad = [], nakedBad = [];

  for (const page of PAGES) {
    const rel = 'static/' + page;
    const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    // 只看页面自己的 <style>；注释先剥掉 —— 说明文字里的变量名不是真实引用
    const css = [...src.matchAll(/<style>([\s\S]*?)<\/style>/g)]
      .map(m => m[1].replace(/\/\*[\s\S]*?\*\//g, '')).join('\n');
    const localVars = new Set([...css.matchAll(/(--[a-z0-9-]+)\s*:/g)].map(m => m[1]));

    for (const m of css.matchAll(/var\((--[a-z0-9-]+)\)/g)) {
      varChecked++;
      if (!globalVars.has(m[1]) && !localVars.has(m[1])) {
        varBad.push(`${rel} 引用了不存在的 ${m[1]}`);
      }
    }

    for (const m of src.matchAll(/h\('table',\s*\{([^}]*)\}/g)) {
      const cls = /class:\s*'([^']*)'/.exec(m[1]);
      if (!cls) {
        // 无类名的嵌套表（弹窗里的 miniTable）由页面自己的**后代选择器**负责：
        // 必须存在一条「… td / … th { padding }」—— 否则它就是一张裸表，
        // 单元格全零内边距，跟上面那条 `.table` 缺失是同一种病。
        nakedChecked++;
        const styled = /([^{}]+)\{([^{}]*)\}/g;
        let hit = false;
        for (const rule of css.matchAll(styled)) {
          if (!/padding/.test(rule[2])) continue;
          if (rule[1].split(',').some(s => /(^|\s)(td|th)(::?[\w-]+)?\s*$/.test(s.trim()))) {
            hit = true; break;
          }
        }
        if (!hit) nakedBad.push(`${rel} 有无类名的 <table>，但页面样式里没给 td/th 写内边距`);
        continue;   // 无类名的嵌套表由页面自己的样式负责
      }
      const list = cls[1].split(/\s+/).filter(Boolean);
      if (!list.length) continue;
      tableChecked++;
      if (list.some(c => BASE_TABLE.includes(c))) continue;
      // 没带基类也行 —— 只要页面样式里真的给它的单元格写了内边距。
      // ★ 类名后面必须加边界断言：`\.sd\-t` 会命中 `.sd-tab` 这个**另一个**类，
      //   而 `.sd-tab` 恰好有 padding —— 不加的话这条守卫就是永远 PASS 的假绿
      //   （负向测试当场抓到过一次）。
      const selfStyled = list.some(c =>
        new RegExp('\\.' + c.replace(/-/g, '\\-') + '(?![\\w-])[^{}]*\\{[^}]*padding').test(css));
      if (!selfStyled) {
        tblBad.push(`${rel} 的 <table class="${cls[1]}"> 既没带基础类、也没写自己的内边距`);
      }
    }

    // `table.xxx { width: 100% }` —— app.css:271-274 专门写过这条的坏处：
    // 列一多就被压得比表头文字还窄，而表头是 nowrap 且没有截断规则，
    // 文字会溢出压到隔壁列上（界面变形）。列宽应当由 `.table` 的
    // `width: max-content; min-width: 100%` 决定；要保底就只写 min-width。
    // ★ 必须判「选择器的**主体**就是这个 table」：`table.dlv-dt .cell-in`
    //   是在给单元格里的输入框写 width:100%，那是对的，不能一起报。
    for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      if (!/width:\s*100%/.test(rule[2])) continue;
      for (const sel of rule[1].split(',')) {
        if (/table\.[\w-]+(::?[\w-]+(\([^)]*\))?)?$/.test(sel.trim())) {
          widthBad.push(`${rel} 的 ${sel.trim().replace(/\s+/g, ' ')} 写了 width:100%`);
        }
      }
    }
  }

  if (varBad.length) {
    bad(`页面引用了未定义的 CSS 变量（${varBad.length} 处）：\n      ` +
      varBad.join('\n      '));
  } else ok(`${varChecked} 处 CSS 变量引用都能找到定义`);

  if (tblBad.length) {
    bad(`表格缺少基础样式（${tblBad.length} 处）：\n      ` + tblBad.join('\n      '));
  } else ok(`${tableChecked} 个带类名的 <table> 都有基础样式来源`);

  if (nakedBad.length) {
    bad(`无类名的嵌套表没有单元格内边距（${nakedBad.length} 处）：\n      ` +
      nakedBad.join('\n      '));
  } else ok(`${nakedChecked} 个无类名的嵌套表都由页面的 td/th 规则兜底`);

  if (widthBad.length) {
    bad(`表格被写死 width:100%（${widthBad.length} 处，列会被挤变形）：\n      ` +
      widthBad.join('\n      '));
  } else ok('没有表格被写死 width:100%（列宽交给 .table 的 max-content）');
}

/* 11. 两个下拉构造器的方法面必须一致
 *
 * fixedSelect 与 combobox 都能造「下拉」，方法面不一致时不会有任何报错：
 * 页面写 `if (sel.setOptions) sel.setOptions(opts)`，某天换了构造器，
 * 那个 if 就永远为假，候选值被静默丢掉、下拉只剩一个占位项 ——
 * 界面上就是「筛选项点了没用」（2026-09-21 发货明细的真实缺陷）。
 * 运行时守卫在 tests/check_ui.js；这里守源头：两个构造器的方法面要对得上。
 */
console.log('\n[11] 下拉构造器方法面一致');
{
  const src = fs.readFileSync(path.join(ROOT, 'static/assets/common.js'), 'utf8');
  const bodyOf = name => {
    const i = src.indexOf(`function ${name}(`);
    if (i < 0) return null;
    // ★ 必须先跳过形参表：fixedSelect 的形参是解构对象
    //   （`function fixedSelect({ name, ... })`），第一对花括号就是形参 ——
    //   直接配平花括号只会取到签名，扫不到任何 `sel.xxx =`，
    //   于是报出「缺少 getValue / setValue / setOptions」这种与事实相反的结论。
    let j = src.indexOf('(', i), pd = 0;
    for (; j < src.length; j++) {
      if (src[j] === '(') pd++;
      else if (src[j] === ')') { pd--; if (pd === 0) { j++; break; } }
    }
    let k = src.indexOf('{', j), depth = 0;
    for (; k < src.length; k++) {
      if (src[k] === '{') depth++;
      else if (src[k] === '}') { depth--; if (depth === 0) return src.slice(j, k + 1); }
    }
    return null;
  };
  const REQUIRED = ['getValue', 'setValue', 'setOptions'];
  for (const name of ['fixedSelect', 'combobox']) {
    const body = bodyOf(name);
    if (!body) { bad(`common.js 里找不到 ${name}()（选择器失效）`); continue; }
    const have = new Set([...body.matchAll(/\b(?:wrap|sel)\.(\w+)\s*=/g)]
      .map(m => m[1]));
    const missing = REQUIRED.filter(k => !have.has(k));
    if (missing.length) {
      bad(`${name}() 缺少 ${missing.join(' / ')} —— ` +
        `页面按另一个构造器的接口调用它时会静默失效`);
    } else ok(`${name}() 提供 ${REQUIRED.join(' / ')}`);
  }
}

/* 12. 发货明细的地址列：宽度覆盖必须**真的赢**
 *
 * 又一条被用户看出来的缺陷（三套自检全绿）：app.css 的
 * `.table .cell-wrap{max-width:300px}` 是给所有可换行列的天花板，
 * 300px 只放得下 22 个汉字，而地址 P50=26 / P75=32 / P90=39 字 ——
 * 于是绝大多数地址折成两行，第二行常常只剩一两个字
 * （…「有限公司」的「司」单独掉一行），行高在 32px / 50px 之间跳，
 * 看起来就是「地址变形了」。
 *
 * 危险点在于：页面里补一条 `.sd-addr{max-width:460px}` 是**没用的** ——
 * 特异性 0,1,0 输给 `.table .cell-wrap` 的 0,2,0，声明被静默丢弃，
 * 界面毫无变化，而 CSS 不报任何错。所以这里机械地校验三件事：
 *   ① 页面确实给地址列写了更大的 max-width；
 *   ② 这条规则的特异性压得过 `.table .cell-wrap`；
 *   ③ 表头与表体都挂了同一个类 —— 只挂一边会让表头与列错位。
 */
console.log('\n[12] 发货明细：地址列宽度覆盖真的生效');
{
  const spec = sel => {
    const ids = (sel.match(/#[\w-]+/g) || []).length;
    const cls = (sel.match(/\.[\w-]+/g) || []).length
      + (sel.match(/\[[^\]]*\]/g) || []).length
      + (sel.match(/:(?!:)/g) || []).length;
    const els = (sel.match(/(^|[\s>+~])[a-z][\w-]*/gi) || []).length
      + (sel.match(/::[\w-]+/g) || []).length;
    return [ids, cls, els];
  };
  const gt = (a, b) => {
    for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
    return false;
  };

  const appCss = fs.readFileSync(path.join(ROOT, 'static/assets/app.css'), 'utf8');
  const detailSrc = fs.readFileSync(
    path.join(ROOT, 'static', 'delivery-detail.html'), 'utf8');
  const css = [...detailSrc.matchAll(/<style>([\s\S]*?)<\/style>/g)]
    .map(m => m[1].replace(/\/\*[\s\S]*?\*\//g, '')).join('\n');

  const wrapRule = /\.table\s+\.cell-wrap\s*\{([^}]*)\}/.exec(appCss);
  const wrapMax = wrapRule && /max-width:\s*(\d+)px/.exec(wrapRule[1]);
  if (!wrapMax) {
    bad('app.css 里找不到 .table .cell-wrap 的 max-width（选择器失效）');
  } else {
    const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .filter(r => /\.sd-addr/.test(r[1]) && /max-width:\s*\d+px/.test(r[2]));
    if (!rules.length) {
      bad('发货明细页没有给 .sd-addr 写列宽（地址会回落到 .cell-wrap 的 300px）');
    } else {
      const sels = rules[0][1].split(',').map(s => s.trim()).filter(Boolean);
      const base = spec('.table .cell-wrap');
      const win = sels.filter(s => gt(spec(s), base));
      // ★ 赢的规则必须有一条**打在 td 上**。只让 th 赢是没用的：表头宽了、
      //   表体照样回落到 300px。第一版的守卫就是只看「有没有一条赢」，
      //   把 `table.sd-t th.sd-addr` 当成了合格证据 —— 负向测试当场抓到。
      const winTd = win.filter(s => /(^|[\s>+~])td(?![\w-])/.test(s));
      if (!winTd.length) {
        bad(`${sels.join(' , ')} 里没有一条「打在 td 上、且特异性压得过 ` +
          `.table .cell-wrap（${base.join(',')}）」的规则 —— 表体会回落到 300px` +
          (win.length ? `（只有表头赢：${win.join(' , ')}）` : ''));
      } else {
        ok(`地址列宽度规则 ${winTd[0]}（特异性 ${spec(winTd[0]).join(',')} > ` +
          `${base.join(',')}）打在 td 上，真的能压过 .table .cell-wrap`);
      }
      const m = /max-width:\s*(\d+)px/.exec(rules[0][2]);
      if (!m || +m[1] <= +wrapMax[1]) {
        bad(`地址列 max-width ${m && m[1]}px 没有宽于默认的 ${wrapMax[1]}px（等于没放宽）`);
      } else {
        ok(`地址列 ${m[1]}px 宽于 .cell-wrap 默认的 ${wrapMax[1]}px`);
      }
    }
  }

  if (!/class:\s*'cell-wrap sd-addr'/.test(detailSrc)) {
    bad('地址单元格没有挂 sd-addr（只写 cell-wrap 就还是 300px）');
  } else ok('地址单元格挂了 cell-wrap sd-addr');
  if (!/key === 'address'[\s\S]{0,60}cls\.push\('sd-addr'\)/.test(detailSrc)) {
    bad('地址表头没有同步挂 sd-addr —— 表头与表体会列宽错位');
  } else ok('地址表头同步挂了 sd-addr');
  if (!/const ADDR_FIT = \d+/.test(detailSrc)
      || !/s\.length > ADDR_FIT/.test(detailSrc)
      || !/attrs\.title = s/.test(detailSrc)) {
    bad('地址列的 ADDR_FIT / title 逻辑缺失 —— 被 line-clamp 截掉的地址看不到全文');
  } else ok('ADDR_FIT 与 title 都在，被截断的长地址可悬浮看全文');
}

/* 13. 自动同步开关必须绑对字段（enabled ≠ running）
 *
 * 2026-09-22 用户报「匹配数据库的自动同步不生效，只能靠手动点击去同步」。
 * 根因不在后端 —— 是 static/items.html 里那个「启用自动同步」开关绑成了
 * `s.running`（= 此刻有没有同步正在跑，几乎恒为 false），而开关该读的是
 * `s.enabled`。症状极具迷惑性：开关永远显示「关」，用户点一下以为打开了，
 * 下一次渲染又变回「关」，而后端其实一直是启用的、每 12 小时都在跑。
 *
 * 这类缺陷三套自检原先都抓不到：HTML 语法没错、JS 不报错、接口 200。
 * 只有把「开关的值必须来自 enabled 而不是 running」写成机械约束才守得住。
 */
console.log('\n[13] 自动同步开关绑对字段（enabled ≠ running）');
{
  /* ★ 2026-09-22：两个业务页上的 ERP 同步卡已整体搬到「自动同步任务」页
     （items.html 只剩物料表、delivery-detail.html 只剩发货明细）。
     下面整段检查因此改盯那个新页面 —— 同步相关的机械约束一处不少，
     只是换了个文件。 */
  const syncTasksSrc = fs.readFileSync(
    path.join(ROOT, 'static', 'sync-tasks.html'), 'utf8');
  const itemsSrc = syncTasksSrc;
  const detailSrc = syncTasksSrc;

  // 只判「绑定那一行」：不能全文件搜 running ——
  // 同步轮询里本来就要读 running（判断这一轮跑完没有）。
  for (const [name, src] of [['items.html', itemsSrc],
                             ['delivery-detail.html', detailSrc]]) {
    const binds = [...src.matchAll(/checked\s*=\s*!!\s*s\.(\w+)/g)]
      .map(m => m[1]);
    if (!binds.length) {
      bad(`${name}：找不到自动同步开关的取值行（<var>.checked = !!s.xxx）`);
    } else if (binds.includes('running')) {
      bad(`${name}：自动同步开关绑的是 s.running —— running 是「此刻有没有` +
        `同步在跑」，几乎恒为 false，开关会永远显示「关」、用户以为没生效`);
    } else if (!binds.includes('enabled')) {
      bad(`${name}：自动同步开关绑的是 s.${binds[0]}，应为 s.enabled`);
    } else ok(`${name}：自动同步开关绑的是 s.enabled`);
  }

  // 界面上要能回答「下次什么时候跑」，否则用户只能靠猜。
  //
  // ⚠️ 判「真的画了一格」，不是判「文件里出现过这四个字」——
  // 第一版写成 /下次自动同步/，负向测试（把 infoBox 的标签改掉）**假绿**：
  // 紧挨着代码的那行 ⚠️ 注释里就有「下次自动同步」，注释把守卫喂饱了。
  // 所以锚在调用本身 + 锚在那句取值表达式（未启用时要显示「未启用」
  // 而不是一个过期日期）。
  const CELL = /infoBox\(\s*['"]下次自动同步['"]\s*,\s*[^)]*?s\.enabled\s*\?\s*\(\s*s\.next_at\s*\|\|\s*'—'\s*\)\s*:\s*'未启用'/s;
  for (const [name, src] of [['items.html', itemsSrc],
                             ['delivery-detail.html', detailSrc]]) {
    if (!CELL.test(src)) {
      bad(`${name} 没有真的画出「下次自动同步」一格 —— 用户无法判断到底排上了没有`);
    } else ok(`${name} 真的画出「下次自动同步」一格（未启用时显示「未启用」）`);
  }
  if (!/next_at/.test(itemsSrc) || !/next_at/.test(detailSrc)) {
    bad('页面没有用 next_at（状态接口已经给了，界面却不用）');
  } else ok('两个页面都展示了 next_at');

  // 开关要能把值写回去。
  if (!/enabled:\s*on\s*\?\s*'1'\s*:\s*'0'/.test(itemsSrc)) {
    bad('自动同步任务页 · 匹配库的开关没有把值写回 /api/items/sync/config');
  } else ok('自动同步任务页的开关把值写回接口');
  if (!/auto_enabled:\s*on\s*\?\s*'1'\s*:\s*'0'/.test(detailSrc)) {
    bad('自动同步任务页 · 发货明细的开关没有把 auto_enabled 写回接口');
  } else ok('自动同步任务页的开关把 auto_enabled 写回接口');
  if (!/auto_interval_hours/.test(detailSrc)) {
    bad('自动同步任务页没有「自动同步间隔」输入项（用户只能吃 8 小时的默认值）');
  } else ok('自动同步任务页可改自动同步间隔');

  /* ★ 2026-09-22：同步拆成两个入口、两个权限点。
     破坏性的那一个（整表覆盖）必须只对拿到权限点的人渲染 —— 光靠服务端 403
     会让用户点下去才发现不能做，光靠前端不渲染又挡不住直接敲 URL。
     所以两端各钉一次，这里钉前端。 */
  if (!/const\s+CAN_SHIP_FULL\s*=\s*hasPerm\(\s*['"]act\.delivery_detail_full['"]\s*\)/
      .test(detailSrc)) {
    bad('自动同步任务页没有按 act.delivery_detail_full 判定「全量同步」权限');
  } else ok('自动同步任务页按 act.delivery_detail_full 收口「全量同步」');
  /* 全量按钮要**同时**满足「有发货明细同步权」与「有全量权」——
     整表覆盖是破坏性动作，与常规增量不是一个量级。 */
  if (!/\(\s*CAN_SHIP\s*&&\s*CAN_SHIP_FULL\s*\)\s*\?\s*h\(\s*'button'/.test(detailSrc)) {
    bad('「全量同步」按钮没有同时要求 CAN_SHIP 与 CAN_SHIP_FULL（人人都能点整表覆盖）');
  } else ok('「全量同步」按钮只有同时拿到两个权限点的人看得到');
  if (!/delivery\/details\/sync\/full/.test(detailSrc)) {
    bad('自动同步任务页没有调用全量同步接口 /api/delivery/details/sync/full');
  } else ok('自动同步任务页调用了独立的全量同步接口');
  /* 范围入口必须消失：用户口径是「不留范围入口，包括其他同步按钮的时间入口
     一并去除」。⚠️ 只判同步相关的那两处 —— 明细表自己的日期筛选是合法的。 */
  if (/手动同步范围|同步范围/.test(detailSrc)) {
    bad('自动同步任务页仍留着「同步范围」入口（用户要求彻底去掉日期范围）');
  } else ok('自动同步任务页已移除「同步范围」入口');

  /* 匹配库那边：同步改成整表覆盖，三个写操作都归 act.items_sync（管理员）。 */
  if (!/const\s+CAN_ITEMS\s*=\s*hasPerm\(\s*['"]act\.items_sync['"]\s*\)/.test(itemsSrc)) {
    bad('自动同步任务页没有按 act.items_sync 判定同步权限（整表覆盖的按钮人人可见）');
  } else ok('自动同步任务页的同步写操作按 act.items_sync 收口');
  for (const [label, pat] of [
    ['「立即同步」按钮', /CAN_ITEMS\s*\?\s*h\(\s*'button'/],
    /* 开关用了 autoSwitch() 组件（里面才建 h('label')），所以这里判
       「CAN_ITEMS 与 autoSwitch 同时出现」—— 兼容两种写法，但不放松要求：
       没有 CAN_ITEMS 的人不该看到任何同步写入口。 */
    ['自动同步开关', /CAN_ITEMS\s*\?\s*autoSwitch\(/],
    ['「连接配置」入口', /CAN_ITEMS\s*\?\s*h\(\s*'button',\s*\{\s*class: 'btn btn--sm btn--ghost',\s*onclick: \(\) => openConnConfig\('items'\)/],
  ]) {
    if (!pat.test(itemsSrc)) bad(`自动同步任务页的${label}没有包在 CAN_ITEMS 里`);
    else ok(`自动同步任务页的${label}只有拿到 act.items_sync 才渲染`);
  }

  /* ★ 2026-09-22：两个任务的**连接**也拆成两份（匹配库 `erp_*`、
     发货明细 `ship_erp_*`）。卡片上的「连接配置」必须各开各的 ——
     两个都指向同一份配置时，界面上完全看不出「改一个动了两个」，
     而这正是拆开它的理由（见 core/erp_conn.py）。 */
  if (!/openConnConfig\('items'\)/.test(itemsSrc)) {
    bad('匹配数据库卡的「连接配置」没有指向自己那份配置');
  } else ok('匹配数据库卡的「连接配置」指向自己那份');
  if (!/openConnConfig\('ship'\)/.test(detailSrc)) {
    bad('发货明细卡的「连接配置」没有指向自己那份配置');
  } else ok('发货明细卡的「连接配置」指向自己那份');
  if (/共用同一份/.test(detailSrc)) {
    bad('界面还写着「共用同一份」（连接已经拆开，这句话现在是假的）');
  } else ok('界面不再声称两个任务共用连接');
  if (!/isShip\s*\?\s*'\/delivery\/details\/config'[\s\S]{0,90}?:\s*'\/items\/sync\/config'/
      .test(detailSrc)) {
    bad('两个任务的连接配置没有分别提交到各自的接口');
  } else ok('两个任务的连接配置分别提交到各自的接口');
}

/* ---------------------------------------------------------------------------
   完结状况两态（2026-09-22）
   「未完结」这三个字里**含「完结」** —— 任何按子串判完结的写法都是 bug：
   会把未完结的行渲染成绿色「已完成」。后端刻意把两层分开
   （存储层存空值、判定用 `NOT LIKE '%完结%'`；展示层归一为「未完结」），
   前端因此必须用**严格等**。守在这里，是因为这种错**看起来完全正常**。
   --------------------------------------------------------------------------- */
{
  const commonSrc = fs.readFileSync(
    path.join(ROOT, 'static', 'assets', 'common.js'), 'utf8');
  const inspectSrc = fs.readFileSync(
    path.join(ROOT, 'static', 'inspect.html'), 'utf8');
  /* 先剥掉注释再匹配 —— 代码里刻意留着「不要用 includes('完结')」的告警注释，
     不剥的话断言会被自己的注释喂饱（第一版就误报了 2 处）。 */
  const stripComments = t => t.split('\n')
    .filter(l => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n');
  const hits = stripComments(commonSrc + inspectSrc)
    .match(/includes\(\s*['"]完结['"]\s*\)/g);
  if (hits) {
    bad(`前端有 ${hits.length} 处 includes('完结') 判完结 ——`
        + `「未完结」含「完结」，会把未完结判成已完结`);
  } else ok('前端判完结一律严格等（没有 includes(\'完结\') 的子串判断）');

  if (!/===\s*'已完结'/.test(commonSrc)) {
    bad('completionTag 没有用严格等判「已完结」');
  } else ok('completionTag 用严格等判「已完结」');
  if (!/未完结/.test(commonSrc)) {
    bad('completionTag 没有「未完结」兜底文案（空值会显示成第三种说法）');
  } else ok('completionTag 有「未完结」兜底');
}

/* ---------------------------------------------------------------------------
   二级弹窗（明细查询）的字段控件覆盖率（2026-09-22）

   改之前 `query.html` 的 makeField **只判了 `search-item`**，于是
   「产品型号 / 产品类别 / 反馈现象 / 检测结果…」一整串 `search-dict` 字段
   落到最后的 else 分支 → 变成**普通输入框**，没有任何候选与模糊匹配。
   这类"漏一个分支"的退化语法检查查不出来、只断言"控件存在"也查不出来 ——
   页面只是静默少了个功能。所以这里直接钉住分支条件本身。
   --------------------------------------------------------------------------- */
{
  const qSrc = fs.readFileSync(path.join(ROOT, 'static', 'query.html'), 'utf8');
  const cSrc = fs.readFileSync(
    path.join(ROOT, 'static', 'assets', 'common.js'), 'utf8');

  if (!/f\.type === 'search-item' \|\| f\.type === 'search-dict'/.test(qSrc)) {
    bad("明细查询弹窗没有同时覆盖 search-item 与 search-dict"
        + "（后者会静默退化成普通输入框、没有任何候选）");
  } else ok('明细查询弹窗同时覆盖 search-item 与 search-dict');

  if (!/autofillFromProduct/.test(qSrc)) {
    bad('明细查询弹窗缺少产品信息自动回填（选中料号/型号后不回填其它字段）');
  } else ok('明细查询弹窗有产品信息自动回填');

  // 回填入口必须排除「类别」—— 一类多料，拿类别查不到唯一物料
  if (!/FILL_ENTRY = \['material_no', 'product_model', 'spec'\]/.test(qSrc)) {
    bad('自动回填的入口字段清单不对（类别不该当入口：一类多料）');
  } else ok('自动回填入口 = 料号 / 型号 / 规格（不含类别）');

  // 「人工优先」：回填只填空格或覆盖上次自动值
  if (!/autoFilled\[k\]/.test(qSrc)) {
    bad('自动回填没有「人工优先」判断（会覆盖人工填过的值）');
  } else ok('自动回填遵守「人工优先」');

  // 候选来源按 match_any 分流：料号走任意列、其余按列去重
  if (!/f\.match_any/.test(cSrc) || !/items\/options\?field=/.test(cSrc)) {
    bad('fetchCandidates 没有按 match_any 分流（型号格会填出一堆料号）');
  } else ok('候选来源按 match_any 分流（料号任意列 / 其余按列去重）');
}

/* ---------------------------------------------------------------------------
   处理登记页的「未处理 / 未检测」计数（2026-09-22）

   用户报「已经处理完的怎么勾选仅看未处理的还显示着」。后端口径是主因
   （见 smoke [21c]），前端这里另有一个字段错配：
   `if (r.untested)` 却显示 `${r.unhandled} 未处理` —— 一单「每行都已处理、
   只是有行没检测」时会显示「· 0 未处理」，看着就像个 bug。
   规则：判的字段与显示的字段必须一致；两个数各显示各的。
   --------------------------------------------------------------------------- */
{
  const hSrc = fs.readFileSync(path.join(ROOT, 'static', 'handle.html'), 'utf8');
  if (!/if \(r\.unhandled\)/.test(hSrc)) {
    bad('处理登记页没有按 r.unhandled 判「未处理」（错配会让 0 未处理也显示出来）');
  } else ok('处理登记页按 r.unhandled 判「未处理」');

  /* 先剥掉注释再匹配 —— 源码注释里也会引用这些标识符，不剥的话断言会被
     自己的注释喂饱（上一轮 includes('完结') 已经踩过一次，这次又踩了）。 */
  const hClean = hSrc.replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  if (/if \(r\.untested\)[^}]*r\.unhandled/.test(hClean)) {
    bad('处理登记页用 untested 判、却显示 unhandled（字段错配）');
  } else ok('处理登记页的未处理 / 未检测各判各的字段');

  if (!/\$\{r\.untested\} 未检测/.test(hSrc)) {
    bad('处理登记页没有单独显示「未检测」数（未检测虽不是处理模块的待办，但要看得见）');
  } else ok('处理登记页单独显示「未检测」数');
}

/* 汇总 */
console.log('\n' + '='.repeat(62));
console.log(`  结果：通过 ${pass} 项，失败 ${fails.length} 项`);
if (fails.length) fails.forEach(f => console.log('    - ' + f));
console.log('='.repeat(62));
process.exit(fails.length ? 1 : 0);
