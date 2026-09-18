import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'document-bot-wiki/scripts'))
from wikibot_core.engine import Bot
from wikibot_core.storage import BotError, FileLock, Store
from wikibot_core.parsing import parse
from wikibot_core.markdown import split_passages


class FakeQmd:
    """Lifecycle test double, not a production search fallback."""
    def __init__(self):
        self.fail = False
        self.hits = []
        self.callback = None

    def update(self):
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        if self.fail:
            raise BotError('qmd_failed', 'Injected failure')
        return {}

    def search(self, question, limit):
        return self.hits[:limit]


@pytest.fixture
def env():
    root = ROOT/'.test-data'/('wiki-'+uuid.uuid4().hex)
    workspace = root/'workspace'
    workspace.mkdir(parents=True)
    qmd = FakeQmd()
    bot = Bot(root/'home', initialize=True, qmd=qmd)
    bot.setup(workspace)
    yield bot, root, qmd
    bot.store.close()


def enqueue(env, name='a.txt', text='員工每年有 15 天特休。', choice=None):
    bot, root, _ = env
    path = root/uuid.uuid4().hex/name
    path.parent.mkdir()
    path.write_text(text, encoding='utf-8')
    item = {'attachment_id':uuid.uuid4().hex, 'name':name, 'path':str(path)}
    result = bot.add({'source':'chat_attachment', 'host':'codex', 'order_known':True, 'attachments':[item]}, {item['attachment_id']:choice} if choice else None)['files'][0]
    return result, path


def compile_one(bot, topic='leave', claim='每年特休十五天。'):
    batch = bot.wiki_next('owner')
    chunk = batch['chunks'][0]
    payload = {'batch_id':batch['batch_id'], 'claims':[{'topic':topic, 'title':'休假政策',
        'claim':claim, 'quote':chunk['text'], 'chunk_id':chunk['chunk_id']}]}
    return payload, bot.wiki_commit(payload, 'owner')


def test_fifo_add_during_index_and_notifications(env):
    bot, _, qmd = env
    a, _ = enqueue(env)
    b, _ = enqueue(env, 'b.txt', 'B')
    qmd.callback = lambda: enqueue(env, 'c.txt', 'C')
    assert bot.work()['status']=='awaiting_assistant'
    events = bot.events('chat','owner')
    assert [e['kind'] for e in events['events']].count('indexed')==1
    assert not any(e['kind']=='completed' for e in events['events'])
    assert [j['name'] for j in bot.status()['jobs']]==['a.txt','b.txt','c.txt']
    bot.ack('chat','owner',events['ack_through'])
    assert not bot.events('chat','owner')['events']
    compile_one(bot)
    assert bot.work()['job_id']==b['job_id']


def test_shared_topic_delete_preserves_other_source(env):
    bot, _, qmd = env
    a, original = enqueue(env)
    bot.work(); compile_one(bot)
    b, _ = enqueue(env, 'b.txt', '第二份文件支持休假規則。')
    bot.work(); compile_one(bot)
    assert bot.wiki_list()['pages'][0]['claims']==2
    bot.delete(a['doc_id'])
    assert original.exists()
    assert bot.wiki_list()['pages'][0]['claims']==1
    assert not bot.artifact('knowledge','sources',a['doc_id']).exists()
    page = bot.artifact('knowledge','wiki','leave.md').read_text(encoding='utf-8')
    assert a['doc_id'] not in page and b['doc_id'] in page
    assert bot.wiki_lint()['status']=='ok'
    bot.delete(b['doc_id'])
    assert not bot.artifact('knowledge','wiki','leave.md').exists()


def test_claim_quote_validation_and_lease(env):
    bot, _, _ = env
    enqueue(env); bot.work()
    batch = bot.wiki_next('a')
    with pytest.raises(BotError, match='另一個助手'):
        bot.wiki_next('b')
    payload = {'batch_id':batch['batch_id'], 'claims':[{'topic':'leave','title':'Leave','claim':'fake','quote':'absent','chunk_id':batch['chunks'][0]['chunk_id']}]}
    with pytest.raises(BotError) as error:
        bot.wiki_commit(payload,'a')
    assert error.value.code=='unsupported_wiki_evidence'
    assert bot.db.execute('SELECT count(*) FROM wiki_claims').fetchone()[0]==0


