from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterator, Optional, Tuple
from uuid import uuid4


class AuditrailHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        detail = body[:500] or "(empty response body)"
        super().__init__("HTTP %s: %s" % (status, detail))
        self.status = status
        self.body = body


def _transport_error(url: str, exc: urllib.error.URLError) -> "AuditrailHTTPError":
    """Wrap a connection-level failure with an explicit, readable message."""
    return AuditrailHTTPError(0, "cannot connect to %s (%s)" % (url, exc.reason))


@dataclass
class AgentClient:
    base_url: str
    user_id: str
    token: Optional[str] = None
    timeout: float = 60.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def _headers(self, json_body: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-User-Id": self.user_id,
        }
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Dict] = None,
    ) -> Dict:
        url = self._url(path, params)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method.upper(),
            headers=self._headers(json_body=body is not None),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise AuditrailHTTPError(exc.code, raw) from exc
        except urllib.error.URLError as exc:
            raise _transport_error(url, exc) from exc
        if not raw:
            return {}
        return json.loads(raw)

    def request_multipart(
        self,
        path: str,
        fields: Dict[str, str],
        file_field: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> Dict:
        boundary = "----auditrail-%s" % uuid4().hex
        body = _multipart_body(boundary, fields, file_field, filename, content, content_type)
        headers = self._headers(json_body=False)
        headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
        url = self._url(path)
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise AuditrailHTTPError(exc.code, raw) from exc
        except urllib.error.URLError as exc:
            raise _transport_error(url, exc) from exc
        if not raw:
            return {}
        return json.loads(raw)

    def fetch_session(self, session_id: str, include_messages: bool = True) -> Dict:
        params = {"session_id": session_id, "messages": "1" if include_messages else "0"}
        return self.request_json("GET", "/api/session", params=params)

    def create_session(self, title: str = "auditrail smoke test", model: str = "") -> Dict:
        return self.request_json(
            "POST",
            "/api/session/new",
            body={"title": title, "model": model},
        )

    def start_chat(
        self,
        session_id: str,
        message: str,
        model: Optional[str] = None,
        max_turns: int = 20,
        attachments: Optional[list] = None,
        skill: Optional[str] = None,
    ) -> Dict:
        body = {
            "session_id": session_id,
            "message": message,
            "max_turns": max_turns,
        }
        if model:
            body["model"] = model
        if attachments:
            body["attachments"] = attachments
        if skill:
            body["skill"] = skill
        return self.request_json("POST", "/api/chat/start", body=body)

    def upload_file(self, session_id: str, filename: str, content: bytes) -> Dict:
        return self.request_multipart(
            "/api/upload",
            {"session_id": session_id},
            "file",
            filename,
            content,
        )

    def respond_clarify(self, session_id: str, response: str) -> Dict:
        return self.request_json(
            "POST",
            "/api/clarify/respond",
            body={"session_id": session_id, "response": response},
        )

    def respond_approval(self, session_id: str, choice: str, approval_id: Optional[str] = None) -> Dict:
        body = {"session_id": session_id, "choice": choice}
        if approval_id:
            body["approval_id"] = approval_id
        return self.request_json("POST", "/api/approval/respond", body=body)

    def stream_events(self, stream_id: str) -> Iterator[Tuple[str, object]]:
        url = self._url("/api/chat/stream", {"stream_id": stream_id})
        request = urllib.request.Request(url, headers=self._headers(json_body=False))
        event_name = "message"
        data_lines = []
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            payload = "\n".join(data_lines)
                            yield event_name, _parse_sse_data(payload)
                        event_name = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise AuditrailHTTPError(exc.code, raw) from exc
        except urllib.error.URLError as exc:
            raise _transport_error(url, exc) from exc

    def _url(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        url = "%s%s" % (self.base_url, path if path.startswith("/") else "/" + path)
        if params:
            url = "%s?%s" % (url, urllib.parse.urlencode(params))
        return url


def _parse_sse_data(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def _multipart_body(
    boundary: str,
    fields: Dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                ("--%s\r\n" % boundary).encode("utf-8"),
                ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_filename = filename.replace('"', "_")
    chunks.extend(
        [
            ("--%s\r\n" % boundary).encode("utf-8"),
            (
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (file_field, safe_filename)
            ).encode("utf-8"),
            ("Content-Type: %s\r\n\r\n" % content_type).encode("utf-8"),
            content,
            b"\r\n",
            ("--%s--\r\n" % boundary).encode("utf-8"),
        ]
    )
    return b"".join(chunks)
