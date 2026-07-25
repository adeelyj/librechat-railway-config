from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.auth import (  # noqa: E402
    AuthorizationContextNotYetValid,
    ExpiredAuthorizationContext,
    InvalidAuthorizationContext,
    sign_authorization_context,
    verify_authorization_context,
)


KEYRING = {
    "old": b"old-authorization-key-material-0001",
    "current": b"current-authorization-key-material-1",
}
BASE_CLAIMS = {
    "tenant_id": "tenant-bauer",
    "knowledge_base_id": "kb-bauer-public",
    "user_id": "user-123",
    "agent_id": "agent-v3",
    "audience": "bauer-evidence-v3",
    "issued_at": 1_000,
    "expires_at": 1_300,
    "authorized_source_ids": ["source-b", "source-a"],
}


def encode_json(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class AuthorizationContextTests(unittest.TestCase):
    def sign(self, claims=None, **kwargs):
        return sign_authorization_context(
            BASE_CLAIMS if claims is None else claims,
            key_id=kwargs.pop("key_id", "current"),
            keyring=kwargs.pop("keyring", KEYRING),
            **kwargs,
        )

    def verify(self, token, **kwargs):
        return verify_authorization_context(
            token,
            keyring=kwargs.pop("keyring", KEYRING),
            audience=kwargs.pop("audience", "bauer-evidence-v3"),
            now=kwargs.pop("now", 1_100),
            **kwargs,
        )

    def test_roundtrip_is_canonical_and_supports_key_rotation(self):
        token = self.sign()
        differently_ordered = dict(reversed(list(BASE_CLAIMS.items())))
        self.assertEqual(token, self.sign(differently_ordered))

        context = self.verify(token)
        self.assertEqual(context.tenant_id, "tenant-bauer")
        self.assertEqual(context.user_id, "user-123")
        self.assertEqual(context.authorized_source_ids, ("source-a", "source-b"))

        old_token = self.sign(key_id="old")
        self.assertEqual(self.verify(old_token).agent_id, "agent-v3")

    def test_payload_tampering_fails_signature_verification(self):
        token = self.sign()
        header, payload, signature = token.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        claims["user_id"] = "attacker"
        tampered = f"{header}.{encode_json(claims)}.{signature}"

        with self.assertRaisesRegex(InvalidAuthorizationContext, "signature"):
            self.verify(tampered)

    def test_expired_token_is_rejected(self):
        token = self.sign()
        with self.assertRaises(ExpiredAuthorizationContext):
            self.verify(token, now=1_300)

    def test_future_issue_and_explicit_not_before_are_rejected(self):
        token = self.sign()
        with self.assertRaises(AuthorizationContextNotYetValid):
            self.verify(token, now=999)

        claims = {**BASE_CLAIMS, "not_before": 1_200}
        not_before_token = self.sign(claims)
        with self.assertRaises(AuthorizationContextNotYetValid):
            self.verify(not_before_token, now=1_199)
        self.assertEqual(self.verify(not_before_token, now=1_200).not_before, 1_200)

    def test_wrong_audience_is_rejected(self):
        token = self.sign()
        with self.assertRaisesRegex(InvalidAuthorizationContext, "audience"):
            self.verify(token, audience="some-other-service")

    def test_missing_or_wrong_key_is_rejected(self):
        token = self.sign()
        with self.assertRaisesRegex(InvalidAuthorizationContext, "key ID"):
            self.verify(token, keyring={"old": KEYRING["old"]})
        with self.assertRaisesRegex(InvalidAuthorizationContext, "signature"):
            self.verify(
                token,
                keyring={"current": b"wrong-authorization-key-material-01"},
            )

    def test_oversized_source_scope_is_rejected_at_issue_and_verify(self):
        claims = {
            **BASE_CLAIMS,
            "authorized_source_ids": ["source-a", "source-b", "source-c"],
        }
        with self.assertRaisesRegex(InvalidAuthorizationContext, "too many"):
            self.sign(claims, max_authorized_sources=2)

        token = self.sign(claims, max_authorized_sources=3)
        with self.assertRaisesRegex(InvalidAuthorizationContext, "too many"):
            self.verify(token, max_authorized_sources=2)

    def test_every_required_claim_is_fail_closed(self):
        for claim in BASE_CLAIMS:
            with self.subTest(claim=claim):
                incomplete = dict(BASE_CLAIMS)
                incomplete.pop(claim)
                with self.assertRaisesRegex(
                    InvalidAuthorizationContext,
                    "missing required claims",
                ):
                    self.sign(incomplete)

        for invalid_user in ("", "   ", None):
            with self.subTest(invalid_user=invalid_user):
                with self.assertRaises(InvalidAuthorizationContext):
                    self.sign({**BASE_CLAIMS, "user_id": invalid_user})

    def test_ttl_is_bounded_during_issue_and_verification(self):
        long_lived = {**BASE_CLAIMS, "expires_at": 1_301}
        with self.assertRaisesRegex(InvalidAuthorizationContext, "maximum TTL"):
            self.sign(long_lived)

        token = self.sign(long_lived, max_ttl_seconds=301)
        with self.assertRaisesRegex(InvalidAuthorizationContext, "maximum TTL"):
            self.verify(token)


if __name__ == "__main__":
    unittest.main()
