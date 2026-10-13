"""MCP Server implementation for FUJIFILM digital camera control."""

import os, threading
from typing import Any, Optional
from datetime import datetime, timezone
import anyio, io
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

#from pathlib import Path
#from jsonschema import validate, ValidationError
#import json

# Import the Camera class from the fujixcam SDK
from fujixcam import Camera, ResultStatus, ResultConfig, ResultFiles, ResultImage
from fujixcam import CameraStatus, ExposureSettings, ExposureOptions, FocusSettings, FocusOptions, ImageSettings, ImageOptions
from fujixcam.camera_api import is_sdk_installed
import base64
from PIL import Image

# Information about this MCP server and its tools
# 1. instructions
# 2. Prompts
# 3. Resources
# 4. PyDantic Schemas

# Initialize MCP server
mcp = MCPServer(
    "MCP server for High-quality imaging using FUJIFILM digital camera",
    instructions="""
    This server provides functions to control a FUJIFILM digital camera connected to a PC,
    Capturing live-view images, adjusting shooting settings, taking photos, and saving the resulting images.
    After starting the server and confirming that the camera is connected and powered on, initiate a session with the camera using `connect_camera`.
    Since the connection is lost if the camera enters auto-power-off mode, you must disable this feature.
    To resume, call `connect_camera` again; when finished using the camera, call `disconnect_camera` and shut down the server.
    Please refer to the documentation for each tool regarding operations during a session.
    """
)

@mcp.prompt()
def system_instructions() -> str:
    """System prompt for operating the camera through this server: the session lifecycle
    (connect_camera / disconnect_camera), which settings can be adjusted by tools and which
    must be set on the camera body beforehand, how to review the current settings and options,
    and how to check the view, the focus target and the effect of the settings before capturing."""
    return """
    # Overview
    This server provides functions to control a FUJIFILM digital camera connected to a PC,
    Capturing live-view images, adjusting shooting settings, taking photos, and saving the resulting images.
    # How to Use
    1. 最初に connect_camera でカメラとのセッションを開始し、tools を使ってカメラの設定を調整して撮影を行い、最後に disconnect_camera を呼んでセッションを終了します。
    2. tools で出来ない設定はセッション開始前にカメラ側で設定しておく必要があります。ほとんどの設定はセッション開始時にカメラの状態として引き継がれます。
    3. cameraとのconnect/disconnect はオーバーヘッドを伴うため、なるべくセッションを維持するようにしてください。
    4. 調整できる設定項目には、露出やフォーカスなどがあり、`get_camera_status` で現在の状態をまとめて確認できます。
    5. 設定で指定する値は`get_exposure_options`など get_XXX_options 系のツールで取得したリストの中から選んで使います。
    6. 設定した撮影条件は、`take_photo` での撮影（ファイルに保存される）や `take_preview` のテストショット（小さい画像データを取得）に反映されます。
    7. 視界の確認には、`take_photo` で撮影した画像ではなく `view_finder_image` でファインダー画像を見てください。
       ファイルに保存する画像は高解像度でサイズが大きいため、繰り返し撮影を行うと処理に時間とコストがかかってしまいます。
    8. 実際に撮影しないと確認できないシャッタースピードや絞りの効果を見たい場合はテストショット機能`take_preview`が利用できます。
    9. finder内のピント位置は、`get_camera_status` で確認、`set_focus_point` で変更でき、`check_target_image` でその場所をクローズアップした画像を確認できます。
    10. バルブ撮影`bulb_exposure`は一時的に長時間露光用の設定に自動的に切り替えて撮影後にもとに戻ります。指定するのは露光時間のみです。
    """

@mcp.resource("docs://architecture")
def get_architecture_doc() -> str:
    """Architecture of the camera control stack: the three layers a request passes through
    (this MCP server, the fujixcam middleware, and the FUJIFILM Camera Control SDK),
    their licenses, and the installation prerequisites for each."""
    return """
    # Architecture Overview
    This server acts as an intermediary between the user and the FUJIFILM camera, providing a structured way to manage camera sessions and operations.
    1. MCP Server Layer: `fujixcam_mcp.server` (MIT License)
        Provides an interface for controlling the camera through tools and manages session states.
        Requires the `fujixcam` Python module and the Camera SDK to be installed and accessible on the PC.
    2. Middleware Layer: `fujixcam` (MIT License)
        This layer abstracts the complexities of direct SDK interactions and provides a simplified interface for the MCP server.
        Installable from PyPI using `pip install fujixcam` or from the source repository.
        When the first initializing the library, the user indicates the folder where the Camera SDK is installed to the library.
    3. Camera SDK Layer: `FUJIFILM Camera Control SDK` (Proprietary License)
        Directly communicates with the FUJIFILM camera hardware.
        The Camera SDK must be downloaded from the manufacturer's website and installed on the PC before using this server.
    for more details on the installation prerequisites and architecture, please refer to the README.
    https://github.com/mterada/fujixcam-mcp#README.md
    """


# ------------------------------------------
# Camara Control Tools
# ------------------------------------------
#
# session management for the camera
#
camera: Optional[Camera] = None
_active_bulb_request_id: Optional[str] = None
_active_bulb_cancel_event: Optional[threading.Event] = None
_active_bulb_control_lock = threading.Lock()

ToolResult = dict[str, Any]

CODE_OK = "OK"
CODE_SDK_NOT_INSTALLED = "SDK_NOT_INSTALLED"
CODE_CAMERA_NOT_CONNECTED = "CAMERA_NOT_CONNECTED"
CODE_INVALID_ARGUMENT = "INVALID_ARGUMENT"
CODE_SDK_CALL_FAILED = "SDK_CALL_FAILED"
CODE_CAPTURE_FAILED = "CAPTURE_FAILED"
CODE_CANCELLED = "CANCELLED"
CODE_NOT_RUNNING = "NOT_RUNNING"

LOG_LEVEL = "INFO"

def _register_active_bulb_request(
    request_id: Optional[str],
    cancel_event: threading.Event,
) -> None:
    global _active_bulb_request_id, _active_bulb_cancel_event
    with _active_bulb_control_lock:
        _active_bulb_request_id = request_id
        _active_bulb_cancel_event = cancel_event


def _clear_active_bulb_request(request_id: Optional[str]) -> None:
    global _active_bulb_request_id, _active_bulb_cancel_event
    with _active_bulb_control_lock:
        if _active_bulb_request_id == request_id:
            _active_bulb_request_id = None
            _active_bulb_cancel_event = None


def _request_active_bulb_cancel() -> bool:
    with _active_bulb_control_lock:
        if _active_bulb_request_id is None:
            return False
        if _active_bulb_cancel_event is not None:
            _active_bulb_cancel_event.set()
            return True
    return False


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result_error_message(result: Any) -> str:
    message = getattr(result, "error_message", None)
    return message or "No error message from camera SDK"


def _response(
    tool: str,
    ok: bool,
    code: str,
    message: str,
    data: Optional[dict[str, Any]] = None,
    error: Optional[dict[str, Any]] = None,
) -> ToolResult:
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "data": data,
        "error": error,
        "meta": {
            "tool": tool,
            "timestamp": _timestamp_utc(),
        },
    }


