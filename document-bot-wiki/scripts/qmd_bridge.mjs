// Only lexical SDK methods are exposed: no embeddings, expansion, or reranking.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const request = JSON.parse(readFileSync(0, 'utf8'));
const { createStore, Maintenance } = await import(pathToFileURL(request.module).href);
const store = await createStore({
  dbPath: request.database,
  config: { collections: {
    sources: { path: request.sources, pattern: '**/*.md' },
    wiki: { path: request.wiki, pattern: '**/*.md' },
  } },
});
try {
  let result;
  if (request.action === 'update') {
    result = await store.update();
    const maintenance = new Maintenance(store.internal);
    maintenance.deleteInactiveDocs();
    maintenance.cleanupOrphanedContent();
    maintenance.optimizeFts();
  } else if (request.action === 'search') {
    const merged = new Map();
    for (const term of request.terms) {
      const hits = await store.searchLex('"' + term + '"', { limit: request.limit });
      hits.forEach(({ filepath }, rank) => {
        merged.set(filepath, (merged.get(filepath) ?? 0) + 1 / (60 + rank));
      });
    }
    result = [...merged].map(([filepath, score]) => ({ filepath, score }))
      .sort((a,b) => b.score-a.score || a.filepath.localeCompare(b.filepath))
      .slice(0, request.limit);
  } else if (request.action === 'status') {
    result = await store.getStatus();
  } else {
    throw new Error('Unsupported lexical operation');
  }
  process.stdout.write(JSON.stringify({ ok: true, data: result }) + '\n');
} finally {
  await store.close();
}
