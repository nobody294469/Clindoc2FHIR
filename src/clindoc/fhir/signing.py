"""Three-Tier Bundle Integrity, Cryptographic JWS Signing, and Transport Byte Binding.

Authority:
  - CLINICALDOC_STEP3_DEF003_IMPLEMENTATION_EXECUTION_PLAN.md (Revision 2.7 §7, §11)
  - HL7 FHIR R4B Bundle.signature / RFC 7515 (JWS Compact Serialization) / RFC 8785 (Canonical JSON)
"""

from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import decimal
import hashlib
import hmac
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.types import (
    PrivateKeyTypes,
    PublicKeyTypes,
)

logger = logging.getLogger(__name__)

AUDIT_PROVENANCE_EXTENSION_URL = "http://clindoc.internal/fhir/StructureDefinition/audit-provenance"


# ─── EXCEPTIONS ───────────────────────────────────────────────────────────────

class BundleSigningError(Exception):
    """Base exception for bundle signing, signature verification, and attestation failures."""


class MissingBundleSignatureError(BundleSigningError):
    """Raised when a FHIR bundle lacks the mandatory Bundle.signature element."""


class MalformedBundleSignatureError(BundleSigningError):
    """Raised when Bundle.signature is structurally invalid, malformed base64url, or has unrecognized JWS header."""


class InvalidBundleSignatureError(BundleSigningError):
    """Raised when cryptographic signature verification against the public key fails."""


class UntrustedSigningKeyError(BundleSigningError):
    """Raised when the key ID is unrecognized, expired, revoked, or untrusted."""


class AuditProvenanceBindingMismatchError(BundleSigningError):
    """Raised when embedded audit provenance extension does not match DB event or JSONL ledger."""


class AttestationEquivalenceMismatchError(BundleSigningError):
    """Raised when recomputed clinical content digest or ledger payload does not match DB state."""


class EHRBundlePayloadTamperedError(BundleSigningError):
    """Raised when transport wire boundary byte digest does not match verified transport_sha256."""


class IncompleteAttestationError(BundleSigningError):
    """Raised when bundle is not fully committed, projected, or checkpoint-sealed."""


class AuditKeyError(BundleSigningError):
    """Base exception for audit and identity attestation key resolution errors."""


class AuditKeyRevokedError(AuditKeyError):
    """Raised when an audit or attestation HMAC key has been explicitly revoked."""


class AuditKeyUnavailableError(AuditKeyError):
    """Raised when an audit or attestation HMAC key is missing, unregistered, or inaccessible."""


class IdentityAttestationError(BundleSigningError):
    """Raised when an identity attestation fails cryptographic or contextual verification."""


# ─── BASE64URL & CANONICAL SERIALIZATION (RFC 8785 / JCS) ─────────────────────

def b64url_encode(data: bytes) -> str:
    """Encode bytes using RFC 7515 Base64url encoding without trailing padding '='."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    """Decode RFC 7515 Base64url encoded string with automatic padding restoration."""
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data)


def rfc8785_format_number(val: float | int) -> str:
    """Format IEEE 754 number strictly according to RFC 8785 §3.2.2.3 and ECMA-262 5.1 §9.8.1."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        # RFC 8785 §3.2.2.3: Numbers are strictly IEEE 754 binary64 values.
        # Python integers that cannot be represented exactly in binary64 without precision loss
        # (e.g. 2**53 + 1, 2**68 + 1) or that overflow binary64 range MUST be rejected.
        try:
            f = float(val)
        except OverflowError as exc:
            raise ValueError(f"RFC 8785 forbids integers exceeding IEEE 754 range: {val}") from exc
        if int(f) != val:
            raise ValueError(
                f"RFC 8785 forbids integers not exactly representable in IEEE 754 binary64: {val}"
            )
        val = f
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError(f"RFC 8785 forbids NaN and Infinity values: {val}")
        # Negative zero and positive zero MUST be serialized as '0'
        if val == 0.0:
            return "0"

        s = repr(val)
        if "e" in s or "E" in s:
            parts = s.lower().split("e")
            mantissa = parts[0]
            exp = int(parts[1])
            # If 10^-6 <= |val| < 10^21, ECMAScript requires full decimal notation
            if -6 <= exp < 21:
                d = decimal.Decimal(s)
                formatted = format(d, "f")
                return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
            else:
                sign = "+" if exp >= 0 else ""
                return f"{mantissa}e{sign}{exp}"
        else:
            if s.endswith(".0"):
                return s[:-2]
            return s

    raise TypeError(f"RFC 8785 unsupported number type: {type(val).__name__}")


