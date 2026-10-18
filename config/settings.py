import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    kotak_consumer_key: str = Field(default_factory=lambda: os.getenv("KOTAK_CONSUMER_KEY", ""))
    kotak_mobile_number: str = Field(default_factory=lambda: os.getenv("KOTAK_MOBILE_NUMBER", ""))
    kotak_ucc: str = Field(default_factory=lambda: os.getenv("KOTAK_UCC", ""))
    kotak_totp_key: str = Field(default_factory=lambda: os.getenv("KOTAK_TOTP_KEY", ""))
    kotak_mpin: str = Field(default_factory=lambda: os.getenv("KOTAK_MPIN", ""))
    kotak_environment: str = Field(default_factory=lambda: os.getenv("KOTAK_ENVIRONMENT", "prod"))
    default_product_type: str = Field(default_factory=lambda: os.getenv("DEFAULT_PRODUCT_TYPE", "MIS").upper())
    default_lots: int = Field(default_factory=lambda: int(os.getenv("DEFAULT_LOTS", "1")))
    sl_limit_buffer_percent: float = Field(default_factory=lambda: float(os.getenv("SL_LIMIT_BUFFER_PERCENT", "1.0")))
    auto_cancel_on_revise: bool = Field(default_factory=lambda: os.getenv("AUTO_CANCEL_ON_REVISE", "true").lower() in ("true", "1", "yes"))
    risk_percentage: Optional[float] = Field(default_factory=lambda: float(os.getenv("RISK_PERCENTAGE")) if os.getenv("RISK_PERCENTAGE") else None)
    default_capital: Optional[float] = Field(default_factory=lambda: float(os.getenv("DEFAULT_CAPITAL")) if os.getenv("DEFAULT_CAPITAL") else None)
    kafka_bootstrap_servers: str = Field(default_factory=lambda: os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"))
    kafka_topic_signals: str = Field(default_factory=lambda: os.getenv("KAFKA_TOPIC_SIGNALS", "kotak_trading_signals"))
    kafka_consumer_group_id: str = Field(default_factory=lambda: os.getenv("KAFKA_CONSUMER_GROUP_ID", "kotak_executor_default"))
    kafka_auto_offset_reset: str = Field(default_factory=lambda: os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest"))
    kafka_security_protocol: str = Field(default_factory=lambda: os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"))
    kafka_sasl_mechanism: Optional[str] = Field(default_factory=lambda: os.getenv("KAFKA_SASL_MECHANISM"))
    kafka_sasl_username: Optional[str] = Field(default_factory=lambda: os.getenv("KAFKA_SASL_USERNAME"))
    kafka_sasl_password: Optional[str] = Field(default_factory=lambda: os.getenv("KAFKA_SASL_PASSWORD"))
    def is_kotak_configured(self) -> bool:
        required = (
            self.kotak_consumer_key, self.kotak_mobile_number, self.kotak_ucc,
            self.kotak_totp_key, self.kotak_mpin
        )
        return all(value.strip() and "your_" not in value.lower() for value in required)


settings = Settings()
