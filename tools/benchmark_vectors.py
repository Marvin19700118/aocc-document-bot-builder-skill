"""Synthetic storage/search scale check, not an ingestion or semantic-accuracy benchmark."""
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import time
import uuid

import numpy as np
import psutil
import sqlite_vec


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / ".test-runtime" / "benchmarks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid.uuid4().hex + ".sqlite3")
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("CREATE VIRTUAL TABLE vectors USING vec0(embedding float[384] distance_metric=cosine, doc_id text)")
    rng = np.random.default_rng(20260918)
    vectors = rng.standard_normal((50000, 384), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    start = time.perf_counter()
    db.executemany("INSERT INTO vectors(rowid,embedding,doc_id) VALUES(?,?,?)", ((i+1, vector.tobytes(), str(i//50)) for i, vector in enumerate(vectors)))
    db.commit()
    insert_seconds = time.perf_counter()-start
    timings, scoped, correct = [], [], 0
    for index in range(0, 50000, 500):
        query = vectors[index].tobytes()
        start = time.perf_counter()
        hits = db.execute("SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=8 ORDER BY distance", (query,)).fetchall()
        timings.append(time.perf_counter()-start)
        correct += hits[0][0] == index+1
        start = time.perf_counter()
        db.execute("SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=8 AND doc_id=? ORDER BY distance", (query, str(index//50))).fetchall()
        scoped.append(time.perf_counter()-start)
    db.close()
    report = {"kind": "synthetic sqlite-vec storage/search; excludes model encoding, document parsing, graph extraction and chat latency", "platform": platform.platform(), "python": platform.python_version(), "documents": 1000, "vectors": 50000, "dimensions": 384, "insert_seconds": insert_seconds, "queries": len(timings), "self_match_top1": correct, "global_p50_seconds": statistics.median(timings), "global_p95_seconds": sorted(timings)[94], "scoped_p50_seconds": statistics.median(scoped), "rss_bytes": psutil.Process().memory_info().rss, "db_bytes": path.stat().st_size}
    output = root / "verification" / "scale-benchmark.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
