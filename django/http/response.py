import datetime
import io
import json
import mimetypes
import os
import re
import sys
import time
import warnings
from contextlib import aclosing
from email.header import Header
from http.client import responses
from typing import (
    Any, AsyncIterable, AsyncIterator, Callable, Dict, Iterable,
    Iterator, List, Mapping, Optional, Tuple, Type, Union
)
from urllib.parse import urlsplit

from asgiref.sync import async_to_sync, sync_to_async

from django.conf import settings
from django.core import signals, signing
from django.core.exceptions import DisallowedRedirect
from django.core.serializers.json import DjangoJSONEncoder
from django.http.cookie import SimpleCookie
from django.utils import timezone
from django.utils.asyncio import maybe_aclosing
from django.utils.datastructures import CaseInsensitiveMapping
from django.utils.deprecation import RemovedInDjango2029Warning
from django.utils.encoding import iri_to_uri
from django.utils.functional import cached_property
from django.utils.http import (
    MAX_URL_REDIRECT_LENGTH,
    content_disposition_header,
    http_date,
)
from django.utils.regex_helper import _lazy_re_compile
from django.utils.warnings import django_file_prefixes

_charset_from_content_type_re = _lazy_re_compile(
    r";\s*charset=(?P<charset>[^\s;]+)", re.I
)
_control_chars_re = _lazy_re_compile(r"[\x00-\x1f\x7f-\x9f]")

class ResponseHeaders(CaseInsensitiveMapping):
    def __init__(self, data: Optional[Union[Mapping[str, str], Iterable[Tuple[str, str]]]] = None) -> None:
        """
        Populate the initial data using __setitem__ to ensure values are
        correctly encoded.
        """
        self._store: Dict[str, Tuple[str, str]] = {}
        if data:
            for header, value in self._unpack_items(data):
                self[header] = value

    def _convert_to_charset(self, value: Any, charset: str, mime_encode: bool = False) -> str:
        """
        Convert headers key/value to ascii/latin-1 native strings.
        `charset` must be 'ascii' or 'latin-1'. If `mime_encode` is True and
        `value` can't be represented in the given charset, apply MIME-encoding.
        """
        try:
            if isinstance(value, str):
                # Ensure string is valid in given charset
                value.encode(charset)
            elif isinstance(value, bytes):
                # Convert bytestring using given charset
                value = value.decode(charset)
            else:
                value = str(value)
                # Ensure string is valid in given charset.
                value.encode(charset)
            if "\n" in value or "\r" in value:
                raise BadHeaderError(
                    f"Header values can't contain newlines (got {value!r})"
                )
        except UnicodeError as e:
            # Encoding to a string of the specified charset failed, but we
            # don't know what type that value was, or if it contains newlines,
            # which we may need to check for before sending it to be
            # encoded for multiple character sets.
            if (isinstance(value, bytes) and (b"\n" in value or b"\r" in value)) or (
                isinstance(value, str) and ("\n" in value or "\r" in value)
            ):
                raise BadHeaderError(
                    f"Header values can't contain newlines (got {value!r})"
                ) from e
            if mime_encode:
                value = Header(value, "utf-8", maxlinelen=sys.maxsize).encode()
            else:
                e.reason += ", HTTP response headers must be in %s format" % charset
                raise
        return value

    def __delitem__(self, key: str) -> None:
        self.pop(key)

    def __setitem__(self, key: str, value: Any) -> None:
        key = self._convert_to_charset(key, "ascii")
        value = self._convert_to_charset(value, "latin-1", mime_encode=True)
        self._store[key.lower()] = (key, value)

    def pop(self, key: str, default: Any = None) -> Any:
        return self._store.pop(key.lower(), default)

    def setdefault(self, key: str, value: Any) -> None:
        if key not in self:
            self[key] = value

class BadHeaderError(ValueError):
    pass

