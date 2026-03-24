"""
TCP Sliding Window Simulator - Core Engine
==========================================
- Real UDP sockets on localhost (Sender -> NetworkChannel -> Receiver)
- OOP: Sender, Receiver, NetworkChannel classes
- TCP Congestion Control: Slow Start + AIMD (Additive Increase, Multiplicative Decrease)
- Per-packet timeout timers with retransmission
"""

import socket
import threading
import random
import time
import json
import struct
from dataclasses import dataclass, field
from typing import Callable, List, Dict
from enum import Enum

# ── Ports ──────────────────────────────────────────────────────────────────────
SENDER_PORT   = 9001   # Sender listens for ACKs here
CHANNEL_PORT  = 9002   # NetworkChannel listens for data here
RECEIVER_PORT = 9003   # Receiver listens for data here
ACK_PORT      = 9004   # Receiver sends ACKs back here (channel may drop)

SLOW_START_THRESHOLD_INIT = 16  # ssthresh starting value

# ── RTT / Adaptive Timeout (Jacobson/Karels, RFC 6298) ─────────────────────────
ALPHA       = 0.125   # EWMA weight for smoothed RTT
BETA        = 0.25    # EWMA weight for RTT deviation
MIN_RTO     = 0.5     # floor: never wait less than 500 ms
MAX_RTO     = 8.0     # ceiling
INITIAL_RTO = 2.0     # seed value before first sample


class RTTEstimator:
    """
    srtt   — smoothed (EWMA) RTT
    rttvar — mean deviation of RTT
    rto    — retransmission timeout = srtt + 4*rttvar  (RFC 6298)
    """
    def __init__(self):
        self.srtt    = INITIAL_RTO
        self.rttvar  = INITIAL_RTO / 2
        self.rto     = INITIAL_RTO
        self._samples: List[float] = []
        self._lock   = threading.Lock()

    def update(self, sample: float):
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) == 1:
                self.srtt   = sample
                self.rttvar = sample / 2
            else:
                err          = sample - self.srtt
                self.srtt   += ALPHA * err
                self.rttvar  = (1 - BETA) * self.rttvar + BETA * abs(err)
            self.rto = max(MIN_RTO, min(MAX_RTO, self.srtt + 4 * self.rttvar))

    @property
    def current_rto(self) -> float:
        with self._lock:
            return self.rto

    @property
    def avg_rtt_ms(self) -> float:
        with self._lock:
            return round(sum(self._samples) / len(self._samples) * 1000, 1) if self._samples else 0.0

    @property
    def rttvar_ms(self) -> float:
        with self._lock:
            return round(self.rttvar * 1000, 1)


class Phase(str, Enum):
    SLOW_START            = "slow_start"
    CONGESTION_AVOIDANCE  = "congestion_avoidance"


@dataclass
class Packet:
    seq: int
    data: str
    timestamp: float = field(default_factory=time.time)

    def encode(self) -> bytes:
        payload = json.dumps({"seq": self.seq, "data": self.data}).encode()
        return struct.pack("!I", len(payload)) + payload

    @staticmethod
    def decode(raw: bytes) -> "Packet":
        size = struct.unpack("!I", raw[:4])[0]
        obj  = json.loads(raw[4:4 + size])
        return Packet(seq=obj["seq"], data=obj["data"])


@dataclass
class AckPacket:
    ack: int

    def encode(self) -> bytes:
        return struct.pack("!I", self.ack)

    @staticmethod
    def decode(raw: bytes) -> "AckPacket":
        return AckPacket(ack=struct.unpack("!I", raw[:4])[0])


# ── Network Channel ────────────────────────────────────────────────────────────

