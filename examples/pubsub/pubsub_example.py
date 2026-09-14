#!/usr/bin/env python3
"""Pub/Sub examples for the Python Multi-Cloud SDK.

Demonstrates cloud-agnostic pub/sub operations. The same code works with any provider.

Usage:
    # AWS SQS Example
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_SESSION_TOKEN=...  # Optional, if using temporary credentials
    export TOPIC_NAME=my-queue  # SQS queue name or ARN
    export SUBSCRIPTION_NAME=my-queue  # SQS queue name for receiving
    export TOPIC_REGION=us-west-2
    python examples/pubsub/pubsub_example.py --provider awssqs

    # AWS SNS Example
    export TOPIC_NAME=arn:aws:sns:us-west-2:123456789012:my-topic
    python examples/pubsub/pubsub_example.py --provider awssns

    # GCP Pub/Sub Example
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
    export TOPIC_NAME=projects/my-project/topics/my-topic
    export SUBSCRIPTION_NAME=projects/my-project/subscriptions/my-subscription
    export TOPIC_REGION=us-central1
    python examples/pubsub/pubsub_example.py --provider gcp

    # Or set provider via environment variable
    export PROVIDER=awssqs  # or "awssns", "gcp" (defaults to "awssqs")
    python examples/pubsub/pubsub_example.py

Prerequisites:
    pip install python-multicloud-sdk[aws]  # or [gcp]
"""

import os
import sys
import time
from datetime import timedelta

from multicloud.common.exceptions import SubstrateSdkException
from multicloud.pubsub.pubsub_client import (
    Message,
    SubscriptionClient,
    TopicClient,
)


def send_single_message():
    """Send a single message to a topic."""
    provider = get_provider()
    client = get_topic_client(provider)

    message = Message.builder().with_body(b"Hello from Python Multi-Cloud SDK!").build()

    print(f"\n=== Send Single Message ===")
    print(f"Provider: {provider}")
    print(f"Topic: {client.topic_name}")

    client.send(message)
    print("Message sent successfully")

    client.close()


def send_with_metadata():
    """Send a message with custom metadata (all values must be strings)."""
    provider = get_provider()
    client = get_topic_client(provider)

    metadata = {
        "source": "python-sdk",
        "version": "1.0",
        "timestamp": str(int(time.time())),
    }
    # Note: All metadata values MUST be strings for GCP compatibility

    message = (
        Message.builder()
        .with_body(b"Message with metadata")
        .with_metadata(metadata)
        .build()
    )

    print(f"\n=== Send with Metadata ===")
    print(f"Metadata: {metadata}")

    client.send(message)
    print("Message with metadata sent successfully")

    client.close()


def send_batch():
    """Send multiple messages individually (SDK batches automatically)."""
    provider = get_provider()
    client = get_topic_client(provider)

    print(f"\n=== Send Batch ===")
    print(f"Sending 5 messages (SDK batches automatically)")

    for i in range(1, 6):
        message = Message.builder().with_body(f"Batch message {i}".encode()).build()
        client.send(message)

    print(f"Sent 5 messages successfully")

    client.close()


def send_with_fifo_attributes():
    """Send messages with FIFO-specific attributes (AWS only)."""
    provider = get_provider()
    if provider not in ("awssqs", "awssns"):
        print(f"\n=== Send with FIFO Attributes ===")
        print(f"Skipped: FIFO attributes are AWS-specific")
        return

    client = get_topic_client(provider)

    # For FIFO queues/topics, you can specify message group and deduplication ID
    metadata = {
        "MessageGroupId": "order-processing",
        "DeduplicationId": f"msg-{int(time.time())}",
    }

    message = (
        Message.builder()
        .with_body(b"FIFO message with ordering")
        .with_metadata(metadata)
        .build()
    )

    print(f"\n=== Send with FIFO Attributes ===")
    print(f"MessageGroupId: {metadata['MessageGroupId']}")
    print(f"DeduplicationId: {metadata['DeduplicationId']}")

    try:
        client.send(message)
        print("FIFO message sent successfully")
    except SubstrateSdkException as e:
        print(f"Note: FIFO attributes require a FIFO queue/topic (.fifo suffix)")
        print(f"Error: {e}")

    client.close()


