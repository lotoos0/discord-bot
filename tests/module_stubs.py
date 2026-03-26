import sys
import types


def install_test_stubs():
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")

        class FFmpegPCMAudio:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class PCMVolumeTransformer:
            def __init__(self, source):
                self.original = source
                self.source = source
                self.volume = 0.5
                self._volume = 0.5

        class Client:
            pass

        class TextChannel:
            pass

        class VoiceChannel:
            def __init__(self):
                self.members = []

        class StageChannel:
            def __init__(self):
                self.members = []

        class Interaction:
            pass

        class Member:
            pass

        class VoiceState:
            pass

        discord.FFmpegPCMAudio = FFmpegPCMAudio
        discord.PCMVolumeTransformer = PCMVolumeTransformer
        discord.Client = Client
        discord.TextChannel = TextChannel
        discord.VoiceChannel = VoiceChannel
        discord.StageChannel = StageChannel
        discord.Interaction = Interaction
        discord.Member = Member
        discord.VoiceState = VoiceState
        sys.modules["discord"] = discord

    if "yt_dlp" not in sys.modules:
        yt_dlp = types.ModuleType("yt_dlp")

        class YoutubeDL:
            def __init__(self, options):
                self.options = options

            def extract_info(self, url, download=False):
                raise RuntimeError("YoutubeDL stub should be patched in tests")

        yt_dlp.YoutubeDL = YoutubeDL
        sys.modules["yt_dlp"] = yt_dlp
