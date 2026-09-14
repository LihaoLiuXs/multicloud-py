# Pub/Sub Guide

The Pub/Sub service provides cloud-agnostic publish-subscribe messaging — send messages to topics and receive them from subscriptions across AWS SNS/SQS, Google Cloud Pub/Sub, and Alibaba Cloud MNS.

## Creating Clients

### Topic Client

`TopicClient` is scoped to a single topic. All operations target that topic.

```python
from multicloud.pubsub import TopicClient

# AWS SNS
with TopicClient(provider="aws", topic_name="orders", region="us-west-2") as client:
    ...

# Google Cloud Pub/Sub
with TopicClient(provider="gcp", topic_name="orders", region="us-central1") as client:
    ...
```

### Subscription Client

`SubscriptionClient` is scoped to a single subscription. All operations target that subscription.

```python
from multicloud.pubsub import SubscriptionClient

# AWS SQS
with SubscriptionClient(provider="aws", subscription_name="orders-processor", region="us-west-2") as client:
    ...

# Google Cloud Pub/Sub
with SubscriptionClient(provider="gcp", subscription_name="orders-processor", region="us-central1") as client:
    ...
```

### Constructor Parameters

**TopicClient:**

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `provider` | `str` | ✅ | Provider id: `"aws"`, `"gcp"`, or `"alibaba"` |
| `topic_name` | `str` | ✅ | Topic name |
| `region` | `str` | | Region/location |
| `endpoint` | `str` | | Endpoint URL override |

**SubscriptionClient:**

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `provider` | `str` | ✅ | Provider id: `"aws"`, `"gcp"`, or `"alibaba"` |
| `subscription_name` | `str` | ✅ | Subscription name |
| `region` | `str` | | Region/location |
| `endpoint` | `str` | | Endpoint URL override |
| `nack_visibility_timeout` | `timedelta` | | Default visibility timeout for nacks |

All parameters are **keyword-only** (enforced by `*` separator).

!!! note
    Credentials are discovered automatically from environment variables, config files, or IAM/ADC. The SDK never accepts explicit credential parameters.

## Publishing Messages

```python
from multicloud.pubsub import TopicClient, Message

with TopicClient(provider="aws", topic_name="orders", region="us-west-2") as client:
    # Build and send a message
    message = (
        Message.builder()
        .with_body(b'{"order_id": "12345", "amount": 99.99}')
        .with_metadata("content-type", "application/json")
        .with_metadata("priority", "high")
        .build()
    )

    client.send(message)
```

### Building Messages

Use the builder pattern to construct messages:

```python
message = (
    Message.builder()
    .with_body("Hello World")  # Accepts str or bytes
    .with_metadata("key1", "value1")
    .with_metadata("key2", "value2")
    .build()
)

# Or provide metadata as a dict
message = (
    Message.builder()
    .with_body(b"Binary data")
    .with_metadata({
        "content-type": "application/octet-stream",
        "version": "1.0"
    })
    .build()
)
```

## Receiving Messages

```python
from multicloud.pubsub import SubscriptionClient

with SubscriptionClient(provider="aws", subscription_name="orders-processor", region="us-west-2") as client:
    # Receive a message
    message = client.receive()

    try:
        # Process the message
        process_order(message.body)

        # Acknowledge successful processing
        client.send_ack(message.ack_id)
    except Exception as e:
        # Negative acknowledge on failure (returns to queue)
        client.send_nack(message.ack_id)
```

### Batch Acknowledgments

Process and acknowledge multiple messages efficiently:

```python
with SubscriptionClient(provider="aws", subscription_name="orders") as client:
    messages = []

    # Receive multiple messages
    for _ in range(10):
        msg = client.receive()
        messages.append(msg)

    # Process all messages
    results = process_batch(messages)

    # Batch acknowledge
    successful_ids = [msg.ack_id for msg, success in zip(messages, results) if success]
    failed_ids = [msg.ack_id for msg, success in zip(messages, results) if not success]

    client.send_acks(successful_ids)
    client.send_nacks(failed_ids)
```

### Negative Acknowledgment with Visibility Timeout

Control when a failed message becomes visible again:

```python
from datetime import timedelta

with SubscriptionClient(provider="aws", subscription_name="orders") as client:
    message = client.receive()

    try:
        process_message(message.body)
        client.send_ack(message.ack_id)
    except RetryableError:
        # Return to queue, invisible for 60 seconds
        client.send_nack(
            message.ack_id,
            visibility_timeout=timedelta(seconds=60)
        )
```

### Subscription Attributes

```python
with SubscriptionClient(provider="aws", subscription_name="orders") as client:
    attrs = client.get_attributes()
    print(f"Approximate messages: {attrs.approximate_number_of_messages}")
```

## Error Handling

All pub/sub operations raise exceptions from the common `multicloud.common.exceptions` hierarchy:

```python
from multicloud.pubsub import TopicClient, Message
from multicloud.common.exceptions import (
    ResourceNotFoundException,
    InvalidArgumentException,
    UnAuthorizedException,
    SubstrateSdkException,
)

with TopicClient(provider="aws", topic_name="orders", region="us-west-2") as client:
    try:
        message = Message.builder().with_body(b"data").build()
        client.send(message)
    except ResourceNotFoundException as e:
        print(f"Topic not found: {e}")
    except UnAuthorizedException as e:
        print(f"Access denied: {e}")
    except SubstrateSdkException as e:
        print(f"SDK error: {e}")
```

| Exception | When Raised |
|-----------|-------------|
| `ResourceNotFoundException` | Topic or subscription does not exist |
| `InvalidArgumentException` | Empty body, invalid metadata, bad visibility timeout |
| `UnAuthorizedException` | Credentials missing, expired, or insufficient |
| `FailedPreconditionException` | Client shutdown, batch handler error |
| `UnknownException` | Unmapped provider error |

## Switching Providers

Change providers by modifying only the `provider` parameter:

```python
# AWS
with TopicClient(provider="aws", topic_name="orders", region="us-west-2") as client:
    client.send(message)

# GCP
with TopicClient(provider="gcp", topic_name="orders", region="us-central1") as client:
    client.send(message)

# Alibaba Cloud
with TopicClient(provider="alibaba", topic_name="orders", region="cn-hangzhou") as client:
    client.send(message)
```

The rest of your code remains identical — the SDK handles all provider-specific differences.

## Complete Example

A full round-trip: publish, receive, acknowledge.

```python
from multicloud.pubsub import TopicClient, SubscriptionClient, Message

# Publish a message
with TopicClient(provider="aws", topic_name="orders", region="us-west-2") as publisher:
    message = (
        Message.builder()
        .with_body(b'{"order_id": "12345", "amount": 99.99}')
        .with_metadata("content-type", "application/json")
        .build()
    )
    publisher.send(message)
    print("Message published")

# Receive and process
with SubscriptionClient(provider="aws", subscription_name="orders-processor", region="us-west-2") as subscriber:
    message = subscriber.receive()
    print(f"Received: {message.body.decode()}")

    # Process and acknowledge
    process_order(message.body)
    subscriber.send_ack(message.ack_id)
    print("Message acknowledged")
```

## Client Lifecycle

Both `TopicClient` and `SubscriptionClient` support the context manager protocol:

```python
# Preferred — automatic cleanup and batch flushing
with TopicClient(provider="aws", topic_name="orders", region="us-west-2") as client:
    client.send(message)

# Manual cleanup
client = TopicClient(provider="aws", topic_name="orders", region="us-west-2")
try:
    client.send(message)
finally:
    client.close()  # Flushes pending batches
```
