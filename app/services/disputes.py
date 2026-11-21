import html

TELEGRAM_MESSAGE_LIMIT = 4096

KIND_UNDERWEIGHT = "underweight"
KIND_LOST = "lost"
KIND_EMPTY = "empty"

KIND_TITLES = {
    KIND_UNDERWEIGHT: "⚖️ Вес меньше заявленного",
    KIND_LOST: "🧭 Точку не нахожу",
    KIND_EMPTY: "📦 На точке ничего нет",
}

UNDERWEIGHT_STEPS = (
    {
        "id": "stash_sealed",
        "emoji": "📦",
        "title": "Заказ на весах, упаковка целая",
        "hint": "Снимите заказ на весах в той упаковке, в которой его забрали. Не вскрывайте.",
    },
    {
        "id": "packaging",
        "emoji": "🛍️",
        "title": "Каждая часть упаковки",
        "hint": "Взвесьте каждую часть упаковки отдельно и пришлите фото.",
    },
    {
        "id": "product_open",
        "emoji": "🌿",
        "title": "Позиция без упаковки",
        "hint": "Положите содержимое на весы уже без упаковки и снимите.",
    },
    {
        "id": "coin_front",
        "emoji": "🪙",
        "title": "Монета 1 ₽, лицевая сторона",
        "hint": "Положите монету 1 рубль на весы. Год на фото должен читаться.",
    },
    {
        "id": "coin_back",
        "emoji": "🪙",
        "title": "Монета 1 ₽, оборот",
        "hint": "Переверните ту же монету на тех же весах. Год на обороте тоже должен читаться.",
    },
)

LOCATION_QUESTIONS = (
    {
        "id": "arrival_time",
        "emoji": "⏰",
        "title": "Время прибытия",
        "hint": "Напишите, во сколько вы были на точке.",
    },
    {
        "id": "search_process",
        "emoji": "🔎",
        "title": "Процесс поиска",
        "hint": "Опишите, как искали заказ и сколько минут это заняло.",
    },
    {
        "id": "place_look",
        "emoji": "🌄",
        "title": "Как выглядело место",
        "hint": "Опишите, как выглядела точка. Были ли чужие следы?",
    },
    {
        "id": "why_not_found",
        "emoji": "🤔",
        "title": "Почему точка пустая",
        "hint": "Напишите, почему заказ на этой точке не нашелся.",
    },
)

LOCATION_PHOTO_STEPS = (
    {
        "id": "place_far",
        "emoji": "📷",
        "title": "Дальний кадр точки",
        "hint": "Снимите местность с того ракурса, что в описании. Дальний план, без смаза.",
    },
    {
        "id": "place_near",
        "emoji": "🔍",
        "title": "Ближний кадр точки",
        "hint": "Тот же ракурс, но ближе к месту, где должен лежать заказ.",
    },
)


