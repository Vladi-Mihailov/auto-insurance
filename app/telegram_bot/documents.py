"""Document photo upload + OCR for the Telegram bot.

Two ways in, ONE processing path (process_documents). The batch is claimed
under the customer's lock, recognized in the background WITHOUT it (so
/start and "❌ Отменить оформление" stay responsive), and applied under the
lock again only if the checkout is still the same one (checkout_gen) -- a
late result of an abandoned checkout is discarded.

- a Telegram album (several photos sent together = several Message updates
  sharing a media_group_id): items are stored silently as they arrive and
  the album is processed automatically, exactly once, when it's complete
  (app.telegram_bot.albums.AlbumCollector);
- photos sent one by one: each is stored and acknowledged, and the customer
  presses "✅ Все документы загружены" when done.

Only Telegram's file references are kept in the draft
(pending_document_files, then document_files); image bytes are downloaded
into memory for the recognition pass and dropped -- never written to disk.

One batch = every pending photo:
1. download + real-bytes validation + EXIF + optional orientation
   correction (app.ocr.image / app.ocr.orientation), off the event loop; an
   unusable photo is skipped (and named once in the result), the rest go on;
2. ONE provider call carrying every usable photo (app.ocr.provider) -- the
   model sees the techpassport sides and the passport together and applies
   its per-document source rules; plus at most ONE orientation retry;
3. catalog matching (app.ocr.parser.build_candidates) -- if it can't run
   (catalog service down), the recognized text is still kept as hints;
4. the shared draft mapping + merge (checkout_service.ocr_draft_update /
   merge_ocr_update): OCR only fills EMPTY fields, never overwrites a typed
   or already confirmed value, and a differing reading is shown as a
   conflict instead of being applied;
5. ALWAYS the vehicle review -- recognized data is never accepted without
   the customer confirming it.

Every failure is caught at its stage, logged with safe metadata only (bot,
a derived batch id, file count, stage, exception class -- never images,
document values, names or Telegram file ids), and answered with ONE simple
Russian message; the photos stay pending so the customer can retry.
"""

import asyncio
import dataclasses
import hashlib
import io
import logging
import time

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.types import CallbackQuery, Message

from app.checkout import service as checkout_service
from app.db import get_connection
from app.diagnostics import describe_exception
from app.ocr.image import (
    ALLOWED_CONTENT_TYPES,
    MAX_FILES_PER_RECOGNITION,
    MAX_UPLOAD_BYTES,
    NormalizedImage,
    UploadValidationError,
    normalize_image_bytes,
    rotated_variants,
)
from app.ocr.models import OcrResult, VehicleDataCandidates, combine_ocr_results
from app.ocr.parser import apply_other_fallback, build_candidates, choose_identifier, normalize_registration_number
from app.ocr.provider import OcrProviderError
from app.telegram_bot import texts
from app.telegram_bot.albums import AlbumCollector
from app.telegram_bot.context import Ctx
from app.telegram_bot.keyboards import DocsCb, DocsRetryCb
from app.telegram_bot.steps import REQUIRED_DOCUMENT_PHOTOS, Flow, checkout_gen, documents_keyboard, documents_status, go, show

logger = logging.getLogger(__name__)

# Smaller than this and a document's text isn't legible anyway.
MIN_IMAGE_SIDE = 300
# Orientation retry: at most this many uncertain photos get rotated copies,
# and the retry call carries at most this many images in total.
_MAX_RETRY_UNCERTAIN = 2
_MAX_RETRY_IMAGES = 8
_VEHICLE_KEYS = {"registration_number", "identifier", "identifier_type", "manufacturer_id", "model_id"}
# How many processed album ids a draft remembers (late duplicates of these
# are ignored). Albums older than that are long finished.
_PROCESSED_GROUPS_KEPT = 20


