import pytest
from unittest.mock import MagicMock, patch
from src.kotak_client import KotakClient
from src.models import ParsedSignal, SignalType, OrderCondition
from src.order_manager import OrderManager


def test_extract_funds_various_formats():
    # 1. Kotak Neo standard sample
    sample_doc = {
        "Category": "CLIENT_SPECIAL",
        "EntityId": "",
        "BoardLotLimit": "5000",
        "CollateralValue": "38.19",
        "MarginUsed": "18.78",
        "Net": "19.41"
    }
    assert KotakClient._extract_funds_from_limits(sample_doc) == 19.41

    # 2. List of segment objects prioritizing FO
    list_segments = [
        {"segment": "CASH", "availableMargin": "10000.00"},
        {"segment": "FO", "availableMargin": "75000.50"},
    ]
    assert KotakClient._extract_funds_from_limits(list_segments) == 75000.50

    # 3. Dict with 'data' wrapping a list
    wrapped_list = {
        "stat": "Ok",
        "data": [
            {"segment": "ALL", "netAllocated": "1,20,000.00", "availableMargin": "95,000.00"}
        ]
    }
    assert KotakClient._extract_funds_from_limits(wrapped_list) == 95000.00

    # 4. Dict with 'data' wrapping 'limits' list
    wrapped_dict_limits = {
        "success": True,
        "data": {
            "limits": [
                {"segment": "FO", "availableMargin": "50000"}
            ]
        }
    }
    assert KotakClient._extract_funds_from_limits(wrapped_dict_limits) == 50000.0

    # 5. Direct Cash or AvailableMargin
    assert KotakClient._extract_funds_from_limits({"AvailableMargin": 32000.0}) == 32000.0
    assert KotakClient._extract_funds_from_limits({"Cash": "0.00"}) == 0.0

    # 6. Unrecognized or error formats return None
    assert KotakClient._extract_funds_from_limits(None) is None
    assert KotakClient._extract_funds_from_limits({}) is None
    assert KotakClient._extract_funds_from_limits({"Error Message": "Complete 2fa"}) is None


def test_is_session_expired():
    assert KotakClient._is_session_expired({"Error Message": "Complete the 2fa process before accessing this application"}) is True
    assert KotakClient._is_session_expired({"error": "Unauthorized session token"}) is True
    assert KotakClient._is_session_expired({"stat": "Not_Ok", "errMsg": "Token expired"}) is True
    assert KotakClient._is_session_expired({"stat": "Ok", "Net": "1000"}) is False


def test_get_available_funds_reauthenticates_on_expired_session():
    client = KotakClient.__new__(KotakClient)
    client.account_id = "default"
    client.is_authenticated = True
    client.client = MagicMock()

    # First call returns 2FA error, reauth happens, second call returns limits with funds
    expired_resp = {"Error Message": "Complete the 2fa process before accessing this application"}
    valid_resp = {"Net": "45000.00"}
    client.client.limits.side_effect = [expired_resp, valid_resp]

    reauth_mock = MagicMock()
    client._init_live_client = reauth_mock

    funds = client.get_available_funds()
    assert funds == 45000.0
    assert reauth_mock.call_count == 1
    assert client.client.limits.call_count == 2


def test_order_manager_fallback_when_funds_fail():
    """
    Verifies that if Kotak limits API fails or raises RuntimeError,
    OrderManager logs the error and safely falls back to default_lots
    so the trade is placed instead of crashing.
    """
    mock_kotak = MagicMock()
    mock_kotak.get_available_funds.side_effect = RuntimeError("Kotak API 502 Bad Gateway")
    mock_kotak.resolve_symbol.return_value = {
        "trading_symbol": "NIFTY26OCT22500CE",
        "exchange_segment": "nse_fo",
        "lot_size": 65,
        "instrument_token": "12345"
    }
    mock_kotak.get_ltp.return_value = 50.0
    mock_kotak.place_order.return_value = {"success": True, "order_id": "1001"}

    mgr = OrderManager(
        kotak_client=mock_kotak,
        risk_percentage=10.0,
        capital=None,
        default_lots=2
    )

    sig = ParsedSignal(
        is_trading_signal=True,
        signal_type=SignalType.NEW_ORDER,
        action="BUY",
        index="NIFTY",
        strike_price=22500.0,
        option_type="CE",
        order_condition=OrderCondition.ABOVE,
        entry_price=55.0,
        trigger_price=55.0,
        stop_loss=45.0,
        raw_message="buy nifty 22500 ce above 55 sl 45"
    )

    res = mgr.process_signal(sig)
    assert res["status"] == "placed"
    assert res["order_id"] == "1001"
    # Fallback to default_lots (2 lots -> 2 * 65 = 130 qty)
    assert res["quantity"] == 130
