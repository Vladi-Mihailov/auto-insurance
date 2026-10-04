"""Russian customer-facing copy for the bot. Presentation only -- prices,
periods and categories are never defined here, only how they are shown."""

from datetime import date

from app.formatting import format_rub
from app.telegram_bot.profile import BotProfile

# Customer-facing labels per canonical vehicle_category_code (the codes
# themselves come from the synced catalog, app.catalog). This dict also
# fixes the display order; a catalog category missing here still shows,
# under its catalog name, after these.
CATEGORY_LABELS: dict[str, str] = {
    "passenger_car": "🚗 Легковой автомобиль",
    "motorcycle": "🏍 Мотоцикл",
    "truck": "🚛 Грузовой автомобиль",
    "bus": "🚌 Автобус",
    "trailer": "🚚 Прицеп",
    "special_vehicle": "🚜 Спецтехника",
}

BTN_APPLY = "🚗 Оформить страховку"
BTN_TODAY = "Сегодня"
BTN_TOMORROW = "Завтра"
BTN_MANUAL_DATE = "Ввести дату"
BTN_BACK = "⬅️ Назад"
BTN_METHOD_DOCUMENTS = "📸 Загрузить документы"
BTN_METHOD_MANUAL = "✍️ Заполнить вручную"

CHOOSE_CATEGORY = "Выберите тип транспортного средства:"
CATEGORY_UNAVAILABLE = "Этот тип транспорта сейчас недоступен. Выберите другой:"
NO_PERIODS = "Для этого типа транспорта оформление сейчас недоступно. Выберите другой тип:"
DATE_PROMPT = "📅 Когда должна начать действовать страховка?"
METHOD_PLACEHOLDER_DOCUMENTS = (
    "📸 Загрузка документов появится на следующем этапе — он ещё в разработке.\n\n"
    "Ваш выбор сохранён. Как заполнить данные?"
)
METHOD_PLACEHOLDER_MANUAL = (
    "✍️ Ручное заполнение появится на следующем этапе — он ещё в разработке.\n\n"
    "Ваш выбор сохранён. Как заполнить данные?"
)
USE_BUTTONS = "Пожалуйста, воспользуйтесь кнопками ниже."
GENERIC_ERROR = "Что-то пошло не так. Попробуйте ещё раз или отправьте /start."


def format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def intro(profile: BotProfile) -> str:
    return f"{profile.intro_title}\n\n{profile.intro_text}\n\nВыберите действие:"


def price_line(label: str, price_rub: int) -> str:
    return f"{label} — {format_rub(price_rub)} ₽"


def choose_period(category_label: str, lines: list[str]) -> str:
    return f"{category_label}\n\nВыберите срок страховки:\n\n" + "\n".join(lines)


def manual_date_prompt(example: date) -> str:
    return f"Введите дату начала страховки в формате ДД.ММ.ГГГГ, например {format_date(example)}."


def invalid_date(example: date) -> str:
    return f"Не удалось распознать дату. Введите её в формате ДД.ММ.ГГГГ, например {format_date(example)}."


def date_rejected(error: str, example: date) -> str:
    return f"{error}.\n\nВведите другую дату в формате ДД.ММ.ГГГГ, например {format_date(example)}."


def selection_summary(*, category_label: str, start_date: date, period_label: str, price_rub: int) -> str:
    return (
        "Вы выбрали:\n\n"
        f"{category_label}\n"
        f"📅 Начало: {format_date(start_date)}\n"
        f"⏱ Срок: {period_label}\n"
        f"💰 Стоимость: {format_rub(price_rub)} ₽\n\n"
        "Как заполнить данные?"
    )


# ---------------------------------------------------------------- phase 4

BTN_KEEP = "➡️ Оставить: {value}"
BTN_SUGGEST = "✅ Из документа: {value}"
BTN_RESTART = "❌ Отменить оформление"
BTN_BACK_ARROW = "← Назад"
BTN_NO_VIN = "У меня нет VIN — указать номер шасси"
BTN_HAVE_VIN = "У меня есть VIN"
BTN_CONFIRM = "✅ Всё верно"
BTN_EDIT_PLATE = "✏️ Изменить госномер"
BTN_EDIT_VIN = "✏️ Изменить VIN"
BTN_EDIT_CHASSIS = "✏️ Изменить шасси"
BTN_EDIT_MANUFACTURER = "✏️ Изменить марку"
BTN_EDIT_MODEL = "✏️ Изменить модель"
BTN_EDIT_MODEL_YEAR = "✏️ Изменить год выпуска"
BTN_MORE_PHOTOS = "📷 Загрузить ещё фото"
BTN_DOCS_DONE = "✅ Все документы загружены ({count})"
BTN_ENTER_MANUALLY = "✍️ Ввести вручную"
BTN_OTHER_MANUFACTURER = "⬅️ Другая марка"
BTN_PREV = "◀️"
BTN_NEXT = "▶️"
BTN_SHARE_PHONE = "📱 Отправить мой номер"
BTN_BACK_TEXT = "⬅️ Назад"
BTN_EDIT_EMAIL = "✏️ Email"
BTN_EDIT_PHONE = "✏️ Телефон"
BTN_RESTART_CONFIRM = "Да, отменить"
BTN_CANCEL = "Нет, продолжить"

