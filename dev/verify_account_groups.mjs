/**
 * 账号分组的浏览器断言（由 verify_account_groups_ui.py 调用）。
 *
 * 走用户路径：默认分组 → 添加分组（填账号目录）→ 移动到分组 → 无目录分组的提示。
 * 另外钉住两件单测看不见的事：
 *   · 分组列表读的是**它自己的目录**（甲组号不在默认分组里，反之亦然）；
 *   · 「在线」徽章来自**该分组自己的上游实例**——若问错实例，甲组号会显示
 *     「未加载」，而这在界面上和真的没加载长得一模一样。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:8023';
const PASS = process.env.WB_PASS || '';
const DEFAULT_URL = process.env.WB_DEFAULT_URL || '';
const GROUP_URL = process.env.WB_GROUP_URL || 'http://127.0.0.1:8022';
const GROUP_KEY = process.env.WB_GROUP_KEY || '';
const GROUP_DIR = process.env.WB_GROUP_DIR || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-account-groups');

async function loadPlaywright() {
  const explicit = process.env.WB_PLAYWRIGHT;
  const candidates = [
    ...(explicit ? [explicit] : []),
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const c of candidates) {
    try {
      const mod = await import(c.startsWith('/') || /^[A-Za-z]:/.test(c) ? pathToFileURL(c).href : c);
      return mod.chromium ?? mod.default?.chromium;
    } catch { /* 试下一个 */ }
  }
  throw new Error(`找不到 playwright-core（试过：${candidates.join(', ')}）`);
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell'))
    .sort().pop();
  if (!dir) return undefined;
  const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');
  return fs.existsSync(exe) ? exe : undefined;
}

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const page = await (await browser.newContext({viewport: {width: 1500, height: 1000}})).newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e)));

// 浮动底栏（fixed z-40）会截走靠近底部的点击；本脚本与底栏无关，隐藏它（测试夹具）。
await page.addInitScript(() => {
  const hide = () => document.querySelectorAll('div.fixed.z-40').forEach((el) => {
    el.style.display = 'none';
  });
  document.addEventListener('DOMContentLoaded', hide);
  setInterval(hide, 400);
});

/** 页面文本，**先把 toast 摘掉**：提示条里也会出现昵称等地名，会把断言喂绿。 */
const bodyText = () => page.evaluate(() => {
  const toast = Array.from(document.querySelectorAll('[data-sonner-toaster]'));
  const prev = toast.map((e) => e.style.display);
  toast.forEach((e) => { e.style.display = 'none'; });
  const out = document.body.innerText.replace(/\s+/g, ' ');
  toast.forEach((e, i) => { e.style.display = prev[i]; });
  return out;
});

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', 'admin');
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(3000);

// ① 默认分组
let text = await bodyText();
step(/默认分组/.test(text), '分组切换里有「默认分组」');
step(text.includes('默认号'), '默认分组列出默认目录的账号',
     text.split('\n').find((l) => l.includes('默认号')) || '');
step(!text.includes('甲组号'), '默认分组的列表里看不到甲组的账号（目录隔离）');
step(!text.includes(GROUP_KEY), '页面上不出现明文 api_key',
     text.includes(GROUP_KEY) ? '出现了明文密钥' : '未出现明文 ✓');
await page.screenshot({path: path.join(OUT, '01-default.png'), fullPage: true});

// ② 添加分组（甲组）：填账号目录 → 保存后自动切过去
await page.getByRole('button', {name: /添加分组/}).first().click();
await page.waitForTimeout(800);
const dlg = page.locator('[role=dialog]').last();
await dlg.getByPlaceholder('例如：业务组').fill('甲组');
// 用户反馈：添加分组不想手填——只填名称时，地址与账号目录都要带出建议值
const urlVal = await dlg.getByPlaceholder('http://127.0.0.1:7863').inputValue();
const dirVal = await dlg.getByPlaceholder('/opt/workbuddy2api-g2/auths').inputValue();
step(!!urlVal && urlVal === DEFAULT_URL, '上游地址自动带出默认分组的地址', urlVal);
step(!!dirVal, '账号目录自动带出建议路径（同级 + 名称后缀）', dirVal);
step(/共用同一套实例/.test(await bodyText()),
     '地址与默认分组相同时给出「共用同一套实例」提示');
