import math
import uuid
from typing import Dict, Optional, List, Any
from datetime import datetime, timezone

from config.settings import settings
from src.logger import logger
from src.models import (
    ParsedSignal,
    SignalType,
    OrderCondition,
    OrderPlacement,
    ActiveOrder,
    OrderStatus
)
from src.kotak_client import KotakClient

def round_tick(price: float, tick_size: float = 0.05) -> float:
    """Rounds price to nearest exchange tick size (0.05 for NSE/BSE options)."""
    return round(round(price / tick_size) * tick_size, 2)

class OrderManager:
    """
    Manages order lifecycle, tracking pending limit/SL orders,
    modifying orders when price revisions arrive, and cancelling
    pending orders on ignore/cancel instructions.
    """

    def __init__(
        self,
        kotak_client: Optional[KotakClient] = None,
        account_id: str = "default",
        default_lots: Optional[int] = None,
        default_product_type: Optional[str] = None,
        risk_percentage: Optional[float] = None,
        capital: Optional[float] = None
    ):
        self.account_id = account_id
        self.kotak = kotak_client or KotakClient()
        self.default_lots = default_lots if default_lots is not None else settings.default_lots
        self.default_product_type = default_product_type or settings.default_product_type
        self.risk_percentage = risk_percentage if risk_percentage is not None else settings.risk_percentage
        self.capital = capital if capital is not None else settings.default_capital
        # Key: contract_key (e.g., 'NIFTY_22500_CE') -> ActiveOrder
        self.pending_orders_by_contract: Dict[str, ActiveOrder] = {}
        # Key: order_id -> ActiveOrder
        self.orders_by_id: Dict[str, ActiveOrder] = {}
        self.last_contract_key: Optional[str] = None
        self.last_order_id: Optional[str] = None

    def process_signal(self, signal: ParsedSignal) -> Dict[str, Any]:
        """
        Main entry point to process a parsed signal from Gemini.
        Dispatches to cancellation, modification, or new order placement.
        """
        if not signal.is_trading_signal:
            logger.debug(f"Non-trading signal ignored: {signal.reasoning}")
            return {"status": "ignored", "reason": "Not a trading signal"}

        logger.info(f"[bold blue]Processing Signal:[/bold blue] {signal.signal_type.value} for {signal.contract_key()}")

        # Case 1: Cancellation, Ignore, or Exit Signal
        if (
            signal.signal_type in (SignalType.CANCEL_ORDER, SignalType.IGNORE_SIGNAL, SignalType.CLOSE_POSITION)
            or (signal.action and signal.action.upper() in ("CANCEL", "IGNORE", "EXIT"))
        ):
            return self._handle_cancellation(signal)

        # Case 2: Modify Order
        if signal.signal_type == SignalType.MODIFY_ORDER or signal.action == "MODIFY":
            return self._handle_modification(signal)

        # Case 3: New Order (or Price Update that comes as NEW_ORDER for an already pending contract)
        if signal.signal_type == SignalType.NEW_ORDER or signal.action in ("BUY", "SELL"):
            contract_key = signal.contract_key()
            if contract_key in self.pending_orders_by_contract:
                existing = self.pending_orders_by_contract[contract_key]
                logger.info(
                    f"Found existing pending order {existing.order_id} for exact contract {contract_key}. "
                    f"Handling update/modification."
                )
                return self._handle_modification(signal)
            else:
                # User Rule: When a new call for the same index arrives (e.g. previous NIFTY 22500, new NIFTY 22350),
                # cancel the existing pending order for that index, and place the new one.
                # Orders for DIFFERENT indices (e.g. BANKEX vs NIFTY) are preserved!
                self._cancel_existing_same_index_pending_orders(signal)
                return self._handle_new_order(signal)

        return {"status": "unhandled", "signal_type": signal.signal_type.value}

    def _handle_cancellation(self, signal: ParsedSignal) -> Dict[str, Any]:
        """
        Cancels pending order(s) corresponding to the signal or the latest pending order.
        """
        target_order: Optional[ActiveOrder] = None
        contract_key = signal.contract_key()

        # 1. Check exact contract match
        if contract_key != "UNKNOWN_0_CE" and contract_key in self.pending_orders_by_contract:
            target_order = self.pending_orders_by_contract[contract_key]
        # 2. Check by index and strike match if option type was omitted
        elif signal.index and signal.strike_price:
            idx = signal.index.upper()
            strike_str = str(int(signal.strike_price))
            for k, ord_rec in list(self.pending_orders_by_contract.items()):
                if k.startswith(f"{idx}_{strike_str}"):
                    target_order = ord_rec
                    break
        # 3. Check by index only (e.g. "exit nifty" or "cancel nifty")
        elif signal.index:
            idx = signal.index.upper().strip()
            for k, ord_rec in list(self.pending_orders_by_contract.items()):
                if k.startswith(f"{idx}_") or (ord_rec.raw_signal and ord_rec.raw_signal.index and ord_rec.raw_signal.index.upper() == idx):
                    target_order = ord_rec
                    break
        # 4. Fallback: Cancel the most recently placed pending order
        elif self.last_order_id and self.last_order_id in self.orders_by_id:
            target_order = self.orders_by_id[self.last_order_id]

        if not target_order:
            logger.warning(
                f"[yellow]Ignore/Cancel signal received for '{signal.raw_message}', "
                f"but no matching active pending order was found.[/yellow]"
            )
            return {"status": "no_pending_order_found", "message": "No matching active pending order found"}

        # Verify current status before cancellation
        current_status = self.kotak.get_order_status(target_order.order_id)
        if current_status == OrderStatus.COMPLETE:
            logger.warning(
                f"[yellow]Order {target_order.order_id} for {target_order.trading_symbol} is already COMPLETE. Cannot cancel.[/yellow]"
            )
            self._remove_from_pending(target_order)
            return {"status": "already_executed", "order_id": target_order.order_id}

        # Issue cancellation to Kotak Neo
        logger.info(
            f"[bold red]Cancelling pending order:[/bold red] ID={target_order.order_id} | "
            f"Contract={target_order.contract_key} | Symbol={target_order.trading_symbol}"
        )
        cancel_resp = self.kotak.cancel_order(target_order.order_id)
        if cancel_resp.get("success"):
            target_order.status = OrderStatus.CANCELLED
            target_order.last_updated_at = datetime.now(timezone.utc)
            self._remove_from_pending(target_order)
            logger.info(f"[bold green]Successfully cancelled order {target_order.order_id}[/bold green]")
            return {"status": "cancelled", "order_id": target_order.order_id}
        else:
            return {"status": "failed_to_cancel", "error": cancel_resp.get("error")}

    def _handle_modification(self, signal: ParsedSignal) -> Dict[str, Any]:
        """
        Modifies price/trigger of an existing pending order or replaces it.
        """
        contract_key = signal.contract_key()
        target_order = self.pending_orders_by_contract.get(contract_key)

        if not target_order and self.last_order_id:
            target_order = self.orders_by_id.get(self.last_order_id)

        if not target_order:
            logger.info(f"No existing order found to modify for {contract_key}. Placing as new order.")
            return self._handle_new_order(signal)

        # Check if already executed or closed
        current_status = self.kotak.get_order_status(target_order.order_id)
        if current_status == OrderStatus.COMPLETE:
            logger.warning(
                f"[yellow]Existing order {target_order.order_id} is already filled. Cannot modify pending order.[/yellow]"
            )
            self._remove_from_pending(target_order)
            return {"status": "already_executed", "order_id": target_order.order_id}

        if current_status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            logger.warning(
                f"[yellow]Existing order {target_order.order_id} is in status {current_status.value}. Placing new order instead of modifying.[/yellow]"
            )
            self._remove_from_pending(target_order)
            return self._handle_new_order(signal)

        # Calculate new price and trigger
        new_trigger, new_price, new_order_type = self._calculate_prices(signal)

        logger.info(
            f"[bold yellow]Modifying pending order:[/bold yellow] ID={target_order.order_id} | "
            f"Old Trigger={target_order.trigger_price} Old Price={target_order.price} -> "
            f"New Trigger={new_trigger} New Price={new_price} Type={new_order_type}"
        )

        mod_resp = self.kotak.modify_order(
            order_id=target_order.order_id,
            price=new_price,
            order_type=new_order_type,
            quantity=target_order.quantity,
            trigger_price=new_trigger
        )

        if mod_resp.get("success"):
            target_order.price = new_price
            target_order.trigger_price = new_trigger
            target_order.order_type = new_order_type
            target_order.last_updated_at = datetime.now(timezone.utc)
            return {"status": "modified", "order_id": target_order.order_id}
        else:
            # If modification rejected, cancel and place new order
            logger.warning("Order modification failed, cancelling old and placing new...")
            self.kotak.cancel_order(target_order.order_id)
            self._remove_from_pending(target_order)
            return self._handle_new_order(signal)

    def _handle_new_order(self, signal: ParsedSignal) -> Dict[str, Any]:
        """
        Prepares and places a new order in Kotak Neo.
        """
        if not signal.index or not signal.strike_price:
            logger.error("Cannot place order: index or strike_price missing from signal.")
            return {"status": "failed", "error": "Missing index or strike"}

        option_type = signal.option_type or "CE"
        index = signal.index.upper()
        strike = float(signal.strike_price)
        transaction_type = (signal.action or "BUY").upper()

        # Resolve symbol first to retrieve exact exchange contract details and scrip lot size
        sym_info = self.kotak.resolve_symbol(
            index=index,
            strike=strike,
            option_type=option_type,
            expiry=signal.target_expiry
        )
        trading_symbol = sym_info["trading_symbol"]
        exchange_segment = sym_info["exchange_segment"]

        # Calculate price & trigger first to determine effective entry price for sizing
        trigger_price, price, order_type = self._calculate_prices(signal)

        # Calculate quantity dynamically based on resolved scrip lot size and risk percentage
        lot_size = sym_info["lot_size"]
        quantity = self.calculate_order_quantity(signal=signal, entry_price=price, lot_size=lot_size)

        # Generate unique tag to satisfy Kotak Neo OMS unique Client Order ID constraint
        unique_tag = f"TG{uuid.uuid4().hex[:8].upper()}"

        placement = OrderPlacement(
            trading_symbol=trading_symbol,
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            order_type=order_type,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            product=self.default_product_type,
            tag=unique_tag,
            contract_key=signal.contract_key()
        )

        place_resp = self.kotak.place_order(placement)
        if place_resp.get("success") and place_resp.get("order_id") and str(place_resp.get("order_id")).strip().lower() != "none":
            order_id = str(place_resp.get("order_id"))
            active_order = ActiveOrder(
                order_id=order_id,
                trading_symbol=trading_symbol,
                contract_key=signal.contract_key(),
                exchange_segment=exchange_segment,
                transaction_type=transaction_type,
                order_type=order_type,
                quantity=quantity,
                price=price,
                trigger_price=trigger_price,
                product=self.default_product_type,
                status=OrderStatus.TRIGGER_PENDING if order_type in ("SL", "SL-M") else OrderStatus.OPEN,
                raw_signal=signal
            )

            self.pending_orders_by_contract[signal.contract_key()] = active_order
            self.orders_by_id[order_id] = active_order
            self.last_contract_key = signal.contract_key()
            self.last_order_id = order_id

            logger.info(
                f"[bold green]New order tracked successfully:[/bold green] ID={order_id} | "
                f"Symbol={trading_symbol} | Qty={quantity} | Trigger={trigger_price} | Price={price}"
            )
            return {"status": "placed", "order_id": order_id, "trading_symbol": trading_symbol, "quantity": quantity}
        else:
            err = place_resp.get("error", "Order placement failed without error detail")
            logger.error(f"[bold red]Order placement failed:[/bold red] {err}")
            return {"status": "placement_failed", "error": err}

    def _calculate_prices(self, signal: ParsedSignal):
        """
        Determines trigger_price, limit_price, and order_type (SL vs L vs MKT).
        """
        buffer_pct = settings.sl_limit_buffer_percent / 100.0
        cond = signal.order_condition or OrderCondition.ABOVE

        if cond == OrderCondition.ABOVE:
            trigger = float(signal.trigger_price or signal.entry_price or 0.0)
            # For buy above, limit price is slightly higher than trigger to ensure execution
            limit_price = round_tick(trigger * (1.0 + buffer_pct))
            return trigger, limit_price, "SL"

        elif cond == OrderCondition.BELOW:
            trigger = float(signal.trigger_price or signal.entry_price or 0.0)
            # For buy below, limit price is slightly lower
            limit_price = round_tick(trigger * (1.0 - buffer_pct))
            return trigger, limit_price, "SL"

        elif cond in (OrderCondition.AT, OrderCondition.LIMIT):
            price = round_tick(float(signal.entry_price or 0.0))
            return 0.0, price, "L"

        elif cond == OrderCondition.MARKET:
            return 0.0, 0.0, "MKT"

        else:
            # Default to Limit order
            price = round_tick(float(signal.entry_price or 0.0))
            return 0.0, price, "L"

    def calculate_order_quantity(self, signal: ParsedSignal, entry_price: float, lot_size: int) -> int:
        """
        Calculates order quantity based on risk percentage of capital if stop loss is defined,
        otherwise falls back to default lot size.
        
        Formula:
          - max_risk_amount = capital * (risk_percentage / 100.0)
          - points_at_risk = abs(entry_price - stop_loss)
          - risk_per_lot = points_at_risk * lot_size
          - lots_by_risk = int(max_risk_amount // risk_per_lot)
          - lots_by_capital = int(capital // (entry_price * lot_size))
          - optimal_lots = min(lots_by_risk, lots_by_capital)
        """
        acc_tag = f"[{self.account_id}] " if self.account_id != "default" else ""

        # Fallback to default lot size if risk_percentage or SL is not specified
        if not self.risk_percentage or signal.stop_loss is None or signal.stop_loss <= 0:
            lots = max(1, self.default_lots)
            logger.debug(f"{acc_tag}Position sizing using default lots: {lots} ({lots * lot_size} qty)")
            return lots * lot_size

        # Resolve capital (from self.capital or live Kotak funds)
        capital = self.capital
        if capital is None or capital <= 0:
            capital = self.kotak.get_available_funds()

        effective_entry = float(signal.entry_price or entry_price or 0.0)
        if effective_entry <= 0:
            effective_entry = entry_price

        action = (signal.action or "BUY").upper()
        if action in ("BUY", "B"):
            points_at_risk = effective_entry - float(signal.stop_loss)
        else:
            points_at_risk = float(signal.stop_loss) - effective_entry

        if points_at_risk <= 0:
            logger.warning(
                f"{acc_tag}Invalid SL for position sizing (entry={effective_entry}, SL={signal.stop_loss}). "
                f"Falling back to default {self.default_lots} lot(s)."
            )
            return max(1, self.default_lots) * lot_size

        max_risk_amount = capital * (self.risk_percentage / 100.0)
        risk_per_lot = points_at_risk * lot_size

        lots_by_risk = int(max_risk_amount // risk_per_lot)

        premium_per_lot = effective_entry * lot_size
        lots_by_capital = int(capital // premium_per_lot) if premium_per_lot > 0 else 0

        if lots_by_capital < 1:
            logger.warning(
                f"{acc_tag}Available capital (₹{capital:.2f}) cannot afford 1 lot (cost: ₹{premium_per_lot:.2f}). "
                f"Setting to 1 lot."
            )
            optimal_lots = 1
        elif lots_by_risk < 1:
            logger.info(
                f"{acc_tag}Risk calculation yielded < 1 lot (Max risk ₹{max_risk_amount:.2f} < Lot risk ₹{risk_per_lot:.2f}). "
                f"Setting to default 1 lot."
            )
            optimal_lots = 1
        else:
            optimal_lots = min(lots_by_risk, lots_by_capital)

        total_quantity = optimal_lots * lot_size

        logger.info(
            f"[bold cyan]{acc_tag}Risk-Based Position Sizing:[/bold cyan] "
            f"Capital=₹{capital:.2f} | Risk={self.risk_percentage}% (Max Risk: ₹{max_risk_amount:.2f}) | "
            f"Entry={effective_entry} | SL={signal.stop_loss} | Pts Risk={points_at_risk:.2f} | "
            f"Risk/Lot=₹{risk_per_lot:.2f} | LotsByRisk={lots_by_risk} | LotsByCapital={lots_by_capital} -> "
            f"[bold green]Selected: {optimal_lots} lot(s) ({total_quantity} qty)[/bold green]"
        )
        return total_quantity

    def _cancel_existing_same_index_pending_orders(self, signal: ParsedSignal):
        """
        Rule: If a new call arrives for the same underlying index (e.g. NIFTY),
        cancels previously existing pending order for that index before placing the new one.
        Orders for DIFFERENT indices (e.g. BANKEX vs NIFTY) are preserved!
        """
        if not signal.index:
            return

        target_index = signal.index.upper().strip()
        orders_to_cancel = []

        for contract_k, ord_rec in list(self.pending_orders_by_contract.items()):
            ord_index = None
            if ord_rec.raw_signal and ord_rec.raw_signal.index:
                ord_index = ord_rec.raw_signal.index.upper().strip()
            elif contract_k:
                ord_index = contract_k.split("_")[0].upper().strip()

            if ord_index == target_index:
                orders_to_cancel.append((contract_k, ord_rec))

        for contract_k, ord_rec in orders_to_cancel:
            curr_status = self.kotak.get_order_status(ord_rec.order_id)
            if curr_status == OrderStatus.COMPLETE:
                logger.info(
                    f"Previous {target_index} order {ord_rec.order_id} ({ord_rec.trading_symbol}) is already COMPLETE. "
                    f"Removing from pending tracking."
                )
                self._remove_from_pending(ord_rec)
            else:
                logger.info(
                    f"[bold yellow]New {target_index} signal received: Cancelling previous pending "
                    f"{target_index} order {ord_rec.order_id} ({ord_rec.trading_symbol}) to replace with new call.[/bold yellow]"
                )
                self.kotak.cancel_order(ord_rec.order_id)
                self._remove_from_pending(ord_rec)
                ord_rec.status = OrderStatus.CANCELLED

    def _remove_from_pending(self, order: ActiveOrder):
        """Removes order from active pending map."""
        if order.contract_key in self.pending_orders_by_contract:
            del self.pending_orders_by_contract[order.contract_key]
        if self.last_order_id == order.order_id:
            self.last_order_id = None
