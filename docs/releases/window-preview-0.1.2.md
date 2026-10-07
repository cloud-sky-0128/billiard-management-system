# Windows 獨立視窗預覽版 window-preview-0.1.2

下載 `BilliardManagerWindow-preview.zip`，完整解壓後執行 `BilliardManagerWindow.exe`。這是獨立視窗預覽版，需要 Microsoft WebView2 Runtime，但操作系統本身不需要連線到網際網路。

## 本次修正

- 啟用獨立視窗中的檔案下載，可儲存個人／全部班表 PNG 與收支 CSV。
- 縮減球檯數時，已過期、已完成或已取消的預約不再阻擋設定。
- 未填結束時間的預約仍以開始後一小時計算。
- 縮桌後保留歷史預約與原桌號，不會刪除或錯誤換桌。
- 新增下載版驗收報告及正式發布檢查清單。

## 注意事項

- 此版本仍標示為預覽版。第一次用於營業資料前，請先備份並用非營業資料試跑。
- 目前尚未接上實體燈控，免費練習只記錄球檯使用狀態。
- 資料預設保存在 `%LOCALAPPDATA%\BilliardManager`；瀏覽器版與視窗版共用資料，兩者不可同時執行。
- ZIP 內附完整操作、備份及回退說明；SHA-256 請以本 Release 的 `SHA256SUMS.txt` 核對。
