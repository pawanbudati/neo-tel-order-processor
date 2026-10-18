from src.models import OrderStatus


class FakeKotakClient:
    """Test double for order manager tests; never connects to a broker."""

    def __init__(self):
        self.orders = {}
        self.next_order_id = 1

    def resolve_symbol(self, index, strike, option_type, expiry=None):
        lot_sizes = {
            "NIFTY": 65,
            "BANKNIFTY": 30,
            "FINNIFTY": 60,
            "MIDCPNIFTY": 120,
            "SENSEX": 20,
            "BANKEX": 30,
        }
        idx = index.upper()
        segment = "bse_fo" if idx in ("SENSEX", "BANKEX") else "nse_fo"
        return {
            "trading_symbol": f"{idx}26OCT{int(strike)}{option_type.upper()}",
            "exchange_segment": segment,
            "lot_size": lot_sizes[idx],
        }

    def place_order(self, order):
        order_id = str(self.next_order_id)
        self.next_order_id += 1
        self.orders[order_id] = {"status": OrderStatus.TRIGGER_PENDING, "placement": order}
        return {
            "success": True,
            "order_id": order_id,
            "data": {"transaction_type": "B" if order.transaction_type.upper() in ("B", "BUY") else "S"},
        }

    def modify_order(self, order_id, price, order_type, quantity, trigger_price=0.0, validity="DAY"):
        if order_id not in self.orders:
            return {"success": False, "error": "Unknown order"}
        return {"success": True, "order_id": order_id}

    def cancel_order(self, order_id):
        if order_id not in self.orders:
            return {"success": False, "error": "Unknown order"}
        self.orders[order_id]["status"] = OrderStatus.CANCELLED
        return {"success": True, "order_id": order_id}

    def get_order_status(self, order_id):
        order = self.orders.get(order_id)
        return order["status"] if order else None

    def get_available_funds(self):
        return 1_000_000.0
