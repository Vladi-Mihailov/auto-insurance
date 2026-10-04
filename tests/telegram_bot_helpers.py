"""Offline harness for the Telegram bot (not a test module -- no test_
prefix). Updates are fed straight into the real production Dispatcher
(app.telegram_bot.app.build_dispatcher); every outgoing Bot API call goes
to RecordingSession instead of the network, which records it and returns a
synthetic Telegram response. An API method the harness doesn't expect
fails the test outright rather than being silently faked.
"""

import asyncio
import itertools
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetFile, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Contact, Document, File, InlineKeyboardMarkup, Message, PhotoSize, Update, User
from pydantic import SecretStr

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection, init_db
from app.telegram_bot.app import build_dispatcher
from app.telegram_bot.config import BotConfig
from app.telegram_bot.profile import BotProfile

# Shape-valid but fake -- never a real token; RecordingSession never sends it anywhere.
FAKE_TOKEN = "123456789:" + "A" * 35

TEST_PROFILE = BotProfile(
    bot_key="testbot",
    country_code="GE",
    intro_title="🇬🇪 ОСАГО Грузии",
    intro_text="Оформите страховку автомобиля для поездки в Грузию онлайн.",
)

GE_CATEGORIES = [
    (7, "passenger_car", "Легковой"),
    (10, "motorcycle", "Мотоцикл"),
    (9, "bus", "Автобус"),
    (8, "truck", "Грузовик"),
    (11, "trailer", "Прицеп"),
    (12, "special_vehicle", "Спецтехника"),
]


# Fictional-enough catalog rows for the vehicle steps: manufacturer name ->
# model names. Every manufacturer is marked models-synced, so no test ever
# triggers the on-demand tpl.ge model sync (see test_models_synced_on_demand
# for that path, which is monkeypatched).
CATALOG = {
    "TOYOTA": ["CAMRY", "COROLLA", "LAND CRUISER", "Other"],
    "VOLKSWAGEN": ["GOLF", "PASSAT", "TIGUAN", "Other"],
    "BMW": ["318", "320", "X5", "Other"],
    # tpl.ge's own catch-all manufacturer (external_id 1 in the real catalog)
    "Other": ["Other"],
}
POPULAR = {"TOYOTA", "VOLKSWAGEN"}


def seed_catalog(db_file) -> dict[str, int]:
    """Categories + CATALOG. Returns {"TOYOTA": id, "TOYOTA/CAMRY": id, ...}."""
    init_db(db_file)
    conn = get_connection(db_file)
    ids: dict[str, int] = {}
    try:
        for external_id, code, name in GE_CATEGORIES:
            upsert_category(conn, external_id=external_id, code=code, name=name, icon=None)
        for m_index, (manufacturer, models) in enumerate(CATALOG.items(), start=1):
            manufacturer_id = upsert_manufacturer(
                conn,
                external_id=1 if manufacturer == "Other" else 50_000 + m_index,
                name=manufacturer,
                is_popular=manufacturer in POPULAR,
            )
            ids[manufacturer] = manufacturer_id
            for model_index, model in enumerate(models, start=1):
                external_id = -1 if model == "Other" else 50_000 + m_index * 100 + model_index
                ids[f"{manufacturer}/{model}"] = upsert_model(
                    conn, external_id=external_id, manufacturer_id=manufacturer_id, name=model
                )
            mark_models_synced(conn, manufacturer_id)
        conn.commit()
    finally:
        conn.close()
    return ids


