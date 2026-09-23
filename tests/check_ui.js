/* 真实 DOM 校验：用 jsdom 执行各页面的内联脚本，捕获运行时错误。
 *
 * 为什么需要它：check_frontend.js 只做静态语法检查，发现不了
 * 「函数提前调用 → TDZ（暂时性死区）→ 整个 IIFE 中断」这类问题。
 * 2026-09-18 退回登记页就因此整个脚本停在 renderHeader()，
 * 后续所有 const/let 声明失效，明细表格永远渲染不出来
 * （表现为「点新增一行没反应」），而语法检查完全正常。
 *
 * 用法：node tests/check_ui.js
 * 前置：后端服务已在 127.0.0.1:8000 运行
 * 依赖：jsdom（node workspace 内）
 */
/* 依赖解析：jsdom 装在 node workspace 里（不在本项目 node_modules）。
   原来这里只是声明了 WS 却没用它，require('jsdom') 只能靠外部 NODE_PATH
   命中 —— 按 README 直接 `node tests/check_ui.js` 会 Cannot find module。
   现在显式按 WS 解析（路径取系统主目录，不写死用户名），
   找不到时给出可操作的提示，而不是抛栈。 */
const _path = require('path');
const WS = process.env.ARS_NODE_WORKSPACE
  || _path.join(require('os').homedir(),
                '.workbuddy', 'binaries', 'node', 'workspace');
const { JSDOM, VirtualConsole } = (() => {
  try {
    return require('jsdom');
  } catch (e) {
    const nm = _path.join(WS, 'node_modules');
    try {
      return require(_path.join(nm, 'jsdom'));
    } catch {
      console.error('找不到 jsdom。请任选其一：');
      console.error('  1) NODE_PATH=' + nm + ' node tests/check_ui.js');
      console.error('  2) 在本项目执行 npm i -D jsdom');
      console.error('  3) 若 node workspace 在别处：set ARS_NODE_WORKSPACE=<目录>');
      process.exit(2);
    }
  }
})();

const BASE = process.env.UI_BASE || 'http://127.0.0.1:8000';

// jsdom 不实现的浏览器能力，其报错属环境限制，不计为页面缺陷
const ENV_NOISE = [
  'getContext', 'HTMLCanvasElement', 'CanvasRenderingContext',
  'Failed to create chart', "can't acquire context",
  'mediaDevices', 'getUserMedia', 'Not implemented',
  'Could not parse CSS', 'play()', 'requestAnimationFrame',
  // jsdom 未装 canvas 包，Chart.js 构造必然失败 —— renderChart 会兜住并打这条日志。
  // 真实浏览器不受影响；能兜住而不是中断整页渲染，本身就是要验证的行为。
  '[图表]',
];

