撞球館管理系統 Windows 視窗預覽版
================================

這是獨立視窗預覽版 `window-preview-0.1.0`，可從 GitHub Releases 下載
`BilliardManagerWindow-preview.zip`，或由原始碼自行建置。
它不取代已發布的瀏覽器版，也尚未連接實體燈控。
請先備份正式資料，再以非營業資料測試；確認穩定後才在營業時使用。

使用方式
--------

1. 完整解壓縮 ZIP，不要直接在壓縮檔內執行。
2. 進入 BilliardManagerWindow 資料夾，雙擊 BilliardManagerWindow.exe。
3. 介面會在獨立視窗顯示；關閉視窗會停止本機服務和備份排程。
4. 如視窗顯示缺少 WebView2，請安裝 Microsoft WebView2 Runtime 後重試。

視窗版使用 Windows WebView2，不會自動改用較舊的 IE 核心。WebView2 Runtime
可事先使用 Microsoft 官方的離線安裝程式安裝；安裝後操作本系統不需要網際網路。
請勿將 Runtime 安裝檔從非官方來源下載。

資料與回退
----------

視窗版和瀏覽器版共用相同的資料路徑選擇邏輯。預設位置是：

%LOCALAPPDATA%\BilliardManager\billiard.db
%LOCALAPPDATA%\BilliardManager\backups
%LOCALAPPDATA%\BilliardManager\logs\app.log

若既有瀏覽器版使用 EXE 旁的 data 資料夾，請保留該資料夾，
不要只複製新的 EXE。新解壓縮目錄不會自動找到舊目錄的 data；
請先用 BILLIARD_DATABASE 指定舊資料庫的完整路徑，避免建立空白資料庫。
同一程式可見的兩個候選位置都有資料庫時，程式也會停止並要求明確指定。

兩個版本不能同時執行。切換版本前，請先在「整體設定 > 資料保護」
手動備份，並完整關閉原版本。若視窗版啟動失敗，可以改用原本的
BilliardManager.exe 和其瀏覽器介面，不需要搬移資料。

備份包含當下完整 SQLite 資料庫。啟動時建立當天第一份每日備份及
日內快照；持續執行期間每 15 分鐘新增快照。備份若只在同一顆硬碟，
無法防止整顆硬碟故障；建議另設 BILLIARD_BACKUP_DIR 指向另一顆磁碟。

視窗版仍在本機 127.0.0.1:8765 啟動服務，僅供這台電腦使用。
這不等於連接網際網路，也不代表其他電腦可直接進入。不要公開連接埠。

若啟動失敗，會顯示錯誤訊息；請查看上述 logs\app.log。
