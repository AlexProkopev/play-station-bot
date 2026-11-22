import html
import re
import unicodedata
from contextvars import ContextVar

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from app.config import SHOP_NAME, MINIAPP_URL
from app.services.catalog import CatalogService

BTN_BACK = "⬅️ К списку"
BTN_CANCEL = "↩️ На главную"
BTN_DISMISS = "⏹ Закрыть окно"
BTN_OTHER_CITY = "🌍 Другой город доставки"
BTN_CONTACTS = "📡 Связь со Store"
BTN_CHEST = "🏆 Ежедневный бонус"
BTN_ROULETTE = "🎲 Розыгрыш призов"
BTN_COUPON = "🎟 Ввести промокод"
BTN_ALL_CITIES_REVIEWS = "⭐ Оценки покупателей"
BTN_ORDER_BACK = "💳 Снова к способам оплаты"
BTN_PROFILE = "👤 Мой аккаунт"
BTN_SUPPORT = "📜 Условия покупки"
BTN_RULES = "📜 Условия покупки"
BTN_SUPPORT_CHAT = "💬 Написать в поддержку"
BTN_DISPUTE = "🛠 Проблема с заказом"
BTN_MISSIONS = "🏆 Недельные цели"
BTN_GUIDE = "📘 Гайд по покупке"
BTN_CLOSE_CHAT = "⏹ Завершить диалог"
BTN_BUY = "🛒 Купить"
BTN_PRICE = "🏷 Цены Store"
BTN_MINIAPP = "📱 Приложение Store"
BTN_BALANCE = "💳 Кошелек"
BTN_PURCHASES = "📚 История заказов"
BTN_SITE = "🌐 Официальная страница"

BTN_CANCEL_LEGACY = "❌ Отмена"
BTN_BUY_LEGACY = "🛍 Магазин"
BTN_BUY_OLD = "🛒 Начать покупку"
BTN_PROFILE_LEGACY = "👤 Профиль"
BTN_BALANCE_LEGACY = "💰 Баланс"
BTN_PURCHASES_LEGACY = "📦 Покупки"

CANCEL_TEXT_ALIASES = {
    BTN_CANCEL,
    BTN_DISMISS,
    BTN_CANCEL_LEGACY,
    "МЕНЮ",
    "Меню",
    "главная",
    "На главную",
    "↩️ На главную",
    "🔵 На главную",
    "/cancel",
    "/отмена",
    "отмена",
    "Отмена",
    "⏹ Закрыть окно",
    "✖️ Закрыть окно",
}

HOME_MENU_TEXT = (
    f"🔵 <b>{SHOP_NAME}</b>\n"
    "Магазин открыт. Выберите раздел.\n\n"
    "🎮 «Открыть каталог» - указать город и посмотреть товары.\n"
    "💳 «Кошелек» показывает деньги на счете.\n"
    "📚 «История заказов» показывает, сколько покупок уже оплачено.\n"
    "↩️ «На главную» возвращает на этот экран.\n\n"
    "Кнопки под сообщением: промокод, бонус, оценки и поддержка."
)
HOME_ACTIONS_TEXT = "Разделы PlayStation™Store:"
CITY_INPUT_TEXT = (
    f"🌍 <b>{SHOP_NAME}</b>\n"
    "Трудимся на благо общества\n\n"
    "Пришлите название города отдельным сообщением.\n"
    "Например: <code>Казань</code>\n\n"
    "Кнопка «На главную» закрывает этот шаг."
)


def format_price_label(value) -> str:
    try:
        amount = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value or "-")
    return f"{amount:,}".replace(",", " ")


def reviews_button_text(city_name: str = "", *, region_name: str | None = None, fallback: str = "Отзывы") -> str:
    from app.services.reviews import ReviewsService

    city = str(city_name or "").strip()
    region = str(region_name or "").strip()
    stats = ReviewsService.get_rating_stats(city_filter=city or None, region_filter=region or None)
    count = int(stats.get("count") or 0)
    avg = float(stats.get("average") or 0)
    if city:
        label_city = city
        if region and len(ReviewsService._homonym_regions(city)) > 1:
            label_city = ReviewsService.format_city_region_label(city, region)
        label = f"⭐ Оценки · {label_city}"
    else:
        label = "⭐ Оценки покупателей" if fallback == "Отзывы" else fallback
    if count <= 0:
        return _short_button_text(label, 64)
    avg_text = f"{avg:.1f}".replace(".", ",")
    return _short_button_text(f"{label} · {avg_text} из 5", 64)


_MENU_USER: ContextVar[int | None] = ContextVar("valley_menu_user", default=None)


def remember_menu_user(user_id: int | None) -> None:
    if user_id:
        _MENU_USER.set(int(user_id))


def _menu_counts(user_id: int | None) -> tuple[str, str]:
    balance = "0"
    purchases = "0"
    user_id = user_id or _MENU_USER.get()
    if not user_id:
        return balance, purchases
    try:
        from app.services.users import UserService

        user = UserService.get_user(user_id) or {}
        balance = format_price_label(UserService.get_balance(user_id))
        purchases = str(int(user.get("purchases_count") or 0))
    except Exception:
        return "0", "0"
    return balance, purchases


def get_base_keyboard(user_id: int | None = None):
    """Нижняя панель: магазин, баланс, покупки, выход. Вторая половина меню."""
    balance, purchases = _menu_counts(user_id)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY)],
            [
                KeyboardButton(text=f"{BTN_BALANCE} · {balance} ₽"),
                KeyboardButton(text=f"{BTN_PURCHASES} · {purchases}"),
            ],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Город доставки или раздел Store",
    )


def get_main_menu_keyboard():
    """Экран долины: магазин, затем ссылки из админки, затем разделы бота."""
    from app.services.settings import SettingsService

    rows = [[InlineKeyboardButton(text=BTN_BUY, callback_data="menu_buy")]]
    for link in SettingsService.get_home_links():
        title = _short_button_text(link["title"], 60)
        rows.append([InlineKeyboardButton(text=title, url=link["url"])])
    if MINIAPP_URL:
        rows.append([InlineKeyboardButton(text=BTN_MINIAPP, web_app=WebAppInfo(url=MINIAPP_URL))])
    rows.extend([
        [
            InlineKeyboardButton(text=BTN_COUPON, callback_data="menu_coupon"),
            InlineKeyboardButton(text=BTN_CHEST, callback_data="menu_chest"),
        ],
        [
            InlineKeyboardButton(text=BTN_PROFILE, callback_data="menu_profile"),
            InlineKeyboardButton(text=reviews_button_text(), callback_data="menu_reviews"),
        ],
    ])
    if SettingsService.is_price_menu_visible():
        rows.append([InlineKeyboardButton(text=BTN_PRICE, callback_data="menu_price")])
    rows.extend([
        [
            InlineKeyboardButton(text=BTN_RULES, callback_data="menu_rules"),
            InlineKeyboardButton(text=BTN_DISPUTE, callback_data="menu_dispute"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_city_input_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")]]
    )


def get_price_list_keyboard(items: list | None = None):
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        title = _short_button_text(item.get("title") or "Товар", 64)
        rows.append([InlineKeyboardButton(text=title, callback_data=f"price_item_{item_id}")])
    rows.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_price_item_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад к прайсу", callback_data="menu_price")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")],
        ]
    )