def rfc8785_escape_str(s: str) -> str:
    """Escape string strictly according to RFC 8785 §3.2.2.2."""
    out = ['"']
    for ch in s:
        cp = ord(ch)
        # RFC 8785 & Unicode standard: lone surrogates (U+D800 to U+DFFF) are invalid in UTF-8
        if 0xD800 <= cp <= 0xDFFF:
            raise ValueError(f"RFC 8785 forbids lone surrogate code point: U+{cp:04X}")

        if ch == '"':
            out.append(r'\"')
        elif ch == '\\':
            out.append(r'\\')
        elif ch == '\b':
            out.append(r'\b')
        elif ch == '\t':
            out.append(r'\t')
        elif ch == '\n':
            out.append(r'\n')
        elif ch == '\f':
            out.append(r'\f')
        elif ch == '\r':
            out.append(r'\r')
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def serialize_canonical_json(obj: Any) -> bytes:
    """Serialize Python object strictly according to RFC 8785 (JSON Canonicalization Scheme / JCS).

    Normative References:
      - RFC 8785 §3.2.1: White Space (strictly minimal ',' and ':')
      - RFC 8785 §3.2.2.2: Strings (literal UTF-8, minimal escaping, unescaped solidus '/')
      - RFC 8785 §3.2.2.3: Numbers (ECMA-262 / IEEE 754-2008, negative zero -> '0', integer floats without dot)
      - RFC 8785 §3.2.3: Object Keys (sorted by UTF-16 code units)
      - Rejects NaN, Infinity, cyclic references, and non-JSON objects.
    """
    seen_ids: Set[int] = set()

    def _serialize(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return rfc8785_format_number(v)
        if isinstance(v, str):
            return rfc8785_escape_str(v)
        if isinstance(v, (list, tuple)):
            obj_id = id(v)
            if obj_id in seen_ids:
                raise ValueError("Cyclic reference detected in RFC 8785 serialization")
            seen_ids.add(obj_id)
            try:
                items = [_serialize(item) for item in v]
            finally:
                seen_ids.remove(obj_id)
            return "[" + ",".join(items) + "]"
        if isinstance(v, dict):
            obj_id = id(v)
            if obj_id in seen_ids:
                raise ValueError("Cyclic reference detected in RFC 8785 serialization")
            seen_ids.add(obj_id)
            try:
                for k in v.keys():
                    if not isinstance(k, str):
                        raise TypeError(f"RFC 8785 object keys must be strings, got {type(k).__name__}")
                # Sort keys by UTF-16 code unit ordering (RFC 8785 §3.2.3)
                sorted_keys = sorted(v.keys(), key=lambda s: s.encode("utf-16-be"))
                items = [rfc8785_escape_str(k) + ":" + _serialize(v[k]) for k in sorted_keys]
            finally:
                seen_ids.remove(obj_id)
            return "{" + ",".join(items) + "}"

        raise TypeError(f"RFC 8785 unsupported type: {type(v).__name__}")

    return _serialize(obj).encode("utf-8")


def _serialize_canonical_json_legacy(obj: Any) -> bytes:
    """Legacy simplified canonical serializer preserved for backward-compatible verification."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# ─── TIER 1: CLINICAL CONTENT DIGEST ─────────────────────────────────────────

def compute_clinical_content_digest(bundle_dict: dict[str, Any]) -> str:
    """Compute Tier 1 SHA-256 digest of pure clinical bundle content.

    Strictly excludes:
      1. `Bundle.signature` element.
      2. The audit provenance extension (`http://clindoc.internal/fhir/StructureDefinition/audit-provenance`).
    """
    clean_bundle = copy.deepcopy(bundle_dict)
    clean_bundle.pop("signature", None)

    # Filter out audit provenance extension if present at bundle root
    if "extension" in clean_bundle and isinstance(clean_bundle["extension"], list):
        clean_bundle["extension"] = [
            ext for ext in clean_bundle["extension"]
            if not (isinstance(ext, dict) and ext.get("url") == AUDIT_PROVENANCE_EXTENSION_URL)
        ]
        if not clean_bundle["extension"]:
            clean_bundle.pop("extension", None)

    canonical_bytes = serialize_canonical_json(clean_bundle)
    return hashlib.sha256(canonical_bytes).hexdigest()


# ─── TIER 2: AUGMENTED SIGNED BUNDLE ─────────────────────────────────────────

def embed_audit_provenance(
    bundle_dict: dict[str, Any],
    audit_sequence_number: int,
    audit_event_hash: str,
    clinical_content_digest: str,
) -> dict[str, Any]:
    """Augment bundle with immutable audit provenance extension linking to PostgreSQL event."""
    augmented = copy.deepcopy(bundle_dict)
    extensions = list(augmented.get("extension", []))

    # Remove existing provenance extension if present
    extensions = [
        ext for ext in extensions
        if not (isinstance(ext, dict) and ext.get("url") == AUDIT_PROVENANCE_EXTENSION_URL)
    ]

    prov_ext = {
        "url": AUDIT_PROVENANCE_EXTENSION_URL,
        "extension": [
            {"url": "auditSequenceNumber", "valueInteger": int(audit_sequence_number)},
            {"url": "auditEventHash", "valueString": str(audit_event_hash).strip()},
            {"url": "clinicalContentDigest", "valueString": str(clinical_content_digest).strip()},
        ],
    }
    extensions.append(prov_ext)
    augmented["extension"] = extensions
    return augmented


def extract_audit_provenance(bundle_dict: dict[str, Any]) -> Tuple[int, str, str]:
    """Extract (audit_sequence_number, audit_event_hash, clinical_content_digest) from bundle.

    Raises:
        IncompleteAttestationError if extension is missing or invalid.
    """
    extensions = bundle_dict.get("extension")
    if not isinstance(extensions, list):
        raise IncompleteAttestationError("Bundle lacks root extensions; audit provenance missing.")

    for ext in extensions:
        if isinstance(ext, dict) and ext.get("url") == AUDIT_PROVENANCE_EXTENSION_URL:
            sub_exts = ext.get("extension")
            if not isinstance(sub_exts, list):
                break
            seq_num = None
            evt_hash = None
            clin_digest = None
            for s in sub_exts:
                if isinstance(s, dict):
                    if s.get("url") == "auditSequenceNumber":
                        seq_num = s.get("valueInteger")
                    elif s.get("url") == "auditEventHash":
                        evt_hash = s.get("valueString")
                    elif s.get("url") == "clinicalContentDigest":
                        clin_digest = s.get("valueString")

            if isinstance(seq_num, int) and evt_hash and clin_digest:
                return seq_num, str(evt_hash).strip(), str(clin_digest).strip()

    raise IncompleteAttestationError("Bundle lacks valid audit provenance extension.")


def sign_fhir_bundle(
    bundle_dict: dict[str, Any],
    private_key: PrivateKeyTypes,
    key_id: str,
    algorithm: str = "RS256",
    who_reference: str = "Organization/clindoc-authoritative-export",
    when: Optional[datetime] = None,
) -> dict[str, Any]:
    """Sign augmented FHIR Bundle producing standards-compliant Bundle.signature element.

    Standard: RFC 7515 JWS Compact Serialization over RFC 8785 canonical JSON
    representation with signature.data stripped/omitted.
    """
    if algorithm not in ("RS256", "ES256"):
        raise ValueError(f"Unsupported signing algorithm: {algorithm}. Must be RS256 or ES256.")

    signed_bundle = copy.deepcopy(bundle_dict)

    ts = when or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts_iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Construct signature element placeholder without data
    sig_element = {
        "type": [
            {
                "system": "urn:iso-astm:E1762-95:2013",
                "code": "1.2.840.10065.1.12.1.1",
                "display": "Author's Signature",
            }
        ],
        "when": ts_iso,
        "who": {"reference": who_reference},
        "sigFormat": "application/jose",
    }
    signed_bundle["signature"] = sig_element

    # 1. Canonicalize bundle with signature element (excluding data)
    canonical_signing_input = serialize_canonical_json(signed_bundle)

    # 2. Construct JWS Protected Header
    header = {"alg": algorithm, "typ": "JOSE", "kid": str(key_id)}
    header_bytes = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64_header = b64url_encode(header_bytes)
    b64_payload = b64url_encode(canonical_signing_input)

    jws_signing_input = f"{b64_header}.{b64_payload}".encode("ascii")

    # 3. Compute Cryptographic Signature
    if algorithm == "RS256":
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError("RS256 requires rsa.RSAPrivateKey")
        raw_sig = private_key.sign(
            jws_signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    elif algorithm == "ES256":
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise TypeError("ES256 requires ec.EllipticCurvePrivateKey")
        raw_sig = private_key.sign(
            jws_signing_input,
            ec.ECDSA(hashes.SHA256()),
        )

    b64_sig = b64url_encode(raw_sig)
    jws_compact = f"{b64_header}.{b64_payload}.{b64_sig}"

    # Embed data in Bundle.signature
    signed_bundle["signature"]["data"] = jws_compact
    return signed_bundle


def verify_bundle_signature(
    bundle_dict: dict[str, Any],
    trust_store: Dict[str, PublicKeyTypes],
    revoked_key_ids: Optional[Set[str]] = None,
) -> Tuple[bool, str, dict[str, Any]]:
    """Cryptographically verify Bundle.signature against trusted public key store.

    Returns:
        Tuple of (is_valid, key_id, verified_header).
    Raises:
        MissingBundleSignatureError
        MalformedBundleSignatureError
        UntrustedSigningKeyError
        InvalidBundleSignatureError
    """
    if "signature" not in bundle_dict or not isinstance(bundle_dict["signature"], dict):
        raise MissingBundleSignatureError("Bundle lacks required 'signature' element.")

    sig_obj = bundle_dict["signature"]
    jws_data = sig_obj.get("data")
    if not jws_data or not isinstance(jws_data, str):
        raise MissingBundleSignatureError("Bundle.signature lacks 'data' field.")

    parts = jws_data.split(".")
    if len(parts) != 3:
        raise MalformedBundleSignatureError(f"Bundle.signature data is not a valid 3-part JWS: {jws_data[:30]}...")

    b64_header, b64_payload, b64_sig = parts

    # 1. Decode and parse Header
    try:
        header_bytes = b64url_decode(b64_header)
        header = json.loads(header_bytes.decode("utf-8"))
    except Exception as exc:
        raise MalformedBundleSignatureError(f"Malformed JWS protected header: {exc}") from exc

    alg = header.get("alg")
    kid = header.get("kid")

    if alg not in ("RS256", "ES256"):
        raise MalformedBundleSignatureError(f"Insecure or unsupported JWS algorithm: '{alg}'.")
    if not kid or not isinstance(kid, str):
        raise MalformedBundleSignatureError("JWS protected header missing 'kid'.")

    # 2. Check Trust Store & Revocation
    revoked = revoked_key_ids or set()
    if kid in revoked:
        raise UntrustedSigningKeyError(f"Signing key '{kid}' has been revoked.")

    if kid not in trust_store:
        raise UntrustedSigningKeyError(f"Signing key '{kid}' not found in trusted key store.")

    public_key = trust_store[kid]

    # 3. Decode Signature Bytes
    try:
        sig_bytes = b64url_decode(b64_sig)
    except Exception as exc:
        raise MalformedBundleSignatureError(f"Malformed base64url signature data: {exc}") from exc

    # 4. Reconstruct Canonical Signing Input
    bundle_to_verify = copy.deepcopy(bundle_dict)
    # Strip signature.data per FHIR specification
    bundle_to_verify["signature"].pop("data", None)
    canonical_signed_bytes = serialize_canonical_json(bundle_to_verify)

    expected_b64_payload = b64url_encode(canonical_signed_bytes)
    if b64_payload != expected_b64_payload:
        raise InvalidBundleSignatureError("JWS payload does not match canonical bundle content.")

    jws_signing_input = f"{b64_header}.{b64_payload}".encode("ascii")

    # 5. Cryptographic Signature Verification
    try:
        if alg == "RS256":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise UntrustedSigningKeyError("Public key is not an RSAPublicKey for RS256.")
            public_key.verify(
                sig_bytes,
                jws_signing_input,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        elif alg == "ES256":
            if not isinstance(public_key, ec.EllipticCurvePublicKey):
                raise UntrustedSigningKeyError("Public key is not an EllipticCurvePublicKey for ES256.")
            public_key.verify(
                sig_bytes,
                jws_signing_input,
                ec.ECDSA(hashes.SHA256()),
            )
    except InvalidSignature as exc:
        raise InvalidBundleSignatureError("Cryptographic signature verification failed.") from exc

    return True, kid, header


# ─── TIER 3: EXACT TRANSPORT WIRE PAYLOAD BINDING ────────────────────────────

def serialize_transport_payload(bundle_dict: dict[str, Any]) -> Tuple[bytes, str]:
    """Serialize the augmented signed bundle ONCE into an immutable transport bytes buffer.

    Returns:
        Tuple of (exact_payload_bytes, transport_sha256).
    """
    exact_payload_bytes = json.dumps(
        bundle_dict,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    transport_sha256 = hashlib.sha256(exact_payload_bytes).hexdigest()
    return exact_payload_bytes, transport_sha256


def verify_transport_boundary(payload_bytes: bytes, expected_transport_sha256: str) -> None:
    """Verify that raw bytes at physical transport boundary strictly match expected transport_sha256.

    Fails closed with EHRBundlePayloadTamperedError if even 1 byte was altered.
    """
    computed = hashlib.sha256(payload_bytes).hexdigest()
    if computed != expected_transport_sha256:
        raise EHRBundlePayloadTamperedError(
            f"Physical transport boundary payload tampering detected: "
            f"wire SHA-256 '{computed}' != expected '{expected_transport_sha256}'. "
            f"Transmission aborted; 0 corrupted bytes dispatched."
        )


# ─── TIER 4: CRYPTOGRAPHIC IDENTITY ATTESTATION & KEYRING (Phase A) ───────────


class HMACKeyring:
    """Manages active, rotated, and revoked HMAC keys for audit logs and identity attestations."""

    def __init__(
        self,
        active_key_id: Optional[str] = None,
        keys: Optional[Dict[str, bytes]] = None,
        revoked_key_ids: Optional[Set[str]] = None,
    ) -> None:
        self._active_key_id = active_key_id or os.environ.get("CLINDOC_AUDIT_HMAC_KEY_ID", "default").strip()
        self._keys: Dict[str, bytes] = dict(keys or {})
        raw_revoked = os.environ.get("CLINDOC_AUDIT_HMAC_KEY_REVOKED", "")
        env_revoked = {k.strip() for k in raw_revoked.split(",") if k.strip()}
        self._revoked_key_ids: Set[str] = set(revoked_key_ids or set()) | env_revoked

    def get_active_key_id(self) -> str:
        return self._active_key_id

    def get_revoked_key_ids(self) -> Set[str]:
        return set(self._revoked_key_ids)

    def is_revoked(self, key_id: str) -> bool:
        return str(key_id).strip() in self._revoked_key_ids

    def register_key(self, key_id: str, key_bytes: bytes, *, is_active: bool = False) -> None:
        self._keys[str(key_id).strip()] = key_bytes
        if is_active:
            self._active_key_id = str(key_id).strip()

    def revoke_key(self, key_id: str) -> None:
        self._revoked_key_ids.add(str(key_id).strip())

    def get_key(self, key_id: str) -> bytes:
        kid = str(key_id).strip()
        if self.is_revoked(kid):
            raise AuditKeyRevokedError(f"Audit HMAC key '{kid}' has been revoked.")

        if kid in self._keys:
            return self._keys[kid]

        # Check environment variables
        if kid.lower() == "default":
            raw = os.environ.get("CLINDOC_AUDIT_HMAC_KEY_DEFAULT") or os.environ.get("CLINDOC_AUDIT_HMAC_KEY")
        else:
            raw = (
                os.environ.get(f"CLINDOC_AUDIT_HMAC_KEY_{kid.upper()}")
                or os.environ.get(f"CLINDOC_AUDIT_HMAC_KEY_{kid}")
            )

        if not raw or not raw.strip():
            # In dev/test environments without explicit key, provide safe fallback
            profile = os.environ.get("CLINDOC_PROFILE", "dev").strip().lower()
            if profile not in ("production", "prod"):
                return b"clindoc-dev-insecure-hmac-key-32bytes-padding!"
            raise AuditKeyUnavailableError(f"Audit HMAC key for key_id '{kid}' is unavailable or unset.")

        return raw.strip().encode("utf-8")


def create_identity_attestation(
    document_id: Any,
    document_content_hash: str,
    patient_context_id: Any,
    patient_identifier: str,
    patient_identifier_system: str,
    actor_id: Any,
    identity_status: str,
    *,
    evidence_artifact_hash: Optional[str] = None,
    supervisor_actor_id: Optional[Any] = None,
    reason_code: Optional[str] = None,
    keyring: Optional[HMACKeyring] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Create a cryptographically authenticated patient identity attestation record.

    Encodes canonical RFC 8785 JSON bound to document content hash, evidence artifact hash,
    patient context, identifier system, actor ID, supervisor ID, reason code, and timestamp,
    signed via HMAC-SHA256.
    """
    ring = keyring or HMACKeyring()
    key_id = ring.get_active_key_id()
    key_bytes = ring.get_key(key_id)

    ts = timestamp or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts_iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload: dict[str, Any] = {
        "actor_id": str(actor_id),
        "canonicalization": "RFC8785",
        "document_content_hash": str(document_content_hash).strip(),
        "document_id": str(document_id),
        "evidence_artifact_hash": str(evidence_artifact_hash).strip() if evidence_artifact_hash else "",
        "identity_status": str(identity_status).strip(),
        "key_id": str(key_id),
        "patient_context_id": str(patient_context_id),
        "patient_identifier": str(patient_identifier).strip(),
        "patient_identifier_system": str(patient_identifier_system).strip(),
        "reason_code": str(reason_code).strip() if reason_code else "",
        "schema_version": "2.0",
        "supervisor_actor_id": str(supervisor_actor_id) if supervisor_actor_id else "",
        "timestamp": ts_iso,
    }

    canonical_bytes = serialize_canonical_json(payload)
    sig_hex = hmac.new(key_bytes, canonical_bytes, hashlib.sha256).hexdigest()

    return {
        "algorithm": "HMAC-SHA256",
        "canonicalization": "RFC8785",
        "key_id": str(key_id),
        "payload": payload,
        "schema_version": "2.0",
        "signature": sig_hex,
    }


def verify_identity_attestation(
    attestation: dict[str, Any],
    current_document_state: dict[str, Any],
    *,
    keyring: Optional[HMACKeyring] = None,
) -> Tuple[bool, str]:
    """Cryptographically verify an identity attestation and assert binding to current document state.

    Returns:
        Tuple of (True, "VALID") on success.
    Raises:
        AuditKeyRevokedError: If signing key has been revoked.
        AuditKeyUnavailableError: If signing key cannot be found.
        IdentityAttestationError: If signature or contextual bindings fail verification.
    """
    if not isinstance(attestation, dict) or "payload" not in attestation or "signature" not in attestation:
        raise IdentityAttestationError("Attestation is missing 'payload' or 'signature' envelope.")

    alg = attestation.get("algorithm")
    if alg != "HMAC-SHA256":
        raise IdentityAttestationError(f"Unsupported attestation algorithm: '{alg}'. Must be 'HMAC-SHA256'.")

    key_id = attestation.get("key_id") or attestation.get("payload", {}).get("key_id")
    if not key_id:
        raise IdentityAttestationError("Attestation missing key_id.")

    ring = keyring or HMACKeyring()
    key_bytes = ring.get_key(key_id)

    payload = attestation["payload"]
    is_v2 = (
        attestation.get("schema_version") in ("2.0", "2.0.0")
        or attestation.get("canonicalization") == "RFC8785"
        or (isinstance(payload, dict) and payload.get("schema_version") in ("2.0", "2.0.0"))
        or (isinstance(payload, dict) and payload.get("canonicalization") == "RFC8785")
    )

    canonical_bytes = serialize_canonical_json(payload)
    expected_sig = hmac.new(key_bytes, canonical_bytes, hashlib.sha256).hexdigest()

    sig_match = hmac.compare_digest(attestation["signature"], expected_sig)
    if not sig_match:
        if is_v2:
            # Modern v2.0 attestations are bound strictly to RFC 8785 JCS; legacy fallback is prohibited
            raise IdentityAttestationError(
                "Cryptographic HMAC-SHA256 signature verification failed under RFC 8785 canonicalization."
            )

        # Versioned migration fallback: permitted strictly for unversioned historical attestations
        legacy_bytes = _serialize_canonical_json_legacy(payload)
        legacy_sig = hmac.new(key_bytes, legacy_bytes, hashlib.sha256).hexdigest()
        sig_match = hmac.compare_digest(attestation["signature"], legacy_sig)

    if not sig_match:
        raise IdentityAttestationError("Cryptographic HMAC-SHA256 signature verification failed.")

    # Contextual assertions against current document state
    expected_doc_id = str(current_document_state.get("document_id", "")).strip()
    if expected_doc_id and payload.get("document_id") != expected_doc_id:
        raise IdentityAttestationError(
            f"Attestation document_id '{payload.get('document_id')}' != current '{expected_doc_id}'."
        )

    expected_doc_hash = str(current_document_state.get("document_content_hash", "")).strip()
    if expected_doc_hash and payload.get("document_content_hash") != expected_doc_hash:
        raise IdentityAttestationError(
            f"Attestation content hash '{payload.get('document_content_hash')}' != current '{expected_doc_hash}'."
        )

    expected_pat_context = str(current_document_state.get("patient_context_id", "")).strip()
    if expected_pat_context and payload.get("patient_context_id") != expected_pat_context:
        raise IdentityAttestationError(
            f"Attestation patient_context_id '{payload.get('patient_context_id')}' != current '{expected_pat_context}'."
        )

    expected_id_status = str(current_document_state.get("identity_status", "")).strip()
    if expected_id_status and payload.get("identity_status") != expected_id_status:
        raise IdentityAttestationError(
            f"Attestation identity_status '{payload.get('identity_status')}' != current '{expected_id_status}'."
        )

    # Validate Four-Eyes constraints if MISMATCH_ACCEPTED
    if payload.get("identity_status") == "MISMATCH_ACCEPTED":
        supervisor = payload.get("supervisor_actor_id")
        actor = payload.get("actor_id")
        if not supervisor or not str(supervisor).strip():
            raise IdentityAttestationError("MISMATCH_ACCEPTED attestation lacks supervisor_actor_id dual-authorization.")
        if str(supervisor).strip() == str(actor).strip():
            raise IdentityAttestationError("Four-Eyes violation in attestation: supervisor cannot equal primary actor.")
        if not payload.get("reason_code"):
            raise IdentityAttestationError("MISMATCH_ACCEPTED attestation lacks mandatory reason_code.")

    return True, "VALID"