@dataclasses.dataclass
class DocumentsRuntime:
    """What album finalization needs outside a handler (it runs after the
    item handlers have returned): the collector, the FSM storage, the
    per-customer isolation lock, and the DB file to open its own connection."""

    collector: AlbumCollector
    storage: BaseStorage
    isolation: BaseEventIsolation
    db_file: object
    # Running OCR batches (they run outside the customer's lock) and which
    # (session, checkout generation) each belongs to -- at most one per checkout.
    tasks: set = dataclasses.field(default_factory=set)
    inflight: set = dataclasses.field(default_factory=set)

    def spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        """Wait for every pending album and running batch (tests, shutdown)."""
        while True:
            await self.collector.drain()
            if not self.tasks:
                if not self.collector.is_idle():
                    continue
                return
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


async def _rt(state: FSMContext) -> str:
    return (await state.get_data()).get("return_to") or ""


def _batch_id(ctx: Ctx, ref: str) -> str:
    """A short, non-reversible id to correlate one batch's log lines."""
    return hashlib.sha256(f"{ctx.profile.bot_key}:{ctx.user_id}:{ref}".encode()).hexdigest()[:10]


async def _answer(callback: CallbackQuery, *args, **kwargs) -> None:
    """Telegram rejects answering a callback that waited too long (e.g.
    queued behind a running OCR batch) -- that must never turn into an
    error for the customer."""
    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest:
        logger.debug("Late callback answer skipped")


# ------------------------------------------------------------- uploads


def _file_entry(message: Message) -> tuple[dict | None, str | None]:
    """(pending entry, rejection note). Only images go into the OCR
    pipeline -- a PDF or any other document is refused here."""
    if message.photo:
        photo = message.photo[-1]  # Telegram's largest rendition
        entry = dict(file_id=photo.file_id, file_unique_id=photo.file_unique_id, kind="photo", mime_type="image/jpeg", file_size=photo.file_size)
    else:
        document = message.document
        if document.mime_type not in ALLOWED_CONTENT_TYPES:
            return None, texts.DOCUMENTS_UNSUPPORTED
        entry = dict(
            file_id=document.file_id, file_unique_id=document.file_unique_id, kind="document",
            mime_type=document.mime_type, file_size=document.file_size,
        )
    if entry["file_size"] is not None and entry["file_size"] > MAX_UPLOAD_BYTES:
        return None, texts.DOCUMENTS_TOO_BIG
    entry["media_group_id"] = message.media_group_id
    return entry, None


async def on_document_message(message: Message, state: FSMContext, ctx: Ctx, documents_runtime: DocumentsRuntime):
    if ctx.draft().get("order_id"):
        raise SkipHandler()  # after an order exists, photos are payment receipts -- never checkout documents
    entry, rejection = _file_entry(message)
    group = message.media_group_id
    draft = ctx.draft()
    pending = list(draft.get("pending_document_files") or [])
    known = {f["file_unique_id"] for f in pending + list(draft.get("document_files") or [])}
    processed = list(draft.get("processed_media_groups") or [])

    if group and group in processed:
        if entry is None or entry["file_unique_id"] in known:
            return  # late duplicate of an album that was already processed
        entry["media_group_id"] = None
        group = None  # a genuinely new late item: handled like a single photo

    if group:
        await _album_item(message, state, ctx, documents_runtime, group, entry, rejection, pending, known)
        return

    keyboard = documents_keyboard(draft, await _rt(state))
    if entry is None:
        await message.answer(rejection, reply_markup=keyboard)
        return
    if entry["file_unique_id"] in known:
        await message.answer(texts.DOCUMENTS_DUPLICATE, reply_markup=keyboard)
        return
    if len(pending) >= MAX_FILES_PER_RECOGNITION:
        await message.answer(texts.DOCUMENTS_LIMIT, reply_markup=keyboard)
        return
    pending.append(entry)
    draft = ctx.merge({"pending_document_files": pending, "ocr_last_batch_failed": False})
    ctx.event("bot_document_uploaded", kind=entry["kind"], pending_count=len(pending), album=False)
    if len(pending) >= REQUIRED_DOCUMENT_PHOTOS:
        # The set is complete: recognize right away -- no "done" button.
        await process_documents(ctx, state, message, documents_runtime, batch_ref=f"single:{message.message_id}")
        return
    await message.answer(documents_status(draft), reply_markup=documents_keyboard(draft, await _rt(state)))


