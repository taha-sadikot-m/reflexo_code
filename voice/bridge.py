"""WhatsApp WebRTC (aiortc) ↔ LiveKit Cloud audio bridge."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from fractions import Fraction
from typing import Any, Optional

import numpy as np
from av import AudioFrame as AVAudioFrame

from voice.errors import VoiceSdpError

logger = logging.getLogger(__name__)

# Meta's media path guidelines: Opus only, plus DTMF for keypad input.
_ALLOWED_MIME_TYPES = ("audio/opus", "audio/telephone-event")


def normalize_whatsapp_sdp(sdp: str, kind: str = "answer") -> str:
    """Return an RFC 8866 session description accepted by WhatsApp Calling.

    aiortc advertises SHA-256, SHA-384 and SHA-512 fingerprints by default,
    while WhatsApp accepts SHA-256 only. Windows can also expose unusable
    link-local interfaces as ICE candidates.
    """
    output: list[str] = []
    for raw_line in str(sdp or "").splitlines():
        line = raw_line.strip("\r")
        lowered = line.lower()
        if lowered.startswith("a=fingerprint:"):
            match = re.match(
                r"^a=fingerprint:sha-256\s+(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            line = f"a=fingerprint:sha-256 {match.group(1)}"
        elif lowered.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) >= 6:
                try:
                    address = ipaddress.ip_address(parts[4])
                except ValueError:
                    address = None
                if address is not None and address.is_link_local:
                    continue
        output.append(line)

    normalized = "\r\n".join(output) + "\r\n"
    _validate_whatsapp_sdp(normalized, kind)
    return normalized


def _validate_whatsapp_sdp(sdp: str, kind: str = "answer") -> None:
    lines = sdp.splitlines()
    fingerprints = [
        line for line in lines if line.lower().startswith("a=fingerprint:")
    ]
    checks = {
        "one SHA-256 fingerprint": (
            len(fingerprints) == 1
            and fingerprints[0].lower().startswith("a=fingerprint:sha-256 ")
        ),
        "one audio media section": sum(line.startswith("m=audio ") for line in lines)
        == 1,
        "Opus at 48 kHz": any(
            re.match(r"^a=rtpmap:\d+\s+opus/48000(?:/[12])?$", line, re.IGNORECASE)
            for line in lines
        ),
        "ICE username": any(line.startswith("a=ice-ufrag:") for line in lines),
        "ICE password": any(line.startswith("a=ice-pwd:") for line in lines),
    }
    if kind == "offer":
        # As the offerer we let WhatsApp pick the DTLS role.
        checks["negotiable DTLS role"] = any(
            line.lower() in {"a=setup:actpass", "a=setup:active"} for line in lines
        )
    else:
        checks["active DTLS role"] = any(
            line.lower() == "a=setup:active" for line in lines
        )
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VoiceSdpError(
            f"invalid WhatsApp SDP {kind}: " + ", ".join(failed)
        )


class QueuedAudioTrack:
    """Outbound aiortc track fed by LiveKit agent frames."""

    kind = "audio"

    def __init__(self) -> None:
        from aiortc.mediastreams import AudioStreamTrack

        # Subclass at runtime so unit tests can import this module without aiortc.
        self._base = AudioStreamTrack
        AudioStreamTrack.__init__(self)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._sample_rate = 48000
        self._timestamp = 0

    async def enqueue(self, lk_frame: Any) -> None:
        try:
            self._queue.put_nowait(lk_frame)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(lk_frame)
            except asyncio.QueueFull:
                pass

    async def recv(self) -> AVAudioFrame:
        samples = self._sample_rate // 50
        try:
            lk_frame = await asyncio.wait_for(self._queue.get(), timeout=0.02)
            audio_data = np.frombuffer(bytes(lk_frame.data), dtype=np.int16)
            if audio_data.size == 0:
                audio_data = np.zeros(samples, dtype=np.int16)
            frame = AVAudioFrame.from_ndarray(
                audio_data.reshape(1, -1),
                format="s16",
                layout="mono",
            )
            frame.sample_rate = int(getattr(lk_frame, "sample_rate", None) or self._sample_rate)
            samples = audio_data.size
        except asyncio.TimeoutError:
            silence = np.zeros((1, samples), dtype=np.int16)
            frame = AVAudioFrame.from_ndarray(silence, format="s16", layout="mono")
            frame.sample_rate = self._sample_rate
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, frame.sample_rate)
        self._timestamp += samples
        return frame


def _bind_queued_track() -> Any:
    from aiortc.mediastreams import AudioStreamTrack

    class _QueuedAudioTrack(AudioStreamTrack, QueuedAudioTrack):
        def __init__(self) -> None:
            AudioStreamTrack.__init__(self)
            self._queue = asyncio.Queue(maxsize=100)
            self._sample_rate = 48000
            self._timestamp = 0

        async def recv(self) -> AVAudioFrame:
            # AudioStreamTrack is first for aiortc's required inheritance, so
            # explicitly select the queue-backed implementation.
            return await QueuedAudioTrack.recv(self)

    return _QueuedAudioTrack()


class WhatsAppAudioBridge:
    """Bridges Meta WhatsApp WebRTC audio with a LiveKit room."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        # Fired once when WhatsApp media starts arriving (outbound pickup hint).
        self.on_first_audio: Any = None
        self._first_audio_seen = False
        self._pc: Any = None
        self._lk_room: Any = None
        self._lk_source: Any = None
        self._whatsapp_audio_track: Any = None
        self._outbound_track: Any = None
        self._closed = False
        self._start_time: float | None = None
        self._forward_tasks: list[asyncio.Task] = []
        self._whatsapp_forward_task: asyncio.Task | None = None
        self._agent_audio_ready = asyncio.Event()
        self._lk_api: Any = None
        self._room_name: str | None = None

    @property
    def livekit_room(self) -> Any:
        return self._lk_room

    def _open_peer_connection(self) -> None:
        """Create the aiortc peer connection with an Opus-only audio track."""
        from aiortc import (
            RTCConfiguration,
            RTCIceServer,
            RTCPeerConnection,
            RTCRtpSender,
        )

        self._pc = RTCPeerConnection(
            configuration=RTCConfiguration(
                iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]
            )
        )
        self._outbound_track = _bind_queued_track()
        self._pc.addTrack(self._outbound_track)

        try:
            capabilities = RTCRtpSender.getCapabilities("audio")
            preferred = [
                codec
                for codec in capabilities.codecs
                if codec.mimeType.lower() in _ALLOWED_MIME_TYPES
            ]
            if preferred:
                for transceiver in self._pc.getTransceivers():
                    if transceiver.kind == "audio":
                        transceiver.setCodecPreferences(preferred)
        except Exception:
            # A wider codec list still negotiates; Meta just prefers Opus only.
            logger.debug("VOICE_BRIDGE codec preference skipped", exc_info=True)

        @self._pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind == "audio":
                logger.info("VOICE_BRIDGE inbound audio track call_id=%s", self.call_id)
                self._whatsapp_audio_track = track
                self._maybe_start_whatsapp_forwarding()

    async def create_sdp_answer(self, sdp_offer: str) -> str:
        if not sdp_offer or not str(sdp_offer).strip():
            raise VoiceSdpError("empty SDP offer")
        from aiortc import RTCSessionDescription

        self._open_peer_connection()

        try:
            offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
            await self._pc.setRemoteDescription(offer)
            answer = await self._pc.createAnswer()
            await self._pc.setLocalDescription(answer)
            await _wait_ice_complete(self._pc)
        except VoiceSdpError:
            raise
        except Exception as exc:
            raise VoiceSdpError(str(exc)) from exc

        sdp = getattr(self._pc.localDescription, "sdp", None)
        if not sdp:
            raise VoiceSdpError("empty SDP answer")
        normalized = normalize_whatsapp_sdp(sdp)
        logger.info(
            "VOICE_BRIDGE SDP ready call_id=%s candidates=%s fingerprints=1",
            self.call_id,
            sum(
                line.startswith("a=candidate:")
                for line in normalized.splitlines()
            ),
        )
        return normalized

    async def create_sdp_offer(self) -> str:
        """Build the offer WhatsApp requires to place a business-initiated call."""
        self._open_peer_connection()

        try:
            offer = await self._pc.createOffer()
            await self._pc.setLocalDescription(offer)
            await _wait_ice_complete(self._pc)
        except Exception as exc:
            raise VoiceSdpError(str(exc)) from exc

        sdp = getattr(self._pc.localDescription, "sdp", None)
        if not sdp:
            raise VoiceSdpError("empty SDP offer")
        normalized = normalize_whatsapp_sdp(sdp, "offer")
        logger.info(
            "VOICE_BRIDGE SDP offer ready call_id=%s candidates=%s",
            self.call_id,
            sum(
                line.startswith("a=candidate:")
                for line in normalized.splitlines()
            ),
        )
        return normalized

    async def apply_sdp_answer(self, sdp_answer: str) -> None:
        """Apply WhatsApp's answer so media can start flowing."""
        if not sdp_answer or not str(sdp_answer).strip():
            raise VoiceSdpError("empty SDP answer")
        if self._pc is None:
            raise VoiceSdpError("no peer connection awaiting an SDP answer")
        from aiortc import RTCSessionDescription

        try:
            await self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp_answer, type="answer")
            )
        except Exception as exc:
            raise VoiceSdpError(str(exc)) from exc
        logger.info("VOICE_BRIDGE SDP answer applied call_id=%s", self.call_id)

    async def connect_to_livekit(
        self,
        room_name: str,
        livekit_url: str,
        api_key: str,
        api_secret: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from livekit import api as livekit_api
        from livekit import rtc as livekit_rtc

        http_url = livekit_url.replace("wss://", "https://").replace("ws://", "http://")
        self._lk_api = livekit_api.LiveKitAPI(http_url, api_key, api_secret)
        self._room_name = room_name
        metadata_json = json.dumps(metadata or {})
        create_kwargs: dict[str, Any] = {
            "name": room_name,
            "empty_timeout": 300,
            "max_participants": 3,
            "metadata": metadata_json,
        }
        try:
            await self._lk_api.room.create_room(
                livekit_api.CreateRoomRequest(**create_kwargs)
            )
        except Exception as exc:
            logger.info(
                "VOICE_BRIDGE room already exists or create raced call_id=%s err=%s",
                self.call_id,
                type(exc).__name__,
            )
        try:
            updated = await self._lk_api.room.update_room_metadata(
                livekit_api.UpdateRoomMetadataRequest(
                    room=room_name,
                    metadata=metadata_json,
                )
            )
            if getattr(updated, "metadata", "") != metadata_json:
                raise VoiceSdpError("LiveKit room metadata was not confirmed")
        except Exception as exc:
            raise VoiceSdpError(
                f"LiveKit room metadata update failed: {type(exc).__name__}"
            ) from exc

        grants = livekit_api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
        )
        token = (
            livekit_api.AccessToken(api_key, api_secret)
            .with_identity(f"wa-caller-{self.call_id}")
            .with_name("WhatsApp Caller")
            .with_grants(grants)
            .with_metadata(metadata_json)
            .to_jwt()
        )

        self._lk_room = livekit_rtc.Room()

        @self._lk_room.on("participant_connected")
        def on_participant_connected(participant: Any) -> None:
            identity = str(getattr(participant, "identity", "") or "")
            if identity and identity != f"wa-caller-{self.call_id}":
                logger.info(
                    "VOICE_BRIDGE agent participant joined call_id=%s",
                    self.call_id,
                )

        @self._lk_room.on("track_subscribed")
        def on_track_subscribed(
            track: Any,
            publication: Any,
            participant: Any,
        ) -> None:
            kind = getattr(track, "kind", None)
            audio_kind = getattr(livekit_rtc.TrackKind, "KIND_AUDIO", "audio")
            if kind == audio_kind:
                logger.info("VOICE_BRIDGE subscribed agent audio call_id=%s", self.call_id)
                self._agent_audio_ready.set()
                self._forward_tasks.append(
                    asyncio.create_task(self._forward_livekit_to_whatsapp(track))
                )

        await self._lk_room.connect(livekit_url, token)
        self._lk_source = livekit_rtc.AudioSource(sample_rate=48000, num_channels=1)
        lk_track = livekit_rtc.LocalAudioTrack.create_audio_track(
            "whatsapp-audio",
            self._lk_source,
        )
        options = livekit_rtc.TrackPublishOptions(
            source=livekit_rtc.TrackSource.SOURCE_MICROPHONE,
        )
        await self._lk_room.local_participant.publish_track(lk_track, options)
        self._maybe_start_whatsapp_forwarding()
        self._start_time = asyncio.get_event_loop().time()
        logger.info("VOICE_BRIDGE livekit connected call_id=%s room=%s", self.call_id, room_name)

    async def wait_for_agent(self, timeout: float = 25.0) -> bool:
        try:
            await asyncio.wait_for(self._agent_audio_ready.wait(), timeout=timeout)
            logger.info("VOICE_BRIDGE agent audio ready call_id=%s", self.call_id)
            return True
        except asyncio.TimeoutError:
            logger.warning("VOICE_BRIDGE agent join timed out call_id=%s", self.call_id)
            return False

    def _maybe_start_whatsapp_forwarding(self) -> None:
        if (
            self._closed
            or self._whatsapp_audio_track is None
            or self._lk_source is None
            or (
                self._whatsapp_forward_task is not None
                and not self._whatsapp_forward_task.done()
            )
        ):
            return
        self._whatsapp_forward_task = asyncio.create_task(
            self._forward_whatsapp_to_livekit()
        )
        self._forward_tasks.append(self._whatsapp_forward_task)

    def _note_first_audio(self) -> None:
        if self._first_audio_seen:
            return
        self._first_audio_seen = True
        logger.info("VOICE_BRIDGE first whatsapp audio call_id=%s", self.call_id)
        callback = self.on_first_audio
        if callback is None:
            return
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            logger.warning(
                "VOICE_BRIDGE first-audio hook failed call_id=%s",
                self.call_id,
                exc_info=True,
            )

    async def _forward_whatsapp_to_livekit(self) -> None:
        if not self._whatsapp_audio_track or not self._lk_source:
            return
        from livekit import rtc as livekit_rtc
        from aiortc.mediastreams import MediaStreamError
        from av import AudioResampler

        resampler = AudioResampler(format="s16", layout="mono", rate=48000)
        try:
            while not self._closed:
                frame = await self._whatsapp_audio_track.recv()
                self._note_first_audio()
                for mono_frame in resampler.resample(frame):
                    samples = int(mono_frame.samples)
                    data = bytes(mono_frame.planes[0])[: samples * 2]
                    lk_frame = livekit_rtc.AudioFrame(
                        data=data,
                        sample_rate=48000,
                        num_channels=1,
                        samples_per_channel=samples,
                    )
                    await self._lk_source.capture_frame(lk_frame)
        except MediaStreamError:
            # The caller hung up; the hangup webhook closes the bridge.
            logger.info("VOICE_BRIDGE whatsapp audio ended call_id=%s", self.call_id)
        except Exception:
            if not self._closed:
                logger.exception("VOICE_BRIDGE whatsapp→livekit failed call_id=%s", self.call_id)

    async def _forward_livekit_to_whatsapp(self, lk_track: Any) -> None:
        from livekit import rtc as livekit_rtc

        audio_stream = livekit_rtc.AudioStream(lk_track)
        try:
            async for frame_event in audio_stream:
                if self._closed:
                    break
                frame = getattr(frame_event, "frame", frame_event)
                if self._outbound_track is not None:
                    await self._outbound_track.enqueue(frame)
        except Exception:
            if not self._closed:
                logger.exception("VOICE_BRIDGE livekit→whatsapp failed call_id=%s", self.call_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._forward_tasks:
            task.cancel()
        self._forward_tasks.clear()
        self._whatsapp_forward_task = None
        if self._lk_room is not None:
            try:
                await self._lk_room.disconnect()
            except Exception:
                logger.debug("livekit disconnect failed", exc_info=True)
            self._lk_room = None
        if self._lk_api is not None:
            if self._room_name:
                try:
                    from livekit import api as livekit_api

                    await self._lk_api.room.delete_room(
                        livekit_api.DeleteRoomRequest(room=self._room_name)
                    )
                except Exception:
                    logger.debug("livekit room delete failed", exc_info=True)
            try:
                await self._lk_api.aclose()
            except Exception:
                logger.debug("livekit api close failed", exc_info=True)
            self._lk_api = None
            self._room_name = None
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception:
                logger.debug("aiortc close failed", exc_info=True)
            self._pc = None
        logger.info("VOICE_BRIDGE closed call_id=%s", self.call_id)


async def _wait_ice_complete(pc: Any, timeout: float = 5.0) -> None:
    state = getattr(pc, "iceGatheringState", None)
    if state == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_ice() -> None:
        if getattr(pc, "iceGatheringState", None) == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("VOICE_BRIDGE ICE gathering timed out; using current SDP")
