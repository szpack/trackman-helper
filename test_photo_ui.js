'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

const html = readFileSync(join(__dirname, 'dashboard.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const response = data => ({ ok: true, json: async () => data });

function video(seq, revision = 'r1', status = 'ready') {
  return {
    seq, revision, status,
    frames: Array.from({ length: 8 }, (_, i) => ({
      name: i === 6 ? 'impact_mark' : `stage_${i}`, label: `阶段候选 ${i + 1}`,
      url: `/photos/${seq}/${revision}/${i}.jpg`, frame_index_zero_based: i * 10,
      actual_container_seconds: i / 10, stage_status: 'candidate', confidence: 0.6,
    })),
    calibration: { status: 'candidate', impact_frame_index: 60, impact_container_seconds: 0.6,
      confidence: 0.8, candidates: [{ url: `/photos/${seq}/${revision}/6.jpg`,
        frame_index_zero_based: 60, actual_container_seconds: 0.6 }] },
  };
}

async function ui(route = () => response({}), aiConfig = { configured: false, use_ai: true, clubs: [] }, modeRoute = null) {
  const elements = new Map();
  function element(id = '', tagName = 'DIV') {
    return { id, tagName, innerHTML: '', textContent: '', value: '', hidden: false, disabled: false,
      checked: false, children: [], selectedOptions: [{ textContent: '自动' }], dataset: {},
      classList: { values: new Set(), add(name) { this.values.add(name); },
        remove(name) { this.values.delete(name); }, contains(name) { return this.values.has(name); } },
      addEventListener(name, fn) { this[`on_${name}`] = fn; },
      replaceChildren(...children) { this.children = children; this.firstChild = children[0] || null; },
    };
  }
  for (const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = element(match[1]);
    node.hidden = /\bhidden(?:\s|>|=)/.test(match[0]);
    elements.set(match[1], node);
  }
  const documentEvents = {}, events = {}, timers = new Map(), requests = [];
  let nextTimer = 0;
  const context = vm.createContext({
    URL, AbortController, location: { href: 'http://dashboard.local/', origin: 'http://dashboard.local' },
    localStorage: { getItem() { return null; }, setItem() {} },
    document: {
      getElementById(id) { assert.ok(elements.has(id), `DOM 挂载位不存在：${id}`); return elements.get(id); },
      createElement(tag) { return element('', tag.toUpperCase()); },
      querySelectorAll() { return []; },
      addEventListener(name, fn) { documentEvents[name] = fn; },
    },
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    EventSource: class { addEventListener(name, fn) { events[name] = fn; } },
    fetch(url, options = {}) {
      requests.push({ url, options });
      if (url === '/api/ai') return Promise.resolve(response(aiConfig));
      if (url === '/api/ai-photo-mode') {
        if (modeRoute) return Promise.resolve(modeRoute(url, options));
        aiConfig = { ...aiConfig, include_photos: JSON.parse(options.body).include_photos };
        return Promise.resolve(response({ ...aiConfig, ok: true }));
      }
      if (url === '/api/status') return Promise.resolve(response({}));
      // 所有请求仅由内存路由返回；不接触真实网络、AI、文件或后端。
      return Promise.resolve(route(url, options));
    },
  });
  vm.runInContext(source, context, { filename: 'dashboard.html', timeout: 1000 });
  await flush();
  const run = code => vm.runInContext(code, context, { timeout: 1000 });
  const select = seq => run(`shots = [1, 2, 3].map(seq => ({ seq, ts: 0, m: {} })); aiSeq = ${seq}; paintAI();`);
  const emit = (name, data) => events[name]({ data: JSON.stringify(data) });
  const click = (id, dataset) => elements.get(id).onclick({ target: {
    closest() { return { dataset, disabled: false }; },
  } });
  return { elements, requests, timers, documentEvents, run, select, emit, click,
    async poll() {
      const item = [...timers].find(([, timer]) => timer.ms === 1500);
      if (!item) return false;
      timers.delete(item[0]); item[1].fn(); await flush(); return true;
    },
  };
}

test('语法、独立挂载位、桌面四列两行、手机横滚及 contain；保留 stale 分支', async () => {
  assert.doesNotThrow(() => new vm.Script(source));
  assert.ok(html.indexOf('id="aiPhotos"') < html.indexOf('id="aiBody"'));
  assert.match(html, /\.photoGrid\{display:grid;grid-template-columns:repeat\(4,minmax\(0,1fr\)\)/);
  assert.match(html, /\.photoGrid\{min-width:760px\}/);
  assert.match(html, /\.photoScroll\{overflow-x:auto/);
  assert.match(html, /\.photoImage img\{[^}]*object-fit:contain/);
  const mock = await ui();
  mock.run('normState = { on: true, conf: "high", stale: true };');
  assert.equal(mock.run('normKey()'), 'soft');
  const photos = mock.elements.get('aiPhotos').innerHTML;
  assert.equal((photos.match(/class="photoTile"/g) || []).length, 8);
  assert.doesNotMatch(photos, /<img /);
  assert.equal(mock.requests.filter(r => r.url.startsWith('/api/video')).length, 0);
});

test('切历史后，旧 GET、其他杆 SSE 和迟到分析不能串图或抢选中杆', async () => {
  let resolveFirst;
  const mock = await ui(url => {
    if (url === '/api/video?seq=1') return new Promise(resolve => { resolveFirst = resolve; });
    if (url === '/api/video?seq=2') return response(video(2));
    throw new Error(`意外请求 ${url}`);
  });
  mock.select(1); mock.select(2); await flush();
  resolveFirst(response(video(1))); await flush();
  mock.emit('video', video(3));
  mock.emit('analysis', { seq: 3, summary: '迟到的另一杆分析' });
  assert.equal(mock.run('aiSeq'), 2);
  const photos = mock.elements.get('aiPhotos').innerHTML;
  assert.doesNotMatch(photos, /photoCaption|阶段状态|置信度|容器时间/);
  assert.match(photos, /\/photos\/2\/r1/);
  assert.doesNotMatch(photos, /\/photos\/[13]\//);
  assert.equal(mock.requests.find(r => r.url === '/api/video?seq=1').options.signal.aborted, true);
  mock.elements.get('tbody').onclick({ target: { closest() { return { dataset: { seq: '3' } }; } } });
  assert.equal(mock.run('aiSeq'), 3);
  assert.match(mock.elements.get('aiPhotos').innerHTML, /\/photos\/3\/r1/);
});

test('SSE 就绪先到可覆盖 pending GET，missing 杆只保留八格占位', async () => {
  let resolveGet;
  const mock = await ui(url => url.includes('seq=1')
    ? new Promise(resolve => { resolveGet = resolve; }) : response(video(2, 'r1', 'missing')));
  mock.select(1);
  mock.emit('video', video(1, 'r2'));
  resolveGet(response(video(1, 'r1', 'pending'))); await flush();
  assert.equal(mock.run('videos[1].data.revision'), 'r2');
  assert.equal(mock.run('videos[1].data.status'), 'ready');
  mock.select(2); await flush();
  const photos = mock.elements.get('aiPhotos').innerHTML;
  assert.match(photos, /本杆暂无录像/);
  assert.equal((photos.match(/class="photoTile"/g) || []).length, 8);
  assert.doesNotMatch(photos, /<img /);
});

test('pending 最多二十次且重复 paintAI 不重启；重试 POST 仅发送 seq', async () => {
  const mock = await ui((url, options) => response(video(options.body ? JSON.parse(options.body).seq : 1, 'r1', 'pending')));
  mock.select(1); await flush();
  for (let i = 0; i < 30; i++) if (!await mock.poll()) break;
  assert.equal(mock.requests.filter(r => r.url === '/api/video?seq=1').length, 20);
  assert.equal(mock.run('videos[1].stopped'), true);
  mock.run('paintAI(); paintAI();'); await flush();
  assert.equal(await mock.poll(), false);
  assert.match(mock.elements.get('aiPhotos').innerHTML, /已停止自动轮询/);
  mock.click('aiPhotos', { action: 'prepare' }); await flush();
  const post = mock.requests.find(r => r.url === '/api/video-prepare');
  assert.equal(post.options.method, 'POST');
  assert.deepEqual(JSON.parse(post.options.body), { seq: 1 });
  assert.equal(mock.run('videos[1].stopped'), false);
  mock.emit('video', video(1, 'r2'));
  assert.equal(await mock.poll(), false);
});

test('照片响应杆号不匹配和请求失败会停止，不把错杆图片挂进当前', async () => {
  const mock = await ui(() => response(video(2)));
  mock.select(1); await flush();
  assert.equal(mock.run('videos[1].data.status'), 'error');
  assert.equal(mock.run('videos[1].stopped'), true);
  assert.match(mock.elements.get('aiPhotos').innerHTML, /杆号不匹配/);
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /<img /);
  assert.equal(await mock.poll(), false);
});

test('放大与校准候选、左右逐帧、确认字段；校准保留已保存的全局照片模式，不额外重分析', async () => {
  const mock = await ui(url => response(url === '/api/video-calibrate' ? video(1, 'r2', 'pending') : video(1)));
  mock.select(1); await flush();
  mock.elements.get('aiIncludePhotos').checked = true;
  await mock.elements.get('aiIncludePhotos').onchange();
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /置信度|input_mode|photoCaption/);
  mock.click('aiPhotos', { photo: '6', seq: '1' });
  assert.equal(mock.elements.get('photoModal').hidden, false);
  assert.match(mock.elements.get('photoTitle').textContent, /照片 7/);
  assert.match(mock.elements.get('photoPreview').firstChild.src, /\/photos\/1\/r1\/6/);
  mock.click('aiPhotos', { action: 'calibrate' });
  assert.match(mock.elements.get('calCandidates').innerHTML, /候选 1 · 帧 60/);
  mock.elements.get('calPreview').firstChild.onload();
  mock.elements.get('calNext').onclick();
  let preview = mock.elements.get('calPreview').firstChild;
  assert.equal(preview.src, '/api/video-frame?seq=1&revision=r1&frame_index=61');
  assert.equal(mock.elements.get('calConfirm').disabled, true);
  preview.onload();
  mock.elements.get('calPrev').onclick();
  preview = mock.elements.get('calPreview').firstChild;
  assert.match(preview.src, /frame_index=60$/); preview.onload();
  mock.run('analyses[1] = { summary: "旧分析" };');
  await mock.elements.get('calConfirm').onclick(); await flush();
  const post = mock.requests.find(r => r.url === '/api/video-calibrate');
  assert.deepEqual(JSON.parse(post.options.body), { seq: 1, revision: 'r1', frame_index: 60 });
  assert.equal(mock.run('analyses[1]'), undefined);
  assert.equal(mock.elements.get('calModal').hidden, true);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
  const ready = video(1, 'r2'); ready.calibration.status = 'manual_confirmed';
  mock.emit('video', ready);
  assert.equal(mock.run('videos[1].data.calibration.status'), 'manual_confirmed');
  mock.emit('analysis', { seq: 1, video_revision: 'r2', summary: '校准后的文本结果', input_mode: 'text' });
  assert.equal(mock.elements.get('aiIncludePhotos').checked, true);
  assert.equal(mock.run('aiInfo.include_photos'), true);
  assert.equal(mock.elements.get('aiPhotoModeLabel').classList.contains('isOn'), true);
  const modes = mock.requests.filter(r => r.url === '/api/ai-photo-mode');
  assert.equal(modes.length, 1);
  assert.deepEqual(JSON.parse(modes[0].options.body), { include_photos: true });
  assert.match(mock.elements.get('aiBody').innerHTML, /input_mode=text.*图片未参与/);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
  assert.match(html, /是否发送照片沿用全局“分析包含照片”开关/);
});

test('逐帧旧加载不覆盖新帧、失败禁止确认、revision 更新关闭旧校准', async () => {
  const mock = await ui(() => response(video(1)));
  mock.select(1); await flush(); mock.run('openCalibration();');
  const old = mock.elements.get('calPreview').firstChild;
  mock.run('previewCalibration(61);');
  const latest = mock.elements.get('calPreview').firstChild;
  old.onload();
  assert.equal(mock.elements.get('calConfirm').disabled, true);
  latest.onerror();
  assert.equal(mock.elements.get('calConfirm').disabled, true);
  mock.run('previewCalibration(0);'); mock.elements.get('calPreview').firstChild.onload();
  assert.equal(mock.elements.get('calPrev').disabled, true);
  mock.emit('video', video(1, 'r2'));
  assert.equal(mock.elements.get('calModal').hidden, true);
  assert.equal(mock.run('calibrationView'), null);
});

test('summary 后三大结论前显示照片证据编号、置信度及定性边界，文本模式如实标注', async () => {
  const mock = await ui(() => response(video(1)));
  mock.select(1); await flush();
  const analysis = { seq: 1, summary: '摘要', source: 'ai', input_mode: 'vision', image_count: 8,
    video_revision: 'r1', visual_findings: [{ title: '<观察>', detail: '定性迹象', evidence_frames: [2, 7], confidence: 0.7 }] };
  mock.emit('analysis', analysis);
  const body = mock.elements.get('aiBody').innerHTML;
  assert.ok(body.indexOf('aiSummary') < body.indexOf('visualEvidence'));
  assert.ok(body.indexOf('visualEvidence') < body.indexOf('aiCols'));
  assert.match(body, /照片证据：照片 2、照片 7/);
  assert.match(body, /非测量精度/);
  assert.match(body, /&lt;观察&gt;/);
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /本次视觉分析|input_mode|photoNote/);
  mock.emit('analysis', { ...analysis, input_mode: 'text', visual_findings: [] });
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /仅文本分析|photoCaption/);
  assert.match(mock.elements.get('aiPhotos').innerHTML, /\/photos\/1\/r1/);
  mock.emit('video', video(1, 'r2'));
  assert.doesNotMatch(mock.elements.get('aiBody').innerHTML, /摘要/);
});

test('校准 SSE 和新分析先到时，迟到 POST 不回退 revision 或删除新分析', async () => {
  let resolvePost;
  const mock = await ui(url => url === '/api/video-calibrate'
    ? new Promise(resolve => { resolvePost = resolve; }) : response(video(1)));
  mock.select(1); await flush(); mock.run('openCalibration();');
  mock.elements.get('calPreview').firstChild.onload();
  mock.run('videos[1].attempts = 20;');
  const confirming = mock.elements.get('calConfirm').onclick();
  mock.emit('video', video(1, 'r2', 'pending'));
  assert.equal(mock.run('videos[1].stopped'), false);
  assert.equal(mock.run('videos[1].attempts'), 0);
  mock.emit('video', video(1, 'r2'));
  mock.emit('analysis', { seq: 1, video_revision: 'r2', summary: '新分析', source: 'ai' });
  resolvePost(response(video(1, 'r2', 'pending')));
  await confirming; await flush();
  assert.equal(mock.run('videos[1].data.status'), 'ready');
  assert.equal(mock.run('analyses[1].summary'), '新分析');
  assert.equal(await mock.poll(), false);
});

test('GET 超时终止；重连重新读取当前历史杆而不抢回最新杆', async () => {
  const mock = await ui((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      const error = new Error('超时'); error.name = 'AbortError'; reject(error);
    });
  }));
  mock.select(1);
  const [id, timeout] = [...mock.timers].find(([, timer]) => timer.ms === 8000);
  mock.timers.delete(id); timeout.fn(); await flush();
  assert.equal(mock.run('videos[1].stopped'), true);
  assert.match(mock.elements.get('aiPhotos').innerHTML, /请求超时/);
  assert.equal(await mock.poll(), false);
  mock.emit('hello', { shots: [1, 2].map(seq => ({ seq, ts: 0, m: {} })), analyses: {} });
  assert.equal(mock.run('aiSeq'), 1);
  assert.equal(mock.requests.filter(r => r.url === '/api/video?seq=1').length, 2);
});

