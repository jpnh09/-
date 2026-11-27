'use strict';
// Offline checks for the desktop helpers in fleet_center.js (date arithmetic, notification presets, filter chips,
// reminder table, bulk notification updates). Run: node tests/test_fleet_center_ui.cjs   (from the repository root)
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const file = path.join(__dirname, '..', 'src', 'apps', 'fleet_bridge', 'fleet_bridge', 'fleet_bridge', 'page', 'fleet_center', 'fleet_center.js');
const code = fs.readFileSync(file, 'utf8');
const pick = (a, b) => { const i = code.indexOf(a); assert.ok(i >= 0, a); const j = code.indexOf(b, i); assert.ok(j > i, b); return code.slice(i, j); };
const calendarSrc = pick('frappe.fleet_calendar = (() => {', 'frappe.pages["fleet-center"]');
const escSrc = pick('  const esc =', '  const route =');
const helpers = pick('  const relDay =', '  function eventButton');

let passed = 0;
const queue = [];
const test = (name, fn) => queue.push([name, fn]);   // run one after another (several tests are async)

const calls = [], confirms = [], messages = [];
const box = {
  frappe: {
    call(o) { calls.push(o); return Promise.resolve({message: {}}); },
    confirm(msg, yes) { confirms.push(msg); return yes(); },
    msgprint(m) { messages.push(m); }, show_alert() {},
  },
  console,
};
vm.createContext(box);
vm.runInContext(calendarSrc, box);
vm.runInContext(`
  const calendar = frappe.fleet_calendar;
  let data = {today: '2026-11-26', reminders: []}, view = 'reminders', period = 'all', category = '', onlyHigh = false,
      showLow = true, bulkSelected = new Set(), bulkBusy = false, reloads = 0;
  const requestId = () => 'req-0000000000000001';
  const sourceDateLabel = r => r.manual ? '提醒日期' : '原記錄到期日';
  const load = async () => { reloads++; };
  const renderResults = () => {};
  ${escSrc}
  ${helpers}
  this.api = {calendar, presetValue, reminderChips, reminderTable, applyBulk,
    set: (k, v) => { eval(k + ' = v'); }, get: k => eval(k)};
`, box);
const api = box.api;

test('addDays across month, year and leap day', () => {
  assert.equal(api.calendar.addDays('2026-03-01', -30), '2026-01-30');
  assert.equal(api.calendar.addDays('2026-01-05', -7), '2025-12-29');
  assert.equal(api.calendar.addDays('2028-03-01', -1), '2028-02-29');
  assert.equal(api.calendar.addDays('not-a-date', -1), null);
});

test('presets: N days before the source date at 08:30', () => {
  assert.equal(api.presetValue('2026-12-09', 30), '2026-11-09 08:30:00');
  assert.equal(api.presetValue('2026-12-09', 7), '2026-12-02 08:30:00');
  assert.equal(api.presetValue(null, 30), null);
});

test('chips show the active filters', () => {
  api.set('period', '7'); api.set('category', '通行證'); api.set('onlyHigh', true);
  const html = api.reminderChips();
  assert.match(html, /data-chip="period" data-value="7" aria-pressed="true"/);
  assert.match(html, /data-chip="category" data-value="通行證" aria-pressed="true"/);
  assert.match(html, /data-chip="high" data-value="1" aria-pressed="true"/);
  const order = [...html.matchAll(/data-chip="period" data-value="([^"]+)"/g)].map(m => m[1]);
  assert.deepEqual(order, ['all', '7', '30', '90', 'past']);   // object keys would put the numbers first
  api.set('view', 'assets'); assert.equal(api.reminderChips(), '');
  api.set('view', 'reminders'); api.set('onlyHigh', false); api.set('period', 'all'); api.set('category', '');
});

const legacy = {record_id: 'LL-R1', asset_id: 'A1', plate: '<b>X</b>', description: '保險', category: '保險', reminder_priority: '一般',
  recorded_due_date: '2026-12-31', notification_at: null, version: 'v1', manual: false, days_remaining: 35, effective_date: '2026-12-31'};
