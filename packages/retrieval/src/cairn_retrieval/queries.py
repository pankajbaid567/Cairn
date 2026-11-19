"""Every registered retrieval query (design §12). The only module that names protected tables."""

from __future__ import annotations

from cairn_retrieval.registry import RegisteredQuery, register

FTS_CANDIDATES = register(
    RegisteredQuery(
        name="fts_candidates",
        template=(
            "SELECT c.chunk_id, "
            "ts_rank_cd(c.tsv_en, qe, 32) + ts_rank_cd(c.tsv_simple, qs, 32) AS score "
            "FROM cairn.chunks c, "
            "websearch_to_tsquery('english', %(q)s) qe, websearch_to_tsquery('simple', %(q)s) qs "
            "WHERE (c.tsv_en @@ qe OR c.tsv_simple @@ qs) AND c.is_current AND {acl:c} "
            "ORDER BY score DESC, c.chunk_id LIMIT %(k)s"
        ),
        protected_aliases=("c",),
    )
)

DENSE_CANDIDATES = register(
    RegisteredQuery(
        name="dense_candidates",
        template=(
            "SELECT e.chunk_id, e.embedding <=> %(qv)s::vector AS distance "
            "FROM cairn.embeddings e "
            "WHERE e.model_key = %(model)s AND {acl:e} "
            "ORDER BY e.embedding <=> %(qv)s::vector LIMIT %(k)s"
        ),
        protected_aliases=("e",),
        session=("hnsw.iterative_scan", "hnsw.ef_search"),
    )
)

DENSE_EXACT_CANDIDATES = register(
    RegisteredQuery(
        name="dense_exact_candidates",
        template=(
            "WITH visible AS MATERIALIZED ("
            "SELECT e.chunk_id, e.embedding FROM cairn.embeddings e "
            "WHERE e.model_key = %(model)s AND {acl:e}) "
            "SELECT v.chunk_id, v.embedding <=> %(qv)s::vector AS distance FROM visible v "
            "ORDER BY v.embedding <=> %(qv)s::vector LIMIT %(k)s"
        ),
        protected_aliases=("e",),
    )
)

CHUNK_DETAILS = register(
    RegisteredQuery(
        name="chunk_details",
        template=(
            "SELECT c.chunk_id, c.item_id, i.external_id, r.content_hash AS revision_hash, "
            'c.start_offset AS start, c.end_offset AS "end", c.text, c.title, c.source_kind, '
            "c.heading_path "
            "FROM cairn.chunks c "
            "JOIN cairn.revisions r ON r.revision_id = c.revision_id AND {acl:r} "
            "JOIN cairn.items i ON i.item_id = c.item_id AND {acl:i} "
            "WHERE c.chunk_id = ANY(%(ids)s) AND c.is_current AND {acl:c}"
        ),
        protected_aliases=("r", "i", "c"),
    )
)

RESOLVE_CITATION = register(
    RegisteredQuery(
        name="resolve_citation",
        template=(
            "SELECT substr(r.text, %(start)s + 1, %(len)s) AS quote, i.title, i.source_kind, "
            "i.external_id, i.item_id "
            "FROM cairn.revisions r JOIN cairn.items i ON i.item_id = r.item_id AND {acl:i} "
            "WHERE i.external_id = %(external_id)s AND starts_with(r.content_hash, %(hash8)s) "
            "AND {acl:r}"
        ),
        protected_aliases=("i", "r"),
    )
)

# --- as-of evidence (plan 2.9, 2.16): evidence recorded after the requested recorded time is never used ---  # noqa: E501

FTS_CANDIDATES_ASOF = register(
    RegisteredQuery(
        name="fts_candidates_asof",
        template=(
            "SELECT c.chunk_id, "
            "ts_rank_cd(c.tsv_en, qe, 32) + ts_rank_cd(c.tsv_simple, qs, 32) AS score "
            "FROM cairn.chunks c, "
            "websearch_to_tsquery('english', %(q)s) qe, websearch_to_tsquery('simple', %(q)s) qs "
            "WHERE (c.tsv_en @@ qe OR c.tsv_simple @@ qs) AND c.is_current AND {acl:c} "
            "AND EXISTS (SELECT 1 FROM cairn.revisions r WHERE r.revision_id = c.revision_id "
            "AND r.source_updated_at <= %(recorded_before)s AND {acl:r}) "
            "ORDER BY score DESC, c.chunk_id LIMIT %(k)s"
        ),
        protected_aliases=("c", "r"),
    )
)

DENSE_EXACT_CANDIDATES_ASOF = register(
    RegisteredQuery(
        name="dense_exact_candidates_asof",
        template=(
            "WITH visible AS MATERIALIZED ("
            "SELECT e.chunk_id, e.embedding FROM cairn.embeddings e "
            "JOIN cairn.chunks c ON c.chunk_id = e.chunk_id AND c.is_current AND {acl:c} "
            "JOIN cairn.revisions r ON r.revision_id = c.revision_id AND {acl:r} "
            "WHERE e.model_key = %(model)s AND r.source_updated_at <= %(recorded_before)s AND {acl:e}) "  # noqa: E501
            "SELECT v.chunk_id, v.embedding <=> %(qv)s::vector AS distance FROM visible v "
            "ORDER BY v.embedding <=> %(qv)s::vector LIMIT %(k)s"
        ),
        protected_aliases=("c", "r", "e"),
    )
)

# --- decision memory (Phase 2) -----------------------------------------------------------------

