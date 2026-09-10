"""Bounded public HTTP transport for Odoo's native Open Graph extraction."""

import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

MAX_PREVIEW_BYTES = 512 * 1024
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def preview_urls(text):
    """Keep the first three distinct explicit URLs; never guess a scheme."""
    urls = []
    for match in URL_PATTERN.finditer(text or ""):
        url = match.group(0).rstrip(".,;!?")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if len(url) <= 2048 and url not in urls:
            urls.append(url)
        if len(urls) == 3:
            break
    return urls


def public_url_target(url):
    """Validate every resolved address, then pin the connection to one address."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in (80, 443)
            or any(char.isspace() for char in url)
        ):
            raise ValueError("Unsupported preview URL")
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        if not ips or any(
            not address.is_global or address.is_multicast or address.is_reserved
            for address in ips
        ):
            raise ValueError("Preview requires public addresses")
        return parsed, port, str(ips[0])
    except (ValueError, OSError) as error:
        raise requests.RequestException("Preview URL is not available") from error


class PublicPreviewSession:
    """The small requests interface consumed by mail.link.preview.

    Pin DNS, validate redirects, preserve TLS hostname verification and bound
    the decoded body. No environment proxies, cookies or credentials are used.
    """

    def __init__(self):
        self.deadline = time.monotonic() + 12
        self.last_url = ""

    def head(self, url, **kwargs):
        return self._request("HEAD", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def _request(self, method, url, **_kwargs):
        for _redirect in range(4):
            parsed, port, address = public_url_target(url)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("Preview deadline exceeded")
            options = {"port": port, "maxsize": 1}
            if parsed.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(
                    address,
                    server_hostname=parsed.hostname,
                    assert_hostname=parsed.hostname,
                    cert_reqs="CERT_REQUIRED",
                    **options,
                )
            else:
                pool = urllib3.HTTPConnectionPool(address, **options)
            response = None
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                response = pool.urlopen(
                    method,
                    path,
                    headers={
                        "Host": parsed.netloc,
                        "User-Agent": "Mozilla/5.0 (compatible; OdooLinkPreview)",
                        "Accept": "text/html,image/*;q=0.8",
                        "Accept-Encoding": "identity",
                    },
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    timeout=urllib3.Timeout(
                        connect=min(3, remaining), read=min(3, remaining)
                    ),
                )
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                result = requests.Response()
                self.last_url = url
                result.status_code = response.status
                result.headers.update(response.headers)
                result.url = url
                result.encoding = requests.utils.get_encoding_from_headers(
                    result.headers
                )
                chunks = []
                size = 0
                if method != "HEAD":
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise requests.RequestException(
                            "Compressed preview bodies are not supported"
                        )
                    for chunk in response.stream(64 * 1024, decode_content=False):
                        size += len(chunk)
                        if size > MAX_PREVIEW_BYTES or time.monotonic() > self.deadline:
                            raise requests.RequestException(
                                "Preview response exceeds limits"
                            )
                        chunks.append(chunk)
                result._content = b"".join(chunks)  # pylint: disable=protected-access
                return result
            except (urllib3.exceptions.HTTPError, OSError) as error:
                raise requests.RequestException("Preview request failed") from error
            finally:
                if response is not None:
                    response.close()
                pool.close()
        raise requests.TooManyRedirects("Preview redirect limit exceeded")
