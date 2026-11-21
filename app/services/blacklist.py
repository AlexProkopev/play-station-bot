import json
import os
from app.services.postgres_store import PostgresDocumentStore

BLACKLIST_FILE = "storage/blacklist.json"
BLACKLIST_DOC_KEY = "blacklist"

class BlacklistService:
    _spam_counters = {}
    _payment_abuse_counters = {} # Отдельный счетчик для отмен оплат
    _banned_list = None
    _banned_set = None

    @staticmethod
    def _set_cache(banned_ids):
        ids = banned_ids if isinstance(banned_ids, list) else []
        BlacklistService._banned_list = ids
        BlacklistService._banned_set = set(ids)

    @staticmethod
    def load_blacklist():
        if BlacklistService._banned_list is not None:
            return BlacklistService._banned_list
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(BLACKLIST_DOC_KEY, [])
            BlacklistService._set_cache(data if isinstance(data, list) else [])
            return BlacklistService._banned_list
        if not os.path.exists(BLACKLIST_FILE):
            BlacklistService._set_cache([])
            return BlacklistService._banned_list
        try:
            with open(BLACKLIST_FILE, 'r') as f:
                data = json.load(f)
            BlacklistService._set_cache(data if isinstance(data, list) else [])
        except Exception:
            BlacklistService._set_cache([])
        return BlacklistService._banned_list

    @staticmethod
    def save_blacklist(banned_ids):
        BlacklistService._set_cache(banned_ids)
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(BLACKLIST_DOC_KEY, banned_ids)
            return
        with open(BLACKLIST_FILE, 'w') as f:
            json.dump(banned_ids, f)

    @staticmethod
    def is_banned(user_id):
        BlacklistService.load_blacklist()
        return user_id in BlacklistService._banned_set

    @staticmethod
    def ban_user(user_id):
        banned = BlacklistService.load_blacklist()
        if user_id not in banned:
            banned.append(user_id)
            BlacklistService.save_blacklist(banned)
            # Очищаем счетчики
            if user_id in BlacklistService._spam_counters:
                del BlacklistService._spam_counters[user_id]
            if user_id in BlacklistService._payment_abuse_counters:
                del BlacklistService._payment_abuse_counters[user_id]
            return True
        return False

    @staticmethod
    def unban_user(user_id):
        banned = BlacklistService.load_blacklist()
        if user_id in banned:
            banned.remove(user_id)
            BlacklistService.save_blacklist(banned)
            # Очищаем счетчики при разбане, чтобы дать шанс
            if user_id in BlacklistService._payment_abuse_counters:
                del BlacklistService._payment_abuse_counters[user_id]
            return True
        return False

    @staticmethod
    def report_bad_input(user_id):
        """
        Увеличивает счетчик ошибок города. Если ошибок >= 3, возвращает True (пора банить).
        """
        count = BlacklistService._spam_counters.get(user_id, 0) + 1
        BlacklistService._spam_counters[user_id] = count
        
        if count >= 5: # Увеличим порог для городов, т.к. люди могут просто ошибаться
            BlacklistService.ban_user(user_id)
            return True
        return False

    @staticmethod
    def report_payment_abuse(user_id):
        """
        Увеличивает счетчик злоупотребления отменой оплаты.
        Возвращает кортеж: (нужен_ли_бан, текст_предупреждения или None)
        """
        count = BlacklistService._payment_abuse_counters.get(user_id, 0) + 1
        BlacklistService._payment_abuse_counters[user_id] = count
        
        # Логика:
        # 1 раз - просто счетчик
        # 2 раза - последнее предупреждение
        # 3 раза - бан
        
        if count == 2:
            return False, "🔶 <b>Последнее предупреждение Store</b>\nВы слишком часто закрываете оплату.\nЕще одна отмена, и <b>доступ закроется</b> автоматически."
            
        if count >= 3:
            BlacklistService.ban_user(user_id)
            return True, "🔒 <b>Доступ в Store закрыт.</b>\nКасса зафиксировала слишком много запросов реквизитов без оплаты."
            
        return False, None
