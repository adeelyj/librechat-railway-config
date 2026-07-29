import unittest

from services.rag_api_custom_import import load_batch_authorization


batch_authorization = load_batch_authorization()


class FakeDocument:
    def __init__(self, user_id):
        self.metadata = {} if user_id is None else {"user_id": user_id}


class BatchAuthorizationTests(unittest.TestCase):
    def test_keeps_matching_and_legacy_public_documents(self):
        documents = [
            (FakeDocument("agent-1"), 0.1),
            (FakeDocument(None), 0.2),
            (FakeDocument("agent-2"), 0.01),
        ]
        allowed, denied = batch_authorization.filter_authorized_documents(
            documents, "agent-1"
        )
        self.assertEqual(len(allowed), 2)
        self.assertEqual(denied, 1)


if __name__ == "__main__":
    unittest.main()
