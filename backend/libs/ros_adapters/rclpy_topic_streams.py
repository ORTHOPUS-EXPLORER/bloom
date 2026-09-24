from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from libs.ros_adapters.messages import resolve_message_class
from libs.ros_adapters.qos import AdaptiveSubscription, QosChoice, build_qos_profile
from libs.sessions.topics import RuntimeTopicSample, RuntimeTopicSampleCallback, RuntimeTopicSubscription

logger = logging.getLogger(__name__)

#: A telemetry widget plots numbers, not pixels. Converting one 480p image costs about three quarters of
#: a second on the single executor thread every other subscription shares, so a camera topic on this path
#: stops /ee_pose and /joint_states updating while the arm is still moving.
MAX_STREAMED_SEQUENCE = 8192


class RclpyRuntimeTopicSubscriptionHandle:
    def __init__(self, subscription: AdaptiveSubscription) -> None:
        self._subscription = subscription

    def close(self) -> None:
        self._subscription.close()


class RclpyRuntimeTopicSubscriptionGateway:
    """Stream ROS topic samples through an existing rclpy node."""

    def __init__(
        self,
        node: Any,
        qos_depth: int = 10,
        qos_factory: Callable[[QosChoice], Any] = build_qos_profile,
    ) -> None:
        self._node = node
        self._qos_depth = qos_depth
        self._qos_factory = qos_factory
        self._message_classes: dict[str, type] = {}

    def subscribe(
        self,
        subscription: RuntimeTopicSubscription,
        on_sample: RuntimeTopicSampleCallback,
    ) -> RclpyRuntimeTopicSubscriptionHandle:
        message_type = subscription.message_type or self._resolve_topic_message_type(subscription.topic)
        message_cls = self._get_message_class(message_type)

        reported_oversize = False

        def on_ros_message(message: Any) -> None:
            nonlocal reported_oversize
            oversized_field = find_oversized_field(message)
            if oversized_field is not None:
                if not reported_oversize:
                    reported_oversize = True
                    logger.warning(
                        "Not streaming %s (%s): field %r carries more than %d elements. "
                        "Bulk topics belong on the camera path, not the telemetry socket.",
                        subscription.topic,
                        message_type,
                        oversized_field,
                        MAX_STREAMED_SEQUENCE,
                    )
                return
            on_sample(
                RuntimeTopicSample(
                    message_type=message_type,
                    topic=subscription.topic,
                    value=to_jsonable_ros_message(message),
                )
            )

        ros_subscription = AdaptiveSubscription(
            self._node,
            message_cls,
            subscription.topic,
            on_ros_message,
            self._qos_depth,
            qos_factory=self._qos_factory,
        )
        return RclpyRuntimeTopicSubscriptionHandle(ros_subscription)

    def _subscription_qos(self, topic: str) -> Any:
        """Match the publishers the way `ros2 topic echo` does.

        A fixed volatile profile connects to a transient-local publisher but never
        receives the sample it holds, so a topic published once (a latched status or
        configuration) stayed blank here while `ros2 topic echo` showed it.
        """
        try:
            from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        except ModuleNotFoundError:
            return self._qos_profile

        try:
            publishers = self._node.get_publishers_info_by_topic(topic)
        except Exception:  # noqa: BLE001 - discovery trouble must not block the subscription
            logger.debug("Could not read publisher QoS for %s", topic, exc_info=True)
            publishers = []
        profiles = [info.qos_profile for info in publishers]

        qos = QoSProfile(depth=self._qos_profile)
        # All publishers must keep their last sample, or a transient-local reader would not match the rest.
        if profiles and all(profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for profile in profiles):
            qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        # A reliable reader never matches a best-effort writer.
        if any(profile.reliability == ReliabilityPolicy.BEST_EFFORT for profile in profiles):
            qos.reliability = ReliabilityPolicy.BEST_EFFORT
        return qos

    def _resolve_topic_message_type(self, topic: str) -> str:
        for topic_name, message_types in self._node.get_topic_names_and_types():
            if topic_name == topic and message_types:
                return message_types[0]
        raise RuntimeError(f"Cannot subscribe to {topic}: message type is required or topic is not available.")

    def _get_message_class(self, message_type: str) -> type:
        return resolve_message_class(message_type, self._message_classes, "subscribe to ROS topics")


def to_jsonable_ros_message(message: Any) -> Any:
    try:
        from rosidl_runtime_py.convert import message_to_ordereddict
    except ModuleNotFoundError as exc:
        raise RuntimeError("rosidl_runtime_py is required to serialize ROS topic messages") from exc

    return to_jsonable_value(message_to_ordereddict(message))


def to_jsonable_value(value: Any) -> Any:
    # JSON has no NaN; passive joints report it and the browser would drop the whole message.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): to_jsonable_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable_value(item) for item in value]
    return value


def find_oversized_field(message: Any) -> str | None:
    """The first field too large to serialize on the executor thread, read without converting anything."""
    for slot in getattr(message, "__slots__", ()):
        value = getattr(message, slot, None)
        if isinstance(value, (str, Mapping)):
            continue
        try:
            length = len(value)
        except TypeError:
            continue
        if length > MAX_STREAMED_SEQUENCE:
            return slot.lstrip("_")
    return None
