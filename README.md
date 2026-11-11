# Frame Proxy Inspector

Frame Proxy Inspector is a small Python backend for turning uncompressed AVI
footage into three review artifacts:

- a deterministic shot index with contiguous scene ranges;
- a bounded PPM contact sheet with one tile per indexed scene;
- a lossless PCM (WAV) audio segment for each indexed scene, sliced from the
  inspected clip itself.

The service verifies the RIFF/AVI container, frame geometry, and frame chunk
count before it admits a clip. When the clip carries an uncompressed PCM audio
stream, intake also proves the track is complete: declared and stored sample
counts must agree, every audio chunk must hold whole samples, and the track
must cover the full video duration. Shot indexing then computes luma and color
fingerprints from sampled frames, applies a minimum-scene guard to hard cuts,
and maps every scene to an exact PCM sample range. Contact sheet requests are
checked against a fixed pixel budget before frame decoding starts.

Scene sample boundaries are derived from absolute frame numbers with exact
rational arithmetic, so adjacent scenes stitch back together over the whole
inspected video time range without overlap, gaps, or accumulated drift — even
at non-integer frame rates such as 30000/1001, and even when AVI audio chunks
span scene boundaries.

Automated unit, integration, and browser tests are intentionally deferred to a
later task stage. This baseline ships executable workflow checks instead.

## Requirements

- Python 3.11 or newer
- No third-party packages
- No external media service

## Start the service

```bash
export PYTHONPATH=src
python3 -m frame_proxy serve --host 127.0.0.1 --port 8765
```

The service prints its bound URL. Press `Ctrl+C` to stop it.

## Generate demonstration footage

```bash
PYTHONPATH=src python3 -m frame_proxy demo-avi \
  --output demo/scene-walk.avi \
  --width 128 \
  --height 72 \
  --fps 12 \
  --with-audio
```

The generated file is an uncompressed 24-bit AVI with four color scenes and
17 total frames. `--with-audio` adds a 48 kHz mono 16-bit PCM track that
exactly covers the video duration; `--fps-denominator 1001` allows NTSC-style
rates such as `--fps 30000 --fps-denominator 1001`. It is suitable for
exercising all four workflows.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | service check |
| `PUT` | `/v1/clips/{clip_id}` | admit an `application/x-avi` payload |
| `GET` | `/v1/clips/{clip_id}` | read clip geometry, audio track, and digest |
| `POST` | `/v1/clips/{clip_id}/inspections` | index shot boundaries |
| `GET` | `/v1/inspections/{inspection_id}` | read an immutable inspection |
| `GET` | `/v1/inspections/{inspection_id}/scenes/{scene_index}/audio` | fetch the scene's lossless WAV segment |
| `POST` | `/v1/inspections/{inspection_id}/contact-sheets` | compose a PPM grid |
| `GET` | `/v1/contact-sheets/{sheet_id}` | read the tile map and digest |
| `GET` | `/v1/contact-sheets/{sheet_id}/artifact` | fetch the PPM bytes |

Identifiers use lowercase letters, digits, `_`, and `-`, begin with a letter,
and contain 3 to 48 characters.

Example inspection request:

```json
{
  "inspection_id": "inspection-demo-a",
  "sensitivity": 0.55,
  "sample_stride": 1,
  "min_scene_frames": 2
}
```

Example contact sheet request:

```json
{
  "sheet_id": "sheet-demo-a",
  "columns": 3,
  "tile_width": 96,
  "gutter": 4,
  "selection": "stored",
  "background": "#20252c"
}
```

`selection` may be `stored`, `midpoint`, or `first`. The rendered PPM is
binary P6 with a maximum value of 255.

## Scene audio

An indexed inspection of a clip with a PCM audio track records an `audio`
object (PCM parameters, the SHA-256 of the source PCM stream, and the digest
of the inspected clip) and a `sample_start`/`sample_end` pair on every scene.
`GET /v1/inspections/{inspection_id}/scenes/{scene_index}/audio` re-verifies
the stored clip bytes against those recorded digests and returns the scene's
samples as a RIFF/WAVE file (`audio/x-wav`) with the PCM bytes copied
verbatim — no resampling or recompression. The response carries the WAV
digest in `ETag` and the inspected clip digest in
`X-Frame-Proxy-Clip-Digest`.

Concatenating the WAV payloads of adjacent scenes reproduces exactly the
source samples spanning the inspected video time range. Clips without an
audio track answer this route with `404 audio_not_found`, and a clip whose
audio track is incomplete is rejected at intake with `422` and never becomes
visible.

## Data layout

`FRAME_PROXY_DATA_DIR` selects the data root. Its default is
`.frame-proxy-data` in the current working directory.

```text
.frame-proxy-data/
├── clips/<clip_id>/
│   ├── clip.avi
│   └── record.json
├── inspections/<inspection_id>.json
├── contact-sheets/<sheet_id>/
│   ├── record.json
│   └── sheet.ppm
└── locks/
```

Records are written through temporary files and renamed into place. Contact
sheet bytes and their ready record are verified before the record becomes
visible.

## Source layout

```text
src/frame_proxy/
├── domain/      records, request rules, and state transitions
├── http/        request routing and response mapping
├── media/       AVI parsing, PCM audio access, frame decoding, fingerprints, and PPM grids
├── service/     workflow orchestration and failure handling
└── storage/     namespaced files, locks, and atomic writes

tools/
├── harness.py
├── check_clip_intake.py
├── check_shot_index.py
├── check_contact_sheet.py
└── check_scene_audio.py
```

## Build and workflow checks

Build:

```bash
python3 -m compileall -q src tools
```

Run one workflow check at a time:

```bash
PYTHONPATH=src:. python3 tools/check_clip_intake.py
PYTHONPATH=src:. python3 tools/check_shot_index.py
PYTHONPATH=src:. python3 tools/check_contact_sheet.py
PYTHONPATH=src:. python3 tools/check_scene_audio.py
```

Each check starts a real loopback HTTP server, generates media through the
production AVI writer, completes the named workflow, and verifies persisted
state or artifact bytes. Temporary records are removed when the check exits.

## Environment variables

| Name | Default | Purpose |
| --- | --- | --- |
| `FRAME_PROXY_DATA_DIR` | `.frame-proxy-data` | durable data root |

Clip, upload, JSON, frame dimension, frame count, audio track, and contact
sheet limits are declared in `src/frame_proxy/config.py`.

## Input boundary

The current adapter accepts one uncompressed 24-bit BGR video stream in a
RIFF/AVI container, optionally followed by one uncompressed PCM audio stream
(8/16/24/32-bit, any positive sample rate within the configured limit). The
audio stream header must use the PCM block align as its scale and the PCM byte
rate as its rate, and its declared length must match the stored sample count
and cover the whole video duration. Compressed video, compressed or
additional audio streams, and additional container formats remain outside
this baseline.