def _ok(tool: str, message: str, data: Optional[dict[str, Any]] = None) -> ToolResult:
    return _response(tool=tool, ok=True, code=CODE_OK, message=message, data=data, error=None)


def _err(
    tool: str,
    code: str,
    message: str,
    hint: Optional[str] = None,
    retryable: bool = False,
    details: Optional[dict[str, Any]] = None,
) -> ToolResult:
    error: dict[str, Any] = {
        "retryable": retryable,
        "hint": hint,
    }
    if details is not None:
        error["details"] = details
    return _response(tool=tool, ok=False, code=code, message=message, data=None, error=error)


def _camera_name() -> str:
    if camera is None:
        return "Unknown"
    return getattr(camera, "product_name", "Unknown")


def _ensure_camera(tool: str) -> Optional[ToolResult]:
    if camera is None or not camera.is_connected():
        return _err(
            tool=tool,
            code=CODE_CAMERA_NOT_CONNECTED,
            message="Camera is not connected",
            hint="Call connect_camera first",
            retryable=True,
        )
    return None

# Exposure values that the camera itself decides are not readable in advance.
# Report *why* the value is absent so that the caller does not mistake it for a failure.
AUTO_AT_CAPTURE = "Auto (determined by the camera at the moment of capture)"
NOT_APPLICABLE = "N/A (not applicable in this program mode)"

# program mode -> exposure items controlled by the camera, not by the caller
_AUTO_CONTROLLED_ITEMS: dict[str, tuple[str, ...]] = {
    "P": ("shutter_speed", "aperture"),
    "A": ("shutter_speed",),
    "S": ("aperture",),
    "M": (),
}

def _auto_controlled_items(program_mode: Optional[str]) -> tuple[str, ...]:
    return _AUTO_CONTROLLED_ITEMS.get(program_mode or "", ())

def _exposure_value(item: str, value: Optional[str], program_mode: Optional[str]) -> str:
    """Return the current value, or the reason why it is not available."""
    if value:
        return value
    if item in _auto_controlled_items(program_mode):
        return AUTO_AT_CAPTURE
    return NOT_APPLICABLE

def _auto_exposure_warnings(program_mode: Optional[str]) -> list[str]:
    """Tell the caller which exposure values cannot be known before the shutter is released."""
    items = _auto_controlled_items(program_mode)
    if not items:
        return []
    return [
        f"In program mode '{program_mode}', {' and '.join(items)} "
        f"{'is' if len(items) == 1 else 'are'} determined by the camera "
        "according to the brightness of the subject at the moment of capture. "
        "It cannot be read in advance, and get_camera_status reports it as "
        f"'{AUTO_AT_CAPTURE}'. Use take_preview() or take_photo() to see the result of the current settings."
    ]

# Values measured from a captured image are results, not settings.
# They are deliberately reported with unit-suffixed keys and numeric types so that
# they are not mistaken for arguments of the set_* tools: the exact value is often
# absent from the camera's option list (e.g. a measured "1/105" is not selectable).
MEASURED_EXPOSURE_NOTE = (
    "Measured from the captured image (EXIF). These are results, not setting values: "
    "passing them to set_* tools may fail because the exact value is not always in the camera's option list. "
    "To reproduce this exposure, pick the nearest value from "
    "get_shutter_speed_options() / get_aperture_options() / get_iso_sensitivity_options()."
)

def _shutter_speed_text(seconds: float) -> str:
    """Human readable shutter speed. Prefixed with '~' because it is a measured value."""
    if seconds <= 0:
        return "unknown"
    if seconds >= 1:
        return f"~{seconds:.1f}s"
    return f"~1/{round(1 / seconds)}"

def _measured_exposure(folder: Optional[str], files: list[str]) -> Optional[dict[str, Any]]:
    """Read the exposure actually used from the EXIF of the captured JPEG, if available."""
    jpegs = [f for f in files if f.lower().endswith((".jpg", ".jpeg"))]
    if not jpegs or len(jpegs) == 0:
        return None
    filename = jpegs[-1]
    path = os.path.join(folder, filename) if folder else filename
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            tags: dict[int, Any] = dict(exif)
            tags.update(dict(exif.get_ifd(0x8769)))   # ExifOffset: exposure related tags
    except Exception:
        return None

    def _f(tag: int) -> Optional[float]:
        value = tags.get(tag)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    exposure_time = _f(0x829A)      # ExposureTime
    measured: dict[str, Any] = {
        "source": f"EXIF of {filename}",
        "shutter_speed_sec": round(exposure_time, 6) if exposure_time else None,
        "shutter_speed_text": _shutter_speed_text(exposure_time) if exposure_time else None,
        "aperture_f_number": _f(0x829D),                # FNumber
        "iso": tags.get(0x8827),                        # ISOSpeedRatings
        "exposure_compensation_ev": _f(0x9204),         # ExposureBiasValue
        "focal_length_mm": _f(0x920A),                  # FocalLength
        "note": MEASURED_EXPOSURE_NOTE,
    }
    return measured if measured["shutter_speed_sec"] or measured["aperture_f_number"] else None

def _applied_exposure(settings: ExposureSettings) -> Optional[dict[str, Any]]:
    mode = settings.program_mode
    return {
        "program_mode": mode,
        "aperture": _exposure_value("aperture", settings.aperture, mode),
        "shutter_speed": _exposure_value("shutter_speed", settings.shutter_speed, mode),
        "exposure_compensation": _exposure_value("exposure_compensation", settings.exposure_compensation, mode),
    }

@mcp.tool()
def connect_camera(camera_id: str = "0") -> ToolResult:
    """
    Connect to a camera device.
    Camera connection is established if the camera is powered on and properly connected.
    When the camera is powered off and the connection is lost, turn on the camera and call `connect_camera()` again to re-establish the connection.

    Args:
        camera_name: Camera device name (default: "" for primary camera)
    
    Returns:
        json object containing the result of the operation
        { "ok": true, "code": "...", "message": "...", "data": {...}, "error": {...}, "meta": {...} }
        { "ok": false, "code": "error_code", "message": "error_message", "data": {...}, "error": {"hint": "hint for the error", ...}, "meta": {...} }
    """
    global camera
    tool = "connect_camera"

    if not is_sdk_installed():
        return _err(
            tool=tool,
            code=CODE_SDK_NOT_INSTALLED,
            message="FUJIFILM Camera SDK is not installed",
            hint='Install the FUJIFILM Camera SDK and try again\nFor installation, run the following command in the terminal:\npython -c "import fujixcam;fujixcam.install_sdk()"',
            retryable=False,
        )

    if camera is not None:
        camera_name = _camera_name()
        if camera.reconnect():
            return _ok(
                tool,
                "Camera reconnected",
                data={"camera_name": camera_name, "reconnected": True},
            )
        camera = None
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message=f"Failed to reconnect camera {camera_name}",
            hint="Reconnect the camera physically, then call connect_camera again",
            retryable=True,
        )

    # camera_id is currently not used
    camera = Camera(wait_for_connection=False, log_level=LOG_LEVEL)

    if camera is not None and camera.is_connected():
        return _ok(
            tool,
            "Camera connected",
            data={"camera_name": _camera_name(), "camera_id": camera_id},
        )
    return _err(
        tool=tool,
        code=CODE_SDK_CALL_FAILED,
        message=f"Failed to connect to camera {camera_id}",
        hint="Verify camera cable, power, and driver setup",
        retryable=True,
    )

