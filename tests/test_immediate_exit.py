import pytest
from src.models import (
    ParsedSignal,
    SignalType,
    OrderCondition,
    OrderStatus
)
from src.order_manager import OrderManager
from fake_kotak_client import FakeKotakClient


def test_immediate_market_exit_on_exit_cost_to_cost():
    """
    Scenario:
    1. 'Nifty 24500 CE ABV 250' -> Buy order placed.
    2. 'TGT 270/290/320+ SL 220' -> SL and targets registered.
    3. Trade executed at 252 -> Stop loss placed at 220.
    4. 'Exit at cost to cost' arrives:
       - Stop loss order is cancelled.
       - Immediate MARKET SELL placed for all open quantity.
       - Trade closed.
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client, default_lots=1, account_id="test_exit")
    mgr.managed_trades = {}

    # Step 1: Buy order placed
    sig1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=24500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=250.0,
        trigger_price=250.0,
        raw_message="Nifty 24500 CE ABV 250"
    )
    res1 = mgr.process_signal(sig1)
    assert res1["status"] == "placed"
    entry_id = res1["order_id"]

    # Step 2: Split SL and targets
    sig2 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.MODIFY_ORDER,
        action="MODIFY",
        index="NIFTY",
        strike_price=24500,
        option_type="CE",
        targets=[270.0, 290.0, 320.0],
        stop_loss=220.0,
        raw_message="TGT 270/290/320+ SL 220"
    )
    res2 = mgr.process_signal(sig2)
    assert res2["status"] in ("modified", "managed_trade_updated", "stop_loss_registered")

    # Step 3: Trade fills
    client.orders[entry_id]["status"] = OrderStatus.COMPLETE
    mgr.manage_open_trades()
    trade = mgr.managed_trades["NIFTY_24500_CE"]
    stop_id = trade["stop_order_id"]
    assert stop_id is not None
    assert client.orders[stop_id]["status"] == OrderStatus.TRIGGER_PENDING

    # Step 4: "Exit at cost to cost" arrives
    sig3 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.CLOSE_POSITION,
        action="EXIT",
        index="NIFTY",
        strike_price=24500,
        option_type="CE",
        raw_message="Exit at cost to cost"
    )
    res3 = mgr.process_signal(sig3)

    assert res3["status"] == "exited_market"
    assert res3["contract_key"] == "NIFTY_24500_CE"
    assert res3["quantity"] == 65

    # Protective stop loss should be cancelled
    assert client.orders[stop_id]["status"] == OrderStatus.CANCELLED

    # Market exit order should be placed
    exit_order_id = res3["order_id"]
    exit_placement = client.orders[exit_order_id]["placement"]
    assert exit_placement.transaction_type == "SELL"
    assert exit_placement.order_type == "MKT"
    assert exit_placement.quantity == 65

    # Trade in managed_trades is closed
    assert trade["open_quantity"] == 0
    assert trade["status"] == "CLOSED"


def test_exit_message_cancels_unfilled_pending_order():
    """
    If 'Exit @253' arrives while the order is still pending (unfilled):
    It cancels the pending order.
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client, default_lots=1, account_id="test_exit_pending")
    mgr.managed_trades = {}

    sig1 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=24500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=250.0,
        trigger_price=250.0,
        raw_message="Nifty 24500 CE ABV 250"
    )
    res1 = mgr.process_signal(sig1)
    entry_id = res1["order_id"]
    assert client.orders[entry_id]["status"] == OrderStatus.TRIGGER_PENDING

    # 'Exit @253' arrives before fill
    sig2 = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.CLOSE_POSITION,
        action="EXIT",
        index="NIFTY",
        strike_price=24500,
        option_type="CE",
        raw_message="Exit @253"
    )
    res2 = mgr.process_signal(sig2)
    assert res2["status"] == "cancelled"
    assert client.orders[entry_id]["status"] == OrderStatus.CANCELLED
