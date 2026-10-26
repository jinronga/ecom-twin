import hashlib
import hmac
import json

import pytest

from ecom_twin.adapters.base import AdapterError, ErrorCategory
from ecom_twin.adapters.miaoshou import MiaoshouClient, generate_sign


def make_client(response: dict | str, status: int = 200, captured: list | None = None):
    def transport(url, headers, body, timeout):
        if captured is not None:
            captured.append((url, headers, body))
        return status, response if isinstance(response, str) else json.dumps(response)

    return MiaoshouClient("ak_test", "as_secret", transport=transport, clock=lambda: 1700000000)


def test_sign_follows_documented_formula():
    path, body = "/open/v1/order/create", '{"a":1}'
    expected = hmac.new(b"as_secret", f"as_secret{path}1700000000ak_test{body}as_secret".encode(),
                        hashlib.sha256).hexdigest()
    assert generate_sign("as_secret", path, 1700000000, "ak_test", body) == expected


def test_call_signs_request_and_returns_data():
    captured = []
    client = make_client({"result": "success", "code": "200", "data": {"shopList": [{"shopId": 1}]}},
                         captured=captured)
    assert client.list_shops("tiktok") == [{"shopId": 1}]
    url, headers, body = captured[0]
    assert url.endswith("/open/v1/product/shop/shop/get_shop_list")
    assert headers["x-app-key"] == "ak_test" and headers["x-timestamp"] == "1700000000"
    path = "/open/v1/product/shop/shop/get_shop_list"
    assert headers["x-sign"] == generate_sign("as_secret", path, 1700000000, "ak_test", body.decode())


def test_common_collect_box_detail_and_edit_payloads():
    captured = []
    client = make_client({"result": "success", "code": "200", "data": {"ossMd5": "md5"}}, captured=captured)
    assert client.common_collect_box_detail(123)["ossMd5"] == "md5"
    assert client.common_collect_box_edit(123, {"title": "new"}, "md5")["ossMd5"] == "md5"
    assert captured[0][0].endswith("/open/v1/product/common_collect_box/common_collect_box/get_common_collect_box_detail")
    edit_body = json.loads(captured[1][2])
    assert edit_body == {
        "commonCollectBoxDetailId": 123,
        "editCommonCollectBoxDetail": {"title": "new"},
        "ossMd5": "md5",
    }


@pytest.mark.parametrize("code,category", [
    ("signInvalid", ErrorCategory.AUTH),
    ("ipNotInWhitelist", ErrorCategory.PERMISSION),
    ("apiQpmRateLimit", ErrorCategory.RATE_LIMIT),
    ("upstreamReadTimeout", ErrorCategory.UPSTREAM),
    ("shopNotFound", ErrorCategory.NOT_FOUND),
    ("somethingNew", ErrorCategory.UNKNOWN),
])
def test_error_codes_are_normalized(code, category):
    client = make_client({"result": "fail", "code": code, "message": "x"})
    with pytest.raises(AdapterError) as e:
        client.list_shops("tiktok")
    assert e.value.category == category


def test_empty_response_is_upstream_error():
    with pytest.raises(AdapterError) as e:
        make_client("").list_shops("tiktok")
    assert e.value.category == ErrorCategory.UPSTREAM and e.value.category.retryable


def test_missing_credentials_fail_fast():
    with pytest.raises(AdapterError):
        MiaoshouClient("", "")