@mcp.tool()
def disconnect_camera() -> ToolResult:
    """
    Disconnect from the camera.
    For optimal performance, avoid frequent connect/disconnect operations.
    
    Returns:
        Status message
    """
    global camera
    tool = "disconnect_camera"

    if camera is None:
        return _ok(tool, "Camera already disconnected", data={"disconnected": False})

#    camera.close()
    camera_name = _camera_name()
    camera = None

    return _ok(tool, "Camera disconnected", data={"camera_name": camera_name, "disconnected": True})

@mcp.tool()
def get_camera_status() -> ToolResult:
    """
    Get current camera connection, status, and settings.
    
    Returns:
        Dictionary containing current camera status and settings
    """
    tool = "get_camera_status"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    result = camera.get_status()
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to get camera status",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )
    status = "Ready" if result.camera_status.is_ready else "Not Ready"

    result = camera.get_config()
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to get camera settings",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    exposure_settings: ExposureSettings = result.settings["exposure"]
    focus_settings: FocusSettings = result.settings["focus"]

    program_mode = exposure_settings.program_mode
    settings = {
        "program_mode": program_mode,
        "shutter_speed": _exposure_value("shutter_speed", exposure_settings.shutter_speed, program_mode),
        "aperture": _exposure_value("aperture", exposure_settings.aperture, program_mode),
        "exposure_compensation": _exposure_value(
            "exposure_compensation", exposure_settings.exposure_compensation, program_mode),
        "iso_sensitivity": exposure_settings.iso_sensitivity,
        "focus_mode": focus_settings.focus_mode,
        "focus_point": focus_settings.af_point or "N/A",
        "mf_distance": focus_settings.mf_distance or "Auto",
        "af_mode": focus_settings.af_mode or "N/A",
        "af_size": focus_settings.af_area_size or "N/A",
        "focal_length": "N/A",
    }

    # experimental: update focal length if zoom options are available
    if camera._zoom_options_dict is not None and camera._zoom_position is not None:
        settings["focal_length"] = camera._zoom_options_dict[camera._zoom_position]

    return _ok(
        tool,
        "Camera status retrieved",
        data={
            "camera_name": _camera_name(),
            "connected": True,
            "status": status,
            "settings": settings,
            "warnings": _auto_exposure_warnings(program_mode),
        },
    )

#
# Exposure settings tools
#
@mcp.tool()
def get_program_mode_options() -> ToolResult:
    """
    Get available program mode options for the camera.
    Program mode 'P' is typically used for automatic exposure settings.
    
    Returns:
        Result message and available program mode options.
        Examples: ['P','A','S','M']
            P - Program mode for automatic exposure settings
            A - Aperture priority mode
            S - Shutter priority mode
            M - Manual mode
    """
    tool = "get_program_mode_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    
    result: ResultConfig = camera.setup()
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to fetch program mode options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(tool,
               "Program mode options retrieved",
               data={"program_mode": result.settings["exposure"].program_mode,
                     "options": result.options["exposure"].program_mode})

@mcp.tool()
def get_exposure_compensation_options() -> ToolResult:
    """
    Get available exposure compensation options for the camera.
    Exposure compensation is used to adjust the brightness of the image when the camera is in program mode 'P','A', or 'S'.
    
    Returns:
        Result message and available options.
        Examples: [ "-3.0EV", "-2.0EV", ..., "+3.0EV"]
    """
    tool = "get_exposure_compensation_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    
    # exposure compensation is invalid in program mode "M"
    warnings = []
    result: ResultConfig = camera.setup()
    if result.success:
        previous_program_mode = result.settings["exposure"].program_mode
        if not previous_program_mode in ["P", "A", "S"]:
            # switch to program mode "P" to fetch exposure compensation options
            result: ResultConfig = camera.setup(program_mode="P")
            if result.success:
                camera.setup(program_mode=previous_program_mode)
                warnings.append(
                    f"These options were read by temporarily switching to program mode 'P'. "
                    f"The camera has been restored to program mode '{previous_program_mode}', "
                    f"in which exposure is manually determined by aperture and shutter speed."
                )

    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to fetch exposure compensation options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(tool,
        "Exposure compensation options retrieved",
        data={"program_mode": result.settings["exposure"].program_mode,
              "exposure_compensation": result.settings["exposure"].exposure_compensation,
              "options": result.options["exposure"].exposure_compensation,
              "warnings": warnings} )

@mcp.tool()
def get_iso_sensitivity_options() -> ToolResult:
    """
    Get available ISO sensitivity options for the camera.
    ISO sensitivity determines the camera sensor's sensitivity to light. Higher ISO values allow for better low-light performance but may introduce more noise into the image.
    This setting is available in all program modes.
    
    Returns:
        Result message and available options.
        Examples: ["ISO 100", "ISO 200", ..., "ISO 1600"]
    """
    tool = "get_iso_sensitivity_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    
    result: ResultConfig = camera.setup()
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to fetch ISO sensitivity options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(tool,
               "ISO sensitivity options retrieved",
               data={"iso_sensitivity": result.settings["exposure"].iso_sensitivity,
                     "options": result.options["exposure"].iso_sensitivity})

@mcp.tool()
def get_aperture_options() -> ToolResult:
    """
    Get available aperture options for the camera.
    Aperture controls the amount of light entering the camera through the lens and affects the depth of field.
    This setting is available in aperture priority mode 'A' and manual mode 'M'.
    
    Returns:
        Result message and available options.
        Examples: ["F1.4", "F2.0", ..., "F16"]
            Smaller aperture values allow more light to enter the camera
    """
    tool = "get_aperture_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    
    # aperture options are valid in program mode "A" or "M"
    warnings: list[str] = []
    result: ResultConfig = camera.setup()
    if result.success:
        previous_program_mode = result.settings["exposure"].program_mode
        if not previous_program_mode in ["A", "M"]:
            result: ResultConfig = camera.setup(program_mode="A")
            if result.success:
                camera.setup(program_mode=previous_program_mode)
                warnings.append(
                    f"These options were read by temporarily switching to program mode 'A'. "
                    f"The camera has been restored to program mode '{previous_program_mode}', "
                    f"in which aperture is controlled by the camera and cannot be set by this tool."
                )
    
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to fetch aperture options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(tool,
               "Aperture options retrieved",
               data={"program_mode": result.settings["exposure"].program_mode,
                     "aperture": result.settings["exposure"].aperture,
                     "options": result.options["exposure"].aperture,
                     "warnings": warnings})

