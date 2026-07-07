"""rag: chunking (prose/faq/product) + RRF merge — pure, no DB."""
from __future__ import annotations

import uuid

from apps.api.app.ai import rag


def test_approx_tokens():
    assert rag.approx_tokens("") == 0
    assert rag.approx_tokens("abcd") == 1
    assert rag.approx_tokens("a" * 400) == 100


def test_chunk_prose_splits_long_text_with_overlap():
    para = "This is a sentence about shipping and returns. " * 40  # ~well over target
    text = "# Shipping\n\n" + para + "\n\n## Returns\n\n" + para
    chunks = rag.chunk_prose(text, target_tokens=120, max_tokens=160, overlap_ratio=0.15)
    assert len(chunks) >= 2
    # no chunk should be wildly larger than max (hard-split guards runaways)
    assert all(rag.approx_tokens(c) <= 320 for c in chunks)


def test_chunk_prose_short_text_single_chunk():
    chunks = rag.chunk_prose("Just a short answer.")
    assert chunks == ["Just a short answer."]


def test_chunk_prose_empty():
    assert rag.chunk_prose("") == []


def test_chunk_faq_one_per_pair():
    items = [
        {"question": "How do I return?", "answer": "Within 30 days."},
        {"q": "Shipping cost?", "a": "Free over $50."},
    ]
    specs = rag.chunk_faq(items)
    assert len(specs) == 2
    assert specs[0].meta["source_type"] == "faq"
    assert "How do I return?" in specs[0].text
    assert "Within 30 days." in specs[0].text


def test_chunk_products_structured_with_handle():
    items = [{"handle": "sku-1", "title": "Blue Widget", "price": "19.99",
              "currency": "USD", "description": "A nice widget."}]
    specs = rag.chunk_products(items)
    assert len(specs) == 1
    assert specs[0].meta["source_type"] == "product"
    assert specs[0].meta["handle"] == "sku-1"
    assert specs[0].meta["price"] == "19.99"
    assert "Blue Widget" in specs[0].text


def test_build_chunks_dispatch():
    prose = rag.build_chunks(source_type="upload", content="Hello world.")
    assert prose and prose[0].meta["source_type"] == "upload"
    faq = rag.build_chunks(source_type="faq", content=[{"q": "a", "a": "b"}])
    assert faq[0].meta["source_type"] == "faq"
    prod = rag.build_chunks(source_type="product", content=[{"handle": "h1", "title": "T"}])
    assert prod[0].meta["handle"] == "h1"


def test_build_chunks_merges_base_meta():
    specs = rag.build_chunks(source_type="upload", content="x " * 10, base_meta={"lang": "en"})
    assert all(s.meta.get("lang") == "en" for s in specs)


# --------------------------------------------------------------------------
# RRF merge
# --------------------------------------------------------------------------
def test_rrf_merge_prefers_ids_high_in_both_lists():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    # a is #1 in list1 and #2 in list2; b is #2 and #1; c/d appear once
    vec = [a, b, c]
    lex = [b, a, d]
    ranked = rag.rrf_merge([vec, lex], k=60)
    ids = [cid for cid, _ in ranked]
    assert set(ids) == {a, b, c, d}
    # a and b (in both lists) outrank c and d (single list)
    assert ids.index(a) < ids.index(c)
    assert ids.index(b) < ids.index(d)


def test_rrf_merge_score_monotonic_with_rank():
    x, y = uuid.uuid4(), uuid.uuid4()
    ranked = dict(rag.rrf_merge([[x, y]], k=60))
    assert ranked[x] > ranked[y]  # earlier rank → higher score


def test_rrf_merge_empty():
    assert rag.rrf_merge([]) == []
    assert rag.rrf_merge([[]]) == []


def test_retrieved_context_text_numbered():
    chunks = [
        rag.RetrievedChunk(id=uuid.uuid4(), document_id=uuid.uuid4(), text="alpha", meta={}, score=1.0),
        rag.RetrievedChunk(id=uuid.uuid4(), document_id=uuid.uuid4(), text="beta", meta={}, score=0.5),
    ]
    r = rag.Retrieved(chunks=chunks)
    assert r.hit is True
    ctx = r.context_text()
    assert "[1] alpha" in ctx and "[2] beta" in ctx
    assert rag.Retrieved().hit is False


def test_retrieved_chunk_handle():
    c = rag.RetrievedChunk(id=uuid.uuid4(), document_id=uuid.uuid4(), text="t",
                           meta={"handle": "sku-9"}, score=1.0)
    assert c.handle == "sku-9"
    assert rag.RetrievedChunk(id=uuid.uuid4(), document_id=uuid.uuid4(), text="t",
                              meta={}, score=1.0).handle is None
