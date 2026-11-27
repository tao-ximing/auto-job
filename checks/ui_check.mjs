// 网页检查（只发 GET 请求，可以对正式服务跑）：
//   1. static/index.html 里每段内联脚本都能通过语法检查（曾经因为重复的 const 整页脚本报错）
//   2. iPhone 和电脑尺寸下页面能启动、列表能画出来、没有脚本报错
//   3. 投递按钮：ARCHITECTURE.md 5.6 那张表的每种情况，从库里各找一个真实职位，核对按钮文字、地址和打开方式
//
// 用法：node checks/ui_check.mjs [http://127.0.0.1:8765]
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';

const require = createRequire(import.meta.url);
const { chromium, devices } = require(process.env.PW_PATH || `${os.homedir()}/.npm-global/lib/node_modules/playwright`);
const BASE = process.argv[2] || 'http://127.0.0.1:8765';
const errors = [];
const fail = (msg) => { errors.push(msg); console.log('  ✗ ' + msg); };
const ok = (msg) => console.log('  ✓ ' + msg);

// ── 1. 内联脚本语法：每段单独当普通脚本检查 ──
const html = await (await fetch(BASE + '/')).text();
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g)];
if (!scripts.length) fail('index.html 里没有找到内联脚本');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-check-'));
try {
  scripts.forEach(([, attrs, code], i) => {
    const file = path.join(tmpDir, `inline${i}.${/type=["']?module/.test(attrs) ? 'mjs' : 'cjs'}`);
    fs.writeFileSync(file, code);
    try { execFileSync(process.execPath, ['--check', file], { stdio: 'pipe' }); }
    catch (e) { fail(`第 ${i + 1} 段内联脚本语法错误：\n${e.stderr}`); }
  });
} finally { fs.rmSync(tmpDir, { recursive: true, force: true }); }
if (scripts.length && !errors.length) ok(`内联脚本语法（${scripts.length} 段）`);

// ── 3 用到的数据：每种情况找一个职位 ──
const jobs = await (await fetch(BASE + '/api/jobs?view=all')).json();
const SRC = { seek: 'SEEK', linkedin: 'LinkedIn', email: 'Gmail' };
// 预期：用哪个地址、iPhone 上是不是去 Chrome、按钮文字（和 ARCHITECTURE.md 5.6 一致）
const expect = p => {
  const dest = p.quick_apply ? p.url : (p.apply_url || p.url);
  const platform = /^https:\/\/([^/]+\.)?(seek\.com|seek\.com\.au|linkedin\.com)\//.test(dest);
  const label = p.quick_apply ? `一键投（${SRC[p.source]}）` : p.apply_url ? '去官网投递' : `在 ${SRC[p.source]} 打开`;
  return { dest, platform, label };
};
const cases = [  // [名字, 条件, 必须有]
  ['SEEK 一键投', p => p.source === 'seek' && p.quick_apply === true, true],
  ['LinkedIn 一键投', p => p.source === 'linkedin' && p.quick_apply === true, true],
  ['SEEK 外部投递，有官网链接', p => p.source === 'seek' && p.quick_apply === false && p.apply_url, true],
  ['LinkedIn 外部投递，有官网链接', p => p.source === 'linkedin' && p.quick_apply === false && p.apply_url, true],
  ['SEEK 外部投递，没有官网链接', p => p.source === 'seek' && p.quick_apply === false && !p.apply_url, false],
  ['LinkedIn 外部投递，没有官网链接', p => p.source === 'linkedin' && p.quick_apply === false && !p.apply_url, false],
  ['不知道是不是一键投，没有官网链接（老帖子）', p => p.source !== 'email' && p.quick_apply == null && !p.apply_url, false],
  ['不知道是不是一键投，有官网链接', p => p.source !== 'email' && p.quick_apply == null && p.apply_url, false],
  ['按邮件建的职位', p => p.source === 'email', false],
].map(([name, pred, required]) => {
  for (const j of jobs) { const p = j.postings.find(pred); if (p) return { name, required, id: j.id, post: p, ...expect(p) }; }
  return { name, required, id: null };
});
if (jobs.length >= 1000) console.log('  注意：/api/jobs 最多返回 1000 条，只在这些职位里找样本');
for (const c of cases) if (!c.id && c.required) fail(`库里找不到「${c.name}」的职位，这种情况没法验证`);

const browser = await chromium.launch({ channel: 'chrome' });
for (const dev of ['iPhone 13', 'Desktop Chrome']) {
  console.log(`== ${dev}`);
  const page = await (await browser.newContext({ ...devices[dev] })).newPage();
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));

  // ── 2. 启动 ──
  await page.goto(BASE + '/');
  try {
    await page.waitForSelector('#tabbar button', { timeout: 10000 });
    await page.waitForFunction(() => document.querySelector('#list')?.children.length > 0 || /没有|暂无/.test(document.querySelector('#list')?.textContent || ''),
                               null, { timeout: 10000 });
    ok('页面启动、列表画出来了');
  } catch { fail(`${dev}：页面没启动（标签栏或列表没出现）`); }

  // ── 3. 投递按钮 ──
  const ios = dev.startsWith('iPhone');
  for (const c of cases) {
    if (!c.id) { console.log(`  - ${c.name}：库里没有这种职位，跳过`); continue; }
    await page.goto(`${BASE}/?job=${c.id}`);
    await page.waitForSelector('.link-btn', { timeout: 10000 }).catch(() => {});
    const detail = await (await fetch(`${BASE}/api/jobs/${c.id}`)).json();
    const btns = await page.$$eval('.link-btn', a => a.map(x => ({ href: x.getAttribute('href'), target: x.getAttribute('target'),
                                                                   label: x.querySelector('.src')?.textContent })));
    const b = btns[detail.postings.findIndex(p => p.source === c.post.source && p.url === c.post.url)];
    if (!b) { fail(`${dev} ${c.name} #${c.id}：找不到按钮`); continue; }
    const want = !ios ? c.dest : c.platform ? c.dest : c.dest.replace(/^https:\/\//, 'googlechromes://');
    const problems = [];
    if (b.href !== want) problems.push(`地址应该是 ${want}，实际是 ${b.href}`);
    if (b.label !== c.label) problems.push(`按钮文字应该是「${c.label}」，实际是「${b.label}」`);
    if (!ios && b.target !== '_blank') problems.push('电脑上应该在新标签页打开');
    if (c.post.source === 'seek' && /\/apply\b/.test(b.href)) problems.push('SEEK 不能链接 /apply（SEEK App 不接管投递页）');
    if (problems.length) fail(`${dev} ${c.name} #${c.id}：${problems.join('；')}`); else ok(`${c.name}  ${b.href.slice(0, 60)}`);
  }
  if (pageErrors.length) fail(`${dev} 脚本报错：${pageErrors.join(' | ')}`);
}
await browser.close();
if (errors.length) { console.log(`\nui_check：${errors.length} 处不对`); process.exit(1); }
console.log('\nui_check：通过');