@mcp.tool()
def get_shutter_speed_options() -> ToolResult:
    """
    Get available shutter speed options for the camera.
    Shutter speed controls the duration for which the camera's sensor is exposed to light.
    This setting is available in shutter priority mode 'S' and manual mode 'M'.
    
    Returns:
        Result message and available options.
        Examples: ["1/4000", "1/2000", ..., "1/60", ..., "1/2", "1s", "30s", "1m"]
            Faster shutter speeds allow images to be captured with less motion blur. (1/60 is 1/60 second, 1s is 1 second, 1m is 1 minute)
    """
    tool = "get_shutter_speed_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    
    # shutter speed options are valid in program mode "S" or "M"
    warnings: list[str] = []
    result: ResultConfig = camera.setup()
    if result.success:
        previous_program_mode = result.settings["exposure"].program_mode
        if not previous_program_mode in ["S", "M"]:
            result: ResultConfig = camera.setup(program_mode="S")
            if result.success:
                camera.setup(program_mode=previous_program_mode)
                warnings.append(
                    f"These options were read by temporarily switching to program mode 'S'. "
                    f"The camera has been restored to program mode '{previous_program_mode}', "
                    f"in which shutter_speed is controlled by the camera and cannot be set by this tool."
                )

    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to fetch shutter speed options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(tool,
               "Shutter speed options retrieved",
               data={"program_mode": result.settings["exposure"].program_mode,
                     "shutter_speed": result.settings["exposure"].shutter_speed,
                     "options": result.options["exposure"].shutter_speed,
                     "warnings": warnings})

#
# Camera control tools for setting various camera parameters
#
@mcp.tool()
def set_exposure_automatic(exposure_compensation: Optional[str] = None) -> ToolResult:
    """
    Set camera program mode to automatic exposure 'P' and optionally update exposure compensation.
    Apeture and Shutter speed will be automatically controlled by the camera.
    
    Args:
        exposure_compensation: Exposure compensation value
            refer to get_exposure_compensation_options() for valid options

    Returns:
        Status message and applied settings.
    """
    tool = "set_exposure_automatic"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    
    args: dict[str, Any] = {"program_mode": "P"}
    if exposure_compensation is not None:
        args["exposure_compensation"] = exposure_compensation

    result = camera.setup(**args)
    if result.success:
        return _ok(
            tool,
            "Exposure set to automatic mode",
            data={
                "requested": args,
                "applied": _applied_exposure(result.settings["exposure"]),
                "warnings": _auto_exposure_warnings(result.settings["exposure"].program_mode),
            },
        )

    return _err(
        tool=tool,
        code=CODE_SDK_CALL_FAILED,
        message="Failed to set automatic exposure mode",
        hint="Check supported compensation options and retry",
        retryable=True,
        details={"requested": args, "sdk_error": _result_error_message(result)},
    )

@mcp.tool()
def set_exposure_shutter_priority(shutter_speed: str, exposure_compensation: Optional[str] = None) -> ToolResult:
    """
    Set camera program mode to shutter priority 'S' and optionally update exposure compensation.
    Aperture will be automatically controlled by the camera.
    
    Args:
        shutter_speed: Shutter speed value
            refer to get_shutter_speed_options() for valid options
        exposure_compensation: Exposure compensation value
            refer to get_exposure_compensation_options() for valid options

    Returns:
        Status message and applied settings.
    """
    tool = "set_exposure_shutter_priority"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    args: dict[str, Any] = {"program_mode": "S", "shutter_speed": shutter_speed}
    if exposure_compensation is not None:
        args["exposure_compensation"] = exposure_compensation

    result = camera.setup(**args)
    if result.success:
        return _ok(
            tool,
            "Exposure set to shutter priority mode",
            data={
                "requested": args,
                "applied": _applied_exposure(result.settings["exposure"]),
                "warnings": _auto_exposure_warnings(result.settings["exposure"].program_mode),
            },
        )

    return _err(
        tool=tool,
        code=CODE_SDK_CALL_FAILED,
        message="Failed to set shutter priority mode",
        hint="Check supported shutter speed and compensation options",
        retryable=True,
        details={"requested": args, "sdk_error": _result_error_message(result)},
    )

@mcp.tool()
def set_exposure_aperture_priority(aperture: str, exposure_compensation: Optional[str] = None) -> ToolResult:
    """
    Set camera program mode to aperture priority 'A' and optionally update exposure compensation.
    Shutter speed will be automatically controlled by the camera.
    
    Args:
        aperture: Aperture value
            refer to get_aperture_options() for valid options
        exposure_compensation: Exposure compensation value
            refer to get_exposure_compensation_options() for valid options

    Returns:
        Status message and applied settings.
    """
    tool = "set_exposure_aperture_priority"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    args: dict[str, Any] = {"program_mode": "A", "aperture": aperture}
    if exposure_compensation is not None:
        args["exposure_compensation"] = exposure_compensation

    result = camera.setup(**args)
    if result.success:
        return _ok(
            tool,
            "Exposure set to aperture priority mode",
            data={
                "requested": args,
                "applied": _applied_exposure(result.settings["exposure"]),
                "warnings": _auto_exposure_warnings(result.settings["exposure"].program_mode),
            },
        )

    return _err(
        tool=tool,
        code=CODE_SDK_CALL_FAILED,
        message="Failed to set aperture priority mode",
        hint="Check supported aperture and compensation options",
        retryable=True,
        details={"requested": args, "sdk_error": _result_error_message(result)},
    )

@mcp.tool()
def set_exposure_manual(shutter_speed: str, aperture: str) -> ToolResult:
    """
    Set camera program mode to manual 'M' and update both shutter speed and aperture.
    Both shutter speed and aperture need to be manually set by the user. 
    Exposure compensation is not applied in manual mode.
    
    Args:
        shutter_speed: Shutter speed value
            refer to get_shutter_speed_options() for valid options
        aperture: Aperture value
            refer to get_aperture_options() for valid options

    Returns:
        Status message and applied settings.
    """
    tool = "set_exposure_manual"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    args: dict[str, Any] = {"program_mode": "M", "shutter_speed": shutter_speed, "aperture": aperture}
    result = camera.setup(**args)
    if result.success:
        return _ok(
            tool,
            "Exposure set to manual mode",
            data={
                "requested": args,
                "applied": _applied_exposure(result.settings["exposure"]),
                "warnings": _auto_exposure_warnings(result.settings["exposure"].program_mode),
            },
        )

    return _err(
        tool=tool,
        code=CODE_SDK_CALL_FAILED,
        message="Failed to set manual exposure mode",
        hint="Check supported shutter speed and aperture options",
        retryable=True,
        details={"requested": args, "sdk_error": _result_error_message(result)},
    )

@mcp.tool()
def set_iso_sensitivity(iso_sensitivity: str) -> ToolResult:
    """
    Set camera ISO sensitivity.
    In all program modes, ISO sensitivity can be adjusted independently.
    
    Args:
        iso_sensitivity: ISO sensitivity value
            refer to get_iso_sensitivity_options() for valid options
    
    Returns:
        Status message and applied settings.
    """
    tool = "set_iso_sensitivity"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    requested = {"iso_sensitivity": iso_sensitivity}

    result = camera.setup(iso_sensitivity=iso_sensitivity)
    if result.success:
        return _ok(
            tool,
            "ISO sensitivity updated",
            data={
                "requested": requested,
                "applied": {"iso_sensitivity": result.settings["exposure"].iso_sensitivity},
                "warnings": [],
            },
        )

    return _err(
        tool=tool,
        code=CODE_SDK_CALL_FAILED,
        message=f"Failed to set ISO sensitivity to {iso_sensitivity}",
        hint="Check supported ISO options",
        retryable=True,
        details={"requested": requested, "sdk_error": _result_error_message(result)},
    )