def get_all_cities_reviews_pagination_kb(current_page: int, total_pages: int):
    safe_total = max(1, int(total_pages or 1))
    safe_page = max(0, min(int(current_page or 0), safe_total - 1))

    buttons = []
    nav_row = []

    if safe_page > 0:
        nav_row.append(InlineKeyboardButton(text="⏮ Прошлая страница", callback_data=f"rev_all_nav_{safe_page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))

    nav_row.append(InlineKeyboardButton(text=f"{safe_page + 1}/{safe_total}", callback_data="ignore"))

    if safe_page < safe_total - 1:
        nav_row.append(InlineKeyboardButton(text="⏭ Следующая страница", callback_data=f"rev_all_nav_{safe_page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))

    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_profile_keyboard():
    return get_base_keyboard()

def get_support_chat_keyboard():
    return get_base_keyboard()


def get_rules_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 НЕНАХОД", callback_data="dispute_start")],
            [InlineKeyboardButton(text=BTN_SUPPORT_CHAT, callback_data="support_write")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel_delete")],
        ]
    )


def get_dispute_need_purchase_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_SUPPORT_CHAT, callback_data="support_write")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel_delete")],
        ]
    )


def get_dispute_flow_keyboard():
    return get_base_keyboard()


def get_dispute_kind_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚖️ Вес меньше заявленного", callback_data="dispute_kind_underweight")],
            [InlineKeyboardButton(text="🧭 Точку не нахожу", callback_data="dispute_kind_lost")],
            [InlineKeyboardButton(text="📦 На точке ничего нет", callback_data="dispute_kind_empty")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="dispute_cancel")],
        ]
    )


def get_dispute_photo_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Приложить фото", callback_data="dispute_provide_photo")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="dispute_cancel")],
        ]
    )


def get_dispute_location_intro_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Заполнить анкету Store", callback_data="dispute_location_form")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="dispute_cancel")],
        ]
    )


def get_dispute_underweight_ready_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Отправить диспут как есть", callback_data="dispute_send")],
            [InlineKeyboardButton(text="✍️ Добавить сообщение куратору", callback_data="dispute_add_comment")],
            [InlineKeyboardButton(text="🚪 Отправить обращение", callback_data="dispute_send")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="dispute_cancel")],
        ]
    )


def get_dispute_preview_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и отправить", callback_data="dispute_send")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="dispute_cancel")],
        ]
    )


