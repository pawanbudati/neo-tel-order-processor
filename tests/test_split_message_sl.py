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
    Verifies the trailing stop logic across all milestones:
    1. Trade is active with 3 lots (195 qty) at entry 27.25, SL 15.0, Targets [35.0, 45.0, 55.0].
    2. When LTP reaches Target 1 (35.0):
       - Position does NOT close. Full 195 qty remains open.
       - Stop loss is trailed to Buy Price (27.25).
    3. When LTP reaches Target 2 (45.0):
       - Stop loss is trailed to Target 1 (35.0).
    4. When LTP reaches Target 3 (55.0):
       - Stop loss is trailed to Target 2 (45.0).
    5. Beyond Target 3 (diff = 55 - 45 = 10):
       - When price reaches 65.0 (55 + 10): SL trails to 55.0 (45 + 10).
       - When price reaches 75.0 (55 + 20): SL trails to 65.0 (45 + 20).
    6. When price drops back to 68.0:
       - SL stays at 65.0 (ratchet: never moves down).
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
    assert trade["open_quantity"] == 195

    # 1. Price reaches Target 1 (35.0) -> SL moves to Buy Price (entry_average 27.25)
    client.get_ltp = lambda token, seg: 35.5
    mgr.manage_open_trades()
    assert trade.get("pending_exit_id") is None  # Does NOT exit
    assert trade["open_quantity"] == 195         # Full position kept
    assert trade["stop_price"] == trade["entry_average"]  # Trailed to buy price
    assert client.orders[initial_stop_id]["placement"].trigger_price == trade["entry_average"]

    # 2. Price reaches Target 2 (45.0) -> SL moves to Target 1 (35.0)
    client.get_ltp = lambda token, seg: 45.5
    mgr.manage_open_trades()
    assert trade["stop_price"] == 35.0
    assert client.orders[initial_stop_id]["placement"].trigger_price == 35.0

    # 3. Price reaches Target 3 (55.0) -> SL moves to Target 2 (45.0)
    client.get_ltp = lambda token, seg: 55.5
    mgr.manage_open_trades()
    assert trade["stop_price"] == 45.0
    assert client.orders[initial_stop_id]["placement"].trigger_price == 45.0

    # 4. Price advances beyond T3 by 10 points (step = 55 - 45 = 10) -> reaches 65.0
    client.get_ltp = lambda token, seg: 65.0
    mgr.manage_open_trades()
    assert trade["stop_price"] == 55.0
    assert client.orders[initial_stop_id]["placement"].trigger_price == 55.0

    # 5. Price advances another 10 points -> reaches 75.0
    client.get_ltp = lambda token, seg: 75.0
    mgr.manage_open_trades()
    assert trade["stop_price"] == 65.0
    assert client.orders[initial_stop_id]["placement"].trigger_price == 65.0

    # 6. Price retraces from 75.0 to 68.0 -> SL must NOT decrease
    client.get_ltp = lambda token, seg: 68.0
    mgr.manage_open_trades()
    assert trade["stop_price"] == 65.0

    # 7. Price hits SL (65.0) -> SL fills and position is closed
    client.orders[initial_stop_id]["filled_quantity"] = 195
    client.orders[initial_stop_id]["status"] = OrderStatus.COMPLETE
    mgr.manage_open_trades()
    assert "NIFTY_23700_CE" not in mgr.managed_trades


def test_trailing_stop_with_single_lot():
    """
    Ensures that 1-lot trades also trail dynamically and do NOT close prematurely at T1.
    """
    client = FakeKotakClient()
    mgr = OrderManager(kotak_client=client, default_lots=1, risk_percentage=None, account_id="test_single_lot")
    mgr.managed_trades = {}

    sig = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=24500,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=100.0,
        trigger_price=100.0,
        stop_loss=80.0,
        targets=[120.0, 140.0, 170.0],
        raw_message="NIFTY 24500 CE ABV 100 TGT 120/140/170 SL 80"
    )

    res = mgr.process_signal(sig)
    entry_id = res["order_id"]
    client.orders[entry_id]["status"] = OrderStatus.COMPLETE

    mgr.manage_open_trades()
    trade = mgr.managed_trades["NIFTY_24500_CE"]
    stop_id = trade["stop_order_id"]
    assert trade["open_quantity"] == 65  # 1 lot
    assert trade["targets"] == [120.0, 140.0, 170.0]  # All targets preserved

    # T1 reached -> SL moves to Buy Price (entry_average = 101.0 with 1% buffer)
    client.get_ltp = lambda token, seg: 125.0
    mgr.manage_open_trades()
    assert trade["open_quantity"] == 65  # NOT closed!
    assert trade["stop_price"] == trade["entry_average"]

    # T2 reached -> SL moves to T1 (120.0)
    client.get_ltp = lambda token, seg: 145.0
    mgr.manage_open_trades()
    assert trade["open_quantity"] == 65
    assert trade["stop_price"] == 120.0

    # T3 reached -> SL moves to T2 (140.0)
    client.get_ltp = lambda token, seg: 172.0
    mgr.manage_open_trades()
    assert trade["open_quantity"] == 65
    assert trade["stop_price"] == 140.0

    # Beyond T3 by (170 - 140 = 30 points) -> LTP reaches 200.0 -> SL moves to 170.0
    client.get_ltp = lambda token, seg: 202.0
    mgr.manage_open_trades()
    assert trade["stop_price"] == 170.0


def test_calculate_trailed_stop_price_unit():
    calc = OrderManager.calculate_trailed_stop_price

    buy = 100.0
    sl = 80.0
    tgts = [120.0, 140.0, 170.0]  # step = 30

    # Price before T1
    assert calc(buy, sl, sl, tgts, 110.0) == 80.0

    # Price at T1
    assert calc(buy, sl, sl, tgts, 120.0) == 100.0

    # Price between T1 and T2
    assert calc(buy, sl, 100.0, tgts, 135.0) == 100.0

    # Price at T2
    assert calc(buy, sl, 100.0, tgts, 140.0) == 120.0

    # Price at T3
    assert calc(buy, sl, 120.0, tgts, 170.0) == 140.0

    # Price between T3 and T3 + step (170 + 30 = 200)
    assert calc(buy, sl, 140.0, tgts, 195.0) == 140.0

    # Price at T3 + step (200)
    assert calc(buy, sl, 140.0, tgts, 200.0) == 170.0

    # Price at T3 + 2*step (230)
    assert calc(buy, sl, 170.0, tgts, 230.0) == 200.0

    # Price pulls back from 235 to 210 -> Ratchet retains highest SL
    assert calc(buy, sl, 200.0, tgts, 210.0) == 200.0


