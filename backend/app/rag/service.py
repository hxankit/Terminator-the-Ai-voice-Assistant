"""Indexing and retrieval. Vectors are stored on chunk docs in MongoDB; similarity computed with numpy.
(For large corpora swap in Atlas Vector Search / Qdrant behind the same `retrieve` signature.)"""
import logging
from pathlib import Path
import numpy as np
from bson import ObjectId
from ..db import db, now
from ..llm import provider
from ..llm.registry import embedding_spec
from .extract import extract, normalize, chunk

log = logging.getLogger("rag")


def _prefix(model: str, kind: str) -> str:
    """nomic-embed-text expects task prefixes; other embedding models don't."""
    return ("search_document: " if kind == "doc" else "search_query: ") if "nomic" in model.lower() else ""


async def index_document(user: dict, doc_id: ObjectId, path: Path) -> dict:
    d = db()
    try:
        sections = extract(path)
        rows = []
        for sec in sections:
            for i, c in enumerate(chunk(normalize(sec["text"]))):
                rows.append({"user_id": user["_id"], "document_id": doc_id, "text": c, "location": sec["location"]})
        if not rows:
            raise ValueError("Document contains no text")
        emb = await embedding_spec(user)
        mode = "keyword"
        if emb:
            spec, model = emb
            try:
                for s in range(0, len(rows), 32):
                    vecs = await provider.embed(spec, [_prefix(model, "doc") + r["text"] for r in rows[s:s + 32]], model)
                    for r, v in zip(rows[s:s + 32], vecs):
                        r["embedding"], r["embed_model"] = v, model
                mode = "vector"
            except provider.LLMError as e:
                log.warning("embedding failed, falling back to keyword retrieval: %s", e)
                for r in rows:
                    r.pop("embedding", None)
                    r.pop("embed_model", None)
        for i, r in enumerate(rows):
            r["ordinal"] = i
        await d.chunks.insert_many(rows)
        upd = {"status": "ready", "chunks": len(rows), "retrieval": mode, "indexed_at": now(),
               "embed_model": emb[1] if emb and mode == "vector" else None}
    except Exception as e:
        log.exception("indexing failed")
        upd = {"status": "failed", "error": str(e)}
    await d.documents.update_one({"_id": doc_id}, {"$set": upd})
    return upd


async def retrieve(user: dict, query: str, k: int = 6, document_ids: list[ObjectId] | None = None) -> list[dict]:
    d = db()
    flt: dict = {"user_id": user["_id"]}
    if document_ids:
        flt["document_id"] = {"$in": document_ids}
    results: dict[ObjectId, dict] = {}
    emb = await embedding_spec(user)
    if emb:
        try:
            spec, model = emb
            qv = np.array((await provider.embed(spec, [_prefix(model, "query") + query], model))[0], dtype=np.float32)
            # only compare vectors made by the same embedding model (and of the same size)
            cand = [c async for c in d.chunks.find({**flt, "embedding": {"$exists": True},
                                                    "embed_model": {"$in": [model, None]}},
                                                   {"text": 1, "document_id": 1, "location": 1, "embedding": 1}).limit(20000)]
            cand = [c for c in cand if len(c["embedding"]) == len(qv)]
            if cand:
                m = np.array([c["embedding"] for c in cand], dtype=np.float32)
                sims = m @ qv / (np.linalg.norm(m, axis=1) * np.linalg.norm(qv) + 1e-9)
                for idx in np.argsort(-sims)[:k]:
                    c = cand[idx]
                    results[c["_id"]] = {**c, "score": float(sims[idx])}
        except (provider.LLMError, ValueError) as e:
            log.warning("vector retrieval failed: %s", e)
    if len(results) < k:  # keyword search covers non-embedded docs and acts as hybrid fallback
        cur = d.chunks.find({**flt, "$text": {"$search": query}},
                            {"score": {"$meta": "textScore"}, "text": 1, "document_id": 1, "location": 1}
                            ).sort([("score", {"$meta": "textScore"})]).limit(k)
        async for c in cur:
            results.setdefault(c["_id"], {**c, "score": float(c["score"])})
    out = sorted(results.values(), key=lambda c: -c["score"])[:k]
    names = {x["_id"]: x["filename"] async for x in d.documents.find({"_id": {"$in": list({c["document_id"] for c in out})}})}
    return [{"document_id": str(c["document_id"]), "filename": names.get(c["document_id"], "?"), "location": c["location"],
             "text": c["text"], "score": round(c["score"], 3)} for c in out]


async def reindex(user: dict, doc: dict) -> dict:
    await db().chunks.delete_many({"document_id": doc["_id"]})
    return await index_document(user, doc["_id"], Path(doc["path"]))


async def reindex_keyword_only():
    """At startup: give documents that were indexed before an embedding model existed real vectors."""
    d = db()
    async for doc in d.documents.find({"status": "ready", "retrieval": {"$ne": "vector"}}):
        user = await d.users.find_one({"_id": doc["user_id"]})
        if user and await embedding_spec(user) and Path(doc["path"]).exists():
            res = await reindex(user, doc)
            log.info("reindexed %s: %s", doc["filename"], res.get("retrieval"))
