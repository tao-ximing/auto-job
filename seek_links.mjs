// SEEK 外部投递职位的官网链接：用专用 profile（已登录 SEEK）打开 /job/<id>/apply，登录状态下 SEEK 会直接跳到公司官网，
// 记下跳转后的地址。不登录的接口拿不到这个地址（只有 isLinkOut 标记）。
// 只打开页面、不点任何按钮，不会提交申请。
//
// 用法（由 run.py 调用）: node seek_links.mjs id1 id2 ...   → stdout 一行 JSON {id: url | null}
import { openLoggedIn, SITES } from './browser.mjs';

const ids = process.argv.slice(2);
const out = {};
const log = (...a) => console.error('[seek-links]', ...a);
let ctx, page;
try { ({ ctx, page } = await openLoggedIn(SITES.seek)); } catch (e) { log(e.message); process.exit(3); }
try {
  for (const id of ids) {
    out[id] = null;
    try {
      await page.goto(`https://au.seek.com/job/${id}/apply`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      // 跳转可能是前端 JS 做的：等到离开 seek 域名，最多 15 秒
      await page.waitForURL(u => !/(^|\.)seek\.com(\.au)?$/i.test(new URL(u).hostname), { timeout: 15000 }).catch(() => {});
      const u = page.url();
      if (/^https:\/\//i.test(u) && !/(^|\.)seek\.com(\.au)?$/i.test(new URL(u).hostname)) out[id] = u;
      else log(`${id} 没有跳到官网（停在 ${u}）`);
    } catch (e) { log(`${id} ${e.message.split('\n')[0]}`); }
    await page.waitForTimeout(2000 + Math.random() * 3000);
  }
} finally { await ctx.close(); }
process.stdout.write(JSON.stringify(out) + '\n');
