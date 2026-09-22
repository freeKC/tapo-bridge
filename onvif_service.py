"""A minimal ONVIF server that fronts a Tapo camera's motor.

Frigate's PTZ controls speak ONVIF and nothing else. Battery Tapo cameras have no
ONVIF at all, so this implements just enough of it — Device, Media and PTZ — to make
those controls work, translating the calls into pytapo motor commands.

This is deliberately NOT a general ONVIF implementation. It answers the specific
operations Frigate's client performs, with responses shaped to match the ONVIF
schemas closely enough for zeep to parse them.

Not supported: autotracking (needs absolute positioning and real status feedback,
which the Tapo API does not expose), imaging, events, analytics.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re

from aiohttp import web

log = logging.getLogger("tapo-bridge.onvif")

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}

PROFILE_TOKEN = "tapo_profile"
PTZ_CONFIG_TOKEN = "tapo_ptz_config"
SOURCE_TOKEN = "tapo_source"

# Tapo has no continuous motion - only stepped moves. ContinuousMove is emulated by
# repeating small steps until Stop arrives. Degrees per tick and tick interval.
STEP_DEGREES = 8
STEP_INTERVAL = 0.45


def envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" '
        f'xmlns:trt="{NS["trt"]}" xmlns:tptz="{NS["tptz"]}" xmlns:tt="{NS["tt"]}">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


def fault(reason: str) -> str:
    return envelope(
        "<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>"
        f"<s:Reason><s:Text xml:lang=\"en\">{reason}</s:Text></s:Reason></s:Fault>"
    )


def operation_of(xml: str) -> str:
    """Name of the SOAP operation, ignoring namespace prefixes.

    Prefixes must be matched loosely. zeep - which is what Frigate uses - emits
    `soap-env:Body`, and a hyphen is perfectly legal in an XML prefix. An earlier
    pattern of `s(oap)?(env)?:` missed it, so every request parsed as empty and got
    an empty response, which surfaced in Frigate as "NoneType object is not iterable".
    """
    m = re.search(r"<[\w.-]*:?Body[^>]*>\s*<([\w.-]+:)?([A-Za-z][\w]*)", xml)
    return m.group(2) if m else ""


def _f(xml: str, tag: str, default: float = 0.0) -> float:
    """Pull an attribute value like x="0.5" from the first matching tag."""
    m = re.search(rf'<(?:[A-Za-z0-9_]+:)?{tag}\b[^>]*\bx="(-?[\d.]+)"[^>]*\by="(-?[\d.]+)"', xml)
    if m:
        return float(m.group(1)), float(m.group(2))
    return (default, default)


class PtzController:
    """Drives the camera motor, emulating ONVIF continuous movement."""

    def __init__(self, make_client):
        self._make_client = make_client
        self._task: asyncio.Task | None = None
        self._vector: tuple[int, int] | None = None
        self._client = None
        self._lock = asyncio.Lock()

    async def _get_client(self):
        """Cached login.

        Building a Tapo client performs a blocking HTTP login that takes longer than
        the ~200ms between the repeated ContinuousMove calls Frigate sends while a
        button is held. Creating one per step meant every move was cancelled mid-login
        and the motor never actually turned.
        """
        if self._client is None:
            loop = asyncio.get_running_loop()
            self._client = await loop.run_in_executor(None, self._make_client)
        return self._client

    async def _move(self, x: int, y: int) -> None:
        loop = asyncio.get_running_loop()
        client = await self._get_client()
        try:
            await loop.run_in_executor(None, lambda: client.moveMotor(x, y))
        except Exception:
            # Session may have expired - drop it so the next call logs in again.
            self._client = None
            raise

    async def _repeat(self, dx: int, dy: int) -> None:
        """Step until cancelled. ONVIF clients expect motion to persist until Stop."""
        try:
            while True:
                try:
                    await self._move(dx, dy)
                except Exception as exc:
                    log.warning("motor step failed: %s", exc)
                    return
                await asyncio.sleep(STEP_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def continuous(self, vx: float, vy: float) -> None:
        # ONVIF velocities are -1.0..1.0; sign is direction, magnitude is speed.
        # Tapo takes degrees, so scale the step by the requested velocity.
        dx = int(round(STEP_DEGREES * max(-1.0, min(1.0, vx))))
        dy = int(round(STEP_DEGREES * max(-1.0, min(1.0, vy))))
        if dx == 0 and dy == 0:
            await self.stop()
            return
        # A held button repeats the same ContinuousMove. Restarting the task each time
        # would cancel the move already in flight, so let an identical vector run on.
        async with self._lock:
            if self._task and not self._task.done() and self._vector == (dx, dy):
                return
        await self.stop()
        async with self._lock:
            self._vector = (dx, dy)
            self._task = asyncio.create_task(self._repeat(dx, dy))

    async def relative(self, vx: float, vy: float) -> None:
        # Frigate sends relative moves for its arrow buttons on some versions.
        dx = int(round(45 * max(-1.0, min(1.0, vx))))
        dy = int(round(45 * max(-1.0, min(1.0, vy))))
        if dx or dy:
            await self._move(dx, dy)

    async def stop(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None
            self._vector = None

    async def presets(self) -> dict:
        loop = asyncio.get_running_loop()
        client = await loop.run_in_executor(None, self._make_client)
        try:
            return await loop.run_in_executor(None, client.getPresets) or {}
        except Exception as exc:
            log.warning("getPresets failed: %s", exc)
            return {}

    async def goto_preset(self, token: str) -> None:
        loop = asyncio.get_running_loop()
        client = await loop.run_in_executor(None, self._make_client)
        await loop.run_in_executor(None, lambda: client.setPreset(token))


class OnvifService:
    def __init__(self, ptz: PtzController, advertise_host: str, port: int,
                 stream_uri: str, name: str = "tapo-bridge"):
        self.ptz = ptz
        self.base = f"http://{advertise_host}:{port}"
        self.stream_uri = stream_uri
        self.name = name

    # -- device -----------------------------------------------------------------

    def _system_date_and_time(self) -> str:
        n = datetime.datetime.now(datetime.timezone.utc)
        return (
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC</tt:TZ></tt:TimeZone>"
            "<tt:UTCDateTime>"
            f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute>"
            f"<tt:Second>{n.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month>"
            f"<tt:Day>{n.day}</tt:Day></tt:Date>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def _capabilities(self) -> str:
        return (
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>{self.base}/onvif/device_service</tt:XAddr>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>"
            "<tt:DiscoveryBye>false</tt:DiscoveryBye>"
            "<tt:RemoteDiscovery>false</tt:RemoteDiscovery>"
            "<tt:SystemBackup>false</tt:SystemBackup>"
            "<tt:SystemLogging>false</tt:SystemLogging>"
            "<tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
            "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor>"
            "</tt:SupportedVersions></tt:System></tt:Device>"
            f"<tt:Media><tt:XAddr>{self.base}/onvif/media_service</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities></tt:Media>"
            f"<tt:PTZ><tt:XAddr>{self.base}/onvif/ptz_service</tt:XAddr></tt:PTZ>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def _services(self) -> str:
        def svc(ns, path, minor):
            return (f"<tds:Service><tds:Namespace>{ns}</tds:Namespace>"
                    f"<tds:XAddr>{self.base}{path}</tds:XAddr>"
                    f"<tds:Version><tt:Major>2</tt:Major><tt:Minor>{minor}</tt:Minor>"
                    "</tds:Version></tds:Service>")
        return ("<tds:GetServicesResponse>"
                + svc(NS["tds"], "/onvif/device_service", 0)
                + svc(NS["trt"], "/onvif/media_service", 0)
                + svc(NS["tptz"], "/onvif/ptz_service", 0)
                + "</tds:GetServicesResponse>")

    def _device_information(self) -> str:
        return ("<tds:GetDeviceInformationResponse>"
                "<tds:Manufacturer>tapo-bridge</tds:Manufacturer>"
                f"<tds:Model>{self.name}</tds:Model>"
                "<tds:FirmwareVersion>1.0</tds:FirmwareVersion>"
                "<tds:SerialNumber>tapo-bridge-1</tds:SerialNumber>"
                "<tds:HardwareId>tapo-bridge</tds:HardwareId>"
                "</tds:GetDeviceInformationResponse>")

    # -- media ------------------------------------------------------------------

    def _ptz_configuration(self, tag: str = "tt:PTZConfiguration") -> str:
        return (
            f'<{tag} token="{PTZ_CONFIG_TOKEN}">'
            "<tt:Name>tapo</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:NodeToken>{PTZ_CONFIG_TOKEN}</tt:NodeToken>"
            "<tt:DefaultContinuousPanTiltVelocitySpace>"
            "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
            "</tt:DefaultContinuousPanTiltVelocitySpace>"
            "<tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout>"
            f"</{tag}>"
        )

    def _profile_body(self, tag: str) -> str:
        return (
            f'<{tag} token="{PROFILE_TOKEN}" fixed="true">'
            "<tt:Name>tapo</tt:Name>"
            f'<tt:VideoSourceConfiguration token="{SOURCE_TOKEN}">'
            "<tt:Name>source</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:SourceToken>{SOURCE_TOKEN}</tt:SourceToken>"
            '<tt:Bounds x="0" y="0" width="3840" height="2160"/>'
            "</tt:VideoSourceConfiguration>"
            '<tt:VideoEncoderConfiguration token="enc">'
            "<tt:Name>enc</tt:Name><tt:UseCount>1</tt:UseCount>"
            "<tt:Encoding>H264</tt:Encoding>"
            "<tt:Resolution><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height>"
            "</tt:Resolution><tt:Quality>5</tt:Quality>"
            "<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            "<tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>"
            "<tt:SessionTimeout>PT10S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration>"
            + self._ptz_configuration()
            + f"</{tag}>"
        )

    def _video_sources(self) -> str:
        """Frigate reads video_source_token from here before it will init PTZ."""
        return (
            "<trt:GetVideoSourcesResponse>"
            f'<trt:VideoSources token="{SOURCE_TOKEN}">'
            "<tt:Framerate>15</tt:Framerate>"
            "<tt:Resolution><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height>"
            "</tt:Resolution>"
            "</trt:VideoSources></trt:GetVideoSourcesResponse>"
        )

    def _profiles(self) -> str:
        return ("<trt:GetProfilesResponse>"
                + self._profile_body("trt:Profiles")
                + "</trt:GetProfilesResponse>")

    def _stream_uri(self) -> str:
        return ("<trt:GetStreamUriResponse><trt:MediaUri>"
                f"<tt:Uri>{self.stream_uri}</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                "<tt:Timeout>PT10S</tt:Timeout>"
                "</trt:MediaUri></trt:GetStreamUriResponse>")

    # -- ptz --------------------------------------------------------------------

    def _ptz_node(self) -> str:
        return (
            f'<tt:PTZNode token="{PTZ_CONFIG_TOKEN}" FixedHomePosition="false" '
            'GeoMove="false"><tt:Name>tapo</tt:Name>'
            "<tt:SupportedPTZSpaces>"
            "<tt:ContinuousPanTiltVelocitySpace>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:URI>"
            "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
            "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
            "</tt:ContinuousPanTiltVelocitySpace>"
            "<tt:RelativePanTiltTranslationSpace>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace</tt:URI>"
            "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
            "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
            "</tt:RelativePanTiltTranslationSpace>"
            "</tt:SupportedPTZSpaces>"
            "<tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets>"
            "<tt:HomeSupported>false</tt:HomeSupported>"
            "</tt:PTZNode>"
        )

    async def _presets_response(self) -> str:
        presets = await self.ptz.presets()
        items = "".join(
            f'<tptz:Preset token="{tok}"><tt:Name>{name}</tt:Name></tptz:Preset>'
            for tok, name in (presets or {}).items()
        )
        return f"<tptz:GetPresetsResponse>{items}</tptz:GetPresetsResponse>"

    async def handle(self, xml: str) -> str:
        op = operation_of(xml)

        if op == "GetSystemDateAndTime":
            return self._system_date_and_time()
        if op == "GetCapabilities":
            return self._capabilities()
        if op in ("GetServices", "GetServiceCapabilities"):
            return self._services() if op == "GetServices" else (
                "<tds:GetServiceCapabilitiesResponse/>")
        if op == "GetDeviceInformation":
            return self._device_information()
        if op == "GetVideoSources":
            return self._video_sources()
        if op == "GetProfiles":
            return self._profiles()
        if op == "GetProfile":
            return ("<trt:GetProfileResponse>"
                    + self._profile_body("trt:Profile") + "</trt:GetProfileResponse>")
        if op == "GetStreamUri":
            return self._stream_uri()

        if op == "GetNodes":
            return f"<tptz:GetNodesResponse>{self._ptz_node()}</tptz:GetNodesResponse>"
        if op == "GetNode":
            return f"<tptz:GetNodeResponse>{self._ptz_node()}</tptz:GetNodeResponse>"
        if op == "GetConfigurations":
            return ("<tptz:GetConfigurationsResponse>"
                    + self._ptz_configuration("tptz:PTZConfiguration")
                    + "</tptz:GetConfigurationsResponse>")
        if op == "GetConfigurationOptions":
            return (
                "<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions>"
                "<tt:Spaces><tt:ContinuousPanTiltVelocitySpace>"
                "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:URI>"
                "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
                "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
                "</tt:ContinuousPanTiltVelocitySpace></tt:Spaces>"
                "<tt:PTZTimeout><tt:Min>PT1S</tt:Min><tt:Max>PT10S</tt:Max></tt:PTZTimeout>"
                "</tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>")
        if op == "GetStatus":
            # No absolute position from the Tapo API. Reporting a fixed origin keeps
            # clients happy but is why autotracking cannot work.
            return ("<tptz:GetStatusResponse><tptz:PTZStatus>"
                    '<tt:Position><tt:PanTilt x="0.0" y="0.0"/><tt:Zoom x="0.0"/></tt:Position>'
                    "<tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>IDLE</tt:Zoom>"
                    "</tt:MoveStatus>"
                    f"<tt:UtcTime>{datetime.datetime.now(datetime.timezone.utc).isoformat()}</tt:UtcTime>"
                    "</tptz:PTZStatus></tptz:GetStatusResponse>")

        if op == "ContinuousMove":
            vx, vy = _f(xml, "PanTilt")
            log.info("ContinuousMove x=%.2f y=%.2f", vx, vy)
            await self.ptz.continuous(vx, vy)
            return "<tptz:ContinuousMoveResponse/>"
        if op == "RelativeMove":
            vx, vy = _f(xml, "PanTilt")
            log.info("RelativeMove x=%.2f y=%.2f", vx, vy)
            await self.ptz.relative(vx, vy)
            return "<tptz:RelativeMoveResponse/>"
        if op == "Stop":
            await self.ptz.stop()
            return "<tptz:StopResponse/>"
        if op == "GetPresets":
            return await self._presets_response()
        if op == "GotoPreset":
            m = re.search(r"<(?:[A-Za-z0-9_]+:)?PresetToken>([^<]+)<", xml)
            if m:
                await self.ptz.goto_preset(m.group(1))
            return "<tptz:GotoPresetResponse/>"

        if not op:
            log.warning("could not parse SOAP operation; body starts: %s",
                        xml[:300].replace("\n", " "))
        else:
            log.info("unhandled ONVIF operation: %s", op)
        return f"<tds:{op or 'Unknown'}Response/>"


def attach(app: web.Application, service: OnvifService) -> None:
    async def soap(request: web.Request) -> web.Response:
        body = await request.text()
        try:
            resp = await service.handle(body)
        except Exception as exc:
            log.exception("ONVIF handler failed")
            return web.Response(text=fault(str(exc)), status=500,
                                content_type="application/soap+xml")
        return web.Response(text=envelope(resp), content_type="application/soap+xml")

    for path in ("/onvif/device_service", "/onvif/media_service",
                 "/onvif/ptz_service"):
        app.router.add_post(path, soap)
