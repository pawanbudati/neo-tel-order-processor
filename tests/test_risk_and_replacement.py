import pytest
from src.models import (
    ParsedSignal,
    SignalType,
    OrderCondition,
    OrderStatus
)
from src.order_manager import OrderManager
from fake_kotak_client import FakeKotakClient

def make_signal(
    index: str,
    strike: float,
    opt: str = "CE",
    cond: OrderCondition = OrderCondition.ABOVE,
    price: float = 50.0,
    sl: float = None,
    action: str = "BUY",
    signal_type: SignalType = SignalType.NEW_ORDER
) -> ParsedSignal:
    return ParsedSignal(
        is_trading_signal=True,
        signal_type=signal_type,
        action=action,
        index=index,
        strike_price=strike,
        option_type=opt,
        order_condition=cond,
        entry_price=price,
        trigger_price=price,
        stop_loss=sl,
        raw_message=f"{action.lower()} {index.lower()} {int(strike)} {opt.lower()} above {price}"
    )

# ==============================================================================
# Feature 1: Same Index Pending Order Cancellation & Cross-Index Preservation
# ==============================================================================

def test_same_index_replacement_cancels_previous_order():
    """
    Scenario:
    1. First signal: "buy nifty 22500 above 50" -> placed.
    2. Second signal: "nifty 22350 above 25" ->
       The moment the 2nd Nifty call arrives, the 1st Nifty call should be cancelled,
       and the new Nifty call placed!
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client)

    sig1 = make_signal("NIFTY", 22500.0, price=50.0)
    res1 = mgr.process_signal(sig1)
    assert res1["status"] == "placed"
    order1_id = res1["order_id"]

    assert "NIFTY_22500_CE" in mgr.pending_orders_by_contract
    assert mgr.orders_by_id[order1_id].status != OrderStatus.CANCELLED

    # Now second NIFTY signal arrives (different strike)
    sig2 = make_signal("NIFTY", 22350.0, price=25.0)
    res2 = mgr.process_signal(sig2)
    assert res2["status"] == "placed"
    order2_id = res2["order_id"]

    # Verify: Previous Nifty 22500 order was CANCELLED and removed from pending
    assert "NIFTY_22500_CE" not in mgr.pending_orders_by_contract
    assert client.orders[order1_id]["status"] == OrderStatus.CANCELLED

    # Verify: New Nifty 22350 order is active
    assert "NIFTY_22350_CE" in mgr.pending_orders_by_contract
    assert mgr.pending_orders_by_contract["NIFTY_22350_CE"].order_id == order2_id

def test_different_index_orders_are_both_placed_and_preserved():
    """
    Scenario:
    1. First signal: "Nifty 22350CE above 35" -> placed.
    2. Second signal: "Bankex 55600 PE above 45" -> placed.
    Both are different items, so BOTH orders should be placed and active!
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client)

    # Signal 1: NIFTY
    sig1 = make_signal("NIFTY", 22350.0, "CE", price=35.0)
    res1 = mgr.process_signal(sig1)
    assert res1["status"] == "placed"
    order1_id = res1["order_id"]

    # Signal 2: BANKEX (Different item)
    sig2 = make_signal("BANKEX", 55600.0, "PE", price=45.0)
    res2 = mgr.process_signal(sig2)
    assert res2["status"] == "placed"
    order2_id = res2["order_id"]

    # Verify BOTH orders are active in pending_orders_by_contract
    assert "NIFTY_22350_CE" in mgr.pending_orders_by_contract
    assert "BANKEX_55600_PE" in mgr.pending_orders_by_contract

    # Neither order should be cancelled
    assert client.orders[order1_id]["status"] != OrderStatus.CANCELLED
    assert client.orders[order2_id]["status"] != OrderStatus.CANCELLED

