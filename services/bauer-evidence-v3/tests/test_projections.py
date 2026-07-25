from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest import compile_source  # noqa: E402
from bauer_evidence_v3.planner import analyze_query  # noqa: E402
from bauer_evidence_v3.projections import project_document  # noqa: E402
from bauer_evidence_v3.retrieval import InMemoryEvidenceIndex  # noqa: E402


class ProjectionTests(unittest.TestCase):
    def project(
        self,
        html: str,
        *,
        source_metadata=None,
        source_document_id="source",
    ):
        compiled = compile_source(
            html.encode("utf-8"),
            source_name="product.html",
            declared_media_type="text/html",
        )
        return project_document(
            compiled.document,
            release_id="release",
            tenant_id="tenant",
            knowledge_base_id="kb",
            source_document_id=source_document_id,
            source_version_id="version",
            source_type="html",
            external_file_id="librechat-file",
            category_path=("Products", "Purification"),
            source_metadata=source_metadata,
        )

    def test_grouped_html_table_produces_distinct_rows_and_numeric_facts(self) -> None:
        bundle = self.project(
            """
            <main>
              <h1>B-SAFE</h1>
              <table>
                <tr><th>Property</th><th>Value</th></tr>
                <tr><th colspan="2">B-SAFE 300</th></tr>
                <tr><th>Maximum pressure</th><td>300 bar</td></tr>
                <tr><th colspan="2">B-SAFE ECO</th></tr>
                <tr><th>Maximum pressure</th><td>350 bar</td></tr>
              </table>
            </main>
            """
        )
        rows = [item for item in bundle.evidence if item.coordinate.table_id]
        self.assertEqual(len(rows), 2)
        self.assertIn("B-SAFE 300", rows[0].content)
        self.assertIn("B-SAFE ECO", rows[1].content)
        self.assertEqual(
            {fact.numeric_value for fact in bundle.facts},
            {"300", "350"},
        )
        self.assertTrue(
            all(fact.provenance_evidence_ids for fact in bundle.facts)
        )

    def test_identifier_terms_and_external_authorization_alias_are_preserved(self) -> None:
        bundle = self.project(
            """
            <main><table>
              <tr><th>Model</th><th>Maximum pressure</th></tr>
              <tr><td>I 15.11-11-V</td><td>525 bar</td></tr>
            </table></main>
            """
        )
        self.assertIn(
            "i 15.11-11-v",
            {term.normalized_term for term in bundle.exact_terms},
        )
        self.assertTrue(
            all(
                item.metadata["external_file_id"] == "librechat-file"
                for item in bundle.evidence
            )
        )
        self.assertTrue(bundle.navigation_nodes)

    def test_same_source_projection_is_deterministic(self) -> None:
        html = (
            "<main><p>Model BM 40.</p><table><tr><th>Pressure</th><th>Value</th></tr>"
            "<tr><td>Maximum</td><td>350 bar</td></tr></table></main>"
        )
        left = self.project(html)
        right = self.project(html)
        self.assertEqual(left, right)

    def test_source_native_metadata_is_an_exact_release_projection(self) -> None:
        metadata = {
            "title": "BAUER PureAir Manual",
            "document_number": "N28210-EN",
            "product_tags": ["PURE-AIR", "P 100"],
            "aliases": ["Portable 100", "P-100"],
        }
        bundle = self.project(
            "<main><h1>Native product page</h1><p>Ordinary prose.</p></main>",
            source_metadata=metadata,
        )
        terms = {
            (term.term_type, term.normalized_term)
            for term in bundle.exact_terms
        }
        self.assertTrue(
            {
                ("external_file_id", "librechat-file"),
                ("filename", "product.html"),
                ("filename", "product"),
                ("title", "bauer pureair manual"),
                ("title", "native product page"),
                ("document_number", "n28210-en"),
                ("product_tag", "pure-air"),
                ("product_tag", "p 100"),
                ("alias", "portable 100"),
                ("alias", "p-100"),
            }.issubset(terms)
        )

        metadata_term_evidence = {
            term.evidence_id
            for term in bundle.exact_terms
            if term.term_type != "identifier"
        }
        self.assertEqual(len(metadata_term_evidence), 1)
        representative_id = next(iter(metadata_term_evidence))
        representative_units = [
            unit
            for unit in bundle.search_units
            if unit.evidence.evidence_id == representative_id
            and unit.unit_type != "fact"
        ]
        self.assertEqual(len(representative_units), 1)
        self.assertIn("Portable 100", representative_units[0].exact_terms)

        index = InMemoryEvidenceIndex()
        for unit in bundle.search_units:
            index.add(unit)
        for node in bundle.navigation_nodes:
            index.add_navigation(node)
        results = index.retrieve(
            analyze_query('"Portable 100"'),
            release_id="release",
            authorized_source_ids={"librechat-file"},
        )
        self.assertTrue(results)
        self.assertTrue(all(result.evidence.is_citable for result in results))
        self.assertTrue(
            all(not result.evidence.generated_summary for result in results)
        )
        self.assertEqual(
            index.retrieve(
                analyze_query('"Portable 100"'),
                release_id="release",
                authorized_source_ids={"different-file"},
            ),
            (),
        )
        reordered = self.project(
            "<main><h1>Native product page</h1><p>Ordinary prose.</p></main>",
            source_metadata={
                "aliases": ["P-100", "Portable 100"],
                "product_tags": ["P 100", "PURE-AIR"],
                "document_number": "N28210-EN",
                "title": "BAUER PureAir Manual",
            },
        )
        self.assertEqual(bundle, reordered)

    def test_navigation_descriptions_are_linked_non_citable_search_units(self) -> None:
        bundle = self.project(
            "<main><h1>PureAir</h1><p>Source-backed content.</p></main>",
            source_metadata={
                "document_number": "N28210",
                "aliases": ["Pure Air"],
            },
        )
        units = {
            unit.search_unit_id: unit for unit in bundle.search_units
        }
        links = {
            link.node_id: link.search_unit_id
            for link in bundle.navigation_descriptions
        }

        self.assertEqual(set(links), {
            node.node_id for node in bundle.navigation_nodes
        })
        self.assertEqual(len(set(links.values())), len(links))
        for node in bundle.navigation_nodes:
            unit = units[links[node.node_id]]
            self.assertEqual(unit.unit_type, "navigation_summary")
            self.assertEqual(unit.evidence.content, node.description)
            self.assertFalse(unit.evidence.is_citable)
            self.assertTrue(unit.evidence.generated_summary)
            self.assertEqual(unit.evidence.release_id, "release")
            self.assertEqual(unit.evidence.source_document_id, "source")
            self.assertEqual(
                unit.evidence.metadata["external_file_id"],
                "librechat-file",
            )

        same = self.project(
            "<main><h1>PureAir</h1><p>Source-backed content.</p></main>",
            source_metadata={
                "aliases": ["Pure Air"],
                "document_number": "N28210",
            },
        )
        other_source = self.project(
            "<main><h1>PureAir</h1><p>Source-backed content.</p></main>",
            source_metadata={
                "document_number": "N28210",
                "aliases": ["Pure Air"],
            },
            source_document_id="other-source",
        )
        self.assertEqual(bundle, same)
        self.assertTrue(
            {
                node.node_id for node in bundle.navigation_nodes
            }.isdisjoint(
                node.node_id for node in other_source.navigation_nodes
            )
        )


if __name__ == "__main__":
    unittest.main()
