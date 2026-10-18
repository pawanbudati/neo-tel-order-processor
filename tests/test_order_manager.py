import pytest
from src.models import ParsedSignal, SignalType, OrderCondition, OrderStatus
from src.order_manager import OrderManager
from src.kotak_client import KotakClient
from fake_kotak_client import FakeKotakClient

@pytest.fixture
def order_manager():
    client = FakeKotakClient()
    return OrderManager(kotak_client=client)

def test_new_order_placement(order_manager):
    signal = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=22500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=50.0,
        trigger_price=50.0,
        raw_message="buy nifty 22500 above 50"
    )

    result = order_manager.process_signal(signal)
    assert result["status"] == "placed"
    assert "order_id" in result

    # Check pending order tracking
    contract_key = signal.contract_key()
    assert contract_key in order_manager.pending_orders_by_contract
    order = order_manager.pending_orders_by_contract[contract_key]
    assert order.trigger_price == 50.0
    # Price should include buffer (default 1% of 50 = 50.5)
    assert order.price >= 50.5
    assert order.order_type == "SL"
    assert order.quantity == 65  # Nifty revised lot size

def test_order_modification_on_price_update(order_manager):
    # Step 1: Place initial order at 50
    signal1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=22500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=50.0,
        trigger_price=50.0,
        raw_message="buy nifty 22500 above 50"
    )
    res1 = order_manager.process_signal(signal1)
    orig_order_id = res1["order_id"]

    # Step 2: Revised signal arrives at 55
    signal2 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.MODIFY_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=22500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=55.0,
        trigger_price=55.0,
        raw_message="nifty 22500 abv 55"
    )
    res2 = order_manager.process_signal(signal2)
    assert res2["status"] == "modified"
    assert res2["order_id"] == orig_order_id

    # Verify updated trigger price
    updated_order = order_manager.orders_by_id[orig_order_id]
    assert updated_order.trigger_price == 55.0
    assert updated_order.price >= 55.55

def test_order_cancellation_on_ignore(order_manager):
    # Step 1: Place initial order
    signal1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=22500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=50.0,
        trigger_price=50.0,
        raw_message="buy nifty 22500 above 50"
    )
    res1 = order_manager.process_signal(signal1)
    orig_order_id = res1["order_id"]

    # Step 2: Receive ignore signal
    ignore_signal = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.IGNORE_SIGNAL,
        action="IGNORE",
        index="NIFTY",
        strike_price=22500,
        option_type="CE",
        raw_message="ignore previous call"
    )
    res_cancel = order_manager.process_signal(ignore_signal)
    assert res_cancel["status"] == "cancelled"
    assert res_cancel["order_id"] == orig_order_id

    # Verify it is removed from active pending orders
    assert signal1.contract_key() not in order_manager.pending_orders_by_contract

def test_sensex_order_placement(order_manager):
    signal = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="SENSEX",
        strike_price=75000,
        option_type="PE",
        order_condition=OrderCondition.AT,
        entry_price=120.0,
        raw_message="buy sensex 75000 pe at 120"
    )

    result = order_manager.process_signal(signal)
    assert result["status"] == "placed"
    order = order_manager.pending_orders_by_contract[signal.contract_key()]
    assert order.exchange_segment == "bse_fo"
    assert order.quantity == 20  # Sensex lot size
    assert order.order_type == "L"
    assert order.price == 120.0

def test_lot_sizes_all_indices():
    from config.settings import settings
    assert settings.get_lot_size("NIFTY") == 65
    assert settings.get_lot_size("nifty") == 65
    assert settings.get_lot_size("SENSEX") == 20
    assert settings.get_lot_size("sensex") == 20
    assert settings.get_lot_size("BANKNIFTY") == 30
    assert settings.get_lot_size("FINNIFTY") == 60
    assert settings.get_lot_size("MIDCPNIFTY") == 120
    assert settings.get_lot_size("BANKEX") == 30
    # Ensure FINNIFTY does not accidentally match NIFTY
    assert settings.get_lot_size("FINNIFTY24OCT") == 60

