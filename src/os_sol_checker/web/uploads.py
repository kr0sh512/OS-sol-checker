"""Streaming parsing before framework form buffering; no temporary files or decoding."""
from __future__ import annotations

import unicodedata

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.requests import ClientDisconnect, Request

from ..domain.errors import CheckerError


class SizeError(CheckerError):
    def __init__(self, code: str, limit: int):
        super().__init__(code, f"Превышен допустимый размер: {limit} байт.", 413)
        self.limit_bytes = limit


def media_type(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    try:
        return parse_options_header(content_type)[0].lower()
    except (ValueError, UnicodeError):
        return b""


async def bounded_chunks(request: Request, limit: int):
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise CheckerError("invalid_multipart", "Некорректная длина запроса.", 422)
    if lengths:
        try:
            length = int(lengths[0])
            if length < 0:
                raise ValueError
        except ValueError:
            raise CheckerError("invalid_multipart", "Некорректная длина запроса.", 422) from None
        if length > limit:
            raise SizeError("request_too_large", limit)
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise SizeError("request_too_large", limit)
            yield chunk
    except ClientDisconnect:
        raise CheckerError("invalid_multipart", "Передача запроса прервана.", 422) from None


async def read_bounded(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in bounded_chunks(request, limit):
        body.extend(chunk)
    return bytes(body)


def validate_filename(raw: bytes) -> str:
    try:
        filename = raw.decode("utf-8", "strict")
    except UnicodeError:
        raise CheckerError("invalid_filename", "Имя файла должно быть в UTF-8.", 422) from None
    if (not filename or len(raw) > 255 or any(c in "/\\" or unicodedata.category(c) in {"Cc", "Cs"}
                                              for c in filename)):
        raise CheckerError("invalid_filename", "Недопустимое имя файла.", 422)
    if not filename.endswith(".c"):
        raise CheckerError("unsupported_extension", "Разрешены только файлы с расширением .c.", 415)
    return filename


def disposition_parameters(raw: bytes) -> tuple[bytes, dict[bytes, bytes]]:
    """Parse parameters without the library's legacy Windows basename rewriting."""
    segments = raw.split(b";", 1)
    disposition = segments[0].strip().lower()
    rest = segments[1] if len(segments) == 2 else b""
    parameters = {}
    while rest.strip():
        rest = rest.lstrip()
        key, separator, rest = rest.partition(b"=")
        key = key.strip().lower()
        if not separator or not key or key in parameters:
            raise ValueError("invalid disposition parameter")
        rest = rest.lstrip()
        if rest.startswith(b'"'):
            value = bytearray()
            index = 1
            while index < len(rest) and rest[index] != 34:
                char = rest[index]
                if char == 92 and index + 1 < len(rest) and rest[index + 1] in (34, 92):
                    index += 1
                    char = rest[index]
                value.append(char)
                index += 1
            if index >= len(rest):
                raise ValueError("unterminated disposition parameter")
            rest = rest[index + 1:].lstrip()
            if rest and not rest.startswith(b";"):
                raise ValueError("invalid disposition delimiter")
            rest = rest[1:] if rest else b""
            parameters[key] = bytes(value)
        else:
            value, separator, rest = rest.partition(b";")
            parameters[key] = value.strip()
    return disposition, parameters




async def read_upload(request: Request, *, request_limit: int, source_limit: int) -> tuple[str, bytes]:
    if media_type(request) != b"multipart/form-data":
        raise CheckerError("unsupported_media_type", "Ожидается multipart/form-data.", 415)
    try:
        _, options = parse_options_header(request.headers["content-type"])
        boundary = options.get(b"boundary", b"")
        if not boundary or len(boundary) > 70 or any(c < 32 or c > 126 for c in boundary):
            raise ValueError
    except (ValueError, UnicodeError):
        raise CheckerError("invalid_multipart", "Некорректная граница multipart.", 422) from None

    source = bytearray()
    headers: dict[bytes, bytes] = {}
    field = bytearray()
    value = bytearray()
    filename = None
    parts = 0
    header_bytes = 0
    complete = False
    part_complete = False

    def part_begin():
        nonlocal parts, header_bytes
        parts += 1
        headers.clear()
        header_bytes = 0

    def header_fragment(target, data, start, end):
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > 8192:
            raise CheckerError("invalid_multipart", "Слишком большие заголовки части.", 422)
        target.extend(data[start:end])

    def header_end():
        key = bytes(field).lower()
        if key in headers or not key or len(headers) >= 16:
            raise CheckerError("invalid_multipart", "Некорректные заголовки части.", 422)
        headers[key] = bytes(value)
        field.clear()
        value.clear()

    def headers_finished():
        nonlocal filename
        disposition, parameters = disposition_parameters(headers.get(b"content-disposition", b""))
        if disposition.lower() != b"form-data":
            raise CheckerError("invalid_multipart", "Нет Content-Disposition: form-data.", 422)
        if parameters.get(b"name") != b"file":
            raise CheckerError("unexpected_field", "Разрешено только поле file.", 422)
        if b"filename" not in parameters:
            raise CheckerError("missing_file", "Поле file должно содержать файл.", 422)
        if parts > 1:
            raise CheckerError("multiple_files", "Разрешён ровно один файл.", 422)
        filename = validate_filename(parameters[b"filename"])

    def part_data(data, start, end):
        if len(source) + end - start > source_limit:
            raise SizeError("source_too_large", source_limit)
        source.extend(data[start:end])

    def part_end():
        nonlocal part_complete
        part_complete = True

    def end():
        nonlocal complete
        complete = True

    parser = MultipartParser(boundary, {
        "on_part_begin": part_begin,
        "on_header_field": lambda data, start, end: header_fragment(field, data, start, end),
        "on_header_value": lambda data, start, end: header_fragment(value, data, start, end),
        "on_header_end": header_end, "on_headers_finished": headers_finished,
        "on_part_data": part_data, "on_part_end": part_end, "on_end": end,
    })
    try:
        async for chunk in bounded_chunks(request, request_limit):
            parser.write(chunk)
        parser.finalize()
    except (MultipartParseError, ValueError, KeyError):
        raise CheckerError("invalid_multipart", "Повреждённый multipart-запрос.", 422) from None
    if complete and parts == 0:
        raise CheckerError("missing_file", "Выберите один файл .c.", 422)
    if not complete or not part_complete:
        raise CheckerError("invalid_multipart", "Незавершённый multipart-запрос.", 422)
    if parts != 1 or filename is None:
        raise CheckerError("missing_file", "Выберите один файл .c.", 422)
    return filename, bytes(source)