test('视觉设置独立，留空 Key 按契约提交，披露数据发送与鉴权；拒绝非本地图片 URL', async () => {
  const mock = await ui(() => response({ ok: true }));
  mock.run('aiInfo = { configured: true, base_url: "text-api", model: "text-model", vision_enabled: true, vision_configured: true, vision_base_url: "vision-api", vision_model: "vision-model" };');
  mock.elements.get('aiCfgBtn').onclick();
  assert.equal(mock.elements.get('cfgVisionEnabled').checked, true);
  assert.equal(mock.elements.get('cfgVisionUrl').value, 'vision-api');
  await mock.run('saveAiCfg()');
  const payload = JSON.parse(mock.requests.find(r => r.url === '/api/ai-config').options.body);
  assert.deepEqual(payload, { base_url: 'text-api', model: 'text-model', api_key: '',
    vision_enabled: true, vision_base_url: 'vision-api', vision_model: 'vision-model', vision_api_key: '' });
  assert.match(html, /全局“分析包含照片”开启，且视觉能力启用、配置就绪时，后续挥杆会自动将照片及击球参数发送至视觉接口/);
  assert.match(html, /保存设置后的当前杆重分析也沿用全局开关/);
  assert.match(html, /请求鉴权时会发送给对应服务商/);
  assert.doesNotMatch(html, /不会上传到任何第三方/);
  assert.equal(mock.run('photoUrl("javascript:alert(1)")'), '');
  assert.equal(mock.run('photoUrl("https://untrusted.invalid/a.jpg")'), '');
});