ASK_PLATE = "🚗 Госномер автомобиля\n\nВведите госномер так, как он указан в техпаспорте, например AB123CD."
ASK_VIN = "🔢 VIN\n\nВведите VIN — латинские буквы и цифры, обычно 17 символов."
ASK_CHASSIS = "🔢 Номер шасси (рамы)\n\nВведите номер шасси — латинские буквы и цифры."
ASK_MANUFACTURER = (
    "🏭 Марка автомобиля\n\n"
    "Введите название марки, например:\nToyota\nHAVAL\nMercedes\n\n"
    "Популярные марки — для быстрого выбора (это не весь каталог):"
)
ASK_MANUFACTURER_SEARCH = "🏭 Марка автомобиля\n\nВведите название марки, например:\nToyota\nHAVAL\nMercedes"
ASK_MANUFACTURER_RESULTS = "🏭 Марка автомобиля\n\nНайдено в каталоге по запросу «{query}». Выберите марку или введите другой запрос:"
MANUFACTURER_NOT_FOUND = "🏭 Марка «{query}» не найдена в каталоге.\nМожно использовать «Other»."
BTN_FIND_OTHER_MANUFACTURER = "🔎 Найти другую марку"
BTN_SEARCH_AGAIN = "🔎 Искать другую марку"
BTN_USE_OTHER = "Other"
ASK_MODEL = "🚘 Модель {manufacturer}\n\nВыберите модель из списка или введите часть названия для поиска.\nЕсли вашей модели нет — выберите «Other»."
ASK_MODEL_FILTERED = "🚘 Модель {manufacturer}\n\nМодели по запросу «{query}». Выберите модель или введите другой запрос:"
MODEL_NOT_FOUND = "🚘 Модель {manufacturer}\n\nПо запросу «{query}» моделей не найдено. Введите другой запрос или выберите «Other»."
MODELS_UNAVAILABLE = "Не удалось загрузить список моделей. Попробуйте ещё раз чуть позже."
ASK_MODEL_YEAR = "📅 Год выпуска автомобиля\n\nВведите год выпуска, например 2015."
OCR_HINT = "Распознано в документе: «{value}» — в каталоге не найдено, выберите вручную."
CURRENT_VALUE = "Сейчас: {value}"

ASK_DOCUMENTS = (
    "📸 Загрузите документы\n\n"
    "Отправьте фотографии:\n"
    "• обеих сторон техпаспорта;\n"
    "• страницы загранпаспорта с фотографией.\n\n"
    "Можно выбрать все 3 фото сразу и отправить одним сообщением — бот сам распознает данные."
)
DOCUMENTS_LIMIT = "Больше фото не нужно — документы уже загружены."
DOCUMENTS_DUPLICATE = "Это фото уже добавлено."
DOCUMENTS_UNSUPPORTED = "Этот файл не подходит для распознавания. Отправьте фото или изображение JPEG, PNG или WEBP (не PDF)."
DOCUMENTS_TOO_BIG = "Файл слишком большой. Максимальный размер — 10 МБ."
DOCUMENTS_NONE_YET = "Сначала отправьте хотя бы одно фото документа."
DOCUMENTS_TEXT_HINT = "Отправьте фото документов (можно все сразу, одним сообщением)."
OCR_IN_PROGRESS = "⏳ Распознаю документы… Это может занять до минуты."
OCR_PROGRESS_DONE = "✅ Документы распознаны — проверьте данные ниже."
OCR_PROGRESS_FAILED = "Распознавание не удалось — подробности ниже."
DOCUMENTS_LIMIT_SKIPPED = "Можно не больше {limit} фото за раз — лишние фото пропущены."
OCR_UNAVAILABLE = "Распознавание документов сейчас недоступно. Пожалуйста, введите данные вручную."
OCR_FAILED = "Не удалось распознать документы. Можно попробовать ещё раз, загрузить другие фото или ввести данные вручную."
DOCUMENTS_FAILED_PENDING = "Распознать фото не получилось ({count} шт.). Выберите, что сделать дальше:"
BTN_OCR_RETRY = "🔄 Попробовать распознать ещё раз"
BTN_OTHER_PHOTOS = "📷 Загрузить другие фото"
OCR_NOTHING_FOUND = "На фото не удалось найти данные документа. Попробуйте другое фото (без бликов, целиком в кадре) или введите данные вручную."
OCR_FILE_SKIPPED = "Фото {index} не удалось обработать: {reason}"
OCR_CONFLICTS = "⚠️ На новых фото распознано иначе: {fields}. Мы оставили прежние значения — при необходимости исправьте их кнопками ниже."

