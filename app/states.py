from aiogram.fsm.state import State, StatesGroup

class OrderState(StatesGroup):
    waiting_for_captcha = State() # Ожидание прохождения капчи
    main_menu = State()
    waiting_for_city = State()
    choosing_city_option = State() # Выбор города из списка
    choosing_product = State()
    choosing_district = State()
    choosing_treasure_type = State()
    choosing_payment = State()
    waiting_for_checkout_coupon = State()
    waiting_for_checkout_coupon_code = State()
    waiting_for_spin_confirm = State()
    
    # Режим чата с поддержкой:
    chat_with_support = State()  # Пользователь ждет ответа от админа
    
    waiting_for_receipt = State()  # Для ручной оплаты (скриншот)
    waiting_for_exchange_order_id = State() # Для ввода номера заявки авто-обмена

class TopUpState(StatesGroup):
    waiting_for_amount = State()
    choosing_method = State()
    waiting_for_receipt = State()


class CouponState(StatesGroup):
    waiting_for_code = State()

class ReviewState(StatesGroup):
    waiting_for_text = State()
    waiting_for_search_city = State()
    browsing_reviews = State()


class DisputeState(StatesGroup):
    choosing_kind = State()
    waiting_photo = State()
    waiting_comment = State()
    underweight_ready = State()
    location_intro = State()
    waiting_answer = State()
    location_ready = State()
