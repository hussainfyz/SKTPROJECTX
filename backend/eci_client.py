"""
Reusable client for the ECI Voters portal's published electoral-roll endpoints.

Endpoints identified in the uploaded ECI main.js bundle:
  GET  /api/v1/printing-publish/get-publish-eroll-type
  POST /api/v1/printing-publish/get-publish-part-list
  POST /api/v1/printing-publish/generate-published-pdfs
  POST /api/v1/printing-publish/get-ac-languages
  POST /api/v1/printing-publish/download-statutory-report

Install:
    python -m pip install requests cryptography

Usage:
    from eci_client import ECIClient

    client = ECIClient()
    result = client.get_publish_year_types(
        state_code="S04",
        year="2025",
        mis_key="YOUR_MIS_KEY",
    )
    print(result)

    # The POST body schemas are intentionally caller-supplied:
    parts = client.get_publish_part_list({"your": "payload"})
    print(parts)

This client does not bypass CAPTCHA, login, authorization, rate limits, or
other server-side access controls. Use only data and access you are authorized
to retrieve. ECI may change its private web-app API at any time.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


LOG = logging.getLogger("eci_client")

# Public RSA SPKI key embedded in the supplied ECI main.js bundle.
# This is a public key, not a secret or a login credential.
ECI_RSA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArb7++BxL/YN8OIln+6FL9Gnw"
    "5DNmQ/VFZXss+J+TuQyJc891JbqbijxYQNEin2c2u+CnpXpoGQ/1gUSzDMJeNS3sNSl"
    "IUykp2dt7xIm/cmV4sZ/c769vCxVRosMfRaZJnBAah+m1X26lEhnOo0wpAB9Txr8RIy"
    "Be6h7PiQWykeJeh6UacOBBX28kgkq7+vJhW8HgB38lt32XRocznRYwS9LqR7ZweFmQh"
    "Tr1+EGrqiEKCOCxMYgHR2SQckb96hZ9kWzfzeun4bUO5oXKJciLkiS1IgKieADEvYLgu"
    "129ZIpn1H+8H+8ikNNVETqEDDMtqcQcQmWppJvcWHaXAs+f8QIDAQAB"
)

# Extracted from the ECI frontend bundle: s.U2.slice(15, 59), then Base64-decoded.
# It is used by the CAPTCHA service response transform (AES-GCM).
ECI_CAPTCHA_RESPONSE_KEY_B64 = "e855n97lc4tcPkj7WWsi38yNWpalLBLZzQdkqHWYbZ0="


class ECIClientError(RuntimeError):
    """Base exception for ECI client errors."""


class ECIHTTPError(ECIClientError):
    """HTTP error with response details."""

    def __init__(self, status_code: int, url: str, body: str):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"ECI returned HTTP {status_code} for {url}: {body[:1000]}")


def _js_json_bytes(value: Any) -> bytes:
    """
    Encode a value like JSON.stringify(value) for the simple JSON values used
    by this API. Compact separators avoid Python's default spaces.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64url(data: bytes) -> str:
    """JavaScript helper d(): URL-safe Base64 without '=' padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


@dataclass
class ECIConfig:
    base_url: str = "https://gateway-voters.eci.gov.in"
    content_loader_ext_base_url: str = "https://gateway-vpd.eci.gov.in"
    # Used only for the frontend's special CDN response path (refId == "CDN").
    cdn_base_url: str = "https://voters.eci.gov.in"
    current_role: Optional[str] = None
    timeout: tuple[float, float] = (15.0, 90.0)
    max_retries: int = 2
    backoff_seconds: float = 0.7
    verify_tls: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    # These are configurable because their exact values are deployment/app
    # constants and can change. Supply them if your browser request includes them.
    application_name: Optional[str] = "VSP"
    platform_type: Optional[str] = "ECIWEB"
    channel_id: Optional[str] = "VSP"
    extra_headers: dict[str, str] = field(default_factory=dict)


class ECIClient:
    def __init__(
        self,
        config: Optional[ECIConfig] = None,
        session: Optional[requests.Session] = None,
    ):
        self.config = config or ECIConfig()
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://voters.eci.gov.in",
            "Referer": "https://voters.eci.gov.in/",
        })
        if self.config.application_name:
            self.session.headers["applicationName"] = self.config.application_name
        if self.config.platform_type:
            self.session.headers["PLATFORM-TYPE"] = self.config.platform_type
        if self.config.channel_id:
            self.session.headers["channelidobo"] = self.config.channel_id
        self.session.headers.update(self.config.extra_headers)

        key_der = base64.b64decode(ECI_RSA_PUBLIC_KEY_B64)
        self._public_key = serialization.load_der_public_key(key_der)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ECIClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _encrypt_publish_query(
        self, state_code: Any, year: Any, mis_key: Any
    ) -> tuple[dict[str, str], dict[str, str]]:
        """
        Reproduce the first three encrypted values from the bundle's helper.

        It creates one random 256-bit AES key and one 96-bit IV, encrypts
        JSON.stringify(state), JSON.stringify(year), and JSON.stringify(misKey)
        with AES-GCM, then RSA-OAEP/SHA-256 encrypts the AES key.

        The bundle reuses the same AES-GCM IV for these three values. This is
        reproduced for wire compatibility; do not copy this pattern into
        newly designed cryptographic protocols.
        """
        aes_key = AESGCM.generate_key(bit_length=256)
        iv = __import__("secrets").token_bytes(12)
        aes = AESGCM(aes_key)

        encrypted_state = aes.encrypt(iv, _js_json_bytes(state_code), None)
        encrypted_year = aes.encrypt(iv, _js_json_bytes(year), None)
        encrypted_mis_key = aes.encrypt(iv, _js_json_bytes(mis_key), None)

        encrypted_key = self._public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        headers = {
            "accept_yek": _b64url(encrypted_key),
            "accept_rotcev": _b64url(iv),
        }
        params = {
            "stateCd": _b64url(encrypted_state),
            "year": _b64url(encrypted_year),
            "misKey": _b64url(encrypted_mis_key),
        }
        return headers, params

    def _encrypt_post_payload(self, payload: Mapping[str, Any]) -> dict[str, str]:
        """
        Encrypt a JSON POST body using the ECI frontend's custom baseQuery.

        The frontend generates a fresh AES-256 key and 96-bit IV per request,
        encrypts JSON.stringify(payload) with AES-GCM, then wraps the AES key
        with RSA-OAEP using SHA-256. The wire body is Base64 JSON containing
        encryptedPayload, encryptedKey, and iv.
        """
        import secrets

        aes_key = AESGCM.generate_key(bit_length=256)
        iv = secrets.token_bytes(12)
        ciphertext_and_tag = AESGCM(aes_key).encrypt(
            iv, _js_json_bytes(dict(payload)), None
        )
        encrypted_key = self._public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return {
            "encryptedPayload": base64.b64encode(ciphertext_and_tag).decode("ascii"),
            "encryptedKey": base64.b64encode(encrypted_key).decode("ascii"),
            "iv": base64.b64encode(iv).decode("ascii"),
        }

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        """Perform a binary-safe GET/HTTP request with the configured timeout."""
        response = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            headers=dict(headers or {}),
            timeout=self.config.timeout,
            verify=self.config.verify_tls,
        )
        if response.status_code >= 400:
            raise ECIHTTPError(response.status_code, response.url, response.text)
        return response.content

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        url = self.config.base_url.rstrip("/") + "/" + path.lstrip("/")
        request_headers = dict(headers or {})
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                    timeout=self.config.timeout,
                    verify=self.config.verify_tls,
                )
                if response.status_code >= 400:
                    # Do not retry ordinary client errors. Print diagnostics
                    # before raising so the server's validation response is visible.
                    if response.status_code < 500 or attempt >= self.config.max_retries:
                        print("\n========== ECI HTTP ERROR ==========")
                        print(f"Method: {method.upper()}")
                        print(f"URL: {response.url}")
                        print(f"HTTP status: {response.status_code}")

                        # Mask CAPTCHA text and IDs if this is a PDF-generation call.
                        debug_body = json_body
                        if isinstance(json_body, Mapping):
                            debug_body = dict(json_body)
                            for key in ("captcha", "captchaId", "misKey"):
                                if key in debug_body:
                                    debug_body[key] = "<redacted>"
                        print("Request JSON:")
                        try:
                            print(json.dumps(debug_body, indent=2, ensure_ascii=False, default=str))
                        except (TypeError, ValueError):
                            print(repr(debug_body))

                        print("Response headers:")
                        for header_name, header_value in response.headers.items():
                            # Avoid exposing cookies or authentication material.
                            if header_name.lower() in {"set-cookie", "authorization", "proxy-authorization"}:
                                header_value = "<redacted>"
                            print(f"  {header_name}: {header_value}")

                        print("Response body (repr):")
                        print(repr(response.text))
                        if response.text:
                            try:
                                print("Response body (formatted JSON):")
                                print(json.dumps(response.json(), indent=2, ensure_ascii=False))
                            except ValueError:
                                pass
                        print("====================================\n")

                        raise ECIHTTPError(
                            response.status_code, response.url, response.text
                        )
                    LOG.warning("ECI server error %s; retry %s",
                                response.status_code, attempt + 1)
                else:
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        return response.text
            except ECIHTTPError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise ECIClientError(f"Request failed: {exc}") from exc

            time.sleep(self.config.backoff_seconds * (2 ** attempt))

        raise ECIClientError(f"Request failed: {last_error}")

    def _decrypt_captcha_response(self, encrypted_value: str) -> dict[str, Any]:
        """
        Decrypt CAPTCHA service response exactly as the frontend transform does.

        The bundle Base64-decodes the response, uses bytes 0:12 as the
        AES-GCM IV, and decrypts the remaining ciphertext+tag with the
        32-byte key extracted from its U2 constant.
        """
        try:
            packed = base64.b64decode(encrypted_value, validate=True)
            if len(packed) <= 12 + 16:
                raise ValueError("Encrypted CAPTCHA response is too short")
            iv, ciphertext_and_tag = packed[:12], packed[12:]
            key = base64.b64decode(ECI_CAPTCHA_RESPONSE_KEY_B64)
            clear = AESGCM(key).decrypt(iv, ciphertext_and_tag, None)
            decoded = json.loads(clear.decode("utf-8"))
        except Exception as exc:
            raise ECIClientError(
                "Could not decrypt CAPTCHA response. The ECI frontend may "
                "have changed its response format or encryption key."
            ) from exc

        if not isinstance(decoded, dict):
            raise ECIClientError(
                f"Unexpected decrypted CAPTCHA response type: {type(decoded).__name__}"
            )
        return decoded

    def get_captcha(self, captcha_type: str = "EROLL") -> dict[str, Any]:
        """
        Request a fresh ECI CAPTCHA and decrypt its response.

        Returns the decrypted object, expected to contain:
            {"id": "...", "captcha": "<base64 image>", ...}
        """
        if not captcha_type or "/" in captcha_type:
            raise ValueError("captcha_type must be a non-empty path value")

        captcha_headers = {
            "appName": self.config.application_name or "VSP",
            "applicationName": self.config.application_name or "VSP",
            "PLATFORM-TYPE": self.config.platform_type or "ECIWEB",
            "channelidobo": self.config.channel_id or "VSP",
        }
        response = self._request(
            "GET",
            f"/api/v1/captcha-service/getCaptcha/{captcha_type}",
            headers=captcha_headers,
        )

        if not isinstance(response, dict) or "data" not in response:
            raise ECIClientError(
                f"Unexpected CAPTCHA service response envelope: {response!r}"
            )

        encrypted_data = response["data"]
        if isinstance(encrypted_data, dict):
            # Some gateway deployments may already expose transformed data.
            captcha = encrypted_data
        elif isinstance(encrypted_data, str):
            captcha = self._decrypt_captcha_response(encrypted_data)
        else:
            raise ECIClientError(
                f"Unexpected CAPTCHA data type: {type(encrypted_data).__name__}"
            )

        if not captcha.get("id") or not captcha.get("captcha"):
            raise ECIClientError(
                f"Decrypted CAPTCHA response is missing id/captcha fields: "
                f"{list(captcha.keys())}"
            )
        return captcha

    @staticmethod
    def save_captcha_image(captcha: Mapping[str, Any],
                           output_path: str = "eci_captcha.jpg") -> str:
        """Decode the CAPTCHA's Base64 image and save it locally."""
        from pathlib import Path

        encoded = captcha.get("captcha")
        if not isinstance(encoded, str) or not encoded:
            raise ECIClientError("CAPTCHA object has no Base64 'captcha' image")
        # Be tolerant if a data-URL prefix is present.
        if "," in encoded and encoded.lstrip().lower().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(encoded)
        except Exception as exc:
            raise ECIClientError("CAPTCHA image is not valid Base64") from exc

        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)
        return str(destination)

    def prompt_captcha(self, output_path: str = "eci_captcha.jpg") -> tuple[str, str]:
        """
        Fetch CAPTCHA, save/show its image, and ask the user to type it.

        Returns (captcha_id, captcha_text). This is manual user input, not
        automated CAPTCHA solving.
        """
        captcha = self.get_captcha()
        image_path = self.save_captcha_image(captcha, output_path)
        try:
            from PIL import Image
            Image.open(image_path).show()
        except ImportError:
            LOG.info("Install Pillow to open CAPTCHA automatically: pip install pillow")
            print(f"CAPTCHA image saved to: {image_path}")

        captcha_text = input("Enter the CAPTCHA text shown in the image: ").strip()
        if not captcha_text:
            raise ECIClientError("CAPTCHA text cannot be empty")
        return str(captcha["id"]), captcha_text



    def get_districts(self, state_code: str) -> Any:
        """Return ECI districts for a state, including districtCd/districtValue."""
        if not state_code:
            raise ValueError("state_code is required")
        return self._request("GET", f"/api/v1/common/districts/{state_code}")

    def get_assemblies(self, district_code: str) -> Any:
        """Return ECI assembly constituencies for a district."""
        if not district_code:
            raise ValueError("district_code is required")
        return self._request("GET", f"/api/v1/common/acs/{district_code}")

    def get_publish_year_types(
        self, state_code: str, year: Any, mis_key: Any
    ) -> Any:
        """Fetch published electoral-roll type options for a state/year."""
        encrypted_headers, encrypted_params = self._encrypt_publish_query(
            state_code, year, mis_key
        )
        return self._request(
            "GET",
            "/api/v1/printing-publish/get-publish-eroll-type",
            params=encrypted_params,
            headers=encrypted_headers,
        )

    def get_publish_part_list(self, payload: Mapping[str, Any]) -> Any:
        """Fetch the published part list using the frontend's encrypted POST."""
        encrypted_body = self._encrypt_post_payload(payload)
        return self._request(
            "POST",
            "/api/v1/printing-publish/get-publish-part-list",
            json_body=encrypted_body,
            headers={"Content-Type": "application/json"},
        )

    def generate_published_pdfs(self, payload: Mapping[str, Any]) -> Any:
        """Request published PDF generation using the frontend's encrypted POST."""
        encrypted_body = self._encrypt_post_payload(payload)
        return self._request(
            "POST",
            "/api/v1/printing-publish/generate-published-pdfs",
            json_body=encrypted_body,
            headers={"Content-Type": "application/json"},
        )


    @staticmethod
    def _safe_filename(name: str, fallback: str = "published_roll.pdf") -> str:
        """Keep a server-provided filename but prevent directory traversal."""
        from pathlib import PurePosixPath, PureWindowsPath

        clean = str(name or "").replace("\\", "/")
        filename = PurePosixPath(clean).name
        filename = PureWindowsPath(filename).name
        if not filename or filename in {".", ".."}:
            filename = fallback
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        return filename

    def download_cdn_pdf_path(
        self, remote_path: str, output_dir: str = "eci_downloads"
    ) -> str:
        """Download one CDN path returned in a top-level refId='CDN' response."""
        from pathlib import Path
        from urllib.parse import quote

        clean_path = str(remote_path).strip().lstrip("/")
        if not clean_path or ".." in Path(clean_path).parts:
            raise ECIClientError(f"Invalid CDN PDF path: {remote_path!r}")

        # This host is taken from the frontend constant used by fetch(.../eroll/...).
        url = self.config.cdn_base_url.rstrip("/") + "/eroll/" + quote(clean_path, safe="/")
        content = self._request_bytes("GET", url)
        if not content.startswith(b"%PDF"):
            raise ECIClientError(
                f"CDN response for {clean_path!r} is not a PDF; "
                f"first bytes: {content[:80]!r}"
            )

        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        filename = self._safe_filename(clean_path)
        output_path = destination / filename
        output_path.write_bytes(content)
        LOG.info("Saved %s (%d bytes)", output_path, len(content))
        return str(output_path)

    def download_generated_pdf(
        self,
        item: Any,
        output_dir: str = "eci_downloads",
        *,
        content_loader_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        """Resolve an ECI fileId or response object and save its PDF bytes."""
        import base64
        from pathlib import Path

        # The portal's normal response is payload=[fileId, ...] with a null
        # top-level refId. It GETs each fileId from the content-loader service.
        if isinstance(item, str):
            file_id = item.strip()
            if not file_id:
                raise ECIClientError("PDF response contains an empty fileId")
            url = (
                self.config.content_loader_ext_base_url.rstrip("/")
                + "/api/v1/ext-printing-publish/get-published-file"
            )
            headers = {
                "Accept": "application/json",
                "applicationName": self.config.application_name or "VSP",
            }
            if self.config.current_role:
                headers["CurrentRole"] = self.config.current_role
            if self.config.channel_id:
                headers["channelidobo"] = self.config.channel_id
            if self.config.platform_type:
                headers["PLATFORM-TYPE"] = self.config.platform_type
            headers.update(self.config.extra_headers)
            if content_loader_headers:
                headers.update(content_loader_headers)

            raw = self._request_bytes(
                "GET", url, params={"fileId": file_id}, headers=headers
            )
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ECIClientError(
                    "The published-file endpoint did not return JSON"
                ) from exc
            if not isinstance(data, dict):
                raise ECIClientError(
                    f"Unexpected published-file response for fileId={file_id!r}: "
                    f"{str(data)[:500]}"
                )
            encoded_pdf = data.get("payload")
            response_filename = data.get("refId") or f"{file_id}.pdf"
            if not encoded_pdf and isinstance(data.get("data"), dict):
                encoded_pdf = data["data"].get("payload")
                response_filename = data["data"].get("refId") or response_filename
            if not encoded_pdf:
                raise ECIClientError(
                    f"No Base64 PDF payload returned for fileId={file_id!r}: "
                    f"{str(data)[:500]}"
                )
            try:
                content = base64.b64decode(encoded_pdf, validate=False)
            except (ValueError, TypeError) as exc:
                raise ECIClientError(
                    f"Could not Base64-decode PDF response for fileId={file_id!r}"
                ) from exc
            filename = self._safe_filename(response_filename)

        elif isinstance(item, Mapping):
            ref_id = str(item.get("refId") or "").strip()
            if not ref_id:
                raise ECIClientError(f"PDF response item has no refId: {item!r}")
            if ref_id.upper() == "CDN":
                remote_path = str(item.get("payload") or "").strip()
                if not remote_path:
                    raise ECIClientError("CDN PDF item has an empty payload/path")
                url = (
                    self.config.cdn_base_url.rstrip("/")
                    + "/eroll/"
                    + remote_path.lstrip("/")
                )
                content = self._request_bytes("GET", url)
                filename = self._safe_filename(remote_path)
            else:
                file_id = str(item.get("payload") or "").strip()
                if not file_id:
                    raise ECIClientError(
                        f"PDF response item {ref_id!r} has no payload/fileId"
                    )
                url = (
                    self.config.content_loader_ext_base_url.rstrip("/")
                    + "/api/v1/ext-printing-publish/get-published-file"
                )
                headers = {
                    "Accept": "application/json",
                    "applicationName": self.config.application_name or "VSP",
                }
                if self.config.current_role:
                    headers["CurrentRole"] = self.config.current_role
                if self.config.channel_id:
                    headers["channelidobo"] = self.config.channel_id
                if self.config.platform_type:
                    headers["PLATFORM-TYPE"] = self.config.platform_type
                headers.update(self.config.extra_headers)
                if content_loader_headers:
                    headers.update(content_loader_headers)
                raw = self._request_bytes(
                    "GET", url, params={"fileId": file_id}, headers=headers
                )
                try:
                    data = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ECIClientError(
                        "The published-file endpoint did not return JSON"
                    ) from exc
                encoded_pdf = data.get("payload") if isinstance(data, dict) else None
                if not encoded_pdf and isinstance(data, dict) and isinstance(data.get("data"), dict):
                    encoded_pdf = data["data"].get("payload")
                if not encoded_pdf:
                    raise ECIClientError(
                        f"No Base64 PDF payload returned for fileId={file_id!r}: "
                        f"{str(data)[:500]}"
                    )
                try:
                    content = base64.b64decode(encoded_pdf, validate=False)
                except (ValueError, TypeError) as exc:
                    raise ECIClientError(
                        f"Could not Base64-decode PDF response for {ref_id!r}"
                    ) from exc
                filename = self._safe_filename(ref_id)
        else:
            raise ECIClientError(
                f"Unexpected PDF response item type: {type(item).__name__}"
            )

        if not content.startswith(b"%PDF"):
            raise ECIClientError(
                f"Downloaded content for {filename!r} does not look like a PDF "
                f"(first bytes: {content[:80]!r})"
            )
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / filename
        output_path.write_bytes(content)
        LOG.info("Saved %s (%d bytes)", output_path, len(content))
        return str(output_path)

    def download_published_pdfs(
        self,
        payload: Mapping[str, Any],
        output_dir: str = "eci_downloads",
        *,
        content_loader_headers: Optional[Mapping[str, str]] = None,
        on_file_downloaded=None,
    ) -> list[str]:
        """
        Generate published PDFs, then download every file in the response.

        `payload` must include the current CAPTCHA text and CAPTCHA id. The
        method does not solve, fetch, or bypass CAPTCHA.
        """
        result = self.generate_published_pdfs(payload)
        if not isinstance(result, dict):
            raise ECIClientError(
                f"Unexpected PDF-generation response: {str(result)[:1000]}"
            )
        if result.get("statusCode") != 200:
            raise ECIClientError(
                f"PDF generation failed: {result.get('message', result)}"
            )

        items = result.get("payload") or []
        if not isinstance(items, list) or not items:
            raise ECIClientError(
                f"No PDFs were returned: {result.get('message', result)}"
            )

        # The ECI frontend's CDN response is a special envelope:
        # result.refId == "CDN" and result.payload is a list of path strings.
        # It fetches each path from https://voters.eci.gov.in/eroll/{path}.
        response_ref_id = str(result.get("refId") or "").strip().upper()
        saved_paths: list[str] = []
        errors: list[str] = []
        for index, item in enumerate(items, start=1):
            try:
                if response_ref_id == "CDN":
                    if not isinstance(item, str) or not item.strip():
                        raise ECIClientError(
                            f"CDN item must be a non-empty path string; got {item!r}"
                        )
                    saved_path = self.download_cdn_pdf_path(item, output_dir)
                    if on_file_downloaded:
                        saved_path = on_file_downloaded(index - 1, saved_path)
                    saved_paths.append(saved_path)
                elif isinstance(item, (dict, str)):
                    saved_path = self.download_generated_pdf(
                        item,
                        output_dir,
                        content_loader_headers=content_loader_headers,
                    )
                    if on_file_downloaded:
                        saved_path = on_file_downloaded(index - 1, saved_path)
                    saved_paths.append(saved_path)
                else:
                    raise ECIClientError(
                        f"Unexpected response item type {type(item).__name__}; "
                        f"top-level refId={result.get('refId')!r}"
                    )
            except Exception as exc:
                errors.append(f"Item {index}: {exc}")

        if errors:
            raise ECIClientError(
                "Some PDFs could not be downloaded. "
                f"Saved {len(saved_paths)} file(s). Errors: " + " | ".join(errors)
            )
        return saved_paths

    def get_ac_languages(self, payload: Mapping[str, Any]) -> Any:
        """Fetch language options for an assembly (payload schema is caller-owned)."""
        return self._request(
            "POST",
            "/api/v1/printing-publish/get-ac-languages",
            json_body=dict(payload),
        )

    def download_statutory_report(self, payload: Mapping[str, Any]) -> Any:
        """Call the statutory-report endpoint; response may be JSON or text."""
        return self._request(
            "POST",
            "/api/v1/printing-publish/download-statutory-report",
            json_body=dict(payload),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import argparse

    parser = argparse.ArgumentParser(
        description="Call ECI published electoral-roll endpoints."
    )
    parser.add_argument("--state", required=True, help="ECI state code, e.g. S04")
    parser.add_argument("--year", required=True, help="Roll year")
    parser.add_argument("--mis-key", required=True, help="misKey supplied by ECI")
    args = parser.parse_args()

    with ECIClient() as client:
        print(json.dumps(
            client.get_publish_year_types(args.state, args.year, args.mis_key),
            ensure_ascii=False,
            indent=2,
        ))