class UnexpectedApiCall(AssertionError):
    pass


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list = []
        self._message_ids = itertools.count(1000)
        # file_id -> bytes served by GetFile + stream_content (in memory only)
        self.files: dict[str, bytes] = {}
        self.downloads: list[str] = []
        # Sends to these chat ids fail like a user who blocked the bot.
        self.fail_chats: set[int] = set()

    async def make_request(self, bot, method, timeout=None):
        now = datetime.now(timezone.utc)
        if getattr(method, "chat_id", None) in self.fail_chats:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        self.calls.append(method)
        if isinstance(method, SendDocument) and not isinstance(method.document, str):
            # an uploaded file (e.g. a downloaded policy PDF): Telegram answers with its new file id
            n = next(self._message_ids)
            return Message(
                message_id=n, date=now, chat=Chat(id=method.chat_id, type="private"),
                document=Document(file_id=f"uploaded-{n}", file_unique_id=f"u-uploaded-{n}", file_name="policy.pdf", mime_type="application/pdf"),
            )
        if isinstance(method, (SendPhoto, SendDocument)):
            return Message(message_id=next(self._message_ids), date=now, chat=Chat(id=method.chat_id, type="private"))
        if isinstance(method, SendMessage):
            # A real Message only ever carries an INLINE keyboard back; reply
            # keyboards stay visible on the recorded call itself.
            inline = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            return Message(
                message_id=next(self._message_ids),
                date=now,
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                reply_markup=inline,
            )
        if isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id,
                date=now,
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                reply_markup=method.reply_markup,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetFile):
            if method.file_id not in self.files:
                raise UnexpectedApiCall("GetFile for a file the test never registered")
            return File(
                file_id=method.file_id,
                file_unique_id="u-" + method.file_id,
                file_size=len(self.files[method.file_id]),
                file_path=f"documents/{method.file_id}",
            )
        raise UnexpectedApiCall(f"unexpected Bot API call in test: {type(method).__name__}")

    async def close(self):
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        file_id = url.rsplit("/", 1)[-1]
        if file_id not in self.files:
            raise UnexpectedApiCall("download of a file the test never registered")
        self.downloads.append(file_id)
        yield self.files[file_id]


def make_user(user_id: int, username: str | None = None) -> User:
    return User(id=user_id, is_bot=False, first_name="Test", username=username)