def test_commit_idempotent_and_recover_projection_failure(env):
    bot, _, qmd = env
    enqueue(env); bot.work()
    qmd.fail = True
    # Acquire before injecting projection failure; indexed projection is already clean.
    batch = bot.wiki_next('owner')
    payload = {'batch_id':batch['batch_id'],'claims':[{'topic':'leave','title':'Leave','claim':'15 days', 'quote':'15','chunk_id':batch['chunks'][0]['chunk_id']}]}
    with pytest.raises(BotError):
        bot.wiki_commit(payload,'owner')
    assert bot.store.setting('projection_dirty')=='1'
    assert bot.status()['jobs'][0]['state']=='awaiting_assistant'
    qmd.fail = False
    bot.wiki_commit(payload,'owner')
    assert bot.wiki_commit(payload,'owner')['status']=='already_committed'
    assert bot.db.execute('SELECT count(*) FROM wiki_claims').fetchone()[0]==1


def test_delete_during_index_and_stale_batch(env):
    bot, _, qmd = env
    a, source = enqueue(env)
    qmd.callback = lambda: bot.delete(a['doc_id'])
    bot.work()
    assert not bot.documents() and source.exists()
    b,_=enqueue(env)
    bot.work(); batch=bot.wiki_next('owner')
    bot.delete(b['doc_id'])
    with pytest.raises(BotError):
        bot.wiki_commit({'batch_id':batch['batch_id'],'claims':[]},'owner')


def test_failure_continues_and_retry_at_tail(env):
    bot, _, qmd=env
    a,_=enqueue(env)
    b,_=enqueue(env,'b.txt','B')
    qmd.fail=True
    assert bot.work(max_jobs=1)['processed']==1
    qmd.fail=False
    retry=bot.retry(a['job_id'])
    assert [j['id'] for j in bot.status()['jobs'] if j['state']=='queued']==[b['job_id'],retry['job_id']]
    assert bot.work()['job_id']==b['job_id']


def test_replace_failed_preserves_old_then_success(env):
    bot, _, qmd=env
    bot.wiki_toggle(False)
    a,_=enqueue(env); bot.work()
    conflict,_=enqueue(env,text='新的內容')
    assert conflict['code']=='name_conflict'
    b,_=enqueue(env,text='新的內容',choice={'action':'replace','document_id':a['doc_id']})
    qmd.fail=True;bot.work();qmd.fail=False
    assert bot.store.resolve_doc(a['doc_id'])['status']=='ready'
    bot.retry(b['job_id']);bot.work()
    assert [d['id'] for d in bot.documents()]==[b['doc_id']]


def test_dedup_snapshot_and_worker_lock(env):
    bot,_,_=env
    a,_=enqueue(env)
    duplicate,_=enqueue(env)
    assert duplicate['status']=='duplicate'
    bot.wiki_toggle(False)
    with FileLock(bot.store.home/'worker.lock'):
        with pytest.raises(BotError): bot.work()
    assert bot.work()['status']=='awaiting_assistant'
    compile_one(bot)
    assert bot.status()['vectors_enabled'] is False


def test_scoped_evidence_and_stale_hits(env):
    bot,_,qmd=env
    bot.wiki_toggle(False)
    a,_=enqueue(env);b,_=enqueue(env,'b.txt','B');bot.work()
    qmd.hits=[{'filepath':f'qmd://sources/{a["doc_id"]}/000000.md','score':1}, {'filepath':f'qmd://sources/{b["doc_id"]}/000000.md','score':.5}]
    assert {e['doc_id'] for e in bot.ask('query',b['doc_id'])['evidence']}=={b['doc_id']}
    bot.delete(a['doc_id'])
    assert {e['doc_id'] for e in bot.ask('query')['evidence']}=={b['doc_id']}