#
# Focus settings tools
#
@mcp.tool()
def get_focus_mode_options() -> ToolResult:
    """
    Retrieve the available focus mode options.
    
    Returns:
        Result message and available focus mode options.
        Examples: ['MF','AF-S','AF-C']
            MF - Manual focus mode
            AF-S - Single autofocus mode
            AF-C - Continuous autofocus mode
    """
    tool = "get_focus_mode_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    result: ResultConfig = camera.setup()
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to get focus mode options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(
        tool,
        "Focus mode options retrieved",
        data={
            "focus_mode": result.settings["focus"].focus_mode,
            "options": result.options["focus"].focus_mode,
            "warnings": [],
        },
    )

@mcp.tool()
def get_focus_distance_options() -> ToolResult:
    """
    Retrieve the available focus distance options for manual focus adjustments.
    Depending on the lens attached to the camera, the available focus distance range may vary.
    
    Returns:
        Result message and available mf_distance range value as options.
        `INF` means infinity, `MOD` means the minimum object distance, and `STEP` indicates the effective incremental value.
    """
    tool = "get_focus_distance_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    warnings = []
    result: ResultConfig = camera.setup()
    if result.success:
        previous_focus_mode = result.settings["focus"].focus_mode
        if previous_focus_mode != "MF":
            result: ResultConfig = camera.setup(focus_mode="MF")
            if result.success:
                camera.setup(focus_mode=previous_focus_mode)
                warnings.append(
                    f"These options were read by temporarily switching to manual focus mode 'MF'. "
                    f"The camera has been restored to its previous focus mode '{previous_focus_mode}',"
                    f"in which focus distance is controlled by the camera and cannot be set by this tool."
                )

    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to get focus distance options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    focus_options: FocusOptions = result.options["focus"]

    return _ok(
        tool,
        "Focus distance options retrieved",
        data={
            "focus_mode": result.settings["focus"].focus_mode,
            "mf_distance": result.settings["focus"].mf_distance,
            "options": dict(zip(["INF", "MOD", "STEP"], focus_options.mf_distance)),
            "warnings": warnings
        }
    )

@mcp.tool()
def get_focus_point_options() -> ToolResult:
    """
    Retrieve the available focus point options for the camera.

    Returns:
        Status message and available focus point options.
            h: Horizontal focus point value.
            v: Vertical focus point value.
            0 is the center of the screen, +h is right, -h is left, +v is up, -v is down.
    """
    tool = "get_focus_point_options"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    warnings = []
    result: ResultConfig = camera.setup()
    if result.success:
        previous_focus_mode = result.settings["focus"].focus_mode
        previous_af_mode = result.settings["focus"].af_mode
        if previous_af_mode != "SINGLE":
            result: ResultConfig = camera.setup(focus_mode="MF")
            if result.success:
                camera.setup(focus_mode=previous_focus_mode)
                warnings.append(
                    f"These options were read by temporarily switching to manual focus mode 'MF'. "
                    f"The camera has been restored to its previous focus mode '{previous_focus_mode}/{previous_af_mode}',"
                    f"in which focus point is controlled by the camera and cannot be set by this tool."
                )

    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to fetch focus point options",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(
        tool,
        "Focus point options retrieved",
        data={"focus_mode": result.settings["focus"].focus_mode,
              "af_area_mode": result.settings["focus"].af_mode,
              "focus_point": result.settings["focus"].af_point,
              "options": {
                  "h": result.options["focus"].af_point_h,
                  "v": result.options["focus"].af_point_v,
                  "orientation": "+h is right, -h is left, +v is up, -v is down (origin is the center of the screen)" },
              "warnings": warnings }
    )

@mcp.tool()
def set_manual_focus_mode( mf_distance: Optional[int] = None) -> ToolResult:
    """
    This tool sets the camera to manual focus mode and retrieves the current manual focus distance along with the available range for manual focus adjustments.
    
    Returns:
        Result message and applied settings with available mf_distance range options.
        INF means infinity, MOD means the minimum object distance, and STEP indicates the effective increment value.
    """
    tool = "set_manual_focus_mode"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None

    args: dict[str, Any] = { "focus_mode": "MF" }
    if mf_distance is not None:
        args["mf_distance"] = mf_distance

    result: ResultConfig = camera.setup(**args)
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Failed to set manual focus mode",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result)},
        )

    return _ok(
        tool,
        "Manual focus mode enabled",
        data={
            "requested": args,
            "applied": {
                "focus_mode": result.settings["focus"].focus_mode,
                "mf_distance": result.settings["focus"].mf_distance
            },
            "options": dict(zip(["INF", "MOD", "STEP"], result.options["focus"].mf_distance)),
            "warnings": [],
        },
    )

@mcp.tool()
def set_auto_focus_mode(focus_mode: str, af_area_mode: Optional[str] = None) -> ToolResult:
    """
    Set camera to auto focus mode.
    Optionally, the AF area mode can be specified.
    
    Args:
        mode: 'AF-S' Auto focus single, 'AF-C' Auto focus continuous
        af_area_mode: Optional AF area mode, 'SINGLE', 'ZONE', 'WIDETRACKING' are available.
    
    Returns:
        Result message and applied settings.
    """
    tool = "set_auto_focus_mode"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    requested = {"focus_mode": focus_mode, "af_area_mode": af_area_mode}

    valid_focus_modes = ["AF-S", "AF-C"]
    if not focus_mode in valid_focus_modes:
        return _err(
            tool=tool,
            code=CODE_INVALID_ARGUMENT,
            message=f"Invalid auto focus mode: {focus_mode}",
            hint="Use AF-S or AF-C",
            retryable=False,
            details={"valid_values": valid_focus_modes, "requested": requested},
        )
    
    if af_area_mode is not None:
        valid_af_area_modes = ["SINGLE", "ZONE", "WIDETRACKING"]
        if not af_area_mode in valid_af_area_modes:
            return _err(
                tool=tool,
                code=CODE_INVALID_ARGUMENT,
                message=f"Invalid AF area mode: {af_area_mode}",
                hint="Use SINGLE, ZONE, or WIDETRACKING",
                retryable=False,
                details={"valid_values": valid_af_area_modes, "requested": requested},
            )
        result: ResultConfig = camera.setup(focus_mode=focus_mode, af_mode=af_area_mode)
    else:
        result = camera.setup(focus_mode=focus_mode)

    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message=f"Failed to set auto focus mode to {focus_mode}",
            hint="Retry when camera is ready",
            retryable=True,
            details={"sdk_error": _result_error_message(result), "requested": requested},
        )

    focus_settings: FocusSettings = result.settings["focus"]
    focus_options: FocusOptions = result.options["focus"]

    return _ok(
        tool,
        "Auto focus mode updated",
        data={
            "requested": requested,
            "applied": {
                "focus_mode": focus_settings.focus_mode,
                "af_mode": focus_settings.af_mode,
                "af_area_size": focus_settings.af_area_size if focus_settings.af_mode in ["SINGLE", "ZONE"] else "N/A"
            },
            "options": { "af_area_size": focus_options.af_area_size },
            "warnings": [],
        },
    )