test('全局照片模式缺省关闭；保存开启后 hello 重连、切杆和新杆均保留，不自动重分析', async () => {
  const input = html.match(/<input\b[^>]*id="aiIncludePhotos"[^>]*>/)[0];
  assert.match(input, /type="checkbox"/);
  assert.doesNotMatch(input, /\bchecked\b/);
  assert.match(html, /for="aiIncludePhotos"[^>]*>[\s\S]*?分析包含照片（全局）<\/label>/);
  const mock = await ui(url => response(video(Number(new URL(url, 'http://dashboard.local').searchParams.get('seq')))),
    { configured: true, use_ai: true, clubs: [], vision_enabled: true, vision_configured: true });
  const checkbox = mock.elements.get('aiIncludePhotos');
  assert.equal(checkbox.checked, false);
  mock.emit('hello', { shots: [{ seq: 1, ts: 0, m: {} }], analyses: {} }); await flush();
  assert.equal(checkbox.checked, false);
  checkbox.checked = true; await checkbox.onchange();
  assert.equal(mock.run('aiInfo.include_photos'), true);
  for (const seq of [1, 2]) {
    mock.emit('hello', { shots: [{ seq, ts: 0, m: {} }], analyses: {} }); await flush();
    assert.equal(checkbox.checked, true);
  }
  mock.emit('shot', { seq: 3, ts: 0, m: {} }); await flush();
  assert.equal(checkbox.checked, true);
  assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
});