class HttpResponseBase:
    """
    An HTTP response base class with dictionary-accessed headers.

    This class doesn't handle content. It should not be used directly.
    Use the HttpResponse and StreamingHttpResponse subclasses instead.
    """

    status_code: int = 200

    def __init__(
        self,
        content_type: Optional[str] = None,
        status: Optional[int] = None,
        reason: Optional[str] = None,
        charset: Optional[str] = None,
        headers: Optional[Union[Mapping[str, str], Iterable[Tuple[str, str]]]] = None,
    ) -> None:
        self.headers = ResponseHeaders(headers)
        self._charset = charset
        if "Content-Type" not in self.headers:
            if content_type is None:
                content_type = f"text/html; charset={self.charset}"
            self.headers["Content-Type"] = content_type
        elif content_type:
            raise ValueError(
                "'headers' must not contain 'Content-Type' when the "
                "'content_type' parameter is provided."
            )
        self._resource_closers: List[Callable[[], None]] = []
        # This parameter is set by the handler. It's necessary to preserve the
        # historical behavior of request_finished.
        self._handler_class: Optional[Type[Any]] = None
        self.cookies: SimpleCookie = SimpleCookie()
        self.closed: bool = False
        if status is not None:
            try:
                self.status_code = int(status)
            except (ValueError, TypeError):
                raise TypeError("HTTP status code must be an integer.")

            if not 100 <= self.status_code <= 599:
                raise ValueError("HTTP status code must be an integer from 100 to 599.")
        self._reason_phrase: Optional[str] = None
        self.reason_phrase = reason

    @property
    def reason_phrase(self) -> str:
        if self._reason_phrase is not None:
            return self._reason_phrase
        # Leave self._reason_phrase unset in order to use the default
        # reason phrase for status code.
        return responses.get(self.status_code, "Unknown Status Code")

    @reason_phrase.setter
    def reason_phrase(self, value: Optional[str]) -> None:
        if value and _control_chars_re.search(value):
            raise BadHeaderError("reason_phrase can't contain control characters.")
        self._reason_phrase = value

    @property
    def charset(self) -> str:
        if self._charset is not None:
            return self._charset
        # The Content-Type header may not yet be set, because the charset is
        # being inserted *into* it.
        if content_type := self.headers.get("Content-Type"):
            if matched := _charset_from_content_type_re.search(content_type):
                # Extract the charset and strip its double quotes.
                # Note that having parsed it from the Content-Type, we don't
                # store it back into the _charset for later intentionally, to
                # allow for the Content-Type to be switched again later.
                return matched["charset"].replace('"', "")
        return settings.DEFAULT_CHARSET

    @charset.setter
    def charset(self, value: str) -> None:
        self._charset = value

    def serialize_headers(self) -> bytes:
        """HTTP headers as a bytestring."""
        return b"\r\n".join(
            [
                key.encode("ascii") + b": " + value.encode("latin-1")
                for key, value in self.headers.items()
            ]
        )

    __bytes__ = serialize_headers

    @property
    def _content_type_for_repr(self) -> str:
        return (
            ', "%s"' % self.headers["Content-Type"]
            if "Content-Type" in self.headers
            else ""
        )

    def __setitem__(self, header: str, value: Any) -> None:
        self.headers[header] = value

    def __delitem__(self, header: str) -> None:
        del self.headers[header]

    def __getitem__(self, header: str) -> str:
        return self.headers[header]

    def has_header(self, header: str) -> bool:
        """Case-insensitive check for a header."""
        return header in self.headers

    __contains__ = has_header

    def items(self) -> Iterable[Tuple[str, str]]:
        return self.headers.items()

    def get(self, header: str, alternate: Any = None) -> Any:
        return self.headers.get(header, alternate)

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: Optional[Union[int, float, datetime.timedelta]] = None,
        expires: Optional[Union[str, datetime.datetime]] = None,
        path: str = "/",
        domain: Optional[str] = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Optional[str] = None,
    ) -> None:
        """
        Set a cookie.

        ``expires`` can be:
        - a string in the correct format,
        - a naive ``datetime.datetime`` object in UTC,
        - an aware ``datetime.datetime`` object in any time zone.
        If it is a ``datetime.datetime`` object then calculate ``max_age``.

        ``max_age`` can be:
        - int/float specifying seconds,
        - ``datetime.timedelta`` object.
        """
        self.cookies[key] = value
        if expires is not None:
            if isinstance(expires, datetime.datetime):
                if timezone.is_naive(expires):
                    expires = timezone.make_aware(expires, datetime.UTC)
                delta = expires - datetime.datetime.now(tz=datetime.UTC)
                # Add one second so the date matches exactly (a fraction of
                # time gets lost between converting to a timedelta and
                # then the date string).
                delta += datetime.timedelta(seconds=1)
                # Just set max_age - the max_age logic will set expires.
                expires = None
                if max_age is not None:
                    raise ValueError("'expires' and 'max_age' can't be used together.")
                max_age = max(0, delta.days * 86400 + delta.seconds)
            else:
                self.cookies[key]["expires"] = expires
        else:
            self.cookies[key]["expires"] = ""
        if max_age is not None:
            if isinstance(max_age, datetime.timedelta):
                max_age = max_age.total_seconds()
            self.cookies[key]["max-age"] = int(max_age)
            # IE requires expires, so set it if hasn't been already.
            if not expires:
                self.cookies[key]["expires"] = http_date(time.time() + max_age)
        if path is not None:
            self.cookies[key]["path"] = path
        if domain is not None:
            self.cookies[key]["domain"] = domain
        if secure:
            self.cookies[key]["secure"] = True
        if httponly:
            self.cookies[key]["httponly"] = True
        if samesite:
            if samesite.lower() not in ("lax", "none", "strict"):
                raise ValueError('samesite must be "lax", "none", or "strict".')
            self.cookies[key]["samesite"] = samesite

    def setdefault(self, key: str, value: Any) -> None:
        """Set a header unless it has already been set."""
        self.headers.setdefault(key, value)

    def set_signed_cookie(self, key: str, value: str, salt: str = "", **kwargs: Any) -> None:
        value = signing.get_cookie_signer(
            salt=signing._cookie_signer_salt(key, salt)
        ).sign(value)
        return self.set_cookie(key, value, **kwargs)

    def delete_cookie(
        self, key: str, path: str = "/", domain: Optional[str] = None, samesite: Optional[str] = None
    ) -> None:
        # Browsers can ignore the Set-Cookie header if the cookie doesn't use
        # the secure flag and:
        # - the cookie name starts with "__Host-" or "__Secure-", or
        # - the samesite is "none".
        secure = key.startswith(("__Secure-", "__Host-")) or (
            samesite and samesite.lower() == "none"
        )
        self.set_cookie(
            key,
            max_age=0,
            path=path,
            domain=domain,
            secure=secure,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            samesite=samesite,
        )

    # Common methods used by subclasses
    def make_bytes(self, value: Any) -> bytes:
        """Turn a value into a bytestring encoded in the output charset."""
        # Per PEP 3333, this response body must be bytes. To avoid returning
        # an instance of a subclass, this function returns `bytes(value)`.
        # This doesn't make a copy when `value` already contains bytes.

        # Handle string types -- we can't rely on force_bytes here because:
        # - Python attempts str conversion first
        # - when self._charset != 'utf-8' it re-encodes the content
        if isinstance(value, (bytes, memoryview)):
            return bytes(value)
        if isinstance(value, str):
            return bytes(value.encode(self.charset))
        # Handle non-string types.
        return str(value).encode(self.charset)

    def close(self) -> None:
        for closer in self._resource_closers:
            try:
                closer()
            except Exception:
                pass
        # Free resources that were still referenced.
        self._resource_closers.clear()
        self.closed = True
        signals.request_finished.send(sender=self._handler_class)

    def write(self, content: Any) -> None:
        raise OSError("This %s instance is not writable" % self.__class__.__name__)

    def flush(self) -> None:
        pass

    def tell(self) -> int:
        raise OSError(
            "This %s instance cannot tell its position" % self.__class__.__name__
        )

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def writelines(self, lines: Iterable[Any]) -> None:
        raise OSError("This %s instance is not writable" % self.__class__.__name__)

