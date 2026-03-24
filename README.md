# TCP Sliding Window Simulator

https://github.com/user-attachments/assets/98d7fd56-388e-462e-8b4e-4f355de57b72

A full-stack network simulation tool that implements **TCP-like reliability over raw UDP sockets** — built entirely with Python's standa

rd library and vanilla JavaScript. No frameworks, no external dependencies.

> Built as a portfolio project to demonstrate applied knowledge of computer networking, systems programming, and full-stack development.

---

## Features

| | |
|---|---|
| 🔌 **Real UDP Sockets** | Packets travel over actual OS-level UDP sockets, not arrays or mocks |
| 📈 **AIMD Congestion Control** | Slow Start → Congestion Avoidance with live CWND graph |
| ⏱️ **Adaptive Timeout (RFC 6298)** | RTO = SRTT + 4×RTTVAR, updated after every ACK — no fixed timeout |
| 📡 **RTT Measurement** | Per-packet round-trip time tracked, smoothed (EWMA), and displayed live |
| ⚡ **TCP Fast Retransmit** | 3 duplicate ACKs trigger immediate retransmit before timeout fires |
| 🧱 **OOP Architecture** | Clean `Sender`, `Receiver`, `NetworkChannel`, `SimulationOrchestrator` classes |
| 🌐 **WebSocket Streaming** | Real-time log and stats streamed to browser — no polling |
| 🖥️ **Zero Dependencies** | Pure Python stdlib + CDN Chart.js. `python3 server.py` and you're done |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  Browser  (index.html)                │
│                                                      │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ Packet      │  │  CWND Graph  │  │  Network    │ │
│  │ Pipeline    │  │  (Chart.js)  │  │  Log Stream │ │
│  └─────────────┘  └──────────────┘  └─────────────┘ │
└────────────────────────┬─────────────────────────────┘
                         │  WebSocket  ws://127.0.0.1:8081
                         │  HTTP       http://127.0.0.1:8080
┌────────────────────────▼─────────────────────────────┐
│                   server.py                          │
│   Pure-stdlib HTTP + WebSocket server                │
│   SimClient  ──►  SimulationOrchestrator             │
└────────────────────────┬─────────────────────────────┘
                         │  Python threads
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   NetworkChannel     Receiver       Sender
   UDP :9002          UDP :9003      UDP :9001 (ACKs)
   UDP :9004 (ACKs)
```

### Data flow

```
Sender ──UDP──► NetworkChannel ──(drop?)──► Receiver
                                               │
Sender ◄──UDP── NetworkChannel ◄──────────── ACK
```

`NetworkChannel` randomly drops packets according to `loss_prob`, simulating an unreliable network. Everything above it — sliding window, retransmission, congestion control — is built on top of raw UDP, exactly like real TCP is built on top of IP.

---

## Getting Started

**Requirements:** Python 3.11+ — no `pip install` needed.

```bash
git clone https://github.com/YashSankhe2701/TCP-Sliding-Window-Simulation.git
cd tcp-sliding-window-simulator
python3 server.py
```

Open your browser to **http://127.0.0.1:8080**

---

## How It Works

### Sliding Window
The sender maintains a congestion window (`cwnd`) that limits how many unacknowledged packets can be in flight at once. As ACKs arrive, the window slides forward and new packets are sent.

### Slow Start
`cwnd` starts at 1 and doubles with every ACK received — exponential growth — until it reaches `ssthresh`. This quickly ramps up throughput on a fresh connection.

### Congestion Avoidance (AIMD)
Once `cwnd ≥ ssthresh`, growth slows to additive increase: `cwnd += 1/cwnd` per ACK. On any loss event, multiplicative decrease kicks in: `ssthresh = cwnd/2`, `cwnd = 1`. This is the classic **AIMD** algorithm used in real TCP.

### Adaptive Timeout (RFC 6298 / Jacobson-Karels)
Rather than a fixed 2-second timeout, the RTO is computed dynamically after every ACK:

```
SRTT   = (1 - α) × SRTT + α × RTT_sample        α = 0.125
RTTVAR = (1 - β) × RTTVAR + β × |SRTT - sample|  β = 0.25
RTO    = SRTT + 4 × RTTVAR                        clamped to [0.5s, 8.0s]
```

This is the same algorithm implemented in the Linux kernel's TCP stack.

### TCP Fast Retransmit
If the sender receives **3 duplicate ACKs** for the same sequence number, it immediately retransmits the missing packet without waiting for the RTO to expire. Unlike a timeout, fast retransmit sets `cwnd = ssthresh` (not `cwnd = 1`), so throughput recovers much faster.

### Selective Buffering at Receiver
Out-of-order packets are held in a buffer at the receiver. Cumulative ACKs are sent for the highest contiguous in-order packet, allowing the sender to detect gaps efficiently.

---

## Project Structure

```
tcp_simulator/
├── server.py        # Pure-stdlib HTTP + WebSocket server
├── simulation.py    # Core engine: Sender, Receiver, NetworkChannel, RTTEstimator
└── index.html       # Frontend: enterprise dashboard with live graph + log
```

---

## Key Implementation Details

- **Port reuse** — all UDP sockets set `SO_REUSEADDR` before `bind()`, so restarting the simulation immediately after stopping never causes "port already in use" errors
- **Thread safety** — the sender's main loop and ACK loop share state protected by a single `threading.Lock`
- **Clean shutdown** — WebSocket disconnect (including browser refresh) triggers a `try/finally` teardown that calls `.stop()` on all threads and joins the simulation thread with a 3-second timeout, preventing zombie threads
- **Karn's algorithm** — RTT samples are only recorded for packets that were not retransmitted, avoiding inflated RTT estimates from ambiguous ACKs

---

## License

MIT
