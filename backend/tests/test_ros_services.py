"""Service calls: policed, audited, gated by the stop latch, and filled from a payload."""

from fastapi.testclient import TestClient

from apps.bloom_api.main import create_app
from apps.bloom_api.settings import Settings
from libs.config import InMemoryConfigurationRepository
from libs.ros_adapters import RosServiceReceipt, RosServiceRequest
from libs.sessions import InMemoryRuntimeAuditLog, RuntimeStoppedError
from tests.conftest import FAULT_RESET_SERVICE, kinova_with_a_fault_reset_preset


class RecordingServiceGateway:
    def __init__(self, success: bool = True) -> None:
        self.requests: list[RosServiceRequest] = []
        self.success = success

    def call(self, request: RosServiceRequest) -> RosServiceReceipt:
        self.requests.append(request)
        return RosServiceReceipt(
            service=request.service,
            service_type=request.service_type,
            status="called",
            success=self.success,
            detail="Fault cleared." if self.success else "Robot still faulted.",
        )


class UnavailableServiceGateway:
    def call(self, request: RosServiceRequest) -> RosServiceReceipt:
        raise RuntimeError(f"Service {request.service} is not available.")


class StopAtFinalGate:
    def rejection_reason(self) -> None:
        return None

    def execute_if_running(self, operation):
        raise RuntimeStoppedError("Runtime stop engaged during command validation.")

    def execute_blocking_if_running(self, operation):
        raise RuntimeStoppedError("Runtime stop engaged during command validation.")


def create_service_client(gateway=None, audit_log=None) -> TestClient:
    return TestClient(
        create_app(
            Settings(environment="test", allowed_ros_service_calls=(FAULT_RESET_SERVICE,)),
            InMemoryConfigurationRepository({"kinova-manager": kinova_with_a_fault_reset_preset()}),
            ros_service_gateway=gateway,
            runtime_audit_log=audit_log,
        )
    )


RESET_FAULT = {"service": "/fault_controller/reset_fault", "service_type": "example_interfaces/srv/Trigger"}


def test_an_allowed_service_call_reaches_the_gateway() -> None:
    gateway = RecordingServiceGateway()
    client = create_service_client(gateway)

    response = client.post("/api/v1/ros/services/call", json=RESET_FAULT)

    assert response.status_code == 200
    assert response.json() == {
        "service": "/fault_controller/reset_fault",
        "service_type": "example_interfaces/srv/Trigger",
        "status": "called",
        "success": True,
        "detail": "Fault cleared.",
        "response": {},
    }
    [request] = gateway.requests
    assert request.service == "/fault_controller/reset_fault"
    assert request.payload == {}


def test_a_service_outside_the_allowlist_is_refused() -> None:
    gateway = RecordingServiceGateway()
    client = create_service_client(gateway)

    response = client.post(
        "/api/v1/ros/services/call",
        json={"service": "/controller_manager/switch_controller", "service_type": "example_interfaces/srv/Trigger"},
    )

    assert response.status_code == 403
    assert gateway.requests == []


def test_service_calls_are_refused_while_the_stop_latch_is_engaged() -> None:
    gateway = RecordingServiceGateway()
    client = create_service_client(gateway)
    client.post("/api/v1/runtime/stop")

    response = client.post("/api/v1/ros/services/call", json=RESET_FAULT)

    assert response.status_code == 409
    assert gateway.requests == []


def test_service_call_checks_stop_again_at_the_gateway() -> None:
    gateway = RecordingServiceGateway()
    client = create_service_client(gateway)
    client.app.state.runtime_stop_controller = StopAtFinalGate()

    response = client.post("/api/v1/ros/services/call", json=RESET_FAULT)

    assert response.status_code == 409
    assert gateway.requests == []


def test_an_unavailable_service_is_a_gateway_error_not_a_success() -> None:
    client = create_service_client(UnavailableServiceGateway())

    response = client.post("/api/v1/ros/services/call", json=RESET_FAULT)

    assert response.status_code == 502
    assert "not available" in response.json()["detail"]


def test_without_ros_the_call_reports_itself_simulated() -> None:
    client = create_service_client()

    response = client.post("/api/v1/ros/services/call", json=RESET_FAULT)

    assert response.status_code == 200
    assert response.json()["status"] == "simulated"


def test_the_kinova_reset_fault_preset_dispatches_the_service() -> None:
    gateway = RecordingServiceGateway()
    client = create_service_client(gateway)

    response = client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "kinova.reset_fault", "config_id": "kinova-manager"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "called"
    assert body["topic"] == "/fault_controller/reset_fault"
    [request] = gateway.requests
    assert request.service_type == "example_interfaces/srv/Trigger"


def test_service_preset_checks_stop_again_at_the_gateway() -> None:
    gateway = RecordingServiceGateway()
    client = create_service_client(gateway)
    client.app.state.runtime_stop_controller = StopAtFinalGate()

    response = client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "kinova.reset_fault", "config_id": "kinova-manager"},
    )

    assert response.status_code == 409
    assert gateway.requests == []


