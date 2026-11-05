import pytest

from app.transform.address import split_address


def test_full_split() -> None:
    assert split_address("114 Hunts Point Market, Bronx, NY 10474, USA") == {
        "street": None,
        "locality": "Bronx",
        "state": "NY",
        "country": "USA",
        "postal_code": "10474",
        "full_address": "114 Hunts Point Market, Bronx, NY 10474, USA",
    }


@pytest.mark.parametrize(
    ("text", "locality", "state", "zip_"),
    [
        ("Boston, MA 02101, USA", "Boston", "MA", "02101"),
        ("Atlanta, GA 30303, USA", "Atlanta", "GA", "30303"),
        ("San Francisco, CA 94102, USA", "San Francisco", "CA", "94102"),
        ("Madison, WI 53703, USA", "Madison", "WI", "53703"),
        ("1460 E Hi Line Rd, Pharr, TX 78577, USA", "Pharr", "TX", "78577"),
        ("Springfield, IL 62701-1234, USA", "Springfield", "IL", "62701-1234"),
    ],
)
def test_split_samples(text: str, locality: str, state: str, zip_: str) -> None:
    result = split_address(text)
    assert result is not None
    assert (result["locality"], result["state"], result["postal_code"]) == (locality, state, zip_)
    assert result["country"] == "USA"
    assert result["street"] is None
    assert result["full_address"] == text


@pytest.mark.parametrize("text", ["London, UK", "Somewhere", "A, B, C", "X, ny 10474, USA"])
def test_unsplittable_keeps_full_address_only(text: str) -> None:
    result = split_address(text)
    assert result is not None
    assert result["full_address"] == text
    assert all(result[k] is None for k in ("street", "locality", "state", "country", "postal_code"))


@pytest.mark.parametrize("text", [None, "", "   "])
def test_blank_is_none(text: str | None) -> None:
    assert split_address(text) is None