class HttpResponse(HttpResponseBase):
    """
    An HTTP response class with a string as content.

    This content can be read, appended to, or replaced.
    """

    streaming: bool = False

    def __init__(self, content: Any = b"", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Content is a bytestring. See the `content` property methods.
        self._container: List[bytes] = []
        self.content = content

    def __repr__(self) -> str:
        return "<%(cls)s status_code=%(status_code)d%(content_type)s>" % {
            "cls": self.__class__.__name__,
            "status_code": self.status_code,
            "content_type": self._content_type_for_repr,
        }

    def serialize(self) -> bytes:
        """Full HTTP message, including headers, as a bytestring."""
        return self.serialize_headers() + b"\r\n\r\n" + self.content

    __bytes__ = serialize

    @property
    def content(self) -> bytes:
        return b"".join(self._container)

    @content.setter
    def content(self, value: Any) -> None:
        # Consume iterators upon assignment to allow repeated iteration.
        if hasattr(value, "__iter__") and not isinstance(
            value, (bytes, memoryview, str)
        ):
            content_bytes = b"".join(self.make_bytes(chunk) for chunk in value)
            if hasattr(value, "close"):
                try:
                    value.close()
                except Exception:
                    pass
        else:
            content_bytes = self.make_bytes(value)
        # Create a list of properly encoded bytestrings to support write().
        self._container = [content_bytes]
        self.__dict__.pop("text", None)

    @cached_property
    def text(self) -> str:
        return self.content.decode(self.charset or "utf-8")

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._container)

    def write(self, content: Any) -> None:
        self._container.append(self.make_bytes(content))

    def tell(self) -> int:
        return len(self.content)

    def getvalue(self) -> bytes:
        return self.content

    def writable(self) -> bool:
        return True

    def writelines(self, lines: Iterable[Any]) -> None:
        for line in lines:
            self.write(line)

