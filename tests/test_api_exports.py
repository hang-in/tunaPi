from tunapi import api


def test_api_exports() -> None:
    assert api.TUNAPI_PLUGIN_API_VERSION == 1
    assert "TransportRuntime" in api.__all__
    assert api.TransportRuntime is not None
