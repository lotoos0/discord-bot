import os
import unittest
from unittest.mock import patch

from tests.module_stubs import install_test_stubs

install_test_stubs()

with patch.dict(os.environ, {"DISCORD_TOKEN": "offline-test-token"}):
    import main as bot_main


class MainCommandPolicyTests(unittest.TestCase):
    def test_all_music_commands_are_guild_only(self):
        guild_only_commands = (
            bot_main.join,
            bot_main.leave,
            bot_main.play,
            bot_main.add,
            bot_main.queue_list,
            bot_main.skip,
            bot_main.clearqueue,
            bot_main.shuffle,
            bot_main.remove,
        )

        for command in guild_only_commands:
            with self.subTest(command=command.__name__):
                self.assertTrue(command.__discord_app_commands_guild_only__)

    def test_spam_prone_commands_have_per_user_cooldowns(self):
        expected_cooldowns = {
            bot_main.play: (1, 5.0),
            bot_main.queue_list: (1, 5.0),
            bot_main.join: (1, 10.0),
            bot_main.leave: (1, 10.0),
        }

        for command, expected in expected_cooldowns.items():
            with self.subTest(command=command.__name__):
                cooldowns = command.__discord_app_commands_test_cooldowns__
                self.assertEqual(len(cooldowns), 1)
                self.assertEqual((cooldowns[0]["rate"], cooldowns[0]["per"]), expected)
                self.assertEqual(cooldowns[0]["key"], "user")