class StreamingHttpResponse(HttpResponseBase):
    """
    A streaming HTTP response class with an iterator as content.

    This should only be iterated once, when the response is streamed to the
    client. However, it can be appended to or replaced with a new iterator
    that wraps the original content (or yields entirely new content).
    """

    streaming: bool = True

    def __init__(self, streaming_content: Union[Iterable[Any], AsyncIterable[Any]] = (), *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # `streaming_content` should be an iterable of bytestrings.
        # See the `streaming_content` property methods.
        self.streaming_content = streaming_content

    def __repr__(self) -> str:
        return "<%(cls)s status_code=%(status_code)d%(content_type)s>" % {
            "cls": self.__class__.__qualname__,
            "status_code": self.status_code,
            "content_type": self._content_type_for_repr,
        }

    @property
    def content(self) -> Any:
        raise AttributeError(
            "This %s instance has no `content` attribute. Use "
            "`streaming_content` instead." % self.__class__.__name__
        )

    @property
    def text(self) -> str:
        raise AttributeError(
            "This %s instance has no `text` attribute." % self.__class__.__name__
        )

    @property
    def streaming_content(self) -> Union[Iterator[bytes], AsyncIterator[bytes]]:
        if self.is_async:
            # pull to lexical scope to capture fixed reference in case
            # streaming_content is set again later.
            _iterator = self._iterator

            async def awrapper() -> AsyncIterator[bytes]:
                async with maybe_aclosing(_iterator) as it:
                    async for part in it:
                        yield self.make_bytes(part)

            return awrapper()
        else:
            return map(self.make_bytes, self._iterator)

    @streaming_content.setter
    def streaming_content(self, value: Union[Iterable[Any], AsyncIterable[Any]]) -> None:
        self._set_streaming_content(value)

    def _set_streaming_content(self, value: Union[Iterable[Any], AsyncIterable[Any]]) -> None:
        # Ensure we can never iterate on "value" more than once.
        try:
            self._iterator = iter(value) # type: ignore
            self.is_async = False
        except TypeError:
            self._iterator = aiter(value) # type: ignore
            self.is_async = True
        if hasattr(value, "close"):
            self._resource_closers.append(value.close)

    def __iter__(self) -> Iterator[bytes]:
        try:
            return iter(self.streaming_content) # type: ignore
        except TypeError:
            warnings.warn(
                "StreamingHttpResponse must consume asynchronous iterators in order to "
                "serve them synchronously. Use a synchronous iterator instead.",
                Warning,
                stacklevel=2,
            )

            # async iterator. Consume in async_to_sync and map back.
            async def to_list(_iterator: AsyncIterator[Any]) -> List[Any]:
                as_list = []
                async with maybe_aclosing(_iterator) as it:
                    async for chunk in it:
                        as_list.append(chunk)
                return as_list

            return map(self.make_bytes, iter(async_to_sync(to_list)(self._iterator))) # type: ignore

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async with aclosing(aiter(self.streaming_content)) as content: # type: ignore
                async for part in content:
                    yield part
        except TypeError:
            warnings.warn(
                "StreamingHttpResponse must consume synchronous iterators in order to "
                "serve them asynchronously. Use an asynchronous iterator instead.",
                Warning,
                stacklevel=2,
            )
            # sync iterator. Consume via sync_to_async and yield via async
            # generator.
            for part in await sync_to_async(list)(self.streaming_content):
                yield part

    def getvalue(self) -> bytes:
        # Assuming the content hasn't been iterated yet and is safe to be consumed.
        # It's an anti-pattern to consume streaming content all at once but provided for tests.
        return b"".join(self)


class FileResponse(StreamingHttpResponse):
    """
    A streaming HTTP response class optimized for files.
    """

    block_size: int = 4096

    def __init__(self, *args: Any, as_attachment: bool = False, filename: str = "", **kwargs: Any) -> None:
        self.as_attachment = as_attachment
        self.filename = filename
        self._no_explicit_content_type = (
            "content_type" not in kwargs or kwargs["content_type"] is None
        )
        super().__init__(*args, **kwargs)

    def _set_streaming_content(self, value: Any) -> None:
        if not hasattr(value, "read"):
            self.file_to_stream = None
            return super()._set_streaming_content(value)

        self.file_to_stream = filelike = value
        if hasattr(filelike, "close"):
            self._resource_closers.append(filelike.close)
        value = iter(lambda: filelike.read(self.block_size), b"")
        self.set_headers(filelike)
        super()._set_streaming_content(value)

    def set_headers(self, filelike: Any) -> None:
        """
        Set some common response headers (Content-Length, Content-Type, and
        Content-Disposition) based on the `filelike` response content.
        """
        filename = getattr(filelike, "name", "")
        filename = filename if isinstance(filename, str) else ""
        seekable = hasattr(filelike, "seek") and (
            not hasattr(filelike, "seekable") or filelike.seekable()
        )
        if hasattr(filelike, "tell"):
            if seekable:
                initial_position = filelike.tell()
                filelike.seek(0, io.SEEK_END)
                self.headers["Content-Length"] = str(filelike.tell() - initial_position)
                filelike.seek(initial_position)
            elif hasattr(filelike, "getbuffer"):
                self.headers["Content-Length"] = str(
                    filelike.getbuffer().nbytes - filelike.tell()
                )
            elif os.path.exists(filename):
                self.headers["Content-Length"] = str(
                    os.path.getsize(filename) - filelike.tell()
                )
        elif seekable:
            self.headers["Content-Length"] = str(sum(
                iter(lambda: len(filelike.read(self.block_size)), 0)
            ))
            filelike.seek(-int(self.headers["Content-Length"]), io.SEEK_END)

        filename = os.path.basename(self.filename or filename)
        if self._no_explicit_content_type:
            if filename:
                content_type, encoding = mimetypes.guess_type(filename)
                # Encoding isn't set to prevent browsers from automatically
                # uncompressing files.
                content_type = {
                    "br": "application/x-brotli",
                    "bzip2": "application/x-bzip",
                    "compress": "application/x-compress",
                    "gzip": "application/gzip",
                    "xz": "application/x-xz",
                }.get(encoding or "", content_type)
                self.headers["Content-Type"] = (
                    content_type or "application/octet-stream"
                )
            else:
                self.headers["Content-Type"] = "application/octet-stream"

        if content_disposition := content_disposition_header(
            self.as_attachment, filename
        ):
            self.headers["Content-Disposition"] = content_disposition

class HttpResponseRedirectBase(HttpResponse):
    allowed_schemes: List[str] = ["http", "https", "ftp"]
    status_code_preserve_request: int

    def __init__(
        self,
        redirect_to: str,
        preserve_request: bool = False,
        *args: Any,
        max_length: Optional[int] = MAX_URL_REDIRECT_LENGTH,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self["Location"] = iri_to_uri(redirect_to)
        if max_length is not None and len(self["Location"]) > max_length:
            raise DisallowedRedirect(
                f"Unsafe redirect exceeding {max_length} characters"
            )
        parsed = urlsplit(str(redirect_to))
        if preserve_request:
            self.status_code = self.status_code_preserve_request
        if parsed.scheme and parsed.scheme not in self.allowed_schemes:
            raise DisallowedRedirect(
                "Unsafe redirect to URL with protocol '%s'" % parsed.scheme
            )

    @property
    def url(self) -> str:
        return self["Location"]

    def __repr__(self) -> str:
        return (
            '<%(cls)s status_code=%(status_code)d%(content_type)s, url="%(url)s">'
            % {
                "cls": self.__class__.__name__,
                "status_code": self.status_code,
                "content_type": self._content_type_for_repr,
                "url": self.url,
            }
        )

class HttpResponseRedirect(HttpResponseRedirectBase):
    status_code = 302
    status_code_preserve_request = 307

class HttpResponsePermanentRedirect(HttpResponseRedirectBase):
    status_code = 301
    status_code_preserve_request = 308

class HttpResponseNotModified(HttpResponse):
    status_code = 304

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        del self["content-type"]

    @HttpResponse.content.setter # type: ignore
    def content(self, value: Any) -> None:
        if value:
            raise AttributeError(
                "You cannot set content to a 304 (Not Modified) response"
            )
        self._container = []

class HttpResponseBadRequest(HttpResponse):
    status_code = 400

class HttpResponseNotFound(HttpResponse):
    status_code = 404

class HttpResponseForbidden(HttpResponse):
    status_code = 403

class HttpResponseNotAllowed(HttpResponse):
    status_code = 405

    def __init__(self, permitted_methods: Iterable[str], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self["Allow"] = ", ".join(permitted_methods)

    def __repr__(self) -> str:
        return "<%(cls)s [%(methods)s] status_code=%(status_code)d%(content_type)s>" % {
            "cls": self.__class__.__name__,
            "status_code": self.status_code,
            "content_type": self._content_type_for_repr,
            "methods": self["Allow"],
        }

class HttpResponseGone(HttpResponse):
    status_code = 410

class HttpResponseServerError(HttpResponse):
    status_code = 500

class Http404(Exception):
    pass

class JsonResponse(HttpResponse):
    """
    An HTTP response class that consumes data to be serialized to JSON.

    :param data: Data to be dumped into json.
    :param encoder: Should be a json encoder class. Defaults to
      ``django.core.serializers.json.DjangoJSONEncoder``.
    :param safe: Controls if only ``dict`` objects may be serialized. Defaults
      to ``False``.
    :param json_dumps_params: A dictionary of kwargs passed to json.dumps().
    """

    def __init__(
        self,
        data: Any,
        encoder: Type[json.JSONEncoder] = DjangoJSONEncoder,
        # RemovedInDjango2029Warning: Remove the safe parameter from here and
        # the docstring.
        safe: Optional[bool] = None,
        json_dumps_params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        # RemovedInDjango2029Warning.
        if safe is None:
            safe = False
        else:
            warnings.warn(
                "The safe parameter is deprecated.",
                category=RemovedInDjango2029Warning,
                skip_file_prefixes=django_file_prefixes(),
            )
        # RemovedInDjango2029Warning.
        if safe and not isinstance(data, dict):
            raise TypeError(
                "In order to allow non-dict objects to be serialized set the "
                "safe parameter to False."
            )
        if json_dumps_params is None:
            json_dumps_params = {}
        kwargs.setdefault("content_type", "application/json")
        json_data = json.dumps(data, cls=encoder, **json_dumps_params)
        super().__init__(content=json_data, **kwargs)
