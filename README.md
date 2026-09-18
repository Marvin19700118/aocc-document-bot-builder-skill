# AOCC Document Bot Builder Skill

可安裝在 Claude Code 與 Codex 的本機文件知識庫 skill。上傳聊天附件後執行 `add`，文件保存於已登記工作目錄的 `bot documents`，依 FIFO 順序建立索引、向量與預設開啟的知識圖譜。每份文件向量完成後立即由目前助手回報，再接續圖譜與下一份文件。

## 安裝

需要 Python 3.11 或更新版本。Windows 是第一版的主要測試平台。解壓縮安裝包，或在本專案根目錄執行：

```powershell
python install.py --host both
```

也可選 `--host codex` 或 `--host claude`。安裝器只寫入自己的 skill 目錄，不修改助手設定、不安裝到其他 skills。已有本套件的安裝可用 `--update` 更新；不覆蓋沒有本套件識別標記的同名 skill。

安裝完成後，在**原本要存放文件的既有工作目錄**開啟助手，執行：

```text
Claude Code: /document-bot setup
Codex:       $document-bot setup
```

首次 setup 建立私人 Python 環境、下載 multilingual-e5-small 並固定模型版本。需連網下載依賴，無須另設模型 API key。運行期間索引與向量在本機計算；回答與圖譜抽取仍使用目前助手的服務及額度。兩個助手必須執行在同一台電腦、同一使用者及相同的資料 home 才會共用知識庫。

CLI/IDE 可安裝 skill，但**聊天附件支援取決於宿主能否提供可讀取的原檔**。本套件不能改寫宿主的上傳按鈕。只收到文字摘要或無法讀取原檔時會回報不支援，不以重建文字假裝匯入成功。

## 日常操作

以下以 Claude Code 為例；Codex 將 `/document-bot` 換成 `$document-bot`。

1. 在對話畫面上傳文字 PDF、DOCX、Markdown 或 UTF-8 TXT。
2. 送出 `/document-bot add`。可一次選多個附件，也可在先前文件處理中明確加入新附件。
3. 助手立即回覆文件 ID、工作 ID 與排隊位置；每份索引與向量完成後各自通知。圖譜完成後再處理下一份。
4. 使用 `/document-bot ask 特休規定是什麼？`，答案附檔名及頁碼／段落／行號。追問會重新檢索。

| 子指令 | 功能 |
|---|---|
| `dir` / `dir.` | 文件清單、保存位置、索引與圖譜狀態 |
| `status` | 目前處理階段、排隊順序、待修復工作 |
| `ask 問題 --doc 文件ID` | 限定單份文件查詢 |
| `delete 文件ID` | 刪除管理副本及其所有衍生資料 |
| `retry 工作ID` | 失敗工作重新排到最後 |
| `graph off` / `graph on` | 修改新工作與查詢的圖譜設定 |
| `graph build 文件ID` / `graph build all` | 既有文件補建圖譜 |

單純上傳不入庫。同內容重複加入會略過；同名不同內容先選保留兩份或取代。取代先建立新索引，失敗時舊索引仍可用。新圖譜開關不改變已排隊工作的設定。

## 資料位置

```text
已登記工作目錄/
└── bot documents/
    └── 文件ID/
        └── 原始檔名

~/.document-bot/
├── runtime/           私人 Python 執行環境
├── models/            固定版本的本機向量模型
├── library.sqlite3    文件、向量、圖譜、佇列與通知
└── worker.log         背景工作紀錄
```

setup 將工作目錄保存為絕對路徑。從另一個工作目錄使用 skill 仍指向同一知識庫。不要手動改名、移動或編輯管理副本；需要新版時重新上傳附件。`delete` 不動使用者原始來源檔案，只移除 `bot documents` 內的副本與衍生資料。

測試或可攜式安裝可設定 `DOCUMENT_BOT_HOME`，或給內部 CLI `--home`；一般使用者不需要設定。資料庫 schema 不相容會停止，不自動破壞或重建既有資料。

## 佇列與通知的保證

- SQLite 持久化工作順序；OS 檔案鎖確保最多一個索引處理者。新附件在 worker 運作時仍可入列。
- 向量與段落以交易一次提交後才通知。查詢看不到半完成索引。
- 圖譜採分批租約與精確來源驗證；不同助手不能提交同一批次。圖譜失敗保留向量，可重試。
- 刪除先標記，處理者在批次邊界停止，再清除副本與資料。若檔案被外部程式鎖住，保留待清理狀態，不回報已刪除。
- 每個對話有獨立通知游標，讀取後由助手實際通知，再確認已送出。正常輪詢不重複；若恰好在「已發訊息、尚未確認」時中斷，可能重播一次，助手恢復時會比對對話。聊天訊息與本機資料庫無共同交易，不能承諾絕對 exactly-once。
- 通知依賴正在運行的助手輪詢，通常以約 2 秒間隔讀取事件；不是作業系統即時推播。對話關閉後保存通知，恢復時補報。
- 索引背景程序在同批文件之間保留模型，圖譜需要助手參與。等待助手超過 120 秒會釋放模型並退出程序，工作仍持久保存；恢復時可重新啟動。離線時後面的文件維持排隊，不會假裝自動抽取圖譜。

## 技術與驗證

使用 SQLite + sqlite-vec、Sentence Transformers multilingual-e5-small、pypdf 與 python-docx。段落上限 400 個模型 token，重疊 60 個；本機 384 維向量；圖譜最多擴展兩層。圖譜是輕量的來源關係模型，不提供完整同名實體消歧義。

參考 [hrithikkoduri/GraphRAG](https://github.com/hrithikkoduri/GraphRAG) 的關係輔助檢索方向；本實作重新編寫，不直接複製其藝人／經紀人資料模型或部署堆疊。第三方套件與模型遵循各自授權。

開發測試：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r document-bot/requirements.txt pytest psutil
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe tools/live_check.py
python tools/build_release.py
```

單元／整合測試使用明確標示的測試向量，以隔離資料一致性和模型品質；`live_check.py` 使用真實模型與產生的四格式文件，結果寫入 `verification/live-retrieval.json`。測試來源轉接不等於真實 UI 附件測試。驗證範圍與尚未完成項目見 `verification/REPORT.md`。

第一版不含 OCR、XLSX/CSV/PPTX 匯入、雲端同步、獨立網頁、多知識庫或多人權限。PDF 無文字頁會明確提示；DOCX 使用段落／表格位置，不編造頁碼。沒有文件依據時拒答，不用網路補答案。
