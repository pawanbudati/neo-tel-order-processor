#!/usr/bin/env python3
import argparse
import asyncio

from src.logger import logger


async def run(args):
    from src.kafka.consumer import KafkaSignalConsumer
    from src.kotak_client import KotakClient
    from src.order_manager import OrderManager
    from src.notifier import notifier
    account_id = args.account_id
    notifier.notify_startup(account_id)
    group_id = args.group_id or f"kotak_executor_{account_id}"
    manager = OrderManager(
        kotak_client=KotakClient(),
        account_id=account_id,
        default_lots=args.lots,
        default_product_type=args.product,
        risk_percentage=args.risk_percent,
        capital=args.capital,
    )
    consumer = KafkaSignalConsumer(
        order_manager=manager,
        account_id=account_id,
        group_id=group_id,
        bootstrap_servers=args.kafka_servers,
        topic=args.kafka_topic,
        lots=args.lots,
        risk_percentage=args.risk_percent,
        capital=args.capital,
    )
    await consumer.run()


def main():
    parser = argparse.ArgumentParser(description="Kotak Neo Kafka signal executor")
    parser.add_argument("--account-id", default="default")
    parser.add_argument("--group-id", help="Unique group per account to receive every signal")
    parser.add_argument("--kafka-servers", default=None)
    parser.add_argument("--kafka-topic", default=None)
    parser.add_argument("--lots", type=int, default=None)
    parser.add_argument("--product", choices=("MIS", "NRML"), default=None)
    parser.add_argument("--risk-percent", type=float, default=None)
    parser.add_argument("--capital", type=float, default=None)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info(f"Consumer {args.account_id} stopped by user.")


if __name__ == "__main__":
    main()
