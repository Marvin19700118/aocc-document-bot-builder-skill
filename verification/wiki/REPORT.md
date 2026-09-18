# QMD + LLM Wiki 2.0.0-alpha.1 驗證報告

日期：2026-09-18。此報告只描述 `document-bot-wiki` 新版，不沿用原向量版測試成績。

## 實測環境

- Windows 11 build 26200，Python 3.12.10，Node.js 24.18.0。
- QMD `@tobilu/qmd` 2.8.3，npm package-lock 固定依賴；pypdf 6.19.0、python-docx 1.2.0。
- 最低支援入口為 Python 3.11、Node 22；未分別實測這兩個最低版本或其他 OS。
- QMD 以隔離 npm prefix 執行，只使用 lexical SDK 與索引更新。原始測試 JSON 的本機絕對專案路徑以 `<PROJECT_ROOT>` 取代。

## 測試與檢查

- `python -m pytest tests_wiki -q -p no:cacheprovider`：**21 passed in 7.85s**。
- Skill 結構驗證：**Skill is valid!**；Python compileall 通過。
- 佇列與一致性測試使用明確標示的 FakeQmd，檢查 FIFO、A 期間新增、工作鎖、事件游標、失敗／重試、同名衝突、取代回退、兩個 schema 隔離，以及失敗暫存搜尋項目不遮蔽指定文件的有效結果。
- Wiki 測試包含租約競爭、引文驗證、路徑穿越拒絕、冪等提交、投影失敗恢復、同主題多來源、補建替換該來源敘述、刪除活動工作、過期批次、QMD 刪除失敗後重試、Wiki 關閉查詢。
- 四種真實檔案格式解析為來源 Markdown，保留 locator；兩宿主的安裝目錄在測試 profile 中驗證，未修改使用者現有安裝。
- 乾淨隔離 runtime 的完整 bootstrap、四格式匯入及中文 QMD 搜尋通過，見 [bootstrap-smoke.json](bootstrap-smoke.json)。Python 套件只有解析器及其依賴，沒有 torch、sentence-transformers 或 sqlite-vec。
- Windows 初次 `npm ci` 曾觸發不必要的 SQLite 編譯並因缺少 Visual Studio 失敗。已改為固定版本的 `npm ci --ignore-scripts`，使用套件附帶的原生預編譯檔，並在寫入安裝成功標記前實際啟動 QMD SDK 驗證。修正後 npm 安裝約 35 秒；不把這段數字當成完整首次安裝總耗時。
- ZIP 解壓、兩個隔離宿主安裝、CLI 入口與套件內容檢查通過，見 [package-smoke.json](package-smoke.json)。

## 真實 QMD

`tools/live_wiki_check.py` 對 `examples/` 的四份測試文件建立索引。使用原始 fixture manifest，不是聊天附件 UI。

| 指標 | 本次結果 |
|---|---|
| 10 題可回答問題的預期原文 Recall@8 | 10/10 |
| QMD `content_vectors` 筆數 | **0** |
| 已安裝依賴後的 setup | 約 1.425 秒 |
| 四份文件解析、MD 與 QMD 索引 | 約 7.292 秒 |
| 15 次查詢延遲中位數 | 約 0.836 秒 |
| Python 程序 RSS 快照 | 約 53.6 MB |

上述索引時間在 Wiki 關閉下量測；不含首次 npm/pip 安裝或 LLM 編寫。每次 QMD 呼叫都啟動 Node 子程序；記憶體只量 Python，不含 Node 峰值，也不是全程序峰值追蹤。此為功能實測伴隨的時間記錄，不是受控的新舊架構效能競賽，不能與原版暖機查詢數字直接比較。

同一次測試以確定性 payload fixture 驗證 Wiki 批次提交、生成主題頁、原文回鏈、搜尋 Wiki、刪除 Aurora 後 Wiki 與 QMD 都無殘留。這段驗證的是編寫協定，不是假裝 LLM 自動摘要。

五題無答案問題已由目前 Codex 助手讀取檢索證據並審查，應全部拒答，見 [QA-REVIEW.md](QA-REVIEW.md)。關鍵字命中本身不保證答案有依據。

## 真實背景程序

`tools/live_wiki_queue_check.py` 同時啟動兩個背景 worker，加入 A/B/C，A 未完成前加入 D，確認 D 位於第 4 位。處理順序為 A → B → C → D，每份 `indexed` 事件先於 `completed`，ack 後再次讀取無重複事件。本次約 **10.00 秒**。

佇列 fixture 刻意不含領域知識，Wiki 提交為空；此數字不包含真實 LLM 編寫時間。事件紀錄見 [live-queue.json](live-queue.json)，檢索及來源結果見 [live-check.json](live-check.json)。

## 尚未完整驗收

- Claude Code／Codex 真實宿主 UI 的附件取得、skill 發現、可見通知及完整回答流程。
- 完整 1,000 份／10,000 頁的解析、QMD 更新與 Wiki 編寫；目前每次 update 會掃描 collection，規模擴大後需另測。
- 跨語言、同義詞密集問題、複雜 PDF 表格及 Wiki 長期語意一致性。
- Python 3.11、Node 22、macOS、Linux，以及完整 Node/Python 記憶體峰值。
- 本分支不自動搬移原向量庫；新舊技能與資料 home 分開，原 `master` 和 v1 Release 保留。

省去向量模型可減少相關下載與計算，但不能由此推論所有問答都更快。Wiki 的語意整理仍消耗助手時間與額度。
