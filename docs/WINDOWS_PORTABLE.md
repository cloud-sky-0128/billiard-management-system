撞球館管理系統 Windows 免安裝版
================================

使用方式
--------

1. 請先完整解壓縮 ZIP，不要直接在壓縮檔內執行。
2. 進入 BilliardManager 資料夾。
3. 雙擊 BilliardManager.exe。
4. 等待瀏覽器自動開啟 http://127.0.0.1:8765。
5. 使用期間請保留黑色程式視窗；關閉該視窗就會停止系統。

Windows 第一次執行可能顯示 SmartScreen 警告。請先確認檔案是從本專案的
GitHub Releases 下載，再選擇「其他資訊」及「仍要執行」。第一版尚未購買
程式碼簽章憑證，因此 Windows 可能無法辨識發行者。

資料保存位置
------------

資料不會放在程式資料夾，而是保存在：

%LOCALAPPDATA%\BilliardManager\billiard.db

備份保存在：

%LOCALAPPDATA%\BilliardManager\backups

因此更新程式時，可以刪除舊的程式資料夾並解壓縮新版，原本資料仍會保留。
若要完整移除所有資料，請在程式關閉後自行刪除上述 BilliardManager 資料夾。

常見問題
--------

- 瀏覽器沒有自動開啟：手動輸入 http://127.0.0.1:8765。
- 網址無法連線：確認 BilliardManager.exe 的黑色視窗仍在執行。
- 顯示連接埠被使用：先關閉另一個 BilliardManager，再重新執行。
- 防毒軟體掃描較久：免安裝程式第一次執行時可能需要等待。

安全提醒
--------

這是單機測試版，只監聽 127.0.0.1，其他電腦不能直接連入。請勿自行設定
Port Forwarding 或將它公開到網際網路。