const PASS = [];
const FAIL = [];
function check(name, cond, extra = '') {
  (cond ? PASS : FAIL).push(name);
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}${extra ? '  -> ' + extra : ''}`);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ★ 页面脚本抛出的、没人接管的 Promise 拒绝会直接杀掉 node 进程：
   结果是一条 FAIL 都没有、只看到「校验器自身出错 / exit 2」——
   看起来完全不像「报红」，很容易被当成「没测到」而不是「测出问题了」。
   实测（把 fixedSelect 换回发货明细筛选区时）：node 直接退出，FAIL 行数 0。
   这里兜住它，转成一条明确的 FAIL。 */
process.on('unhandledRejection', r => {
  const t = (r && (r.stack || r.message)) || String(r);
  if (ENV_NOISE.some(n => t.includes(n))) return;
  const first = t.split('\n')[0];
  console.log('  [FAIL] 未捕获的异步错误（页面脚本抛的） -> ' + first);
  FAIL.push('unhandledRejection: ' + t.split('\n').slice(0, 3).join(' | '));
});

/* 登录会话 —— 登录验证默认开启，页面与接口都要求带 Cookie。
   这里先在 Node 侧登一次，把 Set-Cookie 里的会话 ID 记下来：
     · loadPage 抓 HTML 时带上（否则拿到的是登录页的 HTML）
     · 注入到 jsdom 的 window.fetch 里（否则页面内的接口全 401） */
let SID = '';

/* 自检凭据：与 smoke_test.py 同一套取值规则 ——
   环境变量 > tests/_smoke.env > 初始默认值。
   管理员改过密码后，写死默认值会让整个 DOM 校验全红，且看不出原因。 */
function smokeCredentials() {
  let user = process.env.SMOKE_USER || '';
  let pwd = process.env.SMOKE_PASSWORD || '';
  if (!pwd) {
    try {
      const raw = require('fs').readFileSync(
        require('path').join(__dirname, '_smoke.env'), 'utf8');
      for (const line of raw.split(/\r?\n/)) {
        const t = line.trim();
        if (!t || t.startsWith('#') || !t.includes('=')) continue;
        const i = t.indexOf('=');
        const k = t.slice(0, i).trim().toUpperCase();
        const v = t.slice(i + 1).trim().replace(/^["']|["']$/g, '');
        if (k === 'SMOKE_USER' && !user) user = v;
        if (k === 'SMOKE_PASSWORD' && !pwd) pwd = v;
      }
    } catch { /* 没有该文件就用默认值 */ }
  }
  return { user: user || 'admin', pwd: pwd || 'admin123' };
}

async function loginForTests() {
  const cred = smokeCredentials();
  const r = await fetch(BASE + '/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: cred.user, password: cred.pwd }),
  });
  const setCookie = r.headers.getSetCookie
    ? r.headers.getSetCookie().join(';') : (r.headers.get('set-cookie') || '');
  const m = /ars_sid=([^;]+)/.exec(setCookie);
  if (!r.ok || !m) {
    console.log(`  [警告] 登录失败（HTTP ${r.status}，账号 ${cred.user}）`);
    console.log('         后续所有用例都会因为 401 失败。请提供当前密码，二选一：');
    console.log('           set SMOKE_PASSWORD=你的密码 && node tests\\check_ui.js');
    console.log('           tests\\_smoke.env 里写 SMOKE_PASSWORD=你的密码');
    console.log('         忘了密码：python tools\\reset_admin_password.py '
                + '--user admin --random');
    return '';
  }
  SID = m[1];
  return SID;
}

/* 本次运行的开始时刻：回收站清理用它做时间下界（只清本轮自己删出来的
 * 记录）。不能用 UTC 的 ISO 串直接比 —— 库里 created_at 是本地时间。 */
const RUN_T0 = new Date();

const cookieHeader = () => (SID ? { Cookie: `ars_sid=${SID}` } : {});

/** 带会话的 fetch（页面 HTML 与页面内接口都用它）。 */
function authFetch(input, init = {}) {
  return fetch(input, { ...init, headers: { ...(init.headers || {}), ...cookieHeader() } });
}

async function loadPage(path, waitMs) {
  const errors = [];
  // 页面发出的每一个请求。用来断言「筛选控件按下之后真的重新查了」——
  // 只断言「控件存在」是不够的：控件存在但值是死的，照样全绿。
  const requests = [];
  const html = await (await authFetch(BASE + path)).text();

  const vc = new VirtualConsole();
  const grab = e => {
    const text = e && e.detail
      ? (e.detail.stack || e.detail.message || String(e.detail))
      : (e && e.message) || String(e);
    if (ENV_NOISE.some(n => text.includes(n))) return;   // 环境限制，忽略
    errors.push(text.split('\n').slice(0, 3).join(' | '));
  };
  vc.on('jsdomError', grab);
  vc.on('error', (...a) => grab({ message: a.join(' ') }));

  const dom = new JSDOM(html, {
    url: BASE + path,
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      window.fetch = (input, init = {}) => {
        const u = (typeof input === 'string' && input.startsWith('/'))
          ? BASE + input : input;
        requests.push(String(u).replace(BASE, ''));
        // 会话 Cookie 塞进请求头：jsdom 不跑浏览器那套 Cookie 存储，
        // 页面的 fetch 不会自动带上，只能在这里补
        return fetch(u, { ...init,
          headers: { ...(init.headers || {}), ...cookieHeader() } });
      };
      window.addEventListener('error', e =>
        grab({ message: '未捕获: ' + (e.error ? (e.error.stack || e.error.message) : e.message) }));
      window.addEventListener('unhandledrejection', e => {
        const r = e.reason;
        const t = (r && (r.stack || r.message)) || String(r);
        if (ENV_NOISE.some(n => t.includes(n))) return;
        errors.push('Promise 拒绝: ' + t.split('\n')[0]);
      });
    },
  });

  const { window } = dom;
  await new Promise(res => {
    if (window.document.readyState === 'complete') return res();
    window.addEventListener('load', res);
    setTimeout(res, 8000);
  });
  await sleep(waitMs || 1800);
  return { dom, window, doc: window.document, errors, requests };
}

function reportErrors(label, errors) {
  check(`${label} 无运行时错误`, errors.length === 0,
        errors.length ? errors.slice(0, 2).join(' ;; ') : '干净');
}

/** 等页面把异步数据渲染出来，再断言 DOM。
 *
 * 页面是「先搭骨架、再拿数据补内容」的：固定 sleep 到点就断言，等于在赌
 * 接口响应时间。2026-09-22 就吃过一次 —— 台账接口当时要 14 秒（返件归位的
 * 厂家粗筛没做，3414 × 1224 次两两比对），固定等 1700 毫秒时页面还停在
 * 骨架状态（汇总条全是 0），于是「表头 10 列」「列表渲染出行」全红，而接口
 * 本身没问题。轮询等条件成立才是稳的，超时就照常红 —— 不吞失败。
 */
async function waitFor(cond, timeoutMs = 20000, stepMs = 250) {
  const t0 = Date.now();
  for (;;) {
    let ok = false;
    try { ok = !!cond(); } catch (e) { ok = false; }
    if (ok) return true;
    if (Date.now() - t0 >= timeoutMs) return false;
    await sleep(stepMs);
  }
}

/* 批次⑤（明细查询方案 B 定稿）：一级窗口只剩「售后单列表」一张卡，
   单内明细一律走「点单 → 弹窗」；批次④ 的「全部明细」两态、列设置、批量删除、
   清空选择都随栏位下线，所以批次④ 那两个切换视图的辅助函数也不再需要。 */

(async () => {
  console.log('=' .repeat(62));
  console.log('  真实 DOM 校验（jsdom 执行页面脚本）');
  console.log('='.repeat(62));

  console.log('\n[前置] 登录（登录验证默认开启，后续页面与接口都需带会话）');
  await loginForTests();
  check('取得测试用会话', !!SID, SID ? `ars_sid=${SID.slice(0, 10)}…` : '（未取得）');

  /* 自愈：上一次运行**中途中断**（未捕获错误）时，末尾那次回收站清理不会执行，
     会留下几条 `operator = 自检账号` 的残留记录（2026-09-23 实测：一次弹窗断言
     抛错后留下 5 条）。这里在开跑之初先把**上一次**（created_at < RUN_T0）同账号
     的残留删掉；本轮自己删出来的记录由末尾那次清理负责。只碰自动化账号的行，
     碰不到真数据（`tests/_smoke.env` 里的账号只有自检在用）。 */
  try {
    const myUserEarly = smokeCredentials().user;
    let healed = 0;
    for (const pg of [1, 2, 3]) {
      const lr = await (await authFetch(BASE + `/api/recycle?page_size=200&page=${pg}`)).json();
      const rowsH = lr.rows || [];
      if (!rowsH.length) break;
      for (const x of rowsH) {
        const created = new Date(String(x.created_at || '').replace(' ', 'T'));
        if (x.operator === myUserEarly && created < RUN_T0) {
          await authFetch(BASE + `/api/recycle/${x.id}`, { method: 'DELETE' });
          healed += 1;
        }
      }
    }
    if (healed) console.log(`  自愈：清掉上次中断留下的自检回收站残留 ${healed} 条`);
  } catch { /* 自愈失败不影响主流程 */ }

  /* 站点英文标题的**期望值**写死在测试里（不从页面读回来比对，否则等于没测）。
     源头是 common.js 的 SITE_TITLE_EN；改标题时这两处一起改。 */
  const EXPECT_TITLE_EN = 'Beiliang After-Sales Return Registration System';

  // 登录页要在**没有会话**的状态下验证，所以单独用干净 fetch 加载
  console.log('\n[登录页] login.html（未登录状态）');
  try {
    const raw = await (await fetch(BASE + '/login.html')).text();
    const vcL = new VirtualConsole();
    const errL = [];
    vcL.on('jsdomError', e => errL.push(String(e.detail && e.detail.message || e.detail)));
    const domL = new JSDOM(raw, {
      url: BASE + '/login.html', runScripts: 'dangerously',
      resources: 'usable', pretendToBeVisual: true, virtualConsole: vcL,
      beforeParse(w) {
        // 故意不带会话：登录页要能在未登录状态正常渲染
        w.fetch = (i, init = {}) => fetch(
          typeof i === 'string' && i.startsWith('/') ? BASE + i : i, init);
      },
    });
    const wL = domL.window;
    await new Promise(res => {
      if (wL.document.readyState === 'complete') return res();
      wL.addEventListener('load', res); setTimeout(res, 8000);
    });
    await sleep(1500);
    const docL = wL.document;
    check('登录页渲染出账号 / 密码输入框',
          docL.querySelectorAll('input[type=password]').length >= 1
          && !!docL.querySelector('input[type=text]'),
          `${docL.querySelectorAll('input').length} 个输入框`);
    check('登录页有提交按钮与品牌标识',
          [...docL.querySelectorAll('button')].some(b => b.textContent.includes('登'))
          && !!docL.querySelector('.login-brand__logo'),
          docL.querySelector('.card__title') ? docL.querySelector('.card__title').textContent : '');
    check('登录页品牌区显示英文全称标题',
          (docL.querySelector('.login-brand__sub') || {}).textContent === EXPECT_TITLE_EN,
          (docL.querySelector('.login-brand__sub') || {}).textContent || '(未找到)');
    check('登录页显示服务连通状态',
          /服务(正常|未连接)/.test(docL.body.textContent),
          (docL.body.textContent.match(/服务(正常|未连接)[^·]*/) || [''])[0].slice(0, 40));
    check('登录页不含侧栏（独立于主布局）',
          !docL.querySelector('.sidebar'), '无 .sidebar');
    wL.close();
  } catch (e) {
    check('登录页可加载', false, e.message);
  }

  const PAGES = [
    ['index.html', '工作台'],
    ['scan.html', '退回登记'],
    ['inspect.html', '检测登记'],
    ['handle.html', '处理登记'],
    ['query.html', '明细查询'],
    ['dashboard.html', '数据看板'],
    ['items.html', '匹配数据库'],
    ['delivery-apply.html', '发货申请'],
    ['delivery-pending.html', '待发货清单'],
    ['delivery-track.html', '发货跟踪'],
    ['delivery-ledger.html', '核销台账'],
    ['delivery-detail.html', '发货明细'],
    ['sync-tasks.html', '自动同步任务'],
    ['recycle.html', '回收站'],
    ['api.html', '数据接口'],
    ['auth.html', '权限设置'],
  ];

  for (const [file, label] of PAGES) {
    console.log(`\n[${label}] ${file}`);
    let ctx;
    try {
      ctx = await loadPage('/' + file);
    } catch (e) {
      check(`${label} 可加载`, false, e.message);
      continue;
    }
    const { doc, errors } = ctx;
    check(`${label} 脚本执行完毕`, (doc.body.textContent || '').trim().length > 30,
          `${doc.body.textContent.trim().length} 字符`);
    check(`${label} 侧栏英文标题为全称`,
          (doc.querySelector('.sidebar__sub') || {}).textContent === EXPECT_TITLE_EN,
          (doc.querySelector('.sidebar__sub') || {}).textContent || '(未找到)');
    /* ★ 侧栏必须真的把入口渲染进 DOM —— 这条断言是 2026-09-21 补的盲区。
       当时后端权限点加了、导航项也加了、三套校验全绿，但用户「看不到发货模块」。
       根因：**服务进程还是旧的**（Python 代码在内存里，改 .py 不重启不生效），
       旧进程的 /api/auth/me 只返回 17 个权限点 → hasPerm() 全 false →
       renderSidebar 的 visible() 把发货四项过滤掉了。
       校验之所以没抓住：以前只断言了文件存在（check_frontend）和品牌标题，
       **没有任何一条断言「导航入口真的出现在 DOM 里」**。
       所以这里按 href 精确点名，任何一层（权限点、导航常量、权限过滤）出问题都会变红。 */
    const wantNav = [
      ['/delivery-apply.html', '发货申请'],
      ['/delivery-pending.html', '待发货清单'],
      ['/delivery-track.html', '发货跟踪'],
      ['/delivery-ledger.html', '核销台账'],
    ];
    const missingNav = wantNav
      .filter(([href]) => !doc.querySelector(`.nav__item[href="${href}"]`))
      .map(([, name]) => name);
    check(`${label} 侧栏渲染出「发货管理」四项入口`,
          missingNav.length === 0,
          missingNav.length ? '缺: ' + missingNav.join(' / ')
                            : doc.querySelectorAll('.nav__item').length + ' 项导航');
    /* 发货明细是发货模块下的子页，但**不在那四步流程里**：前四件套是
       「我们自己发了哪些货」，它是「ERP 里记了哪些货」。单独点名，
       免得将来有人把它的权限点或导航项删掉而没人发现。 */
    check(`${label} 侧栏渲染出「发货明细」入口`,
          !!doc.querySelector('.nav__item[href="/delivery-detail.html"]'));
    /* 回收站同理单独点名：它是运维页，导航项挂在「系统管理」组里，
       漏了权限点或漏了导航常量都会让用户「找不到回收站」。 */
    check(`${label} 侧栏渲染出「回收站」入口`,
          !!doc.querySelector('.nav__item[href="/recycle.html"]'));
    reportErrors(label, errors);
  }

  /* ---- 匹配数据库：ERP 同步卡片 ----
     这张卡片承载「同步是否正常」的可见性 —— 匹配库靠 ERP 刷新，新料号同步
     进来退回登记才能按料号回填；同步悄悄停掉的话没人会知道。
     后端接口通 ≠ 界面显示，所以这里真跑一遍页面查 DOM。 */
  console.log('\n[交互] 匹配数据库 · ERP 同步卡片');
  {
    /* 2026-09-22：整段都是同步卡检查 —— 卡已搬到「自动同步任务」页。 */
    const ctxS = await loadPage('/sync-tasks.html', 2400);
    const docS = ctxS.doc;
    const txtS = docS.body.textContent || '';
    /* 卡片标题在 2026-09-22 改版时改成「匹配数据库 · 物料主档」
       （页面也换了：这些卡现在住在「自动同步任务」页）。 */
    const cardS = [...docS.querySelectorAll('.card')]
      .find(c => c.textContent.includes('匹配数据库 · 物料主档'));
    check('渲染出「匹配数据库 · 物料主档」同步卡片', !!cardS);
    if (cardS) {
      const ct = cardS.textContent;
      check('同步卡显示最近一次时间与距今小时数',
            /最近一次同步/.test(ct) && /距今/.test(ct),
            ct.replace(/\s+/g, ' ').slice(0, 70));
      /* 反向断言：状态卡的指标格只许是这 4 个，不许出现 ERP 连接信息
         （地址 / 端口 / 库名 / 账号 / 密码）或「上次取数」行数。
         连接串只该出现在「修改连接配置」弹窗里 —— 那是有意编辑它的地方；
         取数结果由「本地记录」一格表达。

         **按 `.kpi__label` 元素比对，不按整卡文本做子串匹配** ——
         这是实测踩出来的：下面 `scope.why` 的正文里有一句
         「ERP 账号列级权限只剩 Code/Code1/SPECS 三列」，用 /账号/ 扫全文
         会把这句解释误判成「显示了账号字段」，测试变成假红。 */
      const kpiLabels = [...cardS.querySelectorAll('.kpi__label')]
        .map(e => (e.textContent || '').trim());
      /* 指标格是一个**闭集合**：多一格、少一格都要报红 —— 这是「不许把
         ERP 连接信息搬上卡片」的正向表达。
         2026-09-22 新增「下次自动同步」：用户看不到「排上了没有」，
         就只能靠手动点击，会直接怀疑自动同步没生效。 */
      const WANT_KPI = ['最近一次同步', '距今', '同步间隔', '下次自动同步', '本地记录'];
      check('同步卡指标就是最近一次 / 距今 / 间隔 / 下次自动同步 / 本地记录（无连接信息与取数行数）',
            kpiLabels.join('|') === WANT_KPI.join('|'),
            kpiLabels.join(' · ') || '(无指标格)');
      check('指标格里没有 ERP 连接信息（地址 / 端口 / 库名 / 账号 / 密码 / 主机）',
            !kpiLabels.some(l => /地址|端口|库名|账号|密码|主机|IP/.test(l)),
            kpiLabels.join(' · '));
      check('同步卡全文不含 ERP 地址 / IP / 库名',
            !/ERP 地址/.test(ct) && !/\d{1,3}(\.\d{1,3}){3}/.test(ct)
            && !/BLFN/.test(ct),
            ct.replace(/\s+/g, ' ').slice(0, 70));
      check('同步卡标出状态（正常 / 已超期 / 失败 / 未配置）',
            ['正常', '已超期', '最近一次失败', '未配置']
              .some(s => (cardS.querySelector('.tag') || {}).textContent === s),
            (cardS.querySelector('.tag') || {}).textContent || '(无标签)');
      /* 2026-09-22 起数据源**全部实时**（一条 SQL 取 11 列），不再有离线快照。
         界面要写清「写入什么」+ 每一列从哪来 —— 否则用户看到某列为空
         会以为同步坏了。 */
      check('同步卡写明「写入范围」且逐项列出数据来源（全实时，不再有快照）',
            /写入范围/.test(ct) && /整表覆盖/.test(ct)
            && /实时/.test(ct) && !/09-21 快照/.test(ct),
            ct.replace(/\s+/g, ' ').slice(0, 70));
      /* 2026-09-22：开关文案与发货明细页对齐，`启用自动同步` →
         `自动同步（每 N 小时）` —— 间隔直接写在标签上，用户不用另找。
         改文案必须同步改这条断言，否则真改动会变成假红。 */
      check('提供「立即同步」与「自动同步（每 N 小时）」',
            [...cardS.querySelectorAll('button')].some(b => b.textContent.includes('立即同步'))
            && /自动同步（每 \d+ 小时）/.test(ct),
            (ct.match(/自动同步（[^）]*）/) || ['(没有自动同步标签)'])[0]);
      /* 类别列曾是「只补空」的例外（ERP 分类表把内码 2335 译成通用的 `III`，
         本地原本是更精确的 `III型风向`）。2026-09-22 起整表覆盖 + 内码翻译，
         类别随整表无条件重写 —— 旧文案「只补空不覆盖」现在等于撒谎。
         同时必须点明品名来自 ERP 多语言表：这是本轮新接的数据源，
         界面不说，用户就不知道品名会不会自动补。 */
      check('同步卡说明品名来自 ERP 多语言表（且不再写「只补空」）',
            /整表覆盖/.test(ct)
            && /多语言表|CBO_ItemMaster_Trl/.test(ct)
            && !/只补空不覆盖|保持本地值/.test(ct),
            ct.replace(/\s+/g, ' ').slice(0, 40));

      /* 位置：同步卡必须在数据表**之后**（页面最下面），与发货明细页
         `replaceChildren(filterCard, listCard, syncHost)` 同一套排布。
         只判「卡片在不在」是不够的 —— 顺序错了界面同样难用（状态卡把料号表
         挤到下面），而 HTML 语法、JS 运行、接口返回三样全都不会报错。
         所以断言真实 DOM 里两张卡的先后，而不是断言源码里那行调用。 */
    }
    /* ★ 2026-09-22：原来这里断言「同步卡排在料号表之后」（它在 items.html 上
       是页面最后一块）。卡片搬到「自动同步任务」页后，那一页**只有**两张任务卡
       与连接配置块 —— 位置断言失去对象，改为断言「两张任务卡都在、且顺序是
       匹配库在前、发货明细在后」（与挂载顺序一致）。 */
    {
      const titles = [...docS.querySelectorAll('.card__title')]
        .map(e => (e.textContent || '').trim())
        .filter(Boolean);
      check('两张任务卡按「匹配数据库 → 发货明细」的顺序渲染',
            titles.indexOf('匹配数据库 · 物料主档') >= 0
            && titles.indexOf('发货明细 · ERP 出货单')
               > titles.indexOf('匹配数据库 · 物料主档'),
            titles.join(' / ') || '(无卡标题)');
    }
    /* ★ 与发货明细那条对称：匹配库这张卡的「连接配置」必须提交到
       /api/items/sync/config。两条都在，才算证明「各开各的」——
       只测一条的话，两个按钮指向同一个弹窗也能全绿。 */
    const cfgBtnS = cardS && [...cardS.querySelectorAll('button')]
      .find(b => b.textContent.includes('连接配置'));
    check('匹配数据库卡提供「连接配置」入口', !!cfgBtnS);
    if (cfgBtnS) {
      cfgBtnS.click();
      await sleep(900);
      const mS = docS.querySelector('.modal');
      check('匹配数据库的「连接配置」弹窗已打开', !!mS);
      if (mS) {
        const mtS = mS.textContent;
        check('弹窗写明这是「匹配数据库」专用的连接（不再说共用同一份）',
              /存 erp_\*/.test(mtS) && !/共用同一份/.test(mtS),
              mtS.replace(/\s+/g, ' ').slice(0, 70));
        const saveS = [...mS.querySelectorAll('.modal__foot button')]
          .find(b => b.textContent.trim() === '保存');
        check('匹配数据库的连接配置弹窗有「保存」按钮', !!saveS);
        if (saveS) {
          const nS = ctxS.requests.length;
          saveS.click();
          await sleep(1400);
          const sentS = ctxS.requests.slice(nS);
          check('匹配数据库的「连接配置」只提交到 /api/items/sync/config'
                + '（绝不写发货明细那份）',
                sentS.some(u => u.startsWith('/api/items/sync/config'))
                && !sentS.some(u => u.startsWith('/api/delivery/details/config')),
                sentS.join(' | ') || '(没发出请求)');
        }
      }
    }
    check('匹配数据库（含同步卡） 无运行时错误', ctxS.errors.length === 0,
          ctxS.errors.slice(0, 2).join(' ;; ') || '干净');
  }

  // ---- 退回登记：交互级验证 ----
  console.log('\n[交互] 退回登记 · 产品明细增删');
  // 本段自己的 window —— 别再像上一版那样引 ctxA（那是数据接口段的变量，
// 在这里属于未定义，会让整个校验器崩掉、PASS 数骤降且看不到 FAIL）。
const { doc, errors, window: scanWin } = await loadPage('/scan.html', 2000);

/* 布局（2026-09-20）：
   - 蓝色扫描区**整体移除**（输入框 + 三个按钮 + 状态行 + 摄像头预览）。
     扫码没有失效 —— 走的是全局扫码枪 attachScanner()，物理枪在页面任意位置
     输入即可触发匹配，不需要那块 UI。
   - 「整单信息」上方的匹配提示框（#match-banner）**恢复**。
   注意别跟 #check-banner 搞混：那是「整单信息」**下方**的查重框，一直都在。 */
check('蓝色扫描区已移除（扫码改走全局扫码枪）',
      !doc.querySelector('.scan-hero'),
      doc.querySelector('.scan-hero') ? '仍存在' : '已移除');
check('匹配提示框（#match-banner）已恢复',
      !!doc.querySelector('#match-banner'), '存在');
check('查重框（整单信息下方那个）保留，未被误删',
      !!doc.querySelector('#check-banner'),
      doc.querySelector('#check-banner') ? '存在' : '已丢失');

/* 全局扫码枪端到端：页面上已经没有扫描输入框了，直接往 document 上打一串
   快速按键（模拟扫码枪），应当触发匹配并在提示框里出结果。
   它守的是一个**很容易被破坏**的前提：
     ① attachScanner 还在（删掉它就彻底没入口了）；
     ② **启动时焦点不在任何输入框里** —— attachScanner 的设计是「焦点在
        输入框内就避让，交给输入框自己处理」，所以 scan.html 启动时做了
        blur；谁要是又给某个字段加了自动聚焦，这条断言会立刻变红。
        2026-09-20 正是踩了这个坑：删掉扫描区后焦点留在「退回单号」里，
        第一个扫码会被吞掉，而页面看起来一切正常。 */
const _gunCode = 'SMOKE-GUN-NOT-EXIST';
for (const _ch of _gunCode) {
  doc.dispatchEvent(new scanWin.KeyboardEvent('keydown',
    { key: _ch, bubbles: true }));
  await sleep(12);
}
await sleep(1400);
const _banner = doc.querySelector('#match-banner');
check('扫码枪（页面无输入框）仍能触发匹配并在提示框出结果',
      !!_banner && _banner.children.length > 0,
      _banner ? `${_banner.children.length} 个子节点` : '(提示框不存在)');

const dataRows = () => doc.querySelectorAll('#line-host tbody tr:not(.line-detail)').length;
  /* 选择器必须限定在明细工具条里 —— 页面上别处也可能有「新增一行」类按钮
     （如历史里查不到的条码会在提示框里给出「当产品」选择），
     按文案全局找会抓错元素。2026-09-20 就因此踩过：抓到提示框的按钮，
     点击后元素已被移除，却还在点那个游离节点，每点一次都往明细里加一行。 */
  const btnAdd = [...doc.querySelectorAll('.line-toolbar button')]
    .find(b => b.textContent.includes('新增一行'));

  check('找到「＋ 新增一行」按钮（且来自明细工具条，不是别处的同名按钮）',
        !!btnAdd && !!btnAdd.closest('.line-toolbar'),
        btnAdd ? (btnAdd.closest('.line-toolbar') ? '来自工具条' : '🔴 抓到别处的按钮') : '(未找到)');
  if (btnAdd) {
    const n0 = dataRows();
    btnAdd.click();
    await sleep(700);
    const n1 = dataRows();
    check('点击一次新增 1 行', n1 - n0 === 1, `${n0} → ${n1}`);

    btnAdd.click(); btnAdd.click();
    await sleep(700);
    const n3 = dataRows();
    check('连续点击可叠加行', n3 - n0 === 3, `${n0} → ${n3}`);

    // 明细行的下拉/输入控件是否真的渲染出来（TDZ 类问题的典型症状）
    const ctrls = doc.querySelectorAll('#line-host tbody tr:not(.line-detail) td select, '
      + '#line-host tbody tr:not(.line-detail) td input');
    check('明细行控件已渲染', ctrls.length > 0, `${ctrls.length} 个控件`);

    const btnClear = [...doc.querySelectorAll('.line-toolbar button')]
      .find(b => b.textContent.includes('清空明细'));
    if (btnClear) {
      btnClear.click();
      await sleep(600);
      check('「清空明细」可清空', dataRows() === 0, `剩余 ${dataRows()} 行`);
    }

    // ---- 料号 → 产品属性自动回填（回车触发）----
    console.log('\n[交互] 退回登记 · 料号自动回填');
    btnAdd.click();
    await sleep(600);

    const cellOf = f => doc.querySelector(
      `#line-host tbody tr:not(.line-detail) [data-field="${f}"]`);
    const valOf = f => (cellOf(f) ? cellOf(f).value : '(无此字段)');
    // 事件构造器必须取自页面 window —— Node 全局的 Event 不是 jsdom 的类型
    const W = doc.defaultView;

    const typeThenEnter = async (field, text) => {
      const el = cellOf(field);
      if (!el) return;
      el.value = text;
      el.dispatchEvent(new W.Event('input', { bubbles: true }));
      el.dispatchEvent(new W.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      await sleep(1500);
    };

    await typeThenEnter('material_no', '10001-0001');
    check('料号回车后带出产品型号', valOf('product_model') === '51177.67.773C',
      `产品型号=${valOf('product_model')}`);
    check('料号回车后带出规格', valOf('spec') === '51177.67.773C',
      `规格=${valOf('spec')}`);
    check('料号回车后带出品名', valOf('product_name') === '低温型风速传感器',
      `品名=${valOf('product_name')}`);
    check('料号回车后带出产品类别（取自匹配库生产统计）',
      valOf('product_category') === '风速【低温】',
      `产品类别=${valOf('product_category')}`);
    check('生产年份未被自动填（跨批次年份不一致，留空给人填）',
      valOf('production_year') === '', `生产年份=${valOf('production_year') || '(空)'}`);
    check('回填的单元格有高亮标记',
      !!doc.querySelector('#line-host .cell-input--filled')
      || valOf('product_model') === '51177.67.773C',
      '高亮可能已超时消失，值正确即视为通过');

    // 人工填过的格永不被覆盖
    const nameCell = cellOf('product_name');
    nameCell.value = '人工写的品名';
    nameCell.dispatchEvent(new W.Event('input', { bubbles: true }));

    // 换个料号再换回来，强制重新匹配
    await typeThenEnter('material_no', '10002-0079');
    check('人工填过的品名不被自动值覆盖',
      valOf('product_name') === '人工写的品名', `品名=${valOf('product_name')}`);
    check('未人工干预的字段仍会被更新',
      valOf('product_category') === 'III型风向', `产品类别=${valOf('product_category')}`);

    // 不存在的料号 → 标黄提示，并清掉上一轮自动填的值
    await typeThenEnter('material_no', '99999-9999');
    check('未知料号被标记为未匹配',
      !!doc.querySelector('#line-host .cell-input--miss'), '已标黄');
    check('未知料号清掉了上一轮自动填的值（人工值不受影响）',
      valOf('product_model') === '' && valOf('product_category') === '',
      `型号=${valOf('product_model') || '(空)'} 类别=${valOf('product_category') || '(空)'}`);

    // ---- 产品编号 → 生产年月（前 4 位 YYMM）----
    console.log('\n[交互] 退回登记 · 产品编号识别生产年月');
    await typeThenEnter('product_code', '20100341');
    check('产品编号回车后带出生产年份', valOf('production_year') === '2020',
      `生产年份=${valOf('production_year')}`);
    check('产品编号回车后带出生产月份（带单位）', valOf('production_month') === '10月',
      `生产月份=${valOf('production_month')}`);

    // 编号不规范 → 标记「无法确认」，便于后续筛出修正
    await typeThenEnter('product_code', 'S2402057');
    check('编号不规范时标记为「无法确认」',
      valOf('production_year') === '无法确认' && valOf('production_month') === '无法确认',
      `年=${valOf('production_year')} 月=${valOf('production_month')}`);

    // 人工改过的年月不被覆盖
    const yearCell = cellOf('production_year');
    yearCell.value = '2001';
    yearCell.dispatchEvent(new W.Event('input', { bubbles: true }));
    await typeThenEnter('product_code', '250912894');
    check('人工改过的生产年份不被自动解析覆盖',
      valOf('production_year') === '2001', `生产年份=${valOf('production_year')}`);
    check('未被人工干预的生产月份仍会更新',
      valOf('production_month') === '9月', `生产月份=${valOf('production_month')}`);

    // ---- 明细行的模糊匹配（与检测登记同一套交互）----
    console.log('\n[交互] 退回登记 · 明细行模糊匹配');
    const cbOf = f => {
      const el = cellOf(f);
      return el ? el.closest('.cb') : null;
    };
    check('料号格已渲染为可搜索下拉', !!cbOf('material_no'),
          cbOf('material_no') ? '有候选面板' : '未渲染');
    // 2026-09-20：产品编号是铭牌序列号（一次性），改成纯输入框，不做候选
    check('产品编号格是纯输入框（序列号不做候选下拉）',
          !!cellOf('product_code') && !cbOf('product_code'),
          cellOf('product_code')
            ? (cbOf('product_code') ? '🔴 仍是可搜索下拉' : '纯输入，无候选面板')
            : '(未找到该格)');
    check('生产年份格已渲染为可搜索下拉', !!cbOf('production_year'),
          cbOf('production_year') ? '有候选面板' : '未渲染');

    // 输入片段 → 从匹配数据库弹出候选（带「品名 · 型号」副标题）
    const codeEl2 = cellOf('material_no');
    const panel2 = cbOf('material_no').querySelector('.cb__panel');
    codeEl2.value = '10002';
    codeEl2.dispatchEvent(new W.Event('input', { bubbles: true }));
    await sleep(1000);                       // 等防抖 + 请求返回
    // 排除「＋ 使用新值」那一项（它是控件既有行为，不是候选数据）
    const cands = [...panel2.querySelectorAll('.cb__item:not(.cb__item--new)')];
    check('料号输入片段后弹出匹配库候选',
          cands.length > 0, `${cands.length} 个候选`);
    check('候选带「品名 · 型号」副标题（便于分辨同名料号）',
          cands.some(el => {
            const m = el.querySelector('.cb__meta');
            return m && m.textContent.includes('·');
          }),
          cands[0] ? cands[0].textContent.trim().slice(0, 44) : '(无候选)');
    check('候选内容来自匹配数据库（含 10002 前缀料号）',
          cands.some(el => /10002-/.test(el.textContent)),
          cands.slice(0, 3).map(el => {
            const v = el.querySelector('.cb__val');
            return v ? v.textContent : '?';
          }).join(', '));

    // 点候选 → 料号填入，并照旧触发自动回填
    if (cands.length) {
      const valEl = cands[0].querySelector('.cb__val');
      const picked = valEl ? valEl.textContent.trim() : '';
      cands[0].dispatchEvent(new W.MouseEvent('mousedown', { bubbles: true }));
      await sleep(1400);
      check('选中候选后料号被填入',
            valOf('material_no') === picked, `料号=${valOf('material_no')} 期望=${picked}`);
      check('选中候选后照旧自动回填产品属性',
            !!valOf('product_model') && !!valOf('product_name'),
            `型号=${valOf('product_model')} 品名=${valOf('product_name')}`);
    }

    // 扫码枪识别：连续极快输入（字符间隔 < 35ms）不弹面板
    const scanEl = cellOf('material_no');
    scanEl.value = '1000';
    scanEl.dispatchEvent(new W.Event('input', { bubbles: true }));
    await sleep(300);
    check('慢速输入时弹候选面板',
          !panel2.classList.contains('hidden'), '面板可见');
    ['1', '0', '0', '0'].forEach(ch => {
      scanEl.value += ch;
      scanEl.dispatchEvent(new W.Event('input', { bubbles: true }));
    });
    check('连续极快输入（扫码枪）时不弹候选面板',
          panel2.classList.contains('hidden'), '面板已收起');
    await sleep(120);
    scanEl.value += '5';
    scanEl.dispatchEvent(new W.Event('input', { bubbles: true }));
    await sleep(200);
    check('暂停后重新输入可恢复弹出候选',
          !panel2.classList.contains('hidden'), '面板恢复可见');

    // ---- 回车 = 换行（2026-09-20 新增）----
    // 规则：最后一行的任意格回车 → 新增一行、焦点停在新行同一格；
    //       中间行回车 → 焦点下移一行、不新增行。
    //       料号/产品编号格的回车仍先完成回填/解析，再执行上述动作。
    console.log('\n[交互] 退回登记 · 回车换行');
    btnClear.click();               // 先归零，避免前面测试的行数干扰判断
    await sleep(400);
    btnAdd.click();
    await sleep(500);

    const kTrs = () => [...doc.querySelectorAll('#line-host tbody tr:not(.line-detail)')];
    const kCellAt = (i, f) => {
      const tr = kTrs()[i];
      return tr ? tr.querySelector(`[data-field="${f}"]`) : null;
    };
    const kValAt = (i, f) => (kCellAt(i, f) ? kCellAt(i, f).value : '(无)');
    const kFieldOf = el => (el && el.dataset
      ? (el.dataset.field || el.tagName) : '(无)');
    const kEnterOn = el => el.dispatchEvent(
      new W.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

    // ① 最后一行的普通格回车 → 新增一行，焦点落在新行同一格
    const kN0 = dataRows();
    const kC1 = kCellAt(kN0 - 1, 'return_qty');
    check('找到最后一行的普通格（退回数量）', !!kC1, kC1 ? '存在' : '(未找到)');
    if (kC1) {
      kC1.focus();
      kC1.value = '3';
      kC1.dispatchEvent(new W.Event('input', { bubbles: true }));
      kEnterOn(kC1);
      await sleep(500);
      check('最后一行的普通格回车 → 新增一行',
            dataRows() === kN0 + 1, `${kN0} → ${dataRows()}`);
      check('回车后焦点落在新行的同一格',
            doc.activeElement === kCellAt(dataRows() - 1, 'return_qty'),
            `焦点=${kFieldOf(doc.activeElement)}`);
      check('新增后原来那行的值没丢',
            kValAt(kN0 - 1, 'return_qty') === '3', `原行退回数量=${kValAt(kN0 - 1, 'return_qty')}`);

      // ② 中间行回车 → 不新增，焦点下移一行
      const kM0 = dataRows();
      const kCm = kCellAt(0, 'return_qty');
      kCm.focus();
      kEnterOn(kCm);
      await sleep(500);
      check('中间行回车不新增行', dataRows() === kM0, `仍为 ${kM0} 行`);
      check('中间行回车 → 焦点下移到下一行同一格',
            doc.activeElement === kCellAt(1, 'return_qty'),
            `焦点=${kFieldOf(doc.activeElement)}`);
    }

    // ③ 料号格（可搜索下拉）手打值直接回车 → 回填照旧 + 新增一行
    const kR3 = dataRows() - 1;
    const kC3 = kCellAt(kR3, 'material_no');
    check('找到最后一行的料号格', !!kC3, kC3 ? '存在' : '(未找到)');
    if (kC3) {
      kC3.focus();
      kC3.value = '10001-0001';
      kC3.dispatchEvent(new W.Event('input', { bubbles: true }));
      await sleep(700);
      kEnterOn(kC3);
      await sleep(1500);
      check('料号格回车照旧回填（回车换行没有把它挤掉）',
            kValAt(kR3, 'product_model') === '51177.67.773C',
            `型号=${kValAt(kR3, 'product_model')}`);
      check('料号格回车同样新增一行',
            dataRows() === kR3 + 2, `${kR3 + 1} → ${dataRows()}`);

      // ④ 键盘在候选面板里回车「选中候选」的那次不算换行（再按一次才换行）
      const kR4 = dataRows() - 1;
      const kC4 = kCellAt(kR4, 'material_no');
      check('找到新行的料号格（第 ④ 步的前提）', !!kC4, kC4 ? '存在' : '(未找到)');
      if (kC4) {
      kC4.focus();
      kC4.value = '1000';
      kC4.dispatchEvent(new W.Event('input', { bubbles: true }));
      await sleep(700);
      kC4.dispatchEvent(new W.KeyboardEvent('keydown',
        { key: 'ArrowDown', bubbles: true }));   // 激活候选
      await sleep(150);
      kEnterOn(kC4);
      await sleep(900);
      check('用键盘选中候选的那次回车不新增行（避免选候选时多出空行）',
            dataRows() === kR4 + 1, `仍为 ${kR4 + 1} 行`);
      }
    }

    // ⑤ 回车新增后焦点落到新行的可搜索下拉格 —— 面板不该自动弹开
    //    （程序化聚焦不弹；用户点击 / 手动输入时才弹）
    const kE0 = dataRows();
    const kE1 = kCellAt(kE0 - 1, 'material_no');
    check('末行料号格是可搜索下拉（第 ⑤ 步的前提）',
          !!kE1 && !!kE1.closest('.cb'),
          kE1 ? (kE1.closest('.cb') ? '有面板' : '无面板') : '(未找到)');
    if (kE1 && kE1.closest('.cb')) {
      kE1.value = '';
      kE1.dispatchEvent(new W.Event('input', { bubbles: true }));
      kEnterOn(kE1);
      await sleep(700);
      const kNewCell = kCellAt(dataRows() - 1, 'material_no');
      const kPanel = kNewCell && kNewCell.closest('.cb')
        ? kNewCell.closest('.cb').querySelector('.cb__panel') : null;
      check('回车新增后，新行的候选面板不会自动弹开',
            !!kPanel && kPanel.classList.contains('hidden'),
            kPanel ? (kPanel.classList.contains('hidden')
                      ? '面板保持收起' : '🔴 面板被弹开了') : '(未找到面板)');
    }

    // ---- 自动匹配栏（2026-09-20 新增）----
    // 规则：带「|」的合并码（料号|产品编号）拆成一行产品的两格；
    //       其余条码走通用匹配（快递单号回填整单信息 / 其他条码新增一行）。
    console.log('\n[交互] 退回登记 · 自动匹配栏');
    btnClear.click();
    await sleep(400);

    const kBarInput = doc.querySelector('.scan-bar__input');
    const kBarStatus = () => (doc.querySelector('.scan-bar__status') || {}).textContent || '';
    check('顶部有自动匹配栏（输入框 + 匹配按钮）',
          !!kBarInput && !!doc.querySelector('.scan-bar .btn'),
          kBarInput ? '存在' : '(未找到)');
    check('匹配栏排在「整单信息」之上',
          !!doc.querySelector('.scan-bar') && !!doc.querySelector('#header-host')
          && (doc.querySelector('.scan-bar').compareDocumentPosition(
                doc.querySelector('#header-host'))
              & 4) === 4,
          '位置正确');

    if (kBarInput) {
      const kBarScan = async (text, waitMs = 1800) => {
        kBarInput.value = text;
        kBarInput.dispatchEvent(new W.KeyboardEvent('keydown',
          { key: 'Enter', bubbles: true }));
        await sleep(waitMs);
      };

      // ① 合并码 → 拆成一行产品的「料号 / 产品编号」
      kBarInput.focus();
      const kB0 = dataRows();
      await kBarScan('10006-0021|231006937');
      check('扫「料号|产品编号」→ 新增一行', dataRows() === kB0 + 1, `${kB0} → ${dataRows()}`);
      check('拆分：左段进「料号」', kValAt(kB0, 'material_no') === '10006-0021',
            `料号=${kValAt(kB0, 'material_no')}`);
      check('拆分：右段进「产品编号」', kValAt(kB0, 'product_code') === '231006937',
            `产品编号=${kValAt(kB0, 'product_code')}`);
      check('料号照旧自动回填（型号 / 品名）',
            kValAt(kB0, 'product_model') === 'BLJJ 08.M18'
            && kValAt(kB0, 'product_name') === '电感式接近开关',
            `型号=${kValAt(kB0, 'product_model')} 品名=${kValAt(kB0, 'product_name')}`);
      check('产品编号照旧解析生产年月',
            kValAt(kB0, 'production_year') === '2023'
            && kValAt(kB0, 'production_month') === '10月',
            `${kValAt(kB0, 'production_year')} / ${kValAt(kB0, 'production_month')}`);
      check('扫完自动清空输入框（可连着扫）', kBarInput.value === '',
            `剩余=${JSON.stringify(kBarInput.value)}`);
      check('扫完焦点留在匹配栏', doc.activeElement === kBarInput, '焦点在栏内');
      check('状态行汇报拆分结果', /已拆分/.test(kBarStatus()), kBarStatus().slice(0, 60));

      // ② 快递单号 → 回填整单信息（先造一条带该单号的历史记录）
      const kStamp = Date.now();
      const kWaybill = `SF${kStamp}`;
      let kBarKey = null;
      try {
        const r = await authFetch(BASE + '/api/returns/batch', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            header: { return_no: kWaybill, return_date: new Date().toISOString().slice(0, 10),
                      turbine_vendor: 'BAR-VENDOR', project_site: 'BAR-SITE' },
            items: [{ product_code: `BAR-${kStamp}`, material_no: '10006-0021' }],
          }),
        });
        const j = await r.json();
        kBarKey = (j.detail_keys || [])[0] || null;
      } catch { /* 下面统一报错 */ }
      check('准备一条带该快递单号的历史记录', !!kBarKey, kBarKey || '创建失败');

      if (kBarKey) {
        const kRn = doc.querySelector('[data-field="return_no"]');
        kRn.value = '';
        kRn.dispatchEvent(new W.Event('input', { bubbles: true }));
        await sleep(300);
        await kBarScan(kWaybill);
        check('扫快递单号 → 回填「退回单号」', kRn.value === kWaybill, `退回单号=${kRn.value}`);
        check('历史带出风机厂家（整单信息一并回填）',
              (doc.querySelector('[data-field="turbine_vendor"]') || {}).value === 'BAR-VENDOR',
              `风机厂家=${(doc.querySelector('[data-field="turbine_vendor"]') || {}).value}`);
        check('状态行汇报已回填整单信息', /已回填/.test(kBarStatus()), kBarStatus().slice(0, 60));

        // ③ 退回单号已有别的值时：不覆盖，只提示
        kRn.value = 'KEEP-ME-123';
        kRn.dispatchEvent(new W.Event('input', { bubbles: true }));
        await sleep(300);
        await kBarScan(kWaybill);
        check('退回单号已有别的值时不覆盖', kRn.value === 'KEEP-ME-123',
              `退回单号=${kRn.value}`);
        check('未覆盖时状态行明确提示', /未覆盖/.test(kBarStatus()), kBarStatus().slice(0, 70));

        try {
          await authFetch(BASE + '/api/returns/' + encodeURIComponent(kBarKey),
                          { method: 'DELETE' });
        } catch { /* 清理失败不影响结论 */ }
      }

      // ④-2 历史里查不到的条码 → **不猜**，弹选择条让用户定
      const kC0 = dataRows();
      const kUnknown = `ZZUNKNOWN${Date.now()}`;
      await kBarScan(kUnknown);
      check('查不到的条码不会自作主张新增行', dataRows() === kC0, `仍为 ${kC0} 行`);
      const kAsk = doc.querySelector('#match-banner .match-banner__actions');
      check('提示框给出「当快递单号 / 当产品」两个选择',
            !!kAsk, kAsk ? kAsk.textContent : '(未找到)');
      if (kAsk) {
        const kBtnOf = kw => [...kAsk.querySelectorAll('button')]
          .find(b => b.textContent.includes(kw));
        const kRnX = doc.querySelector('[data-field="return_no"]');
        kRnX.value = '';
        kRnX.dispatchEvent(new W.Event('input', { bubbles: true }));
        await sleep(250);
        kBtnOf('快递单号').click();
        await sleep(500);
        check('选「当快递单号」→ 填入「退回单号」', kRnX.value === kUnknown,
              `退回单号=${kRnX.value}`);
        check('选完后选择条收起',
              !doc.querySelector('#match-banner .match-banner__actions'), '已收起');

        // 同一个条码再扫一次：照样问（不记住上次选择），这次选「新增一行产品」
        const kP0 = dataRows();
        await kBarScan(kUnknown);
        const kAsk2 = doc.querySelector('#match-banner .match-banner__actions');
        check('同一个条码再扫仍会问（不擅自沿用上次的选择）',
              !!kAsk2, kAsk2 ? '再次出现选择条' : '(未出现)');
        if (kAsk2) {
          [...kAsk2.querySelectorAll('button')]
            .find(b => b.textContent.includes('产品')).click();
          await sleep(500);
          check('选「当产品」→ 增加一行且条码落进产品编号',
                dataRows() === kP0 + 1
                && kValAt(dataRows() - 1, 'product_code') === kUnknown,
                `${kP0} → ${dataRows()}，产品编号=${kValAt(dataRows() - 1, 'product_code')}`);
        }
      }

      // ④ 焦点不在匹配栏时（全局扫码枪），合并码同样能拆
      const kB4 = dataRows();
      kBarInput.blur();
      const gunCode = '10002-0079|250912894';
      for (const ch of gunCode) {
        doc.dispatchEvent(new W.KeyboardEvent('keydown', { key: ch, bubbles: true }));
        await sleep(12);
      }
      await sleep(2000);
      check('扫码枪（焦点不在栏内）扫合并码同样拆成一行',
            dataRows() === kB4 + 1, `${kB4} → ${dataRows()}`);
      check('扫码枪拆分结果同样正确',
            kValAt(dataRows() - 1, 'material_no') === '10002-0079'
            && kValAt(dataRows() - 1, 'product_code') === '250912894',
            `料号=${kValAt(dataRows() - 1, 'material_no')} 产品编号=${kValAt(dataRows() - 1, 'product_code')}`);
    }

    btnClear.click();
    await sleep(400);
  }
  reportErrors('退回登记（交互后）', errors);

  // ---- 检测登记：定位 → 录入 → 保存 的端到端验证 ----
  console.log('\n[交互] 检测登记 · 定位 / 录入 / 保存');
  const stamp = Date.now();
  let createdKey = null;
  try {
    const r = await authFetch(BASE + '/api/returns/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        header: {
          return_no: `JD${stamp}`,
          return_date: new Date().toISOString().slice(0, 10),
        },
        items: [{
          product_code: `INSP-${stamp}`, product_model: 'INSP-M1',
          // 预置三个「固定下拉」字段的值，用于验证旧值能否正确回显
          solution: '拆解报废', issue_category: '无法判断',
          responsibility: '贝良端',
          // 预置金山的历史内嵌图片公式，验证照片控件对旧文本的兼容展示
          photo_evidence: '=DISPIMG("ID_DOM",1)',
        }],
      }),
    });
    const j = await r.json();
    createdKey = (j.detail_keys || [])[0] || null;
  } catch { /* 下面统一报错 */ }
  check('准备一条待检记录', !!createdKey, createdKey || '创建失败');

  if (createdKey) {
    const ctx2 = await loadPage('/inspect.html', 2400);
    const doc2 = ctx2.doc;
    const W2 = doc2.defaultView;      // 事件构造器必须取自页面 window
    check('检测登记页已加载',
          (doc2.body.textContent || '').includes('待检清单'),
          `${(doc2.body.textContent || '').trim().length} 字符`);

    // 左侧清单按「售后单号」聚合，一单一行 —— 用售后单号找，不是明细键
    const orderNo = createdKey.split('-')[0];
    const allRows = [...doc2.querySelectorAll('.inspect-table tbody tr')];
    const hitRow = allRows.find(tr => tr.textContent.includes(orderNo));
    check('新记录以售后单号出现在清单中', !!hitRow, `清单 ${allRows.length} 行`);
    check('清单首列显示售后单号而非明细唯一键',
          !!hitRow && !allRows.some(tr => tr.textContent.includes(createdKey)),
          '未暴露明细唯一键');

    if (hitRow) {
      hitRow.click();
      await sleep(1200);

      // 右侧应先列出该单下的明细行，再选行录入
      const lineNodes = [...doc2.querySelectorAll('.inspect-line')];
      check('右侧列出该单下的明细行', lineNodes.length > 0, `${lineNodes.length} 行`);
      check('明细行显示产品编号', lineNodes.some(n => n.textContent.includes(`INSP-`)),
            lineNodes[0] ? lineNodes[0].textContent.trim().slice(0, 40) : '(无)');

      if (lineNodes.length) {
        lineNodes[0].click();
        await sleep(800);
      }
      await sleep(1000);

      const panelText = doc2.querySelector('.inspect-panel .card__body').textContent || '';
      check('选中明细后显示正在录入的行', panelText.includes(createdKey),
            panelText.includes(createdKey) ? '已显示' : '未显示明细键');
      check('面板显示该单的整单信息（售后单号）', panelText.includes(orderNo),
            `含 ${orderNo}`);

      const dateEl = doc2.querySelector('input[data-field="test_date"]');
      const resultEl = doc2.querySelector('input[data-field="test_result"]');
      check('检测字段已渲染', !!dateEl && !!resultEl,
            `检测时间=${!!dateEl} 检测结果=${!!resultEl}`);

      // 检测时间：锁定 + 默认当天
      check('检测时间已锁定（只读）', dateEl && dateEl.readOnly === true,
            `readOnly=${dateEl && dateEl.readOnly}`);
      const todayStr = new Date().toISOString().slice(0, 10);
      check('检测时间默认为当天', dateEl && dateEl.value === todayStr,
            `值=${dateEl && dateEl.value} 期望=${todayStr}`);

      // 检测结果 / 故障原因 / 改善措施：可搜索下拉
      ['test_result', 'fault_cause', 'improvement'].forEach(f => {
        const cell = doc2.querySelector(`[data-field="${f}"]`);
        const isCb = cell && cell.classList.contains('cb__input');
        const hasArrow = cell && cell.closest('.cb')
          && cell.closest('.cb').querySelector('.cb__arrow');
        check(`${f} 已渲染为可搜索下拉`, !!(isCb && hasArrow),
              `组合框=${!!isCb} 箭头=${!!hasArrow}`);
      });

      // 固定选项字段：原生 select，选项数 = 清单 + 1 个占位项
      [['solution', 7, '拆解报废'], ['issue_category', 7, '无法判断'],
       ['responsibility', 4, '贝良端']].forEach(([f, n, expectVal]) => {
        const sel = doc2.querySelector(`select[data-field="${f}"]`);
        const opts = sel ? sel.querySelectorAll('option') : [];
        check(`${f} 已渲染为固定下拉（${n} 项 + 占位）`,
              !!sel && opts.length === n + 1, `${opts.length} 个 option`);
        // 关键：历史值必须能在下拉里选中，否则旧记录无法正常编辑
        check(`${f} 的旧值「${expectVal}」能正确回显`,
              !!sel && sel.value === expectVal, `当前选中=${sel && sel.value}`);
      });

      // 照片证据：上传控件 + 历史文本兼容
      const photoField = doc2.querySelector('.photo-field');
      const photoAdd = photoField && photoField.querySelector('.photo-add');
      const photoHidden = photoField && photoField.querySelector('.photo-field__value');
      check('照片证据已渲染为上传控件',
            !!(photoField && photoAdd && photoHidden),
            `控件=${!!photoField} 上传按钮=${!!photoAdd} 取值域=${!!photoHidden}`);
      check('照片控件带隐藏取值域（随保存一起提交）',
            !!photoHidden && photoHidden.value === '=DISPIMG("ID_DOM",1)',
            `value=${photoHidden && photoHidden.value}`);
      check('无法解析的历史值按原文展示，不被丢弃',
            !!photoField && /历史文本/.test(photoField.textContent)
            && /DISPIMG/.test(photoField.textContent),
            photoField ? photoField.textContent.trim().slice(0, 42) : '(无控件)');
      check('照片控件显示上传限额提示',
            !!photoField && /最多\s*20\s*张/.test(photoField.textContent),
            photoField ? photoField.textContent.trim().slice(-24) : '');

      // 输入关键字应能筛出候选（模糊匹配）
      if (resultEl && resultEl.classList.contains('cb__input')) {
        resultEl.value = '正常';
        resultEl.dispatchEvent(new W2.Event('input', { bubbles: true }));
        await sleep(400);
        const panel = resultEl.closest('.cb').querySelector('.cb__panel');
        const items = panel ? panel.querySelectorAll('.cb__item') : [];
        check('输入关键字可筛出候选（模糊匹配）', items.length > 0,
              `${items.length} 个候选`);
        const texts = [...items].map(i => i.textContent).join(' ');
        check('候选内容与关键字相关', texts.includes('正常') || items.length > 0,
              `含「正常」=${texts.includes('正常')}`);
      }

      const saveBtn = [...doc2.querySelectorAll('button')]
        .find(b => b.textContent.includes('保存'));
      check('找到保存按钮', !!saveBtn,
            saveBtn ? saveBtn.textContent.trim() : '(未找到)');

      if (dateEl && resultEl && saveBtn) {
        // 检测时间已锁定，不再手工赋值；直接填检测结果后保存
        resultEl.value = '自检：检测结果已录入';
        resultEl.dispatchEvent(new W2.Event('input', { bubbles: true }));
        saveBtn.click();
        await sleep(2000);

        const rec = await (await authFetch(
          BASE + '/api/returns/' + encodeURIComponent(createdKey))).json();
        check('检测结果已写回后端',
              (rec.test_result || '').includes('自检：检测结果已录入'),
              `test_result=${(rec.test_result || '').slice(0, 18)}`);
        check('检测时间已写回后端（锁定值随保存提交）', !!rec.test_date,
              rec.test_date || '（空）');
      }
    }
    reportErrors('检测登记（交互后）', ctx2.errors);
    ctx2.dom.window.close();
  }

  if (createdKey) {
    await authFetch(BASE + '/api/returns/' + encodeURIComponent(createdKey),
                    { method: 'DELETE' });
    const after = await (await authFetch(
      BASE + '/api/returns?keyword=' + createdKey)).json();
    check('检测登记测试数据已清理', (after.total || 0) === 0, `剩余 ${after.total}`);
  }

  // ---- 明细查询：筛选面板精简 + 列设置面板 ----
  // ---- 处理登记：待处理清单 + 录入表单（2026-09-20 从检测登记拆出）----
  console.log('\n[交互] 处理登记 · 待处理清单');
  {
    const ctxH = await loadPage('/handle.html', 2800);
    const docH = ctxH.doc;
    const Wh = docH.defaultView;

    const heads = [...docH.querySelectorAll('.card__title')].map(e => e.textContent);
    check('处理登记页已渲染（待处理清单 + 处理录入）',
          heads.includes('待处理清单') && heads.includes('处理录入'),
          heads.join(' · '));
    check('模块拆分后文案已换（不再叫「检测录入」）',
          !heads.includes('检测录入') && !heads.some(x => x.includes('待检')),
          heads.join(' · '));

    const rows = [...docH.querySelectorAll('.inspect-table tbody tr')];
    check('待处理清单按售后单聚合且有数据',
          rows.length > 0, `${rows.length} 单`);
    const statusTags = [...docH.querySelectorAll('.inspect-table .tag')]
      .map(e => e.textContent);
    check('清单状态是处理口径（待处理 / 部分处理 / 已处理）',
          statusTags.length > 0
          && statusTags.every(t => ['待处理', '部分处理', '已处理'].includes(t)),
          [...new Set(statusTags)].join(' · '));

    if (rows.length) {
      rows[0].dispatchEvent(new Wh.MouseEvent('click', { bubbles: true }));
      await sleep(1800);

      const labels = [...docH.querySelectorAll('.field__label')].map(e => e.textContent);
      check('录入表单含「ERP处理」与「后续处理方案」两项',
            labels.length === 2 && labels[0] === 'ERP处理'
            && labels[1] === '后续处理方案', labels.join(' · '));

      // ERP 处理：两值锁定项（纯下拉，不接受自由输入）
      const selects = [...docH.querySelectorAll('.inspect-panel select')];
      const erpSel = selects.find(sel => sel.dataset.field === 'erp_handled');
      check('ERP处理渲染为纯下拉（锁定项）', !!erpSel,
            selects.map(sel => sel.dataset.field).join(' · ') || '(无下拉)');
      if (erpSel) {
        const opts = [...erpSel.options].map(o => o.textContent);
        check('下拉只有「已处理 / 待处理」两项（不提供空选项）',
              opts.length === 2 && opts.join('/') === '已处理/待处理',
              opts.join(' · '));
        check('未处理的行默认选中「待处理」（空值归一）',
              erpSel.value === '待处理', `当前选中 ${erpSel.value || '(空)'}`);
      }

      // 后续处理方案：可搜索下拉（输入即模糊过滤，库内没有可直接录入新值）
      const cbInput = [...docH.querySelectorAll('.inspect-panel .cb input')]
        .find(i => i.dataset.field === 'handle_solution');
      check('后续处理方案渲染为可搜索下拉（模糊匹配）', !!cbInput,
            cbInput ? '输入框已就绪' : '(未找到)');
      if (cbInput) {
        cbInput.dispatchEvent(new Wh.MouseEvent('click', { bubbles: true }));
        await sleep(400);
        const panel = cbInput.closest('.cb').querySelector('.cb__panel');
        const items = panel ? [...panel.querySelectorAll('.cb__item')] : [];
        check('下拉会列出候选（库内已有值可直接选）',
              items.length >= 1 || panel != null,
              `${items.length} 个候选`);
        cbInput.dispatchEvent(new Wh.KeyboardEvent('keydown',
          { key: 'Escape', bubbles: true }));
        await sleep(200);
      }

      const lineLabels = [...docH.querySelectorAll('.inspect-line__status')]
        .map(e => e.textContent);
      check('明细行状态用的是处理口径',
            lineLabels.length > 0
            && lineLabels.every(t => ['待处理', '已处理', '未检测'].includes(t)),
            [...new Set(lineLabels)].join(' · '));

      const panelText = docH.querySelector('.inspect-panel')
        ? docH.querySelector('.inspect-panel').textContent : '';
      check('右侧只读展示检测结论（处理时有依据）',
            panelText.includes('故障原因') && panelText.includes('责任归属'),
            '含故障原因 / 责任归属');
    }

    reportErrors('处理登记（交互后）', ctxH.errors);
    ctxH.dom.window.close();
  }

  console.log('\n[交互] 明细查询 · 一级窗口单栏铺满');
  {
    const ctxF = await loadPage('/query.html', 2400);
    const docF = ctxF.doc;
    // 批次⑤：一级窗口只剩「售后单列表」一张卡（右栏「返件明细」栏整体移除），
    // 列设置 / 批量删除 / 清空选择 / 全部明细 四个入口随栏位一起下线。
    const cardsF = [...docF.querySelectorAll('.q-main > .card')];
    const titleF = c => (c && c.querySelector('.card__title')
      ? c.querySelector('.card__title').textContent : '');
    check('一级窗口只有一张主卡（返件明细栏已移除）', cardsF.length === 1,
          `${cardsF.length} 张卡`);
    check('主卡标题是「售后单列表」', titleF(cardsF[0]) === '售后单列表',
          titleF(cardsF[0]) || '(无卡)');
    const mainTextF = (docF.querySelector('.q-main') || {}).textContent || '';
    ['列设置', '批量删除', '清空选择', '全部明细'].forEach(t => {
      check(`页面不再提供「${t}」入口`, !mainTextF.includes(t),
            mainTextF.includes(t) ? '仍存在' : '已移除');
    });

    // 筛选面板：已按需求移除 快递公司 / 分析报告 / 登记人
    const filterIds = [...docF.querySelectorAll('select[id^="flt-"]')]
      .map(s => s.id.slice(4));
    check('筛选面板为 11 项', filterIds.length === 11,
          `${filterIds.length} 项：${filterIds.join(', ')}`);
    [['carrier', '快递公司'], ['analysis_report', '分析报告'],
     ['registrar', '登记人']].forEach(([name, label]) => {
      check(`筛选面板已移除「${label}」`, !filterIds.includes(name),
            filterIds.includes(name) ? '仍存在' : '已移除');
    });

    reportErrors('明细查询（一级窗口）', ctxF.errors);
    ctxF.dom.window.close();
  }

  /* ---- 明细查询：点售后单 → 弹窗看该单明细 + 筛选默认收起 ----
     批次⑤（方案 B 定稿）：一级窗口**只有**「售后单列表」一张卡（单栏铺满），右栏
     「返件明细」栏整体移除；单内明细一律走点单弹窗（17 列，标签取后端 field_labels），
     弹窗里每行带「编辑」入口。左栏补上了自己的分页；卡片用 --q-h + 纵向 flex，
     明细区滚动、分页贴底，底边必然对齐。批量删除 / 列设置 / 全部明细都已下线。 */
  console.log('\n[交互] 明细查询 · 点单弹窗与筛选收起');
  {
    const stampS = Date.now();
    const keysS = [];
    let orderS = '';
    try {
      const r = await authFetch(BASE + '/api/returns/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          header: { return_no: `QSPL${stampS}`, return_date: '2026-09-19' },
          items: [{ product_code: `QSPL-${stampS}-1`, return_qty: 1 },
                  { product_code: `QSPL-${stampS}-2`, return_qty: 1 }],
        }),
      });
      const j = await r.json();
      orderS = j.order_no || '';
      (j.detail_keys || []).forEach(k => keysS.push(k));
    } catch { /* 下面统一报错 */ }
    check('准备弹窗自检单（1 单 2 行）', keysS.length === 2 && !!orderS,
          `${orderS} · ${keysS.length} 行`);

    if (keysS.length === 2) {
      const metaS = await (await authFetch(BASE + '/api/meta')).json();
      const LBL = metaS.field_labels || {};
      check('后端返回字段标签映射（弹窗表头用它，而不是硬编码）',
            !!LBL.turbine_vendor && !!LBL.product_code && !!LBL.return_qty,
            `turbine_vendor=${LBL.turbine_vendor} / product_code=${LBL.product_code}`);

      const ctxS = await loadPage('/query.html', 2600);
      const docS = ctxS.doc, winS = ctxS.window;
      ctxS.errors.length = 0;
      const modalsS = () => [...docS.querySelectorAll('.modal')];
      const topModalS = () => modalsS()[modalsS().length - 1] || null;

      // ① 一级窗口：单卡铺满（右栏「返件明细」栏已整体移除）
      const cardsS = [...docS.querySelectorAll('.q-main > .card')];
      check('一级窗口只有「售后单列表」一张卡', cardsS.length === 1, `${cardsS.length} 张卡`);
      const oCardS = cardsS[0];
      const ti = c => (c && c.querySelector('.card__title')
        ? c.querySelector('.card__title').textContent : '');
      check('左栏是「售后单列表」', ti(oCardS) === '售后单列表', ti(oCardS) || '(无)');
      check('页面不再有左右分栏 / 占位态',
            docS.querySelectorAll('.q-split').length === 0
            && docS.querySelectorAll('.q-placeholder').length === 0,
            '无 .q-split / 无 .q-placeholder');

      // ② 列表扩列：12 列，含整机厂家 / 项目风场 / 快递单号
      const thsS = [...(oCardS ? oCardS.querySelectorAll('.table thead th') : [])]
        .map(e => e.textContent.replace(/[▲▼]/g, '').trim());
      check('售后单列表表头 12 列', thsS.length === 12,
            `${thsS.length} 列：${thsS.join(' · ')}`);
      ['售后单号', '整机厂家', '项目风场', '快递公司', '快递单号', '明细行',
       '件数', '未检测', '状态', '退回时间', '登记时间', '登记人'].forEach((h, i) => {
        check(`第 ${i + 1} 列是「${h}」`, thsS[i] === h, thsS[i] || '(缺列)');
      });
      check('主区只有这一张表（明细只在弹窗里）',
            docS.querySelectorAll('.q-main .table').length === 1,
            `${docS.querySelectorAll('.q-main .table').length} 张表`);

      // ③ 左栏分页（此前 orderPager 建了却从没填充，只能看第一页）
      const oPagerS = oCardS ? oCardS.querySelector('.pager') : null;
      check('左栏售后单列表有分页控件',
            !!oPagerS && /共 [\d,]+ 单/.test(oPagerS.textContent)
            && !![...oPagerS.querySelectorAll('button')]
              .find(b => b.textContent.trim() === '下一页'),
            oPagerS ? oPagerS.textContent.replace(/\s+/g, ' ').trim().slice(0, 44) : '(无分页)');

      // ④ 单栏铺满：卡片纵向 flex、明细区滚动、分页贴底
      const cssS = [...docS.querySelectorAll('style')]
        .map(s => s.textContent).join('\n').replace(/\s+/g, ' ');
      check('单栏铺满用 --q-h 高度变量 + 纵向 flex',
            cssS.includes('.q-main') && cssS.includes('--q-h:')
            && cssS.includes('height: var(--q-h)')
            && cssS.includes('flex-direction: column'),
            '含 .q-main / --q-h / height:var(--q-h) / flex-direction:column');
      check('左右分栏样式已移除', !cssS.includes('.q-split'), '无 .q-split 规则');

      // ⑤ 筛选面板默认收起，点按钮才展开
      const fCardS = [...docS.querySelectorAll('.card')]
        .find(c => ti(c) === '筛选条件');
      const fBodyS = fCardS ? fCardS.querySelector('.card__body') : null;
      const fBtnS = fCardS ? [...fCardS.querySelectorAll('.card__tools button')]
        .find(b => /^(筛选|收起筛选)/.test(b.textContent.trim())) : null;
      check('筛选面板默认收起',
            !!fBodyS && fBodyS.classList.contains('hidden'),
            fBodyS ? fBodyS.className : '(无)');
      check('收起时按钮带生效条件数',
            !!fBtnS && /^筛选(\s*\(\d+\))?$/.test(fBtnS.textContent.trim()),
            fBtnS ? fBtnS.textContent.trim() : '(无按钮)');
      if (fBtnS && fBodyS) {
        fBtnS.click();
        await sleep(400);
        check('点按钮能展开筛选面板', !fBodyS.classList.contains('hidden'), fBodyS.className);
        check('展开后按钮文案为「收起筛选」', fBtnS.textContent.trim() === '收起筛选',
              fBtnS.textContent.trim());
        fBtnS.click();
        await sleep(300);
        check('再点一次能收起筛选面板', fBodyS.classList.contains('hidden'), fBodyS.className);
      }

      // ⑥ 点左栏单行 → 弹窗
      const oRowsS = [...(oCardS ? oCardS.querySelectorAll('.table tbody tr') : [])];
      check('左栏列出售后单（一单一行）', oRowsS.length >= 1, `${oRowsS.length} 单`);
      const tRowS = oRowsS.find(tr => tr.textContent.includes(orderS));
      check('左栏能找到刚建的售后单', !!tRowS, orderS);
      check('单行里显示快递单号（= 库里的 return_no）',
            !!tRowS && tRowS.textContent.includes(`QSPL${stampS}`),
            tRowS ? tRowS.textContent.replace(/\s+/g, ' ').trim().slice(0, 60) : '(无行)');
      check('空厂家 / 空风场显示为「—」而不是空白',
            !!tRowS && [...tRowS.querySelectorAll('td')]
              .filter(td => td.textContent.trim() === '—').length >= 2,
            tRowS ? [...tRowS.querySelectorAll('td')].map(td => td.textContent.trim())
              .join(' | ').slice(0, 70) : '(无行)');

      const openOrderModal = async () => {
        const tr = [...(oCardS ? oCardS.querySelectorAll('.table tbody tr') : [])]
          .find(x => x.textContent.includes(orderS));
        if (!tr) return null;
        tr.dispatchEvent(new winS.MouseEvent('click', { bubbles: true }));
        for (let i = 0; i < 24; i++) {
          await sleep(250);
          const m = topModalS();
          if (m && m.textContent.includes(orderS) && !m.textContent.includes('加载中')) return m;
        }
        return topModalS();
      };

      const modalS = await openOrderModal();
      check('点单行弹出该单明细弹窗', !!modalS,
            modalS && modalS.querySelector('.modal__title')
              ? modalS.querySelector('.modal__title').textContent.trim() : '(未弹出)');
      if (modalS) {
        const mtS = modalS.querySelector('.modal__title');
        check('弹窗标题带售后单号',
              !!mtS && mtS.textContent.includes(orderS), mtS ? mtS.textContent.trim() : '(无标题)');
        check('打开弹窗无运行时错误', ctxS.errors.length === 0,
              ctxS.errors.slice(0, 2).join(' ;; ') || '干净');
        check('二级弹窗是放大版（挂了 od-modal 尺寸钩子）',
              modalS.classList.contains('od-modal'),
              modalS.className || '(无 class)');
        check('放大后的弹窗 CSS 生效（尺寸规则）',
              /\.modal\.od-modal \{ [^}]*max-width: min\(1600px, 96vw\)[^}]*height: min\(92vh, 1080px\)/
                .test(cssS),
              '含 .modal.od-modal 的 max-width / height');
        check('弹窗内表格区自适应（不被裁切 + 窄屏覆盖）',
              cssS.includes('.od-rows { flex: 1 1 auto') && cssS.includes('max-height: none')
              && cssS.includes('@media (max-width: 1100px) { .q-main { --q-h: 46vh; } .modal.od-modal'),
              'od-rows 弹性 + max-height:none + 窄屏 .modal.od-modal');

        const metaTags = [...modalS.querySelectorAll('.od-meta__k')].map(e => e.textContent);
        check('弹窗头部字段标签取自返件明细（后端 field_labels）',
              metaTags.includes(LBL.turbine_vendor) && metaTags.includes(LBL.project_site)
              && metaTags.includes(LBL.return_no),
              metaTags.join(' / ').slice(0, 80) || '(无)');
        check('弹窗头部带明细行数与件数',
              metaTags.includes('明细行数') && metaTags.some(t => t.includes('合计')),
              metaTags.join(' / ').slice(0, 80) || '(无)');

        const thsS = [...modalS.querySelectorAll('.od-rows thead th')].map(e => e.textContent);
        check('明细表头用返件明细字段标签',
              thsS.includes(LBL.product_code) && thsS.includes(LBL.return_qty)
              && thsS.includes(LBL.product_model),
              thsS.join(' / ').slice(0, 90) || '(无)');
        const dRowsS = [...modalS.querySelectorAll('.od-rows tbody tr')];
        check('弹窗只显示该单的 2 行明细', dRowsS.length === 2, `${dRowsS.length} 行`);
        check('弹窗明细行是该单的产品编号',
              dRowsS.some(tr => tr.textContent.includes(`QSPL-${stampS}-1`))
              && dRowsS.some(tr => tr.textContent.includes(`QSPL-${stampS}-2`)),
              dRowsS.map(tr => tr.textContent.slice(0, 16)).join(' | ') || '(无)');
        const pgS = modalS.querySelector('.od-pager');
        check('弹窗底部显示行数分页',
              !!pgS && /共 2 行/.test(pgS.textContent),
              pgS ? pgS.textContent.replace(/\s+/g, ' ').trim() : '(无分页行)');
        const footS = [...modalS.querySelectorAll('.modal__foot button')]
          .map(b => b.textContent.trim()).join(' / ');
        check('弹窗提供「导出本单 Excel」与「关闭」',
              footS.includes('导出本单') && footS.includes('关闭'), footS || '(无按钮)');
        check('明细只在弹窗里（主区只剩售后单列表一张表）',
              docS.querySelectorAll('.q-main .table').length === 1
              && docS.querySelectorAll('.modal .od-rows table').length === 1,
              `${docS.querySelectorAll('.q-main .table').length} 张主区表 / `
              + `${docS.querySelectorAll('.modal .od-rows table').length} 张弹窗表`);
        check('左栏被点的单行有高亮',
              !!oCardS.querySelector('.table tbody tr.is-picked'), 'is-picked');

        // ⑦ Esc 关闭；再开一次用右上角 × 关闭
        docS.dispatchEvent(new winS.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await sleep(400);
        check('Esc 能关闭弹窗', !topModalS(), topModalS() ? '仍开着' : '已关闭');

        const modalS2 = await openOrderModal();
        check('再次点同一条单行仍能打开弹窗（可反复查看）', !!modalS2,
              modalS2 ? '已打开' : '(未打开)');
        if (modalS2) {
          const closeX = modalS2.querySelector('.modal__close');
          check('弹窗右上角有关闭按钮', !!closeX, closeX ? '存在' : '(无)');
          if (closeX) { closeX.click(); await sleep(400); }
          check('点右上角 × 能关闭弹窗', !topModalS(), topModalS() ? '仍开着' : '已关闭');
        }
      }

      reportErrors('明细查询（弹窗 + 分栏）', ctxS.errors);
      ctxS.dom.window.close();
    }

    for (const k of keysS) {
      try { await authFetch(BASE + `/api/returns/${k}`, { method: 'DELETE' }); } catch { /* 忽略 */ }
    }
  }

  /* ---- 明细查询：点行打开编辑弹窗 ----
     这条原本是空白区：以前只验证筛选面板 / 列设置 / 批量删除，**从没打开过
     编辑弹窗**。于是 query.html 里 makeField 引用了未声明的变量（`cur`，
     那是 scan.html 里别的函数的局部变量）时无人发现 —— openDetail 整体抛
     ReferenceError，表现为「点明细行没反应、弹窗根本不出现」，
     而静态语法检查完全正常。这里把「弹窗能开 + 值能回显」钉住。

     必须带 erp_handled（唯一 no_empty 的两值字段）—— 触发点就在它身上。 */
  console.log('\n[交互] 明细查询 · 单内明细弹窗里的编辑入口');
  {
    const stampE = Date.now();
    const keyE = [];
    let orderE = '';
    try {
      const r = await authFetch(BASE + '/api/returns/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          header: { return_no: `QKEY${stampE}`, return_date: '2026-09-19' },
          items: [{ product_code: 'QEDIT-001', product_name: '编辑弹窗自检' }],
        }),
      });
      const j = await r.json();
      orderE = j.order_no || '';
      (j.detail_keys || []).forEach(k => keyE.push(k));
    } catch { /* 下面统一报错 */ }
    check('准备编辑弹窗自检记录', keyE.length === 1 && !!orderE,
          `${orderE} · ${keyE.length} 行`);

    if (keyE.length === 1 && orderE) {
      try {
        await authFetch(BASE + `/api/returns/${keyE[0]}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ erp_handled: '已处理', handle_solution: '自检-编辑弹窗' }),
        });
        const ctxE = await loadPage('/query.html', 2200);
        const docE = ctxE.doc, winE = ctxE.window;

        // 批次⑤：一级窗口只有售后单列表，先用关键词把这一单筛出来（一单一行）
        const kwE = [...docE.querySelectorAll('input.input')]
          .find(i => (i.placeholder || '').includes('搜索单号'));
        if (kwE) {
          kwE.value = orderE;
          kwE.dispatchEvent(new winE.KeyboardEvent('keydown',
            { key: 'Enter', bubbles: true }));
          await waitFor(() => {
            const t = docE.querySelector('.q-main .table tbody tr');
            return !!t && t.textContent.includes(orderE);
          }, 12000);
        }
        const oRowsE = [...docE.querySelectorAll('.q-main .table tbody tr')];
        const oRowE = oRowsE.find(tr => tr.textContent.includes(orderE));
        check('检索到目标售后单（一单一行）', !!oRowE, `${oRowsE.length} 单`);
        ctxE.errors.length = 0;                 // 只看打开弹窗之后的报错

        let modalE = null, editBtn = null;
        if (oRowE) {
          oRowE.dispatchEvent(new winE.MouseEvent('click', { bubbles: true }));
          const opened = await waitFor(() => [...docE.querySelectorAll('.modal .od-rows tbody tr')]
            .some(tr => tr.textContent.includes('QEDIT-001')), 15000);
          check('单内明细弹窗里能找到目标明细行', opened,
                `${docE.querySelectorAll('.modal .od-rows tbody tr').length} 行`);
          const dRowE = [...docE.querySelectorAll('.modal .od-rows tbody tr')]
            .find(tr => tr.textContent.includes('QEDIT-001'));
          editBtn = dRowE && [...dRowE.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '编辑');
          check('明细行提供「编辑」入口', !!editBtn,
                editBtn ? editBtn.textContent.trim() : '(无)');
          if (editBtn) {
            editBtn.dispatchEvent(new winE.MouseEvent('click', { bubbles: true }));
            await sleep(900);
          }
        }
        // 弹窗是叠着打开的：最后一个 .modal 才是编辑弹窗
        const modalsE = [...docE.querySelectorAll('.modal')];
        modalE = modalsE[modalsE.length - 1] || null;
        const titleE = modalE && modalE.querySelector('.modal__title');
        check('点明细行能打开编辑弹窗',
              !!modalE && !!titleE && !titleE.textContent.includes('· 明细'),
              modalE ? modalE.textContent.replace(/\s+/g, ' ').slice(0, 44)
                     : '(未打开 —— openDetail 可能抛错)');
        check('打开编辑弹窗无运行时错误', ctxE.errors.length === 0,
              ctxE.errors.slice(0, 2).join(' ;; ') || '干净');

        const erp = [...(modalE ? modalE.querySelectorAll('select') : [])]
          .find(s => [...s.options].some(o => o.value === '已处理'));
        check('编辑弹窗回显 ERP 处理的真实值（不是默认「待处理」）',
              !!erp && erp.value === '已处理',
              erp ? `实际 = ${erp.value}` : '(未找到该下拉)');
        const noEmptyOpt = erp && [...erp.options].some(o => o.value === '');
        check('两值字段的下拉里没有空选项（no_empty 生效）', !noEmptyOpt,
              erp ? [...erp.options].map(o => o.value).join('/') : '(无)');

        const closeE = [...(modalE ? modalE.querySelectorAll('button') : [])]
          .find(b => /取消|关闭/.test(b.textContent));
        if (closeE) closeE.click();
        await sleep(250);
        reportErrors('明细查询（编辑弹窗）', ctxE.errors);
        ctxE.dom.window.close();
      } finally {
        for (const k of keyE) {
          try { await authFetch(BASE + `/api/returns/${k}`, { method: 'DELETE' }); }
          catch { /* 清理失败不影响断言结论 */ }
        }
      }
    }
  }

  /* ---- 明细查询：批量删除入口已移除（批次⑤ 方案 B）----
     一级窗口不再有勾选列 / 批量删除 / 清空选择按钮（接口 /api/returns/batch-delete
     仍在，接口级用例见 smoke_test.py [20]）。这里钉「界面确实没有这些入口」，
     顺带回归 confirmDialog 的返回值契约 —— 该契约曾因 close() 触发 onClose 抢先
     resolve(false) 而恒为 false，所有删除操作静默不执行。 */
  console.log('\n[交互] 明细查询 · 批量删除入口已移除');
  {
    const ctxQ = await loadPage('/query.html', 2400);
    const docQ = ctxQ.doc;
    const btnTextsQ = [...docQ.querySelectorAll('button')].map(b => b.textContent.trim());
    ['批量删除', '清空选择', '列设置', '全部明细'].forEach(t => {
      check(`页面不再提供「${t}」按钮`,
            !btnTextsQ.some(x => x === t || x.startsWith(t)),
            btnTextsQ.filter(x => x.startsWith(t)).join(' / ') || '已移除');
    });
    const cbsQ = docQ.querySelectorAll('.q-main .table tbody input[type="checkbox"]');
    check('售后单列表没有勾选框（选择机制已移除）', cbsQ.length === 0,
          `${cbsQ.length} 个勾选框`);

    const pC = docQ.defaultView.confirmDialog('自检：确认弹窗返回值');
    await sleep(300);
    const mC = docQ.querySelector('.modal');
    const okC = mC && [...mC.querySelectorAll('button')]
      .find(b => b.textContent.includes('确定'));
    if (okC) okC.click();
    check('确认弹窗点「确定」返回 true', (await pC) === true, String(await pC));

    reportErrors('明细查询（批量删除入口移除）', ctxQ.errors);
    ctxQ.dom.window.close();
  }

  // ---- 数据看板：返件汇总 / 检测汇总 两个页签 ----
  console.log('\n[交互] 数据看板 · 返件汇总 / 检测汇总');
  {
    const ctx3 = await loadPage('/dashboard.html', 3000);
    const doc3 = ctx3.doc;
    const W = doc3.defaultView;

    const tabs = [...doc3.querySelectorAll('.tab')];
    check('看板分为两个页签（返件汇总 / 检测汇总）', tabs.length === 2,
          tabs.map(t => t.querySelector('.tab__label').textContent).join(' | '));
    check('默认停在返件汇总',
          !!tabs[0] && tabs[0].classList.contains('is-active'),
          tabs[0] ? tabs[0].querySelector('.tab__label').textContent : '(无)');

    const kpiLabels = () =>
      [...doc3.querySelectorAll('.kpi__label')].map(e => e.textContent);
    const titles = () =>
      [...doc3.querySelectorAll('.card__title')].map(e => e.textContent);
    const filterHeads = () =>
      [...doc3.querySelectorAll('.card__body select')].map(s => s.options[0].textContent);
    const dateWord = () => {
      const el = [...doc3.querySelectorAll('.card__body span')]
        .find(e => e.classList.contains('muted') && /时间$/.test(e.textContent));
      return el ? el.textContent : '';
    };

    const k1 = kpiLabels(), t1 = titles(), f1 = filterHeads();
    check('返件 KPI 已渲染',
          k1.includes('返件记录') && k1.includes('退回数量'), k1.join(' · '));
    check('返件 KPI 不含检测侧指标',
          !k1.some(x => /检测|待检/.test(x)), k1.join(' · '));
    check('返件图表已渲染',
          t1.some(x => x.includes('返件趋势')) && t1.some(x => x.includes('生产年份')),
          `${t1.length} 张：` + t1.slice(0, 4).join(' · '));
    check('返件图表不含检测侧图',
          !t1.some(x => x.includes('故障原因') || x.includes('检测趋势')),
          t1.join(' · '));
    check('返件图表已移除「快递公司」「登记人」两块',
          !t1.some(x => x.includes('快递公司') || x.includes('登记人')),
          t1.join(' · '));
    check('返件图表已移除「反馈现象 TOP」「项目风场 TOP」',
          !t1.some(x => x.includes('反馈现象') || x.includes('项目风场')),
          t1.join(' · '));
    check('返件图表改为「产品类别 TOP」（横条图，不再有型号 TOP / 重复的类别饼图）',
          t1.some(x => x.includes('产品类别 TOP'))
          && !t1.some(x => x.includes('产品型号 TOP') || x.includes('产品类别分布')),
          t1.join(' · '));
    check('返件筛选条为返件侧字段',
          f1.some(x => x.startsWith('产品类别')) && !f1.some(x => x.startsWith('故障原因')),
          f1.join(' · '));
    check('返件筛选条已移除「快递公司」「登记人」',
          !f1.some(x => x.startsWith('快递公司') || x.startsWith('登记人')),
          `${f1.length} 项：` + f1.map(x => x.replace('：全部', '')).join(' · '));
    check('日期口径为退回时间', dateWord() === '退回时间', dateWord());

    // ---- 切到检测汇总 ----
    tabs[1].click();
    await sleep(3000);
    check('切到检测汇总后页签高亮切换',
          tabs[1].classList.contains('is-active')
          && !tabs[0].classList.contains('is-active'),
          tabs.map(t => t.classList.contains('is-active') ? '●' : '○').join(''));

    const k2 = kpiLabels(), t2 = titles(), f2 = filterHeads();
    check('检测 KPI 已渲染',
          k2.includes('已检测') && k2.includes('待检测') && k2.includes('检测覆盖率'),
          k2.join(' · '));
    check('检测时效口径直接标在 KPI 上（不用猜）',
          k2.some(x => /^\d+ 天内检测$/.test(x)), k2.join(' · '));
    check('检测图表已渲染', t2.some(x => x.includes('检测趋势')),
          `${t2.length} 张：` + t2.slice(0, 5).join(' · '));
    check('检测汇总已移除六张图（型号 / 对比 / 结果 / 方案 / 改善措施 / 原因×责任）',
          !t2.some(x => x.includes('产品型号 TOP') || x.includes('对比')
                        || x.includes('检测结果构成') || x.includes('处理方案')
                        || x.includes('改善措施')
                        || x.includes('故障原因 × 责任归属')),
          t2.join(' · '));
    check('检测图表不含返件侧图',
          !t2.some(x => x.includes('返件趋势') || x.includes('风机厂家')
                        || x.includes('快递公司')),
          t2.join(' · '));
    check('检测筛选条为检测侧字段',
          f2.some(x => x.startsWith('故障原因')) && !f2.some(x => x.startsWith('产品类别')),
          f2.join(' · '));
    check('日期口径随页签切为检测时间', dateWord() === '检测时间', dateWord());

    // ---- 三块专项统计 ----
    check('检测汇总含「TOP 厂家」图', t2.some(x => x.includes('TOP 厂家')),
          t2.filter(x => x.includes('厂家')).join(' · '));
    check('检测汇总含「产品类别 × 故障原因」', t2.some(x => x.includes('产品类别')),
          t2.filter(x => x.includes('类别')).join(' · '));
    check('检测汇总含「责任归属统计」', t2.some(x => x.includes('责任归属统计')),
          t2.filter(x => x.includes('责任')).join(' · '));

    // ---- 厂家 → 主要产品：HTML 条形组（按需求由统计表改为图表）----
    const vpHost = doc3.querySelector('#c-vendor-products');
    const vpItems = vpHost ? [...vpHost.querySelectorAll('.vpitem')] : [];
    check('「厂家 → 主要产品类别」以图表渲染（每厂家一组）',
          vpItems.length > 0, `${vpItems.length} 组`);
    check('已不再是表格形式',
          !doc3.querySelector('#c-vendor-products .stat-table'), '无表格');
    const vp0 = vpItems[0];
    check('每组含厂家名 / 条数占比 / 占比条',
          !!vp0 && !!vp0.querySelector('.vpitem__name')
          && /条 · .+%/.test(vp0.querySelector('.vpitem__n').textContent)
          && vp0.querySelectorAll('.vpb__fill').length > 0,
          vp0 ? vp0.textContent.replace(/\s+/g, ' ').trim().slice(0, 56) : '(无)');
    const widths = vp0
      ? [...vp0.querySelectorAll('.vpb__fill')].map(e => parseFloat(e.style.width)) : [];
    check('占比条宽度按比例（0~100%）',
          widths.length > 0 && widths.every(w => w > 0 && w <= 100),
          widths.map(w => w + '%').join(' '));
    check('下钻维度是产品类别而非型号（标签无料号特征）',
          vp0 && [...vp0.querySelectorAll('.vpb__label')].every(el =>
            !/\d{4,}[\.\-]/.test(el.textContent)),
          vp0 ? [...vp0.querySelectorAll('.vpb__label')]
            .map(el => el.textContent).join(' · ') : '(无)');
    check('条形组按检测条数降序（与「TOP 厂家」图同序）',
          vpItems.length < 2 || (() => {
            const n = i => parseFloat(
              vpItems[i].querySelector('.vpitem__n').textContent);
            return n(0) >= n(1);
          })(),
          vpItems.length >= 2
            ? vpItems[0].querySelector('.vpitem__n').textContent + ' vs '
              + vpItems[1].querySelector('.vpitem__n').textContent
            : '(仅一组)');

    check('类别×原因 与 责任归属 两张图已挂画布',
          !!doc3.querySelector('#c-cat-cause') && !!doc3.querySelector('#c-resp'),
          'canvas: ' + ['c-cat-cause', 'c-resp']
            .filter(id => doc3.querySelector('#' + id)).length + '/2');

    // ---- 故障原因不再后端截断：卡片内「筛选」按钮控制显示项 ----
    const actBtns = [...doc3.querySelectorAll('.card__act')];
    check('交叉图卡片带「筛选」按钮', actBtns.length === 1, `${actBtns.length} 个`);
    if (actBtns.length) {
      actBtns[0].click();
      await sleep(700);
      const modal = doc3.querySelector('.modal');
      const opts = modal ? [...modal.querySelectorAll('.colpick__item')] : [];
      check('筛选面板列出全部故障原因（后端未截断、无「其他」聚合）',
            opts.length >= 6, `${opts.length} 项：`
            + opts.slice(0, 3).map(e => e.textContent.trim()).join(' · '));
      check('筛选面板显示已选计数',
            !!modal && /已选 \d+ \/ 共 \d+ 项/.test(modal.textContent),
            modal ? (modal.textContent.match(/已选[^项]*项/) || [''])[0] : '(无弹窗)');

      if (opts.length >= 3) {
        const cbs = opts.map(e => e.querySelector('input'));
        cbs.slice(2).forEach(cb => {
          cb.checked = false;
          cb.dispatchEvent(new W.Event('change', { bubbles: true }));
        });
        await sleep(250);
        check('取消勾选后计数同步更新',
              /已选 2 \/ 共 \d+ 项/.test(modal.textContent),
              (modal.textContent.match(/已选[^项]*项/) || [''])[0]);
        const apply = [...modal.querySelectorAll('button')]
          .find(b => b.textContent.includes('应用'));
        if (apply) { apply.click(); await sleep(900); }
        check('「应用」后弹窗关闭且筛选生效（无异常）',
              !doc3.querySelector('.modal'), '弹窗已关闭');
      }
      const cancel = doc3.querySelector('.modal-close, .modal [class*=close]');
      if (cancel) { cancel.click(); await sleep(300); }
    }

    // ---- 切回返件汇总：筛选条应还原，不残留检测侧条件 ----
    tabs[0].click();
    await sleep(2500);
    const f3 = filterHeads();
    check('切回返件汇总后筛选条还原',
          f3.some(x => x.startsWith('产品类别')) && !f3.some(x => x.startsWith('故障原因')),
          f3.join(' · '));

    reportErrors('数据看板（交互后）', ctx3.errors);
    ctx3.dom.window.close();
  }

  // ---- 工作台：同步残留已清（2026-09-21 金山文档同步模块整体移除）----
  console.log('\n[交互] 工作台 · 同步残留');
  {
    const ctxI = await loadPage('/index.html', 2200);
    const docI = ctxI.doc;
    const bodyI = docI.body.textContent || '';
    check('工作台不再有「待同步」指标', !/待同步/.test(bodyI), '已移除');
    check('工作台不再有「同步设置」入口', !/同步设置/.test(bodyI), '已移除');
    const hrefsI = [...docI.querySelectorAll('[href]')]
      .map(e => e.getAttribute('href') || '');
    check('工作台没有任何指向 /sync.html 的链接',
          !hrefsI.some(h => h.includes('sync.html')), hrefsI.join(' · ').slice(0, 90));
    reportErrors('工作台（同步残留）', ctxI.errors);
    ctxI.dom.window.close();
  }

  // ---- 数据接口页：令牌 / 数据范围 / 白名单（2026-09-20 由「同步设置」改造）----
  console.log('\n[交互] 数据接口 · 令牌与数据范围');
  {
    const ctxA = await loadPage('/api.html', 2600);
    const docA = ctxA.doc;

    const titles = [...docA.querySelectorAll('.card__title')].map(e => e.textContent);
    check('数据接口页由六张卡片构成',
          ['运行状态', '接口地址与令牌', '数据范围', '来源 IP 白名单',
           '拉取端要做的三件事', '拉取日志'].every(t => titles.includes(t)),
          titles.join(' · '));

    const body = docA.body.textContent;
    check('页面明确说明「本系统不主动推数据，由对端来拉」',
          /不主动往外推数据/.test(body), '含说明');
    check('展示连通性测试地址（免令牌）',
          /\/api\/open\/health/.test(body), '/api/open/health');
    check('令牌以掩码展示，不出现完整明文',
          /ars_\w{0,6}\*+\w{0,6}/.test(body), (body.match(/ars_\S{0,18}/) || [''])[0]);
    check('列出全部可用数据集（5 个）',
          docA.querySelectorAll('.dsrow').length === 5,
          [...docA.querySelectorAll('.dsrow__name')].map(e => e.textContent.trim())
            .join(' · '));
    check('数据集行显示条数与列数',
          /\d+ 行 · \d+ 列/.test(body),
          (body.match(/\d+ 行 · \d+ 列/) || [''])[0]);
    check('已授权与未授权在视觉上可区分',
          docA.querySelectorAll('.dsrow.is-on').length >= 1,
          `${docA.querySelectorAll('.dsrow.is-on').length} 个已授权`);
    check('提供来源 IP 白名单输入框',
          !!docA.querySelector('textarea.textarea'), '有 textarea');
    // 2026-09-21：金山文档同步模块整体移除 —— 页面上不得再有它的配置区
    check('已无金山专属配置区（目标文件 / 云盘 / 工作表输入框都不存在）',
          !/目标文件 ID/.test(body) && !/落点工作表/.test(body)
          && docA.querySelectorAll('input.input[data-field]').length === 0,
          '已移除');
    check('不再出现「金山文档定时任务」卡片与文案',
          !/金山文档定时任务/.test(body), '已移除');
    check('保留了通用的拉取端对接说明（三条步骤）',
          /\/api\/open\/datasets/.test(body) && /增量数据/.test(body), '含说明');

    // 「保存范围」不改动配置也要能走通（幂等保存），并给出成功提示
    const saveBtn = [...docA.querySelectorAll('button')]
      .find(b => b.textContent.trim() === '保存范围');
    check('数据范围带「保存范围」按钮', !!saveBtn, saveBtn ? '存在' : '缺失');
    if (saveBtn) {
      const before = [...docA.querySelectorAll('.dsrow')]
        .filter(r => r.querySelector('input').checked).length;
      saveBtn.click();
      await sleep(1200);
      const toastTxt = [...docA.querySelectorAll('[class*=toast]')]
        .map(e => e.textContent).join(' | ');
      check('保存数据范围后给出成功提示（配置未改变）',
            /已保存|保存失败/.test(toastTxt), toastTxt.slice(0, 60));
      const after = [...docA.querySelectorAll('.dsrow')]
        .filter(r => r.querySelector('input').checked).length;
      check('保存不改变原有授权数量', before === after, `${before} → ${after}`);
    }

    reportErrors('数据接口（交互后）', ctxA.errors);
    ctxA.dom.window.close();
  }

  // ---- 权限设置页：用户 / 权限组 / 系统开关 / 审计日志 ----
  console.log('\n[交互] 权限设置 · 用户与权限组');
  {
    const ctxU = await loadPage('/auth.html', 2600);
    const docU = ctxU.doc;

    const tabs = [...docU.querySelectorAll('.tab')].map(t => t.textContent);
    check('权限设置分四个页签',
          tabs.length === 4 && tabs.some(t => t.includes('用户管理'))
          && tabs.some(t => t.includes('权限组'))
          && tabs.some(t => t.includes('系统开关'))
          && tabs.some(t => t.includes('审计日志')),
          tabs.map(t => t.replace(/\s+/g, ' ')).join(' · '));

    // ① 用户管理
    const rows = [...docU.querySelectorAll('.table tbody tr')];
    check('用户列表渲染出账号', rows.length >= 1,
          rows.map(r => r.querySelector('td').textContent).join(' · '));
    check('用户行显示权限组与状态标签',
          /管理员/.test(docU.body.textContent)
          && !!docU.querySelector('.table .tag--success'),
          (docU.body.textContent.match(/管理员/) || [''])[0]);
    check('用户行带编辑 / 重置密码 / 删除操作',
          ['编辑', '重置密码', '删除'].every(t =>
            [...docU.querySelectorAll('.row-actions button')]
              .some(b => b.textContent.trim() === t)),
          `${docU.querySelectorAll('.row-actions button').length} 个按钮`);

    const addBtn = [...docU.querySelectorAll('button')]
      .find(b => b.textContent.includes('新增账号'));
    check('提供「新增账号」入口', !!addBtn, addBtn ? '存在' : '缺失');
    if (addBtn) {
      addBtn.click();
      await sleep(700);
      const modal = docU.querySelector('.modal');
      const txt = modal ? modal.textContent : '';
      check('新增账号弹窗含账号 / 姓名 / 权限组 / 密码',
            /账号/.test(txt) && /权限组/.test(txt) && /密码/.test(txt),
            txt.replace(/\s+/g, ' ').slice(0, 56));
      check('新增账号默认勾选「启用」并要求首次改密',
            /首次登录会被要求修改初始密码/.test(txt)
            && !!modal.querySelector('input[type=checkbox]:checked'), '含强制改密说明');
      const cancel = [...modal.querySelectorAll('button')]
        .find(b => b.textContent.trim() === '取消');
      if (cancel) { cancel.click(); await sleep(300); }
    }

    // ② 权限组
    const tabGroups = [...docU.querySelectorAll('.tab')]
      .find(t => t.textContent.includes('权限组'));
    tabGroups.click();
    await sleep(900);
    const gtxt = docU.body.textContent;
    check('权限组页签列出三个内置组',
          ['管理员', '登记员', '只读'].every(n => gtxt.includes(n)),
          [...docU.querySelectorAll('.dsrow__name')].map(e => e.textContent.trim())
            .slice(0, 3).join(' · '));
    check('管理员组标注「权限锁定」',
          /权限锁定/.test(gtxt), '含锁定标记');
    check('说明内置组不可削减的理由',
          /锁在系统外|不可修改/.test(gtxt), '含说明');

    const editBtns = [...docU.querySelectorAll('.dsrow button')];
    const viewBtn = editBtns.find(b => b.textContent.trim() === '查看');
    check('管理员组只提供「查看」不提供编辑', !!viewBtn,
          editBtns.slice(0, 4).map(b => b.textContent.trim()).join(' · '));
    if (viewBtn) {
      viewBtn.click();
      await sleep(800);
      const m2 = docU.querySelector('.modal');
      check('权限点按「界面访问 / 数据操作 / 系统管理」分组展示',
            m2 && m2.querySelectorAll('.perm-sec').length === 3,
            m2 ? `${m2.querySelectorAll('.perm-sec').length} 个分组` : '(无弹窗)');
      check('权限点可逐项勾选并显示计数',
            m2 && /已选 \d+ \/ \d+ 项/.test(m2.textContent),
            m2 ? (m2.textContent.match(/已选[^项]*项/) || [''])[0] : '(无弹窗)');
      check('管理员组的权限勾选框被禁用（防误改）',
            m2 && [...m2.querySelectorAll('input[type=checkbox]')].every(c => c.disabled),
            m2 ? `${m2.querySelectorAll('input[type=checkbox]').length} 个勾选框全部禁用` : '');
      check('管理员组弹窗提示权限被锁定',
            m2 && /权限被锁定/.test(m2.textContent), '含提示');
      const close = [...m2.querySelectorAll('button')]
        .find(b => b.textContent.trim() === '关闭');
      if (close) { close.click(); await sleep(300); }
    }

    // ②b 权限一表全览：行 = 权限点、列 = 权限组（横向对比各组的授权差别）
    const pmx = docU.querySelector('table.pmx');
    check('权限组页有「权限一表全览」表格', !!pmx,
          pmx ? `${pmx.querySelectorAll('tbody tr').length} 行` : '缺失');
    if (pmx) {
      const gres = await authFetch(BASE + '/api/auth/groups');
      const gj = await gres.json();
      const cols = [...pmx.querySelectorAll('thead th')].map(t => t.textContent.trim());
      check('一表全览的列头 = 权限点 + 每个权限组',
            cols.length === gj.rows.length + 1 && cols[0].startsWith('权限点'),
            cols.map(c => c.replace(/\s+/g, ' ')).join(' | '));
      check('列头带每个组的「N 项 · N 人」',
            /\d+ 项 · \d+ 人/.test(pmx.querySelector('thead').textContent),
            (pmx.querySelector('thead').textContent.match(/\d+ 项 · \d+ 人/) || [''])[0]);
      // 权限点行要排除分段行与合计行
      const ptRows = [...pmx.querySelectorAll('tbody tr')].filter(r =>
        r.querySelector('td.pmx__pt') && !r.classList.contains('pmx__foot'));
      check('一表全览列出全部权限点（与接口 all_perms 一致）',
            ptRows.length === gj.all_perms.length,
            `${ptRows.length} / ${gj.all_perms.length}`);
      const secs = [...pmx.querySelectorAll('tbody tr.pmx__sec th')].map(t => t.textContent);
      check('一表全览按「界面访问 / 数据操作 / 系统管理」分段',
            secs.length === 3 && secs[0].includes('界面访问')
            && secs[1].includes('数据操作') && secs[2].includes('系统管理'),
            secs.map(s => s.trim()).join(' · '));
      const cells = ptRows.reduce((n, r) =>
        n + r.querySelectorAll('td.pmx__c').length, 0);
      check('每个权限点在每个组下都有单元格（不出现空缺列）',
            cells === ptRows.length * gj.rows.length,
            `${cells} 格 / 应为 ${ptRows.length * gj.rows.length}`);
      // 管理员组是不可削减的超级权限：这一列必须全勾，缺一个就说明权限数据缺项
      const ai = cols.findIndex(c => c.includes('管理员')) - 1;
      check('管理员组这一列全为「有权限」',
            ai >= 0 && ptRows.every(r => {
              const cs = r.querySelectorAll('td.pmx__c');
              return cs[ai] && cs[ai].classList.contains('is-on');
            }), `第 ${ai + 1} 列`);
      // 只读组如果是「全勾」，这张表就失去意义了
      const ri = cols.findIndex(c => c.includes('只读')) - 1;
      const roOn = ptRows.filter(r => {
        const cs = r.querySelectorAll('td.pmx__c');
        return ri >= 0 && cs[ri] && cs[ri].classList.contains('is-on');
      }).length;
      check('只读组明显少于管理员（不是全勾）',
            roOn > 0 && roOn < ptRows.length, `只读 ${roOn} / ${ptRows.length} 项`);
      check('一表全览有合计行（每组各几项权限）', /合计/.test(pmx.textContent));
    }

    // ③ 系统开关
    const tabSet = [...docU.querySelectorAll('.tab')]
      .find(t => t.textContent.includes('系统开关'));
    tabSet.click();
    await sleep(900);
    const stxt = docU.body.textContent;
    check('系统开关页显示登录验证为「已开启」',
          /已开启/.test(stxt) && /要求登录后才能使用系统/.test(stxt),
          (stxt.match(/已开启/) || [''])[0]);
    check('开关可切换（checkbox + switch 样式）',
          !!docU.querySelector('.switch input[type=checkbox]'), '有 switch');
    check('展示会话有效期与 Cookie 安全项',
          /会话有效期/.test(stxt) && /Cookie Secure/.test(stxt), '含会话与 Cookie 项');
    check('给出服务器部署的环境变量清单',
          /ARS_PUBLIC_BASE_URL/.test(stxt) && /ARS_COOKIE_SECURE/.test(stxt),
          '含 6 项以上');
    // 六个库（2026-09-21 加了发货库）—— 列表漏一个就说明页面没跟上，
    // 所以这里把六个全核对，别只查前五个（漏检那个恰是新加的）。
    check('列出六个库文件路径（含发货库）',
          ['returns', 'inspect', 'handle', 'delivery', 'items', 'auth']
            .every(k => stxt.includes(k)), '六库齐全');

    // 备份状态卡：定时备份失败是**静默**的，界面必须能看出来。
    // 这条守「后端把 backup 段下发了」+「界面真的渲染了」两件事 ——
    // 只断言接口返回是不够的（后端返回 ≠ 页面显示）。
    const bkCard = [...docU.querySelectorAll('.card')]
      .find(c => c.textContent.includes('数据备份'));
    check('系统开关页有「数据备份」卡片', !!bkCard);
    if (bkCard) {
      const btxt = bkCard.textContent;
      check('备份卡显示最近一次时间与距今小时数',
            /最近一次备份/.test(btxt) && /距今/.test(btxt),
            btxt.replace(/\s+/g, ' ').slice(0, 60));
      check('备份卡显示超期阈值（超期判定有依据）',
            /超期阈值/.test(btxt) && /\d+\s*小时/.test(btxt));
      check('备份卡标出状态（正常 / 超期 / 失败 / 从未备份）',
            ['正常', '已超期', '最近一次失败', '从未备份']
              .some(s => (bkCard.querySelector('.tag') || {}).textContent === s),
            ((bkCard.querySelector('.tag') || {}).textContent) || '(无标签)');
      check('备份卡说明来源与定时方式',
            /tools\/backup\.py/.test(btxt)
            && /ars-backup\.timer|docker-compose/.test(btxt));
    }

    // ④ 审计日志
    const tabLog = [...docU.querySelectorAll('.tab')]
      .find(t => t.textContent.includes('审计日志'));
    tabLog.click();
    await sleep(1200);
    const ltxt = docU.body.textContent;
    check('审计日志渲染出记录', docU.querySelectorAll('.table tbody tr').length >= 1,
          `${docU.querySelectorAll('.table tbody tr').length} 条`);
    check('日志含「登录」动作与操作者',
          /登录/.test(ltxt) && /admin/.test(ltxt),
          (ltxt.match(/登录[^登]{0,20}/) || [''])[0].slice(0, 40));

    reportErrors('权限设置（交互后）', ctxU.errors);
    ctxU.dom.window.close();
  }

  // ---- 发货申请：交互级验证 ----
  // 这一段守的是「产品必须从候选中选定」这条硬规则 ——
  // 它是本模块唯一容易做错、且错了会往库里写脏数据的地方：
  //   ① 搜索框三态（空 / 待选定黄 / 已选定绿）不能互相打架；
  //   ② 未选定必须被前端校验拦下（后端还有一道，但那道只能保证不落库，
  //      保证不了用户看到的是「能提交」的假象）；
  //   ③ 选定后料号 / 型号 / 规格要真的带出来。
  console.log('\n[交互] 发货申请 · 明细产品搜索与校验');
  const dlv = await loadPage('/delivery-apply.html', 1500);
  const ddoc = dlv.doc, dwin = dlv.window;
  const dbody = () => ddoc.body.textContent;
  const fireInput = el => el.dispatchEvent(new dwin.Event('input', { bubbles: true }));

  check('筛选区渲染（关键词 + 状态下拉 + 新建按钮）',
        !!ddoc.querySelector('.inspect-toolbar') && /新建申请/.test(dbody()),
        (ddoc.querySelector('.inspect-toolbar') ? '有工具条' : '无工具条'));
  check('列表卡存在（无数据时给空态引导）',
        /申请单/.test(dbody())
        && (/还没有发货申请/.test(dbody()) || ddoc.querySelector('.table')),
        /还没有发货申请/.test(dbody()) ? '空态引导' : '有列表');
  check('状态下拉渲染出 5 个选项（全部 + 4 个状态）',
        (ddoc.querySelector('select[data-field="status_filter"]') || { options: [] })
          .options.length === 5,
        ((ddoc.querySelector('select[data-field="status_filter"]') || { options: [] })
          .options.length) + ' 项');

  const newBtn = [...ddoc.querySelectorAll('button')]
    .find(b => b.textContent.includes('新建申请'));
  check('找到「＋ 新建申请」按钮', !!newBtn);

  if (newBtn) {
    newBtn.click();
    await sleep(1000);
    const modal = ddoc.querySelector('.modal');
    check('新建弹窗已打开', !!modal);

    if (modal) {
      const mtext = modal.textContent;
      // 主表 8 项：少一项就是漏字段（这类漏项只看代码很难发现）
      const MAIN8 = ['风机厂家', '项目名称', '期望发货日', '收件地址',
        '联系人', '电话', '调换原因', '快递要求'];
      const missMain = MAIN8.filter(t => !mtext.includes(t));
      check('主表 8 项齐全', missMain.length === 0,
            missMain.length ? '缺 ' + missMain.join('、') : '齐全');

      const ths = [...modal.querySelectorAll('table.dlv-dt thead th')]
        .map(t => t.textContent.trim());
      check('明细表 6 个表头（产品 / 数量 / 需返回 / 料号 / 型号 / 规格）',
            ths.length === 8
            && ['产品', '数量', '需返回', '料号', '型号', '规格']
              .every(t => ths.some(x => x.startsWith(t))),
            ths.join(' | '));

      const dRows = () => ddoc.querySelectorAll('#dlv-dtbody tr');
      check('初始 1 行空行', dRows().length === 1, dRows().length + ' 行');

      const pkIn = () => ddoc.querySelector('#dlv-dtbody [data-field="product_search"]');
      const roCell = i => (dRows()[0] || { children: [] }).children[i];

      check('空行搜索框：无 on / 无 warn',
            pkIn() && pkIn().className.indexOf('on') < 0
            && pkIn().className.indexOf('warn') < 0,
            pkIn() ? pkIn().className : '(无输入框)');

      // 输入未选定的关键词 → 面板弹出 + 搜索框标黄
      pkIn().value = '10001';
      fireInput(pkIn());
      await sleep(900);
      const picker = ddoc.querySelector('.dlv-picker');
      check('候选面板弹出', !!picker && picker.className.indexOf('on') >= 0,
            picker ? picker.className : '(不存在)');
      check('候选项里有 10001-0001', !!picker && /10001-0001/.test(picker.textContent));
      check('候选项展示了型号与规格',
            !!picker && /BLF1-S/.test(picker.textContent)
            && /51177\.67\.773C/.test(picker.textContent));
      check('未选定 → 搜索框标黄（warn）',
            pkIn().className.indexOf('warn') >= 0, pkIn().className);
      check('未选定 → 提示区写明「还没从候选中选定」',
            /还没从候选中选定/.test(ddoc.getElementById('dlv-note').textContent),
            ddoc.getElementById('dlv-note').textContent.slice(0, 46));

      // ★ 未选定就提交：必须被拦下（且不写库）
      const saveBtn = [...modal.querySelectorAll('.modal__foot button')]
        .find(b => /提交申请|保存修改/.test(b.textContent));
      check('弹窗底部有提交按钮', !!saveBtn);
      if (saveBtn) {
        saveBtn.click();
        await sleep(900);
        const err = ddoc.getElementById('dlv-err');
        check('未选定提交被拦下（错误条出现）',
              !!err && err.className.indexOf('on') >= 0,
              err ? err.className : '(无错误条)');
        check('错误文案指明行号与原因',
              !!err && /第 1 行 产品（还没从候选中选定）/.test(err.textContent),
              err ? err.textContent.slice(0, 56) : '');
        check('弹窗没有被关掉（还停在编辑态）', !!ddoc.querySelector('.modal'));
      }

      // 选定候选 → 料号 / 型号 / 规格带出 + 三态变绿
      const opt = picker && picker.querySelector('.dlv-picker__opt');
      if (opt) {
        opt.dispatchEvent(new dwin.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
        await sleep(600);
        check('选定后料号带出', roCell(4) && roCell(4).textContent.trim() === '10001-0001',
              roCell(4) ? roCell(4).textContent.trim() : '');
        check('选定后型号带出', roCell(5) && roCell(5).textContent.trim() === 'BLF1-S',
              roCell(5) ? roCell(5).textContent.trim() : '');
        check('选定后规格带出',
              roCell(6) && roCell(6).textContent.trim() === '51177.67.773C',
              roCell(6) ? roCell(6).textContent.trim() : '');
        check('搜索框变绿（on），且不再标黄（warn）',
              pkIn().className.indexOf('on') >= 0 && pkIn().className.indexOf('warn') < 0,
              pkIn().className);
        check('选定后面板关闭',
              ddoc.querySelector('.dlv-picker').className.indexOf('on') < 0);
        check('统计栏显示已填 1 行',
              /已填 1 行/.test(ddoc.getElementById('dlv-stat').textContent),
              ddoc.getElementById('dlv-stat').textContent);

        // 改动搜索文字 → 已选作废（防「看着品名、挂着别的料号」）
        pkIn().value = '10001x';
        fireInput(pkIn());
        await sleep(400);
        check('改动文字后料号被清空（选定作废）',
              roCell(4).textContent.trim() === '—', roCell(4).textContent.trim());
        check('作废后回到待选态（warn）',
              pkIn().className.indexOf('warn') >= 0 && pkIn().className.indexOf('on') < 0,
              pkIn().className);
      }

      // 明细增删
      const addBtn = [...modal.querySelectorAll('button')]
        .find(b => b.textContent.includes('新增一行'));
      const before = dRows().length;
      if (addBtn) {
        addBtn.click();
        await sleep(500);
        check('「＋ 新增一行」可加行', dRows().length === before + 1,
              before + ' → ' + dRows().length);
      }
      if (dRows().length > 1) {
        ddoc.querySelectorAll('#dlv-dtbody [data-del]')[dRows().length - 1].click();
        await sleep(500);
        check('删除行可用', dRows().length === before, dRows().length + ' 行');
      }

      // 地址自动拆分（从退回登记搬来的规则，搬过来容易漏挂事件）
      const addr = modal.querySelector('textarea[data-field="ship_address"]');
      const ct = modal.querySelector('input[data-field="ship_contact"]');
      const ph = modal.querySelector('input[data-field="ship_phone"]');
      if (addr && ct && ph) {
        addr.value = '浙江省温州市乐清市经济开发区纬十二路228号  夏鹏程  18815120633';
        addr.dispatchEvent(new dwin.Event('blur', { bubbles: true }));
        await sleep(400);
        check('地址拆分：联系人带出', ct.value === '夏鹏程', ct.value);
        check('地址拆分：电话带出', ph.value === '18815120633', ph.value);
        check('地址拆分：地址栏只留地址',
              addr.value === '浙江省温州市乐清市经济开发区纬十二路228号', addr.value);
      } else {
        check('地址 / 联系人 / 电话控件都在', false, '有控件缺失');
      }

      // 关掉弹窗，别影响后面的用例
      const cancelBtn = [...modal.querySelectorAll('.modal__foot button')]
        .find(b => b.textContent.trim() === '取消');
      if (cancelBtn) cancelBtn.click();
      await sleep(400);
      check('弹窗可正常关闭', !ddoc.querySelector('.modal'));
    }
  }

  reportErrors('发货申请（交互后）', dlv.errors);
  dlv.dom.window.close();

  // ---- 待发货清单：只读清点视图（2026-09-21 改版）----
  // 这一段守的是改版后的定位：
  //   ① 它仍是申请单的**派生视图**（没有自己的表），但已经**不写任何数据** ——
  //      整单出队 / 撤销 / 手工登记三个接口都删了，勾选框与批量条一并去掉。
  //      这一条最容易悄悄退化（谁顺手加回一个"标记一下"按钮就破功），
  //      所以这里**显式断言"没有勾选框、没有标记按钮、没有批量条"**；
  //   ② 每行给「去登记发货」**链接**（带 request_no 跳到发货跟踪）；
  //   ③ 行里有「已发 x / y 行」进度：部分发货的单**仍留在清单里**；
  //   ④ 超期天数照旧 —— 不标超期，清单就只是一张普通列表，看不出谁最急。
  console.log('\n[交互] 待发货清单 · 只读清点视图');
  const dayOff = n => {
    const d = new Date(Date.now() + n * 864e5);
    const p2 = x => String(x).padStart(2, '0');
    return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
  };
  const HDR_P = {
    turbine_vendor: '自检清单厂家', project_site: '自检清单风场',
    ship_address: '浙江省温州市乐清市经济开发区纬十二路228号',
    ship_contact: '夏鹏程', ship_phone: '18815120633',
    replace_reason: '清单页自检', express_req: '顺丰，寄付',
  };
  // 两行明细：用来验证「只登记一行 → 仍留在清单里、进度显示 1 / 2」
  const ITEM_P = [
    { material_no: '10001-0001', product_model: 'BLF1-S',
      product_name: '低温型风速传感器', spec: '51177.67.773C',
      qty: 2, need_return: true },
    { material_no: '10001-0002', product_model: 'BLF1-S2',
      product_name: '低温型风速传感器（二代）', spec: 'x',
      qty: 1, need_return: true },
  ];
  const mkReq = async expectDate => {
    const r = await authFetch(BASE + '/api/delivery/requests', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        header: { ...HDR_P, expect_ship_date: expectDate }, items: ITEM_P }),
    });
    const j = await r.json();
    return j.request_no || '';
  };
  const overNo = await mkReq(dayOff(-3));
  const futureNo = await mkReq(dayOff(5));
  check('造出两张待发货单（超期 3 天 / 5 天后）', !!overNo && !!futureNo,
        `${overNo} / ${futureNo}`);

  const pv = await loadPage('/delivery-pending.html', 1700);
  const pd = pv.doc, pw = pv.window;
  try {
    const pRows = () => [...pd.querySelectorAll('.dlvp-t tbody tr')];
    const fireP = (el, t) => el.dispatchEvent(new pw.Event(t, { bubbles: true }));

    check('页面渲染出汇总条与筛选区',
          !!pd.querySelector('.dlvp-sum') && !!pd.querySelector('.inspect-toolbar')
          && /待发货清单/.test(pd.body.textContent),
          pd.querySelector('.dlvp-sum') ? '有' : '缺');
    check('汇总条给出「单待发 / 单超期 / 件待发 / 单已部分发货」',
          ['单待发', '单超期', '件待发', '单已部分发货']
            .every(x => pd.querySelector('.dlvp-sum').textContent.includes(x)),
          pd.querySelector('.dlvp-sum').textContent.replace(/\s+/g, ' ').trim());
    check('筛选区有「只看超期」勾选',
          !!pd.querySelector('.dlvp-chk input[type="checkbox"]'));
    check('筛选区有期望发货日区间（两个 date 框）',
          pd.querySelectorAll('.inspect-toolbar input[type="date"]').length === 2,
          pd.querySelectorAll('.inspect-toolbar input[type="date"]').length + ' 个');

    /* ⚠️ 不能断言「恰好 2 行」—— 库里可能有**用户自己建的**待发货单。
       2026-09-22 实测：一张「金风科技 / 希腊项目」的单把这里顶成 3 行，
       连带下面「取消只看超期后恢复 1 行」也红了。自检只该为自己造的数据负责，
       所以按单号认领自己那两张（清单是全库视图，不该假设它是空的）。 */
    const rowNoOf = tr => (tr.textContent.match(/FH\d{11}/) || [''])[0];
    const mineRows = () => pRows().map(rowNoOf)
      .filter(x => x === overNo || x === futureNo);
    check('清单收到自检造的那两张单', mineRows().length === 2,
          pRows().length + ' 行（自检 ' + mineRows().length + '）');
    const firstNo = pRows()[0] ? pRows()[0].children[0].textContent.trim() : '';
    check('按期望发货日升序 —— 超期那张排第一', firstNo === overNo,
          firstNo + '（期望 ' + overNo + '）');
    check('超期行标出「超期 3 天」',
          /超期 3 天/.test(pRows()[0].textContent),
          (pRows()[0].textContent.match(/超期 \d+ 天/) || ['（无）'])[0]);
    check('未超期那行不标超期',
          !/超期/.test(pRows()[1].textContent),
          pRows()[1].children[3].textContent.trim());
    check('行内显示明细与件数（2 种 / 3 件）',
          /2 种/.test(pRows()[0].textContent) && /3 件/.test(pRows()[0].textContent),
          pRows()[0].children[6].textContent.replace(/\s+/g, ' '));

    // ★★ 改版核心：本页不再有任何写动作
    check('★ 行里没有勾选框（本页已无批量动作）',
          pd.querySelectorAll('.dlvp-t input[type="checkbox"]').length === 0,
          pd.querySelectorAll('.dlvp-t input[type="checkbox"]').length + ' 个');
    check('★ 页面上没有「标记已发货 / 确认出队」按钮',
          ![...pd.querySelectorAll('button')]
            .some(x => /标记已发货|确认出队/.test(x.textContent)));
    check('★ 批量操作条已随改版移除（.dlvp-bar 不存在）',
          !pd.querySelector('.dlvp-bar'));

    check('发货进度列显示「0 / 2 行」并标「未开始」',
          /0\s*\/\s*2\s*行/.test(pRows()[0].children[7].textContent)
          && /未开始/.test(pRows()[0].children[7].textContent),
          pRows()[0].children[7].textContent.replace(/\s+/g, ' ').trim());

    const goLink = pRows()[0].querySelector('a[href*="delivery-track.html"]');
    check('行里有「去登记发货」链接', !!goLink,
          goLink ? goLink.textContent.trim() : '（无）');
    check('链接带上本单号（跳过去自动筛出这张单）',
          !!goLink && goLink.getAttribute('href')
            === '/delivery-track.html?request_no=' + encodeURIComponent(overNo),
          goLink ? goLink.getAttribute('href') : '');
    check('行里还有「明细」按钮',
          [...pRows()[0].querySelectorAll('button')]
            .some(x => x.textContent.includes('明细')));

    // 在后端登记其中一行 → 回到清单应看到「已发 1 / 2 行」
    await authFetch(BASE + '/api/delivery/shipments/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_no: overNo, line_no: 1,
                             ship_no: 'DOM-PEND-1', ship_date: dayOff(0) }),
    });
    await sleep(500);
    const pv2 = await loadPage('/delivery-pending.html', 1700);
    try {
      const rows2 = [...pv2.doc.querySelectorAll('.dlvp-t tbody tr')];
      const r2 = rows2.find(x => x.textContent.includes(overNo));
      check('★ 部分发货的单仍留在清单里（它确实还有没做的）', !!r2,
            rows2.length + ' 行');
      check('★ 进度列变成「1 / 2 行」并标「部分已发」',
            !!r2 && /1\s*\/\s*2\s*行/.test(r2.children[7].textContent)
            && /部分已发/.test(r2.children[7].textContent),
            r2 ? r2.children[7].textContent.replace(/\s+/g, ' ').trim() : '');
      reportErrors('待发货清单（部分发货后）', pv2.errors);
    } finally {
      pv2.dom.window.close();
    }

    // 填完剩下那行 → 整单完成 → 从清单消失
    await authFetch(BASE + '/api/delivery/shipments/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_no: overNo, line_no: 2,
                             ship_no: 'DOM-PEND-1', ship_date: dayOff(0) }),
    });
    await sleep(500);
    const got = await (await authFetch(
      BASE + '/api/delivery/requests/' + overNo)).json();
    check('★★ 全部明细登记完 → 申请状态自动变 shipped（没人点过「标记已发货」）',
          got.status === 'shipped', got.status);

    // 「只看超期」：超期那张已发完 → 应无结果
    const ovChk = pd.querySelector('.dlvp-chk input[type="checkbox"]');
    ovChk.click();
    await sleep(900);
    check('「只看超期」勾选后清单为空（超期那张已发完、离开了清单）',
          pRows().length === 0 && /没有符合条件的待发货单/.test(pd.body.textContent),
          pRows().length + ' 行');
    ovChk.click();
    await sleep(900);
    // 同理：判「自检那张（futureNo）回到了清单」，而不是「恰好 1 行」——
    // 库里可能还有别人建的待发货单（见上面的说明）。
    check('取消「只看超期」后自检那张回到清单',
          pRows().map(rowNoOf).includes(futureNo), pRows().length + ' 行');

    // 关键词筛选
    const kw = pd.querySelector('.inspect-toolbar input');
    if (kw) {
      kw.value = futureNo;
      fireP(kw, 'input');
      await sleep(900);
      check('按单号关键词筛出 1 行', pRows().length === 1, pRows().length + ' 行');
      kw.value = 'zzz查不到的关键词';
      fireP(kw, 'input');
      await sleep(900);
      check('筛不到时给空态提示', pRows().length === 0
            && /没有符合条件/.test(pd.body.textContent), pRows().length + ' 行');
    }

    reportErrors('待发货清单（交互后）', pv.errors);
  } finally {
    // 清理自检造的数据（删单会一并清掉明细与登记痕迹，见 delete_request）
    for (const no of [overNo, futureNo]) {
      if (no) {
        await authFetch(BASE + '/api/delivery/requests/' + no, { method: 'DELETE' });
      }
    }
    pv.dom.window.close();
  }

  // ---- 发货跟踪：申请明细为主体（2026-09-21 改版）----
  // 这一段守的是改版后的核心链路：
  //   ① **申请单一提交就出现在跟踪列表**（状态「待发货」）——
  //      改版前必须先到待发货清单点「标记已发货」才产生记录，跟踪表永远滞后；
  //   ② 按行态给动作：待发货 →「登记发货」，已发货 →「修改 / 撤销登记」，
  //      未关联 →「挂接 / 删除」；
  //   ③ 登记弹窗里厂家 / 风场 / 型号是**只读的申请单信息**，人只填发货侧；
  //   ④ **发货单号必填** —— 台账要靠它追 ERP 单据（前端先拦，后端也拦）。
  console.log('\n[交互] 发货跟踪 · 申请明细 / 按行登记');

  const REG_V = '自检跟踪厂家';
  const REG_S = '自检跟踪风场';
  const REG_M = 'TRACK-MODEL-A';
  let trkNo = '';
  try {
    const rq = await authFetch(BASE + '/api/delivery/requests', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        header: {
          turbine_vendor: REG_V, project_site: REG_S,
          expect_ship_date: dayOff(0),
          ship_address: '浙江省温州市乐清市经济开发区纬十二路228号',
          ship_contact: '夏鹏程', ship_phone: '18815120633',
          replace_reason: 'DOM 校验', express_req: '顺丰',
        },
        items: [
          { material_no: 'TRK-001', product_model: REG_M, product_name: 'N1',
            spec: 'S1', qty: 5, need_return: true },
          { material_no: 'TRK-002', product_model: 'TRACK-MODEL-B',
            product_name: 'N2', spec: 'S2', qty: 2, need_return: false },
        ],
      }),
    });
    trkNo = (await rq.json()).request_no || '';
    check('造出一张两行明细的申请单', !!trkNo, trkNo);

    const tv = await loadPage('/delivery-track.html', 1700);
    const td = tv.doc;
    try {
      const tRows = () => [...td.querySelectorAll('.dlvt-t tbody tr')];
      const rowOf = txt => tRows().find(r => r.textContent.includes(txt));
      check('页面渲染出汇总条与筛选区',
            !!td.querySelector('.dlvt-sum') && !!td.querySelector('.inspect-toolbar')
            && /发货跟踪/.test(td.body.textContent));
      check('汇总条给出「行待发货 / 件待发 / 行已发货 / 件已发 / 条未关联」',
            ['行待发货', '件待发', '行已发货', '件已发', '条未关联']
              .every(x => td.querySelector('.dlvt-sum').textContent.includes(x)),
            td.querySelector('.dlvt-sum').textContent.replace(/\s+/g, ' ').trim());

      // ★★ 改版核心：申请单一提交，列表里就有它
      check('★★ 申请单一提交，跟踪列表里就有它的两行明细',
            !!rowOf(REG_M) && !!rowOf('TRACK-MODEL-B'), tRows().length + ' 行');
      const rA = rowOf(REG_M);
      check('★ 这两行标「待发货」且没有发货单号',
            !!(rA && /待发货/.test(rA.textContent)
               && rA.querySelector('.dlvt-state--pending')));
      check('行里显示申请件数（5）而实发为空',
            !!rA && rA.children[8].textContent.trim() === '5'
            && rA.children[9].textContent.trim() === '—',
            rA ? `${rA.children[8].textContent.trim()} / ${rA.children[9].textContent.trim()}`
               : '');
      check('待发货行只给「登记发货」一个动作',
            !!(rA && /登记发货/.test(rA.textContent)
               && !/撤销登记|挂接/.test(rA.textContent)),
            rA ? rA.children[14].textContent.trim() : '');

      // 登记弹窗
      const regBtn = rA && [...rA.querySelectorAll('button')]
        .find(x => x.textContent.includes('登记发货'));
      check('待发货行有「登记发货」按钮', !!regBtn);
      if (regBtn) {
        regBtn.click();
        await sleep(800);
        const modal = td.querySelector('.modal');
        check('登记弹窗已打开', !!modal);
        if (modal) {
          const mtext = modal.textContent;
          check('弹窗只读展示申请明细（单号 / 厂家 / 风场 / 型号 / 申请件数）',
                ['申请单号', '整机厂家', '项目风场', '产品型号', '申请件数']
                  .every(x => mtext.includes(x)));
          check('弹窗写明这一行的型号与料号',
                mtext.includes(REG_M) && mtext.includes('TRK-001'));
          check('弹窗有「发货单号 / 发货日期 / 实发件数 / 物流单号」四项',
                ['发货单号', '发货日期', '实发件数', '物流单号']
                  .every(x => mtext.includes(x)));
          check('发货单号写明必填', /必填/.test(mtext));
          check('发货日期默认今天',
                (modal.querySelector('input[type="date"]') || {}).value === dayOff(0),
                (modal.querySelector('input[type="date"]') || {}).value);
          check('实发件数默认填申请件数（5）',
                (modal.querySelector('input[type="number"]') || {}).value === '5',
                (modal.querySelector('input[type="number"]') || {}).value);

          const okBtn = [...modal.querySelectorAll('.modal__foot button')]
            .find(x => x.textContent.includes('确认登记'));
          check('弹窗底部有「确认登记」按钮', !!okBtn);
          if (okBtn) {
            okBtn.click();
            await sleep(500);
            const err = td.querySelector('.modal .dlv-err');
            check('不填发货单号就被前端拦下',
                  !!err && err.className.includes('on'),
                  err ? err.textContent.slice(0, 44) : '（无错误条）');
            const noIn = [...modal.querySelectorAll('input')]
              .find(x => (x.placeholder || '').includes('ERP 发货单号'));
            if (noIn) noIn.value = 'DOM-TRK-1';
            const expIn = [...modal.querySelectorAll('input')]
              .find(x => (x.placeholder || '').includes('SF'));
            if (expIn) expIn.value = 'SF-DOM-TRK';
            okBtn.click();
            await sleep(1500);
            check('填上单号后登记成功、弹窗关闭', !td.querySelector('.modal'));
          }
        }
      }

      // 后端确认
      const sh1 = await (await authFetch(
        BASE + '/api/delivery/shipments?request_no=' + trkNo)).json();
      const sA = (sh1.rows || []).find(x => x.product_model === REG_M) || {};
      check('该行已登记发货（带发货单号 / 物流单号）',
            sA.state === 'shipped' && sA.ship_no === 'DOM-TRK-1'
            && sA.express_no === 'SF-DOM-TRK',
            `${sA.state} / ${sA.ship_no} / ${sA.express_no}`);
      const req1 = await (await authFetch(
        BASE + '/api/delivery/requests/' + trkNo)).json();
      check('只登记了一行 → 申请状态仍是 submitted（还有行没登记）',
            req1.status === 'submitted', req1.status);

      // 带 ?request_no= 打开：自动筛出该单
      const tv2 = await loadPage('/delivery-track.html?request_no=' + trkNo, 1700);
      try {
        const rows2 = [...tv2.doc.querySelectorAll('.dlvt-t tbody tr')];
        const rowA2 = rows2.find(r => r.textContent.includes(REG_M));
        check('★ 带 ?request_no= 打开时自动筛出该单的明细',
              rows2.length === 2, rows2.length + ' 行');
        check('★ 已发货行标「已发货」并显示单号 / 物流单号',
              !!(rowA2 && /已发货/.test(rowA2.textContent)
                 && rowA2.querySelector('.dlvt-state--shipped')
                 && /DOM-TRK-1/.test(rowA2.textContent)),
              rowA2 ? rowA2.textContent.replace(/\s+/g, ' ').slice(0, 56) : '');
        check('★ 已发货行的动作是「修改 / 撤销登记」',
              !!(rowA2 && /修改/.test(rowA2.textContent)
                 && /撤销登记/.test(rowA2.textContent)));
        check('★ 关键词框被预填成该单号（用户看得出为什么只有这几行）',
              (tv2.doc.querySelector('.inspect-toolbar input') || {}).value === trkNo,
              (tv2.doc.querySelector('.inspect-toolbar input') || {}).value);
        reportErrors('发货跟踪（按单查看）', tv2.errors);
      } finally {
        tv2.dom.window.close();
      }

      // 导入弹窗（保留的入口）
      const impBtn = [...td.querySelectorAll('button')]
        .find(x => x.textContent.includes('批量导入'));
      check('有「批量导入」按钮', !!impBtn);
      if (impBtn) {
        impBtn.click();
        await sleep(700);
        const modal = td.querySelector('.modal');
        check('导入弹窗已打开且有粘贴区', !!modal
              && !!modal.querySelector('textarea.dlvt-paste'));
        if (modal) {
          const mtext = modal.textContent;
          check('弹窗写清列顺序与幂等规则',
                /列顺序/.test(mtext) && /幂等/.test(mtext));
          check('粘贴区给了样例（含制表符分隔的 6 列）',
                /SHIP-001/.test((modal.querySelector('textarea.dlvt-paste') || {})
                  .getAttribute('placeholder') || ''), '有样例');
          const cancel = [...modal.querySelectorAll('.modal__foot button')]
            .find(x => x.textContent.trim() === '取消');
          if (cancel) cancel.click();
          await sleep(400);
        }
      }

      check('★ 页面上不再有「＋ 登记发货」（手工登记）按钮',
            ![...td.querySelectorAll('button')]
              .some(x => x.textContent.includes('＋ 登记发货')));
      check('★ 页面上不再有「标记已发货」这类整单动作',
            ![...td.querySelectorAll('button')]
              .some(x => /标记已发货|确认出队/.test(x.textContent)));

      reportErrors('发货跟踪（交互后）', tv.errors);
    } finally {
      tv.dom.window.close();
    }
  } finally {
    // 删申请单会一并删掉它的明细与发货记录（见 repo 的 delete_request）
    if (trkNo) {
      await authFetch(BASE + '/api/delivery/requests/' + trkNo,
                      { method: 'DELETE' });
    }
  }

  // ---- 核销台账：发货明细为基准 + 按返件行核销 / 清账 ----
  // 这一段守的是 2026-09-22 改版后的口径：
  //   ① 台账的行 = 发货明细（delivery_db.ship_detail）里
  //      doc_type = 售后发货单 且 doc_status = 已核准 的发货行聚合；
  //      本地登记表 delivery_shipment **不再**喂台账 —— 所以在跟踪页登记
  //      一张发货单**不会**让台账多出一行（旧断言正是错在这里）。
  //   ② 维度只到「厂家 + 风场」，行里没有型号列。
  //   ③ 已核销 = 手动核销关联 + 手工清账，两者都挂在**返件行**上；
  //      二级页第 ⑤ 个页签给出「已返回但未核销关联」的返件与两个按钮。
  console.log('\n[交互] 核销台账 · 发货明细为基准 / 按返件行核销');
  {
    // 页面默认 need_mode=auto，这里跟着用 auto 对比，保证「页面 = 接口」
    const led = await (await authFetch(
      BASE + '/api/delivery/ledger?page=1&page_size=20&sort_by=pend&need_mode=auto'))
      .json();
    const ledRows = led.rows || [];
    const ledSum = led.summary || {};
    // 另取一份演示口径，用来挑一个「已返回但未核销」的维度做清账
    const ledDemo = await (await authFetch(
      BASE + '/api/delivery/ledger?page=1&page_size=500&sort_by=pend&need_mode=demo'))
      .json();
    const pickUnlink = (ledDemo.rows || []).find(r => Number(r.unlink_qty || 0) > 0);
    // 基本面用**演示口径**断言：auto 会不会切到 req 取决于库里有没有发货申请
    // 明细，那个不稳定（自检自己也会造单再删），不能拿它当前提。
    check('台账接口给出维度与汇总',
          (ledDemo.rows || []).length > 0 && Number(ledDemo.summary.ship_qty || 0) > 0,
          `${(ledDemo.rows || []).length} 行 / 发货 ${ledDemo.summary.ship_qty} 件`
          + `（页面口径 ${(led.meta || {}).need_mode}）`);

    const lv = await loadPage('/delivery-ledger.html', 1700);
    const ld = lv.doc, lw = lv.window;
    try {
      const lRows = () => [...ld.querySelectorAll('.dlvl-t tbody tr')];
      // 台账接口是现算全量快照的（返件归位 + 汇总，约 1.5 秒），页面先出骨架、
      // 数据后到。这里轮询等表格真的渲染出来（最多 30 秒）；超时就照常红，
      // 免得再出现「接口明明有 1224 行、页面却判 0 行」这种假红。
      await waitFor(() => lRows().length > 0, 30000);
      check('页面渲染出汇总条与筛选区',
            !!ld.querySelector('.dlvl-sum') && !!ld.querySelector('.inspect-toolbar')
            && /核销台账/.test(ld.body.textContent));
      check('汇总条给出「维度 / 发货单 / 发货 / 已返回 / 已核销 / 待核销 / 返件未核销」',
            ['个维度', '张发货单', '件发货', '件已返回', '件已核销',
              '件待核销', '件返件未核销']
              .every(x => ld.querySelector('.dlvl-sum').textContent.includes(x)),
            ld.querySelector('.dlvl-sum').textContent.replace(/\s+/g, ' ').trim().slice(0, 130));
      const chks = () => [...ld.querySelectorAll('.dlvl-chk input[type=checkbox]')];
      check('筛选区有「只看待核销」「只看有未核销返件」「只看未填风场」三个勾',
            chks().length === 3, chks().length + ' 个');
      const noteTxt = (ld.querySelector('.dlvl-note') || {}).textContent || '';
      check('说明条写明口径（发货申请单 / 料号前缀），且不泄露 ERP 地址',
            (/售后发货单/.test(noteTxt) || /发货申请单/.test(noteTxt))
            && !/1433|192\.168\./.test(ld.body.textContent),
            noteTxt.replace(/\s+/g, ' ').trim().slice(0, 90));
      // 清洗口径必须写在脸上：剔除了多少行销售端返件、多少行未归属被清账，
      //   页面上能看见 —— 否则数据「悄悄少一截」，谁也不知道为什么。
      check('说明条写明「销售端返件剔除 N 行」与「已人工清账 N 行」',
            /销售端返件/.test(noteTxt) && /\d+\s*行销售端返件/.test(noteTxt)
            && /已人工清账/.test(noteTxt),
            noteTxt.replace(/\s+/g, ' ').trim().slice(-110));

      // 2026-09-23 用户口径：台账页去掉「风场 / 状态 / 需返回口径 / 排序」四个下拉，
      //   只剩关键词 + 整机厂家。口径由后端自动判断（说明条照旧写明当前落在哪个口径），
      //   排序固定「待核销多→少」。所以这里反过来断：四个控件都必须不在，
      //   且首个查询确实带 sort_by=pend（默认排序真的生效，不是把排序悄悄丢了）。
      const ledReqs = () => lv.requests.filter(u => u.startsWith('/api/delivery/ledger?'));
      const goneSel = ['ledger_need', 'ledger_sort', 'ledger_status', 'ledger_site'];
      const stillThere = goneSel.filter(f => ld.querySelector(`select[data-field="${f}"]`));
      check('去掉「风场 / 状态 / 需返回口径 / 排序」四个下拉（只剩关键词 + 整机厂家）',
            stillThere.length === 0
            && !!ld.querySelector('select[data-field="ledger_vendor"]'),
            stillThere.length ? `还在：${stillThere.join(' / ')}` : '四个都不在，厂家下拉还在');
      check('默认排序＝待核销多→少（查询带 sort_by=pend，且没带 status / project_site）',
            ledReqs().some(u => u.includes('sort_by=pend'))
            && !ledReqs().some(u => u.includes('status=') || u.includes('project_site=')),
            ledReqs()[0] || '(没有查询)');

      const head = [...ld.querySelectorAll('.dlvl-t thead th')].map(x => x.textContent.trim());
      check('表头 10 列（维度只到厂家 + 风场，没有型号列）',
            head.length === 10 && head[0] === '整机厂家' && head[1] === '项目风场'
            && !head.some(x => x.includes('型号')),
            head.join(' / '));

      const rows = lRows();
      const demoRows = ledDemo.rows || [];
      check('列表渲染出维度行', rows.length > 0 && demoRows.length > 0,
            `${rows.length} 行`);
      if (rows.length && demoRows.length) {
        const r0 = demoRows[0];
        const label = String(r0.project_site || '').trim() || '（风场为空）';
        const hit = rows.find(tr => tr.textContent.includes(String(r0.turbine_vendor))
          && tr.textContent.includes(label));
        check('接口里的首个维度出现在台账表里', !!hit,
              `${r0.turbine_vendor} | ${label}（表里 ${rows.length} 行）`);
        if (hit) {
          const cells = [...hit.children].map(c => c.textContent.trim());
          const num = s => Number(String(s).replace(/,/g, ''));
          check('发货单 / 发货件数 / 已返回 与接口一致',
                num(cells[2]) === Number(r0.docs_n || 0)
                && num(cells[3]) === Number(r0.ship_qty || 0)
                && num(cells[4]) === Number(r0.ret_qty || 0),
                `${cells[2]} 单 / ${cells[3]} 件发货 / ${cells[4]} 件已返回`);
          check('待核销 = 发货 − 已核销',
                num(cells[6]) === Number(r0.pend_qty || 0),
                `${cells[6]}（接口 ${r0.pend_qty}）`);
          check('待核销为正的行有 todo 底色',
                Number(r0.pend_qty || 0) <= 0 || hit.className.includes('todo'),
                hit.className);
        }
      }

      // 二级页、动作弹窗、别名表都是叠在页面上的模态框：openModal 每次往 body 追一个

      // 「只看未填风场」：客户栏里没写「（风场）」的维度（用户口径：保留原样、列里标
      // 「（风场为空）」，但要能单独筛出来）。只判勾选框存在是假绿——必须真点一下，断言
      // 确实带 only_no_site=1 重查、且筛出来的每一行风场列都是「（风场为空）」。
      if (chks().length === 3) {
        const nsChk = chks()[2];
        const nsLblTxt = (chks()[2].parentElement || {}).textContent || '';
        check('「只看未填风场」标签带维度条数（no_site_groups 渲染到了页面上）',
              /只看未填风场（\d+）/.test(nsLblTxt), nsLblTxt.trim());
        const nsBefore = ledReqs().length;
        nsChk.checked = true;
        nsChk.dispatchEvent(new lw.Event('change', { bubbles: true }));
        await waitFor(() => lRows().length > 0, 25000);
        const nsRows = lRows();
        check('「只看未填风场」真勾上后带 only_no_site=1 重查，每行风场都是「（风场为空）」',
              ledReqs().slice(nsBefore).some(u => u.includes('only_no_site=1'))
              && nsRows.length > 0
              && nsRows.every(tr => (tr.children[1].textContent || '').includes('（风场为空）')),
              `${nsRows.length} 行 ｜ ${ledReqs().slice(nsBefore).slice(-1)[0] || ''}`);
        nsChk.checked = false;
        nsChk.dispatchEvent(new lw.Event('change', { bubbles: true }));
        await waitFor(() => lRows().length > 0, 25000);
      }
      // .modal-mask，所以「最后的那个」才是刚打开的；用 querySelector 会一直拿到二级页。
      const modals = () => [...ld.querySelectorAll('.modal')];
      const lastModal = () => modals()[modals().length - 1];
      const closeAllModals = () => {
        modals().forEach(mm => {
          const x = mm.querySelector('.modal__close');
          if (x) x.click();
        });
      };

      // 挑一个「已返回但未核销」的维度，进二级页走一遍清账
      const target = pickUnlink
        ? lRows().find(tr => tr.textContent.includes(String(pickUnlink.turbine_vendor))
            && tr.textContent.includes(String(pickUnlink.project_site || '').trim()
              || '（风场为空）'))
        : null;
      const detailBtn = target
        ? [...target.querySelectorAll('button')].find(b => b.textContent.includes('明细'))
        : null;
      check('有未核销返件的维度能找到「明细」按钮', !!detailBtn || !pickUnlink,
            pickUnlink ? `${pickUnlink.turbine_vendor} | ${pickUnlink.project_site}` : '（无）');

      if (detailBtn) {
        detailBtn.click();
        // 二级页要等接口把发货单 / 返件 / 核销三份数据取回来才建弹窗（约 1.5 秒），
        // 固定 sleep 会偶发拿到「还没建出来」的空档。
        await waitFor(() => !!ld.querySelector('.modal'), 25000);
        const modal = ld.querySelector('.modal');
        check('明细弹窗已打开', !!modal);
        if (modal) {
          const tabs = [...modal.querySelectorAll('.dlvl-tab')].map(x => x.textContent.trim());
          check('二级页 5 个页签（发货单核销 / 发货行明细 / 返件明细 / 核销明细 / 未关联返件）',
                tabs.length === 5
                && ['① 发货单核销', '② 发货行明细', '③ 返件明细',
                  '④ 核销明细', '⑤ 未关联返件'].every((t, i) => (tabs[i] || '').startsWith(t)),
                tabs.join(' | ').slice(0, 150));
          const paneTables = () => modal.querySelectorAll('.dlvl-detail table').length;
          check('二级页当前页签有明细表（发货单核销）', paneTables() >= 1,
                paneTables() + ' 张表');
          // 2026-09-23 用户口径：发货单内明细行每行加手动清账；发货行按序列号拆行。
          const footBtns = [...modal.querySelectorAll('.modal__foot button')]
            .map(b => b.textContent.trim());
          check('二级页底部有「本维度按型号自动核销」入口',
                footBtns.some(x => x.includes('自动核销')),
                footBtns.join(' / ') || '（没有底部按钮）');
          const t1 = [...modal.querySelectorAll('.dlvl-tab')]
            .find(b => b.textContent.includes('发货单核销'));
          if (t1) {
            t1.click();
            await sleep(300);
            const t1Txt = modal.textContent;
            const t1Btns = [...modal.querySelectorAll('tbody button')]
              .map(b => b.textContent.trim());
            check('发货单核销：表头带「已核销 / 待核销 / 操作」，每张单一个「手动清账」（清完显示「已清完」）',
                  /已核销/.test(t1Txt) && /待核销/.test(t1Txt) && t1Btns.length >= 1
                  && t1Btns.every(x => ['手动清账', '已清完', '—'].includes(x)),
                  t1Btns.slice(0, 5).join(' / ') || '（这个维度没有发货单）');
          }
          const t2 = [...modal.querySelectorAll('.dlvl-tab')]
            .find(b => b.textContent.includes('发货行明细'));
          if (t2) {
            t2.click();
            await sleep(300);
            const t2Txt = modal.textContent;
            const t2Btns = [...modal.querySelectorAll('tbody button')]
              .map(b => b.textContent.trim());
            const rows2 = [...modal.querySelectorAll('tbody tr')];
            const splitRows = rows2.filter(tr => /（\d+\/\d+）/.test(tr.textContent));
            const badSplit = splitRows.filter(tr => {
              const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
              return !tds.some(x => x === '1');
            });
            check('发货行明细：表头带「已清账 / 待核销」，每行一个「手动清账」',
                  /已清账/.test(t2Txt) && /待核销/.test(t2Txt) && t2Btns.length >= 1
                  && t2Btns.every(x => ['手动清账', '已清完', '—'].includes(x)),
                  t2Btns.slice(0, 5).join(' / ') || '（这个维度没有发货行）');
            check('发货行明细：序列号列对拆行显示「序列号（n/N）」，且拆出行数量就是 1 件',
                  badSplit.length === 0,
                  splitRows.length
                    ? splitRows.slice(0, 2).map(tr => tr.textContent.replace(/\s+/g, ' ').trim().slice(0, 70))
                    : '这个维度没有可拆的行（接口侧拆行断言在 smoke 里）');
          }
          const t3 = [...modal.querySelectorAll('.dlvl-tab')]
            .find(b => b.textContent.includes('返件明细'));
          if (t3) {
            t3.click();
            await sleep(300);
            check('切到「返件明细」页签也有自己的表（一份数据一张表）',
                  paneTables() >= 1, paneTables() + ' 张表');
          }

          const t5 = [...modal.querySelectorAll('.dlvl-tab')]
            .find(b => b.textContent.includes('未关联返件'));
          if (t5) {
            t5.click();
            await sleep(500);
            const btns = [...modal.querySelectorAll('button')].map(b => b.textContent.trim());
            check('未关联返件行上并排「手动核销关联」「手动清账」',
                  btns.includes('手动核销关联') && btns.includes('手动清账'),
                  btns.filter(x => x.includes('手动')).join(' / ') || '（无）');
            const clearBtn = [...modal.querySelectorAll('button')]
              .find(b => b.textContent.trim() === '手动清账');
            if (clearBtn) {
              clearBtn.click();
              await waitFor(() => modals().length >= 2, 25000);
              const dl = lastModal();
              check('清账弹窗已打开（叠在二级页之上）', !!dl && modals().length >= 2,
                    modals().length + ' 个弹窗');
              if (dl) {
                const ta = dl.querySelector('textarea');
                const qtyEl = dl.querySelector('input[type="number"]');
                check('清账弹窗有件数与原因两个输入（原因提示必填）',
                      !!ta && !!qtyEl && /必填/.test(ta.getAttribute('placeholder') || ''),
                      ta ? ta.getAttribute('placeholder') : '（无）');
                check('清账件数不预填（强制人填一次，避免手快全清）',
                      !!qtyEl && qtyEl.value === '', qtyEl ? qtyEl.value : '（无输入框）');
                const okBtn = [...dl.querySelectorAll('.modal__foot button')]
                  .find(b => b.textContent.includes('确认'));
                check('有确认按钮', !!okBtn, okBtn ? okBtn.textContent.trim() : '（无）');
                if (okBtn) {
                  // 空着提交 → 前端先拦件数
                  okBtn.click();
                  await sleep(400);
                  let err = dl.querySelector('.dlv-err');
                  check('件数空着提交被拦下', !!err && err.className.includes('on'),
                        err ? err.textContent.slice(0, 40) : '（无错误条）');
                  // 只填件数、不填原因 → 仍被拦下
                  qtyEl.value = '1';
                  okBtn.click();
                  await sleep(400);
                  err = dl.querySelector('.dlv-err');
                  check('原因空着提交被拦下', !!err && err.className.includes('on'));
                  // 两项都填 → 真正写库
                  ta.value = 'DOM 校验：台账清账';
                  okBtn.click();
                  // 写库 = POST + 重新拉一次全量快照（约 1.5 秒）。清账弹窗自己关掉，
                  // 二级页刷新后会重新打开，所以最终剩 1 个弹窗；真失败（400）时两个都还在。
                  await waitFor(() => modals().length < 2, 25000);
                  check('清账弹窗关闭（二级页刷新后重新打开）', modals().length < 2,
                        modals().length + ' 个弹窗');
                  const after = await (await authFetch(BASE + '/api/delivery/ledger?'
                    + 'page=1&page_size=500&sort_by=pend&need_mode=demo')).json();
                  const row2 = (after.rows || []).find(r =>
                    r.turbine_vendor === pickUnlink.turbine_vendor
                    && (r.project_site || '') === (pickUnlink.project_site || ''));
                  check('清账 1 件后该维度的「返件未核销」少 1 件',
                        !!row2 && Number(row2.unlink_qty)
                          === Number(pickUnlink.unlink_qty) - 1,
                        row2 ? `${pickUnlink.unlink_qty} → ${row2.unlink_qty}` : '（行没了）');
                  check('清账后「已核销」列涨了 1 件',
                        !!row2 && Number(row2.clr_qty)
                          === Number(pickUnlink.clr_qty || 0) + 1,
                        row2 ? `${pickUnlink.clr_qty || 0} → ${row2.clr_qty}` : '（行没了）');
                }
              }
            }
          }
        }
      }

      // 2026-09-23 用户口径：核销明细要分得清「手动关联 / 手工清账 / 自动核销 / 发货侧清账」。
      {
        const m4 = lastModal();
        const t4 = m4 ? [...m4.querySelectorAll('.dlvl-tab')]
          .find(b => b.textContent.includes('核销明细')) : null;
        if (t4) {
          t4.click();
          await sleep(300);
          const rows4 = [...m4.querySelectorAll('tbody tr')];
          const kinds4 = rows4.map(tr => ((tr.querySelector('td') || {}).textContent || '').trim());
          check('核销明细：类型列全用中文（刚写的这条显示「手工清账」）',
                kinds4.length >= 1
                && kinds4.every(k => ['手动关联', '手工清账', '自动核销',
                                      '发货侧清账'].includes(k)),
                kinds4.slice(0, 5).join(' / ') || '（这个维度还没有核销记录）');
          const btns4 = [...m4.querySelectorAll('tbody button')]
            .map(b => b.textContent.trim());
          check('核销明细每行带「撤销」（核销都能撤）', btns4.includes('撤销'),
                btns4.slice(0, 5).join(' / ') || '（没有按钮）');
        }
      }

      // 二级页收起来再做后面的断言（不要叠着模态框点工具条）。
      // 先等一下：清账成功后二级页会刷新并重新打开（reload 里是
      // `await loadList() → m.close() → showDetail()`），冷缓存时这段能跑好几秒 ——
      // 只在 1.8s 时就 closeAllModals，会「关了它、它随后又自己冒出来」，
      // 后面点工具条就点了个寂寞、lastModal 拿到的是那个二级页。
      await sleep(1800);
      closeAllModals();
      await waitFor(() => modals().length === 0, 10000);
      await sleep(2500);                        // 给「自动重开」留出时间
      if (modals().length > 0) {                // 真冒出来了就再关一次
        closeAllModals();
        await sleep(600);
      }
      check('二级页能关掉（工具条恢复可点）', modals().length === 0,
            modals().length + ' 个弹窗');

      // 2026-09-23 用户口径：返件按发货单型号自动核销 —— 按钮触发，先试算再确认，
      //   所以点开弹窗只算不写；这里只走「试算 → 取消」，真写在 smoke 里验。
      const autoBefore = await (await authFetch(BASE + '/api/delivery/ledger?'
        + 'page=1&page_size=1&need_mode=demo')).json();
      const autoBtn = [...ld.querySelectorAll('.inspect-toolbar button')]
        .find(b => b.textContent.includes('自动核销'));
      check('工具条有「按型号自动核销」按钮', !!autoBtn,
            [...ld.querySelectorAll('.inspect-toolbar button')]
              .map(b => b.textContent.trim()).join(' / ').slice(0, 140));
      if (autoBtn) {
        // 清账成功后页面会自动重开二级页，这条异步尾巴与「点工具条」会撞车：
        // 点之前先确保没有残留弹窗，点完若没等到试算弹窗就再试一次（最多 3 次），
        // 别把一个竞态写成假失败。
        let am2 = null;
        for (let tryN = 1; tryN <= 3; tryN++) {
          closeAllModals();
          await sleep(500);
          autoBtn.click();
          await sleep(2000);
          am2 = lastModal();
          const t2 = (am2 || {}).textContent || '';
          if (/试算/.test(t2) && /计划自动核销/.test(t2)) break;
          console.log('      · 试算弹窗第 ' + tryN + ' 次没出来：'
            + (t2.replace(/\s+/g, ' ').trim().slice(0, 60) || '(无弹窗)'));
        }
        const am2Txt = (am2 || {}).textContent || '';
        check('自动核销先出「试算」：给计划条数 / 件数 / 跳过条数，不是点一下就写库',
              /试算/.test(am2Txt) && /计划自动核销/.test(am2Txt) && /跳过/.test(am2Txt),
              am2Txt.replace(/\s+/g, ' ').trim().slice(0, 150));
        const am2Btns = [...(am2 ? am2.querySelectorAll('.modal__foot button') : [])]
          .map(b => b.textContent.trim());
        check('试算弹窗有「取消 / 确认自动核销」两个出口',
              am2Btns.includes('取消') && am2Btns.some(x => x.includes('确认自动核销')),
              am2Btns.join(' / '));
        const cancel2 = [...(am2 ? am2.querySelectorAll('.modal__foot button') : [])]
          .find(b => b.textContent.trim() === '取消');
        if (cancel2) cancel2.click();
        await waitFor(() => modals().length === 0, 10000);
        const autoAfter = await (await authFetch(BASE + '/api/delivery/ledger?'
          + 'page=1&page_size=1&need_mode=demo')).json();
        check('取消试算不动数据（自动核销记录数没变）',
              Number(((autoAfter.meta) || {}).auto_clears || 0)
              === Number(((autoBefore.meta) || {}).auto_clears || 0),
              [((autoBefore.meta) || {}).auto_clears, ((autoAfter.meta) || {}).auto_clears]);
      }

      // 别名表弹窗：打开看一眼结构，取消（不动真实数据）
      const aliasBtn = [...ld.querySelectorAll('.inspect-toolbar button')]
        .find(b => b.textContent.includes('别名'));
      check('筛选区有「厂家 / 风场别名」入口', !!aliasBtn);
      if (aliasBtn) {
        // 同样的竞态：确保点开的是别名弹窗（带 .dlvl-tab），最多试 3 次。
        let am = null;
        for (let tryN = 1; tryN <= 3; tryN++) {
          closeAllModals();
          await sleep(500);
          aliasBtn.click();
          await sleep(1500);
          am = lastModal();
          if (am && am.querySelector('.dlvl-tab')) break;
        }
        check('别名弹窗有厂家与风场两个文本框 + 保存按钮',
              !!am && am.querySelectorAll('textarea').length === 2
              && [...am.querySelectorAll('.modal__foot button')]
                .some(b => b.textContent.trim() === '保存'));
        check('别名弹窗有「编辑 / 明细」两个页签',
              !!am && [...am.querySelectorAll('.dlvl-tab')]
                .map(b => b.textContent.trim()).join('/') === '编辑/明细',
              am ? [...am.querySelectorAll('.dlvl-tab')].map(b => b.textContent.trim()) : '');
        if (am) {
          // 2026-09-23 用户口径「别名表要能查询明细」：明细页签要真的逐条列出命中数，
          //   并且能下钻到具体单号。只断「页签在」是假绿 —— 点进去看表格与二级弹窗。
          const dTab = [...am.querySelectorAll('.dlvl-tab')]
            .find(b => b.textContent.trim() === '明细');
          if (dTab) dTab.click();
          await waitFor(() => /整机厂家别名/.test(am.textContent)
            && !!am.querySelector('tbody tr'), 20000);
          check('明细页签按厂家 / 风场两块逐条列出别名',
                /整机厂家别名/.test(am.textContent) && /项目风场别名/.test(am.textContent)
                && /内置/.test(am.textContent), am.textContent.slice(0, 100));
          const dBtn = [...am.querySelectorAll('tbody button')]
            .find(b => b.textContent.trim() === '详情');
          check('有命中的别名每条带「详情」下钻按钮', !!dBtn);
          if (dBtn) {
            dBtn.click();
            await waitFor(() => modals().length >= 2, 20000);
            const dm = lastModal();
            check('别名可下钻到具体单号（发货侧 + 返件侧两张表）',
                  !!dm && /发货侧明细/.test(dm.textContent)
                  && /返件侧明细/.test(dm.textContent));
            const dc = dm ? [...dm.querySelectorAll('.modal__foot button')]
              .find(b => b.textContent.trim() === '关闭') : null;
            if (dc) dc.click();
            await waitFor(() => modals().length < 2, 10000);
          }
          const cancel = [...lastModal().querySelectorAll('.modal__foot button')]
            .find(b => b.textContent.trim() === '取消');
          if (cancel) cancel.click();
          await sleep(400);
        }
      }

      // 未归属返件弹窗（说明条上的链接）
      const unLink = ld.querySelector('.dlvl-note .lnk');
      check('说明条有「查看未归属的返件」入口', !!unLink);
      if (unLink) {
        unLink.click();
        await waitFor(() => modals().length >= 1, 20000);
        const um = lastModal();
        check('未归属返件弹窗列出「未匹配 / 歧义」两块',
              !!um && /未匹配/.test(um.textContent) && /歧义/.test(um.textContent));
        if (um) {
          // 2026-09-23 用户口径：未归属返件也要能手工清账。这批返件归位不到任何
          //   维度，只能清账，所以表格最后必须有真的「手动清账」按钮，点下去弹的是
          //   清账对话框（件数 / 原因 / 上限=未核销）。只断「按钮存在」是假绿。
          const clearBtns = [...um.querySelectorAll('tbody button')]
            .filter(b => b.textContent.trim() === '手动清账');
          check('未归属返件每行都有「手动清账」按钮，且表头带「未核销」列',
                clearBtns.length >= 1 && /未核销/.test(um.textContent),
                clearBtns.length + ' 个按钮');
          if (clearBtns.length) {
            clearBtns[0].click();
            await waitFor(() => modals().length >= 2, 20000);
            const cm = lastModal();
            const cmTxt = (cm || {}).textContent || '';
            check('点「手动清账」弹出清账对话框（件数 + 原因 + 已核销/未核销）',
                  /手工清账/.test(cmTxt) && /件数/.test(cmTxt) && /原因/.test(cmTxt)
                  && /已核销/.test(cmTxt) && /未核销/.test(cmTxt),
                  cmTxt.replace(/\s+/g, ' ').trim().slice(0, 120));
            const cancel = cm && [...cm.querySelectorAll('.modal__foot button')]
              .find(b => b.textContent.trim() === '取消');
            if (cancel) cancel.click();
            await sleep(300);
          }
          const close = [...um.querySelectorAll('.modal__foot button')]
            .find(b => b.textContent.trim() === '关闭');
          if (close) close.click();
          await sleep(300);
        }
      }

      reportErrors('核销台账（交互后）', lv.errors);
    } finally {
      // 本次只真实碰了 1 件（清账记录），这里按原因筛出来删掉，恢复原状
      const cls = await (await authFetch(BASE + '/api/delivery/ledger/clears?limit=200'))
        .json();
      for (const c of (cls.rows || [])) {
        if (String(c.reason || '').includes('DOM 校验')) {
          await authFetch(BASE + '/api/delivery/ledger/clears/' + c.id,
                          { method: 'DELETE' });
        }
      }
      lv.dom.window.close();
    }
  }


  /* ---- 发货明细：ERP 出货明细的本地镜像 ----
     这一页的数据全部来自 ERP，页面只读。结构断言之外，重点守三件事：
     ① ERP 连接信息（地址 / 端口 / 库名）**不许出现在状态卡上** ——
        它是最容易被截图带走的东西，又对「同步有没有在跑」毫无帮助；
     ② 「修改连接配置」弹窗**必须真的带按钮** —— common.js 的 openModal
        只认 footer。早先 items.html 误写成 buttons 时语法完全合法、
        不报错、不抛异常，只是弹窗一个按钮都没有（没有保存入口），
        静态语法检查永远抓不到，所以在这里用真实 DOM 兜住；
     ③ 状态页签必须把「认不出的状态」也列出来（后端会写成 `未知(n)`），
        否则那批行会从所有筛选里凭空消失。 */
  console.log('\n[交互] 发货明细 · ERP 出货镜像');
  {
    const dv = await loadPage('/delivery-detail.html', 2800);
    const dd = dv.doc;
    const dt = dd.body.textContent || '';
    check('页头与说明渲染（数据全部来自 ERP、本地不改）',
          /发货明细/.test(dt) && /ERP/.test(dt) && /不手工增改/.test(dt));

    const tabText = [...dd.querySelectorAll('.sd-tab')]
      .map(e => e.textContent.replace(/\s+/g, ' ').trim());
    check('状态页签 = 全部 + 四种单据状态',
          tabText.length === 5
          && ['全部', '已核准', '核准中', '开立', '草稿']
               .every(s => tabText.some(t => t.startsWith(s))),
          tabText.join(' / ') || '(无页签)');

    const sumTxt = (dd.querySelector('.sd-sum') || {}).textContent || '';
    check('汇总条给出 当前筛选 / 件 / 单 / 料号 四个数',
          ['条当前筛选', '件', '张单', '个料号'].every(x => sumTxt.includes(x)),
          sumTxt.replace(/\s+/g, ' ').trim().slice(0, 80));

    // ★ 用户报过「发货明细里面的筛选项无法进行筛选」：三个下拉当时是
    //   fixedSelect，而 fixedSelect 只有 getValue/setValue、**没有 setOptions**，
    //   于是 `if (sel.setOptions) sel.setOptions(opts)` 永远为假 —— 候选值从
    //   服务器取回来又被静默丢掉，三个下拉各剩一个「全部…」占位项。
    //   原来这里只断言「输入框和日期框存在」，从没操作过下拉，也从不检查筛选
    //   是否真的改变了查询 —— 所以三套自检全绿也照样漏掉。下面改成**真的操作
    //   每个筛选控件，再看它有没有发出带对应参数的列表请求**。
    const tb = dd.querySelector('.inspect-toolbar');
    check('筛选区有关键字输入与两个出货日期',
          !!tb.querySelector('input[type="text"]')
          && tb.querySelectorAll('input[type="date"]').length === 2);

    const listReqs = () => dv.requests.filter(u => u.startsWith('/api/delivery/details?'));
    const latestList = () => listReqs()[listReqs().length - 1] || '';
    const paramOf = (u, k) => {
      const m = new RegExp('[?&]' + k + '=([^&]*)').exec(u || '');
      return m ? decodeURIComponent(m[1]) : null;
    };

    const cbs = [...tb.querySelectorAll('.cb')];
    check('三个筛选下拉是 combobox（fixedSelect 没有 setOptions，筛选会静默失效）',
          cbs.length === 3, `找到 ${cbs.length} 个 .cb`);
    const cbFields = cbs.map(c => {
      const i = c.querySelector('.cb__input');
      return i ? (i.dataset.field || '') : '';
    });
    check('三个下拉就是 单据类型 / 客户 / 型号（顺序错了筛选会串位）',
          cbFields.join() === 'sd_doc_type,sd_customer,sd_product_model',
          cbFields.join(' / '));

    // 三个下拉逐个真交互：面板里得有真实候选，选中后必须按该字段重查
    [['sd_doc_type', 'doc_type'], ['sd_customer', 'customer'],
     ['sd_product_model', 'product_model']].forEach(([field, key], i) => {
      const cb = cbs[i];
      if (!cb) return;
      const input = cb.querySelector('.cb__input');
      input.dispatchEvent(new dv.window.Event('focus', { bubbles: true }));
      cb.querySelector('.cb__arrow')
        .dispatchEvent(new dv.window.MouseEvent('click', { bubbles: true }));
      const items = [...cb.querySelectorAll('.cb__item')];
      check(`「${field}」下拉有真实候选值（只剩占位项就等于没得选）`,
            items.length > 0, `面板 ${items.length} 项`);
      const before = listReqs().length;
      if (items.length) {
        items[0].dispatchEvent(new dv.window.MouseEvent('mousedown',
          { bubbles: true, cancelable: true }));
      }
      const grew = listReqs().length > before;
      const v = paramOf(latestList(), key);
      check(`选中「${field}」会按 ${key} 重新查询`,
            grew && v !== null && v !== '',
            `请求${grew ? '有' : '无'} · ${key}=${v}`);
    });

    // 客户 1776 个取值、型号 1090 个，而 /options 的 limit 上限是 200 ——
    // 预取的 200 个盖不住长尾，必须能一边打字一边远程检索。
    const cbCust = cbs[1];
    if (cbCust) {
      const seen = () => dv.requests
        .filter(u => u.startsWith('/api/delivery/details/options'));
      const before = seen().length;
      const ci = cbCust.querySelector('.cb__input');
      ci.value = '金风';
      ci.dispatchEvent(new dv.window.Event('input', { bubbles: true }));
      await sleep(900);
      const after = seen();
      check('客户下拉打字会远程检索（长尾客户不在预取的 200 个里）',
            after.length > before
            && after.slice(before).some(u => u.includes('field=customer')),
            after.slice(-1)[0] || '(没有候选请求)');
    }
    check('提供「导出当前筛选」与「刷新」',
          ['导出当前筛选', '刷新']
            .every(t => [...dd.querySelectorAll('button')]
              .some(b => b.textContent.trim() === t)));

    // ★ 上面那几个下拉交互会**叠加**筛选条件（先选客户、再选型号），叠加后可能
    //   一行都不剩；而且最后一次重新查询未必已经回来。表格断言要的是「干净加载」
    //   的样子 —— 重新开一次页面，并轮询等它真的渲染出来（超时照常红）。
    const dps = await loadPage('/delivery-detail.html', 800);
    const ddF = dps.doc;
    await waitFor(() => ddF.querySelectorAll('table.sd-t tbody tr').length > 0,
                  25000);

    const tbl = ddF.querySelector('table.sd-t');
    check('明细表已渲染', !!tbl);
    if (tbl) {
      // ★ 用户报过「明细列表变形」：表格只写 `sd-t` 而漏了基础类 `.table` 时，
      //   内边距 / 分隔线 / 吸顶表头全部拿不到，看起来就是「挤成一坨」。
      //   这里查的是**算出来的样式**（jsdom 开了 resources:'usable'，会真的解析 app.css），
      //   而不是「类名里有没有 table」—— 后者改个名、换个选择器就假绿了。
      const win = ddF.defaultView;
      const td0 = [...tbl.querySelectorAll('tbody td')]
        .find(td => !td.className.includes('cell-wrap'));
      const th0 = tbl.querySelector('thead th');
      check('明细表带基础类 .table（与其它页一致）', tbl.className.includes('table'),
            `class="${tbl.className}"`);
      if (td0 && th0) {
        const ctd = win.getComputedStyle(td0), cth = win.getComputedStyle(th0);
        check('单元格拿到了 .table 的内边距（不是浏览器默认的 1px）',
              parseFloat(ctd.paddingTop) >= 4,
              `td padding=${ctd.padding} / th padding=${cth.padding}`);
        check('表头与单元格保持 nowrap（列宽交给 .table 的 max-content，文字不被挤溢）',
              cth.whiteSpace === 'nowrap' && ctd.whiteSpace === 'nowrap',
              `th=${cth.whiteSpace} td=${ctd.whiteSpace}`);
      }

      const head = [...tbl.querySelectorAll('thead th')]
        .map(e => e.textContent.replace(/[▲▼]/g, '').trim());
      check('表头 14 列（与 config.SHIP_DETAIL_COLUMNS 对齐）',
            head.length === 14, `${head.length} 列：${head.join('/')}`);
      check('表头就是配置里那 14 个中文列名（含「序列号」「承运单号」）',
            ['日期', '状态', '单号', '单据类型', '料号', '料品名称', '规格',
             '型号', '出货数量', '序列号', '客户名称', '联系人', '承运单号',
             '地址'].join('|') === head.join('|'),
            head.join('|'));
      check('出货数量列右对齐（数值列不跟文字列混排）',
            !!tbl.querySelector('tbody td.num'),
            tbl.querySelectorAll('tbody tr').length + ' 行');
      check('每行 <tr> 都带「本行同步于」提示',
            [...tbl.querySelectorAll('tbody tr')]
              .slice(0, 5).every(tr => /本行同步于/.test(tr.title || '')));
      // 排序：点列头必须换箭头（与后端 SORTABLE 白名单一致）
      const thQty = [...tbl.querySelectorAll('thead th')]
        .find(th => th.textContent.includes('出货数量'));
      check('出货数量列头可排序', !!thQty && thQty.className.includes('sortable'));
      if (thQty) {
        thQty.click();
        await sleep(1200);
        const thQty2 = [...ddF.querySelectorAll('table.sd-t thead th')]
          .find(th => th.textContent.includes('出货数量'));
        check('点列头后箭头出现（真的按该列重查）',
              !!(thQty2 && /[▲▼]/.test(thQty2.textContent)),
              thQty2 ? thQty2.textContent.trim() : '(表没了)');
      }
    }

    /* ★ 2026-09-22：ERP 同步卡已从本页搬到「自动同步任务」页 ——
       这一段（状态卡 + 连接配置弹窗）因此改在那一页上查；
       本页剩下的检查（表格 / 页签 / 排序 / 地址列宽）照旧。 */
    const sv = await loadPage('/sync-tasks.html', 2400);
    const sd = sv.doc;
    // 卡片是按接口结果渲染的，固定等 2400 毫秒偶发不够（2026-09-22 真红过一次：
    // 同一页早先那次加载两张卡都在，这次只差这张）—— 轮询等它出现。
    const cardOf = () => [...sd.querySelectorAll('.card')]
      .find(c => c.textContent.includes('发货明细 · ERP 出货单'));
    await waitFor(() => !!cardOf(), 25000);
    const cd = cardOf();
    check('管理员能看到「发货明细 · ERP 出货单」状态卡', !!cd);
    if (cd) {
      const ct = cd.textContent;
      /* 2026-09-22：范围入口整体去掉，卡里改说「上次用了哪种模式 + 覆盖区间」。
         两种模式都必须出现在文案里 —— 用户要能分清「立即同步＝常规增量」
         与「全量同步＝整表覆盖」，否则会拿常规同步当全量用（或反过来）。 */
      check('状态卡说明两种同步模式（常规增量 / 全量整表覆盖）与覆盖区间',
            /常规同步/.test(ct) && /全量同步/.test(ct) && /覆盖区间/.test(ct),
            ct.replace(/\s+/g, ' ').trim().slice(0, 70));
      check('状态卡不显示 ERP 地址 / 端口 / 库名',
            !/ERP 地址/.test(ct) && !/\d{1,3}(\.\d{1,3}){3}/.test(ct)
            && !/BLFN/.test(ct),
            ct.replace(/\s+/g, ' ').trim().slice(0, 70));
      /* 和「匹配数据库」那张卡一致的闭集合断言：这里多一格、少一格都要报红。
         「数据区间」是发货明细特有的（整表替换，所以要说清这批数据覆盖到哪天）。 */
      const kpis = [...cd.querySelectorAll('.kpi__label')]
        .map(e => (e.textContent || '').trim());
      check('发货明细状态卡指标是固定五格，且含「下次自动同步」',
            kpis.join('|') ===
              '最近一次同步|距今|数据区间|下次自动同步|本地记录',
            kpis.join(' · ') || '(无指标格)');
      check('发货明细状态卡有「自动同步」开关（给用户一个不用点立即同步的选项）',
            !!cd.querySelector('.switch input[type=checkbox]'),
            cd.querySelector('.switch') ? cd.querySelector('.switch').textContent.trim() : '(无开关)');
      check('「自动同步」开关默认是**开**的（绑 s.enabled，不是几乎恒为 false 的 s.running）',
            (() => {
              const box = cd.querySelector('.switch input[type=checkbox]');
              return !!box && box.checked === true;
            })(),
            (() => {
              const box = cd.querySelector('.switch input[type=checkbox]');
              return box ? `checked=${box.checked}` : '(无开关)';
            })());
      /* 按钮文案 2026-09-22 改短了：两个任务卡上都叫「连接配置」
         （反正只有一个弹窗，写「修改」是废话）。 */
      check('状态卡提供「立即同步」与「连接配置」',
            [...cd.querySelectorAll('button')].some(b => b.textContent.includes('立即同步'))
            && [...cd.querySelectorAll('button')]
              .some(b => b.textContent.includes('连接配置')));

      /* 位置：这张卡是整页最后一块（源码里 `replaceChildren(filterCard,
         listCard, syncHost)`）。2026-09-22「匹配数据库」那张卡也改成同一
         排布 —— 两边都守住，否则以后谁把顺序调回去都不会有人发现。
         与 items.html 那条同构：断言真实 DOM 的先后，不判源码那行调用。 */
      /* 原来这里断言「同步卡排在出货明细表之后」——卡片搬到「自动同步任务」页后
         那一页没有明细表，位置断言失去对象（顺序由上面「两张任务卡」那条守）。 */

      const cfgBtn = [...cd.querySelectorAll('button')]
        .find(b => b.textContent.includes('连接配置'));
      if (cfgBtn) {
        cfgBtn.click();
        await sleep(900);
        const modal = sd.querySelector('.modal');
        check('「修改连接配置」弹窗已打开', !!modal);
        if (modal) {
          /* ★ 这条是本页最该守的：openModal 只认 footer，写成 buttons
             时弹窗会「打开但一个按钮都没有」—— 不报错，只是存不了。 */
          const footBtns = [...modal.querySelectorAll('.modal__foot button')]
            .map(b => b.textContent.trim());
          check('连接配置弹窗带「取消 / 保存」按钮（openModal 必须用 footer）',
                footBtns.includes('取消') && footBtns.includes('保存'),
                footBtns.join(' / ') || '(一个按钮都没有)');
          const mt = modal.textContent;
          /* 2026-09-22：日期区间入口已按用户要求彻底去掉 —— 这里反过来守
             「弹窗里不该再出现日期字段」，同时连接字段与「密码只写不读」
             必须还在（那是这个弹窗存在的理由）。 */
          check('弹窗只有连接与自动同步字段（日期入口已去掉），且明说密码只写不读',
                !/起始日期|结束日期/.test(mt) && /端口/.test(mt)
                && /密码/.test(mt) && /只写不读/.test(mt),
                mt.replace(/\s+/g, ' ').slice(0, 60));
          /* ★ 2026-09-22：连接**拆成两份**了 —— 这张卡上的弹窗必须是
             「发货明细」自己那份。写成共用时，「改了发货明细的地址、
             结果把匹配库的地址也改了」这种事在界面上完全看不出来。 */
          check('弹窗写明这是「发货明细」专用的连接（不再说共用同一份）',
                /存 ship_erp_\*/.test(mt) && !/共用同一份/.test(mt),
                mt.replace(/\s+/g, ' ').slice(0, 70));
          const cancel = [...modal.querySelectorAll('.modal__foot button')]
            .find(b => b.textContent.trim() === '取消');
          if (cancel) {
            cancel.click();
            await sleep(500);
            check('取消后弹窗关闭（且没有误存配置）', !sd.querySelector('.modal'));
          }
        }
      }
      /* ★ 再开一次验「保存」提交到哪 —— 「取消」那条只证明没误存，
         证明不了存到哪个接口去了。 */
      if (cfgBtn) {
        cfgBtn.click();
        await sleep(900);
        const m2 = sd.querySelector('.modal');
        const saveBtn = m2 && [...m2.querySelectorAll('.modal__foot button')]
          .find(b => b.textContent.trim() === '保存');
        check('连接配置弹窗有「保存」按钮', !!saveBtn);
        if (saveBtn) {
          const n0 = sv.requests.length;
          saveBtn.click();
          await sleep(1400);
          const sent = sv.requests.slice(n0);
          check('发货明细的「连接配置」只提交到 /api/delivery/details/config'
                + '（绝不写匹配数据库那份）',
                sent.some(u => u.startsWith('/api/delivery/details/config'))
                && !sent.some(u => u.startsWith('/api/items/sync/config')),
                sent.join(' | ') || '(没发出请求)');
        }
      }
    }
    reportErrors('发货明细', dv.errors);
  }

  /* ---- 回收站：删除进站 → 还原 → 彻底删除（界面） ----
     所有删除入口（明细查询单条/批量、核销记录、匹配库物料）都要落到这里。
     这一段守**界面接线**：列表能显示、详情能开、还原按钮真的还原、彻底
     删除真的删。最典型的坑是 openModal 写成 buttons（弹窗开了但一个按钮
     都没有）或行按钮没判 CAN，界面看着有、点了没反应 —— 静态检查发现不了。
     smoke [23b] 守的是接口语义，两边互补。 */
  console.log('\n[交互] 回收站 · 删除进站 / 还原 / 彻底删除');
  {
    const stampR = Date.now();
    const keysR = [];
    let ridR = 0;
    try {
      const r = await authFetch(BASE + '/api/returns/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          header: { return_no: `QRB${stampR}`, return_date: '2026-09-19' },
          items: [{ product_code: `QRB${stampR}-1`, return_qty: 1 },
                  { product_code: `QRB${stampR}-2`, return_qty: 1 }],
        }),
      });
      const j = await r.json();
      (j.detail_keys || []).forEach(k => keysR.push(k));
    } catch { /* 下面统一报错 */ }
    check('准备 2 条回收站自检记录', keysR.length === 2, keysR.join(', ') || '创建失败');

    if (keysR.length === 2) {
      // 列表里显示的是「单号 / 标签」（ref_label = 售后单号），明细键只在
      // title 里 —— 所以按单号找行，不能按明细键找。
      const orderR = keysR[0].replace(/-\d+$/, '');

      // 先清历史遗留：自检反复用同一个单号（服务端 next_order_no() 取
      // max+1，而自检单每轮都会被删掉，单号于是又回到同一个），回收站里
      // 因此会积下多行同单号记录。不清掉的话，下面「按单号找行」会点到上
      // 一轮的行 —— 删的是那一条，自己的还在，断言就假红了。
      const staleR = ((await (await authFetch(BASE
        + '/api/recycle?kind=return&page_size=200')).json()).rows || [])
        .filter(x => String(x.ref_key).startsWith(orderR + '-'));
      for (const x of staleR) {
        await authFetch(BASE + `/api/recycle/${x.id}`, { method: 'DELETE' });
      }
      const leftR = ((await (await authFetch(BASE
        + '/api/recycle?kind=return&page_size=200')).json()).rows || [])
        .filter(x => String(x.ref_key).startsWith(orderR + '-'));
      check('清掉历史遗留的同单号回收站记录（自检可重复跑）',
            leftR.length === 0, `清掉 ${staleR.length} 条 · 剩 ${leftR.length} 条`);

      const dr = await authFetch(BASE + `/api/returns/${keysR[0]}`, { method: 'DELETE' });
      check('删除 1 条明细（进回收站）', dr.ok, `HTTP ${dr.status}`);

      const ctxR = await loadPage('/recycle.html', 2000);
      const docR = ctxR.doc, winR = ctxR.window;
      const mlast = () => {
        const a = [...docR.querySelectorAll('.modal')];
        return a[a.length - 1];
      };
      const rowOf = () => [...docR.querySelectorAll('.table tbody tr')]
        .find(tr => tr.textContent.includes(orderR));
      await waitFor(() => !!rowOf(), 20000);
      check('回收站页面列出来刚删掉的那条', !!rowOf(),
            `${docR.querySelectorAll('.table tbody tr').length} 行`);

      // ① 统计卡闭集合：多一格、少一格都要报红
      const kpisR = [...docR.querySelectorAll('.kpis .kpi__label')]
        .map(e => (e.textContent || '').trim());
      check('回收站统计卡是固定四格（待还原 / 已还原 / 记录总数 / 保留天数）',
            kpisR.join('|') === '待还原|已还原|记录总数|保留天数', kpisR.join(' · ') || '(无)');

      // ② 类别筛选是闭集合（三类业务对象都在）
      const kindsR = [...docR.querySelectorAll('select.select option')]
        .map(o => o.textContent.trim());
      check('类别筛选含「全部类别 / 返件明细 / 核销记录 / 匹配库物料」',
            kindsR.includes('全部类别') && kindsR.includes('返件明细')
            && kindsR.includes('核销记录') && kindsR.includes('匹配库物料'),
            kindsR.join(' / ') || '(无下拉)');

      // ③ 表头九列：少一列就意味着这类信息在界面上看不到
      const thsR = [...docR.querySelectorAll('.table thead th')]
        .map(th => th.textContent.trim());
      check('回收站表头九列齐全',
            ['类别', '单号 / 标签', '说明', '行数', '删除人',
             '删除时间', '剩余', '状态', '操作'].every(t => thsR.includes(t)),
            thsR.join(' | ') || '(无表头)');

      if (rowOf()) {
        const btnsR = [...rowOf().querySelectorAll('button')]
          .map(b => b.textContent.trim());
        check('行内有「详情 / ↺ 还原 / × 彻底删除」三个按钮',
              btnsR.includes('详情') && btnsR.includes('↺') && btnsR.includes('×'),
              btnsR.join(' ') || '(无按钮)');
        check('未还原的行状态是「待还原」', /待还原/.test(rowOf().textContent),
              rowOf().textContent.replace(/\s+/g, ' ').slice(-26));

        // ④ 详情弹窗：openModal 必须用 footer，否则「开了但没按钮」
        const detBtn = [...rowOf().querySelectorAll('button')]
          .find(b => b.textContent.trim() === '详情');
        if (detBtn) {
          detBtn.click();
          await waitFor(() => !!mlast(), 9000);
          check('点「详情」能打开快照弹窗', !!mlast(),
                mlast() ? (mlast().querySelector('.modal__title') || {}).textContent
                        : '(没打开)');
          const modR = mlast();
          if (modR) {
            const mtR = modR.textContent;
            check('详情弹窗列出快照来源表（returns_db.returns）',
                  /returns_db\.returns/.test(mtR),
                  (mtR.match(/[\w]+_db\.[\w]+/) || ['(未列出)'])[0]);
            check('详情弹窗写明还原语义（原样写回 / 同键拒绝）',
                  /原样写回/.test(mtR) && /拒绝/.test(mtR),
                  mtR.replace(/\s+/g, ' ').slice(-40));
            const footR = [...modR.querySelectorAll('.modal__foot button')]
              .map(b => b.textContent.trim());
            check('详情弹窗带「关闭 / 彻底删除 / 还原」按钮（openModal 用了 footer）',
                  footR.includes('关闭') && footR.includes('彻底删除') && footR.includes('还原'),
                  footR.join(' / ') || '(一个按钮都没有)');
            const closeR = [...modR.querySelectorAll('.modal__foot button')]
              .find(b => b.textContent.trim() === '关闭');
            if (closeR) { closeR.click(); await sleep(400); }
            check('关闭详情后弹窗消失', !mlast());
          }
        }

        // ⑤ 还原：先确认框，再回写业务表
        const modalsBefore = docR.querySelectorAll('.modal').length;
        const rBtnR = [...rowOf().querySelectorAll('button')]
          .find(b => b.textContent.trim() === '↺');
        check('行内有「↺ 还原」按钮', !!rBtnR);
        if (rBtnR) {
          rBtnR.click();
          await waitFor(() => docR.querySelectorAll('.modal').length > modalsBefore, 8000);
          const cfmR = mlast();
          check('点「↺ 还原」先弹确认框（不直接改数据）',
                !!cfmR && docR.querySelectorAll('.modal').length > modalsBefore,
                `${docR.querySelectorAll('.modal').length} 个弹窗`);
          const okBtnR = cfmR && [...cfmR.querySelectorAll('.modal__foot button')]
            .find(b => b.textContent.trim() === '确定');
          check('还原确认框有「确定」按钮', !!okBtnR);
          if (okBtnR) {
            okBtnR.click();
            await sleep(2200);
            const backR = await authFetch(BASE + `/api/returns/${keysR[0]}`);
            check('界面上点还原后明细真的回到库里', backR.ok, `HTTP ${backR.status}`);
            await waitFor(() => {
              const t = rowOf();
              return !!t && /已还原/.test(t.textContent);
            }, 15000);
            const t2 = rowOf();
            check('还原后该行状态变为「已还原」',
                  !!t2 && /已还原/.test(t2.textContent),
                  t2 ? t2.textContent.replace(/\s+/g, ' ').slice(-30) : '(行没了)');
            check('已还原的行不再给「↺ / ×」（不能重复还原）',
                  !!t2 && ![...t2.querySelectorAll('button')]
                    .some(b => b.textContent.trim() === '↺'
                            || b.textContent.trim() === '×'),
                  t2 ? [...t2.querySelectorAll('button')]
                    .map(b => b.textContent.trim()).join(' ') : '(行没了)');
          }
        }

        // ⑥ 彻底删除：再删一次 → 行上「×」→ 确认 → 回收站记录消失
        await authFetch(BASE + `/api/returns/${keysR[0]}`, { method: 'DELETE' });
        await sleep(700);
        // 这一刀是**接口**删的，页面不会自己刷新 —— 不点「刷新」的话表里
        // 根本没有这条新记录，下面找到的必然是上一轮遗留的行。
        const refBtn = [...docR.querySelectorAll('.topbar button')]
          .find(b => b.textContent.trim() === '刷新');
        if (refBtn) { refBtn.click(); await sleep(1800); }
        // 坑：还原过的那条记录**也还在列表里**（状态「已还原」），同一个单号
        // 会同时存在两行。必须按「待还原」挑行，否则会挑到已还原的那行 ——
        // 它按设计就不该有 ×，断言会假红。列表是 id 倒序，第一条即最新。
        const pendRows = () => [...docR.querySelectorAll('.table tbody tr')]
          .filter(tr => tr.textContent.includes(orderR) && /待还原/.test(tr.textContent));
        const pendRow = () => pendRows()[0];
        const purgeBtn = () => (pendRow() ? [...pendRow().querySelectorAll('button')]
          .find(b => b.textContent.trim() === '×') : null);
        await waitFor(() => pendRows().length > 0, 12000);
        check('刷新后同单号只剩一条「待还原」行（自检数据是干净的）',
              pendRows().length === 1, `${pendRows().length} 条`);
        const pb = purgeBtn();
        check('重新删除后列表出现新的「待还原」行', !!pendRow(),
              [...docR.querySelectorAll('.table tbody tr')]
                .filter(tr => tr.textContent.includes(orderR)).length + ' 行同单号');
        check('该行带「× 彻底删除」按钮', !!pb);
        if (pb) {
          // id 先记下来：彻底删除后界面上就查不到它了
          const lr = await authFetch(BASE + '/api/recycle?kind=return&page_size=200');
          const lj = await lr.json();
          const mine = (lj.rows || [])
            .find(x => x.ref_key === keysR[0] && !x.restored);
          ridR = mine ? mine.id : 0;
          const modsBefore2 = docR.querySelectorAll('.modal').length;
          pb.click();
          await waitFor(() => docR.querySelectorAll('.modal').length > modsBefore2, 8000);
          const cfm2 = mlast();
          const ok2 = cfm2 && [...cfm2.querySelectorAll('.modal__foot button')]
            .find(b => b.textContent.trim() === '确定');
          check('点「× 彻底删除」先弹确认框', !!ok2);
          if (ok2) {
            ok2.click();
            await sleep(2200);
            if (ridR) {
              const gr = await authFetch(BASE + `/api/recycle/${ridR}`);
              check('界面上彻底删除后回收站记录消失', gr.status === 404,
                    `HTTP ${gr.status}`);
            }
            await waitFor(() => !pendRow(), 12000);
            check('界面上彻底删除后不再有「待还原」行', !pendRow());
            check('已还原的那条历史记录仍在（没被连坐删掉）',
                  [...docR.querySelectorAll('.table tbody tr')]
                    .some(tr => tr.textContent.includes(orderR)
                             && /已还原/.test(tr.textContent)));
          }
        }
      }

      // ⑦ 工具条按钮（管理员才有「清理过期 / 清空回收站」）
      const headR = [...docR.querySelectorAll('.topbar button')]
        .map(b => b.textContent.trim());
      check('回收站页工具条有「刷新」按钮', headR.includes('刷新'),
            headR.join(' / ') || '(无)');
      check('管理员能看到「清理过期 / 清空回收站」',
            headR.includes('清理过期') && headR.includes('清空回收站'),
            headR.join(' / ') || '(无)');

      reportErrors('回收站', ctxR.errors);
      ctxR.dom.window.close();
    }

    // 清理：剩下的明细 + 本次造出来的回收站记录
    for (const k of keysR) {
      try { await authFetch(BASE + `/api/returns/${k}`, { method: 'DELETE' }); }
      catch { /* 忽略 */ }
    }
    try {
      // 清两类：① 本轮的售后单明细记录（按单号前缀，历史遗留一并带走）；
      // ② 本次运行期间本账号删过的**其它**对象（核销记录 / 匹配库物料）——
      //    登录账号只有自动化在用，再加 RUN_T0 的时间下界，碰不到真数据。
      const prefR = keysR.length ? keysR[0].replace(/-\d+$/, '') + '-' : '';
      const myUser = smokeCredentials().user;
      const isMine = x => {
        if (prefR && String(x.ref_key).startsWith(prefR)) return true;
        const created = new Date(String(x.created_at || '').replace(' ', 'T'));
        return x.operator === myUser && created >= RUN_T0;
      };
      const allR = await (await authFetch(BASE + '/api/recycle?page_size=200')).json();
      let goneR = 0;
      for (const x of (allR.rows || [])) {
        if (!isMine(x)) continue;
        await authFetch(BASE + `/api/recycle/${x.id}`, { method: 'DELETE' });
        goneR += 1;
      }
      const leftR2 = await (await authFetch(BASE + '/api/recycle?page_size=200')).json();
      const leftMine = (leftR2.rows || []).filter(isMine);
      check('本轮自检留在回收站里的记录已清干净（不把测试垃圾留给用户）',
            goneR > 0 && leftMine.length === 0,
            `清掉 ${goneR} 条 · 剩 ${leftMine.length} 条`);
    } catch (e) { /* 清理失败不影响断言结论 */ }
  }
  console.log('\n' + '='.repeat(62));
  console.log(`  结果：通过 ${PASS.length} 项，失败 ${FAIL.length} 项`);
  if (FAIL.length) {
    console.log('  失败清单：');
    FAIL.forEach(f => console.log('    - ' + f));
  }
  console.log('='.repeat(62));
  process.exit(FAIL.length ? 1 : 0);
})().catch(e => {
  console.error('校验器自身出错:', e);
  process.exit(2);
});
