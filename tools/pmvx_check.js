// Offline check for the Chrome extension's pure helpers.
//
//   node tools/pmvx_check.js              — helper + adapter checks only
//   node tools/pmvx_check.js <dir>        — also check saved video pages
//                                           (r34_video.html hmv_video.html
//                                            pmvh_video.html paw_post2.html)
const fs = require('fs');
const path = require('path');

global.location = { hostname: '', pathname: '', origin: 'https://example.test' };
global.document = { querySelectorAll: () => [], querySelector: () => null, getElementById: () => null };
global.window = { addEventListener() {} };
global.history = { pushState() {}, replaceState() {} };
global.setInterval = () => 0;
global.chrome = { runtime: { sendMessage() {} } };
const ext = require(path.join(__dirname, '..', 'chrome-extension', 'content.js'));

const site = (host) => ext.SITES.find((s) => s.host.test(host));
let fails = 0;
function check(ok, label, extra) {
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label}${ok || extra === undefined ? '' : ` — got ${JSON.stringify(extra)}`}`);
}

// ── saved video pages (optional) ──────────────────────────────────

const dir = process.argv[2];
if (dir) {
  const read = (n) => fs.readFileSync(path.join(dir, n), 'utf8');
  const cases = [
    ['rule34video.com', '/video/4444829/the-paris-song-hmv/', 'r34_video.html', 'R34 - 2026.06.20 - '],
    ['hmvmania.com', '/video/minahakuba-sana-chika-anko-x-lovesick-girls/', 'hmv_video.html', 'HMVMania - 2026.09.18 - '],
    ['pmvhaven.com', '/video/payback-lizzykink_69f6ea49fdfec82d0ab281aa', 'pmvh_video.html', 'PMVHaven - 2026.05.03 - '],
    ['pawchive.pw', '/patreon/user/147694273/post/133542612', 'paw_post2.html', 'Patreon - 2025.07.07 - '],
  ];
  for (const [host, p, file, want] of cases) {
    const s = site(host);
    const m = s.match(p);
    const raw = s.dateFromHtml ? s.dateFromHtml(read(file), m) : '';
    const day = ext.isoDay(raw);
    const got = day ? `${s.code(m)} - ${day} - ` : `${s.code(m)} - `;
    check(got === want, `${host}: ${JSON.stringify(got)}`, got);
  }
}

// ── url matching ──────────────────────────────────────────────────

const iw = site('www.iwara.tv');
const im = iw.match('/video/5akn6caeqztwrjjyr/hmv-gimme-moretifa');
check(im && im[1] === '5akn6caeqztwrjjyr' && iw.code() === 'Iwara', 'iwara match');
const pm = site('pawchive.pw').match('/patreon/user/147694273/post/133542612');
// The user id and post id are captured too: the day counter needs the listing.
check(pm && pm[1] === 'patreon' && pm[2] === '147694273' && pm[3] === '133542612',
      'pawchive match captures service/user/post', pm && pm.slice(1, 4));
for (const [h, p] of [['rule34video.com', '/members/2472537/'], ['hmvmania.com', '/author/minahakuba/'],
                      ['pmvhaven.com', '/profile/68fdeb86b99aaf24a4c0e454'],
                      ['pawchive.pw', '/patreon/user/147694273'], ['www.iwara.tv', '/profile/user1833289/videos']]) {
  check(!site(h).match(p), `no button on ${h}${p}`);
}

// ── helpers ───────────────────────────────────────────────────────

check(ext.isoDay('2026-05-04T13:47:43.703Z') === '2026.05.04' && ext.titleCase('patreon') === 'Patreon',
      'isoDay / titleCase');
check(ext.pad2(2) === '02' && ext.pad2(12) === '12', 'pad2');
// The site's own wall clock, offset kept — the PMV tab takes the day the same way.
check(ext.rfc822Iso('Thu, 18 Sep 2026 14:03:00 +0900') === '2026-09-18T14:03:00+09:00',
      'rfc822Iso keeps the feed offset', ext.rfc822Iso('Thu, 18 Sep 2026 14:03:00 +0900'));
check(ext.rfc822Iso('Thu, 18 Sep 2026 14:03:00 GMT') === '2026-09-18T14:03:00+00:00', 'rfc822Iso GMT');
check(ext.rfc822Iso('nonsense') === '', 'rfc822Iso rejects junk');

const BLOCK = '<div id="list_videos_uploaded_videos_items">'
  + '<div class="item" data-video-card-id="4593299"><a class="th" href="https://rule34video.com/video/4593299/a/">A</a></div>'
  + '<div class="item" data-video-card-id="4567685"><a class="th" href="https://rule34video.com/video/4567685/b/">B</a></div>'
  + '</div>';
const cards = ext.r34Cards(BLOCK);
check(cards.length === 2 && cards[0].id === 4593299 && /4567685/.test(cards[1].url), 'r34Cards', cards);

const FEED = '<rss><channel><title>Author &#8211; Page 2 of 7</title>'
  + '<item><title><![CDATA[[Mina] One]]></title><link>https://hmvmania.com/video/one/</link>'
  + '<guid isPermaLink="false">https://hmvmania.com/?post_type=video&#038;p=811</guid>'
  + '<pubDate>Thu, 18 Sep 2026 14:03:00 +0900</pubDate></item>'
  + '<item><link>https://hmvmania.com/video/two/</link>'
  + '<guid>https://hmvmania.com/?p=812</guid>'
  + '<pubDate>Thu, 18 Sep 2026 09:00:00 +0900</pubDate></item>'
  + '</channel></rss>';
const feed = ext.parseFeed(FEED);
check(feed.pages === 7 && !feed.notFound && feed.items.length === 2
      && feed.items[0].id === '811' && feed.items[1].id === '812'
      && ext.isoDay(feed.items[0].date) === '2026.09.18', 'parseFeed', feed);
check(ext.parseFeed('<rss><channel><title>Page not found</title></channel></rss>').notFound,
      'parseFeed past the end');

// ── day ordering ──────────────────────────────────────────────────

// A timestamp decides; an equal timestamp (or a site with none) falls to the
// id, compared as a number so 9 sorts before 10 — as the PMV tab does.
check(ext.cmpUpload({ id: '2', date: '2026-07-21T18:00:00Z' }, { id: '1', date: '2026-07-21T19:00:00Z' }) < 0,
      'cmpUpload prefers the timestamp');
check(ext.cmpUpload({ id: '9', date: '2026-01-03' }, { id: '10', date: '2026-01-03' }) < 0,
      'cmpUpload falls back to a numeric id');

const DAY = [
  { id: '3', date: '2026-07-21T19:43:21+00:00' },
  { id: '1', date: '2026-07-21T18:20:26+00:00' },
  { id: '2', date: '2026-07-21T19:30:37+00:00' },
];
check(ext.rankOnDay(DAY, (e) => e.id === '1') === 1, 'rankOnDay oldest first');
check(ext.rankOnDay(DAY, (e) => e.id === '3') === 3, 'rankOnDay newest last');
check(ext.rankOnDay(DAY, (e) => e.id === 'nope') === null, 'rankOnDay: video not in the day');
check(ext.rankOnDay([DAY[0]], (e) => e.id === '3') === null, 'rankOnDay: alone that day → no counter');

// entriesOnDay walks a newest-first, date-ordered listing. Two videos share
// 06-02, which straddles a page boundary.
const PAGES = [
  [{ id: 'a', date: '2026-06-04' }, { id: 'b', date: '2026-06-03' }],
  [{ id: 'c', date: '2026-06-02T20:00:00Z' }, { id: 'd', date: '2026-06-02T08:00:00Z' }],
  [{ id: 'e', date: '2026-06-01' }, { id: 'f', date: '2026-05-30' }],
];
const STRADDLE = [
  [{ id: 'a', date: '2026-06-04' }, { id: 'c', date: '2026-06-02T20:00:00Z' }],
  [{ id: 'd', date: '2026-06-02T08:00:00Z' }, { id: 'e', date: '2026-06-01' }],
];
(async () => {
  const hits = { n: 0 };
  const pager = (pages) => async (n) => { hits.n++; return pages[n] || null; };
  const ids = (rows) => rows.map((r) => r.id).sort().join('');
  check(ids(await ext.entriesOnDay(pager(PAGES), PAGES.length, '2026.06.02')) === 'cd',
        'entriesOnDay bisects to the day');
  check(ids(await ext.entriesOnDay(pager(STRADDLE), 0, '2026.06.02')) === 'cd',
        'entriesOnDay spans a page boundary without a page count');
  check((await ext.entriesOnDay(pager(PAGES), PAGES.length, '2026.06.03')).length === 1,
        'entriesOnDay: a lone video on its day');
  check((await ext.entriesOnDay(pager(PAGES), PAGES.length, '2026.07.09')).length === 0,
        'entriesOnDay: day not in the listing');

  // rule34video: no dates in the listing, so the card is found by bisecting on
  // the video id and the day's extent is read from neighbouring video pages.
  // Ids ascend with upload time; 4183840 and 4183868 share 2026-01-03.
  const R34 = { 4185676: '2026-01-04', 4184960: '2026-01-04', 4183868: '2026-01-03',
                4183840: '2026-01-03', 4180000: '2026-01-02', 4179000: '2026-01-01',
                4178000: '2025-12-30', 4177000: '2025-12-29' };
  const R34_IDS = Object.keys(R34).map(Number).sort((a, b) => b - a);   // newest first
  let reqs = 0;
  global.fetch = async (url) => {
    reqs++;
    const block = /from_videos=(\d+)/.exec(url);
    if (block) {
      const page = R34_IDS.slice((Number(block[1]) - 1) * 2, Number(block[1]) * 2);   // 2 per page
      if (!page.length) return { ok: false };
      return { ok: true, text: async () => '<div id="list_videos_uploaded_videos_items">' + page.map(
        (id) => `<div class="item" data-video-card-id="${id}"><a class="th" href="/video/${id}/x/">x</a></div>`).join('') + '</div>' };
    }
    const vid = /\/video\/(\d+)\//.exec(url);
    if (vid && R34[vid[1]]) return { ok: true, text: async () => `{"uploadDate": "${R34[vid[1]]}"}` };
    return { ok: false };
  };
  check((await ext.r34DaySeq('3826231', '4183840', '2026.01.03')) === 1, 'r34DaySeq: first of the day');
  check((await ext.r34DaySeq('3826231', '4183868', '2026.01.03')) === 2, 'r34DaySeq: second of the day');
  check((await ext.r34DaySeq('3826231', '4180000', '2026.01.02')) === null, 'r34DaySeq: alone that day');
  check((await ext.r34DaySeq('3826231', '9999999', '2026.01.03')) === null, 'r34DaySeq: id not listed');
  check(reqs < 40, `r34DaySeq stays cheap (${reqs} requests for 4 lookups)`, reqs);
  console.log(fails ? `\n${fails} check(s) failed.` : '\nAll checks passed.');
  process.exit(fails ? 1 : 0);
})();
