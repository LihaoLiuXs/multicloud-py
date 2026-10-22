# Pub/Sub guide

`TopicClient` publishes messages; `SubscriptionClient` receives and acknowledges
them. AWS SNS/SQS and GCP Pub/Sub implementations are present. Ali is a
placeholder package and cannot yet provide these operations.

## Install and configure

```bash
pip install multicloud-py-pubsub-aws
# Or:
pip install multicloud-py-pubsub-gcp
```

Create the topic, queue/subscription, and any routing between them before using
the SDK. Configure the provider's standard credentials and permissions.
The clients operate on existing resources; they do not provision them.

| Client | Provider ID | Resource |
| --- | --- | --- |
| `TopicClient` for SNS | `awssns` | Topic ARN |
| `TopicClient` for SQS | `awssqs` | Queue URL or name |
| `SubscriptionClient` for SQS | `aws` | Queue URL or name |
| GCP clients | `gcp` | Fully qualified topic or subscription name |

AWS topic IDs distinguish the two publishing backends. A bare `aws` selects
neither topic implementation. GCP names take the form
`projects/PROJECT/topics/TOPIC` or `projects/PROJECT/subscriptions/SUBSCRIPTION`.

## Publish a message

Replace `YOUR_TOPIC_ARN` with an existing SNS topic ARN. Use `awssqs` with
an existing queue to publish directly to SQS, or `gcp` with a full topic name.

```python
from multicloud.pubsub.pubsub_client import Message, TopicClient

with TopicClient(
    provider="awssns", topic_name="YOUR_TOPIC_ARN", region="us-west-2"
) as publisher:
    message = (
        Message.builder()
        .with_body(b'{"order_id": "example"}')
        .with_metadata("content-type", "application/json")
        .build()
    )
    publisher.send(message)
```

`send` is a synchronous operation: it waits for its internally batched send
result. `Message` accepts bytes or strings through `with_body`, and metadata
can be added as key/value pairs or a dictionary.

## Receive and acknowledge

This example consumes one message from an existing SQS queue. Receiving is
blocking; ensure a message is available. Acknowledgment removes it from the
queue. For GCP, use `provider="gcp"` and a full subscription name.

```python
from multicloud.pubsub.pubsub_client import SubscriptionClient

with SubscriptionClient(
    provider="aws", subscription_name="YOUR_QUEUE_URL", region="us-west-2"
) as subscriber:
    message = subscriber.receive()
    print(message.body.decode())
    subscriber.send_ack(message.ack_id)
```

In an application, acknowledge only after successful processing. Use
`send_nack` on failure when `can_nack()` reports support, so the message can
be delivered again. Both current subscription providers support negative
acknowledgment; the delivery timing remains provider-dependent.

## Batch acknowledgments and negative acknowledgments

`send_acks` and `send_nacks` return `Future` objects. Wait on `.result()` to
observe completion and errors. The following fragment assumes `subscriber`
and successfully or unsuccessfully processed `Message` objects already exist:

```python
# Application fragment: successful and failed contain processed Messages.
subscriber.send_acks([message.ack_id for message in successful]).result()
subscriber.send_nacks([message.ack_id for message in failed]).result()
```

`send_nack` and `send_nacks` accept `visibility_timeout=timedelta(...)`.
A per-call timeout overrides the subscription default; the implementation
clamps to SQS's 43,200-second or Pub/Sub's 600-second limit. Lazy nack mode
waits for the existing visibility/deadline rather than making a native update.
These operations request redelivery; they do not guarantee exactly-once
processing or a precise delivery time.

## Attributes and retry classification

`get_attributes()` returns subscription metadata. The current providers do
not populate `approximate_number_of_messages`, so it can be `None`. Do not
treat it as a guaranteed queue-depth metric.

`is_retryable(error)` is a classification helper, not a promise to retry every
failed API call. Currently the AWS subscription implementation returns
`False`; GCP recognizes `DeadlineExceeded`. Use the [API reference](../api/pubsub.md)
for exact signatures and the [roadmap](../roadmap.md#pubsub)
for the complete operation inventory.

## Errors and cleanup

Public operations map provider failures to `multicloud.common.exceptions`
such as `ResourceNotFoundException`, `InvalidArgumentException`, and
`UnAuthorizedException`. For batch acknowledgments, observe errors through
the returned Future as well as errors raised before enqueueing.

Both clients support `with`. Closing flushes pending sends or acknowledgments
and releases resources; it does not delete the topic, queue, or subscription.
