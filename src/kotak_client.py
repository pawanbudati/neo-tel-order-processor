import uuid
from typing import Dict, Any, List, Optional
import pyotp

from config.settings import settings
from src.logger import logger
from src.models import OrderPlacement, OrderStatus, KotakAccountConfig

try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None

class KotakClient:
    """
    Unified client for Kotak Neo API.
    Connects to Kotak Neo for live order execution only.
    Can be instantiated for a specific account or default settings.
    """

    def __init__(
        self,
        account_config: Optional[KotakAccountConfig] = None
    ):
        self.account_config = account_config
        self.account_id = account_config.account_id if account_config else "default"

        if account_config:
            self.consumer_key = account_config.consumer_key
            self.mobile_number = account_config.mobile_number
            self.ucc = account_config.ucc
            self.totp_key = account_config.totp_key
            self.mpin = account_config.mpin
            self.environment = account_config.environment
            is_configured = bool(
                self.consumer_key
                and self.mobile_number
                and self.ucc
                and self.totp_key
                and self.mpin
                and not any("your_" in str(x).lower() for x in (
                    self.consumer_key, self.mobile_number, self.ucc, self.totp_key, self.mpin
                ))
            )
        else:
            self.consumer_key = settings.kotak_consumer_key
            self.mobile_number = settings.kotak_mobile_number
            self.ucc = settings.kotak_ucc
            self.totp_key = settings.kotak_totp_key
            self.mpin = settings.kotak_mpin
            self.environment = settings.kotak_environment
            is_configured = settings.is_kotak_configured()

        self.client: Optional[NeoAPI] = None
        self.is_authenticated = False

        acc_tag = f"[{self.account_id}] " if self.account_id != "default" else ""
        if not is_configured:
            raise RuntimeError("Kotak credentials are incomplete; live consumer startup aborted")
        if NeoAPI is None:
            raise RuntimeError("kotakneoapi is not installed; live consumer startup aborted")
        self._init_live_client()

    def _init_live_client(self):
        """Initializes and authenticates with Kotak Neo API."""
        acc_tag = f"[{self.account_id}] " if self.account_id != "default" else ""
        try:
            logger.info(f"[cyan]{acc_tag}Authenticating with Kotak Neo API...[/cyan]")
            self.client = NeoAPI(
                consumer_key=self.consumer_key,
                environment=self.environment
            )

            # Generate TOTP
            if not self.totp_key:
                raise ValueError("KOTAK_TOTP_KEY must be configured for live execution")
            totp_code = pyotp.TOTP(self.totp_key.strip()).now()

            # Step 1: TOTP Login
            login_resp = self.client.totp_login(
                mobile_number=self.mobile_number,
                ucc=self.ucc,
                totp=totp_code
            )
            self._raise_if_api_error(login_resp, "TOTP login")
            logger.debug(f"{acc_tag}TOTP Login response: {login_resp}")

            # Step 2: Validate MPIN
            val_resp = self.client.totp_validate(mpin=self.mpin)
            self._raise_if_api_error(val_resp, "MPIN validation")
            logger.debug(f"{acc_tag}MPIN Validation response: {val_resp}")

            self.is_authenticated = True
            logger.info(f"[bold green]{acc_tag}Successfully authenticated with Kotak Neo API.[/bold green]")
        except Exception as e:
            logger.error(f"[bold red]{acc_tag}Kotak Neo authentication failed:[/bold red] {e}")
            self.client = None
            raise RuntimeError("Kotak Neo authentication failed; live consumer startup aborted") from e

    @staticmethod
    def _raise_if_api_error(response: Any, action: str):
        if isinstance(response, dict):
            error = response.get("Error") or response.get("error") or response.get("Error Message")
            stat = response.get("stat")
            if error or (stat and str(stat).lower() not in ("ok", "success")):
                raise RuntimeError(f"Kotak {action} rejected: {error or stat}")

    def resolve_symbol(
        self,
        index: str,
        strike: float,
        option_type: str,
        expiry: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Resolves the exact exchange segment and trading symbol for a given option contract.
        Picks the nearest upcoming active expiry by default and extracts scrip lot size.
        """
        from datetime import datetime
        idx = index.upper().strip()
        opt = option_type.upper().strip()
        strike_int = int(strike)

        # Exchange segment mapping: NFO for NSE, BFO for BSE
        if idx in ("SENSEX", "BANKEX"):
            exchange_segment = "bse_fo"
        else:
            exchange_segment = "nse_fo"

        if self.client and self.is_authenticated:
            try:
                results = self.client.search_scrip(
                    exchange_segment=exchange_segment,
                    symbol=idx,
                    option_type=opt,
                    strike_price=str(strike_int)
                )
                if isinstance(results, list) and len(results) > 0:
                    # Filter strictly by exact index symbol name (e.g. exclude FINNIFTY when searching NIFTY)
                    filtered_results = [
                        r for r in results
                        if r.get("pSymbolName", "").strip().upper() == idx
                        and r.get("pTrdSymbol")
                    ]
                    if not filtered_results:
                        raise RuntimeError(f"Kotak returned no exact scrip match for {idx} {strike_int} {opt}")

                    def parse_expiry(item):
                        exp_str = item.get("pExpiryDate")
                        if not exp_str:
                            return datetime.max
                        try:
                            return datetime.strptime(exp_str, "%d%b%Y")
                        except Exception:
                            return datetime.max

                    # Only trade contracts whose expiry is known and still active.
                    today = datetime.now().date()
                    active_candidates = [
                        r for r in filtered_results
                        if parse_expiry(r).date() >= today and parse_expiry(r) != datetime.max
                    ]
                    if not active_candidates:
                        raise RuntimeError(f"No active expiry returned for {idx} {strike_int} {opt}")
                    candidates = active_candidates

                    # If target_expiry was requested, filter candidates matching it
                    if expiry:
                        exp_req = expiry.strip().upper()
                        matching = [
                            r for r in candidates
                            if exp_req in r.get("pExpiryDate", "").upper() or exp_req in r.get("pTrdSymbol", "").upper()
                        ]
                        if not matching:
                            raise RuntimeError(f"Requested expiry {expiry} was not found")
                        candidates = matching

                    sorted_results = sorted(candidates, key=parse_expiry)
                    chosen = sorted_results[0]
                    matched_symbol = chosen.get("pTrdSymbol")
                    lot_size = chosen.get("lLotSize") or chosen.get("iLotSize")

                    if matched_symbol:
                        if not lot_size:
                            raise RuntimeError(f"Kotak scrip data has no lot size for {matched_symbol}")
                        res_dict = {
                            "trading_symbol": matched_symbol,
                            "exchange_segment": exchange_segment,
                            "lot_size": int(lot_size),
                            "instrument_token": str(chosen.get("pSymbol") or "")
                        }
                        logger.info(
                            f"Resolved scrip via Kotak search: {matched_symbol} "
                            f"(Expiry: {chosen.get('pExpiryDate')}, Lot Size: {res_dict.get('lot_size')})"
                        )
                        return res_dict
                raise RuntimeError(f"Kotak returned no contracts for {idx} {strike_int} {opt}")
            except Exception as e:
                logger.error(f"Kotak scrip lookup failed for {idx} {strike_int} {opt}: {e}")
                raise RuntimeError("Unable to resolve a live Kotak contract; refusing to guess a trading symbol") from e
        raise RuntimeError("Kotak client is not authenticated; live execution is unavailable")

    def get_ltp(self, instrument_token: str, exchange_segment: str) -> float:
        """Fetches the latest traded price for a resolved contract; never returns a stale/default price."""
        if not self.client or not self.is_authenticated:
            raise RuntimeError("Kotak client is not authenticated; cannot fetch live LTP")
        if not instrument_token:
            raise RuntimeError("Resolved Kotak contract has no instrument token; cannot fetch live LTP")

        response = self.client.quotes(
            instrument_tokens=[{
                "instrument_token": str(instrument_token),
                "exchange_segment": exchange_segment,
            }],
            quote_type="ltp",
        )
        if isinstance(response, list):
            quotes = response
        elif isinstance(response, dict):
            quotes = response.get("data")
        else:
            quotes = None
        if not isinstance(quotes, list) or not quotes:
            raise RuntimeError(f"Kotak returned no LTP quote for token {instrument_token}: {response}")
        try:
            ltp = float(quotes[0]["ltp"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RuntimeError(f"Kotak returned an invalid LTP quote for token {instrument_token}: {response}") from exc
        if ltp <= 0:
            raise RuntimeError(f"Kotak returned a non-positive LTP for token {instrument_token}: {ltp}")
        return ltp

    def place_order(self, order: OrderPlacement) -> Dict[str, Any]:
        """Places a new order (SL, Limit, or Market)."""
        price_str = f"{order.price:.2f}"
        trigger_str = f"{order.trigger_price:.2f}" if order.trigger_price else "0"

        # Map transaction_type: Kotak Neo requires 'B' or 'S'
        raw_tt = str(order.transaction_type or "B").strip().upper()
        mapped_tt = "B" if raw_tt in ("B", "BUY") else ("S" if raw_tt in ("S", "SELL") else "B")

        try:
            logger.info(
                f"[bold green][LIVE] Sending Order to Kotak Neo:[/bold green] "
                f"{mapped_tt} {order.quantity}x {order.trading_symbol} | "
                f"Segment={order.exchange_segment} | Product={order.product} | "
                f"Type={order.order_type} | Price={price_str} | Trigger={trigger_str}"
            )

            # Kotak Neo maps 'tag' to 'ig' (Client Order ID / GuiOrdId) which the OMS requires
            # to be strictly unique per trading day. Ensure any non-empty tag is unique, alphanumeric,
            # and within 20 chars to prevent 'Client OrderID already exists' errors.
            tag_to_send = None
            if order.tag:
                clean_tag = "".join(c for c in order.tag if c.isalnum())
                # If tag already contains unique random/timestamp suffix (e.g. TG1A2B3C4D), use it up to 20 chars
                if len(clean_tag) >= 10 and not clean_tag.isalpha():
                    tag_to_send = clean_tag[:20]
                else:
                    unique_suffix = uuid.uuid4().hex[:6].upper()
                    clean_prefix = clean_tag[:14] if clean_tag else "TG"
                    tag_to_send = f"{clean_prefix}{unique_suffix}"[:20]

            resp = self.client.place_order(
                exchange_segment=order.exchange_segment,
                product=order.product,
                price=price_str,
                order_type=order.order_type,
                quantity=str(order.quantity),
                validity=order.validity,
                trading_symbol=order.trading_symbol,
                transaction_type=mapped_tt,
                trigger_price=trigger_str,
                tag=tag_to_send
            )
            logger.info(f"Kotak Neo place_order response: {resp}")

            if not isinstance(resp, dict):
                logger.error(f"[bold red]Unexpected Kotak Neo response:[/bold red] {resp}")
                return {"success": False, "error": f"Unexpected response: {resp}"}

            if "Error" in resp or "error" in resp or "Error Message" in resp:
                err_val = resp.get("Error") or resp.get("error") or resp.get("Error Message")
                logger.error(f"[bold red]Kotak Neo order placement rejected:[/bold red] {err_val}")
                return {"success": False, "error": str(err_val), "response": resp}

            stat = resp.get("stat")
            if stat and stat != "Ok":
                err_msg = resp.get("errMsg") or stat
                if resp.get("errMsg") and resp.get("errMsg") != stat:
                    err_msg = f"{resp.get('errMsg')}: {stat}"
                logger.error(f"[bold red]Kotak Neo order placement failed:[/bold red] {err_msg}")
                return {"success": False, "error": err_msg, "response": resp}

            order_id = resp.get("nOrdNo") or resp.get("order_id") or resp.get("result")
            if not order_id or str(order_id).strip().lower() in ("none", ""):
                err_msg = resp.get("stat") or resp.get("errMsg") or "No valid order ID returned by Kotak Neo"
                logger.error(f"[bold red]Kotak Neo placed order returned no order ID:[/bold red] {resp}")
                return {"success": False, "error": err_msg, "response": resp}

            return {"success": True, "order_id": str(order_id), "response": resp}
        except Exception as e:
            logger.error(f"[bold red]Failed to place order in Kotak Neo:[/bold red] {e}")
            return {"success": False, "error": str(e)}

    def modify_order(
        self,
        order_id: str,
        price: float,
        order_type: str,
        quantity: int,
        trigger_price: float = 0.0,
        validity: str = "DAY"
    ) -> Dict[str, Any]:
        """Modifies a pending order."""
        price_str = f"{price:.2f}"
        trigger_str = f"{trigger_price:.2f}" if trigger_price else "0"

        try:
            logger.info(
                f"[bold yellow][LIVE] Modifying Order:[/bold yellow] ID={order_id} | "
                f"Price={price_str} | Trigger={trigger_str}"
            )
            resp = self.client.modify_order(
                order_id=str(order_id),
                price=price_str,
                order_type=order_type,
                quantity=str(quantity),
                validity=validity,
                trigger_price=trigger_str
            )
            logger.info(f"Kotak Neo modify_order response: {resp}")

            if not isinstance(resp, dict):
                return {"success": False, "error": f"Unexpected response: {resp}"}

            if "Error" in resp or "error" in resp or "Error Message" in resp:
                err_val = resp.get("Error") or resp.get("error") or resp.get("Error Message")
                return {"success": False, "error": str(err_val), "response": resp}

            stat = resp.get("stat")
            if stat and stat != "Ok":
                err_msg = resp.get("errMsg") or stat
                if resp.get("errMsg") and resp.get("errMsg") != stat:
                    err_msg = f"{resp.get('errMsg')}: {stat}"
                return {"success": False, "error": err_msg, "response": resp}

            return {"success": True, "order_id": order_id, "response": resp}
        except Exception as e:
            logger.error(f"[bold red]Failed to modify order {order_id}:[/bold red] {e}")
            return {"success": False, "error": str(e)}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancels a pending order."""
        try:
            logger.info(f"[bold red][LIVE] Cancelling Order:[/bold red] ID={order_id}")
            resp = self.client.cancel_order(order_id=str(order_id))
            logger.info(f"Kotak Neo cancel_order response: {resp}")

            if not isinstance(resp, dict):
                return {"success": False, "error": f"Unexpected response: {resp}"}

            if "Error" in resp or "error" in resp or "Error Message" in resp:
                err_val = resp.get("Error") or resp.get("error") or resp.get("Error Message")
                return {"success": False, "error": str(err_val), "response": resp}

            stat = resp.get("stat")
            if stat and stat != "Ok":
                err_msg = resp.get("errMsg") or stat
                if resp.get("errMsg") and resp.get("errMsg") != stat:
                    err_msg = f"{resp.get('errMsg')}: {stat}"
                return {"success": False, "error": err_msg, "response": resp}

            return {"success": True, "order_id": order_id, "response": resp}
        except Exception as e:
            logger.error(f"[bold red]Failed to cancel order {order_id}:[/bold red] {e}")
            return {"success": False, "error": str(e)}

    def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """Fetches status of a specific order."""
        try:
            report = self.client.order_report(order_id=str(order_id))
            # Format varies between single order list or dictionary
            data = report.get("data") if isinstance(report, dict) else report
            if isinstance(data, list) and len(data) > 0:
                ord_data = data[0]
            elif isinstance(data, dict):
                ord_data = data
            else:
                ord_data = report

            raw_st = str(ord_data.get("ordSt", ord_data.get("status", ""))).lower()
            if "trig" in raw_st:
                return OrderStatus.TRIGGER_PENDING
            elif "open" in raw_st:
                return OrderStatus.OPEN
            elif "comp" in raw_st or "trad" in raw_st:
                return OrderStatus.COMPLETE
            elif "canc" in raw_st:
                return OrderStatus.CANCELLED
            elif "rej" in raw_st:
                return OrderStatus.REJECTED
            return OrderStatus.PENDING
        except Exception as e:
            logger.warning(f"Error fetching status for order {order_id}: {e}")
            return None

    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        """Return the latest Kotak order fields used to reconcile live fills."""
        if not self.client or not self.is_authenticated:
            raise RuntimeError("Kotak client is not authenticated")
        report = self.client.order_report(order_id=str(order_id))
        rows = report.get("data") if isinstance(report, dict) else report
        if isinstance(rows, list):
            row = next((item for item in rows if str(item.get("nOrdNo", item.get("order_id", ""))) == str(order_id)), None)
        elif isinstance(rows, dict):
            row = rows
        else:
            row = None
        if not isinstance(row, dict):
            raise RuntimeError(f"Kotak returned no order details for {order_id}: {report}")
        status_text = str(row.get("ordSt", row.get("status", ""))).lower()
        if "trig" in status_text:
            status = OrderStatus.TRIGGER_PENDING
        elif "open" in status_text or "pending" in status_text:
            status = OrderStatus.OPEN
        elif "comp" in status_text or "trad" in status_text or "execut" in status_text:
            status = OrderStatus.COMPLETE
        elif "canc" in status_text:
            status = OrderStatus.CANCELLED
        elif "rej" in status_text:
            status = OrderStatus.REJECTED
        else:
            status = OrderStatus.PENDING
        filled = row.get("fldQty", row.get("filled_quantity", 0))
        quantity = row.get("qty", row.get("quantity", 0))
        average_price = row.get("avgPrc", row.get("average_price", 0))
        try:
            filled_quantity = int(float(filled or 0))
        except (TypeError, ValueError):
            filled_quantity = 0
        if status == OrderStatus.COMPLETE and filled_quantity <= 0:
            try:
                filled_quantity = int(float(quantity or 0))
            except (TypeError, ValueError):
                filled_quantity = 0
        try:
            avg_price = float(average_price or 0)
        except (TypeError, ValueError):
            avg_price = 0.0
        return {
            "status": status,
            "status_text": status_text,
            "filled_quantity": filled_quantity,
            "quantity": int(float(quantity or 0)),
            "average_price": avg_price,
            "raw": row,
        }

    @staticmethod
    def _is_session_expired(response: Any) -> bool:
        """Detects if response indicates an expired or invalid session token."""
        if isinstance(response, dict):
            err_msg = str(
                response.get("Error Message")
                or response.get("error")
                or response.get("Error")
                or response.get("message")
                or response.get("errMsg")
                or ""
            ).lower()
            if any(term in err_msg for term in ("2fa", "session", "token", "unauthorized", "login", "forbidden", "authenticate")):
                return True
            stat = str(response.get("stat") or "").lower()
            if stat in ("not_ok", "error", "failed") and any(term in err_msg for term in ("auth", "session", "token", "invalid", "expired")):
                return True
        return False

    @staticmethod
    def _extract_funds_from_limits(limits: Any) -> Optional[float]:
        """
        Extracts available trading funds/margin from Kotak Neo limits API response.
        Supports:
        - Dict with top-level fields (Net, AvailableMargin, Cash, CollateralValue, etc.)
        - Dict with 'data' wrapping a dict or a list
        - List of segment limit dicts (prioritizing FO / ALL / CASH)
        - Numeric strings with commas or floats
        """
        if limits is None:
            return None

        candidates = [
            "Net", "net",
            "AvailableMargin", "availableMargin", "available_margin",
            "availableBalance", "available_balance",
            "Cash", "cash", "cshBal",
            "marginAvailable", "MarginAvailable",
            "netAllocated", "NetAllocated",
            "curFund",
            "CollateralValue", "collateralValue",
            "AdhocMargin", "adhocMargin",
        ]

        records: List[Dict[str, Any]] = []

        if isinstance(limits, list):
            records = [r for r in limits if isinstance(r, dict)]
        elif isinstance(limits, dict):
            # Check if there is a nested data structure
            data = limits.get("data")
            if isinstance(data, list):
                records.extend([r for r in data if isinstance(r, dict)])
            elif isinstance(data, dict):
                inner_limits = data.get("limits")
                if isinstance(inner_limits, list):
                    records.extend([r for r in inner_limits if isinstance(r, dict)])
                elif isinstance(inner_limits, dict):
                    records.append(inner_limits)
                records.append(data)
            records.append(limits)

        if not records:
            return None

        # Prioritize FO or ALL segment records if present
        def segment_priority(rec: Dict[str, Any]) -> int:
            seg = str(rec.get("segment") or rec.get("seg") or "").upper().strip()
            if seg in ("FO", "NSE_FO", "BSE_FO"):
                return 0
            if seg == "ALL":
                return 1
            if seg == "CASH":
                return 2
            return 3

        sorted_records = sorted(records, key=segment_priority)

        for rec in sorted_records:
            for key in candidates:
                if key in rec and rec[key] is not None:
                    raw_val = str(rec[key]).replace(",", "").strip()
                    if raw_val != "":
                        try:
                            return float(raw_val)
                        except (ValueError, TypeError):
                            continue

        return None

    def get_available_funds(self) -> float:
        """
        Fetches available margin / funds from Kotak Neo API.
        If the session has expired or requires 2FA re-validation, attempts to re-authenticate and retry.
        Raises RuntimeError if Kotak does not return available funds.
        """
        acc_tag = f"[{self.account_id}] " if self.account_id != "default" else ""

        if not self.client or not self.is_authenticated:
            logger.info(f"{acc_tag}Kotak client not authenticated when fetching funds; authenticating now...")
            self._init_live_client()

        for attempt in range(2):
            try:
                limits = self.client.limits()
                logger.debug(f"{acc_tag}Kotak limits raw response: {limits}")

                # Check if Kotak returned a session/auth expiration error
                if self._is_session_expired(limits):
                    if attempt == 0:
                        logger.warning(
                            f"{acc_tag}Kotak session appears expired or unauthenticated ({limits}). "
                            f"Re-authenticating and retrying limits..."
                        )
                        self._init_live_client()
                        continue
                    else:
                        raise RuntimeError(f"Kotak session expired and re-authentication failed: {limits}")

                funds = self._extract_funds_from_limits(limits)
                if funds is not None:
                    logger.info(f"{acc_tag}Retrieved live available funds from Kotak Neo: ₹{funds:,.2f}")
                    return funds

                logger.error(f"{acc_tag}Kotak limits response format unrecognized. Raw payload: {limits}")
                raise RuntimeError(f"Kotak limits response did not include available funds: {limits}")

            except Exception as e:
                if attempt == 0 and any(k in str(e).lower() for k in ("401", "unauthorized", "session", "token")):
                    logger.warning(f"{acc_tag}Limits call failed with auth error ({e}). Re-authenticating...")
                    try:
                        self._init_live_client()
                        continue
                    except Exception as reauth_err:
                        logger.error(f"{acc_tag}Re-authentication failed: {reauth_err}")
                logger.error(f"{acc_tag}Failed to fetch available funds from Kotak Neo: {e}")
                raise RuntimeError(f"Unable to retrieve live available funds: {e}") from e

        raise RuntimeError("Unable to retrieve live available funds after retry")
