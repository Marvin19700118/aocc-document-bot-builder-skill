"""Real QMD lexical acceptance. Uses generated fixtures, never claims host UI testing."""
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'document-bot-wiki/scripts'))
from wikibot_core.engine import Bot

QUESTIONS = [
    ('特休假每年有幾天？', '20 天', '員工手冊.md'),
    ('遠端工作每週最多幾天？', '3 天', '員工手冊.md'),
    ('出差住宿每晚補助上限？', '3,000 元', '員工手冊.md'),
    ('教育訓練每年的預算？', '18,000 元', '員工手冊.md'),
    ('離職前需提前幾天通知？', '30 天', '員工手冊.md'),
    ('Aurora 的資料匯出格式？', 'JSON 與 CSV', '產品規格.docx'),
    ('Aurora 客服電話是什麼？', '02-5555-0100', '產品規格.docx'),
    ('How long is the hardware warranty?', '24 months', 'warranty.pdf'),
    ('備份資料保存多久？', '90 天', '維運說明.txt'),
    ('維護時段是哪一天幾點？', '星期日凌晨 02:00', '維運說明.txt'),
    ('董事長的私人手機號碼？', None, None),
    ('公司明年的營收預測？', None, None),
    ('Aurora 的上市股價是多少？', None, None),
    ('公司醫療保險的理賠比例？', None, None),
    ('產品是否通過 ISO 27001 認證？', None, None),
]


def main():
    import psutil
    base=ROOT/'.test-runtime'/('wiki-live-'+uuid.uuid4().hex)
    workspace=base/'workspace';workspace.mkdir(parents=True)
    bot=Bot(base/'home',initialize=True)
    start=time.perf_counter();bot.setup(workspace);setup_seconds=time.perf_counter()-start
    bot.wiki_toggle(False)
    files=sorted((ROOT/'examples').iterdir())
    result=bot.add({'source':'chat_attachment','host':'codex','order_known':True,
        'attachments':[{'attachment_id':'generated:'+p.name,'name':p.name,'path':str(p)} for p in files]})
    assert all(r['status']=='queued' for r in result['files'])
    start=time.perf_counter();bot.work();index_seconds=time.perf_counter()-start
    assert all(d['status']=='ready' for d in bot.documents())
    queries=[]
    for question,expected,filename in QUESTIONS:
        start=time.perf_counter();answer=bot.ask(question);elapsed=time.perf_counter()-start
        found=any(expected in e['text'] and filename==e['filename'] for e in answer['evidence']) if expected else None
        queries.append({'question':question,'expected':expected,'expected_file':filename,
            'expected_evidence_found':found,'seconds':elapsed,'retrieved':answer})
    # Deterministic payload fixture validates protocol; does not simulate an LLM.
    bot.wiki_toggle(True)
    doc=next(d for d in bot.documents() if d['name']=='產品規格.docx')
    bot.wiki_build(doc['id']);bot.work()
    while True:
        batch=bot.wiki_next('fixture')
        if batch['status']!='batch': break
        claims=[{'topic':'aurora','title':'Aurora 產品知識','claim':c['text'],
                 'chunk_id':c['chunk_id'],'quote':c['text']} for c in batch['chunks']]
        bot.wiki_commit({'batch_id':batch['batch_id'],'claims':claims},'fixture')
    assert bot.wiki_lint()['status']=='ok'
    wiki_query=bot.ask('Aurora')
    assert wiki_query['wiki_pages']
    deletion=bot.delete(doc['id'])
    assert deletion['status']=='deleted'
    assert not bot.ask('Aurora', None)['wiki_pages']
    assert not bot.ask('Aurora')['evidence']  # No other fixture contains Aurora.
    db=sqlite3.connect(bot.store.home/'qmd.sqlite')
    vector_rows=db.execute('SELECT count(*) FROM content_vectors').fetchone()[0]
    assert vector_rows==0
    db.close()
    assert bot.wiki_lint()['status']=='ok'
    report={'platform':platform.platform(),'python':platform.python_version(),'qmd':'2.8.3',
        'mode':'lexical SDK only; no embedding/expansion/reranking',
        'attachment_adapter':'generated manifest; not host UI',
        'setup_seconds':setup_seconds,'index_seconds':index_seconds,
        'query_median_seconds':statistics.median(q['seconds'] for q in queries),
        'python_rss_bytes':psutil.Process().memory_info().rss,
        'memory_scope':'Python only; excludes short-lived Node subprocess peaks',
        'qmd_vector_rows':vector_rows,'answerable_hits':sum(q['expected_evidence_found'] is True for q in queries),
        'answerable_count':10,'unanswerable':'5 evidence sets saved; requires assistant review',
        'wiki_payload':'deterministic protocol fixture, not autonomous LLM synthesis',
        'wiki_query':wiki_query,'deletion':deletion,'queries':queries}
    out=ROOT/'verification/wiki';out.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(report,ensure_ascii=False,indent=2)
    raw=raw.replace(json.dumps(str(ROOT))[1:-1],'<PROJECT_ROOT>')
    (out/'live-check.json').write_text(raw,encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['answerable_hits','index_seconds','query_median_seconds','qmd_vector_rows']},indent=2))
    bot.store.close()


if __name__=='__main__': main()
