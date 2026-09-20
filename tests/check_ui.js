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

const cookieHeader = () => (SID ? { Cookie: `ars_sid=${SID}` } : {});

/** 带会话的 fetch（页面 HTML 与页面内接口都用它）。 */
function authFetch(input, init = {}) {
  return fetch(input, { ...init, headers: { ...(init.headers || {}), ...cookieHeader() } });
}

async function loadPage(path, waitMs) {
  const errors = [];
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
  return { dom, window, doc: window.document, errors };
}

function reportErrors(label, errors) {
  check(`${label} 无运行时错误`, errors.length === 0,
        errors.length ? errors.slice(0, 2).join(' ;; ') : '干净');
}

(async () => {
  console.log('=' .repeat(62));
  console.log('  真实 DOM 校验（jsdom 执行页面脚本）');
  console.log('='.repeat(62));

  console.log('\n[前置] 登录（登录验证默认开启，后续页面与接口都需带会话）');
  await loginForTests();
  check('取得测试用会话', !!SID, SID ? `ars_sid=${SID.slice(0, 10)}…` : '（未取得）');

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
    reportErrors(label, errors);
  }

  // ---- 退回登记：交互级验证 ----
  console.log('\n[交互] 退回登记 · 产品明细增删');
  const { doc, errors } = await loadPage('/scan.html', 2000);
  const dataRows = () => doc.querySelectorAll('#line-host tbody tr:not(.line-detail)').length;
  const btnAdd = [...doc.querySelectorAll('button')]
    .find(b => b.textContent.includes('新增一行'));

  check('找到「＋ 新增一行」按钮', !!btnAdd);
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

    const btnClear = [...doc.querySelectorAll('button')]
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
    check('产品编号格已渲染为可搜索下拉', !!cbOf('product_code'),
          cbOf('product_code') ? '有候选面板' : '未渲染');
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

  console.log('\n[交互] 明细查询 · 筛选与列设置');
  {
    const ctxF = await loadPage('/query.html', 2400);
    const docF = ctxF.doc;
    // 必须带会话：否则这里拿到的是 401 的错误体，table_columns 为空，
    // 后面「回显 N/N 列」的断言会以 totalCols=0 的形式静默失败
    const metaF = await (await authFetch(BASE + '/api/meta')).json();
    const totalCols = (metaF.table_columns || []).length;

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

    // 列设置：分组 + 计数 + 批量操作
    const colsBtn = docF.getElementById('btn-cols');
    check('列设置按钮显示已选列数',
          !!colsBtn && /列设置 \(\d+\/\d+\)/.test(colsBtn.textContent),
          colsBtn ? colsBtn.textContent : '(未找到按钮)');

    if (colsBtn) {
      colsBtn.click();
      await sleep(500);
      const modal = docF.querySelector('.modal');
      const btnOf = txt => [...(modal ? modal.querySelectorAll('button') : [])]
        .find(b => b.textContent.trim() === txt);
      const groups = docF.querySelectorAll('.colpick__group');
      const items = docF.querySelectorAll('.colpick__item');
      const count = docF.querySelector('.colpick__count');
      const warn = docF.querySelector('.colpick__warn');

      check('列设置分组展示全部字段',
            groups.length >= 4 && items.length >= 20,
            `${groups.length} 组 · ${items.length} 个选项`);
      check('列设置显示已选计数',
            !!count && /已选 \d+ \/ \d+ 列/.test(count.textContent),
            count ? count.textContent : '(无计数)');

      const allOn = btnOf('全选');
      const allOff = btnOf('全不选');
      const reset = btnOf('恢复默认');
      const apply = btnOf('应用');
      check('提供 全选 / 全不选 / 恢复默认',
            !!(allOn && allOff && reset), '三个批量按钮齐备');

      if (allOff) {
        allOff.click();
        await sleep(250);
        check('「全不选」后计数归零、应用被禁用',
              /已选 0 \//.test(count.textContent)
              && !!apply && apply.disabled === true,
              `${count.textContent} · 应用禁用=${apply && apply.disabled}`);
      }
      if (reset) {
        reset.click();
        await sleep(250);
        check('「恢复默认」回到默认 10 列',
              /已选 10 \/ /.test(count.textContent),
              count.textContent);
      }
      if (allOn) {
        allOn.click();
        await sleep(250);
        check('「全选」后计数为总列数并提示横向滚动',
              /已选 \d+ \/ \d+ 列/.test(count.textContent)
              && !!warn && !warn.classList.contains('hidden'),
              `${count.textContent} · 提示可见=${!!warn && !warn.classList.contains('hidden')}`);
      }

      // 应用「全选」→ 表格应真的渲染出全部列（这是「选太多」的真实路径）
      if (apply) {
        apply.click();
        await sleep(800);
        const ths = docF.querySelectorAll('.table thead th').length;
        check('应用全选后表格渲染全部列',
              ths >= totalCols,
              `${ths} 个表头（含勾选列），字段共 ${totalCols} 列`);
        check('按钮回显已选列数',
              new RegExp(`列设置 \\(${totalCols}\\/${totalCols}\\)`).test(colsBtn.textContent),
              colsBtn.textContent);
      }

      // 复原为默认，避免影响后续用例
      colsBtn.click();
      await sleep(400);
      const modal2 = docF.querySelector('.modal');
      const btn2 = txt => [...(modal2 ? modal2.querySelectorAll('button') : [])]
        .find(b => b.textContent.trim() === txt);
      const reset2 = btn2('恢复默认');
      if (reset2) { reset2.click(); await sleep(200); }
      const apply2 = btn2('应用');
      if (apply2) { apply2.click(); await sleep(600); }
      check('可复原为默认列设置',
            /列设置 \(10\//.test(colsBtn.textContent),
            colsBtn.textContent);
    }
  }

  /* ---- 明细查询：点行打开编辑弹窗 ----
     这条原本是空白区：以前只验证筛选面板 / 列设置 / 批量删除，**从没打开过
     编辑弹窗**。于是 query.html 里 makeField 引用了未声明的变量（`cur`，
     那是 scan.html 里别的函数的局部变量）时无人发现 —— openDetail 整体抛
     ReferenceError，表现为「点明细行没反应、弹窗根本不出现」，
     而静态语法检查完全正常。这里把「弹窗能开 + 值能回显」钉住。

     必须带 erp_handled（唯一 no_empty 的两值字段）—— 触发点就在它身上。 */
  console.log('\n[交互] 明细查询 · 编辑弹窗');
  {
    const stampE = Date.now();
    const keyE = [];
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
      (j.detail_keys || []).forEach(k => keyE.push(k));
    } catch { /* 下面统一报错 */ }
    check('准备编辑弹窗自检记录', keyE.length === 1, keyE.join(', ') || '创建失败');

    if (keyE.length === 1) {
      try {
        await authFetch(BASE + `/api/returns/${keyE[0]}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ erp_handled: '已处理', handle_solution: '自检-编辑弹窗' }),
        });
        const ctxE = await loadPage('/query.html', 2200);
        const docE = ctxE.doc, winE = ctxE.window;
        ctxE.errors.length = 0;                 // 只看打开弹窗之后的报错

        const kwE = [...docE.querySelectorAll('input.input')]
          .find(i => (i.placeholder || '').includes('搜索单号'));
        let targetRow = null;
        if (kwE) {
          kwE.value = keyE[0];
          kwE.dispatchEvent(new winE.KeyboardEvent('keydown',
            { key: 'Enter', bubbles: true }));
          await sleep(1600);
          targetRow = [...docE.querySelectorAll('.table tbody tr')]
            .find(tr => tr.textContent.includes(keyE[0]));
        }
        check('检索到目标明细行', !!targetRow,
              `${docE.querySelectorAll('.table tbody tr').length} 行`);

        if (targetRow) {
          targetRow.dispatchEvent(new winE.MouseEvent('click', { bubbles: true }));
          await sleep(900);
          const modalE = docE.querySelector('.modal');
          // ① 弹窗必须真的出现（缺陷下这一步就失败：ReferenceError 中断了 openDetail）
          check('点明细行能打开编辑弹窗', !!modalE,
                modalE ? modalE.textContent.replace(/\s+/g, ' ').slice(0, 40)
                       : '(未打开 —— openDetail 可能抛错)');
          check('打开编辑弹窗无运行时错误', ctxE.errors.length === 0,
                ctxE.errors.slice(0, 2).join(' ;; ') || '干净');

          // ② no_empty 的固定下拉必须回显记录的真实值，而不是落到默认值
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
        }
      } finally {
        for (const k of keyE) {
          try { await authFetch(BASE + `/api/returns/${k}`, { method: 'DELETE' }); }
          catch { /* 清理失败不影响断言结论 */ }
        }
      }
    }
  }

  // ---- 明细查询：勾选 → 批量删除 的端到端验证 ----
  console.log('\n[交互] 明细查询 · 批量删除');
  {
    const stampQ = Date.now();
    const keysQ = [];
    try {
      const r = await authFetch(BASE + '/api/returns/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          header: { return_no: `JDQ${stampQ}`, return_date: '2026-09-19' },
          items: [{ product_code: `QDEL${stampQ}-1`, return_qty: 1 },
                  { product_code: `QDEL${stampQ}-2`, return_qty: 1 }],
        }),
      });
      const j = await r.json();
      (j.detail_keys || []).forEach(k => keysQ.push(k));
    } catch { /* 下面统一报错 */ }
    check('准备 2 条待删记录', keysQ.length === 2, keysQ.join(', ') || '创建失败');

    if (keysQ.length === 2) {
      const ctxQ = await loadPage('/query.html', 2400);
      const docQ = ctxQ.doc;
      const rowsQ = [...docQ.querySelectorAll('.table tbody tr')];

      // 确认弹窗的返回值契约：必须「先定结果再关闭」。
      // 曾因 m.close() 触发 onClose → resolve(false) 抢先解决，
      // 导致点「确定」恒返回 false，所有删除操作静默不执行。
      const pC = docQ.defaultView.confirmDialog('自检：确认弹窗返回值');
      await sleep(300);
      const mC = docQ.querySelector('.modal');
      const okC = mC && [...mC.querySelectorAll('button')]
        .find(b => b.textContent.includes('确定'));
      if (okC) okC.click();
      check('确认弹窗点「确定」返回 true',
            (await pC) === true, String(await pC));

      const targetRow = rowsQ.find(tr => tr.textContent.includes(keysQ[0]));
      check('新记录出现在明细表格中', !!targetRow, `表格 ${rowsQ.length} 行`);

      if (targetRow) {
        const cb = targetRow.querySelector('input[type="checkbox"]');
        check('表格行有勾选框', !!cb, cb ? '已渲染' : '未找到');
        if (cb) {
          cb.click();
          await sleep(400);
          // 工具栏在明细卡片里（页面第一个 .card__tools 是筛选卡的）
          const tools = [...docQ.querySelectorAll('.card__tools')]
            .find(el => el.textContent.includes('批量删除'));
          check('勾选后显示已选条数',
                !!tools && /已选\s*1\s*条/.test(tools.textContent),
                tools ? tools.textContent.trim().slice(0, 30) : '(未找到工具栏)');
          check('勾选行有高亮标记',
                !!docQ.querySelector('.table tbody tr.is-picked'), 'is-picked');
        }
      }

      // 再勾一条，然后点批量删除
      const row2 = rowsQ.find(tr => tr.textContent.includes(keysQ[1]));
      const cb2 = row2 && row2.querySelector('input[type="checkbox"]');
      check('找到第二条记录的勾选框', !!cb2, keysQ[1]);
      if (cb2) { cb2.click(); await sleep(400); }
      check('可选中的行数达 2 条',
            docQ.querySelectorAll('.table tbody tr.is-picked').length === 2,
            `${docQ.querySelectorAll('.table tbody tr.is-picked').length} 行已勾选`);

      const delBtn = [...docQ.querySelectorAll('button')]
        .find(b => b.textContent.includes('批量删除'));
      check('找到「批量删除」按钮', !!delBtn);
      if (delBtn) {
        delBtn.click();
        await sleep(700);
        const modal = docQ.querySelector('.modal');
        check('弹出删除确认框', !!modal,
              modal ? modal.textContent.trim().slice(0, 40) : '(未弹出)');
        check('确认框写明删除条数与不可恢复',
              !!modal && /2\s*条/.test(modal.textContent)
              && modal.textContent.includes('不可恢复'),
              '含条数与警告');

        const okBtn = modal && [...modal.querySelectorAll('button')]
          .find(b => b.textContent.includes('确定'));
        if (okBtn) {
          okBtn.click();
          await sleep(2200);
          const q1 = await (await authFetch(
            BASE + '/api/returns?keyword=' + encodeURIComponent(keysQ[0]))).json();
          const q2 = await (await authFetch(
            BASE + '/api/returns?keyword=' + encodeURIComponent(keysQ[1]))).json();
          check('批量删除已生效（两条都已删除）',
                (q1.total || 0) === 0 && (q2.total || 0) === 0,
                `剩余 ${q1.total} / ${q2.total}`);
        } else {
          check('确认框有「确定」按钮', false, '未找到');
        }
      }

      // 兜底清理：若上面的删除未生效（测试失败），也要把测试数据清掉，
      // 否则残留会污染库、让后续断言看到「多出来的记录」
      for (const k of keysQ) {
        await authFetch(BASE + '/api/returns/' + encodeURIComponent(k),
                        { method: 'DELETE' }).catch(() => {});
      }
      const rest = await (await authFetch(
        BASE + '/api/returns?keyword=' + encodeURIComponent(keysQ[0]))).json();
      check('批量删除测试数据已清理', (rest.total || 0) === 0,
            `剩余 ${rest.total} 条`);
    }
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

  // ---- 数据接口页：令牌 / 数据范围 / 白名单（2026-09-20 由「同步设置」改造）----
  console.log('\n[交互] 数据接口 · 令牌与数据范围');
  {
    const ctxA = await loadPage('/api.html', 2600);
    const docA = ctxA.doc;

    const titles = [...docA.querySelectorAll('.card__title')].map(e => e.textContent);
    check('数据接口页由六张卡片构成',
          ['运行状态', '接口地址与令牌', '数据范围', '来源 IP 白名单',
           '金山文档定时任务', '拉取日志'].every(t => titles.includes(t)),
          titles.join(' · '));

    const body = docA.body.textContent;
    check('页面明确说明「不再由本系统推送，改由金山定时任务拉取」',
          /不再主动写入金山文档/.test(body), '含说明');
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
    check('给金山侧的配置要点含目标文件与工作表',
          /目标文件 ID/.test(body) && /落点工作表/.test(body),
          (body.match(/服务器数据/) || [''])[0]);

    // 金山侧三项（文件 ID / 云盘 ID / 落点工作表）：2026-09-20 由只读展示
    // 改为可填写 —— 换文件或换工作表时在界面上改，不用动 config.py 重启
    const kdocsIn = [...docA.querySelectorAll('input.input[data-field]')]
      .filter(i => ['file_id', 'drive_id', 'sheet'].includes(i.dataset.field));
    check('金山侧三项渲染为输入框（不再是只读文本）',
          kdocsIn.length === 3,
          kdocsIn.map(i => i.dataset.field).join(' · ') || '(未找到)');
    const kv = {};
    kdocsIn.forEach(i => { kv[i.dataset.field] = i.value; });
    check('输入框带出当前值（未配置过时是系统内置默认值）',
          !!kv.file_id && !!kv.sheet,
          `file_id=${(kv.file_id || '').slice(0, 12)}… · sheet=${kv.sheet || '(空)'}`);
    check('三项都有对应的字段标签',
          /目标文件 ID/.test(body) && /云盘 ID/.test(body)
          && /落点工作表/.test(body), '标签完整');

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

    // 金山侧目标信息：填 → 保存 → 回显；测完把原值写回去（别污染用户配置）
    const btnByText = t => [...docA.querySelectorAll('button')]
      .find(b => b.textContent.trim() === t);
    const kSave = btnByText('保存');
    const kReset = btnByText('恢复默认');
    check('金山文档定时任务卡片带「保存」按钮', !!kSave, kSave ? '存在' : '缺失');
    check('带「恢复默认」按钮（可回到系统内置值）', !!kReset,
          kReset ? '存在' : '缺失');
    const kSheetIn = [...docA.querySelectorAll('input.input[data-field]')]
      .find(i => i.dataset.field === 'sheet');
    if (kSave && kSheetIn) {
      const origSheet = kSheetIn.value;
      kSheetIn.value = '自检落点表';
      kSheetIn.dispatchEvent(new ctxA.window.Event('input', { bubbles: true }));
      kSave.click();
      await sleep(1500);
      const kToast = [...docA.querySelectorAll('[class*=toast]')]
        .map(e => e.textContent).join(' | ');
      check('保存金山侧目标信息后给出成功提示',
            /已保存|保存失败/.test(kToast), kToast.slice(0, 60));
      // 保存会整页重渲染，必须按 data-field 重新取节点比对新值
      const reSheet = [...docA.querySelectorAll('input.input[data-field]')]
        .find(i => i.dataset.field === 'sheet');
      check('保存后回显新填写的工作表名（重渲染也没丢）',
            reSheet && reSheet.value === '自检落点表',
            reSheet ? reSheet.value : '(输入框消失)');
      check('输入框未被只读化（仍可继续编辑）',
            reSheet && !reSheet.readOnly && !reSheet.disabled,
            reSheet ? '可编辑' : '(无)');
      if (reSheet) {                      // 复原
        reSheet.value = origSheet;
        reSheet.dispatchEvent(new ctxA.window.Event('input', { bubbles: true }));
        const again = btnByText('保存');
        if (again) { again.click(); await sleep(1500); }
      }
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
    check('列出五个库文件路径',
          ['returns', 'inspect', 'handle', 'items', 'auth']
            .every(k => stxt.includes(k)), '五库齐全');

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