class BotHarness:
    """One "bot process": its own Bot, Dispatcher and storage over the given
    settings' DB file. Build a second one on the same settings to simulate a
    restart."""

    def __init__(
        self, settings, profile: BotProfile = TEST_PROFILE, manager_ids=frozenset({999}), *, ocr_provider=None,
        orientation_detector=None, owner_id: int | None = None,
    ):
        # Building the dispatcher bootstraps manager_ids / owner_id into the
        # staff table, exactly like a bot start (the sole manager -> owner).
        self.config = BotConfig(
            profile=profile, token=SecretStr(FAKE_TOKEN), manager_ids=frozenset(manager_ids), owner_id=owner_id
        )
        self.session = RecordingSession()
        self.bot = Bot(token=FAKE_TOKEN, session=self.session)
        # Never the settings-built defaults: no real OpenAI provider, no
        # host-dependent Tesseract detector unless a test passes one.
        self.dispatcher = build_dispatcher(
            settings=settings, config=self.config, ocr_provider=ocr_provider, orientation_detector=orientation_detector,
            album_debounce_seconds=0.3,  # generous vs. the per-item DB work on a slow test machine
        )
        self.loop = asyncio.new_event_loop()
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)

    def close(self):
        self.loop.close()

    # False: updates return while an OCR batch still runs in the background
    # (concurrency tests); settle() then lets it finish.
    drain_background = True

    def _feed(self, update: Update) -> list:
        start = len(self.session.calls)
        self.loop.run_until_complete(self.dispatcher.feed_update(self.bot, update))
        if self.drain_background:
            self.loop.run_until_complete(self._drain_batches())
        return self.session.calls[start:]

    async def _drain_batches(self) -> None:
        runtime = self.dispatcher["documents_runtime"]
        while runtime.tasks:
            await asyncio.gather(*list(runtime.tasks), return_exceptions=True)

    def settle(self) -> list:
        """Let every pending album and background OCR batch finish."""
        start = len(self.session.calls)
        self.loop.run_until_complete(self.dispatcher["documents_runtime"].drain())
        return self.session.calls[start:]

    def pump_until(self, condition, timeout: float = 10.0) -> bool:
        """Run the event loop (background batches progress) until condition()."""
        async def wait():
            deadline = self.loop.time() + timeout
            while not condition():
                if self.loop.time() > deadline:
                    return False
                await asyncio.sleep(0.01)
            return True
        return self.loop.run_until_complete(wait())

    def next_message_id(self) -> int:
        return next(self._message_ids)

    def send_text(self, user: User, text: str, *, chat_type: str = "private", chat_id: int | None = None) -> list:
        message = Message(
            message_id=next(self._message_ids),
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id if chat_id is not None else user.id, type=chat_type),
            from_user=user,
            text=text,
        )
        return self._feed(Update(update_id=next(self._update_ids), message=message))

    def _message(self, user: User, **fields) -> Message:
        return Message(
            message_id=next(self._message_ids),
            date=datetime.now(timezone.utc),
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            **fields,
        )

    def send_photo(self, user: User, data: bytes, *, file_id: str, file_size: int | None = None) -> list:
        self.session.files[file_id] = data
        photo = PhotoSize(
            file_id=file_id, file_unique_id="u-" + file_id, width=1000, height=700,
            file_size=len(data) if file_size is None else file_size,
        )
        return self._feed(Update(update_id=next(self._update_ids), message=self._message(user, photo=[photo])))

    def _album_message(self, user: User, group: str, item) -> Message:
        kind, file_id, data, *rest = item
        self.session.files[file_id] = data
        if kind == "photo":
            media = {"photo": [PhotoSize(file_id=file_id, file_unique_id="u-" + file_id, width=1000, height=700, file_size=len(data))]}
        else:
            media = {"document": Document(file_id=file_id, file_unique_id="u-" + file_id, file_name="scan", mime_type=rest[0], file_size=len(data))}
        return self._message(user, media_group_id=group, **media)

    def send_album(self, user: User, group: str, items: list, *, concurrent: bool = False, flush: bool = True) -> list:
        """items: ("photo", file_id, bytes) or ("document", file_id, bytes, mime).
        Each becomes its own Message update with the same media_group_id --
        exactly how Telegram delivers an album. concurrent=True feeds them
        all at once (asyncio.gather) instead of one after another."""
        start = len(self.session.calls)
        updates = [Update(update_id=next(self._update_ids), message=self._album_message(user, group, item)) for item in items]
        if concurrent:
            async def feed_all():
                await asyncio.gather(*(self.dispatcher.feed_update(self.bot, u) for u in updates))
            self.loop.run_until_complete(feed_all())
        else:
            for update in updates:
                self.loop.run_until_complete(self.dispatcher.feed_update(self.bot, update))
        if flush:
            self.flush_albums()
        return self.session.calls[start:]

    def flush_albums(self) -> list:
        """Let every pending album finish (the debounce + one processing run)."""
        start = len(self.session.calls)
        runtime = self.dispatcher["documents_runtime"]
        if self.drain_background:
            self.loop.run_until_complete(runtime.drain())
        else:
            self.loop.run_until_complete(runtime.collector.drain())
        return self.session.calls[start:]

    def send_document(self, user: User, data: bytes, *, file_id: str, mime_type: str, file_name: str = "scan.jpg") -> list:
        self.session.files[file_id] = data
        document = Document(
            file_id=file_id, file_unique_id="u-" + file_id, file_name=file_name, mime_type=mime_type, file_size=len(data)
        )
        return self._feed(Update(update_id=next(self._update_ids), message=self._message(user, document=document)))

    def send_contact(self, user: User, phone_number: str, *, contact_user_id: int | None = None) -> list:
        contact = Contact(phone_number=phone_number, first_name="Test", user_id=user.id if contact_user_id is None else contact_user_id)
        return self._feed(Update(update_id=next(self._update_ids), message=self._message(user, contact=contact)))

    def press(self, user: User, callback_data: str, *, message_text: str = "…", message_id: int | None = None) -> list:
        """message_id: the message the button is on (default: a new, newest
        one) -- an older id presses a button of an earlier screen."""
        message = Message(
            message_id=next(self._message_ids) if message_id is None else message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user.id, type="private"),
            text=message_text,
        )
        callback = CallbackQuery(
            id=str(next(self._update_ids)), from_user=user, chat_instance="ci", data=callback_data, message=message
        )
        return self._feed(Update(update_id=next(self._update_ids), callback_query=callback))


def screens(calls: list) -> list:
    """The SendMessage/EditMessageText calls (what the customer sees)."""
    return [c for c in calls if isinstance(c, (SendMessage, EditMessageText))]


def last_screen(calls: list):
    shown = screens(calls)
    assert shown, "expected the bot to show something"
    return shown[-1]


def buttons(call) -> list[tuple[str, str]]:
    """(text, callback_data) of every inline button on a shown screen."""
    markup = call.reply_markup
    if markup is None:
        return []
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def nav(to: str, rt: str = "") -> str:
    """A packed NavCb payload (the rt part is always present)."""
    return f"n:{to}:{rt}"


def sent_to(calls: list, chat_id: int, kind=None) -> list:
    """Outgoing calls addressed to chat_id (optionally of one method type)."""
    return [c for c in calls if getattr(c, "chat_id", None) == chat_id and (kind is None or isinstance(c, kind))]


def make_outbox_due(db_file) -> None:
    """Test helper: every pending outbox job becomes due now (skips backoff)."""
    conn = get_connection(db_file)
    try:
        conn.execute("UPDATE telegram_outbox SET next_attempt_at = '2000-01-01T00:00:00+00:00' WHERE status = 'pending'")
        conn.commit()
    finally:
        conn.close()