test('切换全局照片模式仅 POST 保存 flag，更新提示但不触发分析或改变照片展示', async () => {
  const mock = await ui(() => response(video(1)));
  mock.select(1); await flush();
  const checkbox = mock.elements.get('aiIncludePhotos');
  const photos = mock.elements.get('aiPhotos').innerHTML, count = mock.requests.length;
  for (const checked of [true, false]) {
    checkbox.checked = checked; await checkbox.onchange();
    assert.equal(checkbox.checked, checked);
    assert.equal(mock.run('aiInfo.include_photos'), checked);
    const request = mock.requests.at(-1);
    assert.equal(request.url, '/api/ai-photo-mode');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(request.options.body), { include_photos: checked });
    assert.match(mock.elements.get('aiPhotoConsent').textContent, checked ? /全局照片分析已开启/ : /全局照片分析已关闭/);
    if (checked) {
      assert.match(mock.elements.get('aiPhotoConsent').textContent, /请到 AI 设置配置/);
      assert.match(mock.elements.get('aiPhotoConsent').textContent, /已有结果不自动重跑/);
    } else {
      assert.doesNotMatch(mock.elements.get('aiPhotoConsent').textContent, /请到 AI 设置配置/);
    }
    assert.equal(mock.elements.get('aiPhotos').innerHTML, photos);
  }
  assert.equal(mock.requests.length, count + 2);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
});

test('主动重新分析省略 include_photos 交给后端全局模式，视觉能力不能代替或清除全局开关', async () => {
  for (const [enabled, configured] of [[false, false], [false, true], [true, false], [true, true]]) {
    const mock = await ui(url => response(url === '/api/analyze' ? {} : video(1)),
      { configured: true, use_ai: true, clubs: [], vision_enabled: enabled, vision_configured: configured });
    mock.select(1); await flush();
    mock.elements.get('aiClub').value = '7i';
    const checkbox = mock.elements.get('aiIncludePhotos');
    for (const checked of [false, true, true, false]) {
      checkbox.checked = checked; await checkbox.onchange();
      const before = mock.requests.filter(r => r.url === '/api/analyze').length;
      mock.elements.get('aiRedo').onclick(); await flush();
      const posts = mock.requests.filter(r => r.url === '/api/analyze');
      assert.equal(posts.length, before + 1);
      assert.equal(posts.at(-1).options.method, 'POST');
      assert.deepEqual(JSON.parse(posts.at(-1).options.body), { seq: 1, club: '7i' });
      assert.equal(checkbox.checked, checked);
      assert.equal(mock.run('aiInfo.include_photos'), checked);
    }
  }
});

test('改球杆与主动重分析均沿用全局模式，resetClubPick 仅清除当前球杆', async () => {
  const mock = await ui(url => response(url === '/api/analyze' ? {} : video(1)));
  mock.select(1); await flush();
  const checkbox = mock.elements.get('aiIncludePhotos'), club = mock.elements.get('aiClub');
  checkbox.checked = true; await checkbox.onchange();
  club.value = '7i'; club.onchange({ target: club }); await flush();
  let posts = mock.requests.filter(r => r.url === '/api/analyze');
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].options.body), { seq: 1, club: '7i' });
  assert.equal(checkbox.checked, true);
  mock.elements.get('aiRedo').onclick(); await flush();
  posts = mock.requests.filter(r => r.url === '/api/analyze');
  assert.deepEqual(JSON.parse(posts[1].options.body), { seq: 1, club: '7i' });
  mock.run('resetClubPick();');
  assert.equal(checkbox.checked, true);
  assert.equal(mock.run('aiInfo.include_photos'), true);
  assert.equal(club.value, '');
  assert.equal(mock.run('clubOverride'), '');
  assert.equal(mock.requests.filter(r => r.url === '/api/analyze').length, 2);
  assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
});

test('切历史及新杆不清除全局模式；历史首次请求省略 include_photos，已有结果不重跑且不串杆', async () => {
  const mock = await ui(url => response(url === '/api/analyze' ? {} : video(Number(new URL(url, 'http://dashboard.local').searchParams.get('seq')))));
  mock.select(1); await flush();
  const checkbox = mock.elements.get('aiIncludePhotos');
  mock.elements.get('aiClub').value = '7i';
  mock.run('clubOverride = "7i";');
  checkbox.checked = true; await checkbox.onchange();
  mock.click('tbody', { seq: '2' }); await flush();
  assert.equal(mock.run('aiSeq'), 2);
  assert.equal(checkbox.checked, true);
  assert.equal(mock.elements.get('aiClub').value, '');
  assert.equal(mock.run('clubOverride'), '');
  let posts = mock.requests.filter(r => r.url === '/api/analyze');
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].options.body), { seq: 2, club: null });
  assert.match(mock.elements.get('aiPhotos').innerHTML, /\/photos\/2\/r1/);
  mock.click('tbody', { seq: '2' }); await flush();
  posts = mock.requests.filter(r => r.url === '/api/analyze');
  assert.equal(posts.length, 2);
  assert.deepEqual(JSON.parse(posts[1].options.body), { seq: 2, club: null });
  mock.emit('analysis', { seq: 2, summary: '历史已有结果', input_mode: 'text' });
  mock.click('tbody', { seq: '2' }); await flush();
  assert.equal(mock.requests.filter(r => r.url === '/api/analyze').length, 2);
  assert.equal(checkbox.checked, true);
  mock.emit('shot', { seq: 3, ts: 0, m: {} }); await flush();
  assert.equal(checkbox.checked, true);
  assert.equal(mock.requests.filter(r => r.url === '/api/analyze').length, 2);
  mock.elements.get('aiRedo').onclick(); await flush();
  posts = mock.requests.filter(r => r.url === '/api/analyze');
  assert.deepEqual(JSON.parse(posts[2].options.body), { seq: 3, club: null });
  assert.match(mock.elements.get('aiPhotos').innerHTML, /\/photos\/3\/r1/);
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /\/photos\/[12]\//);
  assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
});