async def _album_item(message, state, ctx, runtime: DocumentsRuntime, group, entry, rejection, pending, known):
    """Store the item (persisted immediately, deduplicated by
    file_unique_id), say nothing, and (re)arm the album's single owner."""
    key = (ctx.profile.bot_key, ctx.user_id, group)
    # An unfinished album from an earlier, interrupted upload (e.g. the bot
    # restarted mid-album) is replaced by this one, never mixed into it.
    fresh = [p for p in pending if p.get("media_group_id") in (None, group)]
    changed = len(fresh) != len(pending)
    if entry is None:
        runtime.collector.add_note(key, rejection)
    elif entry["file_unique_id"] not in known:
        if len(fresh) < MAX_FILES_PER_RECOGNITION:
            fresh.append(entry)
            changed = True
            ctx.event("bot_document_uploaded", kind=entry["kind"], pending_count=len(fresh), album=True)
        else:
            runtime.collector.add_note(key, texts.DOCUMENTS_LIMIT_SKIPPED.format(limit=MAX_FILES_PER_RECOGNITION))
    if changed:
        ctx.merge({"pending_document_files": fresh, "ocr_last_batch_failed": False})

    template = dataclasses.replace(ctx, conn=None)
    fsm_key = state.key
    gen = checkout_gen(ctx.draft())

    async def finalize(notes: list[str]) -> None:
        await _finalize_album(runtime, template, fsm_key, message, group, notes, gen)

    runtime.collector.touch(key, finalize)


async def _finalize_album(
    runtime: DocumentsRuntime, template: Ctx, fsm_key, target: Message, group: str, notes: list[str], gen: str
) -> None:
    async with runtime.isolation.lock(fsm_key):  # the same per-customer lock as every handler
        conn = get_connection(runtime.db_file)
        try:
            ctx = dataclasses.replace(template, conn=conn)
            fsm = FSMContext(storage=runtime.storage, key=fsm_key)
            draft = ctx.draft()
            processed = list(draft.get("processed_media_groups") or [])
            if draft.get("order_id") or group in processed:
                return
            if checkout_gen(draft) != gen:
                return  # the checkout was reset after these photos arrived: they belong to the old one
            if await fsm.get_state() != Flow.documents.state:
                return  # the customer moved on; the photos stay pending for later
            # Claim the album BEFORE recognizing: nothing can start a second batch for it.
            draft = ctx.merge({"processed_media_groups": (processed + [group])[-_PROCESSED_GROUPS_KEPT:]})
            pending = len(draft.get("pending_document_files") or [])
            if 0 < pending < REQUIRED_DOCUMENT_PHOTOS:
                # Part of the set: ONE message with the counter; the rest may follow.
                status = documents_status(draft)
                await ctx.bot.send_message(
                    ctx.chat_id,
                    "\n".join([*notes, status]),
                    reply_markup=documents_keyboard(draft, (await fsm.get_data()).get("return_to") or ""),
                )
                return
            # Only the claim runs under the lock; recognition runs in the background.
            await process_documents(ctx, fsm, target, runtime, batch_ref=f"album:{group}", notes=notes)
        finally:
            conn.close()


# ------------------------------------------------------------ recognition


def orientation_retry_images(normalized: list[NormalizedImage]) -> list[bytes] | None:
    """The images for the single orientation retry, or None when no photo's
    orientation is in doubt (then a retry can't help and isn't made)."""
    uncertain = [n for n in normalized if n.orientation_uncertain][:_MAX_RETRY_UNCERTAIN]
    if not uncertain:
        return None
    images = [n.jpeg_bytes for n in normalized]
    for image in uncertain:
        images.extend(rotated_variants(image.jpeg_bytes))
    return images[:_MAX_RETRY_IMAGES]