const manual = {record_id: 'MR-00001', asset_id: 'A2', plate: 'TRK-0001', description: '通行證', category: '通行證', reminder_priority: '高優先',
  recorded_due_date: '2026-12-09', notification_at: '2026-11-09 09:00:00', version: 'v2', manual: true, notes: 'n', days_remaining: -17, effective_date: '2026-11-09'};
const undated = {...legacy, record_id: 'LL-R2', recorded_due_date: null, effective_date: null, days_remaining: null};

test('reminder table: two date columns, escaped values, selection', () => {
  api.set('data', {today: '2026-11-26', reminders: [legacy, manual, undated]});
  api.get('bulkSelected').add('MR-00001');
  const html = api.reminderTable([legacy, manual, undated]);
  assert.ok(!html.includes('<b>X</b>'), 'plate must be escaped');
  assert.match(html, /<th>證件／記錄日期<\/th><th>預定通知（台北）<\/th>/);
  assert.match(html, /已選 1 筆/);
  assert.match(html, /data-bulk="MR-00001" checked/);
  assert.match(html, /已到通知時間/);
  assert.match(html, /未設定/);
});

test('bulk: legacy and manual endpoints, undated skipped, one reload', async () => {
  calls.length = 0; confirms.length = 0; messages.length = 0;
  ['LL-R1', 'MR-00001', 'LL-R2'].forEach(id => api.get('bulkSelected').add(id));
  await api.applyBulk('30');
  await new Promise(r => setTimeout(r, 0));
  assert.equal(confirms.length, 1);
  assert.match(confirms[0], /2 筆/); assert.match(confirms[0], /1 筆沒有日期/); assert.match(confirms[0], /已過/);
  assert.equal(calls.length, 2);
  const legacyCall = calls.find(c => c.method.endsWith('dashboard_notification_update'));
  assert.equal(legacyCall.args.notification_at, '2026-12-01 08:30:00');
  assert.equal(legacyCall.args.expected_version, 'v1');
  const manualCall = calls.find(c => c.method.endsWith('dashboard_manual_update'));
  assert.deepEqual(Object.keys(manualCall.args.values).sort(), ['category', 'description', 'due_date', 'notes', 'notification_at']);
  assert.equal(manualCall.args.values.notification_at, '2026-11-09 08:30:00');
  assert.equal(api.get('bulkSelected').size, 0);
  assert.equal(api.get('reloads'), 1);
  assert.match(messages[0], /成功 2 筆/);
});

test('bulk clear sends null', async () => {
  calls.length = 0;
  api.get('bulkSelected').add('MR-00001');
  await api.applyBulk('clear');
  await new Promise(r => setTimeout(r, 0));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].args.values.notification_at, null);
});

