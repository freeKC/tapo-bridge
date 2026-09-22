# tapo-bridge

Put **battery-powered Tapo cameras** into Frigate, Home Assistant, or anything else that
can read an HTTP stream.

Battery and solar Tapo cameras — C400, C420, C425, **C660 solar**, and friends — expose
no RTSP and no ONVIF. This bridge serves their video as MPEG-TS over plain HTTP:

```yaml
# Frigate
cameras:
  backyard:
    ffmpeg:
      inputs:
        - path: http://tapo-bridge:8080/stream.ts
          roles: [detect, record]
```

---

## Why this is needed

TP-Link splits its camera line in a way that is not obvious when you buy one:

| | mains-powered (C320WS, C325WB, C520WS…) | battery / solar (C400, C420, C425, C660 solar) |
|---|---|---|
| RTSP (port 554) | yes | **no** |
| ONVIF (port 2020) | yes | **no** |
| "Camera Account" screen in the app | yes | **absent** |
| Works with Frigate out of the box | yes | **no** |

Battery models speak only TP-Link's own encrypted protocol on **port 8800**.

[go2rtc](https://github.com/AlexxIT/go2rtc) has a `tapo://` source for that protocol, and
it *connects* — it authenticates, and megabytes of video arrive. But it reports the media
as **H264**, and at least some of these cameras actually send **HEVC (H.265)**. Parsing
H.265 as H.264 means the frame geometry is never recovered, so every consumer downstream
fails:

```
Could not find codec parameters for stream 0 (Video: h264, none): unspecified size
Consider increasing the value for the 'analyzeduration' and 'probesize' options
```

That error sends you chasing probe windows, and none of it helps — the bytes are fine,
the codec assumption is not. On a Tapo C660 solar the real stream is:

```
codec_name=hevc   width=3840   height=2160   avg_frame_rate=15/1
```

Things that do **not** fix it: a 10-second `analyzeduration`/`probesize`; re-muxing
through go2rtc's `ffmpeg:` module; go2rtc's own `/api/stream.ts` endpoint (it returns
zero bytes, because go2rtc hasn't resolved the geometry either).

**What does work:** [pytapo](https://github.com/JurajNyiri/pytapo) decrypts the protocol
correctly and feeds ffmpeg `-f mpegts -i pipe:0` with `-c:v copy`. MPEG-TS carries codec
parameters inline at every keyframe and needs no upfront header, so consumers learn the
real geometry from the stream itself. This project wraps that in a small HTTP server.

---

## Before anything else: turn on Third-Party Compatibility

**In the Tapo app → Third-Party Services → Third-Party Compatibility → On.** It is an
*account* setting, not per-camera, and it is off by default.

> With it off, every local connection returns **`401 Unauthorized`**, and pytapo reports
> **`Invalid authentication data`** — for entirely correct credentials.

This is the single biggest time-waster with these cameras. A 401 from a Tapo camera is
far more likely to be this toggle than a wrong password. Check it *first*.

---

## Quick start

```bash
git clone https://github.com/d3xc0/tapo-bridge.git
cd tapo-bridge
cp .env.example .env
$EDITOR .env          # TAPO_HOST and TAPO_PASSWORD at minimum
docker compose up -d --build
```

Check it:

```bash
curl -s localhost:8189/ | head            # lists configured cameras
curl -s --max-time 10 -o probe.ts localhost:8189/stream.ts
ffprobe -v error -show_entries stream=codec_name,width,height -of default=nw=1 probe.ts
```

A real resolution in that output means it is working. If you get `unspecified size`, the
stream is not being decrypted — go back to the toggle above.

---

## Configuration

Everything is environment variables. See [`.env.example`](.env.example).

| variable | default | meaning |
|---|---|---|
| `TAPO_HOST` | — | camera IP (required, unless `TAPO_CAMERAS`) |
| `TAPO_PASSWORD` | — | your **TP-Link account** password (required) |
| `TAPO_USER` | `admin` | rarely needs changing |
| `TAPO_NAME` | `stream` | served at `/<name>.ts` |
| `TAPO_QUALITY` | `HD` | `HD` or `SD`; `SD` is much easier on the battery |
| `TAPO_INCLUDE_AUDIO` | `0` | include the audio track |
| `TAPO_FF_ARGS` | — | JSON passed to pytapo's `Streamer` as `ff_args` |
| `TAPO_CAMERAS` | — | JSON list; use instead of the single-camera vars |
| `BRIDGE_PORT` | `8080` | listen port inside the container |
| `BRIDGE_BIND` | `0.0.0.0` | listen address |
| `BRIDGE_LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |

`TAPO_PASSWORD` is the password you sign into the **Tapo app** with. It is not a "camera
account" (battery models have no such screen) and not your Wi-Fi password.

### Several cameras

```bash
TAPO_CAMERAS=[{"name":"front","host":"192.168.1.50","password":"…"},
              {"name":"yard","host":"192.168.1.51","password":"…","quality":"SD"}]
```

Each is served at `/<name>.ts`.

### Transcoding at the bridge

Some clients dislike H.265. `ff_args` goes straight to pytapo's `Streamer`:

```bash
TAPO_FF_ARGS={"-c:v":"libx264"}
```

That costs real CPU — prefer leaving it as `copy` and letting your NVR handle H.265.

---

## Battery life

The obvious worry is that an NVR holds the stream open continuously, keeping a camera
that is designed to sleep permanently awake.

Measured on a C660 solar, continuous 4K pull into Frigate on a spring afternoon:

```
61% → 72% over ~35 minutes,  charging=NORMAL
```

The panel outpaced a continuous 4K stream **in good light**. Do not assume that holds in
winter, deep shade, or a north-facing wall. Check before you rely on it:

```python
from pytapo import Tapo
t = Tapo("192.168.1.50", "admin", PASSWORD, PASSWORD)
print(t.getBatteryStatus()["battery"]["status"])
```

`TAPO_QUALITY=SD` cuts this substantially if you only need detection.

The bridge opens a camera session **only while a client is pulling**, so stopping your
NVR lets the camera sleep normally.

---

## Notes and limitations

- Battery Tapos need TP-Link's cloud to function, so they cannot be firewalled off the
  internet the way a local-only RTSP camera can.
- No ONVIF, so no PTZ or motion events through the usual integrations. This bridge
  carries video only.
- These cameras support a limited number of simultaneous streams. One consumer is safest.
- Tested against a **C660 solar, firmware 1.1.8**. Other battery models use the same
  protocol and should work; reports welcome.

## Credits

Built on [pytapo](https://github.com/JurajNyiri/pytapo) by Juraj Nyíri, which does the
genuinely hard part — the protocol and its encryption.

## License

MIT — see [LICENSE](LICENSE).
