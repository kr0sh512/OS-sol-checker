import pytest
from starlette.requests import Request

from os_sol_checker.domain.errors import CheckerError
from os_sol_checker.web.uploads import read_upload


def multipart(source=b"", filename=b"solution.c", extra=b""):
    return (b'--bound\r\nContent-Disposition: form-data; name="file"; filename="' + filename
            + b'"\r\nContent-Type: text/plain\r\n\r\n' + source + b"\r\n" + extra + b"--bound--\r\n")


def request_for(chunks, *, content_length=None, content_type=b"multipart/form-data; boundary=bound"):
    pending = list(chunks)
    consumed = []

    async def receive():
        chunk = pending.pop(0)
        consumed.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    headers = [(b"content-type", content_type)]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    return Request(scope, receive), consumed


@pytest.mark.parametrize("size", [0, 102400])
async def test_exact_original_bytes_and_inclusive_limit(size):
    source = (b"\x00\xff\r\n" * ((size + 3) // 4))[:size]
    body = multipart(source)
    request, _ = request_for([body[i:i + 17] for i in range(0, len(body), 17)])
    filename, received = await read_upload(request, request_limit=131072, source_limit=102400)
    assert filename == "solution.c"
    assert received == source


async def test_source_over_limit_stops_before_next_network_chunk():
    first = multipart(b"x" * 102401)
    request, consumed = request_for([first, b"never read"])
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=131072, source_limit=102400)
    assert error.value.code == "source_too_large"
    assert consumed == [first]


async def test_whole_body_limit_without_content_length():
    request, consumed = request_for([b"x" * 131073, b"never read"])
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=131072, source_limit=102400)
    assert error.value.code == "request_too_large"
    assert len(consumed) == 1


async def test_declared_body_limit_rejected_before_reading():
    request, consumed = request_for([multipart()], content_length=131073)
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=131072, source_limit=102400)
    assert error.value.code == "request_too_large"
    assert not consumed


@pytest.mark.parametrize("filename,code", [
    (b"", "invalid_filename"), (b"x.C", "unsupported_extension"),
    (b"x.c.exe", "unsupported_extension"), (b"../x.c", "invalid_filename"),
    (b"C:\\fakepath\\x.c", "invalid_filename"), (b"\\\\host\\share\\x.c", "invalid_filename"),
    (b"bad\x00.c", "invalid_filename"), (b"x" * 254 + b".c", "invalid_filename"),
])
async def test_filename_rules_do_not_silently_normalize_paths(filename, code):
    request, _ = request_for([multipart(filename=filename)])
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=131072, source_limit=102400)
    assert error.value.code == code


@pytest.mark.parametrize("extra,code", [
    (b'--bound\r\nContent-Disposition: form-data; name="file"; filename="other.c"\r\n\r\nx\r\n', "multiple_files"),
    (b'--bound\r\nContent-Disposition: form-data; name="name"\r\n\r\nattacker\r\n', "unexpected_field"),
])
async def test_extra_parts_are_rejected(extra, code):
    request, _ = request_for([multipart(extra=extra)])
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=131072, source_limit=102400)
    assert error.value.code == code


async def test_truncated_multipart_not_accepted_even_when_file_is_complete():
    request, _ = request_for([multipart()[:-12]])
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=131072, source_limit=102400)
    assert error.value.code == "invalid_multipart"


async def test_total_body_inclusive_limit_and_epilogue_count():
    body = multipart()
    request, _ = request_for([body])
    assert (await read_upload(request, request_limit=len(body), source_limit=102400))[1] == b""
    request, _ = request_for([body, b"one byte too many"])
    with pytest.raises(CheckerError) as error:
        await read_upload(request, request_limit=len(body), source_limit=102400)
    assert error.value.code == "request_too_large"
