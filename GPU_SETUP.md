# Running Ollama on GPU (NVIDIA / Linux)

Boot and verification commands for the self-hosted Ollama box
(`192.168.68.58`, the `OLLAMA_LAN_URL` in `.env`).

Generation speed is now the dominant cost in a report, and it is bound almost
entirely by the inference hardware — so this is the single biggest improvement
available to this app. Everything below is about getting the model onto the GPU
and *proving* it got there, because Ollama falls back to CPU silently: it does
not error, it just runs ~15x slower.

---

## 0. The baseline you're replacing

Measured on this host, CPU-only, `hermes3:8b` (Q4_0):

| | CPU (today) |
|---|---|
| `size_vram` in `/api/ps` | `0` |
| generation | **~4 tok/s** |
| prompt eval, warm | ~40s |
| prompt eval, cold | ~108s |
| one steady-state report | **~143s** |

Keep these numbers. Step 5 re-measures them so you can confirm the GPU is
actually being used rather than assume it.

---

## 1. Check the GPU is visible to the OS

```bash
nvidia-smi
```

You need a driver version and a listed GPU. If this command is missing or
errors, **stop here** — Ollama cannot use a GPU the OS cannot see, and every
later step will silently no-op back to CPU.

```bash
# driver version alone
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
```

**You do not need to install the CUDA toolkit.** Ollama ships its own CUDA
runtime; it only needs the NVIDIA *driver*. Installing `cuda-toolkit` is a
common and unnecessary detour. Driver 525+ is enough for the CUDA 12 build.

---

## 2. Install (or reinstall) Ollama

If Ollama was installed before the GPU/driver existed, re-run the installer so
it picks up the CUDA libraries:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

This is safe to re-run — it keeps your downloaded models and replaces the
binary and the systemd unit.

---

## 3. Boot commands (systemd)

The installer registers Ollama as a systemd service that starts at boot.

```bash
sudo systemctl enable ollama      # start automatically on boot
sudo systemctl start ollama       # start now
sudo systemctl restart ollama     # apply config changes (see step 4)
sudo systemctl stop ollama
sudo systemctl status ollama      # is it running?
```

Watch it come up — this is where GPU detection is reported:

```bash
journalctl -u ollama -f
```

The line that matters looks like:

```
msg="inference compute" id=GPU-xxxx library=cuda compute=8.6 name="NVIDIA GeForce RTX ..." total="12.0 GiB"
```

`library=cuda` means the GPU was found. `library=cpu` means it was not — go
back to step 1.

```bash
# just the GPU-detection lines from the last boot
journalctl -u ollama -b --no-pager | grep -iE "inference compute|cuda|gpu|vram"
```

---

## 4. Service configuration

Env vars must go in a systemd override — editing `/etc/systemd/system/ollama.service`
directly gets overwritten on the next upgrade.

```bash
sudo systemctl edit ollama
```

Add:

```ini
[Service]
# Listen on all interfaces so this app can reach it over the LAN/WireGuard.
# Without this Ollama binds 127.0.0.1 only and is unreachable from the Flask box.
Environment="OLLAMA_HOST=0.0.0.0:11434"

# Hold the model and its KV cache in VRAM between batches. The app already sends
# keep_alive per request; this is the server-side floor for anything that doesn't.
Environment="OLLAMA_KEEP_ALIVE=30m"

# Don't let a second model (llama3:8b) load alongside hermes3 and evict it.
Environment="OLLAMA_MAX_LOADED_MODELS=1"

# Faster attention and a smaller KV cache. Worth enabling on any recent NVIDIA card.
Environment="OLLAMA_FLASH_ATTENTION=1"

# How many requests the model serves at once. See step 7 before raising this.
Environment="OLLAMA_NUM_PARALLEL=1"
```

Apply:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
systemctl show ollama --property=Environment    # confirm what actually took effect
```

---

## 5. Verify it is *actually* on the GPU

Three independent checks. Do all three — the first is the authoritative one.

**a. `size_vram` must be non-zero.** This is the real answer.

```bash
# load the model, then ask where it lives
curl -s http://192.168.68.58:11434/api/generate \
  -d '{"model":"hermes3:8b","prompt":"hi","stream":false,"keep_alive":"30m"}' > /dev/null