def get_admin_dispute_keyboard(user_id: int):
    from app.services.settings import SettingsService

    rows = []
    current = []
    for index, item in enumerate(SettingsService.get_dispute_replies()):
        label = str(item.get("label") or "").strip() or f"{item.get('percent')}%"
        current.append(
            InlineKeyboardButton(
                text=label[:32],
                callback_data=f"adtp_{user_id}_{index}",
            )
        )
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([
        InlineKeyboardButton(text="⚖️ Вынести решение", callback_data=f"admin_dispute_verdict_{user_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _short_button_text(text: str, limit: int = 32) -> str:
    value = str(text or "").strip() or "-"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _fold_symbol(ch: str) -> str | None:
    code = ord(ch)
    if 0xFF21 <= code <= 0xFF3A:
        return chr(ord("A") + code - 0xFF21)
    if 0xFF41 <= code <= 0xFF5A:
        return chr(ord("a") + code - 0xFF41)
    if 0xFF10 <= code <= 0xFF19:
        return chr(ord("0") + code - 0xFF10)
    blocks = (
        (0x1D400, 26, "A"),
        (0x1D41A, 26, "a"),
        (0x1D434, 26, "A"),
        (0x1D44E, 26, "a"),
        (0x1D468, 26, "A"),
        (0x1D482, 26, "a"),
        (0x1D4D0, 26, "A"),
        (0x1D4EA, 26, "a"),
        (0x1D56C, 26, "A"),
        (0x1D586, 26, "a"),
        (0x1D5A0, 26, "A"),
        (0x1D5BA, 26, "a"),
        (0x1D5D4, 26, "A"),
        (0x1D5EE, 26, "a"),
        (0x1D608, 26, "A"),
        (0x1D622, 26, "a"),
        (0x1D63C, 26, "A"),
        (0x1D656, 26, "a"),
        (0x1D670, 26, "A"),
        (0x1D68A, 26, "a"),
        (0x1D7CE, 10, "0"),
        (0x1D7D8, 10, "0"),
        (0x1D7E2, 10, "0"),
        (0x1D7EC, 10, "0"),
        (0x1D7F6, 10, "0"),
    )
    for start, length, base in blocks:
        if start <= code < start + length:
            return chr(ord(base) + code - start)
    return None


def plain_catalog_text(value: str) -> str:
    chars: list[str] = []
    for ch in str(value or ""):
        folded = _fold_symbol(ch)
        if folded is not None:
            chars.append(folded)
            continue
        if ch in {"|", "/", "\\"}:
            chars.append(" ")
            continue
        if unicodedata.category(ch).startswith(("L", "N")) or ch in " -+.,":
            chars.append(ch)
    return " ".join("".join(chars).split()).strip(" -.,")


_CATALOG_WEIGHT_PREFIX_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?\s*(?:г|g|мг|mg|кг|kg))\b",
    re.IGNORECASE,
)
_CATALOG_DECOR_RE = re.compile(r"[★☆✦✧💎⭐]")
CATALOG_TITLE_MESSAGE_MAX = 40
CATALOG_TITLE_BUTTON_MAX = 24
CATALOG_WEIGHT_BUTTON_MAX = 12


def catalog_name_parts(name: str) -> tuple[str, str]:
    raw = str(name or "").strip()
    if "|" in raw:
        left, right = raw.split("|", 1)
        weight = plain_catalog_text(left)
        title = plain_catalog_text(right)
        if weight and title:
            weight_only = _CATALOG_WEIGHT_PREFIX_RE.match(weight)
            if weight_only:
                weight = weight_only.group(1).strip()
                extra = plain_catalog_text(weight_only.string[weight_only.end() :])
                if extra:
                    title = f"{extra} {title}".strip()
            return weight, title
    title = plain_catalog_text(raw) or "позиция"
    match = _CATALOG_WEIGHT_PREFIX_RE.match(title)
    if match:
        weight = match.group(1).strip()
        rest = plain_catalog_text(title[match.end() :]) or title
        return weight, rest
    return "", title


def short_catalog_title(title: str, *, max_len: int = CATALOG_TITLE_MESSAGE_MAX) -> str:
    value = _CATALOG_DECOR_RE.sub("", str(title or ""))
    value = plain_catalog_text(value) or "позиция"
    if len(value) > max_len and "," in value:
        head = value.split(",", 1)[0].strip()
        if len(head) >= max(12, max_len // 2):
            value = head
    return _short_button_text(value, max_len)


def catalog_product_button_label(product: dict) -> str:
    weight, title = catalog_name_parts(str(product.get("name") or ""))
    short_title = short_catalog_title(title, max_len=CATALOG_TITLE_BUTTON_MAX)
    price = format_price_label(product.get("price"))
    parts = [short_title, f"{price} ₽"]
    if weight:
        parts.insert(1, _short_button_text(weight, CATALOG_WEIGHT_BUTTON_MAX))
    return _fit_button_text(" · ".join(parts), 64)


def _iter_catalog_slots(products: list | None):
    for product in products or []:
        if not isinstance(product, dict) or not product.get("id"):
            continue
        if CatalogService.is_courier_job(product):
            yield {"kind": "job", "product": product}
            continue
        yield {"kind": "item", "product": product}


def _fit_message(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_catalog_board(city_name: str, products: list | None, *, kind: str = "Каталог") -> str:
    city = html.escape(str(city_name or "город не выбран"))
    slots = list(_iter_catalog_slots(products))
    cards: list[str] = []
    for slot in slots:
        product = slot["product"]
        weight, title = catalog_name_parts(str(product.get("name") or ""))
        display_title = html.escape(short_catalog_title(title))
        price = html.escape(format_price_label(product.get("price")))
        lines = [f"<b>{display_title}</b>"]
        if weight:
            lines.append(f"⚖️ {html.escape(weight)}")
        lines.append(f"💰 {price} ₽")
        cards.append("<blockquote>" + "\n".join(lines) + "</blockquote>")
    text = "\n".join([
        f"🔵 <b>{html.escape(SHOP_NAME)}</b>",
        f"📍 {city} · {html.escape(kind)} · позиций: {len(slots)}",
        "",
        "Список товаров:",
        "",
        "".join(cards),
        "",
        "Ниже кнопки с <b>названием</b>, <b>весом</b> и <b>ценой</b> — нажмите нужный товар.",
    ])
    return _fit_message(text)


def district_mark(index: int) -> str:
    return f"R{index + 1}"


def format_district_board(city_name: str, districts: list | None) -> str:
    names = [item.strip() for item in (districts or []) if isinstance(item, str) and item.strip()]
    cards: list[str] = []
    lines: list[str] = []
    for index, name in enumerate(names):
        body = f"<b>{district_mark(index)}</b>   {html.escape(name)}"
        cards.append(f"<blockquote>{body}</blockquote>")
        lines.append(body)
    intro = [
        f"📍 <b>Районы · {html.escape(str(city_name or 'город не выбран'))}</b>",
        f"{html.escape(SHOP_NAME)} готовит выдачу только в выбранном районе.",
        "",
    ]
    outro = [
        "",
        "Нажмите код района под сообщением.",
        "R1 - первая строка списка, R2 - вторая.",
    ]
    rich = "\n".join(intro + cards + outro)
    if len(rich) <= 3900:
        return rich
    return _fit_message("\n".join(intro + lines + outro))


def format_stash_board(product_name: str, options: list | None) -> str:
    weight, title = catalog_name_parts(str(product_name or ""))
    caption = title if not weight else f"{weight}   {title}"
    cards: list[str] = []
    for index, option in enumerate(options or []):
        if not isinstance(option, str):
            continue
        cards.append(f"<b>S{index + 1}</b>  {html.escape(option.strip() or 'способ не указан')}")
    return "\n".join([
        "📦 <b>Способ выдачи</b>",
        f"Товар: {html.escape(caption or 'позиция')}",
        "",
        "\n".join(cards),
        "",
        "Выберите S1 или S2 на кнопке. Номер совпадает со строкой выше.",
    ])


def get_districts_keyboard(districts: list):
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    visible_index = 0
    for index, district in enumerate(districts or []):
        if not isinstance(district, str):
            continue
        row.append(
            InlineKeyboardButton(text=district_mark(visible_index), callback_data=f"dist_{index}")
        )
        visible_index += 1
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text=BTN_BACK, callback_data="nav_back_products")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_treasure_types_keyboard(types: list[str]):
    row: list[InlineKeyboardButton] = []
    for index, option in enumerate(types or []):
        if not isinstance(option, str):
            continue
        label = plain_catalog_text(option) or option.strip() or "-"
        row.append(
            InlineKeyboardButton(
                text=_short_button_text(f"S{index + 1} {label}", 40),
                callback_data=f"stash_{index}",
            )
        )
    rows = [row] if row else []
    rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data="nav_back_district_from_type")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _catalog_button_rows(products: list, callback_prefix: str) -> list[list[InlineKeyboardButton]]:
    buttons: list[list[InlineKeyboardButton]] = []
    for slot in _iter_catalog_slots(products):
        product = slot["product"]
        buttons.append([
            InlineKeyboardButton(
                text=catalog_product_button_label(product),
                callback_data=f"{callback_prefix}{product['id']}",
            )
        ])
    return buttons


def get_products_keyboard(
    city_name: str,
    population: int = 0,
    region_name: str | None = None,
    show_miniopt_discount_badge: bool = False,
):
    products = CatalogService.get_products_for_city(city_name, population, region_name=region_name)
    buttons = _catalog_button_rows(products, "prod_")
    buttons.append([
        InlineKeyboardButton(
            text=reviews_button_text(city_name, region_name=region_name),
            callback_data="catalog_all_reviews",
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            text="📦 Крупный заказ",
            callback_data="catalog_preorder_menu",
        )
    ])
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_miniopt_products_keyboard(city_name: str, population: int = 0, region_name: str | None = None):
    products = CatalogService.get_miniopt_products_for_city(city_name, population, region_name=region_name)
    buttons = _catalog_button_rows(products, "mini_prod_")
    buttons.append([InlineKeyboardButton(text="🎮 Обычный каталог", callback_data="catalog_regular")])
    buttons.append([
        InlineKeyboardButton(text=BTN_OTHER_CITY, callback_data="nav_back_city"),
        InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

RATECORE_METHODS_PAGE_SIZE = 8


def get_payment_keyboard():
    return get_ratecore_methods_keyboard([], page=0, back_callback="nav_back_district", show_balance=True)


def get_store_crypto_coin_keyboard(*, back_callback: str = "rc_back_groups") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="₿ Bitcoin (BTC)", callback_data="store_crypto_btc")],
        [InlineKeyboardButton(text="Ł Litecoin (LTC)", callback_data="store_crypto_ltc")],
    ]
    if back_callback:
        rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data=back_callback)])
    rows.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_ratecore_methods_keyboard(
    methods: list[dict],
    page: int = 0,
    *,
    back_callback: str = "nav_back_district",
    crypto_assets: list[dict] | None = None,
    show_balance: bool = False,
    amount: float | int | None = None,
) -> InlineKeyboardMarkup:
    from app.services.ratecore import format_approx_amount_suffix, group_payment_methods

    grouped = group_payment_methods(methods)
    buttons: list[list[InlineKeyboardButton]] = []
    other_group: tuple[int, dict] | None = None
    for index, method in enumerate(grouped):
        key = str(method.get("key") or "").strip()
        if not key:
            continue
        if key == "_group_other":
            other_group = (index, method)
            continue
        children = method.get("submethods") or []
        label = str(method.get("name") or key)
        if children:
            buttons.append([
                InlineKeyboardButton(
                    text=_fit_button_text(_ratecore_group_label(label, len(children)) + format_approx_amount_suffix(amount, method)),
                    callback_data=f"rcg_{index}",
                )
            ])
        else:
            buttons.append([
                InlineKeyboardButton(
                    text=_fit_button_text(_ratecore_method_label(label) + format_approx_amount_suffix(amount, method)),
                    callback_data=f"rcm_{key}",
                )
            ])

    crypto_suffix = ""
    try:
        rounded = int(round(float(amount or 0)))
        if rounded > 0:
            crypto_suffix = f" · {format_price_label(rounded)} ₽"
    except (TypeError, ValueError):
        crypto_suffix = ""
    buttons.append([
        InlineKeyboardButton(
            text=_fit_button_text(f"💎 Оплата криптой (без комиссии) · BTC / LTC{crypto_suffix}", 64),
            callback_data="store_crypto_menu",
        )
    ])

    if show_balance:
        balance_label = "С баланса"
        try:
            balance_amount = int(round(float(amount or 0)))
        except (TypeError, ValueError):
            balance_amount = 0
        if balance_amount > 0:
            balance_label = f"С баланса · {balance_amount} ₽"
        buttons.append([InlineKeyboardButton(text=balance_label, callback_data="pay_balance")])

    if other_group is not None:
        other_index, other_method = other_group
        other_children = other_method.get("submethods") or []
        buttons.append([
            InlineKeyboardButton(
                text=_fit_button_text(
                    _ratecore_group_label("Другие способы", len(other_children))
                    + format_approx_amount_suffix(amount, other_method)
                ),
                callback_data=f"rcg_{other_index}",
            )
        ])

    if back_callback and back_callback != "nav_cancel":
        buttons.append([InlineKeyboardButton(text=BTN_BACK, callback_data=back_callback)])
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _fit_button_text(text: str, limit: int = 64) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def _ratecore_group_label(name: str, count: int) -> str:
    value = str(name or "").strip()
    lowered = value.casefold()
    if lowered == "карта":
        return f"💳 Карта · {count} {_variantov_word(count)}"
    if "выбрать банк" in lowered:
        return f"🏦 {value}"
    if lowered == "сбп":
        return f"💸 СБП · {count} {_variantov_word(count)}"
    if "крипт" in lowered:
        return "💎 Крипта · BTC / LTC" if count >= 2 else "💎 Крипта"
    if "друг" in lowered:
        return "📂 Другие способы"
    suffix = f"({count} {_sposobov_word(count)})"
    if "qr" in lowered:
        return f"📲 {value} {suffix}"
    if "внутри" in lowered or "межбанк" in lowered:
        return f"🏦 {value} {suffix}"
    return f"📂 {value} {suffix}"


