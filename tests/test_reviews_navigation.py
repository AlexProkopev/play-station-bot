import asyncio
from unittest.mock import AsyncMock, patch

from app.handlers import user_flow
from app.keyboards import get_all_cities_reviews_pagination_kb, get_reviews_pagination_kb
from app.states import OrderState


class DummyState:
    def __init__(self, initial_data):
        self.data = dict(initial_data)
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value):
        self.state = value


def test_reviews_keyboard_shows_back_button_for_all_city_mode():
    markup = get_reviews_pagination_kb(
        current_index=0,
        total_count=5,
        city_filter="Томск",
        back_callback="rev_back_products",
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}
    labels = {button.text for button in buttons}

    assert "rev_back_products" in callbacks
    assert "🎮 Вернуться в каталог" in labels
    assert "rev_search_reset" not in callbacks


def test_reviews_keyboard_keeps_product_actions():
    markup = get_reviews_pagination_kb(
        current_index=1,
        total_count=5,
        city_filter="Томск",
        product_id="p1",
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}

    assert "prod_p1" in callbacks
    assert "rev_back_products" in callbacks
    assert "rev_back_from_all" not in callbacks


def test_reviews_keyboard_hides_reset_button_for_city_filter():
    markup = get_reviews_pagination_kb(
        current_index=0,
        total_count=5,
        city_filter="Томск",
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}

    assert "rev_search_reset" not in callbacks
    assert "rev_search_start" not in callbacks


def test_restore_after_all_city_reviews_returns_to_catalog():
    state = DummyState(
        {
            "city": "Томск",
            "population": 120000,
            "rev_all_city_mode": True,
            "rev_return_city": "Томск",
            "rev_return_product": "Альфа 1г",
            "rev_return_product_id": "p1",
            "rev_return_index": 2,
        }
    )
    send_method = AsyncMock()

    with patch.object(user_flow, "_build_catalog_text", AsyncMock(return_value="catalog")) as build_catalog_text, patch.object(
        user_flow,
        "_show_review_page",
        AsyncMock(),
    ) as show_review_page:
        asyncio.run(user_flow._restore_after_all_city_reviews(send_method, state))

    build_catalog_text.assert_awaited_once_with(
        state,
        "Томск",
        120000,
        header="💎 <b>В магазин · Томск</b>",
    )
    show_review_page.assert_not_called()
    send_method.assert_awaited_once()
    assert state.data["rev_search_city"] is None
    assert state.data["rev_search_product"] is None
    assert state.data["rev_search_product_id"] is None
    assert state.data["rev_all_city_mode"] is False
    assert state.state == OrderState.choosing_product


def test_restore_after_all_city_reviews_returns_to_catalog_without_product_context():
    state = DummyState(
        {
            "city": "Томск",
            "population": 120000,
            "rev_all_city_mode": True,
            "rev_return_city": "Томск",
            "rev_return_product": None,
            "rev_return_product_id": None,
            "rev_return_index": 3,
        }
    )
    send_method = AsyncMock()

    with patch.object(user_flow, "_show_review_page", AsyncMock()) as show_review_page, patch.object(
        user_flow,
        "_build_catalog_text",
        AsyncMock(return_value="catalog"),
    ) as build_catalog_text:
        asyncio.run(user_flow._restore_after_all_city_reviews(send_method, state))

    build_catalog_text.assert_awaited_once_with(
        state,
        "Томск",
        120000,
        header="💎 <b>В магазин · Томск</b>",
    )
    show_review_page.assert_not_called()
    assert state.data["rev_search_city"] is None
    assert state.data["rev_search_product"] is None
    assert state.data["rev_search_product_id"] is None
    assert state.data["rev_all_city_mode"] is False
    assert state.state == OrderState.choosing_product


def test_all_cities_reviews_keyboard_has_prev_and_next_callbacks():
    markup = get_all_cities_reviews_pagination_kb(current_page=1, total_pages=3)
    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}
    labels = {button.text for button in buttons}

    assert "rev_all_nav_0" in callbacks
    assert "rev_all_nav_2" in callbacks
    assert any("Закрыть" in label for label in labels)


def test_show_all_cities_reviews_page_shows_two_reviews_per_page():
    state = DummyState({})
    send_method = AsyncMock()
    reviews = [
        {
            "stars": 5,
            "user": f"User{i}",
            "display_date": "24.07 10:00",
            "item_info": f"Товар {i} | Город {i}",
            "text": f"Отзыв {i}",
        }
        for i in range(1, 5)
    ]

    with patch.object(user_flow.ReviewsService, "get_filtered_reviews", return_value=reviews):
        asyncio.run(user_flow._show_all_cities_reviews_page(send_method, 0, state))

    send_method.assert_awaited_once()
    sent_text = send_method.await_args.args[0]
    assert "Отзывы" in sent_text
    assert "После покупки" in sent_text
    assert "в течение суток" in sent_text
    assert "Скрыто" in sent_text
    assert "Пользователь скрыт" not in sent_text
    assert "User1" not in sent_text
    assert "User2" not in sent_text
    assert "<b>01</b>" in sent_text
    assert "<b>02</b>" in sent_text
    assert "<b>03</b>" not in sent_text
    assert state.data["rev_all_cities_mode"] is True
    assert state.data["rev_all_cities_page"] == 0


def test_public_review_card_always_hides_author():
    from app.services.reviews import ReviewsService

    card = ReviewsService.format_public_review_card(
        {
            "stars": 5,
            "user": "ivan_petrov",
            "display_date": "19.09 05:44",
            "item_info": "Мёд 0.5г | Томск",
            "text": "всё ок",
        },
        index=1,
    )
    assert "👤 <b>Скрыто</b>" in card
    assert "ivan_petrov" not in card
    assert "19.09 05:44" in card