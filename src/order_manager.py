import math
import uuid
from typing import Dict, Optional, List, Any
from datetime import datetime, timezone
from pathlib import Path

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
from src.trade_state_store import TradeStateStore

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
        state_path = Path(settings.active_trades_path)
        if account_id != "default":
            state_path = state_path.with_name(f"{state_path.stem}_{account_id}{state_path.suffix}")
        self.trade_store = TradeStateStore(str(state_path))
        self.managed_trades: Dict[str, Dict[str, Any]] = self.trade_store.load()
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
        try:
            sym_info = None
            if signal.index and signal.strike_price:
                sym_info = self.kotak.resolve_symbol(
                    index=signal.index,
                    strike=signal.strike_price,
                    option_type=signal.option_type or "CE",
                    expiry=signal.target_expiry,
                )
            new_trigger, new_price, new_order_type = self._calculate_prices(signal, sym_info)
        except Exception as exc:
            logger.error(f"Cannot safely calculate revised order prices: {exc}")
            return {"status": "modification_failed", "error": str(exc)}

        # Some brokers do not allow changing a pending SL order into a market order.
        # Cancel it first, then submit a fresh order using a new live quote decision.
        if new_order_type == "MKT" and target_order.order_type != "MKT":
            cancel_resp = self.kotak.cancel_order(target_order.order_id)
            if not cancel_resp.get("success"):
                return {"status": "modification_failed", "error": cancel_resp.get("error", "Could not cancel pending order")}
            self._remove_from_pending(target_order)
            return self._handle_new_order(signal)

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
        try:
            trigger_price, price, order_type = self._calculate_prices(signal, sym_info)
        except Exception as exc:
            logger.error(f"Cannot determine safe order prices: {exc}")
            return {"status": "placement_failed", "error": str(exc)}

        # Calculate quantity dynamically based on resolved scrip lot size and risk percentage
        lot_size = sym_info["lot_size"]
        quantity = self.calculate_order_quantity(signal=signal, entry_price=price, lot_size=lot_size)
        if quantity <= 0:
            return {
                "status": "placement_failed",
                "error": "Available risk/capital cannot support even 1 lot; no order was sent",
            }
        lots_to_trade = quantity // lot_size
        if signal.targets:
            signal.targets = signal.targets[:lots_to_trade]
            signal.target = signal.targets[0] if signal.targets else None

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
            if signal.stop_loss and transaction_type in ("BUY", "B"):
                target_count = min(len(signal.targets), quantity // lot_size)
                if target_count:
                    lots_each, extra = divmod(quantity // lot_size, target_count)
                    target_quantities = [(lots_each + (1 if i < extra else 0)) * lot_size for i in range(target_count)]
                else:
                    target_quantities = []
                self.managed_trades[signal.contract_key()] = {
                    "contract_key": signal.contract_key(),
                    "entry_order_id": order_id,
                    "trading_symbol": trading_symbol,
                    "exchange_segment": exchange_segment,
                    "instrument_token": str(sym_info.get("instrument_token", "")),
                    "product": self.default_product_type,
                    "entry_quantity": quantity,
                    "entry_filled": 0,
                    "entry_average": 0.0,
                    "entry_reference": float(signal.entry_price or signal.trigger_price or 0.0),
                    "stop_loss": float(signal.stop_loss),
                    "targets": [float(x) for x in signal.targets[:target_count]],
                    "target_quantities": target_quantities,
                    "target_index": 0,
                    "open_quantity": 0,
                    "stop_order_id": None,
                    "stop_filled": 0,
                    "stop_quantity": 0,
                    "stop_price": float(signal.stop_loss),
                    "pending_exit_id": None,
                    "pending_exit_quantity": 0,
                    "pending_exit_filled": 0,
                    "pending_exit_target": None,
                }
                self._save_managed_trades()
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

    def _save_managed_trades(self) -> None:
        self.trade_store.save(self.managed_trades)

    def _place_protective_stop(self, trade: Dict[str, Any], quantity: int) -> Optional[str]:
        if quantity <= 0:
            return None
        stop_price = float(trade["stop_price"])
        stop_limit = round_tick(stop_price * (1.0 - settings.sl_limit_buffer_percent / 100.0))
        placement = OrderPlacement(
            trading_symbol=trade["trading_symbol"],
            exchange_segment=trade["exchange_segment"],
            transaction_type="SELL",
            order_type="SL",
            quantity=quantity,
            price=stop_limit,
            trigger_price=stop_price,
            product=trade["product"],
            tag=f"TS{uuid.uuid4().hex[:8].upper()}",
            contract_key=trade["contract_key"],
        )
        response = self.kotak.place_order(placement)
        if not response.get("success"):
            logger.error(f"Could not place protective stop for {trade['contract_key']}: {response.get('error')}")
            return None
        trade["stop_quantity"] = int(quantity)
        return str(response["order_id"])

    def _modify_protective_stop(self, trade: Dict[str, Any], quantity: int, stop_price: float) -> bool:
        order_id = trade.get("stop_order_id")
        if not order_id:
            new_id = self._place_protective_stop(trade, quantity)
            if not new_id:
                return False
            trade["stop_order_id"] = new_id
        else:
            response = self.kotak.modify_order(
                order_id=order_id,
                price=round_tick(stop_price * (1.0 - settings.sl_limit_buffer_percent / 100.0)),
                order_type="SL",
                quantity=int(trade.get("stop_filled", 0)) + int(quantity),
                trigger_price=stop_price,
            )
            if not response.get("success"):
                logger.error(f"Could not update protective stop {order_id}: {response.get('error')}")
                return False
        trade["stop_price"] = float(stop_price)
        trade["stop_quantity"] = int(quantity)
        return True

    def manage_open_trades(self) -> None:
        """Reconcile fills and advance staged target/stop logic for open signal trades."""
        dirty = False
        for key, trade in list(self.managed_trades.items()):
            try:
                entry = self.kotak.get_order_details(trade["entry_order_id"])
                entry_filled = min(int(entry["filled_quantity"]), int(trade["entry_quantity"]))
                if entry_filled > int(trade["entry_filled"]):
                    delta = entry_filled - int(trade["entry_filled"])
                    trade["entry_filled"] = entry_filled
                    trade["open_quantity"] = int(trade["open_quantity"]) + delta
                    if entry.get("average_price", 0) > 0:
                        trade["entry_average"] = float(entry["average_price"])
                    dirty = True

                if trade["open_quantity"] <= 0:
                    if entry["status"] in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.COMPLETE):
                        self.managed_trades.pop(key, None)
                        dirty = True
                    continue

                # Ensure filled entry quantity is protected before evaluating targets.
                if not trade.get("stop_order_id"):
                    pending_remaining = int(trade.get("pending_exit_quantity", 0)) - int(trade.get("pending_exit_filled", 0))
                    protect_quantity = max(0, int(trade["open_quantity"]) - pending_remaining)
                    if protect_quantity > 0:
                        stop_id = self._place_protective_stop(trade, protect_quantity)
                        if stop_id:
                            trade["stop_order_id"] = stop_id
                            dirty = True
                        else:
                            continue
                else:
                    stop = self.kotak.get_order_details(trade["stop_order_id"])
                    stop_filled = int(stop["filled_quantity"])
                    if stop_filled > int(trade.get("stop_filled", 0)):
                        trade["open_quantity"] = max(0, int(trade["open_quantity"]) - (stop_filled - int(trade.get("stop_filled", 0))))
                        trade["stop_filled"] = stop_filled
                        dirty = True
                    if trade["open_quantity"] <= 0:
                        pending_id = trade.get("pending_exit_id")
                        if pending_id:
                            self.kotak.cancel_order(pending_id)
                        self.managed_trades.pop(key, None)
                        dirty = True
                        continue
                    if stop["status"] == OrderStatus.COMPLETE:
                        pending_id = trade.get("pending_exit_id")
                        if pending_id:
                            self.kotak.cancel_order(pending_id)
                            trade["pending_exit_id"] = None
                            trade["pending_exit_quantity"] = 0
                            trade["pending_exit_filled"] = 0
                            trade["pending_exit_target"] = None
                        trade["stop_order_id"] = None
                        trade["stop_filled"] = 0
                        replacement_id = self._place_protective_stop(trade, int(trade["open_quantity"]))
                        if not replacement_id:
                            logger.error(f"Position {key} remains open after stop fill and replacement failed")
                            continue
                        trade["stop_order_id"] = replacement_id
                        dirty = True
                    elif stop["status"] not in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                        pending_remaining = int(trade.get("pending_exit_quantity", 0)) - int(trade.get("pending_exit_filled", 0))
                        expected_stop_quantity = max(0, int(trade["open_quantity"]) - pending_remaining)
                        if expected_stop_quantity != int(trade.get("stop_quantity", 0)):
                            if not self._modify_protective_stop(trade, expected_stop_quantity, float(trade["stop_price"])):
                                continue
                            dirty = True
                    if stop["status"] in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                        trade["stop_order_id"] = None
                        pending_remaining = int(trade.get("pending_exit_quantity", 0)) - int(trade.get("pending_exit_filled", 0))
                        replacement_quantity = max(0, int(trade["open_quantity"]) - pending_remaining)
                        replacement_id = self._place_protective_stop(trade, replacement_quantity) if replacement_quantity else None
                        if replacement_quantity and not replacement_id:
                            logger.error(f"Position {key} is open without a working stop; reconciliation will retry")
                            continue
                        trade["stop_order_id"] = replacement_id
                        dirty = True

                pending_id = trade.get("pending_exit_id")
                if pending_id:
                    exit_order = self.kotak.get_order_details(pending_id)
                    filled = int(exit_order["filled_quantity"])
                    delta = max(0, filled - int(trade.get("pending_exit_filled", 0)))
                    if delta:
                        trade["open_quantity"] = max(0, int(trade["open_quantity"]) - delta)
                        trade["pending_exit_filled"] = filled
                        pending_target = int(trade["pending_exit_target"] or 0)
                        trade["target_quantities"][pending_target] = max(
                            0, int(trade["target_quantities"][pending_target]) - delta
                        )
                        dirty = True
                    if exit_order["status"] in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                        target_idx = int(trade["pending_exit_target"] or 0)
                        if int(trade["target_quantities"][target_idx]) == 0:
                            trade["target_index"] = target_idx + 1
                        # Reconcile stop quantity with the true position after any partial fill.
                        if trade["open_quantity"] > 0:
                            covered_quantity = int(trade["open_quantity"])
                            self._modify_protective_stop(trade, covered_quantity, float(trade["stop_price"]))
                        trade["pending_exit_id"] = None
                        trade["pending_exit_quantity"] = 0
                        trade["pending_exit_filled"] = 0
                        trade["pending_exit_target"] = None
                        dirty = True
                    continue

                target_idx = int(trade["target_index"])
                if target_idx >= len(trade["targets"]):
                    continue
                ltp = self.kotak.get_ltp(trade["instrument_token"], trade["exchange_segment"])
                target_price = float(trade["targets"][target_idx])
                if ltp < target_price:
                    continue

                exit_quantity = min(int(trade["target_quantities"][target_idx]), int(trade["open_quantity"]))
                remaining_after_exit = int(trade["open_quantity"]) - exit_quantity
                target_stop = (
                    float(trade["entry_average"] or trade["entry_reference"])
                    if target_idx == 0
                    else float(trade["targets"][target_idx - 1])
                )
                if remaining_after_exit > 0:
                    if not self._modify_protective_stop(trade, remaining_after_exit, target_stop):
                        continue
                    dirty = True
                else:
                    cancel_resp = self.kotak.cancel_order(trade["stop_order_id"])
                    if not cancel_resp.get("success"):
                        logger.error(f"Cannot cancel stop before final target exit for {key}; skipping exit")
                        continue
                    trade["stop_order_id"] = None

                exit_response = self.kotak.place_order(OrderPlacement(
                    trading_symbol=trade["trading_symbol"],
                    exchange_segment=trade["exchange_segment"],
                    transaction_type="SELL",
                    order_type="MKT",
                    quantity=exit_quantity,
                    price=0.0,
                    product=trade["product"],
                    tag=f"TX{uuid.uuid4().hex[:8].upper()}",
                    contract_key=key,
                ))
                if not exit_response.get("success"):
                    # Restore the stop quantity if the target exit was rejected.
                    self._modify_protective_stop(trade, int(trade["open_quantity"]), float(trade["stop_price"]))
                    dirty = True
                    continue
                trade["pending_exit_id"] = str(exit_response["order_id"])
                trade["pending_exit_quantity"] = exit_quantity
                trade["pending_exit_filled"] = 0
                trade["pending_exit_target"] = target_idx
                dirty = True
                logger.info(f"Target {target_idx + 1} touched for {key}; exit order {exit_response['order_id']} for {exit_quantity} units")
            except Exception as exc:
                logger.error(f"Trade reconciliation failed for {key}: {exc}", exc_info=True)
        if dirty:
            self._save_managed_trades()

    def _calculate_prices(self, signal: ParsedSignal, sym_info: Optional[Dict[str, Any]] = None):
        """
        Determines trigger_price, limit_price, and order_type (SL vs L vs MKT).
        """
        buffer_pct = settings.sl_limit_buffer_percent / 100.0
        cond = signal.order_condition or OrderCondition.ABOVE
        action = (signal.action or "BUY").upper()

        if action in ("BUY", "B") and cond == OrderCondition.ABOVE:
            trigger = float(signal.trigger_price or signal.entry_price or 0.0)
            if trigger <= 0:
                raise ValueError("Buy-above signal is missing a positive trigger price")
            if not sym_info:
                raise RuntimeError("Cannot check current price without resolved contract details")
            ltp = self.kotak.get_ltp(
                instrument_token=sym_info.get("instrument_token", ""),
                exchange_segment=sym_info["exchange_segment"],
            )
            if ltp >= trigger:
                logger.info(f"Buy-above trigger already crossed (LTP={ltp}, trigger={trigger}); using market order")
                return 0.0, 0.0, "MKT"
            limit_price = round_tick(trigger * (1.0 + buffer_pct))
            logger.info(f"Buy-above trigger not reached (LTP={ltp}, trigger={trigger}); using SL-LMT")
            return trigger, limit_price, "SL"

        if action in ("BUY", "B") and cond == OrderCondition.BELOW:
            limit_price = round_tick(float(signal.entry_price or signal.trigger_price or 0.0))
            if limit_price <= 0:
                raise ValueError("Buy-below signal is missing a positive limit price")
            return 0.0, limit_price, "L"

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
            lots = self._round_lots_to_multiple_of_three(self.default_lots)
            logger.debug(f"{acc_tag}Position sizing using default lots: {lots} ({lots * lot_size} qty)")
            return lots * lot_size

        # Resolve capital (from self.capital or live Kotak funds)
        capital = self.capital
        if capital is None or capital <= 0:
            try:
                capital = self.kotak.get_available_funds()
            except Exception as e:
                fallback_lots = self._round_lots_to_multiple_of_three(self.default_lots)
                logger.error(
                    f"{acc_tag}Failed to retrieve live Kotak available funds for risk sizing ({e}). "
                    f"Falling back to default {fallback_lots} lot(s) ({fallback_lots * lot_size} qty) "
                    f"to prevent order placement failure."
                )
                return fallback_lots * lot_size

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
                f"Falling back to default {self._round_lots_to_multiple_of_three(self.default_lots)} lot(s)."
            )
            return self._round_lots_to_multiple_of_three(self.default_lots) * lot_size

        max_risk_amount = capital * (self.risk_percentage / 100.0)
        risk_per_lot = points_at_risk * lot_size

        lots_by_risk = int(max_risk_amount // risk_per_lot)

        premium_per_lot = effective_entry * lot_size
        lots_by_capital = int(capital // premium_per_lot) if premium_per_lot > 0 else 0

        if lots_by_capital < 1:
            logger.warning(
                f"{acc_tag}Available capital (₹{capital:.2f}) cannot afford even 1 lot "
                f"(cost: ₹{premium_per_lot:.2f}); refusing the trade."
            )
            return 0
        elif lots_by_risk < 1:
            logger.info(
                f"{acc_tag}Risk budget cannot support even 1 lot "
                f"(max risk ₹{max_risk_amount:.2f}, risk per lot ₹{risk_per_lot:.2f}); refusing the trade."
            )
            return 0
        else:
            affordable_lots = min(lots_by_risk, lots_by_capital)
            optimal_lots = affordable_lots if affordable_lots < 3 else (affordable_lots // 3) * 3

        total_quantity = optimal_lots * lot_size

        logger.info(
            f"[bold cyan]{acc_tag}Risk-Based Position Sizing:[/bold cyan] "
            f"Capital=₹{capital:.2f} | Risk={self.risk_percentage}% (Max Risk: ₹{max_risk_amount:.2f}) | "
            f"Entry={effective_entry} | SL={signal.stop_loss} | Pts Risk={points_at_risk:.2f} | "
            f"Risk/Lot=₹{risk_per_lot:.2f} | LotsByRisk={lots_by_risk} | LotsByCapital={lots_by_capital} -> "
            f"[bold green]Selected: {optimal_lots} lot(s) ({total_quantity} qty)[/bold green]"
        )
        return total_quantity

    @staticmethod
    def _round_lots_to_multiple_of_three(lots: int) -> int:
        """Keep 1-2 lot requests; round larger requests down to a multiple of three."""
        requested = max(1, int(lots))
        return requested if requested < 3 else (requested // 3) * 3

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


