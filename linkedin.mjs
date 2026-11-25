// LinkedIn 增量抓取：用本机 Chrome + 专用登录 profile（见 browser.mjs）跑 searches.toml 里的 LinkedIn 搜索。
// 每条帖子往 stdout 输出一行 JSON（格式见 ingest.py），日志写 stderr。
//
// 用法（一般由 run.py 调用）: node linkedin.mjs config.json
//   config = { searches: [{name, keywords, geoId, require_remote?, extra_params?}], known: ["jobId", ...], tpr: 86400, maxPages: 8, maxNew: 40 }
//
// 省访问量的关键：卡片上的 componentkey 在点开前就能读到 job id。库里已有的、本次运行里已经点过的都不点开，
// 只输出 id + 搜索名（ingest 只更新 last_seen）。每条搜索最多点开 maxNew 个新职位。
import fs from 'node:fs';
import { openLoggedIn, SITES } from './browser.mjs';

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const known = new Set(cfg.known || []);
const { tpr = 86400, maxPages = 8, maxNew = 40 } = cfg;
const UNIT = { minute: 1, hour: 60, day: 1440, week: 10080, month: 43200 };
const ageMinutes = (s) => {
  const m = /(\d+)\s+(minute|hour|day|week|month)/.exec(s || '');
  return m ? Number(m[1]) * UNIT[m[2]] : null;
};
const emit = (o) => process.stdout.write(JSON.stringify(o) + '\n');
const log = (...a) => console.error('[linkedin]', ...a);

// ── 像人一样操作：随机停顿、鼠标移动过去再点、滚轮滚动、按 JD 长度停留 ──
const rand = (a, b) => a + Math.random() * (b - a);
const sleep = (page, a, b) => page.waitForTimeout(rand(a, b));
// 普通停顿，偶尔（约 12%）走神停更久
async function pause(page, a = 2000, b = 5000) {
  await sleep(page, a, b);
  if (Math.random() < 0.12) await sleep(page, 8000, 20000);
}
// 鼠标分几步移到元素上的随机位置，停一下再点
async function humanClick(page, loc) {
  const box = await loc.boundingBox().catch(() => null);
  if (!box) return loc.click();
  const x = box.x + box.width * rand(0.25, 0.75), y = box.y + box.height * rand(0.3, 0.7);
  await page.mouse.move(x, y, { steps: Math.round(rand(10, 30)) });
  await sleep(page, 200, 700);
  await page.mouse.click(x, y, { delay: rand(40, 140) });
}
// 用滚轮把卡片滚进视野（代替瞬间跳转）
async function scrollTo(page, loc) {
  const vh = page.viewportSize()?.height ?? 900;
  for (let k = 0; k < 25; k++) {
    const box = await loc.boundingBox().catch(() => null);
    if (box && box.y > 80 && box.y + box.height < vh - 40) return;
    await page.mouse.move(rand(150, 450), rand(250, vh - 150), { steps: 5 });
    const dir = !box || box.y > vh / 2 ? 1 : -1;
    await page.mouse.wheel(0, dir * rand(180, 420));
    await sleep(page, 150, 450);
  }
  await loc.scrollIntoViewIfNeeded().catch(() => {});
}
// 在右侧详情面板上边滚边"读"，停留时间跟 JD 长度相关（约 5~18 秒）
async function readPane(page, textLength) {
  const total = Math.min(5000 + textLength * 4, 18000) * rand(0.7, 1.15);
  const vh = page.viewportSize()?.height ?? 900;
  await page.mouse.move(rand(750, 1150), rand(300, vh - 200), { steps: Math.round(rand(8, 20)) });
  const t0 = Date.now();
  while (Date.now() - t0 < total) {
    await sleep(page, 1200, 3500);
    await page.mouse.wheel(0, rand(150, 450) * (Math.random() < 0.15 ? -1 : 1));
  }
}

