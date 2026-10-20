import pytest

from backend.application.clients.avatar.image_url import check_image_url

HOSTS = ["avatars.example.test"]


@pytest.mark.parametrize(
    "url",
    [
        "https://avatars.example.test/catalog/a.png",
        "https://AVATARS.example.test/catalog/a.png?v=2",
        "https://avatars.example.test:443/a.png",
    ],
)
def test_acceptable_urls(url: str) -> None:
    check_image_url(url, HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "http://avatars.example.test/a.png",
        "https://avatars.example.test:8443/a.png",
        "https://avatars.example.test:/a.png",
        "https://user@avatars.example.test/a.png",
        "https://user:pw@avatars.example.test/a.png",
        "https://avatars.example.test@evil.test/a.png",
        "https://127.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://avatars.example.test./a.png",
        "https://avatars.example.test/a/../b.png",
        "https://avatars.example.test/a/./b.png",
        "https://avatars.example.test/a%2fb.png",
        "https://avatars.example.test/a%2Fb.png",
        "https://avatars.example.test/a%5cb.png",
        "https://avatars.example.test/a%2e%2e/b.png",
        "https://avatars.example.test/a.png#frag",
        "https://avatars.example.test\\evil.test/a.png",
        "https://avatars.example.test/a b.png",
        "https://outside.example.test/a.png",
        "https://sub.avatars.example.test/a.png",
        "https://avatars.example.test/" + "a" * 2100,
    ],
)
def test_hostile_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        check_image_url(url, HOSTS)


def test_idna_host_is_compared_in_punycode_form() -> None:
    check_image_url("https://m\u00fcnchen.example.test/a.png", ["xn--mnchen-3ya.example.test"])
    with pytest.raises(ValueError):
        check_image_url("https://m\u00fcnchen.example.test/a.png", HOSTS)


def test_shape_check_without_a_host_list_still_refuses_bad_urls() -> None:
    check_image_url("https://anything.example.test/a.png")
    with pytest.raises(ValueError):
        check_image_url("https://192.168.0.10/a.png")
