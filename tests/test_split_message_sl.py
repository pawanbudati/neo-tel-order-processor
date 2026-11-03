from src.models import ParsedSignal, SignalType, OrderCondition, OrderStatus
from src.order_manager import OrderManager
from fake_kotak_client import FakeKotakClient


def test_split_message_sl_when_entry_already_filled():
    """
    Scenario matching user's real situation:
    1. First message: "NIFTY 23700 CE ABV 27" -> entry order placed and immediately FILLED (status COMPLETE).
    2. 20s later: "TGT 35/45/55 SL 15" arrives with no entry price, only TGT & SL.
    Expected:
    - SL 15 and targets [35, 45, 55] must be attached.
    - A protective SELL SL order at 15.0 must be placed on Kotak Neo!
    - The trade must be stored in managed_trades.
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client, account_id="test_split_msg_filled")
    mgr.managed_trades = {}

    # Message 1 arrives: "NIFTY 23700 CE ABV 27"
    sig1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=23700,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=27.0,
        trigger_price=27.0,
        stop_loss=None,
        targets=[],
        raw_message="NIFTY 23700 CE ABV 27"
    )

    res1 = mgr.process_signal(sig1)
    assert res1["status"] == "placed"
    entry_order_id = res1["order_id"]

    # In real trading, market order immediately fills:
    client.orders[entry_order_id]["status"] = OrderStatus.COMPLETE

    # Message 2 arrives: "TGT 35/45/55 SL 15"
    # Parsed as NEW_ORDER or MODIFY_ORDER by Gemini
    sig2 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.MODIFY_ORDER,
        action="MODIFY",
        index="NIFTY",
        strike_price=23700,
        option_type="CE",
        order_condition=None,
        entry_price=None,
        trigger_price=None,
        stop_loss=15.0,
        targets=[35.0, 45.0, 55.0],
        raw_message="TGT 35/45/55 SL 15"
    )

    res2 = mgr.process_signal(sig2)
    assert res2["status"] == "stop_loss_attached"
    assert "stop_order_id" in res2
    stop_order_id = res2["stop_order_id"]
    assert stop_order_id is not None

    # Verify protective stop order was placed on fake client
    assert stop_order_id in client.orders
    stop_order = client.orders[stop_order_id]["placement"]
    assert stop_order.transaction_type == "SELL"
    assert stop_order.order_type == "SL"
    assert stop_order.trigger_price == 15.0
    assert stop_order.quantity == res1["quantity"]

    # Verify trade is tracked in managed_trades
    assert "NIFTY_23700_CE" in mgr.managed_trades
    managed = mgr.managed_trades["NIFTY_23700_CE"]
    assert managed["stop_loss"] == 15.0
    assert managed["stop_order_id"] == stop_order_id
    assert managed["targets"] == [35.0, 45.0, 55.0]


def test_split_message_sl_when_entry_still_pending():
    """
    Scenario:
    1. First message: "NIFTY 23700 CE ABV 27" -> entry order placed as SL-LMT, still PENDING.
    2. Message 2 arrives: "TGT 35/45/55 SL 15"
    Expected:
    - Stop loss 15 and targets registered into managed_trades.
    - When manage_open_trades() detects entry filled, it automatically places the stop order.
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client, account_id="test_split_msg_filled")
    mgr.managed_trades = {}

    sig1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=23700,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=27.0,
        trigger_price=27.0,
        stop_loss=None,
        targets=[],
        raw_message="NIFTY 23700 CE ABV 27"
    )

    res1 = mgr.process_signal(sig1)
    entry_order_id = res1["order_id"]
    assert client.orders[entry_order_id]["status"] == OrderStatus.TRIGGER_PENDING

    # Message 2 arrives while entry is still pending
    sig2 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.MODIFY_ORDER,
        action="MODIFY",
        index="NIFTY",
        strike_price=23700,
        option_type="CE",
        order_condition=None,
        entry_price=None,
        trigger_price=None,
        stop_loss=15.0,
        targets=[35.0, 45.0, 55.0],
        raw_message="TGT 35/45/55 SL 15"
    )

    res2 = mgr.process_signal(sig2)
    assert res2["status"] == "stop_loss_registered"

    # Trade registered in managed_trades
    assert "NIFTY_23700_CE" in mgr.managed_trades
    assert mgr.managed_trades["NIFTY_23700_CE"]["stop_loss"] == 15.0

    # Now simulate entry order getting filled on Kotak
    client.orders[entry_order_id]["status"] = OrderStatus.COMPLETE

    # Manager reconciles open trades
    mgr.manage_open_trades()

    # Verify protective stop was automatically placed!
    trade = mgr.managed_trades["NIFTY_23700_CE"]
    assert trade["stop_order_id"] is not None
    stop_id = trade["stop_order_id"]
    assert client.orders[stop_id]["placement"].trigger_price == 15.0


def test_target_price_execution_and_trailing_stop():
    """
    Verifies that target execution and stop trailing work properly:
    1. Trade is active with 3 lots (195 qty) at entry 27.0, SL 15.0, Targets [35.0, 45.0, 55.0].
    2. When LTP reaches Target 1 (35.0):
       - Target 1 quantity (65) is exited via Market SELL.
       - Stop loss on remaining 130 qty is trailed to breakeven (27.0).
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client, default_lots=3, risk_percentage=None, account_id="test_target_exec")
    mgr.managed_trades = {}

    sig1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=23700,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=27.0,
        trigger_price=27.0,
        stop_loss=15.0,
        targets=[35.0, 45.0, 55.0],
        raw_message="NIFTY 23700 CE ABV 27 TGT 35/45/55 SL 15"
    )

    res1 = mgr.process_signal(sig1)
    entry_id = res1["order_id"]
    client.orders[entry_id]["status"] = OrderStatus.COMPLETE

    # Initial reconciliation places protective stop at 15.0 for all 195 qty
    mgr.manage_open_trades()
    trade = mgr.managed_trades["NIFTY_23700_CE"]
    initial_stop_id = trade["stop_order_id"]
    assert initial_stop_id is not None
    assert trade["stop_price"] == 15.0

    # Simulate price rising to Target 1 (35.0)
    client.get_ltp = lambda token, seg: 35.5

    # Run reconciliation
    mgr.manage_open_trades()

    # Target 1 exit order should have been placed!
    pending_exit_id = trade.get("pending_exit_id")
    assert pending_exit_id is not None
    exit_order = client.orders[pending_exit_id]["placement"]
    assert exit_order.transaction_type == "SELL"
    assert exit_order.order_type == "MKT"
    assert exit_order.quantity == 65  # 1 lot exited

    # Protective stop should have trailed to breakeven (fill price 27.25)
    assert trade["stop_price"] == trade["entry_average"]

