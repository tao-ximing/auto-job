// 共用的浏览器启动 + 登录维护逻辑。
//
// 方案：脚本有一个专用 Chrome profile（默认 ~/.cache/auto-job-profile），里面的 SEEK / LinkedIn
// 是你在这个 profile 里单独登录的，和日常 Chrome 是两个独立会话，各自续期、互不影响。
// 不要把日常 Chrome 的 cookie 复制过来：SEEK 用 Auth0 refresh token 轮换，两边共用同一个会话时，
// 一边刷新 token 会让另一边手里的旧 token 被判为"盗用"，整个会话被注销，两边都会被登出。
//
// 登录：node browser.mjs login —— 用普通方式（不经 Playwright、没有自动化标记）打开这个 profile 的 Chrome，
// 你手动登录后关掉窗口即可。之所以不用 Playwright 打开，是因为 Google 登录会拒绝被自动化控制的浏览器
// （"This browser or app may not be secure"）。脚本不碰密码。
// 脚本运行时发现掉线，会报错并提示你运行这个命令。
import { createRequire } from 'node:module';
import os from 'node:os';
import { execFileSync, spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_PATH || (() => {
  try { return require.resolve('playwright'); } catch { return `${os.homedir()}/.npm-global/lib/node_modules/playwright`; }
})());

export const PROFILE = process.env.AUTO_JOB_PROFILE || `${os.homedir()}/.cache/auto-job-profile`;

// 抓取时 Chrome 跑在 Xvfb 虚拟屏幕上：仍然是正常的有界面 Chrome（不是 headless，Cloudflare / LinkedIn 看到的指纹和平时一样），
// 但窗口画在看不见的虚拟屏幕上，桌面上什么都不弹。以前是"有界面 + 窗口放到屏幕外"，可 Wayland 不让程序指定窗口位置，
// 窗口每小时弹到桌面上，被人随手关掉就整轮中止（2026-11-23）。
// 没装 Xvfb 时退回新版 headless（UA 换成普通 Chrome，去掉 HeadlessChrome）。手动登录（visible）用真实桌面。
const CHROME_MAJOR = (() => {
  try { return execFileSync('/opt/google/chrome/chrome', ['--version']).toString().match(/(\d+)\./)[1]; } catch { return null; }
})();
const UA = CHROME_MAJOR && `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${CHROME_MAJOR}.0.0.0 Safari/537.36`;
const HAS_XVFB = (() => { try { execFileSync('which', ['Xvfb']); return true; } catch { return false; } })();

// 启动一块虚拟屏幕，返回 { display, stop }。-displayfd 让 Xvfb 自己挑一个空闲的屏幕号并告诉我们。
function startXvfb() {
  return new Promise((resolve, reject) => {
    const x = spawn('Xvfb', ['-displayfd', '3', '-screen', '0', '1920x1080x24', '-nolisten', 'tcp'],
                    { stdio: ['ignore', 'ignore', 'ignore', 'pipe'] });
    let buf = '';
    x.stdio[3].on('data', d => {
      buf += d;
      if (buf.includes('\n')) resolve({ display: ':' + buf.trim(), stop: () => { try { x.kill(); } catch {} } });
    });
    x.on('error', reject);
    x.on('exit', code => reject(new Error(`Xvfb 退出（${code}）`)));
    process.on('exit', () => { try { x.kill(); } catch {} });
  });
}

async function launch({ visible = false } = {}) {
  const common = {
    channel: 'chrome',      // 系统装的 Google Chrome
    // Playwright 默认带 --password-store=basic，Chrome 会解不开系统 keyring 加密的 (v11) cookie 并把它们删掉，
    // 必须去掉这个默认参数、改用系统 keyring。
    ignoreDefaultArgs: ['--password-store=basic', '--use-mock-keychain'],
  };
  if (visible) {
    return chromium.launchPersistentContext(PROFILE, { ...common, headless: false, args: ['--password-store=gnome-libsecret'], viewport: null });
  }
  if (HAS_XVFB) {
    const x = await startXvfb();
    const env = { ...process.env, DISPLAY: x.display };
    delete env.WAYLAND_DISPLAY;   // 不然 Chrome 会优先连真实桌面的 Wayland
    try {
      const ctx = await chromium.launchPersistentContext(PROFILE, {
        ...common, headless: false, env,
        args: ['--password-store=gnome-libsecret', '--ozone-platform=x11', '--window-size=1280,900'],
        viewport: { width: 1280, height: 900 },
      });
      ctx.on('close', x.stop);
      return ctx;
    } catch (e) { x.stop(); throw e; }
  }
  return chromium.launchPersistentContext(PROFILE, {
    ...common, headless: true, args: ['--password-store=gnome-libsecret'],
    viewport: { width: 1280, height: 900 }, ...(UA ? { userAgent: UA } : {}),
  });
}

async function isLoggedIn(page, site) {
  await page.goto(site.checkUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  return site.isLoggedIn(page).catch(() => false);
}

/**
 * 打开一个已登录 `site` 的页面；未登录则报错，提示运行 `node browser.mjs login`。
 * @returns {{ ctx, page }}  用完记得 ctx.close()
 */
export async function openLoggedIn(site) {
  const ctx = await launch();
  const page = ctx.pages()[0] ?? await ctx.newPage();
  if (await isLoggedIn(page, site)) return { ctx, page };
  await ctx.close();
  throw new Error(`[${site.name}] 专用 profile 未登录，请先运行: node browser.mjs login`);
}

export const SITES = {
  seek: {
    name: 'SEEK',
    loginUrl: 'https://au.seek.com/my-activity/saved-searches',  // 未登录时会自动跳到登录页
    checkUrl: 'https://au.seek.com/my-activity/saved-searches',
    isLoggedIn: async (page) => /au\.seek\.com/.test(page.url())
      && (await page.locator('a[data-automation="savedSearchName"], [data-automation="account name"]').count()) > 0,
  },
  linkedin: {
    name: 'LinkedIn',
    loginUrl: 'https://www.linkedin.com/login',
    checkUrl: 'https://www.linkedin.com/feed/',
    isLoggedIn: async (page) => /linkedin\.com\/(feed|in\/|jobs)/.test(page.url())
      && (await page.getByRole('button', { name: /^Me\b/ }).count()) > 0,
  },
};

// node browser.mjs login  → 用普通 Chrome 打开专用 profile，你登录完关掉窗口，再逐个验证
if (import.meta.url === pathToFileURL(process.argv[1]).href && process.argv[2] === 'login') {
  const sites = Object.values(SITES);
  console.error(`已打开 Chrome（专用 profile: ${PROFILE}）。请登录 ${sites.map(s => s.name).join('、')}，完成后关掉这个窗口。`);
  const chrome = spawn('google-chrome', [
    `--user-data-dir=${PROFILE}`, '--password-store=gnome-libsecret', '--no-first-run', '--no-default-browser-check',
    '--new-window', ...sites.map(s => s.loginUrl),
  ], { stdio: 'ignore' });
  await new Promise(r => chrome.on('exit', r));

  console.error('窗口已关闭，验证登录状态…');
  const ctx = await launch();
  const page = ctx.pages()[0] ?? await ctx.newPage();
  let ok = true;
  for (const site of sites) {
    const yes = await isLoggedIn(page, site);
    console.error(`  ${yes ? '✓' : '✗'} ${site.name}`);
    ok &&= yes;
  }
  await ctx.close();
  if (!ok) { console.error('有网站没登录成功，再运行一次 node browser.mjs login'); process.exit(1); }
  console.error('全部登录完成。');
}