def test_transaction_type_mapping():
    from src.kotak_client import KotakClient
    from src.models import OrderPlacement

    client = FakeKotakClient()
    order_buy = OrderPlacement(
        trading_symbol="NIFTY26OCT25000CE",
        exchange_segment="nse_fo",
        transaction_type="BUY",
        order_type="SL",
        quantity=65,
        price=101.0,
        trigger_price=100.0
    )
    res_buy = client.place_order(order_buy)
    assert res_buy["success"] is True
    assert res_buy["data"]["transaction_type"] == "B"

    order_sell = OrderPlacement(
        trading_symbol="NIFTY26OCT25000CE",
        exchange_segment="nse_fo",
        transaction_type="SELL",
        order_type="L",
        quantity=65,
        price=100.0
    )
    res_sell = client.place_order(order_sell)
    assert res_sell["success"] is True
    assert res_sell["data"]["transaction_type"] == "S"

def test_unique_order_tags_on_placement():
    from unittest.mock import MagicMock
    from src.kotak_client import KotakClient
    from src.models import ParsedSignal, SignalType, OrderCondition

    mock_client = FakeKotakClient()
    captured_placements = []
    orig_place = mock_client.place_order

    def mock_place(placement):
        captured_placements.append(placement)
        return orig_place(placement)

    mock_client.place_order = mock_place
    manager = OrderManager(kotak_client=mock_client)

    sig1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=22500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=50.0,
        raw_message="buy nifty 22500 above 50"
    )
    sig2 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="FINNIFTY",
        strike_price=23950,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=35.0,
        raw_message="buy finnifty 23950 ce above 35"
    )

    manager.process_signal(sig1)
    manager.process_signal(sig2)

    assert len(captured_placements) == 2
    tag1 = captured_placements[0].tag
    tag2 = captured_placements[1].tag
    assert tag1 is not None and tag2 is not None
    assert tag1 != tag2
    assert tag1.startswith("TG")
    assert tag2.startswith("TG")

def test_kotak_client_live_tag_sanitization_and_error_parsing():
    from unittest.mock import MagicMock
    from src.kotak_client import KotakClient
    from src.models import OrderPlacement

    mock_neo = MagicMock()
    client = KotakClient.__new__(KotakClient)
    client.client = mock_neo

    # Mock duplicate client order id response
    mock_neo.place_order.return_value = {
        "stCode": 32,
        "errMsg": "error from core",
        "stat": "Put Order Response : PlaceOrderResponse Failed :Client Order Id Error Client OrderID already exists Data: 0"
    }

    order = OrderPlacement(
        trading_symbol="FINNIFTY26OCT23950CE",
        exchange_segment="nse_fo",
        transaction_type="BUY",
        order_type="SL",
        quantity=60,
        price=35.35,
        trigger_price=35.0,
        tag="TG_BOT"
    )

    res = client.place_order(order)
    assert res["success"] is False
    assert "Client OrderID already exists" in res["error"]

    # Verify that the tag passed to mock_neo was sanitized and made unique
    sent_call = mock_neo.place_order.call_args[1]
    sent_tag = sent_call.get("tag")
    assert sent_tag is not None
    assert sent_tag.startswith("TGBOT")
    assert sent_tag.isalnum()
    assert len(sent_tag) <= 20
    assert sent_tag != "TG_BOT"  # must not be static

    # Verify modify_order error extraction
    mock_neo.modify_order.return_value = {
        "stCode": 1022,
        "errMsg": "order modify failed",
        "stat": "invalid trigger price"
    }
    res_mod = client.modify_order("123", 50.0, "SL", 60, 48.0)
    assert res_mod["success"] is False
    assert "invalid trigger price" in res_mod["error"]

    # Verify cancel_order error extraction
    mock_neo.cancel_order.return_value = {
        "stCode": 1022,
        "errMsg": "order is rejected",
        "stat": "please provide valid order number"
    }
    res_canc = client.cancel_order("123")
    assert res_canc["success"] is False
    assert "please provide valid order number" in res_canc["error"]

