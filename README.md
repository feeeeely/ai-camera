# Offline AI Camera

A self-contained camera that reads every photo the moment you press the shutter and
answers with a single sentence — running entirely on a Raspberry Pi 5. No internet, no
cloud, no API keys.

The housing follows the form language of a Mamiya RZ67 Pro. Everything inside is 2026.

<!-- Add a photo of the camera here:
![The camera](docs/camera.jpg)
-->

<!-- Video: https://youtu.be/YOUR-LINK -->

---

## What this is

Most "AI camera" projects send the photo to a hosted model and print the answer. This one
does the opposite: the model lives on the device, the shutter button is the only input,
and the network is never touched during operation.

The more interesting part is the second layer. Six small vision-language models are
installed side by side and can be swapped **on the device with a single tap**. Every model
receives the *same* photo, the *same* instruction and the *same* sampling parameters — so
the differences you see in the output are purely differences in perception, wording and
speed. After each shot the screen shows the measured numbers behind that run.

It is not a benchmark in the academic sense: the photos are unique, the scoring is human,
the latencies are specific to this hardware. It is an instrument for making the
differences between tiny VLMs *visible* rather than tabulated.

---

## How it works

```
shutter press
   └─> rpicam-still  →  snap.jpg (800×600, fixed 1/50s shutter)
         └─> vision.py  →  Ollama HTTP API (127.0.0.1:11434)
               └─> the selected model writes one sentence
                     └─> display + archive card + telemetry
```

**Single stage by design.** An earlier version used a two-model pipeline (a vision model
described the scene, a language model turned that into a sentence). That was dropped:
with a language model in between, every comparison measured the *middleman* as much as the
model under test. Now each model produces the visible text itself, which is the only way
the comparison is fair.

**No Ollama Python package.** `vision.py` talks to the Ollama HTTP API directly with
`urllib`. The Python client turned out to be a confirmed source of unexplained latency on
this hardware.

**Three threads, not four.** `num_thread: 3` leaves one core of the Pi 5 free for the
preview, the GUI and the shutter loop, which keeps the interface responsive while a model
is running.

---

## Telemetry

Every result is shown with the numbers from that specific run:

| Value | Meaning |
|---|---|
| wall time | what the user actually waited |
| image → N tok | how many tokens the encoder turned the image into — a direct fingerprint of the model |
| reply N tok | how verbose the model was |
| tok/s | generation speed, independent of answer length |
| vision | time spent processing image + prompt (the real bottleneck) |
| generate | time spent producing text |
| load | one-off model load into RAM (high only on the first call after a model switch) |

The image-token count is the most revealing number in practice. The same photo can become
a few hundred tokens on one encoder and well over a thousand on another, and that
difference dominates latency far more than text generation does.

---

## Models

Installed and compared (times measured on this Pi 5, models warm in RAM):

| Model | Notes |
|---|---|
| `ministral-3:3b` | default. ~5–6 s, good quality |
| `moondream:latest` | small VQA model, needs short questions. ~9 s, terse |
| `smolvlm2:2.2b` | ~10 s, detailed — fast *and* good |
| `minicpm-v4.6` | very rich descriptions, ~25 s |
| `qwen3-vl:2b-instruct` | slow (85–230 s) but coherent |
| `internvl3.5:2b` | ~90 s, the most accurate and detailed of the set |

Not included: `gemma3:4b` — it hangs for over 20 minutes on this Pi 5.

Adding a model is one line in the `MODELLE` list in `smart_cam.py`; the on-device model
list and the update checker pick it up automatically.

---

## Hardware

- Raspberry Pi 5, 16 GB
- Raspberry Pi HQ Camera with a 6 mm wide-angle CS-mount lens
- 4.3" capacitive DSI touch display (800×480)
- Stainless steel IP67/IK10 push button on GPIO 27
- Waveshare UPS HAT (E) with four Samsung INR21700-40T cells, battery level read over I2C
- 64 GB microSD

Active cooling is strongly recommended. The models push all four cores to 100 % for
extended periods, and a thermally throttled Pi turns a 10-second model into a 25-second
one.

---

## Installation

```bash
# System packages
sudo apt install -y python3-pil python3-libgpiod python3-smbus2 fonts-jetbrains-mono

# Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Models
ollama pull ministral-3:3b
ollama pull moondream
ollama pull qwen3-vl:2b-instruct
# …and whichever others you want to compare

# Scripts
cp smart_cam.py vision.py ~/
python3 ~/smart_cam.py
```

Autostart on boot — create `~/.config/autostart/smartcam.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=SmartCam
Exec=python3 /home/YOUR-USER/smart_cam.py
```

Also disable screen blanking (`sudo raspi-config` → Display Options), otherwise the
viewfinder goes black mid-scene.

For the long-press shutdown to work without a password prompt:

```bash
echo "YOUR-USER ALL=(ALL) NOPASSWD: /usr/sbin/shutdown" | sudo tee /etc/sudoers.d/010_smartcam
sudo chmod 440 /etc/sudoers.d/010_smartcam
```

---

## Using the camera

**Shutter button** — press to take a photo, press again to return to the viewfinder.
Hold for 3 seconds to shut down cleanly.

**Menu bar** (top of the screen, hidden while a photo is developing):

- **Models** — switch the active model
- **Prompt** — type your own instruction on the on-screen keyboard. This changes what the
  camera *is*: a dry observer, a museum label writer, an inventory tool.
- **Wi-Fi** — scan and connect. Needed only to download new models.
- **Updates** — compares local model digests against the Ollama registry and offers to
  pull newer versions.

Every shot is archived to `~/archiv/` as two files: the original photo and a rendered
800×480 card in the display's own design, with the model name below the sentence.

---

## Things that cost me a lot of time

Documented here because none of it is in any leaderboard.

**`qwen3-vl:2b` is a thinking-only variant.** It spends its entire token budget on
invisible reasoning and returns an empty visible answer. The symptom looks like a broken
model; the fix is the `-instruct` tag.

**Moondream returns empty responses on recent Ollama versions.** The package in the
library has not been updated in a long time. It also only tolerates very short prompts —
long multi-part instructions make it abort with an empty answer, which is why the default
prompt in `vision.py` is deliberately short.

**Ollama cannot load separate `mmproj` files.** A lot of vision GGUFs on Hugging Face ship
the vision projector as a separate file, and pulling them with the `hf.co/` prefix
produces a model that loads but cannot see. Use packaged library models.

**Small models are allowed to refuse.** A tiny model may answer a complex instruction with
an immediate stop token and no text. `vision.py` treats that as a legitimate comparison
result (`— no answer —` with `reply 1 tok` in the telemetry) rather than as a crash.

**Models fall out of RAM.** Without `keep_alive`, Ollama unloads after a few minutes and
the next shot pays a 30–60 second reload from the SD card. Everything here uses
`keep_alive: '24h'`.

**Watch the process timeout.** The GUI kills the vision process after a fixed period. When
that limit was too low, slow models produced "empty result" errors that looked like model
failures but were the camera hanging up on them.

---

## Status

Working and in use. Written by a designer, not a developer — the code was produced with
heavy AI assistance, while the hardware integration, debugging and model testing were done
by hand. Feedback on the implementation is very welcome.

## License

<!-- Pick one — MIT is the usual choice for a project like this.
     Add a LICENSE file via GitHub: Add file → Create new file → type "LICENSE"
     → GitHub offers a template picker. -->

## Links

- Video: <!-- https://youtu.be/YOUR-LINK -->
- <https://www.felixbas.la>
