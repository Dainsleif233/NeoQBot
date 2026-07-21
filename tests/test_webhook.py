import hashlib
import hmac

from mua_bot.app import _verify_onebot_signature


def test_onebot_signature() -> None:
    body = b'{"post_type":"meta_event"}'
    signature = "sha1=" + hmac.new(b"secret", body, hashlib.sha1).hexdigest()

    assert _verify_onebot_signature(body, signature, "secret") is True
    assert _verify_onebot_signature(body, "sha1=bad", "secret") is False