VEHICLE_REVIEW_TITLE = "Проверьте данные автомобиля"
NOT_RECOGNIZED = "не распознано"
NOT_ENTERED = "не указано"
CHASSIS_NOT_NEEDED = "не требуется (указан VIN)"
VIN_NOT_NEEDED = "не указан (указан номер шасси)"
CATALOG_MATCHED = "✅ из каталога"
CATALOG_NOT_MATCHED = "❗ не найдено в каталоге"
VEHICLE_INCOMPLETE = "Не хватает данных: {fields}. Исправьте их кнопками ниже или загрузите ещё фото."
VEHICLE_FIELD_LABELS = {
    "registration_number": "госномер",
    "identifier": "VIN или номер шасси",
    "manufacturer": "марка",
    "model": "модель",
    "engine_power": "мощность",
    "model_year": "год выпуска",
}

ASK_FULL_NAME = "👤 ФИО страхователя\n\nВведите фамилию и имя латиницей — так, как в загранпаспорте, например IVANOV IVAN."
ASK_PASSPORT = "🛂 Номер паспорта\n\nВведите номер загранпаспорта (или паспорта) страхователя."
ASK_CITIZENSHIP = "🌍 Гражданство\n\nВыберите из списка или напишите страну (например Russia):"
CITIZENSHIP_NOT_FOUND = "Не удалось определить страну. Напишите её название по-английски (например Russia, Kazakhstan) или выберите из списка."
ASK_DATE_OF_BIRTH = "🎂 Дата рождения страхователя\n\nВведите дату рождения в формате ДД.ММ.ГГГГ, например 05.03.1990."
ASK_EMAIL = "✉️ Email\n\nВведите email — на него придёт полис."
ASK_PHONE = "📱 Телефон\n\nНажмите «📱 Отправить мой номер» или введите номер вручную в международном формате, например +79001234567."
PHONE_NOT_OWN = "Пожалуйста, отправьте свой собственный номер кнопкой «📱 Отправить мой номер» или введите его вручную."
PHONE_SAVED = "✅ Телефон сохранён."

PRICE_UPDATED = "ℹ️ Стоимость обновилась — ниже актуальная цена."
RESTART_CONFIRM = (
    "❌ Отменить текущее оформление?\n\n"
    "Введённые данные и загруженные фото будут удалены. Уже созданные заказы это не затронет."
)
STEP_INVALID = "{error}\n\nПопробуйте ещё раз."

# Most common citizenships of the bot's audience, shown as buttons: (Russian
# label, canonical app.countries.COUNTRIES entry). Display/input convenience
# only -- the stored value is always the canonical English name.
COMMON_CITIZENSHIPS = [
    ("🇷🇺 Россия", "Russia"),
    ("🇧🇾 Беларусь", "Belarus"),
    ("🇰🇿 Казахстан", "Kazakhstan"),
    ("🇦🇲 Армения", "Armenia"),
    ("🇬🇪 Грузия", "Georgia"),
    ("🇦🇿 Азербайджан", "Azerbaijan"),
    ("🇺🇿 Узбекистан", "Uzbekistan"),
    ("🇰🇬 Кыргызстан", "Kyrgyzstan"),
    ("🇹🇯 Таджикистан", "Tajikistan"),
    ("🇺🇦 Украина", "Ukraine"),
]


def citizenship_label(country: str | None) -> str | None:
    for label, canonical in COMMON_CITIZENSHIPS:
        if canonical == country:
            return label.split(" ", 1)[1]
    return country


def russian_citizenship_to_canonical(text: str) -> str | None:
    wanted = (text or "").strip().casefold()
    for label, canonical in COMMON_CITIZENSHIPS:
        if label.split(" ", 1)[1].casefold() == wanted:
            return canonical
    return None


