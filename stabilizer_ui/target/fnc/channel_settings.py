import os

from ...iir.channel_settings import AbstractChannelSettings

class FncChannelSettings(AbstractChannelSettings):
    def __init__(self):
        super().__init__(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/channel.ui")

        