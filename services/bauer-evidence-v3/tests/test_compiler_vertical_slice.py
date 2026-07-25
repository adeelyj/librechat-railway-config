from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.answering import AnswerService  # noqa: E402
from bauer_evidence_v3.auth import AuthorizationContext  # noqa: E402
from bauer_evidence_v3.compiler import (  # noqa: E402
    InMemoryKnowledgeCompiler,
    SourceDescriptor,
)
from bauer_evidence_v3.generation import ExtractiveModelGateway  # noqa: E402
from bauer_evidence_v3.object_store import MemoryObjectStore  # noqa: E402
from bauer_evidence_v3.releases import InMemoryReleaseRegistry  # noqa: E402
from bauer_evidence_v3.retrieval import InMemoryEvidenceIndex  # noqa: E402


SOURCE = b"""
<main>
  <h1>BM compressor technical data</h1>
  <table>
    <tr><th>Model</th><th>Maximum pressure</th></tr>
    <tr><td>BM 40</td><td>350 bar</td></tr>
  </table>
</main>
"""


class GoldenVerticalSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.releases = InMemoryReleaseRegistry()
        self.index = InMemoryEvidenceIndex()
        self.store = MemoryObjectStore()
        self.compiler = InMemoryKnowledgeCompiler(
            releases=self.releases,
            index=self.index,
            object_store=self.store,
        )
        self.descriptor = SourceDescriptor(
            tenant_id="tenant",
            knowledge_base_id="kb",
            external_file_id="file-bm40",
            source_name="bm40.html",
            declared_media_type="text/html",
            source_type="html",
            category_path=("Compressors", "BM"),
        )

    def build(self, release_id: str):
        self.compiler.create_release(
            tenant_id="tenant",
            knowledge_base_id="kb",
            label=release_id,
            expected_source_count=1,
            release_id=release_id,
        )
        compiled = self.compiler.compile_source(
            release_id=release_id,
            descriptor=self.descriptor,
            content=SOURCE,
        )
        self.compiler.finalize_release(release_id)
        return compiled

    def authorization(self) -> AuthorizationContext:
        return AuthorizationContext(
            tenant_id="tenant",
            knowledge_base_id="kb",
            user_id="user",
            agent_id="agent",
            audience="bauer-evidence-v3",
            issued_at=1,
            expires_at=2,
            authorized_source_ids=("file-bm40",),
        )

    def test_source_to_validated_answer_is_complete_and_citable(self) -> None:
        compiled = self.build("release-a")
        self.releases.activate(
            "release-a",
            actor="test",
            reason="golden vertical slice",
        )
        service = AnswerService(
            releases=self.releases,
            index=self.index,
            model=ExtractiveModelGateway(),
        )
        result = asyncio.run(
            service.answer(
                authorization=self.authorization(),
                question="What is the maximum pressure for BM 40 in the table?",
            )
        )
        self.assertEqual(result.status, "answered")
        self.assertTrue(result.validation.valid)
        self.assertIn("350 bar", result.answer)
        self.assertEqual(result.evidence.citations[0].page_number, 1)
        self.assertTrue(
            self.store.exists(compiled.artifact.source_object.object_key)
        )
        self.assertTrue(
            self.store.exists(compiled.artifact.canonical_object.object_key)
        )
        self.assertTrue(compiled.projections.facts)

    def test_artifact_is_reused_but_release_projection_is_rebuilt(self) -> None:
        first = self.build("release-a")
        second = self.build("release-b")
        self.assertFalse(first.artifact.reused)
        self.assertTrue(second.artifact.reused)
        self.assertEqual(
            first.artifact.artifact_set_id,
            second.artifact.artifact_set_id,
        )
        self.assertEqual(
            {item.evidence.release_id for item in second.projections.search_units},
            {"release-b"},
        )

    def test_activation_and_rollback_change_only_the_active_pointer(self) -> None:
        self.build("release-a")
        self.build("release-b")
        self.releases.activate("release-a", actor="test", reason="first")
        self.releases.activate("release-b", actor="test", reason="candidate")
        self.assertEqual(self.releases.pin_active("kb").release_id, "release-b")
        self.releases.rollback(
            "kb",
            "release-a",
            actor="test",
            reason="rollback exercise",
        )
        self.assertEqual(self.releases.pin_active("kb").release_id, "release-a")


if __name__ == "__main__":
    unittest.main()
