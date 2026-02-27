import logging
from ...topic_tree import TopicTree
from ...iir.filters import FILTERS

from . import *

logger = logging.getLogger(__name__)


class StabilizerSettings:
    """
    Defines the MQTT topic tree for firmware-controlled settings.

    Structure:

        settings/
            v_offset
            ki_alpha
            kp_alpha
            afe/<channel>
            iir_ch/<channel>/<stage>
            harmonic_wave_parameters/<channel>/<order>
            stream_target

    These topics map directly to firmware configuration parameters.
    """

    @classmethod
    def set(cls):
        # Root of firmware settings subtree
        cls.root = TopicTree("settings")
        # Top-level scalar parameters
        cls.v_offset = cls.root.create_child("v_offset")
        cls.ki_ff = cls.root.create_child("ki_alpha")
        cls.kp_ff = cls.root.create_child("kp_alpha")
        cls.stream_target = cls.root.create_child("stream_target")
        # Structured groups
        (afe, cls.iir_root, cls.harm_param_root) = cls.root.create_children(["afe", "iir_ch", "harmonic_wave_parameters"])

        # Create per-channel index nodes for IIR filters
        cls.iir_root.create_children(["0", "1"])

        # Create per-channel index nodes for harmonic parameters
        cls.harm_param_root.create_children(["0", "1"])
        
        # Per-channel AFE gain topics
        cls.afes = afe.create_children(["0", "1"])
        
        # IIR structure:
        # settings/iir_ch/<channel>/<stage>
        #
        # Each stage corresponds to one biquad in the cascade.
        cls.iirs = [
            cls.iir_root.create_children(
                [f"{ch}/{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)])
            for ch in range(NUM_CHANNELS)
        ]

        # Harmonic structure:
        # settings/harmonic_wave_parameters/<channel>/<order>
        #
        # Each order contains:
        #     amp
        #     phase
        cls.harm_param = [cls.harm_param_root.create_children(
                [f"{ch}/{order}" for order in range(NUM_OF_HARMONIC_ORDERS)]
        ) for ch in range(NUM_CHANNELS)]


StabilizerSettings.set()


class UiSettings:
    """
    Defines the MQTT topic tree for UI-side configuration.

    This tree mirrors firmware structure but:
    - Allows local editing before pushing to firmware
    - Includes UI-specific nodes
    - Separates display state from firmware state
    """

    @classmethod
    def set(cls):
        cls.root = TopicTree("ui")
        # One subtree per channel
        ui_channels = cls.root.create_children([f"ch{ch}" for ch in range(NUM_CHANNELS)])
        
        # UI IIR filter nodes per channel
        cls.iirs = [
            ui_channels[ch].create_children(
                [f"iir{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)])
            for ch in range(NUM_CHANNELS)
        ]

       # UI harmonic parameter nodes per channel
        cls.h_params = [
            ui_channels[ch].create_children(
                [f"h_params{order}" for order in range(NUM_OF_HARMONIC_ORDERS)]
            ) for ch in range(NUM_CHANNELS)
        ]

        
        # Each harmonic order has amplitude and phase
        for ch in range(NUM_CHANNELS):
            for order in range(NUM_OF_HARMONIC_ORDERS):
                cls.h_params[ch][order].create_children(["amp", "phase"])
        
        # Each IIR stage contains:
        #   filter type
        #   plot configuration
        #   filter-specific parameter sets
        for ch in range(NUM_CHANNELS):
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                cls.iirs[ch][iir].create_children(
                    ["filter", "y_offset", "y_min", "y_max", "x_offset"])
                # Add parameter subtrees for each available filter type
                for filter in FILTERS:
                    filter_topic = cls.iirs[ch][iir].create_child(filter.filter_type)
                    filter_topic.create_children(filter.parameters)

UiSettings.set()

# Global MQTT application root:
#
# dt/sinara/ff_fb/<MAC>/
#
# Under this:
#   settings/  = firmware configuration
#   ui/        = UI state
#   meta/      = metadata
#   alive/     = heartbeat
global app_root
app_root = TopicTree.new("dt/sinara/ff_fb/<MAC>")
app_root.set_children([StabilizerSettings.root, UiSettings.root])
app_root.create_children(["meta", "alive"])
app_root.set_app_root()