def _sposobov_word(count: int) -> str:
    n = abs(int(count)) % 100
    if 11 <= n <= 14:
        return "способов"
    last = n % 10
    if last == 1:
        return "способ"
    if 2 <= last <= 4:
        return "способа"
    return "способов"


def _variantov_word(count: int) -> str:
    n = abs(int(count)) % 100
    if 11 <= n <= 14:
        return "вариантов"
    last = n % 10
    if last == 1:
        return "вариант"
    if 2 <= last <= 4:
        return "варианта"
    return "вариантов"


def _ratecore_method_label(name: str) -> str:
    from app.services.settings import SettingsService

    original = str(name or "").strip()
    value = SettingsService.get_aliased_method_name(original)

    lowered = value.casefold()
    if "сбп" in lowered:
        return f"💸 {value}"
    if "qr" in lowered or "qr-код" in lowered:
        return f"📲 {value}"
    if "телефон" in lowered or "sim" in lowered:
        return f"📱 {value}"
    if "карт" in lowered or "снг" in lowered or "нспк" in lowered:
        return f"💳 {value}"
    if "крип" in lowered or "crypto" in lowered:
        return f"🪙 {value}"
    if "банк" in lowered or "внутри" in lowered:
        return f"🏦 {value}"
    return f"💰 {value}"


def get_ratecore_submethods_keyboard(
    methods: list[dict],
    method_index: int,
    *,
    back_callback: str = "nav_back_district",
    crypto_assets: list[dict] | None = None,
    nest_key: str | None = None,
    amount: float | int | None = None,
) -> InlineKeyboardMarkup:
    from app.services.ratecore import (
        filter_main_crypto_assets,
        find_nested_group,
        format_approx_amount_suffix,
        group_payment_methods,
    )

    buttons: list[list[InlineKeyboardButton]] = []
    back_data = "rc_back_groups"
    if str(method_index) == "crypto":
        children = filter_main_crypto_assets(crypto_assets)
        prefix = "rcc"
        allow_nested = False
    else:
        grouped = group_payment_methods(methods)
        if nest_key:
            parent_index, nest = find_nested_group(grouped, nest_key)
            children = (nest or {}).get("submethods") or []
            prefix = "rcm"
            allow_nested = False
            if parent_index is not None:
                back_data = f"rcg_{parent_index}"
        else:
            try:
                method = grouped[int(method_index)]
            except (IndexError, TypeError, ValueError):
                method = {}
            children = method.get("submethods") or []
            prefix = "rcm"
            allow_nested = True
    for child in children:
        if not isinstance(child, dict):
            continue
        key = str(child.get("key") or "").strip()
        if not key:
            continue
        name = str(child.get("name") or key)
        nested = allow_nested and bool(child.get("submethods"))
        if prefix == "rcc":
            label = f"💎 {name}"
            callback_data = f"rcc_{key}"
        elif nested:
            label = _ratecore_group_label(name, len(child.get("submethods") or []))
            callback_data = f"rcn_{key}"
        else:
            label = _ratecore_method_label(name)
            callback_data = f"{prefix}_{key}"
        buttons.append([
            InlineKeyboardButton(
                text=_fit_button_text(label + format_approx_amount_suffix(amount, child)),
                callback_data=callback_data,
            )
        ])
    buttons.append([InlineKeyboardButton(text=BTN_BACK, callback_data=back_data)])
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_topup_keyboard():
    return get_ratecore_methods_keyboard([], page=0, back_callback="nav_cancel")

def get_chest_keyboard(snapshot: dict):
    buttons = []
    if not snapshot.get("free_used") and not snapshot.get("bonus_capped"):
        buttons.append([InlineKeyboardButton(text="🎰 Крутить рулетку", callback_data="chest_open")])
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_profile_actions_keyboard(invite_url: str | None = None):
    buttons = [
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
        [InlineKeyboardButton(text=BTN_COUPON, callback_data="profile_coupon_activate")],
        [InlineKeyboardButton(text=BTN_CHEST, callback_data="menu_chest")],
    ]
    if invite_url:
        buttons.insert(0, [InlineKeyboardButton(text="👥 Пригласить друга", url=invite_url)])
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_wallet_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="profile_topup")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")],
        ]
    )