await dlg.getByPlaceholder('http://127.0.0.1:7863').fill(GROUP_URL);
await dlg.getByPlaceholder('留空 = 不带鉴权头').fill(GROUP_KEY);
await dlg.getByPlaceholder('/opt/workbuddy2api-g2/auths').fill(GROUP_DIR);
await page.screenshot({path: path.join(OUT, '02-add-group-dialog.png')});
await dlg.getByRole('button', {name: /保存|确定|新增/}).last().click();
await page.waitForTimeout(3000);
text = await bodyText();
step(text.includes('甲组'), '新分组出现在切换条里');
step(text.includes('甲组号'), '保存后自动切到甲组，并列出甲组目录的账号（读的是它自己的目录）');
step(!text.includes('默认号'), '甲组列表里看不到默认分组的账号');
step(/在线/.test(text), '甲组号显示「在线」——状态来自该分组自己的上游实例',
     text.split('\n').find((l) => l.includes('甲组号')) || '');
await page.screenshot({path: path.join(OUT, '03-group-tab.png'), fullPage: true});

// ③ 移动到分组：默认分组 → 甲组
await page.getByRole('button', {name: '默认分组', exact: true}).first().click();
await page.waitForTimeout(2500);
step((await bodyText()).includes('默认号'), '切回默认分组：账号还在');
const moveBtn = page.locator('button[title="移动到分组"]:visible').first();
step((await moveBtn.count()) > 0, '账号行有「移动到分组」按钮');
await moveBtn.click();
await page.waitForTimeout(800);
const mDlg = page.locator('[role=dialog]').last();
await mDlg.getByRole('combobox').click();
await page.waitForTimeout(500);
await page.getByRole('option', {name: '甲组'}).click();
await page.screenshot({path: path.join(OUT, '04-move-dialog.png')});
await mDlg.getByRole('button', {name: /^移动$/}).last().click();
await page.waitForTimeout(3000);
step(!(await bodyText()).includes('默认号'), '移动后默认分组里不再有它（文件已搬走）');
await page.getByRole('button', {name: '甲组', exact: true}).first().click();
await page.waitForTimeout(2500);
text = await bodyText();
step(text.includes('默认号') && text.includes('甲组号'),
     '甲组里现在同时有它和原有的甲组号（移入成功）');
await page.screenshot({path: path.join(OUT, '05-moved.png'), fullPage: true});

// ④ 没配账号目录的分组：只读 + 说明原因
await page.getByRole('button', {name: /添加分组/}).first().click();
await page.waitForTimeout(800);
const dlg2 = page.locator('[role=dialog]').last();
await dlg2.getByPlaceholder('例如：业务组').fill('空组');
await dlg2.getByPlaceholder('http://127.0.0.1:7863').fill('http://127.0.0.1:8021');
// 显式清空账号目录 = 该分组只做密钥转发（新表单默认会带出建议路径）
await dlg2.getByPlaceholder('/opt/workbuddy2api-g2/auths').fill('');
await dlg2.getByRole('button', {name: /保存|确定|新增/}).last().click();
await page.waitForTimeout(2500);
text = await bodyText();
step(/未配置本地账号目录/.test(text), '没配账号目录的分组会说明原因（只读）',
     text.split('\n').find((l) => l.includes('未配置本地账号目录')) || '');
step(/共用同一套/.test(text), '地址与默认分组相同的分组也有常驻说明',
     text.split('\n').find((l) => l.includes('共用同一套')) || '');
await page.screenshot({path: path.join(OUT, '06-no-dir-hint.png'), fullPage: true});

step(pageErrors.length === 0, '没有未捕获的前端异常', pageErrors.slice(0, 2).join(' | '));

console.log('');
console.log('=== 结果 ===');
if (findings.length) {
  console.log('FAILED:');
  for (const f of findings) console.log('  - ' + f);
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
await browser.close();