async def recognize(ctx: Ctx, normalized: list[NormalizedImage], stage: list[str] | None = None) -> tuple[OcrResult, bool]:
    """(result, whether the orientation retry was used). Raises OcrProviderError.
    stage (optional, a one-item list) is updated with the step in progress,
    for failure diagnostics."""
    stage = stage if stage is not None else [""]
    images = [(n.jpeg_bytes, "image/jpeg") for n in normalized]
    stage[0] = "ocr"
    result = await asyncio.to_thread(ctx.ocr_provider.recognize, images)
    if result.is_complete_for_checkout:
        return result, False
    stage[0] = "orientation_retry"
    retry = await asyncio.to_thread(orientation_retry_images, normalized)
    if not retry:
        return result, False
    stage[0] = "ocr_retry"
    second = await asyncio.to_thread(ctx.ocr_provider.recognize, [(image, "image/jpeg") for image in retry])
    return combine_ocr_results(result, second), True


def _candidates_without_catalog(result: OcrResult) -> VehicleDataCandidates:
    """The same normalization build_candidates applies, minus the catalog
    lookup -- used when catalog matching can't run, so the recognized text
    survives as review hints (manufacturer/model then picked by hand)."""
    identifier_type, identifier = choose_identifier(result.vin, result.chassis_number)
    return VehicleDataCandidates(
        registration_number=normalize_registration_number(result.registration_number),
        identifier_type=identifier_type,
        identifier=identifier,
        manufacturer_id=None,
        manufacturer_text=result.manufacturer,
        model_id=None,
        model_text=result.model,
    )


