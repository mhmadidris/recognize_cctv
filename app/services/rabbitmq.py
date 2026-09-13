import json
import os
from datetime import datetime, timezone
from uuid import uuid4


class RabbitMQPublishError(RuntimeError):
    pass


class RabbitMQPublisher:
    def __init__(
        self,
        url=None,
        exchange=None,
        attendance_in_routing_key=None,
        attendance_out_routing_key=None,
        camera_start_routing_key=None,
    ):
        self.url = url or os.getenv("RABBITMQ_URL", "")
        self.exchange = exchange if exchange is not None else os.getenv("RABBITMQ_EXCHANGE", "")
        self.attendance_in_routing_key = attendance_in_routing_key or os.getenv(
            "RABBITMQ_ATTENDANCE_IN_ROUTING_KEY",
            "",
        )
        self.attendance_out_routing_key = attendance_out_routing_key or os.getenv(
            "RABBITMQ_ATTENDANCE_OUT_ROUTING_KEY",
            "",
        )
        self.camera_start_routing_key = camera_start_routing_key or os.getenv(
            "RABBITMQ_CAMERA_START_ROUTING_KEY",
            "",
        )

    def publish_camera_start(self, camera_id, employee_id):
        return self.publish(
            self.camera_start_routing_key,
            {
                "event_type": "cctv.camera.start",
                "camera_id": str(camera_id),
                "employee_id": str(employee_id),
            },
        )

    def publish_attendance(self, attendance_type, payload):
        if attendance_type not in {"in", "out"}:
            raise ValueError("attendance_type must be 'in' or 'out'.")

        routing_key = (
            self.attendance_in_routing_key
            if attendance_type == "in"
            else self.attendance_out_routing_key
        )
        event_payload = {
            **payload,
            "event_type": f"cctv.attendance.{attendance_type}",
            "attendance_type": attendance_type,
        }
        return self.publish(routing_key, event_payload)

    def publish(self, routing_key, payload):
        try:
            import pika
        except ImportError as error:
            raise RabbitMQPublishError(
                "RabbitMQ client is not installed. Install the 'pika' package."
            ) from error

        message = {
            "message_id": str(uuid4()),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

        if not self.url:
            raise RabbitMQPublishError("RABBITMQ_URL is not configured.")
        if not routing_key:
            raise RabbitMQPublishError("RabbitMQ routing key is not configured.")

        try:
            parameters = pika.URLParameters(self.url)
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            if self.exchange:
                exchange_type = os.getenv("RABBITMQ_EXCHANGE_TYPE", "")
                if not exchange_type:
                    raise RabbitMQPublishError("RABBITMQ_EXCHANGE_TYPE is not configured.")
                channel.exchange_declare(
                    exchange=self.exchange,
                    exchange_type=exchange_type,
                    durable=True,
                )
            else:
                channel.queue_declare(queue=routing_key, durable=True)

            channel.basic_publish(
                exchange=self.exchange,
                routing_key=routing_key,
                body=json.dumps(message).encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                    message_id=message["message_id"],
                    timestamp=int(datetime.now(timezone.utc).timestamp()),
                ),
            )
            connection.close()
        except Exception as error:
            raise RabbitMQPublishError(f"Failed to publish RabbitMQ message: {error}") from error

        return {
            "message_id": message["message_id"],
            "routing_key": routing_key,
            "exchange": self.exchange,
            "published_at": message["published_at"],
        }