def test_cancellation_on_ignore_and_exit_signals():
    """
    Scenario:
    An existing order should be cancelled when receiving an 'ignore', 'cancel', or 'exit' signal.
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client)

    # Place NIFTY and SENSEX orders
    mgr.process_signal(make_signal("NIFTY", 22350.0, "CE", price=35.0))
    mgr.process_signal(make_signal("SENSEX", 75000.0, "PE", price=120.0))

    assert "NIFTY_22350_CE" in mgr.pending_orders_by_contract
    assert "SENSEX_75000_PE" in mgr.pending_orders_by_contract

    # Exit Nifty only
    exit_nifty = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.CLOSE_POSITION,
        action="EXIT",
        index="NIFTY",
        raw_message="exit nifty"
    )
    mgr.process_signal(exit_nifty)

    # NIFTY should be cancelled, SENSEX should remain active
    assert "NIFTY_22350_CE" not in mgr.pending_orders_by_contract
    assert "SENSEX_75000_PE" in mgr.pending_orders_by_contract

# ==============================================================================
# Feature 2: Risk-Based Position Sizing based on Capital & Stop Loss
# ==============================================================================

def test_risk_based_lot_sizing_5_percent():
    """
    User's Example 1:
    - Signal: Above 55, SL 45 (10 points at risk)
    - Risk Percentage = 5%
    - Capital = 20,000 INR
    - NIFTY lot size = 65
    - Max lots by capital = 20000 // (55 * 65) = 5 lots
    - Max risk allowed = 20000 * 0.05 = 1,000 INR
    - Risk per lot = 10 * 65 = 650 INR
    - Max lots by risk = 1000 // 650 = 1 lot!
    - Optimal lot size: 1 lot (quantity: 65)
    - Expected loss if SL hit: 650 INR <= 1,000 INR (3.25% <= 5%)
    """
    client = FakeKotakClient()
    mgr = OrderManager(
        kotak_client=client,
        risk_percentage=5.0,
        capital=20000.0,
        default_lots=1
    )

    sig = make_signal("NIFTY", 22500.0, "CE", price=55.0, sl=45.0)
    res = mgr.process_signal(sig)

    assert res["status"] == "placed"
    assert res["quantity"] == 65  # Exactly 1 lot!

def test_risk_based_lot_sizing_50_percent():
    """
    User's Example 2:
    - Signal: Above 55, SL 45 (10 points at risk)
    - Risk Percentage = 50%
    - Capital = 20,000 INR
    - NIFTY lot size = 65
    - Max risk allowed = 20000 * 0.50 = 10,000 INR
    - Lots by risk = 10,000 // 650 = 15 lots
    - Max lots by capital = 20,000 // (55 * 65) = 5 lots
    - Optimal lot size: min(15, 5) = 5 lots! (quantity: 325)
    - Expected loss if SL hit: 325 * 10 = 3,250 INR (16.25% <= 50%)
    """
    client = FakeKotakClient()
    mgr = OrderManager(
        kotak_client=client,
        risk_percentage=50.0,
        capital=20000.0,
        default_lots=1
    )

    sig = make_signal("NIFTY", 22500.0, "CE", price=55.0, sl=45.0)
    res = mgr.process_signal(sig)

    assert res["status"] == "placed"
    assert res["quantity"] == 325  # Exactly 5 lots (5 * 65)!

def test_risk_sizing_fallback_without_sl():
    """
    If no SL is specified in the signal, fallback to default_lots (1 lot).
    """
    client = FakeKotakClient()
    mgr = OrderManager(
        kotak_client=client,
        risk_percentage=5.0,
        capital=20000.0,
        default_lots=1
    )

    sig = make_signal("NIFTY", 22500.0, "CE", price=55.0, sl=None)
    res = mgr.process_signal(sig)

    assert res["status"] == "placed"
    assert res["quantity"] == 65  # Default 1 lot

def test_risk_sizing_fallback_without_risk_percentage():
    """
    If risk_percentage is None, fallback to default_lots (1 lot).
    """
    client = FakeKotakClient()
    mgr = OrderManager(
        kotak_client=client,
        risk_percentage=None,
        capital=20000.0,
        default_lots=1
    )

    sig = make_signal("NIFTY", 22500.0, "CE", price=55.0, sl=45.0)
    res = mgr.process_signal(sig)

    assert res["status"] == "placed"
    assert res["quantity"] == 65  # Default 1 lot