UNTRUSTED_ITEMS = register(
    RegisteredQuery(
        name="untrusted_items",
        template=(
            "SELECT i.item_id FROM cairn.items i WHERE i.item_id = ANY(%(ids)s) "
            "AND i.connector = 'github' AND i.source_kind IN ('issue', 'pull_request') "
            "AND EXISTS (SELECT 1 FROM cairn.principals p WHERE p.external_id = 'group:public' "
            "AND p.principal_id = ANY(i.acl)) AND {acl:i}"
        ),
        protected_aliases=("i",),
    )
)

VISIBLE_SOURCES = register(
    RegisteredQuery(
        name="visible_sources",
        template="SELECT DISTINCT i.source_key FROM cairn.items i WHERE {acl:i} ORDER BY i.source_key",  # noqa: E501
        protected_aliases=("i",),
    )
)

ITEM_SOURCES = register(
    RegisteredQuery(
        name="item_sources",
        template="SELECT DISTINCT i.source_key FROM cairn.items i WHERE i.item_id = ANY(%(ids)s) AND {acl:i}",  # noqa: E501
        protected_aliases=("i",),
    )
)

RECHECK_ITEMS = register(
    RegisteredQuery(
        name="recheck_items",
        template="SELECT i.item_id FROM cairn.items i WHERE i.item_id = ANY(%(ids)s) AND {acl:i}",
        protected_aliases=("i",),
    )
)

DECISION_GET = register(
    RegisteredQuery(
        name="decision_get",
        template=(
            "SELECT d.decision_id, d.key, d.title, d.question, d.outcome, d.origin, d.verification "
            "FROM cairn.decisions d WHERE d.key = %(key)s AND {dvis:d}"
        ),
        protected_aliases=(),
        decision_aliases=("d",),
    )
)

DECISION_SEARCH = register(
    RegisteredQuery(
        name="decision_search",
        template=(
            "SELECT d.key, d.title, d.outcome, d.verification FROM cairn.decisions d "
            "WHERE {dvis:d} AND (d.title ILIKE %(pattern)s ESCAPE '\\' OR d.outcome ILIKE %(pattern)s ESCAPE '\\' "  # noqa: E501
            "OR d.question ILIKE %(pattern)s ESCAPE '\\' OR d.key ILIKE %(pattern)s ESCAPE '\\') "
            "ORDER BY d.key LIMIT %(k)s"
        ),
        protected_aliases=(),
        decision_aliases=("d",),
    )
)

DECISION_TIMELINE = register(
    RegisteredQuery(
        name="decision_timeline",
        template=(
            "SELECT e.event, e.valid_at, e.recorded_at, e.by_key, e.note, e.source_ref, e.seq "
            "FROM cairn.decision_events e JOIN cairn.decisions d ON d.decision_id = e.decision_id AND {dvis:d} "  # noqa: E501
            "WHERE d.key = %(key)s AND e.recorded_at <= %(recorded)s::timestamptz AND {acl:e} "
            "ORDER BY e.valid_at, e.seq"
        ),
        protected_aliases=("e",),
        decision_aliases=("d",),
    )
)

DECISION_CLAIMS = register(
    RegisteredQuery(
        name="decision_claims",
        template=(
            "SELECT c.claim_id, c.key, c.text, c.tier FROM cairn.claims c "
            "JOIN cairn.decisions d ON d.decision_id = c.decision_id AND {dvis:d} "
            "WHERE d.key = %(key)s AND {cvis:c} ORDER BY c.key"
        ),
        protected_aliases=(),
        decision_aliases=("d",),
        claim_aliases=("c",),
    )
)

DECISION_CLAIM_CITATIONS = register(
    RegisteredQuery(
        name="decision_claim_citations",
        template=(
            "SELECT c.key AS claim_key, x.support_set, x.external_id, x.title, x.source_ref "
            "FROM cairn.claims c "
            "JOIN cairn.decisions d ON d.decision_id = c.decision_id AND {dvis:d} "
            "CROSS JOIN LATERAL cairn.claim_citations(c.claim_id) x "
            "WHERE d.key = %(key)s AND {cvis:c} ORDER BY c.key, x.support_set, x.item_id"
        ),
        protected_aliases=(),
        decision_aliases=("d",),
        claim_aliases=("c",),
    )
)

DECISION_NEIGHBORS = register(
    RegisteredQuery(
        name="decision_neighbors",
        template=(
            "SELECT df.key AS from_key, dt.key AS to_key, l.kind FROM cairn.decision_links l "
            "JOIN cairn.decisions df ON df.decision_id = l.from_decision AND {dvis:df} "
            "JOIN cairn.decisions dt ON dt.decision_id = l.to_decision AND {dvis:dt} "
            "WHERE (df.key = ANY(%(keys)s) OR dt.key = ANY(%(keys)s)) AND l.recorded_at <= %(recorded)s::timestamptz"  # noqa: E501
        ),
        protected_aliases=(),
        decision_aliases=("df", "dt"),
    )
)

DECISION_ANCHORS = register(
    RegisteredQuery(
        name="decision_anchors",
        template=(
            "SELECT d.key, a.kind, a.value FROM cairn.anchors a "
            "JOIN cairn.decisions d ON d.decision_id = a.decision_id AND {dvis:d} "
            "WHERE a.value = ANY(%(values)s) AND {dvis:a}"
        ),
        protected_aliases=(),
        decision_aliases=("d", "a"),
    )
)

__all__ = [
    "CHUNK_DETAILS",
    "DENSE_CANDIDATES",
    "DENSE_EXACT_CANDIDATES",
    "FTS_CANDIDATES",
    "RESOLVE_CITATION",
]
