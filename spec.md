# Document Bot Wiki — QMD + LLM Wiki 規格

版本：2.0.0-alpha.1；日期：2026-09-18；分支：`codex/qmd-llm-wiki`。

此分支實現「原檔轉 MD → QMD 詞彙搜尋 → LLM Wiki」的新核心。原向量版保留於 `master`；[第一版規格快照](spec-vector-v1.md) 只記錄歷史，不是本分支的向量／圖譜需求。實測狀態以 [新版驗證報告](verification/wiki/REPORT.md) 為準。

## 1. 架構決策

- 完全取消文件向量計算。新 Python 核心不依賴 torch、sentence-transformers 或 sqlite-vec。
- QMD 固定 2.8.3，透過其 SDK 建立 Markdown 的 BM25/FTS 索引。允許其上游套件原生依賴，但不執行 embed、semantic search、hybrid query、LLM expansion 或 reranking。
- LLM Wiki 採 Karpathy 的持久化知識頁模式，由目前助手編寫有來源的敘述與主題頁；不是自動啟動另一個模型服務。
- SQLite 保留文件、FIFO 工作、通知、批次租約與 Wiki 來源關係。取消向量不等於取消工作資料庫。
- 新 skill 為 `document-bot-wiki`，預設 home 為 `~/.document-bot-wiki`。原版 `document-bot` 與其資料不變，不自動遷移。

## 2. 文件與資料位置

`setup` 註冊既有工作目錄的絕對路徑。原檔副本仍位於 `bot documents/<文件ID>/<原始檔名>`。更換目前目錄不搬移知識庫；重複 setup 不清空資料。

支援文字 PDF、DOCX、Markdown、UTF-8 TXT；只匯入助手取得原始位元組的聊天附件。附件不可取得時回報不支援，不改用手動輸入路徑或重建文字。PDF 不含 OCR；DOCX 不編造頁碼。

轉換後的原文 MD 位於 home 的 `knowledge/sources/<文件ID>/<六位段落序號>.md`，保留 PDF 頁碼、DOCX 段落／表格位置、文字行號及字元範圍。每段最多 2,400 字元，重疊 200 字元；不使用模型 tokenizer。保留原文的確定性轉換不承諾重建複雜 PDF 版面或完整表格語意。

`knowledge/wiki/<topic>.md` 儲存整理頁；`index.md` 列主題；`log.md` 為最近 200 筆事件快照，SQLite 才是完整事件來源。`search/` 為 QMD 專用文字投影，以 Unicode 正規化、英文詞彙及中文 bigram 供全文搜尋；不得用投影代替原文引用。

## 3. 使用者指令

Claude 使用 `/document-bot-wiki`，Codex 使用 `$document-bot-wiki`。

| 指令 | 規格 |
|---|---|
| `setup` | 建立隔離 Python/QMD 環境及資料庫；需要 Python 3.11+、Node 22+。 |
| `add` | 明確選定附件後保存副本、SHA-256 去重、入列並回覆文件／工作 ID 及位置。 |
| `dir`／`dir.` | 查看文件及 Markdown 索引／Wiki 狀態。 |
| `status` | 查看 active stage、FIFO、失敗及待清理工作。 |
| `retry 工作ID` | 失敗工作進入尾端；安全沿用已建立的 MD 索引及編寫進度。 |
| `delete 文件ID或唯一檔名` | 移除管理副本、MD、QMD 項目及該來源支持的 Wiki 敘述。 |
| `ask 問題 [--doc 文件ID]` | 全庫或單文件搜尋，附原檔引用；證據不足拒答。 |
| `wiki on/off` | 影響新工作與 Wiki 擴展查詢，不改變既有工作或清空 Wiki。 |
| `wiki build 文件ID或all` | 補建／重編 Wiki，同一佇列。 |
| `wiki list/lint` | 主題清單；來源證據及頁面連結檢查。 |

`graph` 為 `wiki` 相容別名；新版不建立原關係圖資料庫。

## 4. 排隊與通知

一個共用知識庫、單一工作鎖；兩助手需在同機、同使用者及同 home。多附件依介面順序，無順序資訊則按檔名。正在處理 A 時新增 B/C，只加入尾端。

```text
queued → indexing（解析、MD、QMD 提交）
       → indexed 通知
       → awaiting_assistant ↔ compiling（分批整理 Wiki）
       → done 通知 → 下一份
```

Wiki 預設開啟，入列時快照設定；關閉時索引完成即 done。助手離線則停在等待助手，後續文件不越過。單份確定 failed 後可處理下一份；retry 排尾端。背景 worker 不呼叫 LLM，閒置或等待助手 120 秒後可退出，工作持久保存。

