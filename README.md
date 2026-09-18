# AOCC Document Bot Builder Skill — QMD + LLM Wiki

這是 `codex/qmd-llm-wiki` 的 **2.0.0-alpha.1 實驗版**：附件轉 Markdown，以 QMD 全文搜尋，再由目前的 Claude／Codex 整理成可累積、可追溯來源的 Wiki。完全不計算文件向量，不下載 embedding、query expansion 或 reranker 模型。

[新版規格](spec.md) · [新版驗證報告](verification/wiki/REPORT.md) · [原向量版 master](https://github.com/Marvin19700118/aocc-document-bot-builder-skill/tree/master)

## 安裝

需要 Python 3.11+、Node.js 22+（含 npm）。Windows 是本次實測平台。

```powershell
python install-wiki.py --host both
```

也可指定 `--host claude` 或 `--host codex`。新 skill 名稱為 `document-bot-wiki`，可與原本 `document-bot` 並存，不修改原版安裝、資料庫或助手設定。在要保存文件的既有工作目錄開啟助手：

```text
Claude Code: /document-bot-wiki setup
Codex:       $document-bot-wiki setup
```

首次 setup 建立 `~/.document-bot-wiki/runtime`、安裝兩個 Python 解析套件及鎖定版本 QMD 2.8.3。QMD 仍包含它上游的原生依賴，安裝並非零成本；本專案只呼叫它的 BM25/FTS SDK，不呼叫向量或本機 LLM 模型功能。Wiki 整理及回答使用目前助手的服務，無須另一組模型 API key。

固定版本使用 `npm ci --ignore-scripts` 與套件附帶的預編譯檔，安裝後會驗證 QMD 可啟動。此方式已在 Windows x64 實測；其他平台若缺少可用的預編譯檔會明確報錯，不自動安裝系統編譯工具。

## 使用

上傳 PDF／DOCX／Markdown／UTF-8 TXT 原始附件後，明確執行 `add`。宿主無法提供可讀取原檔時會回報不支援；上傳本身不自動入庫。

| 子指令 | 行為 |
|---|---|
| `setup` | 初始化，重複執行保留資料及設定。 |
| `add` | 保存附件、回覆工作 ID 與排隊位置。 |
| `dir`／`dir.` | 查看原檔副本、Markdown 索引與 Wiki 狀態。 |
| `status` | 查看目前階段、FIFO 順序與失敗工作。 |
| `retry 工作ID` | 失敗工作排到尾端。 |
| `delete 文件ID或唯一檔名` | 刪除管理副本、MD、索引及該文件支持的 Wiki 敘述。 |
| `ask 問題 [--doc 文件ID]` | 搜尋原文及 Wiki；答案附原始來源位置。 |
| `wiki on`／`wiki off` | 控制新工作及查詢是否使用 Wiki；已排隊工作不變。 |
| `wiki build 文件ID或all` | 在同一佇列補建／重編 Wiki。 |
| `wiki list`／`wiki lint` | 查看主題；檢查引用及頁面連結。 |

舊 `graph` 子指令作為 `wiki` 別名保留，此分支不建立關係圖資料庫。

流程為 **原檔 → Markdown → QMD 全文索引 → 逐檔通知 → 助手編寫 Wiki → 完成通知 → 下一份**。Wiki 預設開啟；助手離線時工作停在等待助手，新文件保持排隊。背景 worker 不會自動呼叫 LLM。

```text
已登記工作目錄/bot documents/<文件ID>/原始檔名
~/.document-bot-wiki/
├── runtime/                       Python 解析與佇列程式
├── qmd-runtime/                   鎖定版本的 QMD/npm 套件
├── library.sqlite3                工作、來源、Wiki 敘述及事件
├── qmd.sqlite                     QMD 全文索引（文件向量數為 0）
├── knowledge/
│   ├── sources/<文件ID>/000000.md  原文及頁碼／段落／行號
│   ├── wiki/<主題>.md              有來源的整理內容及相關主題連結
│   ├── index.md                   Wiki 主題索引
│   └── log.md                     最近作業事件快照
└── search/                        可重建的搜尋文字投影
```

中文搜尋使用文字 bigram（相鄰兩字）並合併 QMD 詞彙搜尋排名，沒有隱藏 embedding。原始文字與引用位置不會被斷詞內容取代。詞彙檢索對同義詞、跨語言及語意改寫可能較弱；助手可改寫關鍵字再次搜尋，沒有來源仍須拒答。

Wiki 頁面跨文件累積敘述，逐條保留來源；同一來源支持的主題會互相連結。重編一批時替換該批來源的舊敘述，刪除文件後保留其他文件仍支持的內容。生成頁面由程式管理，請不要手動搬移／修改，以免被恢復流程覆蓋。

## 驗證與效能

[新版報告](verification/wiki/REPORT.md) 分開記錄測試替身、真實 QMD 與尚未完成的 UI 驗收。無向量可省去模型下載與 embedding 計算，但不保證每次問答都更快：目前每次 QMD SDK 呼叫會啟動 Node 子程序，Wiki 編寫仍有助手耗時。

```powershell
python -m pip install -r document-bot-wiki/requirements.txt pytest psutil
python -m pytest tests_wiki -q -p no:cacheprovider
# 真實 QMD 測試：先 setup，或將 DOCUMENT_BOT_QMD_PREFIX 指向已安裝的 npm prefix
python tools/live_wiki_check.py
python tools/build_wiki_release.py
```

原向量版程式與測試保留在 `document-bot/`、`tests/`；`install.py` 仍是原版安裝器。新版使用 `document-bot-wiki/`、`tests_wiki/` 與 `install-wiki.py`。兩版不可共用資料 home；新版不自動遷移既有資料，應重新明確加入附件。

## 技術來源與限制

- [QMD 官方專案](https://github.com/tobi/qmd)：使用公開 `createStore`、`update`、`searchLex` SDK，鎖定 2.8.3。
- [Karpathy 的 LLM Wiki 原始構想](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：採用持久化 Markdown 知識頁、來源追蹤、主題連結及維護流程；不是依賴一個名為 LLM Wiki 的雲端服務。

不含 OCR、雲端同步、獨立網頁、多知識庫、多使用者權限或完整語意消歧義。通知依賴活躍助手輪詢；訊息送出與事件確認間若中斷，可能補報一次。來源引文可程式驗證，Wiki 概括的語意正確性仍需助手判斷。第三方軟體依其各自授權使用。