def short(value, limit: int = 32) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def missing_fields_text(fields: list[str]) -> str:
    seen = []
    for field in fields:
        label = VEHICLE_FIELD_LABELS.get(field, field)
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


# ---------------------------------------------------------------- phase 5

BTN_SEND_RECEIPT = "📎 Отправить чек"
BTN_SEND_ANOTHER_RECEIPT = "📎 Отправить ещё один чек"
BTN_SHOW_DETAILS = "🔄 Показать реквизиты ещё раз"
BTN_NEW_PURCHASE = "➕ Оформить ещё одну страховку"
BTN_RESEND_POLICY = "📄 Прислать полис ещё раз"
BTN_MGR_CONFIRM = "✅ Оплата поступила"
BTN_MGR_REJECT = "❌ Оплата не поступила"
BTN_MGR_SEND_POLICY = "📎 Загрузить полис вручную"
# The primary action on a paid customer order while the automatic tpl.ge
# issuance is off (TELEGRAM_TPL_AUTO_ISSUANCE) -- the same upload action.
BTN_MGR_UPLOAD_POLICY = "📄 Загрузить готовый полис"
BTN_MGR_RESEND_POLICY = "🔁 Отправить полис повторно"
BTN_MGR_CANCEL_UPLOAD = "✖️ Отмена"
BTN_MGR_CLIENT = "💬 Клиент"

PAYMENT_TITLE = "💳 Оплата заказа {number}"
PAYMENT_AFTER = "После оплаты отправьте сюда скриншот или фото чека (можно PDF)."
PAYMENT_UNAVAILABLE = (
    "⏳ Заказ {number} создан.\n\n"
    "Оплата временно недоступна — менеджер свяжется с вами в этом чате."
)
RECEIPT_PROMPT = "📎 Отправьте сюда чек об оплате: фото, скриншот или PDF-файл."
RECEIPT_RECEIVED = (
    "🧾 Чек по заказу {number} получен.\n\n"
    "Менеджер проверит оплату и сообщит результат здесь. Если нужно, можно отправить ещё один чек."
)
RECEIPT_EXTRA_RECEIVED = "🧾 Дополнительный чек по заказу {number} получен и передан менеджеру."
RECEIPT_DUPLICATE = "Этот чек уже получен — повторно отправлять не нужно."
RECEIPT_UNSUPPORTED = "Этот файл не подходит. Отправьте чек фотографией, картинкой (JPEG, PNG, WEBP) или PDF-файлом."
RECEIPT_TOO_BIG = "Файл слишком большой. Отправьте чек размером до 20 МБ."
RECEIPT_NOT_NEEDED = "Оплата по заказу {number} уже подтверждена — новый чек не нужен."
STATUS_PAID = "✅ Оплата по заказу {number} подтверждена.\n\nПолис оформляется — мы пришлём его сюда."
STATUS_POLICY_READY = "🎉 Полис по заказу {number} готов и отправлен в этот чат."
STATUS_CANCELLED = "Заказ {number} отменён."
PAYMENT_CONFIRMED = (
    "✅ Оплата подтверждена\n\n"
    "Заказ {number} передан в оформление.\n"
    "Мы пришлём готовый полис сюда."
)
PAYMENT_REJECTED = (
    "❌ Платёж пока не найден\n\n"
    "Проверьте реквизиты и отправьте чек ещё раз.\n"
    "Если вы уверены, что перевод прошёл, отправьте новый/полный чек."
)
POLICY_CAPTION = (
    "🎉 Страховка готова\n\n"
    "Полис по заказу {number} приложен к сообщению.\n"
    "Сохраните файл на время поездки."
)
NEW_PURCHASE_NOT_ALLOWED = "Сначала завершите оплату текущего заказа."
ORDER_CREATE_FAILED = "Не удалось оформить заказ — проверьте данные и попробуйте ещё раз."