@pytest.mark.parametrize('filename',['warranty.pdf','產品規格.docx','員工手冊.md','維運說明.txt'])
def test_formats_to_markdown(filename):
    chunks=split_passages(parse(ROOT/'examples'/filename)[0])
    assert chunks and all(len(c.text)<=2400 for c in chunks)
    assert all(c.locator for c in chunks)


def test_reject_original_database_before_schema_changes(env):
    bot,root,_=env
    path=root/'old';path.mkdir()
    db=sqlite3.connect(path/'library.sqlite3')
    db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
    db.execute("INSERT INTO settings VALUES('schema_version','1')");db.commit();db.close()
    with pytest.raises(BotError): Store(path,initialize=True)
    db=sqlite3.connect(path/'library.sqlite3')
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('settings',)]
    db.close()


def test_install_side_by_side(env):
    _,root,_=env
    spec=importlib.util.spec_from_file_location('install_wiki',ROOT/'install-wiki.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for host in ('claude','codex'):
        result=module.install(host,root/'profile')
        assert (Path(result['path'])/'scripts/qmd_bridge.mjs').exists()
        assert (Path(result['path'])/'qmd-package/package-lock.json').exists()


def test_deletion_qmd_failure_is_retryable(env):
    bot,_,qmd=env
    a,_=enqueue(env);bot.work();compile_one(bot)
    qmd.fail=True
    assert bot.delete(a['doc_id'])['status']=='deleting'
    assert bot.status()['pending_cleanup']
    qmd.fail=False
    bot.work()
    assert not bot.documents() and not bot.status()['pending_cleanup']
    assert bot.wiki_lint()['status']=='ok'


def test_recompile_replaces_only_this_sources_claims(env):
    bot,_,_=env
    a,_=enqueue(env);bot.work();compile_one(bot,claim='Old summary')
    b,_=enqueue(env,'b.txt','Another source');bot.work();compile_one(bot,claim='Other source')
    bot.wiki_build(a['doc_id']);bot.work();compile_one(bot,claim='Revised summary')
    claims={r[0] for r in bot.db.execute('SELECT claim FROM wiki_claims')}
    assert claims=={'Revised summary','Other source'}


def test_reopen_recovers_indexing_and_notification_cursor(env):
    bot,_,qmd=env
    bot.events('chat','owner',from_now=True)
    a,_=enqueue(env)
    bot.db.execute("UPDATE jobs SET state='indexing' WHERE id=?",(a['job_id'],))
    other=Bot(bot.store.home,qmd=qmd)
    try:
        assert other.work()['status']=='awaiting_assistant'
        replay=other.events('chat','owner',from_now=True)
        assert any(e['kind']=='indexed' for e in replay['events'])
    finally: other.store.close()


def test_path_traversal_claim_rejected(env):
    bot,_,_=env
    enqueue(env);bot.work();batch=bot.wiki_next('owner')
    with pytest.raises(BotError):
        bot.wiki_commit({'batch_id':batch['batch_id'],'claims':[{
            'topic':'../outside','title':'x','claim':'x','quote':'15',
            'chunk_id':batch['chunks'][0]['chunk_id']}]},'owner')
    assert not bot.db.execute('SELECT 1 FROM wiki_claims').fetchone()


def test_wiki_toggle_excludes_generated_page_retrieval(env):
    bot,_,qmd=env
    enqueue(env);bot.work();compile_one(bot)
    qmd.hits=[{'filepath':'qmd://wiki/leave.md','score':1}]
    assert bot.ask('休假政策')['wiki_pages']
    bot.wiki_toggle(False)
    assert bot.ask('休假政策')['status']=='no_evidence'


def test_failed_staging_candidates_cannot_hide_committed_source(env):
    bot,_,qmd=env
    bot.wiki_toggle(False)
    a,_=enqueue(env);bot.work()
    qmd.hits=[{'filepath':f'qmd://sources/{uuid.uuid4().hex}/000000.md','score':1} for _ in range(90)]
    qmd.hits.append({'filepath':f'qmd://sources/{a["doc_id"]}/000000.md','score':.1})
    assert bot.ask('query',a['doc_id'])['evidence'][0]['doc_id']==a['doc_id']
