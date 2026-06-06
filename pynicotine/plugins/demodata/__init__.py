# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Demo Data - fill every widget with synthetic data for offline UI testing.

The Broadway WebUI work needs a densely populated client (long lists, many open
tabs, big treeviews) without a real Soulseek login. This plugin replays the same
internal events the network thread would emit, so the real handlers build the UI
authentically - fully offline.

Two radio settings, both applied on Save:
  - Static: how much data exists (None up to Browser torture - a wall of rows).
    Changing it clears the previously injected data and re-injects at the new
    level (polled from the live ticker, since there is no settings-saved hook).
  - Live: the rate of ongoing events (None up to Shitstorm, which hammers the
    display every tick to stress Broadway rendering). Read live each tick.

Note: Nicotine's events.emit() calls core.quit() if a non-plugin handler raises,
so every emitted message must be well formed.
"""

import random
import time

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import FileAttributes
from pynicotine.slskmessages import FileSearchResponse
from pynicotine.slskmessages import GetUserStats
from pynicotine.slskmessages import GetUserStatus
from pynicotine.slskmessages import JoinRoom
from pynicotine.slskmessages import MessageUser
from pynicotine.slskmessages import Recommendations
from pynicotine.slskmessages import RoomList
from pynicotine.slskmessages import SayChatroom
from pynicotine.slskmessages import SharedFileListResponse
from pynicotine.slskmessages import UserData
from pynicotine.slskmessages import UserInfoResponse
from pynicotine.slskmessages import UserStatus
from pynicotine.transfers import Transfer

_WORDS = (
    "lorem ipsum dolor amet flac album live remaster bootleg vinyl session "
    "discography lossless single demo rare soundboard mix master many much so very"
).split()
_EXTS = ("mp3", "flac", "ogg", "m4a", "wav")
_COUNTRIES = ("US", "DE", "JP", "BR", "FR", "GB", "SE", "PL", "NL", "RU")

_NAMES = [f"user_{w}_{i}" for i in range(50) for w in _WORDS[:16]]   # 800 users
_ROOMS = [f"room-{w}{s}" for s in ("", "-2") for w in _WORDS]        # ~40 rooms

_STATIC_LEVELS = ("None", "Minimal", "Light", "Heavy", "Browser torture")
_STATIC = (
    dict(rooms=0,  room_users=0,  backlog=0,   dms=0,  dm_msgs=0,  users=0,   buddies=0,
         searchers=0,   files=0,  transfers=0,   browse=0, info=0),   # None
    dict(rooms=1,  room_users=5,  backlog=5,   dms=2,  dm_msgs=5,  users=5,   buddies=3,
         searchers=5,   files=3,  transfers=5,   browse=1, info=1),   # Minimal
    dict(rooms=3,  room_users=15, backlog=20,  dms=5,  dm_msgs=10, users=40,  buddies=10,
         searchers=15,  files=5,  transfers=15,  browse=2, info=2),   # Light
    dict(rooms=8,  room_users=30, backlog=60,  dms=12, dm_msgs=15, users=240, buddies=25,
         searchers=40,  files=8,  transfers=40,  browse=3, info=4),   # Heavy
    dict(rooms=20, room_users=80, backlog=300, dms=40, dm_msgs=60, users=600, buddies=80,
         searchers=150, files=20, transfers=200, browse=8, info=12),  # Browser torture
)

_DYNAMIC_LEVELS = ("None", "Occasional", "Steady", "Rapid", "Shitstorm")
_DYNAMIC = (
    dict(every=1,  chat=0,  churn=0),    # None (gated before use)
    dict(every=20, chat=1,  churn=0),    # Occasional ~ every 5s
    dict(every=8,  chat=1,  churn=0),    # Steady ~ every 2s
    dict(every=3,  chat=3,  churn=4),    # Rapid ~ every 0.75s
    dict(every=1,  chat=12, churn=20),   # Shitstorm - hammer the display
)
_TICK = 0.25   # base scheduler interval (s)


def _phrase(low=3, high=14):
    return " ".join(random.choice(_WORDS) for _ in range(random.randint(low, high)))


def _path():
    folder = "\\".join(random.choice(_WORDS).title() for _ in range(random.randint(2, 4)))
    return f"@@demo\\{folder}\\{_phrase(1, 3).replace(' ', '_')}.{random.choice(_EXTS)}"


def _file(name):
    # Canonical in-memory file tuple: (code, name, size, ext, FileAttributes).
    attrs = FileAttributes(bitrate=random.choice((128, 192, 256, 320)),
                           length=random.randint(90, 600), vbr=False)
    return (1, name, random.randint(1_000_000, 80_000_000), name.rsplit(".", 1)[-1], attrs)


def _sample(pool, count):
    return random.sample(pool, min(count, len(pool)))


class Plugin(BasePlugin):

    def __init__(self):
        super().__init__()
        self.settings = {"static": 3, "dynamic": 2}      # Heavy / Steady
        self.metasettings = {
            "static": {
                "description": "Static data volume (re-injected on save):",
                "type": "radio",
                "options": _STATIC_LEVELS,
            },
            "dynamic": {
                "description": "Live event rate:",
                "type": "radio",
                "options": _DYNAMIC_LEVELS,
            },
        }
        self._alive = False
        self._tick_count = 0
        self._applied_static = None   # last static level actually injected
        self._rooms = []              # rooms actually opened
        self._dm_users = []           # DM tabs opened
        self._users = []              # users with a status (for churn)
        self._buddies = []            # buddies added
        self._transfers = []          # (kind, Transfer) for progress churn

    def loaded_notification(self):
        self._alive = True
        # Fake a logged-in state: many handlers (watch_user, etc.) early-return
        # when login_status is OFFLINE, which would leave rooms/DMs unpopulated.
        self._section("login", self._fake_login)
        events.schedule(delay=1, callback=self._begin)   # let GUI pages exist first

    def _begin(self):
        self._sync_static()
        events.schedule(delay=_TICK, callback=self._tick, repeat=True)

    def disable(self):
        self._alive = False

    # -- helpers --------------------------------------------------------

    def _fake_login(self):
        self.core.users.login_username = "demo-tester"
        self.core.users.login_status = UserStatus.ONLINE

    def _section(self, name, func):
        try:
            func()
        except Exception as error:
            self.log(f"demodata: {name} failed: {error}")

    def _userdata(self, name):
        user = UserData()
        user.username = name
        user.status = random.choice((0, 1, 2))   # offline / away / online
        user.avgspeed = random.randint(0, 5_000_000)
        user.uploadnum = random.randint(0, 9999)
        user.unknown = 0
        user.files = random.randint(0, 200_000)
        user.dirs = random.randint(0, 5_000)
        user.slotsfull = random.randint(0, 4)
        user.country = random.choice(_COUNTRIES)
        return user

    def _transfer(self, statuses):
        path = _path()
        size = random.randint(2_000_000, 90_000_000)
        transfer = Transfer(
            random.choice(_NAMES), virtual_path=path, folder_path=path.rsplit("\\", 1)[0],
            size=size, status=random.choice(statuses), current_byte_offset=0)
        if transfer.status == "Transferring":
            transfer.current_byte_offset = random.randint(0, size)
            transfer.speed = random.randint(50_000, 3_000_000)
        elif transfer.status == "Finished":
            transfer.current_byte_offset = size
        return transfer

    # -- static (applied on save, polled from the ticker) ---------------

    def _sync_static(self):
        target = self.settings["static"]
        if target == self._applied_static:
            return
        self._clear()
        self._applied_static = target
        self._fill(_STATIC[target])

    def _clear(self):
        self._section("clear-rooms", self.core.chatrooms.remove_all_rooms)
        self._section("clear-dms", self.core.privatechat.remove_all_users)
        self._section("clear-search", self.core.search.remove_all_searches)
        self._section("clear-downloads", lambda: self.core.downloads.clear_downloads(
            [t for kind, t in self._transfers if kind == "download"]))
        self._section("clear-uploads", lambda: self.core.uploads.clear_uploads(
            [t for kind, t in self._transfers if kind == "upload"]))
        self._section("clear-buddies",
                      lambda: [self.core.buddies.remove_buddy(name) for name in self._buddies])
        self._rooms = []
        self._dm_users = []
        self._users = []
        self._buddies = []
        self._transfers = []

    def _fill(self, profile):
        if not any(profile.values()):
            self.log("demodata: static = None (cleared)")
            return
        self.log(f"demodata: injecting '{_STATIC_LEVELS[self._applied_static]}' static data")
        self._section("rooms", lambda: self._fill_rooms(profile))
        self._section("dms", lambda: self._fill_dms(profile))
        self._section("users", lambda: self._fill_users(profile))
        self._section("buddies", lambda: self._fill_buddies(profile))
        self._section("search", lambda: self._fill_search(profile))
        self._section("transfers", lambda: self._fill_transfers(profile))
        self._section("userinfo", lambda: self._fill_userinfo(profile))
        self._section("userbrowse", lambda: self._fill_userbrowse(profile))
        self._section("interests", self._fill_interests)
        self.log("demodata: injection done")

    def _fill_rooms(self, profile):
        room_list = RoomList()
        room_list.rooms = [(room, random.randint(2, 1500)) for room in _ROOMS] * 8
        events.emit("room-list", room_list)

        for room in _ROOMS[:profile["rooms"]]:
            # Open the tab first; _join_room rejects rooms not in joined_rooms.
            self.core.chatrooms.show_room(room, switch_page=False)
            self._rooms.append(room)
            join = JoinRoom(room)
            join.users = [self._userdata(name) for name in _sample(_NAMES, profile["room_users"])]
            events.emit("join-room", join)
            for _ in range(profile["backlog"]):
                events.emit("say-chat-room",
                            SayChatroom(room, _phrase(), random.choice(_NAMES)))

    def _fill_dms(self, profile):
        for name in _sample(_NAMES, profile["dms"]):
            self.core.privatechat.show_user(name, switch_page=False)   # open the DM tab
            self._dm_users.append(name)
            for _ in range(profile["dm_msgs"]):
                message = MessageUser(name, _phrase())
                message.message_id = 0
                message.timestamp = int(time.time())
                message.is_new_message = False
                events.emit("message-user", message)

    def _fill_users(self, profile):
        self._users = _sample(_NAMES, profile["users"])
        for name in self._users:
            events.emit("user-status", GetUserStatus(name, random.choice((0, 1, 2))))
            events.emit("user-stats", GetUserStats(
                name, random.randint(0, 5_000_000),
                random.randint(0, 200_000), random.randint(0, 5_000)))

    def _fill_buddies(self, profile):
        self._buddies = _sample(_NAMES, profile["buddies"])
        for name in self._buddies:
            self.core.buddies.add_buddy(name)

    def _fill_search(self, profile):
        self.core.search.do_search("demo " + _phrase(1, 2), "global")
        token = self.core.search.token
        for name in _sample(_NAMES, profile["searchers"]):
            response = FileSearchResponse(
                search_username=name, token=token,
                shares=[_file(_path()) for _ in range(random.randint(1, profile["files"]))],
                freeulslots=random.choice((True, False)),
                ulspeed=random.randint(0, 5_000_000),
                inqueue=random.randint(0, 50), private_shares=[])
            response.username = name                 # peer message correlation
            response.addr = ("127.0.0.1", 1)         # handler unpacks msg.addr
            events.emit("file-search-response", response)

    def _fill_transfers(self, profile):
        for _ in range(profile["transfers"]):
            transfer = self._transfer(("Queued", "Transferring", "Finished", "Paused"))
            self.core.downloads._append_transfer(transfer)        # pylint: disable=protected-access
            events.emit("update-download", transfer, True)
            self._transfers.append(("download", transfer))

        for _ in range(profile["transfers"]):
            transfer = self._transfer(("Queued", "Transferring", "Finished"))
            self.core.uploads._append_transfer(transfer)          # pylint: disable=protected-access
            events.emit("update-upload", transfer, True)
            self._transfers.append(("upload", transfer))

    def _fill_userinfo(self, profile):
        for name in _sample(_NAMES, profile["info"]):
            self.core.userinfo.show_user(name, switch_page=False)
            message = UserInfoResponse(
                descr=_phrase(20, 60), pic=None, totalupl=random.randint(0, 50),
                queuesize=random.randint(0, 200), slotsavail=random.choice((0, 1)),
                uploadallowed=1)
            message.username = name
            message.has_pic = False
            events.emit("user-info-response", message)

    def _fill_userbrowse(self, profile):
        for name in _sample(_NAMES, profile["browse"]):
            self.core.userbrowse.browse_user(name, switch_page=False)
            shares = []
            for _ in range(random.randint(8, 20)):
                folder = "@@demo\\" + "\\".join(
                    random.choice(_WORDS).title() for _ in range(random.randint(1, 3)))
                files = [_file(f"{_phrase(1, 2).replace(' ', '_')}.{random.choice(_EXTS)}")
                         for _ in range(random.randint(1, 25))]
                shares.append((folder, files))
            message = SharedFileListResponse()
            message.username = name
            message.list = shares
            message.privatelist = []
            events.emit("shared-file-list-response", message)

    def _fill_interests(self):
        message = Recommendations()
        message.recommendations = [(word, random.randint(1, 100)) for word in _WORDS]
        message.unrecommendations = [(word + "-bad", -random.randint(1, 100)) for word in _WORDS[:6]]
        events.emit("recommendations", message)

    # -- live events ----------------------------------------------------

    def _tick(self):
        if not self._alive:
            return
        self._sync_static()                  # apply a saved static change
        dynamic = self.settings["dynamic"]
        if dynamic == 0:                     # None
            return
        self._tick_count += 1
        profile = _DYNAMIC[dynamic]
        if self._tick_count % profile["every"] != 0:
            return
        self._section("tick", lambda: self._emit_live(profile))

    def _emit_live(self, profile):
        for _ in range(profile["chat"]):
            if self._rooms:
                events.emit("say-chat-room",
                            SayChatroom(random.choice(self._rooms), _phrase(), random.choice(_NAMES)))
            if self._dm_users:
                name = random.choice(self._dm_users)
                message = MessageUser(name, _phrase())
                message.message_id = 0
                message.timestamp = int(time.time())
                message.is_new_message = True
                events.emit("message-user", message)

        for _ in range(profile["churn"]):
            if self._users:
                name = random.choice(self._users)
                events.emit("user-status", GetUserStatus(name, random.choice((0, 1, 2))))
            if self._transfers:
                kind, transfer = random.choice(self._transfers)
                if transfer.status == "Transferring" and transfer.size:
                    transfer.current_byte_offset = min(
                        transfer.size, transfer.current_byte_offset + random.randint(50_000, 2_000_000))
                    transfer.speed = random.randint(50_000, 3_000_000)
                    events.emit(f"update-{kind}", transfer, False)
