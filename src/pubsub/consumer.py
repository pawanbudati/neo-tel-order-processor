import asyncio
import json
import os
from typing import Optional, Dict, Any, Callable

from google.cloud import pubsub_v1
from google.oauth2 import service_account

from config.settings import settings
from src.logger import logger
from src.models import ParsedSignal
from src.order_manager import OrderManager


class PubSubSignalConsumer:
    """
    Google Cloud Pub/Sub Signal Consumer for Kotak Order Execution.
    
    Subscribes to incoming trading signals using a designated Subscription ID.
    
    *KEY ARCHITECTURAL BENEFIT (Always Free & Native Fanout)*:
    To execute orders across multiple Kotak accounts (running on the same or distinct VMs),
    each account worker runs this consumer attached to its own unique Subscription ID
    (e.g., `kotak_executor_yuva`, `kotak_executor_pavan`, `kotak_executor_sahi`).
    
    Google Cloud Pub/Sub automatically replicates every signal published to the topic
    to ALL active account subscriptions with zero cross-account latency or interference,
    completely eliminating the need for an external Kafka broker!
    """

    def __init__(
        self,
        order_manager: Optional[OrderManager] = None,
        account_id: str = "default",
        project_id: Optional[str] = None,
        topic: Optional[str] = None,
        subscription_id: Optional[str] = None,
        credentials_path: Optional[str] = None,
        emulator_host: Optional[str] = None,
        on_signal_callback: Optional[Callable[[ParsedSignal], Any]] = None,
        risk_percentage: Optional[float] = None,
        capital: Optional[float] = None,
        lots: Optional[int] = None,
    ):
        self.account_id = account_id
        self._order_manager = order_manager
        self._lots = lots
        self._risk_percentage = risk_percentage
        self._capital = capital
        self.on_signal_callback = on_signal_callback

        self.project_id = project_id or settings.gcp_project_id
        self.topic_id = topic or settings.pubsub_topic_signals

        # Distinct subscription per account ensures replication/fanout across multiple accounts
        if subscription_id:
            self.subscription_id = subscription_id
        elif settings.pubsub_subscription_id and settings.pubsub_subscription_id != "kotak_executor_default":
            self.subscription_id = settings.pubsub_subscription_id
        else:
            self.subscription_id = f"kotak_executor_{account_id}"

        self.credentials_path = credentials_path or settings.pubsub_credentials_path
        self.emulator_host = emulator_host or settings.pubsub_emulator_host

        if self.emulator_host:
            os.environ["PUBSUB_EMULATOR_HOST"] = self.emulator_host

        self.credentials = None
        if self.credentials_path and os.path.exists(self.credentials_path):
            self.credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path
            )

        self.subscriber: Optional[pubsub_v1.SubscriberClient] = None
        self.subscription_path: Optional[str] = None
        self._streaming_pull_future = None
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._is_running = False

    @property
    def order_manager(self) -> Optional[OrderManager]:
        if self._order_manager is None:
            self._order_manager = OrderManager(
                account_id=self.account_id,
                default_lots=self._lots,
                risk_percentage=self._risk_percentage,
                capital=self._capital,
            )
        return self._order_manager

    @order_manager.setter
    def order_manager(self, value: Optional[OrderManager]):
        self._order_manager = value

    async def start(self):
        """Initializes and connects the Pub/Sub subscriber."""
        if not self.project_id:
            raise ValueError(
                "GCP_PROJECT_ID is not configured. Set GCP_PROJECT_ID in consumer/.env "
                "or pass project_id to PubSubSignalConsumer."
            )

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        logger.info(
            f"[bold cyan]Initializing Pub/Sub Signal Consumer:[/bold cyan] "
            f"Account='{self.account_id}' | Subscription='{self.subscription_id}' | "
            f"Project='{self.project_id}'"
        )

        self.subscriber = pubsub_v1.SubscriberClient(credentials=self.credentials)
        self.subscription_path = self.subscriber.subscription_path(
            self.project_id, self.subscription_id
        )

        flow_control = pubsub_v1.types.FlowControl(
            max_messages=10,
            max_bytes=10 * 1024 * 1024,
        )

        # Start streaming pull on background gRPC threads
        self._streaming_pull_future = self.subscriber.subscribe(
            self.subscription_path,
            callback=self._message_callback,
            flow_control=flow_control,
        )
        self._is_running = True

        logger.info(
            f"[bold green]Pub/Sub Consumer subscribed to '{self.subscription_path}'. "
            f"Waiting for trading signals...[/bold green]"
        )

    def _message_callback(self, message: pubsub_v1.subscriber.message.Message):
        """Called by background gRPC streaming pull threads upon message arrival."""
        if self._is_running and self._loop and self._loop.is_running() and self._queue:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, message)
        else:
            try:
                message.nack()
            except Exception:
                pass

    async def stop(self):
        """Stops the Pub/Sub consumer gracefully."""
        self._is_running = False
        if self._streaming_pull_future:
            logger.info(f"[cyan]Stopping Pub/Sub streaming pull for account '{self.account_id}'...[/cyan]")
            self._streaming_pull_future.cancel()
            self._streaming_pull_future = None

        if self.subscriber:
            try:
                self.subscriber.close()
            except Exception:
                pass
            logger.info(f"[bold green]Pub/Sub Consumer for '{self.account_id}' stopped cleanly.[/bold green]")

    async def run(self):
        """
        Main consumer event loop.
        Continuously processes incoming trading signals from Pub/Sub, executes orders,
        and periodically manages trailing stops and target exits.
        """
        if not self._is_running or not self.subscriber:
            await self.start()

        try:
            while self._is_running:
                try:
                    # Wait up to 1 second for a signal to arrive
                    msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._process_pubsub_message(msg)

                    # Drain any additional queued messages
                    while self._queue and not self._queue.empty():
                        next_msg = self._queue.get_nowait()
                        await self._process_pubsub_message(next_msg)

                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break

                # Poll even when Pub/Sub is quiet: staged exits and stop updates
                # depend on live order and quote changes. Run in worker thread to prevent loop blocking.
                if not self.on_signal_callback and self.order_manager:
                    try:
                        await asyncio.to_thread(self.order_manager.manage_open_trades)
                    except Exception as e:
                        logger.error(
                            f"[bold red]Error in manage_open_trades on '{self.account_id}':[/bold red] {e}",
                            exc_info=True,
                        )

        except asyncio.CancelledError:
            logger.info(f"[yellow]Pub/Sub Consumer task cancelled for account '{self.account_id}'.[/yellow]")
        except Exception as e:
            logger.error(f"[bold red]Pub/Sub Consumer fatal loop error:[/bold red] {e}", exc_info=True)
        finally:
            await self.stop()

    async def _process_pubsub_message(self, msg: pubsub_v1.subscriber.message.Message):
        """Processes a single Google Cloud Pub/Sub message."""
        try:
            raw_bytes = msg.data
            payload = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                logger.warning(f"[yellow]Ignored invalid Pub/Sub message format: {type(payload)}[/yellow]")
                msg.ack()
                return

            meta = payload.get("_meta", {})
            event_id = meta.get("event_id") or msg.attributes.get("event_id", "unknown")
            pub_time = meta.get("published_at") or str(getattr(msg, "publish_time", "unknown"))
            ordering_key = getattr(msg, "ordering_key", "")

            logger.info(
                f"\n[bold magenta]══════ PUBSUB SIGNAL RECEIVED ══════[/bold magenta]\n"
                f"[dim]Account: {self.account_id} | Subscription: {self.subscription_id}[/dim]\n"
                f"[dim]Message ID: {msg.message_id} | Ordering Key: {ordering_key}[/dim]\n"
                f"[dim]Event ID: {event_id} | Published: {pub_time}[/dim]"
            )

            # Reconstruct ParsedSignal Pydantic model
            clean_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
            parsed_signal = ParsedSignal(**clean_payload)

            logger.info(
                f"[bold cyan]Executing Signal on Kotak Account '{self.account_id}':[/bold cyan] "
                f"{parsed_signal.signal_type.value} for {parsed_signal.contract_key()}"
            )

            # Execute via OrderManager or custom callback in worker thread
            if self.on_signal_callback:
                if asyncio.iscoroutinefunction(self.on_signal_callback):
                    res = await self.on_signal_callback(parsed_signal)
                else:
                    res = await asyncio.to_thread(self.on_signal_callback, parsed_signal)
            else:
                res = await asyncio.to_thread(self.order_manager.process_signal, parsed_signal)

            logger.info(f"[bold green]Execution Result on '{self.account_id}':[/bold green] {res}")

            # Acknowledge the message upon completed execution
            msg.ack()

        except Exception as e:
            logger.error(
                f"[bold red]Error processing Pub/Sub message on account '{self.account_id}':[/bold red] {e}",
                exc_info=True,
            )
            # Acknowledge on format or processing error to avoid poison-pill retry loops
            try:
                msg.ack()
            except Exception:
                pass