@mcp.tool()
def set_focus_point(h: int, v: int) -> ToolResult:
    """
    Set the focus point on the camera's viewfinder.
    h, v specify the coordinates of the focus point.
    (0,0) represents the center of the finder screen, and coordinates are relative to this center.
    The sign follows the screen as you see it: +h is to the RIGHT, +v is UP
    (so the lower left of the screen is negative and the upper right is positive).
    After moving the focus point, the camera will attempt to auto focus on that point.
    Only effective in manual focus mode or when AF area mode is SINGLE.
    
    Args:
        h: Horizontal focus point value.
        v: Vertical focus point value.
        0 is the center of the screen, +h is right, -h is left, +v is up, -v is down.
    
    Returns:
        ToolResult indicating the success or failure of the operation.
        Applied focus point coordinates and available options.
        Use check_target_image() afterwards to verify what the camera is actually focused on.
    """
    tool = "set_focus_point"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    requested = {"h": h, "v": v}

    current_focus_settings: FocusSettings = camera.setup().settings["focus"]
    if not (current_focus_settings.focus_mode == "MF" or current_focus_settings.af_mode == "SINGLE"):
        return _err(
            tool=tool,
            code=CODE_INVALID_ARGUMENT,
            message="Focus point can only be set in MF mode or when AF area mode is SINGLE",
            hint="Switch to MF mode or set AF area mode to SINGLE",
            retryable=False,
            details={},
        )

    current_focus_options: FocusOptions = camera.setup().options["focus"]
    if not (h in current_focus_options.af_point_h and v in current_focus_options.af_point_v):
        return _err(
            tool=tool,
            code=CODE_INVALID_ARGUMENT,
            message=f"Out of valid range for focus point: h={h}, v={v}",
            hint="Use valid focus point coordinates",
            retryable=False,
            details={"valid_h": current_focus_options.af_point_h, "valid_v": current_focus_options.af_point_v, "requested": requested},
        )

    result = camera.focus_on(h, v, af_on=True) 
    if not result.success:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message=f"Failed to set focus point to h={h}, v={v}, or perform autofocus",
            hint="Retry to set the smaller values within the valid range, or adjust the distance to the subject",
            retryable=True,
            details={"sdk_error": _result_error_message(result), "requested": requested},
        )
    camera.focus_off()  # if AF-C, stop continuous autofocus

    return _ok(
        tool,
        "Focus point updated",
        data={
            "requested": requested,
            "applied": requested,
            "options": {
                "h": current_focus_options.af_point_h,
                "v": current_focus_options.af_point_v,
                "orientation": "+h is right, -h is left, +v is up, -v is down (origin is the center of the screen)",
            },
            "warnings": [],
        },
    )

#
# image capture tools
#
@mcp.tool()
def view_finder_image( size: Optional[str]=None, temp_filename: Optional[str]=None ) -> ToolResult:
    """
    Capture the current image from the camera's viewfinder for preview purposes.
    
    Args:
        size: Optional[str] = "S"|"M"  # Desired size of the viewfinder image
            "S" is small (320x240), default or "M" is medium (640x480)
        temp_filename: Optional[str] = None  # Temporary file to save the captured viewfinder image
            If `temp_filename` is provided, the captured viewfinder image will be saved to the specified temporary file.
            If `temp_filename` is not provided, the image will be returned as a base64-encoded string.
            temp_filename's extension must be jpg or jpeg.
            Caller is responsible for providing a valid filepath and deleting the temporary file when it is no longer needed.

    Returns:
        applied["filename"]: str  # The temporary filename if `temp_filename` was provided and the image was saved successfully
        applied["image"]: str  # The base64-encoded image data if `temp_filename` was not provided
        The base64-encoded string represents the JPEG image data.

    """
    tool = "view_finder_image"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    if size is None or not size in ["S", "M"]:
        size = "M"
    temp_filename = temp_filename or None
    requested = {"size": size, "temp_filename": temp_filename}

    # Start the camera's liveview to capture the current viewfinder image by default quality, size, zoom
    finder: ResultStatus = camera.start_liveview(size=size)    # size="L", zoom="x1.0"
    if finder.success:
        result: ResultImage = camera.get_liveview_image()
        # Stop the liveview and restore the previous autofocus mode if it was changed.
        camera.stop_liveview()
        if result.success:
            if temp_filename is not None and result.image_data is not None:
                try:
                    with open(temp_filename, "wb") as f:
                        f.write(result.image_data)
                    result.image_data = None
                except Exception as e:
                    return _err(
                        tool=tool,
                        code=CODE_CAPTURE_FAILED,
                        message="Failed to write viewfinder image to temporary file",
                        hint="Check file permissions and available disk space",
                        retryable=True,
                        details={"exception": str(e), "requested": requested},
                    )
            return _ok(
                tool,
                "Finder image captured",
                data={
                    "requested": requested,
                    "applied": {
                        # data:image/jpeg;base64,
                       "image": base64.b64encode(result.image_data).decode() if result.image_data is not None else "",
                       "filename": temp_filename if temp_filename is not None else ""
                    },
                    "warnings": [],
                },
            )
        else:
            message = result.error_message or "get liveview image failed"
    else:
        message = finder.error_message or "start liveview failed"
        
    return _err(
        tool=tool,
        code=CODE_CAPTURE_FAILED,
        message="Failed to capture finder image",
        hint="Verify camera readiness",
        retryable=True,
        details={"sdk_error": message }
    )