MGR_TITLE_REVIEW = "🆕 Новая заявка {number}"
MGR_TITLE = "Заявка {number}"
MGR_STATUS = {
    "payment_review": "⏳ Ожидает проверки оплаты",
    "awaiting_payment": "❌ Оплата не найдена — ждём новый чек",
    "paid": "✅ Оплата подтверждена — оформите полис",
    "processing": "📄 Полис загружен — доставка клиенту",
    "policy_ready": "📄 Полис отправлен",
    "completed": "📄 Полис отправлен",
    "cancelled": "Отменён",
}
MGR_NO_ACCESS = "Нет доступа"
MGR_ALREADY_DONE = "Уже обработано"
MGR_ORDER_NOT_FOUND = "Заказ не найден"
MGR_CONFIRMED = "Оплата подтверждена"
MGR_REJECTED = "Отмечено: оплата не поступила"
MGR_UPLOAD_PROMPT = "📄 Отправьте PDF полиса по заказу {number} одним файлом (в течение {minutes} минут)."
MGR_UPLOAD_NEED_PDF = "Нужен PDF-файл полиса. Отправьте документ .pdf или нажмите «✖️ Отмена»."
MGR_UPLOAD_TOO_BIG = "Файл слишком большой (максимум 20 МБ)."
MGR_UPLOAD_EXPIRED = "Время ожидания PDF истекло. Нажмите «📄 Отправить полис» на карточке заказа ещё раз."
MGR_UPLOAD_NOT_ALLOWED = "Заказ {number} сейчас в статусе «{status}» — отправить полис нельзя."
MGR_UPLOAD_CANCELLED = "Отправка полиса отменена."
MGR_TPL_AUTO_DISABLED = "Автоматическое оформление в tpl.ge отключено — загрузите готовый полис вручную."
MGR_POLICY_SENT = "✅ Полис по заказу {number} отправлен клиенту."
MGR_POLICY_SEND_FAILED = (
    "⚠️ Не удалось отправить полис по заказу {number} клиенту. Полис сохранён — "
    "нажмите «🔁 Отправить полис повторно» позже."
)
MGR_FILE_CAPTIONS = {
    "vehicle_document": "🚗 Документ ТС · {number}",
    "payment_receipt": "🧾 Чек об оплате · {number}\nОжидается: {amount} ₽ · {name}",
    "extra_receipt": "🧾 Дополнительный чек · {number}\nОжидается: {amount} ₽ · {name}",
}
# Manager card: what to look for in the bank before "✅ Оплата поступила".
MGR_PAYMENT_HEADER = "Оплата (проверьте поступление в банке):"


# ------------------------------------------------ consolidated OCR review

CHECKOUT_REVIEW_TITLE = "✅ Документы распознаны\n\nПроверьте данные:"
CHECKOUT_REVIEW_MISSING = "Не хватает: {fields}. Исправьте кнопками ниже."
CATALOG_FALLBACK_NOTE = "ℹ️ {name} отсутствует в каталоге.\nДля оформления будет использовано «Other»."
BTN_R_PLATE = "✏️ Госномер"
BTN_R_VIN = "✏️ VIN"
BTN_R_MANUFACTURER = "✏️ Марка"
BTN_R_MODEL = "✏️ Модель"
BTN_R_MODEL_YEAR = "✏️ Год выпуска"
BTN_R_FULL_NAME = "✏️ ФИО"
BTN_R_PASSPORT = "✏️ Паспорт"
BTN_R_CITIZENSHIP = "✏️ Гражданство"
BTN_R_DATE_OF_BIRTH = "✏️ Дата рождения"
BTN_R_START = "✏️ Дата начала"
BTN_R_PERIOD = "✏️ Период"
BTN_REUPLOAD = "📷 Загрузить документы заново"
DOCUMENTS_COUNTER = "Фото получено: {count} из {required}"
DOCUMENTS_COUNTER_ONLY = "Фото получено: {count}"
OCR_IN_PROGRESS_WITH_COUNT = "{counter}\n\n⏳ Распознаю документы… Это может занять до минуты."
DOCUMENTS_REMAINING = "Отправьте оставшиеся фото — всего нужно {required}."
BTN_PROCESS_PENDING = "🔍 Распознать загруженные фото"
POLICY_FIELD_LABELS = {
    "full_name": "ФИО",
    "identification_number": "паспорт",
    "citizenship": "гражданство",
    "contact_email": "email",
    "contact_phone": "телефон",
}


# ------------------------------------------------ /start, cancel, fresh start

CHECKOUT_CANCELLED = "Оформление отменено."
INTRO_IN_PROGRESS = "У вас есть незавершённое оформление."
INTRO_UNFINISHED_ORDER = "У вас есть незавершённый заказ {number} — он сохранён, к нему можно вернуться."
BTN_NEW_INSURANCE = "🚗 Новая страховка"
BTN_CONTINUE_CHECKOUT = "↩️ Продолжить оформление"
BTN_BACK_TO_ORDER = "↩️ Вернуться к заказу"
STALE_BUTTON = "Эта кнопка относится к отменённому оформлению."
OCR_CANCELLED = "Распознавание остановлено — оформление начато заново."


