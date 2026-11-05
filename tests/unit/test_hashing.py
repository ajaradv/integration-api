import pytest

from app.ingest.hashing import MalformedBody, canonical_hash, raw_hash, strict_parse


def test_key_order_and_whitespace_do_not_change_hash() -> None:
    a = strict_parse(b'{"a": 1, "b": {"c": [1, 2]}}')
    b = strict_parse(b'{\n "b":{"c":[1,2]},\n"a":1}')
    assert canonical_hash(a) == canonical_hash(b)


def test_changed_value_changes_hash() -> None:
    assert canonical_hash(strict_parse(b'{"a": 1}')) != canonical_hash(strict_parse(b'{"a": 2}'))


def test_number_format_is_kept() -> None:
    assert canonical_hash(strict_parse(b'{"a": 10}')) != canonical_hash(
        strict_parse(b'{"a": 10.0}')
    )


def test_raw_hash_is_over_bytes() -> None:
    assert raw_hash(b"abc") != raw_hash(b"abd")
    assert len(raw_hash(b"")) == 64


@pytest.mark.parametrize(
    "body",
    [
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b'{"a": 1e999}',
        b'{"a": "x\\u0000y"}',
        b'{"a\\u0000": 1}',
        b'{"a": ["ok", {"b": "\\u0000"}]}',
        b'{"a": "\xff\xfe"}',
        b"\xef\xbb\xbf{}",
        b"",
        b"not json",
        b'{"a": 1',
        b"[" * 100000,
    ],
)
def test_strict_parse_rejects(body: bytes) -> None:
    assert isinstance(strict_parse(body), MalformedBody)


@pytest.mark.parametrize(
    ("body", "expected"),
    [(b"{}", {}), (b"[1]", [1]), (b"5", 5), (b"null", None), (b'{"a": "\\u00e9"}', {"a": "é"})],
)
def test_strict_parse_accepts_any_json_value(body: bytes, expected: object) -> None:
    assert strict_parse(body) == expected
