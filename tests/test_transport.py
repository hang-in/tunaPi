"""Tests for the transport module."""

from tunapi.transport import MessageRef, RenderedMessage


def test_message_ref_sender_id_field() -> None:
    """Test that MessageRef has sender_id field and it works correctly."""
    # Without sender_id
    ref1 = MessageRef(channel_id=123, message_id=456)
    assert ref1.sender_id is None

    # With sender_id
    ref2 = MessageRef(channel_id=123, message_id=456, sender_id=789)
    assert ref2.sender_id == 789

    # sender_id should not affect equality (compare=False)
    ref3 = MessageRef(channel_id=123, message_id=456, sender_id=999)
    assert ref2 == ref3  # Same channel_id and message_id

    # sender_id should not affect hash (hash=False)
    assert hash(ref2) == hash(ref3)


def test_message_ref_all_fields() -> None:
    """Test MessageRef with all fields populated."""
    raw_data = {"from": {"id": 789, "first_name": "Test"}}
    ref = MessageRef(
        channel_id=123,
        message_id=456,
        raw=raw_data,
        thread_id=100,
        sender_id=789,
    )

    assert ref.channel_id == 123
    assert ref.message_id == 456
    assert ref.raw == raw_data
    assert ref.thread_id == 100
    assert ref.sender_id == 789


class TestMessageRefPush:
    def test_attrs(self):
        ref = MessageRef(channel_id=1, message_id=2)
        assert ref.channel_id == 1
        assert ref.message_id == 2

    def test_thread_id(self):
        ref = MessageRef(channel_id=1, message_id=2, thread_id=3)
        assert ref.thread_id == 3


class TestRenderedMessagePush:
    def test_basic(self):
        msg = RenderedMessage(text="hello")
        assert msg.text == "hello"

    def test_with_extra(self):
        msg = RenderedMessage(text="hello", extra={"key": "val"})
        assert msg.extra["key"] == "val"
