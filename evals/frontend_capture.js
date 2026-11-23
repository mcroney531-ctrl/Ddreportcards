// Capture GM Command's half of the chat prompt by running the real app.
//
// The app builds its system text (persona, roster, intel, style) and a
// per-message context block in the browser. Re-implementing that in Python
// would test a copy, not the app, so this loads the real index.html in
// headless Chromium under its production origin and calls the app's own
// functions: buildSystemPrompt() / fallbackSystemPrompt() and
// buildMessageContext(text) for each user message.
//
// Nothing is written anywhere: /intel, the access key and the news feeds are
// stubbed. Intel comes only from the synthetic fixture passed in.
//
// Usage:
//   node frontend_capture.js <gm_dir> <spec.json> <out.json>
// spec.json: { "sleeper": "live" | "<path to synthetic sleeper fixture>",
//              "intel": { <owner_id>: {name, breakdown, observations:[text]} },
//              "messages": ["user text", ...] }
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [GM_DIR, SPEC_PATH, OUT_PATH] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(SPEC_PATH, 'utf8'));
const PROD = 'https://gm-command.netlify.app';
const TYPES = { '.html': 'text/html', '.json': 'application/json', '.js': 'text/javascript', '.webp': 'image/webp', '.png': 'image/png', '.svg': 'image/svg+xml' };
const synthetic = spec.sleeper !== 'live' ? JSON.parse(fs.readFileSync(spec.sleeper, 'utf8')) : null;

function intelCache(intel) {
  // The app's gmc_intel_v2 cache shape, holding only confirmed (synced) values.
  const lm = {};
  let rev = 0;
  for (const [owner, rec] of Object.entries(intel || {})) {
    const obs = {};
    const ids = [];
    (rec.observations || []).forEach((text, i) => {
      const id = `00000000-0000-4000-8000-${String(owner).slice(-6).padStart(6, '0')}${String(i).padStart(6, '0')}`;
      obs[id] = { id, text, date_label: 'Nov 20', rev: ++rev, seq: 0 };
      ids.push(id);
    });
    lm[owner] = {
      name: rec.name ? { value: rec.name, rev: ++rev } : null, name_pending: null, name_rejected: null,
      breakdown: rec.breakdown ? { value: rec.breakdown, rev: ++rev, covers: ids } : null,
      breakdown_pending: null, breakdown_rejected: null, blocked: null, obs,
    };
  }
  return { schema: 2, last_synced_rev: rev, legacy_imported: true, last_export_at: 0, seq: 0, lm };
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ serviceWorkers: 'block' });
  await ctx.addInitScript((cache) => {
    localStorage.setItem('gmc_intel_v2', JSON.stringify(cache));
    localStorage.removeItem('gmCommandAccessKey');   // no key: the app never syncs or writes
  }, intelCache(spec.intel));
  await ctx.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.origin === PROD) {
      if (u.pathname.startsWith('/.netlify/functions/')) return route.fulfill({ status: 200, contentType: 'text/xml', body: '<rss><channel></channel></rss>' });
      let p = path.join(GM_DIR, decodeURIComponent(u.pathname));
      if (!fs.existsSync(p) || fs.statSync(p).isDirectory()) p = path.join(GM_DIR, 'index.html');
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(p)] || 'application/octet-stream', body: fs.readFileSync(p) });
    }
    if (u.hostname === 'api.sleeper.app') {
      if (!synthetic) return route.continue();
      let body = [];
      if (/\/v1\/user\//.test(u.pathname)) body = synthetic.user;
      else if (/\/rosters$/.test(u.pathname)) body = synthetic.rosters;
      else if (/\/users$/.test(u.pathname)) body = synthetic.users;
      else if (/\/players\/nfl$/.test(u.pathname)) body = synthetic.players;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    }
    return route.abort();   // backend, feeds, fonts: nothing leaves except Sleeper reads
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(PROD + '/index.html');
  await page.waitForFunction(() => typeof leaguemates !== 'undefined' && leaguemates.length > 0 && window.liveSystemPrompt, null, { timeout: 60000 });
  const out = await page.evaluate(async (messages) => ({
    liveSystemPrompt: window.liveSystemPrompt,
    fallbackSystemPrompt: fallbackSystemPrompt(await requireGmConfig()),
    contexts: messages.map((text) => {
      const c = buildMessageContext(text) || {};
      return { text, context: c.context || '', news: c.news || '' };
    }),
  }), spec.messages || []);
  out.page_errors = errors;
  fs.writeFileSync(OUT_PATH, JSON.stringify(out, null, 1));
  await browser.close();
  if (errors.length) { console.error('page errors:', errors); process.exit(1); }
})();