# ------------------------------------------------ staff: manager / owner

BTN_STAFF_ORDERS = "📋 Заказы"
BTN_STAFF_PENDING = "⏳ Ожидают оплаты"
BTN_STAFF_MANAGERS = "👥 Менеджеры"
BTN_STAFF_ADD = "➕ Добавить менеджера"
BTN_STAFF_REMOVE = "➖ Удалить менеджера"
BTN_STAFF_REMOVE_CONFIRM = "Да, удалить"
BTN_STAFF_RECEIPT = "🧾 Показать чек"
BTN_STAFF_BACK = "⬅️ Назад"
BTN_STAFF_TO_PENDING = "⬅️ К ожидающим оплаты"
BTN_STAFF_TO_ORDERS = "⬅️ К заказам"
BTN_STAFF_TO_MANAGERS = "⬅️ К менеджерам"
BTN_PAGE_PREV = "◀️"
BTN_PAGE_NEXT = "▶️"
STAFF_MENU = "🛠 Меню менеджера\n\nВыберите раздел:"
STAFF_ORDERS_TITLE = "📋 Заказы — последние (стр. {page})"
STAFF_ORDERS_EMPTY = "📋 Заказов пока нет."
STAFF_PENDING_TITLE = "⏳ Ожидают проверки оплаты: {count}"
STAFF_PENDING_EMPTY = "⏳ Нет заказов, ожидающих проверки оплаты."
STAFF_PAGE_HINT = "Выберите заказ, чтобы открыть карточку."
# Short status labels for the order lists (the card shows MGR_STATUS).
STAFF_STATUS_SHORT = {
    "draft": "📝 Черновик",
    "data_completed": "📝 Создан",
    "awaiting_payment": "💳 Ждём оплату",
    "payment_review": "⏳ Проверка оплаты",
    "paid": "✅ Оплачено",
    "processing": "📄 Оформление полиса",
    "policy_ready": "🎉 Полис отправлен",
    "completed": "🎉 Полис отправлен",
    "cancelled": "✖️ Отменён",
}
STAFF_REJECTED_SHORT = "❌ Оплата отклонена"
MGR_RECEIPTS_NONE = "Чек: не получен"
MGR_RECEIPTS_SOME = "Чек: получен ({count})"
STAFF_NO_RECEIPT = "По этому заказу чека нет."
STAFF_MANAGERS_TITLE = "👥 Менеджеры"
STAFF_OWNER_MARK = "👑"
STAFF_MANAGER_MARK = "👤"
STAFF_INVITE = (
    "➕ Приглашение менеджера\n\n"
    "Отправьте эту ссылку будущему менеджеру:\n{link}\n\n"
    "Ссылка одноразовая и действует 24 часа (до {expires} UTC). "
    "Менеджером станет аккаунт Telegram, который её откроет."
)
STAFF_INVITE_NO_USERNAME = "Не удалось определить имя бота для ссылки. Попробуйте позже."
STAFF_REMOVE_PICK = "➖ Кого удалить из менеджеров?"
STAFF_REMOVE_NONE = "Менеджеров для удаления нет (владельца удалить нельзя)."
STAFF_REMOVE_ASK = "Удалить {label} из менеджеров?\nДоступ к функциям менеджера пропадёт сразу."
STAFF_REMOVED = "✅ {label} больше не менеджер."
STAFF_REMOVE_FAILED = "Этого менеджера удалить нельзя."
STAFF_OWNER_ONLY = "Только для владельца"
STAFF_INVITE_ADDED = "✅ Вы добавлены менеджером бота. Функции менеджера — в меню ниже."
STAFF_INVITE_ALREADY = "Вы уже в команде бота."
STAFF_INVITE_INVALID = "⚠️ Ссылка-приглашение недействительна или устарела. Попросите владельца прислать новую."
STAFF_NEW_MANAGER_NOTICE = "✅ Новый менеджер: {label}"


# ------------------------------------------------- staff: prices (💰 Цены)
# Global retail price management -- owner AND manager both have access (see
# app.telegram_bot.staff_prices); every screen/action is re-checked against
# telegram_bot_staff, exactly like every other staff screen.

