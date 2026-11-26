# Fleet Center

以 [Frappe](https://frappe.io/) / ERPNext 為基礎的車隊管理 app（`fleet_bridge`），給臺灣的貨運、曳引車與拖車車隊使用：

- **車籍總覽**：貨車、曳引車、拖車與舊系統資料主檔集中查閱，含車輛通用備註與提醒優先度。
- **日期提醒與行事曆**：保險、通行證、驗車等到期提醒；可手動新增提醒、設定通知時間，到時間以 ERP 站內通知提醒（同一筆不重複通知）。
- **道路情報**：每日定時讀取交通部 TDX 道路事件（國道、省道，可選縣市），寫入資料庫並在車隊首頁顯示，重大管制以站內通知提醒。
- **政府 API 專用出口代理**：ERP 容器保持不能上網，只經由固定白名單的小型代理連到核准的政府開放資料 API。
- **AI 存取的權限範圍**：AI 帳號只能讀寫明確授權的車輛與欄位，每次修改都留下不可刪改的稽核紀錄。

> 原本放在這個倉庫的「整體物臨時通行證知識庫」已完整移到 [`permit-knowledge`](../../tree/permit-knowledge) 分支（含所有歷史）。

> 本專案是實際營運系統去識別後的程式碼，不含任何車牌、公司資料、伺服器位址、帳號或金鑰，也不含部署腳本。

## 目錄

```
src/apps/fleet_bridge/
  fleet_bridge/            Frappe app 本體
    dashboard.py           車隊首頁資料（車籍、提醒彙整）
    reminders.py           舊系統提醒的通知時間與稽核
    manual_reminders.py    手動新增的提醒
    notification_dispatch.py  每分鐘檢查到期提醒並發站內通知（去重）
    priorities.py, vehicle_notes.py, profile.py, api.py, events.py, setup.py
    road_events.py         TDX 道路情報：排程、同步、狀態比對、通知、儀表板 API
    road_events_core.py    道路事件純邏輯（不依賴 Frappe，可離線測試）
    gov_gateway.py         ERP 端呼叫出口代理的用戶端
    fleet_bridge/page/fleet_center/  車隊首頁（車籍總覽、日期提醒、行事曆、道路情報）
  egress/gov_egress.py     政府 API 專用出口代理（只用 Python 標準函式庫）
tests/                     離線單元測試
```

## 道路情報（TDX）

- 資料來源：交通部 [TDX 運輸資料流通服務](https://tdx.transportdata.tw/) 「道路事件」API（`/api/basic/v1/Traffic/RoadEvent/...`）。需要自行註冊 TDX 會員並建立 API 金鑰；免費「基礎」方案即可（每把金鑰每分鐘 5 次、每月 3 點）。
- 設定：在 ERP 的 `Fleet Road Info Settings` 填入 Client ID / Client Secret（Password 欄位，加密保存）、每日更新時間（預設台北 05:30、11:30、17:30、22:30）與資料集。預設不啟用定時更新，先按「手動更新」確認。
- 實際 API 行為（2026-11 以真實呼叫確認，與公開的 OAS 規格不同處）：即時事件清單欄位為 `LiveEvents`、預告事件為 `Events`（規格寫 `Items`）；`$top` 預設 30，程式以 `$top=5000` 並核對 `Count`。
- 狀態判斷：
  - 官方沒有「已解除」欄位。事件從**可證明完整**的回應中消失時標為「狀態不明」，官方 `ExpireTime` 已過才標「已結束」。
  - 回應不完整（缺 `Count`、筆數不符、跨頁更新時間不一致、有無法解析的項目）或呼叫失敗時，不改任何既有事件狀態。
  - 官方沒有限高、限寬、限重欄位；未列出限制不代表特殊車輛可以通行。
- 曳引車車道判定（國道）：曳引車不行駛內側車道，因此依事件的 `Impact.BlockedLanes` 與該路段主線車道數判斷影響：

  | 曳引車影響 | 條件 | 通知 |
  |---|---|---|
  | 等同封閉 | 封到最外側車道或全部車道（`999`），或官方全封閉、特定車種管制需求、完全阻斷、災害 | 每筆單獨通知 |
  | 通過時注意 | 只封外側路肩（`RS`），外側車道未封 | 每輪合併一則 |
  | 無影響 | 只封內側車道或內側路肩（`LS`） | 不通知 |
  | 無阻斷／狀態不明 | `-1`；或 `255`、匝道出入口、查無車道數 | 不通知，畫面標示 |

  - 車道編號由內側（左）往外側（右）為 `0,1,2,…`，`LS`／`RS` 為內／外側路肩（依實際資料核對；官方條文待核）。
  - 車道數取自 TDX「路況資訊 v2」國道 VD 靜態資料（`/api/basic/v2/Road/Traffic/VD/Freeway`），只用主線偵測器（VDID 第 5 段 `M`、`N`），事件里程 ±2 km 內取最少車道數；每 30 天更新一次。
  - 省道事件未提供阻斷車道，標為「未提供車道資訊」，只依官方重大管制判定。
- 通知：第一次完整同步、以及判定規則變更後的第一輪都當作基準，不發通知；之後只通知新出現或狀態改變的事件。
- 額度：每次呼叫間隔至少 15 秒，token 重複使用，429/5xx 有限重試並遵守 `Retry-After`，每月估計點數達上限即暫停。

## 政府 API 專用出口代理（gov-egress）

ERP 的資料庫、Redis、背景工作等容器放在 `internal: true` 的 Docker 網路，不能上網。只有背景工作容器另外接到一個也是 internal 的連線網路，經由 `gov-egress` 代理對外：

- 只接受 `/<來源>/<路徑>`；每個來源的上游主機、允許的路徑（正規表示式）、HTTP 方法與查詢參數都寫死在程式裡，預設全部拒絕。
- 不提供 CONNECT 或任意代理；不跟隨重新導向；上游位址必須是公網 IP，連線使用檢查過的位址並驗證 TLS 憑證（防 SSRF 與 DNS rebinding）。
- DNS、連線與整個回應有總時限，回應大小與同時連線數有上限。
- 日誌只記來源、路徑、狀態、大小與耗時，不記查詢參數、標頭、內容、金鑰或 token。
- 建議容器設定：非 root、唯讀根目錄、`cap_drop: [ALL]`、`no-new-privileges`、不開對外埠、不掛載任何 volume（映像若宣告 `VOLUME`，以唯讀 tmpfs 覆蓋），只接 internal 連線網路與一個對外 bridge 網路。

目前白名單只有 TDX：道路事件（v1 `Traffic/RoadEvent` 國道、省道、縣市）、國道 VD 車道數（v2 `Road/Traffic/VD/Freeway`）與認證端點。新增其他政府資料來源時，在 `egress/gov_egress.py` 的 `ROUTES` 加一筆並重新部署。

## 安裝

需求：Frappe v15、ERPNext，以及提供 `Truck`、`Trailers` 等車籍 DocType 的 `vsd_fleet_ms` app（見 `hooks.py` 的 `required_apps`）。

```bash
cd frappe-bench
cp -r /path/to/this/repo/src/apps/fleet_bridge apps/
bench --site <site> install-app fleet_bridge
bench --site <site> execute fleet_bridge.dashboard.install_workspace   # 車隊管理工作區與提醒相關 DocType
bench --site <site> execute fleet_bridge.road_events.install           # 道路情報 DocType 與排程
```

`gov-egress` 以同一個映像執行 `python3 -I apps/fleet_bridge/egress/gov_egress.py`，ERP 端預設連 `http://gov-egress:8080`（見 `gov_gateway.py`）。

## 測試

```bash
python -I tests/test_road_events_core.py
python -I tests/test_gov_egress.py
python -I tests/test_vehicle_access.py
```

測試不連網、不需要 Frappe（以替身物件執行）。真實 TDX 呼叫與 Docker 網路隔離需在部署環境另外驗證。

## 授權

[MIT](LICENSE)。TDX 資料依 TDX 與政府資料開放授權條款使用；使用者需自行取得並遵守 TDX 會員方案限制。