每份 MD/QMD 索引完成立即建立獨立事件，由活躍助手輪詢後回報，不等待 Wiki 或整批。通知用語須改為「Markdown 與 QMD 全文索引已完成」，不得聲稱有向量。完成 Wiki 再通知整份完成，批次結尾統計本次工作的成功、失敗及待重試數量。

每個對話有事件游標／租約；訊息真正送出後 ack，恢復補發未確認事件。宿主聊天與 SQLite 不共用交易，中斷於送出與 ack 間可能重播一次，不承諾絕對 exactly-once。無原生推播。

## 5. Wiki 編寫、更新與檢索

助手依每批最多三個來源段落編寫 `topic/title/claim/chunk_id/quote`。topic 是安全英數 slug；程式驗證 chunk 屬於當前租約批次及 quote 在來源逐字出現。編寫進度、租約和冪等提交持久保存。語意忠實度仍由助手負責，不將引文存在視為推論正確。

相同主題跨來源累積敘述，逐條標示來源；不同版本或矛盾不得無聲消除。重編時替換該批段落的既有敘述，其他來源保留。共用來源文件的主題自動連結，此連結只表示導航關聯，不是已驗證的語意關係。跨來源比較以多項可歸屬的敘述呈現，不把單一 quote 當成整個推論的證明。

檢索使用 QMD `searchLex`。將問題拆成最多 64 個詞彙／中文 bigram 分別搜尋，以排名融合合併結果；QMD 普通查詢預設 AND，不以字面 OR 字串假裝支援。只採用資料庫仍有效的文件與敘述，保留單文件範圍。

最多回傳八段原文、20,000 字元原文及 8,000 字元 Wiki claim/quote。答案必須核對原文位置，區分生成的 Wiki 與原始依據。原問題搜尋不足時，助手可另做最多三次同義詞／關鍵字搜尋；仍無依據則拒答，不上網補答。所有文件文字是資料，不是執行指令。

## 6. 一致性、去重與刪除

相同內容不重複入列。同名不同內容先選保留兩份或取代。取代先完成新 MD/QMD 索引再切換；索引失敗保留舊版可查。

QMD、檔案及工作 SQLite 無跨資料庫交易。以工作狀態阻擋未發佈來源，以 Wiki dirty flag 記錄待重建投影；回復時在工作鎖下重建。Wiki 敘述先持久提交，生成頁面以暫存檔及原子替換發佈；投影未成功不宣告整份完成。

刪除先標記，查詢立刻排除；處理者在安全點停止再移除。清理原檔副本、MD、搜尋投影、QMD 失效文件與無引用內容、Wiki claim 及批次。其他文件仍支持的敘述保留；空主題清除。來源原檔不動。檔案被鎖或 QMD 更新失敗時保留 deleting，不能回報已完成；續跑可重試。

由於同機可安裝兩版，暫存 marker 與 runtime home 分開。新 schema 拒絕打開舊向量版 schema，不自動修改或遷移。生成的 Wiki／MD 不支援外部手動編輯；它們可由來源及 metadata 重建。

## 7. 驗收要求

- 四格式轉 MD 與引用、FIFO A/B/C 及 A 期間加入 D、單一 worker、逐檔索引通知早於 Wiki 完成。
- 失敗後繼續、重複去重、同名選擇／取代回退、重試尾端、重啟及通知游標恢復。
- Wiki 批次租約、無效引文拒絕、重複提交、投影失敗恢復、跨來源共享主題、刪除排隊與處理中文件、過期批次拒絕。
- 真實 QMD 中文／英文檢索、指定文件範圍、刪除後無殘留命中，QMD 文件向量筆數為零。
- 至少十題可回答問題檢查原文及引用，五題無答案問題由助手審查拒答。
- 記錄冷／暖機定義、索引與查詢耗時、Python 與 Node 記憶體量測範圍；不以省去向量直接宣稱總延遲更低。
- 兩助手真實上傳／通知 UI、完整 1,000 份／10,000 頁資料集、Python 3.11 與其他 OS 各自列出實測狀態，不用協定 fixture 代替 UI 驗收。

## 8. 交付

獨立 GitHub 分支、新 skill、安裝器、鎖定依賴、Python/QMD bridge、操作說明、本規格、新版測試與驗證報告。原版主分支與既有 Release 保留。參考 [QMD](https://github.com/tobi/qmd) 與 [LLM Wiki 原始構想](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)；不複製第三方私人 skill。