curl -s http://192.168.68.58:11434/api/ps
```

- `"size_vram": 0` → **still on CPU.** Nothing below matters until this changes.
- `"size_vram"` ≈ `"size"` → fully on GPU. This is what you want.
- `0 < size_vram < size` → partially offloaded; the GPU lacks VRAM for the whole
  model and the remainder is running on CPU, which drags the whole request down
  to roughly CPU speed. See step 6.

**b. Measure tokens/sec.** The number that decides whether this was worth it:

```bash
curl -s http://192.168.68.58:11434/api/generate -d '{
  "model":"hermes3:8b",
  "prompt":"Write three sentences about cloud computing.",
  "stream":false, "keep_alive":"30m"
}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(round(d["eval_count"]/(d["eval_duration"]/1e9),1), "tok/s")'
```

~4 tok/s means you are still on CPU. An 8B Q4_0 model on a current NVIDIA card
should land in the tens of tok/s or better.

**c. Watch VRAM during a call:**

```bash
watch -n 0.5 nvidia-smi
```

An `ollama` process should appear holding several GB while a request runs.

---

## 6. VRAM budget for `hermes3:8b`

Measured from this host's own `/api/ps` and `/api/tags`:

| | |
|---|---|
| model weights (Q4_0) | 4.66 GB |
| KV cache @ `num_ctx=8192` | ~1.07 GB per parallel slot |
| compute buffers | ~0.3 GB |
| **total, 1 slot** | **~6.0 GB** |

So an **8 GB** card fits one slot comfortably. A 12 GB card fits two, 24 GB
fits four or more.

If you are short on VRAM, in order of preference:

```ini
# halves the KV cache; requires OLLAMA_FLASH_ATTENTION=1
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

or lower `OLLAMA_NUM_CTX` in this app's `.env` — but note the prompt is
~4,300 tokens, so anything below 8192 risks Ollama silently truncating the
question bank or the requester's instructions rather than erroring.

---

## 7. What to change in this app once the GPU is live

**`REPORT_WORKERS` is the knob that was waiting for this.** It defaults to `1`
because concurrency measured **0.96x** on CPU — the host served one request at
a time and a single generation already saturated the cores. A GPU changes that.

Raise the server's slot count and the app's worker count *together* — one
without the other does nothing:

```ini
# on the Ollama host (systemd override)
Environment="OLLAMA_NUM_PARALLEL=2"
```

```bash
# in this app's .env
REPORT_WORKERS=2
```

Each parallel slot gets its **own** `num_ctx` worth of KV cache, so VRAM scales
roughly linearly with `OLLAMA_NUM_PARALLEL` (see step 6). Confirm with
`/api/ps` after raising it, and don't set it past what the card can hold — an
over-subscribed GPU spills to CPU and gets slower, not faster.

**One caveat specific to how this app builds prompts.** Every student in a
batch shares a byte-identical system prompt (the question bank), and Ollama
reuses its cached evaluation of that shared prefix — that is where the 46%
speedup in the last commit came from. That cache is **per slot**. With 4 slots
and 4 workers, the first 4 students each pay a cold prompt eval instead of just
the first one. On GPU that penalty is small because prompt eval is fast there,
but it means throughput will not scale perfectly linearly with worker count.

Measure rather than assume: generate the same batch at `REPORT_WORKERS=1` and
`=2`, and compare the `PERF {"stage": "batch"...}` line in the Flask log, which
records worker count and total elapsed for exactly this comparison.

---

## 8. Troubleshooting

**`size_vram` is still 0 after a restart**
```bash
journalctl -u ollama -b --no-pager | grep -iE "cuda|gpu|error|no compatible"
```
Usually the driver is missing/too old, or Ollama was installed before the
driver was. Re-run step 2.

**`nvidia-smi` works for you but Ollama still says CPU**
The service runs as the `ollama` user, not you. Check it can reach the devices:
```bash
sudo -u ollama nvidia-smi
ls -l /dev/nvidia*
```
If that fails, add the service user to the right group:
```bash
sudo usermod -aG video,render ollama && sudo systemctl restart ollama
```

**Model loads but is only partly on the GPU** (`0 < size_vram < size`)
Not enough free VRAM. Close other GPU processes (`nvidia-smi`), set
`OLLAMA_MAX_LOADED_MODELS=1`, or apply the `q8_0` KV cache from step 6.

**The app can't reach Ollama after the change**
`OLLAMA_HOST` probably didn't take effect, or the firewall is closed:
```bash
systemctl show ollama --property=Environment
ss -tlnp | grep 11434            # should show 0.0.0.0:11434, not 127.0.0.1:11434
sudo ufw allow 11434/tcp
```
From the Flask machine: `curl http://192.168.68.58:11434/api/version`

---

## 9. Rollback

Nothing here is destructive — the GPU settings are all env vars.

```bash
sudo systemctl revert ollama      # drop the override entirely
sudo systemctl restart ollama
```

And set `REPORT_WORKERS=1` in this app's `.env` if you had raised it.