def build_shop_rules_text(support_contact: str) -> str:
    contact = (support_contact or "").strip() or "@support"
    return (
        "🔵 <b>Условия PlayStation™Store</b>\n\n"
        "🕐 Поддержка 11:00–20:00 МСК. Отвечаем не реже раза в 4 ч (в среднем 1–3 ч).\n"
        "Пишите по делу; флуд, пустой текст и одно предложение в несколько сообщений — в конец очереди.\n\n"
        "⚖️ <b>Диспуты и перезаклады</b>\n"
        "• 24 ч с покупки на открытие диспута и данные — иначе претензии не рассматриваем.\n"
        "• Решение по репутации аккаунта, на усмотрение магазина.\n"
        "• Неверные координаты — дадим уточнения.\n"
        "• Нет ответа в диспуте 24 ч — вопрос считаем решённым.\n"
        "• Диспут не по правилам — отказ в решении.\n"
        "• Перезаклад или возврат — только если причина на стороне магазина.\n\n"
        "🚫 <b>Откажем в перезакладе</b>\n"
        "Нет нужных фото; мало успешных покупок; мало покупок у нас; средний чек сильно ниже этой покупки; "
        "передача заказа третьим лицам; поздно сообщили; флуд; сообщения без смысла; оскорбления, шантаж, угрозы; умоляния.\n\n"
        "🧭 <b>Заказ не найден</b>\n"
        "Развёрнуто опишите поиск. 4+ дневных фото с разных ракурсов: общий план и место клада, как на фото в заказе. "
        "Каждый выезд на поиск — новые снимки с места.\n\n"
        "📍 <b>Не можете найти место</b>\n"
        "6 фото по координатам курьера (Solocator, GPS Camera 55 или аналог): кадры с перекрытием, координаты на снимке. "
        "Диспут, описание проблемы и фото.\n\n"
        "⚖️ <b>Недовес / количество</b>\n"
        "До вскрытия — на весах, переворачивайте клад с разных сторон. "
        "Затем: без упаковки; с монетой на весах; отдельно взвешенная монета.\n\n"
        "🧪 <b>Качество</b>\n"
        "Фото товара на зеркале, макро, с разных ракурсов.\n\n"
        "📦 <b>Предзаказы</b>\n"
        "BTC −3% и более — магазин вправе отменить сделку; доплата разницы — продолжить (не на моментальные позиции). "
        "При росте курса — компенсация по запросу. Курс на выдаче; компенсация — не позднее 24 ч с выдачи.\n\n"
        f"🔶 Диспут — кнопкой ниже. Личные сообщения: {html.escape(contact)}"
    )


def can_open_dispute(purchases_count) -> bool:
    try:
        return int(purchases_count or 0) >= 1
    except (TypeError, ValueError):
        return False


def get_user_purchase_count(user_id: int) -> int:
    from app.services.users import UserService

    user = UserService.get_user(user_id)
    try:
        return max(0, int((user or {}).get("purchases_count") or 0))
    except (TypeError, ValueError):
        return 0


def user_can_open_dispute(user_id: int) -> bool:
    return can_open_dispute(get_user_purchase_count(user_id))


def build_dispute_start_text() -> str:
    return (
        "🛠 <b>Проблема с заказом</b>\n\n"
        "Выберите причину. Дальше бот попросит фото и короткие ответы.\n"
        "В поддержку отдельно писать не нужно: решение придет в этот чат."
    )


def build_dispute_need_purchase_text() -> str:
    return (
        "🛠 <b>Обращение пока закрыто</b>\n\n"
        "Не вижу у Вас <b>активного заказа</b> в PlayStation™Store.\n"
        "Срочный вопрос можно написать в поддержку."
    )


def build_underweight_step_text(step_index: int) -> str:
    total = len(UNDERWEIGHT_STEPS)
    step = UNDERWEIGHT_STEPS[step_index]
    return (
        f"{step['emoji']} <b>Вес • кадр {step_index + 1} из {total}</b>\n\n"
        f"<b>{step['title']}</b>\n"
        f"{step['hint']}\n\n"
        "👇 Нажмите «Приложить фото» и пришлите снимок в этот чат."
    )


def build_location_intro_text(kind: str) -> str:
    title = KIND_TITLES.get(kind, "Диспут")
    return (
        f"{title}\n\n"
        "🧭 Сначала проверьте точку еще раз:\n\n"
        "⏺️ перечитайте описание заказа и фото\n"
        "⏺️ сопоставьте кадры с местностью и найдите ориентиры\n"
        "⏺️ осмотрите точку и зону рядом: заказ может быть небольшим\n\n"
        "Если после этого заказ не нашелся, заполним анкету по шагам."
    )


def build_location_question_text(step_index: int) -> str:
    total = len(LOCATION_QUESTIONS)
    question = LOCATION_QUESTIONS[step_index]
    return (
        f"{question['emoji']} <b>Анкета Store • вопрос {step_index + 1} из {total}</b>\n\n"
        f"<b>{question['title']}</b>\n"
        f"{question['hint']}\n\n"
        "Напишите ответ одним сообщением."
    )