@mcp.tool()
def check_target_image( size: Optional[str]=None, temp_filename:Optional[str]=None, zoom_selector:int=1 ) -> ToolResult:
    """
    Captures a close-up image of the current focus point in the viewfinder.

    Args:
        size: Optional[str] = "S"|"M"  # Desired size of the viewfinder image
            "S" is small (320x240), default or "M" is medium (640x480)
        temp_filename: Optional[str] = None  # Temporary file to save the captured viewfinder image
            If `temp_filename` is provided, the captured viewfinder image will be saved to the specified temporary file.
            If `temp_filename` is not provided, the image will be returned as a base64-encoded string.
            temp_filename's extension must be jpg or jpeg.
            Caller is responsible for providing a valid filepath and deleting the temporary file when it is no longer needed.
        zoom_selector: int = 1  # The zoom selector to use for capturing the target image
            1: lower zoom level ( approximately x2.0 - 2.5 magnification depending on the camera model )
            2: Higher zoom level ( approximately x4.0 - 6.0 magnification depending on the camera model )

    Returns:
        applied["filename"]: str  # The temporary filename if `temp_filename` was provided and the image was saved successfully
        applied["image"]: str  # The base64-encoded image data if `temp_filename` was not provided
        The base64-encoded string represents the JPEG image data.
    """
    tool = "check_target_image"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    if size is None or not size in ["L", "M", "S"]:
        size = "M"
    temp_filename = temp_filename or None
    if not zoom_selector in [1, 2]:
        zoom_selector = 1
    requested = {"size": size, "temp_filename": temp_filename, "zoom_selector": zoom_selector}

    result = camera.get_liveview_config()
    if result.success:
        options = result.options["liveview"].liveview_zoom
        result = camera.get_monitor_image( size=size, zoom=options[zoom_selector] )
        if result.success and result.image_data is not None:
            if temp_filename is not None:
                try:
                    with open(temp_filename, "wb") as f:
                        f.write(result.image_data)
                    result.image_data = None
                except Exception as e:
                    message = str(e)
                    return _err(
                        tool=tool,
                        code=CODE_CAPTURE_FAILED,
                        message="Failed to save target image to temporary file",
                        hint="Verify the temporary file path and permissions",
                        retryable=False,
                        details={"file_error": message, "requested": requested} 
                    )

            return _ok(
                tool,
                "Target image captured",
                data={
                    "requested": requested,
                    "applied": {
                        "image": base64.b64encode(result.image_data).decode() if result.image_data is not None else "",
                        "filename": temp_filename if temp_filename is not None else ""
                    },
                    "warnings": [],
                },
            )

    return _err(
        tool=tool,
        code=CODE_CAPTURE_FAILED,
        message="Failed to capture target image",
        hint="Verify camera readiness",
        retryable=True,
        details={"sdk_error": result.error_message, "requested": requested} 
    )

@mcp.tool()
def take_preview( size: Optional[str]=None, temp_filename: Optional[str]=None ) -> ToolResult:
    """
    The test shot is intended for quick review and is captured using the current camera settings as same as those used by `take_photo()` except for the final image resolution.
    Camera settings for saving image quality and format do not affect the test shot.
    
    Args:
        size: Optional[str] = "S"|"M"|None  # Desired size of the test shot image
            "S" is small (320x240), default or "M" is VGA or larger (depending on camera capabilities)
        temp_filename: Optional[str] = None  # Temporary file to save the captured test shot image
            If `temp_filename` is provided, the captured viewfinder image will be saved to the specified temporary file.
            If `temp_filename` is not provided, the image will be returned as a base64-encoded string.
            temp_filename's extension must be jpg or jpeg.
            Caller is responsible for providing a valid filepath and deleting the temporary file when it is no longer needed.

    Returns:
        applied["filename"]: str  # The temporary filename if `temp_filename` was provided and the image was saved successfully
        applied["image"]: str  # The base64-encoded image data if `temp_filename` was not provided
        The base64-encoded string represents the JPEG image data.

    """
    tool = "take_preview"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    if size is None or not size in ["S", "M"]:
        size = "M"
    temp_filename = temp_filename or None
    requested = {"size": size, "temp_filename": temp_filename}

    result: ResultImage = camera.preview()
    if result.success and result.image_data is not None:
        if size == "S":
            target_size = (320, 240)    # QVGA
            try:
                with Image.open(io.BytesIO(result.image_data)) as img:
                    if img.mode in {"RGBA", "LA", "P"}:
                        img = img.convert("RGB")
                    resample = getattr(Image, "Resampling", Image).LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS # type: ignore
                    resized = img.resize(target_size, resample)
                    buffer = io.BytesIO()
                    resized.save(buffer, format="JPEG", quality=90)
                    result.image_data = buffer.getvalue()
            except Exception as exc:
                return _err(
                    tool=tool,
                    code=CODE_CAPTURE_FAILED,
                    message="Failed to resize preview image",
                    hint="Retry when camera is ready",
                    retryable=True,
                    details={"exception": str(exc), "requested": requested},
                )
        else:
            target_size = None

        if temp_filename is not None and result.image_data is not None:
            try:
                with open(temp_filename, "wb") as f:
                    if isinstance(result.image_data, (bytearray, memoryview)):
                        f.write(bytes(result.image_data))
                    else:
                        f.write(result.image_data)
                result.image_data = None
            except Exception as exc:
                return _err(
                    tool=tool,
                    code=CODE_CAPTURE_FAILED,
                    message="Failed to write preview image to temporary file",
                    hint="Check file permissions and available disk space",
                    retryable=True,
                    details={"exception": str(exc), "requested": requested},
                )

        return _ok(
            tool,
            "Preview captured",
            data={
                "requested": requested,
                "applied": {
                    # data:image/jpeg;base64,
                    "image": base64.b64encode(result.image_data).decode() if result.image_data is not None else "",
                    "filename": temp_filename if temp_filename is not None else "",
                },
                "warnings": [],
            },
        )

    return _err(
        tool=tool,
        code=CODE_CAPTURE_FAILED,
        message="Failed to capture test shot",
        hint="Verify camera readiness",
        retryable=True,
        details={"sdk_error": result.error_message or "unknown error"},
    )

@mcp.tool()
def take_photo(folder: Optional[str] = None, successive_shots:Optional[int] = 1) -> ToolResult:
    """
    Capture a photo from the camera and save it to local storage.
    
    Args:
        folder: Optional folder path to save the photo.
                If not provided, current folder will be used.
                Filename will be generated based on the current timestamp.
        successive_shots: Number of consecutive shots to capture. Defaults to 1.
    
    Returns:
        Path to the folder and file where the captured images were saved.
        Example of files: ['2026_0101_123456.raf', '2026_0101_123456.jpg']
        note: result depends on the camera settings for image quality and format.
        applied["exposure_used"]: the exposure actually used, measured from the EXIF of the saved JPEG.
            In P/A/S modes this is the only way to know the value the camera chose at the moment of capture.
            These are measured results, not setting values: the exact value may not exist in the camera's
            option list, so pick the nearest one from get_*_options() to reproduce the same exposure.
    """
    tool = "take_photo"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    requested = {"folder": folder, "successive_shots": successive_shots}

    if folder is not None:
        os.makedirs(folder, exist_ok=True)
        if not os.path.isdir(folder):
            return _err(
                tool=tool,
                code=CODE_INVALID_ARGUMENT,
                message=f"Folder does not exist: {folder}",
                hint="Specify an existing folder path",
                retryable=False,
                details={"requested": requested},
            )
    
    result: ResultFiles = camera.capture(folder=folder, count=successive_shots)
    if result.success:
        files = list(result.image_files or [])
        applied: dict[str, Any] = {
            "folder": result.folder,
            "files": files,
            "count": len(files),
        }
        # The exposure the camera actually chose is only knowable after the shot.
        measured = _measured_exposure(result.folder, files)
        if measured is not None:
            applied["exposure_used"] = measured
        return _ok(
            tool,
            "Photo captured",
            data={
                "requested": requested,
                "applied": applied,
                "warnings": [],
            },
        )

    return _err(
        tool=tool,
        code=CODE_CAPTURE_FAILED,
        message="Failed to capture photo",
        hint="Verify camera readiness and storage destination",
        retryable=True,
        details={"requested": requested, "sdk_error": _result_error_message(result)},
    )


