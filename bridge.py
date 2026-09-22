"""tapo-bridge — put battery-powered Tapo cameras into Frigate (or anything else).

Battery and solar Tapo cameras (C400, C420, C425, C660 solar, …) expose no RTSP and no
ONVIF. They speak only TP-Link's encrypted protocol on port 8800, and the "Camera
Account" screen that enables third-party access on mains-powered models does not exist
on them.

go2rtc has a `tapo://` source that connects to that protocol, but it reports the video
as H264 when these cameras actually send **HEVC**. Parsing H.265 as H.264 means the
frame geometry is never recovered, so ffmpeg fails with:

    Could not find codec parameters for stream 0 (Video: h264, none): unspecified size

No probe window fixes that, because nothing in the stream is being read correctly.

This bridge uses pytapo, which decrypts the stream properly and hands ffmpeg an MPEG-TS
with `-c:v copy`. MPEG-TS carries codec parameters inline at every keyframe and needs no
upfront header, so any consumer learns the real geometry from the stream itself. The
result is served over plain HTTP as `video/mp2t`.

A camera session is opened only while a client is actually pulling, so nothing holds the
camera awake when your NVR is stopped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field

from aiohttp import web

try:
    from pytapo import Tapo
    from pytapo.media_stream.streamer import Streamer
except ImportError:  # pragma: no cover
    sys.exit("pytapo is required:  pip install pytapo")

log = logging.getLogger("tapo-bridge")


# --------------------------------------------------------------------------- config


@dataclass
class Camera:
    name: str
    host: str
    password: str
    user: str = "admin"
    quality: str = "HD"
    include_audio: bool = False
    # Passed straight through to pytapo's Streamer as ff_args, for anything this
    # config does not model (e.g. {"-c:v": "libx264"} to transcode at the source).
    ff_args: dict = field(default_factory=dict)

    @property
    def path(self) -> str:
        return f"/{self.name}.ts"


def _bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_cameras() -> list[Camera]:
    """Cameras from TAPO_CAMERAS (JSON), else the single-camera TAPO_* variables.

    JSON form, for more than one camera:

        TAPO_CAMERAS='[{"name":"front","host":"10.0.0.5","password":"…"},
                       {"name":"yard","host":"10.0.0.6","password":"…","quality":"SD"}]'
    """
    raw = os.environ.get("TAPO_CAMERAS", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"TAPO_CAMERAS is not valid JSON: {exc}")
        if not isinstance(entries, list) or not entries:
            sys.exit("TAPO_CAMERAS must be a non-empty JSON list")
        cameras = []
        for i, e in enumerate(entries):
            missing = [k for k in ("name", "host", "password") if not e.get(k)]
            if missing:
                sys.exit(f"TAPO_CAMERAS[{i}] is missing: {', '.join(missing)}")
            cameras.append(Camera(
                name=str(e["name"]),
                host=str(e["host"]),
                password=str(e["password"]),
                user=str(e.get("user", "admin")),
                quality=str(e.get("quality", "HD")),
                include_audio=bool(e.get("include_audio", False)),
                ff_args=dict(e.get("ff_args") or {}),
            ))
        return cameras

    host = os.environ.get("TAPO_HOST", "").strip()
    password = os.environ.get("TAPO_PASSWORD", "")
    if not host or not password:
        sys.exit("Set TAPO_HOST and TAPO_PASSWORD, or TAPO_CAMERAS as JSON. "
                 "See .env.example.")
    ff_args_raw = os.environ.get("TAPO_FF_ARGS", "").strip()
    try:
        ff_args = json.loads(ff_args_raw) if ff_args_raw else {}
    except json.JSONDecodeError as exc:
        sys.exit(f"TAPO_FF_ARGS is not valid JSON: {exc}")

    return [Camera(
        name=os.environ.get("TAPO_NAME", "stream"),
        host=host,
        password=password,
        user=os.environ.get("TAPO_USER", "admin"),
        quality=os.environ.get("TAPO_QUALITY", "HD"),
        include_audio=_bool(os.environ.get("TAPO_INCLUDE_AUDIO"), False),
        ff_args=ff_args,
    )]


# --------------------------------------------------------------------------- serving


CHUNK = 64 * 1024


async def serve_camera(cam: Camera, request: web.Request) -> web.StreamResponse:
    peer = request.remote
    log.info("[%s] client %s connected; opening camera session", cam.name, peer)

    loop = asyncio.get_running_loop()
    try:
        # Tapo() does blocking HTTP in its constructor.
        tapo = await loop.run_in_executor(
            None, lambda: Tapo(cam.host, cam.user, cam.password, cam.password))
    except Exception as exc:
        # The overwhelmingly common cause, and it looks exactly like a bad password.
        log.error("[%s] login failed: %s", cam.name, exc)
        return web.Response(
            status=502,
            text=(f"Could not log in to {cam.host}: {exc}\n\n"
                  "If this is an authentication error, check that Third-Party\n"
                  "Compatibility is ON in the Tapo app:\n"
                  "  Me > Tapo Lab / Third-Party Services > Third-Party Compatibility\n"
                  "With it off, correct credentials still return 401.\n"))

    streamer = Streamer(
        tapo,
        mode="pipe",
        includeAudio=cam.include_audio,
        quality=cam.quality,
        ff_args=cam.ff_args or None,
    )

    try:
        await streamer.start()
    except Exception as exc:
        log.error("[%s] could not start stream: %s", cam.name, exc)
        return web.Response(status=502, text=f"Could not start stream: {exc}\n")

    proc = getattr(streamer, "streamProcess", None)
    if proc is None or proc.stdout is None:
        log.error("[%s] streamer produced no process", cam.name)
        return web.Response(status=503, text="Camera stream unavailable\n")

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"},
    )
    await resp.prepare(request)

    sent = 0
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                log.info("[%s] camera stream ended after %d bytes", cam.name, sent)
                break
            sent += len(chunk)
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        log.info("[%s] client %s disconnected after %d bytes", cam.name, peer, sent)
    except Exception as exc:
        log.warning("[%s] stream aborted after %d bytes: %s", cam.name, sent, exc)
    finally:
        # Let the camera go back to sleep instead of holding the session open.
        try:
            await streamer.stop()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return resp


def build_app(cameras: list[Camera]) -> web.Application:
    app = web.Application()

    async def healthz(_request):
        return web.Response(text="ok\n")

    async def index(_request):
        lines = ["tapo-bridge", ""]
        lines += [f"  {c.name:<16} {c.path:<20} {c.host}  quality={c.quality}"
                  for c in cameras]
        lines += ["", "Point your NVR at one of the paths above."]
        return web.Response(text="\n".join(lines) + "\n")

    app.add_routes([web.get("/", index), web.get("/healthz", healthz)])
    for cam in cameras:
        app.add_routes([web.get(cam.path, lambda r, c=cam: serve_camera(c, r))])
        # Convenience alias so single-camera setups can hardcode /stream.ts. Skipped
        # when the camera is already named "stream", or aiohttp rejects the duplicate.
        if len(cameras) == 1 and cam.path != "/stream.ts":
            app.add_routes([web.get("/stream.ts", lambda r, c=cam: serve_camera(c, r))])
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cameras = load_cameras()
    port = int(os.environ.get("BRIDGE_PORT", "8080"))
    host = os.environ.get("BRIDGE_BIND", "0.0.0.0")

    for c in cameras:
        log.info("serving %s (%s, quality=%s, audio=%s) at %s",
                 c.name, c.host, c.quality, c.include_audio, c.path)

    web.run_app(build_app(cameras), host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
