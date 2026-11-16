import asyncio
import json
from unittest.mock import MagicMock, patch
import pytest

from src.models import ParsedSignal, SignalType, OrderCondition
from src.pubsub.consumer import PubSubSignalConsumer


def test_consumer_init_defaults():
    consumer = PubSubSignalConsumer(
        account_id="yuva",
        project_id="test-project",
    )
    assert consumer.account_id == "yuva"
    assert consumer.project_id == "test-project"
    assert consumer.subscription_id == "kotak_executor_yuva"
    assert consumer._is_running is False


@pytest.mark.asyncio
async def test_consumer_missing_project_raises():
    consumer = PubSubSignalConsumer(account_id="yuva", project_id="")
    with pytest.raises(ValueError, match="GCP_PROJECT_ID is not configured"):
        await consumer.start()


@pytest.mark.asyncio
async def test_consumer_start_and_message_delivery():
    mock_order_manager = MagicMock()
    mock_order_manager.process_signal.return_value = {"order_id": "test-order-999"}

    consumer = PubSubSignalConsumer(
        order_manager=mock_order_manager,
        account_id="pavan",
        project_id="test-project",
        subscription_id="kotak_executor_pavan",
    )

    mock_subscriber = MagicMock()
    mock_subscriber.subscription_path.return_value = (
        "projects/test-project/subscriptions/kotak_executor_pavan"
    )
    mock_future = MagicMock()
    mock_subscriber.subscribe.return_value = mock_future

    with patch("google.cloud.pubsub_v1.SubscriberClient", return_value=mock_subscriber):
        await consumer.start()

        assert consumer.subscription_path == "projects/test-project/subscriptions/kotak_executor_pavan"
        assert consumer._is_running is True
        mock_subscriber.subscribe.assert_called_once()

        # Simulate incoming Pub/Sub message
        sample_signal = {
            "is_trading_signal": True,
            "signal_type": "NEW_ORDER",
            "action": "BUY",
            "index": "NIFTY",
            "strike_price": 24000,
            "option_type": "CE",
            "order_condition": "ABOVE",
            "entry_price": 100.0,
            "trigger_price": 100.0,
            "stop_loss": 80.0,
            "target": 120.0,
            "targets": [120.0, 140.0],
            "reasoning": "Test signal",
            "raw_message": "NIFTY 24000 CE ABV 100",
            "_meta": {
                "event_id": "evt-001",
                "published_at": "2026-11-16T10:00:00Z",
                "source": "telegram_gemini_extractor",
            },
        }

        mock_msg = MagicMock()
        mock_msg.data = json.dumps(sample_signal).encode("utf-8")
        mock_msg.message_id = "msg-999"
        mock_msg.ordering_key = "NIFTY_24000_CE"
        mock_msg.attributes = {"event_id": "evt-001"}
        mock_msg.publish_time = "2026-11-16T10:00:00Z"

        # Process the message
        await consumer._process_pubsub_message(mock_msg)

        # Verify order_manager processed the signal
        mock_order_manager.process_signal.assert_called_once()
        passed_signal = mock_order_manager.process_signal.call_args[0][0]
        assert isinstance(passed_signal, ParsedSignal)
        assert passed_signal.index == "NIFTY"
        assert passed_signal.strike_price == 24000
        assert passed_signal.signal_type == SignalType.NEW_ORDER

        # Verify message was acknowledged
        mock_msg.ack.assert_called_once()

        await consumer.stop()
        assert consumer._is_running is False
        mock_future.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_consumer_invalid_payload_acked_gracefully():
    mock_order_manager = MagicMock()
    consumer = PubSubSignalConsumer(
        order_manager=mock_order_manager,
        account_id="sahi",
        project_id="test-project",
    )

    mock_msg = MagicMock()
    mock_msg.data = b"NOT_A_VALID_JSON"
    mock_msg.message_id = "msg-bad"
    mock_msg.attributes = {}

    await consumer._process_pubsub_message(mock_msg)

    # Order manager should not be called
    mock_order_manager.process_signal.assert_not_called()
    # Message should still be acknowledged to prevent poison pill loop
    mock_msg.ack.assert_called_once()


@pytest.mark.asyncio
async def test_consumer_order_manager_exception_acked():
    mock_order_manager = MagicMock()
    mock_order_manager.process_signal.side_effect = RuntimeError("Broker connection timeout")

    consumer = PubSubSignalConsumer(
        order_manager=mock_order_manager,
        account_id="yuva",
        project_id="test-project",
    )

    sample_signal = {
        "is_trading_signal": True,
        "signal_type": "CLOSE_POSITION",
        "action": "SELL",
        "index": "NIFTY",
        "strike_price": 24000,
        "option_type": "CE",
        "reasoning": "Exit",
    }
    mock_msg = MagicMock()
    mock_msg.data = json.dumps(sample_signal).encode("utf-8")
    mock_msg.message_id = "msg-error"
    mock_msg.attributes = {}

    await consumer._process_pubsub_message(mock_msg)

    # Order manager was called
    mock_order_manager.process_signal.assert_called_once()
    # Message should still be acknowledged to avoid blocking future messages
    mock_msg.ack.assert_called_once()