def build_location_photo_step_text(step_index: int) -> str:
    total = len(LOCATION_PHOTO_STEPS)
    step = LOCATION_PHOTO_STEPS[step_index]
    return (
        f"{step['emoji']} <b>Кадр точки • {step_index + 1} из {total}</b>\n\n"
        f"<b>{step['title']}</b>\n"
        f"{step['hint']}\n\n"
        "👇 Нажмите «Приложить фото» и пришлите снимок в этот чат."
    )


def build_underweight_ready_text() -> str:
    return (
        "✅ <b>Кадры собраны</b>\n\n"
        "Можно отправить обращение как есть или добавить сообщение куратору.\n"
        "После отправки отдельный чат не открывается: решение придет сюда."
    )


def _format_answers_block(answers: list[dict]) -> str:
    lines: list[str] = []
    for index, item in enumerate(answers, start=1):
        title = html.escape(str(item.get("title") or f"Вопрос {index}"))
        text = html.escape(str(item.get("text") or "-"))
        lines.append(f"{index}) <b>{title}</b>\n{text}")
    return "\n\n".join(lines) if lines else "-"


def _format_photos_block(photos: list[dict]) -> str:
    if not photos:
        return "нет"
    lines = []
    for index, item in enumerate(photos, start=1):
        title = html.escape(str(item.get("title") or f"Фото {index}"))
        lines.append(f"{index}. {title}")
    return "\n".join(lines)


def build_location_preview_text(kind: str, answers: list[dict], photos: list[dict]) -> str:
    title = KIND_TITLES.get(kind, "Диспут")
    return (
        f"📋 <b>Проверьте анкету</b>\n"
        f"{title}\n\n"
        f"{_format_answers_block(answers)}\n\n"
        f"📷 <b>Фото:</b>\n{_format_photos_block(photos)}\n\n"
        "Отправить куратору Store или закрыть анкету?"
    )


def build_user_ticket_copy(
    kind: str,
    answers: list[dict],
    photos: list[dict],
    comment: str = "",
) -> str:
    title = KIND_TITLES.get(kind, "Диспут")
    parts = [f"✅ <b>Обращение ушло куратору Store</b>\n{title}"]
    if answers:
        parts.append(_format_answers_block(answers))
    if photos:
        parts.append(f"📷 <b>Фото:</b> {len(photos)} шт.")
    comment = (comment or "").strip()
    if comment:
        parts.append(f"💬 <b>Сообщение:</b>\n{html.escape(comment)}")
    parts.append(
        "Куратор Store посмотрит анкету и фото. При необходимости свяжется с курьером.\n"
        "Решение придет в этот чат. Отдельный диалог открывать не нужно."
    )
    return "\n\n".join(parts)


def build_admin_ticket_text(
    user_id: int,
    username: str | None,
    kind: str,
    answers: list[dict],
    photos: list[dict],
    comment: str = "",
) -> str:
    title = KIND_TITLES.get(kind, "Диспут")
    uname = f"@{username}" if username else "без username"
    parts = [
        "🆘 <b>Новый диспут</b>",
        f"👤 Клиент: <code>{user_id}</code> ({html.escape(uname)})",
        f"📋 Тип: {title}",
    ]
    if answers:
        parts.append("📝 <b>Анкета</b>\n" + _format_answers_block(answers))
    if photos:
        parts.append(f"📷 <b>Фото ({len(photos)}):</b>\n{_format_photos_block(photos)}")
    comment = (comment or "").strip()
    if comment:
        parts.append(f"💬 <b>Сообщение клиента:</b>\n{html.escape(comment)}")
    parts.append("👇 Нажмите «Вынести решение» и напишите ответ - он уйдёт клиенту без открытия чата.")
    return "\n\n".join(parts)


def build_user_verdict_text(decision_html_escaped: str) -> str:
    return (
        "⚖️ <b>Решение по обращению в Store</b>\n\n"
        f"{decision_html_escaped}"
    )