@mcp.tool()
def cancel_bulb_exposure() -> ToolResult:
    """
    Request cancelling currently running bulb exposure WITHOUT saving image files.
    If you want to end the exposure earlier than the scheduled time and save the image, cancel the `bulb_exposure` request itself.

    Returns:
        Status of cancel request dispatch.
    """
    tool = "cancel_bulb_exposure"

    if not _request_active_bulb_cancel():
        return _err(
            tool=tool,
            code=CODE_NOT_RUNNING,
            message="No running bulb exposure",
            hint="Call bulb_exposure first",
            retryable=True,
        )

    return _ok(
        tool,
        "Bulb exposure cancel requested",
        data={"requested": {"action": "cancel"}},
    )

@mcp.tool()
async def bulb_exposure(
    folder: Optional[str] = None,
    exposure_time: int = 1,
    ctx: Context = None,  # type: ignore
) -> ToolResult:
    """
    Capture a photo in bulb mode from the camera and save it to local storage.
    When this function is called, the current shooting settings are temporarily saved and switched to the settings required for bulb mode.
    ISO sensitivity will be automatically determined.
    After the bulb exposure is finished, the original settings are restored.
    If this function call is cancelled, the exposure will stop at that point and the image will be saved.
    
    Args:
        folder: Optional folder path to save the photo
                If not provided, current folder will be used.
                Filename will be generated based on the current timestamp.
        exposure_time: Exposure time in seconds for bulb mode.

    Returns:
        Path to the folder and file where the captured images were saved.
        Example of files: ['2026_0101_123456.raf', '2026_0101_123456.jpg']
        note: result depends on the camera settings for image quality and format.
    """
    tool = "bulb_exposure"
    guard = _ensure_camera(tool)
    if guard is not None:
        return guard
    assert camera is not None
    requested = {"folder": folder, "exposure_time": exposure_time}

    if folder is not None:
        os.makedirs(folder, exist_ok=True)
        if not os.path.isdir(folder):
            return _err(
                tool=tool,
                code=CODE_INVALID_ARGUMENT,
                message=f"Folder does not exist: {folder}",
                hint="Specify an existing folder path",
                retryable=False,
                details={"requested": requested},
            )

    callback_action = "continue"
    elapsed_snapshot = 0.0
    total_snapshot = float(exposure_time)
    state_lock = threading.Lock()
    cancel_requested = threading.Event()
    finish_requested = threading.Event()

    def callback(elapsed_time: float, total_seconds: float):
        nonlocal callback_action, elapsed_snapshot, total_snapshot
        with state_lock:
            elapsed_snapshot = elapsed_time
            total_snapshot = total_seconds
            if cancel_requested.is_set():
                callback_action = "cancel"
                return "cancel"
            if finish_requested.is_set():
                callback_action = "finish"
                return "finish"
            callback_action = "continue"
        return "continue"

    result_holder: dict[str, ResultFiles] = {}
    worker_error: dict[str, Exception] = {}

    def run_capture() -> None:
        try:
            result_holder["result"] = camera.bulb_capture(  # type: ignore
                folder=folder,
                exposure_time=exposure_time,
                callback=callback,
            )
        except Exception as exc:
            worker_error["error"] = exc

    worker = threading.Thread(target=run_capture, daemon=True)
    worker.start()

    request_id = ctx.request_id if ctx is not None else None
    _register_active_bulb_request(request_id=request_id, cancel_event=cancel_requested)

    cancellation_requested = False
    resolved_cancel_action = "none"
    resolved_cancel_action_source = "none"
    try:
        while worker.is_alive():
            await anyio.sleep(0.1)
            if ctx is not None:
                with state_lock:
                    elapsed = elapsed_snapshot
                    total = total_snapshot
                if total > 0:
                    await ctx.report_progress(elapsed, total)
    except anyio.get_cancelled_exc_class():
        cancellation_requested = True
        # Direct MCP request cancellation is treated as finish.
        resolved_cancel_action = "finish"
        resolved_cancel_action_source = "mcp_request_cancellation"
        finish_requested.set()

        # Wait in a shielded section so callback can stop capture safely.
        with anyio.CancelScope(shield=True):
            while worker.is_alive():
                await anyio.sleep(0.05)
    finally:
        _clear_active_bulb_request(request_id=request_id)

    if "error" in worker_error:
        return _err(
            tool=tool,
            code=CODE_SDK_CALL_FAILED,
            message="Bulb capture raised an unexpected exception",
            hint="Retry when camera is ready",
            retryable=True,
            details={
                "requested": requested,
                "exception": str(worker_error["error"]),
            },
        )

    result = result_holder.get("result")
    if result is None:
        return _err(
            tool=tool,
            code=CODE_CAPTURE_FAILED,
            message="Bulb capture did not return a result",
            hint="Retry when camera is ready",
            retryable=True,
            details={ "requested": requested },
        )

    if callback_action == "cancel":
        files = list(result.image_files or [])
        return _err(
            tool=tool,
            code=CODE_CANCELLED,
            message="Bulb exposure cancelled by tool request",
            hint="Retry capture if needed",
            retryable=False,
            details={
                "requested": requested,
                "resolved_cancel_action": "cancel",
                "resolved_cancel_action_source": "cancel_bulb_exposure",
                "callback_action": callback_action,
                "folder": result.folder,
                "files": files,
                "sdk_error": _result_error_message(result),
            },
        )

    if cancellation_requested and resolved_cancel_action == "cancel":
        files = list(result.image_files or [])
        return _err(
            tool=tool,
            code=CODE_CANCELLED,
            message="Bulb exposure cancelled by client request",
            hint="Retry capture if needed",
            retryable=False,
            details={
                "requested": requested,
                "resolved_cancel_action": resolved_cancel_action,
                "resolved_cancel_action_source": resolved_cancel_action_source,
                "callback_action": callback_action,
                "folder": result.folder,
                "files": files,
                "sdk_error": _result_error_message(result),
            },
        )

    if result.success:
        files = list(result.image_files or [])
        message = "Bulb photo captured"
        warnings: list[str] = []
        if callback_action == "finish":
            message = "Bulb photo finished early by stop request"
            warnings.append("Exposure finished earlier than requested exposure_time")

        return _ok(
            tool,
            message,
            data={
                "requested": requested,
                "applied": {
                    "folder": result.folder,
                    "files": files,
                    "count": len(files),
                    "exposure_used": _measured_exposure(result.folder, files) or {},
                    "callback_action": callback_action,
                    "resolved_cancel_action": resolved_cancel_action,
                    "resolved_cancel_action_source": resolved_cancel_action_source,
                },
                "warnings": warnings,
            },
        )

    return _err(
        tool=tool,
        code=CODE_CAPTURE_FAILED,
        message="Failed to capture bulb photo",
        hint="Verify bulb mode support and camera readiness",
        retryable=True,
        details={
            "requested": requested,
            "resolved_cancel_action": resolved_cancel_action,
            "resolved_cancel_action_source": resolved_cancel_action_source,
            "callback_action": callback_action,
            "sdk_error": _result_error_message(result),
        },
    )


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
