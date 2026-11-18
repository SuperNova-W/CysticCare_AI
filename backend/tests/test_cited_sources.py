import unittest

from app.services.runtime_pipeline import RetrievalOutput, cited_sources


def _retrieval(sources, chunk_papers=None):
    """chunk_papers: display_name of each context chunk, in context order."""
    chunk_papers = chunk_papers or [s["display_name"] for s in sources]
    results = [
        {"id": f"chunk-{i}", "metadata": {"display_name": name}}
        for i, name in enumerate(chunk_papers)
    ]
    return RetrievalOutput(query="q", results=results, sources=sources, metadata={}, context="")


BENZ = {"index": 1, "author": "Eric G. Benz, Erum A. Hartung", "year": "2021",
        "display_name": "Eric G. Benz, Erum A. Hartung 2021"}
KDIGO = {"index": 2, "author": "Kidney Disease: Improving Global Outcomes (KDIGO) ADPKD Work Group",
         "year": "2025", "display_name": "Kidney Disease: Improving Global Outcomes (KDIGO) ADPKD Work Group 2025"}
PERRONE = {"index": 3, "author": "Ronald D. Perrone, Kaleab Z. Abebe", "year": "2021",
           "display_name": "Ronald D. Perrone 2021"}


class CitedSourcesTests(unittest.TestCase):
    def test_answer_without_attribution_shows_no_sources(self):
        retrieval = _retrieval([BENZ, KDIGO, PERRONE])
        self.assertEqual(cited_sources("ADPKD is an inherited kidney disease.", retrieval), [])

    def test_only_cited_sources_are_kept_in_order(self):
        retrieval = _retrieval([BENZ, KDIGO, PERRONE])
        answer = "Metformin was studied (Perrone et al., 2021); see also Hartung 2021."
        self.assertEqual(cited_sources(answer, retrieval), [BENZ, PERRONE])

    def test_any_author_surname_or_acronym_with_year_counts(self):
        retrieval = _retrieval([BENZ, KDIGO, PERRONE])
        self.assertEqual(cited_sources("Per KDIGO (2025) guidance...", retrieval), [KDIGO])
        self.assertEqual(cited_sources("... (Group 2025).", retrieval), [KDIGO])

    def test_surname_with_wrong_year_does_not_count(self):
        retrieval = _retrieval([PERRONE])
        self.assertEqual(cited_sources("Perrone 2015 reported ...", retrieval), [])

    def test_source_label_maps_context_chunk_to_deduplicated_source(self):
        # Context chunks: 1=Benz, 2=Benz (same paper), 3=Perrone
        retrieval = _retrieval(
            [BENZ, PERRONE],
            chunk_papers=[BENZ["display_name"], BENZ["display_name"], PERRONE["display_name"]],
        )
        self.assertEqual(cited_sources("As shown [Source 3: Ronald D. Perrone 2021].", retrieval), [PERRONE])
        self.assertEqual(cited_sources("See Sources 2 and 3.", retrieval), [BENZ, PERRONE])

    def test_out_of_range_source_label_is_ignored(self):
        retrieval = _retrieval([BENZ])
        self.assertEqual(cited_sources("(Source 9)", retrieval), [])


if __name__ == "__main__":
    unittest.main()
