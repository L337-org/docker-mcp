import base64
import binascii
import os
import random
import urllib.parse

import pytest

from docker_mcp.redaction import forms


def test_form_identifiers_are_fixed():
    # Markers record these, so renumbering one would make an outstanding marker restore the wrong text.
    assert forms.form_ids() == list(range(16))


def test_the_raw_value_comes_first_and_every_form_renders_back_to_its_own_text():
    value = "s3cr3t-Value_with+chars/=="
    found = forms.forms_of(value)
    assert next(iter(found.items())) == (value, forms.RAW)
    for text, form_id in found.items():
        assert forms.render(value, form_id) == text


def test_the_common_encodings_are_all_matched():
    value = "correct horse battery staple"
    found = set(forms.forms_of(value))
    data = value.encode()
    assert {
        base64.b64encode(data).decode(),
        base64.b64encode(data).decode().rstrip("="),
        base64.urlsafe_b64encode(data).decode(),
        base64.b32encode(data).decode(),
        binascii.hexlify(data).decode(),
        binascii.hexlify(data).decode().upper(),
        urllib.parse.quote(value, safe=""),
    } <= found


@pytest.mark.parametrize("encoder", [base64.b64encode, base64.urlsafe_b64encode])
@pytest.mark.parametrize("shift", [0, 1, 2])
def test_a_value_inside_a_larger_base64_blob_is_found_at_every_alignment(encoder, shift):
    # From 7 characters, where the stable part of even the worst alignment reaches the minimum length;
    # shorter values are matched raw only, an accepted limitation the module states.
    rng = random.Random(shift)  # noqa: S311 - seeded so a failure names a reproducible value; not a secret
    for length in range(7, 40):
        value = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789!@#") for _ in range(length))
        blob = encoder(os.urandom(shift) + value.encode() + os.urandom(7)).decode()
        assert any(text in blob for text, form_id in forms.forms_of(value).items() if form_id != forms.RAW), (
            f"no form of a {length}-character value found in its encoding at shift {shift}"
        )


def test_short_encoded_forms_are_left_out_but_the_raw_value_is_kept():
    found = forms.forms_of("ab")
    assert found == {"ab": forms.RAW}
    assert all(len(text) >= forms.MIN_ENCODED_FORM_LENGTH for text, fid in forms.forms_of("abcdefgh").items() if fid)


def test_url_encoding_is_left_out_when_it_changes_nothing():
    assert "plainvalue123" not in [t for t, fid in forms.forms_of("plainvalue123").items() if fid == 15]
    assert forms.forms_of("a b/c?d=e")["a%20b%2Fc%3Fd%3De"] == 15


def test_an_empty_value_has_no_forms():
    assert forms.forms_of("") == {}