test('road tab: national/provincial groups and the B1 filter', () => {
  const roadSrc = pick('  const roadTime =', '  function showRoadEvent');
  const ctx = {console};
  vm.createContext(ctx);
  vm.runInContext(`${escSrc}
    let roadData = null, roadFilter = 'current', roadQuery = '', roadClass = 'all', roadMode = 'list', mapAll = false, roadLoading = false, roadError = false, roadRequesting = false;
    const loadRoad = () => {};
    ${helpers.slice(helpers.indexOf('  const chip ='), helpers.indexOf('  function reminderChips'))}
    ${roadSrc}
    this.r = {renderRoad, roadEvents, mapPoints, markerKind, popupHtml, roadSignature, set: (k, v) => { eval(k + ' = v'); }};`, ctx);
  const ev = (name, road, feed, cls, permit) => ({name, road, feed, road_class: cls, permit_route: permit, permit_segment: permit ? '土城－大溪' : '',
    status: '持續', location_text: road, feed_label: cls, event_type_label: '施工', title: 't', truck_impact: '', is_major: 0});
  ctx.r.set('roadData', {installed: true, active: [ev('a', '國道一號', 'freeway_live', '國道', ''), ev('b', '台3', 'highway_live', '省道', '台3'),
    ev('c', '台61', 'highway_live', '省道', '')], unknown: [], ended: [], usage: {month: '2026-11', estimated_points: 0, budget: 2.5, data_calls: 0, wire_mb: 0},
    api_status: '正常', schedule: '05:30', feeds: [], notice: '', major_rule: '', b1: {route_count: 29, includes_freeway: false}, enabled: true, configured: true});
  const all = ctx.r.renderRoad();
  assert.match(all, /fc-group-row"><th colspan="5">國道（1）/);
  assert.match(all, /fc-group-row"><th colspan="5">省道（2）/);
  assert.match(all, /B1：台3（區段待核對）/);
  ctx.r.set('roadClass', 'b1');
  assert.deepEqual(ctx.r.roadEvents().map(e => e.name), ['b']);
  assert.match(ctx.r.renderRoad(), /B1 授權路線：29 條省道/);
  ctx.r.set('roadClass', '國道');
  assert.deepEqual(ctx.r.roadEvents().map(e => e.name), ['a']);
});

test('map: two kinds, truck-only by default, draw order, escaped popups, change detection', () => {
  const ctx = {console};
  vm.createContext(ctx);
  const roadSrc = pick('  const roadTime =', '  function showRoadEvent');
  vm.runInContext(`${escSrc}
    let roadData = null, roadFilter = 'current', roadQuery = '', roadClass = 'all', roadMode = 'map', mapAll = false, roadLoading = false, roadError = false, roadRequesting = false;
    const loadRoad = () => {};
    ${helpers.slice(helpers.indexOf('  const chip ='), helpers.indexOf('  function reminderChips'))}
    ${roadSrc}
    this.m = {mapPoints, markerKind, popupHtml, pinSvg, roadSignature, renderRoad, set: (k, v) => { eval(k + ' = v'); }};`, ctx);
  const e = (name, extra) => ({name, road: '國道一號', feed_label: '國道', status: '持續', location_text: 'x', event_type_label: '施工', title: 't', lat: 25, lon: 121.5, ...extra});
  const sample = [e('closed', {truck_impact: '等同封閉'}), e('plain', {}), e('nopos', {lat: null, lon: null}), e('caution', {truck_impact: '通過時注意'}),
    e('unk', {status: '狀態不明'}), e('major', {is_major: 1, truck_impact: '未提供車道資訊'})];
  const pts = ctx.m.mapPoints(sample);
  assert.deepEqual(pts.map(p => p.e.name), ['plain', 'unk', 'caution', 'closed', 'major']);   // important points drawn last (on top)
  assert.deepEqual(pts.map(p => p.kind), ['other', 'other', 'caution', 'closed', 'closed']);   // only two colours that matter
  assert.deepEqual(ctx.m.mapPoints(sample, false).map(p => p.e.name), ['caution', 'closed', 'major']);
  const popup = ctx.m.popupHtml(e('x', {road: '<img src=x onerror=alert(1)>', location_text: '<i>loc</i>', title: '<b>t</b>', regulation_labels: '<s>r</s>'}));
  assert.ok(!popup.includes('<img') && !popup.includes('<b>t</b>') && !popup.includes('<i>loc') && !popup.includes('<s>r'));
  const closedPopup = ctx.m.popupHtml(e('c', {truck_impact: '等同封閉', regulation_labels: '部分封閉', blocked_lanes_label: '第3車道（最外側）｜本路段 3 車道', status: '新增'}));
  assert.match(closedPopup, /曳引車等同封閉（本輪新增）/);
  assert.match(closedPopup, /施工｜部分封閉：第3車道（最外側）受阻/);
  assert.ok(!closedPopup.includes('本路段'));   // one short line; the lane count stays in 詳細資料
  assert.match(ctx.m.popupHtml(e('u', {truck_impact: '通過時注意', status: '狀態不明'})), /通過時注意（最新資料未再出現，狀態不明）/);
  assert.ok(!ctx.m.pinSvg('closed', 24, '"><script>').includes('"><script>'));
  const d1 = {installed: true, active: [e('a', {last_change_at: '1'})], unknown: [], ended: []};
  const d2 = {installed: true, active: [e('a', {last_change_at: '1', source_update_time: 'later'})], unknown: [], ended: []};
  const d3 = {installed: true, active: [e('a', {last_change_at: '2'})], unknown: [], ended: []};
  assert.equal(ctx.m.roadSignature(d1), ctx.m.roadSignature(d2));
  assert.notEqual(ctx.m.roadSignature(d1), ctx.m.roadSignature(d3));
  ctx.m.set('roadData', {...d1, usage: {month: 'm', estimated_points: 0, budget: 2.5, data_calls: 0, wire_mb: 0}, api_status: '正常', schedule: '',
    feeds: [], notice: '', major_rule: '', b1: {route_count: 29}, enabled: true, configured: true});
  const html = ctx.m.renderRoad();
  assert.match(html, /data-road-map/); assert.ok(!html.includes('<table'));
  assert.match(html, /曳引車等同封閉 <b>0<\/b>/); assert.match(html, /通過時注意 <b>0<\/b>/);
  assert.match(html, /data-chip="mapall" data-value="1" aria-pressed="false">顯示其他事件（1）/);
  assert.match(html, /這個條件沒有影響曳引車的事件/);
  ctx.m.set('roadData', {installed: true, active: [e('a', {truck_impact: '等同封閉'}), e('b', {lat: null})], unknown: [], ended: [],
    usage: {month: 'm', estimated_points: 0, budget: 2.5, data_calls: 0, wire_mb: 0}, api_status: '正常', schedule: '', feeds: [], notice: '', major_rule: '',
    b1: {route_count: 29}, enabled: true, configured: true});
  const html2 = ctx.m.renderRoad();
  assert.match(html2, /曳引車等同封閉 <b>1<\/b>/); assert.match(html2, /1 筆沒有座標，請看列表/); assert.ok(!html2.includes('data-chip="mapall"'));
});

test('limits view: import status, form, results, escaping', () => {
  const ctx = {console};
  vm.createContext(ctx);
  const roadSrc = pick('  const roadTime =', '  function showRoadEvent');
  vm.runInContext(`${escSrc}
    let roadData = null, roadFilter = 'current', roadQuery = '', roadClass = 'all', roadMode = 'limits', mapAll = false, roadLoading = false, roadError = false, roadRequesting = false, view = 'road';
    const loadRoad = () => {};
    ${helpers.slice(helpers.indexOf('  const chip ='), helpers.indexOf('  function reminderChips'))}
    ${roadSrc}
    this.l = {renderRoad, set: (k, v) => { eval(k + ' = v'); }};`, ctx);
  ctx.l.set('limitsData', {installed: true, stale_days: 35, sources: {height: 'https://data.gov.tw/dataset/146233', weight: 'https://data.gov.tw/dataset/169930'},
    latest: {height: {record_count: 1494, imported_at: '2026-11-26 17:00', age_days: 40, stale: true, unknown_count: 18}, weight: null}});
  let html = ctx.l.renderRoad();
  assert.match(html, /data-chip="roadmode" data-value="limits" aria-pressed="true"/);
  assert.match(html, /1494 個/); assert.match(html, /超過 35 天，請下載新版/); assert.match(html, /尚未匯入/);
  assert.match(html, /data-limits-form/); assert.match(html, /name="height" inputmode="decimal" value="4.2"/);
  assert.ok(!html.includes('data-limits-map'));                       // no result yet → no map
  ctx.l.set('limitsCheck', {routes: ['台1'], used_b1: false, red: 1, yellow: 1, conflicts: [
    {level: 'red', kind: 'height', road: '台1', km: 35.511, county: '桃園市', town: '中壢區', direction: '順向', position: '右側', limit_value: 4.1, reason: '限高 4.1 m，車高 4.2 m', remark: ''},
    {level: 'yellow', kind: 'height', road: '<b>台1</b>', km: 71.06, county: '新竹縣', town: '竹北市', direction: '順向', position: '右側', limit_value: null, reason: '限高值不明', remark: '<img src=x>'}]});
  html = ctx.l.renderRoad();
  assert.match(html, /過不去 1 處、請確認 1 處/);
  assert.match(html, /class="fc-limit-red"><td>過不去/); assert.match(html, /class="fc-limit-yellow"><td>請確認/);
  assert.match(html, /限高 不明/); assert.match(html, /data-limits-map/);
  assert.ok(!html.includes('<b>台1</b>') && !html.includes('<img src=x>'));
});

(async () => {
  for (const [name, fn] of queue) {
    try { await fn(); passed++; console.log('PASS ' + name); }
    catch (error) { console.log('FAIL ' + name + ': ' + error.message); process.exitCode = 1; }
  }
  console.log(`${passed} of ${queue.length} tests passed`);
})();
