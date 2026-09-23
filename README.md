# 撞球館管理系統

[![Tests](https://github.com/cloud-sky-0128/billiard-management-system/actions/workflows/tests.yml/badge.svg)](https://github.com/cloud-sky-0128/billiard-management-system/actions/workflows/tests.yml)

單機使用的撞球館管理系統，整合球檯計費、餐飲點單、預約、員工班表、每日收支與備份。Windows 免安裝版不需要另外安裝 Python。

> 目前尚未連接實體燈控設備；「免費練習燈」只標示球檯使用中，不會控制燈具。程式只供本機存取，請勿直接公開到網際網路。

## Windows 版本怎麼選

| 版本 | 取得方式 | 啟動程式 | 操作方式與狀態 |
| --- | --- | --- | --- |
| **瀏覽器版 `v0.1.3-desktop`** | [已發布的 Release](https://github.com/cloud-sky-0128/billiard-management-system/releases/tag/v0.1.3-desktop) 中下載 `BilliardManager-windows-x64.zip` | `BilliardManager.exe` | 在本機瀏覽器操作；目前建議使用的已發布版本，不需 WebView2。 |
| **獨立視窗預覽版（`main`，尚無版本標籤）** | 本倉庫原始碼自行建置，產物為 `dist\BilliardManagerWindow-preview.zip`；**目前不在 Releases 提供下載** | `BilliardManagerWindow.exe` | 在獨立視窗操作；需安裝 WebView2 Runtime，先以備份資料試用。 |
| **原始碼開發版（`main`）** | Clone 本倉庫並安裝 `requirements.txt` | `python app.py` | 在本機瀏覽器操作 `http://127.0.0.1:5000`；需 Python，不是 Windows 免安裝版。 |

前兩種 Windows 版都只在這台電腦的 `127.0.0.1:8765` 啟動本機服務，不必連線到網際網路；兩者不能同時執行。一般 `git push` 只更新原始碼，不會更新 Release ZIP。

### 啟動已發布的瀏覽器版

1. 前往 [`v0.1.3-desktop` 下載頁](https://github.com/cloud-sky-0128/billiard-management-system/releases/tag/v0.1.3-desktop)，下載 `BilliardManager-windows-x64.zip`。
2. 對 ZIP 選擇「解壓縮全部」，不要直接在壓縮檔內執行。
3. 進入解壓後的 `BilliardManager` 資料夾，雙擊 `BilliardManager.exe`。
4. 等待瀏覽器自動開啟 `http://127.0.0.1:8765`。使用期間請保留程式視窗；關閉視窗就會停止系統。

下載後可在 PowerShell 執行以下指令，將結果與 GitHub 版本頁的 SHA-256 比對：

```powershell
Get-FileHash -Algorithm SHA256 .\BilliardManager-windows-x64.zip
```

若 Windows 顯示 SmartScreen 警告，先確認下載來源與 SHA-256；不要執行來源不明或驗證結果不符的檔案。更多本機版說明見 [ZIP 內的 README 來源](docs/WINDOWS_PORTABLE.md)。

### 建置獨立視窗預覽版

在 Windows 安裝 Python 後，於專案目錄執行：

```powershell
python -m pip install -r requirements-window.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows_window.ps1
```

完成後解壓 `dist\BilliardManagerWindow-preview.zip`，執行其中的 `BilliardManagerWindow.exe`。此預覽版尚未發布到 GitHub Releases，**不可把瀏覽器版 ZIP 誤認為視窗版**。使用前請先看 [視窗預覽版說明](docs/WINDOWS_WINDOW_PREVIEW.md)，尤其是 WebView2 Runtime、共用資料庫及 EXE 旁 `data` 的注意事項。

## 資料放在哪裡

| 類型 | 預設位置 |
| --- | --- |
| 正式資料庫 | `%LOCALAPPDATA%\BilliardManager\billiard.db` |
| 備份 | `%LOCALAPPDATA%\BilliardManager\backups\` |
| 錯誤日誌 | `%LOCALAPPDATA%\BilliardManager\logs\app.log` |

每台電腦各有自己的資料庫，解壓新版 ZIP 不會自動把舊電腦的資料搬過去。只有在首次建立資料庫且預設位置不可寫時，程式才會改用解壓資料夾內的 `BilliardManager\data`。之後會沿用已存在的資料庫；若預設位置與 `data` 都有資料庫，程式會停止啟動，避免無聲切換。這時請先核對兩份資料，不要任意刪除或覆蓋。

## 備份、升級與還原

- 程式啟動時會建立當日第一份完整備份及日內快照；持續執行時約每 15 分鐘建立新快照。程式關閉期間不會備份。
- 自動保留最近 16 份日內快照、30 天的每日備份，以及最近 12 個月每月首次成功建立的備份。每日備份只保留當天第一份狀態，不會被日內快照覆寫。
- 備份是當下完整的 SQLite 資料庫，包含設定、消費單、收款、預約、班表與操作紀錄；也可能包含客戶資料及管理員密碼雜湊，請妥善保管。
- 只備份在同一顆硬碟無法防止硬碟故障。可設定 `BILLIARD_BACKUP_DIR` 到另一顆磁碟；方式見 [Windows 免安裝版說明](docs/WINDOWS_PORTABLE.md)。

**升級前：** 到「整體設定 > 資料保護」手動建立備份並執行還原演練，確認備份可讀，再關閉舊程式。更新程式時不要覆蓋或刪除資料庫；如果舊版使用解壓資料夾內的 `data`，務必一起保留該資料夾。

**需要回退時：** 先關閉程式，另存目前資料庫，再把升級前的備份複製回原資料庫路徑。這會失去備份之後的新紀錄；舊版程式也不保證能讀取新版資料庫。不要在程式執行中覆蓋資料庫。

## 主要功能

| 模組 | 功能 |
| --- | --- |
| 球檯管理 | 自訂桌數與名稱、計時與包台、換桌、免費練習、加時及分次結帳 |
| 候位消費單 | 未分桌先點餐、分配空桌，或只結餐飲費 |
| 計費與優惠 | 每桌獨立費率、優惠時段、百分比折扣與包台優惠價 |
| 餐飲點單 | 分類選餐、飲料甜度與冰塊、數量及出餐狀態 |
| 行事曆／預約 | 球檯預約、跨午夜時段、維修／活動／比賽及無日期待辦 |
| 員工排班 | 自訂員工與班別、多日排班、跨午夜班次及個人班表 PNG 匯出 |
| 收支與統計 | 營業日統計、系統收款、人工實收、支出、月報及 CSV 匯出 |
| 資料保護 | 自動備份、快照健康檢查、安全清除測試資料及精簡操作紀錄 |

未填結束時間的球檯預約預設保留一小時。營業日以每日 `06:00` 為分界；例如 `2026-09-23 05:59` 的收款屬於 `2026-09-22` 營業日。實際收款、人工實收與支出均以整元處理；資料庫內部以整數分儲存金額，避免浮點數誤差。

## 系統畫面

以下截圖使用虛構展示資料，不包含正式資料庫或真實個資。

### 球檯總覽

![球檯總覽](docs/screenshots/dashboard.png)

### 球檯細項

![球檯細項](docs/screenshots/table-detail.png)

### 行事曆與預約

![行事曆與預約](docs/screenshots/calendar.png)

### 員工班表

![員工班表](docs/screenshots/shifts.png)

### 營業統計

![營業統計](docs/screenshots/stats.png)

## 常見問題

- **瀏覽器沒有自動開啟：** 手動輸入 `http://127.0.0.1:8765`。原始碼開發版才使用 `http://127.0.0.1:5000`。
- **無法連線：** 確認 `BilliardManager.exe` 視窗仍在執行，並確認網址的連接埠是 `8765`。
- **啟動或備份失敗：** 查看資料庫旁的 `logs\app.log`，確認磁碟尚有空間、外接備份磁碟仍可使用。
- **資料看似消失：** 先停止操作並確認目前使用的資料庫路徑；不要建立新資料庫或覆蓋舊檔。

## 從原始碼執行（開發者）

需求：Python 3.10 以上。請先安裝 Python 與 Git，再執行：

```bash
git clone https://github.com/cloud-sky-0128/billiard-management-system.git
cd billiard-management-system
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

瀏覽器開啟 `http://127.0.0.1:5000`。終端機需要保持執行；第一次啟動會建立專案目錄內的 `billiard.db`。此方式使用 Flask 開發伺服器，不應直接公開到網際網路。

### 測試與打包

```bash
python -m unittest discover -s tests -v
```

目前有**超過 100 項自動化測試**；測試使用暫存 SQLite，不會修改正式資料庫。[瀏覽器版建置腳本](scripts/build_windows.ps1) 與 [視窗預覽版建置腳本](scripts/build_windows_window.ps1) 分開執行。現有 `v*` 標籤的 GitHub Actions 只會打包並發布**瀏覽器版**；視窗預覽版目前不會自動發布。

## 已知限制

- 目前沒有個別員工帳號與細分權限；共用管理員登入的操作紀錄無法辨識實際是哪位員工，也不是防竄改稽核系統。
- 實體燈控設備尚未接入；免費練習只記錄球檯狀態。
- Windows 免安裝版只監聽本機 `127.0.0.1`，其他電腦不能直接連入。不要設定路由器 Port Forwarding。
- SQLite 適合目前單店低併發情境；若將來擴充多店或多機使用，需重新評估部署方式與資料庫。
- 長期使用時應固定設定 `SECRET_KEY`，並定期檢查備份容量及實際還原流程；否則重啟程式會使既有登入失效。

## 技術文件

開發架構、核心資料表簡圖、資料流程、技術選擇與面試技術亮點已移至 [技術設計與資料模型](docs/ARCHITECTURE.md)。瀏覽器版 ZIP 的 `README.txt` 來源是 [瀏覽器版說明](docs/WINDOWS_PORTABLE.md)；視窗預覽版 ZIP 的 `README.txt` 來源是 [視窗預覽版說明](docs/WINDOWS_WINDOW_PREVIEW.md)。