def get_missions_keyboard(can_claim: bool = False):
    buttons = []
    if can_claim:
        buttons.append([InlineKeyboardButton(text="💠 Забрать кристалл", callback_data="missions_claim")])
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_exchange_init_keyboard():
    buttons = [
        [InlineKeyboardButton(text="Номер заявки", callback_data="exchange_enter_id")],
        [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_exchange_retry_keyboard():
    buttons = [
        [InlineKeyboardButton(text="Проверить оплату", callback_data="exchange_enter_id")],
        [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def check_payment_keyboard():
    buttons = [
        [InlineKeyboardButton(text="Оплачено", callback_data="check_payment")],
        [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_pending_order_keyboard():
    return get_base_keyboard()


def get_pending_order_warning_keyboard():
    return get_base_keyboard()


def get_pending_order_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[])


def get_admin_topup_request_keyboard(user_id: int, cards: list[str] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, card in enumerate(cards or []):
        label = f"💳 {card}"
        if len(label) > 64:
            label = label[:61] + "..."
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"admin_topup_send_{user_id}_{idx}")]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="🔶 Карты РФ не заданы", callback_data="admin_topup_nocards")]
        )
    rows.append(
        [InlineKeyboardButton(text="✅ Подтвердить пополнение", callback_data=f"admin_confirm_topup_{user_id}")]
    )
    rows.append([InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")])
    rows.append(
        [InlineKeyboardButton(text="❗ Отменить пополнение", callback_data=f"admin_cancel_topup_{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_crypto_order_admin_keyboard(user_id: int, *, topup: bool = False, chest: bool = False):
    if chest:
        confirm = "✅ Подтвердить сундук"
        cancel = "❗ Отменить заявку сундука"
    elif topup:
        confirm = "✅ Подтвердить пополнение"
        cancel = "❗ Отменить пополнение"
    else:
        confirm = "✅ Подтвердить оплату"
        cancel = "❗ Отменить заявку"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=confirm, callback_data=f"admin_confirm_order_{user_id}")],
            [InlineKeyboardButton(text=cancel, callback_data=f"admin_cancel_order_{user_id}")],
            [InlineKeyboardButton(text="💬 Чат с клиентом", callback_data=f"admin_chat_{user_id}")],
        ]
    )


def get_pending_order_warning_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_ORDER_BACK, callback_data="order_back_to_pay")],
            [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")],
        ]
    )


def get_admin_auto_broadcast_list_keyboard(workers: list):
    from app.services.broadcast import media_kind_label

    rows = []
    for index, worker in enumerate(workers or [], 1):
        status = "🟢" if worker.get("enabled") else "⚪️"
        minutes = int(worker.get("interval_minutes") or 60)
        media = worker.get("media") if isinstance(worker.get("media"), dict) else None
        text = (worker.get("text") or "").replace("\n", " ").strip()
        if media and media.get("kind"):
            preview = media_kind_label(str(media.get("kind")))
            if text:
                preview = f"{preview} · {text[:18]}"
        else:
            preview = text[:28] if text else "без текста"
        if len(preview) > 28:
            preview = preview[:27] + "…"
        title = f"{status} {index}. {minutes} мин · {preview}"
        rows.append([
            InlineKeyboardButton(
                text=title[:64],
                callback_data=f"admin_ab_w:{worker.get('id')}",
            )
        ])
    if len(workers or []) < 8:
        rows.append([InlineKeyboardButton(text="➕ Новый воркер", callback_data="admin_ab_new")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_auto_broadcast_keyboard(enabled: bool, worker_id: str = "default"):
    toggle = "⏹ Остановить" if enabled else "▶️ Запустить"
    wid = worker_id or "default"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Текст / фото / файл", callback_data=f"admin_ab_text:{wid}")],
            [InlineKeyboardButton(text="⏱ Интервал", callback_data=f"admin_ab_int:{wid}")],
            [InlineKeyboardButton(text=toggle, callback_data=f"admin_ab_tog:{wid}")],
            [InlineKeyboardButton(text="📤 Отправить сейчас", callback_data=f"admin_ab_now:{wid}")],
            [InlineKeyboardButton(text="🗑 Удалить воркер", callback_data=f"admin_ab_del:{wid}")],
            [InlineKeyboardButton(text="🔙 К списку", callback_data="admin_auto_broadcast")],
        ]
    )


def get_cancel_payment_keyboard():
    """Клавиатура для отмены внутри процесса оплаты"""
    return get_pending_order_inline_keyboard()