test('保存视觉配置后当前杆沿用全局模式；测试按钮仍只测试文本，参与提示以 input_mode 为准', async () => {
  assert.match(html, /id="cfgTest"[^>]*>测试文本接口<\/button>/);
  for (const checked of [false, true]) {
    const mock = await ui(url => response(url === '/api/video?seq=1' ? video(1) : { ok: true, model: 'dummy-text-model', summary: '文本测试' }),
      { configured: true, use_ai: true, clubs: [], vision_enabled: true, vision_configured: true });
    mock.select(1); await flush();
    const checkbox = mock.elements.get('aiIncludePhotos');
    checkbox.checked = checked; await checkbox.onchange();
    mock.elements.get('aiCfgBtn').onclick();
    await mock.elements.get('cfgSave').onclick(); await flush();
    const posts = mock.requests.filter(r => r.url === '/api/analyze');
    assert.equal(posts.length, 1);
    assert.deepEqual(JSON.parse(posts[0].options.body), { seq: 1, club: null });
    assert.equal(checkbox.checked, checked);
    assert.equal(mock.run('aiInfo.include_photos'), checked);
    assert.equal(mock.elements.get('aiModal').hidden, true);
    for (const input_mode of ['text', 'rules', 'vision', undefined, '<未知>']) {
      const result = { seq: 1, summary: '分析结果', source: 'ai', input_mode, image_count: 8 };
      mock.emit('analysis', result);
      const expected = input_mode === 'vision' ? /input_mode=vision.*图片已参与/
        : ['text', 'rules'].includes(input_mode) ? /input_mode=(text|rules).*图片未参与/
        : /input_mode=(未返回|&lt;未知&gt;).*图片是否参与未确认/;
      assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /input_mode|photoCaption|photoNote/);
      assert.match(mock.elements.get('aiBody').innerHTML, expected);
      const photos = mock.elements.get('aiPhotos').innerHTML;
      checkbox.checked = !checkbox.checked; await checkbox.onchange();
      assert.equal(mock.elements.get('aiPhotos').innerHTML, photos);
    }
    const globalMode = checkbox.checked;
    mock.elements.get('aiCfgBtn').onclick();
    const before = mock.requests.length;
    await mock.elements.get('cfgTest').onclick();
    const testRequests = mock.requests.slice(before);
    assert.deepEqual(testRequests.map(r => r.url), ['/api/ai-config', '/api/ai-test']);
    assert.equal(testRequests[1].options.method, 'POST');
    assert.equal(testRequests[1].options.body, undefined);
    assert.equal(Object.hasOwn(JSON.parse(testRequests[0].options.body), 'include_photos'), false);
    assert.match(mock.elements.get('cfgMsg').textContent, /文本接口正常（未测试视觉接口）/);
    assert.equal(checkbox.checked, globalMode);
    assert.equal(mock.run('aiInfo.include_photos'), globalMode);
    assert.equal(mock.requests.filter(r => r.url === '/api/analyze').length, 1);
  }
});

test('刷新按服务端 true 恢复勾选及 isOn 黄底白字，关闭后移除高亮', async () => {
  const css = html.match(/\.aiTools\s+\.photoModeLabel\.isOn\{([^}]*)\}/);
  assert.ok(css, '开启样式必须绑定照片模式标签的 isOn 类');
  assert.match(css[1], /(?:^|;)background:\s*#bd8c00\s*(?:;|$)/);
  assert.match(css[1], /(?:^|;)color:\s*#fff\s*(?:;|$)/);
  const saved = await ui();
  saved.elements.get('aiIncludePhotos').checked = true;
  await saved.elements.get('aiIncludePhotos').onchange();
  const serverConfig = JSON.parse(saved.run('JSON.stringify(aiInfo)'));
  const refreshed = await ui(undefined, serverConfig);
  const checkbox = refreshed.elements.get('aiIncludePhotos');
  const label = refreshed.elements.get('aiPhotoModeLabel');
  assert.equal(checkbox.checked, true);
  assert.equal(label.classList.contains('isOn'), true);
  assert.equal(refreshed.run('aiInfo.include_photos'), true);
  assert.equal(refreshed.requests.some(r => r.url === '/api/ai-photo-mode' || r.url === '/api/analyze'), false);
  checkbox.checked = false; await checkbox.onchange();
  assert.equal(label.classList.contains('isOn'), false);
  assert.equal(checkbox.checked, false);
});

test('mode API 失败或旧服务不确认时重读服务端状态，复原勾选并提示，不能冒充保存成功', async () => {
  const failures = [
    ['HTTP 500', () => ({ ok: false, status: 500 })],
    ['旧服务 HTTP 404', () => ({ ok: false, status: 404 })],
    ['网络失败', () => Promise.reject(new Error('模拟断网'))],
    ['业务拒绝', () => response({ ok: false, include_photos: true })],
    ['旧服务缺少 flag', () => response({ ok: true })],
    ['旧服务 flag 类型错误', () => response({ ok: true, include_photos: 'true' })],
  ];
  for (const [name, modeRoute] of failures) {
    for (const serverMode of [false, true]) {
      const mock = await ui(undefined, { configured: false, use_ai: true, clubs: [], include_photos: serverMode }, modeRoute);
      const checkbox = mock.elements.get('aiIncludePhotos');
      checkbox.checked = !serverMode; await checkbox.onchange();
      assert.equal(checkbox.checked, serverMode, name);
      assert.equal(mock.run('aiInfo.include_photos'), serverMode, name);
      assert.equal(mock.elements.get('aiPhotoModeLabel').classList.contains('isOn'), serverMode, name);
      assert.equal(checkbox.disabled, false, name);
      assert.equal(mock.elements.get('aiRedo').disabled, false, name);
      assert.equal(mock.run('photoModeSaving'), false, name);
      assert.match(mock.elements.get('aiPhotoConsent').textContent, /照片开关保存未确认.*重启看板后重试/, name);
      assert.deepEqual(mock.requests.map(r => r.url), ['/api/ai', '/api/status', '/api/ai-photo-mode', '/api/ai']);
      assert.deepEqual(JSON.parse(mock.requests[2].options.body), { include_photos: !serverMode });
      assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false, name);
    }
  }
});

