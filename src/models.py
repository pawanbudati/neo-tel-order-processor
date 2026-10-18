from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class SignalType(str, Enum):
    NEW_ORDER = "NEW_ORDER"
    MODIFY_ORDER = "MODIFY_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    IGNORE_SIGNAL = "IGNORE_SIGNAL"
    CLOSE_POSITION = "CLOSE_POSITION"
    NONE = "NONE"

class OrderCondition(str, Enum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"
    AT = "AT"
    MARKET = "MARKET"
    LIMIT = "LIMIT"

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

class ParsedSignal(BaseModel):
    is_trading_signal: bool = Field(description="True if message contains an actionable trading command")
    signal_type: SignalType = Field(default=SignalType.NONE, description="Type of signal")
    action: Optional[str] = Field(default=None, description="BUY, SELL, CANCEL, MODIFY, or IGNORE")
    index: Optional[str] = Field(default=None, description="Index name: NIFTY, SENSEX, BANKNIFTY, FINNIFTY, MIDCPNIFTY")
    strike_price: Optional[float] = Field(default=None, description="Strike price e.g. 22500, 75000")
    option_type: Optional[str] = Field(default=None, description="CE or PE")
    order_condition: Optional[OrderCondition] = Field(default=None, description="ABOVE, BELOW, AT, MARKET, or LIMIT")
    entry_price: Optional[float] = Field(default=None, description="Entry price mentioned in signal")
    trigger_price: Optional[float] = Field(default=None, description="Trigger price if stop loss or breakout")
    stop_loss: Optional[float] = Field(default=None, description="Stop loss price if specified")
    target: Optional[float] = Field(default=None, description="Target price if specified")
    targets: List[float] = Field(default_factory=list, description="Ordered partial-exit target prices")
    target_expiry: Optional[str] = Field(default=None, description="Target expiry if specified")
    reasoning: Optional[str] = Field(default="", description="Reasoning / explanation by Gemini")
    raw_message: Optional[str] = Field(default="", description="Raw telegram message text")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def contract_key(self) -> str:
        """Returns unique key identifying the instrument contract, e.g., 'NIFTY_22500_CE'."""
        idx = self.index.upper() if self.index else "UNKNOWN"
        strike = f"{int(self.strike_price)}" if self.strike_price else "0"
        opt = self.option_type.upper() if self.option_type else "CE"
        return f"{idx}_{strike}_{opt}"

class OrderPlacement(BaseModel):
    trading_symbol: str
    exchange_segment: str  # nse_fo, bse_fo
    transaction_type: str  # BUY or SELL
    order_type: str        # L, MKT, SL, SL-M
    quantity: int
    price: float
    trigger_price: float = 0.0
    product: str = "MIS"
    validity: str = "DAY"
    tag: Optional[str] = None
    contract_key: str = ""

class ActiveOrder(BaseModel):
    order_id: str
    trading_symbol: str
    contract_key: str
    exchange_segment: str
    transaction_type: str
    order_type: str
    quantity: int
    price: float
    trigger_price: float
    product: str
    status: OrderStatus = OrderStatus.PENDING
    placed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_signal: Optional[ParsedSignal] = None
    meta: Dict[str, Any] = Field(default_factory=dict)

class KotakAccountConfig(BaseModel):
    account_id: str = Field(default="default", description="Unique identifier for account (e.g. acc_main, acc_family)")
    consumer_key: str = Field(default="", description="Kotak Neo API consumer key")
    mobile_number: str = Field(default="", description="Kotak registered mobile number")
    ucc: str = Field(default="", description="Kotak Unique Client Code (UCC)")
    totp_key: Optional[str] = Field(default="", description="Base32 TOTP secret key for 2FA")
    mpin: str = Field(default="", description="Kotak Neo MPIN")
    environment: str = Field(default="prod", description="Kotak environment (prod/uat)")
    default_lots: int = Field(default=3, description="Default lots; entry order sizes use multiples of three lots")
    default_product_type: str = Field(default="MIS", description="Default product type: MIS or NRML")
    risk_percentage: Optional[float] = Field(default=None, description="Max percentage of capital to risk per trade if SL is hit")
    capital: Optional[float] = Field(default=None, description="Available capital for risk calculations in INR")
