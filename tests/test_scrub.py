from jev_telemetry.reduce import Scrubber


def test_scrubs_common_pii_and_secrets():
    s = Scrubber()
    r = s.scrub(
        "user jane.doe@example.com paid with 4111 1111 1111 1111 using api_key=abc123secret "
        "Authorization: Bearer abcdefghijklmnop.qrstu ssn 123-45-6789"
    )
    assert "jane.doe" not in r.text and "<EMAIL>" in r.text
    assert "4111" not in r.text and "<CARD>" in r.text
    assert "abc123secret" not in r.text
    assert "abcdefghijklmnop" not in r.text
    assert "<SSN>" in r.text
    assert r.hits["EMAIL"] == 1 and r.hits["CARD"] == 1


def test_card_requires_luhn_and_leaves_uuids_alone():
    s = Scrubber()
    assert s.scrub("order 1234567890123456 shipped").text == "order 1234567890123456 shipped"
    uuid = "order 5f0c0a37-7e4d-4f27-9d1e-112233445566 done"
    assert s.scrub(uuid).text == uuid


def test_card_keeps_following_whitespace():
    assert Scrubber().scrub("card=4111 1111 1111 1111 email=x").text.startswith("card=<CARD> email")


def test_placeholders_are_not_rescrubbed():
    s = Scrubber()
    once = s.scrub("api_key=sk-test0123456789abcdef0123").text
    again = s.scrub(once)
    assert not again.dirty and again.text == once


def test_ips_kept_unless_requested():
    assert "10.0.0.1" in Scrubber().scrub("from 10.0.0.1").text
    assert "<IPV4>" in Scrubber(redact_ips=True).scrub("from 10.0.0.1").text