def test_a_refused_reset_says_so_in_the_detail() -> None:
    client = create_service_client(RecordingServiceGateway(success=False))

    response = client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "kinova.reset_fault", "config_id": "kinova-manager"},
    )

    assert response.status_code == 200
    assert response.json()["detail"] == "Service refused: Robot still faulted."
    # Not "called": the runtime must show a refusal, not a confirmed press.
    assert response.json()["status"] == "refused"


def test_a_refused_service_is_audited_as_rejected() -> None:
    audit_log = InMemoryRuntimeAuditLog()
    client = create_service_client(RecordingServiceGateway(success=False), audit_log)

    client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "kinova.reset_fault", "config_id": "kinova-manager"},
    )

    [record] = [item for item in audit_log.list_records(10) if item.channel == "runtime_action"]
    assert record.status == "rejected"
    assert record.payload_summary == {"call_status": "called", "field_count": 0, "fields": [], "success": False}


def test_a_simulated_service_call_is_audited_as_simulated() -> None:
    audit_log = InMemoryRuntimeAuditLog()
    client = create_service_client(audit_log=audit_log)

    client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "kinova.reset_fault", "config_id": "kinova-manager"},
    )

    [record] = [item for item in audit_log.list_records(10) if item.channel == "runtime_action"]
    assert record.payload_summary == {"call_status": "simulated", "field_count": 0, "fields": [], "success": None}


def test_service_preset_needs_the_app_policy_to_allow_it() -> None:
    gateway = RecordingServiceGateway()
    bundle = kinova_with_a_fault_reset_preset()
    stripped = bundle.model_copy(
        update={
            "applications": tuple(
                app.model_copy(
                    update={"runtime_policy": app.runtime_policy.model_copy(update={"allowed_service_calls": ()})}
                )
                for app in bundle.applications
            )
        }
    )
    client = TestClient(
        create_app(
            Settings(environment="test", allowed_ros_service_calls=(FAULT_RESET_SERVICE,)),
            InMemoryConfigurationRepository({"kinova-manager": stripped}),
            ros_service_gateway=gateway,
        )
    )

    response = client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "kinova.reset_fault", "config_id": "kinova-manager"},
    )

    assert response.status_code == 403
    assert gateway.requests == []


def test_service_calls_are_audited() -> None:
    audit_log = InMemoryRuntimeAuditLog()
    client = create_service_client(RecordingServiceGateway(), audit_log)

    client.post("/api/v1/ros/services/call", json=RESET_FAULT)

    [record] = [r for r in audit_log.list_records() if r.channel == "http_ros_service"]
    assert record.status == "accepted"
    assert record.target == "/fault_controller/reset_fault"


def test_capabilities_report_the_service_seam() -> None:
    client = create_service_client()

    capabilities = {c["id"]: c["available"] for c in client.get("/api/v1/capabilities").json()["capabilities"]}

    assert capabilities["service-dispatcher"] is False

    live = create_service_client(RecordingServiceGateway())
    capabilities = {c["id"]: c["available"] for c in live.get("/api/v1/capabilities").json()["capabilities"]}
    assert capabilities["service-dispatcher"] is True


SET_BOOL_SERVICE = "/gripper/enable"
SET_BOOL_TYPE = "std_srvs/srv/SetBool"


def create_set_bool_client(gateway, audit_log=None, **preset_fields) -> TestClient:
    """The Kinova bundle plus one service preset that takes a request payload."""
    preset = RuntimeActionPreset(
        id="gripper-enable",
        name="Enable gripper",
        kind="service-call",
        command="gripper.enable",
        topic=SET_BOOL_SERVICE,
        message_type=SET_BOOL_TYPE,
        **preset_fields,
    )
    bundle = load_configuration_file(KINOVA_FIXTURE_PATH)
    extended = bundle.model_copy(
        update={
            "applications": tuple(
                app.model_copy(
                    update={
                        "action_presets": (*app.action_presets, preset),
                        "runtime_policy": app.runtime_policy.model_copy(
                            update={
                                "allowed_service_calls": (*app.runtime_policy.allowed_service_calls, SET_BOOL_SERVICE)
                            }
                        ),
                    }
                )
                for app in bundle.applications
            )
        }
    )
    return TestClient(
        create_app(
            Settings(
                environment="test",
                allowed_ros_service_calls=(SET_BOOL_SERVICE,),
                allowed_ros_service_types=(SET_BOOL_TYPE,),
            ),
            InMemoryConfigurationRepository({"kinova-manager": extended}),
            ros_service_gateway=gateway,
            runtime_audit_log=audit_log,
        )
    )


def dispatch_gripper_enable(client: TestClient):
    return client.post(
        "/api/v1/runtime/actions",
        json={"app_id": "kinova-manager", "command": "gripper.enable", "config_id": "kinova-manager"},
    )