BTN_STAFF_PRICES = "💰 Цены"
PRICES_TITLE = "💰 Цены ОСАГО Грузия"
# Per-country title for a bot NOT profiled for GE -- GE itself keeps the
# exact literal PRICES_TITLE above, untouched (see prices_title() below).
# "ОСАГО Турции" matches the naming already used for TR on the web side
# (app/web/templates/summary.html's own country-title map).
_PRICES_TITLE_BY_COUNTRY = {
    "TR": "💰 Цены ОСАГО Турции",
    "AM": "💰 Цены ОСАГО Армении",
}
BTN_PRICES_CANCEL_INPUT = "❌ Отмена"
BTN_PRICES_CONFIRM = "✅ Изменить"
BTN_PRICES_RESET = "↩️ Вернуть базовую цену"
BTN_PRICES_RESET_CONFIRM = "✅ Вернуть базовую"
PRICES_NO_CATEGORIES = "Пока нет ни одной категории с редактируемыми тарифами."
PRICES_NO_PERIODS = "У этой категории нет редактируемых периодов."
PRICES_CATEGORY_UNKNOWN = "Эта категория недоступна. Выберите другую."
PRICES_INPUT_CANCELLED = "Изменение цены отменено."
PRICES_INPUT_EXPIRED = "⏱ Время на ввод цены истекло — начните заново через «💰 Цены»."
PRICES_STALE = "⚠️ Цена уже была изменена другим менеджером.\nОбновите список цен и попробуйте ещё раз."


def prices_title(country_code: str) -> str:
    """GE (and anything else unlisted) keeps the exact original PRICES_TITLE
    literal -- never altered by this function."""
    return _PRICES_TITLE_BY_COUNTRY.get(country_code, PRICES_TITLE)


def prices_matrix_text(sections: list[tuple[str, list[str]]], *, title: str = PRICES_TITLE) -> str:
    """sections: [(category_label, [price_line, ...]), ...], in display order."""
    blocks = [title]
    for category_label, lines in sections:
        blocks.append("")
        blocks.append(category_label)
        blocks.extend(lines)
    return "\n".join(blocks)


def prices_current_price_prompt(price_rub: int | None) -> str:
    current = f"{format_rub(price_rub)} ₽" if price_rub is not None else "не задана"
    return f"Текущая цена:\n{current}\n\nВведите новую цену в рублях."


def prices_confirm_prompt(*, category_label: str, period_label: str, old_rub: int | None, new_rub: int) -> str:
    old = f"{format_rub(old_rub)} ₽" if old_rub is not None else "не задана"
    return (
        f"Изменить глобальную цену?\n\n"
        f"{category_label}\n{period_label}\n\n"
        f"Было: {old}\n"
        f"Будет: {format_rub(new_rub)} ₽"
    )


def prices_changed(*, category_label: str, period_label: str, old_rub: int | None, new_rub: int) -> str:
    old = f"{format_rub(old_rub)} ₽" if old_rub is not None else "не задана"
    return f"✅ Цена изменена\n\n{category_label} · {period_label}\n{old} → {format_rub(new_rub)} ₽"


def prices_reset_confirm(*, category_label: str, period_label: str, current_rub: int | None, default_rub: int | None) -> str:
    current = f"{format_rub(current_rub)} ₽" if current_rub is not None else "не задана"
    default = f"{format_rub(default_rub)} ₽" if default_rub is not None else "не задана"
    return (
        f"Вернуть базовую цену?\n\n{category_label}\n{period_label}\n\n"
        f"Сейчас (изменено вручную): {current}\n"
        f"Базовая (из конфигурации): {default}"
    )


def prices_reset_done(*, category_label: str, period_label: str, default_rub: int | None) -> str:
    default = f"{format_rub(default_rub)} ₽" if default_rub is not None else "не задана"
    return f"✅ Цена возвращена к базовой\n\n{category_label} · {period_label}\n{default}"


# ------------------------------------------ operator issuance (staff only)

