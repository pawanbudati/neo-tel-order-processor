import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable
from aiokafka import AIOKafkaConsumer

from config.settings import settings
from src.logger import logger
from src.models import ParsedSignal
from src.order_manager import OrderManager

class KafkaSignalConsumer:
    """
    Kafka Signal Consumer for Kotak Order Execution.
    
    Subscribes to the Kafka trading signals topic using a designated Consumer Group ID.
    
    *KEY ARCHITECTURAL BENEFIT*:
    To execute orders on multiple Kotak accounts (running on the same or distinct VMs),
    each account worker runs this consumer with its own unique `group_id` (e.g.,
    `kotak_group_acc1`, `kotak_group_acc2`). Kafka automatically fans out / replicates
    every trading signal to ALL active consumer groups simultaneously!
    """

    def __init__(
        self,
        order_manager: Optional[OrderManager] = None,
        account_id: str = "default",
        group_id: Optional[str] = None,
        bootstrap_servers: Optional[str] = None,
        topic: Optional[str] = None,
        auto_offset_reset: Optional[str] = None,
        security_protocol: Optional[str] = None,
        sasl_mechanism: Optional[str] = None,
        sasl_plain_username: Optional[str] = None,
        sasl_plain_password: Optional[str] = None,
        on_signal_callback: Optional[Callable[[ParsedSignal], Any]] = None,
        risk_percentage: Optional[float] = None,
        capital: Optional[float] = None,
        lots: Optional[int] = None,
    ):
        self.account_id = account_id
        self.order_manager = order_manager or OrderManager(
            account_id=account_id,
            default_lots=lots,
            risk_percentage=risk_percentage,
            capital=capital
        )
        self.on_signal_callback = on_signal_callback

        # Distinct consumer group per account ensures replication/fanout across multiple accounts
        self.group_id = group_id or settings.kafka_consumer_group_id or f"kotak_executor_{account_id}"
        self.bootstrap_servers = bootstrap_servers or settings.kafka_bootstrap_servers
        self.topic = topic or settings.kafka_topic_signals
        self.auto_offset_reset = auto_offset_reset or settings.kafka_auto_offset_reset
        self.security_protocol = security_protocol or settings.kafka_security_protocol
        self.sasl_mechanism = sasl_mechanism or settings.kafka_sasl_mechanism
        self.sasl_plain_username = sasl_plain_username or settings.kafka_sasl_username
        self.sasl_plain_password = sasl_plain_password or settings.kafka_sasl_password

        self.consumer: Optional[AIOKafkaConsumer] = None
        self._is_running = False

    async def start(self):
        """Initializes and connects the Kafka consumer."""
        logger.info(
            f"[bold cyan]Initializing Kafka Signal Consumer:[/bold cyan] "
            f"Account='{self.account_id}' | GroupID='{self.group_id}' | "
            f"Topic='{self.topic}' | Brokers={self.bootstrap_servers}"
        )

        kwargs: Dict[str, Any] = {
            "bootstrap_servers": self.bootstrap_servers,
            "group_id": self.group_id,
            "auto_offset_reset": self.auto_offset_reset,
            "enable_auto_commit": False,  # Manual commit after order processing
            "security_protocol": self.security_protocol,
            "value_deserializer": lambda v: json.loads(v.decode("utf-8")),
            "key_deserializer": lambda k: k.decode("utf-8") if k else None,
        }

        if self.sasl_mechanism:
            kwargs["sasl_mechanism"] = self.sasl_mechanism
            kwargs["sasl_plain_username"] = self.sasl_plain_username
            kwargs["sasl_plain_password"] = self.sasl_plain_password

        self.consumer = AIOKafkaConsumer(self.topic, **kwargs)
        await self.consumer.start()
        self._is_running = True
        logger.info(
            f"[bold green]Kafka Consumer subscribed to '{self.topic}' "
            f"[Group: {self.group_id}]. Waiting for trading signals...[/bold green]"
        )

    async def stop(self):
        """Stops the Kafka consumer gracefully."""
        self._is_running = False
        if self.consumer:
            logger.info(f"[cyan]Stopping Kafka Consumer for account '{self.account_id}'...[/cyan]")
            await self.consumer.stop()
            logger.info(f"[bold green]Kafka Consumer stopped cleanly.[/bold green]")

    async def run(self):
        """
        Main consumer event loop.
        Continuously polls for incoming signals from Kafka, converts them to ParsedSignal,
        and triggers OrderManager execution.
        """
        if not self._is_running or not self.consumer:
            await self.start()

        try:
            while self._is_running:
                messages = await self.consumer.getmany(timeout_ms=1000, max_records=20)
                for partition_messages in messages.values():
                    for msg in partition_messages:
                        await self._process_kafka_message(msg)
                if not self.on_signal_callback:
                    # Poll even when Kafka is quiet: staged exits and stop updates
                    # depend on live order and quote changes.
                    self.order_manager.manage_open_trades()
        except asyncio.CancelledError:
            logger.info("[yellow]Consumer task cancelled.[/yellow]")
        except Exception as e:
            logger.error(f"[bold red]Kafka Consumer fatal error loop:[/bold red] {e}", exc_info=True)
        finally:
            await self.stop()

    async def _process_kafka_message(self, msg):
        """Processes a single Kafka message."""
        try:
            payload = msg.value
            if not isinstance(payload, dict):
                logger.warning(f"[yellow]Ignored invalid Kafka message format: {type(payload)}[/yellow]")
                await self.consumer.commit()
                return

            meta = payload.get("_meta", {})
            event_id = meta.get("event_id", "unknown")
            pub_time = meta.get("published_at", "unknown")

            logger.info(
                f"\n[bold magenta]══════ KAFKA SIGNAL RECEIVED ══════[/bold magenta]\n"
                f"[dim]Account: {self.account_id} | Group: {self.group_id}[/dim]\n"
                f"[dim]Topic: {msg.topic} | Partition: {msg.partition} | Offset: {msg.offset}[/dim]\n"
                f"[dim]Event ID: {event_id} | Published: {pub_time}[/dim]"
            )

            # Reconstruct ParsedSignal Pydantic model
            clean_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
            parsed_signal = ParsedSignal(**clean_payload)

            logger.info(
                f"[bold cyan]Executing Signal on Kotak Account '{self.account_id}':[/bold cyan] "
                f"{parsed_signal.signal_type.value} for {parsed_signal.contract_key()}"
            )

            # Execute via OrderManager or custom callback
            if self.on_signal_callback:
                if asyncio.iscoroutinefunction(self.on_signal_callback):
                    res = await self.on_signal_callback(parsed_signal)
                else:
                    res = self.on_signal_callback(parsed_signal)
            else:
                res = self.order_manager.process_signal(parsed_signal)

            logger.info(f"[bold green]Execution Result on '{self.account_id}':[/bold green] {res}")

            # Commit offset after successful order processing
            await self.consumer.commit()

        except Exception as e:
            logger.error(
                f"[bold red]Error processing Kafka message on account '{self.account_id}':[/bold red] {e}",
                exc_info=True
            )
            # Even on error, commit offset to avoid poison-pill message loops
            try:
                await self.consumer.commit()
            except Exception:
                pass
