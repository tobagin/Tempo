"""
GStreamer audio system for the metronome application.

This module handles low-latency audio playback of click sounds using GStreamer.
It supports both built-in and custom click sounds with configurable volume levels.
"""

import os
from dataclasses import dataclass
from typing import Optional

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib


@dataclass
class AudioConfig:
    """Configuration for audio system."""
    high_click_path: str = "high.wav"
    low_click_path: str = "low.wav"
    volume: float = 0.8  # 0.0-1.0
    accent_volume: float = 1.0  # 0.0-1.0
    latency_compensation_ms: int = 10


class AudioError(Exception):
    """Base exception for audio-related errors."""
    pass


class MetronomeAudio:
    """
    Low-latency audio system for metronome clicks.
    
    Uses GStreamer with optimized pipeline configuration for minimal latency.
    Supports both built-in and custom click sounds.
    """
    
    def __init__(self, config: Optional[AudioConfig] = None) -> None:
        """
        Initialize the audio system.
        
        Args:
            config: Audio configuration, uses defaults if None
        """
        self.config = config or AudioConfig()
        
        # Initialize GStreamer
        if not Gst.is_initialized():
            Gst.init(None)
            
        # Audio pipeline components
        self.pipeline: Optional[Gst.Pipeline] = None
        self.high_player: Optional[Gst.Element] = None
        self.low_player: Optional[Gst.Element] = None
        
        # State tracking
        self.is_initialized = False
        self.volume = self.config.volume
        self.accent_volume = self.config.accent_volume
        
        # Initialize the pipeline
        self._setup_pipeline()
        
    def _setup_pipeline(self) -> None:
        """Set up the GStreamer pipeline for audio playback."""
        try:
            # Create main pipeline
            self.pipeline = Gst.Pipeline.new("metronome-audio")
            
            # Create audio players for each sound
            self.high_player = self._create_player("high-player", self.config.high_click_path)
            self.low_player = self._create_player("low-player", self.config.low_click_path)
            
            # Add players to pipeline
            self.pipeline.add(self.high_player)
            self.pipeline.add(self.low_player)
            
            # Set up message handling
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_message)
            
            # Set pipeline to ready state
            ret = self.pipeline.set_state(Gst.State.READY)
            if ret == Gst.StateChangeReturn.FAILURE:
                raise AudioError("Failed to set pipeline to READY state")
                
            self.is_initialized = True
            
        except Exception as e:
            raise AudioError(f"Failed to initialize audio system: {e}")
            
    def _create_player(self, name: str, file_path: str) -> Gst.Element:
        """
        Create a GStreamer player element for a sound file.
        
        Args:
            name: Name for the player element
            file_path: Path to the audio file
            
        Returns:
            GStreamer bin element containing the player pipeline
        """
        # Create a bin to hold the player components
        player_bin = Gst.Bin.new(name)
        
        # Find the audio file in various locations
        search_paths = [
            file_path,  # Use as-is if absolute path
            # Flatpak installation paths
            f"/app/share/tempo/sounds/{os.path.basename(file_path)}",
            # Development paths
            f"{os.path.dirname(os.path.dirname(__file__))}/data/sounds/{os.path.basename(file_path)}",
            f"data/sounds/{os.path.basename(file_path)}",
            # Resource paths
            f"/io/github/tobagin/tempo/sounds/{os.path.basename(file_path)}"
        ]
        
        actual_path = None
        for path in search_paths:
            if os.path.exists(path):
                actual_path = path
                break
                
        if not actual_path:
            raise AudioError(f"Audio file not found in any location: {file_path}")
            
        file_path = actual_path
            
        # Create pipeline elements
        source = Gst.ElementFactory.make("filesrc", f"{name}-source")
        decoder = Gst.ElementFactory.make("decodebin", f"{name}-decoder")
        converter = Gst.ElementFactory.make("audioconvert", f"{name}-convert")
        resampler = Gst.ElementFactory.make("audioresample", f"{name}-resample")
        volume = Gst.ElementFactory.make("volume", f"{name}-volume")
        
        # Try different audio sinks for better Flatpak compatibility
        sink_types = ["pulsesink", "pipewireaudiosink", "autoaudiosink"]
        sink = None
        for sink_type in sink_types:
            sink = Gst.ElementFactory.make(sink_type, f"{name}-sink")
            if sink:
                break
        
        if not sink:
            sink = Gst.ElementFactory.make("autoaudiosink", f"{name}-sink")
        
        # Check if all elements were created
        elements = [source, decoder, converter, resampler, volume, sink]
        for element in elements:
            if not element:
                raise AudioError(f"Failed to create GStreamer element for {name}")
        
        # Set file location
        source.set_property("location", file_path)
        
        # Configure volume
        volume.set_property("volume", self.volume)
        
        # Configure sink for low latency
        sink_name = sink.get_factory().get_name()
        
        if sink_name == "pulsesink":
            sink.set_property("buffer-time", 10000)  # 10ms buffer
            sink.set_property("latency-time", 5000)   # 5ms latency
        elif sink_name == "pipewireaudiosink":
            # PipeWire configuration for low latency
            try:
                sink.set_property("buffer-time", 10000)
                sink.set_property("latency-time", 5000)
            except Exception:
                pass
            
        # Add elements to bin
        for element in elements:
            player_bin.add(element)
            
        # Link static elements
        if not source.link(decoder):
            raise AudioError(f"Failed to link source and decoder for {name}")
            
        # Handle dynamic pad linking for decoder
        decoder.connect("pad-added", self._on_pad_added, converter)
        
        # Link converter chain
        if not converter.link(resampler) or not resampler.link(volume) or not volume.link(sink):
            raise AudioError(f"Failed to link converter chain for {name}")
            
        # Store volume element for later access
        setattr(player_bin, "volume_element", volume)
        
        return player_bin
        
    def _on_pad_added(self, element: Gst.Element, pad: Gst.Pad, next_element: Gst.Element) -> None:
        """
        Handle dynamic pad addition from decoder.
        
        Args:
            element: Source element
            pad: New pad
            next_element: Element to link to
        """
        # Get compatible pad from next element
        sink_pad = next_element.get_static_pad("sink")
        
        if sink_pad and not sink_pad.is_linked():
            # Link the pads
            ret = pad.link(sink_pad)
            if ret != Gst.PadLinkReturn.OK:
                pass  # Pad linking failed
                
    def _on_message(self, bus: Gst.Bus, message: Gst.Message) -> bool:
        """
        Handle GStreamer messages.
        
        Args:
            bus: GStreamer bus
            message: Message object
            
        Returns:
            True to continue receiving messages
        """
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            # Silently handle GStreamer errors
            
        elif message.type == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            # Silently handle GStreamer warnings
            
        elif message.type == Gst.MessageType.EOS:
            # End of stream - seek back to beginning for next play
            element = message.src
            if element and hasattr(element, 'seek_simple'):
                element.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0
                )
                
        return True
        
    def play_click(self, is_downbeat: bool = False) -> None:
        """
        Play a metronome click sound using simplified approach.
        
        Args:
            is_downbeat: True for accented beat, False for regular beat
        """
        if not self.is_initialized:
            return
            
        # Choose the appropriate player
        player = self.high_player if is_downbeat else self.low_player
        volume_level = self.accent_volume if is_downbeat else self.volume
        
        # Set volume
        volume_element = getattr(player, "volume_element", None)
        if volume_element:
            volume_element.set_property("volume", volume_level)
            
        # Play using simplified approach
        self._play_player(player)
        
    def _play_player(self, player: Gst.Element) -> None:
        """
        Play a specific player element using simple, direct approach.
        
        Args:
            player: GStreamer player element to play
        """
        # Use simple direct playback - no complex state management
        try:
            # Set to NULL first for clean state
            player.set_state(Gst.State.NULL)
            
            # Set to PLAYING immediately
            player.set_state(Gst.State.PLAYING)
            
            # Schedule stop after short duration
            GLib.timeout_add(400, self._stop_player, player)
            
        except Exception:
            pass  # Audio playback failed silently
        
    def _stop_player(self, player: Gst.Element) -> bool:
        """
        Stop a specific player element.
        
        Args:
            player: GStreamer player element to stop
            
        Returns:
            False to remove from timeout
        """
        player.set_state(Gst.State.READY)
        return False
        
    def set_volume(self, volume: float) -> None:
        """
        Set the volume for regular beats.
        
        Args:
            volume: Volume level (0.0-1.0)
        """
        self.volume = max(0.0, min(1.0, volume))
        
    def set_accent_volume(self, volume: float) -> None:
        """
        Set the volume for accented beats.
        
        Args:
            volume: Volume level (0.0-1.0)
        """
        self.accent_volume = max(0.0, min(1.0, volume))
        
    def set_custom_sounds(self, high_path: str, low_path: str) -> None:
        """
        Set custom sound files for clicks.
        
        Args:
            high_path: Path to high click sound file
            low_path: Path to low click sound file
        """
        # Validate files exist
        if not os.path.exists(high_path):
            raise AudioError(f"High click sound file not found: {high_path}")
            
        if not os.path.exists(low_path):
            raise AudioError(f"Low click sound file not found: {low_path}")
            
        # Update configuration
        self.config.high_click_path = high_path
        self.config.low_click_path = low_path
        
        # Recreate pipeline with new sounds
        self.cleanup()
        self._setup_pipeline()
        
    def test_audio(self) -> bool:
        """
        Test audio system functionality.
        
        Returns:
            True if audio test passes
        """
        if not self.is_initialized:
            return False
            
        try:
            # Test both sounds
            self.play_click(False)  # Regular beat
            GLib.timeout_add(200, lambda: self.play_click(True))  # Downbeat
            return True
            
        except Exception:
            return False
            
    def get_latency_info(self) -> dict:
        """
        Get audio latency information.
        
        Returns:
            Dictionary with latency information
        """
        if not self.pipeline:
            return {"error": "Pipeline not initialized"}
            
        try:
            # Query latency from pipeline
            query = Gst.Query.new_latency()
            if self.pipeline.query(query):
                live, min_latency, max_latency = query.parse_latency()
                return {
                    "live": live,
                    "min_latency_ms": min_latency / 1000000,  # Convert to ms
                    "max_latency_ms": max_latency / 1000000,
                    "configured_latency_ms": self.config.latency_compensation_ms
                }
            else:
                return {"error": "Failed to query latency"}
                
        except Exception as e:
            return {"error": f"Latency query failed: {e}"}
            
    def cleanup(self) -> None:
        """Clean up audio resources."""
        if self.pipeline:
            # Stop pipeline
            self.pipeline.set_state(Gst.State.NULL)
            
            # Remove message handler
            bus = self.pipeline.get_bus()
            bus.remove_signal_watch()
            
            # Clear references
            self.pipeline = None
            self.high_player = None
            self.low_player = None
            
        self.is_initialized = False
        
    def __del__(self) -> None:
        """Destructor to ensure cleanup."""
        self.cleanup()