BTN_OP_ISSUE = "🛡 Оформить страховку"
BTN_OP_PAYMENT_ORDER = "💳 Создать заказ с оплатой"
BTN_OP_ISSUE_GO = "✅ Оформить полис"
BTN_OP_BACK = "⬅️ Назад"
BTN_OP_PAY_TPL = "💳 Оплатить в tpl.ge"
BTN_OP_TPL_PAID = "✅ Оплата TPL завершена"
BTN_OP_NEW_LINK = "🔄 Новая ссылка на оплату"
BTN_OP_GET_POLICY = "🔄 Получить полис"
BTN_OP_RETRY = "🔁 Повторить оформление"
BTN_OP_CHECK = "🔄 Проверить статус"
BTN_OP_FIX = "✏️ Исправить данные"
BTN_OP_RESEND = "📄 Прислать PDF ещё раз"
BTN_OP_STATUS = "🛡 Оформление полиса"
OP_CONFIRM_TITLE = "🛡 Оформить полис?"
OP_CONFIRM_FOOTER = (
    "Заявка будет сразу отправлена в tpl.ge. Оплата клиентом через бота не запрашивается — "
    "оплату с клиентом вы решаете сами."
)
OP_CHANGED = "ℹ️ Данные изменились — проверьте ещё раз."
OP_ORDER_LINE = "Заказ {number} · оформляет менеджер"
OP_BOG_READY = (
    "🛡 Заявка создана в tpl.ge\n\n{summary}\n\n"
    "Стоимость в tpl.ge: {price_gel} GEL\n\n"
    "Оплатите полис картой компании по кнопке ниже (ссылка действует несколько минут; "
    "SMS-код банка вводится вручную). После оплаты нажмите «✅ Оплата TPL завершена»."
)
OP_REPORTED_PAID = "⏳ Оплата отмечена — полис ещё формируется в tpl.ge.\n\n{summary}\n\nПопробуйте получить полис через несколько секунд."
OP_POLICY_DONE = "🎉 Полис {policy} оформлен.\n\n{summary}\n\nPDF отправлен в этот чат — перешлите его клиенту."
OP_POLICY_NOT_SENT = "✅ Полис {policy} оформлен, но PDF отправить не удалось.\n\n{summary}"
OP_REQUESTED = (
    "⏳ Заявка отправлена в tpl.ge, но ответ не получен.\n\n{summary}\n\n"
    "Повторно она автоматически НЕ отправляется. Проверьте статус кнопкой ниже "
    "(не раньше чем через пару минут) или вручную на tpl.ge."
)
OP_FIX_HINT = "исправьте данные и оформите снова."
ISSUE_FIX_HINT_CUSTOMER = "проверьте данные заказа; при необходимости загрузите полис вручную."
OP_FAILED_VALIDATION = "❌ Полис не оформлен — данные не подходят для tpl.ge.\n\n{summary}\n\nПричина: {error}\n\nВ tpl.ge ничего не создано: исправьте данные и оформите снова."
OP_FAILED_REJECTED = "❌ tpl.ge отклонил заявку.\n\n{summary}\n\nПричина: {error}\n\nВ tpl.ge ничего не создано: можно исправить данные или повторить."
OP_FAILED_TEMPORARY = "⚠️ Временная ошибка связи с tpl.ge.\n\n{summary}\n\nПричина: {error}\n\nМожно повторить — повторная попытка не создаст второй полис."
OP_IN_PROGRESS = "⏳ Заявка по этому заказу отправляется прямо сейчас — подождите минуту и проверьте статус."
OP_CANCELLED = "✖️ Заказ {number} отменён (оформление заменено исправленным)."
OP_POLICY_CAPTION = "🛡 Полис {policy} · заказ {number}\nСтрахователь: {name}"
OP_CARD_ORIGIN = "🛡 Оформлен менеджером {label}: оплата клиентом через бота не собиралась, чек не нужен"
OP_SHORT_STATUS = {
    "data_completed": "🛡 Оформление",
    "paid": "🛡 Оформление",
    "processing": "🛡 Оформление",
    "policy_ready": "🛡 Полис выдан",
    "completed": "🛡 Полис выдан",
    "cancelled": "✖️ Отменён",
}


# ------------------------------------- tpl.ge issuance of a customer order

BTN_TPL_START = "🛡 Оформить в tpl.ge"
BTN_TO_ORDER = "⬅️ К заказу"
ISSUE_ORDER_LINE_CUSTOMER = "Заказ {number} · оплачен клиентом"
ISSUE_CUSTOMER_PAID = (
    "✅ Оплата клиента подтверждена\n\n{summary}\n\n"
    "Оформите полис в tpl.ge: заявка уйдёт с данными этого заказа, затем оплата картой компании."
)
ISSUE_NOT_PAID = "Оплата клиента по этому заказу ещё не подтверждена.\n\n{summary}"
ISSUE_NOT_CONFIGURED = (
    "⚠️ Автоматическое оформление в tpl.ge не настроено (нет TPL_GE_STATIC_VISITOR_ID).\n\n{summary}\n\n"
    "Оплата клиента подтверждена и сохранена; в tpl.ge ничего не отправлялось. "
    "После настройки нажмите «🔁 Повторить оформление» — или загрузите полис вручную."
)
ISSUE_POLICY_DONE_CUSTOMER = "🎉 Полис {policy} оформлен и отправлен клиенту.\n\n{summary}"
