"""Basic import tests."""


def test_import() -> None:
    """Test that the package can be imported."""
    import tunapi.discord as discord_transport

    assert discord_transport.BACKEND is not None
