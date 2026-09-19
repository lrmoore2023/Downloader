// Offline check for the Chrome extension's pure helpers against saved video
// pages: node tools/pmvx_check.js <dir with r34_video.html hmv_video.html pmvh_video.html paw_post2.html>
const fs = require('fs');
const path = require('path');

global.location = { hostname: '', pathname: '' };
global.document = { querySelectorAll: () => [], querySelector: () => null, getElementById: () => null };
global.window = { addEventListener() {} };
global.history = { pushState() {}, replaceState() {} };
global.setInterval = () => 0;
global.chrome = { runtime: { sendMessage() {} } };
const ext = require(path.join(__dirname, '..', 'chrome-extension', 'content.js'));

const dir = process.argv[2] || '.';
const read = (n) => fs.readFileSync(path.join(dir, n), 'utf8');
const site = (host) => ext.SITES.find((s) => s.host.test(host));

const cases = [
  ['rule34video.com', '/video/4444829/the-paris-song-hmv/', 'r34_video.html', 'R34 - 2026.06.20 - '],
  ['hmvmania.com', '/video/minahakuba-sana-chika-anko-x-lovesick-girls/', 'hmv_video.html', 'HMVMania - 2026.09.18 - '],
  ['pmvhaven.com', '/video/payback-lizzykink_69f6ea49fdfec82d0ab281aa', 'pmvh_video.html', 'PMVHaven - 2026.05.03 - '],
  ['pawchive.pw', '/patreon/user/147694273/post/133542612', 'paw_post2.html', 'Patreon - 2025.07.07 - '],
];
let fails = 0;
for (const [host, p, file, want] of cases) {
  const s = site(host);
  const m = s.match(p);
  const raw = s.dateFromHtml ? s.dateFromHtml(read(file), m) : '';
  const day = ext.isoDay(raw);
  const got = day ? `${s.code(m)} - ${day} - ` : `${s.code(m)} - `;
  const ok = got === want;
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${host}: ${JSON.stringify(got)}${ok ? '' : ' expected ' + JSON.stringify(want)}`);
}
// iwara: path match + code only (date comes from the API at runtime)
const iw = site('www.iwara.tv');
const im = iw.match('/video/5akn6caeqztwrjjyr/hmv-gimme-moretifa');
console.log(im && im[1] === '5akn6caeqztwrjjyr' && iw.code() === 'Iwara' ? 'PASS iwara match' : 'FAIL iwara match');
// non-video pages must not match
const neg = [['rule34video.com', '/members/2472537/'], ['hmvmania.com', '/author/minahakuba/'],
             ['pmvhaven.com', '/profile/68fdeb86b99aaf24a4c0e454'], ['pawchive.pw', '/patreon/user/147694273'], ['www.iwara.tv', '/profile/user1833289/videos']];
for (const [h, p] of neg) {
  const ok = !site(h).match(p);
  if (!ok) fails++;
  console.log(`${ok ? 'PASS' : 'FAIL'} no button on ${h}${p}`);
}
console.log(ext.isoDay('2026-05-04T13:47:43.703Z') === '2026.05.04' && ext.titleCase('patreon') === 'Patreon' ? 'PASS helpers' : 'FAIL helpers');
process.exit(fails ? 1 : 0);