test('mode API 响应丢失但服务端已保存时，以重新读取的服务端状态为准', async () => {
  const config = { configured: false, use_ai: true, clubs: [], include_photos: false };
  const mock = await ui(undefined, config, () => {
    config.include_photos = true;
    const error = new Error('模拟响应超时'); error.name = 'AbortError';
    return Promise.reject(error);
  });
  mock.elements.get('aiIncludePhotos').checked = true;
  await mock.elements.get('aiIncludePhotos').onchange();
  assert.equal(mock.elements.get('aiIncludePhotos').checked, true);
  assert.equal(mock.elements.get('aiPhotoModeLabel').classList.contains('isOn'), true);
  assert.equal(mock.run('aiInfo.include_photos'), true);
  assert.equal(mock.requests.filter(r => r.url === '/api/ai').length, 2);
  assert.match(mock.elements.get('aiPhotoConsent').textContent, /保存未确认.*请求超时/);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
});

test('全局模式保存中禁用勾选及重新分析，重复事件不重复保存或分析，完成后才可重分析', async () => {
  let resolveMode;
  const mock = await ui(url => response(url === '/api/analyze' ? {} : video(1)), undefined,
    () => new Promise(resolve => { resolveMode = resolve; }));
  mock.select(1); await flush();
  mock.emit('analysis', { seq: 1, summary: '保存中保留的已有结果', input_mode: 'text' });
  const body = mock.elements.get('aiBody').innerHTML;
  const checkbox = mock.elements.get('aiIncludePhotos'), redo = mock.elements.get('aiRedo');
  checkbox.checked = true;
  const saving = checkbox.onchange();
  assert.equal(checkbox.disabled, true);
  assert.equal(redo.disabled, true);
  assert.equal(mock.run('photoModeSaving'), true);
  assert.match(mock.elements.get('aiPhotoConsent').textContent, /正在保存全局照片开关/);
  await checkbox.onchange();
  for (let i = 0; i < 3; i++) redo.onclick();
  assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
  assert.equal(mock.run('analyses[1].summary'), '保存中保留的已有结果');
  assert.equal(mock.elements.get('aiBody').innerHTML, body);
  resolveMode(response({ ok: true, include_photos: true }));
  await saving;
  assert.equal(checkbox.disabled, false);
  assert.equal(redo.disabled, false);
  assert.equal(checkbox.checked, true);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
  redo.onclick(); await flush();
  const posts = mock.requests.filter(r => r.url === '/api/analyze');
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].options.body), { seq: 1, club: null });
});

test('跨页 ai_config SSE 同步全局勾选、能力及高亮，不重跑已有分析或覆盖设置草稿', async () => {
  const first = await ui(() => response(video(1)));
  const second = await ui(() => response(video(2)));
  first.select(1); second.select(2); await flush();
  second.emit('analysis', { seq: 2, summary: '第二页已有文本结果', input_mode: 'text' });
  second.elements.get('aiCfgBtn').onclick();
  second.elements.get('cfgVisionModel').value = 'dummy-unsaved-vision-model';
  first.elements.get('aiIncludePhotos').checked = true;
  await first.elements.get('aiIncludePhotos').onchange();
  second.emit('ai_config', { ...JSON.parse(first.run('JSON.stringify(aiInfo)')), vision_enabled: true, vision_configured: true });
  assert.equal(second.elements.get('aiIncludePhotos').checked, true);
  assert.equal(second.elements.get('aiPhotoModeLabel').classList.contains('isOn'), true);
  assert.equal(second.run('aiInfo.include_photos'), true);
  assert.equal(second.run('aiInfo.vision_configured'), true);
  assert.equal(second.run('aiSeq'), 2);
  assert.equal(second.run('analyses[2].summary'), '第二页已有文本结果');
  assert.match(second.elements.get('aiBody').innerHTML, /input_mode=text.*图片未参与.*已有结果.*重新分析/);
  assert.equal(second.elements.get('aiModal').hidden, false);
  assert.equal(second.elements.get('cfgVisionModel').value, 'dummy-unsaved-vision-model');
  assert.equal(second.requests.some(r => r.url === '/api/ai-photo-mode'), false);
  second.elements.get('aiIncludePhotos').checked = false;
  await second.elements.get('aiIncludePhotos').onchange();
  first.emit('ai_config', JSON.parse(second.run('JSON.stringify(aiInfo)')));
  assert.equal(first.elements.get('aiIncludePhotos').checked, false);
  assert.equal(first.elements.get('aiPhotoModeLabel').classList.contains('isOn'), false);
  assert.equal(first.run('aiInfo.include_photos'), false);
  for (const mock of [first, second]) {
    assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
    assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
  }
});

test('全局开启后加载已有 input_mode=text 仍如实提示未带图的已有结果，不冒充 vision 或自动重跑', async () => {
  const mock = await ui(() => response(video(1)),
    { configured: true, use_ai: true, clubs: [], include_photos: true, vision_enabled: true, vision_configured: true });
  mock.emit('hello', { shots: [{ seq: 1, ts: 0, m: {} }], analyses: {
    1: { seq: 1, summary: '刷新前的文本结果', source: 'ai', input_mode: 'text' },
  } });
  await flush();
  assert.equal(mock.elements.get('aiIncludePhotos').checked, true);
  assert.equal(mock.run('analyses[1].input_mode'), 'text');
  assert.match(mock.elements.get('aiBody').innerHTML, /刷新前的文本结果/);
  assert.match(mock.elements.get('aiBody').innerHTML, /input_mode=text.*图片未参与.*未带图的已有结果.*重新分析/);
  assert.doesNotMatch(mock.elements.get('aiBody').innerHTML, /input_mode=vision|图片已参与|本次视觉分析/);
  assert.match(mock.elements.get('aiPhotoConsent').textContent, /已有结果不自动重跑/);
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /input_mode|photoCaption|photoNote/);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze' || r.url === '/api/ai-photo-mode'), false);
});

// 设置回归使用虚构占位值；不读取或请求真实 API Key。
test('AI 设置：输入点击和遮罩点击均不关闭，选中文字拖出弹窗也保留草稿', async () => {
  const mock = await ui();
  mock.elements.get('aiCfgBtn').onclick();
  const modal = mock.elements.get('aiModal');
  const key = mock.elements.get('cfgVisionKey');
  key.value = 'dummy-clipboard-value';
  modal.onclick?.({ target: key, stopPropagation() {} });
  assert.equal(modal.hidden, false);
  // 浏览器可将“弹窗内按下、遮罩上松开”的 click 重定向至遮罩。
  modal.onclick?.({ target: modal, stopPropagation() {} });
  assert.equal(modal.hidden, false);
  assert.equal(key.value, 'dummy-clipboard-value');
  assert.equal(mock.requests.some(r => r.options.method === 'POST'), false);
});