def send_with_subject():
    """Send message with subject (SNS only)."""
    provider = get_provider()
    if provider != "awssns":
        print(f"\n=== Send with Subject ===")
        print(f"Skipped: Subject is SNS-specific")
        return

    client = get_topic_client(provider)

    metadata = {
        "Subject": "Important Notification",
    }

    message = (
        Message.builder()
        .with_body(b"Message body content")
        .with_metadata(metadata)
        .build()
    )

    print(f"\n=== Send with Subject ===")
    print(f"Subject: {metadata['Subject']}")

    client.send(message)
    print("Message with subject sent successfully")

    client.close()


def send_receive_multiple_messages():
    """Send and receive multiple messages."""
    provider = get_provider()
    topic_client = get_topic_client(provider)
    subscription_client = get_subscription_client(provider)

    print(f"\n=== Send and Receive Multiple Messages ===")
    print(f"Sending and receiving 5 messages...")

    for i in range(1, 6):
        # Send message
        message = (
            Message.builder()
            .with_body(f"Batch message #{i}".encode())
            .with_metadata({"batchId": "batch-1", "messageNumber": str(i)})
            .build()
        )
        topic_client.send(message)

        # Immediately receive
        received = subscription_client.receive()
        if received:
            print(f"Sent and received message #{i}: AckID={received.ack_id}")

    print("Published and received 5 messages")
    topic_client.close()
    subscription_client.close()


def receive_messages():
    """Receive a single message from a subscription."""
    provider = get_provider()
    client = get_subscription_client(provider)

    print(f"\n=== Receive Messages ===")
    print(f"Provider: {provider}")
    print(f"Subscription: {client.subscription_name}")
    print("Waiting for message...")

    # Receive one message
    message = client.receive()

    if message:
        print(f"Received message:")
        print(
            f"  Body: {message.body[:50]}..."
            if len(message.body) > 50
            else f"  Body: {message.body}"
        )
        print(f"  Metadata: {message.metadata}")
        print(f"  Loggable ID: {message.loggable_id}")
        print(f"  Has AckID: {message.ack_id is not None}")
    else:
        print("No messages available")

    client.close()
    return message


def receive_and_acknowledge():
    """Receive a message and acknowledge it."""
    provider = get_provider()
    client = get_subscription_client(provider)

    print(f"\n=== Receive and Acknowledge ===")

    message = client.receive()

    if message and message.ack_id:
        print(f"Received message: {message.body[:30]}...")
        print("Processing message...")

        # Acknowledge successful processing
        client.send_ack(message.ack_id)
        print("Message acknowledged successfully")
    else:
        print("No messages to acknowledge")

    client.close()


def receive_and_nack():
    """Receive a message and negatively acknowledge it for redelivery."""
    provider = get_provider()
    client = get_subscription_client(provider)

    print(f"\n=== Receive and Nack (Negative Acknowledge) ===")

    if not client.can_nack():
        print("Nack is not supported by this provider")
        client.close()
        return

    message = client.receive()

    if message and message.ack_id:
        print(f"Received message: {message.body[:30]}...")
        print("Simulating processing failure...")

        # Nack the message to make it available for redelivery
        client.send_nack(message.ack_id, visibility_timeout=timedelta(seconds=10))
        print("Message nacked (will be redelivered after 10 seconds)")
    else:
        print("No messages to nack")

    client.close()


def check_subscription_attributes():
    """Check subscription attributes like message count."""
    provider = get_provider()
    client = get_subscription_client(provider)

    print(f"\n=== Subscription Attributes ===")

    try:
        attrs = client.get_attributes()
        if attrs.approximate_number_of_messages is not None:
            print(
                f"Approximate messages in queue: {attrs.approximate_number_of_messages}"
            )
        else:
            print("Message count not available for this provider")
    except SubstrateSdkException as e:
        print(f"Could not retrieve attributes: {e}")

    client.close()


def streaming_receive():
    """Continuously receive and process messages in a loop."""
    provider = get_provider()
    client = get_subscription_client(provider)

    print(f"\n=== Streaming Receive ===")
    print("Receiving messages in a loop (will run for ~10 seconds)...")

    message_count = 0
    start_time = time.time()

    try:
        # Receive messages for 10 seconds
        while time.time() - start_time < 10:
            msg = client.receive()
            if msg:
                message_count += 1
                print(f"Received message #{message_count}: {msg.body[:30]}...")

                # Acknowledge immediately after processing
                if msg.ack_id:
                    client.send_ack(msg.ack_id)
            else:
                # No message available, wait a bit
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStream interrupted by user")

    print(f"Total messages received: {message_count}")
    client.close()


