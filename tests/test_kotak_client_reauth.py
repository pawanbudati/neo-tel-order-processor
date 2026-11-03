from unittest.mock import MagicMock
from src.kotak_client import KotakClient
from src.models import OrderPlacement


def test_place_order_auto_reauthenticates_on_expired_session():
    mock_neo = MagicMock()
    client = KotakClient.__new__(KotakClient)
    client.client = mock_neo
    client.is_authenticated = True
    client.account_id = "default"

    # First attempt: invalid session token (stCode 100022)
    # Second attempt: success with order id 261103000141445
    expired_resp = {'stCode': 100022, 'errMsg': 'invalid session token', 'stat': 'Not_Ok'}
    success_resp = {'nOrdNo': '261103000141445', 'stat': 'Ok', 'stCode': 200}
    mock_neo.place_order.side_effect = [expired_resp, success_resp]

    reauth_mock = MagicMock()
    client._init_live_client = reauth_mock

    order = OrderPlacement(
        trading_symbol="NIFTY26N0323600CE",
        exchange_segment="nse_fo",
        transaction_type="BUY",
        order_type="MKT",
        quantity=65,
        price=0.0,
        trigger_price=0.0,
        product="MIS",
        tag="TGBOTTEST"
    )

    res = client.place_order(order)
    assert res["success"] is True
    assert res["order_id"] == "261103000141445"
    assert reauth_mock.call_count == 1
    assert mock_neo.place_order.call_count == 2