test('AI 设置：复制粘贴快捷键及 Escape 不关闭、不吞掉浏览器默认行为', async () => {
  const mock = await ui();
  mock.elements.get('aiCfgBtn').onclick();
  const key = mock.elements.get('cfgKey');
  key.value = 'dummy-editing-value';
  for (const event of [
    { key: 'a', ctrlKey: true }, { key: 'c', ctrlKey: true },
    { key: 'v', ctrlKey: true }, { key: 'v', metaKey: true },
    { key: 'Escape' }, { key: 'Escape', isComposing: true },
  ]) {
    let prevented = false;
    mock.documentEvents.keydown({ ...event, target: key, preventDefault() { prevented = true; } });
    assert.equal(mock.elements.get('aiModal').hidden, false);
    assert.equal(prevented, false);
    assert.equal(key.value, 'dummy-editing-value');
  }
});

test('AI 设置：重复打开与实时新杆、重连、分析刷新不清空输入草稿', async () => {
  const mock = await ui(() => response(video(1)));
  mock.elements.get('aiModal').hidden = true;
  mock.elements.get('aiCfgBtn').onclick();
  const draft = { cfgUrl: 'https://example.invalid', cfgModel: 'dummy-model',
    cfgKey: 'dummy-text-value', cfgVisionUrl: 'https://vision.invalid',
    cfgVisionModel: 'dummy-vision-model', cfgVisionKey: 'dummy-vision-value' };
  for (const [id, value] of Object.entries(draft)) mock.elements.get(id).value = value;
  mock.elements.get('cfgVisionEnabled').checked = true;
  mock.elements.get('aiCfgBtn').onclick();
  mock.emit('shot', { seq: 1, ts: 0, m: {} });
  mock.emit('hello', { shots: [{ seq: 1, ts: 0, m: {} }], analyses: {} });
  mock.emit('analysis', { seq: 1, summary: '后台分析更新', input_mode: 'text' });
  await flush();
  assert.equal(mock.elements.get('aiModal').hidden, false);
  assert.equal(mock.elements.get('cfgVisionEnabled').checked, true);
  for (const [id, value] of Object.entries(draft)) assert.equal(mock.elements.get(id).value, value);
});

test('AI 设置：保存失败留在弹窗，取消与保存成功才关闭，重新打开不残留未保存 Key', async () => {
  let saved = false;
  const mock = await ui(() => response({ ok: saved }));
  mock.elements.get('aiCfgBtn').onclick();
  mock.elements.get('cfgKey').value = 'dummy-unsaved-value';
  await mock.elements.get('cfgSave').onclick();
  assert.equal(mock.elements.get('aiModal').hidden, false);
  assert.equal(mock.elements.get('cfgKey').value, 'dummy-unsaved-value');
  assert.equal(mock.elements.get('cfgMsg').textContent, '保存失败');
  mock.elements.get('cfgCancel').onclick();
  assert.equal(mock.elements.get('aiModal').hidden, true);
  mock.elements.get('aiCfgBtn').onclick();
  assert.equal(mock.elements.get('cfgKey').value, '');
  saved = true;
  await mock.elements.get('cfgSave').onclick();
  assert.equal(mock.elements.get('aiModal').hidden, true);
});

const INTERNAL_CAMERA = '00000000-0000-0000-0000-000000000001';
const OT_CAMERA = '00000000-0000-0000-0000-000000000002';
function cameraVideo(seq, cameraId = OT_CAMERA, revision = cameraId, status = 'ready') {
  return { ...video(seq, revision, status), camera_id: cameraId,
    cameras: [
      { id: INTERNAL_CAMERA, label: 'TrackMan 内置 · DL', key: 'TM4Cam|Internal|DL', is_internal: true, available: true },
      { id: OT_CAMERA, label: 'iPhone · OT', key: 'iPhone|Other|OT', is_internal: false, available: true },
    ] };
}

test('照片区只显示八张图与操作栏，摄像头位于重试准备左侧，不展示技术说明', async () => {
  const mock = await ui(() => response(cameraVideo(1)));
  mock.select(1); await flush();
  const photos = mock.elements.get('aiPhotos').innerHTML;
  assert.equal((photos.match(/<img /g) || []).length, 8);
  assert.equal((photos.match(/class="photoTile"/g) || []).length, 8);
  assert.ok(photos.indexOf('data-action="camera"') < photos.indexOf('data-action="prepare"'));
  assert.match(photos, /TrackMan 内置 · DL/);
  assert.match(photos, /iPhone · OT/);
  assert.match(photos, new RegExp(`value="${OT_CAMERA}" selected`));
  assert.doesNotMatch(photos, /photoCaption|photoNote|置信度|阶段状态|触球帧候选|input_mode|八张挥杆照片/);
  assert.equal(mock.run('videos[1].cameraId'), OT_CAMERA);
});