def get_topic_client(provider):
    """Create and return a TopicClient instance based on the provider."""
    topic_name = os.environ.get("TOPIC_NAME")
    region = os.environ.get("TOPIC_REGION")
    endpoint = os.environ.get("TOPIC_ENDPOINT")

    if not topic_name or topic_name.strip() == "":
        raise ValueError(
            "Topic name must be provided via 'TOPIC_NAME' environment variable"
        )

    client = TopicClient(
        provider=provider,
        topic_name=topic_name,
        region=region,
        endpoint=endpoint,
    )

    return client


def get_subscription_client(provider):
    """Create and return a SubscriptionClient instance based on the provider."""
    subscription_name = os.environ.get("SUBSCRIPTION_NAME")
    region = os.environ.get("SUBSCRIPTION_REGION") or os.environ.get("TOPIC_REGION")
    endpoint = os.environ.get("SUBSCRIPTION_ENDPOINT") or os.environ.get(
        "TOPIC_ENDPOINT"
    )

    if not subscription_name or subscription_name.strip() == "":
        raise ValueError(
            "Subscription name must be provided via 'SUBSCRIPTION_NAME' environment variable"
        )

    # Map topic providers to subscription providers
    # awssqs and awssns both use "aws" for subscriptions (SQS-based)
    sub_provider = "aws" if provider in ("awssqs", "awssns") else provider

    client = SubscriptionClient(
        provider=sub_provider,
        subscription_name=subscription_name,
        region=region,
        endpoint=endpoint,
    )

    return client


def get_provider():
    """Get the cloud provider to use.

    Can be set via PROVIDER environment variable, otherwise defaults to "awssqs".
    """
    return os.environ.get("PROVIDER", "awssqs")  # or "awssns", "gcp"


def main():
    """Run pub/sub examples."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Python Multi-Cloud SDK Pub/Sub Examples"
    )
    parser.add_argument(
        "--provider",
        choices=["awssqs", "awssns", "gcp"],
        help="Cloud provider (can also set PROVIDER env var)",
    )
    parser.add_argument(
        "--mode",
        choices=["send", "receive", "all"],
        default="all",
        help="Run only send examples, receive examples, or all (default: all)",
    )
    args = parser.parse_args()

    # Allow command-line arg to override environment variable
    if args.provider:
        os.environ["PROVIDER"] = args.provider

    provider = get_provider()
    print("=== Python Multi-Cloud SDK Pub/Sub Examples ===")
    print(f"Provider: {provider}")

    send_examples = [
        ("send_single_message", send_single_message),
        ("send_with_metadata", send_with_metadata),
        ("send_batch", send_batch),
        ("send_with_fifo_attributes", send_with_fifo_attributes),
        ("send_with_subject", send_with_subject),
    ]

    receive_examples = [
        ("send_receive_multiple_messages", send_receive_multiple_messages),
        ("receive_messages", receive_messages),
        ("receive_and_acknowledge", receive_and_acknowledge),
        ("receive_and_nack", receive_and_nack),
        ("check_subscription_attributes", check_subscription_attributes),
        ("streaming_receive", streaming_receive),
    ]

    # Select examples based on mode
    if args.mode == "send":
        examples = send_examples
    elif args.mode == "receive":
        examples = receive_examples
    else:
        examples = send_examples + receive_examples

    passed = []
    failed = []

    for name, fn in examples:
        try:
            fn()
            passed.append(name)
        except Exception as exc:
            failed.append((name, exc))
            print(f"\n✗ FAILED: {name}: {exc}", file=sys.stderr)
            import traceback

            traceback.print_exc()

    # Summary
    print(f"\n{'=' * 50}")
    print(
        f"Results: {len(passed)} passed, {len(failed)} failed "
        f"out of {len(examples)} examples"
    )
    if failed:
        print("\nFailed examples:")
        for name, exc in failed:
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
        return 1
    else:
        print("\n✓ All examples completed successfully!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
