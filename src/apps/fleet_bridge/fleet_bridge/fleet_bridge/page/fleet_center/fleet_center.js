// Date-only arithmetic: never convert the source date through a browser timezone.
frappe.fleet_calendar = (() => {
  const pad = n => String(n).padStart(2, "0");
  function parseDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const [y,m,d] = value.split("-").map(Number);
    if (y < 1000 || y > 9999 || m < 1 || m > 12 || d < 1 || d > new Date(Date.UTC(y,m,0)).getUTCDate()) return null;
    return {y,m,d};
  }
  const validMonth = value => typeof value === "string" && /^\d{4}-\d{2}$/.test(value) && !!parseDate(value + "-01");
  function shiftMonth(value, delta) {
    if (!validMonth(value)) throw new Error("Invalid month");
    const p = parseDate(value + "-01"), dt = new Date(Date.UTC(p.y, p.m - 1 + delta, 1));
    const next = `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth()+1)}`;
    return validMonth(next) ? next : value;
  }
  function monthData(records, month) {
    if (!validMonth(month)) throw new Error("Invalid month");
    const p = parseDate(month + "-01"), days = new Date(Date.UTC(p.y,p.m,0)).getUTCDate();
    const offset = new Date(Date.UTC(p.y,p.m-1,1)).getUTCDay();
    const buckets = Object.fromEntries(Array.from({length:days},(_,i)=>[`${month}-${pad(i+1)}`,[]]));
    const undated = [];
    records.forEach(r => {
      const date = r.effective_date || r.recorded_due_date;
      if (!parseDate(date)) undated.push(r);
      else if (buckets[date]) buckets[date].push(r);
    });
    const cells = Array(offset).fill(null).concat(Object.keys(buckets));
    while (cells.length % 7) cells.push(null);
    return {buckets, cells, undated, count:Object.values(buckets).reduce((n,rs)=>n+rs.length,0)};
  }
  const priorityRank = value => ({"高優先":0,"一般":1,"低優先":2}[value] ?? 1);
  const utc = p => Date.UTC(p.y, p.m - 1, p.d);
  function dayDiff(from, to) {
    const a = parseDate(from), b = parseDate(to);
    return a && b ? Math.round((utc(b) - utc(a)) / 86400000) : null;
  }
  function dayLabel(value) {
    const p = parseDate(value);
    return p ? `${pad(p.m)}/${pad(p.d)}（${"日一二三四五六"[new Date(utc(p)).getUTCDay()]}）` : value;
  }
  function addDays(value, n) {
    const p = parseDate(value);
    if (!p) return null;
    const dt = new Date(utc(p) + n * 86400000);
    return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth()+1)}-${pad(dt.getUTCDate())}`;
  }
  return {parseDate, validMonth, shiftMonth, monthData, priorityRank, dayDiff, dayLabel, addDays};
})();

frappe.pages["fleet-center"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({parent: wrapper, title: "車隊首頁", single_column: true});
  const root = document.createElement("div");
  root.className = "fleet-center";
  page.body.append(root);
  const labels = {Truck: "貨車／曳引車", Trailer: "拖車", Passenger: "小客車", "Insurance Folder": "保險資料夾", "Needs Review": "分類待確認"};
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const text = value => esc(value || "未提供");
  const route = (doctype, name) => `/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}`;
  const calendar = frappe.fleet_calendar;
  const categoryClass = c => ({"保險":"insurance", "通行證":"permit", "驗車":"inspection"}[c] || "other");
  const sourceDateLabel = r => r.manual ? r.date_type : sourceKind(r) === "記錄日" ? "原記錄日期" : `原記錄${sourceKind(r)}`;
  const verification = r => r.manual ? '手動新增' : '待核驗';
  const stateKey = "fleet-center-v2:" + frappe.session.user;
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(stateKey) || "{}"); } catch (_) {}
  let showLow = saved.showLow !== false, savingPriority = false, savingNote = false;
  const requestId = () => window.crypto?.randomUUID?.() || `fleet-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const saveError = error => error?.status === 0
    ? '車隊系統暫時無法連線，尚未確認儲存。請稍後重試；相同內容會沿用同一次請求。'
    : error?.responseJSON?.exc_type === 'TimestampMismatchError'
      ? '這筆提醒已被其他操作更新。請重新整理、核對內容後再儲存。'
      : '未能確認儲存成功，請重新整理核對後再試。';
  let activeReminder, refreshing = false, refreshPending = false, refreshTimer;
  let data, selected = saved.selected, highlighted = saved.highlighted,
    view = ["assets","reminders","calendar","road"].includes(saved.view) ? saved.view : "assets",
    roadFilter = ["current","truck","major","latest","unknown","ended"].includes(saved.roadFilter) ? saved.roadFilter : "current",
    roadQuery = typeof saved.roadQuery === "string" ? saved.roadQuery : "",
    roadClass = ["all","國道","省道","b1"].includes(saved.roadClass) ? saved.roadClass : "all",
    roadMode = saved.roadMode === "map" ? "map" : "list", mapAll = saved.mapAll === true,
    roadData = null, roadLoading = false, roadError = false, roadRequesting = false,
    query = typeof saved.query === "string" ? saved.query : "",
    kind = labels[saved.kind] ? saved.kind : "", period = ["all","7","30","60","90","past"].includes(saved.period) ? saved.period : "all",
    onlyHigh = saved.onlyHigh === true, bulkSelected = new Set(), bulkBusy = false,
    category = ["保險","通行證","驗車","其他提醒"].includes(saved.category) ? saved.category : "",
    month = calendar.validMonth(saved.month) ? saved.month : null;
  function saveState() {
    try {sessionStorage.setItem(stateKey,JSON.stringify({selected,highlighted,view,query,kind,period,category,month,showLow,roadFilter,roadQuery,onlyHigh,roadClass,roadMode,mapAll}));} catch (_) {}
  }
  root.innerHTML = `<style>
    .fleet-center{--fc-ink:#163b43;--fc-muted:#5f747b;--fc-line:#dce7e8;color:var(--text-color);padding:4px 0 28px}
    .fc-hero{background:#153f48;color:#fff;border-radius:16px;padding:25px 28px;display:flex;justify-content:space-between;gap:20px;align-items:center}
    .fc-hero h2{color:#fff;margin:5px 0 8px;font-size:25px}.fc-hero p{margin:0;color:#d5e6e9}.fc-eyebrow{font-size:11px;letter-spacing:2px;color:#add2ce}.fc-date{font-size:12px;text-align:right;white-space:nowrap;color:#d5e6e9}
    .fc-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}.fc-card{border:1px solid var(--fc-line);border-radius:12px;padding:16px;background:var(--card-bg);text-align:left;color:inherit}.fc-card strong{display:block;font-size:29px;color:var(--fc-ink);line-height:1.3}.fc-card span{font-size:13px}.fc-card small{display:block;color:var(--fc-muted);margin-top:4px}.fc-card:hover{border-color:#3c7d81}
    .fc-notice{background:#fff7e7;border:1px solid #eedbbb;border-radius:10px;color:#745a22;padding:10px 14px;font-size:12px;line-height:1.7;margin-bottom:18px}
    .fc-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:15px}.fc-tabs{display:flex;gap:5px;margin-right:auto}.fc-tabs button{border:0;border-radius:8px;padding:10px 16px;background:var(--control-bg);color:inherit}.fc-tabs button[aria-selected=true]{background:#246570;color:#fff}.fc-toolbar input,.fc-toolbar select{border:1px solid var(--fc-line);border-radius:8px;padding:9px 12px;background:var(--card-bg);color:inherit;max-width:100%}.fc-toolbar input{width:250px}
    .fc-grid{display:grid;grid-template-columns:minmax(240px,0.9fr) minmax(0,2fr);gap:18px}.fc-list{max-height:730px;overflow:auto;border:1px solid var(--fc-line);border-radius:12px;background:var(--card-bg)}.fc-list-head{padding:12px 16px;background:var(--control-bg);font-size:12px;color:var(--fc-muted)}.fc-asset{display:block;text-align:left;width:100%;border:0;border-bottom:1px solid var(--fc-line);background:transparent;padding:14px 16px;color:inherit}.fc-asset[aria-current=true]{background:#e9f4f3;box-shadow:inset 4px 0 #267a78;color:#143e42}.fc-asset strong{font-size:16px;display:block;letter-spacing:.3px}.fc-asset small{display:block;margin-top:4px;color:var(--fc-muted)}.fc-asset .fc-chip{margin-top:7px}
    .fc-detail{min-width:0;border:1px solid var(--fc-line);border-radius:12px;padding:23px;background:var(--card-bg)}.fc-detail h3{margin:0 0 8px;font-size:25px;color:var(--fc-ink)}.fc-subtitle{color:var(--fc-muted);font-size:13px}.fc-chip{display:inline-block;padding:3px 8px;font-size:11px;border-radius:5px;background:#edf1f2;color:#52636a;margin:2px 4px 2px 0}.fc-tag{background:#e5f2ef;color:#28635e}.fc-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding:18px 0;border-bottom:1px solid var(--fc-line)}.fc-meta dt{font-weight:normal;color:var(--fc-muted);font-size:11px;margin-bottom:5px}.fc-meta dd{margin:0;font-size:14px;overflow-wrap:anywhere}.fc-section{margin-top:22px}.fc-section h4{font-size:15px;margin:0 0 10px}.fc-reminder{display:grid;grid-template-columns:1fr auto;gap:7px 14px;padding:12px 0;border-bottom:1px solid var(--fc-line)}.fc-reminder time{font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}.fc-reminder p{grid-column:1/-1;margin:0;font-size:12px;color:var(--fc-muted);white-space:pre-wrap;overflow-wrap:anywhere}.fc-empty{padding:22px;color:var(--fc-muted);font-size:13px}.fc-source{margin-top:22px;padding-top:14px;border-top:1px solid var(--fc-line);font-size:12px;color:var(--fc-muted);line-height:1.7}.fc-links{display:flex;gap:14px;margin-top:12px;flex-wrap:wrap}.fc-links a,.fc-table button{color:#1f727a}.fc-table-wrap{overflow:auto;border:1px solid var(--fc-line);border-radius:12px}.fc-table{width:100%;background:var(--card-bg);font-size:13px}.fc-table th{background:var(--control-bg);padding:12px;text-align:left;white-space:nowrap}.fc-table td{padding:12px;border-top:1px solid var(--fc-line);vertical-align:top}.fc-table button{border:0;background:transparent;font-weight:600;padding:0;white-space:nowrap}.fc-past{color:#98622d}.fc-count{font-size:12px;color:var(--fc-muted);margin:10px 0}.fc-busy{padding:45px;text-align:center;color:var(--fc-muted)}
    .fc-calendar{border:1px solid var(--fc-line);border-radius:12px;background:var(--card-bg);overflow:hidden}.fc-calendar-toolbar{padding:16px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.fc-month-controls{display:flex;align-items:center;gap:8px}.fc-month-controls label{margin:0;font-size:12px}.fc-month-controls input{border:1px solid var(--fc-line);border-radius:6px;padding:7px;background:var(--card-bg);color:inherit}.fc-legend{display:flex;flex-wrap:wrap;gap:8px;padding:0 16px 16px;font-size:12px}.fc-legend span{padding:3px 8px;border-radius:5px}.fc-weekdays,.fc-month-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}.fc-weekdays span{text-align:center;background:var(--control-bg);padding:9px;color:var(--fc-muted);font-size:12px}.fc-calendar-day{min-height:142px;padding:7px;border-top:1px solid var(--fc-line);border-right:1px solid var(--fc-line);min-width:0}.fc-calendar-day:nth-child(7n){border-right:0}.fc-outside{background:var(--control-bg)}.fc-day-number{display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:600;margin-bottom:7px}.fc-day-number span{font-weight:normal;font-size:10px;background:#246570;color:white;border-radius:4px;padding:2px 4px}.fc-is-today{box-shadow:inset 0 0 0 2px #3e8686}.fc-event{display:block;text-align:left;width:100%;border:0;border-left:3px solid transparent;border-radius:5px;padding:5px 6px;font-size:11px;line-height:1.5;margin:4px 0;white-space:normal;overflow-wrap:anywhere;cursor:pointer}.fc-event-head{display:flex;flex-wrap:wrap;justify-content:space-between;column-gap:6px;font-size:.9em;opacity:.85}.fc-event-head>span{white-space:nowrap;min-width:0}.fc-event-time{margin-left:auto}.fc-event-title{display:block;font-size:1.08em;line-height:1.4;color:var(--fc-ink)}.fc-event-due{display:block;font-weight:600}.fc-nw{white-space:nowrap}.fc-event-overdue{color:#9f1d1d}.fc-event-note{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.fc-agenda-rel{margin-left:6px;font-weight:normal;font-size:12px;color:var(--fc-muted)}.fc-event-insurance{background:#e9f2fc;color:#285986;border-left-color:#4a7ca9}.fc-event-permit{background:#e4f2ed;color:#25664e;border-left-color:#398263}.fc-event-inspection{background:#fff2da;color:#79551e;border-left-color:#bb8b3d}.fc-event-other{background:#f0eafa;color:#644b87;border-left-color:#8362ad}.fc-event:hover{filter:brightness(.96)}.fc-event:focus-visible{outline:2px solid #153f48;outline-offset:2px}.fc-more{border:0;background:none;color:#246570;font-size:12px;padding:5px 0;text-align:left}.fc-calendar-mobile{display:none}.fc-agenda-day{padding:12px 16px;border-top:1px solid var(--fc-line)}.fc-agenda-day h4{font-size:14px}.fc-undated{border-top:1px solid var(--fc-line);padding:14px 16px}.fc-undated summary{cursor:pointer;font-size:13px}.fc-undated .fc-event{max-width:430px}.fc-summary{padding:3px 4px}.fc-summary h3{font-size:21px;margin-top:0;overflow-wrap:anywhere}.fc-summary h4{font-size:14px;margin-top:20px}.fc-summary p{font-size:12px;color:var(--text-muted)}.fc-summary dl{display:flex;gap:18px;border-block:1px solid var(--border-color);padding:13px 0}.fc-summary dd{margin:0}.fc-summary-notes{white-space:pre-wrap;overflow-wrap:anywhere;max-height:35vh;overflow:auto;border-radius:8px;background:var(--control-bg);padding:14px;font-size:13px}.fc-summary-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}.fc-day-dialog .fc-event{padding:10px;font-size:14px}.fc-highlighted{outline:2px solid #2e8180;outline-offset:5px;background:#eff8f4;border-radius:5px}.fc-return{margin-bottom:16px}
    .fc-high{box-shadow:inset 0 0 0 2px #c83232;font-weight:700}.fc-low{background:#f1f3f4;color:#4f5961;border-left-color:#a5afb5}.fc-low .fc-event-title{color:#3d474f}.fc-priority{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0;padding:12px;background:var(--control-bg);border-radius:8px}.fc-priority label{margin:0}.fc-priority select{border:1px solid var(--fc-line);border-radius:7px;padding:7px 10px;background:var(--card-bg);color:inherit}.fc-priority-high{background:#fde8e8;color:#9f1d1d;font-weight:700}.fc-priority-low{background:#eceff1;color:#65727a}.fc-show-low{display:flex;gap:6px;align-items:center;margin:0}.fc-toolbar .fc-show-low input{width:auto}.fc-low-count{font-size:12px;color:var(--fc-muted)}.fc-general-note{margin:16px 0;padding:14px;border:1px solid var(--fc-line);border-radius:10px;background:var(--control-bg)}.fc-general-note-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.fc-general-note h4{margin:0}.fc-general-note p{white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0 0;color:var(--fc-muted)}.fc-manual-bar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:22px;padding-bottom:10px;border-bottom:1px solid var(--fc-line)}.fc-manual-bar h4{margin:0;font-size:15px}.fc-manual-bar small{color:var(--fc-muted)}.fc-manual{background:#e8eefb;color:#34508a}.fc-reminder-actions{display:inline-flex;gap:6px;flex-wrap:wrap}
    .fc-chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:-4px 0 14px}.fc-chip-btn{border:1px solid var(--fc-line);border-radius:16px;padding:5px 12px;background:var(--card-bg);color:inherit;font-size:13px;line-height:1.4}.fc-chip-btn:hover{border-color:#3c7d81}.fc-chip-btn[aria-pressed=true]{background:#246570;border-color:#246570;color:#fff}.fc-chip-label{font-size:12px;color:var(--fc-muted);margin:0 2px 0 4px}.fc-chip-sep{width:1px;align-self:stretch;background:var(--fc-line);margin:0 6px}
    .fc-bulk{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 14px;margin-bottom:10px;border:1px solid #9cc7c4;border-radius:10px;background:#eef7f6}.fc-date-tag{display:inline-block;font-size:11px;border-radius:4px;padding:1px 6px;margin-right:4px}.fc-date-source{background:#edf1f2;color:#40545b}.fc-date-notify{background:#e8eefb;color:#34508a}.fc-muted{color:var(--fc-muted)}.fc-table td small{display:block;color:var(--fc-muted);margin-top:3px}.fc-table td small.fc-past{color:#98622d}.fc-presets{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:-6px 0 10px}.fc-shortcuts{font-size:11px;color:var(--fc-muted);margin-left:4px}
    .fc-map{position:relative;z-index:0;height:560px;border:1px solid var(--fc-line);border-radius:12px}.fc-map.leaflet-container{background:#fff}.fc-map .leaflet-tile-pane{filter:grayscale(1);opacity:.5}.fc-pin{background:none;border:0}.fc-pin svg{display:block;filter:drop-shadow(0 1px 1px rgba(0,0,0,.4))}.fc-map-legend{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;font-size:13px;margin:4px 2px 8px}.fc-map-legend>span{display:inline-flex;align-items:center;gap:5px}.fc-map-legend small{color:var(--fc-muted)}.fc-map-popup{font-size:12px;line-height:1.6;min-width:200px;max-width:300px}.fc-map-popup .fc-pop-head{font-size:14px;font-weight:700}.fc-map-popup p{margin:2px 0}
    .fc-group-row th{background:#f4f8f8;color:var(--fc-ink);font-size:13px;padding:8px 12px}.fc-road-b1{background:#e5f2ef;color:#28635e}
    .fc-road-status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:14px}.fc-road-status>div{border:1px solid var(--fc-line);border-radius:12px;padding:14px 16px;background:var(--card-bg);min-width:0}.fc-road-status span{display:block;font-size:12px;color:var(--fc-muted)}.fc-road-status strong{display:block;font-size:16px;color:var(--fc-ink);margin-top:4px;overflow-wrap:anywhere}.fc-road-status small{display:block;color:var(--fc-muted);margin-top:4px;font-size:12px}.fc-road-stale{border-color:#e2b46b!important;background:#fff7e7!important}.fc-road-stale strong{color:#8a5a12}.fc-road-bad strong{color:#9f1d1d}.fc-road-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}.fc-road-actions a{font-size:13px;color:#1f727a}.fc-road-major{background:#fde8e8;color:#9f1d1d;font-weight:700}.fc-road-special{background:#fff2da;color:#79551e;font-weight:700}.fc-road-new{background:#e4f2ed;color:#25664e}.fc-road-unknown{background:#eceff1;color:#4f5961}.fc-road-row td small{display:block;color:var(--fc-muted);margin-top:3px}.fc-road-desc{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;background:var(--control-bg);border-radius:8px;padding:12px;max-height:40vh;overflow:auto}
    @media(max-width:800px){.fc-road-status{grid-template-columns:repeat(2,minmax(0,1fr))}.fc-cards{grid-template-columns:repeat(2,1fr)}.fc-grid{grid-template-columns:1fr}.fc-list{max-height:300px}.fc-hero{padding:20px;align-items:flex-start}.fc-date{display:none}.fc-toolbar input{width:100%}.fc-meta{grid-template-columns:repeat(2,1fr)}.fc-detail{padding:17px}.fc-calendar-desktop{display:none}.fc-calendar-mobile{display:block}.fc-calendar-mobile .fc-event{font-size:13px;padding:8px}.fc-month-controls{flex-wrap:wrap}.fc-tabs{width:100%}.fc-tabs button{padding:9px 11px}.fc-summary-actions .btn{white-space:normal}}
  </style><div class="fc-content"><div class="fc-busy" role="status">正在讀取車隊資料…</div></div>`;
  const content = root.querySelector(".fc-content");
  function filteredAssets() {
    const q = query.trim().toLocaleLowerCase();
    return data.assets.filter(a => (!kind || a.kind === kind) && (!q || [a.plate, a.profile.make, a.profile.model, ...a.profile.legacy_tags].join(" ").toLocaleLowerCase().includes(q)));
  }
  function filteredReminders(ignorePeriod = false) {
    const allowed = new Set(filteredAssets().map(a => a.id));
    return data.reminders.filter(r => allowed.has(r.asset_id) && (showLow || r.reminder_priority !== "低優先") && (!onlyHigh || r.reminder_priority === "高優先") && (!category || r.category === category) &&
      (ignorePeriod || period === "all" || (period === "past" ? r.days_remaining !== null && r.days_remaining < 0 : r.days_remaining !== null && r.days_remaining >= 0 && r.days_remaining <= Number(period)))).sort((a,b)=>(a.effective_date||"9999").localeCompare(b.effective_date||"9999") || calendar.priorityRank(a.reminder_priority)-calendar.priorityRank(b.reminder_priority) || a.plate.localeCompare(b.plate));
  }
  const relDay = n => n === null ? "" : n === 0 ? "今天" : n === 1 ? "明天" : n > 0 ? `${n} 天後` : `${-n} 天前`;
  // Reminders are often set a month ahead, so countdowns refer to the source date, never the notification.
  // 驗車 dates are the 指定檢驗日 itself (inspection allowed one month either side).
  const sourceKind = r => r.manual ? "提醒日期" : r.category === "驗車" ? "定檢日" : r.date_type === "開始日" ? "開始日" : ["保險","通行證"].includes(r.category) ? "到期日" : "記錄日";
  function sourceCountdown(r) {
    const kind = sourceKind(r), n = calendar.dayDiff(data.today, r.recorded_due_date);
    // An expiry is overdue at once; an inspection only after the one-month grace (fines start there).
    const text = n === null || n >= 0 ? relDay(n) : kind === "到期日" ? `已過期 ${-n} 天` : kind === "定檢日" ? `已過 ${-n} 天` : kind === "開始日" ? `已開始 ${-n} 天` : `${-n} 天前`;
    return {kind, text, past: n !== null && (kind === "到期日" ? n < 0 : kind === "定檢日" && n < -30)};
  }
  const dueHtml = d => `<span class="fc-nw">${esc(d.label)}${d.date?' '+esc(d.date):''}</span>${d.rel?` <span class="fc-nw">· ${esc(d.rel)}</span>`:''}`;
  function dueParts(r, full = false) {
    const date = r.recorded_due_date;
    if (!calendar.parseDate(date)) return full || calendar.parseDate(r.effective_date) ? {label: `${sourceKind(r)}未填`, past: false} : null;
    const c = sourceCountdown(r);
    // On the card that already sits on this date, the date itself is redundant but the countdown is not.
    if (!full && date === r.effective_date) return {label: c.kind, rel: c.text, past: c.past};
    return {label: c.kind, date: calendar.dayLabel(date), rel: c.text, past: c.past};
  }
  // Notification presets: N days before the source date at 08:30 Taipei (date-only arithmetic).
  const PRESET_DAYS = [30, 14, 7];
  const presetValue = (due, days) => { const d = calendar.addDays(due, -days); return d ? `${d} 08:30:00` : null; };
  function wirePresets(dialog, getDue, fieldname, kindLabel) {
    const w = dialog.fields_dict.presets.$wrapper;
    w.html(`<div class="fc-presets"><span class="fc-chip-label">快速帶入</span>${PRESET_DAYS.map(n=>`<button type="button" class="btn btn-xs btn-default" data-preset="${n}">${esc(kindLabel)}前 ${n} 天 08:30</button>`).join('')}<button type="button" class="btn btn-xs btn-default" data-preset="clear">清除通知</button></div>`);
    w.on('click','[data-preset]',function(){
      if(this.dataset.preset==='clear'){dialog.set_value(fieldname,'');return;}
      const value=presetValue(getDue(),Number(this.dataset.preset));
      if(!value){frappe.show_alert({message:`${kindLabel}未填，無法推算通知時間。`,indicator:'orange'});return;}
      dialog.set_value(fieldname,value);
      if(value.slice(0,10)<data.today)frappe.show_alert({message:'這個通知時間已過，儲存後會立即發出通知。',indicator:'orange'});
    });
  }
  const chip = (group, value, label, active) => `<button type="button" class="fc-chip-btn" data-chip="${group}" data-value="${esc(value)}" aria-pressed="${active}">${esc(label)}</button>`;
  const ROAD_FILTERS = {current:'目前事件',truck:'影響曳引車',major:'重大管制／特定車種',latest:'本輪新增及變更',unknown:'狀態不明（近 7 日）',ended:'已結束（近 2 日）'};
  function reminderChips() {
    if (view !== 'reminders' && view !== 'calendar') return '';
    const periods = view === 'reminders' ? `<span class="fc-chip-label">期間</span>${[['all','全部'],['7','7 天內'],['30','30 天內'],['90','90 天內'],['past','已過提醒日期']].map(([k,v])=>chip('period',k,v,period===k)).join('')}<span class="fc-chip-sep"></span>` : '';
    return `<div class="fc-chips" role="group" aria-label="提醒篩選">${periods}<span class="fc-chip-label">類別</span>${[['','全部'],['保險','保險'],['通行證','通行證'],['驗車','驗車'],['其他提醒','其他提醒']].map(([k,v])=>chip('category',k,v,category===k)).join('')}<span class="fc-chip-sep"></span>${chip('high','1','只看高優先',onlyHigh)}${chip('low','1','顯示低優先',showLow)}</div>`;
  }
  function reminderTable(list) {
    const visible = list.map(r => r.record_id);
    const allSelected = visible.length > 0 && visible.every(id => bulkSelected.has(id));
    const bar = bulkSelected.size ? `<div class="fc-bulk" role="region" aria-label="批次設定通知"><strong>已選 ${bulkSelected.size} 筆</strong><span>設定通知時間：</span>${PRESET_DAYS.map(n=>`<button type="button" class="btn btn-xs btn-default" data-bulk-apply="${n}" ${bulkBusy?'disabled':''}>日期前 ${n} 天 08:30</button>`).join('')}<button type="button" class="btn btn-xs btn-default" data-bulk-apply="clear" ${bulkBusy?'disabled':''}>清除通知</button><button type="button" class="btn btn-xs btn-link" data-bulk-clear ${bulkBusy?'disabled':''}>取消選取</button>${bulkBusy?'<span class="fc-muted">處理中…</span>':''}</div>` : '';
    const rows = list.map(r => {
      const d = dueParts(r, true), hasDate = !!calendar.parseDate(r.recorded_due_date);
      const n = r.notification_at ? calendar.dayDiff(data.today, r.notification_at.slice(0,10)) : null;
      const pri = r.reminder_priority!=='一般'?`<span class="fc-chip ${r.reminder_priority==='高優先'?'fc-priority-high':'fc-priority-low'}">${esc(r.reminder_priority)}</span>`:'';
      const source = hasDate ? `<span class="fc-date-tag fc-date-source">${esc(sourceDateLabel(r))}</span>${esc(r.recorded_due_date)}<small class="${d.past?'fc-past':''}">${esc(d.rel||'')}${d.past&&!r.manual?'，待核驗':''}</small>` : `<span class="fc-muted">${esc(d?.label||'日期未填')}</span>`;
      const notify = r.notification_at ? `<span class="fc-date-tag fc-date-notify">通知</span>${esc(r.notification_at.slice(0,16))}<small>${n===null?'':n<0?'已到通知時間':esc(relDay(n))}</small>` : '<span class="fc-muted">未設定</span>';
      return `<tr><td><input type="checkbox" aria-label="選取 ${esc(r.plate)} ${esc(r.description)}" data-bulk="${esc(r.record_id)}" ${bulkSelected.has(r.record_id)?'checked':''} ${bulkBusy?'disabled':''}></td><td><button type="button" data-open="${esc(r.asset_id)}">${esc(r.plate)}</button></td><td>${text(r.description)}${pri}${r.manual?'<span class="fc-chip fc-manual">手動新增</span>':''}</td><td>${source}</td><td>${notify}</td></tr>`;
    }).join('');
    return `${bar}<div class="fc-count">${list.length} 筆日期提醒 · 依顯示日期排序，同日高優先排前；效力尚待核驗 · 勾選後可批次設定通知時間</div><div class="fc-table-wrap"><table class="fc-table"><thead><tr><th><input type="checkbox" aria-label="全選目前清單" data-bulk-all ${allSelected?'checked':''} ${bulkBusy||!visible.length?'disabled':''}></th><th>車牌／主檔</th><th>項目</th><th>證件／記錄日期</th><th>預定通知（台北）</th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="fc-empty">這個條件沒有提醒。</td></tr>'}</tbody></table></div>`;
  }
  async function applyBulk(preset) {
    const items = data.reminders.filter(r => bulkSelected.has(r.record_id));
    const plan = items.map(r => ({r, value: preset === 'clear' ? null : presetValue(r.recorded_due_date, Number(preset))}));
    const todo = plan.filter(x => preset === 'clear' || x.value), skipped = plan.length - todo.length;
    if (!todo.length) { frappe.msgprint('選取的提醒都沒有證件／記錄日期，無法推算通知時間。'); return; }
    const past = todo.filter(x => x.value && x.value.slice(0,10) < data.today).length;
    const label = preset === 'clear' ? '清除預定通知' : `設定為日期前 ${preset} 天 08:30`;
    const message = `將為 ${todo.length} 筆提醒${label}。${skipped?`另有 ${skipped} 筆沒有日期，會略過。`:''}${past?`其中 ${past} 筆的通知時間已過，儲存後會立即發出通知。`:''}`;
    frappe.confirm(esc(message), async () => {
      bulkBusy = true; renderResults();
      let ok = 0, failed = 0;
      for (const {r, value} of todo) {
        try {
          if (r.manual) {
            await Promise.resolve(frappe.call({method:'fleet_bridge.manual_reminders.dashboard_manual_update', type:'POST',
              args:{reminder_id:r.record_id, expected_version:r.version, values:{category:r.category, description:r.description,
                due_date:r.recorded_due_date, notification_at:value, notes:r.notes||''}}}));
          } else {
            await Promise.resolve(frappe.call({method:'fleet_bridge.reminders.dashboard_notification_update', type:'POST',
              args:{asset_id:r.asset_id, reminder_id:r.record_id, notification_at:value, expected_version:r.version, request_id:requestId()}}));
          }
          ok++;
        } catch (e) { failed++; }
      }
      bulkBusy = false; bulkSelected.clear();
      frappe.msgprint(`批次設定完成：成功 ${ok} 筆${skipped?`，略過 ${skipped} 筆（沒有日期）`:''}${failed?`，失敗 ${failed} 筆（可能已被其他操作修改，請重新整理後再試）`:''}。每筆修改都已留下稽核紀錄。`);
      await load(true);
    });
  }
  function noteHint(notes) {
    return String(notes || "").split(/\r?\n/).map(s=>s.trim()).find(s=>s && !s.startsWith("（依")) || "";
  }
  function eventButton(r) {
    const priorityClass = r.reminder_priority === "高優先" ? "fc-high" : r.reminder_priority === "低優先" ? "fc-low" : "";
    const priorityText = r.reminder_priority === "一般" ? "" : ` · ${esc(r.reminder_priority)}`;
    const time = r.notification_at ? `通知日 ${r.notification_at.slice(11,16)}` : "";
    const title = r.description && r.description !== r.category ? r.description : "";
    const due = dueParts(r), hint = noteHint(r.notes);
    return `<button type="button" class="fc-event fc-event-${categoryClass(r.category)} ${priorityClass}" data-event="${esc(r.record_id)}" title="${esc(r.plate)}｜${esc(r.description)} · ${esc(sourceDateLabel(r))} ${esc(r.recorded_due_date||'未填')}${r.notification_at?' · 預定通知 '+esc(r.notification_at):''} · ${esc(r.reminder_priority)} · ${verification(r)}"><span class="fc-event-head"><span>${esc(r.category)}${priorityText}</span>${time?`<span class="fc-event-time">${esc(time)}</span>`:''}</span><span class="fc-event-title"><strong>${esc(r.plate)}</strong>${title?` ${esc(title)}`:''}</span>${due?`<span class="fc-event-due ${due.past?'fc-event-overdue':''}">${dueHtml(due)}</span>`:''}${hint?`<span class="fc-event-note" title="${esc(hint)}">${esc(hint)}</span>`:''}</button>`;
  }
  function showReminder(id, existingDialog) {
    const r = data.reminders.find(item=>item.record_id===id);
    if (!r) return;
    const asset = data.assets.find(a=>a.id===r.asset_id);
    const dialog = existingDialog || new frappe.ui.Dialog({title:"提醒摘要",size:"large",fields:[{fieldtype:"HTML",fieldname:"summary"}]});
    activeReminder = {id, dialog};
    if (!existingDialog) {
      dialog.$wrapper.find('.btn-modal-close').attr('aria-label','關閉提醒摘要');
      dialog.$wrapper.on('hidden.bs.modal',()=>{if(activeReminder?.dialog===dialog)activeReminder=null;});
    }
    const field = dialog.fields_dict.summary.$wrapper;
    const priorityClass = r.reminder_priority === "高優先" ? "fc-priority-high" : r.reminder_priority === "低優先" ? "fc-priority-low" : "";
    field.html(`<div class="fc-summary"><h3>${esc(r.plate)}｜${text(r.description)}</h3><p><span class="fc-chip">${esc(r.category)}</span> <span class="fc-chip ${priorityClass}">${esc(r.reminder_priority)}</span> <strong>${verification(r)}</strong></p><dl><dt>${esc(sourceDateLabel(r))}</dt><dd>${calendar.parseDate(r.recorded_due_date)?`${esc(r.recorded_due_date)}（${esc(sourceCountdown(r).text)}）`:'日期待補'}</dd><dt>預定通知（台北）</dt><dd>${r.notification_at?esc(r.notification_at):'未設定'}</dd></dl><p>${r.manual?'此提醒由使用者在車隊首頁手動新增，不屬於舊系統匯入資料。':'原記錄日期不代表已確認證件效力；驗車定檢日不是到期日，前後一個月內都可驗車。'}</p><h4>完整備註</h4><div class="fc-summary-notes">${text(r.notes)}</div><div class="fc-summary-actions"><button type="button" class="btn btn-primary" data-summary-vehicle>查看車輛完整資料</button><a class="btn btn-default" data-summary-link href="${route(r.manual?'Fleet Manual Reminder':'Fleet Legacy Record',r.record_id)}">開啟提醒原始表單</a>${asset?.native?`<a class="btn btn-default" data-summary-link href="${route(asset.native.doctype,asset.native.name)}">開啟車籍表單</a>`:''}</div></div>`);
    field.find('[data-summary-vehicle]').on('click',()=>{
      dialog.hide(); selected=r.asset_id; highlighted=r.record_id; view='assets'; render();
      const target=Array.from(root.querySelectorAll('[data-record]')).find(el=>el.dataset.record===r.record_id);
      target?.scrollIntoView({block:'center'}); target?.focus({preventScroll:true});
    });
    field.find('[data-summary-link]').on('click',()=>{saveState();dialog.hide();});
    if (!existingDialog) dialog.show();
  }
  function showDay(day) {
    const rows = filteredReminders(true).filter(r=>r.effective_date===day);
    const dialog = new frappe.ui.Dialog({title:day+" · 當日提醒",size:"large",fields:[{fieldtype:"HTML",fieldname:"day"}]});
    dialog.fields_dict.day.$wrapper.html(`<div class="fc-day-dialog">${rows.map(eventButton).join('')}</div>`);
    dialog.fields_dict.day.$wrapper.on('click','[data-event]',function(){const id=this.dataset.event;dialog.hide();showReminder(id);});
    dialog.show();
  }
  function renderCalendar() {
    const model = calendar.monthData(filteredReminders(true),month);
    const days = Object.entries(model.buckets).filter(([,rs])=>rs.length);
    const rows = Object.values(model.buckets).flat();
    return `<section class="fc-calendar" aria-label="車隊行事曆"><div class="fc-calendar-toolbar"><div class="fc-month-controls"><button type="button" class="btn btn-default" data-month-step="-1" aria-label="上個月">‹</button><label>月份 <input type="month" aria-label="行事曆月份" value="${month}" min="1000-01" max="9999-12"></label><button type="button" class="btn btn-default" data-month-step="1" aria-label="下個月">›</button><button type="button" class="btn btn-default" data-today>回到今天</button></div><div class="fc-count" aria-live="polite">${month} · ${model.count} 筆提醒（高優先 ${rows.filter(r=>r.reminder_priority==='高優先').length} · 低優先 ${rows.filter(r=>r.reminder_priority==='低優先').length}） · 日期待補 ${model.undated.length} 筆</div></div><div class="fc-legend">${['保險','通行證','驗車','其他提醒'].map(c=>`<span class="fc-event-${categoryClass(c)}">${c}</span>`).join('')}<span class="fc-priority-high">高優先</span><span>點選項目查看摘要</span></div><div class="fc-calendar-desktop"><div class="fc-weekdays">${['日','一','二','三','四','五','六'].map(d=>`<span>${d}</span>`).join('')}</div><div class="fc-month-grid">${model.cells.map(day=>day?`<div class="fc-calendar-day ${day===data.today?'fc-is-today':''}" data-date="${day}" aria-label="${day}"><div class="fc-day-number">${Number(day.slice(-2))}${day===data.today?'<span>今天</span>':''}</div>${model.buckets[day].slice(0,3).map(eventButton).join('')}${model.buckets[day].length>3?`<button type="button" class="fc-more" data-more-day="${day}">還有 ${model.buckets[day].length-3} 筆</button>`:''}</div>`:'<div class="fc-calendar-day fc-outside" aria-hidden="true"></div>').join('')}</div></div><div class="fc-calendar-mobile">${days.map(([day,rows])=>`<section class="fc-agenda-day"><h4>${esc(calendar.dayLabel(day))}<small class="fc-agenda-rel">${relDay(calendar.dayDiff(data.today,day))}</small></h4>${rows.map(eventButton).join('')}</section>`).join('')}</div>${model.count===0?'<div class="fc-empty" role="status">這個月份與篩選條件沒有提醒。</div>':''}<details class="fc-undated" ${model.undated.length?'open':''}><summary>日期待補（${model.undated.length}）</summary>${model.undated.map(eventButton).join('')||'<p class="fc-empty">目前篩選內的提醒都有有效日期。</p>'}</details></section>`;
  }
  function reminderRow(a, r) {
    // A passed notification is not an expiry; only an expired source date gets the warning colour.
    const src = calendar.parseDate(r.recorded_due_date) ? sourceCountdown(r) : null;
    const past = src?.past ? `<span class="fc-chip fc-past">${esc(src.text)}${r.manual?'':'・待核驗'}</span>` : '';
    const noticeDue = r.days_remaining!==null && r.days_remaining<0;
    const actions = r.manual
      ? `<span class="fc-reminder-actions"><button type="button" class="btn btn-xs btn-default" data-manual-edit="${esc(r.record_id)}" data-manual-asset="${esc(a.id)}">編輯</button><button type="button" class="btn btn-xs btn-default" data-manual-delete="${esc(r.record_id)}" data-manual-asset="${esc(a.id)}">刪除</button></span>`
      : `<button type="button" class="btn btn-xs btn-default" data-notification="${esc(r.record_id)}" data-notification-asset="${esc(a.id)}">調整時間</button>`;
    return `<div class="fc-reminder ${highlighted===r.record_id?'fc-highlighted':''}" data-record="${esc(r.record_id)}" tabindex="-1"><div>${text(r.description)}<br>${r.manual?'<span class="fc-chip fc-manual">手動新增</span>':''}<span class="fc-chip">${esc(sourceDateLabel(r))}</span>${past}</div><time>${src?`${esc(r.recorded_due_date)}（${esc(src.text)}）`:'日期待補'}</time><p>${r.notification_at?`預定通知（台北）：${esc(r.notification_at)}${noticeDue?'（已到通知時間）':''}`:'預定通知：未設定'} ${actions}</p>${r.notes?`<p>${esc(r.notes)}</p>`:''}</div>`;
  }
  function manualDialog(a, r) {
    const dialog = new frappe.ui.Dialog({title:`${a.plate} · ${r?'編輯提醒事項':'新增提醒事項'}`, size:'large', fields:[
      {fieldtype:'Select',fieldname:'category',label:'提醒類別',options:['保險','通行證','驗車','其他提醒'],reqd:1,default:r?.category||'其他提醒'},
      {fieldtype:'Data',fieldname:'description',label:'提醒名稱',reqd:1,length:140,default:r?.description||'',description:'例如：第三人保險、驗車、通行證續辦。'},
      {fieldtype:'Date',fieldname:'due_date',label:'提醒日期',reqd:1,default:r?.recorded_due_date||''},
      {fieldtype:'Datetime',fieldname:'notification_at',label:'預定通知時間（台北，選填）',default:r?.notification_at||'',description:'設定後會在 ERP 站內通知管理員，行事曆也改以通知日期顯示；留空則不通知。'},
      {fieldtype:'HTML',fieldname:'presets'},
      {fieldtype:'Small Text',fieldname:'notes',label:'備註',default:r?.notes||''}]});
    let pendingRequest = null;
    wirePresets(dialog, () => dialog.get_value('due_date'), 'notification_at', '提醒日期');
    dialog.set_primary_action('儲存', values => {
      let notification = values.notification_at || null;
      if (notification && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(notification)) notification += ':00';
      const payload = {category:values.category, description:(values.description||'').trim(), due_date:values.due_date||null,
        notification_at:notification, notes:values.notes||''};
      if (!payload.description) {frappe.msgprint('請填寫提醒名稱。');return;}
      if ((payload.notes||'').length > 10000) {frappe.msgprint('備註不可超過 10000 字。');return;}
      let call;
      if (r) {
        call = frappe.call({method:'fleet_bridge.manual_reminders.dashboard_manual_update',type:'POST',
          args:{reminder_id:r.record_id,values:payload,expected_version:r.version}});
      } else {
        // Reuse the request id for an identical retry so a lost response cannot create a duplicate.
        const key = JSON.stringify(payload);
        if (pendingRequest?.key !== key) pendingRequest = {key, id:requestId()};
        call = frappe.call({method:'fleet_bridge.manual_reminders.dashboard_manual_create',type:'POST',
          args:{asset_id:a.id,values:payload,request_id:pendingRequest.id}});
      }
      dialog.get_primary_btn().prop('disabled',true);
      Promise.resolve(call)
        .then(()=>{dialog.hide();frappe.show_alert({message:r?'提醒事項已更新。':'提醒事項已新增，行事曆同步顯示。',indicator:'green'});return load(true);})
        .catch(error=>frappe.msgprint(saveError(error)))
        .finally(()=>dialog.get_primary_btn().prop('disabled',false));
    });
    dialog.show();
  }
  function detail(a) {
    if (!a) return `<div class="fc-empty">沒有符合條件的資料，請調整搜尋或分類。</div>`;
    const p = a.profile;
    let html = `${highlighted?'<button type="button" class="btn btn-default fc-return" data-return-calendar>← 返回行事曆</button>':''}<h3>${esc(a.plate)}</h3><div class="fc-subtitle">${esc(labels[a.kind])} · ${text(p.make)} ${esc(p.model || "")}</div><div style="margin-top:9px">${p.legacy_tags.map(t=>`<span class="fc-chip fc-tag">${esc(t)}</span>`).join("")}</div>
      <dl class="fc-meta"><div><dt>營運狀態</dt><dd>${esc(a.status)}</dd></div><div><dt>原登錄年份</dt><dd>${text(p.source_year)}</dd></div><div><dt>提醒／歷史紀錄</dt><dd>${p.reminders.length} ／ ${p.historical_records.length + p.notes.length} 筆</dd></div></dl>`;
    html += `<section class="fc-general-note"><div class="fc-general-note-head"><h4>車輛通用備註</h4><button type="button" class="btn btn-default" data-note="${esc(a.id)}">${p.general_note?'編輯備註':'新增備註'}</button></div><p>${p.general_note?esc(p.general_note):'尚未填寫。這裡記錄整台車共用事項，不屬於單一證件或提醒。'}</p></section><div class="fc-priority"><label for="fleet-priority">提醒優先度</label><select id="fleet-priority" data-priority-for="${esc(a.id)}" aria-label="提醒優先度">${['高優先','一般','低優先'].map(value=>`<option ${a.reminder_priority===value?'selected':''}>${value}</option>`).join('')}</select><button type="button" class="btn btn-default" data-priority-history="${esc(a.id)}">查看操作紀錄</button><small>高優先醒目顯示並排前；低優先可隱藏。每次修改由伺服器自動留下不可更改的紀錄。</small></div>`;
    html += `<div class="fc-manual-bar"><h4>提醒事項</h4><button type="button" class="btn btn-primary btn-sm" data-manual-add="${esc(a.id)}">＋ 新增提醒事項</button></div>`;
    for (const label of ["保險", "通行證", "驗車", "其他提醒"]) {
      const items = p.reminders.filter(r => r.category === label);
      if (!items.length) continue;
      html += `<section class="fc-section"><h4>${label} <span class="fc-chip">${items.length}</span></h4>${items.map(r=>reminderRow(a,r)).join("")}</section>`;
    }
    if (!p.reminders.length) html += '<p class="fc-empty">目前沒有日期提醒，可按「新增提醒事項」加入。</p>';
    if (p.historical_records.length || p.notes.length) html += `<section class="fc-section"><h4>歷史紀錄與備註</h4>${[...p.historical_records,...p.notes].map(r=>`<div class="fc-reminder"><div>${text(r.description)}</div><time>${text(r.date)}</time><p>${esc(r.notes)}${r.source_cost!=null?`\n原記錄金額：${esc(r.source_cost)}（幣別未提供）`:''}</p></div>`).join("")}</section>`;
    html += `<div class="fc-source">${text(p.review_notes)}${p.source?`<br>來源：LubeLogger 舊系統匯入 · ${esc(p.source.batch_id)}<br>標籤沿用舊紀錄，不代表已確認司機或所有權。`:''}<div class="fc-links">${a.native?`<a href="${route(a.native.doctype,a.native.name)}">開啟車籍表單 →</a>`:''}${a.archive_id?`<a href="${route('Fleet Legacy Asset',a.archive_id)}">查看原始來源 →</a>`:''}</div></div>`;
    return html;
  }
  const roadTime = v => v ? esc(String(v).slice(0,16)) : '未提供';
  const roadAll = () => roadData?.installed ? [...roadData.active, ...roadData.unknown, ...roadData.ended] : [];
  const roadLatest = e => roadData.last_success_run && e.last_change_run === roadData.last_success_run;
  function roadEvents() {
    if (!roadData?.installed) return [];
    let list = roadFilter === 'truck' ? roadData.active.filter(e => ['等同封閉','通過時注意'].includes(e.truck_impact))
      : roadFilter === 'major' ? roadData.active.filter(e => e.is_major || e.special_vehicle_control)
      : roadFilter === 'latest' ? roadAll().filter(roadLatest)
      : roadFilter === 'unknown' ? roadData.unknown
      : roadFilter === 'ended' ? roadData.ended : roadData.active;
    if (roadClass === 'b1') list = list.filter(e => e.permit_route);
    else if (roadClass !== 'all') list = list.filter(e => e.road_class === roadClass);
    const q = roadQuery.trim().toLowerCase();
    if (q) list = list.filter(e => [e.road, e.location_text, e.title, e.description, e.feed_label, e.event_type_label, e.regulation_labels].join(' ').toLowerCase().includes(q));
    return list;
  }
  const truckChip = e => e.truck_impact === '等同封閉' ? '<span class="fc-chip fc-road-major">曳引車：等同封閉</span>'
    : e.truck_impact === '通過時注意' ? '<span class="fc-chip fc-road-special">曳引車：通過時注意</span>'
    : e.truck_impact === '狀態不明' ? '<span class="fc-chip fc-road-unknown">曳引車：狀態不明</span>' : '';
  function roadChips(e) {
    return `${truckChip(e)}${e.is_major&&e.truck_impact!=='等同封閉'?'<span class="fc-chip fc-road-major">重大管制</span>':''}${e.special_vehicle_control?'<span class="fc-chip fc-road-special">特定車種管制需求</span>':''}${e.permit_route?`<span class="fc-chip fc-road-b1" title="${esc(e.permit_segment)}">B1：${esc(e.permit_route)}（區段待核對）</span>`:''}${roadLatest(e)&&['新增','變更'].includes(e.status)?`<span class="fc-chip fc-road-new">本輪${esc(e.status)}</span>`:''}${e.status==='狀態不明'?'<span class="fc-chip fc-road-unknown">狀態不明</span>':''}${e.status==='已結束'?'<span class="fc-chip">已結束</span>':''}`;
  }
  const LEAFLET = {
    js: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
    jsSri: 'sha384-NElt3Op+9NBMCYaef5HxeJmU4Xeard/Lku8ek6hoPTvYkQPh3zLIrJP7KiRocsxO',
    css: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
    cssSri: 'sha384-c6Rcwz4e4CITMbu/NBmnNS8yN2sC3cUElMEMfP3vqqKFp7GOYaaBBCqmaWBjmkjb'};
  const NLSC_TILES = 'https://wmts.nlsc.gov.tw/wmts/EMAP16/default/GoogleMapsCompatible/{z}/{y}/{x}';
  let leafletLoading = null, roadMap = null, roadMapView = null;
  function loadLeaflet() {
    if (window.L && window.L.map) return Promise.resolve(window.L);
    if (leafletLoading) return leafletLoading;
    leafletLoading = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet'; css.href = LEAFLET.css; css.integrity = LEAFLET.cssSri; css.crossOrigin = 'anonymous';
      document.head.appendChild(css);
      const script = document.createElement('script');
      script.src = LEAFLET.js; script.integrity = LEAFLET.jsSri; script.crossOrigin = 'anonymous';
      script.onload = () => window.L ? resolve(window.L) : reject(new Error('Leaflet missing'));
      script.onerror = () => reject(new Error('Leaflet failed to load'));
      document.head.appendChild(script);
    }).catch(error => { leafletLoading = null; throw error; });
    return leafletLoading;
  }
  const MAP_COLORS = {closed:'#d32f2f', caution:'#ef6c00', other:'#8a9096'};
  const MAP_LABELS = {closed:'曳引車等同封閉', caution:'通過時注意', other:'其他事件'};
  // Road-sign style pins: red no-entry disc = 等同封閉, orange warning triangle = 通過時注意, small grey dot = other.
  const pinSvg = (kind, size, label = '') => {
    const a = label ? `role="img" aria-label="${esc(label)}"` : 'aria-hidden="true"';
    return kind === 'closed'
      ? `<svg viewBox="0 0 24 24" width="${size}" height="${size}" ${a}><circle cx="12" cy="12" r="10.5" fill="${MAP_COLORS.closed}" stroke="#fff" stroke-width="2"/><rect x="6" y="10.2" width="12" height="3.6" rx=".8" fill="#fff"/></svg>`
      : kind === 'caution'
      ? `<svg viewBox="0 0 24 24" width="${size}" height="${size}" ${a}><path d="M12 2.2 22.8 21H1.2Z" fill="${MAP_COLORS.caution}" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 8.6v6" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="17.7" r="1.35" fill="#fff"/></svg>`
      : `<svg viewBox="0 0 24 24" width="${size}" height="${size}" ${a}><circle cx="12" cy="12" r="6" fill="${MAP_COLORS.other}" stroke="#fff" stroke-width="2"/></svg>`;
  };
  const markerKind = e => e.truck_impact === '等同封閉' || e.is_major ? 'closed' : e.truck_impact === '通過時注意' ? 'caution' : 'other';
  const MAP_ORDER = ['other', 'caution', 'closed'];
  // all = false keeps only what matters to a tractor-trailer: few, meaningful pins (other events are one click away)
  const mapPoints = (list, all = true) => list.filter(e => typeof e.lat === 'number' && typeof e.lon === 'number')
    .map(e => ({e, lat: e.lat, lon: e.lon, kind: markerKind(e)}))
    .filter(p => all || p.kind !== 'other')
    .sort((a, b) => MAP_ORDER.indexOf(a.kind) - MAP_ORDER.indexOf(b.kind));
  const mapCause = e => {
    const lanes = String(e.blocked_lanes_label || '').split('｜')[0];
    const what = [e.event_type_label, e.regulation_labels].filter(Boolean).join('｜');
    if (lanes && !['無阻斷車道', '阻斷車道不明'].includes(lanes)) return `${what ? esc(what) + '：' : ''}${esc(lanes)}受阻`;
    return esc(what || e.title || '');
  };
  const popupHtml = e => {
    const k = markerKind(e);
    const note = e.status === '狀態不明' ? '（最新資料未再出現，狀態不明）' : e.status === '已結束' ? '（已結束）'
      : ['新增', '變更'].includes(e.status) ? `（本輪${esc(e.status)}）` : '';
    return `<div class="fc-map-popup"><div class="fc-pop-head" style="color:${MAP_COLORS[k]}">${k === 'other' ? esc(e.event_type_label || '道路事件') : MAP_LABELS[k]}${note}</div><p>${text(e.location_text || e.road || e.feed_label)}</p><p>${mapCause(e)}</p>${e.permit_route ? `<p>B1 授權路線 ${esc(e.permit_route)}（區段待核對）</p>` : ''}<button type="button" class="btn btn-xs btn-default" data-road-event="${esc(e.name)}">詳細資料</button></div>`;
  };
  async function mountRoadMap() {
    const box = content.querySelector('[data-road-map]');
    if (!box) return;
    let L;
    try { L = await loadLeaflet(); }
    catch (error) { box.innerHTML = '<div class="fc-empty" role="alert">地圖元件無法載入（需要能連到 cdnjs.cloudflare.com 與國土測繪中心圖磚）。列表模式不受影響。</div>'; return; }
    if (!box.isConnected) return;  // the page re-rendered while the library was loading
    if (roadMap) { roadMapView = {center: roadMap.getCenter(), zoom: roadMap.getZoom()}; roadMap.remove(); roadMap = null; }
    roadMap = L.map(box, {preferCanvas: true}).setView(roadMapView ? roadMapView.center : [23.75, 120.95], roadMapView ? roadMapView.zoom : 8);
    L.tileLayer(NLSC_TILES, {maxZoom: 19, attribution: '底圖：內政部國土測繪中心｜事件：交通部 TDX'}).addTo(roadMap);
    for (const p of mapPoints(roadEvents(), mapAll)) {
      const faded = ['狀態不明', '已結束'].includes(p.e.status);
      const name = p.kind === 'other' ? (p.e.event_type_label || '道路事件') : MAP_LABELS[p.kind];
      const size = p.kind === 'closed' ? 24 : 22;
      const marker = p.kind === 'other'
        ? L.circleMarker([p.lat, p.lon], {radius: 4, color: '#ffffff', weight: 1, fillColor: MAP_COLORS.other, fillOpacity: faded ? 0.4 : 0.85})
        : L.marker([p.lat, p.lon], {icon: L.divIcon({className: 'fc-pin', html: pinSvg(p.kind, size, `${name}：${p.e.location_text || p.e.road || ''}`),
            iconSize: [size, size], iconAnchor: [size / 2, size / 2], popupAnchor: [0, -10], tooltipAnchor: [0, -12]}),
            opacity: faded ? 0.5 : 1, zIndexOffset: p.kind === 'closed' ? 1000 : 0, riseOnHover: true});
      marker.bindPopup(popupHtml(p.e)).bindTooltip(`${esc(name)}｜${esc(p.e.road || p.e.feed_label)}`, {direction: 'top'}).addTo(roadMap);
    }
  }
  const roadSignature = d => d && d.installed ? JSON.stringify([d.active, d.unknown, d.ended].map(l => l.map(e => [e.name, e.status, e.truck_impact, e.last_change_at]))) : '';
  function renderRoad() {
    if (!roadData) {
      if (!roadLoading && !roadError) loadRoad();
      return roadError ? '<div class="fc-empty" role="alert">無法讀取道路情報。<button type="button" class="btn btn-xs btn-default" data-road-retry>重試</button>（本頁未修改任何資料）</div>' : '<div class="fc-busy" role="status">正在讀取道路情報…</div>';
    }
    if (!roadData.installed) return '<div class="fc-empty">道路情報模組尚未安裝。</div>';
    const d = roadData, u = d.usage, last = d.latest_run;
    const okStatus = d.api_status === '正常' || d.api_status.startsWith('更新');
    const status = `<div class="fc-road-status">
      <div class="${d.stale?'fc-road-stale':''}"><span>最後成功更新（台北）</span><strong>${d.last_success_at?roadTime(d.last_success_at):'尚無成功紀錄'}</strong><small>${d.stale?'最近一次排程未成功，資料可能過期':'定時巡檢，不是即時預警'}</small></div>
      <div><span>下次預定更新</span><strong>${d.enabled?roadTime(d.next_run_at):'未啟用定時更新'}</strong><small>每日 ${esc(d.schedule)}</small></div>
      <div class="${okStatus?'':'fc-road-bad'}"><span>API 服務狀態</span><strong>${esc(d.api_status)}</strong><small>${last?`${esc(last.run_trigger)}｜${esc(last.status)}｜${roadTime(last.finished_at||last.started_at||last.creation)}`:'尚未執行'}${last?.error?`<br>${esc(last.error.split('\n')[0])}`:''}</small></div>
      <div class="${u.exceeded?'fc-road-bad':''}"><span>本月用量估計（${esc(u.month)}）</span><strong>${esc(u.estimated_points)} / ${esc(u.budget)} 點</strong><small>${esc(u.data_calls)} 次呼叫 · ${esc(u.wire_mb)} MB · 以 TDX 會員中心為準</small></div></div>`;
    const actions = `<div class="fc-road-actions"><button type="button" class="btn btn-sm btn-primary" data-road-refresh ${roadRequesting||!d.configured?'disabled':''}>手動更新</button><a href="/app/fleet-road-info-settings">道路情報設定</a><a href="/app/fleet-road-sync-run">更新紀錄</a><a href="/app/fleet-road-event">全部事件</a><small class="fc-low-count">資料集：${esc(d.feeds.join('、')||'未啟用')}${d.lane_table?.refreshed_at?`｜國道車道數 ${roadTime(d.lane_table.refreshed_at)} 更新`:'｜國道車道數尚未取得'}</small></div>`;
    const list = roadEvents();
    const rowHtml = e=>`<tr class="fc-road-row"><td>${esc(e.status)}<br>${roadChips(e)}</td><td><button type="button" data-road-event="${esc(e.name)}">${text(e.road||e.feed_label)}</button><small>${text(e.location_text)}</small><small>${esc(e.feed_label)}</small></td><td>${esc(e.event_type_label)}｜${text(e.title)}</td><td>${text(e.regulation_labels)}${e.blocked_lanes_label?`<small>阻斷：${esc(e.blocked_lanes_label)}</small>`:''}<small>影響：${esc(e.severity_label||'未提供')}${e.block_way_label&&e.block_way_label!=='未提供'?`｜${esc(e.block_way_label)}`:''}${e.blocked_lanes?`｜${esc(e.blocked_lanes)}`:''}</small></td><td>起：${roadTime(e.effective_time)}<small>官方結束：${e.expire_time?roadTime(e.expire_time):'未提供'}</small><small>來源更新：${roadTime(e.source_update_time)}</small>${e.missing_since?`<small>未再出現起：${roadTime(e.missing_since)}</small>`:''}</td></tr>`;
    const groups = roadClass === 'all'
      ? ['國道','省道','縣市道路'].map(g => [g, list.filter(e => e.road_class === g)]).filter(([, l]) => l.length)
      : [[null, list]];
    const rows = groups.map(([g, l]) => (g ? `<tr class="fc-group-row"><th colspan="5">${esc(g)}（${l.length}）</th></tr>` : '') + l.map(rowHtml).join('')).join('');
    const counts = {current:d.active.length, truck:d.active.filter(e=>['等同封閉','通過時注意'].includes(e.truck_impact)).length,
      major:d.active.filter(e=>e.is_major||e.special_vehicle_control).length, latest:roadAll().filter(roadLatest).length,
      unknown:d.unknown.length, ended:d.ended.length};
    const scope = roadFilter === 'unknown' ? d.unknown : roadFilter === 'ended' ? d.ended : d.active;
    const classCounts = {all:scope.length, 國道:scope.filter(e=>e.road_class==='國道').length, 省道:scope.filter(e=>e.road_class==='省道').length, b1:scope.filter(e=>e.permit_route).length};
    const chips = `<div class="fc-chips" role="group" aria-label="道路類別"><span class="fc-chip-label">道路</span>${Object.entries({all:'全部',國道:'國道',省道:'省道',b1:'B1 授權路線'}).map(([k,v])=>chip('roadclass',k,`${v}（${classCounts[k]}）`,roadClass===k)).join('')}</div><div class="fc-chips" role="group" aria-label="顯示方式"><span class="fc-chip-label">顯示</span>${chip('roadmode','list','列表',roadMode==='list')}${chip('roadmode','map','地圖',roadMode==='map')}</div><div class="fc-chips" role="group" aria-label="道路事件範圍"><span class="fc-chip-label">範圍</span>${Object.entries(ROAD_FILTERS).map(([k,v])=>chip('road',k,`${v}（${counts[k]}）`,roadFilter===k)).join('')}</div>`;
    const b1Note = roadClass === 'b1' ? `<div class="fc-notice">B1 授權路線：${esc(d.b1?.route_count ?? 0)} 條省道${d.b1?.includes_freeway?'＋國道（不含超長類別）':''}，以整條路線比對；是否落在授權區段內請依通行證核對（滑鼠移到 B1 標籤可看區段）。台1線與台74甲線叉路口人行陸橋限高 4.1 公尺、禁行。清單可在「道路情報設定」修改。</div>` : '';
    return `${status}${actions}${chips}${b1Note}<div class="fc-notice">${esc(d.notice)}重大管制判定：${esc(d.major_rule)}。</div><div class="fc-count">${list.length} 筆 · 目前事件 ${d.active.length} · 狀態不明（近 7 日）${d.unknown.length} · 已結束（近 2 日）${d.ended.length}</div>${roadMode === 'map' ? (() => { const pts = mapPoints(list), n = k => pts.filter(p => p.kind === k).length, missing = list.length - pts.length;
      return `<div class="fc-map-legend"><span>${pinSvg('closed', 18)}曳引車等同封閉 <b>${n('closed')}</b></span><span>${pinSvg('caution', 18)}通過時注意 <b>${n('caution')}</b></span>${n('other') ? chip('mapall', '1', `顯示其他事件（${n('other')}）`, mapAll) : ''}<small>${!n('closed') && !n('caution') ? '這個條件沒有影響曳引車的事件。' : ''}${missing ? `${missing} 筆沒有座標，請看列表。` : ''}點圖示看說明。</small></div><div class="fc-map" data-road-map role="region" aria-label="道路事件地圖"></div>`; })() : `<div class="fc-table-wrap"><table class="fc-table"><thead><tr><th>狀態</th><th>道路／位置</th><th>事件</th><th>管制方式</th><th>時間（台北）</th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="fc-empty">這個條件沒有道路事件。</td></tr>'}</tbody></table></div>`}`;
  }
  function showRoadEvent(name) {
    const e = roadAll().find(x => x.name === name); if (!e) return;
    const official = e.web_url && /^https:\/\//i.test(e.web_url) ? `<a class="btn btn-default btn-sm" href="${esc(e.web_url)}" target="_blank" rel="noopener noreferrer">官方公告頁面</a>` : '';
    // Frappe renders dialog titles as HTML, so source text must be escaped here too.
    const dialog = new frappe.ui.Dialog({title:`${esc(e.road||e.feed_label)} · ${esc(e.event_type_label)}`, size:'large', fields:[{fieldtype:'HTML',fieldname:'body'}]});
    dialog.fields_dict.body.$wrapper.html(`<div class="fc-summary"><span class="fc-chip">${esc(e.status)}</span>${roadChips(e)}<h3>${text(e.title)}</h3><p>${esc(e.feed_label)}｜${esc(e.event_type_label)}｜通報來源：${text(e.reporting_source)}</p>
      <dl><div><dt>位置</dt><dd>${text(e.location_text)}</dd></div><div><dt>管制方式</dt><dd>${text(e.regulation_labels)}</dd></div><div><dt>影響</dt><dd>${esc(e.severity_label||'未提供')}｜${esc(e.block_way_label||'未提供')}</dd></div></dl>
      <dl><div><dt>曳引車影響</dt><dd>${text(e.truck_impact)}</dd></div><div><dt>判定依據</dt><dd>${text(e.truck_impact_reason)}</dd></div><div><dt>阻斷車道</dt><dd>${text(e.blocked_lanes_label)}</dd></div></dl>
      ${e.permit_route?`<p><strong>B1 授權路線 ${esc(e.permit_route)}</strong>：${esc(e.permit_segment)}（以整條路線比對，是否在授權區段內請依通行證核對）</p>`:''}
      <dl><div><dt>事件起始</dt><dd>${roadTime(e.effective_time)}</dd></div><div><dt>官方結束時間</dt><dd>${e.expire_time?roadTime(e.expire_time):'未提供'}</dd></div><div><dt>來源最後更新</dt><dd>${roadTime(e.source_update_time)}</dd></div></dl>
      <h4>官方描述</h4><div class="fc-road-desc">${text(e.description)}</div>
      ${e.impact_description?`<h4>影響說明</h4><div class="fc-road-desc">${esc(e.impact_description)}</div>`:''}${e.detour_description?`<h4>替代道路</h4><div class="fc-road-desc">${esc(e.detour_description)}</div>`:''}
      <p>首次取得：${roadTime(e.first_seen_at)}｜最後出現：${roadTime(e.last_seen_at)}${e.missing_since?`｜未再出現起：${roadTime(e.missing_since)}（狀態不明，不代表已解除）`:''}</p>
      <p>TDX 未提供限高、限寬、限重欄位；未列出限制不代表特殊車輛可以通行。</p>
      <div class="fc-summary-actions"><a class="btn btn-default btn-sm" href="/app/fleet-road-event/${encodeURIComponent(e.name)}">完整紀錄（含官方原始資料）</a>${official}</div></div>`);
    dialog.show();
  }
  let roadPending = false;
  async function loadRoad(background = false) {
    if (roadLoading) { roadPending = true; return; }  // a newer change arrived mid-request: fetch again after
    roadLoading = true;
    let unchanged = false;
    try {
      const r = await frappe.call({method:'fleet_bridge.road_events.road_overview', type:'GET'});
      unchanged = background && roadMode === 'map' && roadSignature(roadData) === roadSignature(r.message);
      roadData = r.message; roadError = false;
    } catch (e) { if (!background || !roadData) roadError = true; }
    finally {
      roadLoading = false;
      if (view === 'road' && content.querySelector('.fc-results') && !unchanged) renderResults();
      if (roadPending) { roadPending = false; if (!roadError) loadRoad(true); }
    }
  }
  function renderResults() {
    const results = content.querySelector(".fc-results");
    if (view === "road") {
      results.innerHTML = renderRoad();
      if (roadMode === 'map' && roadData?.installed) mountRoadMap();
    } else if (view === "assets") {
      const list = filteredAssets();
      if (!list.some(a=>a.id===selected)) selected = list[0]?.id;
      results.innerHTML = `<div class="fc-grid"><div class="fc-list"><div class="fc-list-head">${list.length} 筆主檔 · 點選查看完整資料</div>${list.map(a=>`<button type="button" class="fc-asset" data-asset="${esc(a.id)}" aria-current="${a.id===selected}"><strong>${esc(a.plate)}</strong><small>${esc(labels[a.kind])} · ${text(a.profile.make)}</small><span class="fc-chip">${a.profile.reminders.length} 筆提醒</span><span class="fc-chip">${esc(a.status)}</span>${a.reminder_priority!=='一般'?`<span class="fc-chip ${a.reminder_priority==='高優先'?'fc-priority-high':'fc-priority-low'}">${esc(a.reminder_priority)}</span>`:''}</button>`).join("") || '<div class="fc-empty">查無資料</div>'}</div><article class="fc-detail" aria-label="車輛詳細資料">${detail(list.find(a=>a.id===selected))}</article></div>`;
    } else if (view === "calendar") {
      results.innerHTML = renderCalendar();
    } else {
      results.innerHTML = reminderTable(filteredReminders());
    }
    saveState();
  }
  function render() {
    const c = data.counts;
    content.innerHTML = `<div class="fc-hero"><div><div class="fc-eyebrow">FLEET OPERATIONS</div><h2>${esc(data.company || "Fleet Center")}</h2><p>車籍、保險、通行證與日期提醒，集中查閱。</p></div><div class="fc-date">資料讀取時間<br>${esc(data.as_of.slice(0,19))}</div></div>
      <div class="fc-cards"><button class="fc-card" data-card="assets"><span>車隊與資料主檔</span><strong>${c.assets}</strong><small>${c.kinds.Truck||0} 貨車 · ${c.kinds.Trailer||0} 拖車 · 其他 ${c.assets-(c.kinds.Truck||0)-(c.kinds.Trailer||0)}</small></button><button class="fc-card" data-card="30"><span>未來 30 天</span><strong>${c.next_30_days}</strong><small>依提醒顯示日期篩選</small></button><button class="fc-card" data-card="90"><span>未來 90 天</span><strong>${c.next_90_days}</strong><small>含未來 30 天</small></button><button class="fc-card" data-card="past"><span>已過提醒日期</span><strong>${c.past_dates}</strong><small>需要核對，未判定失效</small></button></div>
      <div class="fc-notice">${esc(data.notice)}</div><div class="fc-toolbar"><div class="fc-tabs" role="tablist" aria-label="車隊檢視"><button role="tab" data-view="assets" aria-selected="${view==='assets'}" title="Alt+1">車籍總覽</button><button role="tab" data-view="reminders" aria-selected="${view==='reminders'}" title="Alt+2">日期提醒</button><button role="tab" data-view="calendar" aria-selected="${view==='calendar'}" title="Alt+3">行事曆</button><button role="tab" data-view="road" aria-selected="${view==='road'}" title="Alt+4">道路情報</button><span class="fc-shortcuts" aria-hidden="true">Alt+1～4 切換 · / 搜尋</span></div>${view==='road'?`<input type="search" aria-label="搜尋道路、位置或事件" placeholder="搜尋道路、位置或事件" value="${esc(roadQuery)}">`:`<input type="search" aria-label="搜尋車牌、廠牌或標籤" placeholder="搜尋車牌、廠牌或標籤" value="${esc(query)}"><select aria-label="車輛分類" data-filter="kind"><option value="">所有分類</option>${Object.entries(labels).map(([k,v])=>`<option value="${esc(k)}" ${kind===k?'selected':''}>${v}（${c.kinds[k]||0}）</option>`).join('')}</select>`}</div>${reminderChips()}<div class="fc-results"></div>`;
    renderResults();
  }
  root.addEventListener("input", e => {if(e.target.type==='search'){if(view==='road')roadQuery=e.target.value;else query=e.target.value;renderResults();}});
  root.addEventListener("change", e => {
    if(e.target.hasAttribute("data-bulk")){const id=e.target.dataset.bulk;if(e.target.checked)bulkSelected.add(id);else bulkSelected.delete(id);renderResults();return;}
    if(e.target.hasAttribute("data-bulk-all")){const ids=filteredReminders().map(r=>r.record_id);ids.forEach(id=>e.target.checked?bulkSelected.add(id):bulkSelected.delete(id));renderResults();return;}
    if(e.target.hasAttribute("data-priority-for")){
      const a=data.assets.find(x=>x.id===e.target.dataset.priorityFor);if(!a||savingPriority)return;
      savingPriority=true;e.target.disabled=true;
      Promise.resolve(frappe.call({method:"fleet_bridge.dashboard.set_reminder_priority",type:"POST",args:{asset_id:a.id,priority:e.target.value,expected_version:a.priority_version,request_id:requestId()}}))
        .then(()=>{frappe.show_alert({message:"提醒優先度已儲存，行事曆同步套用。",indicator:"green"});return load(true);})
        .catch(error=>{e.target.value=a.reminder_priority;frappe.msgprint(saveError(error));})
        .finally(()=>{savingPriority=false;e.target.disabled=false;});return;
    }
    if(e.target.hasAttribute("data-show-low")){showLow=e.target.checked;renderResults();return;}if(e.target.type==='month'){if(calendar.validMonth(e.target.value)){month=e.target.value;renderResults();}else{e.target.value=month;}return;}const f=e.target.dataset.filter;if(f==='road')roadFilter=e.target.value;if(f==='kind')kind=e.target.value;if(f==='period')period=e.target.value;if(f==='category')category=e.target.value;if(f)renderResults();
  });
  root.addEventListener("click", e => {
    const b=e.target.closest('button'); if(!b)return;
    if(b.dataset.chip){
      const g=b.dataset.chip,v=b.dataset.value;
      if(g==='roadclass'){roadClass=v;renderResults();return;}
      if(g==='roadmode'){roadMode=v;renderResults();return;}
      if(g==='mapall'){mapAll=!mapAll;renderResults();return;}
      if(g==='period')period=v;else if(g==='category')category=v;else if(g==='high')onlyHigh=!onlyHigh;else if(g==='low')showLow=!showLow;else if(g==='road')roadFilter=v;
      if(g==='road')renderResults();else render();
      return;
    }
    if(b.dataset.bulkApply){if(!bulkBusy)applyBulk(b.dataset.bulkApply);return;}
    if(b.hasAttribute('data-bulk-clear')){bulkSelected.clear();renderResults();return;}
    if(b.hasAttribute('data-road-retry')){roadError=false;roadData=null;renderResults();return;}
    if(b.dataset.roadEvent){showRoadEvent(b.dataset.roadEvent);return;}
    if(b.hasAttribute('data-road-refresh')){
      if(roadRequesting)return;roadRequesting=true;b.disabled=true;
      Promise.resolve(frappe.call({method:'fleet_bridge.road_events.road_refresh',type:'POST'}))
        .then(()=>{frappe.show_alert({message:'已排入道路情報更新；免費方案需間隔呼叫，約 1–3 分鐘完成，頁面會自動刷新。',indicator:'green'});return loadRoad(true);})
        .catch(()=>{})
        .finally(()=>{roadRequesting=false;if(view==='road')renderResults();});
      return;
    }
    if(b.dataset.manualAdd){const a=data.assets.find(x=>x.id===b.dataset.manualAdd);if(a)manualDialog(a);return;}
    if(b.dataset.manualEdit||b.dataset.manualDelete){
      const a=data.assets.find(x=>x.id===b.dataset.manualAsset);
      const r=a?.profile.reminders.find(x=>x.manual&&x.record_id===(b.dataset.manualEdit||b.dataset.manualDelete));
      if(!a||!r)return;
      if(b.dataset.manualEdit){manualDialog(a,r);return;}
      frappe.confirm(`確定刪除「${esc(a.plate)}｜${esc(r.description)}」？`,()=>{
        Promise.resolve(frappe.call({method:'fleet_bridge.manual_reminders.dashboard_manual_delete',type:'POST',
          args:{reminder_id:r.record_id,expected_version:r.version}}))
          .then(()=>{frappe.show_alert({message:'提醒事項已刪除。',indicator:'green'});return load(true);})
          .catch(error=>frappe.msgprint(saveError(error)));
      });return;
    }
    if(b.dataset.notification){
      const a=data.assets.find(x=>x.id===b.dataset.notificationAsset);
      const r=a?.profile.reminders.find(x=>x.record_id===b.dataset.notification);
      if(!a||!r)return;
      const dialog=new frappe.ui.Dialog({title:`${a.plate} · ${r.description} · 預定通知時間`,
        fields:[{fieldtype:'Datetime',fieldname:'notification_at',label:'通知日期與時間（台北）',
          default:r.notification_at||'',description:`留空可清除。此欄位與${sourceKind(r)}（${r.recorded_due_date||'未填'}）分開。`},
          {fieldtype:'HTML',fieldname:'presets'}],
        primary_action_label:'儲存'});
      wirePresets(dialog, () => r.recorded_due_date, 'notification_at', sourceKind(r));
      let pendingRequest = null;
      dialog.set_primary_action('儲存',()=>{
        let value=dialog.get_value('notification_at')||null;
        if(value && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(value))value+=':00';
        const key=JSON.stringify([r.version,value]);
        if(pendingRequest?.key!==key)pendingRequest={key,id:requestId()};
        dialog.get_primary_btn().prop('disabled',true);
        Promise.resolve(frappe.call({method:'fleet_bridge.reminders.dashboard_notification_update',type:'POST',
          args:{asset_id:a.id,reminder_id:r.record_id,notification_at:value,
            expected_version:r.version,request_id:pendingRequest.id}}))
          .then(()=>{pendingRequest=null;dialog.hide();frappe.show_alert({message:'預定通知時間已儲存。',indicator:'green'});return load(true);})
          .catch(error=>frappe.msgprint(saveError(error)))
          .finally(()=>dialog.get_primary_btn().prop('disabled',false));
      });dialog.show();return;
    }
    if(b.dataset.priorityHistory){
      const a=data.assets.find(x=>x.id===b.dataset.priorityHistory);if(!a)return;
      b.disabled=true;
      Promise.resolve(frappe.call({method:'fleet_bridge.priorities.dashboard_priority_history',type:'GET',args:{asset_id:a.id,limit:50}}))
        .then(response=>{
          const entries=response.message.entries||[];
          const dialog=new frappe.ui.Dialog({title:`${a.plate} · 優先度操作紀錄`,size:'large',fields:[{fieldtype:'HTML',fieldname:'history'}]});
          dialog.fields_dict.history.$wrapper.html(entries.length?`<div class="fc-table-wrap"><table class="fc-table"><thead><tr><th>時間</th><th>修改前</th><th>修改後</th><th>執行帳號</th><th>來源</th><th>紀錄編號</th></tr></thead><tbody>${entries.map(row=>`<tr><td>${esc(row.at)}</td><td>${esc(row.before_priority)}</td><td>${esc(row.reminder_priority)}</td><td>${esc(row.actor)}</td><td>${esc(row.source)}</td><td><code>${esc(row.operation_id)}</code></td></tr>`).join('')}</tbody></table></div>`:'<p class="fc-empty">尚無優先度修改紀錄。</p>');
          dialog.show();
        })
        .catch(()=>frappe.msgprint('無法讀取優先度操作紀錄。'))
        .finally(()=>{b.disabled=false;});return;
    }
    if(b.dataset.note){
      const a=data.assets.find(x=>x.id===b.dataset.note);if(!a||savingNote)return;
      const dialog=new frappe.ui.Dialog({title:`${a.plate} · 車輛通用備註`,size:'large',fields:[{fieldtype:'Small Text',fieldname:'note',label:'通用備註',default:a.profile.general_note||'',description:'整台車共用；個別保險、通行證或驗車內容請修改該筆提醒。'}],primary_action_label:'儲存'});
      dialog.set_primary_action('儲存',()=>{
        const note=dialog.get_value('note')||'';if(note.length>10000){frappe.msgprint('通用備註不可超過 10000 字。');return;}
        savingNote=true;dialog.get_primary_btn().prop('disabled',true);
        Promise.resolve(frappe.call({method:'fleet_bridge.vehicle_notes.dashboard_note_update',type:'POST',args:{asset_id:a.id,note,expected_version:a.priority_version,request_id:requestId()}}))
          .then(()=>{dialog.hide();frappe.show_alert({message:'車輛通用備註已儲存。',indicator:'green'});return load(true);})
          .catch(error=>frappe.msgprint(saveError(error)))
          .finally(()=>{savingNote=false;dialog.get_primary_btn().prop('disabled',false);});
      });dialog.show();return;
    }
    if(b.dataset.event){showReminder(b.dataset.event);return;}
    if(b.dataset.moreDay){showDay(b.dataset.moreDay);return;}
    if(b.dataset.monthStep){month=calendar.shiftMonth(month,Number(b.dataset.monthStep));renderResults();return;}
    if(b.hasAttribute('data-today')){month=data.today.slice(0,7);renderResults();return;}
    if(b.hasAttribute('data-return-calendar')){view='calendar';render();root.querySelector('.fc-calendar').scrollIntoView({block:'start'});return;}
    if(b.dataset.asset){selected=b.dataset.asset;renderResults();}
    if(b.dataset.view){view=b.dataset.view;if(view==='road'&&roadData)loadRoad(true);render();}
    if(b.dataset.open){selected=b.dataset.open;view='assets';render();}
    if(b.dataset.card){query='';kind='';category='';view=b.dataset.card==='assets'?'assets':'reminders';period=b.dataset.card;render();}
  });
  async function load(background = false) {
    if (refreshing) {refreshPending=true;return;}
    refreshing=true;
    try {
      const r = await frappe.call({method:"fleet_bridge.dashboard.overview", type:"GET"});
      data=r.message; month=month||data.today.slice(0,7);
      for (const id of [...bulkSelected]) if (!data.reminders.some(x=>x.record_id===id)) bulkSelected.delete(id);
      const searchFocused = document.activeElement === content.querySelector('input[type=search]');
      if(background && view === 'road' && roadMode === 'map') {
        // keep the map as it is; road data refreshes on its own and redraws only when events change
      } else if(background && searchFocused) {
        renderResults();
      } else {render();}
      if(activeReminder && activeReminder.dialog.$wrapper.is(':visible')) showReminder(activeReminder.id,activeReminder.dialog);
    }
    catch(e) {if(!background || !data)content.innerHTML='<div class="fc-empty" role="alert">無法讀取車隊資料。請確認登入權限或按「重新整理」重試；本頁未修改任何資料。</div>';}
    finally {refreshing=false;if(refreshPending){refreshPending=false;scheduleRefresh();}}
  }
  function isVisible(){return document.visibilityState==='visible' && frappe.get_route()[0]==='fleet-center';}
  function scheduleRefresh(){clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>{if(isVisible())load(true);},400);}
  document.addEventListener('keydown', e => {
    if (!data || !isVisible() || document.querySelector('.modal.show')) return;
    const t = e.target;
    if (t && (t.isContentEditable || ['INPUT','TEXTAREA','SELECT'].includes(t.tagName))) return;
    const tab = {Digit1:'assets', Digit2:'reminders', Digit3:'calendar', Digit4:'road'}[e.code];
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && tab) {
      e.preventDefault(); view = tab; if (view === 'road' && roadData) loadRoad(true); render(); return;
    }
    if (!e.altKey && !e.ctrlKey && !e.metaKey && e.key === '/') {
      const box = content.querySelector('input[type=search]');
      if (box) { e.preventDefault(); box.focus(); box.select(); }
    }
  });
  frappe.realtime.on('fleet_reminders_changed',scheduleRefresh);
  frappe.realtime.on('fleet_road_events_changed',()=>{if(isVisible()&&view==='road'){if(!roadError)loadRoad(true);}else if(!roadLoading)roadData=null;});
  document.addEventListener('visibilitychange',()=>{if(isVisible())scheduleRefresh();});
  window.addEventListener('focus',()=>{if(isVisible())scheduleRefresh();});
  // After a failed road request, stop automatic retries (each would pop an error); the 重試 button resets it.
  setInterval(()=>{if(isVisible()){load(true);if(view==='road'&&!roadError)loadRoad(true);}},60000);
  frappe.pages['fleet-center'].on_page_show=()=>{if(data)scheduleRefresh();};
  page.set_primary_action("重新整理",()=>load(false),"refresh");
  page.add_inner_button("管理工作區",()=>frappe.set_route("Workspaces","車隊管理"));
  load();
};