def test_a_service_preset_sends_its_payload_text_as_the_request() -> None:
    gateway = RecordingServiceGateway()
    client = create_set_bool_client(gateway, payload_text="{data: true}")

    response = dispatch_gripper_enable(client)

    assert response.status_code == 200
    [request] = gateway.requests
    assert request.service == SET_BOOL_SERVICE
    assert request.payload == {"data": True}


def test_a_service_preset_sends_its_json_payload_as_the_request() -> None:
    gateway = RecordingServiceGateway()
    client = create_set_bool_client(gateway, payload={"data": False})

    dispatch_gripper_enable(client)

    [request] = gateway.requests
    assert request.payload == {"data": False}


def test_an_unreadable_service_payload_is_refused_and_audited() -> None:
    gateway = RecordingServiceGateway()
    audit_log = InMemoryRuntimeAuditLog()
    client = create_set_bool_client(gateway, audit_log, payload_text="[not, a, mapping]")

    response = dispatch_gripper_enable(client)

    assert response.status_code == 422
    assert gateway.requests == []
    [record] = [item for item in audit_log.list_records(10) if item.channel == "runtime_action"]
    assert record.status == "rejected"


def test_a_payload_the_gateway_rejects_is_a_422() -> None:
    class RejectingGateway:
        def call(self, request: RosServiceRequest) -> RosServiceReceipt:
            raise ValueError("Invalid ROS service request: no field 'speed'")

    client = create_set_bool_client(RejectingGateway(), payload_text="{speed: 2}")

    response = dispatch_gripper_enable(client)

    assert response.status_code == 422
    assert "speed" in response.json()["detail"]


def test_the_direct_route_forwards_the_payload_and_the_response() -> None:
    class AnsweringGateway(RecordingServiceGateway):
        def call(self, request: RosServiceRequest) -> RosServiceReceipt:
            self.requests.append(request)
            return RosServiceReceipt(
                service=request.service,
                service_type=request.service_type,
                status="called",
                success=True,
                detail="enabled",
                response={"success": True, "message": "enabled"},
            )

    gateway = AnsweringGateway()
    client = create_set_bool_client(gateway)

    response = client.post(
        "/api/v1/ros/services/call",
        json={"service": SET_BOOL_SERVICE, "service_type": SET_BOOL_TYPE, "payload": {"data": True}},
    )

    assert response.status_code == 200
    assert response.json()["response"] == {"success": True, "message": "enabled"}
    [request] = gateway.requests
    assert request.payload == {"data": True}


class FakeFuture:
    def __init__(self, result) -> None:
        self._result = result

    def add_done_callback(self, callback) -> None:
        callback(self)

    def result(self):
        return self._result


class FakeClient:
    def __init__(self, response) -> None:
        self.response = response
        self.sent = []

    def wait_for_service(self, timeout_sec: float) -> bool:
        return True

    def call_async(self, request):
        self.sent.append(request)
        return FakeFuture(self.response)


class FakeNode:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def create_client(self, service_cls, service: str) -> FakeClient:
        return self.client


def test_the_rclpy_gateway_fills_the_request_from_the_payload() -> None:
    std_srvs = pytest.importorskip("std_srvs.srv")
    fake = FakeClient(std_srvs.SetBool.Response(success=True, message="gripper on"))
    gateway = RclpyRosServiceGateway(FakeNode(fake))

    receipt = gateway.call(
        RosServiceRequest(service=SET_BOOL_SERVICE, service_type=SET_BOOL_TYPE, payload={"data": True})
    )

    [sent] = fake.sent
    assert sent.data is True
    assert receipt.success is True
    assert receipt.detail == "gripper on"
    assert receipt.response == {"success": True, "message": "gripper on"}


def test_the_rclpy_gateway_refuses_a_field_the_request_does_not_have() -> None:
    std_srvs = pytest.importorskip("std_srvs.srv")
    fake = FakeClient(std_srvs.SetBool.Response())
    gateway = RclpyRosServiceGateway(FakeNode(fake))

    with pytest.raises(ValueError, match="Invalid ROS service request"):
        gateway.call(RosServiceRequest(service=SET_BOOL_SERVICE, service_type=SET_BOOL_TYPE, payload={"speed": 2}))

    assert fake.sent == []


def test_the_rclpy_gateway_summarizes_a_response_without_a_message() -> None:
    example_interfaces = pytest.importorskip("example_interfaces.srv")
    fake = FakeClient(example_interfaces.AddTwoInts.Response(sum=5))
    gateway = RclpyRosServiceGateway(FakeNode(fake))

    receipt = gateway.call(
        RosServiceRequest(service="/add", service_type="example_interfaces/srv/AddTwoInts", payload={"a": 2, "b": 3})
    )

    assert receipt.success is None
    assert receipt.response == {"sum": 5}
    assert receipt.detail == 'Service /add answered: {"sum": 5}'