class NetworkChannel:
    """
    Sits between Sender and Receiver.
    Forwards packets but randomly drops them to simulate packet loss.
    """

    def __init__(self, loss_prob: float, log: Callable):
        self.loss_prob     = loss_prob
        self.log           = log
        self._stop         = threading.Event()
        self._sock_in      = None
        self._sock_out     = None
        self._sock_ack_in  = None
        self._sock_ack_out = None

    def start(self):
        self._sock_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_in.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_in.bind(("127.0.0.1", CHANNEL_PORT))
        self._sock_in.settimeout(0.5)

        self._sock_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._sock_ack_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_ack_in.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_ack_in.bind(("127.0.0.1", ACK_PORT))
        self._sock_ack_in.settimeout(0.5)

        self._sock_ack_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        threading.Thread(target=self._forward_data, daemon=True).start()
        threading.Thread(target=self._forward_acks, daemon=True).start()

    def stop(self):
        self._stop.set()
        for s in [self._sock_in, self._sock_out, self._sock_ack_in, self._sock_ack_out]:
            if s:
                try: s.close()
                except: pass

    def _forward_data(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock_in.recvfrom(4096)
                pkt    = Packet.decode(raw)
                if random.random() < self.loss_prob:
                    self.log(f"channel:drop   seq={pkt.seq}")
                else:
                    self._sock_out.sendto(raw, ("127.0.0.1", RECEIVER_PORT))
                    self.log(f"channel:forward seq={pkt.seq}")
            except socket.timeout:
                continue
            except Exception:
                break

    def _forward_acks(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock_ack_in.recvfrom(64)
                ack    = AckPacket.decode(raw)
                self._sock_ack_out.sendto(raw, ("127.0.0.1", SENDER_PORT))
                self.log(f"channel:ack_fwd ack={ack.ack}")
            except socket.timeout:
                continue
            except Exception:
                break


# ── Receiver ───────────────────────────────────────────────────────────────────

class Receiver:
    """
    Listens for packets on RECEIVER_PORT.
    Sends cumulative ACKs back to the channel.
    Buffers out-of-order packets (selective buffering).
    """

    def __init__(self, total_packets: int, log: Callable):
        self.total_packets = total_packets
        self.log           = log
        self._expected     = 0
        self._buffer: Dict[int, Packet] = {}
        self._received_all = threading.Event()
        self._stop         = threading.Event()
        self._sock_in      = None
        self._sock_ack     = None

    @property
    def done(self) -> bool:
        return self._received_all.is_set()

    def wait_done(self, timeout=None):
        return self._received_all.wait(timeout)

    def start(self):
        self._sock_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_in.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_in.bind(("127.0.0.1", RECEIVER_PORT))
        self._sock_in.settimeout(0.5)

        self._sock_ack = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        threading.Thread(target=self._receive_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        for s in [self._sock_in, self._sock_ack]:
            if s:
                try: s.close()
                except: pass

    def _receive_loop(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock_in.recvfrom(4096)
                pkt    = Packet.decode(raw)
                self._handle(pkt)
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle(self, pkt: Packet):
        if pkt.seq < self._expected:
            self._send_ack(self._expected - 1)
            return

        self._buffer[pkt.seq] = pkt
        self.log(f"receiver:got   seq={pkt.seq}")

        while self._expected in self._buffer:
            self.log(f"receiver:deliv seq={self._expected}")
            del self._buffer[self._expected]
            self._expected += 1

        self._send_ack(self._expected - 1)

        if self._expected >= self.total_packets:
            self._received_all.set()

    def _send_ack(self, ack_num: int):
        ack = AckPacket(ack=ack_num)
        self._sock_ack.sendto(ack.encode(), ("127.0.0.1", ACK_PORT))
        self.log(f"receiver:ack   ack={ack_num}")


# ── Sender ─────────────────────────────────────────────────────────────────────

class Sender:
    """
    Implements Go-Back-N style sliding window over UDP with:
    - Slow Start + Congestion Avoidance (AIMD)
    - Per-packet retransmission timers
    """

    def __init__(
        self,
        total_packets: int,
        initial_window: int,
        log: Callable,
        stats_callback: Callable,
    ):
        self.total_packets  = total_packets
        self.cwnd           = 1.0
        self.ssthresh       = float(SLOW_START_THRESHOLD_INIT)
        self.phase          = Phase.SLOW_START
        self.log            = log
        self.stats_callback = stats_callback

        self._base          = 0
        self._next_seq      = 0
        self._acked         = [False] * total_packets
        self._timers: Dict[int, float] = {}   # seq -> sent_time
        self._lock          = threading.Lock()
        self._stop          = threading.Event()
        self._sock_send     = None
        self._sock_ack      = None

        # RTT measurement & adaptive timeout
        self.rtt            = RTTEstimator()

        # Fast retransmit state
        self._last_ack      = -1
        self._dup_count     = 0

        self.history: List[dict] = []
        self._step = 0

    def run(self):
        self._sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_ack  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_ack.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_ack.bind(("127.0.0.1", SENDER_PORT))
        self._sock_ack.settimeout(0.3)

        ack_thread = threading.Thread(target=self._ack_loop, daemon=True)
        ack_thread.start()

        while self._base < self.total_packets and not self._stop.is_set():
            with self._lock:
                window_size = max(1, int(self.cwnd))
                while (
                    self._next_seq < self._base + window_size
                    and self._next_seq < self.total_packets
                ):
                    self._send_packet(self._next_seq)
                    self._next_seq += 1

                now = time.time()
                rto = self.rtt.current_rto
                for seq, sent_time in list(self._timers.items()):
                    if not self._acked[seq] and (now - sent_time) > rto:
                        self._on_timeout(seq)

                self._record_stats()

            time.sleep(0.05)

        self._stop.set()
        self.log("sender:done    all packets acknowledged")

    def stop(self):
        self._stop.set()
        for s in [self._sock_send, self._sock_ack]:
            if s:
                try: s.close()
                except: pass

    def _send_packet(self, seq: int):
        pkt = Packet(seq=seq, data=f"PKT_{seq:04d}")
        self._sock_send.sendto(pkt.encode(), ("127.0.0.1", CHANNEL_PORT))
        self._timers[seq] = time.time()
        self.log(f"sender:send    seq={seq}  cwnd={self.cwnd:.1f}  rto={self.rtt.current_rto:.2f}s  phase={self.phase.value}")

    def _ack_loop(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock_ack.recvfrom(64)
                ack    = AckPacket.decode(raw)
                self._on_ack(ack.ack)
            except socket.timeout:
                continue
            except Exception:
                break

    def _on_ack(self, ack_num: int):
        with self._lock:
            # ── Fast Retransmit: duplicate ACK detection ──────────────────────
            if ack_num == self._last_ack and ack_num < self.total_packets - 1:
                self._dup_count += 1
                self.log(f"sender:dupack  ack={ack_num}  count={self._dup_count}")
                if self._dup_count == 3:
                    retx = ack_num + 1
                    if retx < self.total_packets and not self._acked[retx]:
                        self.ssthresh = max(self.cwnd / 2.0, 1.0)
                        self.cwnd     = self.ssthresh        # fast recovery
                        self.phase    = Phase.CONGESTION_AVOIDANCE
                        self._timers[retx] = time.time()
                        pkt = Packet(seq=retx, data=f"PKT_{retx:04d}")
                        self._sock_send.sendto(pkt.encode(), ("127.0.0.1", CHANNEL_PORT))
                        self.log(f"sender:fastretx seq={retx}  ssthresh={self.ssthresh:.1f}  cwnd={self.cwnd:.1f}")
                return
            else:
                self._dup_count = 0
                self._last_ack  = ack_num

            # ── Measure RTT + slide window ────────────────────────────────────
            newly_acked = 0
            for seq in range(self._base, min(ack_num + 1, self.total_packets)):
                if not self._acked[seq]:
                    self._acked[seq] = True
                    newly_acked += 1
                    sent_t = self._timers.pop(seq, None)
                    if sent_t is not None:
                        sample = time.time() - sent_t
                        self.rtt.update(sample)
                        self.log(f"sender:ack     seq={seq}  rtt={sample*1000:.1f}ms  srtt={self.rtt.srtt*1000:.1f}ms")
                    else:
                        self.log(f"sender:ack     seq={seq}")

            if newly_acked:
                while self._base < self.total_packets and self._acked[self._base]:
                    self._base += 1

                if self.phase == Phase.SLOW_START:
                    self.cwnd += newly_acked
                    if self.cwnd >= self.ssthresh:
                        self.phase = Phase.CONGESTION_AVOIDANCE
                        self.log(f"sender:phase   → congestion_avoidance  cwnd={self.cwnd:.1f}")
                else:
                    self.cwnd += newly_acked / self.cwnd
                    self.log(f"sender:phase   congestion_avoidance  cwnd={self.cwnd:.1f}")

    def _on_timeout(self, seq: int):
        self.ssthresh     = max(self.cwnd / 2.0, 1.0)
        self.cwnd         = 1.0
        self.phase        = Phase.SLOW_START
        self._timers[seq] = time.time()
        pkt = Packet(seq=seq, data=f"PKT_{seq:04d}")
        self._sock_send.sendto(pkt.encode(), ("127.0.0.1", CHANNEL_PORT))
        self._dup_count = 0
        self.log(f"sender:timeout seq={seq}  rto={self.rtt.current_rto:.2f}s  → MD: cwnd=1  ssthresh={self.ssthresh:.1f}")

    def _record_stats(self):
        self._step += 1
        entry = {
            "step":     self._step,
            "cwnd":     round(self.cwnd, 2),
            "ssthresh": round(self.ssthresh, 2),
            "base":     self._base,
            "phase":    self.phase.value,
            "avg_rtt":  self.rtt.avg_rtt_ms,
            "rtt_var":  self.rtt.rttvar_ms,
            "rto_ms":   round(self.rtt.current_rto * 1000, 0),
        }
        self.history.append(entry)
        self.stats_callback(entry)


# ── Orchestrator ───────────────────────────────────────────────────────────────

class SimulationOrchestrator:
    """
    Wires up Channel + Receiver + Sender and drives the simulation.
    Calls log_cb(str) for log events and stats_cb(dict) for graph data.
    """

    def __init__(
        self,
        total_packets: int,
        initial_window: int,
        loss_prob: float,
        log_cb: Callable[[str], None],
        stats_cb: Callable[[dict], None],
    ):
        self.total_packets  = total_packets
        self.initial_window = initial_window
        self.loss_prob      = loss_prob
        self.log_cb         = log_cb
        self.stats_cb       = stats_cb
        self._channel       = None
        self._receiver      = None
        self._sender        = None
        self._running       = False

    def run(self):
        self._running = True
        self.log_cb("orchestrator:start")

        self._channel  = NetworkChannel(self.loss_prob, self.log_cb)
        self._receiver = Receiver(self.total_packets, self.log_cb)
        self._sender   = Sender(
            self.total_packets,
            self.initial_window,
            self.log_cb,
            self.stats_cb,
        )

        self._channel.start()
        time.sleep(0.1)
        self._receiver.start()
        time.sleep(0.1)

        self._sender.run()   # blocks until done

        self._receiver.wait_done(timeout=10)
        self._cleanup()
        self.log_cb("orchestrator:complete")
        self._running = False

    def stop(self):
        if self._sender:   self._sender.stop()
        if self._receiver: self._receiver.stop()
        if self._channel:  self._channel.stop()
        self._running = False

    def _cleanup(self):
        time.sleep(0.3)
        self._receiver.stop()
        self._channel.stop()