class _Progress:
    """Exactly one progress indicator per batch: for a button press it's the
    pressed message (edited into the result afterwards); for an album it's
    one new message, closed with a short final status."""

    def __init__(self, ctx: Ctx, target: Message | CallbackQuery, count: int):
        self._ctx = ctx
        self._target = target
        self._message: Message | None = None
        self._text = texts.OCR_IN_PROGRESS_WITH_COUNT.format(
            counter=texts.DOCUMENTS_COUNTER.format(count=count, required=REQUIRED_DOCUMENT_PHOTOS)
            if count <= REQUIRED_DOCUMENT_PHOTOS
            else texts.DOCUMENTS_COUNTER_ONLY.format(count=count),
        )

    async def start(self) -> None:
        if isinstance(self._target, CallbackQuery):
            await show(self._target, self._text, None)
        else:
            self._message = await self._ctx.bot.send_message(self._ctx.chat_id, self._text)

    async def close(self, ok: bool) -> None:
        if self._message is None:
            return
        try:
            # Explicit chat/message ids: never depends on the returned Message
            # object being bound to a Bot instance.
            await self._ctx.bot.edit_message_text(
                texts.OCR_PROGRESS_DONE if ok else texts.OCR_PROGRESS_FAILED,
                chat_id=self._message.chat.id,
                message_id=self._message.message_id,
            )
        except TelegramBadRequest:
            pass

    async def cancel(self) -> None:
        """The checkout was reset while this batch ran: the progress message
        (or the pressed one) says so -- nothing else is shown."""
        if self._message is not None:
            chat_id, message_id = self._message.chat.id, self._message.message_id
        elif isinstance(self._target, CallbackQuery) and self._target.message is not None:
            chat_id, message_id = self._target.message.chat.id, self._target.message.message_id
        else:
            return
        try:
            await self._ctx.bot.edit_message_text(texts.OCR_CANCELLED, chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest:
            pass


def _fail_log(ctx: Ctx, batch: str, files: int, stage_name: str, exc: BaseException) -> None:
    """Safe diagnostics only: stage/substage, exception class, and for
    Unicode errors the codec, reason and offending code points -- never
    the text itself, a document value, a file id or a key."""
    substage, detail = None, None
    if isinstance(exc, OcrProviderError):
        substage = exc.classification.get("substage")
        cause = exc.__cause__
        detail = describe_exception(cause) if isinstance(cause, UnicodeError) else exc.classification.get("category")
    label = f"{stage_name}/{substage}" if substage else stage_name
    logger.warning(
        "OCR batch %s failed at %s (bot=%s, files=%d): %s%s",
        batch, label, ctx.profile.bot_key, files, describe_exception(exc), f" [{detail}]" if detail else "",
    )


@dataclasses.dataclass
class _Batch:
    """One claimed batch, carried from the claim (under the lock) through
    recognition (no lock) to the apply step (under the lock again)."""

    batch: str  # the log correlation id
    gen: str  # the checkout generation it was claimed for
    pending: list[dict]
    rt: str
    notes: list[str]
    target: Message | CallbackQuery
    progress: _Progress
    session_id: str

    @property
    def inflight_key(self) -> tuple:
        return (self.session_id, self.gen)


@dataclasses.dataclass
class _Outcome:
    kind: str  # ok | no_valid_images | provider_error | nothing_found | unexpected
    notes: list[str]
    stage: str = ""
    files: int = 0
    duration_ms: int = 0
    error: BaseException | None = None
    result: OcrResult | None = None
    retry_used: bool = False
    candidates: VehicleDataCandidates | None = None
    make_text: str | None = None
    model_text: str | None = None
    rotated: int = 0
    uncertain: int = 0


async def process_documents(
    ctx: Ctx, fsm: FSMContext, target: Message | CallbackQuery, runtime: DocumentsRuntime, *, batch_ref: str, notes=()
) -> bool:
    """Claim every pending photo as ONE batch and recognize it in the
    background. Called under the customer's lock (a handler or the album
    finalizer); returns True when a batch was started.

    The slow part (download, OCR, catalog) runs WITHOUT the lock, so /start,
    "❌ Отменить оформление" or any other button stays responsive meanwhile.
    The result is applied under the lock again -- and only if the draft is
    still the same checkout (checkout_gen unchanged, no order, the photos
    still pending): a result of an abandoned checkout is discarded, never
    written into the fresh one."""
    draft = ctx.draft()
    pending = list(draft.get("pending_document_files") or [])
    rt = await _rt(fsm)
    notes = list(notes)
    if not pending:
        if notes:
            await go(target, fsm, ctx, "documents", rt=rt, notice="\n".join(notes))
        return False
    if ctx.ocr_provider is None:
        await go(target, fsm, ctx, "documents", rt=rt, notice="\n".join([*notes, texts.OCR_UNAVAILABLE]))
        return False
    gen = checkout_gen(draft)
    if (ctx.session_id, gen) in runtime.inflight:
        return False  # this checkout's batch is already running; its progress message is up

    batch = _batch_id(ctx, batch_ref)
    # This batch's number: an explicit retry button names it, so a doubled or
    # stale retry press can't start a second run (see on_documents_retry).
    ctx.merge({"ocr_batch_seq": int(draft.get("ocr_batch_seq") or 0) + 1, "ocr_last_batch_failed": False})
    logger.info("OCR batch %s started (bot=%s, files=%d)", batch, ctx.profile.bot_key, len(pending))
    progress = _Progress(ctx, target, len(pending))
    try:
        await progress.start()
    except Exception as exc:  # noqa: BLE001 -- ONE simple message, never a traceback
        _fail_log(ctx, batch, len(pending), "start", exc)
        ctx.merge({"ocr_last_batch_failed": True})
        await go(target, fsm, ctx, "documents", rt=rt, notice=texts.OCR_FAILED)
        return False
    claimed = _Batch(batch=batch, gen=gen, pending=pending, rt=rt, notes=notes, target=target, progress=progress, session_id=ctx.session_id)
    runtime.inflight.add(claimed.inflight_key)
    runtime.spawn(_run_batch(runtime, dataclasses.replace(ctx, conn=None), fsm.key, claimed))
    return True


async def _run_batch(runtime: DocumentsRuntime, template: Ctx, fsm_key, claimed: _Batch) -> None:
    try:
        outcome = await _recognize_batch(runtime, template, claimed)
        async with runtime.isolation.lock(fsm_key):  # the same per-customer lock as every handler
            conn = get_connection(runtime.db_file)
            try:
                ctx = dataclasses.replace(template, conn=conn)
                await _apply_batch(ctx, FSMContext(storage=runtime.storage, key=fsm_key), claimed, outcome)
            except Exception as exc:  # noqa: BLE001
                logger.warning("OCR batch %s: could not apply the result (%s)", claimed.batch, type(exc).__name__)
            finally:
                runtime.inflight.discard(claimed.inflight_key)
                conn.close()
    finally:
        runtime.inflight.discard(claimed.inflight_key)


async def _recognize_batch(runtime: DocumentsRuntime, template: Ctx, claimed: _Batch) -> _Outcome:
    """Download + normalize + OCR + catalog match. No lock, no draft access;
    its own connection for the catalog only."""
    pending, notes = claimed.pending, list(claimed.notes)
    stage = "download"
    conn = get_connection(runtime.db_file)
    ctx = dataclasses.replace(template, conn=conn)
    try:
        normalized: list[NormalizedImage] = []
        for index, file in enumerate(pending, start=1):
            try:
                buffer = await ctx.bot.download(file["file_id"], destination=io.BytesIO())
                data = buffer.getvalue()
            except Exception as exc:  # noqa: BLE001 -- one file failing to download never sinks the batch
                _fail_log(ctx, claimed.batch, len(pending), "download", exc)
                notes.append(texts.OCR_FILE_SKIPPED.format(index=index, reason="не удалось скачать файл."))
                continue
            try:
                normalized.append(
                    await asyncio.to_thread(
                        normalize_image_bytes, data, orientation_detector=ctx.orientation_detector, min_side=MIN_IMAGE_SIDE
                    )
                )
            except UploadValidationError as exc:
                notes.append(texts.OCR_FILE_SKIPPED.format(index=index, reason=str(exc)))
            except Exception as exc:  # noqa: BLE001 -- unexpected decoder/detector failure for one image
                _fail_log(ctx, claimed.batch, len(pending), "image_prepare", exc)  # validation + EXIF + orientation (Tesseract)
                notes.append(texts.OCR_FILE_SKIPPED.format(index=index, reason="не удалось прочитать изображение."))
            finally:
                del data

        if not normalized:
            return _Outcome("no_valid_images", notes, files=len(pending))

        stage = "ocr"
        started = time.monotonic()
        ocr_stage = ["ocr"]
        try:
            result, retry_used = await recognize(ctx, normalized, ocr_stage)
        except OcrProviderError as exc:
            _fail_log(ctx, claimed.batch, len(pending), ocr_stage[0], exc)
            return _Outcome(
                "provider_error", notes, files=len(normalized), error=exc,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            stage = ocr_stage[0]
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        outcome = _Outcome(
            "ok", notes, files=len(normalized), duration_ms=duration_ms, result=result, retry_used=retry_used,
            rotated=sum(1 for n in normalized if n.rotated_clockwise),
            uncertain=sum(1 for n in normalized if n.orientation_uncertain),
        )
        if result.fields_found_count == 0:
            outcome.kind = "nothing_found"
            return outcome

        stage = "catalog_match"
        try:
            # May sync a manufacturer's models from tpl.ge on demand (blocking HTTP).
            outcome.candidates = await asyncio.to_thread(build_candidates, conn, result)
        except Exception as exc:  # noqa: BLE001 -- the recognized text must survive a catalog outage
            _fail_log(ctx, claimed.batch, len(pending), stage, exc)
            conn.rollback()
            outcome.candidates = _candidates_without_catalog(result)

        stage = "catalog_fallback"
        try:
            # Recognized but not in the catalog -> tpl.ge's own "Other"
            # entry, keeping the document's text for display (never
            # treated as "not recognized").
            outcome.candidates, outcome.make_text, outcome.model_text = await asyncio.to_thread(
                apply_other_fallback, conn, outcome.candidates
            )
        except Exception as exc:  # noqa: BLE001 -- keep the unmatched text as a hint instead
            _fail_log(ctx, claimed.batch, len(pending), stage, exc)
            conn.rollback()
        return outcome
    except Exception as exc:  # noqa: BLE001 -- ONE simple message for the whole batch, never a traceback
        _fail_log(ctx, claimed.batch, len(pending), stage, exc)
        return _Outcome("unexpected", notes, stage=stage, files=len(pending), error=exc)
    finally:
        conn.close()


def _still_current(draft: dict, claimed: _Batch) -> bool:
    """The batch's checkout is still the customer's current one: same
    generation, no order since, and its photos not dropped meanwhile."""
    if draft.get("order_id") or checkout_gen(draft) != claimed.gen:
        return False
    still_pending = {f["file_unique_id"] for f in draft.get("pending_document_files") or []}
    return any(f["file_unique_id"] in still_pending for f in claimed.pending)


async def _apply_batch(ctx: Ctx, fsm: FSMContext, claimed: _Batch, outcome: _Outcome) -> None:
    """Under the lock: write the result into the draft and show it -- or,
    for an abandoned checkout, discard it without touching anything."""
    target, progress, rt, notes = claimed.target, claimed.progress, claimed.rt, outcome.notes
    draft = ctx.draft()
    if not _still_current(draft, claimed):
        logger.info("OCR batch %s discarded: the checkout was reset meanwhile (bot=%s)", claimed.batch, ctx.profile.bot_key)
        ctx.event("bot_ocr_discarded", files_count=len(claimed.pending))
        await progress.cancel()
        return
    processed_ids = {f["file_unique_id"] for f in claimed.pending}
    remaining = [f for f in draft.get("pending_document_files") or [] if f["file_unique_id"] not in processed_ids]

    if outcome.kind == "no_valid_images":
        ctx.merge({"pending_document_files": remaining})
        ctx.event("bot_ocr_failed", reason="no_valid_images", files_count=outcome.files)
        await progress.close(False)
        await go(target, fsm, ctx, "documents", rt=rt, notice="\n".join(notes))
        return
    if outcome.kind == "provider_error":
        exc = outcome.error
        ctx.event(
            "bot_ocr_failed", reason="provider_error", files_count=outcome.files, duration_ms=outcome.duration_ms,
            error_type=type(exc).__name__, category=exc.classification.get("category"), substage=exc.classification.get("substage"),
        )
        ctx.merge({"ocr_last_batch_failed": True})
        await progress.close(False)
        # The photos stay pending; the screen now offers an EXPLICIT retry.
        await go(target, fsm, ctx, "documents", rt=rt, notice="\n".join([*notes, texts.OCR_FAILED]))
        return
    if outcome.kind == "unexpected":
        ctx.event("bot_ocr_failed", reason="unexpected", stage=outcome.stage, files_count=outcome.files, error_type=type(outcome.error).__name__)
        ctx.merge({"ocr_last_batch_failed": True})
        await progress.close(False)
        await go(target, fsm, ctx, "documents", rt=rt, notice=texts.OCR_FAILED)
        return
    result = outcome.result
    if outcome.kind == "nothing_found":
        ctx.merge({"pending_document_files": remaining})
        ctx.event(
            "bot_ocr_failed", reason="nothing_found", files_count=outcome.files, duration_ms=outcome.duration_ms,
            retry_used=outcome.retry_used,
        )
        await progress.close(False)
        await go(target, fsm, ctx, "documents", rt=rt, notice="\n".join([*notes, texts.OCR_NOTHING_FOUND]))
        return

    update = checkout_service.ocr_draft_update(result, outcome.candidates)
    writes, conflicts = checkout_service.merge_ocr_update(draft, update)
    if _VEHICLE_KEYS & writes.keys():
        writes["vehicle_confirmed"] = False
    if "manufacturer_id" in writes and outcome.make_text:
        writes["vehicle_make_text"] = outcome.make_text
    if "model_id" in writes and outcome.model_text:
        writes["vehicle_model_text"] = outcome.model_text
    # Passport name/number/citizenship from the same batch fill the
    # still-empty policyholder fields (shown on the review to confirm).
    writes.update(checkout_service.ocr_policyholder_prefill({**draft, **writes}))
    writes["pending_document_files"] = remaining
    writes["document_files"] = list(draft.get("document_files") or []) + claimed.pending
    ctx.merge(writes)
    ctx.event(
        "bot_ocr_completed",
        complete=result.is_complete_for_checkout,
        fields_found_count=result.fields_found_count,
        files_count=outcome.files,
        duration_ms=outcome.duration_ms,
        retry_used=outcome.retry_used,
        rotated_images=outcome.rotated,
        uncertain_orientation_images=outcome.uncertain,
        conflicts=len(conflicts),
    )
    logger.info(
        "OCR batch %s completed (bot=%s, files=%d, complete=%s)",
        claimed.batch, ctx.profile.bot_key, outcome.files, result.is_complete_for_checkout,
    )
    if conflicts:
        notes.append(texts.OCR_CONFLICTS.format(fields=texts.missing_fields_text(conflicts)))
    await progress.close(True)
    # ONE consolidated review of everything known so far.
    await go(target, fsm, ctx, "checkout_review", notice="\n".join(notes) or None)


async def on_documents_done(callback: CallbackQuery, state: FSMContext, ctx: Ctx, documents_runtime: DocumentsRuntime):
    """"✅ Все документы загружены" (and the old "🔍 Распознать" of messages
    sent before this change) -- runs the batch for the photos sent one by
    one. A repeated/queued press finds nothing pending and does nothing."""
    await _answer(callback)
    draft = ctx.draft()
    if draft.get("order_id"):
        await go(callback, state, ctx, "order")
        return
    if not draft.get("pending_document_files"):
        await go(callback, state, ctx, "resume")
        return
    if draft.get("ocr_last_batch_failed"):
        # A stale "done" button after a failed batch never silently re-runs
        # it -- show the explicit retry choice instead.
        await go(callback, state, ctx, "documents", rt=await _rt(state))
        return
    await state.set_state(Flow.documents)
    await process_documents(ctx, state, callback, documents_runtime, batch_ref=f"manual:{callback.id}")


async def on_documents_retry(
    callback: CallbackQuery, callback_data: DocsRetryCb, state: FSMContext, ctx: Ctx, documents_runtime: DocumentsRuntime
):
    """"🔄 Попробовать распознать ещё раз": exactly ONE new attempt for the
    batch this button was shown for. A doubled, queued or stale press (its
    seq no longer the latest failed batch) only re-shows the current screen."""
    await _answer(callback)
    draft = ctx.draft()
    if draft.get("order_id"):
        await go(callback, state, ctx, "order")
        return
    is_current = (
        draft.get("ocr_last_batch_failed")
        and draft.get("pending_document_files")
        and callback_data.seq == int(draft.get("ocr_batch_seq") or 0)
    )
    if not is_current:
        await go(callback, state, ctx, "resume")
        return
    await state.set_state(Flow.documents)
    await process_documents(ctx, state, callback, documents_runtime, batch_ref=f"retry:{callback_data.seq}")


async def on_documents_discard(callback: CallbackQuery, state: FSMContext, ctx: Ctx):
    """"📷 Загрузить другие фото": drop the failed batch and start over with
    new photos (already-recognized data in the draft is untouched)."""
    await _answer(callback)
    if not ctx.draft().get("order_id"):
        ctx.merge({"pending_document_files": [], "ocr_last_batch_failed": False})
    await go(callback, state, ctx, "documents", rt=await _rt(state))


async def on_documents_text(message: Message, state: FSMContext, ctx: Ctx):
    draft = ctx.draft()
    # The counter ("Фото получено: N из 3") and what happens next -- e.g.
    # after a restart interrupted an album, the button below finishes it.
    text = documents_status(draft) if draft.get("pending_document_files") else texts.DOCUMENTS_TEXT_HINT
    await message.answer(text, reply_markup=documents_keyboard(draft, await _rt(state)))


def register(router: Router) -> None:
    router.message.register(on_document_message, Flow.documents, F.photo | F.document)
    router.message.register(on_documents_text, Flow.documents, F.text)
    router.callback_query.register(on_documents_done, DocsCb.filter(F.action.in_({"done", "recognize"})))
    router.callback_query.register(on_documents_discard, DocsCb.filter(F.action == "discard"))
    router.callback_query.register(on_documents_retry, DocsRetryCb.filter())