test('切摄像头保留全局模式且不发 AI，旧 GET、SSE 和分析不串图；全局开启时分析与校准传所选 ID', async () => {
  let resolveInternal;
  const mock = await ui((url, options) => {
    if (url.includes('/api/video?') && url.includes('camera_id=' + INTERNAL_CAMERA))
      return new Promise(resolve => { resolveInternal = resolve; });
    if (url === '/api/video-calibrate') return response(cameraVideo(1, INTERNAL_CAMERA, 'internal-r2', 'pending'));
    if (url === '/api/analyze') return response({ ok: true });
    return response(cameraVideo(1));
  });
  mock.select(1); await flush();
  mock.elements.get('aiIncludePhotos').checked = true;
  await mock.elements.get('aiIncludePhotos').onchange();
  mock.elements.get('aiPhotos').onchange({ target: { dataset: { action: 'camera' }, value: INTERNAL_CAMERA } });
  await flush();
  assert.equal(mock.elements.get('aiIncludePhotos').checked, true);
  assert.equal(mock.run('aiInfo.include_photos'), true);
  assert.equal(mock.elements.get('aiPhotoModeLabel').classList.contains('isOn'), true);
  assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
  assert.doesNotMatch(mock.elements.get('aiPhotos').innerHTML, /<img /);
  mock.emit('video', cameraVideo(1, OT_CAMERA, 'late-ot'));
  mock.emit('analysis', { seq: 1, input_mode: 'vision', video_camera_id: OT_CAMERA, summary: '错摄像头分析' });
  assert.equal(mock.run('videos[1].cameraId'), INTERNAL_CAMERA);
  assert.equal(mock.run('analyses[1]'), undefined);
  mock.emit('video', cameraVideo(1, INTERNAL_CAMERA, 'internal-r1'));
  resolveInternal(response(cameraVideo(1, OT_CAMERA, 'wrong-get'))); await flush();
  assert.equal(mock.run('videos[1].data.revision'), 'internal-r1');
  const photos = mock.elements.get('aiPhotos').innerHTML;
  assert.match(photos, /\/internal-r1\//);
  assert.doesNotMatch(photos, /late-ot|wrong-get/);
  mock.elements.get('aiRedo').onclick(); await flush();
  const post = mock.requests.find(r => r.url === '/api/analyze');
  assert.deepEqual(JSON.parse(post.options.body), { seq: 1, club: null, camera_id: INTERNAL_CAMERA });
  mock.run('openCalibration();');
  mock.elements.get('calPreview').firstChild.onload();
  mock.elements.get('calNext').onclick();
  assert.match(mock.elements.get('calPreview').firstChild.src, new RegExp('camera_id=' + INTERNAL_CAMERA));
  mock.elements.get('calPreview').firstChild.onload();
  await mock.elements.get('calConfirm').onclick(); await flush();
  assert.deepEqual(JSON.parse(mock.requests.find(r => r.url === '/api/video-calibrate').options.body),
    { seq: 1, revision: 'internal-r1', frame_index: 61, camera_id: INTERNAL_CAMERA });
  assert.equal(mock.elements.get('aiIncludePhotos').checked, true);
  assert.equal(mock.run('aiInfo.include_photos'), true);
  assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
  assert.equal(mock.requests.filter(r => r.url === '/api/analyze').length, 1);
});

test('重分析、改球杆与配置保存仅在全局开启时携带所选 camera_id，始终省略 include_photos', async () => {
  for (const checked of [false, true]) {
    const mock = await ui(url => response(url === '/api/analyze' || url === '/api/ai-config' ? { ok: true } : cameraVideo(1)));
    mock.select(1); await flush();
    const checkbox = mock.elements.get('aiIncludePhotos'), club = mock.elements.get('aiClub');
    checkbox.checked = checked; await checkbox.onchange();
    mock.elements.get('aiPhotos').onchange({ target: { dataset: { action: 'camera' }, value: INTERNAL_CAMERA } });
    await flush();
    assert.equal(mock.run('videos[1].cameraId'), INTERNAL_CAMERA);
    assert.equal(checkbox.checked, checked);
    assert.equal(mock.requests.some(r => r.url === '/api/analyze'), false);
    mock.elements.get('aiRedo').onclick(); await flush();
    club.value = '7i'; club.onchange({ target: club }); await flush();
    mock.elements.get('aiCfgBtn').onclick();
    await mock.elements.get('cfgSave').onclick(); await flush();
    const posts = mock.requests.filter(r => r.url === '/api/analyze');
    assert.equal(posts.length, 3);
    for (const [index, post] of posts.entries()) {
      assert.deepEqual(JSON.parse(post.options.body), { seq: 1, club: index === 0 ? null : '7i',
        ...(checked ? { camera_id: INTERNAL_CAMERA } : {}) });
    }
    assert.equal(checkbox.checked, checked);
    assert.equal(mock.run('aiInfo.include_photos'), checked);
    assert.equal(mock.requests.filter(r => r.url === '/api/ai-photo-mode').length, 1);
  }
});

test('手动摄像头偏好跨杆使用新的录像ID，偏好不可用时接受服务器自动默认', async () => {
  const internal2 = '00000000-0000-0000-0000-000000000003';
  const ot2 = '00000000-0000-0000-0000-000000000004';
  const mock = await ui(url => {
    const query = new URL(url, 'http://dashboard.local').searchParams;
    const seq = Number(query.get('seq'));
    const data = cameraVideo(seq, query.get('camera_id') || (seq === 2 ? ot2 : OT_CAMERA));
    if (seq === 2){ data.cameras[0].id = internal2; data.cameras[1].id = ot2; }
    return response(data);
  });
  mock.select(1); await flush();
  mock.elements.get('aiPhotos').onchange({ target: { dataset: { action: 'camera' }, value: INTERNAL_CAMERA } });
  await flush();
  assert.equal(mock.run('cameraPreference'), 'TM4Cam|Internal|DL');
  mock.select(2); await flush();
  assert.equal(mock.run('videos[2].cameraId'), internal2);
  assert.ok(mock.requests.some(r => r.url === '/api/video?seq=2&camera_id=' + internal2));
  mock.run('cameraPreference = "missing-camera-preference";');
  mock.select(3); await flush();
  assert.equal(mock.run('videos[3].cameraId'), OT_CAMERA);
});

test('自动默认随外接录像后到升级，手选后不被自动事件覆盖', async () => {
  const first = cameraVideo(1, INTERNAL_CAMERA);
  first.camera_auto = true;
  first.cameras = [first.cameras[0]];
  const mock = await ui(() => response(first));
  mock.select(1); await flush();
  assert.equal(mock.run('videos[1].cameraManual'), false);
  assert.equal(mock.run('videos[1].cameraId'), INTERNAL_CAMERA);
  const external = cameraVideo(1);
  external.camera_auto = true;
  mock.emit('video', external);
  assert.equal(mock.run('videos[1].cameraId'), OT_CAMERA);
  assert.equal(mock.run('videos[1].cameraManual'), false);
  mock.elements.get('aiPhotos').onchange({ target: { dataset: { action: 'camera' }, value: INTERNAL_CAMERA } });
  await flush();
  mock.emit('video', external);
  assert.equal(mock.run('videos[1].cameraId'), INTERNAL_CAMERA);
  assert.equal(mock.run('videos[1].cameraManual'), true);
});