def get_rf_check_retry_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🔄 Обновить проверку", callback_data="rf_check_retry")],
        [InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_reviews_pagination_kb(
    current_index: int,
    total_count: int,
    city_filter: str = None,
    display_text: str = None,
    product_id: str | None = None,
    back_callback: str | None = None,
    show_city_reviews: bool = False,
    region_name: str | None = None,
):
    # current_index is 0-based.
    # Display index is usually current_index + 1, OR handled by display_text
    
    buttons = []
    
    nav_row = []
    
    # Prev button (Left)
    if current_index > 0:
        nav_row.append(InlineKeyboardButton(text="⏮ Раньше", callback_data=f"rev_nav_{current_index-1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="·", callback_data="ignore"))
        
    # Counter in middle
    if display_text:
        count_display = display_text
    else:
        count_display = f"{current_index + 1}/{total_count}"
        
    nav_row.append(InlineKeyboardButton(text=count_display, callback_data="ignore"))
    
    # Next button (Right)
    if current_index < total_count - 1:
        nav_row.append(InlineKeyboardButton(text="⏭ Позже", callback_data=f"rev_nav_{current_index+1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="·", callback_data="ignore"))
        
    buttons.append(nav_row)
    
    if product_id:
        buttons.append([
            InlineKeyboardButton(text="🛒 Добавить в заказ", callback_data=f"prod_{product_id}"),
            InlineKeyboardButton(text="🎮 Вернуться в каталог", callback_data="rev_back_products"),
        ])
    else:
        if back_callback:
            buttons.append([InlineKeyboardButton(text="🎮 Вернуться в каталог", callback_data=back_callback)])
        if not city_filter:
            buttons.append([InlineKeyboardButton(text="Поиск города", callback_data="rev_search_start")])
    if show_city_reviews:
        buttons.append([
            InlineKeyboardButton(
                text=reviews_button_text(city_filter or "", region_name=region_name),
                callback_data="catalog_all_reviews",
            )
        ])
    
    buttons.append([InlineKeyboardButton(text=BTN_DISMISS, callback_data="nav_cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():

    # Импорт внутри функции чтобы избежать циклических импортов
    from app.services.settings import SettingsService
    is_redirect = SettingsService.is_redirect_active()
    toggle_text = "🔴 Вкл. редирект на Авто" if not is_redirect else "🟢 Выкл. редирект (Активен)"

    buttons = [
        [InlineKeyboardButton(text="🛒 Управление товарами (Каталоги)", callback_data="admin_catalog_search")],
        [InlineKeyboardButton(text="📦 База товаров (all_products)", callback_data="admin_products_global_menu")],
        [InlineKeyboardButton(text=" Генерация прайса: фильтр товаров", callback_data="admin_catalog_generation_settings")],
        [InlineKeyboardButton(text="🧺 Мини-опт: города", callback_data="admin_miniopt_search")],
        [InlineKeyboardButton(text="🧺 Мини-опт: база товаров", callback_data="admin_miniopt_products_global_menu")],
        [InlineKeyboardButton(text="🧺 Мини-опт: фильтр генерации", callback_data="admin_miniopt_generation_settings")],
        [InlineKeyboardButton(text="🧺 Типы кладов miniopt", callback_data="admin_miniopt_stash_types_menu")],
        [InlineKeyboardButton(text="💰 Управление наценками на районы", callback_data="admin_pricing_menu")],
        [InlineKeyboardButton(text="💎 Кошельки BTC / LTC", callback_data="admin_crypto_menu")],
        [InlineKeyboardButton(text="💸 Комиссия ручной оплаты", callback_data="admin_commission_menu")],
        [InlineKeyboardButton(text="💸 Комиссия Карта РФ", callback_data="admin_rf_commission_menu")],
        [InlineKeyboardButton(text="💸 Комиссия ЮMoney", callback_data="admin_ym_commission_menu")],
        [InlineKeyboardButton(text="🏷 Названия способов оплаты", callback_data="admin_payment_aliases_menu")],
        [InlineKeyboardButton(text="💳 Карты РФ (оплата)", callback_data="admin_rf_cards_menu")],
        [InlineKeyboardButton(text="💜 Кошельки ЮMoney", callback_data="admin_yoomoney_menu")],
        [InlineKeyboardButton(text="✅ Разбан Карта РФ", callback_data="admin_rf_unblock_menu")],
        [InlineKeyboardButton(text="🔗 Ссылка авто-оплаты", callback_data="admin_exchange_bot_menu")],
        [InlineKeyboardButton(text="💎 Экран долины", callback_data="admin_home_screen")],
        [InlineKeyboardButton(text="📞 Контакт поддержки", callback_data="admin_support_contact_menu")],
        [InlineKeyboardButton(text="⚖️ Шаблоны диспута", callback_data="adtpl_menu")],
        [InlineKeyboardButton(text="📇 Кнопка Контакты", callback_data="admin_contacts_text_menu")],
        [InlineKeyboardButton(text="📦 Прайс в меню", callback_data="admin_featured_product_menu")],
        [InlineKeyboardButton(text="🛒 Текст предзаказа", callback_data="admin_preorder_text_menu")],
        [InlineKeyboardButton(text="🎯 Миссии и серия", callback_data="adm_ms")],
        [InlineKeyboardButton(text="🏘 Управление районами города", callback_data="admin_districts_menu")],
        [InlineKeyboardButton(text="➕ Добавить город в базу", callback_data="admin_manual_city_start")],
        [InlineKeyboardButton(text=" Настройка отзывов", callback_data="admin_reviews_menu")],
        [InlineKeyboardButton(text="✉️ Шаблон ручной оплаты", callback_data="admin_manual_pay_template")],
        [InlineKeyboardButton(text="🏷 Типы кладов (глобально)", callback_data="admin_stash_types_menu")],
        [InlineKeyboardButton(text="📄 Выгрузка каталога в PDF", callback_data="admin_export_catalog_pdf")],
        [InlineKeyboardButton(text="📋 Активные заказы", callback_data="admin_active_orders")],
        [InlineKeyboardButton(text="📈 Интерес к товарам (просмотры)", callback_data="admin_product_interest_stats")],
            [InlineKeyboardButton(text="🏁 Гонка рефералов", callback_data="admin_race_menu")],
            [InlineKeyboardButton(text="🎰 Игры / розыгрыши", callback_data="admin_roulette_menu")],
        [InlineKeyboardButton(text=toggle_text, callback_data="admin_toggle_redirect")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⏰ Авторассылка", callback_data="admin_auto_broadcast")],
        [InlineKeyboardButton(text="👤 Найти пользователя", callback_data="admin_user_lookup")],
        [InlineKeyboardButton(text="📩 ЛС пользователю", callback_data="admin_dm")],
        [InlineKeyboardButton(text="🎟 Создать купон", callback_data="admin_coupon_create_start")],
        [InlineKeyboardButton(text="🧹 Очистить базу юзеров", callback_data="admin_users_clear_confirm")],
        [InlineKeyboardButton(text="📜 Список забаненных", callback_data="admin_list_bans")],
        [
            InlineKeyboardButton(text="🔨 Забанить ID", callback_data="admin_ban_user"), 
            InlineKeyboardButton(text="😇 Разбанить ID", callback_data="admin_unban_user")
        ],
        [InlineKeyboardButton(text="❗ Закрыть панель", callback_data="admin_close")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_catalog_generation_menu(all_products: list, selected_ids: set[str], optional_chance: int | None = None):
    return get_admin_generation_menu(
        all_products,
        selected_ids,
        optional_chance=optional_chance,
        select_all_callback="adm_cat_gen_select_all",
        reset_callback="adm_cat_gen_reset",
        toggle_prefix="adm_cat_gen_tog_",
        back_callback="admin_back_main",
        reset_label="↺ Сбросить группу (p1/p4)",
    )


def get_admin_generation_menu(
    all_products: list,
    selected_ids: set[str],
    optional_chance: int | None = None,
    select_all_callback: str = "adm_cat_gen_select_all",
    reset_callback: str = "adm_cat_gen_reset",
    toggle_prefix: str = "adm_cat_gen_tog_",
    back_callback: str = "admin_back_main",
    reset_label: str = "↺ Сбросить фильтр",
):
    chance_label = f"🎲 Шанс рандомных: {optional_chance}%" if isinstance(optional_chance, int) else "🎲 Шанс рандомных"
    buttons = [
        [InlineKeyboardButton(text=chance_label, callback_data="ignore")],
        [InlineKeyboardButton(text="✅ Выбрать все", callback_data=select_all_callback)],
        [InlineKeyboardButton(text=reset_label, callback_data=reset_callback)],
    ]

    for p in all_products:
        pid = p.get("id")
        if not pid:
            continue
        mark = "✅" if pid in selected_ids else "⬜️"
        name = p.get("name", pid)
        buttons.append([
            InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"{toggle_prefix}{pid}")
        ])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_crypto_menu(
    btc_wallets: list[str],
    ltc_wallets: list[str],
    btc_rate: float | None,
    ltc_rate: float | None,
    updated_at: str | None,
):
    def _rate_text(rate: float | None) -> str:
        if isinstance(rate, (int, float)) and rate > 0:
            return f"{rate:,.2f}".replace(",", " ")
        return "не получен"

    def _wallet_lines(wallets: list[str], symbol: str) -> list[str]:
        if not wallets:
            return [f"{symbol}: <i>не заданы</i>"]
        lines = [f"{symbol} ({len(wallets)} шт., клиенту выдаётся случайный):"]
        for idx, wallet in enumerate(wallets, start=1):
            lines.append(f"{idx}. <code>{html.escape(wallet)}</code>")
        return lines

    updated_line = f"🕒 Курс обновлён: {updated_at}" if updated_at else "🕒 Курс ещё не обновлялся"

    buttons: list[list[InlineKeyboardButton]] = []
    for idx, _wallet in enumerate(btc_wallets):
        buttons.append([
            InlineKeyboardButton(text=f"₿ #{idx + 1}", callback_data=f"admin_btc_wallet_noop_{idx}"),
            InlineKeyboardButton(text="❌", callback_data=f"admin_btc_wallet_del_{idx}"),
        ])
    for idx, _wallet in enumerate(ltc_wallets):
        buttons.append([
            InlineKeyboardButton(text=f"Ł #{idx + 1}", callback_data=f"admin_ltc_wallet_noop_{idx}"),
            InlineKeyboardButton(text="❌", callback_data=f"admin_ltc_wallet_del_{idx}"),
        ])
    buttons.extend([
        [InlineKeyboardButton(text="➕ BTC-кошелёк", callback_data="admin_btc_wallet_set")],
        [InlineKeyboardButton(text="➕ LTC-кошелёк", callback_data="admin_ltc_wallet_set")],
        [InlineKeyboardButton(text="🔄 Обновить курс", callback_data="admin_crypto_refresh")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ])
    text_lines = [
        "💎 <b>Оплата Crypto (BTC / LTC)</b>",
        "",
        *_wallet_lines(btc_wallets, "₿ BTC"),
        "",
        *_wallet_lines(ltc_wallets, "Ł LTC"),
        "",
        f"Курс 1 BTC: <b>{_rate_text(btc_rate)}</b> ₽",
        f"Курс 1 LTC: <b>{_rate_text(ltc_rate)}</b> ₽",
        updated_line,
        "",
        "Курс CoinGecko, кэш ~24ч. Клиенту: сумма в ₽ и в выбранной монете.",
    ]
    return "\n".join(text_lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_ltc_menu(rate: float | None, updated_at: str | None):
    from app.services.settings import SettingsService

    btc_rate, ltc_rate, stamp = SettingsService.get_crypto_rates()
    return get_admin_crypto_menu(
        SettingsService.get_btc_wallets(),
        SettingsService.get_ltc_wallets(),
        btc_rate,
        ltc_rate or rate,
        stamp or updated_at,
    )

def get_admin_reviews_menu():
    buttons = [
        [InlineKeyboardButton(text="✍️ Добавить MANUALLY", callback_data="adm_rev_manual_start")],
        [InlineKeyboardButton(text="🛡 Модерация отзывов", callback_data="adm_rev_mod_start")],
        [InlineKeyboardButton(text="🧪 Модерация сгенерированных", callback_data="adm_rev_gen_mod_start")],
        [InlineKeyboardButton(text="📤 Выгрузить JSON (последняя волна)", callback_data="adm_rev_gen_export_json")],
        [InlineKeyboardButton(text="📥 Загрузить JSON (текст/район)", callback_data="adm_rev_gen_import_json")],
        [InlineKeyboardButton(text="🎲 Генерировать (Все Города)", callback_data="adm_rev_gen_mass")],
        [InlineKeyboardButton(text="🗑 Удалить последнюю волну", callback_data="adm_rev_delete_last")],
        [InlineKeyboardButton(text="🔄 Кастомные фразы (AI)", callback_data="admin_rev_phrases_menu")],
        [InlineKeyboardButton(text="⚙️ Настройки авторассылки ✨", callback_data="adm_rev_settings")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_catalog_menu(city_name: str):
    buttons = [
        [InlineKeyboardButton(text="🔄 Пересоздать случайно", callback_data=f"adm_cat_reset_{city_name}")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data=f"adm_cat_add_menu_{city_name}")],
        [InlineKeyboardButton(text="✏️ Районы для товаров", callback_data=f"adm_pd_list_{city_name}")],
        [InlineKeyboardButton(text="🏷 Типы кладов", callback_data=f"adm_stash_menu_{city_name}")],
        [InlineKeyboardButton(text="➖ Удалить товар", callback_data=f"adm_cat_del_menu_{city_name}")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_menu(city_name: str):
    buttons = [
        [InlineKeyboardButton(text="🔄 Пересоздать mini-опт", callback_data=f"adm_miniopt_reset_{city_name}")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data=f"adm_miniopt_add_menu_{city_name}")],
        [InlineKeyboardButton(text="✏️ Районы для товаров", callback_data=f"adm_miniopt_pd_list_{city_name}")],
        [InlineKeyboardButton(text="🏷 Типы кладов", callback_data=f"adm_miniopt_stash_menu_{city_name}")],
        [InlineKeyboardButton(text="➖ Удалить товар", callback_data=f"adm_miniopt_del_menu_{city_name}")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_products_global_menu(all_products: list):
    count = len(all_products)
    buttons = [
        [InlineKeyboardButton(text=f"➕ Добавить товар (всего {count})", callback_data="admin_products_global_add")],
        [InlineKeyboardButton(text="➖ Удалить товар", callback_data="admin_products_global_del_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_products_global_menu(all_products: list):
    count = len(all_products)
    buttons = [
        [InlineKeyboardButton(text=f"➕ Добавить miniopt товар (всего {count})", callback_data="admin_miniopt_products_global_add")],
        [InlineKeyboardButton(text="✏️ Изменить miniopt товар", callback_data="admin_miniopt_products_global_edit_menu")],
        [InlineKeyboardButton(text="➖ Удалить miniopt товар", callback_data="admin_miniopt_products_global_del_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_products_global_delete_list(all_products: list):
    buttons = []
    for product in all_products:
        pid = product.get("id")
        name = product.get("name") or pid
        if not pid:
            continue
        buttons.append([InlineKeyboardButton(text=f"❗ {name}", callback_data=f"adm_glob_prod_del_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_products_global_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_products_global_delete_list(all_products: list):
    buttons = []
    for product in all_products:
        pid = product.get("id")
        name = product.get("name") or pid
        if not pid:
            continue
        buttons.append([InlineKeyboardButton(text=f"❗ {name}", callback_data=f"adm_glob_miniopt_del_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_miniopt_products_global_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_products_global_edit_list(all_products: list):
    buttons = []
    for product in all_products:
        pid = product.get("id")
        name = product.get("name") or pid
        price = product.get("base_price")
        if not pid:
            continue
        buttons.append([InlineKeyboardButton(text=f"✏️ {name} ({price}р)", callback_data=f"adm_glob_miniopt_edit_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_miniopt_products_global_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_add_product_list(city_name: str, existing_ids: list, all_products: list):
    buttons = []
    # Button to create new custom product
    buttons.append([InlineKeyboardButton(text="✨ Создать новый товар (Custom)", callback_data=f"adm_cat_new_{city_name}")])
    
    for p in all_products:
        if p['id'] not in existing_ids:
            buttons.append([InlineKeyboardButton(text=f"➕ {p['name']}", callback_data=f"adm_cat_add_{city_name}_{p['id']}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_add_miniopt_product_list(city_name: str, existing_ids: list, all_products: list):
    buttons = []
    for p in all_products:
        if p['id'] not in existing_ids:
            buttons.append([InlineKeyboardButton(text=f"➕ {p['name']}", callback_data=f"adm_miniopt_add_{city_name}_{p['id']}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_miniopt_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_del_product_list(city_name: str, current_products: list):
    buttons = []
    for p in current_products:
        buttons.append([InlineKeyboardButton(text=f"❗ {p['name']}", callback_data=f"adm_cat_del_{city_name}_{p['id']}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_del_miniopt_product_list(city_name: str, current_products: list):
    buttons = []
    for p in current_products:
        buttons.append([InlineKeyboardButton(text=f"❗ {p['name']}", callback_data=f"adm_miniopt_del_{city_name}_{p['id']}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_miniopt_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_stash_product_list(city_name: str, products: list):
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"🏷 {p['name']}", callback_data=f"adm_stash_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_stash_product_list(city_name: str, products: list):
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"🏷 {p['name']}", callback_data=f"adm_miniopt_stash_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_miniopt_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_stash_editor(city_name: str, product_id: str, all_types: list[str], selected: list[str]):
    buttons = [[InlineKeyboardButton(text="💾 Сохранить", callback_data=f"adm_stash_save_{city_name}&{product_id}")]]
    for idx, item in enumerate(all_types):
        mark = "✅" if item in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {item}", callback_data=f"adm_stash_toggle_{idx}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_stash_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_stash_editor(city_name: str, product_id: str, all_types: list[str], selected: list[str]):
    buttons = [[InlineKeyboardButton(text="💾 Сохранить", callback_data=f"adm_miniopt_stash_save_{city_name}&{product_id}")]]
    for idx, item in enumerate(all_types):
        mark = "✅" if item in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {item}", callback_data=f"adm_miniopt_stash_toggle_{idx}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_miniopt_stash_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_global_stash_menu(
    stash_types: list[str],
    delete_prefix: str = "adm_global_stash_del_",
    add_callback: str = "adm_global_stash_add",
    back_callback: str = "admin_back_main",
):
    buttons = []
    for idx, item in enumerate(stash_types):
        buttons.append([InlineKeyboardButton(text=f"❗ {item}", callback_data=f"{delete_prefix}{idx}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить тип", callback_data=add_callback)])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_districts_menu(city_name: str, districts: list):
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить районы", callback_data="adm_dist_add")],
        [InlineKeyboardButton(text="➖ Удалить район", callback_data="adm_dist_del_menu")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_del_district_list(city_name: str, districts: list):
    buttons = []
    for d in districts:
        # Use callback prefix adm_dist_del_do_CITY_DISTRICT
        # If district has spaces, it might break simple splitting. 
        # But we can reconstruct city name if we know parts logic.
        # Or even better, use an index if we stored index, but we don't.
        # Let's hope city_name doesn't contain crazy chars.
        # We will handle splitting carefully in handler.
        buttons.append([InlineKeyboardButton(text=f"❗ {d}", callback_data=f"adm_dist_del_do_{city_name}&{d}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_dist_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_product_districts_selector(city_name: str, product_id: str, all_districts: list, selected_districts: list):
    buttons = []
    # Button to Save
    buttons.append([InlineKeyboardButton(text="💾 СОХРАНИТЬ", callback_data=f"adm_pd_save_{city_name}&{product_id}")])
    
    # Toggle "Select All" / "Clear All" logic is hard in pure inline, usually just per item
    
    for idx, d in enumerate(all_districts):
        is_sel = d in selected_districts
        icon = "✅" if is_sel else "⬜️"
        # Toggle callback optimized: adm_pd_tog_{INDEX}
        # We rely on FSM to map index back to district
        cb = f"adm_pd_tog_{idx}"
        buttons.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=cb)])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_pd_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_miniopt_product_districts_selector(city_name: str, product_id: str, all_districts: list, selected_districts: list):
    buttons = []
    buttons.append([InlineKeyboardButton(text="💾 СОХРАНИТЬ", callback_data=f"adm_miniopt_pd_save_{city_name}&{product_id}")])

    for idx, d in enumerate(all_districts):
        is_sel = d in selected_districts
        icon = "✅" if is_sel else "⬜️"
        cb = f"adm_pd_tog_{idx}"
        buttons.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=cb)])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_miniopt_pd_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_reviews_main_menu():
    buttons = [
        [InlineKeyboardButton(text="✍️ Добавить вручную", callback_data="adm_rev_manual_start")],
        [InlineKeyboardButton(text="💣 Генерация (Тест)", callback_data="adm_rev_gen_start")],
        [InlineKeyboardButton(text="⚙️ Настройки авторассылки ✨", callback_data="adm_rev_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_reviews_settings_menu(settings):
    # settings = {interval_hours, min_reviews, max_reviews}
    interval = settings.get('interval_hours', 12)
    min_r = settings.get('min_reviews', 5)
    max_r = settings.get('max_reviews', 10)
    
    buttons = [
        [InlineKeyboardButton(text=f"⏳ Интервал: {interval} ч.", callback_data="adm_rev_set_interval")],
        [InlineKeyboardButton(text=f"🔢 Мин. кол-во: {min_r}", callback_data="adm_rev_set_min")],
        [InlineKeyboardButton(text=f"🔢 Макс. кол-во: {max_r}", callback_data="adm_rev_set_max")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_roulette_game_keyboard(
    game_id: int,
    is_active: bool = True,
    *,
    joined: bool = False,
    participants_count: int = 0,
    max_participants: int = 0,
    list_visible: bool = False,
    result_announced: bool = False,
):
    rows = []
    count_label = f"{int(participants_count)}/{int(max_participants)}" if int(max_participants) > 0 else str(int(participants_count))
    if is_active:
        join_text = f"В игре {count_label}" if joined else f"Вход {count_label}"
        rows.append([InlineKeyboardButton(text=join_text, callback_data=f"roul_join_{game_id}")])
    if joined:
        if list_visible:
            rows.append([InlineKeyboardButton(text="Скрыть состав", callback_data=f"roul_hide_{game_id}")])
        else:
            rows.append([InlineKeyboardButton(text="Состав", callback_data=f"roul_list_{game_id}")])
    if result_announced:
        rows.append([InlineKeyboardButton(text="Итог", callback_data=f"roul_result_{game_id}")])
    rows.append([InlineKeyboardButton(text="Обновить", callback_data="roulette_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_roulette_results_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Меню", callback_data="nav_cancel")],
        ]
    )


def get_admin_roulette_menu_keyboard(has_games: bool = True):
    buttons = [[InlineKeyboardButton(text="➕ Создать игру", callback_data="admin_roulette_create_start")]]
    if has_games:
        buttons.append([InlineKeyboardButton(text="🎛 Управление играми", callback_data="admin_roulette_games")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_roulette_games_keyboard(games: list[dict]):
    buttons = []
    for game in games:
        status = "🟢" if game.get("status") == "active" else "⚪️"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{status} #{game.get('id')} {(game.get('title') or 'Игра')[:28]}",
                    callback_data=f"admin_roulette_game_{game.get('id')}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_roulette_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_roulette_manage_keyboard(game_id: int, is_active: bool, result_announced: bool = False):
    toggle_text = "⏹ Закрыть набор" if is_active else "▶️ Открыть набор"
    toggle_cb = "admin_roulette_close_" + str(game_id) if is_active else "admin_roulette_reopen_" + str(game_id)
    rows = [
        [InlineKeyboardButton(text="👥 Участники", callback_data=f"admin_roulette_participants_{game_id}")],
        [InlineKeyboardButton(text="🏆 Назначить победителей (секретно)", callback_data=f"admin_roulette_set_winners_{game_id}")],
    ]
    if not result_announced:
        rows.append([InlineKeyboardButton(text="🚀 Запуск розыгрыша", callback_data=f"admin_roulette_force_draw_{game_id}")])
    rows.extend(
        [
            [InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
            [InlineKeyboardButton(text="🗑 Удалить игру", callback_data=f"admin_roulette_del_req_{game_id}")],
            [InlineKeyboardButton(text="🔙 К списку игр", callback_data="admin_roulette_games")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_race_keyboard(running: bool, real_rows: list | None = None):
    rows = []
    if running:
        rows.append([InlineKeyboardButton(text="⏹ Остановить гонку", callback_data="admin_race_stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Старт на 48 часов", callback_data="admin_race_start")])
    for item in (real_rows or [])[:12]:
        uid = item.get("id")
        rows.append(
            [
                InlineKeyboardButton(text=f"🥇 {uid}", callback_data=f"arw1_{uid}"),
                InlineKeyboardButton(text="🥈", callback_data=f"arw2_{uid}"),
                InlineKeyboardButton(text="🥉", callback_data=f"arw3_{uid}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_user_keyboard(user_id: int, has_discount: bool = False):
    rows = [
        [InlineKeyboardButton(text="📉 Выдать скидку", callback_data=f"admin_udisc_{user_id}")],
    ]
    if has_discount:
        rows.append(
            [InlineKeyboardButton(text="🗑 Снять скидку", callback_data=f"admin_udisc_clear_{user_id}")]
        )
    rows.append([InlineKeyboardButton(text="📩 Написать", callback_data=f"admin_chat_{user_id}")])
    rows.append([InlineKeyboardButton(text="🔎 Другой пользователь", callback_data="admin_user_lookup")])
    rows.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