// 退出码 3 = 登录失效（或被登出 / 跳到验证页，一张卡片都读不到），run.py 会自动改用不登录模式
let ctx, page;
try { ({ ctx, page } = await openLoggedIn(SITES.linkedin)); } catch (e) { log(e.message); process.exit(3); }
let totalSeen = 0;
try {
  for (const s of cfg.searches) {
    // extra_params：比如 remote 筛选 f_SAL（比较宽松，所以 remote 搜索还会按卡片地点再筛一遍）
    const params = { keywords: s.keywords, geoId: s.geoId, f_TPR: `r${tpr}`, ...(s.extra_params || {}) };
    await page.goto('https://www.linkedin.com/jobs/search-results/?' + new URLSearchParams(params), { waitUntil: 'domcontentloaded' });
    const cards = page.locator('div[role=button][componentkey^="job-card-component-ref-"]');
    await cards.first().waitFor({ timeout: 20000 }).catch(() => {});
    await pause(page, 2500, 6000);
    // 按页面显示的结果数决定翻几页（每页 25 条），保证 remote 搜索能看全；最多 maxPages 页
    const total = await page.evaluate(() => Number(((document.body.innerText.match(/([\d,]+)\+? results?/i) || [])[1] || '').replace(/,/g, '')) || null);
    const lastPage = total ? Math.min(Math.ceil(total / 25), maxPages) : maxPages;
    let opened = 0, seen = 0, skipped = 0, offTarget = 0;

    for (let pageNo = 1; pageNo <= lastPage; pageNo++) {
      const n = await cards.count();
      for (let i = 0; i < n; i++) {
        const card = cards.nth(i);
        const id = (await card.getAttribute('componentkey'))?.replace('job-card-component-ref-', '');
        if (!id) continue;
        seen++;
        const label = await card.locator('button[aria-label^="Dismiss "]').first().getAttribute('aria-label', { timeout: 1000 }).catch(() => null);
        const lines = (await card.innerText()).split('\n').map(x => x.trim()).filter(Boolean);
        // 职位名末尾常带空格 / "(Verified job)"，而 lines 已经 trim 过：比较前两边都规范化，
        // 否则找不到职位名那一行（ti = -1），公司、地点整体错位（公司栏变成职位名，地点栏变成公司名）。
        const norm = s => (s || '').replace(/\s*\(Verified job\)\s*$/, '').replace(/\s+/g, ' ').trim();
        const title = norm(label ? label.replace(/^Dismiss /, '').replace(/ job$/, '') : lines[0]);
        let ti = lines.findLastIndex(l => norm(l) === title);
        if (ti < 0) ti = 0;   // 找不到就当第一行是职位名
        const company = lines[ti + 1], location = lines[ti + 2];
        const base = { source: 'linkedin', source_id: id, search: s.name, url: `https://www.linkedin.com/jobs/view/${id}/`, title, company, location };

        if (s.require_remote && !/\(Remote\)/i.test(location || '')) { offTarget++; continue; }
        if (known.has(id)) { emit(base); continue; }
        if (opened >= maxNew) { skipped++; continue; }  // 超过上限的不入库，下次运行再点开

        await sleep(page, 800, 2500);            // 扫一眼列表再决定点哪个
        await scrollTo(page, card);
        await humanClick(page, card);
        await page.waitForURL(new RegExp(`currentJobId=${id}`), { timeout: 10000 }).catch(() => {});
        await sleep(page, 1500, 3000);           // 等面板渲染
        const more = page.locator('button:has-text("more")').filter({ hasText: /^…?\s*more$/ }).first();
        if (await more.isVisible().catch(() => false)) { await humanClick(page, more).catch(() => {}); await sleep(page, 600, 1500); }

        const d = await page.evaluate(() => {
          const h = [...document.querySelectorAll('h2')].find(x => x.textContent.trim() === 'About the job');
          if (!h) return null;
          let jd = h; while (jd && jd.innerText.length < 300) jd = jd.parentElement;
          let pane = h; while (pane && !pane.querySelector('a[href*="/apply"], button[aria-label*="Apply"], a[aria-label*="Apply"]')) pane = pane.parentElement;
          const lines = (pane?.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
          return {
            meta: lines.find(l => / · /.test(l) && /ago/.test(l)) || null,
            easyApply: !!pane?.querySelector('button[aria-label*="Easy Apply"], a[aria-label*="Easy Apply"]'),
            description: jd?.innerText.replace(/^About the job\s*/, '').replace(/\s*…\s*more$/, '').trim(),
          };
        });
        if (!d) { log(`${id} 读不到 JD，跳过`); continue; }
        await readPane(page, d.description?.length || 0);
        known.add(id);
        opened++;
        const [, posted, applicants] = (d.meta || '').split(' · ').map(x => x.trim());
        const age = ageMinutes(posted);
        emit({
          ...base,
          description: d.description,
          posted_at: age == null ? null : new Date(Date.now() - age * 60000).toISOString().replace(/\.\d+Z$/, 'Z'),
          work_arrangement: /\((Remote|Hybrid|On-site)\)/.exec(location || '')?.[1] ?? null,
          extra: { posted_text: posted ?? null, applicants: applicants ?? null, easy_apply: d.easyApply },
        });
      }

      const next = page.getByRole('button', { name: `Page ${pageNo + 1}`, exact: true });
      if (pageNo === lastPage || !(await next.count())) break;
      const firstKey = await cards.first().getAttribute('componentkey');
      await pause(page, 2000, 5000);
      await scrollTo(page, next.first());
      await humanClick(page, next.first());
      await page.waitForFunction(k => document.querySelector('div[role=button][componentkey^="job-card-component-ref-"]')?.getAttribute('componentkey') !== k,
        firstKey, { timeout: 15000 }).catch(() => {});
      await pause(page, 3000, 7000);
    }
    totalSeen += seen;
    log(`${s.name}: 结果 ${total ?? '?'}，翻 ${lastPage} 页，卡片 ${seen}，点开 ${opened}，超过上限留到下次 ${skipped}${s.require_remote ? `，非 remote 跳过 ${offTarget}` : ''}`);
    await pause(page, 15000, 40000);           // 两条搜索之间歇一会儿
  }
} finally {
  await ctx.close();
}
if (totalSeen === 0) { log('所有搜索都没读到卡片，可能被登出或跳到了验证页'); process.exit(3); }
