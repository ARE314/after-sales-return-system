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
let dlgChecked = 0, dlgBad = [];
// common.js 里也有调用点（删照片的确认框），一并扫
const dlgFiles = PAGES.map(p => ({ label: p, rel: 'static/' + p }))
  .concat([{ label: 'common.js', rel: 'static/assets/common.js' }]);
dlgFiles.forEach(({ label, rel }) => {
  const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  let idx = 0;
  while ((idx = src.indexOf('confirmDialog(', idx)) !== -1) {
    // 窗口要**往前也留一段**：`const preview = ...escapeHtml(k)...` 这种
    // 「先算好再拼」的写法，转义证据在调用点的上一行 —— 只往后看会误报。
    const win = src.slice(Math.max(0, idx - 300), idx + 400);
    const interps = [...win.matchAll(/\$\{([^}]*)\}/g)].map(m => m[1]);
    const safe = x => /escapeHtml\s*\(/.test(x)
      || /\.length\s*$/.test(x.trim())
      || /^\s*\d+\s*$/.test(x);
    const risky = interps.filter(x => !safe(x));
    // 窗口里只要出现过 escapeHtml 就认为该调用点已经意识到要转义
    // （preview 这类先算好再拼的写法靠这个放行）
    const hasEscape = /escapeHtml\s*\(/.test(win);
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

/* 汇总 */
console.log('\n' + '='.repeat(62));
console.log(`  结果：通过 ${pass} 项，失败 ${fails.length} 项`);
if (fails.length) fails.forEach(f => console.log('    - ' + f));
console.log('='.repeat(62));
process.exit(fails.length ? 1 : 0);
